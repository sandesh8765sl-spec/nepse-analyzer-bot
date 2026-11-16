"""A polite web client shared by all data sources.

Politeness rules (from config/bot.yaml, ``ingest`` section):

* at least one second between two requests to the same website;
* failed requests are retried with a growing pause (exponential backoff);
* one raw sample response per source per day is saved under ``data/raw`` for
  debugging, and samples older than seven days are deleted.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from nepse_bot.settings import DATA_DIR, bot_setting

logger = logging.getLogger(__name__)

RAW_DIR = DATA_DIR / "raw"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class SourceError(RuntimeError):
    """A data source could not deliver what was asked for."""


class HostRateLimiter:
    """Make sure requests to one website are spaced out, across all threads."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = float(min_interval_seconds)
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}
        self._sleep = time.sleep
        self._clock = time.monotonic

    def wait(self, host: str) -> float:
        """Block until this host may be contacted again; return seconds waited."""
        with self._lock:
            now = self._clock()
            ready_at = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = ready_at + self.min_interval_seconds
        delay = ready_at - now
        if delay > 0:
            self._sleep(delay)
        return max(0.0, delay)


_GLOBAL_LIMITER = HostRateLimiter(float(bot_setting("ingest", "min_seconds_between_requests", default=1.0)))


def save_raw_sample(source: str, name: str, text: str, today: date | None = None) -> Path | None:
    """Keep the first response of each kind per source per day."""
    day = (today or datetime.now().date()).isoformat()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80]
    target = RAW_DIR / source / day / safe_name
    if target.exists():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text[:2_000_000], encoding="utf-8")
        return target
    except OSError as exc:
        logger.debug("Could not save raw sample %s: %s", target, exc)
        return None


def prune_raw_samples(keep_days: int | None = None, today: date | None = None) -> int:
    """Delete raw sample folders older than ``keep_days``; return how many."""
    keep = int(keep_days if keep_days is not None else bot_setting("ingest", "raw_sample_keep_days", default=7))
    cutoff = (today or datetime.now().date()) - timedelta(days=keep)
    removed = 0
    if not RAW_DIR.exists():
        return 0
    for source_dir in RAW_DIR.iterdir():
        if not source_dir.is_dir():
            continue
        for day_dir in source_dir.iterdir():
            try:
                folder_day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if folder_day < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)
                removed += 1
    return removed


class PoliteClient:
    """HTTP GET with rate limiting, retries and raw-sample capture."""

    def __init__(
        self,
        source: str,
        session: Any | None = None,
        limiter: HostRateLimiter | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        timeout_seconds: float | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.source = source
        self.session = session or requests.Session()
        self.limiter = limiter or _GLOBAL_LIMITER
        self.max_retries = int(max_retries if max_retries is not None else bot_setting("ingest", "max_retries", default=4))
        self.backoff_seconds = float(
            backoff_seconds if backoff_seconds is not None else bot_setting("ingest", "backoff_seconds", default=2.0)
        )
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None else bot_setting("http", "timeout_seconds", default=20)
        )
        self._sleep = sleep
        self._lock = threading.Lock()

    def get_json(self, url: str, params: dict[str, Any] | None = None, sample_name: str | None = None) -> Any:
        """GET a URL and return parsed JSON, retrying temporary failures."""
        host = urlparse(url).netloc
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.limiter.wait(host)
            try:
                with self._lock:
                    response = self.session.get(
                        url,
                        params=params,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                        timeout=self.timeout_seconds,
                    )
                status = response.status_code
                if status == 200:
                    if sample_name:
                        save_raw_sample(self.source, sample_name, response.text)
                    return response.json()
                if status in (400, 401, 403, 404):
                    # Asking again will not change the answer.
                    raise SourceError(f"{self.source} returned HTTP {status} for {url}")
                last_error = SourceError(f"{self.source} returned HTTP {status}")
            except SourceError:
                raise
            except Exception as exc:  # network trouble or invalid JSON
                last_error = exc
            if attempt < self.max_retries:
                pause = self.backoff_seconds * (2 ** (attempt - 1))
                logger.info("%s request failed (attempt %s/%s): %s. Retrying in %.0fs.", self.source, attempt, self.max_retries, last_error, pause)
                self._sleep(pause)
        raise SourceError(f"{self.source} failed after {self.max_retries} attempts: {last_error}")
