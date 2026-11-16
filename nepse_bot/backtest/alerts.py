"""How often each alert type would have fired in the backtest, and how those trades ended.

Reads the candidates and simulated trades of the Stage 4b backtest
(reports/stage4b/final/trades.csv) and applies the alert filters of
config/alerts.yaml to them. Nothing is re-tuned here: it only counts.

    python -m nepse_bot.backtest.alerts

Writes config/alerts_backtest.yaml (read by /settings alerts and by the alert
text) and reports/alerts/backtest.md.

Early alerts need intraday prices, which the database does not hold, so they
are NOT backtested; the live journal is their only test.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Any

import pandas as pd

from nepse_bot.alerts.config import alert_setting
from nepse_bot.db.store import get_database
from nepse_bot.engine.config import setting
from nepse_bot.engine.market import classify_sectors
from nepse_bot.settings import CONFIG_DIR, PROJECT_ROOT

SOURCE = PROJECT_ROOT / "reports" / "stage4b" / "final"
OUTPUT = CONFIG_DIR / "alerts_backtest.yaml"
REPORT = PROJECT_ROOT / "reports" / "alerts" / "backtest.md"


def summarise(trades: pd.DataFrame) -> dict[str, Any]:
    """Count, win rate, average result and profit factor of a set of simulated trades."""
    done = trades[(trades["filled"] == True) & (trades["status"] != "open")]  # noqa: E712
    if done.empty:
        return {"alerts": int(len(trades)), "finished": 0}
    won = done.loc[done["net_profit"] > 0, "net_profit"].sum()
    lost = -done.loc[done["net_profit"] <= 0, "net_profit"].sum()
    return {
        "alerts": int(len(trades)), "finished": int(len(done)), "win_rate": round(float((done["net_profit"] > 0).mean() * 100), 1),
        "expectancy_pct": round(float(done["net_percent"].mean()), 2), "net_rupees": round(float(done["net_profit"].sum()), 0),
        "profit_factor": round(float(won / lost), 2) if lost > 0 else None,
    }


def _avg_range(db: Any) -> pd.DataFrame:
    """20-day average high-low range, % of price, for every stock and day."""
    prices = db.frame("SELECT symbol, date, COALESCE(adj_high, high) AS high, COALESCE(adj_low, low) AS low, COALESCE(adj_close, close) AS close FROM daily_prices ORDER BY symbol, date")
    prices["range_pct"] = (prices["high"] - prices["low"]) / prices["close"] * 100.0
    prices["avg_range"] = prices.groupby("symbol")["range_pct"].transform(lambda series: series.rolling(20, min_periods=10).mean())
    return prices[["symbol", "date", "avg_range"]]


def _weak_sectors(db: Any, days: list[str]) -> set[tuple[str, str]]:
    """(date, sector) pairs where the sector was rated weak on that date."""
    rows = db.frame("SELECT index_code, date, close FROM index_daily ORDER BY date")
    series = {code: group.set_index("date")["close"].astype(float) for code, group in rows.groupby("index_code")}
    weak: set[tuple[str, str]] = set()
    for day in days:
        upto = {code: values.loc[:day].tail(260) for code, values in series.items()}
        for sector, state in classify_sectors(upto, upto.get("NEPSE", pd.Series(dtype=float))).items():
            if state.label == "weak":
                weak.add((day, sector))
    return weak


def _price_events(db: Any) -> pd.DataFrame:
    rows = db.frame("SELECT symbol, announcement_date, book_closure_date FROM corporate_actions WHERE book_closure_date IS NOT NULL")
    return rows


def top_picks(trades: pd.DataFrame, db: Any, source: Any = None, all_days: list[str] | None = None) -> pd.DataFrame:
    """Apply the TOP PICK filter day by day: at most one a day, no repeat of a stock within 10 trading days.

    ``source`` = the backtest folder (default: Stage 4b); ``all_days`` = every replayed day, needed when
    ``trades`` is only a part of the candidates (the broker study).
    """
    source = source or SOURCE
    cfg = lambda key, default: float(alert_setting("top_pick", key, default=default))  # noqa: E731
    # The reference target does not filter (Part 3); a close resistance only ranks a stock slightly lower.
    pool = trades[(trades["is_buy"] == True) & (trades["score"] >= cfg("min_score", 80)) & (trades["confidence"] == "High")].copy()  # noqa: E712
    close_above = pool["reward_risk"] < float(setting("plan", "resistance_warning_reward_risk", default=1.5))
    pool["rank_score"] = pool["score"] * close_above.map({True: float(setting("plan", "resistance_rank_penalty", default=0.97)), False: 1.0})
    pool = pool.merge(_avg_range(db), on=["symbol", "date"], how="left")
    pool = pool[pool["avg_range"] <= cfg("max_avg_range_percent", 2.5)]
    regimes = pd.read_csv(source / "regimes.csv")
    hot =set(regimes.loc[regimes["pct_above_ma50"] >= float(setting("regime", "overheated_pct_above_ma50", default=75)), "date"])
    pool = pool[~pool["date"].isin(hot)]
    weak = _weak_sectors(db, sorted(pool["date"].unique()))
    pool = pool[[(day, sector) not in weak for day, sector in zip(pool["date"], pool["sector"])]]
    events = _price_events(db)
    window = int(cfg("holding_window_sessions", 40) * 7 / 5) + 2
    keep = []
    for row in pool.itertuples():
        end = (date.fromisoformat(row.date) + timedelta(days=window)).isoformat()
        hits = events[(events["symbol"] == row.symbol) & (events["book_closure_date"] > row.date) & (events["book_closure_date"] <= end)
                      & (events["announcement_date"].isna() | (events["announcement_date"] <= row.date))]
        keep.append(hits.empty)
    pool = pool[keep]
    all_days = sorted(set(all_days) | set(trades["date"])) if all_days is not None else sorted(trades["date"].unique())
    position ={day: index for index, day in enumerate(all_days)}
    gap = int(alert_setting("no_repeat_trading_days", default=10))
    last: dict[str, int] = {}
    chosen = []
    for day, group in pool.sort_values(["date", "rank_score"], ascending=[True, False]).groupby("date", sort=True):
        for row in group.itertuples():
            if row.symbol in last and position[day] - last[row.symbol] < gap:
                continue
            chosen.append(row.Index)
            last[row.symbol] = position[day]
            break
    return pool.loc[chosen]


def holding_stats(trades: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Trading days a finished trade was held, per setup (median and the middle half)."""
    done = trades[(trades["is_buy"] == True) & (trades["filled"] == True) & (trades["status"] != "open")]  # noqa: E712
    result = {}
    for setup, group in done.groupby("setup"):
        sessions = group["sessions_held"].dropna()
        winners = group.loc[group["net_profit"] > 0, "sessions_held"].dropna()
        if len(sessions) < 20:
            continue
        result[str(setup)] = {
            "trades": int(len(sessions)), "median": int(sessions.median()), "p25": int(sessions.quantile(0.25)), "p75": int(sessions.quantile(0.75)),
            "winners_median": int(winners.median()) if len(winners) else None,
        }
    return result


