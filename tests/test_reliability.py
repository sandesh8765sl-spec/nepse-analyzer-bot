"""Backups and restore, catch-up after sleep, keep-awake."""

from __future__ import annotations

import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from nepse_bot.bot import manage
from nepse_bot.db.store import Database
from nepse_bot.trading_calendar import get_calendar
from nepse_bot.utils import backup
from nepse_bot.utils.keep_awake import keep_awake
from test_engine import market  # noqa: F401  (fixture)


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    db = Database(data / backup.DATABASE_NAME)
    db.upsert_prices([{"symbol": "NABIL", "date": "2026-11-05", "close": 500, "volume": 10}], "test")
    db.upsert("broker_flow_daily", [{"date": "2026-11-05", "symbol": "NABIL", "broker": 58, "buy_qty": 10}], ("date", "symbol", "broker"))
    db.set_setting("owner.note", "keep me")
    db.close()
    (data / "user_state.json").write_text('{"users": {"7": {"watchlist": ["NABIL"]}}}', encoding="utf-8")
    return data


def test_backup_keeps_14_days_and_restore_never_deletes(tmp_path: Path) -> None:
    data, folder, config = _data(tmp_path), tmp_path / "backups", tmp_path / "config"
    config.mkdir()
    for day in range(1, 17):                                   # 16 daily backups
        result = backup.make_backup(date(2026, 11, day), data, folder)
    names = [path.name for path in backup.list_backups(folder)]
    assert len(names) == 14 and names[0].endswith("2026-11-03.zip") and names[-1].endswith("2026-11-16.zip")
    assert result["kept"] == 14 and not backup.backup_due(date(2026, 11, 16), folder) and backup.backup_due(date(2026, 11, 17), folder)
    with zipfile.ZipFile(folder / names[-1]) as archive:
        assert {"data/nepse.sqlite3", "data/user_state.json"} <= set(archive.namelist())

    # The owner's data changes after the backup, then the backup is restored.
    (data / "user_state.json").write_text("{}", encoding="utf-8")
    message = backup.restore("2026-11-16", data, folder, config)
    assert message.startswith("Restored")
    assert "NABIL" in (data / "user_state.json").read_text(encoding="utf-8")
    restored = Database(data / backup.DATABASE_NAME)
    assert restored.get_setting("owner.note") == "keep me" and restored.scalar("SELECT COUNT(*) FROM daily_prices") == 1
    assert restored.scalar("SELECT COUNT(*) FROM broker_flow_daily") == 0          # public data, left out on purpose
    aside = [path for path in data.iterdir() if path.name.startswith("before_restore_")]
    assert len(aside) == 1 and (aside[0] / "user_state.json").read_text(encoding="utf-8") == "{}"   # moved aside, not deleted
    assert backup.restore("2020-01-01", data, folder, config).startswith("No backup for 2020-01-01")


