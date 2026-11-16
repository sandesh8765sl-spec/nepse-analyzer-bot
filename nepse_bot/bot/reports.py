"""Telegram report text for the engine's results (HTML formatting).

Everything here is a pure function of the engine's output, so the same text
can be tested without Telegram. All dynamic text is HTML-escaped.
"""

from __future__ import annotations

from datetime import date
from html import escape

from nepse_bot.engine import evidence, exits
from nepse_bot.engine.config import PROFILE_KEYS, Profile
from nepse_bot.engine.engine import ScanResult, StockResult
from nepse_bot.portfolio.fees import fee_schedule
from nepse_bot.trading_calendar import TradingCalendar, get_calendar
from nepse_bot.utils.freshness import as_of_line

MESSAGE_LIMIT = 3900
VERDICT_MARK = {"STRONG BUY": "✅✅", "BUY": "✅", "SPECULATIVE": "🧪", "WATCH": "👀", "AVOID": "⛔", "IGNORE": "➖"}
DISCLAIMER = "Not financial advice. You place every order yourself."
SUB_SCORE_LABELS = (
    ("trend", "Trend"), ("momentum", "Momentum"), ("relative_strength", "Strength"),
    ("volume_flow", "Volume"), ("fundamentals", "Fundamentals"), ("setup_quality", "Setup"),
)


def money(value: float) -> str:
    """Format rupees compactly: 98,880 or 1.2 lakh style is avoided for clarity."""
    return f"Rs {value:,.0f}"


def price(value: float) -> str:
    """Format a share price without needless decimals."""
    return f"{value:,.0f}" if value >= 200 or abs(value - round(value)) < 0.05 else f"{value:,.1f}"


def freshness_line(scan: ScanResult, calendar: TradingCalendar | None = None) -> str:
    """The as-of line: data date and source, with a warning when stale."""
    calendar = calendar or get_calendar()
    return as_of_line(date.fromisoformat(scan.as_of), "NEPSE, bot database", calendar)


def _position_range(profile: Profile) -> str:
    def short(value: float) -> str:
        return f"{value / 100000:g}L" if value >= 100000 else f"{value / 1000:g}k"

    return f"Rs {short(profile.min_position)}-{short(profile.max_position)}"


def _market_line(scan: ScanResult, item: StockResult) -> str:
    regime = scan.regime
    sector = item.sector_state
    sector_text = f"{escape(item.sector)} - {sector.label}" if sector else escape(item.sector)
    heat = " • overheated" if regime.overheated else ""
    return f"Market: {regime.label}{heat} • Sector: {sector_text}"


def _fundamentals_line(item: StockResult) -> str:
    view = item.fundamentals
    if not view or view.get("eps") is None:
        return "Fundamentals: unavailable (counted as neutral, lowers confidence)"
    parts = []
    if view.get("negative_earnings"):
        parts.append("loss-making")
    elif view.get("pe") is not None:
        median = view.get("pe_median")
        scope = "all-market" if view.get("median_scope") == "market" else "sector"
        parts.append(f"P/E {view['pe']:.1f}" + (f" ({scope} median {median:.0f})" if median else ""))
    if view.get("pb") is not None:
        parts.append(f"P/B {view['pb']:.1f}")
    if view.get("eps_growth") is not None:
        parts.append(f"EPS {view['eps_growth']:+.0f}% y/y")
    if view.get("roe") is not None:
        parts.append(f"ROE {view['roe']:.0f}%")
    if view.get("npl") is not None:
        parts.append(f"NPL {view['npl']:.1f}%")
    period = f"FY {view.get('fiscal_year')} Q{view.get('quarter')}"
    if view.get("behind_newest_quarter"):
        period += ", its newest published report; most companies have a newer quarter out"
    return "Fundamentals: " + " • ".join(parts) + f" [{period}]"


def _level_text(level: exits.Level) -> str:
    return f"{price(level.price)} ({level.gain_pct:+.1f}%) → net {money(level.net_rupees)}"


