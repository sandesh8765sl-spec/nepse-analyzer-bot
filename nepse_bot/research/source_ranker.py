"""Source reliability and freshness scoring for NEPSE research items."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse


@dataclass(frozen=True)
class SourceScore:
    """Reliability metadata assigned to a research source."""

    source: str
    reliability_score: float
    trust_label: str
    source_type: str


class SourceRanker:
    """Rank financial research sources by trust and freshness.

    The ranker intentionally treats social/forum sources as weak sentiment only.
    Official notices rank highest, trusted Nepali financial portals rank next,
    and unknown domains are clearly labeled as low confidence.
    """

    OFFICIAL_DOMAINS = {
        "nepalstock.com": ("Official NEPSE", 5.0),
        "nepalstock.com.np": ("Official NEPSE", 5.0),
        "sebon.gov.np": ("SEBON", 5.0),
        "cdsc.com.np": ("CDSC", 4.5),
    }
    PORTAL_DOMAINS = {
        "sharesansar.com": ("ShareSansar", 4.0),
        "merolagani.com": ("MeroLagani", 4.0),
        "nepalipaisa.com": ("Nepali Paisa", 4.0),
        "nepsealpha.com": ("Nepse Alpha", 4.0),
        "bizmandu.com": ("Bizmandu", 3.5),
        "arthasarokar.com": ("Artha Sarokar", 3.5),
        "onlinekhabar.com": ("OnlineKhabar", 3.5),
        "kathmandupost.com": ("Kathmandu Post", 3.5),
        "myrepublica.nagariknetwork.com": ("Republica", 3.5),
    }
    LOW_TRUST_DOMAINS = {
        "reddit.com": ("Reddit", 1.5),
        "facebook.com": ("Facebook", 1.5),
        "x.com": ("X / Twitter", 1.5),
        "twitter.com": ("X / Twitter", 1.5),
        "youtube.com": ("YouTube", 1.5),
        "tiktok.com": ("TikTok", 1.5),
    }

    def score(self, source: str | None, url: str | None = None) -> SourceScore:
        """Return a trust score for a source name and URL."""
        source_text = (source or "").strip()
        domain = self._domain(url)
        normalized_domain = domain.removeprefix("www.")

        for known_domain, (label, score) in self.OFFICIAL_DOMAINS.items():
            if normalized_domain.endswith(known_domain):
                return SourceScore(label, score, "Very High", "official")

        for known_domain, (label, score) in self.PORTAL_DOMAINS.items():
            if normalized_domain.endswith(known_domain) or label.lower() in source_text.lower():
                return SourceScore(label, score, "High", "financial_portal")

        for known_domain, (label, score) in self.LOW_TRUST_DOMAINS.items():
            if normalized_domain.endswith(known_domain) or label.lower() in source_text.lower():
                return SourceScore(label, score, "Low", "social")

        if source_text:
            return SourceScore(source_text, 3.0, "Medium", "media")
        if normalized_domain:
            return SourceScore(normalized_domain, 1.0, "Unknown", "unknown")
        return SourceScore("Unknown source", 1.0, "Unknown", "unknown")

    def reliability_weight(self, reliability_score: float) -> float:
        """Map a 1-5 reliability score into a conservative multiplier."""
        if reliability_score >= 5:
            return 1.0
        if reliability_score >= 4:
            return 0.85
        if reliability_score >= 3:
            return 0.65
        if reliability_score >= 2:
            return 0.40
        return 0.25

    def freshness_label(self, published_date: datetime | None) -> str:
        """Classify a news item's age."""
        age_days = self.age_days(published_date)
        if age_days is None:
            return "undated"
        if age_days <= 1:
            return "fresh"
        if age_days <= 7:
            return "recent"
        if age_days <= 30:
            return "older but still recent"
        return "old"

    def freshness_weight(self, published_date: datetime | None, category: str | None = None) -> float:
        """Return a freshness multiplier with event exceptions.

        Old items normally matter less, but event categories such as lock-in,
        book closure, rights issue, merger, and AGM remain relevant for longer.
        """
        category_text = (category or "").lower()
        long_lived = any(
            token in category_text
            for token in ("book closure", "lock-in", "right share", "merger", "agm", "auction")
        )
        age_days = self.age_days(published_date)
        if age_days is None:
            return 0.45
        if age_days <= 1:
            return 1.0
        if age_days <= 7:
            return 0.9
        if age_days <= 30:
            return 0.7
        if long_lived and age_days <= 120:
            return 0.55
        return 0.25

    @staticmethod
    def age_days(published_date: datetime | None) -> int | None:
        """Return integer age in days from now."""
        if published_date is None:
            return None
        value = published_date
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        now = datetime.now(value.tzinfo)
        return max(0, (now - value).days)

    @staticmethod
    def _domain(url: str | None) -> str:
        """Extract a hostname from a URL."""
        if not url:
            return ""
        parsed = urlparse(url)
        if parsed.netloc:
            return parsed.netloc.lower()
        return urlparse("https://" + url).netloc.lower()

