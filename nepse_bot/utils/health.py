"""Runtime health tracking: data sources, recent errors, and owner alerts."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import RLock


@dataclass
class SourceStatus:
    """What we know about one external data source."""

    name: str
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    success_count: int = 0
    failure_count: int = 0
    data_date: date | None = None

    @property
    def healthy(self) -> bool | None:
        """True/False once the source has been used; None if never tried."""
        if self.last_success is None and self.last_failure is None:
            return None
        return self.consecutive_failures == 0


class SourceHealth:
    """Thread-safe record of the last success and failure per source."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sources: dict[str, SourceStatus] = {}

    def record_success(self, source: str, data_date: date | None = None) -> None:
        """Note that a source just answered correctly."""
        with self._lock:
            status = self._sources.setdefault(source, SourceStatus(name=source))
            status.last_success = datetime.now(timezone.utc)
            status.consecutive_failures = 0
            status.success_count += 1
            if data_date is not None and (status.data_date is None or data_date > status.data_date):
                status.data_date = data_date

    def record_failure(self, source: str, error: object) -> None:
        """Note that a source just failed."""
        with self._lock:
            status = self._sources.setdefault(source, SourceStatus(name=source))
            status.last_failure = datetime.now(timezone.utc)
            status.last_error = str(error)[:160]
            status.consecutive_failures += 1
            status.failure_count += 1

    def snapshot(self) -> list[SourceStatus]:
        """Return a copy of every source's status, sorted by name."""
        with self._lock:
            return [SourceStatus(**vars(status)) for _, status in sorted(self._sources.items())]

    def reset(self) -> None:
        """Forget everything (used by tests)."""
        with self._lock:
            self._sources.clear()


class RecentErrorCounter(logging.Handler):
    """Logging handler that remembers when ERROR-level records happened."""

    def __init__(self, keep: int = 2000) -> None:
        super().__init__(level=logging.ERROR)
        self._events: deque[tuple[datetime, str]] = deque(maxlen=keep)
        self._events_lock = RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = f"{record.name}: {record.getMessage()}"[:160]
        except Exception:
            message = record.name
        with self._events_lock:
            self._events.append((datetime.now(timezone.utc), message))

    def count_since(self, window: timedelta) -> int:
        """Return how many errors were logged inside the window."""
        cutoff = datetime.now(timezone.utc) - window
        with self._events_lock:
            return sum(1 for moment, _ in self._events if moment >= cutoff)

    def latest(self) -> tuple[datetime, str] | None:
        """Return the most recent error, if any."""
        with self._events_lock:
            return self._events[-1] if self._events else None


SOURCE_HEALTH = SourceHealth()
ERROR_COUNTER = RecentErrorCounter()

_alerts: deque[str] = deque(maxlen=50)
_alerts_lock = RLock()


def queue_admin_alert(text: str) -> None:
    """Queue a message for the owner; a scheduled job delivers it via Telegram."""
    with _alerts_lock:
        if text not in _alerts:
            _alerts.append(text)


def drain_admin_alerts() -> list[str]:
    """Return and clear all queued owner alerts."""
    with _alerts_lock:
        items = list(_alerts)
        _alerts.clear()
        return items
