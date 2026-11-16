"""Price adjustment for bonus shares and right shares.

When a company issues bonus or right shares its price drops on the first day
the shares trade without that entitlement, although nobody lost money. Left
alone, that drop looks like a crash to every indicator. So all prices BEFORE
that day are multiplied by an adjustment factor, which makes the series
continuous. Raw prices are kept untouched and are what the owner is shown.

Cash dividends are deliberately not adjusted for (NEPSE does not adjust for
them, and the upgrade instructions say bonus/right/split only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from nepse_bot.settings import MARKET_CONFIG_PATH, load_yaml

# How far around the announced book-closure date to look for the real price gap.
SEARCH_DAYS_BEFORE = 3
SEARCH_DAYS_AFTER = 6
MIN_EXPECTED_DROP_TO_ALIGN = 0.06
GAP_TOLERANCE_ABOVE = 0.045
LIMIT_SLACK = 0.6  # percentage points of slack over the daily price limit

_limits_cache: list[tuple[str, float]] | None = None


def _LIMITS() -> list[tuple[str, float]]:
    """Return the cached daily price limit history."""
    global _limits_cache
    if _limits_cache is None:
        _limits_cache = price_limits()
    return _limits_cache


@dataclass(frozen=True)
class AppliedAction:
    """One corporate action as it was applied to the price series."""

    action_type: str
    book_closure_date: str
    ex_date: str
    factor: float
    aligned_to_gap: bool


def price_limits() -> list[tuple[str, float]]:
    """Return the daily price limit history as (from_date, percent), oldest first."""
    try:
        entries = load_yaml(MARKET_CONFIG_PATH).get("daily_price_limit") or []
        limits = sorted((str(item["from"]), float(item["percent"])) for item in entries)
    except Exception:
        limits = []
    return limits or [("1900-01-01", 10.0)]


def limit_for(day: str, limits: list[tuple[str, float]]) -> float:
    """Return the daily price limit (percent) in force on a date."""
    current = limits[0][1]
    for start, percent in limits:
        if day >= start:
            current = percent
    return current


def action_factor(action: dict[str, Any], previous_close: float | None) -> float | None:
    """Return the adjustment factor for one action (None if it does not adjust prices)."""
    kind = action.get("action_type")
    ratio = action.get("ratio")
    if kind == "bonus" and ratio and float(ratio) > 0:
        return 1.0 / (1.0 + float(ratio) / 100.0)
    if kind == "split" and ratio and float(ratio) > 0:
        return 1.0 / float(ratio)
    if kind == "right" and ratio and float(ratio) > 0 and previous_close and previous_close > 0:
        new_per_held = float(ratio)
        issue_price = float(action.get("amount") or 100.0)
        theoretical = (previous_close + new_per_held * issue_price) / (1.0 + new_per_held)
        factor = theoretical / previous_close
        return factor if 0 < factor < 1 else None  # rights priced above market do not lower the price
    return None


def adjust_prices(prices: pd.DataFrame, actions: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[AppliedAction]]:
    """Return prices with ``adj_factor`` and adjusted OHLC columns filled in.

    Args:
        prices: One symbol's rows with ``date`` (YYYY-MM-DD text), open, high,
            low, close; any order.
        actions: That symbol's corporate actions.
    """
    frame = prices.sort_values("date").reset_index(drop=True).copy()
    frame["adj_factor"] = 1.0
    applied: list[AppliedAction] = []
    if frame.empty:
        return frame, applied

    dates = frame["date"].tolist()
    closes = frame["close"].astype(float).tolist()
    returns = [0.0] + [closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0 for i in range(1, len(closes))]
    used_positions: set[int] = set()

    for action in sorted(actions, key=lambda item: str(item.get("book_closure_date") or "")):
        book_close = str(action.get("book_closure_date") or "")[:10]
        if not book_close or book_close > dates[-1] or book_close < dates[0]:
            continue
        default_position = next((i for i, day in enumerate(dates) if day >= book_close), None)
        if default_position is None or default_position == 0:
            continue
        factor = action_factor(action, closes[default_position - 1])
        if factor is None or factor >= 0.999:
            continue

        position, aligned = default_position, False
        expected_return = factor - 1.0
        if -expected_return >= MIN_EXPECTED_DROP_TO_ALIGN:
            # Find the day whose actual overnight move best matches the expected drop.
            window = range(max(1, default_position - SEARCH_DAYS_BEFORE), min(len(dates), default_position + SEARCH_DAYS_AFTER + 1))
            # On the real ex-day the price falls by the expected amount plus or
            # minus that day's ordinary market move, which the exchange caps
            # at the daily limit. A smaller fall than expected is tolerated
            # less, because an ordinary down day could then be mistaken for it.
            daily_limit = limit_for(dates[default_position], _LIMITS()) / 100.0
            candidates = [
                (abs(returns[i] - expected_return), i)
                for i in window
                if i not in used_positions
                and returns[i] < -0.03
                and returns[i] >= expected_return - daily_limit - 0.005
                and (
                    returns[i] <= expected_return + GAP_TOLERANCE_ABOVE
                    # A fall beyond the daily limit cannot be ordinary trading.
                    or returns[i] < -(daily_limit + LIMIT_SLACK / 100.0)
                )
            ]
            if default_position not in used_positions and returns[default_position] <= expected_return * 0.5:
                # The book-closure date itself shows at least half the expected
                # fall: that is the normal case, so trust the announced date.
                candidates = []
                aligned = True
            elif not candidates and returns[default_position] < -0.03:
                # A smaller fall than expected on the book-closure date (a
                # rising market that day). Accept it.
                aligned = True
            if candidates:
                position, aligned = min(candidates)[1], True
                if action.get("action_type") == "right":
                    factor = action_factor(action, closes[position - 1]) or factor
        used_positions.add(position)
        frame.loc[: position - 1, "adj_factor"] *= factor
        applied.append(
            AppliedAction(
                action_type=str(action.get("action_type")),
                book_closure_date=book_close,
                ex_date=dates[position],
                factor=round(factor, 6),
                aligned_to_gap=aligned,
            )
        )

    for column in ("open", "high", "low", "close"):
        frame[f"adj_{column}"] = (frame[column].astype(float) * frame["adj_factor"]).round(4)
    frame["adj_factor"] = frame["adj_factor"].round(8)
    return frame, applied


def find_quality_flags(
    prices: pd.DataFrame,
    applied: list[AppliedAction],
    actions: list[dict[str, Any]],
    limits: list[tuple[str, float]] | None = None,
) -> list[dict[str, str]]:
    """Flag price moves and corporate actions that do not add up.

    * ``unexplained_gap``: an overnight fall bigger than the exchange's daily
      limit allows, with no corporate action on that day. It usually means a
      bonus, right, split or merger the bot does not know about.
    * ``gap_after_book_closure``: a fall of more than 10% within five trading
      days after a book closure that no adjustment accounts for.
    * ``action_without_gap``: a large bonus or right whose expected price drop
      could not be found near its book-closure date.
    """
    limits = limits or price_limits()
    frame = prices.sort_values("date").reset_index(drop=True)
    flags: list[dict[str, str]] = []
    if len(frame) < 2:
        return flags
    dates = frame["date"].tolist()
    closes = frame["close"].astype(float).tolist()
    ex_dates = {item.ex_date for item in applied}
    book_closures = sorted({str(a.get("book_closure_date"))[:10] for a in actions if a.get("book_closure_date")})

    for i in range(1, len(dates)):
        if closes[i - 1] <= 0:
            continue
        change = (closes[i] / closes[i - 1] - 1.0) * 100.0
        if dates[i] in ex_dates:
            continue
        if change < -(limit_for(dates[i], limits) + LIMIT_SLACK):
            flags.append(
                {
                    "date": dates[i],
                    "kind": "unexplained_gap",
                    "detail": f"close fell {change:.1f}% overnight, more than the daily limit, with no known corporate action",
                }
            )
        elif change < -10.0:
            recent_closures = [b for b in book_closures if b <= dates[i] and b >= dates[max(0, i - 5)]]
            if recent_closures:
                flags.append(
                    {
                        "date": dates[i],
                        "kind": "gap_after_book_closure",
                        "detail": f"close fell {change:.1f}% within 5 days of book closure {recent_closures[-1]} with no matching adjustment",
                    }
                )

    for item in applied:
        if not item.aligned_to_gap and (1.0 - item.factor) >= MIN_EXPECTED_DROP_TO_ALIGN:
            flags.append(
                {
                    "date": item.ex_date,
                    "kind": "action_without_gap",
                    "detail": f"{item.action_type} (factor {item.factor}) near book closure {item.book_closure_date}: expected price drop not found",
                }
            )
    return flags
