"""Stage 5: events engine, calendar, position manager, portfolio risk, targets, exit styles, daily messages."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from nepse_bot.backtest import longterm
from nepse_bot.bot import briefs, handlers, manage, personal, reports
from nepse_bot.db.store import Database
from nepse_bot.engine import evidence, exits
from nepse_bot.engine.config import Profile, engine_config
from nepse_bot.engine.indicators import atr
from nepse_bot.events import engine as events
from nepse_bot.portfolio import positions
from nepse_bot.portfolio.fees import fee_schedule
from test_engine import DAYS, LAST, _bars, _setups_switched_on, _store, _trend, market  # noqa: F401  (fixtures)

FEES = fee_schedule()
PROFILE = Profile(total_capital=500000, min_position=50000, max_position=100000, max_open_positions=5, risk_percent=2.0, risk_rupees=None)
TODAY = "2026-11-13"


# ------------------------------------------------------------------- events
def test_events_are_typed_fresh_and_deduplicated() -> None:
    assert events.classify("Book Close Information- NMB [NMB]") == "book_closure"
    assert events.classify("NSBL Dividend Declaration for FY 2082-83 [SBI]") == "dividend"
    assert events.classify("Intention for sale of share [SPHL]") == "promoter_sale"
    assert events.classify("Appointment of Auditor [BNHC]") == "management"
    assert events.classify("Something unrelated") == "other"
    rows = [
        {"symbol": "NMB", "title": "Book Close Information- NMB [NMB]", "published_at": "2026-11-13T10:00:00", "source": "ShareHub"},
        {"symbol": "NMB", "title": "NMB book closure notice", "published_at": "2026-11-12", "source": "Portal"},          # same event, other source
        {"symbol": "SBI", "title": "Dividend Declaration [SBI]", "published_at": "2026-11-13", "source": "ShareHub"},
        {"symbol": "SBI", "title": "Dividend Declaration [SBI]", "published_at": "2026-10-01", "source": "ShareHub"},      # stale
        {"symbol": "XYZ", "title": "Bonus Declaration [XYZ]", "published_at": "", "source": "ShareHub"},                  # undated
        {"symbol": "ABC", "title": "Bonus Declaration [ABC]", "published_at": "2026-11-20", "source": "ShareHub"},        # dated in the future
    ]
    found = events.build_events(rows, TODAY)
    assert [(event.symbol, event.kind) for event in found] == [("NMB", "book_closure"), ("SBI", "dividend")]
    assert found[1].impact == "positive" and found[0].note


def test_one_net_statement_per_stock_never_contradicts_itself() -> None:
    rows = [
        {"symbol": "SPHL", "title": "Dividend Declaration [SPHL]", "published_at": "2026-11-12", "source": "ShareHub"},
        {"symbol": "SPHL", "title": "Intention for sale of share [SPHL]", "published_at": "2026-11-13", "source": "ShareHub"},
        {"symbol": "SPHL", "title": "Appointment of Auditor [SPHL]", "published_at": "2026-11-13", "source": "ShareHub"},
    ]
    found = events.build_events(rows, TODAY)
    assert events.net_impact(found)[0] == "mixed"
    lines = events.event_lines(found)
    assert len(lines) == 1 and lines[0].startswith("🟡 SPHL") and "takes no side" in lines[0]
    assert "Appointment" not in lines[0], "routine items stay out of the daily messages"
    assert events.net_impact([event for event in found if event.kind == "dividend"])[0] == "positive"
    text = personal.format_news("SPHL", found)
    assert "Taken together: <b>mixed</b>" in text and "Usual effect" in text


def test_calendar_lists_book_closures_with_the_last_day_to_buy(tmp_path: Path, calendar) -> None:
    db = Database(tmp_path / "c.sqlite3")
    db.upsert("corporate_actions", [
        {"symbol": "NABIL", "action_type": "cash_dividend", "fiscal_year": "2082/2083", "amount": 10.0, "book_closure_date": "2026-11-23", "source_id": "1"},
    ], ("symbol", "action_type", "fiscal_year", "source_id"))
    db.upsert("corporate_actions", [
        {"symbol": "NABIL", "action_type": "bonus", "fiscal_year": "2082/2083", "ratio": 5.0, "book_closure_date": "2026-11-23", "source_id": "2"},
        {"symbol": "OLD", "action_type": "bonus", "fiscal_year": "2081/2082", "ratio": 5.0, "book_closure_date": "2026-01-10", "source_id": "3"},
    ], ("symbol", "action_type", "fiscal_year", "source_id"))
    items = events.upcoming_closures(db, TODAY, calendar)       # 13 Nov 2026 is a Friday, 23 Nov a Monday
    assert len(items) == 1 and items[0].symbol == "NABIL" and items[0].payout == "10% cash + 5% bonus"
    assert items[0].trading_days_left == 6 and items[0].last_buy_day == "2026-11-20"
    text = briefs.format_calendar(items, {"NABIL"}, [("2026-11-25", "Test holiday")], [], TODAY)
    assert "you hold it" in text and "2026-11-20" in text and "Test holiday" in text


# ---------------------------------------------------------------- positions
def _position_bars(closes: list[float]) -> pd.DataFrame:
    index = [day.strftime("%Y-%m-%d") for day in pd.bdate_range(end="2026-11-13", periods=len(closes))]
    close = pd.Series(closes, index=index, dtype=float)
    frame = pd.DataFrame({"high": close * 1.01, "low": close * 0.99, "close": close, "adj_factor": 1.0})
    frame["atr14"] = atr(frame, 14)
    return frame


def _review(closes: list[float], entry_index: int = -20, **extra):
    bars = _position_bars(closes)
    position = {"symbol": "TEST", "quantity": 100, "avg_price": float(bars["close"].iloc[entry_index]), "opened_at": bars.index[entry_index],
                "stop_loss": None, "target_1": extra.pop("target", None)}
    return positions.review_position(position, bars, today=TODAY, style=extra.pop("style", "trailing"), fees=FEES, profile=PROFILE, **extra)


def test_buy_and_sell_are_recorded_with_fees_and_tax(tmp_path: Path) -> None:
    db = Database(tmp_path / "p.sqlite3")
    positions.record_buy(db, "NABIL", 60, 500.0, "2026-10-01", FEES)
    position = positions.record_buy(db, "NABIL", 40, 500.0, "2026-10-05", FEES)
    assert position["quantity"] == 100 and position["avg_price"] == 500.0 and position["opened_at"] == "2026-10-01"
    sale = positions.record_sell(db, "NABIL", 100, 550.0, "2026-11-13", FEES)
    assert round(sale["result"].net_profit) == 4367, "5,000 gross - 402.25 costs - 231.14 tax (as in the Stage 3 portfolio)"
    assert sale["remaining"] == 0 and positions.open_positions(db) == []
    assert db.scalar("SELECT COUNT(*) FROM positions") == 1 and db.scalar("SELECT COUNT(*) FROM trades") == 3, "nothing is deleted"
    assert round(positions.realised_between(db, "2026-11-09", "2026-11-13")[0]["realised"]) == 4367
    assert positions.record_sell(db, "NOPE", 10, 100.0, TODAY, FEES) is None
    positions.record_buy(db, "UPPER", 100, 200.0, TODAY, FEES)
    assert positions.remove_position(db, "UPPER", TODAY) and db.scalar("SELECT COUNT(*) FROM positions WHERE symbol = 'UPPER'") == 1


def test_advice_hold_while_above_the_trailing_stop_and_sell_below_it() -> None:
    rising = _review([300 + i for i in range(60)])
    assert rising.action == "HOLD" and rising.trailing_stop < rising.close and "trailing stop" in rising.reasons[0]
    assert rising.trailing_stop > rising.avg_price * 0.96, "the stop has followed the price up"
    assert rising.net.net_profit > 0 and any("Tax" in note for note in rising.notes)
    fallen = _review([300 + i for i in range(50)] + [349 - 4 * i for i in range(1, 11)], entry_index=-30)
    assert fallen.action == "SELL" and "below the trailing stop" in fallen.reasons[0]
    loser = _review([300 - 0.2 * i for i in range(80)], entry_index=-50)
    assert loser.action == "SELL" and any("without a profit" in reason for reason in loser.reasons)
    assert any("No capital gains tax on a loss" in note for note in loser.notes)


def test_advice_follows_the_exit_style_at_the_reference_target() -> None:
    closes = [300 + i for i in range(60)]
    assert _review(closes, target=350.0, style="trailing").action == "HOLD"
    hybrid = _review(closes, target=350.0, style="hybrid")
    assert hybrid.action == "TRIM" and "sell half" in hybrid.reasons[0]
    assert _review(closes, target=350.0, style="hybrid", sold_part=True).action == "HOLD", "half is sold only once"
    target = _review(closes, target=350.0, style="target")
    assert target.action == "SELL" and "reached your target" in target.reasons[0]
    assert _review(closes, target=350.0, verdict="AVOID", verdict_reasons=["downtrend"]).action == "TRIM"


def test_book_closure_and_long_term_tax_helpers() -> None:
    closure = events.ClosureItem("TEST", "2026-11-23", 6, "2026-11-20", 10.0, 5.0, None)
    view = _review([300 + i for i in range(60)], closures=[closure])
    assert any("Book closure 2026-11-23 (10% cash + 5% bonus)" in note and "2026-11-20" in note for note in view.notes)
    bars = _position_bars([200 + 0.5 * i for i in range(260)])
    old = positions.review_position({"symbol": "TEST", "quantity": 100, "avg_price": 205.0, "opened_at": "2025-12-01", "stop_loss": None, "target_1": None},
                                    bars, today=TODAY, style="trailing", fees=FEES, profile=PROFILE)
    assert old.days_held == 347 and any("3.75%" in note and "19 days" in note for note in old.notes)


def test_bonus_shares_since_purchase_are_brought_into_todays_terms() -> None:
    bars = _position_bars([100.0] * 40)
    bars.iloc[:20, bars.columns.get_loc("adj_factor")] = 0.8          # a 25% bonus 20 sessions ago
    view = positions.review_position({"symbol": "TEST", "quantity": 100, "avg_price": 125.0, "opened_at": bars.index[5], "stop_loss": None, "target_1": None},
                                     bars, today=TODAY, style="trailing", fees=FEES, profile=PROFILE)
    assert view.quantity == 125 and view.avg_price == pytest.approx(100.0) and view.adjusted


def test_portfolio_risk_rules() -> None:
    def view(symbol: str, sector: str, value: float, back: float = 0.0):
        return SimpleNamespace(symbol=symbol, sector=sector, value=value, given_back_at_stop=back)

    calm = [view("A", "Hydro Power", 90000, 4000), view("B", "Commercial Banks", 90000, 4000)]
    assert positions.portfolio_risk(calm, PROFILE) == []
    heavy = [view(f"H{i}", "Hydro Power", 90000, 8000) for i in range(6)]
    text = " ".join(positions.portfolio_risk(heavy, PROFILE, overheated=True))
    assert "6 open positions" in text and "Hydro Power is 108%" in text and "every trailing stop" in text and "overheated" in text
    notes = " ".join(positions.new_position_warnings(heavy[:5], PROFILE, "NEW", "Hydro Power", 100000))
    assert "already have 5 open positions" in notes and "free" in notes and "Hydro Power would be" in notes
    assert "already hold" in positions.new_position_warnings(calm, PROFILE, "A", "Hydro Power", 50000)[0]
    assert positions.new_position_warnings([], PROFILE, "A", "Hydro Power", 50000) == []


def test_trade_command_parsing() -> None:
    assert manage.parse_trade(["nabil", "100", "520"], TODAY) == ("NABIL", 100, 520.0, TODAY)
    assert manage.parse_trade(["NABIL", "1,000", "520.5", "2026-10-01"], TODAY) == ("NABIL", 1000, 520.5, "2026-10-01")
    for bad in (["NABIL", "100"], ["NABIL", "0", "520"], ["NABIL", "10.5", "520"], ["NABIL", "100", "520", "2027-01-01"], ["NABIL", "100", "520", "soon"]):
        assert isinstance(manage.parse_trade(bad, TODAY), str)


# ------------------------------------------------------ targets, exit styles
def test_exit_style_default_and_honest_comparison(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.sqlite3")
    assert exits.default_style() == "trailing" and exits.exit_style(db) == "trailing"
    db.set_setting(exits.SETTING_KEY, "hybrid")
    assert exits.exit_style(db) == "hybrid"
    db.set_setting(exits.SETTING_KEY, "nonsense")
    assert exits.exit_style(db) == "trailing"
    assert exits.worse_than_default_note("trailing") is None
    note = exits.worse_than_default_note("target")
    assert note and "worse than 'trailing' in 2020-2024" in note
    text = reports.format_exit_help("target")
    for expected in ("/settings exit trailing", "/settings exit target", "/settings exit hybrid", "(default)", "LOST money", "worse than"):
        assert expected in text
    assert "Exit style" in reports.format_settings(PROFILE, "hybrid")


def test_reference_targets_typical_outcome_and_partial_level() -> None:
    plan = SimpleNamespace(setup_key="trend_pullback", entry=590.0, quantity=160, target_1=638.0, target_1_reason="previous swing high",
                           high_52w=680.0, hold_text="up to 40 trading days", trail_text="a stop that follows the price up")
    targets = exits.targets_for(plan, FEES)
    stats = exits.winners("trend_pullback")
    assert stats is not None and stats.count >= exits.MIN_WINNERS
    assert targets.resistance.price == 638.0 and targets.resistance.net_rupees == pytest.approx(FEES.round_trip(160, 590.0, 638.0, 25).net_profit)
    assert targets.high_52w.price == 680.0 and targets.typical.price == pytest.approx(590.0 * (1 + stats.median_pct / 100), abs=0.06)
    assert targets.partial.price == 638.0 and targets.partial_quantity == 80      # one rule everywhere: the reference target
    assert exits.targets_for(SimpleNamespace(**{**plan.__dict__, "high_52w": 590.0}), FEES).high_52w is None
    near = exits.targets_for(SimpleNamespace(**{**plan.__dict__, "target_1": 600.0}), FEES)
    assert near.partial.price == near.resistance.price and "close to the entry" in near.partial_reason, "same level as the target shown, with a note"
    same = exits.targets_for(SimpleNamespace(**{**plan.__dict__, "target_1": 680.0}), FEES)
    assert same.high_52w is None and "52-week high" in same.resistance.name, "one line when the resistance IS the 52-week high"
    text = "\n".join(reports.target_lines(plan, "hybrid"))
    for expected in ("Reference targets", "52-week high", "Typical outcome for this setup", "past winners: +", "top quarter +", "Consider booking partial profit", "Sell half at 638"):
        assert expected in text
    assert "Sell everything at 638" in "\n".join(reports.target_lines(plan, "target"))
    assert "No fixed target" in "\n".join(reports.target_lines(plan, "trailing"))


def test_small_fixes_full_data_marker_and_daily_limit_line(market, monkeypatch) -> None:  # noqa: F811
    engine, _ = market
    monkeypatch.setattr(evidence, "for_setup", lambda key: evidence.Evidence(key, None, None))
    scan = engine.scan(use_cache=False)
    top = reports.format_top10(scan, engine.calendar)[0]
    assert "★" not in top and "📊 full data" in top and "Ref target" in top
    for text in (top, reports.format_buy_page(scan, 0, calendar=engine.calendar)[0], reports.format_market(scan, engine.calendar)):
        assert "Act on at most" not in text, "the daily limit is shown only when something is graded BUY"
    good = evidence.Record("2024-11 to 2026-11", 80, 45.0, 9.0, -5.0, 1.3, 1.5)
    monkeypatch.setattr(evidence, "for_setup", lambda key: evidence.Evidence(key, good, good))
    proven = engine.scan(use_cache=False)
    assert "Act on at most" in reports.format_top10(proven, engine.calendar)[0]
    page = reports.format_buy_page(proven, 0, calendar=engine.calendar, style="hybrid")[0]
    assert "Reference target" in page and "Partial profit" in page and "Exit (hybrid)" in page


# ----------------------------------------------------------- daily messages
def test_daily_messages_and_position_flow(market, monkeypatch) -> None:  # noqa: F811
    engine, db = market
    monkeypatch.setattr(handlers, "_engine", engine)
    monkeypatch.setattr(evidence, "for_setup", lambda key: evidence.Evidence(key, None, None))
    scan = engine.scan(use_cache=False)
    assert "no open positions" in manage.build_positions_text(scan)
    stop, target, high = manage._levels_for_new_position(scan, "BRK", scan.results["BRK"].plan.entry)
    assert stop == scan.results["BRK"].plan.stop and target == scan.results["BRK"].plan.target_1
    positions.record_buy(db, "UP", 300, 240.0, DAYS[-30].strftime("%Y-%m-%d"), FEES)
    positions.record_buy(db, "DOWN", 100, 300.0, DAYS[-30].strftime("%Y-%m-%d"), FEES)
    db.upsert("news", [{"symbol": "UP", "title": "Dividend Declaration [UP]", "source": "ShareHub", "url": "u1", "published_at": LAST, "kind": "announcement"}], ("source", "url", "title"))
    views, risk, missing = manage.review_all(scan)
    by_symbol = {view.symbol: view for view in views}
    assert by_symbol["UP"].action == "HOLD" and by_symbol["DOWN"].action in {"SELL", "TRIM"} and not missing
    text = manage.build_positions_text(scan)
    assert "<b>UP</b> — <b>HOLD</b>" in text and "Trailing stop" in text and "after fees and tax" in text
    assert text.index("DOWN") < text.index("<b>UP</b>"), "positions needing action come first"
    assert any("already hold" in note for note in manage.new_buy_notes(scan, "BRK")) is False
    brief = manage.build_morning_brief(scan)
    for expected in ("Morning brief", "As of:", "Your positions", "Candidates", "SPECULATIVE", "Fresh events", "UP: Dividend / bonus announced"):
        assert expected in brief, expected
    evening = manage.build_evening_report(scan)
    for expected in ("Evening report", "stocks up", "Change on the last session", "Announced today"):
        assert expected in evening, expected
    positions.record_sell(db, "DOWN", 100, 250.0, LAST, FEES)
    weekly = manage.build_weekly_summary(scan)
    for expected in ("Weekly summary", "NEPSE:", "New candidates logged", "Sold 100 DOWN", "Realised this week"):
        assert expected in weekly, expected
    for message in (brief, evening, weekly, text, manage.build_calendar_text(scan)):
        assert len(message) < 3900 and "Not financial advice" in message or "Calendar" in message


# ------------------------------------------------------- long-horizon study
def test_longterm_ranking_prefers_cheap_quality_and_ignores_unpublished_reports(market, monkeypatch) -> None:  # noqa: F811
    _, db = market
    rows = []
    for i, symbol in enumerate(["UP", "FLAT", "MID0", "MID1", "MID2", "MID3"]):
        rows.append({"symbol": symbol, "fiscal_year": "2082/2083", "quarter": 4, "publication_date": "2026-08-20", "eps_annualized": 10.0 + 6 * i, "book_value": 150.0, "roe": 8.0 + 2 * i})
    rows.append({"symbol": "UP", "fiscal_year": "2083/2084", "quarter": 1, "publication_date": "2099-01-01", "eps_annualized": 999.0, "book_value": 150.0, "roe": 99.0})
    db.upsert("fundamentals", rows[:-1], ("symbol", "fiscal_year", "quarter"))
    db.upsert("fundamentals", rows[-1:], ("symbol", "fiscal_year", "quarter"))
    monkeypatch.setattr(longterm, "get_database", lambda: db)
    data = longterm.Data()
    ranking = data.rank(LAST)
    assert set(ranking.index) == {"UP", "FLAT", "MID0", "MID1", "MID2", "MID3"}, "only liquid stocks with published fundamentals"
    assert ranking.loc["UP", "eps"] == 10.0, "a report published in the future must not be used"
    assert ranking.index[0] == "MID3" and ranking.loc["MID3", "rank"] == 1, "highest earnings yield and ROE ranks first"
    assert data.rank("2026-08-19").empty, "nothing is known before the publication date"
    days = data.rebalance_days(DAYS[0].strftime("%Y-%m-%d"), LAST, monthly=False)
    assert days and all(int(day[5:7]) in (3, 6, 9, 12) for day in days)
    monkeypatch.setitem(engine_config(), "longterm", {**engine_config()["longterm"], "positions": 2})
    result = longterm.simulate(data, "2026-08-25", LAST, monthly=True, pause_overheated=False, fees=FEES)
    assert result["trades"] >= 1 and abs(result["net_profit"]) < 500000


# ------------------------------------------------------------- lock-in expiry
def test_lockin_rows_are_parsed_and_flagged_within_15_trading_days(tmp_path: Path) -> None:
    from nepse_bot.bot import briefs
    from nepse_bot.db.store import Database
    from nepse_bot.events import unlocks
    from nepse_bot.sources.sharehub import parse_lockins
    from nepse_bot.trading_calendar import get_calendar

    payload = {"data": {"content": [
        {"symbol": "vlucl", "type": "Promoter", "totalListedShares": 19125000, "lockedShares": 16724590,
         "allotmentDate": "2023-11-21T00:00:00", "lockInEndDate": "2026-11-20T00:00:00"},
        {"symbol": "YMHL", "type": "MutualFund", "totalListedShares": 7000000, "lockedShares": 105000, "lockInEndDate": "2027-01-11T00:00:00"},
        {"symbol": "BAD", "type": "Promoter", "lockInEndDate": None},
    ]}}
    rows = parse_lockins(payload)
    assert [(row["symbol"], row["holder_type"], row["unlock_date"]) for row in rows] == [("VLUCL", "Promoter", "2026-11-20"), ("YMHL", "MutualFund", "2027-01-11")]

    db = Database(tmp_path / "t.sqlite3")
    db.upsert("lockin_unlocks", rows, ("symbol", "holder_type", "unlock_date"))
    calendar = get_calendar()
    items = unlocks.upcoming_unlocks(db, "2026-11-16", calendar, days=90)
    near, far = items
    assert near.symbol == "VLUCL" and near.trading_days_left == 4 and near.flagged
    assert round(near.share_percent, 1) == 87.4 and "promoter lock-in ends 2026-11-20" in near.short()
    assert not far.flagged
    assert set(unlocks.flagged_by_symbol(db, "2026-11-16", calendar)) == {"VLUCL"}
    text = briefs.format_calendar([], {"VLUCL"}, [], [], "2026-11-16", unlocks=items)
    assert "Lock-in expiry" in text and "VLUCL</b> (you hold it) ⚠️" in text
