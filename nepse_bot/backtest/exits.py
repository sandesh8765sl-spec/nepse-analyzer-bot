"""Stage 5: compare three exit styles on the Stage 4b candidates, after all costs.

    trailing  the current plan: trailing stop, 40-session limit, no target
    target    sell everything at the reference target (next major resistance)
    hybrid    sell half at the reference target, trail the rest

Also measures how far past winning trades went (median and top quarter), which
the reports show as "typical outcome for this setup".

    python -m nepse_bot.backtest.exits [--sample]
    writes reports/stage5/exit_styles.md and config/exits.yaml
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from nepse_bot.backtest import report
from nepse_bot.backtest.run import simulate_all
from nepse_bot.engine.config import load_profile, setting
from nepse_bot.settings import CONFIG_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE = PROJECT_ROOT / "reports" / "stage4b" / "final" / "trades.csv"
OUT_DIR = PROJECT_ROOT / "reports" / "stage5"
STYLES = {"trailing": 0.0, "target": 1.0, "hybrid": 0.5}   # share sold at the reference target
OUTCOME_COLUMNS = ["status", "filled", "entry_price", "entry_date", "exit_date", "final_exit", "exit_price", "sessions_held",
                   "exit_locked", "position_value_sim", "gross_profit", "costs", "tax", "net_profit", "net_percent", "pending"]


def style_outcomes(buys: pd.DataFrame, workers: int) -> dict[str, pd.DataFrame]:
    """The same candidates, followed with each exit style."""
    plans = buys.drop(columns=[column for column in OUTCOME_COLUMNS if column in buys.columns])
    outcomes = {"trailing": buys}
    for style, fraction in STYLES.items():
        if style != "trailing":
            outcomes[style] = simulate_all(plans.assign(t1_fraction=fraction), workers)
    return outcomes


def winner_stats(trades: pd.DataFrame) -> dict[str, Any]:
    """How far winning trades went, as a price gain from the entry."""
    wins = trades[trades["filled"] & (trades["net_profit"] > 0)]
    gain = (wins["exit_price"] / wins["entry_price"] - 1.0) * 100.0
    if len(gain) == 0:
        return {"count": 0}
    return {"count": int(len(gain)), "median_pct": round(float(gain.median()), 1), "top_quarter_pct": round(float(gain.quantile(0.75)), 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare exit styles on the Stage 4b candidates")
    parser.add_argument("--sample", action="store_true", help="30 stocks only, quick check")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"

    signals = pd.read_csv(SOURCE)
    buys = signals[signals["is_buy"]].copy()
    buys["filled"] = buys["filled"].fillna(False).astype(bool)
    if args.sample:
        buys = buys[buys["symbol"].isin(buys["symbol"].value_counts().head(30).index)]
    end = signals["date"].max()
    oos_start = (date.fromisoformat(end) - timedelta(days=round(365.25 * float(setting("backtest", "out_of_sample_years", default=2))))).isoformat()
    capital = load_profile().total_capital
    outcomes = style_outcomes(buys, 1 if args.sample else args.workers)

    rows, per_setup, summary = [], [], {}
    for style, frame in outcomes.items():
        chosen = pd.concat([report.without_overlap(group, ["symbol"]) for _, group in frame.groupby("setup")])
        summary[style] = {}
        for period, part in (("tuning", chosen[chosen["date"] < oos_start]), ("out_of_sample", chosen[chosen["date"] >= oos_start])):
            numbers = report.metrics(part, capital)
            keep = {key: numbers.get(key) for key in ("trades", "win_rate", "avg_win_pct", "avg_loss_pct", "expectancy_pct", "profit_factor", "avg_sessions")}
            rows.append({"exit_style": style, "period": period, **keep})
            summary[style][period] = keep
            for setup, group in part.groupby("setup"):
                numbers = report.metrics(group, capital)
                per_setup.append({"exit_style": style, "period": period, "setup": setup, **{key: numbers.get(key) for key in ("trades", "win_rate", "expectancy_pct", "profit_factor")}})
    table, setup_table = pd.DataFrame(rows), pd.DataFrame(per_setup)

    trailing = pd.concat([report.without_overlap(group, ["symbol"]) for _, group in outcomes["trailing"].groupby("setup")])
    winners = {
        setup: {"recent": winner_stats(group[group["date"] >= oos_start]), "all": winner_stats(group)}
        for setup, group in trailing.groupby("setup")
    }
    recent = table[table["period"] == "out_of_sample"].set_index("exit_style")["expectancy_pct"]
    tuning = table[table["period"] == "tuning"].set_index("exit_style")["expectancy_pct"]
    best = tuning.idxmax()   # chosen on the tuning period only, as in Stage 4b

    out = OUT_DIR / "sample" if args.sample else OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    text = [
        "# Stage 5: exit styles compared on the Stage 4b candidates", "",
        "Same candidates, same entries and stops; only the exit differs. After commission, fees and tax.",
        "trailing = trailing stop only; target = sell all at the next major resistance; hybrid = sell half there, trail the rest.", "",
        "## All four setups together", "", report.markdown(table), "",
        "## Per setup", "", report.markdown(setup_table), "",
        f"Best on the tuning period (average result per trade): **{best}**. Out-of-sample average result per trade: "
        + ", ".join(f"{style} {recent[style]:+.2f}%" for style in STYLES) + ".", "",
        "## How far past winners went (trailing exit, price gain from entry)", "",
        report.markdown(pd.DataFrame([{"setup": setup, **{f"{period}_{key}": value for period, stats in data.items() for key, value in stats.items()}} for setup, data in winners.items()])), "",
    ]
    (out / "exit_styles.md").write_text("\n".join(text), encoding="utf-8")
    if not args.sample:
        lines = [
            "# Exit styles and past winners. Written by `python -m nepse_bot.backtest.exits` (Stage 5).",
            f"# Source: reports/stage4b/final/trades.csv; out-of-sample from {oos_start}. Details: reports/stage5/exit_styles.md",
            f"default_exit_style: {best}",
            "exit_styles:",
        ]
        for style in STYLES:
            lines.append(f"  {style}:")
            for period in ("tuning", "out_of_sample"):
                data = summary[style][period]
                lines.append(f"    {period}: {{trades: {data['trades']}, win_rate: {data['win_rate']}, expectancy_pct: {data['expectancy_pct']}, profit_factor: {data['profit_factor']}}}")
        lines.append("winners:   # price gain of past winning trades, % above the entry")
        for setup, data in winners.items():
            lines.append(f"  {setup}:")
            for period, stats in data.items():
                lines.append(f"    {period}: {{" + ", ".join(f"{key}: {value}" for key, value in stats.items()) + "}")
        (CONFIG_DIR / "exits.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(table.to_string(index=False))
    print("best on tuning:", best)
    print(winners)


if __name__ == "__main__":
    main()
