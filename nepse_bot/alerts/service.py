"""Alert service: decides which alerts are due and remembers what was sent.

No Telegram here: every function returns ``Outgoing`` messages and the bot
layer (``nepse_bot.bot.alerting``) sends them. What was sent is kept in the
``alerts`` table, which is what makes the rules hold across restarts:

* an early alert is ``pending`` until the evening scan confirms or cancels it;
* a stock is not alerted again within 10 trading days unless its plan changed;
* a holding gets at most one alert per event per day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from nepse_bot.alerts import rules, text
from nepse_bot.alerts.config import alert_setting, enabled
from nepse_bot.db.store import Database, utc_now_text
from nepse_bot.engine import evidence as evidence_module
from nepse_bot.engine.config import Profile
from nepse_bot.engine.indicators import add_indicators
from nepse_bot.engine.plan import PlanRejection, SetupSignal, TradePlan, build_plan
from nepse_bot.events import engine as events
from nepse_bot.ingest.adjust import limit_for, price_limits
from nepse_bot.portfolio import journal, positions
from nepse_bot.portfolio.fees import FeeSchedule

logger = logging.getLogger(__name__)

PLAN_KINDS = ("buy", "top_pick", "early", "confirmed")
EVENING_DONE = "alerts.evening_done"


@dataclass
class Outgoing:
    """One message to send. ``button`` = (symbol, quantity, price) for the 'I bought it' button."""

    text: str
    kind: str
    symbol: str = ""
    button: tuple[str, int, float] | None = None


# ------------------------------------------------------------------ the record
def log_alert(db: Database, kind: str, symbol: str, day: str, event: str = "", status: str = "sent", detail: dict[str, Any] | None = None) -> bool:
    """Remember an alert. Returns False when the same alert was already recorded (so it is not sent twice)."""
    with db.transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO alerts (kind, symbol, day, event, status, sent_at, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (kind, symbol, day, event) DO NOTHING",
            (kind, symbol, day, event, status, utc_now_text(), json.dumps(detail or {}, default=str)),
        )
        return cursor.rowcount > 0


def last_plan_alert(db: Database, symbol: str) -> tuple[str, dict[str, Any]] | None:
    """The day and plan of the most recent buy-type alert for a stock."""
    marks = ",".join("?" for _ in PLAN_KINDS)
    rows = db.query(f"SELECT day, detail_json FROM alerts WHERE symbol = ? AND kind IN ({marks}) ORDER BY day DESC, id DESC LIMIT 1", (symbol, *PLAN_KINDS))
    if not rows:
        return None
    try:
        return rows[0]["day"], json.loads(rows[0]["detail_json"] or "{}")
    except ValueError:
        return rows[0]["day"], {}


def sessions_since(db: Database, day: str, today: str) -> int:
    """Trading days between two dates, counted from the price history."""
    return int(db.scalar("SELECT COUNT(DISTINCT date) FROM daily_prices WHERE date > ? AND date <= ?", (day, today)) or 0)


def _may_alert(db: Database, symbol: str, today: str, fingerprint: dict[str, Any]) -> bool:
    previous = last_plan_alert(db, symbol)
    if previous is None:
        return True
    return rules.may_alert_again(previous[1], sessions_since(db, previous[0], today), fingerprint)


# --------------------------------------------------------------------- events
def holding_window_events(db: Database, symbol: str, today: str, close: float) -> tuple[list[str], list[str]]:
    """Known events for a stock inside the planned holding window: (all, price-adjusting only)."""
    sessions = int(alert_setting("top_pick", "holding_window_sessions", default=40))
    end = (date.fromisoformat(today) + timedelta(days=int(sessions * 7 / 5) + 2)).isoformat()
    found = [item for item in events.corporate_events(db, today, [symbol], {symbol: close}) if item.closure_day is None or item.closure_day <= end]
    lines = [item.line().split(": ", 1)[1] for item in found]
    return lines, [line for item, line in zip(found, lines) if item.adjusts_price]


# ---------------------------------------------------------------- evening run
def evening_due(db: Database, scan: Any, calendar: Any, now: datetime | None = None) -> bool:
    """Are the evening alerts still owed for this scan, and still valid for the next session?

    True only when the scan is of the last completed trading day, the market is not open
    (after a sleep the alerts go out on wake, but never once the next session has started),
    and they were not already sent for this day.
    """
    now = now or calendar.now()
    return bool(
        not scan.stale and scan.as_of == calendar.last_completed_trading_day(now).isoformat()
        and not calendar.is_market_open(now) and db.get_setting(EVENING_DONE) != scan.as_of
    )


def setup_changes(db: Database) -> list[Outgoing]:
    """A one-time message when a setup passes the backtest gate (promoted to BUY) or falls back."""
    from nepse_bot.engine.setups import enabled_setups

    names = {setup.key: setup.name for setup in enabled_setups()}
    proven = sorted(key for key in names if evidence_module.for_setup(key).proven)
    stored = db.get_setting("alerts.proven_setups")
    db.set_setting("alerts.proven_setups", json.dumps(proven))
    if stored is None:
        return []
    try:
        before = set(json.loads(stored))
    except ValueError:
        before = set()
    out = []
    for key in sorted(set(proven) ^ before):
        if key in names:
            out.append(Outgoing(text.format_setup_change(names[key], key in proven, evidence_module.for_setup(key).lines()), "setup", key))
    return out


def _plan_from(detail: dict[str, Any]) -> TradePlan | None:
    try:
        data = dict(detail["plan"])
        data["notes"] = tuple(data.get("notes") or ())
        return TradePlan(**data)
    except (KeyError, TypeError):
        return None


def evening_alerts(engine: Any, scan: Any, style: str = "trailing") -> list[Outgoing]:
    """Everything owed after the evening scan: confirmations, BUY alerts, the top pick. Records what it returns."""
    db, fees, day = engine.db, engine.fees, scan.as_of
    next_session = engine.calendar.next_trading_day(date.fromisoformat(day)).isoformat()
    out: list[Outgoing] = setup_changes(db)
    handled: set[str] = set()

    def build(kind: str, item: Any, plan: TradePlan | None = None, **extra: Any) -> text.AlertInput:
        lines, _ = holding_window_events(db, item.symbol, day, item.close)
        return text.from_item(kind, item, scan, plan, events=lines, style=style, next_session=next_session, **extra)

    # ---- 1. every early alert of today is confirmed or cancelled
    for row in db.query("SELECT id, symbol, detail_json FROM alerts WHERE kind = 'early' AND day = ? AND status = 'pending'", (day,)):
        item = scan.results.get(row["symbol"])
        detail = json.loads(row["detail_json"] or "{}")
        if item is None or not detail.get("trigger"):
            continue
        handled.add(item.symbol)
        outcome = rules.confirm_early(float(detail["trigger"]), item.close, float(detail.get("stop") or 0.0))
        with db.transaction() as connection:
            connection.execute("UPDATE alerts SET status = ? WHERE id = ?", ("confirmed" if outcome.confirmed else "cancelled", row["id"]))
        if outcome.confirmed:
            plan = item.plan or item.blocked_plan or _plan_from(detail)
            if plan is None:
                continue
            data = build("CONFIRMED", item, plan, trigger=float(detail["trigger"]))
            log_alert(db, "confirmed", item.symbol, day, detail=rules.plan_fingerprint(data.verdict, plan))
            out.append(Outgoing(text.format_alert(data, fees, outcome), "confirmed", item.symbol, (item.symbol, plan.quantity, plan.entry)))
        else:
            log_alert(db, "cancelled", item.symbol, day)
            out.append(Outgoing(text.format_cancelled(item.symbol, item.name, float(detail["trigger"]), item.close, outcome), "cancelled", item.symbol))

    # ---- 2. stocks that qualify at the close: the normal evening alert with the full plan
    if enabled(db, "strong"):
        graded = [item for item in scan.buys if item.verdict in rules.BUY_GRADES and item.plan is not None and item.symbol not in handled]
        for item in graded[: int(alert_setting("max_buy_alerts_per_day", default=3))]:
            fingerprint = rules.plan_fingerprint(item.verdict, item.plan)
            if not _may_alert(db, item.symbol, day, fingerprint) or not log_alert(db, "buy", item.symbol, day, detail=fingerprint):
                continue
            out.append(Outgoing(text.format_alert(build(item.verdict, item), fees), "buy", item.symbol, (item.symbol, item.plan.quantity, item.plan.entry)))

    # ---- 3. at most one TOP PICK: the best speculative candidate that passes every rule
    #         (held back while protection mode is on: losing streak or portfolio drop)
    from nepse_bot.portfolio import protection

    warning = protection.check(db, day, engine.calendar, scan.profile.total_capital)
    if warning:
        out.append(Outgoing(warning, "protection"))
    if enabled(db, "toppick") and not protection.paused_until(db, day) and not db.scalar("SELECT COUNT(*) FROM alerts WHERE kind = 'top_pick' AND day = ?", (day,)):
        pick = find_top_pick(db, scan)
        if pick is not None and log_alert(db, "top_pick", pick.symbol, day, detail=rules.plan_fingerprint(pick.verdict, pick.plan)):
            out.append(Outgoing(text.format_alert(build("TOP PICK", pick), fees), "top_pick", pick.symbol, (pick.symbol, pick.plan.quantity, pick.plan.entry)))

    review = early_review(db)
    if review:
        out.append(Outgoing(review, "review"))
    db.set_setting(EVENING_DONE, day)
    return out


def find_top_pick(db: Database, scan: Any) -> Any | None:
    """The day's top pick, or None. Applies every rule, including events and the no-repeat rule."""
    overheated = scan.regime.overheated
    first = [item for item in scan.buys if not rules.top_pick_failures(item, overheated, [])]
    price_events = {item.symbol: holding_window_events(db, item.symbol, scan.as_of, item.close)[1] for item in first}
    allowed = [item for item in first if _may_alert(db, item.symbol, scan.as_of, rules.plan_fingerprint(item.verdict, item.plan))]
    return rules.choose_top_pick(allowed, overheated, price_events)


