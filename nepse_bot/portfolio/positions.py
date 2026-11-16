"""Position manager: the owner's own holdings (Stage 5).

The owner tells the bot what was bought and sold (``/bought``, ``/sold``); the
bot never places an order. For every open position it works out, after each
close: the trailing stop, the profit or loss after fees and tax, and one piece
of advice - HOLD, TRIM or SELL - with the reasons. It also checks the whole
portfolio against the risk rules in config/engine.yaml (``portfolio_risk``).

Positions live in the ``positions`` and ``trades`` tables. Nothing is ever
deleted: a sold or removed position is kept with status ``closed``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd

from nepse_bot.db.store import Database
from nepse_bot.engine.config import Profile, setting
from nepse_bot.engine.indicators import atr
from nepse_bot.engine.plan import resistances_above
from nepse_bot.events.engine import ClosureItem
from nepse_bot.portfolio.fees import FeeSchedule, TradeResult

ACTION_ORDER = {"SELL": 0, "TRIM": 1, "HOLD": 2}


def risk_setting(key: str, default: float) -> float:
    return float(setting("portfolio_risk", key, default=default))


# ------------------------------------------------------------------ storage
def open_positions(db: Database) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM positions WHERE status = 'open' ORDER BY symbol")


def get_position(db: Database, symbol: str) -> dict[str, Any] | None:
    rows = db.query("SELECT * FROM positions WHERE status = 'open' AND symbol = ?", (symbol,))
    return rows[0] if rows else None


def _meta(position: dict[str, Any]) -> dict[str, Any]:
    """Extra facts kept in the ``notes`` column as JSON (older rows hold plain text: ignored)."""
    try:
        value = json.loads(position.get("notes") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def record_buy(db: Database, symbol: str, quantity: int, price: float, day: str | None, fees: FeeSchedule,
               stop: float | None = None, target_1: float | None = None, target_2: float | None = None,
               levels_from: str | None = None) -> dict[str, Any]:
    """Save a purchase. A second purchase of the same stock is averaged into the open position.

    ``day`` is the real purchase date (None = unknown: the owner is asked for it). ``levels_from``
    is the date the stored stop and target were worked out on (today, also for an older purchase).
    """
    amount = quantity * price
    cost = fees.commission(amount) + fees.sebon_fee(amount)
    existing = get_position(db, symbol)
    with db.transaction() as connection:
        if existing:
            total = existing["quantity"] + quantity
            average = (existing["quantity"] * existing["avg_price"] + amount) / total
            # The holding period for tax counts from the earliest purchase.
            first = min(day, existing["opened_at"]) if day and existing["opened_at"] else existing["opened_at"]
            connection.execute("UPDATE positions SET quantity = ?, avg_price = ?, opened_at = ? WHERE id = ?", (total, average, first, existing["id"]))
            position_id = existing["id"]
        else:
            notes = json.dumps({"levels_from": levels_from or day, **({} if day else {"date_unknown": True})})
            cursor = connection.execute(
                "INSERT INTO positions (symbol, opened_at, quantity, avg_price, stop_loss, target_1, target_2, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (symbol, day, quantity, price, stop, target_1, target_2, notes),
            )
            position_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO trades (position_id, symbol, side, trade_date, quantity, price, fees, tax, net_amount) VALUES (?, ?, 'buy', ?, ?, ?, ?, 0, ?)",
            (position_id, symbol, day, quantity, price, round(cost, 2), round(-(amount + cost), 2)),
        )
    return get_position(db, symbol) or {}


def record_sell(db: Database, symbol: str, quantity: int, price: float, day: str, fees: FeeSchedule) -> dict[str, Any] | None:
    """Save a sale and return its result after fees and tax (None if there is no such position)."""
    position = get_position(db, symbol)
    if position is None:
        return None
    quantity = int(min(quantity, position["quantity"]))
    # Real holding days decide the tax rate (more than 365 days = the long-term rate).
    held_days = max(0, (date.fromisoformat(day) - date.fromisoformat(position["opened_at"] or day)).days)
    result = fees.round_trip(quantity, position["avg_price"], price, held_days)
    amount = quantity * price
    sell_costs = fees.commission(amount) + fees.sebon_fee(amount) + fees.dp_charge
    remaining = position["quantity"] - quantity
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO trades (position_id, symbol, side, trade_date, quantity, price, fees, tax, net_amount, notes) VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?)",
            (position["id"], symbol, day, quantity, price, round(sell_costs, 2), round(result.tax, 2), round(amount - sell_costs - result.tax, 2),
             json.dumps({"realised": round(result.net_profit, 2), "held_days": held_days, "avg_price": position["avg_price"]})),
        )
        if remaining <= 0:
            connection.execute("UPDATE positions SET quantity = 0, status = 'closed', closed_at = ? WHERE id = ?", (day, position["id"]))
        else:
            connection.execute("UPDATE positions SET quantity = ? WHERE id = ?", (remaining, position["id"]))
    return {"quantity": quantity, "remaining": remaining, "held_days": held_days, "result": result, "tax_percent": fees.cgt_percent(held_days),
            "date_unknown": not position["opened_at"]}


def set_purchase_date(db: Database, symbol: str, day: str) -> bool:
    """Record the real purchase date of an open position (used for tax and for the trailing stop)."""
    position = get_position(db, symbol)
    if position is None:
        return False
    meta = {key: value for key, value in _meta(position).items() if key != "date_unknown"}
    with db.transaction() as connection:
        connection.execute("UPDATE positions SET opened_at = ?, notes = ? WHERE id = ?", (day, json.dumps(meta), position["id"]))
        connection.execute("UPDATE trades SET trade_date = ? WHERE position_id = ? AND side = 'buy' AND trade_date IS NULL", (day, position["id"]))
    return True


def import_legacy_holdings(db: Database, holdings: dict[str, tuple[float, float]], fees: FeeSchedule) -> list[str]:
    """Copy holdings saved by the old /portfolio command (symbol -> quantity, price). Their purchase
    date is unknown, so it is left empty and the owner is asked for it. Runs once."""
    if db.get_setting("legacy_holdings_imported"):
        return []
    added = []
    for symbol, (quantity, price) in holdings.items():
        if quantity > 0 and price > 0 and get_position(db, symbol) is None:
            record_buy(db, symbol, int(quantity), float(price), None, fees)
            added.append(symbol)
    db.set_setting("legacy_holdings_imported", "yes")
    return added


def set_levels(db: Database, position_id: int, stop: float | None, target: float | None, day: str) -> None:
    """Store a new reference target (after the old one was reached) without touching anything else."""
    row = db.query("SELECT * FROM positions WHERE id = ?", (position_id,))
    if not row:
        return
    meta = {**_meta(row[0]), "levels_from": day}
    with db.transaction() as connection:
        connection.execute("UPDATE positions SET stop_loss = COALESCE(?, stop_loss), target_1 = ?, notes = ? WHERE id = ?", (stop, target, json.dumps(meta), position_id))


def remove_position(db: Database, symbol: str, day: str) -> bool:
    """Close a position that was entered by mistake. The row is kept, marked as removed."""
    position = get_position(db, symbol)
    if position is None:
        return False
    with db.transaction() as connection:
        connection.execute("UPDATE positions SET status = 'closed', closed_at = ?, notes = 'removed by owner (no sale recorded)' WHERE id = ?", (day, position["id"]))
    return True


def has_sold_part(db: Database, position_id: int) -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM trades WHERE position_id = ? AND side = 'sell'", (position_id,)))


def realised_between(db: Database, start: str, end: str) -> list[dict[str, Any]]:
    """Sales recorded between two dates, with the realised result of each."""
    rows = db.query("SELECT symbol, trade_date, quantity, price, notes FROM trades WHERE side = 'sell' AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date", (start, end))
    for row in rows:
        try:
            row["realised"] = float(json.loads(row["notes"] or "{}").get("realised", 0.0))
        except (TypeError, ValueError):
            row["realised"] = 0.0
    return rows


def bars_for(db: Database, symbol: str) -> pd.DataFrame:
    """Adjusted daily bars with the average daily range, indexed by date text."""
    frame = db.frame(
        "SELECT date, COALESCE(adj_high, high) AS high, COALESCE(adj_low, low) AS low, COALESCE(adj_close, close) AS close, "
        "COALESCE(adj_factor, 1) AS adj_factor FROM daily_prices WHERE symbol = ? ORDER BY date",
        (symbol,),
    ).set_index("date")
    frame["atr14"] = atr(frame, 14)
    return frame


# ------------------------------------------------------------------- advice
@dataclass
class PositionView:
    """One open position with everything the reports show."""

    symbol: str
    sector: str
    quantity: int
    avg_price: float
    opened_at: str
    days_held: int
    sessions_held: int
    close: float
    change_today: float              # rupees gained or lost on the last session
    value: float
    net: TradeResult                 # result if sold at the last close, after fees and tax
    trailing_stop: float
    stop_distance_pct: float
    given_back_at_stop: float        # rupees of today's value lost if the trailing stop is hit
    target: float | None             # reference target = partial-profit level: next major resistance above the price
    target_reached: float | None = None   # the stored reference target, when the price has now reached it
    highest: float = 0.0             # highest price since purchase
    date_unknown: bool = False       # purchase date not given yet
    position_id: int | None = None
    action: str = "HOLD"
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    adjusted: bool = False           # a bonus or right issue changed the share count since purchase


def initial_stop(entry: float, atr_value: float | None) -> float:
    """The Stage 4b stop: 2.5 x the average daily range under the entry, between 4% and 12.5%."""
    multiple = float(setting("plan", "stop_atr_multiple", default=2.5))
    low, high = float(setting("plan", "stop_min_percent", default=4)), float(setting("plan", "stop_max_percent", default=12.5))
    percent = multiple * atr_value / entry * 100.0 if atr_value and atr_value > 0 else low
    return entry * (1 - min(high, max(low, percent)) / 100.0)


def trailing_stop(held: pd.DataFrame, start_stop: float | None) -> float:
    """Highest price since purchase minus 3 x the average daily range; it never moves down.

    ``start_stop`` is the plan's own stop for a fresh position. An older holding has none: its
    stop comes only from the prices since the purchase, never from the buy price.
    """
    if held.empty:
        return float(start_stop or 0.0)
    multiple = float(setting("plan", "chandelier_atr_multiple", default=3.0))
    levels = held["high"].cummax() - multiple * held["atr14"].fillna(held["close"] * 0.03)
    return max(float(start_stop or 0.0), float(levels.max()))


def _factor_at(known: pd.DataFrame, day: str) -> float:
    """Multiply a price quoted on ``day`` by this to bring it into today's (bonus-adjusted) terms."""
    before = known[known.index <= day]
    factor = float((before if len(before) else known)["adj_factor"].iloc[-1 if len(before) else 0]) / float(known["adj_factor"].iloc[-1] or 1.0)
    return factor if factor > 0 else 1.0


