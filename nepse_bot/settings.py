"""Project paths and YAML configuration loading.

Everything that locates a file on disk goes through this module so the bot
behaves the same on Windows and Linux and never depends on the folder it was
started from.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(os.getenv("NEPSE_BOT_HOME") or Path(__file__).resolve().parent.parent)
CONFIG_DIR = Path(os.getenv("NEPSE_BOT_CONFIG_DIR") or PROJECT_ROOT / "config")
DATA_DIR = Path(os.getenv("NEPSE_BOT_DATA_DIR") or PROJECT_ROOT / "data")
LOG_DIR = Path(os.getenv("NEPSE_BOT_LOG_DIR") or PROJECT_ROOT / "logs")
CHART_DIR = Path(os.getenv("NEPSE_BOT_CHART_DIR") or PROJECT_ROOT / "charts")

MARKET_CONFIG_PATH = CONFIG_DIR / "market.yaml"
HOLIDAYS_CONFIG_PATH = CONFIG_DIR / "holidays.yaml"
BOT_CONFIG_PATH = CONFIG_DIR / "bot.yaml"


def load_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML file and return its top-level mapping.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file does not contain a mapping.
    """
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


@lru_cache(maxsize=1)
def bot_config() -> dict[str, Any]:
    """Return the bot behaviour settings from ``config/bot.yaml``."""
    try:
        return load_yaml(BOT_CONFIG_PATH)
    except FileNotFoundError:
        return {}


def bot_setting(*keys: str, default: Any = None) -> Any:
    """Look up a nested setting, e.g. ``bot_setting("cache", "live_table_seconds")``."""
    node: Any = bot_config()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def parse_id_list(raw_value: str | None) -> set[int]:
    """Parse a comma or space separated list of Telegram numeric IDs."""
    ids: set[int] = set()
    for token in (raw_value or "").replace(",", " ").split():
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return ids