def early_review(db: Database) -> str | None:
    """After 20 finished early alerts: say so once if they did worse than the end-of-day signals."""
    needed = int(alert_setting("early", "review_after_signals", default=20))
    if db.get_setting("alerts.early_review_done"):
        return None
    rows = journal.live_results(db)
    early = next((row for row in rows if row["setup"] == journal.EARLY_SETUP), None)
    if early is None or early["finished"] < needed:
        return None
    others = [row for row in rows if row["setup"] != journal.EARLY_SETUP and row["finished"]]
    other_done = sum(row["finished"] for row in others)
    other_average = sum(row["net_profit"] for row in others) / other_done if other_done else None
    early_average = early["net_profit"] / early["finished"]
    factor = early["profit_factor"]
    worse = (factor is not None and factor < 1.0) or (other_average is not None and early_average < other_average)
    db.set_setting("alerts.early_review_done", utc_now_text()[:10])
    compare = f" End-of-day signals: Rs {other_average:+,.0f} per trade over {other_done} finished." if other_average is not None else ""
    head = f"📊 <b>Early alerts after {early['finished']} finished signals</b>\nRs {early_average:+,.0f} per trade, {early['wins']} won, profit factor {factor}.{compare}"
    if worse:
        return head + "\n⚠️ Early alerts are doing WORSE than waiting for the close. Suggestion: switch them off with /settings alerts early off."
    return head + "\nEarly alerts are holding up against the end-of-day signals so far. They stay on."


