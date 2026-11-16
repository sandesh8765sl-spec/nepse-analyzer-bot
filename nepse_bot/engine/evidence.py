"""Backtest evidence for each setup, shown next to every candidate.

``config/evidence.yaml`` holds each setup's real backtested results after
commission, fees and tax (written from ``python -m nepse_bot.backtest``).
A setup is "proven" only if it made money in BOTH the older tuning period and
the most recent out-of-sample period, with enough trades in each to mean
something. Candidates from a proven setup can be BUY or STRONG BUY; anything
else is labelled SPECULATIVE and carries its numbers and a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from nepse_bot.engine.config import setting
from nepse_bot.settings import CONFIG_DIR, load_yaml

EVIDENCE_PATH = CONFIG_DIR / "evidence.yaml"


@dataclass(frozen=True)
class Record:
    """Results of one setup in one period, after all costs and tax."""

    period: str
    trades: int
    win_rate: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    expectancy_pct: float | None        # average net result per trade, % of the position
    profit_factor: float | None         # money won / money lost

    def passes(self) -> bool:
        minimum_trades = int(setting("backtest", "gate_min_trades", default=50))
        minimum_factor = float(setting("backtest", "gate_min_profit_factor", default=1.2))
        return self.trades >= minimum_trades and self.profit_factor is not None and self.profit_factor >= minimum_factor

    def line(self) -> str:
        if not self.trades or self.win_rate is None:
            return f"{self.period}: no trades"
        return (
            f"{self.period}: {self.trades} trades, {self.win_rate:.0f}% won, average win +{self.avg_win_pct:.1f}%, "
            f"average loss {self.avg_loss_pct:.1f}%, average result {self.expectancy_pct:+.1f}% per trade"
        )


@dataclass(frozen=True)
class Evidence:
    """What the backtest says about one setup."""

    setup: str
    recent: Record | None       # out-of-sample: the most recent years, never used for tuning
    earlier: Record | None      # the tuning period

    @property
    def proven(self) -> bool:
        return bool(self.recent and self.earlier and self.recent.passes() and self.earlier.passes())

    @property
    def grade(self) -> str:
        """proven / mixed (made money in one period only) / unproven."""
        if self.proven:
            return "proven"
        gains = [r for r in (self.recent, self.earlier) if r and r.expectancy_pct is not None and r.expectancy_pct > 0]
        return "mixed" if gains else "unproven"

    @property
    def priority(self) -> float:
        """Used to choose between two setups on the same stock: better recent result first."""
        value = self.recent.expectancy_pct if self.recent and self.recent.expectancy_pct is not None else -99.0
        return (100.0 if self.proven else 0.0) + value

    def warning(self) -> str:
        if self.proven:
            return "Backtest: this setup made money after costs in both test periods."
        if self.recent is None or not self.recent.trades:
            return "⚠️ SPECULATIVE: this setup has no usable backtest record yet."
        if self.grade == "mixed":
            recent_lost = self.recent.expectancy_pct is not None and self.recent.expectancy_pct <= 0
            if recent_lost:
                return ("⚠️ SPECULATIVE: in backtests this setup made money in the earlier years but LOST money after costs "
                        "in the most recent period. Not proven: an idea to research, not a signal.")
            return "⚠️ SPECULATIVE: in backtests this setup made money recently but not in the earlier years. Not proven."
        return "⚠️ SPECULATIVE: in backtests this setup LOST money after costs. Treat it as an idea to research, not a signal."

    def lines(self) -> list[str]:
        return [record.line() for record in (self.recent, self.earlier) if record is not None]


def _record(data: dict[str, Any] | None) -> Record | None:
    if not data:
        return None

    def number(key: str) -> float | None:
        value = data.get(key)
        return float(value) if value is not None else None

    return Record(
        period=str(data.get("period", "")), trades=int(data.get("trades") or 0), win_rate=number("win_rate"),
        avg_win_pct=number("avg_win_pct"), avg_loss_pct=number("avg_loss_pct"), expectancy_pct=number("expectancy_pct"),
        profit_factor=number("profit_factor"),
    )


def load_file(path: Any) -> dict[str, Evidence]:
    """Read an evidence file (config/evidence.yaml, or the result of a new backtest)."""
    try:
        data = load_yaml(path)
    except (FileNotFoundError, ValueError):
        return {}
    return {
        key: Evidence(setup=key, recent=_record(value.get("recent")), earlier=_record(value.get("earlier")))
        for key, value in (data.get("setups") or {}).items()
    }


@lru_cache(maxsize=1)
def _all() -> dict[str, Evidence]:
    return load_file(EVIDENCE_PATH)


def for_setup(key: str) -> Evidence:
    """The evidence for a setup (an empty, unproven record if it was never backtested)."""
    return _all().get(key) or Evidence(setup=key, recent=None, earlier=None)


def any_proven() -> bool:
    """True if at least one setup passed the gate."""
    return any(item.proven for item in _all().values())


def summary_note() -> str:
    """One honest line for the top of the ranking reports."""
    if any_proven():
        return "BUY / STRONG BUY come only from setups that made money in backtests. SPECULATIVE ones did not: see each record."
    return (
        "Honest note: in backtests NONE of the bot's setups made money after costs over the last 2 years. "
        "Use this as a ranked research list, not as buy signals."
    )
