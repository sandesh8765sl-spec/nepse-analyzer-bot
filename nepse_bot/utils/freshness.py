"""The "As of" line shown on every report, with a stale-data warning."""

from __future__ import annotations

from datetime import date, datetime

from nepse_bot.trading_calendar import TradingCalendar

STALE_MARK = "⚠️"


def as_of_line(
    data_date: date | None,
    source: str,
    calendar: TradingCalendar,
    now: datetime | None = None,
) -> str:
    """Build the one-line data freshness statement for a report."""
    current = now or calendar.now()
    if data_date is None:
        return f"{STALE_MARK} As of: data date unknown ({source})"
    last_completed = calendar.last_completed_trading_day(current)
    if data_date < last_completed:
        return (
            f"{STALE_MARK} As of: {data_date.isoformat()} ({source}) - OLDER than the last completed "
            f"trading day ({last_completed.isoformat()}). Treat with caution."
        )
    if data_date == current.date() and calendar.is_market_open(current):
        return (
            f"As of: {data_date.isoformat()} {current.strftime('%H:%M')} live ({source}) - "
            "session in progress, today's bar is not final"
        )
    return f"As of: {data_date.isoformat()} close ({source}) ✓"


def fetched_line(source: str, calendar: TradingCalendar, now: datetime | None = None) -> str:
    """Build the freshness statement for content that has no market date (news, journal)."""
    current = now or calendar.now()
    return f"As of: {current.strftime('%Y-%m-%d %H:%M')} Nepal time ({source})"


def insert_after_title(text: str, line: str) -> str:
    """Insert ``line`` as the second line of a report (or append to a one-liner)."""
    if not text:
        return text
    if line in text:
        return text
    head, separator, tail = text.partition("\n")
    if not separator:
        return f"{head}\n{line}"
    return f"{head}\n{line}\n{tail}"
