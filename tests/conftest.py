"""Shared test setup.

Tests must never touch the real saved data or the real Telegram account, so the
data folder is redirected to a throw-away directory and dummy credentials are
set BEFORE any bot module is imported.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TEMP_HOME = Path(tempfile.mkdtemp(prefix="nepse_bot_tests_"))
os.environ["NEPSE_BOT_DATA_DIR"] = str(_TEMP_HOME / "data")
os.environ["NEPSE_BOT_LOG_DIR"] = str(_TEMP_HOME / "logs")
os.environ["NEPSE_BOT_CHART_DIR"] = str(_TEMP_HOME / "charts")
os.environ["TELEGRAM_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["TELEGRAM_CHAT_ID"] = "111"
os.environ["ALLOWED_USER_IDS"] = ""
os.environ["ALLOWED_CHAT_IDS"] = ""

import pytest  # noqa: E402

from nepse_bot.trading_calendar import TradingCalendar  # noqa: E402
from nepse_bot.utils import health  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

MARKET_YAML = """
timezone: Asia/Kathmandu
trading_days: [monday, tuesday, wednesday, thursday, friday]
trading_hours:
  open: "11:00"
  close: "15:00"
"""


class FrozenCalendar(TradingCalendar):
    """A calendar whose clock the test controls."""

    frozen_now: datetime | None = None

    def now(self) -> datetime:
        if self.frozen_now is None:
            return super().now()
        return self.frozen_now

    def set_now(self, text: str) -> None:
        """Set the clock, e.g. ``set_now("2026-11-18 12:00")`` (Nepal time)."""
        self.frozen_now = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)


@pytest.fixture()
def calendar(tmp_path: Path) -> FrozenCalendar:
    """A Monday-Friday calendar with an empty, writable holiday file."""
    market = tmp_path / "market.yaml"
    market.write_text(MARKET_YAML, encoding="utf-8")
    holidays = tmp_path / "holidays.yaml"
    holidays.write_text("holidays: []\n", encoding="utf-8")
    return FrozenCalendar(market_path=market, holidays_path=holidays)


@pytest.fixture(autouse=True)
def _clean_health() -> None:
    """Start every test with no recorded source health and no queued alerts."""
    health.SOURCE_HEALTH.reset()
    health.drain_admin_alerts()


def fixture_text(name: str) -> str:
    """Read one saved sample file."""
    return (FIXTURES / name).read_text(encoding="utf-8")
