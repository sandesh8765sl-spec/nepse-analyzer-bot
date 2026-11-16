"""Stage 5 research: a long-horizon value + quality portfolio, after all costs.

Idea tested: every month or quarter, rank liquid stocks by sector-relative
value (earnings yield, book-to-price) and quality (ROE, EPS growth, cash
dividend yield), hold the best ten for 3-12 months. Optionally pause new
buying while the market is "overheated". Fundamentals are used only from
their publication date. Compared with holding the NEPSE index and with an
equal-weight basket of all liquid stocks.

    python -m nepse_bot.backtest.longterm [--sample]   # writes reports/stage5/longterm.md
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from nepse_bot.backtest import report
from nepse_bot.db.store import get_database
from nepse_bot.engine.config import setting
from nepse_bot.portfolio.fees import FeeSchedule, fee_schedule

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "reports" / "stage5"


def cfg(key: str, default: Any) -> Any:
    return setting("longterm", key, default=default)


def _days_between(start: str, end: str) -> int:
    return (pd.Timestamp(end) - pd.Timestamp(start)).days


def _shift(day: str, days: int) -> str:
    return (pd.Timestamp(day) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")


class Data:
    """Everything the test needs, loaded once (read only)."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        db = get_database()
        prices = db.frame(
            "SELECT p.symbol, p.date, p.open, p.close, p.volume, p.turnover, p.adj_open, p.adj_close "
            "FROM daily_prices p JOIN securities s ON s.symbol = p.symbol "
            "WHERE s.instrument_type = 'equity' AND s.is_promoter = 0"
        )
        if symbols:
            prices = prices[prices["symbol"].isin(symbols)]
        prices["turnover"] = prices["turnover"].where(prices["turnover"] > 0, prices["volume"] * prices["close"])
        self.close = prices.pivot(index="date", columns="symbol", values="close")
        self.adj_close = prices.pivot(index="date", columns="symbol", values="adj_close")
        self.adj_open = prices.pivot(index="date", columns="symbol", values="adj_open")
        turnover = prices.pivot(index="date", columns="symbol", values="turnover")
        self.dates = list(self.close.index)
        traded = self.close.notna()
        self.liquid = (
            (turnover.fillna(0).rolling(20).mean() >= float(setting("universe", "min_avg_turnover_20", default=5e6)))
            & (traded.rolling(60).mean() * 100 >= float(setting("universe", "min_traded_days_pct_60", default=80)))
            & (traded.cumsum() >= int(setting("universe", "min_listed_trading_days", default=120)))
        )
        self.adj_filled = self.adj_close.ffill()
        self.sector = {row["symbol"]: row["sector"] for row in db.query("SELECT symbol, sector FROM securities")}
        self.fundamentals = db.frame(
            "SELECT symbol, fiscal_year, quarter, publication_date, COALESCE(eps_annualized, eps) AS eps, book_value, roe "
            "FROM fundamentals WHERE publication_date IS NOT NULL ORDER BY publication_date"
        )
        self.fundamentals["fy"] = pd.to_numeric(self.fundamentals["fiscal_year"].str[:4], errors="coerce")
        self.dividends = db.frame(
            "SELECT symbol, amount, announcement_date, book_closure_date FROM corporate_actions "
            "WHERE action_type = 'cash_dividend' AND amount > 0 ORDER BY announcement_date"
        )
        self.dividend_days = {(row.symbol, row.book_closure_date): float(row.amount) for row in self.dividends.itertuples()}
        self.breadth = db.frame("SELECT date, pct_above_ma50 FROM market_breadth_daily").set_index("date")["pct_above_ma50"]
        self.index = db.frame("SELECT date, close FROM index_daily WHERE index_code = 'NEPSE' ORDER BY date").set_index("date")["close"]

    # ---------------------------------------------------------------- ranking
    def rank(self, day: str) -> pd.DataFrame:
        """Liquid stocks with usable fundamentals on ``day``, best first."""
        liquid = self.liquid.loc[day]
        frame = pd.DataFrame({"close": self.close.loc[day][liquid[liquid].index]}).dropna()
        known = self.fundamentals[self.fundamentals["publication_date"] <= day]
        oldest = _shift(day, int(setting("scoring", "fundamentals_max_age_days", default=460)))
        latest = known[known["publication_date"] >= oldest].groupby("symbol").tail(1).set_index("symbol")
        frame = frame.join(latest[["eps", "book_value", "roe", "fy", "quarter"]], how="inner")
        frame = frame[(frame["eps"] > 0) & (frame["book_value"] > 0)]
        if frame.empty:
            return frame
        previous = known.set_index(["symbol", "fy", "quarter"])["eps"]
        previous = previous[~previous.index.duplicated(keep="last")]
        before = previous.reindex(list(zip(frame.index, frame["fy"] - 1, frame["quarter"]))).to_numpy()
        frame["eps_growth"] = np.clip((frame["eps"].to_numpy() - before) / np.abs(before), -2, 2)
        since = _shift(day, int(cfg("dividend_lookback_days", 400)))
        paid = self.dividends[(self.dividends["announcement_date"] <= day) & (self.dividends["announcement_date"] >= since)]
        frame["dividend_yield"] = paid.groupby("symbol")["amount"].last().reindex(frame.index).fillna(0) / frame["close"]
        frame["earnings_yield"] = frame["eps"] / frame["close"]
        frame["book_to_price"] = frame["book_value"] / frame["close"]
        frame["sector"] = [self.sector.get(symbol) or "?" for symbol in frame.index]
        sizes = frame.groupby("sector")["close"].transform("size")
        group = frame["sector"].where(sizes >= int(setting("scoring", "min_sector_peers", default=5)), "MARKET")

        def pct(column: str) -> pd.Series:
            return frame[column].groupby(group).rank(pct=True).fillna(0.5) * 100

        frame["value"] = (pct("earnings_yield") + pct("book_to_price")) / 2
        frame["quality"] = (pct("roe") + pct("eps_growth") + pct("dividend_yield")) / 3
        frame["composite"] = (frame["value"] + frame["quality"]) / 2
        frame = frame.sort_values("composite", ascending=False)
        frame["rank"] = range(1, len(frame) + 1)
        return frame

    def rebalance_days(self, start: str, end: str, monthly: bool) -> list[str]:
        months = set(range(1, 13)) if monthly else set(cfg("quarterly_months", [3, 6, 9, 12]))
        days: list[str] = []
        seen: set[str] = set()
        for day in self.dates:
            if start <= day < end and day[:7] not in seen:
                seen.add(day[:7])
                if int(day[5:7]) in months:
                    days.append(day)
        return days


