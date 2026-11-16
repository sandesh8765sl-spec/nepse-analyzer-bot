"""Play one trade plan forward on the days after its signal.

Order handling (fills, gaps through a stop, limit orders) is done by the
``backtesting.py`` library; this module only tells it the plan's rules:

* buy on the next trading day - at the open, or with a limit order that fills
  only if that day's low reaches the limit;
* no selling before T+2 (``earliest_sell_after_days`` in config/market.yaml);
* stop-loss for the whole position; if the stop and a target are both touched
  on the same day, the stop is assumed to come first;
* optional target 1 where part of the position is sold, optionally moving the
  stop on the rest to breakeven + costs;
* optional trailing exit: a close below EMA20, or a chandelier stop (highest
  high since entry minus N x ATR), from the start or only after target 1;
* optional time stop (too little gain after N sessions) and maximum holding time.

Costs and capital gains tax are then applied with the bot's own fee rules.
The live journal uses this same function, so live results and backtest
results are measured in exactly the same way.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, fields
from typing import Any

os.environ.setdefault("TQDM_DISABLE", "1")   # no progress bars from the backtesting library

import pandas as pd  # noqa: E402
from backtesting import Backtest, Strategy  # noqa: E402

from nepse_bot.engine.indicators import atr, ema  # noqa: E402
from nepse_bot.portfolio.fees import FeeSchedule  # noqa: E402

MAX_HOLD_SESSIONS = 250
_CASH = 1_000_000_000.0   # never the limit: position size comes from the plan


@dataclass(frozen=True)
class PlanSpec:
    """Everything the simulation needs to follow a plan (prices in the signal day's terms).

    The defaults reproduce the original Stage 3 plan: limit entry, half off at
    target 1, breakeven stop, EMA20 trail after target 1, 15-session time stop.
    """

    entry: float | None                     # limit price; None = buy at the next open
    stop: float
    quantity: int
    target_1: float | None = None
    breakeven_stop: float | None = None     # stop for the rest after target 1 (None = leave the stop)
    t1_fraction: float = 0.5                # share of the position sold at target 1
    trail: str = "ema20"                    # ema20 / chandelier / none
    trail_from: str = "t1"                  # t1 = only after target 1, entry = from the first day
    chandelier_multiple: float = 2.5
    time_stop_sessions: int | None = 15
    time_stop_gain: float = 3.0             # percent
    max_hold_sessions: int | None = None
    entry_valid_sessions: int = 1

    @classmethod
    def from_mapping(cls, row: Any) -> "PlanSpec":
        """Build from a stored plan (journal row, CSV row); unknown or empty fields keep their defaults."""
        values: dict[str, Any] = {}
        optional = ("entry", "target_1", "breakeven_stop", "time_stop_sessions", "max_hold_sessions")
        for item in fields(cls):
            if item.name not in row:
                continue
            value = row.get(item.name)
            if value is None or (isinstance(value, float) and pd.isna(value)):
                if item.name in optional:
                    values[item.name] = None          # present but empty = "not used"
                continue
            values[item.name] = value
        if row.get("entry_style") == "open":
            values["entry"] = None                    # buy at the next open, not with a limit
        for name in ("entry", "stop", "target_1", "breakeven_stop", "t1_fraction", "chandelier_multiple", "time_stop_gain"):
            if values.get(name) is not None:
                values[name] = float(values[name])
        for name in ("quantity", "time_stop_sessions", "max_hold_sessions", "entry_valid_sessions"):
            if values.get(name) is not None:
                values[name] = int(values[name])
        values.setdefault("entry", None)
        return cls(**values)


class _PlanStrategy(Strategy):
    """One plan, followed mechanically."""

    spec: PlanSpec = None  # type: ignore[assignment]
    first: int = 0          # shares sold at target 1 (or the whole position when there is no second part)
    rest: int = 0
    sell_after: int = 2

    def init(self) -> None:
        self.entry_bar: int | None = None
        self.armed = False
        self.t1_done = False
        self.reasons: dict[str, str] = {}
        self.highest = 0.0
        self.stop_now = self.spec.stop

    def next(self) -> None:
        bar = len(self.data) - 1
        spec = self.spec
        if bar == 1:                                    # the signal day's close: place the order for tomorrow
            self.buy(size=self.first, limit=spec.entry, tag="t1")
            if self.rest:
                self.buy(size=self.rest, limit=spec.entry, tag="run")
            return
        if self.entry_bar is None:
            if self.trades:
                self.entry_bar = bar
            else:
                if bar >= 1 + spec.entry_valid_sessions:
                    for order in list(self.orders):
                        order.cancel()
                return
        if not self.trades:
            return
        self.highest = max(self.highest, float(self.data.High[-1]))
        held = bar - self.entry_bar
        if held < self.sell_after - 1:
            return
        if not self.t1_done and spec.target_1 is not None and any(
                t.tag == "t1" and t.exit_price >= spec.target_1 - 1e-6 for t in self.closed_trades):
            self.t1_done = True
            if spec.breakeven_stop is not None:
                self.stop_now = max(self.stop_now, spec.breakeven_stop)
        trailing = spec.trail_from == "entry" or self.t1_done
        if trailing and spec.trail == "chandelier":
            self.stop_now = max(self.stop_now, self.highest - spec.chandelier_multiple * float(self.data.atr14[-1]))
        for trade in self.trades:                       # target first, stop second: the stop is then checked first
            if not self.armed and trade.tag == "t1" and spec.target_1 is not None and (self.rest or spec.t1_fraction >= 1.0):
                trade.tp = spec.target_1
            if trade.sl is None or self.stop_now > trade.sl + 1e-9:
                trade.sl = self.stop_now
        self.armed = True
        close = float(self.data.Close[-1])
        if trailing and spec.trail == "ema20" and close < float(self.data.ema20[-1]):
            self._sell_all("trail")
        elif spec.max_hold_sessions is not None and held >= spec.max_hold_sessions:
            self._sell_all("max_hold")
        elif not self.t1_done and spec.time_stop_sessions is not None and held >= spec.time_stop_sessions:
            if close < self.trades[0].entry_price * (1 + spec.time_stop_gain / 100.0):
                self._sell_all("time_stop")

    def _sell_all(self, reason: str) -> None:
        for trade in self.trades:
            self.reasons.setdefault(trade.tag, reason)
            trade.close()


def _stop_touched(leg: Any, data: pd.DataFrame, exit_bar: int) -> bool:
    """True if the exit day's low reached the leg's stop (a real stop-out, not the end of the data)."""
    stop = leg.get("SL")
    return stop is not None and pd.notna(stop) and float(data["Low"].iloc[exit_bar]) <= float(stop) + 1e-9


def prepare_bars(history: pd.DataFrame) -> pd.DataFrame:
    """Add the columns the simulation needs to a stock's full adjusted history.

    ``history`` needs open/high/low/close (adjusted) and adj_factor, indexed by date.
    """
    bars = history.copy()
    bars["ema20"] = ema(bars["close"], 20)
    bars["atr14"] = atr(bars, 14)
    return bars


def simulate_plan(bars: pd.DataFrame, signal_date: str, spec: PlanSpec, fees: FeeSchedule, sell_after: int = 2) -> dict[str, Any]:
    """Follow one plan from its signal day and return what happened.

    Args:
        bars: Output of ``prepare_bars`` for the stock (may extend past the trade).
        signal_date: The day the plan was produced (its close is known; buying starts the next day).
        spec: The plan, in the signal day's price terms.
        fees: The cost rules.
        sell_after: Earliest sell day after buying (2 = T+2).

    Returns a dict with ``status`` (not_filled / stop / target_1 / time_stop /
    trail / max_hold / open), prices, dates, costs, tax and net profit.
    """
    stamp = pd.Timestamp(signal_date)
    if stamp not in bars.index:
        return {"status": "no_data", "filled": False}
    position = bars.index.get_loc(stamp)
    if position < 1 or position + 1 >= len(bars):
        return {"status": "open", "filled": False, "pending": True}   # no later day yet
    window = bars.iloc[position - 1: position + 1 + MAX_HOLD_SESSIONS]
    factor = float(bars["adj_factor"].iloc[position]) or 1.0
    data = pd.DataFrame(
        {
            "Open": window["open"] / factor, "High": window["high"] / factor, "Low": window["low"] / factor,
            "Close": window["close"] / factor, "ema20": window["ema20"] / factor, "atr14": window["atr14"] / factor,
        }
    ).ffill()
    if spec.target_1 is None or spec.t1_fraction <= 0 or spec.t1_fraction >= 1:
        first, rest = spec.quantity, 0
    else:
        first = fees.round_lot(spec.quantity * spec.t1_fraction) or spec.quantity
        rest = spec.quantity - first
    reached_end = position + 1 + MAX_HOLD_SESSIONS >= len(bars)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        test = Backtest(data, _PlanStrategy, cash=_CASH, commission=0.0, trade_on_close=False, hedging=False,
                        exclusive_orders=False, finalize_trades=True)
        stats = test.run(spec=spec, first=first, rest=rest, sell_after=sell_after)
    trades = stats["_trades"]
    if trades.empty:
        return {"status": "not_filled", "filled": False}
    strategy = stats["_strategy"]
    still_open = False
    legs = []
    for _, leg in trades.iterrows():
        tag = str(leg["Tag"])
        exit_bar = int(leg["ExitBar"])
        if tag == "t1" and spec.target_1 is not None and float(leg["ExitPrice"]) >= spec.target_1 - 1e-6 and tag not in strategy.reasons \
                and pd.notna(leg.get("TP")):
            reason = "target_1"
        elif tag in strategy.reasons:
            reason = strategy.reasons[tag]
        elif _stop_touched(leg, data, exit_bar):
            reason = "trail_stop" if float(leg["SL"]) > spec.stop + 1e-9 else "stop"
        else:                                           # the library closed it only because the data ended
            reason = "open" if reached_end else "max_hold"
            still_open = still_open or reached_end
            leg = {**leg, "ExitPrice": float(data["Close"].iloc[-1])}
        legs.append({"tag": tag, "quantity": abs(int(leg["Size"])), "exit_price": float(leg["ExitPrice"]),
                     "exit_date": pd.Timestamp(leg["ExitTime"]), "reason": reason})
    entry_price = float(trades["EntryPrice"].iloc[0])
    entry_date = pd.Timestamp(trades["EntryTime"].iloc[0])
    money = settle(fees, entry_price, entry_date, legs)
    t1_leg = next((leg for leg in legs if leg["tag"] == "t1"), legs[0])
    last_leg = max(legs, key=lambda leg: leg["exit_date"])
    status = "open" if still_open else t1_leg["reason"]
    exit_position = bars.index.get_loc(last_leg["exit_date"])
    exit_bar_row = bars.iloc[exit_position]
    return {
        "status": status, "filled": True, "entry_price": entry_price, "entry_date": entry_date.strftime("%Y-%m-%d"),
        "exit_date": last_leg["exit_date"].strftime("%Y-%m-%d"), "final_exit": last_leg["reason"],
        "exit_price": round(sum(leg["exit_price"] * leg["quantity"] for leg in legs) / sum(leg["quantity"] for leg in legs), 2),
        "quantity": sum(leg["quantity"] for leg in legs),
        "sessions_held": int(exit_position - bars.index.get_loc(entry_date)),
        "exit_locked": bool(exit_bar_row["high"] == exit_bar_row["low"]),
        **money,
    }


def settle(fees: FeeSchedule, entry_price: float, entry_date: pd.Timestamp, legs: list[dict[str, Any]]) -> dict[str, float]:
    """Apply commission, SEBON fee, DP charge and capital gains tax to a trade sold in parts."""
    quantity = sum(leg["quantity"] for leg in legs)
    buy_amount = quantity * entry_price
    buy_costs = fees.commission(buy_amount) + fees.sebon_fee(buy_amount)
    costs, tax, gross = buy_costs, 0.0, 0.0
    sell_days: set[pd.Timestamp] = set()
    for leg in legs:
        share = leg["quantity"] / quantity
        sell_amount = leg["quantity"] * leg["exit_price"]
        sell_costs = fees.commission(sell_amount) + fees.sebon_fee(sell_amount)
        if leg["exit_date"] not in sell_days:
            sell_days.add(leg["exit_date"])
            costs += fees.dp_charge
        costs += sell_costs
        leg_gross = sell_amount - leg["quantity"] * entry_price
        gross += leg_gross
        taxable = leg_gross - sell_costs - buy_costs * share
        holding_days = max(0, (leg["exit_date"] - entry_date).days)
        tax += max(0.0, taxable) * fees.cgt_percent(holding_days) / 100.0
    net = gross - costs - tax
    return {
        "position_value": round(buy_amount, 2), "gross_profit": round(gross, 2), "costs": round(costs, 2),
        "tax": round(tax, 2), "net_profit": round(net, 2), "net_percent": round(100.0 * net / buy_amount, 3) if buy_amount else 0.0,
    }
