"""Replay the live engine on past trading days and collect every valid plan.

The replay calls the same ``Engine._scan`` the bot uses, one past day at a
time, so a scan for day D sees only data dated D or earlier. For each stock
that triggers a setup it then asks the engine again with only that setup
switched on, once per risk-target rule being compared (e.g. 2x and 1.5x), and
records the verdict and the plan.

Differences from the live bot, all deliberate:

* every setup is evaluated, including ones switched off in config, because the
  backtest is what decides which setups deserve to be on;
* stocks that are delisted or suspended TODAY are analysed on the days they
  still traded (otherwise the test would only see the survivors);
* turnover is missing from the database before February 2023, so for those
  rows it is estimated as volume x close. Nothing is written to the database.
"""

from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Sequence

import pandas as pd

from nepse_bot.db.store import Database
from nepse_bot.engine import engine as engine_module
from nepse_bot.engine.config import Profile, engine_config, load_profile
from nepse_bot.engine.engine import BUY_VERDICTS, Engine, StockResult
from nepse_bot.engine.plan import PlanRejection, TradePlan
from nepse_bot.engine.setups import ALL_SETUPS

logger = logging.getLogger(__name__)

PLAN_FIELDS = (
    "entry", "entry_low", "entry_high", "stop", "stop_percent", "target_1", "target_1_percent", "target_1_reason",
    "target_2", "reward_risk", "quantity", "position_value", "breakeven_stop", "cost_percent", "time_stop_sessions",
    "entry_style", "t1_fraction", "trail", "trail_from", "chandelier_multiple", "max_hold_sessions",
)


class _HistoricalDatabase:
    """Read-only view of the database that hides today's delisted/suspended labels."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        rows = self._db.query(sql, params)
        if sql.lstrip().startswith("SELECT symbol, name, sector, status"):
            for row in rows:
                if row.get("status") in ("delisted", "suspended"):
                    row["status"] = "active"
        return rows


class ReplayEngine(Engine):
    """The live engine, instrumented to report every setup's plan separately."""

    def __init__(self, r_variants: Sequence[float], symbols: Sequence[str] | None = None, db: Database | None = None) -> None:
        super().__init__(db=db)
        self.db = _HistoricalDatabase(self.db)  # type: ignore[assignment]
        self.r_variants = [float(value) for value in r_variants]
        self.symbols = set(symbols) if symbols else None
        self.records: list[dict[str, Any]] = []
        self.rejections: Counter[tuple[str, float, str]] = Counter()
        volume = self.db.frame("SELECT symbol, date, volume FROM daily_prices WHERE turnover IS NULL OR turnover <= 0")
        volume["date"] = pd.to_datetime(volume["date"])
        self._volume = {symbol: group.set_index("date")["volume"].astype(float) for symbol, group in volume.groupby("symbol")}

    # ------------------------------------------------------------------ data
    def _load_prices(self, as_of: str) -> dict[str, pd.DataFrame]:
        frames = super()._load_prices(as_of)
        if self.symbols is not None:
            frames = {symbol: frame for symbol, frame in frames.items() if symbol in self.symbols}
        for symbol, frame in frames.items():
            volume = self._volume.get(symbol)
            if volume is None:
                continue
            missing = frame["turnover"] <= 0
            if missing.any():
                # adjusted close / factor = the raw close of that day
                raw = frame["close"] * float(frame["adj_factor"].iloc[-1]) / frame["adj_factor"]
                estimate = (volume.reindex(frame.index) * raw).fillna(0.0)
                frame.loc[missing, "turnover"] = estimate[missing]
        return frames

    # --------------------------------------------------------------- verdicts
    def _judge(self, **kwargs: Any) -> StockResult:  # type: ignore[override]
        triggered: list[str] = []
        real_build = engine_module.build_plan
        real_enabled = engine_module.enabled_setups
        plan_settings = engine_config().setdefault("plan", {})
        configured_r = plan_settings.get("target1_min_r")

        def spy(signal: Any, *args: Any, **kw: Any) -> TradePlan | PlanRejection:
            if signal.key not in triggered:
                triggered.append(signal.key)
            return real_build(signal, *args, **kw)

        engine_module.build_plan = spy  # type: ignore[assignment]
        engine_module.enabled_setups = lambda: list(ALL_SETUPS)  # type: ignore[assignment]
        try:
            overall = super()._judge(**kwargs)
            if not triggered:
                return overall
            captured: list[TradePlan | PlanRejection] = []

            def capture(signal: Any, *args: Any, **kw: Any) -> TradePlan | PlanRejection:
                outcome = real_build(signal, *args, **kw)
                captured.append(outcome)
                return outcome

            engine_module.build_plan = capture  # type: ignore[assignment]
            for setup in ALL_SETUPS:
                if setup.key not in triggered:
                    continue
                engine_module.enabled_setups = lambda setup=setup: [setup]  # type: ignore[assignment]
                for r_value in self.r_variants:
                    plan_settings["target1_min_r"] = r_value
                    captured.clear()
                    result = super()._judge(**kwargs)
                    outcome = captured[-1] if captured else None
                    if isinstance(outcome, PlanRejection):
                        self.rejections[(setup.key, r_value, outcome.kind)] += 1
                    elif isinstance(outcome, TradePlan):
                        self._record(setup.key, r_value, result, outcome, kwargs)
            return overall
        finally:
            engine_module.build_plan = real_build  # type: ignore[assignment]
            engine_module.enabled_setups = real_enabled  # type: ignore[assignment]
            if configured_r is None:
                plan_settings.pop("target1_min_r", None)
            else:
                plan_settings["target1_min_r"] = configured_r

    def _record(self, setup_key: str, r_value: float, result: StockResult, plan: TradePlan, kwargs: dict[str, Any]) -> None:
        regime = kwargs["regime"]
        row: dict[str, Any] = {
            "date": result.as_of, "symbol": result.symbol, "sector": result.sector, "setup": setup_key, "r_rule": r_value,
            "verdict": result.verdict, "is_buy": result.verdict in BUY_VERDICTS, "score": round(result.score, 1),
            "threshold": regime.buy_threshold, "regime": regime.label, "confidence": result.confidence,
            "close": result.close, "adj_factor": float(kwargs["frame"]["adj_factor"].iloc[-1]),
            "why_not_buy": "; ".join(result.why_not_buy)[:200],
        }
        for name in PLAN_FIELDS:
            row[name] = getattr(plan, name)
        self.records.append(row)


