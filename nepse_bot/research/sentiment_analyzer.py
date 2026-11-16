"""Keyword-based sentiment scoring for Nepali market research."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SentimentResult:
    """Sentiment and category extracted from a headline or summary."""

    sentiment_score: int
    impact_score: int
    label: str
    category: str
    summary: str
    why_it_matters: str
    positive_hits: tuple[str, ...]
    negative_hits: tuple[str, ...]


class SentimentAnalyzer:
    """Rule-based analyzer tuned for NEPSE news and announcements."""

    CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Dividend / bonus share", ("dividend", "bonus share", "bonus", "cash dividend")),
        ("Right share", ("right share", "rights share", "right issue", "rights issue")),
        ("Book closure", ("book closure", "book close", "closure date")),
        ("AGM / SGM", ("agm", "annual general meeting", "sgm", "special general meeting")),
        ("Earnings report", ("earning", "eps", "net profit", "quarter", "financial statement")),
        ("Profit growth", ("profit rises", "profit growth", "net profit increased", "surge in profit")),
        ("Loss / weak quarter", ("loss", "profit declines", "net profit decreased", "weak quarter")),
        ("Merger / acquisition", ("merger", "acquisition", "swap ratio")),
        ("Lock-in expiry", ("lock-in", "lock in", "lockin", "promoter share")),
        ("Promoter share sale", ("promoter share sale", "promoter shares", "promoter selling")),
        ("Auction", ("auction", "share auction")),
        ("IPO/FPO/listing", ("ipo", "fpo", "listed", "listing")),
        ("Regulatory action", ("sebon", "nepse notice", "regulatory", "warning", "suspended")),
        ("Project update", ("project", "commercial operation", "generation", "powerhouse")),
        ("Hydropower generation issue", ("hydropower", "flood", "landslide", "generation issue", "shutdown")),
        ("Loan/default/debt issue", ("debt", "default", "loan", "interest expense")),
        ("Management change", ("resigns", "appointed", "ceo", "director", "management")),
        ("Sector policy", ("policy", "monetary policy", "nrn", "nrb")),
        ("Interest rate / liquidity news", ("interest rate", "liquidity", "banking liquidity")),
        ("Macro economy", ("inflation", "remittance", "forex", "economy", "budget")),
        ("Market rumor", ("rumor", "rumour", "market chatter", "unverified")),
        ("Technical market commentary", ("technical analysis", "market trend", "nepse index", "support", "resistance")),
    )

    STRONG_POSITIVE = (
        "strong profit",
        "profit surges",
        "profit rises",
        "record profit",
        "bonus share",
        "cash dividend",
        "dividend proposed",
        "commercial operation",
        "project resumes",
        "merger benefit",
        "favorable policy",
        "approved",
    )
    POSITIVE = (
        "dividend",
        "bonus",
        "right share approved",
        "profit growth",
        "eps increased",
        "upgrade",
        "rating upgraded",
        "generation started",
        "listed",
        "agm approved",
        "positive",
        "bullish",
    )
    STRONG_NEGATIVE = (
        "no dividend",
        "loss",
        "net loss",
        "regulatory warning",
        "suspended",
        "default",
        "project shutdown",
        "fire",
        "flood damaged",
        "lock-in expiry",
        "promoter selling",
    )
    NEGATIVE = (
        "profit declines",
        "profit decreased",
        "weak quarter",
        "rights dilution",
        "book closure risk",
        "poor liquidity",
        "downgrade",
        "negative",
        "bearish",
        "auction unsold",
    )
    HIGH_IMPACT = (
        "dividend",
        "bonus",
        "right share",
        "book closure",
        "lock-in",
        "merger",
        "profit",
        "eps",
        "regulatory",
        "sebon",
        "project",
        "shutdown",
        "default",
        "auction",
    )
    VERY_HIGH_IMPACT = (
        "no dividend",
        "bonus share",
        "cash dividend",
        "lock-in expiry",
        "regulatory warning",
        "suspended",
        "merger",
        "project shutdown",
        "commercial operation",
    )

    def analyze(self, title: str, summary: str = "") -> SentimentResult:
        """Analyze a title and optional description."""
        text = f"{title} {summary}".lower()
        category = self.category_for(text)
        strong_positive_hits = self._hits(text, self.STRONG_POSITIVE)
        positive_hits = self._hits(text, self.POSITIVE)
        strong_negative_hits = self._hits(text, self.STRONG_NEGATIVE)
        negative_hits = self._hits(text, self.NEGATIVE)

        raw_score = len(positive_hits) + (2 * len(strong_positive_hits))
        raw_score -= len(negative_hits) + (2 * len(strong_negative_hits))
        if raw_score >= 2:
            sentiment_score = 2
        elif raw_score == 1:
            sentiment_score = 1
        elif raw_score <= -2:
            sentiment_score = -2
        elif raw_score == -1:
            sentiment_score = -1
        else:
            sentiment_score = 0

        if any(token in text for token in self.VERY_HIGH_IMPACT):
            impact_score = 5
        elif any(token in text for token in self.HIGH_IMPACT):
            impact_score = 4
        elif category != "General market news":
            impact_score = 3
        else:
            impact_score = 2

        label = self.label(sentiment_score)
        return SentimentResult(
            sentiment_score=sentiment_score,
            impact_score=impact_score,
            label=label,
            category=category,
            summary=self.summary_for(category, label),
            why_it_matters=self.why_it_matters(category, sentiment_score),
            positive_hits=tuple(strong_positive_hits + positive_hits),
            negative_hits=tuple(strong_negative_hits + negative_hits),
        )

    def category_for(self, text: str) -> str:
        """Return the first matching NEPSE news category."""
        lowered = text.lower()
        for category, keywords in self.CATEGORY_KEYWORDS:
            if any(keyword in lowered for keyword in keywords):
                return category
        return "General market news"

    @staticmethod
    def label(score: int) -> str:
        """Return a human label for sentiment score."""
        if score >= 2:
            return "Strong Positive"
        if score == 1:
            return "Positive"
        if score == -1:
            return "Negative"
        if score <= -2:
            return "Strong Negative"
        return "Neutral"

    @staticmethod
    def summary_for(category: str, label: str) -> str:
        """Create a concise factual summary from detected category."""
        if category == "General market news":
            return f"{label} market-related item; no specific corporate event detected."
        return f"{label} {category.lower()} item detected from the headline/summary."

    @staticmethod
    def why_it_matters(category: str, score: int) -> str:
        """Explain trading relevance without inventing facts."""
        if category in {"Dividend / bonus share", "Right share", "Book closure", "AGM / SGM"}:
            return "Corporate actions can affect demand, supply, and short-term price behavior."
        if category in {"Earnings report", "Profit growth", "Loss / weak quarter"}:
            return "Earnings quality can support or weaken valuation confidence."
        if category in {"Lock-in expiry", "Promoter share sale", "Auction"}:
            return "Share supply events can pressure price if demand is weak."
        if category in {"Project update", "Hydropower generation issue"}:
            return "Project execution news can quickly change hydropower sentiment."
        if category == "Regulatory action":
            return "Regulatory notices are high-impact and should be treated as confirmed only from trusted sources."
        if score > 0:
            return "This may support sentiment, but price and volume must confirm."
        if score < 0:
            return "This can weaken sentiment; avoid trading against confirmed negative catalysts."
        return "This looks routine or neutral; do not trade from this item alone."

    @staticmethod
    def _hits(text: str, keywords: tuple[str, ...]) -> list[str]:
        """Return matched keywords."""
        return [keyword for keyword in keywords if keyword in text]