def next_reference_target(known: pd.DataFrame, price: float) -> float | None:
    """The next major resistance above a price, from the bars up to today."""
    if len(known) < 30:
        return None
    try:
        levels = resistances_above(known, price)
    except Exception:
        return None
    return round(float(levels[0][0]), 1) if levels else None


def review_position(position: dict[str, Any], bars: pd.DataFrame, *, today: str, style: str, fees: FeeSchedule, profile: Profile,
                    sector: str = "", closures: list[ClosureItem] | None = None, verdict: str | None = None,
                    verdict_reasons: list[str] | None = None, flags: list[str] | None = None, sold_part: bool = False) -> PositionView | None:
    """Work out the state of one position and the advice for it. Pure: no database, no clock."""
    known = bars[bars.index <= today]
    if known.empty:
        return None
    date_unknown = not position.get("opened_at")
    opened = position.get("opened_at") or today
    levels_from = _meta(position).get("levels_from") or opened
    held = known[known.index >= opened]
    # Prices are adjusted for bonus and right shares, so bring the purchase into today's terms.
    at_entry = known[known.index <= opened]
    factor = _factor_at(known, opened)
    level_factor = _factor_at(known, levels_from)
    entry = float(position["avg_price"]) * factor
    quantity = int(round(float(position["quantity"]) / factor))
    close = float(known["close"].iloc[-1])
    previous = float(known["close"].iloc[-2]) if len(known) > 1 else close
    days_held = max(0, (date.fromisoformat(today) - date.fromisoformat(opened)).days)
    sessions = max(0, len(held) - 1)
    net = fees.round_trip(quantity, entry, close, days_held)

    # ---- trailing stop: from the highest price since purchase and today's average daily range.
    # Only a fresh position (bought within the last few sessions) starts from the plan's stop.
    stored_stop = position.get("stop_loss")
    entry_atr = at_entry["atr14"].iloc[-1] if len(at_entry) else None
    if stored_stop:
        start: float | None = float(stored_stop) * level_factor
    elif sessions <= 3 or held.empty:
        start = initial_stop(entry if not held.empty else close, None if entry_atr is None or pd.isna(entry_atr) else float(entry_atr))
    else:
        start = None
    stop = trailing_stop(held, start)
    highest = float(held["high"].max()) if len(held) else close
    # ---- reference target (also the partial-profit level): the next major resistance above the
    # CURRENT price. A stored target is kept until the price reaches it, then the next one is used.
    stored_target = float(position["target_1"]) * level_factor if position.get("target_1") else None
    since_levels = known[known.index > levels_from]
    reached = stored_target if stored_target and len(since_levels) and float(since_levels["high"].max()) >= stored_target else None
    target = stored_target if stored_target and reached is None and stored_target > close else next_reference_target(known, close)
    value = quantity * close
    view = PositionView(
        symbol=position["symbol"], sector=sector, quantity=quantity, avg_price=entry, opened_at=opened, days_held=days_held,
        sessions_held=sessions, close=close, change_today=quantity * (close - previous) if sessions >= 1 else quantity * (close - entry),
        value=value, net=net, trailing_stop=stop, stop_distance_pct=(close - stop) / close * 100.0 if close else 0.0,
        given_back_at_stop=max(0.0, (close - stop) * quantity), target=target, adjusted=abs(factor - 1.0) > 1e-6,
        target_reached=reached, highest=highest, date_unknown=date_unknown, position_id=position.get("id"),
    )

    sell: list[str] = []
    trim: list[str] = []
    hold: list[str] = []
    max_hold = int(setting("plan", "max_hold_sessions", default=40))
    single_limit = risk_setting("max_single_position_percent", 25)
    if close < stop:
        sell.append(f"closed at {close:,.1f}, below the trailing stop {stop:,.1f}")
    if reached and style == "target":
        sell.append(f"reached your target {reached:,.1f} (exit style: target)")
    if reached and style == "hybrid" and not sold_part:
        trim.append(f"reached the reference target {reached:,.1f}: sell half, keep the trailing stop on the rest (exit style: hybrid)")
    if sessions >= max_hold:
        if net.net_profit <= 0:
            sell.append(f"held {sessions} sessions without a profit; the plan's limit is {max_hold}")
    if verdict == "AVOID":
        why = f" ({', '.join((verdict_reasons or [])[:2])})" if verdict_reasons else ""
        trim.append(f"the engine now rates it AVOID{why}")
    for flag in flags or []:
        if "OPERATOR" in flag.upper():
            trim.append("operator-risk warning on this stock")
    if profile.total_capital and value / profile.total_capital * 100.0 > single_limit:
        trim.append(f"it is {value / profile.total_capital * 100.0:.0f}% of your capital (limit {single_limit:g}%)")
    if not sell and not trim:
        hold.append(f"above the trailing stop {stop:,.1f} ({view.stop_distance_pct:.1f}% below the last close)")
        if reached:
            hold.append(f"reached the reference target {reached:,.1f}: the partial-profit level - consider selling half")
        if target:
            hold.append(f"{'next ' if reached else ''}reference target {target:,.1f} is {(target / close - 1) * 100:+.1f}% away")
        if sessions >= max_hold:
            hold.append(f"held {sessions} sessions, past the plan's {max_hold}: it is in profit, so the trailing stop is now the only exit rule")
    view.action = "SELL" if sell else "TRIM" if trim else "HOLD"
    view.reasons = sell + trim if sell else trim if trim else hold

    # ---- helpers: book closure and tax
    if date_unknown:
        view.notes.append(
            f"Purchase date unknown. Tell me: /bought date {view.symbol} YYYY-MM-DD. Until then the tax is shown at the short-term rate "
            "and the trailing stop only counts prices from today."
        )
    notice_days = int(risk_setting("book_closure_notice_days", 15))
    for item in closures or []:
        if item.symbol == view.symbol and item.trading_days_left <= notice_days:
            view.notes.append(
                f"Book closure {item.day} ({item.payout}), in {item.trading_days_left} trading day(s). To receive it, still hold the shares at the close of "
                f"{item.last_buy_day}. The price normally opens lower by about the payout afterwards"
                + (f" (about {item.price_after(close):,.0f} from today's {close:,.0f})." if item.price_after(close) else ".")
            )
    if net.tax > 0:
        to_long = fees.cgt_long_after_days + 1 - days_held
        if 0 < to_long <= int(risk_setting("long_term_tax_notice_days", 90)):
            saving = net.tax * (1 - fees.cgt_long_percent / fees.cgt_short_percent)
            when = (date.fromisoformat(today) + timedelta(days=to_long)).isoformat()
            view.notes.append(f"Tax: {fees.cgt_short_percent:g}% now (Rs {net.tax:,.0f}). From {when} ({to_long} days) the rate is {fees.cgt_long_percent:g}%: about Rs {saving:,.0f} less at this price.")
        else:
            view.notes.append(f"Tax if sold now: Rs {net.tax:,.0f} ({fees.cgt_percent(days_held):g}% of the gain).")
    elif net.net_profit < 0:
        view.notes.append("No capital gains tax on a loss. A loss cannot be set against gains on other stocks.")
    if view.adjusted:
        view.notes.append(f"Bonus/right shares since you bought: shown as {quantity:,} shares at an adjusted cost of {entry:,.1f}.")
    return view