def target_lines(plan, style: str) -> list[str]:
    """Reference targets, typical outcome, partial-profit level and the exit for the chosen style."""
    targets = exits.targets_for(plan, fee_schedule())
    lines = ["<b>Reference targets</b> (after fees and tax, whole position)"]
    lines.append(f"• {escape(targets.resistance.name.capitalize())}: {_level_text(targets.resistance)}")
    if targets.high_52w is not None:
        lines.append(f"• 52-week high: {_level_text(targets.high_52w)}")
    elif "52-week high" not in targets.resistance.name:
        lines.append("• 52-week high: the entry is already at or above it")
    stats = targets.stats
    if stats is not None and targets.typical is not None and targets.top_quarter is not None:
        lines.append(f"<b>Typical outcome for this setup</b> (backtest {stats.period}, {stats.count} winning trades)")
        lines.append(
            f"• past winners: +{stats.median_pct:g}% → {price(targets.typical.price)}, "
            f"top quarter +{stats.top_quarter_pct:g}% → {price(targets.top_quarter.price)}"
        )
    partial = targets.partial
    lines.append(
        f"<b>Consider booking partial profit</b> at {price(partial.price)} ({partial.gain_pct:+.1f}%): sell {targets.partial_quantity:,} sh "
        f"(net about {money(partial.net_rupees)}) - {escape(targets.partial_reason)}."
    )
    lines.append("")
    lines.append(f"<b>EXIT</b> — your style: <b>{style}</b> ({escape(exits.STYLE_TEXT[style])}). Change: /settings exit")
    if style == "target":
        lines.append(f"Sell everything at {price(targets.resistance.price)}. Until then keep the stop; sell after {escape(plan.hold_text)} anyway.")
    elif style == "hybrid":
        lines.append(f"Sell half at {price(targets.resistance.price)}, then follow the rest with the trailing stop. Sell after {escape(plan.hold_text)} anyway.")
    else:
        lines.append(f"No fixed target: sell when the trailing stop is hit, or after {escape(plan.hold_text)}.")
    lines.append(f"Trailing stop: {escape(plan.trail_text)}. Raise your stop-loss order as the price makes new highs.")
    note = exits.worse_than_default_note(style)
    if note:
        lines.append(f"⚠️ {escape(note)}")
    return lines


