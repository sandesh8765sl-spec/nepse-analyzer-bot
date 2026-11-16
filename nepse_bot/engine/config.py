"""Engine settings: config/engine.yaml plus the owner's profile from the database."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from nepse_bot.settings import CONFIG_DIR, load_yaml

ENGINE_CONFIG_PATH = CONFIG_DIR / "engine.yaml"

PROFILE_KEYS: dict[str, str] = {
    "total_capital": "Total trading capital (Rs)",
    "min_position": "Smallest position per stock (Rs)",
    "max_position": "Largest position per stock (Rs)",
    "max_open_positions": "Maximum open positions",
    "risk_percent": "Risk per trade (% of capital)",
    "risk_rupees": "Risk per trade (fixed Rs, overrides the %)",
}


@lru_cache(maxsize=1)
def engine_config() -> dict[str, Any]:
    """Return the engine settings."""
    return load_yaml(ENGINE_CONFIG_PATH)


def setting(*keys: str, default: Any = None) -> Any:
    """Look up a nested engine setting."""
    node: Any = engine_config()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


_disabled_setups: frozenset[str] = frozenset()


def set_disabled_setups(keys: Any) -> None:
    """Record the setups the live journal has switched off (see portfolio/journal.py)."""
    global _disabled_setups
    _disabled_setups = frozenset(keys)


def disabled_setups() -> frozenset[str]:
    """Setups switched off by the live journal, whatever the config file says."""
    return _disabled_setups


@dataclass(frozen=True)
class Profile:
    """The owner's capital and risk settings."""

    total_capital: float
    min_position: float
    max_position: float
    max_open_positions: int
    risk_percent: float
    risk_rupees: float | None

    @property
    def risk_budget(self) -> float:
        """Rupees the owner accepts losing on one trade."""
        if self.risk_rupees is not None and self.risk_rupees > 0:
            return float(self.risk_rupees)
        return self.total_capital * self.risk_percent / 100.0


def load_profile(db: Any | None = None) -> Profile:
    """Return the profile: database values (set with /settings) over config defaults."""
    values: dict[str, Any] = dict(engine_config().get("profile") or {})
    if db is not None:
        for key in PROFILE_KEYS:
            stored = db.get_setting(f"profile.{key}")
            if stored is not None and stored != "":
                values[key] = None if stored == "none" else float(stored)
    risk_rupees = values.get("risk_rupees")
    return Profile(
        total_capital=float(values.get("total_capital", 500000)),
        min_position=float(values.get("min_position", 50000)),
        max_position=float(values.get("max_position", 100000)),
        max_open_positions=int(float(values.get("max_open_positions", 5))),
        risk_percent=float(values.get("risk_percent", 2.0)),
        risk_rupees=float(risk_rupees) if risk_rupees not in (None, "") else None,
    )
