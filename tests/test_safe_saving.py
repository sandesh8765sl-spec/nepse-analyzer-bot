"""Safe saving: atomic writes, and damaged files are kept, never overwritten."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nepse_bot.research.news_fetcher import ResearchCache
from nepse_bot.storage.user_store import UserStore
from nepse_bot.utils import safe_json
from nepse_bot.utils.health import drain_admin_alerts


def test_atomic_write_replaces_whole_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    safe_json.atomic_write_json(target, {"a": 1})
    safe_json.atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp files left behind


def test_failed_save_leaves_old_file_untouched(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    safe_json.atomic_write_json(target, {"good": True})
    with pytest.raises(TypeError):
        safe_json.atomic_write_json(target, {"bad": object()})  # cannot be saved as JSON
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": True}


def test_interrupted_write_leaves_old_file_untouched(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "state.json"
    safe_json.atomic_write_json(target, {"good": True})

    def boom(*_args, **_kwargs):
        raise OSError("simulated power cut")

    monkeypatch.setattr(safe_json.os, "replace", boom)
    with pytest.raises(OSError):
        safe_json.atomic_write_json(target, {"good": False})
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_user_file_is_quarantined_and_owner_alerted(tmp_path: Path) -> None:
    path = tmp_path / "user_state.json"
    damaged = '{"users": {"42": {"watchlist": ["NABIL"'  # cut off mid-write
    path.write_text(damaged, encoding="utf-8")

    store = UserStore(path)
    assert store.get_watchlist(42) == []  # bot keeps running with empty data

    kept = list(tmp_path.glob("user_state.json.corrupt-*"))
    assert len(kept) == 1, "the damaged file must be kept under a .corrupt-<timestamp> name"
    assert kept[0].read_text(encoding="utf-8") == damaged, "the damaged content must be preserved exactly"

    alerts = drain_admin_alerts()
    assert len(alerts) == 1 and "user_state.json" in alerts[0] and "NOT overwritten" in alerts[0]

    # Saving afterwards creates a new file; the quarantined copy is still there.
    store.add_watchlist_symbol(42, "NICA")
    assert store.get_watchlist(42) == ["NICA"]
    assert kept[0].exists()


def test_file_with_byte_order_mark_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "user_state.json"
    path.write_text('﻿{"users": {"7": {"watchlist": ["API"]}}, "signal_memory": {}}', encoding="utf-8")
    assert UserStore(path).get_watchlist(7) == ["API"]
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_user_store_round_trip_and_threads(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = UserStore(tmp_path / "user_state.json")
    symbols = [f"SYM{i}" for i in range(40)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda symbol: store.add_asset_symbol(1, symbol), symbols))
    assert store.get_asset_symbols(1) == sorted(symbols), "no update may be lost when threads save together"


def test_research_cache_drops_expired_entries(tmp_path: Path) -> None:
    path = tmp_path / "research_cache.json"
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "entries": {
                    "old": {"expires_at": (now - timedelta(hours=1)).isoformat(), "items": []},
                    "broken": {"expires_at": "not-a-date", "items": []},
                    "fresh": {"expires_at": (now + timedelta(hours=1)).isoformat(), "items": []},
                }
            }
        ),
        encoding="utf-8",
    )
    cache = ResearchCache(path)
    assert cache.prune_expired() == 2
    assert set(json.loads(path.read_text(encoding="utf-8"))["entries"]) == {"fresh"}
    assert cache.get_items("fresh") == []
    assert cache.get_items("old") is None

    # Expired entries also disappear the next time anything is saved.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"]["old2"] = {"expires_at": (now - timedelta(minutes=5)).isoformat(), "items": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    cache.set_items("new", [], ttl_minutes=10)
    assert set(json.loads(path.read_text(encoding="utf-8"))["entries"]) == {"fresh", "new"}
