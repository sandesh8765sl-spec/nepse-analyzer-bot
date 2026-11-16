"""One-off history download (safe to stop and re-run; it resumes).

Downloads, from ShareHub's data feed:

* the security list, all dividend and right-share records, index history;
* daily prices for every ordinary share (including suspended and delisted
  ones, so that backtests are not flattered by only looking at survivors);
* every quarterly report for the active companies.

Then it computes adjusted prices, quality flags, breadth and liquidity, and
cross-checks the stored closes against NEPSE's own records.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from nepse_bot.ingest.pipeline import Ingestor
from nepse_bot.sources.http import SourceError

logger = logging.getLogger(__name__)

DEFAULT_SINCE = "2019-01-01"


def backfill_prices(ingestor: Ingestor, since: str, symbols: list[str] | None = None) -> dict[str, int]:
    """Download price history per symbol. Returns counts of what happened."""
    db = ingestor.db
    if symbols is None:
        symbols = [
            row["symbol"]
            for row in db.query(
                "SELECT symbol FROM securities WHERE instrument_type = 'equity' AND is_promoter = 0 ORDER BY symbol"
            )
        ]
    counts = {"symbols": len(symbols), "done": 0, "skipped": 0, "empty": 0, "failed": 0, "rows": 0}
    last_completed = ingestor.calendar.last_completed_trading_day().isoformat()
    for position, symbol in enumerate(symbols, start=1):
        marker = f"backfill_prices:{symbol}"
        if db.get_setting(marker) == since:
            counts["skipped"] += 1
            continue
        try:
            # Only completed sessions: today's unfinished bar must never be stored.
            rows = [row for row in ingestor.sharehub.price_history(symbol, since=since) if row["date"] <= last_completed]
        except SourceError as exc:
            counts["failed"] += 1
            logger.warning("Backfill failed for %s: %s", symbol, exc)
            continue
        if rows:
            # Never overwrite a day already stored from NEPSE itself.
            have_official = {
                row["date"]
                for row in db.query("SELECT date FROM daily_prices WHERE symbol = ? AND source = 'NEPSE'", (symbol,))
            }
            counts["rows"] += db.upsert_prices([row for row in rows if row["date"] not in have_official], source="ShareHub")
            counts["done"] += 1
        else:
            counts["empty"] += 1
        db.set_setting(marker, since)
        if position % 25 == 0:
            logger.info("Backfill progress: %s of %s symbols, %s rows so far.", position, len(symbols), counts["rows"])
            print(f"  prices: {position}/{len(symbols)} symbols, {counts['rows']} rows", flush=True)
    return counts


def run_backfill(since: str = DEFAULT_SINCE, ingestor: Ingestor | None = None, skip_fundamentals: bool = False) -> dict[str, Any]:
    """Run the whole history download and return a summary."""
    ingestor = ingestor or Ingestor()
    db = ingestor.db
    summary: dict[str, Any] = {"since": since}

    def stage(name: str, action: Any) -> Any:
        print(f"[backfill] {name} ...", flush=True)
        try:
            result = action()
            summary[name] = result
            print(f"[backfill] {name}: {result}", flush=True)
            return result
        except Exception as exc:
            logger.exception("Backfill stage %s failed: %s", name, exc)
            summary[name] = f"FAILED: {exc}"
            print(f"[backfill] {name}: FAILED {exc}", flush=True)
            return None

    stage("securities", ingestor.refresh_securities)
    stage("corporate actions", lambda: ingestor.refresh_corporate_actions(full=True))
    stage("indices", lambda: ingestor.refresh_indices(since=since))
    stage("prices", lambda: backfill_prices(ingestor, since))
    stage("securities (first trade dates)", ingestor.refresh_securities)
    if not skip_fundamentals:
        symbols = db.active_equity_symbols()
        pending = [s for s in symbols if not db.scalar("SELECT 1 FROM fundamentals WHERE symbol = ? LIMIT 1", (s,))]
        stage("fundamentals", lambda: ingestor.refresh_fundamentals(symbols=pending))
    stage("official NEPSE figures for the last year", lambda: reconcile_with_nepse(ingestor))
    stage("adjusted prices", ingestor.recompute_adjustments)
    stage("breadth and liquidity", lambda: ingestor.recompute_market_metrics(full=True))
    stage("table sizes", db.table_counts)
    return summary


def reconcile_with_nepse(ingestor: Ingestor, days: int = 360) -> dict[str, Any]:
    """Replace the last year of stored prices with NEPSE's own official figures.

    ShareHub's history occasionally carries a wrong volume for a day. NEPSE
    keeps about one year of official date-wise data, so for that year the
    official numbers win. Returns how many differences were found and fixed.
    """
    db = ingestor.db
    start = (date.today() - timedelta(days=days)).isoformat()
    last_completed = ingestor.calendar.last_completed_trading_day().isoformat()
    dates = [day for day in db.trading_dates(start=start) if day <= last_completed]
    stats: dict[str, Any] = {"days": 0, "rows": 0, "close_differences": 0, "volume_differences": 0, "worst_days": [], "unavailable": 0}
    per_day: list[tuple[int, str]] = []
    for position, day in enumerate(dates, start=1):
        if db.get_setting(f"reconciled:{day}") == "1":
            continue
        try:
            official = ingestor.nepse.prices_for_date(day)
        except SourceError as exc:
            stats["unavailable"] += 1
            logger.info("Reconcile skipped %s: %s", day, exc)
            continue
        if len(official) < 50:
            stats["unavailable"] += 1
            continue
        stored = {row["symbol"]: row for row in db.query("SELECT symbol, close, volume FROM daily_prices WHERE date = ?", (day,))}
        day_volume_differences = 0
        for row in official:
            previous = stored.get(row["symbol"])
            if previous is None:
                continue
            if abs(float(previous["close"]) - row["close"]) > 0.011:
                stats["close_differences"] += 1
            if abs(float(previous["volume"] or 0) - row["volume"]) > max(1.0, 0.01 * row["volume"]):
                day_volume_differences += 1
        stats["volume_differences"] += day_volume_differences
        per_day.append((day_volume_differences, day))
        stats["rows"] += db.upsert_prices(official, source="NEPSE")
        db.mark_ingest_day(day, "ok", len(official), "NEPSE", "reconciled with official data")
        db.set_setting(f"reconciled:{day}", "1")
        stats["days"] += 1
        if position % 40 == 0:
            print(f"  reconcile: {position}/{len(dates)} days, {stats['volume_differences']} volume differences fixed", flush=True)
    stats["worst_days"] = [f"{day}: {count}" for count, day in sorted(per_day, reverse=True)[:6] if count]
    stats["days_with_differences"] = sum(1 for count, _ in per_day if count)
    return stats
