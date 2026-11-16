"""The broker-history download as a quiet background task inside the bot.

It starts with the bot, works one day at a time at low priority, and steps aside by itself:

* no internet            -> waits, asks again every few minutes, continues when it is back;
* data updates and the evening scan (trading days, ``pause_windows``) -> waits;
* market hours           -> waits for the rest of the session if a live alert check was slow;
* disk almost full       -> waits and tells the owner once;
* a download started by hand is running -> waits.

Nothing is remembered in memory that matters: the database says which days are saved, so after
sleep, a shutdown, a restart or lost internet it simply continues with the next missing day.
When no day is missing it tells the owner once, runs the broker study (``backtest/broker_study.py``)
and then keeps looking for new days the daily update may have missed.

Rules: ``config/bot.yaml`` -> ``ingest.broker_backfill``.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any, Callable

from nepse_bot.ingest import broker_backfill as backfill
from nepse_bot.settings import bot_setting
from nepse_bot.sources.chukul import SOURCE, ChukulClient, DownloadInterrupted
from nepse_bot.sources.http import SourceError

logger = logging.getLogger(__name__)
DONE = "broker_backfill.done_notice"            # when the "download complete" message was sent
STUDY_DONE = "broker_backfill.study_done"       # when the automatic backtest finished
STUDY_RESULT = "broker_backfill.study_result"   # its one-line result, for /status
STUDY_FAILED = "broker_backfill.study_failed_on"
DISK_NOTICE = "broker_backfill.disk_notice"
AVERAGE = "broker_backfill.seconds_per_day"
ATTEMPTS = "broker_backfill.attempts"
DEFAULT_SECONDS_PER_DAY = 145.0                 # measured on the first 30 days
CONNECTIVITY_HOSTS = (("chukul.com", 443), ("1.1.1.1", 443), ("8.8.8.8", 53))


def _setting(name: str, default: Any) -> Any:
    return bot_setting("ingest", "broker_backfill", name, default=default)


def is_online(timeout: float = 5.0) -> bool:
    """True when at least one well-known address answers."""
    for host in CONNECTIVITY_HOSTS:
        try:
            socket.create_connection(host, timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def _free_mb(path: Any) -> float:
    return shutil.disk_usage(path).free / 1_000_000


def _lower_thread_priority() -> None:
    """Ask Windows to run this thread only when nothing else wants the processor."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.SetThreadPriority(kernel.GetCurrentThread(), -2)     # THREAD_PRIORITY_LOWEST
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not lower the thread priority: %s", exc)


def _in_window(now: datetime, window: str) -> bool:
    start, end = (part.strip() for part in str(window).split("-"))
    return start <= now.strftime("%H:%M") < end


