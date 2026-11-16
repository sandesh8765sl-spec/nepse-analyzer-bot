"""Turn simulated trades into the Stage 4 tables: per-setup results, the gate,
the 2x-versus-1.5x comparison, the walk-forward check and the portfolio view."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from nepse_bot.engine.config import Profile, setting

SETUP_NAMES = {
    "trend_pullback": "Trend pullback", "base_breakout": "Base breakout", "rs_leader": "RS leader",
    "value_turn": "Value + turn", "broker_accumulation": "Broker accumulation", "event_runup": "Event run-up",
}


# ------------------------------------------------------------------ selection
def without_overlap(signals: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Drop signals that arrive while an earlier trade in the same group is still open.

    The live bot repeats a signal on following days; a trader takes it once.
    An order that did not fill does not block the next signal.
    """
    kept = []
    for _, group in signals.sort_values("date").groupby(keys, sort=False):
        busy_until = ""
        for index, row in group.iterrows():
            if row["date"] <= busy_until:
                continue
            kept.append(index)
            if row["filled"]:
                busy_until = row["exit_date"]
    return signals.loc[sorted(kept)]


def portfolio_trades(buys: pd.DataFrame, regimes: pd.DataFrame, profile: Profile) -> pd.DataFrame:
    """The trades the owner could actually have taken: best signals first, within the position limits."""
    per_day = setting("regime", "max_new_positions_per_day", default={}) or {}
    regime_on = dict(zip(regimes["date"], regimes["regime"]))
    hot_limit = float(setting("regime", "overheated_pct_above_ma50", default=75))
    hot_new = int(setting("regime", "max_new_positions_when_overheated", default=1))
    hot_days = set(regimes.loc[regimes["pct_above_ma50"] >= hot_limit, "date"]) if "pct_above_ma50" in regimes else set()
    taken: list[Any] = []
    open_until: dict[str, str] = {}          # symbol -> exit date of its open position
    ordered = buys.sort_values(["date", "score", "reward_risk"], ascending=[True, False, False])
    for day, group in ordered.groupby("date", sort=True):
        open_until = {symbol: until for symbol, until in open_until.items() if until > day}
        allowed_new = hot_new if day in hot_days else int(per_day.get(regime_on.get(day, "NEUTRAL"), 1))
        placed = 0
        for index, row in group.iterrows():
            if placed >= allowed_new or len(open_until) >= profile.max_open_positions:
                break
            if row["symbol"] in open_until:
                continue
            placed += 1
            taken.append(index)
            if row["filled"]:
                open_until[row["symbol"]] = row["exit_date"]
    return buys.loc[taken]


# -------------------------------------------------------------------- metrics
def metrics(trades: pd.DataFrame, capital: float) -> dict[str, Any]:
    """Results of a set of orders (filled or not), after costs and tax."""
    filled = trades[trades["filled"]] if len(trades) else trades
    count = len(filled)
    if count == 0:
        return {"orders": len(trades), "trades": 0, "win_rate": None, "avg_win": None, "avg_loss": None, "profit_factor": None,
                "expectancy": None, "expectancy_pct": None, "net_profit": 0.0, "max_drawdown": 0.0, "avg_sessions": None, "still_open": 0}
    net = filled["net_profit"]
    wins, losses = net[net > 0], net[net <= 0]
    curve = filled.sort_values("exit_date")["net_profit"].cumsum()
    drawdown = float((curve.cummax().clip(lower=0.0) - curve).max())
    loss_total = float(-losses.sum())
    return {
        "orders": len(trades), "trades": count, "win_rate": round(100.0 * len(wins) / count, 1),
        "avg_win": round(float(wins.mean()), 0) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 0) if len(losses) else 0.0,
        "avg_win_pct": round(float(filled.loc[net > 0, "net_percent"].mean()), 2) if len(wins) else 0.0,
        "avg_loss_pct": round(float(filled.loc[net <= 0, "net_percent"].mean()), 2) if len(losses) else 0.0,
        "profit_factor": round(float(wins.sum()) / loss_total, 2) if loss_total > 0 else math.inf,
        "expectancy": round(float(net.mean()), 0), "expectancy_pct": round(float(filled["net_percent"].mean()), 2),
        "net_profit": round(float(net.sum()), 0), "max_drawdown": round(drawdown, 0),
        "max_drawdown_pct": round(100.0 * drawdown / capital, 1) if capital else None,
        "avg_sessions": round(float(filled["sessions_held"].mean()), 1), "still_open": int((filled["status"] == "open").sum()),
    }


def index_return(index: pd.Series, start: str, end: str) -> float | None:
    """NEPSE index change in percent between two dates."""
    part = index[(index.index >= start) & (index.index <= end)]
    return round(100.0 * (float(part.iloc[-1]) / float(part.iloc[0]) - 1.0), 1) if len(part) > 1 else None