def simulate(data: Data, start: str, end: str, monthly: bool, pause_overheated: bool, fees: FeeSchedule) -> dict[str, Any]:
    """Run the portfolio from ``start`` to ``end``; everything is sold on the last day."""
    capital, slots = float(cfg("capital", 500000)), int(cfg("positions", 10))
    min_hold, max_hold, buffer = int(cfg("min_hold_days", 90)), int(cfg("max_hold_days", 365)), int(cfg("sell_rank_buffer", 2))
    dividend_keep = 1 - float(cfg("dividend_tax_percent", 5)) / 100
    overheated = float(setting("regime", "overheated_pct_above_ma50", default=75))
    days = [day for day in data.dates if start <= day <= end]
    decisions = set(data.rebalance_days(start, end, monthly))
    cash, equity, paused = capital, capital, 0
    holdings: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    curve: dict[str, float] = {}
    pending: tuple[list[str], list[str]] | None = None

    def close_position(symbol: str, day: str, at_open: bool, reason: str) -> None:
        nonlocal cash
        pos = holdings.pop(symbol)
        adj_exit = data.adj_open.at[day, symbol] if at_open else np.nan
        if pd.isna(adj_exit):
            adj_exit = data.adj_filled.at[day, symbol]
        held = _days_between(pos["date"], day)
        result = fees.round_trip(pos["qty"], pos["price"], pos["price"] * adj_exit / pos["adj_entry"], held)
        cash += pos["cost"] + result.net_profit
        net = result.net_profit + pos["dividends"]
        trades.append({"symbol": symbol, "entry_date": pos["date"], "exit_date": day, "held_days": held, "reason": reason,
                       "buy_amount": round(result.buy_amount), "net_profit": round(net), "net_pct": round(100 * net / result.buy_amount, 2),
                       "dividends": round(pos["dividends"])})

    for i, day in enumerate(days):
        if pending:  # orders decided on the previous trading day are filled at today's open
            sells, buys = pending
            for symbol in sells:
                if symbol in holdings:
                    close_position(symbol, day, True, "rebalance")
            for symbol in buys:
                free = slots - len(holdings)
                adj_open, adj_close, close = data.adj_open.at[day, symbol], data.adj_close.at[day, symbol], data.close.at[day, symbol]
                if free <= 0:
                    break
                if symbol in holdings or pd.isna(adj_open) or pd.isna(adj_close) or pd.isna(close):
                    continue
                raw_open = close * adj_open / adj_close
                qty = fees.round_lot(min(cash / free, equity / slots) * 0.99 / raw_open)
                cost = fees.buy_cost(qty, raw_open)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                holdings[symbol] = {"qty": qty, "price": raw_open, "adj_entry": adj_open, "date": day, "cost": cost, "dividends": 0.0}
            pending = None
        value = 0.0
        for symbol, pos in holdings.items():
            worth = pos["qty"] * pos["price"] * data.adj_filled.at[day, symbol] / pos["adj_entry"]
            amount = data.dividend_days.get((symbol, day))  # cash dividend at book closure
            close = data.close.at[day, symbol]
            if amount and pos["date"] < day and pd.notna(close) and close > 0:
                paid = worth / close * amount * dividend_keep
                pos["dividends"] += paid
                cash += paid
            value += worth
        equity = cash + value
        curve[day] = equity
        if day in decisions and i + 1 < len(days):
            ranking = data.rank(day)
            ranks = ranking["rank"] if len(ranking) else pd.Series(dtype=float)
            sells = []
            for symbol, pos in holdings.items():
                held = _days_between(pos["date"], day)
                if held >= min_hold and (ranks.get(symbol, 10**6) > slots * buffer or held >= max_hold):
                    sells.append(symbol)
            hot = pause_overheated and float(data.breadth.get(day, 0) or 0) >= overheated
            paused += int(hot)
            buys = [] if hot or len(ranking) < slots * buffer else [s for s in ranking.index[: slots * buffer] if s not in holdings]
            pending = (sells, buys)
    for symbol in list(holdings):
        close_position(symbol, days[-1], False, "end of test")
    curve[days[-1]] = cash
    frame = pd.DataFrame(trades)
    equity_curve = pd.Series(curve)
    wins = frame[frame["net_profit"] > 0]["net_profit"] if len(frame) else pd.Series(dtype=float)
    losses = frame[frame["net_profit"] <= 0]["net_profit"] if len(frame) else pd.Series(dtype=float)
    return {
        "trades": len(frame), "win_rate": round(100 * len(wins) / max(1, len(frame)), 1),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if losses.sum() < 0 else None,
        "avg_held_days": int(round(frame["held_days"].mean())) if len(frame) else 0,
        "net_profit": int(round(cash - capital)), "return_pct": round(100 * (cash / capital - 1), 1),
        "max_drawdown_pct": round(float((1 - equity_curve / equity_curve.cummax()).max() * 100), 1),
        "paused_rebalances": paused, "frame": frame,
    }


