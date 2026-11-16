"""Stage 5 commands and scheduled messages.

/bought, /sold, /positions (also /portfolio), /calendar, /brief, /evening,
and the jobs that send the morning brief, evening report and weekly summary.
The bot only records what the owner says was traded; it never places orders.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from nepse_bot.bot import briefs, handlers
from nepse_bot.engine import exits
from nepse_bot.engine.engine import ScanResult
from nepse_bot.engine.plan import resistances_above
from nepse_bot.events import engine as events
from nepse_bot.events import unlocks
from nepse_bot.portfolio import protection
from nepse_bot.settings import bot_setting
from nepse_bot.portfolio import positions
from nepse_bot.portfolio.fees import fee_schedule
from nepse_bot.trading_calendar import get_calendar

logger = logging.getLogger(__name__)


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "").lower().replace("rs", ""))
    except ValueError:
        return None


def parse_trade(args: list[str], today: str) -> tuple[str, int, float, str] | str:
    """Understand ``SYMBOL QTY PRICE [YYYY-MM-DD]``. Returns the parts, or a message saying what is wrong."""
    if len(args) < 3:
        return "Use: SYMBOL QTY PRICE, for example NABIL 100 520 (add a date like 2026-10-01 for an older trade)."
    symbol = re.sub(r"[^A-Za-z0-9]", "", args[0]).upper()
    quantity, price = _number(args[1]), _number(args[2])
    if not symbol or not quantity or not price or quantity <= 0 or price <= 0 or quantity != int(quantity):
        return "Quantity must be a whole number of shares and the price a positive number, for example NABIL 100 520."
    day = today
    if len(args) >= 4:
        try:
            day = date.fromisoformat(args[3]).isoformat()
        except ValueError:
            return "Write the date as YYYY-MM-DD, for example 2026-10-01."
        if day > today:
            return "That date is in the future."
    return symbol, int(quantity), float(price), day


def check_trade_day(db: Any, calendar: Any, symbol: str, day: str, price: float | None = None) -> tuple[str | None, list[str]]:
    """Is ``day`` a day the stock could have been traded? Returns (refusal or None, warnings).

    Past dates are checked against the database itself (the exchange traded Sunday-Thursday
    before April 2026, so the weekday alone is not enough); dates after the last stored
    session are checked against the trading calendar.
    """
    last = db.last_price_date()
    warnings: list[str] = []
    if last and day <= last:
        if not db.scalar("SELECT COUNT(*) FROM daily_prices WHERE date = ?", (day,)):
            before = db.scalar("SELECT MAX(date) FROM daily_prices WHERE date < ?", (day,))
            after = db.scalar("SELECT MIN(date) FROM daily_prices WHERE date > ?", (day,))
            near = " or ".join(item for item in (before, after) if item)
            return f"{day} was not a trading day (the market was closed). The nearest trading days are {near}. Please send it again with the right date.", warnings
        bar = db.query("SELECT low, high FROM daily_prices WHERE symbol = ? AND date = ?", (symbol, day))
        if not bar:
            warnings.append(f"{symbol} has no trade in my database on {day}; recorded anyway, please check the date.")
        elif price and bar[0]["low"] and bar[0]["high"] and not float(bar[0]["low"]) * 0.98 <= price <= float(bar[0]["high"]) * 1.02:
            warnings.append(f"On {day} {symbol} traded between {float(bar[0]['low']):,.1f} and {float(bar[0]['high']):,.1f}; your price {price:,.1f} is outside that. Recorded anyway, please check.")
    elif not calendar.is_trading_day(date.fromisoformat(day)):
        return f"{day} is not a trading day. Please send it again with the day you actually traded.", warnings
    return None, warnings


# --------------------------------------------------------------------- glue
def review_all(scan: ScanResult) -> tuple[list[positions.PositionView], list[str], list[str]]:
    """Every open position reviewed. Returns (views, portfolio-risk lines, symbols without prices)."""
    engine = handlers.get_engine()
    db, calendar, fees = engine.db, engine.calendar, fee_schedule()
    rows = positions.open_positions(db)
    style = exits.exit_style(db)
    closures = events.upcoming_closures(db, scan.as_of, calendar, days=45, symbols=[row["symbol"] for row in rows]) if rows else []
    unlock_items = [note for note in unlocks.upcoming_unlocks(db, scan.as_of, calendar, symbols=[row["symbol"] for row in rows]) if note.flagged] if rows else []
    views, missing = [], []
    for row in rows:
        item = scan.results.get(row["symbol"])
        sector = item.sector if item else (db.scalar("SELECT sector FROM securities WHERE symbol = ?", (row["symbol"],)) or "")
        view = positions.review_position(
            row, positions.bars_for(db, row["symbol"]), today=scan.as_of, style=style, fees=fees, profile=scan.profile, sector=sector,
            closures=closures, verdict=item.verdict if item else None, verdict_reasons=item.why_not_buy if item else None,
            flags=item.flags if item else None, sold_part=positions.has_sold_part(db, row["id"]),
        )
        if view is None:
            missing.append(row["symbol"])
        else:
            view.notes.extend(
                f"{note.short()}. Extra supply can weigh on the price around that date."
                for note in unlock_items if note.symbol == view.symbol
            )
            views.append(view)
    risk = positions.portfolio_risk(views, scan.profile, scan.regime.overheated)
    paused = protection.status_line(db, scan.as_of)
    if paused:
        risk.append(paused)
    return views, risk, missing


def new_buy_notes(scan: ScanResult, symbol: str) -> list[str]:
    """Portfolio-risk remarks about adding ``symbol`` (for the /stock report)."""
    item = scan.results.get(symbol)
    if item is None or item.plan is None or not positions.open_positions(handlers.get_engine().db):
        return []
    views, _, _ = review_all(scan)
    return positions.new_position_warnings(views, scan.profile, symbol, item.sector, item.plan.position_value)


def index_move(db: Any, as_of: str) -> tuple[float, float] | None:
    rows = db.query("SELECT close FROM index_daily WHERE index_code = 'NEPSE' AND date <= ? ORDER BY date DESC LIMIT 2", (as_of,))
    if len(rows) < 2 or not rows[1]["close"]:
        return None
    return float(rows[0]["close"]), 100.0 * (float(rows[0]["close"]) / float(rows[1]["close"]) - 1.0)


def upcoming_holidays(days: int = 30) -> list[tuple[str, str]]:
    calendar = get_calendar()
    today = calendar.now().date()
    return [(day.isoformat(), reason) for day, reason in sorted(calendar.holidays().items()) if today <= day <= today + timedelta(days=days)]


def build_positions_text(scan: ScanResult) -> str:
    views, risk, missing = review_all(scan)
    return briefs.format_positions(views, risk, scan.as_of, exits.exit_style(handlers.get_engine().db), missing)


def build_calendar_text(scan: ScanResult) -> str:
    engine = handlers.get_engine()
    today = engine.calendar.now().date().isoformat()
    closures = events.upcoming_closures(engine.db, today, engine.calendar, days=30)
    held = {row["symbol"] for row in positions.open_positions(engine.db)}
    prices = {symbol: item.close for symbol, item in scan.results.items()}
    known = events.corporate_events(engine.db, today, prices=dict(prices))
    pending = [item for item in known if item.closure_day is None and item.kind in {"right", "bonus", "dividend", "book_closure"}]
    meetings = [item for item in known if item.kind in {"agm", "merger"}]
    unlock_items = unlocks.upcoming_unlocks(engine.db, today, engine.calendar)
    return briefs.format_calendar(closures, held, upcoming_holidays(), meetings, today, pending, prices, unlock_items)


def corporate_lines(scan: ScanResult, symbol: str) -> list[str]:
    """Known upcoming corporate events of one stock, however old the announcement (for /stock and the alerts)."""
    engine = handlers.get_engine()
    item = scan.results.get(symbol)
    today = engine.calendar.now().date().isoformat()
    found = events.corporate_events(engine.db, today, [symbol], {symbol: item.close} if item else None)
    lines = [event.line().split(": ", 1)[1] for event in found]
    # Lock-in expiries beyond the warning window (the near ones are already shown as a warning).
    lines.extend(item.short() for item in unlocks.upcoming_unlocks(engine.db, today, engine.calendar, symbols=[symbol]) if not item.flagged)
    return lines


def watchlist_symbols() -> list[str]:
    from nepse_bot.bot.personal import user_store

    return user_store().iter_watchlist_symbols()


def build_morning_brief(scan: ScanResult) -> str:
    engine = handlers.get_engine()
    today = engine.calendar.now().date().isoformat()
    views, risk, _ = review_all(scan)
    return briefs.format_morning_brief(
        scan, views, risk, events.load_events(engine.db, today), events.upcoming_closures(engine.db, today, engine.calendar, days=14),
        index_move(engine.db, scan.as_of), engine.calendar, watchlist_symbols(),
    )


def build_evening_report(scan: ScanResult) -> str:
    engine = handlers.get_engine()
    views, risk, _ = review_all(scan)
    breadth = engine.db.query("SELECT advancers, decliners, turnover FROM market_breadth_daily WHERE date = ?", (scan.as_of,))
    from nepse_bot.alerts import service

    # New alerts for the holdings (each event once a day): shown here, so they are not sent a second time.
    alerts = service.position_alerts(engine.db, views, scan.as_of) if views and not scan.stale else []
    return briefs.format_evening_report(
        scan, views, risk, events.load_events(engine.db, scan.as_of), index_move(engine.db, scan.as_of), breadth[0] if breadth else None, engine.calendar,
        position_alerts=alerts,
    )


def build_why(scan: ScanResult, symbol: str) -> str:
    """The answer to "why is X falling / rising"."""
    from nepse_bot.bot import ask

    engine = handlers.get_engine()
    item = scan.results.get(symbol)
    if item is None or item.verdict == "IGNORE":
        return handlers._stock_report(scan, symbol)[0]
    closes = [float(row["close"]) for row in engine.db.query(
        "SELECT COALESCE(adj_close, close) AS close FROM daily_prices WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 21", (symbol, scan.as_of))]
    changes = {
        "roc5": (closes[0] / closes[5] - 1) * 100 if len(closes) > 5 and closes[5] else None,
        "roc20": (closes[0] / closes[20] - 1) * 100 if len(closes) > 20 and closes[20] else None,
    }
    today = engine.calendar.now().date().isoformat()
    lines = [line.split(": ", 1)[-1] for line in events.event_lines(events.load_events(engine.db, today, [symbol]), include_routine=True)]
    return ask.explain_move(item, scan, changes, lines + corporate_lines(scan, symbol))


def build_weekly_summary(scan: ScanResult) -> str:
    engine = handlers.get_engine()
    data = briefs.weekly_data(engine.db, scan.as_of)
    views, _, _ = review_all(scan)
    today = engine.calendar.now().date().isoformat()
    closures = events.upcoming_closures(engine.db, today, engine.calendar, days=10)
    return briefs.format_weekly(data, positions.realised_between(engine.db, data["start"], data["end"]), views, closures, upcoming_holidays(10))


# ------------------------------------------------------------------ commands
def _levels_for_new_position(scan: ScanResult, symbol: str, price: float, fresh: bool = True) -> tuple[float | None, float | None, float | None]:
    """Stop, reference target and 52-week high to store with a new position.

    A fresh purchase near today's plan takes the plan's levels. Anything else (an older
    purchase, a different price) gets no stored stop and a target above the CURRENT price:
    levels are never worked out from the buy price.
    """
    item = scan.results.get(symbol)
    plan = (item.plan or item.blocked_plan) if item else None
    if fresh and plan is not None and abs(plan.entry / price - 1.0) <= 0.05:
        return plan.stop, plan.target_1, plan.high_52w
    db = handlers.get_engine().db
    frame = db.frame(
        "SELECT COALESCE(adj_high, high) AS high, COALESCE(adj_low, low) AS low, COALESCE(adj_close, close) AS close FROM daily_prices WHERE symbol = ? ORDER BY date",
        (symbol,),
    )
    if len(frame) < 30:
        return None, None, None
    current = float(frame["close"].iloc[-1])
    levels = resistances_above(frame, current)
    high = float(frame["high"].tail(252).max())
    return None, (round(levels[0][0], 1) if levels else None), (round(high, 1) if high > current else None)


async def _set_date(message: Any, args: list[str]) -> None:
    """/bought date SYMBOL YYYY-MM-DD - give the real purchase date of a holding."""
    engine = handlers.get_engine()
    if len(args) < 2:
        await message.reply_text("Use: /bought date SYMBOL YYYY-MM-DD, for example /bought date HRL 2024-03-14")
        return
    symbol = re.sub(r"[^A-Za-z0-9]", "", args[0]).upper()
    try:
        day = date.fromisoformat(args[1]).isoformat()
    except ValueError:
        await message.reply_text("Write the date as YYYY-MM-DD, for example 2024-03-14.")
        return
    if day > engine.calendar.now().date().isoformat():
        await message.reply_text("That date is in the future.")
        return
    refusal, warnings = check_trade_day(engine.db, engine.calendar, symbol, day)
    if refusal:
        await message.reply_text(refusal)
        return
    if not positions.set_purchase_date(engine.db, symbol, day):
        await message.reply_text(f"You have no open position in {symbol}. See /positions.")
        return
    scan = await handlers._scan()
    await handlers._send(message, f"✅ {symbol}: purchase date set to {day}." + "".join(f"\n⚠️ {text}" for text in warnings) + "\n\n" + await asyncio.to_thread(build_positions_text, scan))


async def bought_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bought SYMBOL QTY PRICE [DATE] - record a purchase."""
    message = update.effective_message
    if message is None:
        return
    engine = handlers.get_engine()
    args = list(context.args or [])
    if args and args[0].lower() == "date":
        await _set_date(message, args[1:])
        return
    today = engine.calendar.now().date().isoformat()
    parsed = parse_trade(args, today)
    if isinstance(parsed, str):
        await message.reply_text("/bought " + parsed)
        return
    symbol, quantity, price, day = parsed
    if not engine.db.scalar("SELECT COUNT(*) FROM daily_prices WHERE symbol = ?", (symbol,)):
        await message.reply_text(f"I don't have prices for {symbol}, so I can't manage it. Check the symbol.")
        return
    refusal, warnings = check_trade_day(engine.db, engine.calendar, symbol, day, price)
    if refusal:
        await message.reply_text(refusal)
        return
    try:
        scan = await handlers._scan()
        fresh = day >= (engine.calendar.now().date() - timedelta(days=7)).isoformat()
        stop, target, high = await asyncio.to_thread(_levels_for_new_position, scan, symbol, price, fresh)
        await asyncio.to_thread(positions.record_buy, engine.db, symbol, quantity, price, day, fee_schedule(), stop, target, high, today)
        cost = fee_schedule().buy_cost(quantity, price)
        held_days = (date.fromisoformat(today) - date.fromisoformat(day)).days
        tax = f" Held {held_days} days so far: capital gains tax rate {fee_schedule().cgt_percent(held_days):g}%." if held_days > 0 else ""
        note = "".join(f"\n⚠️ {text}" for text in warnings)
        await handlers._send(message, f"✅ Recorded: bought {quantity:,} {symbol} at {price:,.1f} on {day} (cost with fees Rs {cost:,.0f}).{tax}{note}\n\n" + await asyncio.to_thread(build_positions_text, scan))
    except Exception as exc:
        logger.exception("/bought failed: %s", exc)
        await message.reply_text(f"Sorry, that failed: {exc}")


