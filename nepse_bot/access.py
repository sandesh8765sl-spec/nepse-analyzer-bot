"""Access control: only allow-listed Telegram users and chats may use the bot."""

from __future__ import annotations

import logging
from pathlib import Path
from threading import RLock

from nepse_bot.settings import DATA_DIR
from nepse_bot.utils.safe_json import atomic_write_json, load_json

logger = logging.getLogger(__name__)

REFUSAL_TEXT = "Sorry, this is a private bot and it is not available for public use."


class AccessControl:
    """Decide who may use the bot, and refuse everyone else exactly once."""

    def __init__(
        self,
        allowed_user_ids: set[int],
        allowed_chat_ids: set[int],
        refused_path: Path | str | None = None,
    ) -> None:
        self.allowed_user_ids = frozenset(allowed_user_ids)
        self.allowed_chat_ids = frozenset(allowed_chat_ids)
        self.refused_path = Path(refused_path) if refused_path is not None else DATA_DIR / "refused_users.json"
        self._lock = RLock()
        self._refused: set[int] | None = None

    def is_allowed(self, user_id: int | None, chat_id: int | None) -> bool:
        """Return whether this user or chat is on the allowlist."""
        if user_id is not None and user_id in self.allowed_user_ids:
            return True
        return chat_id is not None and chat_id in self.allowed_chat_ids

    def should_send_refusal(self, user_id: int | None) -> bool:
        """Return True the first time a stranger is seen, and never again."""
        if user_id is None:
            return False
        with self._lock:
            refused = self._load_refused()
            if user_id in refused:
                return False
            refused.add(user_id)
            try:
                atomic_write_json(self.refused_path, {"refused_user_ids": sorted(refused)})
            except OSError as exc:
                logger.warning("Could not save the refused-users list: %s", exc)
            return True

    def refused_count(self) -> int:
        """Return how many different strangers have been turned away."""
        with self._lock:
            return len(self._load_refused())

    def _load_refused(self) -> set[int]:
        if self._refused is None:
            data = load_json(self.refused_path, lambda: {"refused_user_ids": []})
            self._refused = {int(item) for item in data.get("refused_user_ids", []) if str(item).lstrip("-").isdigit()}
        return self._refused
