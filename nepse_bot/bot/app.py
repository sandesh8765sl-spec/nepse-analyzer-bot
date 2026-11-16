"""The bot application: access control, admin commands, scheduled jobs, start-up.

Analysis commands live in ``handlers.py`` and personal commands in
``personal.py``. This module wires everything together.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from datetime import datetime, time, timedelta
from typing import Any

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from nepse_bot.access import REFUSAL_TEXT, AccessControl
from nepse_bot.bot import alerting, ask, extras, handlers, manage, personal
from nepse_bot.settings import DATA_DIR, PROJECT_ROOT, bot_setting, parse_id_list
from nepse_bot.trading_calendar import get_calendar
from nepse_bot.utils.freshness import as_of_line
from nepse_bot.utils.health import ERROR_COUNTER, SOURCE_HEALTH, drain_admin_alerts, queue_admin_alert
from nepse_bot.utils.logging_setup import configure_logging
from nepse_bot.utils.safe_json import atomic_write_text

load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
if not TELEGRAM_TOKEN:
    raise ValueError("Missing TELEGRAM_TOKEN. Please define it in your .env file.")
if not TELEGRAM_CHAT_ID:
    raise ValueError("Missing TELEGRAM_CHAT_ID. Please define it in your .env file.")

logger = logging.getLogger(__name__)

# Trading days, hours and holidays all come from config through this calendar.
CALENDAR = get_calendar()
KATHMANDU_TZ = CALENDAR.tz
BOT_STARTED_AT = datetime.now(KATHMANDU_TZ)

# The owner's own chat is always allowed; more can be added in .env.
_OWNER_IDS = parse_id_list(TELEGRAM_CHAT_ID)
ACCESS = AccessControl(
    allowed_user_ids=parse_id_list(os.getenv("ALLOWED_USER_IDS")) | {item for item in _OWNER_IDS if item > 0},
    allowed_chat_ids=parse_id_list(os.getenv("ALLOWED_CHAT_IDS")) | _OWNER_IDS,
)

# Commands that existed before Stage 3 and were removed, with where to go instead.
REMOVED_COMMANDS: dict[str, str] = {
    "strategy": "/stock", "pro": "/stock", "compare": "/stock", "chartsymbol": "/chart",
    "strongbuy": "/buy", "strong_buy": "/buy", "strong": "/buy", "topbuy": "/top10", "breakout": "/watch",
    "near_support": "/watch", "sellzone": "/portfolio", "avoid": "/stock", "value": "/top10",
    "currentmarket": "/market", "daily": "/market", "weekly": "/market", "menu": "/help",
    "add": "/watchlist add", "remove": "/watchlist remove", "autosignals": "/buy",
    "sentiment": "/news", "catalyst": "/news", "latestnews": "/news", "sectornews": "/news", "rumors": "/news",
    "research": "/stock", "deep": "/stock", "why": "/stock", "scannews": "/news", "eventcalendar": "/calendar",
    "impact": "/news", "longterm": "/report", "sources": "/status", "performance": "/buy", "journal": "/buy", "missed": "/watch",
}

_ingestor: Any = None


def get_ingestor() -> Any:
    """Create the data ingestor on first use."""
    global _ingestor
    if _ingestor is None:
        from nepse_bot.ingest.pipeline import Ingestor

        _ingestor = Ingestor(calendar=CALENDAR)
    return _ingestor


# ------------------------------------------------------------- access control
async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop every update that does not come from an allow-listed user or chat.

    A stranger receives one polite refusal, once ever, and nothing else.
    """
    del context
    user, chat = update.effective_user, update.effective_chat
    user_id = user.id if user is not None else None
    chat_id = chat.id if chat is not None else None
    if ACCESS.is_allowed(user_id, chat_id):
        return
    if user_id is not None and ACCESS.should_send_refusal(user_id):
        logger.warning("Refused access for a user who is not on the allowlist.")
        try:
            if update.callback_query is not None:
                await update.callback_query.answer(REFUSAL_TEXT, show_alert=True)
            elif update.effective_message is not None:
                await update.effective_message.reply_text(REFUSAL_TEXT)
        except TelegramError as exc:
            logger.info("Could not deliver the refusal message: %s", exc)
    raise ApplicationHandlerStop


