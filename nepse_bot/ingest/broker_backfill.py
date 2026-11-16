"""Backfill of historical broker flow (who bought and sold, per symbol, broker and day).

The bot does this by itself in the background (see ``broker_worker.py``). By hand:

    python -m nepse_bot.ingest brokers --sample      # the 3 most recent missing days
    python -m nepse_bot.ingest brokers               # everything, newest day first

Safe to stop at any moment: a day is saved in one step (all of it or nothing), is never
downloaded twice, and every saved day is compared with the price table.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import date, timedelta
from typing import Any

from nepse_bot.db.store import Database, get_database
from nepse_bot.settings import bot_setting
from nepse_bot.sources.chukul import SOURCE, ChukulClient, aggregate_trades
from nepse_bot.sources.http import SourceError

logger = logging.getLogger(__name__)
DATASET = "floorsheet_history"      # source_runs: one line per downloaded day
CHECK = "floorsheet_check"          # source_runs: one line per day compared with the price table
HEARTBEAT = "broker_backfill.heartbeat"
RECENT_DAYS = 7                     # a day this young with nothing published is asked again later


def _setting(name: str, default: Any) -> Any:
    return bot_setting("ingest", "broker_backfill", name, default=default)


def since_date() -> str:
    return str(_setting("since", "2022-02-01"))


def missing_days(db: Database, since: str) -> list[str]:
    """Trading days without broker data, newest first. Days the source confirmed as empty are left out."""
    rows = db.query(
        """SELECT DISTINCT p.date AS date FROM daily_prices p
           WHERE p.date >= ?
             AND p.date NOT IN (SELECT DISTINCT date FROM broker_flow_daily)
             AND p.date NOT IN (SELECT date FROM source_runs WHERE source = ? AND dataset = ? AND ok = 1 AND rows = 0)
           ORDER BY p.date DESC""",
        (since, SOURCE, DATASET),
    )
    return [row["date"] for row in rows]


def volume_match_percent(db: Database, day: str) -> float | None:
    """Broker-flow volume as a percentage of the day's traded volume in the price table."""
    flow = db.scalar("SELECT SUM(buy_qty) FROM broker_flow_daily WHERE date = ?", (day,)) or 0
    prices = db.scalar(
        "SELECT SUM(volume) FROM daily_prices WHERE date = ? AND symbol IN (SELECT DISTINCT symbol FROM broker_flow_daily WHERE date = ?)",
        (day, day),
    ) or 0
    return round(100.0 * flow / prices, 1) if prices else None


def day_problem(db: Database, day: str, min_percent: float | None = None) -> str | None:
    """Why a saved day looks incomplete (None = it is complete).

    Complete = every share bought was also sold, and the broker volume covers the whole
    traded volume of the price table (at least ``complete_min_volume_percent``).
    """
    min_percent = float(_setting("complete_min_volume_percent", 99)) if min_percent is None else min_percent
    row = db.query(
        """SELECT SUM(buy_qty) AS buy, SUM(sell_qty) AS sell,
                  SUM(CASE WHEN symbol IN (SELECT symbol FROM daily_prices WHERE date = ?) THEN buy_qty ELSE 0 END) AS known
           FROM broker_flow_daily WHERE date = ?""",
        (day, day),
    )[0]
    buy, sell = float(row["buy"] or 0), float(row["sell"] or 0)
    if buy <= 0:
        return "no broker rows"
    if abs(buy - sell) > 0.5:
        return f"bought {buy:,.0f} shares but sold {sell:,.0f}"
    traded = float(db.scalar("SELECT SUM(volume) FROM daily_prices WHERE date = ?", (day,)) or 0)
    if traded > 0 and 100.0 * float(row["known"] or 0) / traded < min_percent:
        return f"covers only {100.0 * float(row['known'] or 0) / traded:.1f}% of the traded volume"
    return None


