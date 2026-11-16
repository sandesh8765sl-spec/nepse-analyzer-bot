"""The broker study: the "broker buying" number, the gate, and switching on only what passed."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nepse_bot.alerts.rules import top_pick_failures
from nepse_bot.backtest import broker_study as study
from nepse_bot.db.store import Database
from nepse_bot.engine.config import engine_config
from nepse_bot.engine.engine import has_broker_buying
from nepse_bot.engine.evidence import Evidence, Record, load_file

EVIDENCE = """# record
setups:
  trend_pullback:
    recent: {period: "a", trades: 561, win_rate: 29.6, avg_win_pct: 7.4, avg_loss_pct: -6.02, expectancy_pct: -2.05, profit_factor: 0.51}
    earlier: {period: "b", trades: 603, win_rate: 40.8, avg_win_pct: 15.92, avg_loss_pct: -6.37, expectancy_pct: 2.72, profit_factor: 1.74}
  broker_accumulation:
    recent: {period: "a", trades: 0}
    earlier: {period: "b", trades: 0}
  event_runup:
    recent: {period: "a", trades: 37, win_rate: 32.4, avg_win_pct: 5.22, avg_loss_pct: -5.36, expectancy_pct: -1.93, profit_factor: 0.39}
    earlier: {period: "b", trades: 111, win_rate: 36.9, avg_win_pct: 15.55, avg_loss_pct: -7.4, expectancy_pct: 1.08, profit_factor: 1.19}
"""
ENGINE = """setups:
  trend_pullback: true
  broker_accumulation: false       # OFF until its backtest passes
  broker_accumulation_min_days: 10
broker_filter:
  min_top5_net_share: 15
  filter_setups: []                # none
  filter_top_pick: false           # off
