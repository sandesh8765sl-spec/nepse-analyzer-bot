"""Protection mode: after a losing streak or a portfolio drop, warn and pause TOP PICK alerts.

Triggers (numbers in config/alerts.yaml -> protection):
* the last 3 recorded sales (/sold) all lost money, or
* the portfolio is 8% or more below its highest value.

Then the bot sends a warning and holds back TOP PICK alerts for 5 trading sessions.
Nothing else changes: /top10, /buy, position alerts and BUY alerts from proven setups keep working.

Portfolio value = trading capital (/settings) + every realised result + the open positions'
gain or loss at the last close (before selling costs).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from nepse_bot.alerts.config import alert_setting
from nepse_bot.portfolio import positions

UNTIL, REASON, PEAK, LAST_STREAK_SALE = "protection.until", "protection.reason", "protection.peak", "protection.streak_sale"


def _cfg(key: str, default: float) -> float:
    return float(alert_setting("protection", key, default=default))


def losing_streak(db: Any) -> tuple[int, int | None]:
    """How many of the most recent sales lost money in a row, and the id of the newest sale."""
    rows = db.query("SELECT id, notes FROM trades WHERE side = 'sell' ORDER BY trade_date DESC, id DESC LIMIT 20")
    streak = 0
    for row in rows:
        try:
            realised = float(json.loads(row["notes"] or "{}").get("realised", 0.0))
        except (TypeError, ValueError):
            realised = 0.0
        if realised >= 0:
            break
        streak += 1
    return streak, (rows[0]["id"] if rows else None)


def portfolio_value(db: Any, capital: float, day: str) -> float:
    realised = sum(row["realised"] for row in positions.realised_between(db, "1900-01-01", day))
    unrealised = 0.0
    for row in positions.open_positions(db):
        close = db.scalar("SELECT close FROM daily_prices WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1", (row["symbol"], day))
        if close and row["avg_price"]:
            unrealised += float(row["quantity"]) * (float(close) - float(row["avg_price"]))
    return float(capital) + realised + unrealised


def paused_until(db: Any, day: str) -> str | None:
    """The last trading day of the current pause, or None when TOP PICK alerts are allowed."""
    until = db.get_setting(UNTIL)
    return until if until and day <= until else None


def status_line(db: Any, day: str) -> str | None:
    until = paused_until(db, day)
    if not until:
        return None
    return f"🛡 Protection mode: TOP PICK alerts are paused until {until} ({db.get_setting(REASON) or 'recent losses'})."


def check(db: Any, day: str, calendar: Any, capital: float) -> str | None:
    """Run once per evening. Returns the warning text when protection mode is newly triggered."""
    reasons: list[str] = []
    need = int(_cfg("losing_trades_in_a_row", 3))
    streak, newest = losing_streak(db)
    if need > 0 and streak >= need and newest is not None and str(newest) != db.get_setting(LAST_STREAK_SALE):
        reasons.append(f"your last {streak} sales all lost money")
        db.set_setting(LAST_STREAK_SALE, str(newest))

    value = portfolio_value(db, capital, day)
    peak = max(float(db.get_setting(PEAK) or 0.0), value)
    drop_limit = _cfg("portfolio_drop_percent", 8)
    drop = 100.0 * (1.0 - value / peak) if peak > 0 else 0.0
    if drop_limit > 0 and drop >= drop_limit:
        reasons.append(f"the portfolio is {drop:.1f}% below its highest value (Rs {value:,.0f} against Rs {peak:,.0f})")
        peak = value            # a further drop of the same size from here triggers again
    db.set_setting(PEAK, f"{peak:.2f}")
    if not reasons:
        return None

    sessions = max(1, int(_cfg("pause_sessions", 5)))
    until = date.fromisoformat(day)
    for _ in range(sessions - 1):
        until = calendar.next_trading_day(until)
    reason = " and ".join(reasons)
    db.set_setting(UNTIL, until.isoformat())
    db.set_setting(REASON, reason)
    return (
        "🛡 <b>Protection mode is ON</b>\n"
        f"Why: {reason}.\n"
        f"TOP PICK alerts are paused for {sessions} trading sessions (until {until.isoformat()}).\n"
        "What still works: /top10, /buy, alerts for your holdings, and BUY alerts from proven setups.\n"
        "Suggestion: take no new speculative position until the pause ends, and review the losing trades with /report."
    )