class BrokerWorker:
    """One step at a time: decide whether to wait, otherwise download the next missing day."""

    def __init__(self, db: Any = None, client: Any = None, calendar: Any = None, notify: Callable[[str], None] | None = None,
                 online: Callable[[], bool] = is_online, clock: Callable[[], datetime] | None = None,
                 study: Callable[[Any], str] | None = None, free_mb: Callable[[Any], float] = _free_mb) -> None:
        if db is None:
            from nepse_bot.db.store import get_database

            db = get_database()
        if calendar is None:
            from nepse_bot.trading_calendar import get_calendar

            calendar = get_calendar()
        if notify is None:
            from nepse_bot.utils.health import queue_admin_alert as notify
        self.db, self.calendar, self.notify, self.online, self.free_mb = db, calendar, notify, online, free_mb
        self.clock = clock or calendar.now
        self.client = client or ChukulClient(workers=int(_setting("workers", 1)), pause_seconds=float(_setting("pause_seconds", 0.95)),
                                             should_stop=self._should_stop)
        self.study = study
        self.since = backfill.since_date()
        self.state, self.reason = "starting", ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._checked = False                       # saved days compared with the price table since this start
        self._redo: list[str] = []                  # incomplete days to download again
        self._retry_at: dict[str, datetime] = {}    # a failed or not-yet-published day is not asked again before this
        self._failures_in_a_row = 0
        self._hold_until: datetime | None = None
        self._online = True
        self._online_checked: datetime | None = None
        self._slow_day: Any = None                  # the session in which a live alert check was slow

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="broker-history", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        _lower_thread_priority()
        self._stop.wait(float(_setting("start_delay_seconds", 90)))     # let the bot finish starting first
        while not self._stop.is_set():
            try:
                wait = self.step()
            except Exception as exc:  # the download must never take the bot down
                logger.exception("Broker history worker error: %s", exc)
                self._set("paused", f"an error ({type(exc).__name__}); trying again in 10 minutes")
                wait = 600
            self._stop.wait(wait)

    def _set(self, state: str, reason: str = "") -> None:
        if (state, reason) != (self.state, self.reason):
            logger.info("Broker history: %s%s", state, f" ({reason})" if reason else "")
        self.state, self.reason = state, reason

    # ---------------------------------------------------------------- pauses
    def _quick_pause(self, now: datetime) -> str | None:
        """Reasons that need no disk or network check (also asked between two requests of a day)."""
        if not _setting("auto", True):
            return "switched off in config/bot.yaml"
        if self.calendar.is_trading_day(now.date()):
            for window in _setting("pause_windows", ["15:15-17:45"]):
                if _in_window(now, window):
                    return f"data update and evening scan ({window})"
            mode = str(_setting("pause_in_market_hours", "auto")).lower()
            if self.calendar.is_market_open(now) and (mode == "always" or (mode == "auto" and self._slow_day == now.date())):
                return "market hours" + (": a live alert check was slow" if mode == "auto" else "")
        if self._hold_until is not None and now < self._hold_until:
            return f"the data site is not answering; next try {self._hold_until.strftime('%H:%M')}"
        return None

    def _should_stop(self) -> bool:
        """Asked before every request of a day in progress."""
        backfill.beat(self.db, 30)
        return self._stop.is_set() or self._quick_pause(self.clock()) is not None

    def _is_online(self, now: datetime, force: bool = False) -> bool:
        every = timedelta(minutes=float(_setting("connectivity_check_minutes", 3)))
        if force or self._online_checked is None or now - self._online_checked >= every:
            self._online, self._online_checked = bool(self.online()), now
        return self._online

    def pause_reason(self, now: datetime) -> str | None:
        """Why the download must wait right now (None = go on)."""
        quick = self._quick_pause(now)
        if quick:
            return quick
        if backfill.other_downloader_running(self.db):
            return "another download, started by hand, is running"
        min_free = float(bot_setting("reliability", "min_free_mb_for_downloads", default=1500))
        free = self.free_mb(self.db.path)
        if free < min_free:
            if self.db.get_setting(DISK_NOTICE) != "1":
                self.notify(f"⚠️ The broker-history download is paused: only {free / 1000:.1f} GB free on the disk "
                            f"(it needs {min_free / 1000:.1f} GB). Free some space and it continues by itself.")
                self.db.set_setting(DISK_NOTICE, "1")
            return f"disk almost full ({free / 1000:.1f} GB free, needs {min_free / 1000:.1f} GB)"
        if self.db.get_setting(DISK_NOTICE) == "1":
            self.db.set_setting(DISK_NOTICE, "")
        if not self._is_online(now):
            return "no internet connection"
        return None

    def note_alert_check(self, seconds: float, now: datetime | None = None) -> None:
        """Called after every live alert check: a slow one while downloading pauses the download for the session."""
        now = now or self.clock()
        if self.state == "running" and seconds > float(_setting("slow_alert_seconds", 30)) and self.calendar.is_market_open(now):
            logger.info("A live alert check took %.0f s during the broker download; pausing it for this session.", seconds)
            self._slow_day = now.date()

    # ------------------------------------------------------------------ work
    def _queue(self, now: datetime) -> list[str]:
        if not self._checked:
            self._redo = backfill.incomplete_days(self.db, self.since)
            self._checked = True
        days = self._redo + [day for day in backfill.missing_days(self.db, self.since) if day not in self._redo]
        return [day for day in days if self._retry_at.get(day, now) <= now]

    def step(self) -> float:
        """Do one thing. Returns the seconds to wait before the next step."""
        now = self.clock()
        reason = self.pause_reason(now)
        if reason:
            self._set("paused", reason)
            return 60.0 * float(_setting("connectivity_check_minutes", 3)) if reason.startswith("no internet") else 60.0
        queue = self._queue(now)
        if not queue:
            return self._nothing_to_download(now)
        day = queue[0]
        self._set("running")
        backfill.beat(self.db)
        started = time.monotonic()
        try:
            with self._awake():
                result = backfill.download_day(self.db, self.client, day, today=now.date())
        except DownloadInterrupted:
            return 0.0                              # the next step says why it waits; the day starts again later
        except SourceError as exc:
            return self._failed(day, exc, now)
        self._failures_in_a_row = 0
        if day in self._redo:
            self._redo.remove(day)
        if result["status"] == "waiting":
            self._retry_at[day] = now + timedelta(minutes=float(_setting("recheck_minutes", 30)))
        elif result["status"] == "stored":
            old = float(self.db.get_setting(AVERAGE) or DEFAULT_SECONDS_PER_DAY)
            self.db.set_setting(AVERAGE, f"{0.9 * old + 0.1 * (time.monotonic() - started):.1f}")
        return 0.0

    def _awake(self) -> Any:
        if not _setting("keep_pc_awake", True):
            return nullcontext()
        from nepse_bot.utils.keep_awake import keep_awake

        return keep_awake()

    def _failed(self, day: str, exc: Exception, now: datetime) -> float:
        if not self._is_online(now, force=True):
            return 0.0                              # lost internet: not the day's fault, nothing is recorded
        try:
            attempts = json.loads(self.db.get_setting(ATTEMPTS) or "{}")
        except ValueError:
            attempts = {}
        attempts[day] = int(attempts.get(day, 0)) + 1
        limit = int(_setting("give_up_after_attempts", 6))
        if attempts[day] >= limit:
            self.db.record_source_run(SOURCE, backfill.DATASET, day, True, 0, f"gave up after {limit} failed attempts: {exc}")
            attempts.pop(day)
            if day in self._redo:
                self._redo.remove(day)
        else:
            self.db.record_source_run(SOURCE, backfill.DATASET, day, False, 0, str(exc))
            self._retry_at[day] = now + timedelta(minutes=float(_setting("retry_failed_day_minutes", 60)))
        self.db.set_setting(ATTEMPTS, json.dumps(attempts))
        logger.warning("Broker history: %s failed (attempt %s): %s", day, attempts.get(day, limit), exc)
        self._failures_in_a_row += 1
        if self._failures_in_a_row >= 5:
            self._failures_in_a_row = 0
            self._hold_until = now + timedelta(minutes=30)
        return 5.0

    def _nothing_to_download(self, now: datetime) -> float:
        recent = (now.date() - timedelta(days=backfill.RECENT_DAYS)).isoformat()
        # Days of the last week that the source has not published yet do not hold up "complete".
        stuck = [day for day in dict.fromkeys(backfill.missing_days(self.db, self.since) + self._redo) if day < recent]
        if stuck:
            waiting = [self._retry_at[day] for day in stuck if day in self._retry_at]
            again = f"; next try {min(waiting).strftime('%H:%M')}" if waiting else ""
            self._set("paused", f"{len(stuck)} day(s) failed{again}")
            return 300.0
        self._completed(now)
        self._set("complete")
        self._checked = False       # next round: compare the days the daily update added with the price table
        return 60.0 * float(_setting("recheck_minutes", 30))

    # ------------------------------------------------------------ completion
    def study_allowed(self, now: datetime) -> bool:
        """The backtest is heavy: never started during the session or the evening jobs of a trading day."""
        quiet = str(_setting("backtest_quiet_hours", "10:30-17:45"))
        return not (self.calendar.is_trading_day(now.date()) and _in_window(now, quiet))

    def _completed(self, now: datetime) -> None:
        if not self.db.get_setting(DONE):
            numbers = backfill.progress(self.db, self.since)
            extra = f" {numbers['empty']} day(s) have no trades at the source." if numbers["empty"] else ""
            extra += f" {numbers['differ']} day(s) differ a little from the price table (kept, listed in /status)." if numbers["differ"] else ""
            self.notify(
                f"✅ Broker history download is complete: {numbers['stored']:,} days, back to {numbers['oldest']}.{extra}\n"
                "Next, by itself: the broker-accumulation backtest (about an hour, in the background, outside market hours). "
                "I will send you the result. New days are added by the daily update from now on."
            )
            self.db.set_setting(DONE, now.isoformat())
        if self.db.get_setting(STUDY_DONE) or self.db.get_setting(STUDY_FAILED) == now.date().isoformat() or not self.study_allowed(now):
            return
        self._set("complete", "running the broker backtest (about an hour)")
        try:
            study = self.study
            if study is None:
                from nepse_bot.backtest.broker_study import run_and_apply as study
            text = study(self.db)
        except Exception as exc:
            logger.exception("The automatic broker backtest failed: %s", exc)
            self.db.set_setting(STUDY_FAILED, now.date().isoformat())
            self.notify(f"⚠️ The broker backtest stopped with an error ({str(exc)[:200]}). Nothing was changed. I will try again tomorrow.")
            return
        self.db.set_setting(STUDY_DONE, self.clock().isoformat())
        self.notify(text)


