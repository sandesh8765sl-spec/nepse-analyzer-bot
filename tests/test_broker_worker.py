"""The automatic broker-history download: pauses, resumes, no duplicate or missing days, completion."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nepse_bot.db.store import Database
from nepse_bot.ingest import broker_backfill as backfill
from nepse_bot.ingest import broker_worker
from nepse_bot.ingest.broker_worker import DONE, STUDY_DONE, BrokerWorker, status_line
from nepse_bot.sources.chukul import SOURCE, ChukulClient, DownloadInterrupted
from nepse_bot.sources.http import SourceError

DAYS = ("2026-11-03", "2026-11-04", "2026-11-05")
EVENING = datetime(2026, 11, 20, 20, 0)      # long after the test days, outside every pause window


def _trade(number: int, quantity: float = 100, buyer: str = "58", seller: str = "34") -> dict[str, Any]:
    return {"transaction": str(number), "symbol": "NABIL", "buyer": buyer, "seller": seller, "quantity": quantity, "amount": quantity * 500}


FULL_DAY = [_trade(1), _trade(2, quantity=50)]      # 150 shares = the whole volume of the price table


class _Client:
    def __init__(self, days: dict[str, Any] | None = None) -> None:
        self.days, self.asked = days or {}, []

    def floorsheet_day(self, day: str) -> list[dict[str, Any]]:
        self.asked.append(day)
        answer = self.days.get(day, FULL_DAY)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Calendar:
    def __init__(self, trading: bool = True, market_open: bool = False) -> None:
        self.trading, self.market_open = trading, market_open

    def is_trading_day(self, day: Any = None) -> bool:
        return self.trading

    def is_market_open(self, moment: Any = None) -> bool:
        return self.market_open


class _Bench:
    """A worker with everything around it replaced by something the test controls."""

    def __init__(self, db: Database, client: _Client | None = None, calendar: _Calendar | None = None, now: datetime = EVENING) -> None:
        self.db, self.client, self.calendar = db, client or _Client(), calendar or _Calendar()
        self.now, self.is_online, self.free, self.messages, self.studies = now, True, 20_000.0, [], 0
        self.worker = self.new_worker()

    def _study(self, db: Any) -> str:
        self.studies += 1
        return "STUDY RESULT"

    def new_worker(self) -> BrokerWorker:
        """What a restart of the bot does: a fresh worker on the same database."""
        self.worker = BrokerWorker(db=self.db, client=self.client, calendar=self.calendar, notify=self.messages.append,
                                   online=lambda: self.is_online, clock=lambda: self.now, study=self._study, free_mb=lambda path: self.free)
        return self.worker

    def run(self, steps: int = 10) -> None:
        for _ in range(steps):
            self.worker.step()


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_prices([{"symbol": "NABIL", "date": day, "close": 500, "volume": 150} for day in DAYS], "test")
    return database


def _saved(db: Database) -> dict[str, float]:
    return {row["date"]: row["bought"] for row in db.query("SELECT date, SUM(buy_qty) AS bought FROM broker_flow_daily GROUP BY date")}


def test_continues_after_a_restart_without_duplicate_or_missing_days(db: Database) -> None:
    bench = _Bench(db)
    bench.worker.step()                                   # one day, then the PC is switched off
    assert bench.client.asked == ["2026-11-05"]
    bench.new_worker()
    bench.run()
    assert sorted(bench.client.asked) == sorted(DAYS)     # every day exactly once
    assert _saved(db) == {day: 150 for day in DAYS}
    assert bench.worker.state == "complete"


def test_completion_message_and_study_happen_once(db: Database) -> None:
    bench = _Bench(db)
    bench.run()
    assert len(bench.messages) == 2 and "complete" in bench.messages[0] and bench.messages[1] == "STUDY RESULT"
    assert db.get_setting(DONE) and db.get_setting(STUDY_DONE)
    bench.new_worker()
    bench.run()
    assert len(bench.messages) == 2 and bench.studies == 1


def test_study_waits_for_a_quiet_hour_and_is_retried_after_an_error(db: Database) -> None:
    bench = _Bench(db, now=datetime(2026, 11, 20, 11, 0))            # a trading day, during the session
    bench.run()
    assert bench.studies == 0 and len(bench.messages) == 1          # only the "download complete" message
    bench.now = datetime(2026, 11, 20, 19, 0)
    bench.worker.study = lambda database: (_ for _ in ()).throw(RuntimeError("out of memory"))
    bench.run(2)
    assert "error" in bench.messages[-1] and not db.get_setting(STUDY_DONE)
    bench.worker.study = bench._study
    bench.run(2)
    assert bench.studies == 0                                         # not twice on the same day
    bench.now += timedelta(days=1)
    bench.run(2)
    assert bench.studies == 1 and bench.messages[-1] == "STUDY RESULT"


def test_waits_without_internet_and_continues_when_it_is_back(db: Database) -> None:
    bench = _Bench(db)
    bench.is_online = False
    wait = bench.worker.step()
    assert bench.worker.state == "paused" and "no internet" in bench.worker.reason and wait == 180
    assert bench.client.asked == []
    bench.is_online = True
    bench.worker.step()                                   # still inside the 3 minutes: not asked again yet
    assert bench.client.asked == []
    bench.now += timedelta(minutes=3)
    bench.run()
    assert _saved(db) == {day: 150 for day in DAYS}


def test_internet_lost_in_the_middle_of_a_day_leaves_no_trace(db: Database) -> None:
    client = _Client({"2026-11-05": SourceError("connection reset")})
    bench = _Bench(db, client)
    bench.worker.step()
    bench.is_online = False
    client.days["2026-11-04"] = SourceError("connection reset")
    bench.now += timedelta(minutes=5)
    bench.worker.step()                                   # fails, and the internet turns out to be gone
    assert db.scalar("SELECT COUNT(*) FROM source_runs WHERE date = '2026-11-04'") == 0
    bench.worker.step()
    assert "no internet" in bench.worker.reason
    assert "2026-11-04" in backfill.missing_days(db, "2026-01-01")


def test_a_failed_day_does_not_block_the_others_and_is_tried_again_later(db: Database) -> None:
    client = _Client({"2026-11-05": SourceError("site error")})
    bench = _Bench(db, client)
    bench.run(6)
    assert client.asked.count("2026-11-05") == 1 and set(_saved(db)) == {"2026-11-03", "2026-11-04"}
    assert bench.worker.state == "paused" and "1 day(s) failed" in bench.worker.reason and not bench.messages
    client.days.pop("2026-11-05")                         # the site works again
    bench.now += timedelta(minutes=61)
    bench.run(3)
    assert _saved(db) == {day: 150 for day in DAYS} and bench.worker.state == "complete"


def test_pauses_for_the_data_update_and_evening_scan_on_trading_days_only(db: Database) -> None:
    bench = _Bench(db, now=datetime(2026, 11, 20, 16, 0))
    bench.worker.step()
    assert bench.worker.state == "paused" and "15:15-17:45" in bench.worker.reason and bench.client.asked == []
    assert bench.worker._should_stop()                    # a day in progress is given up at once
    bench.calendar.trading = False                        # a holiday: nothing to wait for
    bench.worker.step()
    assert bench.client.asked == ["2026-11-05"]


def test_a_slow_live_alert_check_pauses_the_download_for_the_session(db: Database) -> None:
    bench = _Bench(db, calendar=_Calendar(market_open=True), now=datetime(2026, 11, 20, 12, 0))
    bench.worker.step()
    assert bench.worker.state == "running"
    bench.worker.note_alert_check(5)                      # a normal check changes nothing
    assert bench.worker.pause_reason(bench.now) is None
    bench.worker.note_alert_check(45)
    assert "market hours" in bench.worker.pause_reason(bench.now)
    bench.calendar.market_open = False                    # after the close it goes on
    bench.now = datetime(2026, 11, 20, 18, 0)
    assert bench.worker.pause_reason(bench.now) is None


def test_disk_almost_full_pauses_and_tells_the_owner_once(db: Database) -> None:
    bench = _Bench(db)
    bench.free = 1000.0
    bench.run(3)
    assert bench.client.asked == [] and "disk almost full" in bench.worker.reason
    assert len(bench.messages) == 1 and "1.0 GB free" in bench.messages[0]
    bench.new_worker()                                    # not again after a restart either
    bench.run(2)
    assert len(bench.messages) == 1
    bench.free = 20_000.0
    bench.worker.step()
    assert bench.client.asked == ["2026-11-05"]


def test_an_incomplete_day_is_found_and_replaced(db: Database) -> None:
    # Half a day was saved somehow, by a broker that is not in the real data.
    db.upsert("broker_flow_daily", [{"date": "2026-11-04", "symbol": "NABIL", "broker": 99, "buy_qty": 50, "sell_qty": 50,
                                    "buy_amount": 1, "sell_amount": 1}], ("date", "symbol", "broker"))
    assert backfill.day_problem(db, "2026-11-04") == "covers only 33.3% of the traded volume"
    bench = _Bench(db)
    bench.run()
    assert bench.client.asked[0] == "2026-11-04"          # repaired first
    assert _saved(db) == {day: 150 for day in DAYS}
    assert db.scalar("SELECT COUNT(*) FROM broker_flow_daily WHERE broker = 99") == 0
    assert backfill.incomplete_days(db, "2026-01-01") == []
    assert sorted(bench.client.asked) == sorted(DAYS)


def test_a_new_day_the_source_has_not_published_yet_is_asked_again_later(db: Database) -> None:
    client = _Client({"2026-11-05": []})
    bench = _Bench(db, client, now=datetime(2026, 11, 6, 20, 0))
    bench.run(6)
    assert client.asked.count("2026-11-05") == 1
    assert "2026-11-05" in backfill.missing_days(db, "2026-01-01")      # not written off as empty
    assert bench.worker.state == "complete"                             # the history itself is done
    client.days.pop("2026-11-05")
    bench.now += timedelta(minutes=31)
    bench.run(3)
    assert _saved(db)["2026-11-05"] == 150


def test_an_old_day_without_trades_is_remembered_as_empty(db: Database) -> None:
    client = _Client({"2026-11-04": []})
    bench = _Bench(db, client)
    bench.run()
    assert client.asked.count("2026-11-04") == 1 and bench.worker.state == "complete"
    numbers = backfill.progress(db, "2026-01-01")
    assert (numbers["done"], numbers["stored"], numbers["empty"], numbers["remaining"]) == (3, 2, 1, 0)


def test_only_one_downloader_at_a_time(db: Database) -> None:
    db.set_setting(backfill.HEARTBEAT, f"1|{datetime.now().timestamp():.0f}")      # another process, just now
    bench = _Bench(db)
    bench.worker.step()
    assert "another download" in bench.worker.reason and bench.client.asked == []
    said: list[str] = []
    assert "skipped" in backfill.run_broker_backfill(since="2026-01-01", db=db, client=bench.client, echo=said.append)
    db.set_setting(backfill.HEARTBEAT, "1|1000")                                   # long ago: it is gone
    bench.worker.step()
    assert bench.client.asked == ["2026-11-05"]


def test_status_line_shows_progress_state_time_left_and_oldest_day(db: Database) -> None:
    bench = _Bench(db)
    assert "0 of ~3 days (0%)" in status_line(db, bench.worker) and "oldest day so far: none yet" in status_line(db, bench.worker)
    bench.worker.step()
    line = status_line(db, bench.worker)
    assert line.startswith("Broker history: 1 of ~3 days (33%), running, about ")
    assert "of PC-on time left, oldest day so far: 2026-11-05" in line
    bench.is_online = False
    bench.now += timedelta(minutes=5)
    bench.worker.step()
    assert "paused (no internet connection)" in status_line(db, bench.worker)
    bench.is_online = True
    bench.now += timedelta(minutes=5)
    bench.run()
    assert status_line(db, bench.worker).startswith("Broker history: complete, 3 of 3 days, back to 2026-11-03")
    assert "not running" in status_line(Database(db.path.parent / "other.sqlite3"), None)


def test_the_client_gives_up_a_day_at_once_when_told_to_stop() -> None:
    class _Session:
        calls = 0

        def get(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("no request may be sent")

    session = _Session()
    with pytest.raises(DownloadInterrupted):
        ChukulClient(session=session, sleep=lambda seconds: None, should_stop=lambda: True).floorsheet_day("2026-11-05")
    assert session.calls == 0


def test_module_level_hook_is_safe_without_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_worker, "_worker", None)
    broker_worker.note_alert_check(99)
    assert SOURCE == "Chukul"
