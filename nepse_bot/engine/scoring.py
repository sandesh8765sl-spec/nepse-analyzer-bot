"""The 100-point score: six sub-scores, each 0-100, weighted from config.

The missing-data rule, applied everywhere in this module:

* a piece of data that is missing counts as a neutral 50 - it can never add
  points and never blocks the analysis on its own;
* missing data lowers ``coverage``, which is what the confidence level
  (high / medium / low) is built from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from nepse_bot.engine.config import setting
from nepse_bot.engine.plan import TradePlan
from nepse_bot.engine.setups import StockContext

NEUTRAL = 50.0


@dataclass
class SubScore:
    """One part of the total score."""

    name: str
    score: float = NEUTRAL
    coverage: float = 0.0                    # 0-1: how much of this score rests on real data
    positives: list[tuple[float, str]] = field(default_factory=list)   # (importance, text)
    negatives: list[tuple[float, str]] = field(default_factory=list)


class _Builder:
    """Add up weighted parts; a missing part earns half its weight (neutral)."""

    def __init__(self, name: str) -> None:
        self.sub = SubScore(name)
        self._earned = self._possible = self._known = 0.0

    def check(self, condition: bool | None, weight: float, good: str = "", bad: str = "") -> None:
        self._possible += weight
        if condition is None:
            self._earned += weight / 2.0
            return
        self._known += weight
        if condition:
            self._earned += weight
            if good:
                self.sub.positives.append((weight, good))
        elif bad:
            self.sub.negatives.append((weight, bad))

    def graded(self, value: float | None, weight: float, good: str = "", bad: str = "") -> None:
        """Add a part that is itself a 0-100 grade (None = missing)."""
        self._possible += weight
        if value is None:
            self._earned += weight / 2.0
            return
        self._known += weight
        self._earned += weight * max(0.0, min(100.0, value)) / 100.0
        if value >= 70 and good:
            self.sub.positives.append((weight * (value - 50) / 50.0, good))
        elif value <= 30 and bad:
            self.sub.negatives.append((weight * (50 - value) / 50.0, bad))

    def done(self) -> SubScore:
        if self._possible > 0:
            self.sub.score = round(100.0 * self._earned / self._possible, 1)
            self.sub.coverage = round(self._known / self._possible, 3)
        return self.sub


def _above(a: float | None, b: float | None) -> bool | None:
    return None if a is None or b is None else a > b


def score_trend(ctx: StockContext) -> SubScore:
    """Trend: price versus its averages, their alignment and slope, weekly trend, swing structure."""
    frame, build = ctx.frame, _Builder("trend")
    close, ema20, ema50, sma200 = ctx.value("close"), ctx.value("ema20"), ctx.value("ema50"), ctx.value("sma200")
    build.check(_above(close, ema20), 15, "Above its 20-day average", "Below its 20-day average")
    build.check(_above(close, ema50), 15, "Above its 50-day average", "Below its 50-day average")
    build.check(_above(close, sma200), 15, "Above its 200-day average", "Below its 200-day average")
    build.check(_above(ema20, ema50), 10, "", "20-day average is below the 50-day (short-term downtrend)")
    build.check(_above(ema50, sma200), 10, "Averages lined up in an uptrend (50-day above 200-day)", "50-day average is below the 200-day")
    slope = None
    if len(frame) > 70 and pd.notna(frame["ema50"].iloc[-11]):
        slope = bool(frame["ema50"].iloc[-1] > frame["ema50"].iloc[-11])
    build.check(slope, 10, "", "50-day average is falling")
    weekly_close, ma10, ma30 = ctx.weekly.get("close"), ctx.weekly.get("ma10"), ctx.weekly.get("ma30")
    build.check(_above(weekly_close, ma10), 8, "Weekly uptrend (above its 10-week average)", "Weekly trend is down (below its 10-week average)")
    build.check(_above(weekly_close, ma30), 7, "", "Below its 30-week average")
    build.check(_above(ma10, ma30), 5)
    structure = None
    if len(frame) >= 41:
        recent, prior = frame.iloc[-20:], frame.iloc[-40:-20]
        structure = bool(recent["high"].max() > prior["high"].max() and recent["low"].min() > prior["low"].min())
    build.check(structure, 5, "Making higher highs and higher lows", "")
    return build.done()


def is_overextended(ctx: StockContext) -> str | None:
    """Return a reason if the stock has run too far to buy now."""
    close, ema20, rsi = ctx.value("close"), ctx.value("ema20"), ctx.value("rsi14")
    limit = float(setting("scoring", "overextended_above_ema20_percent", default=15))
    if close is not None and ema20 and (close / ema20 - 1) * 100.0 > limit:
        return f"price is {(close / ema20 - 1) * 100:.0f}% above its 20-day average"
    if rsi is not None and rsi > float(setting("scoring", "overextended_rsi", default=75)):
        return f"RSI is {rsi:.0f} (overbought)"
    return None


def score_momentum(ctx: StockContext, roc20_rank: float | None, roc60_rank: float | None) -> SubScore:
    """Momentum: RSI zone, MACD direction, and 20/60-day rate of change versus the market."""
    frame, build = ctx.frame, _Builder("momentum")
    rsi = ctx.value("rsi14")
    if rsi is None:
        rsi_grade = None
    elif 50 <= rsi <= 70:
        rsi_grade = 100.0
    elif 45 <= rsi < 50 or 70 < rsi <= 75:
        rsi_grade = 60.0
    elif 40 <= rsi < 45:
        rsi_grade = 35.0
    elif rsi > 75:
        rsi_grade = 30.0
    else:
        rsi_grade = 10.0
    build.graded(rsi_grade, 35, f"RSI {rsi:.0f}: healthy momentum" if rsi is not None else "", f"RSI {rsi:.0f}: weak momentum" if rsi is not None and rsi < 45 else "")
    hist = ctx.value("macd_hist")
    macd_grade = None
    if hist is not None and len(frame) > 4 and pd.notna(frame["macd_hist"].iloc[-4]):
        rising = hist > float(frame["macd_hist"].iloc[-4])
        macd_grade = 100.0 if rising and hist > 0 else 60.0 if rising else 40.0 if hist > 0 else 15.0
    build.graded(macd_grade, 25, "MACD rising", "MACD falling")
    build.graded(roc20_rank, 20, "Stronger 20-day move than most stocks", "Weaker 20-day move than most stocks")
    build.graded(roc60_rank, 20, "Stronger 60-day move than most stocks", "Weaker 60-day move than most stocks")
    sub = build.done()
    reason = is_overextended(ctx)
    if reason:
        sub.score = min(sub.score, 40.0)
        sub.negatives.append((30.0, f"Overextended: {reason} - wait for a pullback"))
    return sub


def score_relative_strength(rank: float | None, versus_market_60: float | None, versus_sector_60: float | None) -> SubScore:
    """Relative strength: rank across all stocks, and 60-day return versus NEPSE and the sector."""
    build = _Builder("relative_strength")
    build.graded(
        rank, 60,
        f"Relative strength rank {rank:.0f} of 100" if rank is not None else "",
        f"Relative strength rank only {rank:.0f} of 100" if rank is not None else "",
    )
    build.check(
        None if versus_market_60 is None else versus_market_60 > 0, 20,
        f"Beat NEPSE by {versus_market_60:.0f}% in 60 days" if versus_market_60 is not None else "",
        f"Lagged NEPSE by {-versus_market_60:.0f}% in 60 days" if versus_market_60 is not None else "",
    )
    build.check(
        None if versus_sector_60 is None else versus_sector_60 > 0, 20,
        f"Beat its sector by {versus_sector_60:.0f}% in 60 days" if versus_sector_60 is not None else "",
        f"Lagged its sector by {-versus_sector_60:.0f}% in 60 days" if versus_sector_60 is not None else "",
    )
    return build.done()


def score_volume_flow(ctx: StockContext) -> SubScore:
    """Volume and broker flow: buying versus selling volume, OBV trend, and what top brokers did."""
    frame, build = ctx.frame, _Builder("volume_flow")
    recent = frame.tail(20)
    change = recent["close"].diff()
    up, down = float(recent["turnover"][change > 0].sum()), float(recent["turnover"][change < 0].sum())
    ratio = up / down if down > 0 else (2.0 if up > 0 else None)
    ratio_grade = None if ratio is None else max(0.0, min(100.0, (ratio - 0.6) / 0.9 * 100.0))
    build.graded(
        ratio_grade, 35,
        f"Up-day volume is {ratio:.1f}x down-day volume" if ratio is not None else "",
        f"More volume on down days than up days ({ratio:.1f}x)" if ratio is not None else "",
    )
    obv_up = bool(frame["obv"].iloc[-1] > frame["obv"].iloc[-21]) if len(frame) > 21 else None
    build.check(obv_up, 25, "", "Money flow (OBV) is falling")

    broker = ctx.broker or {}
    enough = broker.get("days", 0) >= int(setting("scoring", "broker_days_needed", default=5))
    net_share = broker.get("top5_net_share_10d") if enough else None
    net_grade = None if net_share is None else max(0.0, min(100.0, 50.0 + float(net_share) * 2.5))
    names = broker.get("top_buyers_text", "")
    build.graded(
        net_grade, 30,
        f"Top brokers net bought {net_share:.0f}% of volume in 10 days{names}" if net_share is not None else "",
        f"Top brokers net sold {-net_share:.0f}% of volume in 10 days" if net_share is not None else "",
    )
    concentration = broker.get("top_buyer_share_5d") if enough else None
    build.check(None if concentration is None else concentration < 30, 10, "", "Buying is concentrated in one broker")
    return build.done()


def score_fundamentals(view: dict[str, Any] | None) -> SubScore:
    """Fundamentals: sector-relative percentiles. No absolute cut-offs anywhere."""
    build = _Builder("fundamentals")
    view = view or {}
    grades = view.get("grades") or {}
    sector = view.get("sector_label", "its sector")

    def text(key: str, good: str, bad: str) -> tuple[str, str]:
        return (good.format(**view, sector=sector), bad.format(**view, sector=sector)) if grades.get(key) is not None else ("", "")

    parts = [
        ("pe", 25, *text("pe", "P/E {pe:.0f} is cheaper than most of {sector} (median {pe_median:.0f})", "P/E {pe:.0f} vs {sector} median {pe_median:.0f}")),
        ("pb", 15, *text("pb", "P/B {pb:.1f} is low for {sector}", "P/B {pb:.1f} is high for {sector}")),
        ("eps_growth", 20, *text("eps_growth", "Earnings per share up {eps_growth:.0f}% year on year", "Earnings per share changed {eps_growth:.0f}% year on year")),
        ("roe", 20, *text("roe", "Return on equity {roe:.0f}% is among the best in {sector}", "Return on equity {roe:.0f}% is low for {sector}")),
        ("dividend_yield", 10, *text("dividend_yield", "Dividend yield {dividend_yield:.1f}%", "")),
    ]
    if view.get("is_lender"):
        parts.append(("npl", 5, *text("npl", "Bad loans (NPL) {npl:.1f}% are low for {sector}", "Bad loans (NPL) {npl:.1f}% are high for {sector}")))
        parts.append(("car", 5, *text("car", "", "Capital adequacy {car:.1f}% is low for {sector}")))
    for key, weight, good, bad in parts:
        build.graded(grades.get(key), weight, good, bad)
    sub = build.done()
    if sub.coverage == 0:
        sub.negatives.append((5.0, "Fundamentals unavailable (counted as neutral)"))
    return sub


def score_setup_quality(plan: TradePlan | None, ctx: StockContext) -> SubScore:
    """Setup quality: a triggered setup, how calm the stock is, and how tight the recent range is."""
    build = _Builder("setup_quality")
    frame, close = ctx.frame, ctx.value("close")
    width = None
    if close and len(frame) >= 20:
        width = (float(frame["high"].tail(20).max()) - float(frame["low"].tail(20).min())) / close * 100.0
    base_grade = None if width is None else max(0.0, min(100.0, (30.0 - width) / 22.0 * 100.0))
    build.graded(base_grade, 30, "Tight, clean price range", "Wide, loose price range")
    if plan is None:
        # No valid entry today is real information (not missing data): it earns a low grade.
        build.graded(20.0, 70)
        return build.done()
    build.graded(75.0, 30, "A setup has triggered", "")
    # Calm stocks did better than jumpy ones in both backtest periods, so a small stop earns more.
    stop_grade = max(0.0, min(100.0, 100.0 - (plan.stop_percent - 4.0) * 11.0))
    build.graded(stop_grade, 40, f"Calm stock (stop only {plan.stop_percent:.1f}% below entry)", "Jumpy stock: needs a wide stop")
    return build.done()


def combine(subs: dict[str, SubScore]) -> tuple[float, float, str]:
    """Return (total score, coverage, confidence label) from the weighted sub-scores."""
    weights: dict[str, float] = {name: float(value) for name, value in (setting("weights", default={}) or {}).items()}
    total_weight = sum(weights.get(name, 0.0) for name in subs) or 1.0
    score = sum(weights.get(name, 0.0) * sub.score for name, sub in subs.items()) / total_weight
    coverage = sum(weights.get(name, 0.0) * sub.coverage for name, sub in subs.items()) / total_weight
    if coverage >= float(setting("scoring", "confidence_high", default=0.9)):
        confidence = "High"
    elif coverage >= float(setting("scoring", "confidence_medium", default=0.75)):
        confidence = "Medium"
    else:
        confidence = "Low"
    return round(score, 1), round(coverage, 3), confidence


def top_reasons(subs: dict[str, SubScore], count: int = 3) -> tuple[list[str], list[str]]:
    """Return the strongest positive and negative reasons across all sub-scores."""
    weights: dict[str, float] = {name: float(value) for name, value in (setting("weights", default={}) or {}).items()}
    positives = [(weights.get(name, 0) * importance, text) for name, sub in subs.items() for importance, text in sub.positives if text]
    negatives = [(weights.get(name, 0) * importance, text) for name, sub in subs.items() for importance, text in sub.negatives if text]
    positives.sort(reverse=True)
    negatives.sort(reverse=True)
    return [text for _, text in positives[:count]], [text for _, text in negatives[:count]]
