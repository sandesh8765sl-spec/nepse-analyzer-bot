"""Keep the PC awake while a long job runs (backtest, history download).

Windows may put an idle laptop to sleep in the middle of a job that takes hours.
While the ``keep_awake()`` block is active Windows is asked not to sleep by itself;
the screen may still switch off. Closing the lid or pressing the power button
still puts the PC to sleep: the long jobs are written to continue (history
download) or to be restarted (backtest) afterwards.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_state(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not change the keep-awake state: %s", exc)
        return False


@contextmanager
def keep_awake() -> Iterator[bool]:
    """Ask Windows not to sleep on idle until the block ends. Yields True when the request was accepted."""
    active = _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield active
    finally:
        if active:
            _set_state(ES_CONTINUOUS)


def main() -> None:
    """Hold the PC awake for N seconds (used by the Windows wake task): python -m nepse_bot.utils.keep_awake 900"""
    import time

    seconds = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 900
    with keep_awake():
        time.sleep(seconds)


if __name__ == "__main__":
    main()
