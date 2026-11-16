"""Stage 4: the live journal (signal outcomes, /report, auto-disable)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from nepse_bot.bot import reports
from nepse_bot.db.store import Database
from nepse_bot.engine import config as engine_config
from nepse_bot.engine.setups import TrendPullback
from nepse_bot.portfolio import journal
from nepse_bot.utils import health

PLAN = {"as_of": "", "entry": 100.0, "stop": 94.0, "target_1": 112.0, "target_2": 118.0, "breakeven_stop": 101.0,
        "quantity": 200, "time_stop_sessions": 15}


def _db(tmp_path: Path, symbol_rows: dict[str, list[tuple[float, float, float, float]]]) -> tuple[Database, str]:
    """A database where each symbol is flat at 100 for 30 days and then follows the given days."""
    db = Database(tmp_path / "journal.sqlite3")
    signal_day = ""
    for symbol, rows in symbol_rows.items():
        data = [(100.0, 100.5, 99.5, 100.0)] * 30 + rows
        days = pd.bdate_range("2026-01-01", periods=len(data))
        signal_day = days[29].strftime("%Y-%m-%d")
        db.upsert_prices(
            [{"symbol": symbol, "date": day.strftime("%Y-%m-%d"), "open": o, "high": h, "low": l, "close": c, "volume": 1000, "turnover": 1e7}
             for day, (o, h, l, c) in zip(days, data)],
            "test",
        )
    return db, signal_day


def _log(db: Database, symbol: str, day: str, setup: str = "trend_pullback", status: str = "open", outcome: dict | None = None, closed_at: str | None = None) -> None:
    plan = {**PLAN, "as_of": day, **({"outcome": outcome} if outcome else {})}
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO signals (id, symbol, created_at, setup, verdict, score, entry, stop_loss, target_1, target_2, status, closed_at, plan_json) "
            "VALUES (?, ?, ?, ?, 'BUY', 80, 100, 94, 112, 118, ?, ?, ?)",
            (f"{day}:{symbol}:{setup}:{closed_at}", symbol, day, setup, status, closed_at, json.dumps(plan)),
        )


def test_outcomes_use_the_daily_high_and_low(tmp_path: Path) -> None:
    db, day = _db(tmp_path, {
        "STOPPED": [(100, 101, 99, 100), (100, 101, 99, 100), (100, 100, 93, 99), (99, 100, 99, 100)],   # low touches the stop, close does not
        "WINNER": [(100, 101, 99, 100), (100, 104, 100, 104), (104, 119, 104, 117), (117, 118, 100, 101), (101, 102, 100, 101), (101, 102, 100, 101)],
        "MISSED": [(104, 106, 103, 105), (105, 106, 104, 105)],
        "RUNNING": [(100, 101, 99, 100), (100, 102, 100, 102)],
    })
    for symbol in ("STOPPED", "WINNER", "MISSED", "RUNNING"):
        _log(db, symbol, day)
    counts = journal.update_journal(db)
    status = {row["symbol"]: row for row in db.query("SELECT symbol, status, outcome_price, closed_at, plan_json FROM signals")}
    assert status["STOPPED"]["status"] == "stop" and status["STOPPED"]["outcome_price"] == pytest.approx(94.0)
    assert status["WINNER"]["status"] == "target_2"          # target 1 filled and the high also reached target 2
    assert status["MISSED"]["status"] == "not_filled"
    assert status["RUNNING"]["status"] == "open" and status["RUNNING"]["closed_at"] is None
    assert json.loads(status["STOPPED"]["plan_json"])["outcome"]["net_profit"] < 0
    assert counts == {"stop": 1, "target_2": 1, "not_filled": 1, "open": 1}
    journal.update_journal(db)                               # running it again changes nothing that is finished
    assert db.scalar("SELECT COUNT(*) FROM signals WHERE status = 'stop'") == 1


def test_losing_setup_is_switched_off_and_the_owner_is_told(tmp_path: Path) -> None:
    db, day = _db(tmp_path, {"AAA": [(100, 101, 99, 100)]})
    for index in range(20):
        profit = 2000.0 if index < 5 else -1500.0             # won 10,000, lost 22,500: profit factor 0.44
        _log(db, "AAA", day, status="stop", outcome={"net_profit": profit}, closed_at=f"2026-03-{index + 1:02d}")
    health.drain_admin_alerts()
    assert journal.check_auto_disable(db) == ["trend_pullback"]
    assert "trend_pullback" in journal.disabled_setups(db)
    assert TrendPullback().enabled() is False                # the engine no longer uses it
    alerts = health.drain_admin_alerts()
    assert len(alerts) == 1 and "/report enable trend_pullback" in alerts[0]
    assert journal.check_auto_disable(db) == []              # not announced twice
    text = reports.format_live_report(journal.live_results(db), journal.disabled_setups(db))
    assert "SWITCHED OFF" in text and "profit factor 0.44" in text
    assert journal.enable_setup(db, "trend_pullback") is True
    assert "trend_pullback" not in engine_config.disabled_setups()


def test_setup_with_too_few_finished_signals_is_left_alone(tmp_path: Path) -> None:
    db, day = _db(tmp_path, {"AAA": [(100, 101, 99, 100)]})
    for index in range(19):
        _log(db, "AAA", day, status="stop", outcome={"net_profit": -1000.0}, closed_at=f"2026-03-{index + 1:02d}")
    assert journal.check_auto_disable(db) == []
    engine_config.set_disabled_setups(())
