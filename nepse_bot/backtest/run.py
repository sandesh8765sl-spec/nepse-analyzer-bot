"""Command line for the Stage 4 backtest.

    python -m nepse_bot.backtest --sample      # 30 liquid stocks, 1 year: a quick check
    python -m nepse_bot.backtest               # everything (takes a while)
    python -m nepse_bot.backtest --reuse       # rebuild the tables from the saved signals

Results go to reports/stage4/ (or reports/stage4/sample/). Nothing is written
to the database and config/engine.yaml is never changed by this command.
"""

from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from nepse_bot.backtest import report
from nepse_bot.backtest.replay import replay
from nepse_bot.backtest.simulate import PlanSpec, prepare_bars, simulate_plan
from nepse_bot.db.store import get_database
from nepse_bot.engine.config import load_profile, setting
from nepse_bot.portfolio.fees import fee_schedule
from nepse_bot.settings import load_yaml, CONFIG_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PLAN_KEY = ["symbol", "date", "setup", "entry", "stop", "quantity"]
WARM_UP_SESSIONS = 250


def load_history(symbol: str) -> pd.DataFrame:
    """A stock's full adjusted price history, ready for the simulation."""
    frame = get_database().frame(
        "SELECT date, COALESCE(adj_open, open) AS open, COALESCE(adj_high, high) AS high, COALESCE(adj_low, low) AS low, "
        "COALESCE(adj_close, close) AS close, COALESCE(adj_factor, 1) AS adj_factor FROM daily_prices WHERE symbol = ? ORDER BY date",
        (symbol,),
    )
    frame["date"] = pd.to_datetime(frame["date"])
    return prepare_bars(frame.set_index("date"))


