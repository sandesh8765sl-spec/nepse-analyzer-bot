"""Bot wiring: access control, commands, schedules, /status, freshness, logging."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationBuilder, ApplicationHandlerStop, CommandHandler

from nepse_bot.access import REFUSAL_TEXT, AccessControl
from nepse_bot.bot import app, handlers, personal
from nepse_bot.storage.user_store import StockAlert
from nepse_bot.utils import freshness
from nepse_bot.utils.health import SOURCE_HEALTH

TOKEN = "123456:TEST-TOKEN-NOT-REAL"
PROJECT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ access
def test_allowlist_logic(tmp_path: Path) -> None:
    access = AccessControl({111}, {111, -500}, refused_path=tmp_path / "refused.json")
    assert access.is_allowed(111, 111)
    assert access.is_allowed(999, -500), "anyone inside an allow-listed group chat is allowed"
    assert not access.is_allowed(999, 999)
    assert not access.is_allowed(None, None)


def test_stranger_is_refused_exactly_once_even_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "refused.json"
    access = AccessControl({111}, {111}, refused_path=path)
    assert access.should_send_refusal(999) is True
    assert access.should_send_refusal(999) is False
    restarted = AccessControl({111}, {111}, refused_path=path)
    assert restarted.should_send_refusal(999) is False
    assert restarted.refused_count() == 1


class _Message:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kwargs) -> None:
        self.replies.append(text)


def _update(user_id: int, chat_id: int, message: _Message) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message, callback_query=None,
    )


def test_guard_lets_the_owner_through_and_stops_strangers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app, "ACCESS", AccessControl({111}, {111}, refused_path=tmp_path / "refused.json"))
    owner_message = _Message()
    asyncio.run(app.access_guard(_update(111, 111, owner_message), None))  # no exception = allowed
    assert owner_message.replies == []
    stranger_message = _Message()
    for _ in range(3):
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(app.access_guard(_update(999, 999, stranger_message), None))
    assert stranger_message.replies == [REFUSAL_TEXT], "one polite refusal, then silence"


def _commands(application) -> set[str]:
    names: set[str] = set()
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                names |= set(handler.commands)
    return names


def test_guard_runs_first_and_the_command_list_is_the_new_short_one() -> None:
    application = ApplicationBuilder().token(TOKEN).build()
    app.register_handlers(application)
    assert min(application.handlers) == -1 and application.handlers[-1][0].callback is app.access_guard
    commands = _commands(application)
    expected = {
        "start", "help", "stock", "analyze", "analze", "plan", "top10", "buy", "watch", "market", "sector",
        "settings", "report", "portfolio", "alert", "alerts", "news", "status", "holiday", "dataupdate",
        "bought", "sold", "positions", "calendar", "brief", "evening", "chart", "watchlist", "retest", "selftest",
    }
    assert commands == expected, f"unexpected commands: {sorted(commands ^ expected)}"
    assert not (commands & set(app.REMOVED_COMMANDS)), "a removed command is still registered"


def test_removed_commands_point_to_their_replacement() -> None:
    message = _Message()
    message.text = "/strongbuy"
    asyncio.run(app.unknown_command(SimpleNamespace(effective_message=message), None))
    assert "removed" in message.replies[0] and "/buy" in message.replies[0]
    message = _Message()
    message.text = "/nonsense"
    asyncio.run(app.unknown_command(SimpleNamespace(effective_message=message), None))
    assert "/help" in message.replies[0]


def test_old_analysis_code_is_gone_so_there_is_only_one_brain() -> None:
    for old in ("strategy/strategy_manager.py", "advisor/market_scanner.py", "advisor/trade_plan.py", "engine/ta_engine.py"):
        assert not (PROJECT / "nepse_bot" / old).exists(), f"{old} should have been removed"
    main_source = (PROJECT / "main.py").read_text(encoding="utf-8")
    assert len(main_source.splitlines()) < 40, "main.py is now only an entry point"
    for path in (PROJECT / "nepse_bot" / "bot").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("StrategyManager", "MarketScanner", "fetch_ohlcv", ".weekday()"):
            assert forbidden not in text, f"{path.name} still uses {forbidden}"


# --------------------------------------------------------------- schedules
def _jobs() -> dict[str, object]:
    async def build() -> dict[str, object]:
        application = ApplicationBuilder().token(TOKEN).build()
        app.schedule_jobs(application)
        return {job.name: job for job in application.job_queue.jobs()}

    return asyncio.run(build())


def test_schedule_follows_the_calendar_and_old_scans_are_gone() -> None:
    jobs = _jobs()
    for name in ("data_update_1520", "data_update_1700", "data_catch_up_startup", "price_alert_monitor", "owner_alert_delivery", "housekeeping", "catch_up", "daily_backup", "monthly_retest"):
        assert name in jobs, name
    for gone in ("premarket_auto_signal_scan", "midmarket_auto_signal_scan", "latemarket_auto_signal_scan", "eod_auto_signal_scan",
                 "daily_nepse_scanner", "active_watchlist_monitor", "news_alert_monitor", "market_status_monitor"):
        assert gone not in jobs, f"{gone} belongs to the old system"
    for name in ("data_update_1520", "data_update_1700"):
        trigger = str(jobs[name].job.trigger)
        assert "mon" in trigger and "fri" in trigger and "sun" not in trigger and "sat" not in trigger


def test_jobs_do_nothing_on_a_holiday(monkeypatch) -> None:
    calls: list[str] = []

    async def job(_context) -> None:
        calls.append("ran")

    guarded = app.trading_day_only(job)
    monkeypatch.setattr(app.CALENDAR, "is_trading_day", lambda day=None: False)
    asyncio.run(guarded(None))
    assert calls == []
    monkeypatch.setattr(app.CALENDAR, "is_trading_day", lambda day=None: True)
    asyncio.run(guarded(None))
    assert calls == ["ran"]


# ------------------------------------------------------------------ as-of
def test_as_of_line_states(calendar) -> None:
    calendar.set_now("2026-11-18 16:00")  # Wednesday after the close
    assert "✓" in freshness.as_of_line(date(2026, 11, 18), "src", calendar)
    stale = freshness.as_of_line(date(2026, 11, 17), "src", calendar)
    assert stale.startswith("⚠️") and "OLDER" in stale and "2026-11-18" in stale
    assert freshness.as_of_line(None, "src", calendar).startswith("⚠️")
    calendar.set_now("2026-11-18 12:00")  # during the session
    assert "✓" in freshness.as_of_line(date(2026, 11, 17), "src", calendar), "yesterday's close is current before today's close"
    calendar.set_now("2026-11-23 09:00")  # Monday morning: Friday is still current
    assert "✓" in freshness.as_of_line(date(2026, 11, 20), "src", calendar)


def test_as_of_line_is_inserted_under_the_title() -> None:
    text = freshness.insert_after_title("Title\n\n- body", "As of: X")
    assert text.split("\n")[:2] == ["Title", "As of: X"]
    assert freshness.insert_after_title(text, "As of: X") == text, "never added twice"


# ----------------------------------------------------------------- /status
def test_status_report_sections() -> None:
    SOURCE_HEALTH.record_success("NEPSE prices", data_date=date(2026, 11, 13))
    SOURCE_HEALTH.record_failure("ShareHub fundamentals", "no values")
    report = app.build_status_report(None)
    for heading in ("BOT", "DATABASE", "DATA SOURCES", "SCHEDULED JOBS", "ERRORS"):
        assert heading in report
    assert "✅ NEPSE prices" in report and "❌ ShareHub fundamentals" in report
    assert "TEST-TOKEN" not in report, "no secrets in /status"
    assert app.format_uptime(__import__("datetime").timedelta(days=2, hours=3, minutes=5)) == "2d 3h 5m"


# ------------------------------------------------------------------ alerts
def test_price_alert_parsing_and_triggering() -> None:
    assert personal.parse_alert(["NABIL", "above", "600"], 549.0) == StockAlert("NABIL", "price", "above", 600.0)
    assert personal.parse_alert(["nabil", "below", "500"], 549.0) == StockAlert("NABIL", "price", "below", 500.0)
    assert personal.parse_alert(["NABIL", "600"], 549.0).operator == "above", "a level above the last close means 'above'"
    assert personal.parse_alert(["NABIL", "500"], 549.0).operator == "below"
    assert personal.parse_alert(["NABIL"], 549.0) is None and personal.parse_alert(["NABIL", "abc"], 549.0) is None
    above = StockAlert("NABIL", "price", "above", 600.0)
    assert personal.alert_hit(above, 600.0) and personal.alert_hit(above, 612.0) and not personal.alert_hit(above, 599.9)
    assert not personal.alert_hit(StockAlert("NABIL", "rsi", "above", 70.0), 999.0), "old alert types are no longer checked"
    assert "old alert type" in personal.format_alerts([StockAlert("NABIL", "breakout")])


def test_stage5_messages_are_scheduled_on_trading_days() -> None:
    jobs = _jobs()
    for name in ("morning_brief", "evening_report"):
        trigger = str(jobs[name].job.trigger)
        assert "mon" in trigger and "fri" in trigger and "sat" not in trigger and "sun" not in trigger
    assert "fri" in str(jobs["weekly_summary"].job.trigger)


def test_settings_aliases_cover_every_profile_key() -> None:
    from nepse_bot.engine.config import PROFILE_KEYS

    assert set(handlers.SETTING_ALIASES.values()) == set(PROFILE_KEYS)
    assert "/stock" in handlers.HELP_TEXT and "/top10" in handlers.HELP_TEXT and "never places orders" in handlers.HELP_TEXT


# ------------------------------------------------------------------ logging
def test_logging_rotates_at_five_megabytes(tmp_path: Path, monkeypatch) -> None:
    from logging.handlers import RotatingFileHandler

    from nepse_bot.utils import logging_setup

    monkeypatch.setattr(logging_setup, "PROJECT_ROOT", tmp_path)
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        log_path = logging_setup.configure_logging()
        found = [item for item in root.handlers if isinstance(item, RotatingFileHandler)]
        assert len(found) == 1 and found[0].maxBytes == 5 * 1024 * 1024 and found[0].backupCount == 5
        assert log_path.parent == tmp_path / "logs"
        assert logging.getLogger("httpx").level == logging.WARNING, "request URLs (which contain the token) stay out of the log"
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            if handler not in saved:
                try:
                    handler.close()
                except Exception:
                    pass
        for handler in saved:
            root.addHandler(handler)
