"""Text of the Stage 5 messages: positions, calendar, morning brief, evening report, weekly summary.

Pure functions of data that is already loaded, so they can be tested without
Telegram. All dynamic text is HTML-escaped.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from html import escape
from typing import Any

from nepse_bot.bot import reports
from nepse_bot.engine import evidence
from nepse_bot.engine.engine import ScanResult
from nepse_bot.events.engine import ClosureItem, Event, event_lines
from nepse_bot.portfolio import journal
from nepse_bot.portfolio.positions import ACTION_ORDER, PositionView
from nepse_bot.trading_calendar import TradingCalendar, get_calendar

ACTION_MARK = {"SELL": "🔴", "TRIM": "🟠", "HOLD": "🟢"}
SHORT_SETUP = {"Relative-strength leader": "RS leader", "Trend pullback": "Pullback", "Base breakout": "Breakout"}


def _ordered(views: list[PositionView]) -> list[PositionView]:
    return sorted(views, key=lambda view: (ACTION_ORDER.get(view.action, 9), view.symbol))


def _total_line(views: list[PositionView]) -> str:
    cost = sum(view.net.buy_amount for view in views)
    net = sum(view.net.net_profit for view in views)
    percent = f" ({100 * net / cost:+.1f}%)" if cost else ""
    return f"{len(views)} open, worth {reports.money(sum(view.value for view in views))}; net <b>Rs {net:+,.0f}</b>{percent} if sold at the last close, after fees and tax"


# ----------------------------------------------------------------- positions
def format_positions(views: list[PositionView], risk_lines: list[str], as_of: str, style: str, missing: list[str] | None = None) -> str:
    """The /positions report: every holding with HOLD / TRIM / SELL and the reasons."""
    if not views and not missing:
        return (
            "💼 You have no open positions.\n\n"
            "Tell me when you buy: /bought NABIL 100 520   (add a date for an older purchase: /bought NABIL 100 520 2026-10-01)\n"
            "and when you sell: /sold NABIL 100 560"
        )
    lines = [f"💼 <b>Your positions</b> — prices at {as_of} close", _total_line(views), ""]
    for view in _ordered(views):
        lines.append(f"{ACTION_MARK[view.action]} <b>{escape(view.symbol)}</b> — <b>{view.action}</b>")
        lines.append(
            f"{view.quantity:,} sh @ {view.avg_price:,.1f} → {view.close:,.1f} | net Rs {view.net.net_profit:+,.0f} ({view.net.net_percent:+.1f}%) | "
            f"held {view.days_held} days ({view.sessions_held} sessions)"
        )
        lines.append("Why: " + escape("; ".join(view.reasons)))
        target = f" • reference target and partial-profit level {view.target:,.1f} ({(view.target / view.close - 1) * 100:+.1f}% from the last close)" if view.target else ""
        lines.append(f"Trailing stop {view.trailing_stop:,.1f} (highest since you bought {view.highest:,.1f}){target}")
        lines.extend(f"• {escape(note)}" for note in view.notes)
        lines.append("")
    for symbol in missing or []:
        lines.append(f"<b>{escape(symbol)}</b>: no price in the database, so it cannot be judged.")
    if risk_lines:
        lines.append("<b>Portfolio risk</b>")
        lines.extend(f"⚠️ {escape(text)}" for text in risk_lines)
    elif views:
        lines.append("Portfolio risk: within your rules (positions, sector share, total risk).")
    lines.extend(
        [
            "",
            f"Exit style: {style} (/settings exit). SELL = act at the next session; TRIM = reduce; HOLD = keep, and move your stop-loss order up to the trailing stop.",
            reports.DISCLAIMER,
        ]
    )
    return "\n".join(lines)


def format_sale(symbol: str, price: float, sale: dict[str, Any]) -> str:
    """Confirmation of /sold with the realised result."""
    result = sale["result"]
    lines = [
        f"✅ Sold {sale['quantity']:,} {escape(symbol)} at {price:,.1f} (held {sale['held_days']} days{': purchase date unknown, short-term tax assumed' if sale.get('date_unknown') else ''}).",
        f"Gross {result.gross_profit:+,.0f} • fees {result.costs:,.0f} • tax {result.tax:,.0f} ({sale['tax_percent']:g}%)",
        f"<b>Net result: Rs {result.net_profit:+,.0f} ({result.net_percent:+.1f}%)</b>",
    ]
    lines.append(f"{sale['remaining']:,.0f} shares still open." if sale["remaining"] > 0 else "Position closed.")
    return "\n".join(lines)


# ------------------------------------------------------------------ calendar
HOLDER_NAMES = {"Promoter": "promoter shares", "MutualFund": "mutual fund shares"}


def closure_line(item: ClosureItem, held: bool = False, price: float | None = None) -> str:
    mine = " (you hold it)" if held else ""
    text = f"• {item.day} <b>{escape(item.symbol)}</b>{mine}: {escape(item.payout)} — in {item.trading_days_left} trading day(s); last day to buy or still hold for it: {item.last_buy_day}"
    after = item.price_after(price) if price else None
    return text + (f"; expected price after: about {after:,.0f} (last close {price:,.0f})" if after else "")


def format_calendar(closures: list[ClosureItem], held: set[str], holidays: list[tuple[str, str]], meetings: list[Any], today: str,
                    pending: list[Any] | None = None, prices: dict[str, float] | None = None, unlocks: list[Any] | None = None) -> str:
    """The /calendar report. ``pending`` and ``meetings`` are CorporateEvent items without a book closure date."""
    prices = prices or {}
    lines = [f"🗓 <b>Calendar</b> — from {today}", ""]
    mine = [item for item in closures if item.symbol in held]
    if mine:
        lines.append("<b>Your holdings</b>")
        lines.extend(closure_line(item, True, prices.get(item.symbol)) for item in mine)
        lines.append("")
    lines.append("<b>Book closures, next 30 days</b>")
    others = [item for item in closures if item.symbol not in held]
    lines.extend(closure_line(item, False, prices.get(item.symbol)) for item in others[:25])
    if len(others) > 25:
        lines.append(f"… and {len(others) - 25} more.")
    if not closures:
        lines.append("None in the database.")
    if pending:
        ordered = sorted(pending, key=lambda item: (item.symbol not in held, item.kind != "right", item.announced or ""), reverse=False)
        lines.extend(["", "<b>Announced, book closure date not fixed yet</b> (right shares first; kept until it happens)"])
        lines.extend(f"• {escape(item.line())}{' (you hold it)' if item.symbol in held else ''}" for item in ordered[:12])
        if len(ordered) > 12:
            lines.append(f"… and {len(ordered) - 12} more; /stock SYMBOL shows a stock's own events.")
    if meetings:
        lines.extend(["", "<b>Meetings announced recently</b>"])
        lines.extend(f"• {escape(item.line())}" for item in meetings[:8])
    if unlocks:
        ordered_unlocks = sorted(unlocks, key=lambda item: (item.symbol not in held, item.day, item.symbol))
        lines.extend(["", "<b>Lock-in expiry</b> (locked promoter / mutual fund shares become free to sell; extra supply can weigh on the price)"])
        lines.extend(
            f"• {item.day} <b>{escape(item.symbol)}</b>{' (you hold it)' if item.symbol in held else ''}{' ⚠️' if item.flagged else ''}: "
            f"{escape(HOLDER_NAMES.get(item.holder, item.holder))}, {escape(item.size_text)} — {item.when_text}"
            for item in ordered_unlocks[:20]
        )
        if len(ordered_unlocks) > 20:
            lines.append(f"… and {len(ordered_unlocks) - 20} more; /stock SYMBOL shows a stock's own dates.")
    lines.extend(["", "<b>Market holidays, next 30 days</b>"])
    lines.extend([f"• {day}: {escape(reason or 'holiday')}" for day, reason in holidays] or ["None listed (/holiday to add one)."])
    lines.extend(["", "A share bought ON the book closure date does not get the dividend or bonus. The bot gives no new plan within 5 trading days before one."])
    return "\n".join(lines)


# ------------------------------------------------------------------- briefs
def _market_line(scan: ScanResult, index_move: tuple[float, float] | None) -> str:
    regime = scan.regime
    parts = [f"Market: <b>{regime.label}</b>"]
    if index_move:
        parts.append(f"NEPSE {index_move[0]:,.1f} ({index_move[1]:+.1f}%)")
    if regime.pct_above_ma50 is not None:
        parts.append(f"{regime.pct_above_ma50:.0f}% of stocks above their 50-day average")
    line = " • ".join(parts)
    return line + ("\n⚠️ Overheated: new buys did worst in this state in 5 of 7 years." if regime.overheated else "")


def _position_lines(views: list[PositionView], risk_lines: list[str], with_today: bool = False) -> list[str]:
    if not views:
        return ["<b>Your positions</b>", "None open. Record a purchase with /bought SYMBOL QTY PRICE."]
    lines = ["<b>Your positions</b>", _total_line(views)]
    if with_today:
        lines.append(f"Change on the last session: Rs {sum(view.change_today for view in views):+,.0f}")
    quiet = []
    for view in _ordered(views):
        if view.action == "HOLD":
            quiet.append(f"{escape(view.symbol)} (stop {view.trailing_stop:,.1f})")
        else:
            lines.append(f"{ACTION_MARK[view.action]} <b>{escape(view.symbol)} {view.action}</b> — {escape(view.reasons[0])}")
    if quiet:
        lines.append(f"🟢 HOLD: {', '.join(quiet)}")
    lines.extend(f"⚠️ {escape(text)}" for text in risk_lines)
    return lines


def _candidate_lines(scan: ScanResult, limit: int = 3) -> list[str]:
    buys = scan.buys[:limit]
    if not buys:
        return ["<b>Candidates</b>", "No stock has a valid entry setup. See /watch."]
    lines = [f"<b>Candidates</b> (top {len(buys)} of {len(scan.buys)}; /buy for the plans)"]
    for index, item in enumerate(buys, start=1):
        plan = item.plan
        setup = SHORT_SETUP.get(item.setup_name or "", item.setup_name or "")
        lines.append(
            f"{index}. <b>{escape(item.symbol)}</b> {item.score:.0f} {item.verdict} {escape(setup)} — buy {reports.price(plan.entry_low)}-{reports.price(plan.entry_high)}, "
            f"stop {reports.price(plan.stop)}, ref target {reports.price(plan.target_1)} (+{plan.target_1_percent:.0f}%)"
        )
    if not evidence.any_proven():
        lines.append("All are SPECULATIVE: no setup made money in the backtest over the last 2 years. A ranked research list, not buy signals.")
    return lines


def _event_block(events: list[Event], focus: set[str], title: str, limit: int = 6) -> list[str]:
    first = [event for event in events if event.symbol in focus]
    rest = [event for event in events if event.symbol not in focus]
    lines = event_lines(first, limit) + event_lines(rest, max(0, limit - len({event.symbol for event in first})))
    return [title, *[escape(line) for line in lines]] if lines else []


def watchlist_lines(scan: ScanResult, symbols: list[str]) -> list[str]:
    """The personal watchlist: one line per stock with the engine's view of it."""
    lines = ["<b>Your watchlist</b>"]
    for symbol in symbols:
        item = scan.results.get(symbol)
        if item is None:
            lines.append(f"• <b>{escape(symbol)}</b>: no recent prices in the database")
            continue
        change = f" ({item.change_percent:+.1f}%)" if item.change_percent is not None else ""
        lines.append(f"• <b>{escape(symbol)}</b>  {item.close:,.1f}{change}  {item.verdict}  score {item.score:.0f}")
        lines.extend(f"    {escape(note)}" for note in item.notes)
        if item.verdict == "WATCH" and item.watch_trigger:
            lines.append(f"    {escape(item.watch_trigger[:140])}")
    return lines


