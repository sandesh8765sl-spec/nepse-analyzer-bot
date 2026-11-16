"""The alert filters. Pure functions: no database, no clock, no Telegram.

* ``top_pick_failures`` / ``choose_top_pick`` - the single best SPECULATIVE candidate of the day;
* ``early_failures`` - the stricter rules for an alert DURING the session;
* ``may_alert_again`` - the 10-trading-day no-repeat rule;
* ``confirm_early`` - CONFIRMED or CANCELLED after the close.

Every threshold comes from config/alerts.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nepse_bot.alerts.config import alert_setting
from nepse_bot.engine.config import setting as engine_setting
from nepse_bot.engine.plan import resistance_rank_factor

BUY_GRADES = ("STRONG BUY", "BUY")


# ------------------------------------------------------------------- top pick
def top_pick_failures(item: Any, overheated: bool, price_events: list[str]) -> list[str]:
    """Why a candidate is NOT a top pick (empty list = it passes every rule)."""
    cfg = lambda key, default: float(alert_setting("top_pick", key, default=default))  # noqa: E731
    plan = item.plan
    failures: list[str] = []
    if item.verdict != "SPECULATIVE" or plan is None:
        return ["not a speculative candidate with a plan"]
    if item.score < cfg("min_score", 80):
        failures.append(f"score {item.score:.0f} is below {cfg('min_score', 80):.0f}")
    if item.confidence != "High":
        failures.append("the score does not rest on full data")
    if not plan.avg_range_percent or plan.avg_range_percent > cfg("max_avg_range_percent", 2.5):
        failures.append(f"not a calm stock (average daily range {plan.avg_range_percent:.1f}% of price)")
    if item.sector_state is not None and item.sector_state.label == "weak":
        failures.append(f"its sector ({item.sector}) is weak")
    if overheated:
        failures.append("the market is overheated")
    if price_events:
        failures.append("a price-adjusting event falls inside the holding window: " + price_events[0])
    # Only when the broker study switched it on (config/engine.yaml -> broker_filter.filter_top_pick).
    if engine_setting("broker_filter", "filter_top_pick", default=False) is True:
        from nepse_bot.engine.engine import has_broker_buying

        if not has_broker_buying(getattr(item, "broker_net_share", None)):
            failures.append("no clear buying by the top brokers")
    return failures


def choose_top_pick(candidates: list[Any], overheated: bool, price_events: dict[str, list[str]]) -> Any | None:
    """The best candidate that passes every top-pick rule (highest score), or None."""
    passing = [item for item in candidates if not top_pick_failures(item, overheated, price_events.get(item.symbol, []))]
    # A close resistance never disqualifies; it only ranks the stock slightly lower.
    return max(passing, key=lambda item: item.score * resistance_rank_factor(item.plan), default=None)


# ------------------------------------------------------------------ no repeat
def plan_fingerprint(verdict: str, plan: Any) -> dict[str, Any]:
    return {"verdict": verdict, "setup": plan.setup_key, "entry": float(plan.entry), "stop": float(plan.stop)}


def material_change(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Did the plan change enough to justify a second alert? (new setup or grade, or entry / stop moved)."""
    limit = float(alert_setting("material_change_percent", default=3)) / 100.0
    if previous.get("setup") != current.get("setup") or previous.get("verdict") != current.get("verdict"):
        return True
    for key in ("entry", "stop"):
        old, new = previous.get(key), current.get(key)
        if old and new and abs(float(new) / float(old) - 1.0) >= limit:
            return True
    return False


def may_alert_again(previous: dict[str, Any] | None, sessions_since: int | None, current: dict[str, Any]) -> bool:
    """The no-repeat rule: not within 10 trading days of the last alert unless the plan changed materially."""
    if previous is None or sessions_since is None:
        return True
    if sessions_since >= int(alert_setting("no_repeat_trading_days", default=10)):
        return True
    return material_change(previous, current)


# --------------------------------------------------------------- early alerts
@dataclass(frozen=True)
class EarlyState:
    """Everything an early alert is judged on, at one moment of the session."""

    symbol: str
    trigger: float                         # the level a close must clear
    price: float                           # live price
    samples: tuple[tuple[datetime, float], ...]   # live prices seen so far today, oldest first
    now: datetime
    session_open: datetime
    session_close: datetime
    volume_today: float | None
    normal_volume: float | None            # average full-day volume
    day_change_percent: float | None
    price_limit_percent: float             # the daily price limit in force
    index_change_percent: float | None     # NEPSE today
    sent_today: int = 0
    already_alerted: bool = False


