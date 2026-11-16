"""Stage 4b strategy lab: simple entry rules x simple exit styles, after all costs.

Every combination is traded as the owner could really trade it: buy at the
next day's open, at most 2 new positions a day and 5 open at once, one per
stock, Rs 1 lakh each, T+2, commission, fees and tax. Results are shown for
the tuning period and the out-of-sample period separately, and per year, so
a rule that only worked in one stretch of history is easy to spot.

    python -m nepse_bot.backtest.lab [--sample]   # writes reports/stage4b/02_strategy_lab.md
"""

from __future__ import annotations

import argparse
import os
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import pandas as pd

from nepse_bot.backtest import report
from nepse_bot.backtest.panel import OUT_DIR
from nepse_bot.backtest.simulate import PlanSpec, simulate_plan
from nepse_bot.engine.config import load_profile
from nepse_bot.portfolio.fees import fee_schedule

PER_DAY, MAX_OPEN, TOP_PER_DAY = 2, 5, 3
SETUPS = ("trend_pullback", "base_breakout", "rs_leader", "value_turn")

# name -> PlanSpec settings; "stop_atr" is turned into a stop price per trade
EXITS: dict[str, dict[str, Any]] = {
    "trail3atr_hold40": {"stop_atr": 2.5, "trail": "chandelier", "trail_from": "entry", "chandelier_multiple": 3.0, "time_stop_sessions": None, "max_hold_sessions": 40},
    "hold20_stop": {"stop_atr": 2.5, "trail": "none", "time_stop_sessions": None, "max_hold_sessions": 20},
    "half_at_3atr_ema20": {"stop_atr": 2.0, "target_atr": 3.0, "trail": "ema20", "trail_from": "t1", "time_stop_sessions": 30, "time_stop_gain": 0.0, "max_hold_sessions": 60},
}