def format_stock(item: StockResult, scan: ScanResult, calendar: TradingCalendar | None = None, style: str | None = None,
                 risk_notes: list[str] | None = None, corporate: list[str] | None = None) -> str:
    """The full /stock report. ``risk_notes`` are the portfolio-risk remarks about adding this stock."""
    style = style or exits.default_style()
    change = f" ({item.change_percent:+.1f}%)" if item.change_percent is not None else ""
    lines = [
        f"📈 <b>{escape(item.symbol)}</b> — {escape(item.name)} | {escape(item.sector)} | LTP {price(item.close)}{change}",
        escape(freshness_line(scan, calendar)),
        "",
    ]
    mark = VERDICT_MARK.get(item.verdict, "")
    if item.verdict == "IGNORE":
        lines.append(f"Verdict: <b>IGNORE</b> {mark}")
        lines.append("Not analysed, because:")
        lines.extend(f"− {escape(reason)}" for reason in item.why_not_buy)
        lines.extend(["", DISCLAIMER])
        return "\n".join(lines)

    lines.append(f"Verdict: <b>{item.verdict}</b> {mark}   Score {item.score:.0f}/100   Confidence: {item.confidence}")
    if item.setup_name:
        lines.append(f"Setup: {escape(item.setup_name)}")
    lines.append(_market_line(scan, item))
    for flag in item.flags:
        lines.append(f"⚠️ {escape(flag)}")
    lines.extend(escape(note) for note in item.notes)
    if scan.regime.overheated:
        lines.append("⚠️ Market is overheated (most stocks far above their 50-day average): new buys did worst here in 5 of 7 years.")

    plan = item.plan
    if item.is_buy and plan is not None:
        lines.extend(["", f"<b>PLAN</b> (sized for {_position_range(scan.profile)})", *_plan_block(plan)])
        lines.append(f"Stop is {escape(plan.stop_reason)}.")
        lines.extend(f"⚠️ {escape(note)}" for note in plan.notes)
        lines.extend(f"⚠️ Your portfolio: {escape(note)}" for note in risk_notes or [])
        lines.extend(["", *target_lines(plan, style)])
        lines.extend(["", *_evidence_lines(item)])
    elif item.verdict == "WATCH":
        lines.append("")
        lines.append("<b>WAIT FOR</b>")
        lines.append(escape(item.watch_trigger) if item.watch_trigger else "No entry trigger yet. Re-check after the next close.")
        if item.why_not_buy:
            lines.append("")
            lines.append("<b>WHY NOT A BUY TODAY</b>")
            lines.extend(f"• {escape(reason)}" for reason in item.why_not_buy[:4])
    elif item.verdict == "AVOID":
        lines.append("")
        lines.append("<b>AVOID BECAUSE</b>")
        lines.extend(f"• {escape(reason)}" for reason in item.why_not_buy[:3])

    if corporate:
        lines.extend(["", "<b>UPCOMING CORPORATE EVENTS</b>"])
        lines.extend(f"• {escape(text)}" for text in corporate[:5])
    lines.extend(["", "<b>WHY</b>"])
    lines.extend(f"+ {escape(text)}" for text in item.positives[:3])
    lines.extend(f"− {escape(text)}" for text in item.negatives[:3])
    lines.append("")
    lines.append("Score parts: " + " • ".join(f"{label} {item.sub_scores.get(key, 50):.0f}" for key, label in SUB_SCORE_LABELS))
    lines.append(escape(_fundamentals_line(item)))
    if item.rs_rank is not None:
        lines.append(f"Relative strength rank: {item.rs_rank:.0f} of 100")
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def _plan_block(plan) -> list[str]:
    """The numbers of a plan, as a fixed-width block."""
    text = "\n".join(
        [
            f"Buy:   {price(plan.entry_low)}-{price(plan.entry_high)} next day (not above {price(plan.entry_high)})",
            f"Stop:  {price(plan.stop)}  (-{plan.stop_percent:.1f}%)",
            f"Qty:   {plan.quantity:,} sh = {money(plan.position_value)}",
            f"Risk:  {money(plan.risk_rupees)} if stopped (after costs)",
        ]
    )
    return ["<pre>" + escape(text) + "</pre>"]


def _evidence_lines(item: StockResult) -> list[str]:
    """The setup's backtest record and the warning that goes with it."""
    record = item.evidence or evidence.for_setup("")
    lines = [f"<b>This setup's record</b> ({escape(item.setup_name or 'setup')}, backtest after fees and tax)"]
    lines.extend(f"• {escape(text)}" for text in record.lines())
    lines.append(escape(record.warning()))
    return lines


def _short_record(item: StockResult) -> str:
    record = item.evidence
    recent = record.recent if record else None
    if recent is None or not recent.trades or recent.win_rate is None:
        return "no backtest record"
    return f"last 2 yrs: {recent.win_rate:.0f}% won, {recent.expectancy_pct:+.1f}%/trade ({recent.trades} trades)"


FULL_DATA_MARK = "📊"


def _note_lines(item: StockResult) -> str:
    """Information-only warnings of a stock (lock-in expiry, resistance close above), indented for lists."""
    return "".join("\n    " + escape(note) for note in item.notes)


def _one_line(index: int, item: StockResult) -> str:
    star = f" {FULL_DATA_MARK}" if item.confidence == "High" else ""
    plan = item.plan
    setup = (item.setup_name or "").replace("Relative-strength leader", "RS leader").replace("Trend pullback", "Pullback").replace("Base breakout", "Breakout")
    if plan is not None and item.is_buy:
        return (
            f"{index}. <b>{escape(item.symbol)}</b>  {item.score:.0f}{star}  <b>{item.verdict}</b>  {escape(setup)}\n"
            f"    Buy {price(plan.entry_low)}-{price(plan.entry_high)}  Stop {price(plan.stop)} (-{plan.stop_percent:.0f}%)  "
            f"Ref target {price(plan.target_1)} (+{plan.target_1_percent:.0f}%)\n"
            f"    {escape(_short_record(item))}" + _note_lines(item)
        )
    return f"{index}. <b>{escape(item.symbol)}</b>  {item.score:.0f}{star}  {item.verdict}  LTP {price(item.close)}" + _note_lines(item)


