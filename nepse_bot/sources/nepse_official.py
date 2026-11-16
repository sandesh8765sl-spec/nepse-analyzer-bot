"""NEPSE's own website (nepalstock.com) through the open-source NepseUnofficialApi.

This is the primary source for each day's prices and the only source for the
floorsheet (who bought and sold through which broker). NEPSE keeps roughly one
year of date-wise prices, so older history comes from ShareHub.

Parsing is separate from network access so it can be tested with saved samples.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from datetime import date
from typing import Any

from nepse_bot.sources.http import HostRateLimiter, SourceError, _GLOBAL_LIMITER, save_raw_sample

logger = logging.getLogger(__name__)

SOURCE = "NEPSE"
HOST = "nepalstock.com"
FLOORSHEET_PAGE_SIZE = 500

# NEPSE index id -> our index code (same codes ShareHub uses)
INDEX_CODES: dict[int, str] = {
    58: "NEPSE", 57: "SENSITIVE", 62: "FLOAT", 63: "SENFLOAT", 51: "BANKING", 52: "HOTELS",
    53: "OTHERS", 54: "HYDROPOWER", 55: "DEVBANK", 56: "MANUFACTURE", 59: "NONLIFEINSU",
    60: "FINANCE", 61: "TRADING", 64: "MICROFINANCE", 65: "LIFEINSU", 66: "MUTUAL", 67: "INVESTMENT",
}
MAIN_INDEX_CODES = {"NEPSE", "SENSITIVE", "FLOAT", "SENFLOAT"}


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- parsers
def parse_price_volume_history(payload: Any) -> list[dict[str, Any]]:
    """Parse the all-symbols price table for one business date."""
    content = payload.get("content") if isinstance(payload, dict) else payload
    if not isinstance(content, list):
        raise SourceError("NEPSE price history has no record list")
    rows = []
    for item in content:
        symbol = str(item.get("symbol") or "").strip().upper()
        close = _number(item.get("closePrice")) or _number(item.get("lastUpdatedPrice"))
        day = str(item.get("businessDate") or "")[:10]
        if not symbol or not day or close is None or close <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": _number(item.get("openPrice")) or close,
                "high": _number(item.get("highPrice")) or close,
                "low": _number(item.get("lowPrice")) or close,
                "close": close,
                "volume": _number(item.get("totalTradedQuantity")) or 0.0,
                "turnover": _number(item.get("totalTradedValue")) or 0.0,
                "trades": int(_number(item.get("totalTrades")) or 0),
            }
        )
    return rows


def parse_company_list(payload: Any) -> list[dict[str, Any]]:
    """Parse NEPSE's company list into security master rows."""
    status_names = {"A": "active", "S": "suspended", "D": "delisted"}
    rows = []
    for item in payload if isinstance(payload, list) else []:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": item.get("securityName") or item.get("companyName"),
                "sector": item.get("sectorName"),
                "instrument_type": str(item.get("instrumentType") or "").strip().lower().replace(" ", "_").replace("-", "_") or None,
                "status": status_names.get(str(item.get("status") or "").upper(), "unknown"),
                "nepse_id": item.get("id"),
            }
        )
    return rows


def parse_index_snapshot(main_payload: Any, sub_payload: Any, day: str) -> list[dict[str, Any]]:
    """Parse today's index values (used only if ShareHub's index history fails)."""
    rows = []
    for item in list(main_payload or []) + list(sub_payload or []):
        code = INDEX_CODES.get(int(item.get("id") or 0))
        value = _number(item.get("currentValue")) or _number(item.get("close"))
        if code is None or value is None:
            continue
        rows.append(
            {
                "index_code": code,
                "date": day,
                "open": None,
                "high": _number(item.get("high")),
                "low": _number(item.get("low")),
                "close": value,
                "volume": None,
                "turnover": None,
                "trades": None,
                "is_sector": 0 if code in MAIN_INDEX_CODES else 1,
            }
        )
    return rows


