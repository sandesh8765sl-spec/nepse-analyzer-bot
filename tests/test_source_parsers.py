"""Parser tests against saved samples of real responses.

If a website changes its layout, refresh the sample in tests/fixtures and the
matching test fails loudly, instead of the bot silently storing empty data.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from nepse_bot.sources import nepse_official, sharehub
from nepse_bot.sources.http import SourceError
from nepse_bot.sources.sharesansar_reports import parse_quarterly_report

from conftest import fixture_text


def load(name: str):
    return json.loads(fixture_text(name))


# ------------------------------------------------------------------ ShareHub
def test_sharehub_securities() -> None:
    rows = sharehub.parse_securities(load("sharehub_security.json"))
    assert len(rows) == 12
    for row in rows:
        assert row["symbol"] == row["symbol"].upper() and row["symbol"]
        assert row["status"] in {"active", "suspended", "delisted", "unknown"}
        assert row["is_promoter"] in (0, 1)
    assert any(row["sector"] for row in rows)
    assert any(row["instrument_type"] for row in rows)


def test_sharehub_price_history() -> None:
    rows = sharehub.parse_price_history(load("sharehub_price_history.json"))
    assert len(rows) == 8
    for row in rows:
        assert date.fromisoformat(row["date"])
        assert row["close"] > 0 and row["high"] > 0 and row["low"] > 0
        assert row["volume"] >= 0 and row["turnover"] >= 0 and isinstance(row["trades"], int)
    assert len({row["symbol"] for row in rows}) == 1


def test_sharehub_dividends_split_into_bonus_and_cash() -> None:
    payload = load("sharehub_dividend.json")
    rows = sharehub.parse_dividends(payload)
    source_records = payload["data"]["content"]
    expected = sum(1 for r in source_records if (r.get("bonus") or 0) > 0) + sum(1 for r in source_records if (r.get("cash") or 0) > 0)
    assert len(rows) == expected > 0
    for row in rows:
        assert row["action_type"] in {"bonus", "cash_dividend"}
        assert row["fiscal_year"].count("/") == 1 and len(row["fiscal_year"]) == 9
        assert (row["ratio"] if row["action_type"] == "bonus" else row["amount"]) > 0
        assert row["source_id"]


def test_sharehub_rights_and_ratio_text() -> None:
    assert sharehub.parse_right_ratio("1:0.50") == pytest.approx(0.5)
    assert sharehub.parse_right_ratio("10:3") == pytest.approx(0.3)
    assert sharehub.parse_right_ratio("1:1") == pytest.approx(1.0)
    assert sharehub.parse_right_ratio("n/a") is None
    rows = sharehub.parse_rights(load("sharehub_right_share.json"))
    assert rows and all(row["action_type"] == "right" and row["ratio"] > 0 and row["amount"] > 0 for row in rows)


def test_sharehub_fundamentals_skip_empty_quarters_and_keep_numbers() -> None:
    payload = load("sharehub_fundamental_values_NABIL.json")
    rows = sharehub.parse_fundamentals(payload, first_seen=date(2026, 11, 13))
    filled = [item for item in payload["data"] if item["values"]]
    assert 0 < len(rows) == len(filled) < len(payload["data"]), "quarters with no values must be skipped"
    latest = max(rows, key=lambda row: row["period_end_date"])
    assert latest["symbol"] == "NABIL" and latest["fiscal_year"] == "2083/2084" and latest["quarter"] == 1
    assert latest["eps_annualized"] == pytest.approx(33.89)
    assert latest["book_value"] == pytest.approx(233.9)
    assert latest["npl"] is not None and latest["car"] is not None and latest["cd_ratio"] is not None
    assert "deposit" in json.loads(latest["extra_json"])


def test_fundamentals_publication_date_never_looks_ahead() -> None:
    payload = load("sharehub_fundamental_values_NABIL.json")
    rows = sharehub.parse_fundamentals(payload, first_seen=date(2026, 11, 13))
    by_period = {(row["fiscal_year"], row["quarter"]): row for row in rows}
    latest = by_period[("2083/2084", 1)]
    # Quarter ended 17 Oct 2026; the estimate would be 21 Nov, but the report
    # already exists on 13 Nov, so it is usable from 13 Nov.
    assert latest["period_end_date"] == "2026-10-17"
    assert latest["publication_date"] == "2026-11-13" and latest["publication_date_source"] == "observed"
    older = by_period[("2082/2083", 1)]
    assert older["publication_date"] == "2025-11-21" and older["publication_date_source"] == "estimated"
    for row in rows:
        assert row["publication_date"] >= row["period_end_date"], "a report cannot be published before its quarter ends"


def test_fiscal_year_and_quarter_dates() -> None:
    assert sharehub.normalize_fiscal_year("083/084") == "2083/2084"
    assert sharehub.normalize_fiscal_year("2082/2083") == "2082/2083"
    assert sharehub.quarter_end_date("2083/2084", 1) == date(2026, 10, 17)
    assert sharehub.quarter_end_date("2083/2084", 2) == date(2027, 1, 14)
    assert sharehub.quarter_end_date("2083/2084", 4) == date(2027, 7, 16)
    assert sharehub.quarter_end_date("garbage", 1) is None


def test_sharehub_indices_and_announcements() -> None:
    indices = sharehub.parse_indices(load("sharehub_index.json"))
    codes = {item["code"] for item in indices}
    assert {"NEPSE", "BANKING", "HYDROPOWER", "MICROFINANCE"} <= codes
    assert next(item for item in indices if item["code"] == "NEPSE")["is_sector"] == 0
    assert next(item for item in indices if item["code"] == "BANKING")["is_sector"] == 1
    news = sharehub.parse_announcements(load("sharehub_announcement.json"))
    assert news and all(item["title"] and item["kind"] == "announcement" and item["url"] for item in news)


def test_sharehub_rejects_error_payloads() -> None:
    with pytest.raises(SourceError):
        sharehub.parse_securities({"success": False, "message": "nope"})
    with pytest.raises(SourceError):
        sharehub.parse_price_history({"success": True, "data": "unexpected"})


# --------------------------------------------------------------------- NEPSE
def test_nepse_price_volume_history() -> None:
    rows = nepse_official.parse_price_volume_history(load("nepse_price_volume_history.json"))
    by_symbol = {row["symbol"]: row for row in rows}
    assert {"NABIL", "API", "CBBL", "NICA"} <= set(by_symbol)
    nabil = by_symbol["NABIL"]
    assert nabil["date"] == "2026-11-06" and nabil["close"] == pytest.approx(549.0)
    assert nabil["low"] <= nabil["open"] <= nabil["high"]
    assert nabil["volume"] > 0 and nabil["turnover"] > 0 and nabil["trades"] > 0


def test_nepse_company_list_and_index() -> None:
    companies = nepse_official.parse_company_list(load("nepse_company_list.json"))
    assert companies and all(row["status"] in {"active", "suspended", "delisted"} for row in companies)
    nabil = next(row for row in companies if row["symbol"] == "NABIL")
    assert nabil["sector"] == "Commercial Banks" and nabil["instrument_type"] == "equity"
    snapshot = load("nepse_index.json")
    rows = nepse_official.parse_index_snapshot(snapshot["main"], snapshot["sub"], "2026-11-13")
    codes = {row["index_code"]: row for row in rows}
    assert codes["NEPSE"]["close"] > 1000 and codes["NEPSE"]["is_sector"] == 0
    assert codes["BANKING"]["is_sector"] == 1 and len(codes) >= 15


def test_floorsheet_aggregation_per_broker() -> None:
    trades = [
        {"stockSymbol": "NABIL", "businessDate": "2026-11-13", "contractQuantity": 100, "contractAmount": 55000, "buyerMemberId": "34", "sellerMemberId": "58"},
        {"stockSymbol": "NABIL", "businessDate": "2026-11-13", "contractQuantity": 50, "contractAmount": 27500, "buyerMemberId": "34", "sellerMemberId": "7"},
        {"stockSymbol": "NABIL", "businessDate": "2026-11-13", "contractQuantity": 10, "contractAmount": 5500, "buyerMemberId": "58", "sellerMemberId": "34"},
    ]
    rows, hidden = nepse_official.aggregate_floorsheet(trades)
    assert hidden == 0
    by_broker = {row["broker"]: row for row in rows}
    assert by_broker[34]["buy_qty"] == 150 and by_broker[34]["sell_qty"] == 10 and by_broker[34]["buy_amount"] == 82500
    assert by_broker[58]["sell_qty"] == 100 and by_broker[58]["buy_qty"] == 10
    assert sum(r["buy_qty"] for r in rows) == sum(r["sell_qty"] for r in rows) == 160


def test_floorsheet_without_broker_numbers_is_reported_not_stored() -> None:
    # Real sample taken while the session was running: NEPSE hides brokers then.
    rows, hidden = nepse_official.aggregate_floorsheet(load("nepse_floorsheet_live.json"))
    assert rows == [] and hidden == 8


# --------------------------------------------------------------- ShareSansar
def test_sharesansar_quarterly_report_bank() -> None:
    row = parse_quarterly_report(fixture_text("sharesansar_quarterly_NABIL.html"), "nabil", first_seen=date(2026, 11, 13))
    assert row is not None and row["symbol"] == "NABIL"
    assert (row["fiscal_year"], row["quarter"]) == ("2083/2084", 1)
    assert row["book_value"] == pytest.approx(233.9) and row["npl"] == pytest.approx(4.06)
    assert row["car"] == pytest.approx(11.67) and row["cd_ratio"] == pytest.approx(80.01)
    assert row["paidup_capital"] == pytest.approx(33_409_847_000.0)


def test_sharesansar_quarterly_report_hydropower() -> None:
    row = parse_quarterly_report(fixture_text("sharesansar_quarterly_API.html"), "API")
    assert row is not None and (row["fiscal_year"], row["quarter"]) == ("2082/2083", 4)
    assert row["eps_annualized"] == pytest.approx(15.06) and row["book_value"] == pytest.approx(119.51)
    assert row["npl"] is None, "hydropower companies have no NPL"
    assert parse_quarterly_report("<html><body>No data available</body></html>", "API") is None