def incomplete_days(db: Database, since: str) -> list[str]:
    """Saved days that were never compared with the price table and turn out incomplete, newest first.

    Complete days are marked so they are looked at only once.
    """
    rows = db.query(
        """SELECT DISTINCT date FROM broker_flow_daily
           WHERE date >= ? AND date NOT IN (SELECT date FROM source_runs WHERE source = ? AND dataset = ?)
           ORDER BY date DESC""",
        (since, SOURCE, CHECK),
    )
    bad = []
    for row in rows:
        problem = day_problem(db, row["date"])
        if problem is None:
            db.record_source_run(SOURCE, CHECK, row["date"], True, 0, "complete")
        else:
            logger.info("Broker day %s is incomplete (%s); it will be downloaded again.", row["date"], problem)
            bad.append(row["date"])
    return bad


def download_day(db: Database, client: Any, day: str, today: date | None = None) -> dict[str, Any]:
    """Download one day and save it in one step. Returns {"status": stored | empty | waiting | kept, ...}.

    * stored  - saved (a day that was there but incomplete is replaced, never mixed);
    * empty   - the source has no trades for this day (remembered, not asked again);
    * waiting - a recent day the source has not published yet (asked again later);
    * kept    - what was already saved is at least as good as the new download.
    """
    today = today or date.today()
    recent = day >= (today - timedelta(days=RECENT_DAYS)).isoformat()
    trades = client.floorsheet_day(day)
    rows = aggregate_trades(day, trades)
    existing = float(db.scalar("SELECT SUM(buy_qty) FROM broker_flow_daily WHERE date = ?", (day,)) or 0)
    new_volume = sum(float(row["buy_qty"]) for row in rows)
    if existing and new_volume <= existing:
        if recent and not rows:
            return {"status": "waiting", "rows": 0, "trades": 0}
        db.record_source_run(SOURCE, CHECK, day, False, 0, f"kept as saved ({day_problem(db, day) or 'complete'}); the source has nothing better")
        return {"status": "kept", "rows": 0, "trades": len(trades)}
    if not rows:
        if recent:
            return {"status": "waiting", "rows": 0, "trades": 0}
        db.record_source_run(SOURCE, DATASET, day, True, 0, "no trades published for this day")
        return {"status": "empty", "rows": 0, "trades": 0}
    with db.transaction() as connection:        # all of the day or nothing
        if existing:
            connection.execute("DELETE FROM broker_flow_daily WHERE date = ?", (day,))
        stored = db.upsert("broker_flow_daily", rows, ("date", "symbol", "broker"))
    problem = day_problem(db, day)
    db.record_source_run(SOURCE, DATASET, day, True, stored, f"{len(trades)} trades, volume match {volume_match_percent(db, day)}%")
    db.record_source_run(SOURCE, CHECK, day, problem is None, stored, problem or "complete")
    if problem:
        logger.warning("Broker day %s differs from the price table: %s", day, problem)
    return {"status": "stored", "rows": stored, "trades": len(trades), "problem": problem}


def progress(db: Database, since: str | None = None) -> dict[str, Any]:
    """How far the download is: days wanted, days done, the oldest day so far."""
    since = since or since_date()
    total = int(db.scalar("SELECT COUNT(DISTINCT date) FROM daily_prices WHERE date >= ?", (since,)) or 0)
    stored = int(db.scalar(
        "SELECT COUNT(DISTINCT date) FROM broker_flow_daily WHERE date >= ? AND date IN (SELECT date FROM daily_prices)", (since,)) or 0)
    empty = int(db.scalar(
        """SELECT COUNT(*) FROM source_runs WHERE source = ? AND dataset = ? AND ok = 1 AND rows = 0 AND date >= ?
           AND date IN (SELECT date FROM daily_prices) AND date NOT IN (SELECT DISTINCT date FROM broker_flow_daily)""",
        (SOURCE, DATASET, since)) or 0)
    done = min(total, stored + empty)
    return {
        "total": total, "done": done, "stored": stored, "empty": empty, "remaining": max(0, total - done),
        "percent": round(100.0 * done / total, 1) if total else 0.0,
        "oldest": db.scalar("SELECT MIN(date) FROM broker_flow_daily WHERE date >= ?", (since,)),
        "differ": int(db.scalar("SELECT COUNT(*) FROM source_runs WHERE source = ? AND dataset = ? AND ok = 0", (SOURCE, CHECK)) or 0),
    }