def setup_table(signals: pd.DataFrame, capital: float, buys_only: bool = True) -> pd.DataFrame:
    """One row per setup and risk-target rule."""
    rows = []
    pool = signals[signals["is_buy"]] if buys_only else signals
    for (setup, r_rule), group in pool.groupby(["setup", "r_rule"]):
        chosen = without_overlap(group, ["symbol"])
        rows.append({"setup": setup, "r_rule": r_rule, **metrics(chosen, capital)})
    return pd.DataFrame(rows)


def _gate_row(table: pd.DataFrame, setup: str, r_rule: float) -> tuple[int, float | None]:
    chosen = table[(table["r_rule"] == r_rule) & (table["setup"] == setup)] if len(table) else table
    if not len(chosen):
        return 0, None
    factor = chosen["profit_factor"].iloc[0]
    return int(chosen["trades"].iloc[0]), (None if pd.isna(factor) else float(factor))


def gate_table(recent: pd.DataFrame, earlier: pd.DataFrame, r_rule: float) -> pd.DataFrame:
    """Pass/fail for each setup: it must pass in BOTH the out-of-sample and the tuning period."""
    min_pf = float(setting("backtest", "gate_min_profit_factor", default=1.2))
    min_trades = int(setting("backtest", "gate_min_trades", default=50))
    rows = []
    for setup, name in SETUP_NAMES.items():
        problems, row = [], {"setup": setup, "name": name}
        for label, table in (("recent", recent), ("earlier", earlier)):
            trades, factor = _gate_row(table, setup, r_rule)
            row[f"{label}_trades"], row[f"{label}_profit_factor"] = trades, factor
            if trades < min_trades:
                problems.append(f"{label}: only {trades} trades (need {min_trades})")
            if factor is None or factor < min_pf:
                problems.append(f"{label}: profit factor {'n/a' if factor is None else factor} (need {min_pf})")
        rows.append({**row, "passed": not problems, "reason": "passes" if not problems else "; ".join(problems)})
    return pd.DataFrame(rows)


def evidence_document(recent: pd.DataFrame, earlier: pd.DataFrame, r_rule: float, recent_period: str, earlier_period: str) -> str:
    """The text of config/evidence.yaml: each setup's record in both periods."""
    lines = ["# Backtest record of each setup, after commission, fees and tax.",
             "# Written by `python -m nepse_bot.backtest`; copy to config/evidence.yaml to use it in the bot.",
             "setups:"]
    for setup in SETUP_NAMES:
        lines.append(f"  {setup}:")
        for label, table, period in (("recent", recent, recent_period), ("earlier", earlier, earlier_period)):
            chosen = table[(table["r_rule"] == r_rule) & (table["setup"] == setup)] if len(table) else table
            if not len(chosen):
                lines.append(f'    {label}: {{period: "{period}", trades: 0}}')
                continue
            row = chosen.iloc[0]

            def value(key: str) -> str:
                item = row.get(key)
                if item is None or pd.isna(item):
                    return "null"
                return "99" if math.isinf(float(item)) else f"{float(item):g}"

            lines.append(
                f'    {label}: {{period: "{period}", trades: {int(row["trades"])}, win_rate: {value("win_rate")}, avg_win_pct: {value("avg_win_pct")}, '
                f'avg_loss_pct: {value("avg_loss_pct")}, expectancy_pct: {value("expectancy_pct")}, profit_factor: {value("profit_factor")}}}')
    return "\n".join(lines) + "\n"


def walk_forward(signals: pd.DataFrame, regimes: pd.DataFrame, profile: Profile, r_rules: list[float], first_test_year: int) -> pd.DataFrame:
    """Each year: pick the rule that earned more on all EARLIER data, then see how it did that year."""
    books = {r: portfolio_trades(signals[(signals["r_rule"] == r) & signals["is_buy"]], regimes, profile) for r in r_rules}
    years = sorted({day[:4] for day in regimes["date"]})
    rows = []
    for year in years:
        if int(year) < first_test_year:
            continue
        before = {r: float(book[(book["date"] < f"{year}-01-01") & book["filled"]]["net_profit"].sum()) for r, book in books.items()}
        during = {r: float(book[(book["date"].str[:4] == year) & book["filled"]]["net_profit"].sum()) for r, book in books.items()}
        counts = {r: int(((book["date"].str[:4] == year) & book["filled"]).sum()) for r, book in books.items()}
        picked = max(r_rules, key=lambda r: before[r])
        row: dict[str, Any] = {"test_year": year, "picked_from_earlier_data": picked, "net_of_pick": round(during[picked], 0)}
        for r in r_rules:
            row[f"earlier_net_{r}x"] = round(before[r], 0)
            row[f"year_net_{r}x"] = round(during[r], 0)
            row[f"year_trades_{r}x"] = counts[r]
        rows.append(row)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------- markdown
def markdown(frame: pd.DataFrame) -> str:
    """A small markdown table (no extra library needed)."""
    if frame is None or frame.empty:
        return "_no rows_\n"

    def cell(value: Any) -> str:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "-"
        if isinstance(value, float):
            if math.isinf(value):
                return "no losses"
            return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:g}"
        return str(value)

    lines = ["| " + " | ".join(str(column) for column in frame.columns) + " |", "|" + "---|" * len(frame.columns)]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join(lines) + "\n"