# ------------------------------------------------------------- admin commands
async def holiday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List, add or remove market holidays (days NEPSE is closed)."""
    message = update.effective_message
    if message is None:
        return
    args = list(context.args or [])
    action = args[0].lower() if args else "list"
    if action in {"add", "remove"}:
        if len(args) < 2:
            await message.reply_text("Use /holiday add YYYY-MM-DD reason, or /holiday remove YYYY-MM-DD.")
            return
        try:
            closed_date = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            await message.reply_text("I could not read that date. Please use YYYY-MM-DD, for example 2026-11-20.")
            return
        if action == "add":
            reason = " ".join(args[2:]).strip() or "Holiday"
            added = CALENDAR.add_holiday(closed_date, reason)
            await message.reply_text(f"Marked {closed_date} as closed ({reason})." if added else f"{closed_date} was already marked as closed.")
        else:
            removed = CALENDAR.remove_holiday(closed_date)
            await message.reply_text(f"Removed {closed_date} from the closed days." if removed else f"{closed_date} was not in the closed-days list.")
        return
    today = datetime.now(KATHMANDU_TZ).date()
    upcoming = [(day, reason) for day, reason in sorted(CALENDAR.holidays().items()) if day >= today]
    lines = [
        "Market calendar",
        f"Trading days: {CALENDAR.trading_day_names()}, {CALENDAR.open_time.strftime('%H:%M')}-{CALENDAR.close_time.strftime('%H:%M')} Nepal time",
        f"Today: {CALENDAR.status_label()}",
        "",
        "Upcoming closed days:",
    ]
    lines.extend([f"- {day.isoformat()} ({day.strftime('%a')}): {reason or 'Holiday'}" for day, reason in upcoming] or ["- None listed"])
    lines.extend(["", "Add: /holiday add YYYY-MM-DD reason", "Remove: /holiday remove YYYY-MM-DD"])
    await message.reply_text("\n".join(lines))


def format_uptime(delta: timedelta) -> str:
    """Format a duration compactly, e.g. ``2d 3h 5m``."""
    total = max(0, int(delta.total_seconds()))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [f"{days}d"] * bool(days) + [f"{hours}h"] * bool(hours) + [f"{minutes}m"] * bool(minutes)
    return " ".join(parts) or f"{seconds}s"


def database_status_lines(last_completed: Any) -> list[str]:
    """Describe what the database holds and how fresh it is."""
    try:
        from nepse_bot.db.store import get_database

        db = get_database()
        counts = db.table_counts()
        first_day, last_day = db.scalar("SELECT MIN(date) FROM daily_prices"), db.last_price_date()
        if last_day is None:
            return ["- Empty. Run the history download: python -m nepse_bot.ingest backfill"]
        fresh = "✓" if last_day >= last_completed.isoformat() else f"⚠️ behind (last completed trading day is {last_completed.isoformat()})"
        equities = db.scalar(
            "SELECT COUNT(DISTINCT p.symbol) FROM daily_prices p JOIN securities s ON s.symbol = p.symbol "
            "WHERE s.status = 'active' AND s.instrument_type = 'equity' AND s.is_promoter = 0"
        )
        lines = [
            f"- Prices: {first_day} to {last_day} {fresh}",
            f"- {counts['daily_prices']:,} price rows for {equities} active stocks; {counts['index_daily']:,} index rows",
            f"- Fundamentals: {counts['fundamentals']:,} reports for {db.scalar('SELECT COUNT(DISTINCT symbol) FROM fundamentals')} companies",
            f"- Corporate actions: {counts['corporate_actions']:,} | Broker-flow rows: {counts['broker_flow_daily']:,} "
            f"({db.scalar('SELECT COUNT(DISTINCT date) FROM broker_flow_daily')} days)",
            f"- Data-quality flags: {counts['data_quality_flags']:,} | Signals logged: {counts['signals']:,}",
        ]
        last_run = db.get_setting("last_ingest_run")
        if last_run:
            lines.append(f"- Last data update: {datetime.fromisoformat(last_run).astimezone(KATHMANDU_TZ).strftime('%Y-%m-%d %H:%M')} Nepal time")
        for row in db.query(
            "SELECT source, dataset, date, detail FROM source_runs r WHERE ok = 0 AND date = "
            "(SELECT MAX(date) FROM source_runs WHERE source = r.source AND dataset = r.dataset) ORDER BY source, dataset"
        ):
            lines.append(f"- ❌ {row['source']} {row['dataset']} failed on {row['date']}: {(row['detail'] or '')[:90]}")
        return lines
    except Exception as exc:
        logger.warning("Could not read database status: %s", exc)
        return [f"- Could not read the database: {exc}"]


def broker_history_line() -> str:
    """Progress of the broker-history download: days done, running or paused (why), time left, oldest day."""
    try:
        from nepse_bot.db.store import get_database
        from nepse_bot.ingest import broker_worker

        return "- " + broker_worker.status_line(get_database())
    except Exception as exc:
        logger.warning("Could not read the broker history progress: %s", exc)
        return f"- Broker history: could not read the progress ({exc})"


def job_rows(context: ContextTypes.DEFAULT_TYPE | None) -> list[str]:
    """One line per scheduled job with its next run time."""
    rows: list[tuple[datetime | None, str]] = []
    try:
        jobs = tuple(context.job_queue.jobs()) if context is not None and context.job_queue is not None else ()
    except Exception:
        jobs = ()
    for job in jobs:
        try:
            rows.append((job.next_t, str(job.name)))
        except Exception:
            rows.append((None, str(job.name)))
    far_future = datetime.max.replace(tzinfo=KATHMANDU_TZ)
    rows.sort(key=lambda item: item[0] or far_future)
    return [f"- {name}: {moment.astimezone(KATHMANDU_TZ).strftime('%a %m-%d %H:%M') if moment else 'not scheduled'}" for moment, name in rows]


def build_status_report(context: ContextTypes.DEFAULT_TYPE | None) -> str:
    """The bot and data health report shown by /status."""
    now = datetime.now(KATHMANDU_TZ)
    last_completed = CALENDAR.last_completed_trading_day(now)
    lines = [
        "Bot Status",
        "",
        "BOT",
        f"- Running since: {BOT_STARTED_AT.strftime('%Y-%m-%d %H:%M')} Nepal time ({format_uptime(now - BOT_STARTED_AT)})",
        f"- Time now: {now.strftime('%Y-%m-%d %H:%M')} Nepal time",
        f"- Market: {CALENDAR.status_label(now)}",
        f"- Trading days: {CALENDAR.trading_day_names()}",
        f"- Access: private ({len(ACCESS.allowed_user_ids)} allowed user(s), {ACCESS.refused_count()} stranger(s) turned away)",
        "",
        "DATABASE",
        *database_status_lines(last_completed),
    ]
    try:
        from nepse_bot.db.store import get_database

        last_day = get_database().last_price_date()
        if last_day:
            lines.append(f"- {as_of_line(datetime.strptime(last_day, '%Y-%m-%d').date(), 'analysis data', CALENDAR, now)}")
    except Exception:
        pass
    lines.extend(["", "BROKER HISTORY (background download)", broker_history_line()])
    lines.extend(["", "DATA SOURCES (since the bot started)"])
    sources = SOURCE_HEALTH.snapshot()
    if not sources:
        lines.append("- Nothing fetched yet since this start. The next data update will fill this in.")
    for status in sources:
        mark = "✅" if status.healthy else "⚠️" if status.last_success is not None else "❌"
        last_ok = status.last_success.astimezone(KATHMANDU_TZ).strftime("%m-%d %H:%M") if status.last_success is not None else "never"
        line = f"{mark} {status.name}: last OK {last_ok}"
        if status.consecutive_failures:
            line += f"; {status.consecutive_failures} failure(s) in a row ({status.last_error})"
        lines.append(line)
    lines.extend(["", "SCHEDULED JOBS (next run, Nepal time)", *(job_rows(context) or ["- No scheduled jobs found"])])
    lines.extend(["", "ERRORS", f"- Last hour: {ERROR_COUNTER.count_since(timedelta(hours=1))} | Last 24 hours: {ERROR_COUNTER.count_since(timedelta(hours=24))} (since the bot started)"])
    latest = ERROR_COUNTER.latest()
    if latest is not None:
        lines.append(f"- Most recent: {latest[0].astimezone(KATHMANDU_TZ).strftime('%m-%d %H:%M')} {latest[1]}")
    return "\n".join(lines)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status - bot and data health."""
    if update.effective_message is not None:
        for part in handlers.reports.split_message(build_status_report(context)):
            await update.effective_message.reply_text(part, disable_web_page_preview=True)