async def sold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sold SYMBOL QTY PRICE [DATE] - record a sale and show the result after fees and tax."""
    message = update.effective_message
    if message is None:
        return
    engine = handlers.get_engine()
    parsed = parse_trade(list(context.args or []), engine.calendar.now().date().isoformat())
    if isinstance(parsed, str):
        await message.reply_text("/sold " + parsed)
        return
    symbol, quantity, price, day = parsed
    position = positions.get_position(engine.db, symbol)
    if position is None:
        await message.reply_text(f"You have no open position in {symbol}. See /positions.")
        return
    if quantity > position["quantity"]:
        await message.reply_text(f"You hold {position['quantity']:,.0f} {symbol}; you cannot sell {quantity:,}.")
        return
    try:
        sale = await asyncio.to_thread(positions.record_sell, engine.db, symbol, quantity, price, day, fee_schedule())
        await handlers._send(message, briefs.format_sale(symbol, price, sale))
    except Exception as exc:
        logger.exception("/sold failed: %s", exc)
        await message.reply_text(f"Sorry, that failed: {exc}")


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/positions (also /portfolio) - holdings with HOLD / TRIM / SELL. '/portfolio add' and 'remove' still work."""
    message = update.effective_message
    if message is None:
        return
    args = list(context.args or [])
    action = args[0].lower() if args else "show"
    if action == "add":
        context.args = args[1:]
        await bought_command(update, context)
        return
    if action in {"remove", "delete", "del"} and len(args) >= 2:
        engine = handlers.get_engine()
        symbol = args[1].upper()
        removed = positions.remove_position(engine.db, symbol, engine.calendar.now().date().isoformat())
        await message.reply_text(f"{symbol} removed from your positions (kept in the records as removed)." if removed else f"No open position in {symbol}.")
        return
    await handlers._run(update, "Reviewing your positions...", lambda scan: (build_positions_text(scan), None))


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/calendar - book closures and holidays ahead."""
    del context
    await handlers._run(update, "Building the calendar...", lambda scan: (build_calendar_text(scan), None))


async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brief - the morning brief, on demand."""
    del context
    await handlers._run(update, "Preparing the brief...", lambda scan: (build_morning_brief(scan), None))


