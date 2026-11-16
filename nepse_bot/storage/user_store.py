"""Persistent per-user state for watchlists, portfolios, and alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from nepse_bot.settings import DATA_DIR
from nepse_bot.utils.locking import synchronize_public_methods
from nepse_bot.utils.safe_json import atomic_write_json, load_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Holding:
    """One portfolio holding."""

    symbol: str
    quantity: int
    buy_price: float


@dataclass(frozen=True)
class StockAlert:
    """One user alert condition."""

    symbol: str
    kind: str
    operator: str | None = None
    value: float | None = None


@synchronize_public_methods
class UserStore:
    """JSON-backed storage for Telegram user state."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DATA_DIR / "user_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def iter_watchlist_symbols(self) -> list[str]:
        """Every symbol on anyone's personal watchlist (the bot has one owner)."""
        state = self._load()
        found = {str(symbol).strip().upper() for user in state.get("users", {}).values() for symbol in user.get("watchlist", [])}
        return sorted(symbol for symbol in found if symbol)

    def add_watchlist_symbol(self, user_id: int, symbol: str) -> None:
        """Add a symbol to the user's watchlist."""
        state = self._load()
        user = self._user_state(state, user_id)
        normalized = symbol.strip().upper()
        if normalized and normalized not in user["watchlist"]:
            user["watchlist"].append(normalized)
            user["watchlist"].sort()
        self._save(state)

    def remove_watchlist_symbol(self, user_id: int, symbol: str) -> None:
        """Remove a symbol from the user's watchlist."""
        state = self._load()
        user = self._user_state(state, user_id)
        normalized = symbol.strip().upper()
        user["watchlist"] = [item for item in user["watchlist"] if item != normalized]
        self._save(state)

    def get_watchlist(self, user_id: int) -> list[str]:
        """Return the user's watchlist."""
        state = self._load()
        return list(self._user_state(state, user_id)["watchlist"])

    def add_asset_symbol(self, user_id: int, symbol: str) -> None:
        """Add a symbol to the user's persistent local asset database."""
        state = self._load()
        user = self._user_state(state, user_id)
        normalized = symbol.strip().upper()
        if normalized and normalized not in user["asset_database"]:
            user["asset_database"].append(normalized)
            user["asset_database"].sort()
        self._save(state)

    def get_asset_symbols(self, user_id: int) -> list[str]:
        """Return saved symbols from the user's persistent local asset database."""
        state = self._load()
        return list(self._user_state(state, user_id)["asset_database"])

    def get_portfolio(self, user_id: int) -> dict[str, Holding]:
        """Return the user's portfolio holdings."""
        state = self._load()
        portfolio = self._user_state(state, user_id)["portfolio"]
        result: dict[str, Holding] = {}
        for symbol, values in portfolio.items():
            try:
                result[symbol] = Holding(
                    symbol=symbol,
                    quantity=int(values["quantity"]),
                    buy_price=float(values["buy_price"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping invalid holding for %s: %s", symbol, exc)
        return result

    def add_alert(self, user_id: int, chat_id: int, alert: StockAlert) -> None:
        """Add an alert for a user."""
        state = self._load()
        user = self._user_state(state, user_id)
        user["chat_id"] = int(chat_id)
        user["alerts"].append(
            {
                "symbol": alert.symbol.strip().upper(),
                "kind": alert.kind,
                "operator": alert.operator,
                "value": alert.value,
            }
        )
        self._save(state)

    def remove_alert(self, user_id: int, index: int) -> bool:
        """Remove one alert by one-based index."""
        state = self._load()
        user = self._user_state(state, user_id)
        zero_index = index - 1
        if zero_index < 0 or zero_index >= len(user["alerts"]):
            return False
        user["alerts"].pop(zero_index)
        self._save(state)
        return True

    def get_alerts(self, user_id: int) -> list[StockAlert]:
        """Return alerts for a user."""
        state = self._load()
        alerts = self._user_state(state, user_id)["alerts"]
        return [self._alert_from_dict(item) for item in alerts]

    def iter_user_alerts(self) -> list[tuple[int, int | None, int, StockAlert]]:
        """Return all alerts as user_id, chat_id, one-based index, alert tuples."""
        state = self._load()
        rows: list[tuple[int, int | None, int, StockAlert]] = []
        for user_id_text, user in state.get("users", {}).items():
            try:
                user_id = int(user_id_text)
            except ValueError:
                continue
            chat_id = user.get("chat_id")
            for index, alert_data in enumerate(user.get("alerts", []), start=1):
                rows.append((user_id, chat_id, index, self._alert_from_dict(alert_data)))
        return rows

    def clear_alert(self, user_id: int, alert: StockAlert) -> None:
        """Remove the first matching alert."""
        state = self._load()
        user = self._user_state(state, user_id)
        for index, item in enumerate(user["alerts"]):
            existing = self._alert_from_dict(item)
            if existing == alert:
                user["alerts"].pop(index)
                break
        self._save(state)

    def _load(self) -> dict[str, Any]:
        """Load state from disk."""
        with self._lock:
            # A damaged file is renamed aside and reported, never overwritten.
            state = load_json(self.path, lambda: {"users": {}, "signal_memory": {}})
            if not isinstance(state.get("users"), dict):
                state["users"] = {}
            if not isinstance(state.get("signal_memory"), dict):
                state["signal_memory"] = {}
            return state

    def _save(self, state: dict[str, Any]) -> None:
        """Save state to disk."""
        with self._lock:
            atomic_write_json(self.path, state)

    @staticmethod
    def _user_state(state: dict[str, Any], user_id: int) -> dict[str, Any]:
        """Return mutable state for one user."""
        users = state.setdefault("users", {})
        user = users.setdefault(str(user_id), {})
        user.setdefault("watchlist", [])
        user.setdefault("portfolio", {})
        user.setdefault("alerts", [])
        user.setdefault("chat_id", None)
        user.setdefault("auto_signals", False)
        user.setdefault("active_watchlist", None)
        user.setdefault("asset_database", [])
        return user

    @staticmethod
    def _alert_from_dict(data: dict[str, Any]) -> StockAlert:
        """Create a StockAlert from JSON data."""
        value = data.get("value")
        return StockAlert(
            symbol=str(data.get("symbol", "")).upper(),
            kind=str(data.get("kind", "")),
            operator=data.get("operator"),
            value=float(value) if value is not None else None,
        )
