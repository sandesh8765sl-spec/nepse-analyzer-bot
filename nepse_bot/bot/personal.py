"""Personal commands: price alerts and news.

Holdings are managed in ``manage.py`` (/bought, /sold, /positions). News goes
through the events engine: fresh items only, duplicates merged, each with its
event type and usual price effect, and one net statement per stock.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from nepse_bot.bot import handlers, reports
from nepse_bot.storage.user_store import StockAlert, UserStore
from nepse_bot.trading_calendar import get_calendar

logger = logging.getLogger(__name__)

_user_store: UserStore | None = None
_nepse_client: Any = None
_news_fetcher: Any = None


def user_store() -> UserStore:
    """Return the saved-user-data store (alerts and holdings)."""
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store


def _nepse() -> Any:
    global _nepse_client
    if _nepse_client is None:
        from nepse_bot.sources.nepse_official import NepseOfficialClient

        _nepse_client = NepseOfficialClient()
    return _nepse_client


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "").replace("rs", "").replace("Rs", ""))
    except ValueError:
        return None


def last_closes(symbols: list[str]) -> dict[str, float]:
    """Last stored close for each symbol."""
    if not symbols:
        return {}
    db = handlers.get_engine().db
    marks = ",".join("?" for _ in symbols)
    rows = db.query(
        f"SELECT p.symbol, p.close FROM daily_prices p JOIN (SELECT symbol, MAX(date) AS d FROM daily_prices "
        f"WHERE symbol IN ({marks}) GROUP BY symbol) m ON m.symbol = p.symbol AND m.d = p.date",
        symbols,
    )
    return {row["symbol"]: float(row["close"]) for row in rows}


# -------------------------------------------------------------------- alerts
def parse_alert(args: list[str], reference_price: float | None) -> StockAlert | None:
    """Understand ``NABIL above 600``, ``NABIL below 500`` or ``NABIL 600``."""
    if len(args) < 2:
        return None
    symbol = args[0].strip().upper()
    if len(args) >= 3 and args[1].lower() in {"above", "below"}:
        value = _number(args[2])
        return StockAlert(symbol=symbol, kind="price", operator=args[1].lower(), value=value) if value else None
    value = _number(args[1])
    if not value or reference_price is None:
        return None
    return StockAlert(symbol=symbol, kind="price", operator="above" if value >= reference_price else "below", value=value)


def alert_hit(alert: StockAlert, price: float) -> bool:
    """Return whether a price alert has triggered."""
    if alert.kind != "price" or alert.value is None:
        return False
    return price >= alert.value if alert.operator == "above" else price <= alert.value


def format_alerts(alerts: list[StockAlert]) -> str:
    """List a user's alerts."""
    price_alerts = [(index, alert) for index, alert in enumerate(alerts, start=1)]
    if not price_alerts:
        return "You have no alerts. Add one with: /alert NABIL above 600"
    lines = ["🔔 <b>Your price alerts</b>", ""]
    for index, alert in price_alerts:
        if alert.kind == "price" and alert.value is not None:
            lines.append(f"{index}. {escape(alert.symbol)} {alert.operator} {alert.value:,.1f}")
        else:
            lines.append(f"{index}. {escape(alert.symbol)} ({escape(alert.kind)}) — old alert type, no longer checked; remove it")
    lines.extend(["", "Remove one with: /alert remove NUMBER"])
    return "\n".join(lines)


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alert SYMBOL PRICE, /alert SYMBOL above|below PRICE, /alert remove N."""
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if message is None or user is None or chat is None:
        return
    args = list(context.args or [])
    store = user_store()
    if not args or args[0].lower() in {"list", "show"}:
        await handlers._send(message, format_alerts(store.get_alerts(user.id)))
        return
    if args[0].lower() in {"remove", "delete", "del"} and len(args) >= 2:
        index = int(args[1]) if args[1].isdigit() else 0
        await message.reply_text(f"Alert {index} removed." if store.remove_alert(user.id, index) else "No alert with that number. See /alerts")
        return
    symbol = args[0].strip().upper()
    reference = last_closes([symbol]).get(symbol)
    if reference is None:
        await message.reply_text(f"I don't have prices for {symbol}, so I can't watch it.")
        return
    alert = parse_alert(args, reference)
    if alert is None:
        await message.reply_text("Use: /alert NABIL above 600, /alert NABIL below 500, or /alert NABIL 600")
        return
    store.add_alert(user.id, chat.id, alert)
    await message.reply_text(
        f"Alert saved: {alert.symbol} {alert.operator} {alert.value:,.1f} (last close {reference:,.1f}). "
        "Checked every 5 minutes while the market is open."
    )


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alerts - list alerts."""
    del context
    if update.effective_message is not None and update.effective_user is not None:
        await handlers._send(update.effective_message, format_alerts(user_store().get_alerts(update.effective_user.id)))