def held_above(samples: tuple[tuple[datetime, float], ...], level: float, now: datetime, minutes: float, max_gap: float) -> bool:
    """True if every price check of the last ``minutes`` was at or above ``level``, with no long gap between checks."""
    if not samples:
        return False
    start = now - timedelta(minutes=minutes)
    run: list[datetime] = []
    for moment, price in reversed(samples):          # walk back from the newest check
        if price < level:
            break
        if run and (run[-1] - moment) > timedelta(minutes=max_gap):
            break
        run.append(moment)
        if moment <= start:
            return (now - samples[-1][0]) <= timedelta(minutes=max_gap)
    return False


def early_failures(state: EarlyState) -> list[str]:
    """Why an early alert is NOT sent now (empty list = send it)."""
    cfg = lambda key, default: alert_setting("early", key, default=default)  # noqa: E731
    failures: list[str] = []
    if state.already_alerted:
        return ["already alerted today"]
    if state.sent_today >= int(cfg("max_per_day", 2)):
        return ["the daily limit of early alerts is reached"]
    if not state.session_open <= state.now <= state.session_close:
        return ["the market is not open"]
    hour, minute = (int(part) for part in str(cfg("start_time", "12:30")).split(":"))
    earliest = max(state.now.replace(hour=hour, minute=minute, second=0, microsecond=0), state.session_open + timedelta(minutes=90))
    if state.now < earliest:
        failures.append(f"too early in the session (before {earliest.strftime('%H:%M')})")
    hold_level = state.trigger * (1 + float(cfg("hold_above_trigger_percent", 1)) / 100.0)
    if state.price < hold_level:
        failures.append(f"price {state.price:,.1f} is not 1% above the trigger {state.trigger:,.1f}")
    elif not held_above(state.samples, hold_level, state.now, float(cfg("hold_minutes", 30)), float(cfg("max_sample_gap_minutes", 12))):
        failures.append(f"has not held above {hold_level:,.1f} for {int(cfg('hold_minutes', 30))} minutes yet (could be a spike)")
    ceiling = state.trigger * (1 + float(cfg("max_above_trigger_percent", 4)) / 100.0)
    if state.price > ceiling:
        failures.append(f"already above the buy range (don't buy above {ceiling:,.1f})")
    elapsed = (state.now - state.session_open).total_seconds()
    length = (state.session_close - state.session_open).total_seconds()
    if not state.volume_today or not state.normal_volume or elapsed <= 0:
        failures.append("volume is unknown")
    else:
        pace = state.volume_today / min(1.0, elapsed / length) / state.normal_volume
        if pace < float(cfg("volume_pace_multiple", 2)):
            failures.append(f"volume is on pace for only {pace:.1f}x a normal day (needs {float(cfg('volume_pace_multiple', 2)):g}x)")
    if state.day_change_percent is None:
        failures.append("today's change is unknown")
    else:
        if state.day_change_percent > float(cfg("max_day_gain_percent", 7)):
            failures.append(f"already up {state.day_change_percent:.1f}% today")
        if state.day_change_percent >= state.price_limit_percent - float(cfg("price_limit_margin_percent", 2)):
            failures.append("near the daily price limit")
    if state.index_change_percent is None:
        failures.append("the NEPSE index change is unknown")
    elif state.index_change_percent < -float(cfg("max_index_fall_percent", 1)):
        failures.append(f"the market is falling sharply (NEPSE {state.index_change_percent:+.1f}%)")
    return failures


# ----------------------------------------------------------- confirm / cancel
@dataclass(frozen=True)
class Confirmation:
    confirmed: bool
    headline: str
    advice: str                # what to do if the owner already bought the first part


def confirm_early(trigger: float, close: float, early_stop: float) -> Confirmation:
    """After the evening scan: did the stock hold above its trigger at the close?"""
    if close >= trigger:
        return Confirmation(True, "CONFIRMED", "Closed above the trigger. If you bought the first part, you may buy the rest with the plan below.")
    if close < early_stop:
        return Confirmation(
            False, "CANCELLED — closed below the trigger",
            f"It also closed below the stop ({early_stop:,.1f}). If you already bought: the rule is to exit at the next session. Do not buy more.",
        )
    return Confirmation(
        False, "CANCELLED — closed below the trigger",
        f"If you already bought: hold what you have with the stop at {early_stop:,.1f} and do NOT buy the second part. "
        "Sell if the stop is hit; the trailing-stop and 40-day rules still apply.",
    )
