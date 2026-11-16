"""Fees, tax and the trade-plan rules (the fix for "buy 301, sell 303")."""

from __future__ import annotations

import pandas as pd
import pytest

from nepse_bot.engine.config import Profile
from nepse_bot.engine.indicators import add_indicators
from nepse_bot.engine.plan import PlanRejection, SetupSignal, TradePlan, build_plan, resistances_above, round_tick
from nepse_bot.portfolio.fees import FeeSchedule, fee_schedule

FEES = FeeSchedule(
    commission_tiers=((50000.0, 0.36), (500000.0, 0.33), (2000000.0, 0.31), (10000000.0, 0.27), (None, 0.24)),
    commission_minimum=10.0, sebon_percent=0.015, dp_charge=25.0,
    cgt_short_percent=5.0, cgt_long_percent=3.75, cgt_long_after_days=365, board_lot=10,
)
PROFILE = Profile(total_capital=500000, min_position=50000, max_position=100000, max_open_positions=5, risk_percent=2.0, risk_rupees=None)


# ----------------------------------------------------------------------- fees
def test_commission_tiers_and_minimum() -> None:
    assert FEES.commission(50000) == pytest.approx(180.0)        # 0.36% up to Rs 50,000
    assert FEES.commission(55000) == pytest.approx(181.5)        # 0.33% above that
    assert FEES.commission(600000) == pytest.approx(1860.0)      # 0.31% above Rs 5 lakh
    assert FEES.commission(2000) == pytest.approx(10.0)          # minimum Rs 10
    assert FEES.commission(0) == 0.0


def test_worked_example_buy_500_sell_550() -> None:
    # Buy 100 @ 500 = 50,000: commission 180 + SEBON 7.50
    assert FEES.buy_cost(100, 500) == pytest.approx(50187.5)
    result = FEES.round_trip(100, 500, 550, holding_days=20)
    # Sell 100 @ 550 = 55,000: commission 181.50 + SEBON 8.25 + DP 25
    assert result.costs == pytest.approx(180 + 181.5 + 7.5 + 8.25 + 25)
    # Taxable gain = (55,000 - 181.50 - 8.25) - 50,187.50 = 4,622.75; tax 5%
    assert result.tax == pytest.approx(231.1375)
    assert result.gross_profit == pytest.approx(5000.0)
    assert result.net_profit == pytest.approx(5000 - 402.25 - 231.1375)
    assert result.net_percent == pytest.approx(8.733, abs=0.01)


def test_long_term_tax_rate_and_no_tax_on_a_loss() -> None:
    assert FEES.round_trip(100, 500, 550, holding_days=400).tax == pytest.approx(4622.75 * 0.0375)
    assert FEES.cgt_percent(365) == 5.0 and FEES.cgt_percent(366) == 3.75
    loss = FEES.round_trip(100, 500, 480)
    assert loss.tax == 0.0 and loss.net_profit < -2000


def test_board_lot_rounding_and_real_config() -> None:
    assert FEES.round_lot(243.9) == 240 and FEES.round_lot(9) == 0 and FEES.round_lot(250) == 250
    real = fee_schedule()
    assert real.board_lot == 10 and real.sebon_percent == 0.015 and real.dp_charge == 25
    assert 0.6 < real.round_trip_cost_percent(240, 412) < 0.9


# ----------------------------------------------------------------- plan rules
def _frame(closes: list[float], spread: float = 0.002) -> pd.DataFrame:
    index = pd.bdate_range("2025-06-02", periods=len(closes))
    close = pd.Series(closes, index=index, dtype=float)
    frame = pd.DataFrame({"open": close, "high": close * (1 + spread), "low": close * (1 - spread), "close": close, "turnover": 20_000_000.0})
    return add_indicators(frame)


def _uptrend(end: float = 400.0, bars: int = 260) -> pd.DataFrame:
    """A smooth rise with nothing above the last close: no overhead resistance."""
    return _frame([end * (0.75 + 0.25 * i / (bars - 1)) for i in range(bars)])


def _signal(frame: pd.DataFrame, invalidation: float = 380.0, entry: float | None = None) -> SetupSignal:
    close = float(frame["close"].iloc[-1])
    return SetupSignal(
        key="test", name="Test setup", entry_low=close * 0.99, entry_high=close * 1.01, entry_ref=entry or close,
        invalidation=invalidation, invalidation_text="below the test level", hold_text="about 2-5 weeks",
    )


def test_a_good_plan_obeys_every_rule() -> None:
    frame = _uptrend()
    plan = build_plan(_signal(frame), frame, PROFILE, FEES)
    assert isinstance(plan, TradePlan)
    risk = plan.entry - plan.stop
    assert 4.0 <= plan.stop_percent <= 12.5
    assert plan.target_2 > plan.target_1 > plan.entry
    assert plan.quantity % 10 == 0 and PROFILE.min_position <= plan.position_value <= PROFILE.max_position
    assert plan.breakeven_stop > plan.entry
    gross = plan.quantity * (plan.target_1 - plan.entry)
    assert 0 < plan.net_at_target_1 < gross, "net profit is after fees and tax"
    assert plan.risk_rupees > plan.quantity * risk, "the loss at the stop includes costs"