async def dataupdate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dataupdate - run the data update now and report what it did."""
    message = update.effective_message
    if message is None:
        return
    note = await message.reply_text("Updating the database. This can take a few minutes...")
    try:
        report = await asyncio.to_thread(get_ingestor().run)
        engine_line = await asyncio.to_thread(handlers.refresh_after_update)
        await note.edit_text((report.as_text() + f"\n- analysis: {engine_line}")[:3900])
        await deliver_admin_alerts(context)
    except Exception as exc:
        logger.exception("Manual data update failed: %s", exc)
        await note.edit_text(f"The data update failed: {exc}")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain removed and unknown commands instead of staying silent."""
    del context
    message = update.effective_message
    if message is None or not message.text:
        return
    name = message.text.split()[0].lstrip("/").split("@")[0].lower()
    replacement = REMOVED_COMMANDS.get(name)
    if replacement:
        await message.reply_text(f"/{name} was removed in the upgrade. Use {replacement} instead. See /help for everything.")
    else:
        await message.reply_text("I don't know that command. See /help.")


PLAIN_HELP = (
    "I did not understand that. You can ask in plain words, for example:\n"
    "• why is NABIL falling\n• news NABIL\n• what should I watch today\n• how is the market\n"
    "or send a symbol such as NABIL, or /help for every command."
)


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain questions ("why is NABIL falling", "how is the market") and bare symbols, mapped to the right command."""
    message = update.effective_message
    if message is None or not message.text:
        return
    engine = handlers.get_engine()
    known = {row["symbol"] for row in engine.db.query("SELECT symbol FROM securities")}
    routed = ask.route(message.text, known)
    if routed is None:
        await message.reply_text(PLAIN_HELP)
        return
    intent, symbol = routed
    if intent == "stock":
        context.args = [symbol]
        await handlers.stock_command(update, context)
    elif intent == "news":
        await message.reply_text(f"Fresh news and announcements for {symbol} (same as /news {symbol}), each with its usual price effect:")
        context.args = [symbol]
        await personal.news_command(update, context)
    elif intent == "market":
        await message.reply_text("The market as of the last close (same as /market): trend label, how many stocks are rising, and the sectors.")
        await handlers.market_command(update, context)
    elif intent == "watch":
        await message.reply_text("Stocks worth watching, each with the price to wait for (same as /watch). /top10 ranks today's candidates; /brief is the full morning brief.")
        await handlers.watch_command(update, context)
    elif intent == "why" and symbol:
        await handlers._run(update, f"Looking at {symbol}...", lambda scan: (manage.build_why(scan, symbol), None))


# ------------------------------------------------------------- scheduled jobs
def trading_day_only(callback: Any) -> Any:
    """Wrap a job so it does nothing on holidays listed in config/holidays.yaml."""

    @functools.wraps(callback)
    async def guarded(context: ContextTypes.DEFAULT_TYPE) -> None:
        if not CALENDAR.is_trading_day():
            logger.info("Skipping %s: today is not a trading day.", callback.__name__)
            return
        await callback(context)

    return guarded


async def deliver_admin_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send queued owner alerts (damaged file, failing source, detected holiday)."""
    for text in drain_admin_alerts():
        try:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[:4000], disable_web_page_preview=True)
        except Exception as exc:
            logger.error("Could not deliver an owner alert (it is tried again in a minute): %s", exc)
            queue_admin_alert(text)         # e.g. no internet right now: keep it for the next round


