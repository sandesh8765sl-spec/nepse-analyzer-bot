"""Derived daily numbers: market breadth and per-stock liquidity."""

from __future__ import annotations

from typing import Any

import pandas as pd


def liquidity_for_symbol(prices: pd.DataFrame, market_dates: list[str], as_of: str) -> dict[str, Any] | None:
    """Return liquidity metrics for one symbol on one date.

    Args:
        prices: The symbol's rows with ``date``, ``turnover`` and ``trades``.
        market_dates: Every date the market traded, oldest first.
        as_of: The date to measure on (uses data up to and including it).

    Returns:
        20-day average turnover, 20-day average number of trades, and the
        percentage of the last 60 market days on which the stock traded.
        Days without a trade count as zero.
    """
    window_60 = [day for day in market_dates if day <= as_of][-60:]
    if not window_60:
        return None
    by_date = prices[prices["date"] <= as_of].set_index("date")
    window_20 = window_60[-20:]
    turnover = by_date["turnover"].reindex(window_20).fillna(0.0)
    trades = by_date["trades"].reindex(window_20).fillna(0.0)
    traded_days = int(by_date.reindex(window_60)["close"].notna().sum()) if "close" in by_date else 0
    return {
        "as_of_date": as_of,
        "avg_turnover_20": round(float(turnover.mean()), 2),
        "avg_trades_20": round(float(trades.mean()), 2),
        "traded_days_pct_60": round(100.0 * traded_days / len(window_60), 2),
    }


def liquidity_table(all_prices: pd.DataFrame, as_of: str) -> list[dict[str, Any]]:
    """Return liquidity metrics for every symbol on one date."""
    market_dates = sorted(all_prices["date"].unique().tolist())
    rows = []
    for symbol, group in all_prices.groupby("symbol"):
        metrics = liquidity_for_symbol(group, market_dates, as_of)
        if metrics is not None:
            rows.append({"symbol": symbol, **metrics})
    return rows


def breadth_table(all_prices: pd.DataFrame, since: str | None = None) -> list[dict[str, Any]]:
    """Return market breadth per day.

    Args:
        all_prices: Rows with ``symbol``, ``date``, ``close``, ``adj_close`` and
            ``turnover`` for the ordinary-share universe.
        since: Only return dates on or after this (history before it is still
            used to compute moving averages).
    """
    if all_prices.empty:
        return []
    frame = all_prices.sort_values(["symbol", "date"]).copy()
    adjusted = frame["adj_close"].where(frame["adj_close"].notna(), frame["close"]).astype(float)
    frame["adj"] = adjusted
    grouped = frame.groupby("symbol")["adj"]
    frame["change"] = grouped.pct_change()
    frame["ma50"] = grouped.transform(lambda s: s.rolling(50, min_periods=50).mean())
    frame["ma200"] = grouped.transform(lambda s: s.rolling(200, min_periods=200).mean())
    if since:
        frame = frame[frame["date"] >= since]

    rows = []
    for day, group in frame.groupby("date"):
        with_ma50 = group[group["ma50"].notna()]
        with_ma200 = group[group["ma200"].notna()]
        rows.append(
            {
                "date": day,
                "advancers": int((group["change"] > 0).sum()),
                "decliners": int((group["change"] < 0).sum()),
                "unchanged": int((group["change"] == 0).sum()),
                "traded_symbols": int(len(group)),
                "turnover": round(float(group["turnover"].fillna(0).sum()), 2),
                "pct_above_ma50": round(100.0 * float((with_ma50["adj"] > with_ma50["ma50"]).mean()), 2) if len(with_ma50) else None,
                "pct_above_ma200": round(100.0 * float((with_ma200["adj"] > with_ma200["ma200"]).mean()), 2) if len(with_ma200) else None,
            }
        )
    return rows
