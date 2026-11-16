"""Price fetcher: expired-session recovery, per-thread sessions, date-tied caches."""

from __future__ import annotations

import json
import threading
from typing import Any

import pandas as pd
import pytest

from nepse_bot.ingestion.historical_fetcher import NepseHistoricalFetcher, SessionExpiredError
from nepse_bot.utils.health import SOURCE_HEALTH

from conftest import fixture_text

TOKEN_PAGE = '<html><form><input type="hidden" name="_token" value="TEST-TOKEN-{n}"></form></html>'


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text if payload is None else json.dumps(payload)
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeShareSansar:
    """Stands in for the ShareSansar website."""

    def __init__(self, expire_first_posts: int = 0, always_expired: bool = False, status: int = 419) -> None:
        self.expire_first_posts = expire_first_posts
        self.always_expired = always_expired
        self.status = status
        self.calls: list[tuple[str, str]] = []
        self.tokens_issued = 0
        self.tokens_seen: list[str] = []
        self.cookies = self
        self.cleared = 0

    def clear(self) -> None:  # session.cookies.clear()
        self.cleared += 1

    def close(self) -> None:
        pass

    def get(self, url: str, **_kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url))
        self.tokens_issued += 1
        if "/company/" in url:
            return FakeResponse(text=fixture_text("sharesansar_company_NABIL.html").replace("TEST-TOKEN-1", f"TEST-TOKEN-{self.tokens_issued}"))
        return FakeResponse(text=TOKEN_PAGE.format(n=self.tokens_issued))

    def post(self, url: str, headers: dict[str, str] | None = None, data: dict[str, str] | None = None, **_kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url))
        self.tokens_seen.append((headers or {}).get("X-CSRF-Token") or (data or {}).get("_token", ""))
        if self.always_expired:
            return FakeResponse(status_code=self.status, text="Page Expired")
        if self.expire_first_posts > 0:
            self.expire_first_posts -= 1
            return FakeResponse(status_code=self.status, text="Page Expired")
        if "company-price-history" in url:
            return FakeResponse(payload=json.loads(fixture_text("sharesansar_price_history_NABIL.json")))
        if (data or {}).get("date"):
            return FakeResponse(text="<table><tr><th>S.No</th></tr><tr><td>No Record Found</td></tr></table>")
        return FakeResponse(text=fixture_text("sharesansar_latest_table.html"))

    def count(self, method: str, fragment: str) -> int:
        return sum(1 for verb, url in self.calls if verb == method and fragment in url)


def make_fetcher(site: FakeShareSansar, calendar) -> NepseHistoricalFetcher:
    return NepseHistoricalFetcher(
        session=site,  # type: ignore[arg-type]
        calendar=calendar,
        session_retry_backoff_seconds=0,
        backoff_base_seconds=0,
        live_table_seconds=120,
    )


