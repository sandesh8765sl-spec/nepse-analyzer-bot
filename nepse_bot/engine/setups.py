"""Trade setups. Each one is a small strategy with its own entry and exit logic.

A setup looks at one stock on one day and answers one of three things:

* a ``SetupSignal`` - the setup is valid today (the plan rules still have to pass);
* a watch trigger - a good candidate, with the price and condition to wait for;
* nothing - this setup does not apply.

Setups only see completed daily bars. Stage 4 backtests each setup separately,
and only setups that pass stay enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from nepse_bot.engine.config import disabled_setups, setting
from nepse_bot.engine.plan import SetupSignal


@dataclass
class StockContext:
    """Everything a setup may look at for one stock."""

    symbol: str
    frame: pd.DataFrame                      # bars with indicators, oldest first
    weekly: dict[str, float | None] = field(default_factory=dict)
    rs_rank: float | None = None             # 0-100 percentile of relative strength
    value_score: float | None = None         # 0-100, high = cheap versus its sector
    eps_growth_yoy: float | None = None      # percent
    broker: dict[str, Any] | None = None     # broker-flow summary, if enough history
    book_closure_in_days: int | None = None  # trading days until the next book closure
    bonus_percent_pending: float | None = None

    @property
    def last(self) -> pd.Series:
        return self.frame.iloc[-1]

    def value(self, column: str) -> float | None:
        item = self.last.get(column)
        return float(item) if item is not None and pd.notna(item) else None


@dataclass(frozen=True)
class SetupResult:
    """What one setup concluded."""

    key: str
    name: str
    signal: SetupSignal | None = None
    watch: str | None = None
    trigger: float | None = None     # the price a close must clear for the watch to become a buy


class Setup:
    """Base class for a setup."""

    key = "setup"
    name = "Setup"
    hold_text = "about 2-5 weeks"

    def enabled(self) -> bool:
        return bool(setting("setups", self.key, default=False)) and self.key not in disabled_setups()

    def evaluate(self, ctx: StockContext) -> SetupResult | None:  # pragma: no cover - interface
        raise NotImplementedError

    def _result(self, signal: SetupSignal | None = None, watch: str | None = None, trigger: float | None = None) -> SetupResult:
        return SetupResult(self.key, self.name, signal, watch, trigger)

    @staticmethod
    def in_uptrend(ctx: StockContext) -> bool:
        close, ema20, ema50, sma200 = ctx.value("close"), ctx.value("ema20"), ctx.value("ema50"), ctx.value("sma200")
        if close is None or ema20 is None or ema50 is None:
            return False
        if close <= ema50 or ema20 < ema50 * 0.99:
            return False
        if sma200 is not None and (close <= sma200 or ema50 <= sma200):
            return False
        weekly_close, weekly_ma10 = ctx.weekly.get("close"), ctx.weekly.get("ma10")
        if weekly_close is not None and weekly_ma10 is not None and weekly_close < weekly_ma10:
            return False
        return True


class TrendPullback(Setup):
    """A. An uptrending stock pulls back to its 20- or 50-day average and turns up."""

    key = "trend_pullback"
    name = "Trend pullback"
    hold_text = "about 2-5 weeks"

    def evaluate(self, ctx: StockContext) -> SetupResult | None:
        if not self.in_uptrend(ctx):
            return None
        frame = ctx.frame
        close, ema20, ema50 = ctx.value("close"), ctx.value("ema20"), ctx.value("ema50")
        rsi_now, rsi_before = ctx.value("rsi14"), float(frame["rsi14"].iloc[-2]) if len(frame) > 1 else None
        assert close is not None and ema20 is not None and ema50 is not None
        support = ema20 if close >= ema20 * 0.98 else ema50
        support_name = "20-day average" if support == ema20 else "50-day average"
        touched = float(frame["low"].tail(3).min()) <= support * 1.02 and close >= support * 0.98
        if not touched:
            return self._result(
                watch=f"Uptrend, but extended. Buy near {ema20 * 0.99:,.0f}-{ema20 * 1.01:,.0f} "
                "(a pullback to its 20-day average) once it turns back up."
            )
        turning_up = rsi_now is not None and rsi_before is not None and 40 <= rsi_now <= 55 and rsi_now > rsi_before
        volume_dried = float(frame["turnover"].tail(5).mean()) < float(frame["turnover_avg20"].iloc[-1] or 0) * 1.0
        if not turning_up or not volume_dried:
            trigger = float(frame["high"].iloc[-1])
            missing = "momentum has not turned up yet" if not turning_up else "selling volume has not dried up yet"
            return self._result(watch=f"Pulling back to its {support_name} ({support:,.0f}); {missing}. Buy if it closes above {trigger:,.0f}.", trigger=trigger)
        pullback_low = float(frame["low"].tail(10).min())
        return self._result(
            SetupSignal(
                key=self.key,
                name=self.name,
                entry_low=min(close, support * 0.995),
                entry_high=close * 1.01,
                entry_ref=close,
                invalidation=pullback_low,
                invalidation_text="below the low of this pullback",
                hold_text=self.hold_text,
                why=(f"Uptrend pulled back to its {support_name} and momentum is turning up", "Volume dried up during the pullback"),
            )
        )


class BaseBreakout(Setup):
    """B. A completed daily close above a tight 20-60 day range, on strong volume."""

    key = "base_breakout"
    name = "Base breakout"
    hold_text = "about 2-6 weeks"

    def evaluate(self, ctx: StockContext) -> SetupResult | None:
        frame = ctx.frame
        close = ctx.value("close")
        if close is None or len(frame) < 70:
            return None
        shortest, longest = setting("setups", "breakout_base_days", default=[20, 60])
        max_width = float(setting("setups", "breakout_max_base_range_percent", default=20))
        base = None
        for days in (int(longest), 40, int(shortest)):
            prior = frame.iloc[-(days + 1) : -1]
            high, low = float(prior["high"].max()), float(prior["low"].min())
            if low > 0 and (high - low) / low * 100.0 <= max_width:
                base = (days, high, low)
                break
        if base is None:
            return None
        days, range_high, range_low = base
        multiple = float(setting("setups", "breakout_volume_multiple", default=1.5))
        average_before = float(frame["turnover_avg20"].iloc[-2]) if pd.notna(frame["turnover_avg20"].iloc[-2]) else 0.0
        volume_ok = average_before > 0 and float(frame["turnover"].iloc[-1]) >= multiple * average_before
        band = float(setting("plan", "breakout_entry_band_percent", default=2)) / 100.0

        if close <= range_high:
            if close >= range_high * 0.97:
                return self._result(
                    watch=f"{days}-day base under {range_high:,.0f}. Buy if it closes above {range_high:,.0f} on at least {multiple:.1f}x normal volume.", trigger=range_high
                )
            return None
        if not volume_ok:
            return self._result(watch=f"Closed above its {days}-day range ({range_high:,.0f}) but on weak volume. Wait for a close above it on {multiple:.1f}x volume.", trigger=range_high)
        if close > range_high * (1 + band + 0.01):
            return self._result(watch=f"Broke out above {range_high:,.0f} and has already run. Buy only on a retest near {range_high:,.0f}-{range_high * (1 + band):,.0f}.")
        return self._result(
            SetupSignal(
                key=self.key,
                name=self.name,
                entry_low=range_high,
                entry_high=range_high * (1 + band),
                entry_ref=min(close, range_high * (1 + band)),
                invalidation=range_high - (range_high - range_low) * 0.5,
                invalidation_text="back inside the middle of the base",
                hold_text=self.hold_text,
                why=(
                    f"Closed above a {days}-day range high ({range_high:,.0f})",
                    f"Volume was {float(frame['turnover'].iloc[-1]) / average_before:.1f}x normal",
                ),
                measured_target=range_high + (range_high - range_low),
                ignore_resistance_below=range_high * 1.01,
            )
        )


class RelativeStrengthLeader(Setup):
    """C. A market leader near its 52-week high, bought on a small pullback or tight flag."""

    key = "rs_leader"
    name = "Relative-strength leader"
    hold_text = "about 3-8 weeks"

    def evaluate(self, ctx: StockContext) -> SetupResult | None:
        frame = ctx.frame
        close, high_52w = ctx.value("close"), ctx.value("high_252")
        if close is None or high_52w is None or ctx.rs_rank is None or len(frame) < 200:
            return None
        if ctx.rs_rank < float(setting("setups", "rs_leader_min_rank", default=90)):
            return None
        if close < high_52w * (1 - float(setting("setups", "rs_leader_near_high_percent", default=10)) / 100.0):
            return None
        if not self.in_uptrend(ctx):
            return None
        recent_high = float(frame["high"].tail(10).max())
        pullback = (recent_high - close) / recent_high * 100.0
        flag_width = (float(frame["high"].tail(5).max()) - float(frame["low"].tail(5).min())) / close * 100.0
        if not (2.0 <= pullback <= 8.0 or flag_width <= 6.0):
            return self._result(watch=f"Market leader (strength rank {ctx.rs_rank:.0f}) but extended. Buy on a 3-6% pullback, near {recent_high * 0.96:,.0f}.")
        return self._result(
            SetupSignal(
                key=self.key,
                name=self.name,
                entry_low=close * 0.98,
                entry_high=close * 1.01,
                entry_ref=close,
                invalidation=float(frame["low"].tail(10).min()),
                invalidation_text="below the low of the last 10 sessions",
                hold_text=self.hold_text,
                why=(
                    f"Relative strength rank {ctx.rs_rank:.0f} of 100",
                    f"Within {(high_52w - close) / high_52w * 100:.0f}% of its 52-week high, resting in a tight range",
                ),
            )
        )


class ValueTurn(Setup):
    """D. Cheap versus its sector, earnings improving, price reclaiming its 50-day average."""

    key = "value_turn"
    name = "Value + turn"
    hold_text = "about 4-10 weeks"

    def evaluate(self, ctx: StockContext) -> SetupResult | None:
        frame = ctx.frame
        close, ema50 = ctx.value("close"), ctx.value("ema50")
        if close is None or ema50 is None or ctx.value_score is None or ctx.eps_growth_yoy is None:
            return None
        if ctx.value_score < 70 or ctx.eps_growth_yoy <= 0:
            return None
        if close <= ema50:
            return self._result(watch=f"Cheap versus its sector with rising earnings. Buy when it closes above its 50-day average ({ema50:,.0f}).", trigger=ema50)
        was_below = bool((frame["close"].iloc[-11:-1] < frame["ema50"].iloc[-11:-1]).any())
        if not was_below:
            return None  # reclaimed long ago: other setups judge it from here
        return self._result(
            SetupSignal(
                key=self.key,
                name=self.name,
                entry_low=ema50,
                entry_high=close * 1.01,
                entry_ref=close,
                invalidation=float(frame["low"].tail(20).min()),
                invalidation_text="below the low of the last 20 sessions",
                hold_text=self.hold_text,
                why=(
                    f"Cheaper than {ctx.value_score:.0f}% of its sector on P/E and P/B",
                    f"Earnings per share up {ctx.eps_growth_yoy:.0f}% on the same quarter last year",
                    "Price has just reclaimed its 50-day average",
                ),
            )
        )


class BrokerAccumulation(Setup):
    """E. Top brokers keep buying while the price stays in a range."""

    key = "broker_accumulation"
    name = "Broker accumulation"
    hold_text = "about 3-8 weeks"

    def evaluate(self, ctx: StockContext) -> SetupResult | None:
        broker = ctx.broker
        needed = int(setting("setups", "broker_accumulation_min_days", default=10))
        if not broker or broker.get("days", 0) < needed:
            return None  # not enough broker history yet
        frame = ctx.frame
        close = ctx.value("close")
        if close is None:
            return None
        width = (float(frame["high"].tail(20).max()) - float(frame["low"].tail(20).min())) / close * 100.0
        net_share = float(broker.get("top5_net_share_10d") or 0.0)
        if width > 12.0 or net_share < 15.0:
            return None
        return self._result(
            SetupSignal(
                key=self.key,
                name=self.name,
                entry_low=close * 0.98,
                entry_high=close * 1.01,
                entry_ref=close,
                invalidation=float(frame["low"].tail(20).min()),
                invalidation_text="below the bottom of the accumulation range",
                hold_text=self.hold_text,
                why=(
                    f"Top 5 brokers net bought {net_share:.0f}% of traded volume over 10 days",
                    f"Price held a {width:.0f}% range while they bought",
                ),
            )
        )


class EventRunUp(Setup):
    """F. Buy ahead of an announced bonus book closure, exit before it. Off by default."""

    key = "event_runup"
    name = "Event run-up"
    hold_text = "until 2 sessions before book closure"

    def evaluate(self, ctx: StockContext) -> SetupResult | None:
        days, bonus = ctx.book_closure_in_days, ctx.bonus_percent_pending
        close = ctx.value("close")
        if days is None or bonus is None or close is None or not (8 <= days <= 25) or bonus < 10 or not self.in_uptrend(ctx):
            return None
        return self._result(
            SetupSignal(
                key=self.key,
                name=self.name,
                entry_low=close * 0.98,
                entry_high=close * 1.01,
                entry_ref=close,
                invalidation=float(ctx.frame["low"].tail(10).min()),
                invalidation_text="below the low of the last 10 sessions",
                hold_text=self.hold_text,
                why=(f"{bonus:.0f}% bonus with book closure in {days} sessions",),
            )
        )


ALL_SETUPS: tuple[Setup, ...] = (
    BaseBreakout(),
    TrendPullback(),
    RelativeStrengthLeader(),
    ValueTurn(),
    BrokerAccumulation(),
    EventRunUp(),
)


def enabled_setups() -> list[Setup]:
    """Return the setups switched on in config/engine.yaml."""
    return [item for item in ALL_SETUPS if item.enabled()]
