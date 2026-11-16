"""Stage 4b research: does the score predict returns, and which parts of it do?

Works on the panel built by ``panel.py`` (one row per stock per past day).
For every row it measures what the stock did next - bought at the NEXT day's
open, valued 10, 20 and 40 of its own trading days later - and compares that
with the average stock on the same day ("excess" return), so a rising or
falling market does not flatter or punish the score.

    python -m nepse_bot.backtest.study            # writes reports/stage4b/01_score_study.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from nepse_bot.backtest import report
from nepse_bot.backtest.panel import OUT_DIR
from nepse_bot.db.store import get_database

HORIZONS = (10, 20, 40)
SUB_SCORES = ("s_trend", "s_momentum", "s_relative_strength", "s_volume_flow", "s_fundamentals", "s_setup_quality")
SETUPS = ("trend_pullback", "base_breakout", "rs_leader", "value_turn", "event_runup")
SCORE_BINS = [0, 45, 55, 65, 75, 85, 101]
SCORE_LABELS = ["<45", "45-55", "55-65", "65-75", "75-85", "85+"]


def load_panel(path: Path, oos_start: str) -> pd.DataFrame:
    """The panel with forward returns, excess returns and derived features added."""
    panel = pd.read_csv(path, low_memory=False)
    prices = get_database().frame(
        "SELECT symbol, date, COALESCE(adj_open, open) AS open, COALESCE(adj_close, close) AS close FROM daily_prices ORDER BY symbol, date")
    grouped = prices.groupby("symbol", sort=False)
    next_open = grouped["open"].shift(-1)
    for days in HORIZONS:
        prices[f"fwd{days}"] = (grouped["close"].shift(-days) / next_open - 1.0) * 100.0
    panel = panel.merge(prices[["symbol", "date", *[f"fwd{d}" for d in HORIZONS]]], on=["symbol", "date"], how="left")
    for days in HORIZONS:
        column = f"fwd{days}"
        panel[column] = panel[column].clip(-60, 150)                       # one freak print must not drive an average
        panel[f"x{days}"] = panel[column] - panel.groupby("date")[column].transform("mean")
    panel["period"] = np.where(panel["date"] >= oos_start, "oos", "tune")
    panel["score_bucket"] = pd.cut(panel["score"], SCORE_BINS, labels=SCORE_LABELS, right=False)
    panel["dist_ema20"] = (panel["close"] / panel["ema20"] - 1) * 100
    panel["dist_ema50"] = (panel["close"] / panel["ema50"] - 1) * 100
    panel["dist_sma200"] = (panel["close"] / panel["sma200"] - 1) * 100
    panel["dist_high252"] = (panel["close"] / panel["high_252"] - 1) * 100
    panel["atr_pct"] = panel["atr14"] / panel["close"] * 100
    panel["log_turnover"] = np.log10(panel["turnover_avg20"].clip(lower=1))
    panel["earnings_yield"] = np.where(panel["pe"] > 0, 100.0 / panel["pe"], np.nan)
    panel["book_yield"] = np.where(panel["pb"] > 0, 1.0 / panel["pb"], np.nan)
    return panel


def bucket_table(panel: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Forward returns by group: rows, average, median, share that rose, average excess."""
    rows = []
    for key, group in panel.groupby(by, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(by, key))
        row["rows"] = len(group)
        for days in HORIZONS:
            values = group[f"fwd{days}"].dropna()
            row[f"avg{days}"] = round(values.mean(), 2) if len(values) else None
            row[f"up{days}%"] = round(100 * (values > 0).mean(), 0) if len(values) else None
            row[f"excess{days}"] = round(group[f"x{days}"].mean(), 2)
        rows.append(row)
    return pd.DataFrame(rows)


