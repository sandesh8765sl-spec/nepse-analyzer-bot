"""Historical OHLCV ingestion for NEPSE symbols.

One fetcher object is shared by the whole bot. It is safe to use from several
threads at once: each thread gets its own web session (a ShareSansar security
token only works with the session that received it), while the cached market
tables are shared behind a lock.

Reliability rules implemented here:

* If ShareSansar answers 419 (session expired) or 403 (blocked), the thread's
  session, security token and company cache are thrown away, a fresh token is
  fetched, and the request is retried once after a short pause.
* Cached market tables are tied to the trading date. A table fetched before a
  session closed is never reused after that close.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Any, TypeVar

import cloudscraper
import pandas as pd
from bs4 import BeautifulSoup
from requests import Response, Session
from requests.exceptions import RequestException

from nepse_bot.settings import bot_setting
from nepse_bot.trading_calendar import TradingCalendar, get_calendar
from nepse_bot.utils.health import SOURCE_HEALTH

logger = logging.getLogger(__name__)

T = TypeVar("T")

SOURCE_PRICE_HISTORY = "ShareSansar price history"
SOURCE_MARKET_TABLE = "ShareSansar market table"
SOURCE_NEPSEALPHA = "NepseAlpha (legacy)"


class SessionExpiredError(RequestException):
    """ShareSansar rejected the session (HTTP 419) or blocked it (HTTP 403)."""


class NepseHistoricalFetcher:
    """Fetch daily OHLCV data for NEPSE symbols, with caching and self-healing sessions."""

    BASE_URL = "https://nepsealpha.com/api/smx9156/trading_view/history"
    SHARESANSAR_COMPANY_URL = "https://www.sharesansar.com/company/{symbol}"
    SHARESANSAR_COMPANY_PRICE_HISTORY_URL = "https://www.sharesansar.com/company-price-history"
    SHARESANSAR_TOKEN_URL = "https://www.sharesansar.com/today-share-price"
    SHARESANSAR_AJAX_URL = "https://www.sharesansar.com/ajaxtodayshareprice"
    SHARESANSAR_FALLBACK_MAX_DAYS = 120
    SHARESANSAR_PRICE_HISTORY_PAGE_SIZE = 50
    SESSION_ERROR_STATUSES = frozenset({403, 419})
    DAILY_CACHE_MAX_ENTRIES = 200
    POST_CLOSE_SETTLE = timedelta(minutes=10)

    DEFAULT_HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://nepsealpha.com/",
    }

    def __init__(
        self,
        session: Session | None = None,
        timeout_seconds: int | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.5,
        session_factory: Callable[[], Session] | None = None,
        calendar: TradingCalendar | None = None,
        session_retry_backoff_seconds: float | None = None,
        live_table_seconds: float | None = None,
    ) -> None:
        """Initialize the fetcher.

        Args:
            session: Optional single session to use on every thread (tests).
            session_factory: Builds one session per thread when ``session`` is not given.
            calendar: Trading calendar; defaults to the shared configured one.
        """
        self._injected_session = session
        self._session_factory = session_factory or cloudscraper.create_scraper
        self.calendar = calendar or get_calendar()
        self.timeout_seconds = int(timeout_seconds or bot_setting("http", "timeout_seconds", default=20))
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.session_retry_backoff_seconds = float(
            session_retry_backoff_seconds
            if session_retry_backoff_seconds is not None
            else bot_setting("http", "session_retry_backoff_seconds", default=2.0)
        )
        self.live_table_seconds = float(
            live_table_seconds
            if live_table_seconds is not None
            else bot_setting("cache", "live_table_seconds", default=120)
        )

        self._local = threading.local()
        self._cache_lock = threading.RLock()
        self._latest_fetch_lock = threading.Lock()
        self._company_ids: dict[str, str] = {}
        self._daily_cache: OrderedDict[str, tuple[datetime, pd.DataFrame]] = OrderedDict()
        self._daily_empty_dates: set[str] = set()
        self._latest_cache: tuple[datetime, str, pd.DataFrame] | None = None

    # ------------------------------------------------------------ sessions
    @property
    def session(self) -> Session:
        """Return this thread's web session, creating it on first use."""
        if self._injected_session is not None:
            return self._injected_session
        existing = getattr(self._local, "session", None)
        if existing is None:
            existing = self._session_factory()
            self._local.session = existing
        return existing

    def _reset_session(self) -> None:
        """Forget this thread's session and token, plus the company cache."""
        self._local.csrf_token = None
        with self._cache_lock:
            self._company_ids.clear()
        if self._injected_session is not None:
            try:
                self._injected_session.cookies.clear()
            except Exception:  # pragma: no cover - defensive
                pass
            return
        old = getattr(self._local, "session", None)
        self._local.session = None
        if old is not None:
            try:
                old.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _with_session_retry(self, label: str, action: Callable[[], T]) -> T:
        """Run a ShareSansar action; on 419/403 refresh the session and retry once."""
        try:
            return action()
        except SessionExpiredError as exc:
            logger.warning(
                "%s rejected the session (%s). Fetching a fresh token and retrying once.",
                label,
                exc,
            )
            self._reset_session()
            time.sleep(self.session_retry_backoff_seconds)
            return action()

    def _check_sharesansar_response(self, response: Response, label: str) -> None:
        """Raise the right error for a non-200 ShareSansar response."""
        if response.status_code in self.SESSION_ERROR_STATUSES:
            raise SessionExpiredError(f"{label} returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise RequestException(f"{label} returned HTTP {response.status_code}: {response.text[:200]}")

    # --------------------------------------------------------- cache rules
    def _fresh_for_session_date(self, fetched_at: datetime, session_date: date) -> bool:
        """Return whether a table for ``session_date`` fetched at ``fetched_at`` is still usable."""
        now = self.calendar.now()
        age = (now - fetched_at).total_seconds()
        close_at = datetime.combine(session_date, self.calendar.close_time, tzinfo=self.calendar.tz)
        if now <= close_at:
            # That day's session has not finished: the table is still changing.
            return age < self.live_table_seconds
        if fetched_at >= close_at + self.POST_CLOSE_SETTLE:
            return True
        # Fetched before (or just after) the close: only trust it briefly.
        return fetched_at >= close_at and age < self.live_table_seconds

    def _latest_cache_fresh(self, fetched_at: datetime) -> bool:
        """Return whether the cached "latest" table still reflects the latest session."""
        now = self.calendar.now()
        age = (now - fetched_at).total_seconds()
        if self.calendar.is_market_open(now):
            return age < self.live_table_seconds
        last_close = self.calendar.last_close_datetime(now)
        if fetched_at >= last_close + self.POST_CLOSE_SETTLE:
            return True
        return fetched_at >= last_close and age < self.live_table_seconds

    # ------------------------------------------------------------- public
    def fetch_ohlcv(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """Fetch and normalize daily OHLCV data for a NEPSE symbol."""
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol must be a non-empty string")
        if days <= 0:
            raise ValueError("days must be a positive integer")

        end_date = datetime.now(tz=timezone.utc)
        start_date = end_date - timedelta(days=days)

        try:
            result = self._with_session_retry(
                f"ShareSansar price history for {normalized_symbol}",
                lambda: self._fetch_ohlcv_from_sharesansar_price_history(
                    symbol=normalized_symbol,
                    end_date=end_date,
                    days=days,
                ),
            )
            SOURCE_HEALTH.record_success(
                SOURCE_PRICE_HISTORY,
                data_date=max(result.index.date) if not result.empty else None,
            )
            return self._append_latest_market_row(normalized_symbol, result)
        except Exception as primary_exc:
            SOURCE_HEALTH.record_failure(SOURCE_PRICE_HISTORY, primary_exc)
            logger.warning(
                "ShareSansar company price-history API failed for %s; trying legacy "
                "NepseAlpha route. Error=%s",
                normalized_symbol,
                primary_exc,
            )

        params: dict[str, str | int] = {
            "symbol": normalized_symbol,
            "resolution": "1D",
            "from": int(start_date.timestamp()),
            "to": int(end_date.timestamp()),
        }
        try:
            payload = self._get_json_with_retries(params=params)
            result = self._payload_to_dataframe(payload=payload, symbol=normalized_symbol)
            SOURCE_HEALTH.record_success(SOURCE_NEPSEALPHA)
            return self._append_latest_market_row(normalized_symbol, result)
        except (RequestException, ValueError) as exc:
            SOURCE_HEALTH.record_failure(SOURCE_NEPSEALPHA, exc)
            logger.warning(
                "Legacy NepseAlpha route failed for %s; falling back to slower "
                "ShareSansar daily market table. Error=%s",
                normalized_symbol,
                exc,
            )
            result = self._fetch_ohlcv_from_sharesansar(
                symbol=normalized_symbol,
                end_date=end_date,
                days=min(days, self.SHARESANSAR_FALLBACK_MAX_DAYS),
            )
            return self._append_latest_market_row(normalized_symbol, result)

    # ---------------------------------------------------- NepseAlpha route
    def _get_json_with_retries(self, params: dict[str, str | int]) -> dict[str, Any]:
        """Execute a GET request with exponential backoff and return JSON."""
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    self.BASE_URL,
                    params=params,
                    headers=self.DEFAULT_HEADERS,
                    timeout=self.timeout_seconds,
                )
                if response.status_code in (403, 404):
                    # Blocked or removed: retrying the same request cannot help.
                    raise RequestException(f"NepseAlpha returned HTTP {response.status_code}")
                self._assert_successful_response(response)
                return response.json()

            except RequestException as exc:
                last_error = exc
                logger.info(
                    "Historical OHLCV request failed on attempt %s/%s: %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
                if "HTTP 403" in str(exc) or "HTTP 404" in str(exc):
                    break

            except ValueError as exc:
                last_error = exc
                logger.info(
                    "Failed to decode historical OHLCV JSON on attempt %s/%s: %s",
                    attempt,
                    self.max_retries,
                    exc,
                )

            if attempt < self.max_retries:
                time.sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))

        if last_error is not None:
            raise RequestException(
                f"Failed to fetch historical OHLCV data after {self.max_retries} attempts"
            ) from last_error

        raise RequestException("Failed to fetch historical OHLCV data for unknown reasons")

    @staticmethod
    def _assert_successful_response(response: Response) -> None:
        """Assert that the HTTP response is successful."""
        if response.status_code != 200:
            logger.info(
                "Historical OHLCV endpoint returned non-200 status. Status=%s Body=%s",
                response.status_code,
                response.text[:200],
            )
            response.raise_for_status()

    @staticmethod
    def _payload_to_dataframe(payload: dict[str, Any], symbol: str) -> pd.DataFrame:
        """Convert TradingView-style JSON payload into a typed OHLCV DataFrame."""
        status = str(payload.get("s", "ok")).lower()
        if status not in {"ok", "success"}:
            logger.warning("Historical endpoint returned non-ok status for %s: %s", symbol, status)

        required_keys = ("t", "o", "h", "l", "c", "v")
        missing_keys = [key for key in required_keys if key not in payload]
        if missing_keys:
            raise ValueError(
                f"Historical payload for {symbol} is missing keys: {', '.join(missing_keys)}"
            )

        series_lengths = {key: len(payload[key]) for key in required_keys}
        if len(set(series_lengths.values())) != 1:
            raise ValueError(
                f"Historical payload arrays for {symbol} have inconsistent lengths: {series_lengths}"
            )

        if series_lengths["t"] == 0:
            logger.info("Historical endpoint returned no OHLCV rows for %s.", symbol)
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        index = pd.to_datetime(payload["t"], unit="s", utc=True)
        try:
            index = index.tz_convert("Asia/Kathmandu")
        except Exception as exc:
            logger.warning(
                "Could not convert OHLCV index to Asia/Kathmandu timezone; keeping UTC. %s",
                exc,
            )

        df = pd.DataFrame(
            {
                "Open": pd.to_numeric(payload["o"], errors="coerce"),
                "High": pd.to_numeric(payload["h"], errors="coerce"),
                "Low": pd.to_numeric(payload["l"], errors="coerce"),
                "Close": pd.to_numeric(payload["c"], errors="coerce"),
                "Volume": pd.to_numeric(payload["v"], errors="coerce"),
            },
            index=index,
        )

        df.index.name = "DateTime"
        df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        df = df[df["Volume"] > 0]
        df = df.sort_index()

        logger.info("Fetched %s clean OHLCV rows for %s.", len(df), symbol)
        return df

    # ------------------------------------------- ShareSansar price history
    def _fetch_ohlcv_from_sharesansar_price_history(
        self,
        symbol: str,
        end_date: datetime,
        days: int,
    ) -> pd.DataFrame:
        """Fetch per-symbol OHLCV rows from ShareSansar's company DataTables route."""
        company_id, csrf_token, referer_url = self._get_sharesansar_company_context(symbol)
        start_date = end_date.date() - timedelta(days=days)
        page_size = self.SHARESANSAR_PRICE_HISTORY_PAGE_SIZE
        start = 0
        draw = 1
        total_records: int | None = None
        rows: list[dict[str, Any]] = []

        headers = {
            **self.DEFAULT_HEADERS,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.sharesansar.com",
            "Referer": referer_url,
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }

        while total_records is None or start < total_records:
            response = self.session.post(
                self.SHARESANSAR_COMPANY_PRICE_HISTORY_URL,
                headers=headers,
                data={
                    "company": company_id,
                    "draw": str(draw),
                    "start": str(start),
                    "length": str(page_size),
                },
                timeout=self.timeout_seconds,
            )
            self._check_sharesansar_response(response, "ShareSansar company price-history")

            try:
                payload = response.json()
            except ValueError as exc:
                raise RequestException("ShareSansar company price-history returned invalid JSON") from exc

            page_rows = payload.get("data") or []
            if not isinstance(page_rows, list) or not page_rows:
                break

            rows.extend(page_rows)
            total_records = self._safe_int(payload.get("recordsTotal")) or len(rows)

            page_dates = pd.to_datetime(
                [row.get("published_date") for row in page_rows],
                errors="coerce",
            ).dropna()
            if not page_dates.empty and page_dates.min().date() < start_date:
                break

            start += len(page_rows)
            draw += 1

            if len(page_rows) < page_size:
                break

        if not rows:
            raise RequestException(f"ShareSansar company price-history returned no rows for {symbol}")

        result = self._sharesansar_price_history_to_ohlcv(
            rows=rows,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date.date(),
        )
        logger.info(
            "ShareSansar company price-history fetched %s clean OHLCV rows for %s.",
            len(result),
            symbol,
        )
        return result

    def _get_sharesansar_company_context(self, symbol: str) -> tuple[str, str, str]:
        """Return the company id, this thread's security token and the referer URL."""
        profile_url = self.SHARESANSAR_COMPANY_URL.format(symbol=symbol)
        with self._cache_lock:
            cached_company_id = self._company_ids.get(symbol)
        thread_token = getattr(self._local, "csrf_token", None)
        if cached_company_id and thread_token:
            return cached_company_id, thread_token, profile_url

        response = self.session.get(
            profile_url,
            headers={**self.DEFAULT_HEADERS, "Referer": "https://www.sharesansar.com/"},
            timeout=self.timeout_seconds,
        )
        self._check_sharesansar_response(response, f"ShareSansar company page for {symbol}")

        soup = BeautifulSoup(response.text, "html.parser")
        company_node = soup.find(id="companyid")
        company_id = company_node.get_text(strip=True) if company_node else ""
        token_node = soup.find("meta", attrs={"name": "_token"})
        csrf_token = str(token_node.get("content", "")).strip() if token_node else ""

        if not company_id:
            raise ValueError(f"ShareSansar company id was not found for {symbol}")
        if not csrf_token:
            raise ValueError(f"ShareSansar company CSRF token was not found for {symbol}")

        with self._cache_lock:
            self._company_ids[symbol] = company_id
        self._local.csrf_token = csrf_token
        return company_id, csrf_token, profile_url

    @staticmethod
    def _sharesansar_price_history_to_ohlcv(
        rows: list[dict[str, Any]],
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Normalize ShareSansar company price-history JSON into OHLCV columns."""
        working_df = pd.DataFrame(rows).rename(
            columns={
                "published_date": "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "traded_quantity": "Volume",
            }
        )

        required_columns = ("Date", "Open", "High", "Low", "Close", "Volume")
        missing_columns = [column for column in required_columns if column not in working_df.columns]
        if missing_columns:
            raise ValueError(
                f"ShareSansar company price-history for {symbol} is missing columns: "
                f"{', '.join(missing_columns)}"
            )

        working_df["Date"] = pd.to_datetime(working_df["Date"], errors="coerce")
        working_df = working_df[
            (working_df["Date"].dt.date >= start_date)
            & (working_df["Date"].dt.date <= end_date)
        ].copy(deep=True)

        for column in ("Open", "High", "Low", "Close", "Volume"):
            working_df[column] = (
                working_df[column]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("%", "", regex=False)
                .str.strip()
                .replace({"": pd.NA, "-": pd.NA, "nan": pd.NA})
            )
            working_df[column] = pd.to_numeric(working_df[column], errors="coerce")

        localized_index = working_df["Date"].dt.tz_localize("Asia/Kathmandu")
        ohlcv_df = pd.DataFrame(
            {
                "Open": working_df["Open"].to_numpy(),
                "High": working_df["High"].to_numpy(),
                "Low": working_df["Low"].to_numpy(),
                "Close": working_df["Close"].to_numpy(),
                "Volume": working_df["Volume"].to_numpy(),
            },
            index=pd.DatetimeIndex(localized_index),
        )
        ohlcv_df.index.name = "DateTime"
        ohlcv_df = ohlcv_df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        ohlcv_df = ohlcv_df[ohlcv_df["Volume"] > 0].sort_index()
        return ohlcv_df

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        """Convert JSON scalar values into optional integers."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # --------------------------------------------- ShareSansar daily tables
    def _fetch_ohlcv_from_sharesansar(
        self,
        symbol: str,
        end_date: datetime,
        days: int,
    ) -> pd.DataFrame:
        """Fallback OHLCV source using ShareSansar's daily AJAX market table.

        ShareSansar provides one market-wide table per trading day rather than a
        direct per-symbol history endpoint. Each date's table is cached so one
        scanner run pays the date-fetch cost once and reuses it for every symbol.
        """
        all_rows: list[pd.DataFrame] = []
        start_date = end_date.date() - timedelta(days=days)
        current_date = start_date

        logger.info(
            "Starting ShareSansar fallback for %s across %s calendar days.",
            symbol,
            days,
        )

        while current_date <= end_date.date():
            date_text = current_date.strftime("%Y-%m-%d")
            try:
                daily_df = self._get_sharesansar_daily_table(date_text)
            except Exception as exc:
                logger.debug("No ShareSansar fallback data for %s: %s", date_text, exc)
                current_date += timedelta(days=1)
                continue

            if not daily_df.empty and "Symbol" in daily_df.columns:
                symbol_rows = daily_df[daily_df["Symbol"].astype(str).str.upper() == symbol]
                if not symbol_rows.empty:
                    all_rows.append(symbol_rows.copy(deep=True))

            current_date += timedelta(days=1)

        if not all_rows:
            raise RequestException(f"ShareSansar fallback returned no OHLCV rows for {symbol}")

        result = pd.concat(all_rows, ignore_index=True)
        return self._sharesansar_table_to_ohlcv(result, symbol)

    def _ajax_headers(self) -> dict[str, str]:
        return {
            **self.DEFAULT_HEADERS,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.sharesansar.com",
            "Referer": self.SHARESANSAR_TOKEN_URL,
            "X-Requested-With": "XMLHttpRequest",
        }

    def _get_sharesansar_daily_table(self, date_text: str) -> pd.DataFrame:
        """Fetch and cache ShareSansar's all-symbol market table for one date."""
        try:
            session_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid date for ShareSansar daily table: {date_text}") from exc

        with self._cache_lock:
            if date_text in self._daily_empty_dates:
                raise ValueError(f"ShareSansar returned no records for {date_text}")
            cached = self._daily_cache.get(date_text)
            if cached is not None:
                fetched_at, cached_df = cached
                if self._fresh_for_session_date(fetched_at, session_date):
                    self._daily_cache.move_to_end(date_text)
                    return cached_df
                del self._daily_cache[date_text]

        daily_df = self._with_session_retry(
            f"ShareSansar daily table {date_text}",
            lambda: self._download_daily_table(date_text, session_date),
        )
        with self._cache_lock:
            self._daily_cache[date_text] = (self.calendar.now(), daily_df)
            while len(self._daily_cache) > self.DAILY_CACHE_MAX_ENTRIES:
                self._daily_cache.popitem(last=False)
        return daily_df

    def _download_daily_table(self, date_text: str, session_date: date) -> pd.DataFrame:
        """Download one dated market table (no caching, no retry)."""
        token = self._get_sharesansar_csrf_token()
        response = self.session.post(
            self.SHARESANSAR_AJAX_URL,
            headers=self._ajax_headers(),
            data={"_token": token, "sector": "all_sec", "date": date_text},
            timeout=self.timeout_seconds,
        )
        self._check_sharesansar_response(response, f"ShareSansar daily AJAX for {date_text}")

        try:
            tables = pd.read_html(StringIO(response.text))
        except ValueError:
            tables = []
        if not tables or tables[0].empty or "No Record Found" in tables[0].to_string():
            # A finished day with no table (weekend, holiday) will never get one.
            if session_date < self.calendar.now().date():
                with self._cache_lock:
                    self._daily_empty_dates.add(date_text)
            raise ValueError(f"ShareSansar returned no records for {date_text}")

        daily_df = tables[0]
        daily_df.columns = [str(column).strip() for column in daily_df.columns]
        daily_df["Date"] = date_text
        SOURCE_HEALTH.record_success(SOURCE_MARKET_TABLE, data_date=session_date)
        return daily_df

    def _append_latest_market_row(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """Overlay the latest ShareSansar market row onto historical OHLCV data.

        The company price-history endpoint often reflects completed candles only.
        During an active session, the daily market table is closer to a live bar
        because it carries the current LTP, high, low, open, and traded volume.
        This method replaces or appends the latest available daily row without
        disturbing the older 365-day historical chart.
        """
        if df.empty:
            return df

        today = self.calendar.now().date()
        daily_df: pd.DataFrame | None
        latest_date: date | None
        try:
            date_text, daily_df = self._get_sharesansar_latest_table()
            latest_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except Exception as exc:
            logger.debug("Latest market table overlay unavailable for %s: %s", symbol, exc)
            daily_df = None
            latest_date = None
            date_text = ""

        for offset in range(0, 14):
            if daily_df is not None and latest_date is not None:
                break
            candidate_date = today - timedelta(days=offset)
            candidate_text = candidate_date.strftime("%Y-%m-%d")
            try:
                candidate_df = self._get_sharesansar_daily_table(candidate_text)
            except Exception as exc:
                logger.debug("Latest market overlay unavailable for %s on %s: %s", symbol, candidate_text, exc)
                continue
            if candidate_df.empty:
                continue
            daily_df = candidate_df
            latest_date = candidate_date
            date_text = candidate_text
            break

        if daily_df is None or latest_date is None:
            return df

        if daily_df.empty or "Symbol" not in daily_df.columns:
            return df

        symbol_rows = daily_df[daily_df["Symbol"].astype(str).str.upper() == symbol]
        if symbol_rows.empty:
            return df

        row = symbol_rows.iloc[0]
        close = self._numeric_from_row(row, ("LTP", "Close"))
        open_price = self._numeric_from_row(row, ("Open", "Prev. Close", "Prev Close", "Close", "LTP"))
        high = self._numeric_from_row(row, ("High", "LTP", "Close"))
        low = self._numeric_from_row(row, ("Low", "LTP", "Close"))
        volume = self._numeric_from_row(row, ("Vol", "Volume"))

        if close is None or open_price is None or high is None or low is None or volume is None:
            logger.debug("Latest market overlay row incomplete for %s: %s", symbol, row.to_dict())
            return df

        working = df.copy(deep=True).sort_index()
        latest_existing_date = max(working.index.date)
        if latest_date < latest_existing_date:
            return working

        # Use the same timezone object as the history so the combined index
        # stays a proper date index instead of degrading to plain objects.
        index_tz = getattr(working.index, "tz", None) or self.calendar.tz
        live_index = pd.Timestamp(date_text).tz_localize(index_tz)
        same_day_mask = pd.Series(working.index.date == latest_date, index=working.index)
        if same_day_mask.any():
            working = working.loc[~same_day_mask].copy(deep=True)

        live_row = pd.DataFrame(
            {
                "Open": [float(open_price)],
                "High": [float(max(high, open_price, close))],
                "Low": [float(min(low, open_price, close))],
                "Close": [float(close)],
                "Volume": [float(volume)],
            },
            index=pd.DatetimeIndex([live_index], name=working.index.name or "DateTime"),
        )
        combined = pd.concat([working, live_row]).sort_index()
        logger.debug(
            "Applied latest ShareSansar market overlay for %s at %s. Close=%.2f Volume=%.0f",
            symbol,
            date_text,
            close,
            volume,
        )
        return combined

    def _get_sharesansar_latest_table(self) -> tuple[str, pd.DataFrame]:
        """Return ShareSansar's latest market-wide table, cached per trading date."""
        with self._cache_lock:
            cached = self._latest_cache
        if cached is not None and self._latest_cache_fresh(cached[0]):
            return cached[1], cached[2]

        # Only one thread downloads; the others wait and then reuse the result.
        with self._latest_fetch_lock:
            with self._cache_lock:
                cached = self._latest_cache
            if cached is not None and self._latest_cache_fresh(cached[0]):
                return cached[1], cached[2]
            try:
                date_text, daily_df = self._with_session_retry(
                    "ShareSansar latest table",
                    self._download_latest_table,
                )
            except Exception as exc:
                SOURCE_HEALTH.record_failure(SOURCE_MARKET_TABLE, exc)
                raise
            with self._cache_lock:
                self._latest_cache = (self.calendar.now(), date_text, daily_df)
            return date_text, daily_df

    def _download_latest_table(self) -> tuple[str, pd.DataFrame]:
        """Download the latest market table (no caching, no retry)."""
        today_text = self.calendar.now().strftime("%Y-%m-%d")
        token = self._get_sharesansar_csrf_token()
        response = self.session.post(
            self.SHARESANSAR_AJAX_URL,
            headers=self._ajax_headers(),
            data={"_token": token, "sector": "all_sec"},
            timeout=self.timeout_seconds,
        )
        self._check_sharesansar_response(response, "ShareSansar latest AJAX")

        soup = BeautifulSoup(response.text, "html.parser")
        as_of_node = soup.find("span", class_="text-org")
        date_text = as_of_node.get_text(strip=True) if as_of_node else today_text
        try:
            data_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            date_text = today_text
            data_date = self.calendar.now().date()

        tables = pd.read_html(StringIO(response.text))
        if not tables:
            raise ValueError("ShareSansar latest table response contained no tables")
        daily_df = tables[0]
        if daily_df.empty or "No Record Found" in daily_df.to_string():
            raise ValueError("ShareSansar latest table contained no records")

        daily_df.columns = [str(column).strip() for column in daily_df.columns]
        daily_df["Date"] = date_text
        SOURCE_HEALTH.record_success(SOURCE_MARKET_TABLE, data_date=data_date)
        return date_text, daily_df

    @staticmethod
    def _numeric_from_row(row: pd.Series, columns: tuple[str, ...]) -> float | None:
        """Return the first numeric value found in a set of possible columns."""
        for column in columns:
            if column not in row.index:
                continue
            value = row.get(column)
            try:
                parsed = pd.to_numeric(
                    str(value).replace(",", "").replace("%", "").strip(),
                    errors="coerce",
                )
            except Exception:
                continue
            if pd.notna(parsed):
                return float(parsed)
        return None

    def _get_sharesansar_csrf_token(self) -> str:
        """Return this thread's ShareSansar security token, fetching it if needed."""
        existing = getattr(self._local, "csrf_token", None)
        if existing:
            return existing

        response = self.session.get(
            self.SHARESANSAR_TOKEN_URL,
            headers={**self.DEFAULT_HEADERS, "Referer": "https://www.sharesansar.com/"},
            timeout=self.timeout_seconds,
        )
        self._check_sharesansar_response(response, "ShareSansar token page")

        token_match = pd.Series(response.text).str.extract(
            r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']',
            expand=False,
        ).iloc[0]
        if pd.isna(token_match):
            raise ValueError("ShareSansar CSRF token not found")

        self._local.csrf_token = str(token_match)
        return self._local.csrf_token

    @staticmethod
    def _sharesansar_table_to_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Normalize ShareSansar table rows into the standard OHLCV DataFrame."""
        column_mapping = {
            "Vol": "Volume",
            "Prev. Close": "PreviousClose",
            "Prev Close": "PreviousClose",
        }
        working_df = df.rename(columns=column_mapping).copy(deep=True)

        required_columns = ("Date", "Open", "High", "Low", "Close", "Volume")
        missing_columns = [column for column in required_columns if column not in working_df.columns]
        if missing_columns:
            raise ValueError(
                f"ShareSansar fallback table for {symbol} is missing columns: "
                f"{', '.join(missing_columns)}"
            )

        for column in ("Open", "High", "Low", "Close", "Volume"):
            working_df[column] = (
                working_df[column]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("%", "", regex=False)
                .str.strip()
                .replace({"": pd.NA, "-": pd.NA, "nan": pd.NA})
            )
            working_df[column] = pd.to_numeric(working_df[column], errors="coerce")

        index = pd.to_datetime(working_df["Date"], errors="coerce").dt.tz_localize(
            "Asia/Kathmandu"
        )

        ohlcv_df = pd.DataFrame(
            {
                "Open": working_df["Open"].to_numpy(),
                "High": working_df["High"].to_numpy(),
                "Low": working_df["Low"].to_numpy(),
                "Close": working_df["Close"].to_numpy(),
                "Volume": working_df["Volume"].to_numpy(),
            },
            index=pd.DatetimeIndex(index),
        )
        ohlcv_df.index.name = "DateTime"
        ohlcv_df = ohlcv_df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        ohlcv_df = ohlcv_df[ohlcv_df["Volume"] > 0].sort_index()

        logger.info("ShareSansar fallback fetched %s clean OHLCV rows for %s.", len(ohlcv_df), symbol)
        return ohlcv_df
