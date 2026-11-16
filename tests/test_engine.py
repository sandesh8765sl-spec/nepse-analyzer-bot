"""The analysis engine: scoring rules, verdicts, no-lookahead and reports."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from nepse_bot.settings import load_yaml

from nepse_bot.bot import reports
from nepse_bot.db.store import Database
from nepse_bot.engine import evidence, scoring
from nepse_bot.engine.config import ENGINE_CONFIG_PATH, engine_config, load_profile
from nepse_bot.engine.engine import Engine
from nepse_bot.engine.indicators import add_indicators, rsi, weekly_trend
from nepse_bot.engine.market import classify_regime
from nepse_bot.engine.setups import ALL_SETUPS, BaseBreakout, StockContext, TrendPullback
from nepse_bot.ingest import metrics

ALLOWED_VERDICTS = {"SPECULATIVE", "STRONG BUY", "BUY", "WATCH", "AVOID", "IGNORE"}
DAYS = pd.bdate_range("2025-09-01", periods=300)
LAST = DAYS[-1].strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _setups_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests check the engine's logic, so they must not depend on which
    setups the owner currently has switched on in config/engine.yaml."""
    switches = {**engine_config()["setups"], "trend_pullback": True, "base_breakout": True, "rs_leader": True,
                "value_turn": True, "broker_accumulation": True, "event_runup": False}
    monkeypatch.setitem(engine_config(), "setups", switches)


def _bars(closes: list[float], turnover: float = 30_000_000.0, spread: float = 0.004) -> pd.DataFrame:
    index = DAYS[-len(closes):]
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({"open": close, "high": close * (1 + spread), "low": close * (1 - spread), "close": close, "turnover": turnover})


def _trend(start: float, end: float, bars: int = 300, wobble: float = 0.004) -> list[float]:
    return [(start + (end - start) * i / (bars - 1)) * (1 + wobble * math.sin(i / 3.0)) for i in range(bars)]


def _context(frame: pd.DataFrame, **extra) -> StockContext:
    prepared = add_indicators(frame)
    return StockContext(symbol="T", frame=prepared, weekly=weekly_trend(prepared), **extra)


# --------------------------------------------------------- the missing-data rule
def test_missing_fundamentals_score_exactly_neutral_and_lower_confidence() -> None:
    sub = scoring.score_fundamentals(None)
    assert sub.score == 50.0 and sub.coverage == 0.0
    assert any("unavailable" in text for _, text in sub.negatives)


def test_missing_data_never_adds_points() -> None:
    full = {"pe": 90.0, "pb": 80.0, "eps_growth": 85.0, "roe": 75.0, "dividend_yield": 70.0}
    view = {"grades": dict(full), "pe": 10, "pe_median": 20, "pb": 1.0, "eps_growth": 30, "roe": 18, "dividend_yield": 4, "sector_label": "Hydro Power"}
    best = scoring.score_fundamentals(view).score
    for missing_key in full:
        partial = {**view, "grades": {**full, missing_key: None}}
        result = scoring.score_fundamentals(partial)
        assert result.score < best, f"losing {missing_key} must not raise a good score"
        assert result.coverage < 1.0
    weak = {**view, "grades": {key: 10.0 for key in full}}
    weak_score = scoring.score_fundamentals(weak).score
    for missing_key in full:
        partial = {**weak, "grades": {**weak["grades"], missing_key: None}}
        assert scoring.score_fundamentals(partial).score <= 50.0, "missing data lifts a bad score to at most neutral"
    assert weak_score < 50.0


def test_short_history_does_not_get_trend_points_for_missing_averages() -> None:
    long_up = scoring.score_trend(_context(_bars(_trend(200, 400))))
    short_up = scoring.score_trend(_context(_bars(_trend(300, 400, bars=120))))  # no 200-day average yet
    assert long_up.coverage == 1.0 and short_up.coverage < 1.0
    assert short_up.score < long_up.score, "an unknown 200-day average must not count as a pass"


def test_confidence_comes_from_data_coverage() -> None:
    subs = {name: scoring.SubScore(name, 70.0, 1.0) for name in engine_config()["weights"]}
    assert scoring.combine(subs)[2] == "High"
    subs["fundamentals"] = scoring.SubScore("fundamentals", 50.0, 0.0)   # 20% of the score has no data
    score, coverage, confidence = scoring.combine(subs)
    assert confidence == "Medium" and coverage == pytest.approx(0.8) and score == pytest.approx(66.0)
    subs["volume_flow"] = scoring.SubScore("volume_flow", 50.0, 0.0)
    assert scoring.combine(subs)[2] == "Low"
    assert sum(engine_config()["weights"].values()) == 100


