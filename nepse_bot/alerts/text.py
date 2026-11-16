"""The text of every alert. Pure functions of data that is already loaded.

Every alert - EARLY, CONFIRMED, STRONG BUY, BUY or TOP PICK - carries the same
complete trade plan (``plan_lines``): why, entry, position, stop, targets,
holding period, exit rules, events, the setup's record and how to log the buy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Any

from nepse_bot.alerts.config import TYPES, TYPE_TEXT, alert_setting, backtest_results, enabled, holding_stats
from nepse_bot.alerts.rules import Confirmation
from nepse_bot.engine import evidence as evidence_module
from nepse_bot.engine import exits
from nepse_bot.engine.plan import TradePlan, price_text, resistance_warning, round_tick
from nepse_bot.portfolio.fees import FeeSchedule

DISCLAIMER = "Not financial advice. The bot never places an order: you decide and you place it."
HEAD = {
    "EARLY": "⚡ <b>EARLY ALERT — not confirmed until close</b>",
    "CONFIRMED": "✅ <b>CONFIRMED</b>",
    "STRONG BUY": "✅✅ <b>STRONG BUY</b>",
    "BUY": "✅ <b>BUY</b>",
    "TOP PICK": "🧪 <b>TOP PICK (speculative, not proven)</b>",
}


def price(value: float) -> str:
    return f"{value:,.0f}" if value >= 200 or abs(value - round(value)) < 0.05 else f"{value:,.1f}"


def money(value: float) -> str:
    return f"Rs {value:,.0f}"


@dataclass
class AlertInput:
    """Everything one alert message is written from."""

    kind: str                              # EARLY / CONFIRMED / STRONG BUY / BUY / TOP PICK
    symbol: str
    name: str
    sector: str
    sector_label: str | None               # strong / neutral / weak
    market_label: str
    overheated: bool
    score: float
    verdict: str                           # the grade the plan carries (SPECULATIVE / BUY / STRONG BUY)
    setup_name: str
    reasons: list[str]
    plan: TradePlan
    events: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # information-only warnings (resistance close above, lock-in expiry)
    style: str = "trailing"
    live_price: float | None = None        # EARLY only
    trigger: float | None = None           # EARLY / CONFIRMED: the level that was cleared
    next_session: str | None = None        # date of the next trading day (for the AMO line)
    test: bool = False


def from_item(kind: str, item: Any, scan: Any, plan: TradePlan | None = None, **extra: Any) -> AlertInput:
    """Build the input from an engine result."""
    plan = plan or item.plan
    notes = [note for note in getattr(item, "notes", []) if not note.startswith("⚠️ Resistance close above")]
    warning = resistance_warning(plan)
    if warning:
        notes.insert(0, warning)
    extra.setdefault("notes", notes)
    return AlertInput(
        kind=kind, symbol=item.symbol, name=item.name, sector=item.sector,
        sector_label=item.sector_state.label if item.sector_state else None, market_label=scan.regime.label,
        overheated=scan.regime.overheated, score=item.score,
        verdict=item.verdict if item.verdict in ("STRONG BUY", "BUY", "SPECULATIVE") else ("BUY" if evidence_module.for_setup(plan.setup_key).proven else "SPECULATIVE"),
        setup_name=plan.setup_name, reasons=list(item.positives[:3]), plan=plan, **extra,
    )


def trailing_examples(plan: TradePlan) -> list[str]:
    """The trailing rule in plain numbers."""
    distance = plan.chandelier_multiple * plan.atr if plan.atr else plan.entry - plan.stop
    start = round_tick(plan.stop + distance, up=True)
    breakeven = round_tick(plan.breakeven_stop + distance, up=True)
    lines = [
        f"Raise it as the price rises: the stop sits {price_text(distance)} below the highest price since you bought, and never moves down.",
        f"• It starts to move once the price passes {price(start)}.",
    ]
    if breakeven > start:
        lines.append(f"• Once the price reaches {price(breakeven)}, move the stop to {price(plan.breakeven_stop)} (your cost: no loss from there).")
    if plan.target_1 - distance > plan.stop and abs(plan.target_1 - breakeven) / plan.entry > 0.01:
        lines.append(f"• Once the price reaches {price(plan.target_1)}, move the stop to {price(round_tick(plan.target_1 - distance))}.")
    return lines


def plan_lines(data: AlertInput, fees: FeeSchedule) -> list[str]:
    """The complete, specific trade plan that every alert carries."""
    plan = data.plan
    early = data.kind == "EARLY"
    record = evidence_module.for_setup(plan.setup_key)
    targets = exits.targets_for(plan, fees)
    lots = fees.round_lot(plan.quantity * float(alert_setting("early", "first_part_fraction", default=0.5))) or plan.quantity
    sector = f"{data.sector} ({data.sector_label})" if data.sector_label else data.sector
    market = data.market_label + (", overheated" if data.overheated else "")
    lines = [f"<b>Why {escape(data.symbol)} now</b>"]
    lines.extend(f"• {escape(reason)}" for reason in data.reasons[:3])
    lines.append(f"Score {data.score:.0f}/100 • Setup: {escape(data.setup_name)} • Sector: {escape(sector)} • Market: {escape(market)}")

    # ---- entry
    lines.extend(["", "<b>Entry</b>"])
    lines.append(f"• Buy range: {price(plan.entry_low)}–{price(plan.entry_high)}")
    if early:
        lines.append(f"• Buy now up to {price(plan.entry_high)} (live price {price(data.live_price or plan.entry)}). Don't buy above {price(plan.entry_high)}.")
        lines.append(f"• Buy only a part now: {lots:,} of the {plan.quantity:,} shares. Buy the rest only after tonight's CONFIRMED message.")
        lines.append(f"• If the price runs above {price(plan.entry_high)} before you buy: skip it today and wait for tonight's message.")
    else:
        when = f" for {data.next_session}" if data.next_session else ""
        lines.append(f"• After-market order (AMO) tonight{when}: limit price {price(plan.amo_limit)}")
        lines.append(f"• Don't buy above {price(plan.entry_high)}.")
        lines.append(
            f"• If it opens above {price(plan.entry_high)}: do not chase. Wait for a pullback into {price(plan.entry)}–{price(plan.entry_high)}; "
            "if it has not come back within 2 sessions, skip it."
        )

    # ---- position
    lines.extend(["", "<b>Position</b> (your settings, 10-share lots)"])
    lines.append(f"• {plan.quantity:,} shares = {money(plan.position_value)} invested")
    lines.append(f"• At risk if the stop is hit: {money(plan.risk_rupees)} (after fees)")

    # ---- stop
    lines.extend(["", "<b>Stop-loss</b>"])
    lines.append(f"• {price(plan.stop)} (-{plan.stop_percent:.1f}%): {escape(plan.stop_reason)}, so normal daily movement does not hit it.")
    lines.extend(escape(text) for text in trailing_examples(plan))

    # ---- targets
    lines.extend(["", "<b>Targets</b> (net = after fees and tax, whole position)"])
    lines.append(f"• Reference target {price(targets.resistance.price)} ({targets.resistance.gain_pct:+.1f}%) → net {money(targets.resistance.net_rupees)} — {escape(targets.resistance.name)}")
    if targets.high_52w is not None:
        lines.append(f"• 52-week high {price(targets.high_52w.price)} ({targets.high_52w.gain_pct:+.1f}%) → net {money(targets.high_52w.net_rupees)}")
    partial = targets.partial
    lines.append(f"• Partial profit: at {price(partial.price)} sell {targets.partial_quantity:,} shares (net about {money(partial.net_rupees)}), trail the rest.")
    if targets.stats is not None:
        lines.append(
            f"• Typical outcome (backtest {targets.stats.period}, {targets.stats.count} winning trades): the median winner gained "
            f"+{targets.stats.median_pct:g}%, the top quarter +{targets.stats.top_quarter_pct:g}% or more. Most trades of this setup were NOT winners: see the record."
        )

    # ---- holding period
    hold = holding_stats(plan.setup_key)
    limit = plan.max_hold_sessions or 40
    lines.extend(["", "<b>Expected holding period</b>"])
    if hold:
        lines.append(
            f"• Backtest: half of the trades ended within {hold['median']} trading days; typical range {hold['p25']}–{hold['p75']} days"
            + (f"; winners were held about {hold['winners_median']} days." if hold.get("winners_median") else ".")
        )
    lines.append(f"• Time limit: {limit} trading days (about {limit // 5} weeks).")

    # ---- exit rules
    lines.extend(["", "<b>Exit rules</b>"])
    lines.append("• Sell everything: (1) the stop or trailing stop is hit; (2) the trend breaks - /positions says SELL;")
    lines.append(f"  (3) after {limit} trading days if it is not in profit; (4) a negative event (regulatory action, bad results, operator warning).")
    if data.style == "target":
        lines.append(f"• Your exit style is 'target': sell everything at {price(targets.resistance.price)}.")
    else:
        lines.append(f"• Take partial profit at {price(partial.price)} (half), then follow the rest with the trailing stop.")

    # ---- events
    lines.extend(["", "<b>Events in the holding window</b>"])
    lines.extend([f"• {escape(text)}" for text in data.events[:4]] or ["• None known (book closures, right shares, bonus, AGM)."])

    # ---- record and warnings
    lines.extend(["", f"<b>This setup's honest record</b> ({escape(plan.setup_name)}, backtest after fees and tax)"])
    lines.extend([f"• {escape(text)}" for text in record.lines()] or ["• No backtest record."])
    if data.kind == "TOP PICK":
        lines.append(f"• {escape(top_pick_record())}")
    if early:
        lines.append("⚠️ EARLY: the day can still reverse, and early alerts have no backtest. Only confirmed at the close.")
    if data.verdict == "SPECULATIVE":
        lines.append("⚠️ SPECULATIVE: this setup has NOT made money after costs over the last 2 years. Risk only what the plan says.")

    # ---- log it
    buy_quantity = lots if early else plan.quantity
    buy_price = data.live_price if early and data.live_price else plan.entry
    lines.extend(["", f"If you buy, tell me: <code>/bought {escape(data.symbol)} {buy_quantity} {buy_price:g}</code> (use your real quantity and price)"])
    return lines


def top_pick_record() -> str:
    """How the TOP PICK filter itself did in the backtest."""
    results = (backtest_results().get("alerts") or {}).get("toppick") or {}
    recent, earlier = results.get("recent") or {}, results.get("earlier") or {}
    if not recent.get("finished"):
        return "The TOP PICK filter itself: too few cases in the backtest to judge."
    text = (f"The TOP PICK filter itself, last 2 years: {recent['alerts']} picks, {recent['finished']} finished, "
            f"{recent['win_rate']:.0f}% won, average result {recent['expectancy_pct']:+.1f}% per trade")
    text += f"; {earlier.get('alerts', 0)} picks in the earlier years." if not earlier.get("finished") else f"; earlier years {earlier['expectancy_pct']:+.1f}% per trade."
    return text + " Too few cases to prove anything."


def format_alert(data: AlertInput, fees: FeeSchedule, confirmation: Confirmation | None = None) -> str:
    """One complete alert message (HTML)."""
    test = "🔧 <b>TEST — sample only, not a real alert</b>\n" if data.test else ""
    head = HEAD.get(data.kind, f"<b>{escape(data.kind)}</b>")
    if data.kind == "CONFIRMED" and data.verdict == "SPECULATIVE":
        head += " (speculative setup)"
    title = f"{test}{head}\n<b>{escape(data.symbol)}</b> — {escape(data.name)}"
    lines = [title]
    if data.kind == "EARLY" and data.trigger:
        lines.append(f"Holding above its trigger {price(data.trigger)} for 30+ minutes on heavy volume; now {price(data.live_price or data.plan.entry)}.")
    if confirmation is not None:
        lines.append(escape(confirmation.advice))
    lines.extend(escape(note) for note in data.notes)
    lines.append("")
    lines.extend(plan_lines(data, fees))
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def format_cancelled(symbol: str, name: str, trigger: float, close: float, confirmation: Confirmation, test: bool = False) -> str:
    """The evening message for an early alert that did not hold."""
    test_line = "🔧 <b>TEST — sample only, not a real alert</b>\n" if test else ""
    return "\n".join(
        [
            f"{test_line}❌ <b>{escape(confirmation.headline)}</b>",
            f"<b>{escape(symbol)}</b> — {escape(name)}",
            f"Today's early alert is cancelled: it closed at {price(close)}, below the trigger {price(trigger)}.",
            "",
            escape(confirmation.advice),
            "If you did not buy: do nothing. It stays on the watch list (/watch).",
            "",
            DISCLAIMER,
        ]
    )


def format_setup_change(setup_name: str, promoted: bool, record_lines: list[str]) -> str:
    """One-time message when a setup passes the gate (promoted to BUY) or falls back (demoted)."""
    if promoted:
        head = f"📈 <b>Setup promoted to BUY: {escape(setup_name)}</b>\nIts backtest now passes the gate in both periods, so its candidates are graded BUY / STRONG BUY and get buy alerts."
    else:
        head = f"📉 <b>Setup demoted: {escape(setup_name)}</b>\nIts backtest no longer passes the gate, so its candidates are SPECULATIVE again and no longer get BUY alerts."
    return "\n".join([head, *[f"• {escape(text)}" for text in record_lines]])


def format_alert_settings(db: Any) -> str:
    """/settings alerts: what is on, and what each type would have sent in the backtest."""
    results = backtest_results()
    alerts = results.get("alerts") or {}
    lines = ["🔔 <b>Automatic alerts</b>", ""]
    for name in TYPES:
        state = "ON" if enabled(db, name) else "off"
        lines.append(f"<b>{name}</b>: {state} — {escape(TYPE_TEXT[name])}")
        if name == "early":
            lines.append("   Backtest: not backtested (needs intraday prices). Tracked on its own line in /report.")
            if alert_setting("early", "require_proven_setup", default=True):
                lines.append("   Only for setups graded BUY. No setup is graded BUY today, so no early alert will come until one is.")
            continue
        recent = (alerts.get(name) or {}).get("recent") or {}
        if not recent:
            lines.append("   Backtest: no numbers yet (run python -m nepse_bot.backtest.alerts).")
        elif not recent.get("alerts"):
            lines.append("   Backtest, last 2 years: 0 alerts (no setup passed the gate, so nothing was graded BUY).")
        elif not recent.get("finished"):
            lines.append(f"   Backtest, last 2 years: {recent['alerts']} alerts, none finished.")
        else:
            factor = recent.get("profit_factor")
            lines.append(
                f"   Backtest, last 2 years: {recent['alerts']} alerts, {recent['finished']} finished, {recent['win_rate']:.0f}% won, "
                f"average {recent['expectancy_pct']:+.1f}% per trade, net {money(recent['net_rupees'])}"
                + (f", profit factor {factor:g}" if factor is not None else "") + " (after fees and tax)."
            )
    lines.extend(
        [
            "",
            "Alerts for your holdings (stop hit or near, raise the stop, target reached, time limit, events) are always on once you log a buy with /bought.",
            "",
            "Change: /settings alerts strong on|off • /settings alerts early on|off • /settings alerts toppick on|off",
            "Honest note: the top pick is rare and its few backtest cases lost money. It is the best of a speculative list, not a proven signal.",
        ]
    )
    return "\n".join(lines)