def position_events(view: PositionView, live_price: float | None = None, last_notified_stop: float | None = None) -> list[tuple[str, str]]:
    """What the owner should be told about one holding: (event key, text). Pure.

    With ``live_price`` (during the session) only the price events are checked: stop hit, stop
    near, target reached. After the close the trailing stop and the time limit are checked too.
    """
    events: list[tuple[str, str]] = []
    price = live_price if live_price is not None else view.close
    when = "now trading at" if live_price is not None else "closed at"
    near = risk_setting("stop_near_percent", 2)
    stop = view.trailing_stop
    if stop > 0 and price <= stop:
        action = "The rule is to sell: place the sell order now or at the next session's open." if live_price is not None else "The rule is to sell at the next session."
        events.append(("stop_hit", f"🔴 {view.symbol} STOP HIT: {when} {price:,.1f}, at or below your trailing stop {stop:,.1f}. {action}"))
    elif stop > 0 and (price - stop) / price * 100.0 <= near:
        events.append(("stop_near", f"🟠 {view.symbol} is close to its stop: {when} {price:,.1f}, only {(price - stop) / price * 100.0:.1f}% above the trailing stop {stop:,.1f}. Make sure your stop-loss order is in."))
    half = max(10, int(view.quantity / 2 // 10 * 10)) if view.quantity >= 20 else view.quantity
    if live_price is not None:
        if view.target and price >= view.target:
            events.append(("target", f"🎯 {view.symbol} reached the reference target {view.target:,.1f} (now {price:,.1f}). This is the partial-profit level: consider selling {half:,} of your {view.quantity:,} shares and trailing the rest."))
        return events
    if view.target_reached:
        events.append(("target", f"🎯 {view.symbol} reached the reference target {view.target_reached:,.1f} (closed {view.close:,.1f}). This is the partial-profit level: consider selling {half:,} of your {view.quantity:,} shares and trailing the rest."
                       + (f" Next reference target: {view.target:,.1f}." if view.target else "")))
    if stop > 0 and price > stop and (last_notified_stop is None or stop > last_notified_stop * 1.003):
        was = f" (was {last_notified_stop:,.1f})" if last_notified_stop else ""
        events.append(("raise_stop", f"⬆️ {view.symbol}: move your stop-loss order up to {stop:,.1f}{was}. Highest price since you bought: {view.highest:,.1f}; last close {view.close:,.1f}."))
    max_hold = int(setting("plan", "max_hold_sessions", default=40))
    notice = int(risk_setting("time_limit_notice_sessions", 5))
    if max_hold - notice <= view.sessions_held < max_hold + 1 and not view.date_unknown:
        left = max(0, max_hold - view.sessions_held)
        verdict = "it is in profit, so after that only the trailing stop decides" if view.net.net_profit > 0 else "it is not in profit: the plan is to sell at the limit"
        events.append(("time_limit", f"⏳ {view.symbol}: held {view.sessions_held} of the plan's {max_hold} sessions ({left} left); {verdict}."))
    return events


# -------------------------------------------------------------- portfolio risk
def portfolio_risk(views: list[PositionView], profile: Profile, overheated: bool = False) -> list[str]:
    """Warnings when the holdings as a whole break a risk rule (empty list = all within the rules)."""
    lines: list[str] = []
    capital = profile.total_capital
    if not views or capital <= 0:
        return lines
    invested = sum(view.value for view in views)
    if len(views) > profile.max_open_positions:
        lines.append(f"{len(views)} open positions; your limit is {profile.max_open_positions}.")
    if invested > capital:
        lines.append(f"Holdings are worth Rs {invested:,.0f}, more than your trading capital of Rs {capital:,.0f} (update it with /settings capital).")
    sector_limit = risk_setting("max_sector_percent", 40)
    sectors: dict[str, float] = {}
    for view in views:
        sectors[view.sector or "unknown"] = sectors.get(view.sector or "unknown", 0.0) + view.value
    for sector, value in sorted(sectors.items(), key=lambda pair: -pair[1]):
        if value / capital * 100.0 > sector_limit:
            lines.append(f"{sector} is {value / capital * 100.0:.0f}% of your capital (limit {sector_limit:g}%): one bad sector day hits everything at once.")
    risk_limit = risk_setting("max_open_risk_percent", 6)
    open_risk = sum(view.given_back_at_stop for view in views)
    if open_risk / capital * 100.0 > risk_limit:
        lines.append(f"If every trailing stop were hit you would give back Rs {open_risk:,.0f} ({open_risk / capital * 100.0:.1f}% of capital; limit {risk_limit:g}%).")
    if overheated:
        lines.append("Market is overheated: avoid adding; at most 1 new position a day.")
    return lines


def new_position_warnings(views: list[PositionView], profile: Profile, symbol: str, sector: str, position_value: float) -> list[str]:
    """What the risk rules say about adding this stock to the current holdings."""
    lines: list[str] = []
    capital = profile.total_capital
    if capital <= 0:
        return lines
    if any(view.symbol == symbol for view in views):
        lines.append(f"You already hold {symbol}; buying more raises the risk on one stock.")
    elif len(views) >= profile.max_open_positions:
        lines.append(f"You already have {len(views)} open positions (your limit is {profile.max_open_positions}).")
    cash = capital - sum(view.value for view in views)
    if views and position_value > cash:
        lines.append(f"Only about Rs {max(0.0, cash):,.0f} of your capital is free; this plan needs Rs {position_value:,.0f}.")
    sector_limit = risk_setting("max_sector_percent", 40)
    sector_value = sum(view.value for view in views if view.sector == sector) + position_value
    if any(view.sector == sector for view in views) and sector_value / capital * 100.0 > sector_limit:
        lines.append(f"{sector} would be {sector_value / capital * 100.0:.0f}% of your capital (limit {sector_limit:g}%).")
    return lines
