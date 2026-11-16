"""Stage 4b: one row per stock per past day, with everything the engine knew.

The panel is the raw material for the research in ``study.py``: does the
score predict returns, which parts of it carry signal, which exits work.
It is built by replaying the real engine (see ``replay.py``), so a row for
day D uses only data dated D or earlier.

    python -m nepse_bot.backtest.panel            # every day (about 30 minutes)
    python -m nepse_bot.backtest.panel --sample   # 30 liquid stocks, last 60 days
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd

from nepse_bot.backtest.replay import _HistoricalDatabase
from nepse_bot.db.store import get_database
from nepse_bot.engine import engine as engine_module
from nepse_bot.engine.config import Profile, load_profile
from nepse_bot.engine.engine import Engine, StockResult
from nepse_bot.engine.setups import ALL_SETUPS

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = PROJECT_ROOT / "reports" / "stage4b"
BAR_COLUMNS = ("atr14", "ema20", "ema50", "sma200", "rsi14", "roc5", "roc20", "roc60", "roc120", "turnover_avg20", "high_252", "macd_hist")
FUNDAMENTAL_KEYS = ("pe", "pb", "eps_growth", "roe", "dividend_yield", "value_score")


class PanelEngine(Engine):
    """The live engine with every setup on, recording one row per analysed stock."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        super().__init__()
        self.db = _HistoricalDatabase(self.db)  # type: ignore[assignment]
        self.symbols = set(symbols) if symbols else None
        self.rows: list[dict[str, Any]] = []
        volume = self.db.frame("SELECT symbol, date, volume FROM daily_prices WHERE turnover IS NULL OR turnover <= 0")
        volume["date"] = pd.to_datetime(volume["date"])
        self._volume = {symbol: group.set_index("date")["volume"].astype(float) for symbol, group in volume.groupby("symbol")}

    def _load_prices(self, as_of: str) -> dict[str, pd.DataFrame]:
        frames = super()._load_prices(as_of)
        if self.symbols is not None:
            frames = {symbol: frame for symbol, frame in frames.items() if symbol in self.symbols}
        for symbol, frame in frames.items():
            volume = self._volume.get(symbol)
            missing = frame["turnover"] <= 0
            if volume is not None and missing.any():
                raw = frame["close"] * float(frame["adj_factor"].iloc[-1]) / frame["adj_factor"]
                frame.loc[missing, "turnover"] = (volume.reindex(frame.index) * raw).fillna(0.0)[missing]
        return frames

    def _judge(self, **kwargs: Any) -> StockResult:  # type: ignore[override]
        triggered: dict[str, Any] = {}
        real_build, real_enabled = engine_module.build_plan, engine_module.enabled_setups

        def spy(signal: Any, *args: Any, **kw: Any) -> Any:
            triggered[signal.key] = signal
            return real_build(signal, *args, **kw)

        engine_module.build_plan = spy  # type: ignore[assignment]
        engine_module.enabled_setups = lambda: list(ALL_SETUPS)  # type: ignore[assignment]
        try:
            result = super()._judge(**kwargs)
        finally:
            engine_module.build_plan, engine_module.enabled_setups = real_build, real_enabled  # type: ignore[assignment]
        if result.verdict == "IGNORE":
            return result
        frame, regime, sector_state = kwargs["frame"], kwargs["regime"], kwargs["sector_state"]
        last = frame.iloc[-1]
        row: dict[str, Any] = {
            "date": result.as_of, "symbol": result.symbol, "sector": result.sector, "verdict": result.verdict,
            "score": result.score, "coverage": result.coverage, "rs_rank": result.rs_rank,
            "regime": regime.label, "regime_score": regime.score, "breadth_10d": regime.breadth_10d, "pct_above_ma50": regime.pct_above_ma50,
            "sector_label": sector_state.label if sector_state else None, "sector_score": sector_state.score if sector_state else None,
            "close": float(last["close"]), "adj_factor": float(last["adj_factor"]), "range20": float(frame["range_pct"].tail(20).mean()),
            "flags": "; ".join(result.flags)[:120], "blocked": "; ".join(result.why_not_buy)[:160],
        }
        for name, value in result.sub_scores.items():
            row[f"s_{name}"] = value
        for name in BAR_COLUMNS:
            row[name] = float(last[name]) if pd.notna(last[name]) else None
        fundamentals = kwargs.get("fundamentals") or {}
        for name in FUNDAMENTAL_KEYS:
            row[name] = fundamentals.get(name)
        for setup in ALL_SETUPS:
            signal = triggered.get(setup.key)
            row[f"t_{setup.key}"] = signal is not None
            if signal is not None:
                row[f"inv_{setup.key}"] = signal.invalidation
                row[f"ent_{setup.key}"] = signal.entry_ref
        self.rows.append(row)
        return result


_worker: PanelEngine | None = None
_profile: Profile | None = None


def _start(symbols: list[str] | None, profile: Profile) -> None:
    global _worker, _profile
    logging.getLogger("nepse_bot").setLevel(logging.WARNING)
    _worker, _profile = PanelEngine(symbols), profile


def _run(days: list[str]) -> list[dict[str, Any]]:
    assert _worker is not None and _profile is not None
    for day in days:
        _worker._scan(day, _profile)
    rows, _worker.rows = _worker.rows, []
    return rows


def build_panel(days: list[str], symbols: list[str] | None = None, workers: int = 6) -> pd.DataFrame:
    """Replay ``days`` and return the panel."""
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    chunks = [days[i:i + 10] for i in range(0, len(days), 10)]
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_start, initargs=(symbols, load_profile(get_database()))) as pool:
        for number, chunk_rows in enumerate(pool.map(_run, chunks), 1):
            rows.extend(chunk_rows)
            if number % 20 == 0:
                print(f"  {min(number * 10, len(days))}/{len(days)} days, {len(rows)} rows", flush=True)
    return pd.DataFrame.from_records(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Stage 4b research panel.")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    arguments = parser.parse_args()
    from nepse_bot.backtest.run import WARM_UP_SESSIONS, liquid_sample

    all_days = get_database().trading_dates()
    symbols, days, name = None, all_days[WARM_UP_SESSIONS:], "panel.csv.gz"
    if arguments.sample:
        symbols, days, name = liquid_sample(30, all_days[-1]), all_days[-60:], "panel_sample.csv.gz"
    began = time.time()
    panel = build_panel(days, symbols, arguments.workers)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel.to_csv(OUT_DIR / name, index=False)
    print(f"{len(panel)} rows for {len(days)} days in {(time.time() - began) / 60:.1f} min -> {OUT_DIR / name}")


if __name__ == "__main__":
    main()
