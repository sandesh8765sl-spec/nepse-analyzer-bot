"""Trade plan rules: entry, stop, exit, position size and net results.

Redesigned in Stage 4b from what the backtests showed (docs/RESEARCH.md):

* the entry is at most 3% above the last close (buy the next day at or near the open);
* the stop is a multiple of the stock's own average daily range (ATR) below the
  entry - tight "structure" stops of 4-6% were hit within a week 58% of the time;
* a stock that would need a stop wider than the limit is too volatile: no plan;
* there is no fixed profit target. The stop follows the price up (highest price
  since entry minus N x ATR) and the trade is closed after a maximum holding
  time. The next resistance is shown for information only;
* the position is at least the owner's minimum size.

Every threshold comes from config/engine.yaml.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from nepse_bot.engine.config import Profile, setting
from nepse_bot.engine.indicators import swing_points
from nepse_bot.portfolio.fees import FeeSchedule

TICK = 0.1


def round_tick(price: float, up: bool = False) -> float:
    """Round a price to NEPSE's 10-paisa tick."""
    steps = price / TICK
    return round((math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)) * TICK, 1)


@dataclass(frozen=True)
class SetupSignal:
    """What a setup proposes before the plan rules are applied."""

    key: str
    name: str
    entry_low: float
    entry_high: float
    entry_ref: float                 # the planned buy price (also the after-market order limit)
    invalidation: float              # price level that proves the setup wrong
    invalidation_text: str
    hold_text: str
    why: tuple[str, ...] = ()
    measured_target: float | None = None
    ignore_resistance_below: float | None = None


@dataclass(frozen=True)
class TradePlan:
    """A complete plan that passed every rule."""

    setup_key: str
    setup_name: str
    entry_low: float
    entry_high: float
    entry: float
    amo_limit: float
    stop: float
    stop_percent: float
    stop_reason: str
    target_1: float
    target_1_percent: float
    target_1_reason: str
    target_2: float
    target_2_percent: float
    reward_risk: float
    quantity: int
    position_value: float
    risk_rupees: float               # net loss if the stop is hit, after costs
    net_at_target_1: float           # net profit if the whole position is sold at the first resistance
    net_at_target_2: float           # net profit if sold at 3x the risk
    cost_percent: float
    breakeven_stop: float            # entry plus costs (for information)
    hold_text: str
    time_stop_sessions: int | None
    trail_text: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    # how the trade is managed (read by the backtest and the live journal)
    entry_style: str = "open"        # buy at the next day's open
    t1_fraction: float = 0.0         # nothing is sold at a fixed target
    trail: str = "chandelier"
    trail_from: str = "entry"
    chandelier_multiple: float = 3.0
    max_hold_sessions: int | None = 40
    high_52w: float | None = None    # for the reference targets in the reports
    atr: float = 0.0                 # average daily range in rupees (for the trailing-stop examples)
    avg_range_percent: float = 0.0   # average high-low range of the last 20 days, % of price


@dataclass(frozen=True)
class PlanRejection:
    """Why no plan was produced."""

    kind: str                        # entry_too_far / stop_too_wide / position_too_small
    reason: str
    target_1: float | None = None
    stop: float | None = None


def _round_number_above(price: float) -> float:
    """The next big round number above a price."""
    step = 10.0 if price < 100 else 50.0 if price < 500 else 100.0 if price < 2000 else 500.0
    return (math.floor(price / step) + 1) * step


