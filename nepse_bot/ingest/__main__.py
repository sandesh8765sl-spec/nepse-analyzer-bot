"""Command line for the data layer.

    python -m nepse_bot.ingest backfill [--since 2019-01-01]
    python -m nepse_bot.ingest daily
    python -m nepse_bot.ingest status
    python -m nepse_bot.ingest brokers [--sample] [--since 2022-02-01]
"""

from __future__ import annotations

import argparse
import json
import logging

from nepse_bot.db.store import get_database
from nepse_bot.ingest.backfill import DEFAULT_SINCE, run_backfill
from nepse_bot.ingest.pipeline import Ingestor


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m nepse_bot.ingest", description="NEPSE bot data layer")
    commands = parser.add_subparsers(dest="command", required=True)
    backfill = commands.add_parser("backfill", help="download history (resumable)")
    backfill.add_argument("--since", default=DEFAULT_SINCE)
    backfill.add_argument("--skip-fundamentals", action="store_true")
    commands.add_parser("daily", help="run the evening update now")
    commands.add_parser("status", help="show what the database holds")
    brokers = commands.add_parser("brokers", help="download historical broker flow (resumable, takes hours)")
    brokers.add_argument("--since", default=None)
    brokers.add_argument("--sample", action="store_true", help="only the 3 most recent missing days")
    brokers.add_argument("--limit", type=int, default=None, help="at most this many days")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    if args.command == "backfill":
        summary = run_backfill(since=args.since, skip_fundamentals=args.skip_fundamentals)
        print(json.dumps(summary, indent=1, default=str))
    elif args.command == "brokers":
        from nepse_bot.ingest.broker_backfill import run_broker_backfill

        from nepse_bot.utils.keep_awake import keep_awake

        with keep_awake():      # the download takes many hours; do not let the PC fall asleep on idle
            summary = run_broker_backfill(since=args.since, limit=3 if args.sample else args.limit)
        print(json.dumps(summary, indent=1, default=str))
    elif args.command == "daily":
        print(Ingestor().run().as_text())
    else:
        db = get_database()
        print(json.dumps(
            {
                "database": str(db.path),
                "rows": db.table_counts(),
                "first_price_date": db.scalar("SELECT MIN(date) FROM daily_prices"),
                "last_price_date": db.last_price_date(),
                "last_ingest_run": db.get_setting("last_ingest_run"),
            },
            indent=1,
        ))


if __name__ == "__main__":
    main()