async def run_data_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Evening data update, then a fresh engine scan on the new data."""
    try:
        report = await asyncio.to_thread(get_ingestor().run)
    except Exception as exc:
        logger.exception("Data update crashed: %s", exc)
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⚠️ The data update crashed: {exc}")
        return
    logger.info("Data update finished. ok=%s\n%s", report.ok, report.as_text())
    if not report.ok:
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="⚠️ " + report.as_text(), disable_web_page_preview=True)
    try:
        logger.info("Engine refreshed after data update: %s", await asyncio.to_thread(handlers.refresh_after_update))
    except Exception as exc:
        logger.exception("Engine refresh after the data update failed: %s", exc)
    await deliver_admin_alerts(context)
    if _evening_report_time_passed():
        # A late update (slow source, or the PC woke up after 17:30): the alerts still go out if valid for the next session.
        await alerting.send_evening_alerts(context)


def _evening_report_time_passed() -> bool:
    """True outside the window between today's close and the evening report (when the data may still be incomplete)."""
    now = CALENDAR.now()
    hour_text, minute_text = str(bot_setting("reports", "evening_report", default="17:30")).split(":")
    report_time = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
    return not CALENDAR.is_trading_day(now.date()) or now >= report_time or now.time() < CALENDAR.open_time


async def evening_report_and_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The evening report, then the alerts that follow the evening scan (so an AMO can be placed for the next session)."""
    await manage.send_evening_report(context)
    await alerting.send_evening_alerts(context)


async def run_startup_catch_up(context: ContextTypes.DEFAULT_TYPE) -> None:
    """After a start (or the laptop waking up), fetch any trading days that were missed."""
    try:
        ingestor = get_ingestor()
        if ingestor.db.last_price_date() is None:
            logger.warning("Database is empty; run: python -m nepse_bot.ingest backfill")
            return
        last_run_text = ingestor.db.get_setting("last_ingest_run")
        last_run = datetime.fromisoformat(last_run_text) if last_run_text else None
        missing = await asyncio.to_thread(ingestor.missing_trading_days)
        if not missing and last_run is not None and last_run >= CALENDAR.last_close_datetime():
            logger.info("Startup catch-up: database is up to date.")
            await asyncio.to_thread(handlers.refresh_after_update)
            if _evening_report_time_passed():
                await alerting.send_evening_alerts(context)      # missed while the PC was off: sent now if still valid
            return
        logger.info("Startup catch-up: %s missing trading day(s); running a data update.", len(missing))
        await run_data_update(context)
    except Exception as exc:
        logger.exception("Startup catch-up failed: %s", exc)


_last_data_check: datetime | None = None


async def catch_up_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every few minutes: anything missed while the PC was asleep or off is done now, if it is still useful.

    * morning brief: until the market closes;
    * data update: whenever a finished trading day is missing (asked at most once an hour);
    * evening report and evening alerts: until the next session opens.
    """
    global _last_data_check
    engine = handlers.get_engine()
    try:
        if manage.morning_brief_due(engine.db, CALENDAR):
            logger.info("Catch-up: sending the missed morning brief.")
            await manage.send_morning_brief(context)
        now = CALENDAR.now()
        behind = (engine.db.last_price_date() or "") < CALENDAR.last_completed_trading_day(now).isoformat()
        if behind and _evening_report_time_passed() and (_last_data_check is None or (now - _last_data_check).total_seconds() >= 3600):
            _last_data_check = now
            logger.info("Catch-up: the database is behind; running the data update.")
            await run_startup_catch_up(context)
        if manage.evening_report_due(engine.db, CALENDAR):
            logger.info("Catch-up: sending the missed evening report and alerts.")
            await evening_report_and_alerts(context)
    except Exception as exc:
        logger.exception("Catch-up job failed: %s", exc)


