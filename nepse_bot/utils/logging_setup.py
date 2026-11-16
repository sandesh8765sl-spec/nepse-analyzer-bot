"""Logging configuration with size-limited, rotating log files."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from nepse_bot.settings import PROJECT_ROOT, bot_setting
from nepse_bot.utils.health import ERROR_COUNTER

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging() -> Path:
    """Send logs to a rotating file (and to the screen when run by hand).

    Returns:
        The path of the active log file.
    """
    log_path = Path(str(bot_setting("logging", "file", default="logs/bot.log")))
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(float(bot_setting("logging", "max_megabytes", default=5)) * 1024 * 1024)
    backups = int(bot_setting("logging", "backup_files", default=5))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(ERROR_COUNTER)

    # Only echo to the screen for a person watching; the launcher redirects
    # output to a file, and duplicating every line there would defeat rotation.
    if sys.stderr is not None and sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    # httpx logs full request URLs at INFO, which include the Telegram token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    return log_path