# -------------------------------------------------------------- early alerts
def load_frame(db: Database, symbol: str, as_of: str) -> pd.DataFrame:
    """One stock's adjusted bars with indicators, in today's prices (as the engine sees them)."""
    start = (date.fromisoformat(as_of) - timedelta(days=640)).isoformat()
    frame = db.frame(
        "SELECT date, COALESCE(adj_open, open) AS open, COALESCE(adj_high, high) AS high, COALESCE(adj_low, low) AS low, "
        "COALESCE(adj_close, close) AS close, close AS raw_close, COALESCE(turnover, 0) AS turnover, COALESCE(adj_factor, 1) AS adj_factor "
        "FROM daily_prices WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
        (symbol, start, as_of),
    )
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index("date")
    factor = float(frame["adj_factor"].iloc[-1]) or 1.0
    if factor != 1.0:
        frame[["open", "high", "low", "close"]] = frame[["open", "high", "low", "close"]] / factor
    return add_indicators(frame)


def early_plan(frame: pd.DataFrame, setup_key: str, setup_name: str, trigger: float, live_price: float, today: str,
               profile: Profile, fees: FeeSchedule) -> TradePlan | PlanRejection:
    """The plan for buying NOW at the live price, after the trigger was cleared.

    The buy range runs from the trigger up to "don't buy above" (trigger + 4%); the stop is the
    usual 2.5 x average daily range under the live price.
    """
    bar = frame.iloc[-1].copy()
    for column in ("open", "high", "low", "close", "raw_close"):
        bar[column] = live_price
    extended = pd.concat([frame, pd.DataFrame([bar.to_numpy()], columns=frame.columns, index=[pd.Timestamp(today)])]).astype(float)
    ceiling = trigger * (1 + float(alert_setting("early", "max_above_trigger_percent", default=4)) / 100.0)
    signal = SetupSignal(
        key=setup_key, name=setup_name, entry_low=trigger, entry_high=ceiling, entry_ref=live_price, invalidation=trigger,
        invalidation_text="back below the trigger level", hold_text="",
        why=(f"Cleared its trigger level {trigger:,.0f} and held above it for 30+ minutes", "Volume is on pace for at least twice a normal day"),
        ignore_resistance_below=live_price,
    )
    return build_plan(signal, extended, profile, fees)