def test_overextension_caps_momentum_and_is_reported() -> None:
    frame = _bars(_trend(200, 300, bars=280) + [300 * (1.04 ** i) for i in range(1, 21)])
    ctx = _context(frame)
    assert scoring.is_overextended(ctx) is not None
    sub = scoring.score_momentum(ctx, 95.0, 95.0)
    assert sub.score <= 40.0 and any("wait for a pullback" in text for _, text in sub.negatives)


def test_rsi_and_regime_basics() -> None:
    assert rsi(pd.Series([100.0 + i for i in range(40)])).iloc[-1] == pytest.approx(100.0)
    falling = rsi(pd.Series([100.0 - i for i in range(40)])).iloc[-1]
    assert falling < 5
    index = pd.Series(_trend(2000, 2800, wobble=0.0), index=[d.strftime("%Y-%m-%d") for d in DAYS])
    breadth = pd.DataFrame({"date": index.index, "advancers": 180, "decliners": 80, "turnover": 5e9, "pct_above_ma50": 75.0})
    bull = classify_regime(index, breadth, LAST)
    assert bull.label == "BULL" and bull.buy_threshold == 65
    assert bull.overheated and bull.max_new_positions == 1, "75% of stocks above their 50-day average = overheated: 1 new position"
    down = pd.Series(_trend(2800, 2000, wobble=0.0), index=index.index)
    weak = breadth.assign(advancers=60, decliners=200, pct_above_ma50=15.0, turnover=[5e9] * 250 + [2e9] * 50)
    bear = classify_regime(down, weak, LAST)
    assert bear.label == "BEAR" and bear.buy_threshold == 65, "the label no longer changes the score needed (Stage 4b)"
    assert not bear.overheated and bear.max_new_positions == 2


# ------------------------------------------------------------------- setups
def test_breakout_setup_fires_only_on_a_completed_close_with_volume() -> None:
    base = _trend(200, 300, bars=255, wobble=0.0) + [300 + 2 * math.sin(i) for i in range(44)]
    quiet = _bars(base + [303.5])
    assert BaseBreakout().evaluate(_context(quiet)).signal is None, "inside the base: no buy"
    weak = _bars(base + [308.0])
    outcome = BaseBreakout().evaluate(_context(weak))
    assert outcome.signal is None and "weak volume" in outcome.watch
    strong = _bars(base + [308.0])
    strong.iloc[-1, strong.columns.get_loc("turnover")] = 90_000_000.0
    signal = BaseBreakout().evaluate(_context(strong)).signal
    assert signal is not None and signal.entry_low < 308.0 <= signal.entry_high * 1.01
    chased = _bars(base + [325.0])
    chased.iloc[-1, chased.columns.get_loc("turnover")] = 90_000_000.0
    outcome = BaseBreakout().evaluate(_context(chased))
    assert outcome.signal is None and "retest" in outcome.watch, "never chase a breakout that already ran"


def test_pullback_setup_gives_a_trigger_price_when_not_ready() -> None:
    extended = _context(_bars(_trend(200, 400, wobble=0.0)))
    outcome = TrendPullback().evaluate(extended)
    assert outcome is not None and outcome.signal is None and "Buy" in outcome.watch, "a price to wait for is always given"
    assert TrendPullback().evaluate(_context(_bars(_trend(400, 200, wobble=0.0)))) is None, "no pullback buys in a downtrend"


def test_setups_are_on_but_none_is_called_proven_without_evidence() -> None:
    # Stage 4b: setups stay on so the bot always ranks candidates, but the grade comes from the backtest record.
    switches = load_yaml(ENGINE_CONFIG_PATH)["setups"]
    assert switches["trend_pullback"] and switches["base_breakout"] and not switches["event_runup"]
    assert evidence.for_setup("no_such_setup").proven is False
    good = evidence.Record("p", 80, 45.0, 9.0, -5.0, 1.3, 1.5)
    thin = evidence.Record("p", 20, 60.0, 9.0, -5.0, 3.0, 2.5)
    bad = evidence.Record("p", 80, 30.0, 7.0, -6.0, -2.0, 0.5)
    assert evidence.Evidence("x", good, good).proven
    assert not evidence.Evidence("x", thin, good).proven, "20 trades is too few to prove anything"
    assert not evidence.Evidence("x", bad, good).proven and evidence.Evidence("x", bad, good).grade == "mixed"
    assert evidence.Evidence("x", bad, bad).grade == "unproven" and "LOST money" in evidence.Evidence("x", bad, bad).warning()


