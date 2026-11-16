"""Technical indicators on completed daily bars (adjusted prices).

The input frame has columns ``open, high, low, close, turnover`` indexed by
date, oldest first. Rupee turnover is used instead of share volume because it
is not distorted by bonus and right-share adjustments.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average (empty until ``length`` bars exist)."""
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(length, min_periods=length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index."""
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    strength = gain / loss.replace(0, np.nan)
    result = 100 - 100 / (1 + strength)
    return result.where(loss != 0, 100.0).where(gain.notna())


def atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - previous_close).abs(), (frame["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def macd_histogram(close: pd.Series) -> pd.Series:
    """MACD (12, 26) minus its 9-period signal line."""
    line = close.ewm(span=12, adjust=False, min_periods=26).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
    return line - line.ewm(span=9, adjust=False, min_periods=9).mean()


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of the frame with every indicator the engine uses."""
    data = frame.copy()
    close = data["close"]
    data["ema20"] = ema(close, 20)
    data["ema50"] = ema(close, 50)
    data["sma200"] = sma(close, 200)
    data["rsi14"] = rsi(close, 14)
    data["atr14"] = atr(data, 14)
    data["macd_hist"] = macd_histogram(close)
    for days in (5, 20, 60, 120):
        data[f"roc{days}"] = close.pct_change(days) * 100.0
    data["turnover_avg20"] = data["turnover"].rolling(20, min_periods=10).mean()
    data["high_252"] = data["high"].rolling(252, min_periods=120).max()
    direction = np.sign(close.diff().fillna(0.0))
    data["obv"] = (direction * data["turnover"].fillna(0.0)).cumsum()
    data["range_pct"] = (data["high"] - data["low"]) / close * 100.0
    return data


def weekly_trend(frame: pd.DataFrame) -> dict[str, float | None]:
    """Weekly close and its 10-week and 30-week averages."""
    weekly = frame["close"].resample("W-FRI").last().dropna()
    if len(weekly) < 10:
        return {"close": None, "ma10": None, "ma30": None}
    return {
        "close": float(weekly.iloc[-1]),
        "ma10": float(weekly.rolling(10).mean().iloc[-1]),
        "ma30": float(weekly.rolling(30).mean().iloc[-1]) if len(weekly) >= 30 else None,
    }


def swing_points(frame: pd.DataFrame, window: int = 5, lookback: int = 250) -> tuple[list[float], list[float]]:
    """Return (swing highs, swing lows): bars that stand out from ``window`` bars each side."""
    recent = frame.tail(lookback)
    highs, lows = recent["high"].to_numpy(), recent["low"].to_numpy()
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(window, len(recent) - window):
        if highs[i] == highs[i - window : i + window + 1].max():
            swing_highs.append(float(highs[i]))
        if lows[i] == lows[i - window : i + window + 1].min():
            swing_lows.append(float(lows[i]))
    return swing_highs, swing_lows