def test_backup_refuses_when_the_disk_is_nearly_full(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(backup, "free_megabytes", lambda path: 50.0)
    with pytest.raises(RuntimeError, match="Free some space"):
        backup.make_backup(date(2026, 11, 5), _data(tmp_path), tmp_path / "backups")


def test_missed_brief_and_evening_report_are_owed_only_while_still_useful(tmp_path: Path) -> None:
    db, calendar = Database(tmp_path / "t.sqlite3"), get_calendar()
    db.upsert_prices([{"symbol": "NABIL", "date": "2026-11-05", "close": 500, "volume": 10}], "test")   # Thursday's data is in

    def at(day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 11, day, hour, minute, tzinfo=calendar.tz)

    # Morning brief (10:15): owed from then until the close, once.
    assert not manage.morning_brief_due(db, calendar, at(5, 9))
    assert manage.morning_brief_due(db, calendar, at(5, 13))               # woke up at 13:00: still useful
    assert not manage.morning_brief_due(db, calendar, at(5, 15, 30))       # market closed: skip it
    assert not manage.morning_brief_due(db, calendar, at(7, 12))           # Saturday
    db.set_setting(manage.MORNING_SENT, "2026-11-05")
    assert not manage.morning_brief_due(db, calendar, at(5, 13))

    # Evening report (17:30): owed after the data is in, until the next session opens.
    assert not manage.evening_report_due(db, calendar, at(5, 16))          # too early, the update is running
    assert manage.evening_report_due(db, calendar, at(5, 22))              # woke up late in the evening
    assert manage.evening_report_due(db, calendar, at(6, 8))               # ... or early next morning
    assert not manage.evening_report_due(db, calendar, at(6, 12))          # next session is open: old news
    db.set_setting(manage.EVENING_SENT, "2026-11-05")
    assert not manage.evening_report_due(db, calendar, at(5, 22))
    # Friday evening without Friday's data: nothing to report yet.
    assert not manage.evening_report_due(db, calendar, at(6, 19))


def test_keep_awake_block_runs_and_releases() -> None:
    with keep_awake() as active:
        assert active in (True, False)


# ------------------------------------------------------- housekeeping and /selftest
def test_housekeeping_removes_only_old_rebuildable_files(tmp_path: Path) -> None:
    import os
    import time

    from nepse_bot.utils import maintenance

    charts = tmp_path / "charts"
    charts.mkdir()
    old, new = charts / "old.png", charts / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    long_ago = time.time() - 10 * 86400
    os.utime(old, (long_ago, long_ago))
    assert maintenance.prune_files(charts, "*.png", 7) == 1 and new.exists() and not old.exists()

    reports = tmp_path / "reports"
    for month in ("2026-10", "2026-11"):
        folder = reports / "monthly" / month
        folder.mkdir(parents=True)
        for name in ("signals.csv", "trades.csv", "SUMMARY.md", "SUGGESTIONS.md"):
            (folder / name).write_text("x", encoding="utf-8")
    assert maintenance.prune_monthly_tables(reports) == 2
    assert sorted(path.name for path in (reports / "monthly" / "2026-10").iterdir()) == ["SUGGESTIONS.md", "SUMMARY.md"]
    assert len(list((reports / "monthly" / "2026-11").iterdir())) == 4


def test_selftest_builds_every_message_type_and_reports_failures(market, monkeypatch) -> None:  # noqa: F811
    import asyncio

    from nepse_bot.bot import handlers, selftest
    from nepse_bot.storage.user_store import UserStore

    engine, _ = market
    monkeypatch.setattr(handlers, "_engine", engine)
    monkeypatch.setattr("nepse_bot.bot.personal.user_store", lambda: UserStore(Path(engine.db.path).parent / "users.json"))
    items = selftest.build_samples(engine.scan(use_cache=False))
    labels = [item.label.split()[0] for item in items]
    for wanted in ("/stock", "/top10", "/buy", "/positions", "/calendar", "/chart", "/brief", "/evening", "/report", "news", "position"):
        assert wanted in labels, wanted
    assert {"STRONG BUY alert", "BUY alert", "TOP PICK alert"} <= {item.label for item in items}
    failed = [f"{item.label}: {item.error}" for item in items if item.error and "alert" not in item.label]
    assert not failed, failed
    assert next(item for item in items if item.label.startswith("/chart")).photo

    class Bot:
        def __init__(self) -> None:
            self.texts, self.photos = [], 0

        async def send_message(self, chat_id, text, **kwargs) -> None:
            self.texts.append(text)

        async def send_photo(self, chat_id, photo, caption) -> None:
            self.photos += 1

    bot = Bot()
    items.append(selftest.Sample("broken thing", error="boom"))
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: asyncio.wait_for(asyncio.Event().wait(), 0) if False else _nothing())
    problems = asyncio.run(selftest.deliver(bot, 111, items))
    assert bot.photos == 1 and any("TEST 1/" in text for text in bot.texts)
    assert any("broken thing: boom" in problem for problem in problems) and "Self-test finished" in bot.texts[-1]


async def _nothing() -> None:
    return None
