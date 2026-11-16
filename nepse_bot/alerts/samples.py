"""One sample of every alert type, built from the latest real scan and clearly marked TEST.

    python -m nepse_bot.alerts.samples          # save the samples to reports/alerts/
    python -m nepse_bot.alerts.samples --send   # also send TOP PICK and the EARLY -> CONFIRMED pair to Telegram

Nothing is recorded: no alert is logged and no signal enters the journal. The
stocks are real, but they did NOT necessarily pass the alert rules - a sample
shows what a message looks like, it is not a recommendation.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import date

from nepse_bot.alerts import rules, service, text
from nepse_bot.engine.engine import Engine
from nepse_bot.engine.plan import PlanRejection
from nepse_bot.settings import PROJECT_ROOT

FOLDER = PROJECT_ROOT / "reports" / "alerts"


def build(engine: Engine | None = None, scan=None) -> dict[str, str]:
    engine = engine or Engine()
    scan = scan or engine.scan()
    fees, db = engine.fees, engine.db
    next_session = engine.calendar.next_trading_day(date.fromisoformat(scan.as_of)).isoformat()
    with_plan = sorted((item for item in scan.results.values() if item.plan or item.blocked_plan), key=lambda item: -item.score)
    if not with_plan:
        raise SystemExit("No stock has a plan in the latest scan; no sample can be built.")
    best = with_plan[0]
    plan = best.plan or best.blocked_plan

    def data(kind: str, item, item_plan, **extra) -> text.AlertInput:
        lines, _ = service.holding_window_events(db, item.symbol, scan.as_of, item.close)
        return text.from_item(kind, item, scan, item_plan, events=lines, test=True, **extra)

    samples = {
        "top_pick": text.format_alert(data("TOP PICK", best, plan, next_session=next_session), fees),
        "buy": text.format_alert(data("BUY", best, plan, next_session=next_session), fees),
        "strong_buy": text.format_alert(data("STRONG BUY", best, plan, next_session=next_session), fees),
    }
    # EARLY -> CONFIRMED / CANCELLED: a watch-list stock, pretending it trades 2% above its trigger
    for item in sorted((entry for entry in scan.by_verdict("WATCH") if entry.trigger_level and entry.trigger_setup), key=lambda entry: -entry.score):
        trigger = float(item.trigger_level)
        live = round(trigger * 1.02, 1)
        name = item.setup_name or service.evidence_setup_name(item.trigger_setup)
        early = service.early_plan(service.load_frame(db, item.symbol, scan.as_of), item.trigger_setup, name, trigger, live, next_session, scan.profile, fees)
        if isinstance(early, PlanRejection):
            continue
        entry = data("EARLY", item, early, live_price=live, trigger=trigger)
        entry.reasons = service.plan_reasons(item, early)
        samples["early"] = text.format_alert(entry, fees)
        confirmed = data("CONFIRMED", item, early, trigger=trigger, next_session=next_session)
        confirmed.reasons = entry.reasons
        samples["confirmed"] = text.format_alert(confirmed, fees, rules.confirm_early(trigger, live, early.stop))
        samples["cancelled"] = text.format_cancelled(item.symbol, item.name, trigger, round(trigger * 0.99, 1), rules.confirm_early(trigger, trigger * 0.99, early.stop), test=True)
        break
    return samples


async def send(messages: list[str]) -> None:
    from dotenv import load_dotenv
    from telegram import Bot

    load_dotenv(PROJECT_ROOT / ".env")
    bot = Bot(os.environ["TELEGRAM_TOKEN"])
    async with bot:
        for message in messages:
            await bot.send_message(chat_id=os.environ["TELEGRAM_CHAT_ID"], text=message, parse_mode="HTML", disable_web_page_preview=True)


def main() -> int:
    samples = build()
    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, message in samples.items():
        (FOLDER / f"sample_{name}.txt").write_text(re.sub(r"</?(b|code|pre|i)>", "", message).replace("&amp;", "&") + "\n", encoding="utf-8")
    print("saved:", ", ".join(f"sample_{name}.txt ({len(message)} chars)" for name, message in samples.items()))
    if "--send" in sys.argv:
        wanted = [samples[name] for name in ("top_pick", "early", "confirmed") if name in samples]
        asyncio.run(send(wanted))
        print(f"sent {len(wanted)} TEST messages to Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
