"""NEPSE trading calendar: which days and hours the exchange is open.

All answers come from ``config/market.yaml`` (trading weekdays and hours) and
``config/holidays.yaml`` (extra closed days). No other module is allowed to
hard-code a weekday or a market hour.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from nepse_bot.settings import HOLIDAYS_CONFIG_PATH, MARKET_CONFIG_PATH, load_yaml

logger = logging.getLogger(__name__)

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _parse_time(value: Any, fallback: time) -> time:
    """Parse ``"HH:MM"`` into a time."""
    try:
        hour_text, minute_text = str(value).split(":")[:2]
        return time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError):
        return fallback


class TradingCalendar:
    """Answer "is the market open?" style questions from configuration."""

    def __init__(
        self,
        market_path: Path | str = MARKET_CONFIG_PATH,
        holidays_path: Path | str = HOLIDAYS_CONFIG_PATH,
    ) -> None:
        self.market_path = Path(market_path)
        self.holidays_path = Path(holidays_path)
        self._lock = RLock()
        self._holidays: dict[date, str] = {}
        self._holidays_mtime: float | None = None

        config = load_yaml(self.market_path)
        self.tz = ZoneInfo(str(config.get("timezone", "Asia/Kathmandu")))
        day_names = [str(item).strip().lower() for item in config.get("trading_days", [])]
        unknown = [name for name in day_names if name not in _WEEKDAY_NAMES]
        if unknown or not day_names:
            raise ValueError(f"market.yaml trading_days is invalid: {day_names}")
        self.trading_weekdays: frozenset[int] = frozenset(_WEEKDAY_NAMES.index(name) for name in day_names)
        hours = config.get("trading_hours", {}) or {}
        self.open_time = _parse_time(hours.get("open"), time(11, 0))
        self.close_time = _parse_time(hours.get("close"), time(15, 0))

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        """Return the current time in the exchange timezone."""
        return datetime.now(self.tz)

    def _localize(self, moment: datetime | None) -> datetime:
        if moment is None:
            return self.now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.tz)
        return moment.astimezone(self.tz)

    # -------------------------------------------------------------- holidays
    def holidays(self) -> dict[date, str]:
        """Return closed dates, re-reading the file whenever it changes."""
        with self._lock:
            try:
                mtime = self.holidays_path.stat().st_mtime
            except OSError:
                self._holidays, self._holidays_mtime = {}, None
                return {}
            if mtime == self._holidays_mtime:
                return dict(self._holidays)
            parsed: dict[date, str] = {}
            try:
                for entry in load_yaml(self.holidays_path).get("holidays") or []:
                    if isinstance(entry, dict):
                        raw_date, reason = entry.get("date"), str(entry.get("reason", "") or "")
                    else:
                        raw_date, reason = entry, ""
                    try:
                        parsed[raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))] = reason
                    except ValueError:
                        logger.warning("Ignoring invalid holiday entry: %r", entry)
            except Exception as exc:
                logger.error("Could not read %s; keeping previous holiday list: %s", self.holidays_path, exc)
                return dict(self._holidays)
            self._holidays, self._holidays_mtime = parsed, mtime
            return dict(parsed)

    def add_holiday(self, closed_date: date, reason: str = "") -> bool:
        """Mark a date as closed. Returns False if it was already listed."""
        with self._lock:
            current = self.holidays()
            if closed_date in current:
                return False
            current[closed_date] = reason.strip()
            self._write_holidays(current)
            return True

    def remove_holiday(self, closed_date: date) -> bool:
        """Remove a closed date. Returns False if it was not listed."""
        with self._lock:
            current = self.holidays()
            if closed_date not in current:
                return False
            del current[closed_date]
            self._write_holidays(current)
            return True

    def _write_holidays(self, entries: dict[date, str]) -> None:
        from nepse_bot.utils.safe_json import atomic_write_text

        body = yaml.safe_dump(
            {"holidays": [{"date": day.isoformat(), "reason": reason} for day, reason in sorted(entries.items())]},
            sort_keys=False,
            allow_unicode=True,
        )
        header = (
            "# Days NEPSE is closed even though they fall on a normal trading day.\n"
            "# Edit by hand, or use /holiday add YYYY-MM-DD reason in Telegram.\n"
        )
        atomic_write_text(self.holidays_path, header + body)
        self._holidays_mtime = None

    # ------------------------------------------------------------- questions
    def is_trading_day(self, day: date | None = None) -> bool:
        """Return whether the exchange trades on this date."""
        target = day or self.now().date()
        return target.weekday() in self.trading_weekdays and target not in self.holidays()

    def is_market_open(self, moment: datetime | None = None) -> bool:
        """Return whether the regular session is running at this moment."""
        current = self._localize(moment)
        if not self.is_trading_day(current.date()):
            return False
        return self.open_time <= current.time().replace(tzinfo=None) <= self.close_time

    def previous_trading_day(self, day: date) -> date:
        """Return the closest trading day strictly before ``day``."""
        candidate = day - timedelta(days=1)
        for _ in range(60):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise RuntimeError("No trading day found in the previous 60 days; check the calendar config")

    def next_trading_day(self, day: date) -> date:
        """Return the closest trading day strictly after ``day``."""
        candidate = day + timedelta(days=1)
        for _ in range(60):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise RuntimeError("No trading day found in the next 60 days; check the calendar config")

    def last_completed_trading_day(self, moment: datetime | None = None) -> date:
        """Return the most recent trading day whose session has already closed."""
        current = self._localize(moment)
        today = current.date()
        if self.is_trading_day(today) and current.time().replace(tzinfo=None) > self.close_time:
            return today
        return self.previous_trading_day(today)

    def last_close_datetime(self, moment: datetime | None = None) -> datetime:
        """Return the date and time of the most recent session close."""
        return datetime.combine(self.last_completed_trading_day(moment), self.close_time, tzinfo=self.tz)

    def status_label(self, moment: datetime | None = None) -> str:
        """Return a short human-readable market status."""
        current = self._localize(moment)
        today = current.date()
        if self.is_market_open(current):
            return "OPEN - session in progress"
        if today in self.holidays():
            reason = self.holidays().get(today) or "holiday"
            return f"CLOSED - {reason}"
        if today.weekday() not in self.trading_weekdays:
            return "CLOSED - weekend"
        if current.time().replace(tzinfo=None) < self.open_time:
            return "CLOSED - opens today at " + self.open_time.strftime("%H:%M")
        return "CLOSED - session finished for today"

    def trading_day_names(self) -> str:
        """Return the trading days as text, e.g. ``Monday to Friday``."""
        ordered = sorted(self.trading_weekdays)
        names = [_WEEKDAY_NAMES[index].capitalize() for index in ordered]
        if ordered == list(range(ordered[0], ordered[-1] + 1)) and len(ordered) > 1:
            return f"{names[0]} to {names[-1]}"
        return ", ".join(names)

    def scheduler_days(self) -> tuple[int, ...]:
        """Return trading weekdays in python-telegram-bot numbering (Sunday = 0)."""
        return tuple(sorted((weekday + 1) % 7 for weekday in self.trading_weekdays))


_calendar: TradingCalendar | None = None
_calendar_lock = RLock()


def get_calendar() -> TradingCalendar:
    """Return the shared calendar instance."""
    global _calendar
    with _calendar_lock:
        if _calendar is None:
            _calendar = TradingCalendar()
        return _calendar