# ------------------------------------------------------------- parallel replay
_worker: ReplayEngine | None = None
_worker_profile: Profile | None = None


def _start_worker(r_variants: Sequence[float], symbols: Sequence[str] | None, profile: Profile) -> None:
    global _worker, _worker_profile
    logging.getLogger("nepse_bot").setLevel(logging.WARNING)
    _worker = ReplayEngine(r_variants, symbols)
    _worker_profile = profile


def _replay_days(days: list[str]) -> tuple[list[dict[str, Any]], dict[tuple[str, float, str], int], list[tuple[Any, ...]]]:
    assert _worker is not None and _worker_profile is not None
    regimes = []
    for day in days:
        scan = _worker._scan(day, _worker_profile)
        regimes.append((day, scan.regime.label, scan.regime.pct_above_ma50))
    records, rejections = _worker.records, dict(_worker.rejections)
    _worker.records, _worker.rejections = [], Counter()
    return records, rejections, regimes


def replay(days: list[str], r_variants: Sequence[float], symbols: Sequence[str] | None = None,
           profile: Profile | None = None, workers: int = 8, progress: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Replay the engine over ``days``.

    Returns (signals, rejections, regimes): one row per valid plan, the count
    of rejected plans by reason, and the market regime on each day.
    """
    profile = profile or load_profile(None)
    chunks = [days[i:i + 10] for i in range(0, len(days), 10)]
    records: list[dict[str, Any]] = []
    rejections: Counter[tuple[str, float, str]] = Counter()
    regimes: list[tuple[Any, ...]] = []
    done = 0
    if workers <= 1:
        _start_worker(r_variants, symbols, profile)
        outputs = map(_replay_days, chunks)
    else:
        pool = ProcessPoolExecutor(max_workers=workers, initializer=_start_worker, initargs=(list(r_variants), symbols, profile))
        outputs = pool.map(_replay_days, chunks)
    for chunk_records, chunk_rejections, chunk_regimes in outputs:
        records.extend(chunk_records)
        rejections.update(chunk_rejections)
        regimes.extend(chunk_regimes)
        done += 1
        if progress and done % 10 == 0:
            print(f"  replayed {min(done * 10, len(days))}/{len(days)} days, {len(records)} plans so far", flush=True)
    if workers > 1:
        pool.shutdown()
    signals = pd.DataFrame.from_records(records)
    rejected = pd.DataFrame([{"setup": k[0], "r_rule": k[1], "reason": k[2], "count": v} for k, v in sorted(rejections.items())])
    return signals, rejected, pd.DataFrame(regimes, columns=["date", "regime", "pct_above_ma50"])