def resistances_above(frame: pd.DataFrame, price: float, ignore_below: float | None = None) -> list[tuple[float, str]]:
    """Real resistance levels above a price, nearest first, with a description.

    The newest bar is the signal bar itself, so its own high is not counted
    as a barrier; only price history before it is.
    """
    floor = max(price * 1.005, ignore_below or 0.0)
    history = frame.iloc[:-1] if len(frame) > 30 else frame
    # A "real" resistance is a peak that stood out for N bars on each side
    # (config: plan.resistance_pivot_bars). Small wiggles do not count.
    pivot_bars = int(setting("plan", "resistance_pivot_bars", default=10))
    swing_highs, _ = swing_points(history, window=pivot_bars, lookback=250)
    levels: list[tuple[float, str]] = [(level, "previous swing high") for level in swing_highs if level > floor]
    # The most recent peak is too new to be confirmed as a swing high, but it
    # is the first barrier a pullback has to get back through.
    recent_high = float(history["high"].tail(pivot_bars).max())
    if recent_high > floor:
        levels.append((recent_high, "recent high"))
    high_52w = float(history["high"].tail(252).max())
    if high_52w > floor:
        levels.append((high_52w, "52-week high"))
    # A round number only counts as a barrier when it is not right on top of the entry.
    round_number = _round_number_above(max(floor, price * 1.03))
    levels.append((round_number, f"round number {round_number:,.0f}"))
    levels.sort()
    merged: list[tuple[float, str]] = []
    for level, label in levels:
        if merged and level <= merged[-1][0] * 1.015:
            continue  # same zone as the previous level
        merged.append((level, label))
    return merged


