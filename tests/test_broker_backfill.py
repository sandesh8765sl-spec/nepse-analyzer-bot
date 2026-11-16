"""Historical broker flow: Chukul parsing and the resumable backfill."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nepse_bot.db.store import Database
from nepse_bot.ingest.broker_backfill import DATASET, missing_days, run_broker_backfill
from nepse_bot.sources.chukul import PAGE_SIZE, SOURCE, ChukulClient, aggregate_trades
from nepse_bot.sources.http import SourceError


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_prices(
        [{"symbol": "NABIL", "date": day, "close": 500, "volume": 150} for day in ("2026-11-03", "2026-11-04", "2026-11-05")], "test"
    )
    return database


def _trade(number: int, symbol: str = "NABIL", buyer: str = "58", seller: str = "34", quantity: float = 100, rate: float = 500) -> dict[str, Any]:
    return {"transaction": str(number), "symbol": symbol, "buyer": buyer, "seller": seller, "quantity": quantity, "rate": rate, "amount": quantity * rate}


def test_aggregate_trades_sums_buys_and_sells_per_broker() -> None:
    rows = aggregate_trades("2026-11-05", [_trade(1), _trade(2, quantity=50), _trade(3, buyer="34", seller="58", quantity=10), {"symbol": "X"}])
    by_broker = {row["broker"]: row for row in rows}
    assert by_broker[58]["buy_qty"] == 150 and by_broker[58]["sell_qty"] == 10
    assert by_broker[34]["sell_qty"] == 150 and by_broker[34]["buy_amount"] == 5000
    assert all(row["date"] == "2026-11-05" and row["symbol"] == "NABIL" for row in rows)


class _Answer:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload, self.status_code = payload, status

    def json(self) -> Any:
        return self._payload


class _Session:
    def __init__(self, pages: dict[int, Any]) -> None:
        self.pages, self.calls = pages, 0

    def get(self, url: str, params: dict[str, Any], **_: Any) -> _Answer:
        self.calls += 1
        answer = self.pages[params["page"]]
        return answer.pop(0) if isinstance(answer, list) else answer


def test_client_reads_every_page_and_retries_when_told_to_slow_down() -> None:
    first = [_trade(n) for n in range(PAGE_SIZE)]
    second = [_trade(n) for n in range(PAGE_SIZE, PAGE_SIZE + 7)]
    session = _Session({
        1: _Answer({"data": first, "last_page": 2}),
        2: [_Answer({}, status=429), _Answer({"data": second, "last_page": 2})],
    })
    client = ChukulClient(session=session, sleep=lambda seconds: None)
    assert len(client.floorsheet_day("2026-11-05")) == PAGE_SIZE + 7
    assert session.calls == 3


def test_client_refuses_an_incomplete_day() -> None:
    session = _Session({
        1: _Answer({"data": [_trade(n) for n in range(PAGE_SIZE)], "last_page": 3}),
        2: _Answer({"data": [_trade(PAGE_SIZE + 1)], "last_page": 3}),
        3: _Answer({"data": [_trade(PAGE_SIZE + 2)], "last_page": 3}),
    })
    with pytest.raises(SourceError):
        ChukulClient(session=session, sleep=lambda seconds: None).floorsheet_day("2026-11-05")


class _FakeClient:
    def __init__(self, days: dict[str, Any]) -> None:
        self.days, self.asked = days, []

    def floorsheet_day(self, day: str) -> list[dict[str, Any]]:
        self.asked.append(day)
        answer = self.days[day]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_backfill_is_newest_first_and_never_downloads_a_day_twice(db: Database) -> None:
    client = _FakeClient({
        "2026-11-05": [_trade(1), _trade(2, quantity=50)],
        "2026-11-04": [],
        "2026-11-03": SourceError("site is down"),
    })
    summary = run_broker_backfill(since="2026-01-01", db=db, client=client, echo=lambda text: None)
    assert client.asked == ["2026-11-05", "2026-11-04", "2026-11-03"]
    assert (summary["days_stored"], summary["days_empty"], summary["days_failed"]) == (1, 1, 1)
    assert db.scalar("SELECT SUM(buy_qty) FROM broker_flow_daily WHERE date = '2026-11-05'") == 150
    note = db.scalar("SELECT detail FROM source_runs WHERE source = ? AND dataset = ? AND date = '2026-11-05'", (SOURCE, DATASET))
    assert "volume match 100.0%" in note
    # Only the failed day is still wanted on the next run.
    assert missing_days(db, "2026-01-01") == ["2026-11-03"]
