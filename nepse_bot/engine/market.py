"""Market regime (BULL / NEUTRAL / BEAR) and sector strength."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from nepse_bot.engine.config import setting
from nepse_bot.engine.indicators import sma

# securities.sector -> index code
SECTOR_INDEX: dict[str, str] = {
    "Commercial Banks": "BANKING",
    "Development Banks": "DEVBANK",
    "Finance": "FINANCE",
    "Hydro Power": "HYDROPOWER",
    "Microfinance": "MICROFINANCE",
    "Life Insurance": "LIFEINSU",
    "Non Life Insurance": "NONLIFEINSU",
    "Manufacturing And Processing": "MANUFACTURE",
    "Hotels And Tourism": "HOTELS",
    "Investment": "INVESTMENT",
    "Tradings": "TRADING",
    "Others": "OTHERS",
}


@dataclass(frozen=True)
class Regime:
    """The state of the whole market on one day."""

    label: str                       # BULL / NEUTRAL / BEAR
    score: float                     # 0-100
    as_of: str
    index_close: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    breadth_10d: float | None = None        # % of advancing issues over 10 days
    pct_above_ma50: float | None = None
    turnover_ratio: float | None = None     # 10-day vs 50-day average turnover
    turnover_today: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def buy_threshold(self) -> float:
        return float(setting("regime", "buy_threshold", self.label, default=75))

    @property
    def overheated(self) -> bool:
        """Most stocks are already far above their 50-day average: new buys did worst here in 5 of 7 years."""
        limit = float(setting("regime", "overheated_pct_above_ma50", default=75))
        return self.pct_above_ma50 is not None and self.pct_above_ma50 >= limit

    @property
    def max_new_positions(self) -> int:
        if self.overheated:
            return int(setting("regime", "max_new_positions_when_overheated", default=1))
        return int(setting("regime", "max_new_positions_per_day", self.label, default=2))


@dataclass(frozen=True)
class SectorState:
    """How one sector index is doing."""

    sector: str
    code: str
    label: str                       # strong / neutral / weak
    score: float
    return_20d: float | None
    return_60d: float | None
    versus_market_60d: float | None


def _points(condition: bool | None, weight: float) -> tuple[float, float]:
    """Return (earned, possible); an unknown condition earns half and is counted."""
    if condition is None:
        return weight / 2.0, weight
    return (weight if condition else 0.0), weight


def classify_regime(index: pd.Series, breadth: pd.DataFrame, as_of: str) -> Regime:
    """Work out the market regime from the NEPSE index and market breadth.

    Args:
        index: NEPSE index closes indexed by date text, oldest first, up to ``as_of``.
        breadth: ``market_breadth_daily`` rows up to ``as_of`` (date, advancers,
            decliners, turnover, pct_above_ma50).
    """
    if len(index) < 60:
        return Regime(label="NEUTRAL", score=50.0, as_of=as_of, notes=("not enough index history",))
    close = float(index.iloc[-1])
    ma50, ma200 = sma(index, 50), sma(index, 200)
    sma50 = float(ma50.iloc[-1]) if pd.notna(ma50.iloc[-1]) else None
    sma200 = float(ma200.iloc[-1]) if pd.notna(ma200.iloc[-1]) else None
    slope50 = bool(ma50.iloc[-1] > ma50.iloc[-21]) if len(ma50.dropna()) > 21 else None
    slope200 = bool(ma200.iloc[-1] > ma200.iloc[-21]) if len(ma200.dropna()) > 21 else None

    recent = breadth.tail(10)
    advancers, decliners = float(recent["advancers"].sum()), float(recent["decliners"].sum())
    breadth_10d = 100.0 * advancers / (advancers + decliners) if advancers + decliners > 0 else None
    above50 = float(breadth["pct_above_ma50"].iloc[-1]) if len(breadth) and pd.notna(breadth["pct_above_ma50"].iloc[-1]) else None
    turnover = breadth["turnover"].astype(float)
    turnover_ratio = (
        float(turnover.tail(10).mean() / turnover.tail(50).mean()) if len(turnover) >= 50 and turnover.tail(50).mean() > 0 else None
    )

    parts = [
        _points(close > sma50 if sma50 else None, 20),
        _points(close > sma200 if sma200 else None, 20),
        _points(slope50, 10),
        _points(slope200, 10),
        _points(sma50 > sma200 if sma50 and sma200 else None, 10),
    ]
    earned = sum(part[0] for part in parts)
    earned += 15.0 * min(1.0, max(0.0, ((breadth_10d if breadth_10d is not None else 50.0) - 35.0) / 30.0))
    earned += 10.0 * min(1.0, max(0.0, ((above50 if above50 is not None else 50.0) - 25.0) / 50.0))
    earned += 5.0 * min(1.0, max(0.0, ((turnover_ratio if turnover_ratio is not None else 1.0) - 0.7) / 0.6))
    score = round(earned, 1)

    if score >= float(setting("regime", "bull_score", default=60)):
        label = "BULL"
    elif score <= float(setting("regime", "bear_score", default=40)):
        label = "BEAR"
    else:
        label = "NEUTRAL"

    notes = []
    if sma50:
        notes.append(f"NEPSE {'above' if close > sma50 else 'below'} its 50-day average")
    if sma200:
        notes.append(f"{'above' if close > sma200 else 'below'} its 200-day average")
    if breadth_10d is not None:
        notes.append(f"{breadth_10d:.0f}% of moves were up over 10 days")
    year = index.tail(252)
    return Regime(
        label=label, score=score, as_of=as_of, index_close=close, sma50=sma50, sma200=sma200,
        high_52w=float(year.max()), low_52w=float(year.min()), breadth_10d=breadth_10d,
        pct_above_ma50=above50, turnover_ratio=turnover_ratio,
        turnover_today=float(turnover.iloc[-1]) if len(turnover) else None, notes=tuple(notes),
    )


def classify_sectors(indices: dict[str, pd.Series], market: pd.Series) -> dict[str, SectorState]:
    """Rate every sector index as strong, neutral or weak."""

    def change(series: pd.Series, days: int) -> float | None:
        return float(series.iloc[-1] / series.iloc[-1 - days] - 1.0) * 100.0 if len(series) > days else None

    states: dict[str, SectorState] = {}
    market_60 = change(market, 60)
    for sector, code in SECTOR_INDEX.items():
        series = indices.get(code)
        if series is None or len(series) < 60:
            states[sector] = SectorState(sector, code, "neutral", 50.0, None, None, None)
            continue
        close = float(series.iloc[-1])
        ma50, ma200 = sma(series, 50).iloc[-1], sma(series, 200).iloc[-1]
        return_20, return_60 = change(series, 20), change(series, 60)
        versus = return_60 - market_60 if return_60 is not None and market_60 is not None else None
        parts = [
            _points(close > ma50 if pd.notna(ma50) else None, 30),
            _points(close > ma200 if pd.notna(ma200) else None, 25),
            _points(return_20 > 0 if return_20 is not None else None, 15),
            _points(versus > 0 if versus is not None else None, 30),
        ]
        score = round(sum(part[0] for part in parts), 1)
        label = "strong" if score >= 65 else "weak" if score <= 35 else "neutral"
        states[sector] = SectorState(sector, code, label, score, return_20, return_60, versus)
    return states
