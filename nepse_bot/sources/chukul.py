"""Chukul: historical floorsheet (every trade of a past day, with buyer and seller broker).

NEPSE itself only publishes the current day. Chukul keeps the trade list from
February 2022 onwards, 500 trades per page, without a login. We only keep the
daily total per symbol and broker.
"""

from __future__ import annotations

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from nepse_bot.sources.http import USER_AGENT, SourceError

SOURCE = "Chukul"
FLOORSHEET_URL = "https://chukul.com/api/data/v2/floorsheet/bydate/"
PAGE_SIZE = 500


def aggregate_trades(day: str, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse one day's trades into one row per symbol and broker."""
    totals: dict[tuple[str, int], dict[str, float]] = defaultdict(
        lambda: {"buy_qty": 0.0, "sell_qty": 0.0, "buy_amount": 0.0, "sell_amount": 0.0}
    )
    for trade in trades:
        symbol = str(trade.get("symbol") or "").strip().upper()
        try:
            quantity = float(trade.get("quantity") or 0)
            amount = float(trade.get("amount") or 0)
            buyer, seller = int(trade["buyer"]), int(trade["seller"])
        except (KeyError, TypeError, ValueError):
            continue
        if not symbol or quantity <= 0:
            continue
        buy = totals[(symbol, buyer)]
        buy["buy_qty"] += quantity
        buy["buy_amount"] += amount
        sell = totals[(symbol, seller)]
        sell["sell_qty"] += quantity
        sell["sell_amount"] += amount
    return [{"date": day, "symbol": symbol, "broker": broker, **values} for (symbol, broker), values in sorted(totals.items())]


class DownloadInterrupted(Exception):
    """The download of a day was stopped on purpose (the bot needs the PC). Nothing of that day is saved."""


class ChukulClient:
    """Downloads one day's floorsheet, several pages at a time."""

    def __init__(self, workers: int = 3, pause_seconds: float = 0.2, max_retries: int = 5,
                 timeout_seconds: float = 40, session: Any | None = None, sleep: Any = time.sleep,
                 should_stop: Any | None = None) -> None:
        self.should_stop = should_stop      # asked before every request; True = give up this day now
        self.workers = max(1, int(workers))
        self.pause_seconds = float(pause_seconds)
        self.max_retries = int(max_retries)
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests.Session()
        self._sleep = sleep

    def _page(self, day: str, page: int) -> dict[str, Any]:
        last_error: Any = None
        for attempt in range(1, self.max_retries + 1):
            if self.should_stop is not None and self.should_stop():
                raise DownloadInterrupted(f"stopped while reading {day}")
            try:
                response = self.session.get(
                    FLOORSHEET_URL, params={"date": day, "page": page, "size": PAGE_SIZE},
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=self.timeout_seconds,
                )
                if response.status_code == 200:
                    payload = response.json()
                    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                        self._sleep(self.pause_seconds)
                        return payload
                    last_error = "unexpected answer"
                else:
                    last_error = f"HTTP {response.status_code}"
            except Exception as exc:  # network trouble or invalid JSON
                last_error = f"{type(exc).__name__} {exc}"
            # Back off harder when the site says "too many requests".
            self._sleep(min(120.0, 3.0 * (2 ** (attempt - 1))))
        raise SourceError(f"Chukul floorsheet {day} page {page} failed after {self.max_retries} attempts: {last_error}")

    def floorsheet_day(self, day: str) -> list[dict[str, Any]]:
        """All trades of one day. An empty list means Chukul has nothing for that day."""
        first = self._page(day, 1)
        trades = list(first["data"])
        pages = int(first.get("last_page") or 1)
        if not trades or pages <= 1:
            return trades
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda number: self._page(day, number), range(2, pages + 1)))
        for number, payload in enumerate(results, start=2):
            rows = payload["data"]
            if number < pages and len(rows) != PAGE_SIZE:
                raise SourceError(f"Chukul floorsheet {day} page {number} is incomplete ({len(rows)} trades)")
            trades.extend(rows)
        ids = {str(trade.get("transaction")) for trade in trades}
        if len(ids) != len(trades):
            raise SourceError(f"Chukul floorsheet {day} has repeated trades (the list moved while reading)")
        return trades