# ------------------------------------------------------------ one downloader at a time
_last_beat = 0.0


def beat(db: Database, every_seconds: float = 0.0) -> bool:
    """Tell other processes that this one is downloading right now (written at most once per ``every_seconds``).

    Always returns False, so it can serve as the downloader's "should I stop?" question.
    """
    global _last_beat
    if time.time() - _last_beat >= every_seconds:
        _last_beat = time.time()
        db.set_setting(HEARTBEAT, f"{os.getpid()}|{_last_beat:.0f}")
    return False


def other_downloader_running(db: Database, max_age_seconds: float = 90) -> bool:
    """True when another process (the bot, or a download started by hand) was downloading in the last 90 seconds."""
    try:
        pid, stamp = str(db.get_setting(HEARTBEAT) or "").split("|")
        return int(pid) != os.getpid() and time.time() - float(stamp) < max_age_seconds
    except ValueError:
        return False


def run_broker_backfill(since: str | None = None, limit: int | None = None, db: Database | None = None,
                        client: ChukulClient | None = None, echo: Any = print) -> dict[str, Any]:
    """The download by hand. The bot does the same in the background; only one of them works at a time."""
    db = db or get_database()
    since = since or since_date()
    if other_downloader_running(db):
        echo("The bot is already downloading the broker history in the background. Nothing to do: see /status.")
        return {"skipped": "another download is running", **progress(db, since)}
    client = client or ChukulClient(workers=int(_setting("workers", 3)), pause_seconds=float(_setting("pause_seconds", 0.2)),
                                    should_stop=lambda: beat(db, 30))
    days = incomplete_days(db, since) + missing_days(db, since)
    if limit:
        days = days[:limit]
    summary = {"days_wanted": len(days), "days_stored": 0, "days_empty": 0, "days_failed": 0, "rows": 0, "trades": 0}
    started = time.time()
    failures_in_a_row = 0
    min_free = float(bot_setting("reliability", "min_free_mb_for_downloads", default=1500))
    for index, day in enumerate(days, start=1):
        if shutil.disk_usage(db.path).free / 1_000_000 < min_free:
            echo(f"Stopping: less than {min_free:,.0f} MB free on the disk. Free some space, then run the same command again.")
            summary["stopped_for_disk_space"] = True
            break
        beat(db)
        try:
            result = download_day(db, client, day)
        except SourceError as exc:
            summary["days_failed"] += 1
            failures_in_a_row += 1
            db.record_source_run(SOURCE, DATASET, day, False, 0, str(exc))
            logger.warning("broker backfill %s failed: %s", day, exc)
            if failures_in_a_row >= 5:
                echo("Stopping: 5 days failed in a row. Run the same command again later.")
                break
            continue
        failures_in_a_row = 0
        if result["status"] == "stored":
            summary["days_stored"] += 1
            summary["rows"] += result["rows"]
            summary["trades"] += result["trades"]
        elif result["status"] == "empty":
            summary["days_empty"] += 1
        if index % 10 == 0 or index == len(days):
            minutes = (time.time() - started) / 60
            echo(f"{index}/{len(days)} days, {summary['trades']:,} trades, {minutes:.0f} min, at {day}")
    db.set_setting(HEARTBEAT, "")
    summary["minutes"] = round((time.time() - started) / 60, 1)
    summary["first_day"] = db.scalar("SELECT MIN(date) FROM broker_flow_daily")
    summary["days_in_database"] = db.scalar("SELECT COUNT(DISTINCT date) FROM broker_flow_daily")
    return summary
