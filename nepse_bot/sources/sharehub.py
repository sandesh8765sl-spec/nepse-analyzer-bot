"""ShareHub (sharehubnepal.com) public data feed.

Network access and parsing are kept apart: every ``parse_*`` function is pure
and is tested against saved sample responses in ``tests/fixtures``.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from nepse_bot.sources.http import PoliteClient, SourceError

SOURCE = "ShareHub"
BASE_URL = "https://sharehubnepal.com/data/api/v1"
PAGE_SIZE = 500

# ShareHub field name -> our fundamentals column
FUNDAMENTAL_COLUMNS: dict[str, str] = {
    "eps": "eps",
    "eps_a": "eps_annualized",
    "net_worth": "book_value",
    "roe": "roe",
    "roa": "roa",
    "net_profit": "net_profit",
    "distributable_profit": "distributable_profit",
    "paidup_capital": "paidup_capital",
    "npl": "npl",
    "capital_fund_to_rwa": "car",
    "cd_ratio": "cd_ratio",
}

# The Nepali fiscal year starts in mid-July. Approximate quarter ends (month, day)
# and whether they fall in the calendar year after the fiscal year starts.
_QUARTER_ENDS: dict[int, tuple[int, int, int]] = {1: (0, 10, 17), 2: (1, 1, 14), 3: (1, 4, 13), 4: (1, 7, 16)}
PUBLICATION_LAG_DAYS = 35


# ---------------------------------------------------------------- helpers
def _content(payload: Any) -> list[dict[str, Any]]:
    """Return the list of records inside a ShareHub response."""
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise SourceError(f"{SOURCE} answered without success: {str(payload)[:120]}")
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("content")
    if not isinstance(data, list):
        raise SourceError(f"{SOURCE} response has no record list")
    return [item for item in data if isinstance(item, dict)]


def _total_pages(payload: Any) -> int:
    data = payload.get("data") if isinstance(payload, dict) else None
    return int(data.get("totalPages") or 1) if isinstance(data, dict) else 1


def _day(value: Any) -> str | None:
    """Return the YYYY-MM-DD part of a date-like value."""
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_fiscal_year(text: Any) -> str:
    """Turn ``083/084`` or ``2082/2083`` into ``2082/2083`` form."""
    match = re.search(r"(\d{2,4})\s*[/-]\s*(\d{2,4})", str(text or ""))
    if match is None:
        return str(text or "").strip()
    first, second = int(match.group(1)), int(match.group(2))
    first = first + 2000 if first < 1000 else first
    second = second + 2000 if second < 1000 else second
    return f"{first}/{second}"


def quarter_end_date(fiscal_year: str, quarter: int) -> date | None:
    """Approximate calendar date on which a Nepali fiscal quarter ends."""
    match = re.match(r"(\d{4})/", fiscal_year)
    if match is None or quarter not in _QUARTER_ENDS:
        return None
    start_year_ad = int(match.group(1)) - 57  # BS 2083 starts in AD 2026
    year_offset, month, day = _QUARTER_ENDS[quarter]
    return date(start_year_ad + year_offset, month, day)


# ---------------------------------------------------------------- parsers
def parse_securities(payload: Any) -> list[dict[str, Any]]:
    """Parse the security master list."""
    status_names = {"A": "active", "S": "suspended", "D": "delisted"}
    rows = []
    for item in _content(payload):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": item.get("name") or item.get("securityName"),
                "sector": item.get("sector"),
                "instrument_type": str(item.get("instrument") or "").strip().lower().replace(" ", "_") or None,
                "status": status_names.get(str(item.get("activeStatus") or "").upper(), "unknown"),
                "is_promoter": 1 if item.get("isPromoter") else 0,
                "nepse_id": item.get("nepseId"),
            }
        )
    return rows


def parse_price_history(payload: Any) -> list[dict[str, Any]]:
    """Parse daily price rows (works for stocks and for indices)."""
    rows = []
    for item in _content(payload):
        day, close = _day(item.get("date")), _number(item.get("close"))
        if day is None or close is None or close <= 0:
            continue
        rows.append(
            {
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "date": day,
                "open": _number(item.get("open")) or close,
                "high": _number(item.get("high")) or close,
                "low": _number(item.get("low")) or close,
                "close": close,
                "volume": _number(item.get("volume")) or 0.0,
                "turnover": _number(item.get("turnover")) or 0.0,
                "trades": int(_number(item.get("transactions")) or 0),
            }
        )
    return rows


def parse_dividends(payload: Any) -> list[dict[str, Any]]:
    """Parse dividend announcements into bonus and cash corporate actions."""
    rows = []
    for item in _content(payload):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        common = {
            "symbol": symbol,
            "fiscal_year": normalize_fiscal_year(item.get("fiscalYear")),
            "announcement_date": _day(item.get("announcementDate")),
            "book_closure_date": _day(item.get("bookClosureDate")),
            "status": item.get("status"),
            "source": SOURCE,
            "source_id": str(item.get("id") or ""),
        }
        bonus, cash = _number(item.get("bonus")) or 0.0, _number(item.get("cash")) or 0.0
        if bonus > 0:
            rows.append({**common, "action_type": "bonus", "ratio": bonus, "amount": None})
        if cash > 0:
            rows.append({**common, "action_type": "cash_dividend", "ratio": None, "amount": cash})
    return rows


def parse_right_ratio(text: Any) -> float | None:
    """Turn ``1:0.50`` (or ``10:3``) into new shares per share held."""
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$", str(text or ""))
    if match is None:
        return None
    held, new = float(match.group(1)), float(match.group(2))
    return new / held if held > 0 and new > 0 else None


def parse_rights(payload: Any) -> list[dict[str, Any]]:
    """Parse right-share offerings into corporate actions."""
    rows = []
    for item in _content(payload):
        symbol = str(item.get("symbol") or "").strip().upper()
        ratio = parse_right_ratio(item.get("rightShareRatio"))
        if not symbol or ratio is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "action_type": "right",
                "fiscal_year": "",
                "ratio": ratio,
                "amount": _number(item.get("price")) or 100.0,
                "announcement_date": _day(item.get("openingDate")),
                "book_closure_date": _day(item.get("bookClosureDate")),
                "status": item.get("status"),
                "source": SOURCE,
                "source_id": str(item.get("id") or ""),
            }
        )
    return rows


def parse_fundamentals(payload: Any, first_seen: date | None = None) -> list[dict[str, Any]]:
    """Parse quarterly reports.

    ShareHub does not say when a report was published, so the date is
    estimated as quarter end + 35 days. When the bot itself sees a report
    appear earlier than that estimate, ``first_seen`` is used instead.
    """
    rows = []
    for item in _content(payload):
        symbol = str(item.get("symbol") or "").strip().upper()
        quarter_match = re.search(r"(\d)", str(item.get("quarter") or ""))
        if not symbol or quarter_match is None:
            continue
        quarter = int(quarter_match.group(1))
        fiscal_year = normalize_fiscal_year(item.get("fiscalYear"))
        values = {
            str(entry.get("key")): entry.get("value")
            for entry in item.get("values") or []
            if isinstance(entry, dict) and entry.get("value") is not None
        }
        if not values:
            # The free feed lists every quarter but only fills some of them in.
            continue
        period_end = quarter_end_date(fiscal_year, quarter)
        publication, publication_source = None, "estimated"
        if period_end is not None:
            estimated = period_end + timedelta(days=PUBLICATION_LAG_DAYS)
            if first_seen is not None and first_seen < estimated:
                # The report already exists today, so it was published by today.
                publication, publication_source = max(first_seen, period_end), "observed"
            else:
                publication = estimated
        row: dict[str, Any] = {
            "symbol": symbol,
            "fiscal_year": fiscal_year,
            "quarter": quarter,
            "period_end_date": period_end.isoformat() if period_end else None,
            "publication_date": publication.isoformat() if publication else None,
            "publication_date_source": publication_source,
            "source": SOURCE,
        }
        for source_key, column in FUNDAMENTAL_COLUMNS.items():
            row[column] = _number(values.get(source_key))
        row["extra_json"] = json.dumps(
            {key: value for key, value in values.items() if key not in FUNDAMENTAL_COLUMNS}, sort_keys=True
        )
        rows.append(row)
    return rows


def parse_indices(payload: Any) -> list[dict[str, Any]]:
    """Parse the list of market indices."""
    return [
        {"code": str(item.get("symbol") or "").upper(), "name": item.get("name"), "is_sector": 1 if item.get("isSubIndex") else 0}
        for item in _content(payload)
        if item.get("symbol")
    ]


def parse_announcements(payload: Any) -> list[dict[str, Any]]:
    """Parse company announcements into news rows."""
    rows = []
    for item in _content(payload):
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        rows.append(
            {
                "symbol": str(item.get("symbol") or "").strip().upper() or None,
                "title": title[:300],
                "source": SOURCE,
                "url": item.get("attachmentUrl") or f"sharehub-announcement-{item.get('id')}",
                "published_at": str(item.get("announcementDate") or "")[:19] or None,
                "sentiment": None,
                "kind": "announcement",
            }
        )
    return rows


def parse_lockins(payload: Any) -> list[dict[str, Any]]:
    """Parse the lock-in list: when promoter / mutual fund shares become free to sell."""
    rows = []
    for item in _content(payload):
        symbol = str(item.get("symbol") or "").strip().upper()
        unlock = _day(item.get("lockInEndDate"))
        holder = str(item.get("type") or "").strip()
        if not symbol or not unlock or not holder:
            continue
        rows.append(
            {
                "symbol": symbol,
                "holder_type": holder,
                "unlock_date": unlock,
                "allotment_date": _day(item.get("allotmentDate")),
                "locked_shares": _number(item.get("lockedShares")),
                "total_shares": _number(item.get("totalListedShares")) or _number(item.get("totalShares")),
                "source": SOURCE,
            }
        )
    return rows


# ----------------------------------------------------------------- client
class ShareHubClient:
    """Fetch data from ShareHub politely."""

    def __init__(self, client: PoliteClient | None = None) -> None:
        self.client = client or PoliteClient(SOURCE)

    def _pages(
        self, path: str, params: dict[str, Any], sample: str, max_pages: int | None = None, size: int = PAGE_SIZE
    ) -> list[Any]:
        payloads = []
        page = 1
        while True:
            payload = self.client.get_json(
                f"{BASE_URL}{path}", params={**params, "size": size, "page": page}, sample_name=f"{sample}.json"
            )
            payloads.append(payload)
            if page >= _total_pages(payload) or (max_pages is not None and page >= max_pages) or not _content(payload):
                return payloads
            page += 1

    def securities(self) -> list[dict[str, Any]]:
        """Return every listed security."""
        return [row for payload in self._pages("/security", {}, "security") for row in parse_securities(payload)]

    def price_history(self, symbol: str, since: str | None = None, size: int = PAGE_SIZE) -> list[dict[str, Any]]:
        """Return daily prices for a stock or index, newest first, back to ``since``."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.client.get_json(
                f"{BASE_URL}/price-history",
                params={"symbol": symbol, "size": size, "page": page},
                sample_name="price-history.json",
            )
            page_rows = parse_price_history(payload)
            rows.extend(page_rows)
            oldest = min((row["date"] for row in page_rows), default=None)
            if not page_rows or page >= _total_pages(payload) or (since and oldest and oldest < since):
                break
            page += 1
        return [row for row in rows if not since or row["date"] >= since]

    def dividends(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        """Return dividend announcements (all companies)."""
        return [row for payload in self._pages("/dividend", {}, "dividend", max_pages) for row in parse_dividends(payload)]

    def rights(self, max_pages: int | None = None) -> list[dict[str, Any]]:
        """Return right-share offerings (all companies)."""
        return [
            row
            for payload in self._pages("/public-offering", {"For": 2, "Type": 2}, "right-share", max_pages)
            for row in parse_rights(payload)
        ]

    def fundamentals(self, symbol: str, first_seen: date | None = None) -> list[dict[str, Any]]:
        """Return every quarterly report ShareHub holds for a company."""
        payload = self.client.get_json(f"{BASE_URL}/fundamental/values/{symbol}", sample_name="fundamental-values.json")
        return parse_fundamentals(payload, first_seen=first_seen)

    def indices(self) -> list[dict[str, Any]]:
        """Return the list of indices."""
        payload = self.client.get_json(f"{BASE_URL}/index", params={"size": 100}, sample_name="index.json")
        return parse_indices(payload)

    def lockins(self) -> list[dict[str, Any]]:
        """Return upcoming lock-in expiries (promoter and mutual fund shares)."""
        return [row for payload in self._pages("/lock-in-period", {}, "lock_in_period", size=100) for row in parse_lockins(payload)]

    def announcements(self, pages: int = 1) -> list[dict[str, Any]]:
        """Return the most recent company announcements."""
        return [
            row
            for payload in self._pages("/announcement", {}, "announcement", pages, size=100)
            for row in parse_announcements(payload)
        ]
