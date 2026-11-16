"""Crash-safe JSON files.

Two rules protect saved data:

1. Saving writes a temporary file first and then swaps it into place, so a
   crash or power cut can never leave a half-written file.
2. If a file cannot be read as JSON it is renamed to ``*.corrupt-<timestamp>``
   and the owner is alerted. The damaged file is kept; it is never silently
   replaced by an empty one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from nepse_bot.utils.health import queue_admin_alert

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path | str, text: str) -> None:
    """Write text so the target is either fully old or fully new."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, data: Any) -> None:
    """Serialize first, then write atomically (a failed dump touches nothing)."""
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))


def quarantine_corrupt_file(path: Path | str, reason: str) -> Path | None:
    """Rename an unreadable file aside and alert the owner."""
    source = Path(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = source.with_name(f"{source.name}.corrupt-{stamp}")
    try:
        os.replace(source, destination)
    except OSError as exc:
        logger.error("Could not quarantine corrupt file %s: %s", source, exc)
        queue_admin_alert(
            f"⚠️ Saved-data problem: {source.name} could not be read ({reason}) and could not be "
            "moved aside. The bot will not overwrite it. Please check the file."
        )
        return None
    logger.error("Corrupt data file %s moved to %s. Reason: %s", source, destination.name, reason)
    queue_admin_alert(
        f"⚠️ Saved-data problem: {source.name} could not be read ({reason}).\n"
        f"The damaged file was kept as {destination.name} and was NOT overwritten.\n"
        "The bot is continuing with empty data for this file until it is restored."
    )
    return destination


def load_json(path: Path | str, default_factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Load a JSON object, quarantining the file if it is damaged."""
    source = Path(path)
    if not source.exists():
        return default_factory()
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if quarantine_corrupt_file(source, f"invalid JSON: {exc}") is None:
            raise RuntimeError(f"{source} is corrupt and could not be moved aside") from exc
        return default_factory()
    if not isinstance(data, dict):
        if quarantine_corrupt_file(source, "top level is not an object") is None:
            raise RuntimeError(f"{source} is corrupt and could not be moved aside")
        return default_factory()
    return data
