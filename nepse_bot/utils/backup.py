"""Daily backup of the database and the owner's data, and the restore step.

    python -m nepse_bot.utils.backup              # make today's backup now
    python -m nepse_bot.utils.backup list         # show the backups
    python -m nepse_bot.utils.backup restore 2026-11-14

A backup is one zip file, backups/nepse_backup_YYYY-MM-DD.zip, holding
* the database (positions, trades, journal, settings, prices, fundamentals, news),
* the files in data/ that hold your own state (watchlist, price alerts ...),
* config/holidays.yaml and config/evidence.yaml.

The broker-flow history is left out: it is public data that can be downloaded
again (python -m nepse_bot.ingest brokers) and would make every backup about ten
times larger. Secrets (.env) are never copied. The newest 14 backups are kept.

Restoring never deletes anything: the files being replaced are first moved to
data/before_restore_<time>/.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nepse_bot.settings import CONFIG_DIR, DATA_DIR, PROJECT_ROOT, bot_setting

logger = logging.getLogger(__name__)
DATABASE_NAME = "nepse.sqlite3"
PREFIX = "nepse_backup_"
LEFT_OUT_TABLES = ("broker_flow_daily",)
CONFIG_FILES = ("holidays.yaml", "evidence.yaml")


def backup_folder() -> Path:
    configured = bot_setting("backup", "folder", default="")
    return Path(configured) if configured else PROJECT_ROOT / "backups"


def keep_days() -> int:
    return int(bot_setting("backup", "keep_days", default=14))


def list_backups(folder: Path | None = None) -> list[Path]:
    folder = folder or backup_folder()
    return sorted(folder.glob(f"{PREFIX}*.zip")) if folder.exists() else []


def free_megabytes(path: Path) -> float:
    return shutil.disk_usage(path).free / 1_000_000


def _copy_database(source: Path, target: Path) -> None:
    """A consistent copy taken while the bot may be writing, without the re-downloadable tables."""
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as origin, closing(sqlite3.connect(target)) as copy:
        copy.execute("PRAGMA journal_mode=OFF")
        tables = origin.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for name, sql in tables:
            copy.execute(sql)
            if name in LEFT_OUT_TABLES:
                continue
            columns = [row[1] for row in origin.execute(f'PRAGMA table_info("{name}")')]
            marks = ",".join("?" for _ in columns)
            rows = origin.execute(f'SELECT * FROM "{name}"')
            while True:
                batch = rows.fetchmany(20_000)
                if not batch:
                    break
                copy.executemany(f'INSERT INTO "{name}" VALUES ({marks})', batch)
        for (sql,) in origin.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL").fetchall():
            copy.execute(sql)
        copy.commit()


def make_backup(today: date | None = None, data_dir: Path | None = None, folder: Path | None = None) -> dict[str, Any]:
    """Write today's backup (replacing an earlier one of the same day) and delete backups beyond the limit."""
    today = today or date.today()
    data_dir, folder = data_dir or DATA_DIR, folder or backup_folder()
    folder.mkdir(parents=True, exist_ok=True)
    need = float(bot_setting("backup", "min_free_mb", default=700))
    if free_megabytes(folder) < need:
        raise RuntimeError(f"only {free_megabytes(folder):,.0f} MB free on the disk; a backup needs {need:,.0f} MB. Free some space.")
    target = folder / f"{PREFIX}{today.isoformat()}.zip"
    partial = target.with_suffix(".zip.part")
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            database = data_dir / DATABASE_NAME
            if database.exists():
                copy = Path(scratch) / DATABASE_NAME
                _copy_database(database, copy)
                archive.write(copy, f"data/{DATABASE_NAME}")
            for path in sorted(data_dir.glob("*.json")) + sorted(data_dir.glob("*.txt")):
                archive.write(path, f"data/{path.name}")
            for name in CONFIG_FILES:
                if (CONFIG_DIR / name).exists():
                    archive.write(CONFIG_DIR / name, f"config/{name}")
    partial.replace(target)
    removed = [path.name for path in list_backups(folder)[:-keep_days()]]
    for name in removed:
        (folder / name).unlink()
    result = {"file": str(target), "megabytes": round(target.stat().st_size / 1_000_000, 1), "kept": len(list_backups(folder)), "removed": removed}
    logger.info("Backup written: %s", result)
    return result


def backup_due(today: date | None = None, folder: Path | None = None) -> bool:
    return not ((folder or backup_folder()) / f"{PREFIX}{(today or date.today()).isoformat()}.zip").exists()


def restore(day: str, data_dir: Path | None = None, folder: Path | None = None, config_dir: Path | None = None) -> str:
    """Put a backup back. STOP THE BOT FIRST. The files it replaces are moved aside, never deleted."""
    data_dir, folder, config_dir = data_dir or DATA_DIR, folder or backup_folder(), config_dir or CONFIG_DIR
    source = folder / f"{PREFIX}{day}.zip"
    if not source.exists():
        return f"No backup for {day}. Available: {', '.join(path.stem.replace(PREFIX, '') for path in list_backups(folder)) or 'none'}"
    aside = data_dir / f"before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    aside.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            base = Path(name).name
            target = (config_dir if name.startswith("config/") else data_dir) / base
            if target.exists():
                shutil.move(str(target), str(aside / base))
            for extra in ("-wal", "-shm"):          # leftovers of the old database must not be mixed with the restored one
                leftover = Path(str(target) + extra)
                if leftover.exists():
                    shutil.move(str(leftover), str(aside / leftover.name))
            with archive.open(name) as packed, open(target, "wb") as out:
                shutil.copyfileobj(packed, out)
    return (f"Restored the backup of {day}. The files it replaced are in {aside}. Broker history is not part of a backup: "
            "run `python -m nepse_bot.ingest brokers` to download it again. Now start the bot.")


def main() -> None:
    arguments = sys.argv[1:]
    if arguments[:1] == ["list"]:
        for path in list_backups():
            print(f"{path.stem.replace(PREFIX, '')}  {path.stat().st_size / 1_000_000:,.1f} MB")
    elif arguments[:1] == ["restore"] and len(arguments) == 2:
        print(restore(arguments[1]))
    elif not arguments:
        print(make_backup())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