def build_plan(
    signal: SetupSignal,
    frame: pd.DataFrame,
    profile: Profile,
    fees: FeeSchedule,
    available_cash: float | None = None,
) -> TradePlan | PlanRejection:
    """Apply every plan rule to a setup signal.

    Args:
        signal: The setup's proposal.
        frame: The stock's bars with indicators (adjusted prices), oldest first.
        profile: The owner's capital and risk settings.
        fees: The cost rules.
        available_cash: Cash not already in open positions (defaults to total capital).
    """
    last_close = float(frame["close"].iloc[-1])
    atr = float(frame["atr14"].iloc[-1]) if pd.notna(frame["atr14"].iloc[-1]) else last_close * 0.03
    entry = round_tick(signal.entry_ref)
    cfg = lambda key, default: float(setting("plan", key, default=default))  # noqa: E731

    # ---- entry
    max_above = cfg("max_entry_above_close_percent", 3)
    if entry > last_close * (1 + max_above / 100.0) + 1e-9:
        return PlanRejection("entry_too_far", f"entry {entry:,.1f} is more than {max_above:.0f}% above the last close {last_close:,.1f}")

    # ---- stop: a multiple of the stock's own average daily range below the entry
    stop_multiple = cfg("stop_atr_multiple", 2.5)
    stop_min, stop_max = cfg("stop_min_percent", 4), cfg("stop_max_percent", 12.5)
    stop_percent = stop_multiple * atr / entry * 100.0
    if stop_percent > stop_max:
        return PlanRejection(
            "stop_too_wide",
            f"too volatile: a stop that normal movement does not hit would be {stop_percent:.1f}% below entry; the limit is {stop_max:g}%",
            stop=round_tick(entry * (1 - stop_percent / 100.0)),
        )
    reason = f"{stop_multiple:g}x the average daily range below the entry"
    if stop_percent < stop_min:
        stop_percent = stop_min
        reason = f"{reason}, widened to {stop_min:g}%"
    stop = round_tick(entry * (1 - stop_percent / 100.0))
    risk_per_share = entry - stop
    stop_percent = risk_per_share / entry * 100.0

    # ---- position size
    cash = profile.total_capital if available_cash is None else max(0.0, available_cash)
    value = min(profile.max_position, cash, profile.risk_budget / (stop_percent / 100.0))
    quantity = fees.round_lot(value / entry)
    position_value = quantity * entry
    if position_value < profile.min_position:
        return PlanRejection(
            "position_too_small",
            f"position too small for your rules: Rs {position_value:,.0f} is below your minimum of Rs {profile.min_position:,.0f}",
            stop=stop,
        )
    cost_percent = fees.round_trip_cost_percent(quantity, entry)

    # ---- reference levels (information only: the exit is the trailing stop, not a target)
    levels = resistances_above(frame, entry, signal.ignore_resistance_below)
    target_1, target_1_reason = levels[0]
    target_1 = round_tick(target_1)
    target_2 = round_tick(max(entry + cfg("target2_r", 3.0) * risk_per_share, target_1 + TICK))

    # ---- money, after commission, SEBON fee, DP charge and capital gains tax
    net_1 = fees.round_trip(quantity, entry, target_1, 25).net_profit
    net_2 = fees.round_trip(quantity, entry, target_2, 30).net_profit
    loss_at_stop = -fees.round_trip(quantity, entry, stop, 10).net_profit

    notes = []
    average_range = float(frame["range_pct"].tail(20).mean())
    if average_range > cfg("gap_risk_avg_range_percent", 5):
        notes.append(f"Gap risk: this stock moves {average_range:.1f}% a day on average, so price can jump past the stop.")
    trail_multiple = cfg("chandelier_atr_multiple", 3.0)
    max_hold = int(cfg("max_hold_sessions", 40))
    trail = (
        f"a stop that follows the price up: the highest price since you bought minus {price_text(trail_multiple * atr)} "
        f"({trail_multiple:g}x the average daily range). It never moves down"
    )
    return TradePlan(
        setup_key=signal.key,
        setup_name=signal.name,
        # The buy zone never reaches further down than a quarter of the way to the stop.
        entry_low=max(round_tick(signal.entry_low), round_tick(entry - 0.25 * risk_per_share)),
        entry_high=max(round_tick(signal.entry_high, up=True), entry),
        entry=entry,
        amo_limit=entry,
        stop=stop,
        stop_percent=round(stop_percent, 2),
        stop_reason=reason,
        target_1=target_1,
        target_1_percent=round((target_1 - entry) / entry * 100.0, 2),
        target_1_reason=target_1_reason,
        target_2=target_2,
        target_2_percent=round((target_2 - entry) / entry * 100.0, 2),
        reward_risk=round((target_1 - entry) / risk_per_share, 2),
        quantity=quantity,
        position_value=position_value,
        risk_rupees=round(loss_at_stop, 0),
        net_at_target_1=round(net_1, 0),
        net_at_target_2=round(net_2, 0),
        cost_percent=round(cost_percent, 3),
        breakeven_stop=round_tick(entry * (1 + cost_percent / 100.0), up=True),
        hold_text=f"up to {max_hold} trading days (about {max_hold // 5} weeks)",
        time_stop_sessions=None,
        trail_text=trail,
        notes=tuple(notes),
        chandelier_multiple=trail_multiple,
        max_hold_sessions=max_hold,
        high_52w=round_tick(float(frame["high"].tail(252).max())),
        atr=round(atr, 2),
        avg_range_percent=round(average_range, 2),
    )


def price_text(value: float) -> str:
    """A rupee distance without needless decimals."""
    return f"Rs {value:,.0f}" if value >= 20 else f"Rs {value:,.1f}"


def resistance_is_close(plan: Any) -> bool:
    """Is the reference target less than the configured multiple of the stop distance above the entry?"""
    return float(plan.reward_risk) < float(setting("plan", "resistance_warning_reward_risk", default=1.5))


def resistance_warning(plan: Any) -> str | None:
    """Information-only warning shown with a plan whose reference target is close. Never changes a grade."""
    if not resistance_is_close(plan):
        return None
    return (
        f"⚠️ Resistance close above at {plan.target_1:,.0f} (+{plan.target_1_percent:.1f}%): "
        f"upside may be limited until it breaks above {plan.target_1:,.0f}"
    )


def resistance_rank_factor(plan: Any | None) -> float:
    """Ranking multiplier: slightly below 1 when resistance is close above."""
    if plan is None or not resistance_is_close(plan):
        return 1.0
    return float(setting("plan", "resistance_rank_penalty", default=0.97))