def information_table(panel: pd.DataFrame, features: list[str], horizon: int = 20, step: int = 10) -> pd.DataFrame:
    """How well each feature ranks stocks by their next ``horizon``-day excess return.

    For every ``step``-th day the stocks are ranked by the feature and by what
    they did next; "ic" is the average rank correlation (0 = useless, 0.05 is
    useful, 0.10 is strong) and "t" says how consistent it was (above about 2
    is unlikely to be luck).
    """
    target = f"x{horizon}"
    rows = []
    for period, part in (("tune", panel[panel["period"] == "tune"]), ("oos", panel[panel["period"] == "oos"])):
        days = sorted(part["date"].unique())[::step]
        part = part[part["date"].isin(days)]
        for feature in features:
            values = []
            for _, group in part.groupby("date"):
                pair = group[[feature, target]].dropna()
                if len(pair) >= 15 and pair[feature].nunique() > 3:
                    values.append(pair[feature].rank().corr(pair[target].rank()))
            series = pd.Series(values, dtype=float).dropna()
            if len(series) >= 5:
                rows.append({"feature": feature, "period": period, "days": len(series), "ic": round(series.mean(), 3),
                             "t": round(series.mean() / series.std() * np.sqrt(len(series)), 1) if series.std() > 0 else None})
    table = pd.DataFrame(rows)
    if table.empty:
        return table
    wide = table.pivot(index="feature", columns="period", values=["ic", "t"])
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    return wide.reset_index().sort_values(wide.columns[0], ascending=False)


def top_slice_table(panel: pd.DataFrame, feature: str, share: float = 0.1) -> pd.DataFrame:
    """Each day take the best ``share`` of stocks by a feature; how did they do next, by period and regime?"""
    rank = panel.groupby("date")[feature].rank(pct=True)
    top = panel[rank >= 1 - share]
    return bucket_table(top, ["period", "regime"])


def yearly_factor_table(panel: pd.DataFrame, share: float = 0.2) -> pd.DataFrame:
    """20-day excess return of the best ``share`` of stocks by each input, year by year.

    A real edge shows up as the same sign in most years. Nothing here does.
    """
    year = panel["date"].str[:4]
    rows = {}
    picks = {"score": True, "rs_rank": True, "roc60": True, "roc20": True, "value_score": True, "s_fundamentals": True,
             "s_trend": True, "dist_high252": True, "atr_pct": False, "log_turnover": False, "rsi14": False}
    for feature, high in picks.items():
        rank = panel.groupby("date")[feature].rank(pct=True)
        chosen = rank >= 1 - share if high else rank <= share
        rows[("highest " if high else "lowest ") + feature] = panel[chosen].groupby(year[chosen])["x20"].mean().round(2)
    return pd.DataFrame(rows).T.reset_index().rename(columns={"index": "top 20% by"})


def market_state_table(panel: pd.DataFrame) -> pd.DataFrame:
    """Average 20-day return of ALL stocks (bought at the next open) by the state of the market, year by year."""
    year = panel["date"].str[:4]
    frames = []
    states = {
        "regime label": panel["regime"],
        "% of stocks above 50-day avg": pd.cut(panel["pct_above_ma50"], [0, 25, 50, 75, 100], include_lowest=True).astype(str),
        "10-day breadth": pd.cut(panel["breadth_10d"], [0, 45, 50, 55, 100]).astype(str),
    }
    for name, state in states.items():
        table = panel.groupby([state, year])["fwd20"].mean().round(1).unstack()
        table.insert(0, "state", [f"{name}: {value}" for value in table.index])
        frames.append(table.reset_index(drop=True))
    return pd.concat(frames, ignore_index=True)


