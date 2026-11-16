"""/chart, /watchlist and /retest (the monthly re-test that only suggests)."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from html import escape
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from nepse_bot.backtest import monthly
from nepse_bot.bot import briefs, chart, handlers, manage
from nepse_bot.bot.personal import user_store
from nepse_bot.engine.engine import ScanResult

logger = logging.getLogger(__name__)
_retest_running = False


# --------------------------------------------------------------------- /chart
def build_chart(scan: ScanResult, symbol: str) -> tuple[bytes | None, str]:
    """The chart picture and its caption (None and a reason when there is nothing to draw)."""
    engine = handlers.get_engine()
    frame = chart.chart_frame(engine.db, symbol, scan.as_of)
    if len(frame) < 30:
        return None, f"I don't have enough price history for {symbol} to draw a chart."
    item = scan.results.get(symbol)
    views, _, _ = manage.review_all(scan)
    view = next((entry for entry in views if entry.symbol == symbol), None)
    levels = chart.chart_levels(item, view)
    subtitle = f"{item.verdict}, score {item.score:.0f}" if item is not None else ""
    picture = chart.render_chart(frame, symbol, levels, subtitle)
    parts = [f"{symbol} to {scan.as_of}. Lines: EMA 20 (orange), EMA 50 (blue), EMA 200 (black)."]
    if view is not None:
        parts.append("Dashed: your trailing stop, your average price and the reference target.")
    elif levels:
        parts.append("Dashed: the plan's entry, stop and reference target (next major resistance).")
    else:
        parts.append("No plan today, so no stop or target line.")
    parts.append(f"Details: /stock {symbol}")
    return picture, " ".join(parts)


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chart SYMBOL."""
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Tell me which stock. Example: /chart NABIL")
        return
    symbol = re.sub(r"[^A-Za-z0-9]", "", context.args[0]).upper()
    try:
        scan = await handlers._scan()
        picture, caption = await asyncio.to_thread(build_chart, scan, symbol)
        if picture is None:
            await message.reply_text(caption)
        else:
            await message.reply_photo(photo=io.BytesIO(picture), caption=caption)
    except Exception as exc:
        logger.exception("Chart failed: %s", exc)
        await message.reply_text(f"Sorry, the chart failed: {exc}")


# ----------------------------------------------------------------- /watchlist
def format_watchlist(scan: ScanResult, symbols: list[str]) -> str:
    if not symbols:
        return "Your watchlist is empty. Add stocks with: /watchlist add NABIL NTC"
    lines = briefs.watchlist_lines(scan, symbols)
    lines.extend(["", "These stocks are included in the morning brief and in news and event alerts.",
                  "/watchlist add SYMBOL • /watchlist remove SYMBOL • /chart SYMBOL • /stock SYMBOL"])
    return "\n".join(lines)


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watchlist [show] | add SYMBOL... | remove SYMBOL..."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    args = [re.sub(r"[^A-Za-z0-9]", "", word).upper() for word in (context.args or [])]
    action = args[0].lower() if args else "show"
    store = user_store()
    notes: list[str] = []
    if action in {"add", "remove", "delete", "del"}:
        if len(args) < 2:
            await message.reply_text(f"Tell me which stock. Example: /watchlist {action} NABIL")
            return
        db = handlers.get_engine().db
        for symbol in args[1:]:
            if action == "add":
                if db.scalar("SELECT COUNT(*) FROM securities WHERE symbol = ?", (symbol,)):
                    store.add_watchlist_symbol(user.id, symbol)
                    notes.append(f"Added {symbol}.")
                else:
                    notes.append(f"I don't know the symbol {symbol}; not added.")
            else:
                known = symbol in store.get_watchlist(user.id)
                store.remove_watchlist_symbol(user.id, symbol)
                notes.append(f"Removed {symbol}." if known else f"{symbol} was not on your watchlist.")
    elif action not in {"show", "list"}:
        await message.reply_text("Use: /watchlist, /watchlist add NABIL, /watchlist remove NABIL")
        return
    try:
        scan = await handlers._scan()
        text = format_watchlist(scan, store.get_watchlist(user.id))
    except Exception as exc:
        logger.exception("Watchlist failed: %s", exc)
        text = "Watchlist: " + (", ".join(store.get_watchlist(user.id)) or "empty")
    await handlers._send(message, "\n".join([*[escape(note) for note in notes], *([""] if notes else []), text]))


# -------------------------------------------------------------------- /retest
async def _run_retest(bot: Any, chat_id: Any) -> None:
    global _retest_running
    if _retest_running:
        return
    _retest_running = True
    engine = handlers.get_engine()
    try:
        text = await asyncio.to_thread(monthly.run, engine.db, engine.calendar.now().date())
    except Exception as exc:
        logger.exception("Monthly re-test failed: %s", exc)
        text = f"🧪 The monthly re-test failed: {escape(str(exc)[:300])}. Nothing was changed. Try /retest run later."
    finally:
        _retest_running = False
    for part in handlers.reports.split_message(text):
        await bot.send_message(chat_id=chat_id, text=part, parse_mode="HTML", disable_web_page_preview=True)


async def monthly_retest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily check: once per calendar month, run the re-test and send the suggestions."""
    engine = handlers.get_engine()
    if _retest_running or not monthly.due(engine.db, engine.calendar.now().date()):
        return
    await _run_retest(context.bot, os.getenv("TELEGRAM_CHAT_ID"))


async def retest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/retest (show the last suggestions), /retest run, /retest apply."""
    message = update.effective_message
    if message is None:
        return
    action = (context.args[0].lower() if context.args else "show")
    db = handlers.get_engine().db
    if action == "apply":
        await message.reply_text(await asyncio.to_thread(monthly.apply, db))
    elif action == "run":
        if _retest_running:
            await message.reply_text("A re-test is already running. You get the result when it is done.")
            return
        await message.reply_text("Re-test started. It takes about an hour; the bot keeps working. Nothing will be changed without /retest apply.")
        context.application.create_task(_run_retest(context.bot, message.chat_id))
    else:
        await handlers._send(message, escape(monthly.pending_text(db)))


def register(application: Application) -> None:
    application.add_handler(CommandHandler("chart", chart_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("retest", retest_command))
