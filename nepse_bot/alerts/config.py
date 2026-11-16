"""Alert settings: config/alerts.yaml (rules) and config/alerts_backtest.yaml (backtest counts)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from nepse_bot.settings import CONFIG_DIR, load_yaml

ALERTS_PATH = CONFIG_DIR / "alerts.yaml"
BACKTEST_PATH = CONFIG_DIR / "alerts_backtest.yaml"
TYPES = ("strong", "early", "toppick")
TYPE_TEXT = {
    "strong": "STRONG BUY / BUY alerts after the evening scan",
    "early": "EARLY alerts during the session, then CONFIRMED or CANCELLED in the evening",
    "toppick": "TOP PICK: at most one speculative candidate a day",
}


@lru_cache(maxsize=1)
def alerts_config() -> dict[str, Any]:
    try:
        return load_yaml(ALERTS_PATH)
    except (FileNotFoundError, ValueError):
        return {}


@lru_cache(maxsize=1)
def backtest_results() -> dict[str, Any]:
    try:
        return load_yaml(BACKTEST_PATH)
    except (FileNotFoundError, ValueError):
        return {}


def alert_setting(*keys: str, default: Any = None) -> Any:
    """Read one value from config/alerts.yaml, e.g. ``alert_setting("early", "max_per_day")``."""
    value: Any = alerts_config()
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return default if value is None else value


def enabled(db: Any, name: str) -> bool:
    """Is an alert type switched on? (/settings alerts, else the default in config/alerts.yaml)."""
    stored = db.get_setting(f"alerts.{name}") if db is not None else None
    if stored in ("on", "off"):
        return stored == "on"
    return bool(alert_setting("defaults", name, default=True))


def set_enabled(db: Any, name: str, on: bool) -> None:
    db.set_setting(f"alerts.{name}", "on" if on else "off")


def holding_stats(setup_key: str) -> dict[str, Any]:
    """Trading days backtest trades of a setup were held: median, p25, p75."""
    return ((backtest_results().get("holding") or {}).get(setup_key)) or {}