async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Once a day (also right after a start if the backup of today is missing): database and user data into backups/."""
    from nepse_bot.utils import backup

    if not backup.backup_due():
        return
    try:
        result = await asyncio.to_thread(backup.make_backup)
        logger.info("Daily backup done: %s", result)
    except Exception as exc:
        logger.exception("Daily backup failed: %s", exc)
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⚠️ The daily backup failed: {exc}")


async def maintenance_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily housekeeping: debug samples, expired news cache, old chart files and log overflow."""
    del context
    try:
        from nepse_bot.utils import maintenance

        logger.info("Housekeeping done: %s", await asyncio.to_thread(maintenance.run))
    except Exception as exc:
        logger.warning("Housekeeping failed: %s", exc)


def is_owner(update: Update) -> bool:
    """The owner = the Telegram account or chat named in TELEGRAM_CHAT_ID."""
    user, chat = update.effective_user, update.effective_chat
    return bool((user and user.id in _OWNER_IDS) or (chat and chat.id in _OWNER_IDS))


async def selftest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/selftest (owner only) - one real sample of every message type, each marked TEST."""
    message = update.effective_message
    if message is None:
        return
    if not is_owner(update):
        await message.reply_text("/selftest is for the owner of the bot only.")
        return
    from nepse_bot.bot import selftest

    await message.reply_text("Self-test started: about 18 sample messages follow, each marked TEST. Nothing is recorded or changed.")
    try:
        await selftest.run(context.bot, message.chat_id)
    except Exception as exc:
        logger.exception("Self-test failed: %s", exc)
        await message.reply_text(f"The self-test stopped with an error: {exc}")


def _startup_notice_allowed(cooldown: timedelta = timedelta(minutes=10)) -> bool:
    path = DATA_DIR / "last_startup_notice.txt"
    try:
        last = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        if last.tzinfo is None:
            last = last.replace(tzinfo=KATHMANDU_TZ)
        return datetime.now(KATHMANDU_TZ) - last >= cooldown
    except (OSError, ValueError):
        return True


async def send_startup_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell the owner the bot is online, retrying if the internet is late."""
    attempt = int(context.job.data or 1) if context.job else 1
    if attempt == 1 and not _startup_notice_allowed():
        return
    text = "\n".join(
        [
            "NEPSE Analyzer Bot is online",
            f"Started: {BOT_STARTED_AT.strftime('%Y-%m-%d %H:%M')} Nepal time",
            f"Market: {CALENDAR.status_label()}",
            "",
            "Try /top10, /market or /status. /help lists everything.",
        ]
    )
    try:
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)
        atomic_write_text(DATA_DIR / "last_startup_notice.txt", datetime.now(KATHMANDU_TZ).isoformat())
    except Exception as exc:
        logger.warning("Startup notification failed on attempt %s: %s", attempt, exc)
        if attempt < 12 and context.job_queue is not None:
            context.job_queue.run_once(send_startup_notification, when=30, data=attempt + 1, name=f"startup_notice_retry_{attempt + 1}")


