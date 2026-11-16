"""Daily data ingestion: fetch, store, adjust, measure, and check health.

One ``Ingestor.run()`` call does everything for an evening:

1. catches up any trading days the database is missing (the laptop may have
   been asleep);
2. refreshes the security list, corporate actions, index history, today's
   broker flow, recent announcements and a rotating batch of fundamentals;
3. recomputes adjusted prices, data-quality flags, market breadth and
   liquidity;
4. checks source health and queues an alert for the owner if something has
   been failing.

Every step is isolated: one failing step never stops the others.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from nepse_bot.db.store import Database, get_database, utc_now_text
from nepse_bot.ingest import adjust, metrics
from nepse_bot.settings import bot_setting
from nepse_bot.sources.http import SourceError, prune_raw_samples, save_raw_sample
from nepse_bot.sources.nepse_official import NepseOfficialClient, aggregate_floorsheet
from nepse_bot.sources.sharehub import ShareHubClient
from nepse_bot.trading_calendar import TradingCalendar, get_calendar
from nepse_bot.utils.health import SOURCE_HEALTH, queue_admin_alert

logger = logging.getLogger(__name__)

INSTRUMENT_NAMES = {
    "equity": "equity",
    "mutual_fund": "mutual_fund",
    "mutual_funds": "mutual_fund",
    "non_convertible_debentures": "debenture",
    "debenture": "debenture",
    "debentures": "debenture",
    "preference_shares": "preference",
    "preference_share": "preference",
}
MIN_ROWS_FOR_A_REAL_TRADING_DAY = 50


@dataclass
class IngestReport:
    """What one ingestion run did, in a form that can be shown to the owner."""

    started_at: datetime
    days_ingested: list[str] = field(default_factory=list)
    days_closed: list[str] = field(default_factory=list)
    days_failed: list[str] = field(default_factory=list)
    steps: dict[str, str] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)

    def step(self, name: str, outcome: str) -> None:
        self.steps[name] = outcome
        logger.info("Ingestion step %s: %s", name, outcome)

    @property
    def ok(self) -> bool:
        return not self.days_failed and not any(text.startswith("FAILED") for text in self.steps.values())

    def as_text(self) -> str:
        lines = ["Data update " + ("completed" if self.ok else "finished WITH PROBLEMS")]
        if self.days_ingested:
            lines.append(f"- Trading days stored: {', '.join(self.days_ingested[-5:])}" + (f" (+{len(self.days_ingested) - 5} earlier)" if len(self.days_ingested) > 5 else ""))
        if self.days_closed:
            lines.append(f"- No trading (marked closed): {', '.join(self.days_closed)}")
        if self.days_failed:
            lines.append(f"- Could not fetch: {', '.join(self.days_failed)}")
        lines.extend(f"- {name}: {outcome}" for name, outcome in self.steps.items())
        return "\n".join(lines)


class Ingestor:
    """Coordinates all data sources and the database."""

    def __init__(
        self,
        db: Database | None = None,
        calendar: TradingCalendar | None = None,
        nepse: NepseOfficialClient | None = None,
        sharehub: ShareHubClient | None = None,
        sharesansar: Any | None = None,
    ) -> None:
        self.db = db or get_database()
        self.calendar = calendar or get_calendar()
        self.nepse = nepse or NepseOfficialClient()
        self.sharehub = sharehub or ShareHubClient()
        self._sharesansar = sharesansar
        self._run_lock = threading.Lock()

    @property
    def sharesansar(self) -> Any:
        """The Stage 1 ShareSansar fetcher, used here only as a price fallback."""
        if self._sharesansar is None:
            from nepse_bot.ingestion.historical_fetcher import NepseHistoricalFetcher

            self._sharesansar = NepseHistoricalFetcher(calendar=self.calendar)
        return self._sharesansar

    # ------------------------------------------------------------ bookkeeping
    def _record(self, source: str, dataset: str, day: str, ok: bool, rows: int = 0, detail: str = "") -> None:
        self.db.record_source_run(source, dataset, day, ok, rows, detail)
        label = f"{source} {dataset}"
        if ok:
            SOURCE_HEALTH.record_success(label)
        else:
            SOURCE_HEALTH.record_failure(label, detail)

    # ----------------------------------------------------------------- prices
    def _sharesansar_prices(self, day: str) -> list[dict[str, Any]]:
        table = self.sharesansar._get_sharesansar_daily_table(day)
        try:
            save_raw_sample("ShareSansar", "daily-table.csv", table.head(60).to_csv(index=False))
        except Exception:  # sampling must never break ingestion
            pass
        rows = []
        for _, item in table.iterrows():
            number = self.sharesansar._numeric_from_row
            close = number(item, ("Close", "LTP"))
            if close is None:
                continue
            rows.append(
                {
                    "symbol": str(item.get("Symbol", "")).strip().upper(),
                    "date": day,
                    "open": number(item, ("Open",)),
                    "high": number(item, ("High",)),
                    "low": number(item, ("Low",)),
                    "close": close,
                    "volume": number(item, ("Vol", "Volume")),
                    "turnover": number(item, ("Turnover",)),
                    "trades": number(item, ("Trans.", "Trans")),
                }
            )
        return rows

    def _other_source_confirms_closed(self, day: str) -> bool:
        """Return True only if a second source also shows no trading that day."""
        try:
            rows = self.sharehub.price_history("NEPSE", since=day, size=10)
            return not any(row["date"] == day for row in rows)
        except SourceError:
            pass
        try:
            self.sharesansar._get_sharesansar_daily_table(day)
            return False
        except ValueError:
            return True
        except Exception:
            return False

    def ingest_prices_for_day(self, day: date) -> str:
        """Fetch and store one trading day's prices. Returns ok / closed / failed."""
        text = day.isoformat()
        errors: list[str] = []

        try:
            rows = self.nepse.prices_for_date(text)
            if len(rows) >= MIN_ROWS_FOR_A_REAL_TRADING_DAY:
                stored = self.db.upsert_prices(rows, source="NEPSE")
                self._record("NEPSE", "prices", text, True, stored)
                self.db.mark_ingest_day(text, "ok", stored, "NEPSE")
                return "ok"
            if not rows and self._other_source_confirms_closed(text):
                self._record("NEPSE", "prices", text, True, 0, "no trading that day")
                self.db.mark_ingest_day(text, "closed", 0, "NEPSE", "two sources show no trading")
                return "closed"
            errors.append(f"NEPSE returned only {len(rows)} rows")
            self._record("NEPSE", "prices", text, False, len(rows), errors[-1])
        except SourceError as exc:
            errors.append(str(exc))
            self._record("NEPSE", "prices", text, False, 0, str(exc))

        try:
            rows = self._sharesansar_prices(text)
            if len(rows) >= MIN_ROWS_FOR_A_REAL_TRADING_DAY:
                stored = self.db.upsert_prices(rows, source="ShareSansar")
                self._record("ShareSansar", "prices", text, True, stored)
                self.db.mark_ingest_day(text, "ok", stored, "ShareSansar", "; ".join(errors))
                return "ok"
            errors.append(f"ShareSansar returned only {len(rows)} rows")
            self._record("ShareSansar", "prices", text, False, len(rows), errors[-1])
        except Exception as exc:
            errors.append(f"ShareSansar: {exc}")
            self._record("ShareSansar", "prices", text, False, 0, str(exc))

        try:
            symbols = self.db.active_equity_symbols()
            rows = []
            for symbol in symbols:
                try:
                    rows.extend(row for row in self.sharehub.price_history(symbol, since=text, size=10) if row["date"] == text)
                except SourceError:
                    continue
            if len(rows) >= MIN_ROWS_FOR_A_REAL_TRADING_DAY:
                stored = self.db.upsert_prices(rows, source="ShareHub")
                self._record("ShareHub", "prices", text, True, stored)
                self.db.mark_ingest_day(text, "ok", stored, "ShareHub", "; ".join(errors))
                return "ok"
            errors.append(f"ShareHub returned only {len(rows)} rows")
            self._record("ShareHub", "prices", text, False, len(rows), errors[-1])
        except Exception as exc:
            errors.append(f"ShareHub: {exc}")

        self.db.mark_ingest_day(text, "failed", 0, "", "; ".join(errors))
        return "failed"

    def missing_trading_days(self, now: datetime | None = None, max_days: int = 400) -> list[date]:
        """Return completed trading days that are not in the database yet."""
        last_completed = self.calendar.last_completed_trading_day(now)
        last_stored = self.db.last_price_date()
        if last_stored is None:
            return [last_completed]
        settled = {row["date"] for row in self.db.query("SELECT date FROM ingest_days WHERE status IN ('ok', 'closed')")}
        have = set(self.db.trading_dates(start=(last_completed - timedelta(days=max_days)).isoformat()))
        days: list[date] = []
        cursor = date.fromisoformat(last_stored) + timedelta(days=1)
        cursor = max(cursor, last_completed - timedelta(days=max_days))
        while cursor <= last_completed:
            text = cursor.isoformat()
            if self.calendar.is_trading_day(cursor) and text not in have and text not in settled:
                days.append(cursor)
            cursor += timedelta(days=1)
        return days

    def catch_up(self, report: IngestReport, now: datetime | None = None) -> None:
        """Ingest every missing trading day, oldest first."""
        for day in self.missing_trading_days(now):
            outcome = self.ingest_prices_for_day(day)
            text = day.isoformat()
            if outcome == "ok":
                report.days_ingested.append(text)
            elif outcome == "closed":
                report.days_closed.append(text)
                if self.calendar.add_holiday(day, "Auto-detected: no trading on NEPSE"):
                    report.alerts.append(
                        f"No trading happened on {text} ({day.strftime('%A')}), confirmed by two sources. "
                        f"I marked it as a market holiday. If that is wrong: /holiday remove {text}"
                    )
            else:
                report.days_failed.append(text)

    # -------------------------------------------------------------- reference
    def refresh_securities(self) -> int:
        """Update the security master list from ShareHub and NEPSE."""
        merged: dict[str, dict[str, Any]] = {}
        errors = []
        today = self.calendar.now().date().isoformat()
        try:
            for row in self.sharehub.securities():
                merged[row["symbol"]] = row
            self._record("ShareHub", "securities", today, True, len(merged))
        except SourceError as exc:
            errors.append(str(exc))
            self._record("ShareHub", "securities", today, False, 0, str(exc))
        try:
            companies = self.nepse.companies()
            for row in companies:
                base = merged.setdefault(row["symbol"], {"symbol": row["symbol"], "is_promoter": 0})
                for key in ("name", "sector", "instrument_type", "status", "nepse_id"):
                    if row.get(key) not in (None, "", "unknown"):
                        base[key] = row[key]
            self._record("NEPSE", "securities", today, True, len(companies))
        except SourceError as exc:
            errors.append(str(exc))
            self._record("NEPSE", "securities", today, False, 0, str(exc))
        if not merged:
            raise SourceError("; ".join(errors) or "no securities returned")

        now_text = utc_now_text()
        rows = []
        for row in merged.values():
            instrument = INSTRUMENT_NAMES.get(str(row.get("instrument_type") or ""), row.get("instrument_type") or "other")
            rows.append(
                {
                    "symbol": row["symbol"],
                    "name": row.get("name"),
                    "sector": row.get("sector"),
                    "instrument_type": instrument,
                    "status": row.get("status") or "unknown",
                    "is_promoter": int(row.get("is_promoter") or 0),
                    "nepse_id": row.get("nepse_id"),
                    "updated_at": now_text,
                }
            )
        stored = self.db.upsert("securities", rows, ("symbol",))
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE securities SET first_trade_date = "
                "(SELECT MIN(date) FROM daily_prices WHERE daily_prices.symbol = securities.symbol)"
            )
        return stored

    def refresh_corporate_actions(self, full: bool = False) -> int:
        """Update bonus, cash dividend and right-share records."""
        today = self.calendar.now().date().isoformat()
        pages = None if full else 1
        rows: list[dict[str, Any]] = []
        for name, fetch in (("dividends", self.sharehub.dividends), ("rights", self.sharehub.rights)):
            try:
                found = fetch(max_pages=pages)
                rows.extend(found)
                self._record("ShareHub", name, today, True, len(found))
            except SourceError as exc:
                self._record("ShareHub", name, today, False, 0, str(exc))
        now_text = utc_now_text()
        for row in rows:
            row["updated_at"] = now_text
        return self.db.upsert("corporate_actions", rows, ("symbol", "action_type", "fiscal_year", "source_id"))

    def refresh_indices(self, since: str | None = None) -> int:
        """Update index history (NEPSE and every sector index)."""
        today = self.calendar.now().date().isoformat()
        last = self.db.scalar("SELECT MAX(date) FROM index_daily")
        start = since or last or "2019-01-01"
        page_size = 500 if since or last is None or (date.fromisoformat(today) - date.fromisoformat(start)).days > 10 else 15
        stored = 0
        try:
            indices = self.sharehub.indices()
            last_completed = self.calendar.last_completed_trading_day().isoformat()
            for index in indices:
                # Completed sessions only: never store today's unfinished bar.
                rows = [
                    row
                    for row in self.sharehub.price_history(index["code"], since=start, size=page_size)
                    if row["date"] <= last_completed
                ]
                stored += self.db.upsert(
                    "index_daily",
                    [
                        {
                            "index_code": index["code"], "date": row["date"], "open": row["open"], "high": row["high"],
                            "low": row["low"], "close": row["close"], "volume": row["volume"],
                            "turnover": row["turnover"], "trades": row["trades"],
                            "is_sector": index["is_sector"], "source": "ShareHub",
                        }
                        for row in rows
                    ],
                    ("index_code", "date"),
                )
            self._record("ShareHub", "indices", today, True, stored)
            return stored
        except SourceError as exc:
            self._record("ShareHub", "indices", today, False, stored, str(exc))
        # Fallback: today's closing values straight from NEPSE.
        rows = self.nepse.index_snapshot(self.calendar.last_completed_trading_day().isoformat())
        for row in rows:
            row["source"] = "NEPSE"
        stored = self.db.upsert("index_daily", rows, ("index_code", "date"))
        self._record("NEPSE", "indices", today, True, stored)
        return stored

    def ingest_floorsheet(self) -> str:
        """Store today's broker flow (NEPSE only publishes the current day)."""
        now = self.calendar.now()
        today = now.date()
        text = today.isoformat()
        if not self.calendar.is_trading_day(today) or self.calendar.last_completed_trading_day(now) != today:
            return "skipped (only available on the evening of a trading day)"
        try:
            trades = self.nepse.floorsheet()
        except SourceError as exc:
            self._record("NEPSE", "floorsheet", text, False, 0, str(exc))
            return f"FAILED: {exc}"
        todays = [trade for trade in trades if str(trade.get("businessDate", ""))[:10] == text]
        rows, hidden = aggregate_floorsheet(todays)
        if not rows:
            detail = (
                f"{hidden} trades had no broker numbers" if hidden else f"no trades dated {text} (got {len(trades)})"
            )
            self._record("NEPSE", "floorsheet", text, False, 0, detail)
            return f"FAILED: {detail}"
        stored = self.db.upsert("broker_flow_daily", rows, ("date", "symbol", "broker"))
        self._record("NEPSE", "floorsheet", text, True, stored)
        return f"{len(todays)} trades -> {stored} symbol/broker rows"

    def refresh_fundamentals(self, symbols: list[str] | None = None, limit: int | None = None) -> tuple[int, int]:
        """Update quarterly reports. Returns (companies checked, new reports found).

        Without ``symbols`` a rotating batch is refreshed: the companies whose
        fundamentals were checked longest ago go first.
        """
        today = self.calendar.now().date()
        if symbols is None:
            batch = int(limit if limit is not None else bot_setting("ingest", "fundamentals_per_run", default=80))
            rows = self.db.query(
                # Never-checked companies first, then those still on an older quarter than the newest
                # one in the database (a late report), then whoever was checked longest ago.
                "SELECT s.symbol, MAX(f.updated_at) AS checked, MAX(f.period_end_date) AS newest FROM securities s "
                "LEFT JOIN fundamentals f ON f.symbol = s.symbol "
                "WHERE s.status = 'active' AND s.instrument_type = 'equity' AND s.is_promoter = 0 "
                "GROUP BY s.symbol ORDER BY checked IS NOT NULL, "
                "(newest < (SELECT MAX(period_end_date) FROM fundamentals)) DESC, checked LIMIT ?",
                (batch,),
            )
            symbols = [row["symbol"] for row in rows]
        checked = new_reports = failures = 0
        now_text = utc_now_text()
        for symbol in symbols:
            try:
                known = {
                    (row["fiscal_year"], row["quarter"])
                    for row in self.db.query("SELECT fiscal_year, quarter FROM fundamentals WHERE symbol = ?", (symbol,))
                }
                try:
                    reports = self.sharehub.fundamentals(symbol, first_seen=today)
                except SourceError:
                    reports = []
                if not reports:
                    # Second source: ShareSansar's latest quarterly report.
                    from nepse_bot.sources.sharesansar_reports import fetch_quarterly_report

                    fallback = fetch_quarterly_report(self.sharesansar, symbol, first_seen=today)
                    if fallback is None:
                        raise SourceError(f"no quarterly report found for {symbol} on ShareHub or ShareSansar")
                    reports = [fallback]
                fresh = []
                for row in reports:
                    row["updated_at"] = now_text
                    if (row["fiscal_year"], row["quarter"]) not in known:
                        new_reports += 1
                    else:
                        # Keep the publication date recorded when the report was first stored.
                        row.pop("publication_date")
                        row.pop("publication_date_source")
                    fresh.append(row)
                # Rows may have different keys (see above), so store them one shape at a time.
                for shape in {tuple(sorted(row)) for row in fresh}:
                    self.db.upsert("fundamentals", [row for row in fresh if tuple(sorted(row)) == shape], ("symbol", "fiscal_year", "quarter"))
                checked += 1
            except SourceError as exc:
                failures += 1
                logger.info("Fundamentals unavailable for %s: %s", symbol, exc)
        if symbols:
            ok = failures <= len(symbols) / 2
            self._record("ShareHub", "fundamentals", today.isoformat(), ok, checked, f"{failures} of {len(symbols)} companies failed")
        return checked, new_reports

    def refresh_announcements(self) -> int:
        """Store the latest company announcements."""
        today = self.calendar.now().date().isoformat()
        try:
            rows = self.sharehub.announcements(pages=1)
        except SourceError as exc:
            self._record("ShareHub", "announcements", today, False, 0, str(exc))
            raise
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT INTO news (symbol, title, source, url, published_at, sentiment, kind) "
                "VALUES (:symbol, :title, :source, :url, :published_at, :sentiment, :kind) "
                "ON CONFLICT (source, url, title) DO NOTHING",
                rows,
            )
        self._record("ShareHub", "announcements", today, True, len(rows))
        return len(rows)

    def refresh_lockins(self) -> int:
        """Store the dates on which locked promoter / mutual fund shares become free to sell."""
        today = self.calendar.now().date().isoformat()
        try:
            rows = self.sharehub.lockins()
        except SourceError as exc:
            self._record("ShareHub", "lockins", today, False, 0, str(exc))
            raise
        stamp = self.calendar.now().isoformat(timespec="seconds")
        stored = self.db.upsert("lockin_unlocks", [{**row, "updated_at": stamp} for row in rows], ("symbol", "holder_type", "unlock_date"))
        self._record("ShareHub", "lockins", today, True, stored)
        return stored

    # ---------------------------------------------------------- post-process
    def recompute_adjustments(self, symbols: list[str] | None = None) -> tuple[int, int]:
        """Recalculate adjusted prices and quality flags. Returns (symbols, flags)."""
        if symbols is None:
            symbols = [row["symbol"] for row in self.db.query("SELECT DISTINCT symbol FROM daily_prices")]
        actions_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in self.db.query(
            "SELECT * FROM corporate_actions WHERE book_closure_date IS NOT NULL AND action_type IN ('bonus', 'right', 'split')"
        ):
            actions_by_symbol.setdefault(row["symbol"], []).append(row)
        all_closures: dict[str, list[dict[str, Any]]] = {}
        for row in self.db.query("SELECT symbol, book_closure_date FROM corporate_actions WHERE book_closure_date IS NOT NULL"):
            all_closures.setdefault(row["symbol"], []).append(row)
        limits = adjust.price_limits()
        now_text = utc_now_text()
        flag_count = 0
        for symbol in symbols:
            prices = self.db.frame(
                "SELECT symbol, date, open, high, low, close FROM daily_prices WHERE symbol = ? ORDER BY date", (symbol,)
            )
            if prices.empty:
                continue
            adjusted, applied = adjust.adjust_prices(prices, actions_by_symbol.get(symbol, []))
            flags = adjust.find_quality_flags(prices, applied, all_closures.get(symbol, []), limits)
            with self.db.transaction() as connection:
                connection.executemany(
                    "UPDATE daily_prices SET adj_factor = ?, adj_open = ?, adj_high = ?, adj_low = ?, adj_close = ? "
                    "WHERE symbol = ? AND date = ?",
                    [
                        (r.adj_factor, r.adj_open, r.adj_high, r.adj_low, r.adj_close, symbol, r.date)
                        for r in adjusted.itertuples(index=False)
                    ],
                )
                connection.execute("DELETE FROM data_quality_flags WHERE symbol = ?", (symbol,))
                connection.executemany(
                    "INSERT INTO data_quality_flags (symbol, date, kind, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                    [(symbol, flag["date"], flag["kind"], flag["detail"], now_text) for flag in flags],
                )
            flag_count += len(flags)
        return len(symbols), flag_count

    def recompute_market_metrics(self, full: bool = False) -> tuple[int, int]:
        """Recalculate breadth and liquidity. Returns (breadth days, liquidity rows)."""
        last = self.db.last_price_date()
        if last is None:
            return 0, 0
        last_breadth = self.db.scalar("SELECT MAX(date) FROM market_breadth_daily")
        since = None if full or last_breadth is None else last_breadth
        # Moving averages need about 300 calendar days of history before ``since``.
        load_from = "1900-01-01" if since is None else (date.fromisoformat(since) - timedelta(days=420)).isoformat()
        prices = self.db.frame(
            "SELECT p.symbol, p.date, p.close, p.adj_close, p.turnover, p.trades FROM daily_prices p "
            "JOIN securities s ON s.symbol = p.symbol "
            "WHERE s.instrument_type = 'equity' AND s.is_promoter = 0 AND p.date >= ?",
            (load_from,),
        )
        if prices.empty:
            return 0, 0
        breadth = metrics.breadth_table(prices, since=since)
        self.db.upsert("market_breadth_daily", breadth, ("date",))
        liquidity = metrics.liquidity_table(prices[prices["date"] >= (date.fromisoformat(last) - timedelta(days=130)).isoformat()], last)
        self.db.upsert("liquidity_metrics", liquidity, ("symbol", "as_of_date"))
        return len(breadth), len(liquidity)

    # ---------------------------------------------------------------- health
    def health_alerts(self) -> list[str]:
        """Return owner alerts about sources that keep failing or return empty fields."""
        alerts: list[str] = []
        recent_days = [row["date"] for row in self.db.query("SELECT DISTINCT date FROM source_runs ORDER BY date DESC LIMIT 2")]
        if len(recent_days) == 2:
            for row in self.db.query(
                "SELECT source, dataset, COUNT(*) AS runs, SUM(ok) AS successes, MAX(detail) AS detail "
                "FROM source_runs WHERE date IN (?, ?) GROUP BY source, dataset",
                recent_days,
            ):
                if row["runs"] == 2 and not row["successes"]:
                    alerts.append(
                        f"⚠️ Data source problem: {row['source']} has failed to deliver {row['dataset']} "
                        f"two days in a row ({recent_days[1]} and {recent_days[0]}). Last reason: {row['detail'] or 'unknown'}"
                    )

        active = int(self.db.scalar(
            "SELECT COUNT(*) FROM securities WHERE status = 'active' AND instrument_type = 'equity' AND is_promoter = 0"
        ) or 0)
        if active:
            checks = {
                "sector": "SELECT COUNT(*) FROM securities WHERE status = 'active' AND instrument_type = 'equity' "
                          "AND is_promoter = 0 AND (sector IS NULL OR sector = '')",
            }
            with_reports = int(self.db.scalar("SELECT COUNT(DISTINCT symbol) FROM fundamentals") or 0)
            if with_reports >= 20:
                for column in ("eps", "book_value"):
                    checks[f"fundamentals {column}"] = (
                        "SELECT COUNT(*) FROM (SELECT f.symbol FROM fundamentals f JOIN "
                        "(SELECT symbol, MAX(period_end_date) AS latest FROM fundamentals GROUP BY symbol) m "
                        f"ON m.symbol = f.symbol AND m.latest = f.period_end_date WHERE f.{column} IS NULL)"
                    )
            last = self.db.last_price_date()
            for name, sql in checks.items():
                base = with_reports if name.startswith("fundamentals") else active
                empty = int(self.db.scalar(sql) or 0)
                if base and empty / base > 0.5:
                    alerts.append(f"⚠️ Data quality problem: '{name}' is empty for {empty} of {base} companies (more than half).")
            if last:
                total = int(self.db.scalar("SELECT COUNT(*) FROM daily_prices WHERE date = ?", (last,)) or 0)
                for column in ("turnover", "trades", "volume"):
                    empty = int(self.db.scalar(
                        f"SELECT COUNT(*) FROM daily_prices WHERE date = ? AND ({column} IS NULL OR {column} = 0)", (last,)
                    ) or 0)
                    if total and empty / total > 0.5:
                        alerts.append(f"⚠️ Data quality problem: '{column}' is empty for {empty} of {total} price rows on {last}.")
        return alerts

    # -------------------------------------------------------------------- run
    def run(self, now: datetime | None = None, light: bool = False) -> IngestReport:
        """Run a full evening update (or a light catch-up of prices only)."""
        report = IngestReport(started_at=self.calendar.now())
        if not self._run_lock.acquire(blocking=False):
            report.step("run", "skipped: another update is already running")
            return report
        try:
            self._safely(report, "missed days", lambda: self.catch_up(report, now) or
                         f"{len(report.days_ingested)} stored, {len(report.days_closed)} closed, {len(report.days_failed)} failed")
            if not light:
                self._safely(report, "securities", lambda: f"{self.refresh_securities()} listed")
                self._safely(report, "corporate actions", lambda: f"{self.refresh_corporate_actions()} checked")
                self._safely(report, "indices", lambda: f"{self.refresh_indices()} rows")
                self._safely(report, "broker flow", self.ingest_floorsheet)
                self._safely(report, "announcements", lambda: f"{self.refresh_announcements()} checked")
                self._safely(report, "lock-in expiries", lambda: f"{self.refresh_lockins()} dates")
                self._safely(report, "fundamentals", lambda: "{} companies checked, {} new reports".format(*self.refresh_fundamentals()))
            self._safely(report, "adjusted prices", lambda: "{} symbols, {} quality flags".format(*self.recompute_adjustments()))
            self._safely(report, "breadth and liquidity", lambda: "{} days, {} stocks".format(*self.recompute_market_metrics()))
            if not light:
                self._safely(report, "raw samples", lambda: f"{prune_raw_samples()} old folders removed")
            try:
                report.alerts.extend(self.health_alerts())
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Health check failed: %s", exc)
            for alert in report.alerts:
                queue_admin_alert(alert)
            self.db.set_setting("last_ingest_run", utc_now_text())
            self.db.set_setting("last_ingest_summary", report.as_text())
            return report
        finally:
            self._run_lock.release()

    @staticmethod
    def _safely(report: IngestReport, name: str, action: Any) -> None:
        try:
            report.step(name, str(action()))
        except Exception as exc:
            logger.exception("Ingestion step '%s' failed: %s", name, exc)
            report.step(name, f"FAILED: {exc}")