def format_morning_brief(scan: ScanResult, views: list[PositionView], risk_lines: list[str], events: list[Event], closures: list[ClosureItem],
                         index_move: tuple[float, float] | None, calendar: TradingCalendar | None = None,
                         watchlist: list[str] | None = None) -> str:
    """The message sent before the market opens."""
    calendar = calendar or get_calendar()
    today = calendar.now().date()
    held = {view.symbol for view in views}
    focus = held | {item.symbol for item in scan.buys[:10]} | set(watchlist or [])
    lines = [f"🌅 <b>Morning brief</b> — {today.strftime('%a %Y-%m-%d')}", escape(reports.freshness_line(scan, calendar)), _market_line(scan, index_move), ""]
    lines.extend(_position_lines(views, risk_lines))
    lines.extend(["", *_candidate_lines(scan)])
    if watchlist:
        lines.extend(["", *watchlist_lines(scan, watchlist)])
    block = _event_block(events, focus, "<b>Fresh events</b> (your stocks, watchlist and candidates first)")
    if block:
        lines.extend(["", *block])
    soon = [item for item in closures if item.trading_days_left <= 7 and (item.symbol in focus or item.trading_days_left <= 3)]
    if soon:
        lines.extend(["", "<b>Book closures this week</b>"])
        lines.extend(closure_line(item, item.symbol in held) for item in soon[:6])
    lines.extend(["", "More: /positions /top10 /calendar", reports.DISCLAIMER])
    return "\n".join(lines)