def entry_rules(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Each rule returns its candidate rows with a ``rank`` column (higher = taken first)."""
    clean = panel[~panel["flags"].fillna("").str.contains("OPERATOR")]
    rules: dict[str, pd.DataFrame] = {}
    for setup in SETUPS:
        rules[f"setup:{setup}"] = clean[(clean[f"t_{setup}"] == True) & (clean["score"] >= 60)].assign(rank=lambda f: f["score"])  # noqa: E712
    rules["top_score_75"] = clean[clean["score"] >= 75].assign(rank=lambda f: f["score"])
    uptrend = clean[(clean["close"] > clean["sma200"]) & (clean["close"] > clean["ema50"])]
    rules["momentum_roc60"] = uptrend.assign(rank=lambda f: f["roc60"]).dropna(subset=["rank"])
    rules["value_in_uptrend"] = clean[(clean["s_trend"] >= 50)].assign(rank=lambda f: f["value_score"]).dropna(subset=["rank"])
    rules["value_in_uptrend"] = rules["value_in_uptrend"][rules["value_in_uptrend"]["rank"] >= 70]
    washed = clean[clean["regime_score"] <= 25]
    rules["washed_out_market_top_score"] = washed.assign(rank=lambda f: f["score"])
    calm = clean[clean["pct_above_ma50"] < 80]
    rules["top_score_75_not_overheated"] = calm[calm["score"] >= 75].assign(rank=lambda f: f["score"])
    return {name: frame.sort_values(["date", "rank"], ascending=[True, False]).groupby("date").head(TOP_PER_DAY) for name, frame in rules.items()}


def make_spec(row: dict[str, Any], exit_name: str, max_position: float, lot: int) -> PlanSpec | None:
    settings = dict(EXITS[exit_name])
    close, atr = float(row["close"]), float(row["atr14"]) if pd.notna(row["atr14"]) else float(row["close"]) * 0.03
    quantity = int(max_position / close // lot) * lot
    if quantity <= 0:
        return None
    stop = round(close - settings.pop("stop_atr") * atr, 1)
    target = settings.pop("target_atr", None)
    return PlanSpec(entry=None, stop=max(stop, close * 0.75), quantity=quantity,
                    target_1=round(close + target * atr, 1) if target else None,
                    breakeven_stop=round(close * 1.008, 1) if target else None, **settings)


def _simulate_symbol(job: tuple[str, list[dict[str, Any]], float]) -> list[dict[str, Any]]:
    from nepse_bot.backtest.run import load_history

    symbol, rows, max_position = job
    bars, fees = load_history(symbol), fee_schedule()
    out = []
    for row in rows:
        for exit_name in EXITS:
            spec = make_spec(row, exit_name, max_position, fees.board_lot)
            if spec is None:
                continue
            out.append({"symbol": symbol, "date": row["date"], "exit": exit_name, **simulate_plan(bars, row["date"], spec, fees)})
    return out


def simulate_candidates(rules: dict[str, pd.DataFrame], max_position: float, workers: int) -> pd.DataFrame:
    """Simulate every distinct (stock, day) under every exit style, once."""
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    distinct = pd.concat(rules.values())[["symbol", "date", "close", "atr14"]].drop_duplicates(["symbol", "date"])
    jobs = [(symbol, group.to_dict("records"), max_position) for symbol, group in distinct.groupby("symbol")]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        outputs = list(pool.map(_simulate_symbol, jobs, chunksize=4))
    return pd.DataFrame([row for output in outputs for row in output])


def take_trades(candidates: pd.DataFrame) -> pd.DataFrame:
    """Apply the position limits: best-ranked first, 2 new a day, 5 open, one per stock."""
    taken, open_until = [], {}
    for day, group in candidates.groupby("date", sort=True):
        open_until = {symbol: until for symbol, until in open_until.items() if until > day}
        placed = 0
        for index, row in group.sort_values("rank", ascending=False).iterrows():
            if placed >= PER_DAY or len(open_until) >= MAX_OPEN:
                break
            if row["symbol"] in open_until or not row["filled"]:
                continue
            placed += 1
            taken.append(index)
            open_until[row["symbol"]] = row["exit_date"]
    return candidates.loc[taken]


def evaluate(rules: dict[str, pd.DataFrame], outcomes: pd.DataFrame, oos_start: str, capital: float) -> tuple[pd.DataFrame, pd.DataFrame, dict[tuple[str, str], pd.DataFrame]]:
    """Summary per rule x exit for each period, the same per year, and the trades themselves."""
    summary, yearly, books = [], [], {}
    for rule, frame in rules.items():
        for exit_name in EXITS:
            merged = frame.merge(outcomes[outcomes["exit"] == exit_name], on=["symbol", "date"], how="inner")
            merged["filled"] = merged["filled"].fillna(False).astype(bool)
            trades = take_trades(merged.reset_index(drop=True))
            books[(rule, exit_name)] = trades
            for period, part in (("tune", trades[trades["date"] < oos_start]), ("oos", trades[trades["date"] >= oos_start])):
                numbers = report.metrics(part, capital)
                summary.append({"rule": rule, "exit": exit_name, "period": period, **{k: numbers.get(k) for k in
                                ("trades", "win_rate", "avg_win", "avg_loss", "profit_factor", "expectancy_pct", "net_profit", "max_drawdown", "avg_sessions")}})
            by_year = trades.groupby(trades["date"].str[:4])["net_profit"].agg(["count", "sum"]) if len(trades) else pd.DataFrame()
            row: dict[str, Any] = {"rule": rule, "exit": exit_name}
            for year, values in by_year.iterrows():
                row[year] = f"{values['sum'] / 1000:+.0f}k ({int(values['count'])})"
            yearly.append(row)
    return pd.DataFrame(summary), pd.DataFrame(yearly), books


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="last 250 days of the panel only")
    parser.add_argument("--oos-start", default="2024-11-13")
    parser.add_argument("--workers", type=int, default=6)
    arguments = parser.parse_args()
    panel = pd.read_pickle(OUT_DIR / "panel_fwd.pkl")
    if arguments.sample:
        days = sorted(panel["date"].unique())[-250:]
        panel = panel[panel["date"] >= days[0]]
    profile = load_profile(None)
    rules = entry_rules(panel)
    outcomes = simulate_candidates(rules, profile.max_position, arguments.workers)
    summary, yearly, books = evaluate(rules, outcomes, arguments.oos_start, profile.total_capital)
    name = "sample_strategy_lab" if arguments.sample else "02_strategy_lab"
    wide = summary.pivot(index=["rule", "exit"], columns="period", values=["trades", "win_rate", "profit_factor", "expectancy_pct", "net_profit", "max_drawdown"])
    wide.columns = [f"{b}_{a}" for a, b in wide.columns]
    order = [c for p in ("tune", "oos") for c in wide.columns if c.startswith(p)]
    text = ["# Stage 4b study 2: entry rules x exit styles, after all costs", "",
            f"Limits: {PER_DAY} new positions a day, {MAX_OPEN} open, Rs {profile.max_position:,.0f} each, buy at next open. "
            f"tune = before {arguments.oos_start}, oos = after. expectancy_pct = average net result per trade in %.", "",
            "## Summary", "", report.markdown(wide[order].reset_index()),
            "## Net profit per year in Rs thousand (number of trades)", "", report.markdown(yearly.fillna("-")),
            "## Full detail", "", report.markdown(summary)]
    (OUT_DIR / f"{name}.md").write_text("\n".join(text), encoding="utf-8")
    summary.to_csv(OUT_DIR / f"{name}.csv", index=False)
    pd.to_pickle(books, OUT_DIR / f"{name}_trades.pkl")
    print(f"written {OUT_DIR / (name + '.md')}")


if __name__ == "__main__":
    main()
