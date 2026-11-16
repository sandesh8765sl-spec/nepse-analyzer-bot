"""Multi-source news fetcher for NEPSE research commands."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

from nepse_bot.research.source_ranker import SourceRanker
from nepse_bot.settings import DATA_DIR
from nepse_bot.utils.health import SOURCE_HEALTH
from nepse_bot.utils.locking import synchronize_public_methods
from nepse_bot.utils.safe_json import atomic_write_json, load_json
from nepse_bot.research.sentiment_analyzer import SentimentAnalyzer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    """One research item with anti-hallucination metadata."""

    title: str
    source: str
    source_url: str
    published_date: datetime | None
    related_symbol: str | None
    related_company: str | None
    category: str
    sentiment_score: int
    impact_score: int
    reliability_score: float
    summary: str
    why_it_matters: str
    freshness: str
    final_score: float
    is_rumor: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize the news item for JSON cache storage."""
        data = asdict(self)
        data["published_date"] = self.published_date.isoformat() if self.published_date else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsItem":
        """Deserialize a news item from cache."""
        value = dict(data)
        date_text = value.get("published_date")
        value["published_date"] = _parse_datetime(date_text) if date_text else None
        return cls(**value)


@synchronize_public_methods
class ResearchCache:
    """Small JSON cache for source responses."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DATA_DIR / "research_cache.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def get_items(self, key: str) -> list[NewsItem] | None:
        """Return cached items if still valid."""
        state = self._load()
        entry = state.get("entries", {}).get(key)
        if not isinstance(entry, dict):
            return None
        expires_at = _parse_datetime(entry.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return None
        raw_items = entry.get("items", [])
        if not isinstance(raw_items, list):
            return None
        try:
            return [NewsItem.from_dict(item) for item in raw_items if isinstance(item, dict)]
        except TypeError as exc:
            logger.warning("Ignoring invalid research cache entry %s: %s", key, exc)
            return None

    def set_items(self, key: str, items: list[NewsItem], ttl_minutes: int) -> None:
        """Persist items with a TTL."""
        state = self._load()
        entries = state.setdefault("entries", {})
        entries[key] = {
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(),
            "items": [item.to_dict() for item in items],
        }
        self._save(state)

    def _load(self) -> dict[str, Any]:
        """Load cache JSON."""
        with self._lock:
            data = load_json(self.path, lambda: {"entries": {}})
            if not isinstance(data.get("entries"), dict):
                data["entries"] = {}
            # Expired entries are dropped every time the cache is loaded, so
            # the file cannot grow without limit.
            self._drop_expired(data["entries"])
            return data

    def _save(self, data: dict[str, Any]) -> None:
        """Save cache JSON."""
        with self._lock:
            atomic_write_json(self.path, data)

    @staticmethod
    def _drop_expired(entries: dict[str, Any]) -> int:
        """Remove expired or malformed entries in place; return how many."""
        now = datetime.now(timezone.utc)
        stale = [
            key
            for key, entry in entries.items()
            if not isinstance(entry, dict)
            or (_parse_datetime(entry.get("expires_at")) or now) <= now
        ]
        for key in stale:
            del entries[key]
        return len(stale)

    def prune_expired(self) -> int:
        """Delete expired entries from the file; return how many were removed."""
        if not self.path.exists():
            return 0
        raw = load_json(self.path, lambda: {"entries": {}})
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return 0
        removed = self._drop_expired(entries)
        if removed > 0:
            self._save(raw)
        return removed


class MultiSourceNewsFetcher:
    """Fetch NEPSE news from RSS, source-scoped RSS, and portal HTML fallbacks."""

    GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    SOURCE_SITES = (
        "sharesansar.com",
        "merolagani.com",
        "nepalipaisa.com",
        "nepsealpha.com",
        "nepalstock.com",
        "sebon.gov.np",
    )
    PORTAL_PAGES = (
        "https://www.sharesansar.com/category/latest",
        "https://www.sharesansar.com/category/company-news",
        "https://www.sharesansar.com/category/nepse",
        "https://merolagani.com/NewsList.aspx",
        "https://www.nepalipaisa.com/News.aspx",
    )

    def __init__(
        self,
        cache: ResearchCache | None = None,
        session: requests.Session | None = None,
        timeout_seconds: int = 14,
    ) -> None:
        self.cache = cache or ResearchCache()
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.source_ranker = SourceRanker()
        self.sentiment_analyzer = SentimentAnalyzer()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ne;q=0.7",
        }

    def fetch_symbol_news(self, symbol: str, limit: int = 10, refresh: bool = False) -> list[NewsItem]:
        """Fetch latest company-specific news for a symbol."""
        normalized = symbol.strip().upper()
        cache_key = f"symbol_news:{normalized}:{limit}"
        if not refresh:
            cached = self.cache.get_items(cache_key)
            if cached is not None:
                return cached[:limit]

        queries = [
            f'"{normalized}" NEPSE stock',
            f'"{normalized}" dividend bonus AGM book closure right share',
            f'"{normalized}" ShareSansar OR MeroLagani OR NepaliPaisa',
            f'"{normalized}" site:sharesansar.com OR site:merolagani.com',
        ]
        items = self._items_from_queries(queries, normalized, limit=limit * 2)
        items.extend(self._portal_items(keyword=normalized, related_symbol=normalized, limit=limit))
        cleaned = self._dedupe(items)[:limit]
        self.cache.set_items(cache_key, cleaned, ttl_minutes=45)
        return cleaned

    def _items_from_queries(self, queries: list[str], related_symbol: str | None, limit: int) -> list[NewsItem]:
        """Fetch and normalize items from multiple RSS queries."""
        items: list[NewsItem] = []
        for query in queries:
            try:
                items.extend(self._google_news_rss(query=query, related_symbol=related_symbol, limit=max(3, limit // 2)))
            except Exception as exc:
                SOURCE_HEALTH.record_failure("Google News RSS", exc)
                logger.info("Google News RSS query failed for %s: %s", query, exc)
        return self._dedupe(items)[:limit]

    def _google_news_rss(self, query: str, related_symbol: str | None, limit: int) -> list[NewsItem]:
        """Fetch Google News RSS search results."""
        url = self.GOOGLE_NEWS_RSS.format(query=quote_plus(query))
        response = self.session.get(url, headers=self.headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        SOURCE_HEALTH.record_success("Google News RSS")
        soup = BeautifulSoup(response.text, "xml")
        items: list[NewsItem] = []
        for item in soup.find_all("item")[:limit]:
            title = _clean_text(item.title.get_text(" ", strip=True) if item.title else "")
            link = item.link.get_text(" ", strip=True) if item.link else ""
            description = _clean_text(item.description.get_text(" ", strip=True) if item.description else "")
            published_date = _parse_rss_date(item.pubDate.get_text(" ", strip=True) if item.pubDate else "")
            source_tag = item.find("source")
            source = _clean_text(source_tag.get_text(" ", strip=True) if source_tag else "")
            if not title:
                continue
            items.append(
                self._normalize_item(
                    title=title,
                    source=source,
                    source_url=link,
                    published_date=published_date,
                    related_symbol=related_symbol,
                    related_company=None,
                    summary_seed=description,
                    is_rumor=False,
                )
            )
        return items

    def _portal_items(self, keyword: str, related_symbol: str | None, limit: int) -> list[NewsItem]:
        """Scrape lightweight portal listing pages as a fallback."""
        items: list[NewsItem] = []
        normalized_keyword = keyword.strip().lower()
        for url in self.PORTAL_PAGES:
            try:
                response = self.session.get(url, headers=self.headers, timeout=self.timeout_seconds)
                response.raise_for_status()
                SOURCE_HEALTH.record_success(f"News portal: {urlparse(url).netloc}")
            except Exception as exc:
                SOURCE_HEALTH.record_failure(f"News portal: {urlparse(url).netloc}", exc)
                logger.info("Portal page unavailable %s: %s", url, exc)
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                title = _clean_text(anchor.get_text(" ", strip=True))
                if not title or len(title) < 12:
                    continue
                if normalized_keyword and normalized_keyword not in title.lower():
                    continue
                href = anchor.get("href", "")
                full_url = href if href.startswith("http") else url.rstrip("/") + "/" + href.lstrip("/")
                items.append(
                    self._normalize_item(
                        title=title,
                        source=SourceRanker._domain(full_url),
                        source_url=full_url,
                        published_date=None,
                        related_symbol=related_symbol,
                        related_company=None,
                        summary_seed="",
                        is_rumor=False,
                    )
                )
                if len(items) >= limit:
                    return self._dedupe(items)
        return self._dedupe(items)

    def _normalize_item(
        self,
        title: str,
        source: str,
        source_url: str,
        published_date: datetime | None,
        related_symbol: str | None,
        related_company: str | None,
        summary_seed: str,
        is_rumor: bool,
    ) -> NewsItem:
        """Attach category, sentiment, source, and freshness metadata."""
        source_score = self.source_ranker.score(source, source_url)
        sentiment = self.sentiment_analyzer.analyze(title=title, summary=summary_seed)
        reliability = min(source_score.reliability_score, 1.5) if is_rumor else source_score.reliability_score
        freshness = self.source_ranker.freshness_label(published_date)
        reliability_weight = self.source_ranker.reliability_weight(reliability)
        freshness_weight = self.source_ranker.freshness_weight(published_date, sentiment.category)
        final_score = sentiment.sentiment_score * sentiment.impact_score * reliability_weight * freshness_weight
        summary = summary_seed or sentiment.summary
        if is_rumor:
            summary = "Unverified chatter only. Treat as weak sentiment, not confirmed fact."
        return NewsItem(
            title=title,
            source=source_score.source,
            source_url=source_url,
            published_date=published_date,
            related_symbol=related_symbol,
            related_company=related_company,
            category=sentiment.category,
            sentiment_score=sentiment.sentiment_score,
            impact_score=sentiment.impact_score,
            reliability_score=reliability,
            summary=summary,
            why_it_matters=sentiment.why_it_matters,
            freshness=freshness,
            final_score=round(final_score, 3),
            is_rumor=is_rumor,
        )

    @staticmethod
    def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
        """De-duplicate items by title and URL."""
        seen: set[str] = set()
        result: list[NewsItem] = []
        for item in sorted(items, key=lambda value: value.published_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            key = re.sub(r"\W+", "", item.title.lower())[:100] + "|" + item.source_url[:120]
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result


def _parse_rss_date(value: str) -> datetime | None:
    """Parse RSS pubDate text."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse ISO datetime text safely."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _clean_text(value: str) -> str:
    """Strip HTML, decode entities, and normalize whitespace."""
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