def format_evening_report(scan: ScanResult, views: list[PositionView], risk_lines: list[str], events: list[Event],
                          index_move: tuple[float, float] | None, breadth: dict[str, Any] | None, calendar: TradingCalendar | None = None,
                          position_alerts: list[str] | None = None) -> str:
    """The message sent after the evening data update."""
    lines = [f"🌇 <b>Evening report</b> — {scan.as_of} close", escape(reports.freshness_line(scan, calendar)), _market_line(scan, index_move)]
    if breadth and breadth.get("advancers") is not None:
        turnover = f" • turnover Rs {breadth['turnover'] / 1e7:,.0f} crore" if breadth.get("turnover") else ""
        lines.append(f"{breadth['advancers']} stocks up, {breadth['decliners']} down{turnover}")
    lines.extend(["", *_position_lines(views, risk_lines, with_today=True)])
    if position_alerts:
        lines.extend(["", "<b>Alerts for your positions</b>", *[escape(text) for text in position_alerts]])
    lines.extend(["", *_candidate_lines(scan)])
    lines.append(f"{len(scan.watches)} more on the watch list (/watch).")
    today_events = [event for event in events if event.day == scan.as_of]
    block = _event_block(today_events, {view.symbol for view in views}, "<b>Announced today</b>")
    if block:
        lines.extend(["", *block])
    lines.extend(["", "More: /positions /buy /report", reports.DISCLAIMER])
    return "\n".join(lines)