def _simulate_symbol(job: tuple[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    symbol, plans = job
    bars = load_history(symbol)
    fees = fee_schedule()
    sell_after = int(load_yaml(CONFIG_DIR / "market.yaml").get("earliest_sell_after_days", 2))
    return [{**{key: plan[key] for key in PLAN_KEY}, **simulate_plan(bars, plan["date"], PlanSpec.from_mapping(plan), fees, sell_after)} for plan in plans]


def simulate_all(signals: pd.DataFrame, workers: int) -> pd.DataFrame:
    """Simulate every distinct plan once and attach the outcome to each signal row."""
    distinct = signals.drop_duplicates(PLAN_KEY)
    jobs = [(symbol, group.to_dict("records")) for symbol, group in distinct.groupby("symbol")]
    if workers <= 1:
        outputs = [_simulate_symbol(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(_simulate_symbol, jobs))
    outcomes = pd.DataFrame([row for output in outputs for row in output])
    merged = signals.merge(outcomes, on=PLAN_KEY, how="left", suffixes=("", "_sim"))
    merged["filled"] = merged["filled"].fillna(False).astype(bool)
    return merged


def liquid_sample(count: int, end: str) -> list[str]:
    """The most traded ordinary shares over the 120 days before ``end``."""
    start = (date.fromisoformat(end) - timedelta(days=120)).isoformat()
    rows = get_database().query(
        "SELECT p.symbol FROM daily_prices p JOIN securities s ON s.symbol = p.symbol WHERE s.instrument_type = 'equity' "
        "AND s.is_promoter = 0 AND p.date >= ? AND p.date <= ? GROUP BY p.symbol ORDER BY SUM(p.turnover) DESC LIMIT ?",
        (start, end, count),
    )
    return [row["symbol"] for row in rows]


def build_reports(signals: pd.DataFrame, rejections: pd.DataFrame, regimes: pd.DataFrame, out: Path, r_rules: list[float], note: str) -> dict[str, Any]:
    """Write every table and SUMMARY.md; return the headline numbers."""
    db = get_database()
    profile = load_profile(db)
    capital = profile.total_capital
    index = db.frame("SELECT date, close FROM index_daily WHERE index_code = 'NEPSE' ORDER BY date").set_index("date")["close"].astype(float)
    start, end = regimes["date"].min(), regimes["date"].max()
    oos_years = float(setting("backtest", "out_of_sample_years", default=2))
    oos_start = (date.fromisoformat(end) - timedelta(days=round(365.25 * oos_years))).isoformat()
    oos = signals[signals["date"] >= oos_start]
    older = signals[signals["date"] < oos_start]
    oos_regimes, older_regimes = regimes[regimes["date"] >= oos_start], regimes[regimes["date"] < oos_start]

    # ---- what the owner could actually have traded
    compare = []
    for label, part, part_regimes in (("older data (tuning)", older, older_regimes), ("last 2 years (out-of-sample)", oos, oos_regimes)):
        for r in r_rules:
            book = report.portfolio_trades(part[(part["r_rule"] == r) & part["is_buy"]], part_regimes, profile) if len(part) else part
            numbers = report.metrics(book, capital)
            window_start = part_regimes["date"].min() if len(part_regimes) else start
            window_end = part_regimes["date"].max() if len(part_regimes) else end
            compare.append({"period": label, "rule": f"{r}x", **{k: numbers.get(k) for k in ("orders", "trades", "win_rate", "profit_factor", "net_profit", "max_drawdown", "expectancy")},
                            "return_on_capital_pct": round(100.0 * numbers["net_profit"] / capital, 1),
                            "nepse_index_pct": report.index_return(index, window_start, window_end)})
    compare_table = pd.DataFrame(compare)
    oos_rows = compare_table[compare_table["period"].str.startswith("last")]
    best_rule = float(oos_rows.sort_values(["net_profit", "trades"], ascending=False)["rule"].iloc[0].rstrip("x")) if len(oos_rows) else r_rules[0]

    # ---- per setup
    setup_oos = report.setup_table(oos, capital) if len(oos) else pd.DataFrame()
    setup_older = report.setup_table(older, capital) if len(older) else pd.DataFrame()
    setup_any_plan = report.setup_table(oos, capital, buys_only=False) if len(oos) else pd.DataFrame()
    gate = report.gate_table(setup_oos, setup_older, best_rule)
    evidence = report.evidence_document(setup_oos, setup_older, best_rule, f"{max(oos_start, start)} to {end}", f"{start} to {oos_start}")
    forward = report.walk_forward(signals, regimes, profile, r_rules, first_test_year=int(start[:4]) + 2) if len(signals) else pd.DataFrame()

    out.mkdir(parents=True, exist_ok=True)
    signals.to_csv(out / "trades.csv", index=False)
    rejections.to_csv(out / "rejections.csv", index=False)
    regimes.to_csv(out / "regimes.csv", index=False)
    compare_table.to_csv(out / "rule_comparison.csv", index=False)
    setup_oos.to_csv(out / "setups_out_of_sample.csv", index=False)
    setup_older.to_csv(out / "setups_older_data.csv", index=False)
    setup_any_plan.to_csv(out / "setups_any_valid_plan.csv", index=False)
    gate.to_csv(out / "gate.csv", index=False)
    (out / "evidence.yaml").write_text(evidence, encoding="utf-8")
    forward.to_csv(out / "walk_forward.csv", index=False)

    filled = signals[signals["filled"]] if len(signals) else signals
    locked = int(filled.drop_duplicates(PLAN_KEY)["exit_locked"].sum()) if len(filled) else 0
    columns = ["setup", "orders", "trades", "win_rate", "avg_win", "avg_loss", "avg_win_pct", "avg_loss_pct", "profit_factor", "expectancy", "expectancy_pct", "net_profit", "max_drawdown", "avg_sessions", "still_open"]
    show = lambda table: report.markdown(table[[c for c in columns if c in table.columns]]) if len(table) else "_no trades_\n"  # noqa: E731
    reject_view = rejections.pivot_table(index=["setup", "reason"], columns="r_rule", values="count", fill_value=0).reset_index() if len(rejections) else rejections
    text = [
        "# Stage 4 backtest results", "",
        note, "",
        f"- Days replayed: {len(regimes)} ({start} to {end}). Out-of-sample = from {max(oos_start, start)}.",
        f"- Capital Rs {capital:,.0f}, position Rs {profile.min_position:,.0f}-{profile.max_position:,.0f}, max {profile.max_open_positions} open positions.",
        "- All money figures are NET: after commission, SEBON fee, DP charge and capital gains tax.",
        f"- NEPSE index over the out-of-sample period: {report.index_return(index, max(oos_start, start), end)}%.",
        f"- Exits that happened on a day the price did not move at all (possibly locked at the limit): {locked}.", "",
        "## 1. Trades the owner could really take (position limits applied, all setups together)", "", report.markdown(compare_table),
        "## 2. Each setup, out-of-sample, BUY signals only", "", show(setup_oos),
        f"## 3. The gate: profit factor >= {setting('backtest', 'gate_min_profit_factor', default=1.2)} with >= {setting('backtest', 'gate_min_trades', default=50)} trades, in BOTH periods", "",
        report.markdown(gate),
        "## 4. Year by year (trades the owner could really take)", "", report.markdown(forward),
        "## 5. Each setup on the older (tuning) data, BUY signals only", "", show(setup_older),
        "## 6. For information: every valid plan out-of-sample, ignoring the score threshold", "", show(setup_any_plan),
        "## 7. Why plans were rejected (count of stock-days)", "", report.markdown(reject_view),
    ]
    (out / "SUMMARY.md").write_text("\n".join(text), encoding="utf-8")
    return {"best_rule": best_rule, "gate": gate, "compare": compare_table, "setup_oos": setup_oos, "forward": forward, "any_plan": setup_any_plan}


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the engine on history and measure each setup after costs.")
    parser.add_argument("--sample", action="store_true", help="quick check: 30 liquid stocks, the last year")
    parser.add_argument("--start", help="first day to replay (YYYY-MM-DD)")
    parser.add_argument("--end", help="last day to replay (YYYY-MM-DD)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--reuse", action="store_true", help="skip the replay and reuse the saved signals")
    parser.add_argument("--out", default="backtest", help="folder under reports/ for the results")
    arguments = parser.parse_args()
    # Worker processes inherit these: one maths thread each, or 8 workers x 16 threads exhaust the memory.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"

    from nepse_bot.utils.keep_awake import keep_awake

    with keep_awake():          # a full run takes about an hour; do not let the PC fall asleep on idle
        _run(arguments)


def _run(arguments: argparse.Namespace) -> None:
    db = get_database()
    out = PROJECT_ROOT / "reports" / arguments.out / ("sample" if arguments.sample else "")
    out.mkdir(parents=True, exist_ok=True)
    # Several values of one plan setting can be compared in a single replay (Stage 4 compared 2x and 1.5x targets).
    r_rules = [float(value) for value in setting("backtest", "r_rules_to_compare", default=[0.0])]
    all_days = db.trading_dates()
    end = arguments.end or all_days[-1]
    symbols = None
    if arguments.sample:
        start = arguments.start or (date.fromisoformat(end) - timedelta(days=365)).isoformat()
        symbols = liquid_sample(30, end)
        note = f"SAMPLE RUN: {len(symbols)} most-traded stocks, one year. For catching bugs only - not for decisions."
    else:
        start = arguments.start or all_days[WARM_UP_SESSIONS]
        note = "FULL RUN: every stock, every day with enough history."
    days = [day for day in all_days if start <= day <= end]

    began = time.time()
    if arguments.reuse:
        signals = pd.read_csv(out / "signals.csv")
        rejections = pd.read_csv(out / "rejections.csv")
        regimes = pd.read_csv(out / "regimes.csv")
    else:
        print(f"Replaying {len(days)} days ({days[0]} to {days[-1]}) with {arguments.workers} workers ...", flush=True)
        signals, rejections, regimes = replay(days, r_rules, symbols, load_profile(db), arguments.workers)
        signals.to_csv(out / "signals.csv", index=False)
        rejections.to_csv(out / "rejections.csv", index=False)
        regimes.to_csv(out / "regimes.csv", index=False)
        print(f"Replay done in {(time.time() - began) / 60:.1f} min: {len(signals)} valid plans.", flush=True)
    if signals.empty:
        print("No valid plans were produced; nothing to simulate.")
        return
    simulated = simulate_all(signals, arguments.workers)
    print(f"Simulated {len(simulated.drop_duplicates(PLAN_KEY))} distinct plans in {(time.time() - began) / 60:.1f} min total.", flush=True)
    headline = build_reports(simulated, rejections, regimes, out, r_rules, note)
    print(f"Setups passing the gate: {int(headline['gate']['passed'].sum())}. Tables are in {out / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
