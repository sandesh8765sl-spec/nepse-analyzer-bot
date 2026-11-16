"""Sends the automatic alerts (Telegram side of ``nepse_bot.alerts``).

* ``intraday_job`` - every few minutes while the market is open: early alerts and
  live alerts for the owner's holdings (stop hit or near, target reached);
* ``send_evening_alerts`` - after the evening scan (or on wake, if still valid for
  the next session): CONFIRMED / CANCELLED, BUY alerts, the top pick, holdings alerts;
* the "I bought it" button under an alert, which records the purchase like /bought.

The bot only tells the owner. It never places an order.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from nepse_bot.alerts import service
from nepse_bot.alerts.config import alert_setting
from nepse_bot.bot import handlers, manage, personal, reports
from nepse_bot.engine import exits

logger = logging.getLogger(__name__)

WATCHER = service.EarlyWatcher()


def _keyboard(item: service.Outgoing) -> InlineKeyboardMarkup | None:
    if item.button is None:
        return None
    symbol, quantity, price = item.button
    label = f"✅ I bought {quantity} {symbol} @ {price:g}"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"bt:{symbol}:{quantity}:{price:g}")]])


async def send_outgoing(bot: Any, items: list[service.Outgoing], chat_id: str | None = None) -> int:
    """Send alert messages to the owner. Returns how many were delivered."""
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    delivered = 0
    for item in items:
        parts = reports.split_message(item.text)
        for index, part in enumerate(parts):
            markup = _keyboard(item) if index == len(parts) - 1 else None
            try:
                await bot.send_message(chat_id=chat_id, text=part, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            except Exception as exc:
                logger.warning("Alert HTML rejected or send failed (%s); retrying as plain text.", exc)
                try:
                    await bot.send_message(chat_id=chat_id, text=handlers._strip_tags(part), reply_markup=markup, disable_web_page_preview=True)
                except Exception as second:
                    logger.error("Alert for %s could not be sent: %s", item.symbol or item.kind, second)
                    break
        else:
            delivered += 1
    return delivered


def _holdings_message(lines: list[str], title: str) -> list[service.Outgoing]:
    if not lines:
        return []
    from html import escape

    return [service.Outgoing("\n".join([f"🔔 <b>{title}</b>", *[escape(line) for line in lines], "", "Details: /positions"]), "position")]


# -------------------------------------------------------------------- evening
def build_evening(scan: Any) -> list[service.Outgoing]:
    """Everything owed after the evening scan (records it as sent)."""
    engine = handlers.get_engine()
    items = service.evening_alerts(engine, scan, exits.exit_style(engine.db))
    views, _, _ = manage.review_all(scan)
    from nepse_bot.bot.personal import user_store

    watchlist = user_store().iter_watchlist_symbols()
    title = "Alerts for your positions and watchlist" if watchlist else "Alerts for your positions"
    return items + _holdings_message(service.position_alerts(engine.db, views, scan.as_of, watchlist=watchlist), title)


async def send_evening_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """After the evening scan, or on wake: send the alerts if they are still valid for the next session."""
    try:
        engine = handlers.get_engine()
        scan = await handlers._scan()
        if not service.evening_due(engine.db, scan, engine.calendar):
            return
        items = await asyncio.to_thread(build_evening, scan)
        sent = await send_outgoing(context.bot, items)
        logger.info("Evening alerts for %s: %s built, %s sent.", scan.as_of, len(items), sent)
    except Exception as exc:
        logger.exception("Evening alerts failed: %s", exc)


# ------------------------------------------------------------------- intraday
def _index_change(db: Any, as_of: str, live_index: float | None) -> float | None:
    previous = db.scalar("SELECT close FROM index_daily WHERE index_code = 'NEPSE' AND date <= ? ORDER BY date DESC LIMIT 1", (as_of,))
    return (live_index / float(previous) - 1.0) * 100.0 if live_index and previous else None


def build_intraday(scan: Any, quotes: dict[str, dict[str, float | None]], live_index: float | None) -> list[service.Outgoing]:
    engine = handlers.get_engine()
    now = engine.calendar.now()
    items = WATCHER.tick(engine, scan, quotes, _index_change(engine.db, scan.as_of, live_index), now, exits.exit_style(engine.db))
    views, _, _ = manage.review_all(scan)
    if views:
        prices = {symbol: float(quote["price"]) for symbol, quote in quotes.items() if quote.get("price")}
        items += _holdings_message(service.position_alerts(engine.db, views, now.date().isoformat(), prices), "Your positions — live")
    return items


async def intraday_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every few minutes during the session: early alerts and live alerts for the holdings."""
    engine = handlers.get_engine()
    if not engine.calendar.is_market_open():
        return
    try:
        client = personal._nepse()
        quotes = await asyncio.to_thread(client.live_quotes)
        if not quotes:
            return
        live_index = await asyncio.to_thread(client.live_index)
        scan = await handlers._scan()
        items = await asyncio.to_thread(build_intraday, scan, quotes, live_index)
        if items:
            await send_outgoing(context.bot, items)
    except Exception as exc:
        logger.warning("Intraday alert check failed: %s", exc)


def check_seconds() -> int:
    return max(60, int(float(alert_setting("early", "check_minutes", default=5)) * 60))


# ------------------------------------------------------------ "I bought" button
async def bought_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The button under an alert: record the purchase exactly like /bought SYMBOL QTY PRICE."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer("Recording your purchase...")
    _, symbol, quantity, price = (query.data or "bt:::").split(":", 3)
    context.args = [symbol, quantity, price]
    await manage.bought_command(update, context)
    await query.message.reply_text(f"Different quantity or price? Send /positions remove {symbol} and then /bought {symbol} QTY PRICE with the real numbers.")


def register(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(bought_button, pattern=r"^bt:"))