# -------------------------------------------------------------------- weekly
def weekly_data(db: Any, today: str) -> dict[str, Any]:
    """Numbers for the weekly summary: the 7 days ending ``today``."""
    start = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    index = db.query("SELECT date, close FROM index_daily WHERE index_code = 'NEPSE' AND date >= ? AND date <= ? ORDER BY date", ((date.fromisoformat(start) - timedelta(days=7)).isoformat(), today))
    before = [row for row in index if row["date"] < start]
    during = [row for row in index if row["date"] >= start]
    new_by_setup: dict[str, int] = {}
    for row in db.query("SELECT setup, COUNT(*) AS n FROM signals WHERE substr(id, 1, 10) >= ? AND substr(id, 1, 10) <= ? GROUP BY setup", (start, today)):
        new_by_setup[row["setup"] or "unknown"] = row["n"]
    finished = []
    for row in db.query("SELECT status, plan_json FROM signals WHERE closed_at >= ? AND closed_at <= ?", (start, today)):
        if row["status"] in journal.FINISHED:
            try:
                finished.append(float(json.loads(row["plan_json"])["outcome"]["net_profit"]))
            except (KeyError, TypeError, ValueError):
                continue
    return {
        "start": start, "end": today,
        "index_start": float((before[-1] if before else during[0])["close"]) if (before or during) else None,
        "index_end": float(during[-1]["close"]) if during else None,
        "new_by_setup": new_by_setup, "finished": finished, "all_time": journal.live_results(db),
    }


