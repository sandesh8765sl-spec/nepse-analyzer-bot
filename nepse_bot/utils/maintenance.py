"""Daily housekeeping: throw away what can be rebuilt, so the disk does not fill up.

Only caches, debug samples, old log overflow and rebuildable backtest tables are
removed. The owner's data (database, watchlist, backups, reports' summaries) is never touched.

    python -m nepse_bot.utils.maintenance
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from nepse_bot.settings import CHART_DIR, LOG_DIR, PROJECT_ROOT, bot_setting

logger = logging.getLogger(__name__)
BULKY_TABLES = ("signals.csv", "trades.csv")      # written by a backtest; rebuilt by running it again


def _older_than(path: Path, days: float) -> bool:
    return time.time() - path.stat().st_mtime > days * 86400


def prune_files(folder: Path, pattern: str, days: float) -> int:
    """Delete files matching ``pattern`` in ``folder`` that are older than ``days``."""
    removed = 0
    if not folder.exists():
        return 0
    for path in folder.glob(pattern):
        try:
            if path.is_file() and _older_than(path, days):
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.debug("Could not remove %s: %s", path, exc)
    return removed


def prune_monthly_tables(root: Path | None = None, keep_months: int = 1) -> int:
    """Monthly re-tests keep their summaries for ever, but the two big tables only for the newest month(s)."""
    folder = (root or PROJECT_ROOT / "reports") / "monthly"
    if not folder.exists():
        return 0
    months = sorted(path for path in folder.iterdir() if path.is_dir())
    removed = 0
    for month in months[:-keep_months] if keep_months > 0 else months:
        for name in BULKY_TABLES:
            for path in month.rglob(name):
                path.unlink()
                removed += 1
    return removed


def run() -> dict[str, Any]:
    from nepse_bot.research.news_fetcher import ResearchCache
    from nepse_bot.sources.http import prune_raw_samples

    keep = float(bot_setting("maintenance", "keep_days", default=7))
    result = {
        "raw_sample_folders": prune_raw_samples(),
        "news_cache_entries": ResearchCache().prune_expired(),
        "chart_files": prune_files(CHART_DIR, "*.png", keep),
        "old_launcher_logs": prune_files(LOG_DIR, "*.log.1", float(bot_setting("maintenance", "keep_log_overflow_days", default=30))),
        "monthly_retest_tables": prune_monthly_tables(),
    }
    logger.info("Housekeeping removed: %s", result)
    return result


if __name__ == "__main__":
    print(run())
