"""Stage 5b: the fixes from live testing and the automatic alerts.

Alert filters (early and end-of-day), the confirm / cancel flow, the 10-day
no-repeat rule, the plan content, and the Part A fixes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nepse_bot.alerts import config as alert_config
from nepse_bot.alerts import rules, service, text
from nepse_bot.bot import ask, briefs, handlers, manage, reports
from nepse_bot.db.store import Database
from nepse_bot.engine import evidence, exits
from nepse_bot.engine.config import Profile
from nepse_bot.engine.market import SectorState
from nepse_bot.engine.plan import TradePlan
from nepse_bot.events import engine as events
from nepse_bot.portfolio import journal, positions
from nepse_bot.portfolio.fees import fee_schedule
from test_engine import DAYS, LAST, _bars, _setups_switched_on, _store, _trend, market  # noqa: F401  (fixtures)
from test_stage5 import _position_bars

FEES = fee_schedule()
PROFILE = Profile(total_capital=500000, min_position=50000, max_position=100000, max_open_positions=5, risk_percent=2.0, risk_rupees=None)
TODAY = "2026-11-13"


def _plan(**changes) -> TradePlan:
    """A calm stock with room: entry 500, stop 470 (-6%), reference target 560 (+12%, 2x the stop distance)."""
    values = dict(
        setup_key="trend_pullback", setup_name="Trend pullback", entry_low=495.0, entry_high=505.0, entry=500.0, amo_limit=500.0, stop=470.0,
        stop_percent=6.0, stop_reason="2.5x the average daily range below the entry", target_1=560.0, target_1_percent=12.0,
        target_1_reason="previous swing high", target_2=590.0, target_2_percent=18.0, reward_risk=2.0, quantity=160, position_value=80000.0,
        risk_rupees=5400.0, net_at_target_1=8500.0, net_at_target_2=13000.0, cost_percent=0.72, breakeven_stop=503.6,
        hold_text="up to 40 trading days (about 8 weeks)", time_stop_sessions=None, trail_text="a stop that follows the price up",
        high_52w=600.0, atr=12.0, avg_range_percent=2.1,
    )
    values.update(changes)
    return TradePlan(**values)


def _item(symbol: str = "CALM", score: float = 84.0, verdict: str = "SPECULATIVE", sector_label: str = "neutral", **changes):
    values = dict(
        symbol=symbol, name=f"{symbol} Limited", sector="Hydro Power", close=500.0, verdict=verdict, score=score, confidence="High",
        plan=_plan(), blocked_plan=None, positives=["Uptrend pulled back to its 20-day average", "Volume dried up during the pullback", "RSI 52: healthy"],
        sector_state=SectorState("Hydro Power", "HYDROPOWER", sector_label, 55.0, 2.0, 5.0, 1.0), flags=[], setup_name="Trend pullback",
        trigger_level=None, trigger_setup=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _scan(items, overheated: bool = False, as_of: str = TODAY):
    regime = SimpleNamespace(label="NEUTRAL", overheated=overheated, buy_threshold=65.0, max_new_positions=2)
    results = {item.symbol: item for item in items}
    return SimpleNamespace(
        as_of=as_of, regime=regime, results=results, profile=PROFILE, stale=False,
        buys=sorted((i for i in items if i.verdict in ("STRONG BUY", "BUY", "SPECULATIVE") and i.plan), key=lambda i: -i.score),
        by_verdict=lambda *verdicts: sorted((i for i in items if i.verdict in verdicts), key=lambda i: -i.score),
    )


def _engine(tmp_path: Path, calendar, now: str = "2026-11-13 18:00"):
    db = Database(tmp_path / "alerts.sqlite3")
    calendar.set_now(now)
    db.upsert_prices([{"symbol": "CALM", "date": day.strftime("%Y-%m-%d"), "open": 500, "high": 505, "low": 495, "close": 500, "volume": 10000, "turnover": 5e6, "trades": 50}
                      for day in DAYS[-30:]], "test")
    return SimpleNamespace(db=db, calendar=calendar, fees=FEES), db


# ============================================================ end-of-day: top pick
def test_top_pick_needs_every_rule() -> None:
    assert rules.top_pick_failures(_item(), False, []) == []
    cases = {
        "score": _item(score=79.0),
        "full data": _item(confidence="Medium"),
        "calm": _item(plan=_plan(avg_range_percent=2.6)),
        "sector": _item(sector_label="weak"),
    }
    for word, item in cases.items():
        failures = rules.top_pick_failures(item, False, [])
        assert len(failures) == 1 and word in failures[0], (word, failures)
    assert "overheated" in rules.top_pick_failures(_item(), True, [])[0]
    assert "price-adjusting event" in rules.top_pick_failures(_item(), False, ["right shares 0.5 per share held"])[0]
    assert rules.top_pick_failures(_item(verdict="BUY"), False, []), "the top pick is for SPECULATIVE candidates only"


def test_top_pick_is_the_single_best_or_none() -> None:
    good, better, noisy = _item("AAA", 82.0), _item("BBB", 88.0), _item("CCC", 95.0, plan=_plan(avg_range_percent=4.0))
    assert rules.choose_top_pick([good, better, noisy], False, {}).symbol == "BBB"
    assert rules.choose_top_pick([good, better], False, {"BBB": ["book closure 2026-12-01"]}).symbol == "AAA"
    assert rules.choose_top_pick([noisy], False, {}) is None, "zero top picks is fine"
    assert rules.choose_top_pick([good, better], True, {}) is None


def test_evening_sends_one_top_pick_and_respects_the_ten_day_rule(tmp_path: Path, calendar) -> None:
    engine, db = _engine(tmp_path, calendar)
    scan = _scan([_item("CALM", 84.0), _item("SECOND", 82.0)])
    assert service.evening_due(db, scan, calendar)
    out = service.evening_alerts(engine, scan)
    assert [(o.kind, o.symbol) for o in out] == [("top_pick", "CALM")], "at most one top pick a day"
    assert "TOP PICK (speculative, not proven)" in out[0].text and out[0].button == ("CALM", 160, 500.0)
    assert not service.evening_due(db, scan, calendar), "never sent twice for the same day"
    # the next trading day the same plan is NOT alerted again; the second-best stock may be
    db.upsert_prices([{"symbol": "CALM", "date": "2026-11-16", "open": 500, "high": 505, "low": 495, "close": 500, "volume": 1, "turnover": 5e6, "trades": 5}], "test")
    calendar.set_now("2026-11-16 18:00")
    again = service.evening_alerts(engine, _scan([_item("CALM", 84.0)], as_of="2026-11-16"))
    assert again == [], "no repeat within 10 trading days"
    # ... unless the plan changed materially (entry moved more than 3%)
    changed = _item("CALM", 84.0, plan=_plan(entry=520.0, entry_low=515.0, entry_high=525.0, amo_limit=520.0, stop=489.0, target_1=585.0))
    db.set_setting(service.EVENING_DONE, "")
    assert [o.kind for o in service.evening_alerts(engine, _scan([changed], as_of="2026-11-16"))] == ["top_pick"]


def test_no_repeat_rule() -> None:
    plan = rules.plan_fingerprint("SPECULATIVE", _plan())
    assert rules.may_alert_again(None, None, plan)
    assert not rules.may_alert_again(plan, 9, plan)
    assert rules.may_alert_again(plan, 10, plan), "after 10 trading days it may be alerted again"
    assert rules.may_alert_again(plan, 2, {**plan, "stop": 450.0}), "stop moved 4%: a material change"
    assert not rules.may_alert_again(plan, 2, {**plan, "entry": 505.0}), "a 1% move is not"
    assert rules.may_alert_again(plan, 2, {**plan, "verdict": "BUY"}) and rules.may_alert_again(plan, 2, {**plan, "setup": "rs_leader"})


def test_alert_types_can_be_switched_off(tmp_path: Path, calendar) -> None:
    engine, db = _engine(tmp_path, calendar)
    assert all(alert_config.enabled(db, name) for name in alert_config.TYPES), "all on by default"
    alert_config.set_enabled(db, "toppick", False)
    assert service.evening_alerts(engine, _scan([_item()])) == []
    shown = text.format_alert_settings(db)
    assert "<b>toppick</b>: off" in shown and "<b>strong</b>: ON" in shown and "not backtested" in shown and "last 2 years" in shown


def test_buy_grade_gets_the_normal_evening_alert(tmp_path: Path, calendar, monkeypatch) -> None:
    engine, db = _engine(tmp_path, calendar)
    out = service.evening_alerts(engine, _scan([_item("PROVEN", 80.0, verdict="STRONG BUY"), _item("ALSO", 70.0, verdict="BUY")]))
    assert [(o.kind, o.symbol) for o in out] == [("buy", "PROVEN"), ("buy", "ALSO")]
    assert "STRONG BUY" in out[0].text and "After-market order (AMO) tonight for 2026-11-16: limit price 500" in out[0].text


def test_setup_promotion_and_demotion_are_announced_once(tmp_path: Path, calendar, monkeypatch) -> None:
    _, db = _engine(tmp_path, calendar)
    assert service.setup_changes(db) == [], "the first run only remembers the state"
    proven = SimpleNamespace(proven=True, lines=lambda: ["recent: 60 trades, 55% won"])
    monkeypatch.setattr(evidence, "for_setup", lambda key: proven if key == "rs_leader" else evidence.Evidence(key, None, None))
    out = service.setup_changes(db)
    assert len(out) == 1 and "promoted to BUY" in out[0].text and "Relative-strength leader" in out[0].text
    assert service.setup_changes(db) == [], "one-time message"
    monkeypatch.setattr(evidence, "for_setup", lambda key: evidence.Evidence(key, None, None))
    assert "demoted" in service.setup_changes(db)[0].text


# ================================================================ early alerts
def _early(**changes) -> rules.EarlyState:
    now = datetime(2026, 11, 16, 13, 0)
    samples = tuple((now - timedelta(minutes=5 * i), 306.0) for i in range(8, -1, -1))     # 40 minutes of checks at 306
    values = dict(
        symbol="BRK", trigger=300.0, price=306.0, samples=samples, now=now, session_open=datetime(2026, 11, 16, 11, 0),
        session_close=datetime(2026, 11, 16, 15, 0), volume_today=120_000.0, normal_volume=100_000.0, day_change_percent=3.0,
        price_limit_percent=15.0, index_change_percent=0.2,
    )
    values.update(changes)
    return rules.EarlyState(**values)


def test_early_alert_passes_only_when_every_strict_rule_holds() -> None:
    assert rules.early_failures(_early()) == []       # 120k by half-time = 2.4x a normal day


def test_early_alert_filters() -> None:
    now = datetime(2026, 11, 16, 13, 0)
    spike = tuple((now - timedelta(minutes=5 * i), 306.0 if i < 3 else 298.0) for i in range(8, -1, -1))          # above for 10 minutes only
    gap = ((now - timedelta(minutes=40), 306.0), (now, 306.0))                                                 # the PC slept in between
    cases = {
        "too early": _early(now=datetime(2026, 11, 16, 12, 10), samples=tuple((datetime(2026, 11, 16, 12, 10) - timedelta(minutes=5 * i), 306.0) for i in range(8, -1, -1)), volume_today=90_000.0),
        "not 1% above": _early(price=302.0),
        "spike": _early(samples=spike),
        "has not held": _early(samples=gap),
        "above the buy range": _early(price=313.0, samples=tuple((m, 313.0) for m, _ in _early().samples)),
        "volume is on pace": _early(volume_today=80_000.0),
        "already up": _early(day_change_percent=7.5),
        "price limit": _early(day_change_percent=13.5),
        "falling sharply": _early(index_change_percent=-1.2),
        "daily limit": _early(sent_today=2),
        "already alerted": _early(already_alerted=True),
        "not open": _early(now=datetime(2026, 11, 16, 15, 30)),
        "volume is unknown": _early(volume_today=None),
    }
    for word, state in cases.items():
        failures = rules.early_failures(state)
        assert failures and any(word in failure for failure in failures), (word, failures)


def test_early_alert_confirm_and_cancel_flow(market, monkeypatch) -> None:  # noqa: F811
    engine, db = market
    monkeypatch.setattr(handlers, "_engine", engine)
    scan = engine.scan(use_cache=False)
    watcher = service.EarlyWatcher()
    # a watch-list stock with a trigger; only proven setups get early alerts
    item = _item("FLAT", 75.0, verdict="WATCH", plan=None, trigger_level=305.0, trigger_setup="base_breakout", close=303.0, setup_name=None)
    scan.results["FLAT"] = item
    monkeypatch.setattr(scan, "by_verdict", lambda *verdicts: [item] if "WATCH" in verdicts else [], raising=False)
    assert watcher.candidates(scan) == [], "speculative setups get no early alert"
    monkeypatch.setattr(evidence, "for_setup", lambda key: SimpleNamespace(proven=True, lines=lambda: ["recent: 60 trades"], recent=None, earlier=None, priority=1))
    assert watcher.candidates(scan) == [item]

    next_day = engine.calendar.next_trading_day(DAYS[-1].date())
    quote = {"FLAT": {"price": 309.0, "volume": 400_000.0, "previous_close": 303.0, "change_percent": 2.0}}
    out = []
    for minutes in range(0, 45, 5):                      # 12:25 ... 13:05: the price holds 1.3% above the trigger
        now = datetime.combine(next_day, datetime.min.time()).replace(hour=12, minute=25, tzinfo=engine.calendar.tz) + timedelta(minutes=minutes)
        engine.calendar.frozen_now = now
        out += watcher.tick(engine, scan, quote, 0.1, now)
    assert len(out) == 1 and out[0].kind == "early", "one early alert, after 30 minutes above the trigger and after 12:30"
    message = out[0].text
    assert "EARLY ALERT — not confirmed until close" in message and "Buy now up to" in message and "Buy only a part now" in message
    today = next_day.isoformat()
    pending = db.query("SELECT status, detail_json FROM alerts WHERE kind = 'early'")
    assert [row["status"] for row in pending] == ["pending"]
    rows = journal.live_results(db)
    assert [row["setup"] for row in rows if row["setup"] == journal.EARLY_SETUP] == [journal.EARLY_SETUP], "early alerts are tracked separately in /report"
    assert "EARLY alerts" in reports.format_live_report(rows, {})
    assert watcher.tick(engine, scan, quote, 0.1, now + timedelta(minutes=5)) == [], "not sent twice"

    # ---- evening, case 1: closed above the trigger -> CONFIRMED with the final plan and the AMO price
    stop = json.loads(pending[0]["detail_json"])["stop"]
    confirmed_scan = _scan([_item("FLAT", 78.0, verdict="WATCH", plan=None, close=310.0)], as_of=today)
    out = service.evening_alerts(engine, confirmed_scan)
    assert [o.kind for o in out] == ["confirmed"] and "<b>CONFIRMED</b>" in out[0].text and "After-market order (AMO)" in out[0].text
    assert db.query("SELECT status FROM alerts WHERE kind = 'early'")[0]["status"] == "confirmed"
    # ---- evening, case 2: closed below the trigger -> CANCELLED, with what to do if already bought
    with db.transaction() as connection:
        connection.execute("UPDATE alerts SET status = 'pending' WHERE kind = 'early'")
        connection.execute("DELETE FROM alerts WHERE kind = 'confirmed'")
    out = service.evening_alerts(engine, _scan([_item("FLAT", 70.0, verdict="WATCH", plan=None, close=302.0)], as_of=today))
    assert [o.kind for o in out] == ["cancelled"]
    assert "CANCELLED — closed below the trigger" in out[0].text and f"hold what you have with the stop at {stop:,.1f}" in out[0].text
    assert rules.confirm_early(305.0, 280.0, 290.0).advice.count("exit at the next session") == 1


def test_stale_data_never_gives_an_early_alert(market, monkeypatch) -> None:  # noqa: F811
    engine, db = market
    scan = engine.scan(use_cache=False)
    late = datetime.combine(DAYS[-1].date() + timedelta(days=7), datetime.min.time()).replace(hour=13, tzinfo=engine.calendar.tz)
    assert service.EarlyWatcher().tick(engine, scan, {"BRK": {"price": 400.0}}, 0.0, late) == [], "the scan is a week old"
    engine.calendar.frozen_now = late
    assert not service.evening_due(db, scan, engine.calendar, late), "old evening alerts are not sent during a later session"


# ================================================================ plan content
def test_every_alert_carries_the_complete_plan() -> None:
    scan = _scan([_item()])
    for kind in ("TOP PICK", "BUY", "STRONG BUY", "CONFIRMED", "EARLY"):
        extra = {"live_price": 502.0, "trigger": 495.0} if kind == "EARLY" else {"next_session": "2026-11-16"}
        data = text.from_item(kind, _item(), scan, events=["5% bonus shares - book closure 2026-12-01"], **extra)
        message = text.format_alert(data, FEES)
        for expected in (
            "Why CALM now", "Score 84/100", "Setup: Trend pullback", "Sector: Hydro Power (neutral)", "Market: NEUTRAL",
            "Buy range: 495–505", "160 shares = Rs 80,000 invested", "At risk if the stop is hit: Rs 5,400",
            "470 (-6.0%)", "Once the price reaches", "move the stop to", "Reference target 560 (+12.0%)", "net Rs", "Partial profit: at 560 sell 80 shares",
            "Typical outcome", "Expected holding period", "half of the trades ended within", "Time limit: 40 trading days",
            "Exit rules", "Sell everything", "Events in the holding window", "5% bonus shares", "honest record", "/bought CALM",
            "Not financial advice",
        ):
            assert expected in message, (kind, expected)
        if kind == "EARLY":
            assert "Buy now up to 505" in message and "⚠️ EARLY" in message and "/bought CALM 80 502" in message
        else:
            assert "After-market order (AMO) tonight for 2026-11-16: limit price 500" in message and "Don't buy above 505" in message
            assert "If it opens above 505" in message and "/bought CALM 160 500" in message
        assert "⚠️ SPECULATIVE" in message
    assert "TOP PICK (speculative, not proven)" in text.format_alert(text.from_item("TOP PICK", _item(), scan), FEES)
    test_message = text.format_alert(text.from_item("TOP PICK", _item(), scan, test=True), FEES)
    assert test_message.startswith("🔧 <b>TEST")


def test_trailing_rule_is_given_in_plain_numbers() -> None:
    lines = text.trailing_examples(_plan())          # stop 470, trail distance 3 x 12 = 36
    assert "Rs 36 below the highest price" in lines[0] and "passes 506" in lines[1]
    assert any("reaches 560, move the stop to 524" in line for line in lines)


# ============================================================ Part A: the fixes
def test_a_candidate_without_upside_becomes_a_watch_with_a_breakout_trigger(market, monkeypatch) -> None:  # noqa: F811
    engine, db = market
    monkeypatch.setattr(evidence, "for_setup", lambda key: evidence.Evidence(key, None, None))
    scan = engine.scan(use_cache=False)
    # The reference target is information only: a close resistance keeps the plan and the grade,
    # adds a warning and ranks the stock slightly lower.
    assert scan.buys
    for item in scan.buys:
        close_above = item.plan.reward_risk < 1.5
        assert any(note.startswith("⚠️ Resistance close above at") for note in item.notes) == close_above
        plain = item.score * {"High": 1.0, "Medium": 0.92, "Low": 0.8}[item.confidence]
        assert item.rank_value == pytest.approx(plain * (0.97 if close_above else 1.0))
    from nepse_bot.engine import config as engine_config_module

    original = engine_config_module.setting
    monkeypatch.setattr("nepse_bot.engine.engine.setting", lambda *keys, default=None: 50.0 if keys == ("plan", "min_reward_risk") else original(*keys, default=default))
    strict = engine.scan(use_cache=False)
    item = strict.results["BRK"]
    assert item.verdict == "WATCH" and item.plan is None and item.blocked_plan is not None
    assert "Buy only on a close above" in item.watch_trigger and "normal volume" in item.watch_trigger
    assert item.trigger_level == item.blocked_plan.target_1
    top10, _ = reports.format_top10(strict)
    assert "BRK" in top10 and top10.index("without an entry today") < top10.index("<b>BRK</b>"), "ranked lower: under watch, not as a candidate"
    assert "BRK" not in reports.format_buy_page(strict)[0]


def test_partial_profit_level_is_the_reference_target_everywhere() -> None:
    plan = _plan()
    targets = exits.targets_for(plan, FEES)
    assert targets.partial.price == targets.resistance.price == 560.0
    assert exits.partial_level(500.0, 560.0)[0] == 560.0
    page = reports.target_lines(plan, "trailing")
    assert any("Consider booking partial profit</b> at 560" in line for line in page)
    close_target = exits.partial_level(500.0, 510.0)
    assert close_target[0] == 510.0 and "close to the entry" in close_target[1], "never a different price from the target shown"


def test_old_purchase_date_sets_the_tax_rate_and_levels_use_current_prices(tmp_path: Path) -> None:
    db = Database(tmp_path / "p.sqlite3")
    positions.record_buy(db, "HRL", 21, 100.0, "2024-03-14", FEES)
    sale = positions.record_sell(db, "HRL", 21, 470.0, "2026-11-13", FEES)
    assert sale["held_days"] > 365 and sale["tax_percent"] == FEES.cgt_long_percent == 3.75
    positions.record_buy(db, "NEW", 100, 100.0, "2026-10-01", FEES)
    assert positions.record_sell(db, "NEW", 100, 120.0, "2026-11-13", FEES)["tax_percent"] == FEES.cgt_short_percent

    # bought long ago at 100, now around 470: the stop and target come from recent prices, not from 100
    closes = [100.0 + i * 1.5 for i in range(250)]                # rises to about 473
    bars = _position_bars(closes)
    position = {"id": 1, "symbol": "HRL", "quantity": 21, "avg_price": 100.0, "opened_at": bars.index[5], "stop_loss": None, "target_1": 150.0,
                "notes": json.dumps({"levels_from": bars.index[-1]})}
    view = positions.review_position(position, bars, today=TODAY, style="trailing", fees=FEES, profile=PROFILE)
    assert view.trailing_stop > 400 and view.trailing_stop < view.close, "trails the highest price since purchase"
    assert view.highest == pytest.approx(float(bars["high"].max()))
    assert view.target is not None and view.target > view.close, "the reference target is above the CURRENT price, not the stored 150"
    assert view.days_held > 300


def test_unknown_purchase_date_is_asked_for_not_assumed(tmp_path: Path) -> None:
    db = Database(tmp_path / "u.sqlite3")
    assert positions.import_legacy_holdings(db, {"OLD": (50, 200.0)}, FEES) == ["OLD"]
    assert positions.import_legacy_holdings(db, {"OLD": (50, 200.0)}, FEES) == [], "imported once"
    row = positions.get_position(db, "OLD")
    assert row["opened_at"] is None
    bars = _position_bars([200.0 + i * 0.2 for i in range(60)])
    view = positions.review_position(row, bars, today=TODAY, style="trailing", fees=FEES, profile=PROFILE)
    assert view.date_unknown and any("/bought date OLD" in note for note in view.notes)
    assert positions.set_purchase_date(db, "OLD", "2025-01-15")
    row = positions.get_position(db, "OLD")
    assert row["opened_at"] == "2025-01-15" and not positions.review_position(row, bars, today=TODAY, style="trailing", fees=FEES, profile=PROFILE).date_unknown


def test_bought_rejects_a_non_trading_day_and_accepts_a_date(market) -> None:  # noqa: F811
    engine, db = market
    saturday = (DAYS[-1].date() - timedelta(days=DAYS[-1].weekday() + 2)).isoformat()      # the Saturday before the last week
    refusal, _ = manage.check_trade_day(db, engine.calendar, "UP", saturday, 200.0)
    assert refusal and "not a trading day" in refusal and "nearest trading days" in refusal
    good = DAYS[-40].strftime("%Y-%m-%d")
    price = float(db.scalar("SELECT close FROM daily_prices WHERE symbol = 'UP' AND date = ?", (good,)))
    assert manage.check_trade_day(db, engine.calendar, "UP", good, price) == (None, [])
    _, warnings = manage.check_trade_day(db, engine.calendar, "UP", good, price * 3)
    assert warnings and "outside" in warnings[0]
    assert manage.parse_trade(["HRL", "21", "100", "2024-03-15"], TODAY) == ("HRL", 21, 100.0, "2024-03-15")


def test_known_corporate_events_stay_listed_however_old(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.sqlite3")
    db.upsert("corporate_actions", [
        {"symbol": "NICA", "action_type": "right", "fiscal_year": "", "ratio": 0.5, "amount": 100.0, "status": "ComingSoon", "source_id": "r1"},
    ], ("symbol", "action_type", "fiscal_year", "source_id"))
    db.upsert("corporate_actions", [
        {"symbol": "API", "action_type": "bonus", "fiscal_year": "2082/2083", "ratio": 5.0, "announcement_date": "2026-11-05", "book_closure_date": "2026-11-24", "status": "Open", "source_id": "b1"},
        {"symbol": "DONE", "action_type": "bonus", "fiscal_year": "2081/2082", "ratio": 10.0, "announcement_date": "2026-01-05", "book_closure_date": "2026-02-01", "status": "Closed", "source_id": "b2"},
    ], ("symbol", "action_type", "fiscal_year", "source_id"))
    db.upsert("news", [
        {"symbol": "XYZ", "title": "Right Share approval by SEBON [XYZ]", "source": "ShareHub", "url": "u1", "published_at": "2026-09-01T10:00:00", "kind": "announcement"},
        {"symbol": "XYZ", "title": "Minutes of 12th Annual General Meeting [XYZ]", "source": "ShareHub", "url": "u2", "published_at": "2026-11-10", "kind": "announcement"},
    ], ("source", "url", "title"))
    found = {item.symbol: item for item in events.corporate_events(db, "2026-11-14", prices={"NICA": 341.0, "API": 333.0})}
    assert set(found) == {"NICA", "API", "XYZ"}, "a finished book closure and old meeting minutes are left out"
    assert "right shares 0.5 per share held (50%)" in found["NICA"].line() and found["NICA"].price_after == pytest.approx((341 + 50) / 1.5, abs=0.1)
    assert found["API"].closure_day == "2026-11-24" and found["API"].price_after == pytest.approx(333 / 1.05, abs=0.1)
    assert found["XYZ"].kind == "right" and found["XYZ"].announced == "2026-09-01", "10 weeks old and still listed"
    assert events.expected_adjustment(500.0, cash_percent=10.0, bonus_percent=5.0) == pytest.approx(490 / 1.05, abs=0.1)
    stock_text = briefs.format_calendar([], set(), [], [], "2026-11-14", list(found.values()), {})
    assert "book closure date not fixed yet" in stock_text and "NICA" in stock_text


def test_others_sector_is_compared_with_the_whole_market(market) -> None:  # noqa: F811
    engine, db = market
    rows = []
    for i, symbol in enumerate(["UP", "FLAT", "MID0", "MID1", "MID2", "MID3", "MID4", "MID5"]):
        rows.append({"symbol": symbol, "fiscal_year": "2082/2083", "quarter": 4, "period_end_date": "2026-07-16", "publication_date": "2026-08-20",
                     "eps": 10.0 + i, "eps_annualized": 10.0 + i, "book_value": 150.0})
    db.upsert("fundamentals", rows, ("symbol", "fiscal_year", "quarter"))
    db.upsert("fundamentals", [{"symbol": "BRK", "fiscal_year": "2082/2083", "quarter": 3, "period_end_date": "2026-04-13", "publication_date": "2026-05-15",
                                "eps": 20.0, "eps_annualized": 20.0, "book_value": 150.0}], ("symbol", "fiscal_year", "quarter"))
    with db.transaction() as connection:
        connection.execute("UPDATE securities SET sector = 'Others' WHERE symbol IN ('UP', 'FLAT', 'MID0', 'MID1', 'MID2')")
    scan = engine.scan(use_cache=False)
    others, hydro = scan.results["UP"].fundamentals, scan.results["MID4"].fundamentals
    assert others["median_scope"] == "market" and hydro["median_scope"] == "market", "'Others', and a sector with fewer than 5 comparable companies"
    assert others["pe_median"] == hydro["pe_median"]
    assert "all-market median" in reports._fundamentals_line(scan.results["UP"])
    assert scan.results["BRK"].fundamentals["behind_newest_quarter"] and "newest published report" in reports._fundamentals_line(scan.results["BRK"])


def test_plain_questions_map_to_the_right_command() -> None:
    known = {"NABIL", "NICA", "UP", "HRL"}
    assert ask.route("why is NABIL falling", known) == ("why", "NABIL")
    assert ask.route("Why is nica rising?", known) == ("why", "NICA")
    assert ask.route("news NABIL", known) == ("news", "NABIL")
    assert ask.route("what should I watch today", known) == ("watch", None)
    assert ask.route("how is the market", known) == ("market", None)
    assert ask.route("NABIL", known) == ("stock", "NABIL")
    assert ask.route("thanks a lot my friend, see you later", known) is None
    item = _item("NABIL", 72.0, verdict="WATCH", change_percent=-2.4, negatives=["RSI 38: weak momentum"], plan=None)
    answer = ask.explain_move(item, _scan([item]), {"roc5": -5.0, "roc20": 1.0}, ["Dividend / bonus announced (11-12)"])
    for expected in ("fell 2.4%", "-5.0% over 5 days", "RSI 38", "Dividend / bonus announced", "/stock NABIL", "cannot know why"):
        assert expected in answer, expected


# ========================================================= alerts for holdings
def test_holding_alerts_once_per_event_per_day(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite3")
    closes = [200.0 + i for i in range(60)] + [259.0, 240.0]              # a sharp drop below the trailing stop
    bars = _position_bars(closes)
    position = {"id": 7, "symbol": "DROP", "quantity": 100, "avg_price": 220.0, "opened_at": bars.index[20], "stop_loss": None, "target_1": None}
    view = positions.review_position(position, bars, today=TODAY, style="trailing", fees=FEES, profile=PROFILE)
    assert [key for key, _ in positions.position_events(view)] == ["stop_hit"]
    first = service.position_alerts(db, [view], TODAY)
    assert len(first) == 1 and "STOP HIT" in first[0]
    assert service.position_alerts(db, [view], TODAY) == [], "max one alert per stock per event per day"

    rising = _position_bars([200.0 + i for i in range(60)])
    held = {"id": 8, "symbol": "RISE", "quantity": 100, "avg_price": 205.0, "opened_at": rising.index[5], "stop_loss": None, "target_1": 250.0,
            "notes": json.dumps({"levels_from": rising.index[5]})}
    view = positions.review_position(held, rising, today=TODAY, style="trailing", fees=FEES, profile=PROFILE)
    keys = [key for key, _ in positions.position_events(view)]
    assert "target" in keys and "raise_stop" in keys and "time_limit" not in keys
    assert [key for key, _ in positions.position_events(view, last_notified_stop=view.trailing_stop)] == ["target"], "the stop is only announced when it rises"
    live = positions.position_events(view, live_price=view.trailing_stop * 1.01)
    assert [key for key, _ in live] == ["stop_near"] and "close to its stop" in live[0][1]
    assert [key for key, _ in positions.position_events(view, live_price=view.trailing_stop * 0.99)] == ["stop_hit"]
    late = positions.review_position({**held, "opened_at": rising.index[-37]}, rising, today=TODAY, style="trailing", fees=FEES, profile=PROFILE)
    assert "time_limit" in [key for key, _ in positions.position_events(late)]


def test_reference_target_never_filters_a_top_pick_but_ranks_it_lower() -> None:
    near = _item(score=85.0, plan=_plan(reward_risk=1.2))
    far = _item(score=84.0, plan=_plan(reward_risk=2.0))
    assert rules.top_pick_failures(near, False, []) == []            # a close resistance does not disqualify
    assert rules.choose_top_pick([near, far], False, {}) is far      # 85 x 0.97 < 84
    assert rules.choose_top_pick([near], False, {}) is near
    data = text.from_item("TOP PICK", near, SimpleNamespace(regime=SimpleNamespace(label="NEUTRAL", overheated=False)))
    message = text.format_alert(data, fee_schedule())
    assert "⚠️ Resistance close above at 560 (+12.0%): upside may be limited until it breaks above 560" in message