def format_weekly(data: dict[str, Any], sales: list[dict[str, Any]], views: list[PositionView], closures: list[ClosureItem], holidays: list[tuple[str, str]]) -> str:
    """The weekly summary (/report week, and sent automatically at the end of the week)."""
    lines = [f"🗓 <b>Weekly summary</b> — {data['start']} to {data['end']}", ""]
    if data.get("index_start") and data.get("index_end"):
        change = 100 * (data["index_end"] / data["index_start"] - 1)
        lines.append(f"NEPSE: {data['index_start']:,.1f} → {data['index_end']:,.1f} ({change:+.1f}%)")
    lines.extend(["", "<b>The bot's signals</b> (live journal, after fees and tax)"])
    new = data["new_by_setup"]
    detail = ", ".join(f"{reports.SETUP_LABELS.get(setup, setup)} {count}" for setup, count in sorted(new.items(), key=lambda pair: -pair[1]))
    lines.append(f"• New candidates logged: {sum(new.values())}" + (f" ({escape(detail)})" if detail else ""))
    finished = data["finished"]
    if finished:
        wins = sum(1 for value in finished if value > 0)
        lines.append(f"• Finished this week: {len(finished)} — {wins} won, {len(finished) - wins} lost, net Rs {sum(finished):+,.0f}")
    else:
        lines.append("• Finished this week: none")
    done = sum(row["finished"] for row in data["all_time"])
    if done:
        total = sum(row["net_profit"] for row in data["all_time"])
        wins = sum(row["wins"] for row in data["all_time"])
        lines.append(f"• Since the journal began: {done} finished, {wins} won, net Rs {total:+,.0f} (per setup: /report)")
    lines.extend(["", "<b>Your trades</b>"])
    if sales:
        for sale in sales:
            lines.append(f"• Sold {sale['quantity']:,.0f} {escape(sale['symbol'])} at {sale['price']:,.1f} on {sale['trade_date']}: Rs {sale['realised']:+,.0f}")
        lines.append(f"Realised this week: <b>Rs {sum(sale['realised'] for sale in sales):+,.0f}</b> after fees and tax")
    else:
        lines.append("• No sales recorded this week.")
    if views:
        lines.append("• " + _total_line(views))
        acting = [f"{view.symbol} {view.action}" for view in _ordered(views) if view.action != "HOLD"]
        if acting:
            lines.append("• Needs attention: " + escape(", ".join(acting)) + " (/positions)")
    else:
        lines.append("• No open positions.")
    held = {view.symbol for view in views}
    upcoming = [item for item in closures if item.symbol in held or item.trading_days_left <= 5]
    if upcoming or holidays:
        lines.extend(["", "<b>Next week</b>"])
        lines.extend(closure_line(item, item.symbol in held) for item in upcoming[:6])
        lines.extend(f"• Market closed {day}: {escape(reason or 'holiday')}" for day, reason in holidays)
    lines.extend(["", reports.DISCLAIMER])
    return "\n".join(lines)