# ---------------------------------------------------------- the whole engine
def _store(db: Database, symbol: str, frame: pd.DataFrame, sector: str = "Hydro Power", status: str = "active") -> None:
    db.upsert("securities", [{"symbol": symbol, "name": f"{symbol} Limited", "sector": sector, "instrument_type": "equity", "status": status, "is_promoter": 0}], ("symbol",))
    rows = [
        {"symbol": symbol, "date": day.strftime("%Y-%m-%d"), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.turnover / r.close, "turnover": r.turnover, "trades": 100}
        for day, r in frame.iterrows()
    ]
    db.upsert_prices(rows, "test")
    with db.transaction() as connection:
        connection.execute("UPDATE daily_prices SET adj_factor = 1, adj_open = open, adj_high = high, adj_low = low, adj_close = close WHERE symbol = ?", (symbol,))


@pytest.fixture()
def market(tmp_path: Path, calendar) -> tuple[Engine, Database]:
    db = Database(tmp_path / "engine.sqlite3")
    base = _trend(200, 300, bars=255, wobble=0.0) + [300 + 2 * math.sin(i) for i in range(44)]
    breakout = _bars(base + [308.0])
    breakout.iloc[-1, breakout.columns.get_loc("turnover")] = 90_000_000.0
    _store(db, "BRK", breakout)
    _store(db, "UP", _bars(_trend(150, 260)))
    _store(db, "DOWN", _bars(_trend(500, 250)))
    _store(db, "FLAT", _bars(_trend(300, 303)))
    _store(db, "ILLIQ", _bars(_trend(100, 160), turnover=400_000.0))
    _store(db, "NEW", _bars(_trend(100, 130, bars=60)))
    _store(db, "SUSP", _bars(_trend(100, 160)), status="suspended")
    for i in range(6):
        _store(db, f"MID{i}", _bars(_trend(200 + 10 * i, 215 + 12 * i)))
    index = [{"index_code": code, "date": day.strftime("%Y-%m-%d"), "close": 2000 + 800 * i / 299, "is_sector": int(code != "NEPSE")} for code in ("NEPSE", "HYDROPOWER") for i, day in enumerate(DAYS)]
    db.upsert("index_daily", index, ("index_code", "date"))
    prices = db.frame("SELECT symbol, date, close, adj_close, turnover, trades FROM daily_prices")
    db.upsert("market_breadth_daily", [{**row, "advancers": 180, "decliners": 70} for row in metrics.breadth_table(prices)], ("date",))
    calendar.set_now(f"{LAST} 16:00")
    return Engine(db=db, calendar=calendar), db


def test_engine_verdicts(market, monkeypatch) -> None:
    engine, _ = market
    import requests

    def no_network(*_args, **_kwargs):
        raise AssertionError("the engine must not use the network")

    monkeypatch.setattr(requests.Session, "request", no_network)
    scan = engine.scan()
    results = scan.results
    assert scan.as_of == LAST and not scan.stale and scan.regime.label == "BULL"
    assert {item.verdict for item in results.values()} <= ALLOWED_VERDICTS, "never SELL or HOLD for stocks the owner does not hold"
    assert results["ILLIQ"].verdict == "IGNORE" and "illiquid" in results["ILLIQ"].why_not_buy[0]
    assert results["NEW"].verdict == "IGNORE" and "listed only" in " ".join(results["NEW"].why_not_buy)
    assert results["SUSP"].verdict == "IGNORE" and "suspended" in " ".join(results["SUSP"].why_not_buy)
    assert results["DOWN"].verdict == "AVOID" and results["DOWN"].why_not_buy == ["downtrend"]
    assert results["UP"].score > results["FLAT"].score > results["DOWN"].score
    assert results["UP"].rs_rank > results["DOWN"].rs_rank
    for item in results.values():
        if item.verdict != "IGNORE":
            assert item.sub_scores["fundamentals"] == 50.0, "no fundamentals in this test market: neutral"
            assert item.confidence != "High", "missing fundamentals must lower confidence"
            assert len(item.positives) <= 3 and len(item.negatives) <= 3