def aggregate_floorsheet(trades: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse individual trades into one row per date, symbol and broker.

    Returns the aggregated rows and the number of trades that carried no
    broker numbers (NEPSE hides them while the session is running).
    """
    totals: dict[tuple[str, str, int], dict[str, float]] = defaultdict(
        lambda: {"buy_qty": 0.0, "sell_qty": 0.0, "buy_amount": 0.0, "sell_amount": 0.0}
    )
    without_brokers = 0
    for trade in trades:
        symbol = str(trade.get("stockSymbol") or "").strip().upper()
        day = str(trade.get("businessDate") or "")[:10]
        quantity = _number(trade.get("contractQuantity")) or 0.0
        amount = _number(trade.get("contractAmount")) or 0.0
        buyer, seller = trade.get("buyerMemberId"), trade.get("sellerMemberId")
        if not symbol or not day or quantity <= 0:
            continue
        if buyer in (None, "") or seller in (None, ""):
            without_brokers += 1
            continue
        buy = totals[(day, symbol, int(buyer))]
        buy["buy_qty"] += quantity
        buy["buy_amount"] += amount
        sell = totals[(day, symbol, int(seller))]
        sell["sell_qty"] += quantity
        sell["sell_amount"] += amount
    rows = [
        {"date": day, "symbol": symbol, "broker": broker, **values}
        for (day, symbol, broker), values in sorted(totals.items())
    ]
    return rows, without_brokers


# ----------------------------------------------------------------- client
class NepseOfficialClient:
    """Thin, rate-limited wrapper around the NepseUnofficialApi library."""

    def __init__(self, api: Any | None = None, limiter: HostRateLimiter | None = None) -> None:
        self._api = api
        self._limiter = limiter or _GLOBAL_LIMITER
        self._lock = threading.Lock()

    @property
    def api(self) -> Any:
        """Create the library client on first use (it downloads a token)."""
        if self._api is None:
            from nepse import Nepse  # imported lazily: optional dependency

            self._api = Nepse()
        return self._api

    def _call(self, name: str, *args: Any, sample: str | None = None) -> Any:
        self._limiter.wait(HOST)
        try:
            with self._lock:
                result = getattr(self.api, name)(*args)
        except Exception as exc:
            raise SourceError(f"NEPSE {name} failed: {type(exc).__name__} {exc}".strip()) from exc
        if sample:
            try:
                save_raw_sample(SOURCE, f"{sample}.json", json.dumps(result, default=str)[:400_000])
            except Exception:  # pragma: no cover - sampling must never break ingestion
                pass
        return result

    def is_market_open(self) -> bool | None:
        """Return NEPSE's own open/closed flag, or None if it cannot be read."""
        try:
            status = self._call("getMarketStatus")
            return str(status.get("isOpen", "")).upper() == "OPEN"
        except SourceError:
            return None

    def live_prices(self) -> dict[str, float]:
        """Return the last traded price of every symbol traded today (monitoring only)."""
        prices: dict[str, float] = {}
        for item in self._call("getPriceVolume") or []:
            symbol = str(item.get("symbol") or "").strip().upper()
            value = _number(item.get("lastTradedPrice")) or _number(item.get("closePrice"))
            if symbol and value:
                prices[symbol] = value
        return prices

    def live_quotes(self) -> dict[str, dict[str, float | None]]:
        """Return price, volume and change of every symbol traded today (monitoring and alerts only)."""
        quotes: dict[str, dict[str, float | None]] = {}
        for item in self._call("getPriceVolume") or []:
            symbol = str(item.get("symbol") or "").strip().upper()
            price = _number(item.get("lastTradedPrice")) or _number(item.get("closePrice"))
            if not symbol or not price:
                continue
            quotes[symbol] = {
                "price": price,
                "volume": _number(item.get("totalTradeQuantity")) or _number(item.get("totalTradedQuantity")),
                "previous_close": _number(item.get("previousClose")) or _number(item.get("previousDayClosePrice")),
                "change_percent": _number(item.get("percentageChange")),
                "high": _number(item.get("highPrice")),
                "low": _number(item.get("lowPrice")),
            }
        return quotes

    def live_index(self) -> float | None:
        """The NEPSE index value right now (None if it cannot be read)."""
        try:
            for row in parse_index_snapshot(self._call("getNepseIndex"), [], ""):
                if row.get("index_code") == "NEPSE" and row.get("close"):
                    return float(row["close"])
        except Exception:
            return None
        return None

    def prices_for_date(self, day: date | str) -> list[dict[str, Any]]:
        """Return every symbol's prices for one business date (empty on a closed day)."""
        text = day.isoformat() if isinstance(day, date) else str(day)
        return parse_price_volume_history(self._call("getPriceVolumeHistory", text, sample="price-volume-history"))

    def companies(self) -> list[dict[str, Any]]:
        """Return NEPSE's company list."""
        return parse_company_list(self._call("getCompanyList", sample="company-list"))

    def index_snapshot(self, day: str) -> list[dict[str, Any]]:
        """Return today's index values."""
        return parse_index_snapshot(self._call("getNepseIndex", sample="index"), self._call("getNepseSubIndices"), day)

    def floorsheet(self, max_pages: int = 400) -> list[dict[str, Any]]:
        """Return today's individual trades (every page, politely)."""
        api = self.api
        url = f"{api.api_end_points['floor_sheet']}?&size={FLOORSHEET_PAGE_SIZE}&sort=contractId,desc"
        trades: list[dict[str, Any]] = []
        page, total_pages = 0, 1
        while page < min(total_pages, max_pages):
            self._limiter.wait(HOST)
            try:
                with self._lock:
                    sheet = api.requestPOSTAPI(
                        url=url if page == 0 else f"{url}&page={page}",
                        payload_generator=api.getPOSTPayloadIDForFloorSheet,
                    )
            except Exception as exc:
                raise SourceError(f"NEPSE floorsheet page {page} failed: {type(exc).__name__} {exc}".strip()) from exc
            block = sheet.get("floorsheets", {}) if isinstance(sheet, dict) else {}
            content = block.get("content") or []
            if page == 0:
                total_pages = int(block.get("totalPages") or 1)
                save_raw_sample(SOURCE, "floorsheet.json", json.dumps(content[:50], default=str))
            trades.extend(content)
            if not content:
                break
            page += 1
        return trades
