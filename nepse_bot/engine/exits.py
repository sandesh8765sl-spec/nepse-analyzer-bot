"""Exit styles and reference targets (Stage 5).

The owner chooses how to take profit with ``/settings exit``:

    trailing  trailing stop only (the Stage 4b plan)
    target    sell everything at the reference target (next major resistance)
    hybrid    sell half at the reference target, trail the rest

The backtest comparison of the three, and how far past winning trades went,
are in config/exits.yaml (written by ``python -m nepse_bot.backtest.exits``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from nepse_bot.engine.config import setting
from nepse_bot.portfolio.fees import FeeSchedule
from nepse_bot.settings import CONFIG_DIR, load_yaml

EXITS_PATH = CONFIG_DIR / "exits.yaml"
STYLES = ("trailing", "target", "hybrid")
STYLE_TEXT = {
    "trailing": "trailing stop only, no fixed target",
    "target": "sell everything at the reference target",
    "hybrid": "sell half at the reference target, trail the rest",
}
SETTING_KEY = "exit_style"
MIN_WINNERS = 20      # fewer recent winners than this: use the whole backtest instead


@lru_cache(maxsize=1)
def exits_config() -> dict[str, Any]:
    try:
        return load_yaml(EXITS_PATH)
    except (FileNotFoundError, ValueError):
        return {}


def default_style() -> str:
    style = str(exits_config().get("default_exit_style", "trailing"))
    return style if style in STYLES else "trailing"


def exit_style(db: Any | None = None) -> str:
    """The owner's exit style: /settings exit, else the backtest's best."""
    stored = db.get_setting(SETTING_KEY) if db is not None else None
    return stored if stored in STYLES else default_style()


def style_record(style: str, period: str) -> dict[str, Any]:
    return ((exits_config().get("exit_styles") or {}).get(style) or {}).get(period) or {}


def comparison_lines() -> list[str]:
    """The backtest comparison, for /settings: average result per trade after fees and tax."""
    lines = []
    for style in STYLES:
        recent, earlier = style_record(style, "out_of_sample"), style_record(style, "tuning")
        if not recent or recent.get("expectancy_pct") is None:
            continue
        lines.append(
            f"{style}: last 2 yrs {recent['expectancy_pct']:+.2f}% per trade ({recent.get('win_rate', 0):.0f}% won), "
            f"2020-2024 {earlier.get('expectancy_pct', 0):+.2f}% ({earlier.get('win_rate', 0):.0f}% won)"
        )
    return lines


def worse_than_default_note(style: str) -> str | None:
    """Say plainly when the chosen style did worse than the default in the backtest."""
    default = default_style()
    if style == default:
        return None
    notes = []
    for period, label in (("tuning", "2020-2024"), ("out_of_sample", "the last 2 years")):
        mine, best = style_record(style, period).get("expectancy_pct"), style_record(default, period).get("expectancy_pct")
        if mine is None or best is None:
            continue
        gap = mine - best
        notes.append(f"{abs(gap):.2f}% per trade {'worse' if gap < 0 else 'better'} than '{default}' in {label}")
    return f"In the backtest '{style}' was " + " and ".join(notes) + "." if notes else None


@dataclass(frozen=True)
class WinnerStats:
    """How far past winning trades of a setup went (price gain from the entry)."""

    median_pct: float
    top_quarter_pct: float
    count: int
    period: str            # "last 2 yrs" or "2020-2026"


def winners(setup_key: str) -> WinnerStats | None:
    data = (exits_config().get("winners") or {}).get(setup_key) or {}
    for key, label in (("recent", "last 2 yrs"), ("all", "2020-2026")):
        stats = data.get(key) or {}
        if int(stats.get("count") or 0) >= MIN_WINNERS and stats.get("median_pct") is not None:
            return WinnerStats(float(stats["median_pct"]), float(stats["top_quarter_pct"]), int(stats["count"]), label)
    return None


@dataclass(frozen=True)
class Level:
    """A price level above the entry, with what selling there would bring."""

    name: str
    price: float
    gain_pct: float
    net_rupees: float


@dataclass(frozen=True)
class Targets:
    """Everything the reports show about taking profit on one plan."""

    resistance: Level
    high_52w: Level | None             # None when the entry is at or above it, or it IS the resistance
    typical: Level | None              # the median past winner
    top_quarter: Level | None
    stats: WinnerStats | None
    partial: Level                     # where to consider selling half
    partial_quantity: int
    partial_reason: str


def _level(name: str, price: float, entry: float, quantity: int, fees: FeeSchedule) -> Level:
    return Level(name, price, (price / entry - 1.0) * 100.0, fees.round_trip(quantity, entry, price, 25).net_profit)


def partial_level(entry: float, reference_target: float) -> tuple[float, str]:
    """THE partial-profit rule, used by /stock, /buy, the alerts and /positions alike.

    Part of the position is booked at the reference target (the next major resistance), where
    rallies often stall. One level, one rule: it is never a different price from the target shown.
    """
    reason = "the reference target (next major resistance), where rallies often stall"
    minimum = entry * (1 + float(setting("plan", "partial_profit_min_percent", default=4)) / 100.0)
    if reference_target < minimum:
        reason += "; it is close to the entry, so the profit to book is small"
    return float(reference_target), reason


def targets_for(plan: Any, fees: FeeSchedule) -> Targets:
    """Reference targets, typical outcome and the partial-profit level for a trade plan."""
    entry, quantity = float(plan.entry), int(plan.quantity)
    resistance = _level(f"next major resistance ({plan.target_1_reason})", float(plan.target_1), entry, quantity, fees)
    high = getattr(plan, "high_52w", None)
    high_52w = _level("52-week high", float(high), entry, quantity, fees) if high and float(high) > entry * 1.005 else None
    if high_52w is not None and abs(high_52w.price / resistance.price - 1.0) < 0.005:
        resistance, high_52w = _level("next major resistance = the 52-week high", resistance.price, entry, quantity, fees), None
    stats = winners(plan.setup_key)
    typical = top = None
    if stats is not None:
        typical = _level("past winners (median)", round(entry * (1 + stats.median_pct / 100.0), 1), entry, quantity, fees)
        top = _level("top quarter of winners", round(entry * (1 + stats.top_quarter_pct / 100.0), 1), entry, quantity, fees)
    partial_price, reason = partial_level(entry, resistance.price)
    half = fees.round_lot(quantity / 2) or quantity
    return Targets(resistance, high_52w, typical, top, stats, _level("partial profit", partial_price, entry, half, fees), half, reason)
