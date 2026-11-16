"""Protection mode, /chart, /watchlist and the monthly re-test."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from nepse_bot.backtest import monthly
from nepse_bot.bot import briefs, chart
from nepse_bot.db.store import Database
from nepse_bot.portfolio import protection
from nepse_bot.storage.user_store import UserStore
from nepse_bot.trading_calendar import get_calendar
from test_engine import LAST, market  # noqa: F401  (fixtures)


# ------------------------------------------------------------ protection mode
def _sale(db: Database, number: int, realised: float, day: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO trades (position_id, symbol, side, trade_date, quantity, price, notes) VALUES (1, ?, 'sell', ?, 10, 100, ?)",
            (f"S{number}", day, json.dumps({"realised": realised})),
        )


def test_protection_mode_after_three_losses_pauses_top_picks_for_five_sessions(tmp_path: Path) -> None:
    db, calendar = Database(tmp_path / "p.sqlite3"), get_calendar()
    _sale(db, 1, +500, "2026-11-02")
    _sale(db, 2, -300, "2026-11-03")
    _sale(db, 3, -200, "2026-11-04")
    assert protection.check(db, "2026-11-04", calendar, 1_000_000) is None        # two losses: nothing
    _sale(db, 4, -100, "2026-11-05")
    warning = protection.check(db, "2026-11-05", calendar, 1_000_000)
    assert "Protection mode is ON" in warning and "last 3 sales all lost money" in warning
    until = date(2026, 11, 5)
    for _ in range(4):                                                            # the trigger day plus 4 more = 5 sessions
        until = calendar.next_trading_day(until)
    after = calendar.next_trading_day(until).isoformat()
    assert protection.paused_until(db, "2026-11-05") == until.isoformat()
    assert protection.check(db, "2026-11-06", calendar, 1_000_000) is None        # the same streak does not warn twice
    assert protection.paused_until(db, until.isoformat()) and protection.paused_until(db, after) is None
    assert f"paused until {until.isoformat()}" in protection.status_line(db, "2026-11-06")


def test_protection_mode_after_an_eight_percent_drop_from_the_peak(tmp_path: Path) -> None:
    db, calendar = Database(tmp_path / "p.sqlite3"), get_calendar()
    assert protection.check(db, "2026-11-02", calendar, 1_000_000) is None        # the peak is set at 1,000,000
    _sale(db, 1, -50_000, "2026-11-03")
    assert protection.check(db, "2026-11-03", calendar, 1_000_000) is None        # -5%
    _sale(db, 2, +10_000, "2026-11-04")
    _sale(db, 3, -45_000, "2026-11-04")
    warning = protection.check(db, "2026-11-04", calendar, 1_000_000)             # -8.5%
    assert warning and "8.5% below its highest value" in warning
    assert protection.check(db, "2026-11-05", calendar, 1_000_000) is None        # no repeat until a further 8% drop


# --------------------------------------------------------------------- /chart
def test_chart_is_a_png_with_stop_and_target_lines(market) -> None:  # noqa: F811
    _, db = market
    frame = chart.chart_frame(db, "BRK", LAST)
    assert {"ema20", "ema50", "ema200", "volume"} <= set(frame.columns) and len(frame) > 100
    plan = SimpleNamespace(stop=90.0, target_1=130.0, entry=100.0)
    levels = chart.chart_levels(SimpleNamespace(plan=plan, blocked_plan=None), None)
    assert levels == {"Stop": 90.0, "Reference target": 130.0, "Entry": 100.0}
    picture = chart.render_chart(frame, "BRK", levels, "SPECULATIVE, score 70")
    assert picture.startswith(bytes([0x89]) + b"PNG") and len(picture) > 20_000
    held = SimpleNamespace(trailing_stop=95.0, avg_price=101.0, target=120.0)
    assert set(chart.chart_levels(None, held)) == {"Trailing stop", "Your average price", "Reference target"}


# ----------------------------------------------------------------- /watchlist
def test_watchlist_is_stored_and_shown_in_the_morning_brief(market, tmp_path: Path) -> None:  # noqa: F811
    engine, _ = market
    store = UserStore(tmp_path / "users.json")
    store.add_watchlist_symbol(7, "brk")
    store.add_watchlist_symbol(7, "ZZZ")
    store.remove_watchlist_symbol(7, "ZZZ")
    assert store.iter_watchlist_symbols() == ["BRK"]
    scan = engine.scan(use_cache=False)
    text = briefs.format_morning_brief(scan, [], [], [], [], None, watchlist=["BRK", "NOPE"])
    assert "<b>Your watchlist</b>" in text and "<b>BRK</b>" in text and "NOPE</b>: no recent prices" in text


# ------------------------------------------------------------ monthly re-test
def test_monthly_retest_only_suggests_and_applies_on_request(tmp_path: Path, monkeypatch) -> None:
    good = "{period: p, trades: 80, win_rate: 50, avg_win_pct: 9, avg_loss_pct: -5, expectancy_pct: 2, profit_factor: 1.6}"
    bad = "{period: p, trades: 80, win_rate: 30, avg_win_pct: 7, avg_loss_pct: -6, expectancy_pct: -2, profit_factor: 0.5}"
    config_dir, folder = tmp_path / "config", tmp_path / "reports" / "monthly" / "2026-12"
    config_dir.mkdir()
    folder.mkdir(parents=True)

    def document(pullback: str, leader: str) -> str:
        return "\n".join(["setups:", "  trend_pullback:", f"    recent: {pullback}", f"    earlier: {good}",
                          "  rs_leader:", f"    recent: {leader}", f"    earlier: {good}", ""])

    (config_dir / "evidence.yaml").write_text(document(bad, good), encoding="utf-8")
    (folder / "evidence.yaml").write_text(document(good, bad), encoding="utf-8")
    monkeypatch.setattr(monthly, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(monthly, "run_backtest", lambda month, sample=False: folder)
    db = Database(tmp_path / "m.sqlite3")
    assert monthly.due(db, date(2026, 12, 1))
    text = monthly.run(db, date(2026, 12, 1))
    assert "PROMOTE Trend pullback" in text and "DEMOTE Relative-strength leader" in text and "Nothing has been changed" in text
    assert (config_dir / "evidence.yaml").read_text(encoding="utf-8") == document(bad, good)   # untouched until approved
    assert not monthly.due(db, date(2026, 12, 20)) and monthly.due(db, date(2027, 1, 2))
    assert "PROMOTE" in monthly.pending_text(db)
    assert monthly.apply(db).startswith("Applied")
    assert (config_dir / "evidence.yaml").read_text(encoding="utf-8") == document(good, bad)
    assert (folder / "evidence_before.yaml").exists() and monthly.apply(db).startswith("There is nothing to apply")