def test_engine_produces_a_complete_plan_for_a_clean_breakout(market, monkeypatch) -> None:
    engine, db = market
    monkeypatch.setattr(evidence, "for_setup", lambda key: evidence.Evidence(key, None, None))
    scan = engine.scan(use_cache=False)
    item = scan.results["BRK"]
    assert item.verdict == "SPECULATIVE", f"an unproven setup is never called BUY, got {item.verdict}: {item.why_not_buy} {item.watch_trigger}"
    plan = item.plan
    assert item.is_buy and item.setup_name == "Base breakout" and plan is not None
    assert plan.entry <= 308.0 * 1.03 and 4.0 <= plan.stop_percent <= 12.5 and plan.quantity % 10 == 0
    assert item.score >= scan.regime.buy_threshold
    assert engine.record_signals(scan) >= 1 and engine.record_signals(scan) >= 1
    logged = db.query("SELECT * FROM signals")
    assert len(logged) == len(scan.buys), "each candidate with a plan is logged once"
    assert logged[0]["entry"] == plan.entry and logged[0]["status"] == "open"
    text = reports.format_stock(item, scan, engine.calendar)
    for expected in ("PLAN", "Buy:", "Stop:", "Qty:", "Risk:", "No fixed target", "Reference targets", "Consider booking partial profit", "This setup's record", "SPECULATIVE", "WHY", "As of:", "Not financial advice"):
        assert expected in text


def test_a_proven_setup_can_be_a_buy_and_shows_its_record(market, monkeypatch) -> None:
    engine, _ = market
    good = evidence.Record("2024-11 to 2026-11", 80, 45.0, 9.0, -5.0, 1.3, 1.5)
    monkeypatch.setattr(evidence, "for_setup", lambda key: evidence.Evidence(key, good, good))
    scan = engine.scan(use_cache=False)
    item = scan.results["BRK"]
    assert item.verdict in {"BUY", "STRONG BUY"}
    text = reports.format_stock(item, scan, engine.calendar)
    assert "80 trades, 45% won, average win +9.0%, average loss -5.0%, average result +1.3% per trade" in text
    top, _ = reports.format_top10(scan, engine.calendar)
    assert "45% won, +1.3%/trade (80 trades)" in top


def test_a_stale_database_never_produces_a_buy(market, calendar) -> None:
    engine, _ = market
    calendar.set_now("2026-12-15 12:00")   # weeks after the newest data
    scan = engine.scan(use_cache=False)
    assert scan.stale and not scan.buys
    assert "behind the last completed trading day" in " ".join(scan.results["BRK"].why_not_buy + [scan.results["BRK"].watch_trigger or ""])
    assert reports.freshness_line(scan, calendar).startswith("⚠️")


def test_no_lookahead_a_past_scan_ignores_later_data(market) -> None:
    engine, db = market
    earlier = DAYS[-30].strftime("%Y-%m-%d")
    before = {symbol: (item.verdict, item.score) for symbol, item in engine.scan(earlier, use_cache=False).results.items()}
    with db.transaction() as connection:
        connection.execute("DELETE FROM daily_prices WHERE date > ?", (earlier,))
        connection.execute("DELETE FROM index_daily WHERE date > ?", (earlier,))
        connection.execute("DELETE FROM market_breadth_daily WHERE date > ?", (earlier,))
    after = {symbol: (item.verdict, item.score) for symbol, item in engine.scan(earlier, use_cache=False).results.items()}
    assert {k: v[1] for k, v in before.items()} == {k: v[1] for k, v in after.items()}, "a past scan's scores must not be influenced by anything after it"
    plan_verdicts = {"STRONG BUY", "BUY", "SPECULATIVE"}
    for symbol, (verdict, _) in before.items():
        # After the delete the database really is behind the calendar, so a candidate is (correctly) held back as WATCH.
        assert after[symbol][0] == verdict or (verdict in plan_verdicts and after[symbol][0] == "WATCH")


