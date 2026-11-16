"""ShareSansar's latest quarterly report, used when ShareHub has nothing.

ShareSansar shows only the most recent quarter, so this is a fallback for the
current fundamentals, not a source of history.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from io import StringIO
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from nepse_bot.sources.http import SourceError, save_raw_sample
from nepse_bot.sources.sharehub import PUBLICATION_LAG_DAYS, normalize_fiscal_year, quarter_end_date

SOURCE = "ShareSansar"
REPORT_URL = "https://www.sharesansar.com/company-quarterly-report"

# our column -> label fragments to look for (lower case, first match wins)
_LABELS: dict[str, tuple[str, ...]] = {
    "eps_annualized": ("annualized eps", "eps annualized", "earnings per share"),
    "book_value": ("net worth per share", "networth per share", "book value per share"),
    "roe": ("return on equity",),
    "roa": ("return on assets",),
    "npl": ("non-performing loan", "npl"),
    "car": ("capital fund to rwa", "capital adequacy"),
    "cd_ratio": ("credit to deposit", "cd ratio"),
    "net_profit": ("net profit", "profit for the period", "profit/(loss) for the period"),
    "paidup_capital": ("paid up capital", "paid-up capital", "share capital"),
    "distributable_profit": ("distributable profit",),
}
_AMOUNTS_IN_THOUSANDS = {"net_profit", "paidup_capital", "distributable_profit"}
_ORDINALS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "first": 1, "second": 2, "third": 3, "fourth": 4}


def parse_quarterly_report(html: str, symbol: str, first_seen: date | None = None) -> dict[str, Any] | None:
    """Parse ShareSansar's quarterly report tables into one fundamentals row."""
    try:
        tables = pd.read_html(StringIO(html))
    except (ValueError, ImportError):  # no tables in the page
        return None
    values: dict[str, float] = {}
    extra: dict[str, float] = {}
    period: tuple[str, int] | None = None
    for table in tables:
        if table.shape[1] < 2:
            continue
        header = str(table.columns[1])
        match = re.search(r"(\w+)\s+Quarter\s+(\d{2,4}\s*/\s*\d{2,4})", header, flags=re.IGNORECASE)
        if match is None or match.group(1).lower() not in _ORDINALS:
            continue
        period = (normalize_fiscal_year(match.group(2)), _ORDINALS[match.group(1).lower()])
        in_thousands = "'000" in str(table.columns[0])
        for label, raw in zip(table.iloc[:, 0], table.iloc[:, 1]):
            number = pd.to_numeric(raw, errors="coerce")
            if pd.isna(number):
                continue
            text = str(label).strip().lower()
            extra[str(label).strip()] = float(number)
            for column, fragments in _LABELS.items():
                if column in values or not any(fragment in text for fragment in fragments):
                    continue
                scale = 1000.0 if in_thousands and column in _AMOUNTS_IN_THOUSANDS else 1.0
                values[column] = float(number) * scale
    if period is None or not values:
        return None

    fiscal_year, quarter = period
    period_end = quarter_end_date(fiscal_year, quarter)
    publication, publication_source = None, "estimated"
    if period_end is not None:
        estimated = period_end + timedelta(days=PUBLICATION_LAG_DAYS)
        if first_seen is not None and first_seen < estimated:
            publication, publication_source = max(first_seen, period_end), "observed"
        else:
            publication = estimated
    row: dict[str, Any] = {
        "symbol": symbol.strip().upper(),
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "period_end_date": period_end.isoformat() if period_end else None,
        "publication_date": publication.isoformat() if publication else None,
        "publication_date_source": publication_source,
        "source": SOURCE,
        "eps": None,
    }
    for column in ("eps_annualized", "book_value", "roe", "roa", "net_profit", "distributable_profit", "paidup_capital", "npl", "car", "cd_ratio"):
        row[column] = values.get(column)
    row["extra_json"] = json.dumps(extra, sort_keys=True)
    return row


def fetch_quarterly_report(fetcher: Any, symbol: str, first_seen: date | None = None) -> dict[str, Any] | None:
    """Download and parse the latest quarterly report for one company.

    Args:
        fetcher: The shared ``NepseHistoricalFetcher`` (it owns the ShareSansar
            session, security token and expired-session recovery).
    """
    normalized = symbol.strip().upper()

    def download() -> str:
        company_id, token, profile_url = fetcher._get_sharesansar_company_context(normalized)
        page = fetcher.session.get(profile_url, headers=fetcher.DEFAULT_HEADERS, timeout=fetcher.timeout_seconds)
        fetcher._check_sharesansar_response(page, f"ShareSansar company page for {normalized}")
        sector_node = BeautifulSoup(page.text, "html.parser").find(id="sector")
        response = fetcher.session.post(
            REPORT_URL,
            headers={
                **fetcher.DEFAULT_HEADERS,
                "X-CSRF-Token": token,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": profile_url,
                "Origin": "https://www.sharesansar.com",
            },
            data={"company": company_id, "symbol": normalized, "sector": sector_node.get_text(strip=True) if sector_node else ""},
            timeout=fetcher.timeout_seconds,
        )
        fetcher._check_sharesansar_response(response, f"ShareSansar quarterly report for {normalized}")
        return response.text

    try:
        html = fetcher._with_session_retry(f"ShareSansar quarterly report for {normalized}", download)
    except Exception as exc:
        raise SourceError(f"ShareSansar quarterly report failed for {normalized}: {exc}") from exc
    save_raw_sample(SOURCE, "quarterly-report.html", html)
    return parse_quarterly_report(html, normalized, first_seen=first_seen)
