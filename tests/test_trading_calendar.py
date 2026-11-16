"""Trading calendar: Monday-Friday, holidays, hours, scheduler days."""

from __future__ import annotations

from datetime import date

from nepse_bot.trading_calendar import TradingCalendar, get_calendar


def test_friday_is_open_and_sunday_is_closed(calendar) -> None:
    assert calendar.is_trading_day(date(2026, 11, 20))  # Friday
    assert calendar.is_trading_day(date(2026, 11, 16))  # Monday
    assert not calendar.is_trading_day(date(2026, 11, 22))  # Sunday
    assert not calendar.is_trading_day(date(2026, 11, 21))  # Saturday


def test_market_hours(calendar) -> None:
    calendar.set_now("2026-11-18 10:59")
    assert not calendar.is_market_open()
    calendar.set_now("2026-11-18 11:00")
    assert calendar.is_market_open()
    calendar.set_now("2026-11-18 15:00")
    assert calendar.is_market_open()
    calendar.set_now("2026-11-18 15:01")
    assert not calendar.is_market_open()
    calendar.set_now("2026-11-22 12:00")  # Sunday midday
    assert not calendar.is_market_open()


def test_holiday_closes_a_normal_trading_day(calendar) -> None:
    wednesday = date(2026, 11, 18)
    assert calendar.is_trading_day(wednesday)
    assert calendar.add_holiday(wednesday, "Test holiday") is True
    assert calendar.add_holiday(wednesday, "again") is False
    assert not calendar.is_trading_day(wednesday)
    calendar.set_now("2026-11-18 12:00")
    assert not calendar.is_market_open()
    assert "Test holiday" in calendar.status_label()
    assert calendar.remove_holiday(wednesday) is True
    assert calendar.is_trading_day(wednesday)


def test_holiday_file_edited_by_hand_is_picked_up(calendar) -> None:
    calendar.holidays()
    calendar.holidays_path.write_text(
        'holidays:\n  - {date: "2026-11-19", reason: "Typed by owner"}\n', encoding="utf-8"
    )
    import os

    os.utime(calendar.holidays_path, (1, 1))  # force a different modified-time
    assert not calendar.is_trading_day(date(2026, 11, 19))


def test_last_completed_trading_day(calendar) -> None:
    calendar.set_now("2026-11-18 12:00")  # Wednesday, session running
    assert calendar.last_completed_trading_day() == date(2026, 11, 17)
    calendar.set_now("2026-11-18 15:30")  # Wednesday after the close
    assert calendar.last_completed_trading_day() == date(2026, 11, 18)
    calendar.set_now("2026-11-22 09:00")  # Sunday -> Friday
    assert calendar.last_completed_trading_day() == date(2026, 11, 20)
    calendar.set_now("2026-11-23 09:00")  # Monday before the open -> Friday
    assert calendar.last_completed_trading_day() == date(2026, 11, 20)


def test_last_completed_trading_day_skips_holidays(calendar) -> None:
    calendar.add_holiday(date(2026, 11, 20), "Friday holiday")
    calendar.set_now("2026-11-23 09:00")
    assert calendar.last_completed_trading_day() == date(2026, 11, 19)


def test_scheduler_days_use_telegram_numbering(calendar) -> None:
    # python-telegram-bot counts Sunday as 0, so Monday-Friday is 1..5.
    assert calendar.scheduler_days() == (1, 2, 3, 4, 5)
    assert calendar.trading_day_names() == "Monday to Friday"


def test_real_config_is_monday_to_friday() -> None:
    real = get_calendar()
    assert isinstance(real, TradingCalendar)
    assert real.trading_weekdays == frozenset({0, 1, 2, 3, 4})
    assert real.open_time.strftime("%H:%M") == "11:00"
    assert real.close_time.strftime("%H:%M") == "15:00"


