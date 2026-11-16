"""The one analysis engine. Every command, report and alert goes through it.

It reads ONLY the database and uses ONLY completed daily bars. A scan for a
given date uses nothing published after that date, so the same code can be
replayed on history in Stage 4 (backtesting).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from nepse_bot.db.store import Database, get_database, utc_now_text
from nepse_bot.engine import evidence as evidence_module
from nepse_bot.engine import scoring
from nepse_bot.engine.evidence import Evidence
from nepse_bot.engine.config import Profile, load_profile, set_disabled_setups, setting
from nepse_bot.engine.indicators import add_indicators, weekly_trend
from nepse_bot.engine.market import SECTOR_INDEX, Regime, SectorState, classify_regime, classify_sectors
from nepse_bot.engine.plan import PlanRejection, TradePlan, build_plan, resistance_rank_factor, resistance_warning
from nepse_bot.engine.setups import StockContext, enabled_setups
from nepse_bot.ingest.adjust import limit_for, price_limits
from nepse_bot.portfolio.fees import FeeSchedule, fee_schedule
from nepse_bot.trading_calendar import TradingCalendar, get_calendar

logger = logging.getLogger(__name__)

LENDER_SECTORS = {"Commercial Banks", "Development Banks", "Finance", "Microfinance"}
HISTORY_CALENDAR_DAYS = 640     # enough for a 200-day average plus a 52-week high
BUY_VERDICTS = ("STRONG BUY", "BUY", "SPECULATIVE")   # every verdict that comes with a trade plan
VERDICT_ORDER = {"STRONG BUY": 0, "BUY": 1, "SPECULATIVE": 2}


def has_broker_buying(net_share: float | None) -> bool:
    """The one "broker buying" rule: the top 5 brokers net bought at least broker_filter.min_top5_net_share % of the volume."""
    return net_share is not None and net_share >= float(setting("broker_filter", "min_top5_net_share", default=15))


@dataclass
class StockResult:
    """The engine's full conclusion about one stock on one day."""

    symbol: str
    name: str
    sector: str
    as_of: str
    close: float
    verdict: str                              # STRONG BUY / BUY / SPECULATIVE / WATCH / AVOID / IGNORE
    score: float = 0.0
    confidence: str = "Low"
    coverage: float = 0.0
    sub_scores: dict[str, float] = field(default_factory=dict)
    setup_name: str | None = None
    plan: TradePlan | None = None
    watch_trigger: str | None = None
    why_not_buy: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # information only: shown as warnings, never change the grade
    fundamentals: dict[str, Any] = field(default_factory=dict)
    sector_state: SectorState | None = None
    rs_rank: float | None = None
    change_percent: float | None = None
    evidence: Evidence | None = None          # the setup's backtest record
    trigger_level: float | None = None        # WATCH: the price a close must clear
    trigger_setup: str | None = None          # ... and the setup key it belongs to
    blocked_plan: TradePlan | None = None     # WATCH: the plan that was held back (no upside, low score ...)
    broker_net_share: float | None = None     # top 5 brokers' net buying, % of the volume over 10 days (None = no broker data)

    @property
    def is_buy(self) -> bool:
        return self.verdict in BUY_VERDICTS

    @property
    def rank_value(self) -> float:
        """Score x confidence, used to order the Top 10."""
        return self.score * {"High": 1.0, "Medium": 0.92, "Low": 0.8}.get(self.confidence, 0.8) * resistance_rank_factor(self.plan)


@dataclass
class ScanResult:
    """One full scan of the market."""

    as_of: str
    regime: Regime
    sectors: dict[str, SectorState]
    results: dict[str, StockResult]
    profile: Profile
    stale: bool = False
    last_completed_trading_day: str | None = None
    generated_at: datetime | None = None

    def by_verdict(self, *verdicts: str) -> list[StockResult]:
        return sorted((r for r in self.results.values() if r.verdict in verdicts), key=lambda r: r.rank_value, reverse=True)

    @property
    def buys(self) -> list[StockResult]:
        """Every candidate with a plan: STRONG BUY first, then BUY, then SPECULATIVE; best score first within each."""
        return sorted(self.by_verdict(*BUY_VERDICTS), key=lambda r: VERDICT_ORDER.get(r.verdict, 9))

    @property
    def watches(self) -> list[StockResult]:
        return [item for item in self.by_verdict("WATCH") if item.watch_trigger]


