"""Copy saved JSON data into the database (safe to run repeatedly).

Nothing is deleted: the JSON files stay where they are and the bot's existing
commands keep using them until those commands are rebuilt on the database.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nepse_bot.db.store import Database
from nepse_bot.settings import DATA_DIR

logger = logging.getLogger(__name__)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def migrate_signal_journal(db: Database, path: Path | str | None = None) -> int:
    """Copy every signal in ``signal_journal.json`` into the ``signals`` table."""
    source = Path(path) if path is not None else DATA_DIR / "signal_journal.json"
    if not source.exists():
        return 0
    try:
        state = json.loads(source.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s for migration: %s", source.name, exc)
        return 0
    rows = []
    for record in state.get("signals", []) if isinstance(state, dict) else []:
        if not isinstance(record, dict) or not record.get("id") or not record.get("symbol"):
            continue
        rows.append(
            {
                "id": f"legacy:{record['id']}",
                "symbol": str(record["symbol"]).upper(),
                "created_at": record.get("created_at"),
                "setup": record.get("technical_setup"),
                "verdict": record.get("signal_type"),
                "score": _number(record.get("score")),
                "entry": _number(record.get("entry_price")),
                "stop_loss": _number(record.get("stop_loss")),
                "target_1": _number(record.get("target_1")),
                "target_2": _number(record.get("target_2")),
                "status": record.get("status"),
                "outcome_price": _number(record.get("last_price") or record.get("exit_price")),
                "closed_at": record.get("updated_at") if record.get("status") not in (None, "active") else None,
                "plan_json": json.dumps(record, sort_keys=True, default=str),
            }
        )
    return db.upsert("signals", rows, ("id",))