def timed_alert_check(callback: Any) -> Any:
    """Measure a live alert check: a slow one during the session pauses the broker-history download."""

    @functools.wraps(callback)
    async def timed(context: ContextTypes.DEFAULT_TYPE) -> None:
        from time import monotonic

        from nepse_bot.ingest import broker_worker

        started = monotonic()
        try:
            await callback(context)
        finally:
            broker_worker.note_alert_check(monotonic() - started)

    return timed


def schedule_jobs(application: Application) -> None:
    """Register every background job. Trading days come from the calendar."""
    trading_days = CALENDAR.scheduler_days()
    queue = application.job_queue
    for run_time in bot_setting("ingest", "run_times", default=["15:20", "17:00"]):
        hour_text, minute_text = str(run_time).split(":")
        queue.run_daily(
            trading_day_only(run_data_update),
            time=time(hour=int(hour_text), minute=int(minute_text), second=0, tzinfo=KATHMANDU_TZ),
            days=trading_days,
            name=f"data_update_{hour_text}{minute_text}",
            job_kwargs={"misfire_grace_time": 6 * 3600, "coalesce": True},
        )
    for name, callback, default in (("morning_brief", manage.send_morning_brief, "10:15"), ("evening_report", evening_report_and_alerts, "17:30")):
        hour_text, minute_text = str(bot_setting("reports", name, default=default)).split(":")
        queue.run_daily(
            trading_day_only(callback),
            time=time(hour=int(hour_text), minute=int(minute_text), second=0, tzinfo=KATHMANDU_TZ),
            days=trading_days,
            name=name,
            job_kwargs={"misfire_grace_time": 2 * 3600, "coalesce": True},
        )
    week_names = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
    week_day = str(bot_setting("reports", "weekly_summary_day", default="friday")).lower()
    hour_text, minute_text = str(bot_setting("reports", "weekly_summary", default="18:00")).split(":")
    queue.run_daily(
        manage.send_weekly_summary,
        time=time(hour=int(hour_text), minute=int(minute_text), second=0, tzinfo=KATHMANDU_TZ),
        days=(week_names.index(week_day) if week_day in week_names else 5,),
        name="weekly_summary",
        job_kwargs={"misfire_grace_time": 12 * 3600, "coalesce": True},
    )
    hour_text, minute_text = str(bot_setting("retest", "check_time", default="19:30")).split(":")
    queue.run_daily(
        extras.monthly_retest_job,
        time=time(hour=int(hour_text), minute=int(minute_text), second=0, tzinfo=KATHMANDU_TZ),
        name="monthly_retest",
        job_kwargs={"misfire_grace_time": 12 * 3600, "coalesce": True},
    )
    hour_text, minute_text = str(bot_setting("backup", "time", default="20:15")).split(":")
    queue.run_daily(
        daily_backup_job,
        time=time(hour=int(hour_text), minute=int(minute_text), second=0, tzinfo=KATHMANDU_TZ),
        name="daily_backup",
        job_kwargs={"misfire_grace_time": 20 * 3600, "coalesce": True},
    )
    queue.run_once(daily_backup_job, when=600, name="daily_backup_startup")
    # After sleep or a late start: missed brief, data update, evening report and alerts, while still useful.
    queue.run_repeating(catch_up_job, interval=int(bot_setting("reliability", "catch_up_minutes", default=10)) * 60, first=240, name="catch_up")
    queue.run_once(run_startup_catch_up, when=45, name="data_catch_up_startup")
    queue.run_repeating(timed_alert_check(personal.check_price_alerts), interval=300, first=120, name="price_alert_monitor")
    queue.run_repeating(timed_alert_check(alerting.intraday_job), interval=alerting.check_seconds(), first=150, name="intraday_alerts")
    queue.run_repeating(deliver_admin_alerts, interval=60, first=15, name="owner_alert_delivery")
    queue.run_daily(maintenance_job, time=time(hour=6, minute=0, second=0, tzinfo=KATHMANDU_TZ), name="housekeeping",
                    job_kwargs={"misfire_grace_time": 20 * 3600, "coalesce": True})
    queue.run_once(send_startup_notification, when=8, data=1, name="startup_notice")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled errors without stopping the bot."""
    logger.error("Unhandled bot error. Update=%r", update, exc_info=context.error)


def register_handlers(application: Application) -> None:
    """Attach every command. The access guard runs before all of them."""
    application.add_handler(TypeHandler(Update, access_guard), group=-1)
    handlers.register(application)
    personal.register(application)
    manage.register(application)
    alerting.register(application)
    extras.register(application)
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("holiday", holiday_command))
    application.add_handler(CommandHandler("dataupdate", dataupdate_command))
    application.add_handler(CommandHandler("selftest", selftest_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text))
    application.add_error_handler(error_handler)


def build_application() -> Application:
    """Create the Telegram application with handlers and jobs attached."""
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    register_handlers(application)
    schedule_jobs(application)
    return application


def _import_old_holdings(db: Any) -> None:
    """Copy holdings saved by the old /portfolio command into the position manager, once. Their dates are unknown."""
    from nepse_bot.portfolio import positions
    from nepse_bot.portfolio.fees import fee_schedule
    from nepse_bot.utils.health import queue_admin_alert

    holdings: dict[str, tuple[float, float]] = {}
    for user_id in ACCESS.allowed_user_ids:
        for symbol, holding in personal.user_store().get_portfolio(user_id).items():
            holdings[symbol] = (holding.quantity, holding.buy_price)
    added = positions.import_legacy_holdings(db, holdings, fee_schedule())
    if added:
        queue_admin_alert(
            "I copied your old holdings into /positions: " + ", ".join(added) + ". I do not know when you bought them. "
            "Please tell me each date: /bought date SYMBOL YYYY-MM-DD (it decides the tax rate and the trailing stop)."
        )


def main() -> None:
    """Start the bot."""
    log_path = configure_logging()
    logger.info("Logging to %s (rotating).", log_path)
    logger.info("Trading days: %s. Market now: %s.", CALENDAR.trading_day_names(), CALENDAR.status_label())
    try:
        from nepse_bot.db.store import get_database
        from nepse_bot.ingest.migrate import migrate_signal_journal

        logger.info("Database ready. %s saved signals are mirrored in it.", migrate_signal_journal(get_database()))
        _import_old_holdings(get_database())
    except Exception as exc:
        logger.exception("Database start-up step failed (the bot continues): %s", exc)
    application = build_application()
    try:
        from nepse_bot.ingest import broker_worker

        broker_worker.start_worker()        # broker history: quiet background download, continues where it stopped
        logger.info("Broker history worker started.")
    except Exception as exc:
        logger.exception("The broker history worker could not start (the bot continues): %s", exc)
    logger.info("NEPSE assistant is starting. Polling is about to begin.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