"""


def _evidence(key: str, recent_factor: float, earlier_factor: float, trades: int = 80) -> Evidence:
    return Evidence(key, Record("2024-11 to 2026-11", trades, 45.0, 9.0, -5.0, 1.3, recent_factor),
                    Record("2022-03 to 2024-11", trades, 50.0, 12.0, -6.0, 3.0, earlier_factor))


def _result(accumulation: Evidence, filtered: Evidence, plain: Evidence, top_pick: bool = False) -> dict[str, Any]:
    pick = {"recent": {"alerts": 9, "finished": 6, "win_rate": 16.7, "expectancy_pct": -2.2, "profit_factor": 0.3},
            "earlier": {"alerts": 4, "finished": 0}}
    return {"periods": ("a", "b"), "min_share": 15.0, "broker_days": 1101, "accumulation": accumulation,
            "filters": {"trend_pullback": {"plain": plain, "filtered": filtered}},
            "top_pick": {"plain": pick, "filtered": pick}, "top_pick_passed": top_pick}


@pytest.fixture()
def files(tmp_path: Path) -> tuple[Path, Path]:
    evidence_path, engine_path = tmp_path / "evidence.yaml", tmp_path / "engine.yaml"
    evidence_path.write_text(EVIDENCE, encoding="utf-8")
    engine_path.write_text(ENGINE, encoding="utf-8")
    return evidence_path, engine_path


def test_broker_net_share_is_the_top_five_net_buyers_share_of_volume(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    days = [f"2026-10-{day:02d}" for day in range(1, 11)]
    rows = []
    for day in days:        # broker 1 buys 60 from broker 2 every day; brokers 3 to 7 buy and sell 8 each (no net buying)
        rows += [
            {"date": day, "symbol": "NABIL", "broker": 1, "buy_qty": 60, "sell_qty": 0, "buy_amount": 0, "sell_amount": 0},
            {"date": day, "symbol": "NABIL", "broker": 2, "buy_qty": 0, "sell_qty": 60, "buy_amount": 0, "sell_amount": 0},
            *[{"date": day, "symbol": "NABIL", "broker": broker, "buy_qty": 8, "sell_qty": 8, "buy_amount": 0, "sell_amount": 0} for broker in range(3, 8)],
        ]
    db.upsert("broker_flow_daily", rows, ("date", "symbol", "broker"))
    # 60 net bought out of 100 traded a day
    assert study.broker_net_share(db, "2026-10-10", ["NABIL", "NICA"]) == {"NABIL": pytest.approx(60.0)}
    assert study.broker_net_share(db, "2026-10-09", ["NABIL"]) == {}        # only 9 days of data by then: no opinion


def test_nothing_passes_nothing_changes(files: tuple[Path, Path], tmp_path: Path) -> None:
    result = _result(_evidence("broker_accumulation", 0.6, 1.5), _evidence("trend_pullback", 1.1, 1.9), _evidence("trend_pullback", 0.5, 1.7))
    assert study.apply(result, date(2026, 11, 20), *files, backup_folder=tmp_path / "backup") == []
    assert files[0].read_text(encoding="utf-8") == EVIDENCE and files[1].read_text(encoding="utf-8") == ENGINE
    text = study.summary_text(result, [])
    assert "Nothing passed, so nothing was switched on" in text and "Broker accumulation setup: does not pass" in text
    assert "nothing passed the gate" in study.result_line(result, [], date(2026, 11, 20))


def test_too_few_trades_do_not_pass_even_with_a_high_profit_factor(files: tuple[Path, Path], tmp_path: Path) -> None:
    result = _result(_evidence("broker_accumulation", 3.0, 3.0, trades=49), _evidence("trend_pullback", 0.5, 0.5), _evidence("trend_pullback", 0.5, 0.5))
    assert study.apply(result, date(2026, 11, 20), *files, backup_folder=tmp_path / "backup") == []


def test_only_what_passed_is_switched_on(files: tuple[Path, Path], tmp_path: Path) -> None:
    result = _result(_evidence("broker_accumulation", 1.4, 1.3), _evidence("trend_pullback", 1.25, 1.9), _evidence("trend_pullback", 0.5, 1.7))
    changes = study.apply(result, date(2026, 11, 20), *files, backup_folder=tmp_path / "backup")
    assert len(changes) == 2
    engine_text = files[1].read_text(encoding="utf-8")
    assert "  broker_accumulation: true    # switched on by the broker study on 2026-11-20" in engine_text
    assert "  broker_accumulation_min_days: 10" in engine_text
    assert "  filter_setups: [trend_pullback]" in engine_text
    assert "  filter_top_pick: false" in engine_text                       # TOP PICK did not pass: untouched
    saved = load_file(files[0])
    assert saved["broker_accumulation"].proven and saved["trend_pullback_broker"].proven
    assert not saved["trend_pullback"].proven and saved["event_runup"].recent.trades == 37     # the rest is untouched
    assert (tmp_path / "backup" / "evidence_before.yaml").read_text(encoding="utf-8") == EVIDENCE
    assert "Switched ON (only what passed)" in study.summary_text(result, changes)


def test_a_setup_that_already_passes_without_the_filter_is_left_alone(files: tuple[Path, Path], tmp_path: Path) -> None:
    result = _result(_evidence("broker_accumulation", 0.5, 0.5), _evidence("trend_pullback", 1.5, 1.5), _evidence("trend_pullback", 1.4, 1.4))
    assert study.apply(result, date(2026, 11, 20), *files, backup_folder=tmp_path / "backup") == []


def test_broker_buying_rule_and_top_pick_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(engine_config(), "broker_filter", {"min_top5_net_share": 15, "filter_setups": [], "filter_top_pick": False})
    assert has_broker_buying(15.0) and not has_broker_buying(14.9) and not has_broker_buying(None)
    item = SimpleNamespace(verdict="SPECULATIVE", plan=SimpleNamespace(avg_range_percent=2.0), score=90.0, confidence="High",
                           sector_state=None, sector="Banks", broker_net_share=5.0)
    assert top_pick_failures(item, False, []) == []                         # the filter is off: broker flow is not asked
    monkeypatch.setitem(engine_config(), "broker_filter", {"min_top5_net_share": 15, "filter_setups": [], "filter_top_pick": True})
    assert top_pick_failures(item, False, []) == ["no clear buying by the top brokers"]
    item.broker_net_share = 22.0
    assert top_pick_failures(item, False, []) == []
