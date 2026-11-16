"""Monthly re-test: run the backtest again on the newest data and SUGGEST changes.

Nothing is ever applied automatically. The result of a re-test is a list of
suggestions (reports/monthly/<YYYY-MM>/SUGGESTIONS.md, also sent on Telegram).
Only the owner's `/retest apply` copies the new record to config/evidence.yaml,
which is what decides BUY against SPECULATIVE.

    python -m nepse_bot.backtest.monthly            # run now (about an hour)
    python -m nepse_bot.backtest.monthly --sample   # quick check of the plumbing
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from nepse_bot.engine import evidence
from nepse_bot.engine.setups import ALL_SETUPS
from nepse_bot.settings import CONFIG_DIR, PROJECT_ROOT, bot_setting

LAST_MONTH, PENDING = "retest.last_month", "retest.pending"
NAMES = {setup.key: setup.name for setup in ALL_SETUPS}


def month_key(today: date) -> str:
    return today.strftime("%Y-%m")


def due(db: Any, today: date) -> bool:
    """True once per calendar month (also when the PC was off on the 1st)."""
    return db.get_setting(LAST_MONTH) != month_key(today)


def out_folder(month: str, sample: bool = False) -> Path:
    return PROJECT_ROOT / "reports" / "monthly" / month / ("sample" if sample else "")


def run_backtest(month: str, sample: bool = False) -> Path:
    """Run the normal backtest in its own process (so the bot keeps answering). Returns the result folder."""
    workers = int(bot_setting("retest", "workers", default=4))
    command = [sys.executable, "-m", "nepse_bot.backtest", "--out", f"monthly/{month}", "--workers", str(workers)]
    if sample:
        command.append("--sample")
    done = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"the backtest failed: {(done.stderr or done.stdout)[-400:]}")
    return out_folder(month, sample)


def _record_text(record: evidence.Record | None) -> str:
    if record is None or not record.trades or record.profit_factor is None:
        return "no trades"
    return f"{record.trades} trades, {record.win_rate:.0f}% won, {record.expectancy_pct:+.1f}% per trade, profit factor {record.profit_factor:.2f}"


def suggestions(current: dict[str, evidence.Evidence], new: dict[str, evidence.Evidence]) -> tuple[list[str], list[str]]:
    """Compare the record in use with the new one. Returns (suggested changes, per-setup summary lines)."""
    changes: list[str] = []
    summary: list[str] = []
    for key, fresh in new.items():
        name = NAMES.get(key, key)
        old = current.get(key) or evidence.Evidence(key, None, None)
        summary.append(f"{name}: last 2 years {_record_text(fresh.recent)}; earlier {_record_text(fresh.earlier)} -> {'PASSES the gate' if fresh.proven else 'does not pass'}")
        if fresh.proven and not old.proven:
            changes.append(f"PROMOTE {name}: it now passes the gate in both periods, so its candidates could be graded BUY.")
        elif old.proven and not fresh.proven:
            changes.append(f"DEMOTE {name}: it no longer passes the gate, so its candidates should go back to SPECULATIVE.")
        elif fresh.recent and old.recent and fresh.recent.profit_factor is not None and old.recent.profit_factor is not None:
            move = fresh.recent.profit_factor - old.recent.profit_factor
            if abs(move) >= 0.15:
                changes.append(f"NOTE {name}: recent profit factor moved from {old.recent.profit_factor:.2f} to {fresh.recent.profit_factor:.2f} (grade unchanged).")
    return changes, summary


def report_text(month: str, changes: list[str], summary: list[str], sample: bool = False) -> str:
    lines = [f"🧪 <b>Monthly re-test — {month}</b>" + (" (SAMPLE run, not for decisions)" if sample else ""),
             "The backtest was run again on the newest data, after fees and tax.", ""]
    lines.append("<b>Each setup now</b>")
    lines.extend(f"• {line}" for line in summary)
    lines.extend(["", "<b>Suggested changes</b>"])
    lines.extend([f"• {line}" for line in changes] or ["• None. The grades in use still match the evidence."])
    lines.extend(["", "<b>Nothing has been changed.</b> To accept the new record: /retest apply. To ignore it: do nothing.",
                  f"Tables: reports/monthly/{month}/SUMMARY.md"])
    return "\n".join(lines)


def run(db: Any, today: date, sample: bool = False) -> str:
    """Run the re-test and return the Telegram text. Records the result as pending; applies nothing."""
    month = month_key(today)
    folder = run_backtest(month, sample)
    new = evidence.load_file(folder / "evidence.yaml")
    if not new:
        raise RuntimeError(f"the backtest wrote no evidence file in {folder}")
    changes, summary = suggestions(evidence.load_file(CONFIG_DIR / "evidence.yaml"), new)
    text = report_text(month, changes, summary, sample)
    plain = text.replace("<b>", "**").replace("</b>", "**")
    (folder / "SUGGESTIONS.md").write_text(plain + "\n", encoding="utf-8")
    if not sample:
        db.set_setting(LAST_MONTH, month)
        db.set_setting(PENDING, str(folder))
    return text


def pending_text(db: Any) -> str:
    folder = db.get_setting(PENDING)
    path = Path(folder) / "SUGGESTIONS.md" if folder else None
    if path is None or not path.exists():
        last = db.get_setting(LAST_MONTH)
        return ("No re-test result is waiting." + (f" The last one ran for {last}." if last else " None has run yet.")
                + " It runs by itself once a month; /retest run starts one now (about an hour).")
    return path.read_text(encoding="utf-8").replace("**", "")


def apply(db: Any) -> str:
    """The owner's approval: put the pending record into use. The old file is kept beside the new one."""
    folder = db.get_setting(PENDING)
    source = Path(folder) / "evidence.yaml" if folder else None
    if source is None or not source.exists():
        return "There is nothing to apply. Run /retest first."
    target = CONFIG_DIR / "evidence.yaml"
    shutil.copy2(target, Path(folder) / "evidence_before.yaml")
    shutil.copy2(source, target)
    evidence._all.cache_clear()
    db.set_setting(PENDING, "")
    return (f"Applied: config/evidence.yaml now holds the record from {Path(folder).name}. The previous file is saved as "
            f"reports/monthly/{Path(folder).name}/evidence_before.yaml. You get a message if a setup changed grade.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monthly re-test: suggests changes, applies nothing.")
    parser.add_argument("--sample", action="store_true")
    arguments = parser.parse_args()
    from nepse_bot.db.store import get_database

    print(run(get_database(), date.today(), arguments.sample).replace("<b>", "").replace("</b>", ""))


if __name__ == "__main__":
    main()