def benchmarks(data: Data, start: str, end: str, fees: FeeSchedule) -> dict[str, Any]:
    """NEPSE index, and an equal-weight basket of every liquid stock (re-formed each quarter)."""
    days = [day for day in data.dates if start <= day <= end]
    reform = set(data.rebalance_days(start, end, monthly=False)) | {days[0]}
    returns = data.adj_filled.loc[days].pct_change(fill_method=None)
    value, members, curve = 1.0, [], []
    for day in days:
        if members:
            change = returns.loc[day, members].mean(skipna=True)
            value *= 1 + (0.0 if pd.isna(change) else float(change))
        if day in reform:
            row = data.liquid.loc[day]
            members = list(row[row].index)
        curve.append(value)
    series = pd.Series(curve)
    gross = value - 1
    cost = fees.round_trip_cost_percent(1000, 500) / 100
    net = gross - cost - max(0.0, gross - cost) * fees.cgt_percent(_days_between(start, end)) / 100
    return {"nepse_index_pct": report.index_return(data.index, start, end), "basket_gross_pct": round(100 * gross, 1),
            "basket_net_pct": round(100 * net, 1), "basket_max_drawdown_pct": round(float((1 - series / series.cummax()).max() * 100), 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Long-horizon value + quality test")
    parser.add_argument("--sample", action="store_true", help="60 liquid stocks only, quick check")
    args = parser.parse_args()
    db, fees = get_database(), fee_schedule()
    symbols = None
    if args.sample:
        symbols = [r["symbol"] for r in db.query("SELECT symbol FROM daily_prices WHERE date >= '2025-01-01' GROUP BY symbol ORDER BY SUM(turnover) DESC LIMIT 60")]
    data = Data(symbols)
    end = data.dates[-1]
    oos_start = (pd.Timestamp(end) - pd.DateOffset(years=int(setting("backtest", "out_of_sample_years", default=2)))).strftime("%Y-%m-%d")
    periods = {"tuning": (str(cfg("tuning_start", "2022-08-01")), oos_start), "out_of_sample": (oos_start, end)}
    min_pf = float(setting("backtest", "gate_min_profit_factor", default=1.2))
    min_trades = int(setting("backtest", "gate_min_trades", default=50))
    rows, bench_rows, yearly = [], [], []
    out = OUT_DIR / "sample" if args.sample else OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    for period, (start, stop) in periods.items():
        bench = benchmarks(data, start, stop, fees)
        bench_rows.append({"period": period, "from": start, "to": stop, **bench})
        for monthly in (True, False):
            for pause in (False, True):
                name = ("monthly" if monthly else "quarterly") + ("_pause_overheated" if pause else "")
                result = simulate(data, start, stop, monthly, pause, fees)
                frame = result.pop("frame")
                frame.to_csv(out / f"longterm_trades_{period}_{name}.csv", index=False)
                passed = (result["profit_factor"] or 0) >= min_pf and result["trades"] >= min_trades
                rows.append({"variant": name, "period": period, **result, "gate": "pass" if passed else "fail",
                             "beats_index": result["return_pct"] > (bench["nepse_index_pct"] or 0),
                             "beats_basket": result["return_pct"] > bench["basket_net_pct"]})
                if len(frame):
                    by_year = frame.groupby(frame["exit_date"].str[:4])["net_profit"].sum().round()
                    yearly.append({"variant": name, "period": period, **by_year.to_dict()})
    table, bench_table = pd.DataFrame(rows), pd.DataFrame(bench_rows)
    both = table.pivot(index="variant", columns="period", values="gate")
    passed_names = [name for name in both.index if (both.loc[name] == "pass").all()]
    text = [
        "# Stage 5 Part A: long-horizon value + quality portfolio", "",
        f"10 equal holdings from Rs {float(cfg('capital', 500000)):,.0f}, held 3-12 months, after commission, fees, capital gains tax and dividend tax.",
        f"Gate: profit factor >= {min_pf} over >= {min_trades} finished holdings in BOTH periods.", "",
        "## Results", "", report.markdown(table), "",
        "## Benchmarks (index: no costs, no dividends; basket: all liquid stocks, equal weight)", "", report.markdown(bench_table), "",
        "## Net profit by year of sale (Rs)", "", report.markdown(pd.DataFrame(yearly).fillna(0)), "",
        f"**Variants passing the gate in both periods: {', '.join(passed_names) if passed_names else 'none'}.**", "",
    ]
    (out / "longterm.md").write_text("\n".join(text), encoding="utf-8")
    print(table.drop(columns=["paused_rebalances", "avg_held_days"]).to_string(index=False))
    print(bench_table.to_string(index=False))
    print("PASS:", passed_names or "none")


if __name__ == "__main__":
    main()