def _yaml_map(data: dict[str, Any]) -> str:
    return "{" + ", ".join(f"{key}: {'null' if value is None else value}" for key, value in data.items()) + "}"


def main() -> int:
    trades = pd.read_csv(SOURCE / "trades.csv", low_memory=False)
    db = get_database()
    split = str(trades["date"].max())
    years = int(setting("backtest", "out_of_sample_years", default=2))
    cut = (date.fromisoformat(split) - timedelta(days=365 * years)).isoformat()
    recent = trades[trades["date"] >= cut]
    earlier = trades[trades["date"] < cut]
    picks = top_picks(trades, db)
    proven = trades[trades["verdict"].isin(["BUY", "STRONG BUY"])]
    results = {
        "strong": {"recent": summarise(proven[proven["date"] >= cut]), "earlier": summarise(proven[proven["date"] < cut])},
        "toppick": {"recent": summarise(picks[picks["date"] >= cut]), "earlier": summarise(picks[picks["date"] < cut])},
        "all_candidates": {"recent": summarise(recent[recent["is_buy"] == True]), "earlier": summarise(earlier[earlier["is_buy"] == True])},  # noqa: E712
    }
    holding = holding_stats(trades)
    # How often there is something to look at: days with at least one candidate that has a plan.
    replay_days = pd.read_csv(SOURCE / "regimes.csv")["date"]
    recent_days = replay_days[replay_days >= cut]
    plan_days = set(recent.loc[recent["is_buy"] == True, "date"])  # noqa: E712
    coverage = {
        "days": int(len(recent_days)), "days_with_a_plan": int(recent_days.isin(plan_days).sum()),
        "percent_of_days": round(100.0 * float(recent_days.isin(plan_days).mean()), 1) if len(recent_days) else 0.0,
        "candidates_per_day": round(float((recent["is_buy"] == True).sum()) / max(1, len(recent_days)), 1),  # noqa: E712
        "resistance_close_percent": round(100.0 * float((recent.loc[recent["is_buy"] == True, "reward_risk"] < 1.5).mean()), 1),  # noqa: E712
    }
    lines = [
        "# How often each alert type would have fired in the backtest, and how those trades ended (after fees and tax).",
        f"# Written by `python -m nepse_bot.backtest.alerts` from reports/stage4b/final. Recent = {cut} to {split}; earlier = before that.",
        "# Early alerts are NOT backtested: they need intraday prices, which the database does not hold.",
        f"recent_period: \"{cut} to {split}\"",
        f"earlier_period: \"{trades['date'].min()} to {cut}\"",
        "alerts:",
    ]
    for name, periods in results.items():
        lines.append(f"  {name}:")
        for period, data in periods.items():
            lines.append(f"    {period}: {_yaml_map(data)}")
    lines.append(f"coverage: {_yaml_map(coverage)}   # recent period: days with at least one candidate that has a plan")
    lines.append("holding:   # trading days a finished backtest trade was held, per setup")
    for setup, data in holding.items():
        lines.append(f"  {setup}: {_yaml_map(data)}")
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    text = ["# Alerts in the backtest", "", f"Recent = {cut} to {split} (never used for tuning). After commission, fees and tax.", "",
            "| Alert | Period | Alerts | Finished | Won | Avg result per trade | Profit factor | Net |", "|---|---|---|---|---|---|---|---|"]
    names = {"strong": "STRONG BUY / BUY", "toppick": "TOP PICK (max 1 a day)", "all_candidates": "(for comparison) every candidate with a plan"}
    for name, periods in results.items():
        for period, data in periods.items():
            if not data.get("finished"):
                text.append(f"| {names[name]} | {period} | {data['alerts']} | 0 | - | - | - | - |")
            else:
                text.append(f"| {names[name]} | {period} | {data['alerts']} | {data['finished']} | {data['win_rate']}% | {data['expectancy_pct']:+.2f}% | {data['profit_factor']} | Rs {data['net_rupees']:+,.0f} |")
    text += ["", "Early alerts: not backtested (no intraday price history). The live journal (/report) tracks them separately.", "",
             "TOP PICK filter: score 80+ on full data, calm stock (average daily range 2.5% or less), sector not weak, market not overheated,",
             "no book closure in the holding window, best one per day, no repeat within 10 trading days. The reference target does not filter;",
             "a resistance closer than 1.5x the stop distance only ranks a stock slightly lower.", "",
             f"Recent period: {coverage['days_with_a_plan']} of {coverage['days']} days ({coverage['percent_of_days']}%) had at least one candidate with a plan,",
             f"{coverage['candidates_per_day']} a day on average; {coverage['resistance_close_percent']}% of them carry the \"Resistance close above\" warning."]
    REPORT.write_text("\n".join(text) + "\n", encoding="utf-8")
    print("\n".join(text[4:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