class EarlyWatcher:
    """Follows the watch-list triggers during the session and raises early alerts."""

    def __init__(self) -> None:
        self.day: str | None = None
        self.samples: dict[str, list[tuple[datetime, float]]] = {}
        self.volumes: dict[str, float | None] = {}

    def candidates(self, scan: Any) -> list[Any]:
        """Watch-list stocks with a price trigger whose setup may be alerted early."""
        proven_only = bool(alert_setting("early", "require_proven_setup", default=True))
        found = []
        for item in scan.by_verdict("WATCH"):
            if not item.trigger_level or not item.trigger_setup or item.flags or item.score < scan.regime.buy_threshold:
                continue
            if item.close >= item.trigger_level * 1.01:
                continue                                   # already above it at the last close: not a fresh break
            if proven_only and not evidence_module.for_setup(item.trigger_setup).proven:
                continue
            found.append(item)
        return found

    def _normal_volume(self, db: Database, symbol: str, as_of: str) -> float | None:
        if symbol not in self.volumes:
            value = db.scalar("SELECT AVG(volume) FROM (SELECT volume FROM daily_prices WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 20)", (symbol, as_of))
            self.volumes[symbol] = float(value) if value else None
        return self.volumes[symbol]

    def tick(self, engine: Any, scan: Any, quotes: dict[str, dict[str, float | None]], index_change: float | None, now: datetime,
             style: str = "trailing") -> list[Outgoing]:
        """One check during the session. Returns the early alerts to send now (and records them)."""
        db, calendar = engine.db, engine.calendar
        today = now.date().isoformat()
        if today != self.day:
            self.day, self.samples, self.volumes = today, {}, {}
        # The plans come from the last completed session: with older data nothing is alerted.
        if not calendar.is_market_open(now) or scan.as_of != calendar.previous_trading_day(now.date()).isoformat():
            return []
        candidates = self.candidates(scan)
        for item in candidates:
            quote = quotes.get(item.symbol)
            if quote and quote.get("price"):
                self.samples.setdefault(item.symbol, []).append((now, float(quote["price"])))
        if not enabled(db, "early") or not candidates:
            return []
        session_open = now.replace(hour=calendar.open_time.hour, minute=calendar.open_time.minute, second=0, microsecond=0)
        session_close = now.replace(hour=calendar.close_time.hour, minute=calendar.close_time.minute, second=0, microsecond=0)
        sent = int(db.scalar("SELECT COUNT(*) FROM alerts WHERE kind = 'early' AND day = ?", (today,)) or 0)
        limit = limit_for(today, price_limits())
        names = {item.symbol: item.setup_name for item in candidates}
        out: list[Outgoing] = []
        for item in sorted(candidates, key=lambda entry: entry.score, reverse=True):
            quote = quotes.get(item.symbol)
            if not quote or not quote.get("price"):
                continue
            live = float(quote["price"])
            previous = quote.get("previous_close") or item.close
            change = quote.get("change_percent")
            if change is None and previous:
                change = (live / float(previous) - 1.0) * 100.0
            state = rules.EarlyState(
                symbol=item.symbol, trigger=float(item.trigger_level), price=live, samples=tuple(self.samples.get(item.symbol, [])), now=now,
                session_open=session_open, session_close=session_close, volume_today=quote.get("volume"),
                normal_volume=self._normal_volume(db, item.symbol, scan.as_of), day_change_percent=change, price_limit_percent=limit,
                index_change_percent=index_change, sent_today=sent,
                already_alerted=bool(db.scalar("SELECT COUNT(*) FROM alerts WHERE kind = 'early' AND day = ? AND symbol = ?", (today, item.symbol))),
            )
            if rules.early_failures(state):
                continue
            setup_name = names.get(item.symbol) or evidence_setup_name(item.trigger_setup)
            plan = early_plan(load_frame(db, item.symbol, scan.as_of), item.trigger_setup, setup_name, float(item.trigger_level), live, today, scan.profile, engine.fees)
            if isinstance(plan, PlanRejection):
                continue
            detail = {"trigger": float(item.trigger_level), "stop": plan.stop, "price": live, "plan": plan.__dict__, **rules.plan_fingerprint("EARLY", plan)}
            if not log_alert(db, "early", item.symbol, today, status="pending", detail=detail):
                continue
            record_early_signal(db, item, plan, scan, today)
            lines, _ = holding_window_events(db, item.symbol, today, live)
            data = text.from_item("EARLY", item, scan, plan, events=lines, style=style, live_price=live, trigger=float(item.trigger_level))
            data.reasons = list(plan_reasons(item, plan))
            part = engine.fees.round_lot(plan.quantity * float(alert_setting("early", "first_part_fraction", default=0.5))) or plan.quantity
            out.append(Outgoing(text.format_alert(data, engine.fees), "early", item.symbol, (item.symbol, part, live)))
            sent += 1
        return out