def index_timing_table() -> pd.DataFrame:
    """Simple "be in the market only when..." rules applied to the NEPSE index, before any cost."""
    index = get_database().frame("SELECT date, close FROM index_daily WHERE index_code = 'NEPSE' ORDER BY date").set_index("date")["close"].astype(float)
    tomorrow = index.pct_change().shift(-1)
    recent = index.index >= "2020"
    rules = {
        "always in the market": pd.Series(True, index=index.index),
        "index above its 50-day average": index > index.rolling(50).mean(),
        "index above its 200-day average": index > index.rolling(200).mean(),
        "50-day average above 200-day": index.rolling(50).mean() > index.rolling(200).mean(),
        "index higher than 20 days ago": index.pct_change(20) > 0,
        "index higher than 60 days ago": index.pct_change(60) > 0,
    }
    rows = []
    for name, signal in rules.items():
        signal = signal.astype(bool)
        row = {"rule": name, "days in market %": round(100 * signal[recent].mean()), "switches": int((signal[recent] != signal[recent].shift()).sum())}
        for year in sorted({day[:4] for day in index.index if day >= "2020"}):
            mask = (index.index.str[:4] == year) & signal
            row[year] = round(100 * ((1 + tomorrow[mask]).prod() - 1), 0)
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", default=str(OUT_DIR / "panel.csv.gz"))
    parser.add_argument("--oos-start", default="2024-11-13")
    parser.add_argument("--out", default=str(OUT_DIR / "01_score_study.md"))
    arguments = parser.parse_args()
    panel = load_panel(Path(arguments.panel), arguments.oos_start)
    features = ["score", *SUB_SCORES, "rs_rank", "roc5", "roc20", "roc60", "roc120", "rsi14", "dist_ema20", "dist_ema50", "dist_sma200",
                "dist_high252", "atr_pct", "range20", "log_turnover", "earnings_yield", "book_yield", "eps_growth", "roe",
                "dividend_yield", "value_score", "sector_score", "macd_hist"]
    md = report.markdown
    text = [
        "# Stage 4b study 1: does the score predict returns?", "",
        f"Rows: {len(panel):,} stock-days, {panel['date'].nunique()} days ({panel['date'].min()} to {panel['date'].max()}). "
        f"'tune' = before {arguments.oos_start}, 'oos' = after (kept aside).", "",
        "Returns: bought at the next day's open, in percent, before costs (a round trip costs about 0.75% plus 5% tax on gains). "
        "avgN = average after N trading days, upN% = share that rose, excessN = versus the average stock that day.", "",
        "## 1. By score bucket", "", md(bucket_table(panel, ["period", "score_bucket"])),
        "## 2. By score bucket and market regime", "", md(bucket_table(panel, ["period", "regime", "score_bucket"])),
        "## 3. The whole universe by market regime (is the regime label useful?)", "", md(bucket_table(panel, ["period", "regime"])),
        "## 4. Which inputs rank stocks correctly? (20-day excess return)", "", md(information_table(panel, features)),
        "## 5. Same, 40-day excess return", "", md(information_table(panel, features, horizon=40, step=20)),
        "## 6. By verdict", "", md(bucket_table(panel, ["period", "verdict"])),
        "## 7. Days a setup triggered (before any plan rule)", "",
    ]
    for setup in SETUPS:
        hit = panel[panel[f"t_{setup}"] == True]  # noqa: E712
        text += [f"### {setup}", "", md(bucket_table(hit, ["period", "regime"])) if len(hit) else "_never triggered_\n"]
    text += ["## 8. Top 10% of stocks each day by total score", "", md(top_slice_table(panel, "score")),
             "## 9. By sector (all rows, and rows scoring 75+)", "", md(bucket_table(panel, ["period", "sector"])),
             md(bucket_table(panel[panel["score"] >= 75], ["period", "sector"]))]
    Path(arguments.out).write_text("\n".join(text), encoding="utf-8")
    panel.to_pickle(OUT_DIR / "panel_fwd.pkl")          # reused by lab.py
    more = ["# Stage 4b study 3: is anything consistent year by year?", "",
            "## 1. Best 20% of stocks by each input: 20-day return versus the average stock (percent)", "", md(yearly_factor_table(panel)),
            "## 2. Average 20-day return of all stocks, by the state of the market on the day of buying", "", md(market_state_table(panel)),
            "## 3. Market-timing rules on the NEPSE index (percent gained per year while 'in', before costs)", "", md(index_timing_table())]
    (Path(arguments.out).parent / "03_consistency_by_year.md").write_text("\n".join(more), encoding="utf-8")
    print(f"written {arguments.out}")


if __name__ == "__main__":
    main()