def _regime_warning(scan: ScanResult) -> list[str]:
    regime = scan.regime
    lines = []
    if regime.overheated:
        lines.append("⚠️ Market is overheated: most stocks are far above their 50-day average. New buys did worst in this state in 5 of 7 years.")
    if scan.by_verdict("STRONG BUY", "BUY"):   # the daily limit only matters when something is graded BUY
        limit = regime.max_new_positions
        lines.append(f"Act on at most {limit} new position{'s' if limit != 1 else ''} per day.")
    return lines


def format_top10(scan: ScanResult, calendar: TradingCalendar | None = None) -> tuple[str, list[str]]:
    """The /top10 report: always ten ranked names. Returns (text, symbols shown) so buttons can be attached."""
    regime = scan.regime
    lines = [f"🏆 <b>Top 10</b> — {scan.as_of} close | Market: {regime.label}", escape(freshness_line(scan, calendar)), ""]
    buys = scan.buys[:10]
    shown: list[str] = []
    if buys:
        lines.append("<b>Candidates with a plan</b>")
        for index, item in enumerate(buys, start=1):
            lines.append(_one_line(index, item))
            shown.append(item.symbol)
    else:
        lines.append("<b>No stock has a valid entry setup today.</b>")
    remaining = 10 - len(shown)
    if remaining > 0:
        watches = [item for item in scan.by_verdict("WATCH") if item.symbol not in shown][:remaining]
        if watches:
            lines.extend(["", "<b>Highest scores without an entry today (watch)</b>"])
            for index, item in enumerate(watches, start=len(shown) + 1):
                lines.append(_one_line(index, item))
                if item.watch_trigger:
                    lines.append(f"    {escape(item.watch_trigger[:150])}")
                shown.append(item.symbol)
    lines.append("")
    lines.append(escape(evidence.summary_note()))
    lines.extend(_regime_warning(scan))
    lines.append(f"{FULL_DATA_MARK} full data = the score rests on complete data (it is not a confidence rating). Ref target = next major resistance.")
    lines.append("The ref target is information only. \"Resistance close above\" = it is less than 1.5x the stop distance away; such a stock ranks slightly lower.")
    lines.append("Tap a symbol for the full plan, targets and record.")
    return "\n".join(lines), shown