# ------------------------------------------------------------ parsing (fixtures)
def test_price_history_fixture_parses_into_clean_bars(calendar) -> None:
    calendar.set_now("2026-11-18 16:00")
    fetcher = make_fetcher(FakeShareSansar(), calendar)
    bars = fetcher.fetch_ohlcv("NABIL", days=365)
    assert list(bars.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(bars) >= 8
    assert bars.index.is_monotonic_increasing
    assert (bars["High"] >= bars["Low"]).all() and (bars["Volume"] > 0).all()
    assert str(bars.index.tz) == "Asia/Kathmandu"


def test_latest_table_fixture_parses_with_its_date(calendar) -> None:
    calendar.set_now("2026-11-18 16:00")
    fetcher = make_fetcher(FakeShareSansar(), calendar)
    date_text, table = fetcher._get_sharesansar_latest_table()
    assert date_text == "2026-11-06"
    assert {"NABIL", "API", "CBBL", "NICA"} <= set(table["Symbol"])
    for column in ("Open", "High", "Low", "LTP", "Vol"):
        assert column in table.columns
    status = {item.name: item for item in SOURCE_HEALTH.snapshot()}["ShareSansar market table"]
    assert status.healthy and str(status.data_date) == "2026-11-06"


# ------------------------------------------------------------ 419 / 403 recovery
@pytest.mark.parametrize("status", [419, 403])
def test_expired_session_is_refreshed_and_retried_once(calendar, status: int) -> None:
    calendar.set_now("2026-11-18 16:00")
    site = FakeShareSansar(expire_first_posts=1, status=status)
    fetcher = make_fetcher(site, calendar)

    bars = fetcher.fetch_ohlcv("NABIL", days=365)

    assert not bars.empty, "the scan must succeed after one automatic retry"
    assert site.count("POST", "company-price-history") == 2
    assert site.count("GET", "/company/NABIL") == 2, "a fresh token must be fetched after the rejection"
    assert site.tokens_seen[0] != site.tokens_seen[1], "the retry must use the NEW token"
    assert site.cleared == 1, "the old session cookies must be dropped"


def test_expired_latest_table_recovers_too(calendar) -> None:
    calendar.set_now("2026-11-18 16:00")
    site = FakeShareSansar(expire_first_posts=1)
    fetcher = make_fetcher(site, calendar)
    date_text, table = fetcher._get_sharesansar_latest_table()
    assert date_text == "2026-11-06" and not table.empty
    assert site.count("GET", "today-share-price") == 2


def test_retry_happens_only_once(calendar) -> None:
    calendar.set_now("2026-11-18 16:00")
    site = FakeShareSansar(always_expired=True)
    fetcher = make_fetcher(site, calendar)
    with pytest.raises(SessionExpiredError):
        fetcher._get_sharesansar_latest_table()
    assert site.count("POST", "ajaxtodayshareprice") == 2, "exactly one retry, no endless loop"
    status = {item.name: item for item in SOURCE_HEALTH.snapshot()}["ShareSansar market table"]
    assert status.healthy is False


# ------------------------------------------------------------ sessions per thread
def test_each_thread_gets_its_own_session(calendar) -> None:
    created: list[object] = []

    def factory() -> FakeShareSansar:
        site = FakeShareSansar()
        created.append(site)
        return site

    fetcher = NepseHistoricalFetcher(session_factory=factory, calendar=calendar)  # type: ignore[arg-type]
    seen: dict[str, object] = {}

    def work(name: str) -> None:
        seen[name] = fetcher.session
        assert fetcher.session is seen[name], "the same thread must reuse its session"

    threads = [threading.Thread(target=work, args=(f"t{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(created) == 3 and len({id(item) for item in seen.values()}) == 3


# ------------------------------------------------------------ cache rules
def test_latest_table_cache_is_tied_to_the_trading_date(calendar) -> None:
    site = FakeShareSansar()
    fetcher = make_fetcher(site, calendar)

    calendar.set_now("2026-11-18 16:00")  # Wednesday, after the close
    fetcher._get_sharesansar_latest_table()
    calendar.set_now("2026-11-18 22:00")
    fetcher._get_sharesansar_latest_table()
    calendar.set_now("2026-11-19 10:30")  # Thursday before the open: still the same session
    fetcher._get_sharesansar_latest_table()
    assert site.count("POST", "ajaxtodayshareprice") == 1, "one download serves the whole evening and next morning"

    calendar.set_now("2026-11-19 15:30")  # Thursday after ITS close: Wednesday's table is out of date
    fetcher._get_sharesansar_latest_table()
    assert site.count("POST", "ajaxtodayshareprice") == 2


def test_latest_table_refreshes_quickly_during_the_session(calendar) -> None:
    site = FakeShareSansar()
    fetcher = make_fetcher(site, calendar)
    calendar.set_now("2026-11-18 12:00")
    fetcher._get_sharesansar_latest_table()
    calendar.set_now("2026-11-18 12:01")
    fetcher._get_sharesansar_latest_table()
    assert site.count("POST", "ajaxtodayshareprice") == 1, "reused for a short time"
    calendar.set_now("2026-11-18 12:05")
    fetcher._get_sharesansar_latest_table()
    assert site.count("POST", "ajaxtodayshareprice") == 2, "refreshed after the live time limit"


def test_table_fetched_during_session_is_not_reused_after_close(calendar) -> None:
    site = FakeShareSansar()
    fetcher = make_fetcher(site, calendar)
    calendar.set_now("2026-11-18 14:59")
    fetcher._get_sharesansar_latest_table()
    calendar.set_now("2026-11-18 15:30")
    fetcher._get_sharesansar_latest_table()
    assert site.count("POST", "ajaxtodayshareprice") == 2


def test_empty_past_date_is_remembered(calendar) -> None:
    calendar.set_now("2026-11-18 16:00")
    site = FakeShareSansar()
    fetcher = make_fetcher(site, calendar)
    for _ in range(3):
        with pytest.raises(ValueError):
            fetcher._get_sharesansar_daily_table("2026-11-15")  # a Sunday
    assert site.count("POST", "ajaxtodayshareprice") == 1, "a closed day is asked about only once"
