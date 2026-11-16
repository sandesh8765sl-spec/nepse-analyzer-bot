"""Stage 4: the trade simulation, the cost settlement and the results tables."""

from __future__ import annotations

import pandas as pd
import pytest

from nepse_bot.backtest import report
from nepse_bot.backtest.simulate import PlanSpec, prepare_bars, settle, simulate_plan
from nepse_bot.portfolio.fees import FeeSchedule

FEES = FeeSchedule.from_config(
    {
        "board_lot": 10, "sebon_fee_percent": 0.015, "dp_charge_rupees": 25,
        "broker_commission": {"minimum_rupees": 10, "tiers": [{"up_to": 50000, "percent": 0.36}, {"up_to": None, "percent": 0.33}]},
        "capital_gains_tax": {"short_term_percent": 5, "long_term_percent": 3.75, "long_term_after_days": 365},
    }
)
SPEC = PlanSpec(entry=100.0, stop=94.0, target_1=112.0, breakeven_stop=101.0, quantity=200, time_stop_sessions=15)


def bars(rows: list[tuple[float, float, float, float]], warm_up: int = 30) -> pd.DataFrame:
    """Flat history at 100, then the given (open, high, low, close) days. The signal day is the last flat day."""
    data = [(100.0, 100.5, 99.5, 100.0)] * warm_up + rows
    index = pd.bdate_range("2025-01-01", periods=len(data))
    frame = pd.DataFrame(data, columns=["open", "high", "low", "close"], index=index)
    frame["adj_factor"] = 1.0
    return prepare_bars(frame)


def run(rows: list[tuple[float, float, float, float]], spec: PlanSpec = SPEC) -> dict:
    frame = bars(rows)
    signal_day = frame.index[29].strftime("%Y-%m-%d")
    return simulate_plan(frame, signal_day, spec, FEES, sell_after=2)


def test_order_is_not_filled_when_the_price_never_comes_back_to_the_limit() -> None:
    result = run([(103, 105, 102, 104), (104, 106, 103, 105), (99, 100, 98, 99)])
    assert result["status"] == "not_filled" and result["filled"] is False


def test_stop_is_not_used_before_t_plus_2_and_fills_at_the_stop_price() -> None:
    # Day 1 buy at 100 and the price dives through the stop the same day and the next: no sale allowed yet.
    result = run([(100, 100, 92, 93), (93, 95, 92, 95), (95, 96, 93.5, 95), (95, 96, 95, 96)])
    assert result["status"] == "stop"
    assert result["entry_date"] != result["exit_date"]
    assert result["sessions_held"] == 2                       # sold on T+2, not before
    assert result["exit_price"] == pytest.approx(94.0)        # opened above the stop, so filled at the stop
    assert result["net_profit"] < -1200                       # 200 x 6 loss plus costs


def test_gap_below_the_stop_sells_at_the_open_not_at_the_stop() -> None:
    result = run([(100, 101, 99, 100), (100, 101, 99, 100), (90, 91, 89, 90), (90, 91, 90, 90)])
    assert result["status"] == "stop" and result["exit_price"] == pytest.approx(90.0)


def test_stop_wins_when_stop_and_target_are_touched_on_the_same_day() -> None:
    result = run([(100, 101, 99, 100), (100, 101, 99, 100), (100, 113, 93, 100), (100, 101, 99, 100)])
    assert result["status"] == "stop"


def test_target_1_sells_half_then_the_rest_is_trailed() -> None:
    rows = [(100, 101, 99, 100), (100, 104, 100, 104), (104, 113, 104, 112), (112, 116, 111, 115), (115, 118, 114, 117),
            (117, 118, 100, 102), (102, 103, 101, 102), (102, 103, 101, 102)]
    result = run(rows)
    assert result["status"] == "target_1"
    assert result["final_exit"] in {"trail", "trail_stop"}
    assert result["quantity"] == 200
    assert result["net_profit"] > 0 and result["tax"] > 0
    assert result["net_profit"] < result["gross_profit"]


def test_time_stop_sells_a_trade_that_goes_nowhere() -> None:
    result = run([(100, 101, 99, 100)] * 25)
    assert result["status"] == "time_stop"
    assert 15 <= result["sessions_held"] <= 17
    assert result["net_profit"] < 0                           # flat price still costs commission