def plan_reasons(item: Any, plan: TradePlan) -> list[str]:
    return [f"Cleared its trigger level and held 1%+ above it for 30+ minutes ({plan.setup_name})", "Volume is on pace for at least twice a normal day",
            *item.positives[:1]]


def evidence_setup_name(key: str) -> str:
    from nepse_bot.engine.setups import enabled_setups

    return next((setup.name for setup in enabled_setups() if setup.key == key), key)


def record_early_signal(db: Database, item: Any, plan: TradePlan, scan: Any, today: str) -> None:
    """Log an early alert in the live journal as its own row, so /report can compare it with end-of-day signals.

    It is followed like any other signal, bought on the alert day with a limit at the alert price.
    """
    payload = {"as_of": scan.as_of, "regime": scan.regime.label, "confidence": item.confidence, **plan.__dict__,
               "entry_style": "limit", "base_setup": plan.setup_key, "early": True, "alert_day": today}
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO signals (id, symbol, created_at, setup, verdict, score, entry, stop_loss, target_1, target_2, status, plan_json) "
            "VALUES (?, ?, ?, ?, 'EARLY', ?, ?, ?, ?, ?, 'open', ?) ON CONFLICT (id) DO NOTHING",
            (f"{today}:{item.symbol}:early", item.symbol, utc_now_text(), journal.EARLY_SETUP, item.score, plan.entry, plan.stop, plan.target_1,
             plan.target_2, json.dumps(payload, default=str)),
        )


# ------------------------------------------------------------ holdings alerts
def position_alerts(db: Database, views: list[Any], day: str, live_prices: dict[str, float] | None = None,
                    watchlist: list[str] | None = None) -> list[str]:
    """New alerts for the owner's holdings (each stock, each event, at most once a day). Records what it returns.

    News and events are also reported for the stocks on the personal watchlist.
    """
    lines: list[str] = []
    for view in views:
        key = f"pos_stop.{view.position_id}"
        stored = db.get_setting(key)
        live = live_prices.get(view.symbol) if live_prices is not None else None
        if live_prices is not None and live is None:
            continue
        for event, message in positions.position_events(view, live, float(stored) if stored else None):
            if not log_alert(db, "position", view.symbol, day, event):
                continue
            lines.append(message)
            if event == "raise_stop":
                db.set_setting(key, f"{view.trailing_stop:.1f}")
            if event == "target" and live_prices is None and view.target_reached and view.position_id is not None:
                positions.set_levels(db, view.position_id, None, view.target, day)     # the next reference target takes over
    if live_prices is not None:
        return lines
    held = [view.symbol for view in views]
    followed = held + [symbol for symbol in (watchlist or []) if symbol not in held]
    if not followed:
        return lines
    tag = lambda symbol: "" if symbol in held else "(watchlist) "  # noqa: E731
    for event in events.load_events(db, day, followed):
        if event.day == day and not event.routine and event.symbol and log_alert(db, "position", event.symbol, day, f"news_{event.kind}"):
            lines.append(f"📰 {tag(event.symbol)}{event.symbol}: {event.label} — {event.title[:120]}. Usual effect: {event.note}")
    for item in events.corporate_events(db, day, followed):
        # told once per event, however many days it stays on the calendar
        if log_alert(db, "position", item.symbol, item.closure_day or "pending", f"corp_{item.kind}"):
            lines.append(f"🗓 {tag(item.symbol)}{item.line()}")
    return lines
