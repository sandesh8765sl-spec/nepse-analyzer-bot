"""Database, polite client, price adjustment, metrics and the ingestion pipeline."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from nepse_bot.db.store import Database
from nepse_bot.ingest import adjust, metrics
from nepse_bot.ingest.migrate import migrate_signal_journal
from nepse_bot.ingest.pipeline import Ingestor
from nepse_bot.sources import http
from nepse_bot.sources.http import HostRateLimiter, PoliteClient, SourceError
from nepse_bot.utils.health import drain_admin_alerts

LIMITS = [("1900-01-01", 10.0), ("2026-04-20", 15.0)]


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.sqlite3")


# ------------------------------------------------------------------ database
def test_database_creates_all_tables_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "x.sqlite3"
    first = Database(path)
    expected = {
        "securities", "daily_prices", "corporate_actions", "fundamentals", "broker_flow_daily", "index_daily",
        "market_breadth_daily", "news", "signals", "positions", "trades", "settings",
    }
    assert expected <= set(first.table_counts()) | {"settings"}
    first.upsert_prices([{"symbol": "nabil", "date": "2026-11-06", "close": 549, "volume": 10}], "test")
    first.close()
    again = Database(path)  # migrations must not run twice or lose data
    assert again.scalar("SELECT COUNT(*) FROM daily_prices") == 1
    assert again.scalar("SELECT symbol FROM daily_prices") == "NABIL"


def test_upsert_updates_instead_of_duplicating(db: Database) -> None:
    db.upsert_prices([{"symbol": "NABIL", "date": "2026-11-06", "close": 540}], "ShareHub")
    db.upsert_prices([{"symbol": "NABIL", "date": "2026-11-06", "close": 549, "open": 550, "high": 554, "low": 549}], "NEPSE")
    rows = db.query("SELECT * FROM daily_prices")
    assert len(rows) == 1 and rows[0]["close"] == 549 and rows[0]["source"] == "NEPSE"
    db.upsert_prices([{"symbol": "ODD", "date": "2026-11-06", "open": 183.58, "high": 183.58, "low": 183.58, "close": 183.19}], "t")
    odd = db.query("SELECT * FROM daily_prices WHERE symbol = 'ODD'")[0]
    assert odd["low"] <= odd["close"] <= odd["high"], "inconsistent bars from a source are repaired"
    assert db.upsert_prices([{"symbol": "X", "date": "2026-11-06", "close": 0}, {"symbol": "", "date": "2026-11-06", "close": 5}], "t") == 0


def test_fundamentals_as_of_has_no_lookahead(db: Database) -> None:
    base = {"symbol": "NABIL", "source": "t", "eps": None, "eps_annualized": None, "book_value": None}
    db.upsert(
        "fundamentals",
        [
            {**base, "fiscal_year": "2082/2083", "quarter": 4, "period_end_date": "2026-07-16", "publication_date": "2026-08-20", "eps_annualized": 25.0},
            {**base, "fiscal_year": "2083/2084", "quarter": 1, "period_end_date": "2026-10-17", "publication_date": "2026-11-13", "eps_annualized": 33.89},
        ],
        ("symbol", "fiscal_year", "quarter"),
    )
    assert db.fundamentals_as_of("NABIL", "2026-08-19") is None
    assert db.fundamentals_as_of("NABIL", "2026-11-12")["eps_annualized"] == 25.0, "the Q1 report was not public yet"
    assert db.fundamentals_as_of("nabil", "2026-11-13")["eps_annualized"] == 33.89


def test_saved_signals_are_copied_not_moved(db: Database, tmp_path: Path) -> None:
    journal = tmp_path / "signal_journal.json"
    journal.write_text(
        json.dumps({"signals": [{"id": "abc", "symbol": "phcl", "score": 88, "entry_price": 400, "stop_loss": 380, "target_1": 440, "status": "active", "technical_setup": "Breakout"}, {"symbol": "no-id"}]}),
        encoding="utf-8",
    )
    before = journal.read_text(encoding="utf-8")
    assert migrate_signal_journal(db, journal) == 1
    assert migrate_signal_journal(db, journal) == 1  # running again does not duplicate
    rows = db.query("SELECT * FROM signals")
    assert len(rows) == 1 and rows[0]["symbol"] == "PHCL" and rows[0]["entry"] == 400 and rows[0]["setup"] == "Breakout"
    assert journal.read_text(encoding="utf-8") == before, "the original file must be left untouched"


# ------------------------------------------------------------ polite client
class _Clock:
    def __init__(self) -> None:
        self.now, self.sleeps = 0.0, []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_at_least_one_second_between_requests_to_the_same_site() -> None:
    clock = _Clock()
    limiter = HostRateLimiter(1.0)
    limiter._clock, limiter._sleep = clock.time, clock.sleep
    assert limiter.wait("a.com") == 0
    assert limiter.wait("a.com") == pytest.approx(1.0)
    assert limiter.wait("b.com") == 0, "another website is not delayed"
    clock.now += 5
    assert limiter.wait("a.com") == 0, "no delay when enough time has already passed"


class _Reply:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _Session:
    def __init__(self, replies: list[Any]) -> None:
        self.replies, self.calls = list(replies), 0

    def get(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _client(replies: list[Any], sleeps: list[float]) -> tuple[PoliteClient, _Session]:
    session = _Session(replies)
    limiter = HostRateLimiter(0.0)
    return PoliteClient("TestSource", session=session, limiter=limiter, max_retries=4, backoff_seconds=2.0, sleep=sleeps.append), session


def test_retries_with_growing_pauses_then_succeeds() -> None:
    sleeps: list[float] = []
    client, session = _client([ConnectionError("down"), _Reply(503), _Reply(200, {"ok": 1})], sleeps)
    assert client.get_json("https://x.test/a") == {"ok": 1}
    assert session.calls == 3 and sleeps == [2.0, 4.0], "exponential backoff"


def test_gives_up_after_the_retry_limit_and_does_not_retry_not_found() -> None:
    sleeps: list[float] = []
    client, session = _client([_Reply(500)] * 4, sleeps)
    with pytest.raises(SourceError):
        client.get_json("https://x.test/a")
    assert session.calls == 4 and sleeps == [2.0, 4.0, 8.0]
    client, session = _client([_Reply(404)], sleeps := [])
    with pytest.raises(SourceError):
        client.get_json("https://x.test/a")
    assert session.calls == 1 and sleeps == []


def test_raw_samples_one_per_day_and_old_ones_pruned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(http, "RAW_DIR", tmp_path / "raw")
    assert http.save_raw_sample("Src", "prices.json", "first", today=date(2026, 11, 1)) is not None
    assert http.save_raw_sample("Src", "prices.json", "second", today=date(2026, 11, 1)) is None
    assert (tmp_path / "raw" / "Src" / "2026-11-01" / "prices.json").read_text(encoding="utf-8") == "first"
    http.save_raw_sample("Src", "prices.json", "new", today=date(2026, 11, 13))
    assert http.prune_raw_samples(keep_days=7, today=date(2026, 11, 13)) == 1
    assert [p.name for p in (tmp_path / "raw" / "Src").iterdir()] == ["2026-11-13"]


# -------------------------------------------------------------- adjustment
def _prices(closes: list[float], start: str = "2025-01-01") -> pd.DataFrame:
    days = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"symbol": "T", "date": days, "open": closes, "high": closes, "low": closes, "close": closes})


def test_bonus_makes_the_adjusted_series_continuous() -> None:
    prices = _prices([1000, 1010, 1020, 816, 820, 830])  # 25% bonus: 1020 -> 816
    ex_day = prices["date"][3]
    adjusted, applied = adjust.adjust_prices(prices, [{"action_type": "bonus", "ratio": 25.0, "book_closure_date": ex_day}])
    assert applied[0].ex_date == ex_day and applied[0].factor == pytest.approx(0.8)
    assert adjusted["adj_close"].tolist() == pytest.approx([800, 808, 816, 816, 820, 830])
    assert adjusted["close"].tolist() == [1000, 1010, 1020, 816, 820, 830], "raw prices are never changed"
    assert adjusted["adj_close"].pct_change().abs().max() < 0.02
    assert adjust.find_quality_flags(prices, applied, [], LIMITS) == []


def test_right_share_uses_the_theoretical_ex_rights_price() -> None:
    # 1:0.5 right at Rs 100 when the share is at 400: (400 + 0.5*100) / 1.5 = 300
    factor = adjust.action_factor({"action_type": "right", "ratio": 0.5, "amount": 100}, 400.0)
    assert factor == pytest.approx(0.75)
    assert adjust.action_factor({"action_type": "right", "ratio": 0.5, "amount": 100}, 90.0) is None, "a right above market price does not lower it"
    assert adjust.action_factor({"action_type": "cash_dividend", "amount": 15}, 400.0) is None, "cash dividends are not adjusted"


def test_adjustment_finds_the_real_gap_near_the_announced_date() -> None:
    prices = _prices([500, 505, 510, 425, 430, 428, 431])  # 20% bonus, gap on day 3
    announced = prices["date"][5]  # the announced date is two days late
    adjusted, applied = adjust.adjust_prices(prices, [{"action_type": "bonus", "ratio": 20.0, "book_closure_date": announced}])
    assert applied[0].ex_date == prices["date"][3] and applied[0].aligned_to_gap
    assert adjusted["adj_close"].pct_change().min() > -0.05


def test_two_actions_multiply() -> None:
    prices = _prices([1000, 800, 810, 648, 650])
    actions = [
        {"action_type": "bonus", "ratio": 25.0, "book_closure_date": prices["date"][1]},
        {"action_type": "bonus", "ratio": 25.0, "book_closure_date": prices["date"][3]},
    ]
    adjusted, applied = adjust.adjust_prices(prices, actions)
    assert len(applied) == 2 and adjusted["adj_factor"][0] == pytest.approx(0.64)
    assert adjusted["adj_factor"].iloc[-1] == 1.0


def test_impossible_drop_without_a_corporate_action_is_flagged() -> None:
    prices = _prices([500, 505, 440, 445])  # -12.9% under a 10% daily limit
    flags = adjust.find_quality_flags(prices, [], [], LIMITS)
    assert [flag["kind"] for flag in flags] == ["unexplained_gap"] and flags[0]["date"] == prices["date"][2]


def test_daily_limit_depends_on_the_date() -> None:
    assert adjust.limit_for("2026-04-19", LIMITS) == 10.0
    assert adjust.limit_for("2026-04-20", LIMITS) == 15.0
    after = _prices([500, 505, 440, 445], start="2026-05-04")  # -12.9% is legal under the 15% limit
    assert adjust.find_quality_flags(after, [], [], LIMITS) == []
    assert adjust.price_limits()[-1] == ("2026-04-20", 15.0), "the real config carries the 2026 rule change"


def test_large_drop_just_after_a_book_closure_is_flagged() -> None:
    prices = _prices([500, 505, 440, 445], start="2026-05-04")
    closure = [{"book_closure_date": prices["date"][1]}]  # e.g. a cash dividend: no adjustment
    flags = adjust.find_quality_flags(prices, [], closure, LIMITS)
    assert [flag["kind"] for flag in flags] == ["gap_after_book_closure"]


# ------------------------------------------------------------------ metrics
def test_liquidity_counts_untraded_days_as_zero() -> None:
    market_dates = pd.bdate_range("2026-01-01", periods=60).strftime("%Y-%m-%d").tolist()
    traded = market_dates[::2]  # trades every second day
    prices = pd.DataFrame({"symbol": "T", "date": traded, "close": 100.0, "turnover": 1_000_000.0, "trades": 40})
    result = metrics.liquidity_for_symbol(prices, market_dates, market_dates[-1])
    assert result["traded_days_pct_60"] == pytest.approx(50.0)
    assert result["avg_turnover_20"] == pytest.approx(500_000.0)
    assert result["avg_trades_20"] == pytest.approx(20.0)


def test_breadth_counts_and_moving_average_share() -> None:
    days = pd.bdate_range("2026-01-01", periods=60).strftime("%Y-%m-%d").tolist()
    rows = []
    for symbol, step in (("UP", 1.0), ("DOWN", -1.0), ("FLAT", 0.0)):
        for i, day in enumerate(days):
            rows.append({"symbol": symbol, "date": day, "close": 200 + step * i, "adj_close": 200 + step * i, "turnover": 1000.0})
    table = metrics.breadth_table(pd.DataFrame(rows))
    last = table[-1]
    assert (last["advancers"], last["decliners"], last["unchanged"]) == (1, 1, 1)
    assert last["traded_symbols"] == 3 and last["turnover"] == 3000.0
    assert last["pct_above_ma50"] == pytest.approx(33.33, abs=0.01)
    assert last["pct_above_ma200"] is None, "not enough history for a 200-day average"
    assert len(metrics.breadth_table(pd.DataFrame(rows), since=days[-2])) == 2


# ----------------------------------------------------------------- pipeline
def _day_rows(day: str, count: int = 60, close: float = 100.0) -> list[dict[str, Any]]:
    return [
        {"symbol": f"S{i:03d}", "date": day, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000, "turnover": 100000, "trades": 20}
        for i in range(count)
    ]


class FakeNepse:
    def __init__(self, days: dict[str, Any]) -> None:
        self.days, self.asked = days, []

    def prices_for_date(self, day: str) -> list[dict[str, Any]]:
        self.asked.append(day)
        result = self.days.get(day, [])
        if isinstance(result, Exception):
            raise result
        return result

    def companies(self) -> list[dict[str, Any]]:
        return [{"symbol": f"S{i:03d}", "name": f"Company {i}", "sector": "Hydro Power", "instrument_type": "equity", "status": "active", "nepse_id": i} for i in range(60)]

    def floorsheet(self) -> list[dict[str, Any]]:
        return []

    def index_snapshot(self, day: str) -> list[dict[str, Any]]:
        return []


class FakeShareHub:
    def __init__(self, index_days: set[str] | None = None, fail: set[str] | None = None) -> None:
        self.index_days, self.fail = index_days or set(), fail or set()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise SourceError(f"ShareHub {name} is down")

    def price_history(self, symbol: str, since: str | None = None, size: int = 500) -> list[dict[str, Any]]:
        self._maybe_fail("price_history")
        return [{"symbol": symbol, "date": d, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "turnover": 1, "trades": 1} for d in sorted(self.index_days) if not since or d >= since]

    def securities(self) -> list[dict[str, Any]]:
        self._maybe_fail("securities")
        return []

    def dividends(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        self._maybe_fail("dividends")
        return []

    def rights(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        return []

    def indices(self) -> list[dict[str, Any]]:
        self._maybe_fail("indices")
        return [{"code": "NEPSE", "name": "NEPSE Index", "is_sector": 0}]

    def fundamentals(self, symbol: str, first_seen: date | None = None) -> list[dict[str, Any]]:
        self._maybe_fail("fundamentals")
        return []

    def announcements(self, pages: int = 1) -> list[dict[str, Any]]:
        return []


class FakeShareSansar:
    def __init__(self, tables: dict[str, pd.DataFrame] | None = None) -> None:
        self.tables = tables or {}

    def _get_sharesansar_daily_table(self, day: str) -> pd.DataFrame:
        if day not in self.tables:
            raise ValueError(f"ShareSansar returned no records for {day}")
        return self.tables[day]

    @staticmethod
    def _numeric_from_row(row: pd.Series, columns: tuple[str, ...]) -> float | None:
        for column in columns:
            if column in row.index and pd.notna(row[column]):
                return float(row[column])
        return None


def _ingestor(db: Database, calendar, nepse: FakeNepse, sharehub: FakeShareHub | None = None, sharesansar: FakeShareSansar | None = None) -> Ingestor:
    return Ingestor(db=db, calendar=calendar, nepse=nepse, sharehub=sharehub or FakeShareHub(), sharesansar=sharesansar or FakeShareSansar())


def test_normal_day_is_stored_from_nepse(db: Database, calendar) -> None:
    ingestor = _ingestor(db, calendar, FakeNepse({"2026-11-18": _day_rows("2026-11-18")}))
    assert ingestor.ingest_prices_for_day(date(2026, 11, 18)) == "ok"
    assert db.scalar("SELECT COUNT(*) FROM daily_prices WHERE date = '2026-11-18' AND source = 'NEPSE'") == 60
    assert db.query("SELECT status, source FROM ingest_days")[0] == {"status": "ok", "source": "NEPSE"}


def test_falls_back_to_sharesansar_when_nepse_is_down(db: Database, calendar) -> None:
    table = pd.DataFrame({"Symbol": [f"S{i:03d}" for i in range(60)], "Open": 99.0, "High": 101.0, "Low": 98.0, "Close": 100.0, "Vol": 500.0, "Turnover": 50000.0, "Trans.": 12.0})
    ingestor = _ingestor(db, calendar, FakeNepse({"2026-11-18": SourceError("NEPSE is down")}), sharesansar=FakeShareSansar({"2026-11-18": table}))
    assert ingestor.ingest_prices_for_day(date(2026, 11, 18)) == "ok"
    row = db.query("SELECT * FROM daily_prices WHERE symbol = 'S001'")[0]
    assert row["source"] == "ShareSansar" and row["close"] == 100.0 and row["trades"] == 12
    runs = {(r["source"], r["ok"]) for r in db.query("SELECT source, ok FROM source_runs WHERE dataset = 'prices'")}
    assert runs == {("NEPSE", 0), ("ShareSansar", 1)}


def test_all_sources_failing_is_recorded_as_failed_not_as_a_holiday(db: Database, calendar) -> None:
    db.upsert("securities", [{"symbol": "S001", "status": "active", "instrument_type": "equity", "is_promoter": 0}], ("symbol",))
    ingestor = _ingestor(db, calendar, FakeNepse({"2026-11-18": SourceError("down")}), sharehub=FakeShareHub(fail={"price_history"}))
    assert ingestor.ingest_prices_for_day(date(2026, 11, 18)) == "failed"
    assert db.scalar("SELECT status FROM ingest_days") == "failed"
    assert calendar.is_trading_day(date(2026, 11, 18)), "an outage must never be mistaken for a holiday"


def test_catch_up_after_the_laptop_slept_and_holiday_detection(db: Database, calendar) -> None:
    # Database last updated Friday 13 Nov. The laptop then slept for a week.
    db.upsert_prices(_day_rows("2026-11-13"), "NEPSE")
    nepse = FakeNepse({day: _day_rows(day) for day in ("2026-11-16", "2026-11-17", "2026-11-19", "2026-11-20")})  # Wed 18th: no trading
    ingestor = _ingestor(db, calendar, nepse, sharehub=FakeShareHub(index_days={"2026-11-16", "2026-11-17", "2026-11-19", "2026-11-20"}))
    calendar.set_now("2026-11-21 10:00")  # Saturday morning
    ingestor.refresh_securities()

    assert [d.isoformat() for d in ingestor.missing_trading_days()] == ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    report = ingestor.run(light=True)

    assert report.days_ingested == ["2026-11-16", "2026-11-17", "2026-11-19", "2026-11-20"]
    assert report.days_closed == ["2026-11-18"] and not report.days_failed
    assert not calendar.is_trading_day(date(2026, 11, 18)), "the closed day is added to the holiday list"
    alerts = drain_admin_alerts()
    assert any("2026-11-18" in text and "/holiday remove" in text for text in alerts), "the owner is told and can undo it"
    assert ingestor.missing_trading_days() == [], "nothing is fetched twice"
    assert db.scalar("SELECT COUNT(DISTINCT date) FROM daily_prices") == 5
    assert db.scalar("SELECT COUNT(*) FROM daily_prices WHERE adj_close IS NULL") == 0, "adjusted prices are filled in"
    assert db.scalar("SELECT COUNT(*) FROM market_breadth_daily") == 5
    assert db.scalar("SELECT COUNT(*) FROM liquidity_metrics WHERE as_of_date = '2026-11-20'") == 60


def test_todays_unfinished_session_is_never_ingested(db: Database, calendar) -> None:
    db.upsert_prices(_day_rows("2026-11-17"), "NEPSE")
    ingestor = _ingestor(db, calendar, FakeNepse({}))
    calendar.set_now("2026-11-18 12:00")  # Wednesday, session running
    assert ingestor.missing_trading_days() == []
    calendar.set_now("2026-11-18 15:20")
    assert [d.isoformat() for d in ingestor.missing_trading_days()] == ["2026-11-18"]


def test_one_failing_step_does_not_stop_the_update(db: Database, calendar) -> None:
    db.upsert_prices(_day_rows("2026-11-17"), "NEPSE")
    nepse = FakeNepse({"2026-11-18": _day_rows("2026-11-18")})
    ingestor = _ingestor(db, calendar, nepse, sharehub=FakeShareHub(index_days={"2026-11-18"}, fail={"securities", "dividends", "indices"}))
    calendar.set_now("2026-11-18 17:00")
    report = ingestor.run()
    assert report.days_ingested == ["2026-11-18"]
    assert report.steps["adjusted prices"].startswith("120 symbols") or "symbols" in report.steps["adjusted prices"]
    assert "securities" in report.steps and "breadth and liquidity" in report.steps
    assert db.scalar("SELECT COUNT(*) FROM securities") == 60, "NEPSE still supplied the company list"


def test_alert_when_a_source_fails_two_trading_days_in_a_row(db: Database, calendar) -> None:
    ingestor = _ingestor(db, calendar, FakeNepse({}))
    db.record_source_run("ShareHub", "fundamentals", "2026-11-17", False, 0, "HTTP 500")
    db.record_source_run("NEPSE", "prices", "2026-11-17", True, 300)
    assert ingestor.health_alerts() == [], "one bad day is not an alert yet"
    db.record_source_run("ShareHub", "fundamentals", "2026-11-18", False, 0, "HTTP 500")
    db.record_source_run("NEPSE", "prices", "2026-11-18", True, 300)
    alerts = ingestor.health_alerts()
    assert len(alerts) == 1 and "ShareHub" in alerts[0] and "fundamentals" in alerts[0] and "two days in a row" in alerts[0]


def test_alert_when_a_field_is_empty_for_most_companies(db: Database, calendar) -> None:
    ingestor = _ingestor(db, calendar, FakeNepse({}))
    db.upsert("securities", [{"symbol": f"S{i:03d}", "status": "active", "instrument_type": "equity", "is_promoter": 0, "sector": "Hydro Power"} for i in range(40)], ("symbol",))
    reports = [
        {"symbol": f"S{i:03d}", "fiscal_year": "2083/2084", "quarter": 1, "period_end_date": "2026-10-17", "publication_date": "2026-11-13",
         "eps": 10.0 if i < 10 else None, "book_value": 150.0}
        for i in range(40)
    ]
    db.upsert("fundamentals", reports, ("symbol", "fiscal_year", "quarter"))
    alerts = ingestor.health_alerts()
    assert len(alerts) == 1 and "fundamentals eps" in alerts[0] and "30 of 40" in alerts[0]
    # This is exactly the failure that went unnoticed for seven weeks in 2026.