def format_buy_page(scan: ScanResult, page: int = 0, page_size: int = 3, calendar: TradingCalendar | None = None, style: str | None = None) -> tuple[str, int]:
    """One page of candidates with full plans. Returns (text, number of pages)."""
    style = style or exits.default_style()
    buys = scan.buys
    pages = max(1, -(-len(buys) // page_size))
    page = max(0, min(page, pages - 1))
    lines = [f"📋 <b>Candidates with a plan</b> — {scan.as_of} close | Market: {scan.regime.label} | page {page + 1}/{pages}", escape(freshness_line(scan, calendar)), ""]
    if not buys:
        lines.append("<b>No stock has a valid entry setup today.</b> See /top10 for the ranking and /watch for what to wait for.")
        lines.extend(["", *_regime_warning(scan)])
        return "\n".join(lines), pages
    for item in buys[page * page_size : (page + 1) * page_size]:
        plan = item.plan
        assert plan is not None
        lines.append(f"<b>{escape(item.symbol)}</b> — {item.verdict} {VERDICT_MARK.get(item.verdict, '')} • score {item.score:.0f} • {escape(plan.setup_name)}")
        lines.extend(_plan_block(plan))
        lines.extend(escape(note) for note in item.notes)
        targets = exits.targets_for(plan, fee_schedule())
        high = f" • 52-week high {price(targets.high_52w.price)} ({targets.high_52w.gain_pct:+.0f}%)" if targets.high_52w else ""
        lines.append(f"Reference target {price(targets.resistance.price)} ({targets.resistance.gain_pct:+.1f}%, net {money(targets.resistance.net_rupees)}){high}")
        lines.append(f"Partial profit: consider selling half at the reference target {price(targets.partial.price)} ({targets.partial.gain_pct:+.1f}%).")
        lines.append(f"Exit ({style}): {escape(exits.STYLE_TEXT[style])}; sell after {escape(plan.hold_text)} anyway.")
        record = item.evidence or evidence.for_setup("")
        lines.extend(f"• {escape(text)}" for text in record.lines()[:1])
        lines.append(escape(record.warning()))
        lines.append("")
    lines.append(escape(evidence.summary_note()))
    lines.extend(_regime_warning(scan))
    return "\n".join(lines), pages


def format_watch(scan: ScanResult, limit: int = 15, calendar: TradingCalendar | None = None) -> tuple[str, list[str]]:
    """The /watch report: good stocks with the price and condition to wait for."""
    watches = scan.watches[:limit]
    lines = [f"👀 <b>Watch list</b> — {scan.as_of} close | Market: {scan.regime.label}", escape(freshness_line(scan, calendar)), ""]
    if not watches:
        lines.append("Nothing on the watch list today.")
    for index, item in enumerate(watches, start=1):
        lines.append(f"{index}. <b>{escape(item.symbol)}</b>  score {item.score:.0f}  LTP {price(item.close)}")
        lines.append(f"    {escape((item.watch_trigger or '')[:200])}")
    lines.extend(["", "These are not buy signals. The bot re-checks them after every close."])
    return "\n".join(lines), [item.symbol for item in watches]


def format_market(scan: ScanResult, calendar: TradingCalendar | None = None) -> str:
    """The /market report."""
    regime = scan.regime
    lines = [f"🌐 <b>Market</b> — {scan.as_of} close", escape(freshness_line(scan, calendar)), ""]
    lines.append(f"Regime: <b>{regime.label}</b> (score {regime.score:.0f}/100)")
    lines.append("In backtests this label did not predict the following weeks, so it is shown for information only.")
    lines.extend(_regime_warning(scan))
    if regime.index_close:
        lines.append("")
        lines.append(f"NEPSE index: {regime.index_close:,.1f}")
        if regime.sma50:
            lines.append(f"• 50-day average {regime.sma50:,.0f} ({'above' if regime.index_close > regime.sma50 else 'below'})")
        if regime.sma200:
            lines.append(f"• 200-day average {regime.sma200:,.0f} ({'above' if regime.index_close > regime.sma200 else 'below'})")
        if regime.high_52w and regime.low_52w:
            lines.append(f"• 52-week range {regime.low_52w:,.0f} - {regime.high_52w:,.0f}")
    lines.append("")
    lines.append("<b>Breadth and turnover</b>")
    if regime.breadth_10d is not None:
        lines.append(f"• {regime.breadth_10d:.0f}% of price moves were up over the last 10 days")
    if regime.pct_above_ma50 is not None:
        lines.append(f"• {regime.pct_above_ma50:.0f}% of stocks are above their 50-day average")
    if regime.turnover_today:
        ratio = f" (10-day average is {regime.turnover_ratio:.1f}x the 50-day)" if regime.turnover_ratio else ""
        lines.append(f"• Turnover Rs {regime.turnover_today / 1e7:,.0f} crore{ratio}")
    lines.extend(["", "<b>Sectors</b> (strongest first)"])
    marks = {"strong": "🟢", "neutral": "🟡", "weak": "🔴"}
    for state in sorted(scan.sectors.values(), key=lambda s: s.score, reverse=True):
        move = f"{state.return_20d:+.1f}% in 20 days" if state.return_20d is not None else "no data"
        versus = f", {state.versus_market_60d:+.1f}% vs NEPSE in 60 days" if state.versus_market_60d is not None else ""
        lines.append(f"{marks.get(state.label, '')} {escape(state.sector)}: {state.label} ({move}{versus})")
    counts = {verdict: len(scan.by_verdict(verdict)) for verdict in ("STRONG BUY", "BUY", "SPECULATIVE", "WATCH", "AVOID", "IGNORE")}
    lines.extend(
        [
            "",
            f"Today: {counts['STRONG BUY'] + counts['BUY']} buy • {counts['SPECULATIVE']} speculative • {counts['WATCH']} watch • {counts['AVOID']} avoid • {counts['IGNORE']} ignored (illiquid or new)",
            "See /top10, /buy, /watch, or /sector NAME.",
        ]
    )
    return "\n".join(lines)


def match_sector(scan: ScanResult, text: str) -> str | None:
    """Find a sector by a loose name such as ``hydro`` or ``bank``."""
    wanted = text.strip().lower()
    if not wanted:
        return None
    for sector in scan.sectors:
        if wanted == sector.lower():
            return sector
    for sector in scan.sectors:
        if wanted in sector.lower() or sector.lower().startswith(wanted[:4]):
            return sector
    return None


def format_sector(scan: ScanResult, sector: str | None, calendar: TradingCalendar | None = None) -> tuple[str, list[str]]:
    """The /sector report: sector ranking, or one sector's stocks ranked."""
    if sector is None:
        lines = [f"🏭 <b>Sectors</b> — {scan.as_of} close", escape(freshness_line(scan, calendar)), ""]
        for index, state in enumerate(sorted(scan.sectors.values(), key=lambda s: s.score, reverse=True), start=1):
            members = [item for item in scan.results.values() if item.sector == state.sector and item.verdict != "IGNORE"]
            best = max(members, key=lambda item: item.score, default=None)
            best_text = f" • best: {escape(best.symbol)} ({best.score:.0f})" if best else ""
            lines.append(f"{index}. {escape(state.sector)} — {state.label} ({state.score:.0f}){best_text}")
        lines.extend(["", "Use /sector NAME, for example /sector hydro"])
        return "\n".join(lines), []
    state = scan.sectors.get(sector)
    members = sorted((item for item in scan.results.values() if item.sector == sector and item.verdict != "IGNORE"), key=lambda item: item.score, reverse=True)
    lines = [f"🏭 <b>{escape(sector)}</b> — {state.label if state else 'unknown'} | {scan.as_of} close", escape(freshness_line(scan, calendar)), ""]
    for index, item in enumerate(members[:15], start=1):
        lines.append(f"{index}. <b>{escape(item.symbol)}</b>  {item.score:.0f}  {item.verdict} {VERDICT_MARK.get(item.verdict, '')}  LTP {price(item.close)}")
    ignored = sum(1 for item in scan.results.values() if item.sector == sector and item.verdict == "IGNORE")
    if not members:
        lines.append("No liquid stocks in this sector.")
    lines.extend(["", f"{ignored} more in this sector are ignored (illiquid or newly listed). Tap a symbol for details."])
    return "\n".join(lines), [item.symbol for item in members[:15]]


def format_exit_help(style: str) -> str:
    """/settings exit: the three exit styles with their backtest record."""
    default = exits.default_style()
    lines = ["⚙️ <b>Exit style</b>", "", f"Yours: <b>{style}</b> ({escape(exits.STYLE_TEXT[style])})", ""]
    for name in exits.STYLES:
        lines.append(f"• /settings exit {name} — {escape(exits.STYLE_TEXT[name])}{' (default)' if name == default else ''}")
    comparison = exits.comparison_lines()
    if comparison:
        lines.extend(["", "<b>Backtest, same candidates, after fees and tax</b> (average result per trade)"])
        lines.extend(f"• {escape(text)}" for text in comparison)
        lines.append(
            f"'{default}' is the default because it did best in the period the rules were chosen on (2020-2024) and over the whole backtest. "
            "Honest note: all three LOST money over the last 2 years; 'target' and 'hybrid' win more often but their wins are small, "
            "so they lost a little less recently and made far less in rising years."
        )
    note = exits.worse_than_default_note(style)
    if note:
        lines.extend(["", f"⚠️ {escape(note)}"])
    lines.extend(["", "The style changes the EXIT text in /stock and the advice in /positions. The stop-loss is the same in all three."])
    return "\n".join(lines)


def format_settings(profile: Profile, style: str | None = None) -> str:
    """The /settings report."""
    style = style or exits.default_style()
    values = {
        "total_capital": money(profile.total_capital),
        "min_position": money(profile.min_position),
        "max_position": money(profile.max_position),
        "max_open_positions": str(profile.max_open_positions),
        "risk_percent": f"{profile.risk_percent:g}%",
        "risk_rupees": money(profile.risk_rupees) if profile.risk_rupees else "not set (the % is used)",
    }
    lines = ["⚙️ <b>Your settings</b>", ""]
    for key, label in PROFILE_KEYS.items():
        lines.append(f"• {escape(label)}: <b>{escape(values[key])}</b>")
    lines.extend(
        [
            "",
            f"Risk per trade right now: {money(profile.risk_budget)}",
            "",
            "Change one with, for example:",
            "/settings capital 600000",
            "/settings min 50000",
            "/settings max 100000",
            "/settings positions 5",
            "/settings risk 2      (percent of capital)",
            "/settings riskrs 8000 (fixed rupees; /settings riskrs off to go back to %)",
            "",
            f"Exit style: <b>{style}</b> ({escape(exits.STYLE_TEXT[style])})",
            "/settings exit trailing | target | hybrid   (send /settings exit for the backtest comparison)",
            "",
            "Automatic alerts: /settings alerts   (strong, early, toppick: on or off)",
        ]
    )
    return "\n".join(lines)


SETUP_LABELS = {
    "trend_pullback": "Trend pullback", "base_breakout": "Base breakout", "rs_leader": "RS leader",
    "value_turn": "Value + turn", "broker_accumulation": "Broker accumulation", "event_runup": "Event run-up",
    "early_alert": "EARLY alerts (bought during the session, all setups)",
}


def format_live_report(rows: list[dict], disabled: dict[str, str]) -> str:
    """The /report text: how the bot's own BUY signals turned out, per setup."""
    lines = ["📒 <b>Live results of the bot's BUY signals</b>", "After commission, fees and tax. Judged with each day's high and low.", ""]
    if not rows:
        lines.append("No BUY signal has been logged yet.")
    for row in rows:
        label = SETUP_LABELS.get(row["setup"], row["setup"])
        lines.append(f"<b>{escape(label)}</b>{' — SWITCHED OFF' if row.get('disabled') else ''}")
        lines.append(f"• Signals: {row['signals']} (open {row['open']}, never filled {row['not_filled']}, finished {row['finished']})")
        if row["finished"]:
            factor = row["profit_factor"]
            factor_text = "no losses yet" if factor == float("inf") else f"{factor:g}"
            lines.append(f"• Finished: {row['wins']} won of {row['finished']}, net {money(row['net_profit'])}, profit factor {factor_text}")
        lines.append("")
    if disabled:
        lines.append("<b>Switched off automatically</b>")
        for setup, reason in disabled.items():
            lines.append(f"• {escape(SETUP_LABELS.get(setup, setup))}: {escape(reason)}")
            lines.append(f"  Switch back on: /report enable {escape(setup)}")
        lines.append("")
    lines.append("Profit factor = money won ÷ money lost. Above 1 means the signals made money overall.")
    lines.append("A setup is switched off automatically if this drops below 1 over its last 20 finished signals.")
    return "\n".join(lines)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split a long report on line breaks so each part fits in one Telegram message."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def fetched_news_line(calendar: TradingCalendar | None = None) -> str:
    """The as-of line for news (no market date applies)."""
    from nepse_bot.utils.freshness import fetched_line

    return fetched_line("news sources, cached up to 45 minutes", calendar or get_calendar())
