"""Telegram commands that use the analysis engine.

Every command here answers from the bot's own database through the one
engine. Nothing in this module scrapes a website to produce a verdict.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from nepse_bot.bot import reports
from nepse_bot.engine.config import PROFILE_KEYS, load_profile
from nepse_bot.engine import exits
from nepse_bot.engine.engine import Engine, ScanResult
from nepse_bot.portfolio import journal

logger = logging.getLogger(__name__)

_engine: Engine | None = None

SETTING_ALIASES: dict[str, str] = {
    "capital": "total_capital", "total": "total_capital", "total_capital": "total_capital",
    "min": "min_position", "min_position": "min_position",
    "max": "max_position", "max_position": "max_position",
    "positions": "max_open_positions", "max_open_positions": "max_open_positions",
    "risk": "risk_percent", "risk_percent": "risk_percent",
    "riskrs": "risk_rupees", "risk_rupees": "risk_rupees",
}

HELP_TEXT = """<b>NEPSE Analyzer Bot</b>

<b>Analysis</b> (from the bot's own database, last completed session)
/stock NABIL — full analysis and trade plan
/top10 — best candidates today
/buy — today's candidates with full plans and their backtest record
/watch — good stocks and the price to wait for
/market — market regime, breadth and sectors
/sector hydro — one sector's stocks ranked
/chart NABIL — price chart with EMA 20/50/200, volume, stop and reference target
/watchlist — your own list (/watchlist add NABIL, /watchlist remove NABIL); included in the morning brief and news alerts

<b>Your positions</b>
/bought NABIL 100 520 — record a purchase (older one: /bought NABIL 100 520 2025-03-12)
/sold NABIL 100 560 — record a sale; shows the result after fees and tax
/positions — your holdings with HOLD / TRIM / SELL, trailing stop and net profit
/settings — capital, risk, position size, exit style and alerts (/settings alerts)
/alert NABIL above 600 — price alert
/alerts — list your alerts

<b>Daily messages</b> (sent automatically; also on request)
/brief — morning brief
/evening — evening report
/report — how the bot's past signals turned out; /report week for the weekly summary
/calendar — book closures and holidays ahead

<b>Automatic alerts</b> (/settings alerts to switch on or off)
Buy alerts with a full plan, an early alert during the session, at most one TOP PICK a day, and alerts for your holdings (stop, target, time limit, events).

<b>Plain questions also work</b>
"why is NABIL falling", "news NABIL", "what should I watch today", "how is the market"

<b>Other</b>
/news NABIL — fresh news and announcements, with their usual price effect
/status — bot and data health
/holiday — market holidays
/dataupdate — refresh the database now
/retest — monthly re-test of the setups: suggestions only, /retest apply to accept
/selftest — (owner only) sends one sample of every message type, marked TEST

The bot only advises. It never places orders."""


def get_engine() -> Engine:
    """Return the shared engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


async def _scan() -> ScanResult:
    return await asyncio.to_thread(get_engine().scan)


def _strip_tags(text: str) -> str:
    return re.sub(r"</?(b|pre|i|code)>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def _send(message: Any, text: str, keyboard: InlineKeyboardMarkup | None = None, edit: Any | None = None) -> None:
    """Send (or edit into) an HTML message; fall back to plain text if Telegram rejects it."""
    parts = reports.split_message(text)
    for index, part in enumerate(parts):
        markup = keyboard if index == len(parts) - 1 else None
        try:
            if edit is not None and index == 0:
                await edit.edit_text(part, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            else:
                await message.reply_text(part, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except TelegramError as exc:
            logger.warning("HTML message rejected (%s); sending plain text.", exc)
            await message.reply_text(_strip_tags(part), reply_markup=markup, disable_web_page_preview=True)


def _symbol_keyboard(symbols: list[str], per_row: int = 5) -> InlineKeyboardMarkup | None:
    if not symbols:
        return None
    buttons = [InlineKeyboardButton(symbol, callback_data=f"st:{symbol}") for symbol in symbols]
    return InlineKeyboardMarkup([buttons[i : i + per_row] for i in range(0, len(buttons), per_row)])


async def _run(update: Update, working: str, build: Any) -> None:
    """Shared wrapper: show a 'working' note, run the scan, send the report."""
    message = update.effective_message
    if message is None:
        return
    note = await message.reply_text(working)
    try:
        scan = await _scan()
        text, keyboard = build(scan)
        await _send(message, text, keyboard, edit=note)
    except Exception as exc:
        logger.exception("Engine command failed: %s", exc)
        await note.edit_text(f"Sorry, that failed: {exc}")


# ------------------------------------------------------------------ commands
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Short help with examples."""
    del context
    if update.effective_message is not None:
        await _send(update.effective_message, HELP_TEXT)


def _stock_report(scan: ScanResult, symbol: str) -> tuple[str, InlineKeyboardMarkup | None]:
    item = scan.results.get(symbol)
    if item is None:
        known = get_engine().db.query("SELECT symbol, status, instrument_type FROM securities WHERE symbol = ?", (symbol,))
        if known:
            return (
                f"<b>{symbol}</b> is listed ({known[0]['instrument_type']}, {known[0]['status']}) but has no recent "
                "price history in the database, so it cannot be analysed.",
                None,
            )
        close = [row["symbol"] for row in get_engine().db.query("SELECT symbol FROM securities WHERE symbol LIKE ? ORDER BY symbol LIMIT 8", (symbol[:3] + "%",))]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"I don't know the symbol <b>{symbol}</b>.{hint}", _symbol_keyboard(close)
    from nepse_bot.bot import manage

    try:
        risk_notes = manage.new_buy_notes(scan, symbol) if item.is_buy else []
    except Exception as exc:  # the risk remarks must never break the analysis
        logger.warning("Portfolio-risk remarks failed for %s: %s", symbol, exc)
        risk_notes = []
    try:
        corporate = manage.corporate_lines(scan, symbol)
    except Exception as exc:  # the events list must never break the analysis
        logger.warning("Corporate events failed for %s: %s", symbol, exc)
        corporate = []
    return reports.format_stock(item, scan, style=exits.exit_style(get_engine().db), risk_notes=risk_notes, corporate=corporate), None


async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stock SYMBOL (also /analyze, /analze, /plan)."""
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Tell me which stock. Example: /stock NABIL")
        return
    symbol = re.sub(r"[^A-Za-z0-9]", "", context.args[0]).upper()
    await _run(update, f"Analysing {symbol}...", lambda scan: _stock_report(scan, symbol))


async def top10_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/top10 - the best candidates today."""
    del context

    def build(scan: ScanResult) -> tuple[str, InlineKeyboardMarkup | None]:
        text, symbols = reports.format_top10(scan)
        return text, _symbol_keyboard(symbols)

    await _run(update, "Ranking the market...", build)


def _buy_page(scan: ScanResult, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    text, pages = reports.format_buy_page(scan, page, style=exits.exit_style(get_engine().db))
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀ Previous", callback_data=f"buy:{page - 1}"))
    if page < pages - 1:
        buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"buy:{page + 1}"))
    return text, InlineKeyboardMarkup([buttons]) if buttons else None


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/buy - all current BUY signals with full plans."""
    del context
    await _run(update, "Collecting BUY signals...", lambda scan: _buy_page(scan, 0))


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watch - good stocks with the trigger to wait for."""
    del context

    def build(scan: ScanResult) -> tuple[str, InlineKeyboardMarkup | None]:
        text, symbols = reports.format_watch(scan)
        return text, _symbol_keyboard(symbols)

    await _run(update, "Building the watch list...", build)


async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/market - regime, breadth, sectors."""
    del context
    await _run(update, "Reading the market...", lambda scan: (reports.format_market(scan), None))


async def sector_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sector [NAME]."""
    name = " ".join(context.args or [])

    def build(scan: ScanResult) -> tuple[str, InlineKeyboardMarkup | None]:
        sector = reports.match_sector(scan, name) if name else None
        if name and sector is None:
            return f"I don't know a sector called '{name}'.\n\n" + reports.format_sector(scan, None)[0], None
        text, symbols = reports.format_sector(scan, sector)
        return text, _symbol_keyboard(symbols)

    await _run(update, "Ranking sectors...", build)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings - view or change capital and risk settings."""
    message = update.effective_message
    if message is None:
        return
    db = get_engine().db
    args = list(context.args or [])
    if args and args[0].lower() in {"alerts", "alert"}:
        from nepse_bot.alerts import config as alert_config
        from nepse_bot.alerts import text as alert_text

        names = {"strong": "strong", "buy": "strong", "early": "early", "toppick": "toppick", "top": "toppick", "top_pick": "toppick"}
        if len(args) >= 3 and names.get(args[1].lower()) and args[2].lower() in {"on", "off"}:
            alert_config.set_enabled(db, names[args[1].lower()], args[2].lower() == "on")
        elif len(args) >= 2:
            await message.reply_text("Use: /settings alerts strong on|off, /settings alerts early on|off, /settings alerts toppick on|off")
            return
        await _send(message, alert_text.format_alert_settings(db))
        return
    if args and args[0].lower() == "exit":
        choice = args[1].lower() if len(args) >= 2 else ""
        if choice in exits.STYLES:
            db.set_setting(exits.SETTING_KEY, choice)
        elif choice:
            await message.reply_text("Choose one of: /settings exit trailing, /settings exit target, /settings exit hybrid")
            return
        await _send(message, reports.format_exit_help(exits.exit_style(db)))
        return
    if len(args) >= 2:
        key = SETTING_ALIASES.get(args[0].lower())
        raw = args[1].lower().replace(",", "").replace("rs", "").replace("%", "")
        if key is None:
            await message.reply_text("I don't know that setting. Send /settings to see the list.")
            return
        if key == "risk_rupees" and raw in {"off", "none", "0"}:
            db.set_setting(f"profile.{key}", "none")
        else:
            try:
                value = float(raw)
            except ValueError:
                await message.reply_text("Please give a number, for example: /settings capital 600000")
                return
            limits = {"risk_percent": (0.1, 10), "max_open_positions": (1, 20)}
            low, high = limits.get(key, (1000, 1e10))
            if not low <= value <= high:
                await message.reply_text(f"{PROFILE_KEYS[key]} must be between {low:g} and {high:g}.")
                return
            db.set_setting(f"profile.{key}", str(value))
        profile = load_profile(db)
        if profile.min_position > profile.max_position:
            await message.reply_text("Note: your smallest position is now larger than your largest. Please fix one of them.")
    await _send(message, reports.format_settings(load_profile(db), exits.exit_style(db)))


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report - live results per setup; '/report enable <setup>' switches an auto-disabled setup back on."""
    message = update.effective_message
    if message is None:
        return
    db = get_engine().db
    args = [item.lower() for item in (context.args or [])]
    if args and args[0] in {"week", "weekly"}:
        from nepse_bot.bot import manage

        await _run(update, "Preparing the weekly summary...", lambda scan: (manage.build_weekly_summary(scan), None))
        return
    if len(args) >= 2 and args[0] == "enable":
        if await asyncio.to_thread(journal.enable_setup, db, args[1]):
            await message.reply_text(f"Setup '{args[1]}' is switched back on.")
        else:
            await message.reply_text(f"'{args[1]}' was not switched off by the journal. Send /report to see the list.")
    try:
        await asyncio.to_thread(journal.update_journal, db)
        rows = await asyncio.to_thread(journal.live_results, db)
        await _send(message, reports.format_live_report(rows, journal.disabled_setups(db)))
    except Exception as exc:
        logger.exception("/report failed: %s", exc)
        await message.reply_text(f"Sorry, the report failed: {exc}")


async def engine_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline buttons: open a stock, or turn a page of /buy."""
    del context
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    kind, _, value = (query.data or "").partition(":")
    try:
        scan = await _scan()
        if kind == "st":
            text, keyboard = _stock_report(scan, value)
            await _send(query.message, text, keyboard)
        elif kind == "buy":
            text, keyboard = _buy_page(scan, int(value or 0))
            await _send(query.message, text, keyboard, edit=query.message)
    except Exception as exc:
        logger.exception("Engine button failed: %s", exc)
        await query.message.reply_text(f"Sorry, that failed: {exc}")


def refresh_after_update() -> str:
    """Re-run the scan on fresh data and log BUY signals. Called after each data update."""
    engine = get_engine()
    scan = engine.scan(use_cache=False)
    logged = engine.record_signals(scan)
    try:
        journal_line = journal.run_daily(engine.db)
    except Exception as exc:  # the journal must never break the data update
        logger.exception("Journal update failed: %s", exc)
        journal_line = f"journal failed: {exc}"
    return f"{len(scan.buys)} candidates with a plan, {len(scan.watches)} watch ({logged} logged in the journal); market {scan.regime.label}; {journal_line}"


def register(application: Application) -> None:
    """Attach the engine commands to the bot."""
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("start", help_command))
    for name in ("stock", "analyze", "analze", "plan"):
        application.add_handler(CommandHandler(name, stock_command))
    application.add_handler(CommandHandler("top10", top10_command))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("market", market_command))
    application.add_handler(CommandHandler("sector", sector_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CallbackQueryHandler(engine_callback, pattern=r"^(st|buy):"))