async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled: compare alerts with live prices during the session."""
    if not get_calendar().is_market_open():
        return
    rows = [row for row in user_store().iter_user_alerts() if row[3].kind == "price" and row[1] is not None]
    if not rows:
        return
    try:
        prices = await asyncio.to_thread(_nepse().live_prices)
    except Exception as exc:
        logger.warning("Could not read live prices for alerts: %s", exc)
        return
    for user_id, chat_id, _index, alert in rows:
        price = prices.get(alert.symbol)
        if price is None or not alert_hit(alert, price):
            continue
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔔 Price alert: {alert.symbol} is at {price:,.1f} ({alert.operator} your level {alert.value:,.1f}).\n"
                     f"Live price during the session. For the plan: /stock {alert.symbol}",
            )
            user_store().clear_alert(user_id, alert)
        except Exception as exc:
            logger.warning("Could not send alert for %s: %s", alert.symbol, exc)


# ---------------------------------------------------------------------- news
def format_news(symbol: str, found: list[Any]) -> str:
    """Fresh, de-duplicated events for one symbol, each with its type and usual price effect."""
    from nepse_bot.events import engine as events

    lines = [f"📰 <b>News: {escape(symbol)}</b>", escape(reports.fetched_news_line()), ""]
    if not found:
        lines.append("Nothing fresh: no announcement or headline in the last few days.")
    for event in found[:8]:
        lines.append(f"{event.mark} {event.day} <b>{escape(event.label)}</b>: {escape(event.title[:140])} ({escape(event.source)})")
        if not event.routine and event.note:
            lines.append(f"    Usual effect: {escape(event.note)}")
    if found:
        impact, sentence = events.net_impact(found)
        lines.extend(["", f"Taken together: <b>{impact}</b> — {escape(sentence)}."])
    lines.extend(["", "Old and repeated items are left out. The usual effects are general market experience, not measured by this bot; news is not part of the score."])
    return "\n".join(lines)


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/news SYMBOL - recent headlines and announcements."""
    global _news_fetcher
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Use: /news SYMBOL, for example /news NABIL")
        return
    symbol = context.args[0].strip().upper()
    note = await message.reply_text(f"Fetching news for {symbol}...")
    try:
        if _news_fetcher is None:
            from nepse_bot.research.news_fetcher import MultiSourceNewsFetcher

            _news_fetcher = MultiSourceNewsFetcher()
        from nepse_bot.events import engine as events

        try:
            items = await asyncio.to_thread(_news_fetcher.fetch_symbol_news, symbol, 8)
        except Exception as exc:  # the portals are optional; company announcements still work
            logger.warning("Headline fetch failed for %s: %s", symbol, exc)
            items = []
        engine = handlers.get_engine()
        rows = engine.db.query("SELECT symbol, title, source, published_at FROM news WHERE symbol = ? ORDER BY published_at DESC LIMIT 30", (symbol,))
        rows += [
            {"symbol": symbol, "title": item.title, "source": item.source or "unknown source", "published_at": item.published_date.strftime("%Y-%m-%d") if item.published_date else ""}
            for item in items
        ]
        found = events.build_events(rows, engine.calendar.now().date().isoformat())
        await handlers._send(message, format_news(symbol, found), edit=note)
    except Exception as exc:
        logger.exception("News failed for %s: %s", symbol, exc)
        await note.edit_text(f"Could not fetch news for {symbol} right now.")


def register(application: Application) -> None:
    """Attach the personal commands to the bot."""
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("news", news_command))
