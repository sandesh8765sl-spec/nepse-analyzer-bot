"""The live journal: what happened to every BUY signal the bot gave.

Each logged signal is followed day by day with the daily high and low, using
the same simulation as the backtest (``nepse_bot.backtest.simulate``), so live
results and backtest results are measured the same way.

Outcomes: ``not_filled`` (the price never came to the entry), ``stop``,
``target_1`` (half sold at target 1, the rest trailed), ``target_2`` (target 1
hit and the price also reached target 2), ``time_stop`` or still ``open``.

If a setup's live profit factor over its last 20 finished signals falls below
1.0 it is switched off automatically and the owner is told. The owner can
switch it back on with ``/report enable <setup>``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from nepse_bot.backtest.simulate import PlanSpec, prepare_bars, simulate_plan
from nepse_bot.db.store import Database, utc_now_text
from nepse_bot.engine.config import set_disabled_setups, setting
from nepse_bot.portfolio.fees import FeeSchedule, fee_schedule
from nepse_bot.settings import MARKET_CONFIG_PATH, load_yaml
from nepse_bot.utils.health import queue_admin_alert

logger = logging.getLogger(__name__)

DISABLED_PREFIX = "setup_disabled."
EARLY_SETUP = "early_alert"       # early alerts are tracked as their own row in the journal
FINISHED = ("stop", "target_1", "target_2", "time_stop", "trail", "trail_stop", "breakeven_stop", "max_hold")


def _history(db: Database, symbol: str) -> pd.DataFrame:
    frame = db.frame(
        "SELECT date, COALESCE(adj_open, open) AS open, COALESCE(adj_high, high) AS high, COALESCE(adj_low, low) AS low, "
        "COALESCE(adj_close, close) AS close, COALESCE(adj_factor, 1) AS adj_factor FROM daily_prices WHERE symbol = ? ORDER BY date",
        (symbol,),
    )
    frame["date"] = pd.to_datetime(frame["date"])
    return prepare_bars(frame.set_index("date"))


def _reached(bars: pd.DataFrame, signal_date: str, result: dict[str, Any], level: float) -> bool:
    """Did the price (in the signal day's terms) reach ``level`` between entry and exit?"""
    factor = float(bars.loc[pd.Timestamp(signal_date), "adj_factor"]) or 1.0
    held = bars.loc[pd.Timestamp(result["entry_date"]): pd.Timestamp(result["exit_date"])]
    return bool(len(held)) and float(held["high"].max()) / factor >= level


def update_journal(db: Database, fees: FeeSchedule | None = None) -> dict[str, int]:
    """Re-evaluate every open signal with the latest daily bars. Returns counts by new status."""
    fees = fees or fee_schedule()
    sell_after = int(load_yaml(MARKET_CONFIG_PATH).get("earliest_sell_after_days", 2))
    counts: dict[str, int] = {}
    histories: dict[str, pd.DataFrame] = {}
    updates = []
    for row in db.query("SELECT id, symbol, plan_json FROM signals WHERE status = 'open'"):
        try:
            plan = json.loads(row["plan_json"] or "{}")
            symbol = row["symbol"]
            if symbol not in histories:
                histories[symbol] = _history(db, symbol)
            bars = histories[symbol]
            result = simulate_plan(bars, plan["as_of"], PlanSpec.from_mapping(plan), fees, sell_after)
        except Exception as exc:  # one bad row must not stop the journal
            logger.warning("Journal could not evaluate %s: %s", row["id"], exc)
            continue
        status = result["status"]
        if status == "no_data" or result.get("pending"):
            continue
        if status == "target_1" and plan.get("target_2") and _reached(bars, plan["as_of"], result, float(plan["target_2"])):
            status = "target_2"
        plan["outcome"] = {**result, "status": status}
        closed = status != "open"
        updates.append(
            {
                "id": row["id"], "status": status, "outcome_price": result.get("exit_price") if closed else None,
                "closed_at": (result.get("exit_date") or utc_now_text()[:10]) if closed else None,
                "plan_json": json.dumps(plan, default=str),
            }
        )
        counts[status] = counts.get(status, 0) + 1
    if updates:
        with db.transaction() as connection:
            connection.executemany(
                "UPDATE signals SET status = :status, outcome_price = :outcome_price, closed_at = :closed_at, plan_json = :plan_json WHERE id = :id",
                updates,
            )
    return counts


def _profit_factor(profits: list[float]) -> float | None:
    """Money won divided by money lost (None when there is nothing to judge)."""
    if not profits:
        return None
    won = sum(value for value in profits if value > 0)
    lost = -sum(value for value in profits if value <= 0)
    return round(won / lost, 2) if lost > 0 else float("inf")


def live_results(db: Database) -> list[dict[str, Any]]:
    """Live results per setup, from the journal."""
    window = int(setting("backtest", "live_disable_after_signals", default=20))
    by_setup: dict[str, dict[str, Any]] = {}
    for row in db.query("SELECT setup, status, closed_at, plan_json FROM signals ORDER BY closed_at, id"):
        entry = by_setup.setdefault(row["setup"] or "unknown", {"signals": 0, "open": 0, "not_filled": 0, "profits": []})
        entry["signals"] += 1
        if row["status"] == "open":
            entry["open"] += 1
        elif row["status"] == "not_filled":
            entry["not_filled"] += 1
        elif row["status"] in FINISHED:
            try:
                entry["profits"].append(float(json.loads(row["plan_json"])["outcome"]["net_profit"]))
            except (KeyError, TypeError, ValueError):
                continue
    disabled = disabled_setups(db)
    rows = []
    for setup, entry in sorted(by_setup.items()):
        profits = entry["profits"]
        rows.append(
            {
                "setup": setup, "signals": entry["signals"], "open": entry["open"], "not_filled": entry["not_filled"],
                "finished": len(profits), "wins": sum(1 for value in profits if value > 0), "net_profit": round(sum(profits), 0),
                "profit_factor": _profit_factor(profits), "recent_count": len(profits[-window:]),
                "recent_profit_factor": _profit_factor(profits[-window:]), "disabled": disabled.get(setup),
            }
        )
    return rows


# ------------------------------------------------------------- auto-disable
def disabled_setups(db: Database) -> dict[str, str]:
    """Setups switched off by the journal: key -> reason."""
    rows = db.query("SELECT key, value FROM settings WHERE key LIKE ? AND value IS NOT NULL AND value != ''", (DISABLED_PREFIX + "%",))
    return {row["key"][len(DISABLED_PREFIX):]: row["value"] for row in rows}


def sync_disabled(db: Database) -> None:
    """Tell the engine which setups the journal has switched off."""
    set_disabled_setups(disabled_setups(db))


def enable_setup(db: Database, setup: str) -> bool:
    """Switch an auto-disabled setup back on. Returns False if it was not disabled."""
    if setup not in disabled_setups(db):
        return False
    db.set_setting(DISABLED_PREFIX + setup, "")
    sync_disabled(db)
    return True


def check_auto_disable(db: Database) -> list[str]:
    """Switch off any setup whose recent live results are losing money. Returns the setups switched off now."""
    window = int(setting("backtest", "live_disable_after_signals", default=20))
    floor = float(setting("backtest", "live_min_profit_factor", default=1.0))
    switched_off = []
    for row in live_results(db):
        recent = row["recent_profit_factor"]
        if row["setup"] == EARLY_SETUP:
            continue                                   # early alerts are reviewed separately (nepse_bot.alerts.service)
        if row["disabled"] or row["recent_count"] < window or recent is None or recent >= floor:
            continue
        reason = f"{utc_now_text()[:10]}: live profit factor {recent} over the last {window} finished signals (needs {floor:g})"
        db.set_setting(DISABLED_PREFIX + row["setup"], reason)
        switched_off.append(row["setup"])
        queue_admin_alert(
            f"Setup '{row['setup']}' has been switched OFF automatically: its last {window} finished signals lost money "
            f"(profit factor {recent}). No new BUY signals will come from it. See /report. "
            f"To switch it back on: /report enable {row['setup']}"
        )
    sync_disabled(db)
    return switched_off


def run_daily(db: Database) -> str:
    """Update the journal and apply the auto-disable rule. Returns one line for the update report."""
    counts = update_journal(db)
    switched_off = check_auto_disable(db)
    parts = [f"{count} {status}" for status, count in sorted(counts.items())] or ["nothing to update"]
    text = "journal: " + ", ".join(parts)
    if switched_off:
        text += f"; switched off: {', '.join(switched_off)}"
    return text