def test_fundamentals_are_graded_within_the_sector_and_respect_publication_dates(market) -> None:
    engine, db = market
    reports_rows = []
    for i, symbol in enumerate(["UP", "FLAT", "MID0", "MID1", "MID2", "MID3"]):
        reports_rows.append({"symbol": symbol, "fiscal_year": "2082/2083", "quarter": 4, "period_end_date": "2026-07-16", "publication_date": "2026-08-20", "eps_annualized": 10.0 + 6 * i, "book_value": 150.0, "roe": 8.0 + 2 * i})
    reports_rows.append({"symbol": "UP", "fiscal_year": "2083/2084", "quarter": 1, "period_end_date": "2026-10-17", "publication_date": "2099-01-01", "eps_annualized": 999.0, "book_value": 150.0, "roe": 99.0})
    db.upsert("fundamentals", [r for r in reports_rows if r["quarter"] == 4], ("symbol", "fiscal_year", "quarter"))
    db.upsert("fundamentals", [r for r in reports_rows if r["quarter"] == 1], ("symbol", "fiscal_year", "quarter"))
    scan = engine.scan(use_cache=False)
    view = scan.results["UP"].fundamentals
    assert view["eps"] == 10.0, "a report published in the future must not be used"
    assert scan.results["MID3"].fundamentals["grades"]["roe"] > scan.results["UP"].fundamentals["grades"]["roe"]
    assert scan.results["UP"].sub_scores["fundamentals"] != 50.0
    assert scan.results["UP"].fundamentals["sector_label"] == "Hydro Power"


def test_book_closure_within_five_days_blocks_a_buy(market, calendar) -> None:
    engine, db = market
    closure = calendar.next_trading_day(calendar.next_trading_day(date.fromisoformat(LAST)))
    db.upsert("corporate_actions", [{"symbol": "BRK", "action_type": "bonus", "fiscal_year": "2082/2083", "ratio": 10.0, "book_closure_date": closure.isoformat(), "source_id": "1"}], ("symbol", "action_type", "fiscal_year", "source_id"))
    item = engine.scan(use_cache=False).results["BRK"]
    assert not item.is_buy and item.verdict == "WATCH"
    assert any("Book closure in 2" in flag for flag in item.flags)


def test_profile_from_settings_changes_position_size(market) -> None:
    engine, db = market
    first = engine.scan().results["BRK"].plan
    db.set_setting("profile.max_position", "60000")
    profile = load_profile(db)
    assert profile.max_position == 60000 and profile.risk_budget == 10000
    second = engine.scan().results["BRK"].plan
    assert first is not None and second is not None and second.position_value <= 60000 < first.position_value


# ------------------------------------------------------------------ reports
def test_every_report_has_an_as_of_line_and_fits_in_a_message(market) -> None:
    engine, _ = market
    scan = engine.scan()
    texts = [
        reports.format_top10(scan, engine.calendar)[0],
        reports.format_buy_page(scan, 0, calendar=engine.calendar)[0],
        reports.format_watch(scan, calendar=engine.calendar)[0],
        reports.format_market(scan, engine.calendar),
        reports.format_sector(scan, None, engine.calendar)[0],
        reports.format_sector(scan, "Hydro Power", engine.calendar)[0],
        *(reports.format_stock(item, scan, engine.calendar) for item in scan.results.values()),
    ]
    for text in texts:
        assert "As of:" in text and len(text) < 3900
    top, symbols = reports.format_top10(scan, engine.calendar)
    assert "BRK" in symbols and "Buy " in top and "Stop " in top
    assert len(symbols) >= min(10, sum(1 for item in scan.results.values() if item.verdict in {"WATCH", *("STRONG BUY", "BUY", "SPECULATIVE")})), "always a full ranked list"
    assert reports.match_sector(scan, "hydro") == "Hydro Power" and reports.match_sector(scan, "zzz") is None
    assert "IGNORE" in reports.format_stock(scan.results["ILLIQ"], scan, engine.calendar)
    assert "AVOID BECAUSE" in reports.format_stock(scan.results["DOWN"], scan, engine.calendar)


def test_reports_escape_html_and_settings_text() -> None:
    assert "&lt;b&gt;" in reports.escape("<b>")
    text = reports.format_settings(load_profile(None))
    assert "Rs 500,000" in text and "/settings capital" in text
    assert reports.split_message("a\n" * 5000, limit=1000)[0].count("\n") < 1000