def test_trade_still_running_when_the_data_ends_is_reported_open() -> None:
    result = run([(100, 101, 99, 100), (100, 102, 100, 102), (102, 104, 101, 103)])
    assert result["status"] == "open" and result["exit_price"] == pytest.approx(103.0)


def test_settle_charges_costs_and_tax_per_sale() -> None:
    day = pd.Timestamp("2026-01-05")
    legs = [
        {"tag": "t1", "quantity": 100, "exit_price": 110.0, "exit_date": day + pd.Timedelta(days=10), "reason": "target_1"},
        {"tag": "run", "quantity": 100, "exit_price": 95.0, "exit_date": day + pd.Timedelta(days=20), "reason": "stop"},
    ]
    money = settle(FEES, 100.0, day, legs)
    assert money["gross_profit"] == pytest.approx(500.0)
    # buy 20,000: 72 + 3; sell 11,000: 39.6 + 1.65 + 25; sell 9,500: 34.2 + 1.425 + 25
    assert money["costs"] == pytest.approx(72 + 3 + 39.6 + 1.65 + 25 + 34.2 + 1.425 + 25, abs=0.01)
    # tax only on the winning half: (1000 - 41.25 - 37.5) x 5%; the losing half does not reduce it
    assert money["tax"] == pytest.approx((1000 - 41.25 - 37.5) * 0.05, abs=0.01)
    assert money["net_profit"] == pytest.approx(500 - money["costs"] - money["tax"], abs=0.01)


def _signals(rows: list[dict]) -> pd.DataFrame:
    base = {"setup": "trend_pullback", "r_rule": 2.0, "is_buy": True, "filled": True, "score": 80.0, "reward_risk": 2.0,
            "status": "stop", "net_percent": 1.0, "sessions_held": 5}
    return pd.DataFrame([{**base, **row} for row in rows])


def test_repeat_signals_while_a_trade_is_open_are_counted_once() -> None:
    signals = _signals([
        {"symbol": "AAA", "date": "2026-01-05", "exit_date": "2026-01-20", "net_profit": 1000.0},
        {"symbol": "AAA", "date": "2026-01-06", "exit_date": "2026-01-21", "net_profit": 900.0},
        {"symbol": "AAA", "date": "2026-01-22", "exit_date": "2026-02-10", "net_profit": -500.0},
    ])
    kept = report.without_overlap(signals, ["symbol"])
    assert list(kept["date"]) == ["2026-01-05", "2026-01-22"]


def test_metrics_and_gate() -> None:
    wins = [{"symbol": f"W{i}", "date": f"2026-01-{i + 1:02d}", "exit_date": f"2026-02-{i + 1:02d}", "net_profit": 3000.0} for i in range(20)]
    losses = [{"symbol": f"L{i}", "date": f"2026-03-{i + 1:02d}", "exit_date": f"2026-04-{i + 1:02d}", "net_profit": -2000.0} for i in range(15)]
    table = report.setup_table(_signals(wins + losses), capital=500000)
    row = table.iloc[0]
    assert row["trades"] == 35 and row["profit_factor"] == pytest.approx(2.0)
    assert row["max_drawdown"] == pytest.approx(30000.0)
    gate = report.gate_table(table, table, 2.0).set_index("setup")
    assert bool(gate.loc["trend_pullback", "passed"]) is False        # 35 trades is fewer than the 50 the gate needs
    assert bool(gate.loc["base_breakout", "passed"]) is False         # no trades at all
    many = report.setup_table(_signals(wins * 3 + losses * 3).assign(symbol=lambda f: [f"S{i}" for i in range(len(f))]), capital=500000)
    assert bool(report.gate_table(many, many, 2.0).set_index("setup").loc["trend_pullback", "passed"]) is True
    assert bool(report.gate_table(many, table, 2.0).set_index("setup").loc["trend_pullback", "passed"]) is False   # must pass in BOTH periods
    text = report.evidence_document(many, table, 2.0, "recent", "earlier")
    assert "trend_pullback:" in text and "trades: 105" in text and "profit_factor: 2" in text