def test_the_exit_is_a_trailing_stop_not_a_nearby_target() -> None:
    # The owner's old complaint was "buy 301, sell 303". The plan no longer sells at a
    # target at all: a resistance 1% above the entry is only shown for information, and
    # the trade is managed with a trailing stop and a maximum holding time.
    frame = _uptrend()
    frame.iloc[-60, frame.columns.get_loc("high")] = 404.0  # an old peak just 1% above today's close of 400
    assert resistances_above(frame, 400.0)[0][0] == pytest.approx(404.0)
    plan = build_plan(_signal(frame), frame, PROFILE, FEES)
    assert isinstance(plan, TradePlan)
    assert plan.t1_fraction == 0.0, "nothing is sold at the nearby level"
    assert plan.trail == "chandelier" and plan.trail_from == "entry" and plan.max_hold_sessions == 40
    assert plan.time_stop_sessions is None
    assert plan.target_1 == pytest.approx(404.0) and plan.target_2 >= plan.entry + 3 * (plan.entry - plan.stop) - 0.05


def test_stop_comes_from_the_stocks_own_daily_range() -> None:
    frame = _frame([400.0 * (0.75 + 0.25 * i / 259) for i in range(260)], spread=0.015)   # moves about 3% a day
    plan = build_plan(_signal(frame), frame, PROFILE, FEES)
    assert isinstance(plan, TradePlan)
    expected = 2.5 * float(frame["atr14"].iloc[-1]) / plan.entry * 100.0
    assert plan.stop_percent == pytest.approx(expected, abs=0.1) and "2.5x the average daily range" in plan.stop_reason


def test_todays_own_high_is_not_treated_as_resistance() -> None:
    frame = _uptrend()
    frame.iloc[-1, frame.columns.get_loc("high")] = 404.0    # the signal bar's own high
    assert isinstance(build_plan(_signal(frame), frame, PROFILE, FEES), TradePlan)


def test_a_stock_too_volatile_for_a_sensible_stop_gets_no_plan() -> None:
    frame = _frame([400.0 * (0.75 + 0.25 * i / 259) for i in range(260)], spread=0.07)  # very volatile
    outcome = build_plan(_signal(frame, invalidation=300.0), frame, PROFILE, FEES)
    assert isinstance(outcome, PlanRejection) and outcome.kind == "stop_too_wide" and "too volatile" in outcome.reason


def test_stop_tighter_than_four_percent_is_widened() -> None:
    frame = _uptrend()
    plan = build_plan(_signal(frame, invalidation=399.0), frame, PROFILE, FEES)
    assert isinstance(plan, TradePlan)
    assert plan.stop_percent == pytest.approx(4.0, abs=0.05) and "widened" in plan.stop_reason


def test_entry_more_than_three_percent_above_the_close_is_rejected() -> None:
    frame = _uptrend()
    outcome = build_plan(_signal(frame, entry=416.5), frame, PROFILE, FEES)
    assert isinstance(outcome, PlanRejection) and outcome.kind == "entry_too_far"
    assert isinstance(build_plan(_signal(frame, entry=410.0), frame, PROFILE, FEES), (TradePlan, PlanRejection))


def test_quantity_is_rounded_down_to_the_board_lot() -> None:
    frame = _uptrend()
    odd = Profile(total_capital=500000, min_position=50000, max_position=97300, max_open_positions=5, risk_percent=2.0, risk_rupees=None)
    plan = build_plan(_signal(frame), frame, odd, FEES)
    assert isinstance(plan, TradePlan) and plan.quantity == 240   # 97,300 / 400 = 243.25 -> 240


def test_position_below_the_minimum_is_not_suggested() -> None:
    frame = _uptrend()
    cautious = Profile(total_capital=500000, min_position=50000, max_position=100000, max_open_positions=5, risk_percent=2.0, risk_rupees=1000.0)
    outcome = build_plan(_signal(frame), frame, cautious, FEES)   # Rs 1,000 risk / 4% stop = Rs 25,000
    assert isinstance(outcome, PlanRejection) and outcome.kind == "position_too_small"
    assert "position too small for your rules" in outcome.reason


def test_risk_budget_limits_size_when_the_stop_is_wide() -> None:
    frame = _frame([400.0 * (0.75 + 0.25 * i / 259) for i in range(260)], spread=0.02)
    small_risk = Profile(total_capital=500000, min_position=50000, max_position=100000, max_open_positions=5, risk_percent=1.0, risk_rupees=None)
    plan = build_plan(_signal(frame, invalidation=376.0), frame, small_risk, FEES)
    if isinstance(plan, TradePlan):   # Rs 5,000 risk / stop% caps the position below the Rs 1 lakh maximum
        assert plan.position_value <= small_risk.risk_budget / (plan.stop_percent / 100.0) + 400


def test_available_cash_limits_the_position() -> None:
    frame = _uptrend()
    plan = build_plan(_signal(frame), frame, PROFILE, FEES, available_cash=60000)
    assert isinstance(plan, TradePlan) and plan.position_value <= 60000


def test_tick_rounding() -> None:
    assert round_tick(412.37) == 412.3 and round_tick(412.31, up=True) == 412.4 and round_tick(412.3, up=True) == 412.3
