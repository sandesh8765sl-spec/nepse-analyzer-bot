"""/selftest (owner only): one real sample of every message type, each marked TEST.

Built from the live database with the same code as the real messages, so anything
that breaks or reads badly shows up here first. Nothing is recorded: no alert is
logged, no signal is journalled, no position is changed.

    python -m nepse_bot.bot.selftest          # build everything and send it to the owner
    python -m nepse_bot.bot.selftest --dry    # build only, print the sizes and any failure
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from dataclasses import dataclass
from html import escape
from typing import Any, Callable

from nepse_bot.alerts import samples
from nepse_bot.bot import extras, handlers, manage, reports
from nepse_bot.engine import exits
from nepse_bot.events import engine as events
from nepse_bot.portfolio import positions

logger = logging.getLogger(__name__)
ALERT_NAMES = {
    "strong_buy": "STRONG BUY alert", "buy": "BUY alert", "top_pick": "TOP PICK alert", "early": "EARLY alert",
    "confirmed": "CONFIRMED alert", "cancelled": "CANCELLED alert",
}


@dataclass
class Sample:
    label: str
    text: str = ""
    photo: bytes | None = None
    error: str = ""


def _news_alert(scan: Any) -> str:
    engine = handlers.get_engine()
    today = engine.calendar.now().date().isoformat()
    found = [event for event in events.load_events(engine.db, today) if event.symbol and not event.routine][:2]
    if found:
        lines = [f"📰 {event.symbol}: {event.label} — {event.title[:120]}. Usual effect: {event.note}" for event in found]
    else:
        lines = ["📰 NABIL: Dividend — (sample, no fresh news today) NABIL proposes 10% cash dividend. Usual effect: often rises into the book closure, then adjusts."]
    return "\n".join(["🔔 <b>Alerts for your positions and watchlist</b>", *[escape(line) for line in lines], "", "Details: /positions"])


def _position_alert(scan: Any) -> str:
    views, _, _ = manage.review_all(scan)
    lines: list[str] = []
    if views:
        view = views[0]
        lines += [message for _, message in positions.position_events(view, live_price=round(view.trailing_stop * 0.995, 1))][:1]
        lines += [message for _, message in positions.position_events(view)][:2]
    if not lines:
        lines = ["🔴 NABIL STOP HIT: now trading at 498.0, at or below your trailing stop 500.0. The rule is to sell: place the sell order now or at the next session's open.",
                 "(sample text: you have no open position recorded with /bought)"]
    return "\n".join(["🔔 <b>Your positions — live</b>", *[escape(line) for line in lines], "", "Details: /positions"])


def build_samples(scan: Any) -> list[Sample]:
    """Every message type, in the order the owner asked for. A failure becomes an entry with ``error`` set."""
    engine = handlers.get_engine()
    style = exits.exit_style(engine.db)
    symbol = scan.buys[0].symbol if scan.buys else next(iter(sorted(scan.results)), "NABIL")
    jobs: list[tuple[str, Callable[[], Any]]] = [
        (f"/stock {symbol}", lambda: handlers._stock_report(scan, symbol)[0]),
        ("/top10", lambda: reports.format_top10(scan)[0]),
        ("/buy", lambda: reports.format_buy_page(scan, 0, style=style)[0]),
        ("/positions", lambda: manage.build_positions_text(scan)),
        ("/calendar", lambda: manage.build_calendar_text(scan)),
        (f"/chart {symbol}", lambda: extras.build_chart(scan, symbol)),
        ("/brief", lambda: manage.build_morning_brief(scan)),
        ("/evening", lambda: manage.build_evening_report(scan)),
        ("/report week", lambda: manage.build_weekly_summary(scan)),
        ("news alert", lambda: _news_alert(scan)),
        ("position alert", lambda: _position_alert(scan)),
    ]
    out: list[Sample] = []
    for label, job in jobs:
        try:
            result = job()
            if isinstance(result, tuple):                      # the chart: (picture, caption)
                out.append(Sample(label, text=result[1], photo=result[0]) if result[0] else Sample(label, error=result[1]))
            else:
                out.append(Sample(label, text=result))
        except Exception as exc:
            logger.exception("Self-test: %s failed", label)
            out.append(Sample(label, error=f"{type(exc).__name__}: {exc}"))
    try:
        alerts = samples.build(engine, scan)
        for key, name in ALERT_NAMES.items():
            out.append(Sample(name, text=alerts[key]) if key in alerts else Sample(name, error="no stock qualifies for this sample today"))
    except (Exception, SystemExit) as exc:
        logger.exception("Self-test: alert samples failed")
        out.append(Sample("buy alerts", error=f"{type(exc).__name__}: {exc}"))
    return out


async def deliver(bot: Any, chat_id: Any, items: list[Sample]) -> list[str]:
    """Send every sample. Returns the problems found (build failures, HTML Telegram refused)."""
    problems: list[str] = []
    total = len(items)
    for number, item in enumerate(items, start=1):
        head = f"🔧 <b>TEST {number}/{total} — {escape(item.label)}</b>"
        if item.error:
            problems.append(f"{item.label}: {item.error}")
            await bot.send_message(chat_id=chat_id, text=f"{head}\n❌ could not be built: {escape(item.error[:300])}", parse_mode="HTML")
            continue
        if item.photo is not None:
            await bot.send_photo(chat_id=chat_id, photo=io.BytesIO(item.photo), caption=f"TEST {number}/{total} — {item.label}. {item.text}"[:1000])
            continue
        parts = reports.split_message(f"{head}\n{item.text}")
        for part in parts:
            try:
                await bot.send_message(chat_id=chat_id, text=part, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as exc:
                problems.append(f"{item.label}: Telegram refused the formatting ({exc})")
                await bot.send_message(chat_id=chat_id, text=handlers._strip_tags(part), disable_web_page_preview=True)
        await asyncio.sleep(0.4)                               # stay well under Telegram's rate limit
    summary = (f"🔧 <b>Self-test finished</b>: {total - len({p.split(':')[0] for p in problems})} of {total} message types OK."
               + ("".join(f"\n❌ {escape(problem[:200])}" for problem in problems) if problems else "\nNothing was recorded or changed."))
    await bot.send_message(chat_id=chat_id, text=summary, parse_mode="HTML")
    return problems


async def run(bot: Any, chat_id: Any) -> list[str]:
    scan = await handlers._scan()
    items = await asyncio.to_thread(build_samples, scan)
    return await deliver(bot, chat_id, items)


def main() -> int:
    from dotenv import load_dotenv
    from telegram import Bot

    from nepse_bot.settings import PROJECT_ROOT

    logging.basicConfig(level=logging.WARNING)
    load_dotenv(PROJECT_ROOT / ".env")
    if "--dry" in sys.argv:
        items = build_samples(handlers.get_engine().scan())
        for item in items:
            print(f"{'FAIL' if item.error else 'ok  '} {item.label}: {item.error or str(len(item.text)) + ' characters'}{' + picture' if item.photo else ''}")
        return 1 if any(item.error for item in items) else 0

    async def go() -> list[str]:
        bot = Bot(os.environ["TELEGRAM_TOKEN"])
        async with bot:
            return await run(bot, os.environ["TELEGRAM_CHAT_ID"])

    problems = asyncio.run(go())
    print(f"Self-test sent. Problems: {problems or 'none'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