async def evening_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/evening - the evening report, on demand."""
    del context
    await handlers._run(update, "Preparing the evening report...", lambda scan: (build_evening_report(scan), None))


# ---------------------------------------------------------------- scheduled
MORNING_SENT, EVENING_SENT = "sent.morning_brief", "sent.evening_report"
_send_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _send_lock
    if _send_lock is None:
        _send_lock = asyncio.Lock()
    return _send_lock


def _clock(name: str, default: str, now: datetime) -> datetime:
    hour_text, minute_text = str(bot_setting("reports", name, default=default)).split(":")
    return now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)


def morning_brief_due(db: Any, calendar: Any, now: datetime | None = None) -> bool:
    """Owed and still useful: a trading day, after the brief time, before the close, not sent today."""
    now = now or calendar.now()
    if not calendar.is_trading_day(now.date()) or db.get_setting(MORNING_SENT) == now.date().isoformat():
        return False
    return _clock("morning_brief", "10:15", now) <= now and now.time().replace(tzinfo=None) < calendar.close_time


def evening_report_due(db: Any, calendar: Any, now: datetime | None = None) -> bool:
    """Owed and still useful: the last finished session has its data, no report was sent for it, the next session has not opened."""
    now = now or calendar.now()
    day = calendar.last_completed_trading_day(now)
    if calendar.is_market_open(now) or db.get_setting(EVENING_SENT) == day.isoformat():
        return False
    if day == now.date() and now < _clock("evening_report", "17:30", now):
        return False                      # the data update of today is still running
    if calendar.is_trading_day(now.date()) and now.date() > day and now.time().replace(tzinfo=None) >= calendar.open_time:
        return False                      # the next session has started: the old report is old news
    return (db.last_price_date() or "") >= day.isoformat()


async def _send_scheduled(context: ContextTypes.DEFAULT_TYPE, name: str, build: Any) -> bool:
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    try:
        scan = await handlers._scan()
        text = await asyncio.to_thread(build, scan)
    except Exception as exc:
        logger.exception("%s could not be built: %s", name, exc)
        return False
    from nepse_bot.bot import reports

    for part in reports.split_message(text):
        try:
            await context.bot.send_message(chat_id=chat_id, text=part, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:
            logger.warning("%s: HTML rejected or send failed (%s); retrying as plain text.", name, exc)
            try:
                await context.bot.send_message(chat_id=chat_id, text=handlers._strip_tags(part), disable_web_page_preview=True)
            except Exception as second:
                logger.error("%s could not be sent: %s", name, second)
    logger.info("%s sent.", name)
    return True


async def send_morning_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The scheduled brief, or the catch-up after the PC was asleep. Never sent twice for one day."""
    engine = handlers.get_engine()
    async with _lock():
        if not morning_brief_due(engine.db, engine.calendar):
            return
        if await _send_scheduled(context, "Morning brief", build_morning_brief):
            engine.db.set_setting(MORNING_SENT, engine.calendar.now().date().isoformat())


async def send_evening_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The scheduled evening report, or the catch-up on wake. Never sent twice for one session."""
    engine = handlers.get_engine()
    async with _lock():
        if not evening_report_due(engine.db, engine.calendar):
            return
        if await _send_scheduled(context, "Evening report", build_evening_report):
            engine.db.set_setting(EVENING_SENT, engine.calendar.last_completed_trading_day().isoformat())


async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_scheduled(context, "Weekly summary", build_weekly_summary)


def register(application: Application) -> None:
    """Attach the Stage 5 commands to the bot."""
    application.add_handler(CommandHandler("bought", bought_command))
    application.add_handler(CommandHandler("sold", sold_command))
    for name in ("positions", "portfolio"):
        application.add_handler(CommandHandler(name, positions_command))
    application.add_handler(CommandHandler("calendar", calendar_command))
    application.add_handler(CommandHandler("brief", brief_command))
    application.add_handler(CommandHandler("evening", evening_command))