def _percentile_ranks(values: dict[str, float | None], higher_is_better: bool = True) -> dict[str, float]:
    """Rank the known values 0-100 (100 = best). Unknown values are left out."""
    known = pd.Series({key: value for key, value in values.items() if value is not None and pd.notna(value)}, dtype=float)
    if known.empty:
        return {}
    ranks = known.rank(pct=True, ascending=higher_is_better) * 100.0
    if len(known) == 1:
        ranks[:] = 50.0
    return {key: round(float(value), 1) for key, value in ranks.items()}


class Engine:
    """Scan the market from the database and explain every conclusion."""

    def __init__(self, db: Database | None = None, calendar: TradingCalendar | None = None, fees: FeeSchedule | None = None) -> None:
        self.db = db or get_database()
        self.calendar = calendar or get_calendar()
        self.fees = fees or fee_schedule()
        self._cache: dict[tuple[Any, ...], ScanResult] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def scan(self, as_of: str | None = None, profile: Profile | None = None, use_cache: bool = True) -> ScanResult:
        """Analyse every stock as of a date (default: the newest date in the database)."""
        as_of = as_of or self.db.last_price_date()
        if as_of is None:
            raise RuntimeError("The database has no prices yet. Run: python -m nepse_bot.ingest backfill")
        profile = profile or load_profile(self.db)
        key = (as_of, profile, self.db.get_setting("last_ingest_run"))
        with self._lock:
            if use_cache and key in self._cache:
                return self._cache[key]
            result = self._scan(as_of, profile)
            self._cache = {key: result}
            return result

    def record_signals(self, scan: ScanResult) -> int:
        """Log every BUY signal with its full plan (the live journal used from Stage 4)."""
        rows = []
        for item in scan.buys:
            plan = item.plan
            if plan is None:
                continue
            rows.append(
                {
                    "id": f"{scan.as_of}:{item.symbol}:{plan.setup_key}",
                    "symbol": item.symbol,
                    "created_at": utc_now_text(),
                    "setup": plan.setup_key,
                    "verdict": item.verdict,
                    "score": item.score,
                    "entry": plan.entry,
                    "stop_loss": plan.stop,
                    "target_1": plan.target_1,
                    "target_2": plan.target_2,
                    "status": "open",
                    "plan_json": json.dumps({"as_of": scan.as_of, "regime": scan.regime.label, "confidence": item.confidence, **plan.__dict__}, default=str),
                }
            )
        if rows:
            with self.db.transaction() as connection:
                connection.executemany(
                    "INSERT INTO signals (id, symbol, created_at, setup, verdict, score, entry, stop_loss, target_1, target_2, status, plan_json) "
                    "VALUES (:id, :symbol, :created_at, :setup, :verdict, :score, :entry, :stop_loss, :target_1, :target_2, :status, :plan_json) "
                    "ON CONFLICT (id) DO NOTHING",
                    rows,
                )
        return len(rows)

    # ------------------------------------------------------------ data loading
    def _load_prices(self, as_of: str) -> dict[str, pd.DataFrame]:
        start = (date.fromisoformat(as_of) - timedelta(days=HISTORY_CALENDAR_DAYS)).isoformat()
        extra = []
        if setting("universe", "include_mutual_funds", default=False):
            extra.append("s.instrument_type = 'mutual_fund'")
        types = " OR ".join(["s.instrument_type = 'equity'", *extra])
        promoter = "" if setting("universe", "include_promoter_shares", default=False) else "AND s.is_promoter = 0"
        frame = self.db.frame(
            "SELECT p.symbol, p.date, COALESCE(p.adj_open, p.open) AS open, COALESCE(p.adj_high, p.high) AS high, "
            "COALESCE(p.adj_low, p.low) AS low, COALESCE(p.adj_close, p.close) AS close, p.close AS raw_close, "
            "COALESCE(p.turnover, 0) AS turnover, COALESCE(p.adj_factor, 1) AS adj_factor "
            f"FROM daily_prices p JOIN securities s ON s.symbol = p.symbol WHERE ({types}) {promoter} "
            "AND p.date >= ? AND p.date <= ? ORDER BY p.symbol, p.date",
            (start, as_of),
        )
        if frame.empty:
            return {}
        # Re-base so the newest bar is unadjusted: all levels are then in today's prices.
        frame["date"] = pd.to_datetime(frame["date"])
        frames: dict[str, pd.DataFrame] = {}
        for symbol, group in frame.groupby("symbol", sort=False):
            group = group.set_index("date").drop(columns="symbol")
            last_factor = float(group["adj_factor"].iloc[-1]) or 1.0
            if last_factor != 1.0:
                group[["open", "high", "low", "close"]] = group[["open", "high", "low", "close"]] / last_factor
            frames[symbol] = group
        return frames

    def _load_fundamentals(self, as_of: str, closes: dict[str, float], sectors: dict[str, str]) -> dict[str, dict[str, Any]]:
        max_age = int(setting("scoring", "fundamentals_max_age_days", default=460))
        oldest = (date.fromisoformat(as_of) - timedelta(days=max_age)).isoformat()
        reports: dict[str, list[dict[str, Any]]] = {}
        for row in self.db.query(
            "SELECT symbol, fiscal_year, quarter, period_end_date, publication_date, eps, eps_annualized, book_value, roe, npl, car "
            "FROM fundamentals WHERE publication_date <= ? ORDER BY symbol, period_end_date DESC",
            (as_of,),
        ):
            reports.setdefault(row["symbol"], []).append(row)
        dividends: dict[str, float] = {}
        for row in self.db.query(
            "SELECT symbol, amount, announcement_date FROM corporate_actions WHERE action_type = 'cash_dividend' "
            "AND announcement_date <= ? AND announcement_date >= ? ORDER BY announcement_date",
            (as_of, (date.fromisoformat(as_of) - timedelta(days=400)).isoformat()),
        ):
            dividends[row["symbol"]] = float(row["amount"] or 0.0)  # newest wins

        newest_period = max((rows[0]["period_end_date"] or "" for rows in reports.values() if rows), default="")
        raw: dict[str, dict[str, Any]] = {}
        for symbol, close in closes.items():
            rows = reports.get(symbol) or []
            latest = rows[0] if rows and (rows[0]["period_end_date"] or "") >= oldest else None
            if latest is None:
                continue
            eps = latest["eps_annualized"] if latest["eps_annualized"] is not None else latest["eps"]
            previous = next(
                (r for r in rows[1:] if r["quarter"] == latest["quarter"] and r["fiscal_year"] < latest["fiscal_year"]), None
            )
            previous_eps = (previous["eps_annualized"] if previous and previous["eps_annualized"] is not None else previous["eps"]) if previous else None
            growth = (eps - previous_eps) / abs(previous_eps) * 100.0 if eps is not None and previous_eps not in (None, 0) else None
            book = latest["book_value"]
            raw[symbol] = {
                "fiscal_year": latest["fiscal_year"],
                "quarter": latest["quarter"],
                "publication_date": latest["publication_date"],
                "behind_newest_quarter": bool(newest_period and (latest["period_end_date"] or "") < newest_period),
                "eps": eps,
                "book_value": book,
                "pe": close / eps if eps and eps > 0 else None,
                "negative_earnings": bool(eps is not None and eps <= 0),
                "pb": close / book if book and book > 0 else None,
                "eps_growth": growth,
                "roe": latest["roe"],
                "npl": latest["npl"],
                "car": latest["car"],
                "dividend_yield": dividends[symbol] / close * 100.0 if symbol in dividends and close > 0 else None,
            }

        # Sector-relative grades (0-100, 100 = best in sector). Never absolute cut-offs.
        # A sector with too few companies that report a number, or a mixed-bag sector such as
        # "Others" (a telecom next to a cable maker), is compared with the whole market instead.
        min_peers = int(setting("scoring", "min_sector_peers", default=5))
        mixed = {str(name) for name in setting("scoring", "market_median_sectors", default=["Others"]) or []}
        by_sector: dict[str, list[str]] = {}
        for symbol in raw:
            by_sector.setdefault(sectors.get(symbol, ""), []).append(symbol)
        metrics = {"pe": False, "pb": False, "eps_growth": True, "roe": True, "dividend_yield": True, "npl": False, "car": True}
        for sector, members in by_sector.items():
            for metric, higher_is_better in metrics.items():
                comparable = sum(1 for s in members if raw[s][metric] is not None and not (metric == "pe" and raw[s]["negative_earnings"]))
                own = sector not in mixed and comparable >= min_peers
                peers = members if own else list(raw)
                if metric == "pe":
                    for symbol in members:
                        raw[symbol]["sector_label"] = sector if own else "the market"
                        raw[symbol]["median_scope"] = "sector" if own else "market"
                ranks = _percentile_ranks({s: raw[s][metric] for s in peers}, higher_is_better)
                known = [raw[s][metric] for s in peers if raw[s][metric] is not None]
                median = float(pd.Series(known).median()) if known else None
                for symbol in members:
                    view = raw[symbol]
                    view.setdefault("grades", {})[metric] = ranks.get(symbol)
                    view[f"{metric}_median"] = median
            for symbol in members:
                view = raw[symbol]
                if view["negative_earnings"]:
                    view["grades"]["pe"] = 0.0     # losing money is the worst valuation grade
                    view["pe"] = view["pe"] or 0.0
                    view["pe_median"] = view.get("pe_median") or 0.0
                view["is_lender"] = sector in LENDER_SECTORS
                value_parts = [view["grades"].get(k) for k in ("pe", "pb") if view["grades"].get(k) is not None]
                view["value_score"] = sum(value_parts) / len(value_parts) if value_parts else None
        return raw

    def _load_broker_flow(self, as_of: str) -> dict[str, dict[str, Any]]:
        dates = [row["date"] for row in self.db.query("SELECT DISTINCT date FROM broker_flow_daily WHERE date <= ? ORDER BY date DESC LIMIT 20", (as_of,))]
        if not dates:
            return {}
        flow = self.db.frame(
            f"SELECT date, symbol, broker, buy_qty, sell_qty FROM broker_flow_daily WHERE date IN ({','.join('?' for _ in dates)})", dates
        )
        last5, last10 = set(dates[:5]), set(dates[:10])
        summary: dict[str, dict[str, Any]] = {}
        for symbol, group in flow.groupby("symbol"):
            five, ten = group[group["date"].isin(last5)], group[group["date"].isin(last10)]
            buys5 = five.groupby("broker")["buy_qty"].sum()
            net10 = (ten.groupby("broker")["buy_qty"].sum() - ten.groupby("broker")["sell_qty"].sum()).sort_values(ascending=False)
            volume10 = float(ten["buy_qty"].sum())
            top = net10.head(5)
            first_half, second_half = group[group["date"].isin(set(dates[10:]))], ten
            summary[symbol] = {
                "days": int(group["date"].nunique()),
                "top_buyer_share_5d": float(buys5.max() / buys5.sum() * 100.0) if buys5.sum() > 0 else None,
                "top5_net_share_10d": float(top.sum() / volume10 * 100.0) if volume10 > 0 else None,
                "top_buyers_text": " (brokers " + ", ".join(str(int(b)) for b in top.index[:2]) + ")" if len(top) else "",
                "buyers_recent": int(second_half[second_half["buy_qty"] > 0]["broker"].nunique()),
                "buyers_before": int(first_half[first_half["buy_qty"] > 0]["broker"].nunique()) if len(first_half) else None,
            }
        return summary

    def _upcoming_book_closures(self, as_of: str) -> dict[str, tuple[int, float | None]]:
        """Return symbol -> (trading days until book closure, pending bonus %)."""
        start = date.fromisoformat(as_of)
        horizon = (start + timedelta(days=45)).isoformat()
        result: dict[str, tuple[int, float | None]] = {}
        for row in self.db.query(
            "SELECT symbol, action_type, ratio, book_closure_date FROM corporate_actions "
            "WHERE book_closure_date > ? AND book_closure_date <= ? ORDER BY book_closure_date DESC",
            (as_of, horizon),
        ):
            closure = date.fromisoformat(row["book_closure_date"])
            days, cursor = 0, start
            while cursor < closure:
                cursor += timedelta(days=1)
                if self.calendar.is_trading_day(cursor):
                    days += 1
            bonus = float(row["ratio"]) if row["action_type"] == "bonus" and row["ratio"] else None
            previous = result.get(row["symbol"])
            result[row["symbol"]] = (days, bonus if bonus is not None else (previous[1] if previous else None))
        return result

    def _add_unlock_notes(self, results: dict[str, StockResult], as_of: str) -> None:
        """Warn about locked promoter / mutual fund shares that become sellable soon. Information only."""
        from nepse_bot.events import unlocks

        try:
            flagged = unlocks.flagged_by_symbol(self.db, as_of, self.calendar)
        except Exception:  # the table is new; a scan must never fail because of it
            logger.exception("Could not read lock-in expiries.")
            return
        for symbol, items in flagged.items():
            if symbol in results:
                results[symbol].notes.extend(item.short() for item in items)

    # ---------------------------------------------------------------- the scan
    def _scan(self, as_of: str, profile: Profile) -> ScanResult:
        # Setups the live journal switched off stay off, whatever config/engine.yaml says.
        set_disabled_setups(
            row["key"].split(".", 1)[1]
            for row in self.db.query("SELECT key FROM settings WHERE key LIKE 'setup_disabled.%' AND value IS NOT NULL AND value != ''")
        )
        frames = self._load_prices(as_of)
        securities = {row["symbol"]: row for row in self.db.query("SELECT symbol, name, sector, status, instrument_type FROM securities")}
        listed_days = {row["symbol"]: int(row["n"]) for row in self.db.query("SELECT symbol, COUNT(*) AS n FROM daily_prices WHERE date <= ? GROUP BY symbol", (as_of,))}
        market_dates = [row["date"] for row in self.db.query("SELECT DISTINCT date FROM daily_prices WHERE date <= ? ORDER BY date DESC LIMIT 60", (as_of,))][::-1]

        # ---- market and sectors
        index_rows = self.db.frame("SELECT index_code, date, close FROM index_daily WHERE date <= ? AND date >= ? ORDER BY date",
                                   (as_of, (date.fromisoformat(as_of) - timedelta(days=HISTORY_CALENDAR_DAYS)).isoformat()))
        indices = {code: group.set_index("date")["close"].astype(float) for code, group in index_rows.groupby("index_code")}
        breadth = self.db.frame("SELECT * FROM market_breadth_daily WHERE date <= ? ORDER BY date DESC LIMIT 60", (as_of,)).iloc[::-1]
        market = indices.get("NEPSE", pd.Series(dtype=float))
        regime = classify_regime(market, breadth, as_of)
        sector_states = classify_sectors(indices, market)

        def index_return(code: str, days: int) -> float | None:
            series = indices.get(code)
            return float(series.iloc[-1] / series.iloc[-1 - days] - 1.0) * 100.0 if series is not None and len(series) > days else None

        # ---- indicators for every stock that traded on the scan date
        prepared: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            if frame.index[-1].strftime("%Y-%m-%d") != as_of and symbol in securities and securities[symbol]["status"] == "active":
                # Did not trade on the scan date: use its last bar, liquidity rules decide the rest.
                pass
            if len(frame) >= 30:
                prepared[symbol] = add_indicators(frame)

        closes = {symbol: float(frame["raw_close"].iloc[-1]) for symbol, frame in prepared.items()}
        sectors = {symbol: (securities.get(symbol) or {}).get("sector") or "" for symbol in prepared}
        fundamentals = self._load_fundamentals(as_of, closes, sectors)
        broker = self._load_broker_flow(as_of)
        closures = self._upcoming_book_closures(as_of)

        # ---- market-wide ranks (only stocks that pass the liquidity rules take part)
        universe_status = {symbol: self._universe_check(symbol, frame, securities.get(symbol) or {}, listed_days.get(symbol, 0), market_dates) for symbol, frame in prepared.items()}
        eligible = [symbol for symbol, reasons in universe_status.items() if not reasons]

        def last(symbol: str, column: str) -> float | None:
            value = prepared[symbol][column].iloc[-1]
            return float(value) if pd.notna(value) else None

        composite = {}
        for symbol in eligible:
            parts = [(last(symbol, "roc20"), 0.3), (last(symbol, "roc60"), 0.4), (last(symbol, "roc120"), 0.3)]
            known = [(value, weight) for value, weight in parts if value is not None]
            composite[symbol] = sum(value * weight for value, weight in known) / sum(weight for _, weight in known) if known else None
        rs_ranks = _percentile_ranks(composite)
        roc20_ranks = _percentile_ranks({s: last(s, "roc20") for s in eligible})
        roc60_ranks = _percentile_ranks({s: last(s, "roc60") for s in eligible})
        market_60 = index_return("NEPSE", 60)

        last_completed = self.calendar.last_completed_trading_day().isoformat()
        # Staleness only matters for a live scan of the newest data, not for replaying history.
        stale = as_of == self.db.last_price_date() and as_of < last_completed
        limits = price_limits()
        results: dict[str, StockResult] = {}
        for symbol, frame in prepared.items():
            info = securities.get(symbol) or {}
            if info.get("status") == "delisted":
                continue
            results[symbol] = self._judge(
                symbol=symbol, info=info, frame=frame, as_of=as_of, regime=regime,
                sector_state=sector_states.get(sectors[symbol]), ignore_reasons=universe_status[symbol],
                fundamentals=fundamentals.get(symbol), broker=broker.get(symbol), closure=closures.get(symbol),
                rs_rank=rs_ranks.get(symbol), roc20_rank=roc20_ranks.get(symbol), roc60_rank=roc60_ranks.get(symbol),
                market_60=market_60, sector_60=index_return(SECTOR_INDEX.get(sectors[symbol], ""), 60),
                profile=profile, stale=stale, limits=limits,
            )
        self._add_unlock_notes(results, as_of)
        logger.info(
            "Engine scan for %s: %s stocks, regime %s (%.0f), %s buy, %s watch.",
            as_of, len(results), regime.label, regime.score,
            sum(1 for r in results.values() if r.is_buy), sum(1 for r in results.values() if r.verdict == "WATCH"),
        )
        return ScanResult(
            as_of=as_of, regime=regime, sectors=sector_states, results=results, profile=profile, stale=stale,
            last_completed_trading_day=last_completed, generated_at=self.calendar.now(),
        )

    # ------------------------------------------------------- universe and flags
    def _universe_check(self, symbol: str, frame: pd.DataFrame, info: dict[str, Any], listed_days: int, market_dates: list[str]) -> list[str]:
        """Return the reasons a stock is left out of the universe (empty = it is in)."""
        reasons = []
        if info.get("status") == "suspended":
            reasons.append("trading is suspended")
        minimum_days = int(setting("universe", "min_listed_trading_days", default=120))
        if listed_days < minimum_days:
            reasons.append(f"listed only {listed_days} trading days ago (need {minimum_days})")
        by_date = frame["turnover"].copy()
        by_date.index = by_date.index.strftime("%Y-%m-%d")
        window = by_date.reindex(market_dates)
        average_turnover = float(window.tail(20).fillna(0.0).mean()) if len(window) else 0.0
        traded = 100.0 * float(window.notna().sum()) / len(market_dates) if market_dates else 0.0
        minimum_turnover = float(setting("universe", "min_avg_turnover_20", default=5_000_000))
        if average_turnover < minimum_turnover:
            reasons.append(f"illiquid: trades Rs {average_turnover / 100000:.0f} lakh a day on average (need {minimum_turnover / 100000:.0f} lakh)")
        minimum_traded = float(setting("universe", "min_traded_days_pct_60", default=80))
        if traded < minimum_traded:
            reasons.append(f"traded on only {traded:.0f}% of the last 60 days")
        return reasons

    def _operator_risk(self, frame: pd.DataFrame, broker: dict[str, Any] | None, limits: list[tuple[str, float]]) -> str | None:
        """Return a description if the trading pattern looks manipulated."""
        cfg = lambda key, default: float(setting("universe", "operator_risk", key, default=default))  # noqa: E731
        roc5 = frame["roc5"].iloc[-1]
        enough = bool(broker) and broker.get("days", 0) >= cfg("min_broker_days", 3)
        if enough and pd.notna(roc5) and roc5 > cfg("price_gain_5d_percent", 20):
            share = broker.get("top_buyer_share_5d")
            if share is not None and share > cfg("broker_share_percent", 40):
                return f"one broker made {share:.0f}% of the buying while the price rose {roc5:.0f}% in 5 days"
        recent = frame.tail(10)
        change = recent["close"].pct_change() * 100.0
        at_limit = 0
        for moment, value in change.items():
            if pd.notna(value) and value >= limit_for(moment.strftime("%Y-%m-%d"), limits) - 0.6 and recent.loc[moment, "turnover"] < cfg("thin_turnover", 2_500_000):
                at_limit += 1
        if at_limit >= cfg("upper_limit_days_in_10", 3):
            return f"{at_limit} upper-limit days in the last 10 on thin volume"
        roc20 = frame["roc20"].iloc[-1]
        if enough and pd.notna(roc20) and roc20 > cfg("gain_20d_percent", 50):
            before, now = broker.get("buyers_before"), broker.get("buyers_recent")
            if before and now is not None and now < before * 0.7:
                return f"up {roc20:.0f}% in 20 days while the number of buying brokers fell from {before} to {now}"
        return None

    # ---------------------------------------------------------------- verdicts
    def _judge(self, *, symbol: str, info: dict[str, Any], frame: pd.DataFrame, as_of: str, regime: Regime,
               sector_state: SectorState | None, ignore_reasons: list[str], fundamentals: dict[str, Any] | None,
               broker: dict[str, Any] | None, closure: tuple[int, float | None] | None, rs_rank: float | None,
               roc20_rank: float | None, roc60_rank: float | None, market_60: float | None, sector_60: float | None,
               profile: Profile, stale: bool, limits: list[tuple[str, float]]) -> StockResult:
        close = float(frame["raw_close"].iloc[-1])
        change = float(frame["close"].pct_change().iloc[-1] * 100.0) if len(frame) > 1 else None
        result = StockResult(
            symbol=symbol, name=info.get("name") or symbol, sector=info.get("sector") or "Unknown", as_of=as_of,
            close=close, verdict="IGNORE", sector_state=sector_state, rs_rank=rs_rank,
            change_percent=change, fundamentals=fundamentals or {},
        )
        last_bar = frame.index[-1].strftime("%Y-%m-%d")
        if last_bar != as_of:
            ignore_reasons = [*ignore_reasons, f"did not trade on {as_of} (last traded {last_bar})"]
        if ignore_reasons:
            result.why_not_buy = ignore_reasons
            result.negatives = ignore_reasons[:3]
            return result

        ctx = StockContext(
            symbol=symbol, frame=frame, weekly=weekly_trend(frame), rs_rank=rs_rank,
            value_score=(fundamentals or {}).get("value_score"), eps_growth_yoy=(fundamentals or {}).get("eps_growth"),
            broker=broker, book_closure_in_days=closure[0] if closure else None,
            bonus_percent_pending=closure[1] if closure else None,
        )

        if broker and broker.get("days", 0) >= 10:
            result.broker_net_share = broker.get("top5_net_share_10d")

        # ---- setups and plans
        best_plan: TradePlan | None = None
        best_setup: str | None = None
        triggers: list[tuple[str, float | None, str]] = []
        rejections: list[str] = []
        for setup in enabled_setups():
            outcome = setup.evaluate(ctx)
            if outcome is None:
                continue
            if outcome.signal is not None:
                plan = build_plan(outcome.signal, frame, profile, self.fees)
                if isinstance(plan, PlanRejection):
                    rejections.append(f"{outcome.name}: {plan.reason}")
                    continue
                if best_plan is None or evidence_module.for_setup(plan.setup_key).priority > evidence_module.for_setup(best_plan.setup_key).priority:
                    best_plan, best_setup = plan, outcome.name
                    result.positives = list(outcome.signal.why)
            elif outcome.watch:
                triggers.append((outcome.watch, outcome.trigger, outcome.key))

        # ---- score
        stock_60 = float(frame["roc60"].iloc[-1]) if pd.notna(frame["roc60"].iloc[-1]) else None
        subs = {
            "trend": scoring.score_trend(ctx),
            "momentum": scoring.score_momentum(ctx, roc20_rank, roc60_rank),
            "relative_strength": scoring.score_relative_strength(
                rs_rank,
                stock_60 - market_60 if stock_60 is not None and market_60 is not None else None,
                stock_60 - sector_60 if stock_60 is not None and sector_60 is not None else None,
            ),
            "volume_flow": scoring.score_volume_flow(ctx),
            "fundamentals": scoring.score_fundamentals(fundamentals),
            "setup_quality": scoring.score_setup_quality(best_plan, ctx),
        }
        result.score, result.coverage, result.confidence = scoring.combine(subs)
        result.sub_scores = {name: sub.score for name, sub in subs.items()}
        positives, negatives = scoring.top_reasons(subs)
        result.positives = (result.positives + [p for p in positives if p not in result.positives])[:3]
        result.negatives = negatives

        # ---- flags that block a buy
        blockers: list[str] = []
        operator = self._operator_risk(frame, broker, limits)
        if operator:
            result.flags.append(f"OPERATOR RISK: {operator}")
        overextended = scoring.is_overextended(ctx)
        if overextended:
            blockers.append(f"overextended ({overextended}); wait for a pullback")
        block_days = int(setting("universe", "book_closure_block_days", default=5))
        if closure and closure[0] <= block_days and best_setup != "Event run-up":
            result.flags.append(f"Book closure in {closure[0]} trading day(s)")
            blockers.append(f"book closure in {closure[0]} trading day(s): the price will adjust")
        if stale:
            blockers.append("the database is behind the last completed trading day")
        # The reference target is information only. This filter is OFF (plan.min_reward_risk: 0); a close
        # resistance is shown as a warning and ranks the stock slightly lower instead.
        min_rr = float(setting("plan", "min_reward_risk", default=1.5))
        no_upside = min_rr > 0 and best_plan is not None and best_plan.reward_risk < min_rr
        if no_upside:
            blockers.append(
                f"little room: the reference target {best_plan.target_1:,.0f} is only +{best_plan.target_1_percent:.1f}% above the entry, "
                f"against a stop {best_plan.stop_percent:.1f}% below (needs {min_rr:g}x the stop distance)"
            )

        # ---- verdict
        trend = subs["trend"].score
        fundamentals_score = subs["fundamentals"].score
        threshold = regime.buy_threshold
        if operator:
            result.verdict = "AVOID"
            result.why_not_buy = [f"operator risk: {operator}"]
        elif trend < float(setting("verdicts", "avoid_trend_below", default=35)):
            result.verdict = "AVOID"
            result.why_not_buy = ["downtrend"]
        elif fundamentals_score < float(setting("verdicts", "weak_fundamentals_below", default=35)) and trend < 50 and subs["fundamentals"].coverage > 0:
            result.verdict = "AVOID"
            result.why_not_buy = ["weak fundamentals and a weak trend"]
        elif result.score < float(setting("verdicts", "avoid_below_score", default=45)):
            result.verdict = "AVOID"
            result.why_not_buy = [f"weak overall score ({result.score:.0f}/100)"]
        elif best_plan is not None and result.score >= threshold and not blockers:
            # The grade depends on the setup's backtest record, not on the score alone.
            record = evidence_module.for_setup(best_plan.setup_key)
            # Broker buying as a filter: used only for setups the broker study switched on (broker_filter.filter_setups).
            if best_plan.setup_key in (setting("broker_filter", "filter_setups", default=[]) or []) and has_broker_buying(result.broker_net_share):
                confirmed = evidence_module.for_setup(best_plan.setup_key + "_broker")
                if confirmed.proven:
                    record = confirmed
                    result.notes.append(f"Broker buying confirmed: the top 5 brokers net bought {result.broker_net_share:.0f}% of the volume in 10 days.")
            strong =result.score >= threshold + float(setting("regime", "strong_buy_margin", default=10)) and result.confidence == "High"
            result.verdict = ("STRONG BUY" if strong else "BUY") if record.proven else "SPECULATIVE"
            result.plan, result.setup_name, result.evidence = best_plan, best_setup, record
            warning = resistance_warning(best_plan)
            if warning:
                result.notes.append(warning)
        else:
            result.verdict = "WATCH"
            if best_plan is not None:
                result.setup_name = best_setup
                result.blocked_plan = best_plan
                if result.score < threshold:
                    blockers.insert(0, f"score {result.score:.0f} is below the {threshold:.0f} needed in a {regime.label} market")
                if no_upside and result.score >= threshold and len(blockers) == 1:
                    multiple = float(setting("setups", "breakout_volume_multiple", default=1.5))
                    result.trigger_level, result.trigger_setup = best_plan.target_1, best_plan.setup_key
                    result.watch_trigger = (
                        f"{best_setup} setup, but no room to the resistance at {best_plan.target_1:,.0f} ({best_plan.target_1_reason}, "
                        f"+{best_plan.target_1_percent:.1f}%). Buy only on a close above {best_plan.target_1:,.0f} on at least {multiple:g}x normal volume."
                    )
                else:
                    result.watch_trigger = (
                        f"{best_setup} setup is ready (entry {best_plan.entry_low:,.0f}-{best_plan.entry_high:,.0f}, stop {best_plan.stop:,.0f}) "
                        "but: " + "; ".join(blockers) + "."
                    )
            elif triggers:
                result.watch_trigger, result.trigger_level, result.trigger_setup = triggers[0]
            result.why_not_buy = blockers + rejections
            if not result.why_not_buy and not result.watch_trigger:
                result.why_not_buy = ["no valid entry setup today"]
        return result