# ------------------------------------------------------------------ module level
_worker: BrokerWorker | None = None


def start_worker() -> BrokerWorker:
    """Start the background download (once per bot process)."""
    global _worker
    if _worker is None:
        _worker = BrokerWorker()
    _worker.start()
    return _worker


def note_alert_check(seconds: float) -> None:
    if _worker is not None:
        _worker.note_alert_check(seconds)


def status_line(db: Any, worker: BrokerWorker | None = None) -> str:
    """The broker-history line of /status."""
    worker = worker or _worker
    numbers = backfill.progress(db)
    differ = f"; {numbers['differ']} day(s) differ a little from the price table" if numbers["differ"] else ""
    if db.get_setting(DONE) and not numbers["remaining"]:
        result = db.get_setting(STUDY_RESULT)
        study = result or ("backtest: " + (worker.reason if worker and worker.reason else "waiting for a quiet hour (outside 10:30-17:45 on trading days)"))
        return (f"Broker history: complete, {numbers['done']:,} of {numbers['total']:,} days, back to {numbers['oldest']}{differ}. "
                f"New days come with the daily update. {study}")
    if worker is None:
        state = "not running (it starts with the bot)"
    elif worker.state == "complete":
        state = "up to date, waiting for the newest day"
    elif worker.state == "paused":
        state = f"paused ({worker.reason})"
    else:
        state = worker.state
    seconds = float(db.get_setting(AVERAGE) or DEFAULT_SECONDS_PER_DAY)
    hours = numbers["remaining"] * seconds / 3600.0
    left = f"about {hours:.0f} hours" if hours >= 1.5 else f"about {max(1, round(hours * 60))} minutes"
    return (f"Broker history: {numbers['done']:,} of ~{numbers['total']:,} days ({numbers['percent']:.0f}%), {state}, "
            f"{left} of PC-on time left, oldest day so far: {numbers['oldest'] or 'none yet'}{differ}")
