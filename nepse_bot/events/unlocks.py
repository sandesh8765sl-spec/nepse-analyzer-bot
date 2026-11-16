"""Lock-in expiry: the day locked promoter or mutual fund shares become free to sell.

A large block of shares that may suddenly be sold is extra supply, which often
weighs on the price around the date. The bot only warns; it does not change a grade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from nepse_bot.events.engine import events_config
from nepse_bot.trading_calendar import TradingCalendar

HOLDER_TEXT = {"Promoter": "promoter", "MutualFund": "mutual fund"}


def flag_days() -> int:
    return int(events_config().get("unlock_flag_trading_days", 15))


def calendar_days() -> int:
    return int(events_config().get("unlock_calendar_days", 60))


@dataclass(frozen=True)
class UnlockItem:
    symbol: str
    holder: str                  # Promoter / MutualFund
    day: str                     # first day the shares may be sold
    trading_days_left: int
    locked_shares: float | None
    total_shares: float | None

    @property
    def share_percent(self) -> float | None:
        if not self.locked_shares or not self.total_shares:
            return None
        return 100.0 * self.locked_shares / self.total_shares

    @property
    def flagged(self) -> bool:
        return self.trading_days_left <= flag_days()

    @property
    def size_text(self) -> str:
        if not self.locked_shares:
            return "number of shares not published"
        share = self.share_percent
        return f"{self.locked_shares:,.0f} shares" + (f" ({share:.1f}% of listed shares)" if share is not None else "")

    @property
    def when_text(self) -> str:
        return "today" if self.trading_days_left <= 0 else f"in {self.trading_days_left} trading day(s)"

    def short(self) -> str:
        """One short line for lists and alerts."""
        return f"🔓 {HOLDER_TEXT.get(self.holder, self.holder)} lock-in ends {self.day} ({self.when_text}): {self.size_text} can be sold from then"

    def line(self) -> str:
        return f"{self.symbol}: {self.short()[2:]}"


def upcoming_unlocks(db: Any, today: str, calendar: TradingCalendar, days: int | None = None,
                     symbols: Iterable[str] | None = None) -> list[UnlockItem]:
    """Lock-in expiries from today to ``days`` calendar days ahead, soonest first."""
    start = date.fromisoformat(today)
    end = (start + timedelta(days=days if days is not None else calendar_days())).isoformat()
    rows = db.query(
        "SELECT symbol, holder_type, unlock_date, locked_shares, total_shares FROM lockin_unlocks "
        "WHERE unlock_date >= ? AND unlock_date <= ? ORDER BY unlock_date, symbol",
        (today, end),
    )
    wanted = set(symbols) if symbols is not None else None
    items = []
    for row in rows:
        if wanted is not None and row["symbol"] not in wanted:
            continue
        left, cursor, target = 0, start, date.fromisoformat(row["unlock_date"])
        while cursor < target:
            cursor += timedelta(days=1)
            if calendar.is_trading_day(cursor):
                left += 1
        items.append(UnlockItem(row["symbol"], row["holder_type"], row["unlock_date"], left, row["locked_shares"], row["total_shares"]))
    return items


def flagged_by_symbol(db: Any, today: str, calendar: TradingCalendar) -> dict[str, list[UnlockItem]]:
    """Stocks with a lock-in expiry inside the warning window (config/events.yaml)."""
    found: dict[str, list[UnlockItem]] = {}
    # 15 trading days never span more than about 6 calendar weeks, holidays included.
    for item in upcoming_unlocks(db, today, calendar, days=flag_days() * 3):
        if item.flagged:
            found.setdefault(item.symbol, []).append(item)
    return found
