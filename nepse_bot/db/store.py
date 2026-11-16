"""The only module that talks to the database.

Everything else calls the methods here, so moving from SQLite to PostgreSQL
later means changing this one file.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from nepse_bot.db.schema import MIGRATIONS
from nepse_bot.settings import DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = DATA_DIR / "nepse.sqlite3"


def utc_now_text() -> str:
    """Return the current UTC time as ISO text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thread-safe access to the bot's database."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------ plumbing
    def _migrate(self) -> None:
        with self._lock:
            self._connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self._connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = int(row["v"] or 0)
            for number, script in enumerate(MIGRATIONS, start=1):
                if number <= current:
                    continue
                logger.info("Applying database migration %s.", number)
                self._connection.executescript("BEGIN;" + script + f"INSERT INTO schema_version VALUES ({number});COMMIT;")

    @contextmanager
    def transaction(self) -> Any:
        """Run several statements as one all-or-nothing unit."""
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._connection.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a SELECT and return rows as dictionaries."""
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run a SELECT that returns one value."""
        with self._lock:
            row = self._connection.execute(sql, params).fetchone()
            return row[0] if row is not None else None

    def frame(self, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
        """Run a SELECT and return a DataFrame."""
        with self._lock:
            return pd.read_sql_query(sql, self._connection, params=list(params))

    def upsert(self, table: str, rows: Iterable[dict[str, Any]], key_columns: Sequence[str]) -> int:
        """Insert rows, updating existing ones that share the same key."""
        rows = list(rows)
        if not rows:
            return 0
        columns = list(rows[0].keys())
        updates = [column for column in columns if column not in key_columns]
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT ({', '.join(key_columns)}) DO "
            + (f"UPDATE SET {', '.join(f'{column} = excluded.{column}' for column in updates)}" if updates else "NOTHING")
        )
        with self.transaction() as connection:
            connection.executemany(sql, [[row.get(column) for column in columns] for row in rows])
        return len(rows)

    # -------------------------------------------------------------- prices
    def upsert_prices(self, rows: Iterable[dict[str, Any]], source: str) -> int:
        """Store raw daily prices. Adjusted columns are filled by the adjuster."""
        prepared = []
        for row in rows:
            close = row.get("close")
            if not row.get("symbol") or not row.get("date") or close is None or float(close) <= 0:
                continue
            close = float(close)
            open_price = float(row.get("open") or close)
            # Sources occasionally report a high below (or a low above) the
            # close; keep every bar internally consistent.
            high = max(float(row.get("high") or close), open_price, close)
            low = min(float(row.get("low") or close), open_price, close)
            prepared.append(
                {
                    "symbol": str(row["symbol"]).strip().upper(),
                    "date": str(row["date"])[:10],
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": row.get("volume") or 0,
                    "turnover": row.get("turnover") or 0,
                    "trades": int(row.get("trades") or 0),
                    "source": source,
                }
            )
        return self.upsert("daily_prices", prepared, ("symbol", "date"))

    def price_history(self, symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """Return one symbol's daily prices, oldest first, indexed by date."""
        sql = "SELECT * FROM daily_prices WHERE symbol = ?"
        params: list[Any] = [symbol.strip().upper()]
        if start:
            sql += " AND date >= ?"
            params.append(start)
        if end:
            sql += " AND date <= ?"
            params.append(end)
        frame = self.frame(sql + " ORDER BY date", params)
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date")
        return frame

    def last_price_date(self) -> str | None:
        """Return the newest date that has prices."""
        return self.scalar("SELECT MAX(date) FROM daily_prices")

    def trading_dates(self, start: str | None = None) -> list[str]:
        """Return every date that has prices (the real historical trading days)."""
        if start:
            rows = self.query("SELECT DISTINCT date FROM daily_prices WHERE date >= ? ORDER BY date", (start,))
        else:
            rows = self.query("SELECT DISTINCT date FROM daily_prices ORDER BY date")
        return [row["date"] for row in rows]

    # ------------------------------------------------------------- lookups
    def active_equity_symbols(self) -> list[str]:
        """Return ordinary shares that are currently active."""
        rows = self.query(
            "SELECT symbol FROM securities WHERE status = 'active' AND instrument_type = 'equity' "
            "AND is_promoter = 0 ORDER BY symbol"
        )
        return [row["symbol"] for row in rows]

    def fundamentals_as_of(self, symbol: str, as_of: str) -> dict[str, Any] | None:
        """Return the newest report that was already published on ``as_of`` (no lookahead)."""
        rows = self.query(
            "SELECT * FROM fundamentals WHERE symbol = ? AND publication_date <= ? "
            "ORDER BY period_end_date DESC LIMIT 1",
            (symbol.strip().upper(), as_of),
        )
        return rows[0] if rows else None

    # ---------------------------------------------------------- bookkeeping
    def record_source_run(self, source: str, dataset: str, date: str, ok: bool, rows: int = 0, detail: str = "") -> None:
        """Remember whether a source delivered a dataset for a trading day."""
        self.upsert(
            "source_runs",
            [
                {
                    "source": source,
                    "dataset": dataset,
                    "date": date,
                    "ok": 1 if ok else 0,
                    "rows": rows,
                    "detail": detail[:300],
                    "updated_at": utc_now_text(),
                }
            ],
            ("source", "dataset", "date"),
        )

    def mark_ingest_day(self, date: str, status: str, price_rows: int = 0, source: str = "", detail: str = "") -> None:
        """Record the outcome of ingesting one trading day."""
        self.upsert(
            "ingest_days",
            [
                {
                    "date": date,
                    "status": status,
                    "price_rows": price_rows,
                    "source": source,
                    "detail": detail[:300],
                    "updated_at": utc_now_text(),
                }
            ],
            ("date",),
        )

    def set_setting(self, key: str, value: str) -> None:
        """Save one setting."""
        self.upsert("settings", [{"key": key, "value": value, "updated_at": utc_now_text()}], ("key",))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Read one setting."""
        value = self.scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return value if value is not None else default

    def table_counts(self) -> dict[str, int]:
        """Return the number of rows in each main table."""
        tables = (
            "securities", "daily_prices", "corporate_actions", "fundamentals", "broker_flow_daily", "lockin_unlocks",
            "index_daily", "market_breadth_daily", "liquidity_metrics", "news", "signals",
            "positions", "trades", "data_quality_flags",
        )
        return {table: int(self.scalar(f"SELECT COUNT(*) FROM {table}") or 0) for table in tables}


_database: Database | None = None
_database_lock = threading.Lock()


def get_database() -> Database:
    """Return the shared database instance."""
    global _database
    with _database_lock:
        if _database is None:
            _database = Database()
        return _database
