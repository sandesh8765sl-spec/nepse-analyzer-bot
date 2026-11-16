"""The /chart picture: price with EMA 20 / 50 / 200, volume, and the stop and reference target lines."""

from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Any

import pandas as pd

from nepse_bot.engine.indicators import ema

SESSIONS_SHOWN = 160
LEVEL_COLOURS = {"Stop": "#d62728", "Trailing stop": "#d62728", "Reference target": "#2ca02c", "Entry": "#1f77b4", "Your average price": "#7f7f7f"}


def chart_frame(db: Any, symbol: str, as_of: str) -> pd.DataFrame:
    """Adjusted daily bars with the three averages (about 2.5 years, so the 200-day line is settled)."""
    start = (date.fromisoformat(as_of) - timedelta(days=900)).isoformat()
    frame = db.frame(
        "SELECT date, COALESCE(adj_open, open) AS open, COALESCE(adj_high, high) AS high, COALESCE(adj_low, low) AS low, "
        "COALESCE(adj_close, close) AS close, COALESCE(volume, 0) AS volume, COALESCE(adj_factor, 1) AS adj_factor "
        "FROM daily_prices WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
        (symbol, start, as_of),
    )
    if frame.empty:
        return frame
    # Show today's prices: undo the adjustment of the newest bar on every bar.
    factor = float(frame["adj_factor"].iloc[-1]) or 1.0
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column].astype(float) * factor
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index("date")
    for length in (20, 50, 200):
        frame[f"ema{length}"] = ema(frame["close"], length)
    return frame


def render_chart(frame: pd.DataFrame, symbol: str, levels: dict[str, float] | None = None, subtitle: str = "") -> bytes:
    """Draw the chart and return it as PNG bytes."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    view = frame.tail(SESSIONS_SHOWN)
    x = range(len(view))
    figure, (top, bottom) = plt.subplots(2, 1, figsize=(10, 6.2), dpi=110, sharex=True, gridspec_kw={"height_ratios": [3.4, 1], "hspace": 0.05})
    up = (view["close"] >= view["open"]).to_numpy()
    colours = ["#26a69a" if rising else "#ef5350" for rising in up]
    top.vlines(x, view["low"], view["high"], colors=colours, linewidth=0.8)
    body_low = view[["open", "close"]].min(axis=1)
    body_height = (view["close"] - view["open"]).abs().clip(lower=view["close"] * 0.0005)
    top.bar(x, body_height, bottom=body_low, color=colours, width=0.7)
    for length, colour in ((20, "#ff9800"), (50, "#3f51b5"), (200, "#000000")):
        series = view[f"ema{length}"]
        if series.notna().sum() >= 2 and len(frame) >= length:
            top.plot(x, series, color=colour, linewidth=1.1, label=f"EMA {length}")
    last = float(view["close"].iloc[-1])
    for name, value in (levels or {}).items():
        if not value:
            continue
        colour = LEVEL_COLOURS.get(name, "#9467bd")
        top.axhline(value, color=colour, linestyle="--", linewidth=1.0)
        top.text(len(view) + 0.5, value, f"{name} {value:,.0f} ({(value / last - 1) * 100:+.1f}%)", color=colour, fontsize=8, va="center")
    top.set_xlim(-1, len(view) + 26)
    top.set_title(f"{symbol} — last close {last:,.1f}" + (f" | {subtitle}" if subtitle else ""), fontsize=11, loc="left")
    top.legend(loc="upper left", fontsize=8, frameon=False)
    top.grid(alpha=0.25)
    bottom.bar(x, view["volume"], color=colours, width=0.7)
    bottom.set_ylabel("Volume", fontsize=8)
    bottom.grid(alpha=0.25)
    step = max(1, len(view) // 8)
    ticks = list(range(0, len(view), step))
    bottom.set_xticks(ticks)
    bottom.set_xticklabels([view.index[i].strftime("%d %b %y") for i in ticks], fontsize=8)
    bottom.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda value, _: f"{value / 1000:,.0f}k"))
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(figure)
    return buffer.getvalue()


def chart_levels(item: Any | None, view: Any | None) -> dict[str, float]:
    """Lines to draw: the owner's own position if there is one, otherwise the engine's plan."""
    if view is not None:
        levels = {"Trailing stop": float(view.trailing_stop), "Your average price": float(view.avg_price)}
        if view.target:
            levels["Reference target"] = float(view.target)
        return levels
    plan = (item.plan or item.blocked_plan) if item is not None else None
    if plan is None:
        return {}
    return {"Stop": float(plan.stop), "Reference target": float(plan.target_1), "Entry": float(plan.entry)}
