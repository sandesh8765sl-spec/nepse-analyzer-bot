"""The broker study: runs by itself once the broker history is complete.

Three questions, one gate (profit factor >= 1.2 over >= 50 trades in BOTH periods, after fees and tax):

1. Does the broker-accumulation setup pass?
2. Does "broker buying" as a filter make an existing setup pass? Broker buying is ONE rule fixed
   before the test (``config/engine.yaml`` -> ``broker_filter.min_top5_net_share``): the top 5 brokers
   net bought at least that share of the traded volume over 10 days. No other values are tried.
3. Does the same filter make TOP PICK pass?

Only what passes is switched on (``apply``); everything else stays off. The owner gets a short summary.

    python -m nepse_bot.backtest.broker_study --sample        # plumbing check, changes nothing
    python -m nepse_bot.backtest.broker_study --no-backtest   # evaluate reports/broker as it is, changes nothing
    python -m nepse_bot.backtest.broker_study --apply         # the full run, as the bot does it
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from nepse_bot.backtest import alerts as alert_backtest
from nepse_bot.backtest import report
from nepse_bot.engine import evidence
from nepse_bot.engine.config import ENGINE_CONFIG_PATH, engine_config, load_profile, setting
from nepse_bot.settings import PROJECT_ROOT, bot_setting

FOLDER = PROJECT_ROOT / "reports" / "broker"
SUFFIX = "_broker"              # evidence key of "setup + broker buying"
NAMES = {**report.SETUP_NAMES}
STUDY_RESULT = "broker_backfill.study_result"


def _setting(name: str, default: Any) -> Any:
    return bot_setting("ingest", "broker_backfill", name, default=default)


def min_share() -> float:
    return float(setting("broker_filter", "min_top5_net_share", default=15))


# ------------------------------------------------------------------ backtest
def run_backtest(sample: bool = False) -> Path:
    """The normal backtest over the years with broker data, in its own low-priority process."""
    workers = max(1, min(6, int(_setting("backtest_workers", 4))))
    command = [sys.executable, "-m", "nepse_bot.backtest", "--out", "broker", "--workers", str(workers)]
    command += ["--sample"] if sample else ["--start", str(_setting("backtest_start", "2022-03-01"))]
    flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if sys.platform == "win32" else 0
    done = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, creationflags=flags)
    if done.returncode != 0:
        raise RuntimeError(f"the backtest failed: {(done.stderr or done.stdout)[-400:]}")
    return FOLDER / ("sample" if sample else "")


# ------------------------------------------------------------- broker buying
def broker_net_share(db: Any, as_of: str, symbols: list[str], days: int = 10) -> dict[str, float]:
    """Top-5 brokers' net buying as % of the traded volume over the last ``days`` sessions up to ``as_of``.

    The same number the live engine shows ("Top brokers net bought X% of volume in 10 days").
    Empty when fewer than ``days`` sessions of broker data exist.
    """
    dates = [row["date"] for row in db.query("SELECT DISTINCT date FROM broker_flow_daily WHERE date <= ? ORDER BY date DESC LIMIT ?", (as_of, days))]
    if len(dates) < days or not symbols:
        return {}
    flow = db.frame(
        f"SELECT symbol, broker, SUM(buy_qty) AS bought, SUM(sell_qty) AS sold FROM broker_flow_daily "
        f"WHERE date IN ({','.join('?' for _ in dates)}) AND symbol IN ({','.join('?' for _ in symbols)}) GROUP BY symbol, broker",
        [*dates, *symbols],
    )
    shares: dict[str, float] = {}
    for symbol, group in flow.groupby("symbol"):
        volume = float(group["bought"].sum())
        if volume > 0:
            shares[str(symbol)] = float((group["bought"] - group["sold"]).nlargest(5).sum() / volume * 100.0)
    return shares


def add_broker_share(trades: pd.DataFrame, db: Any) -> pd.DataFrame:
    """Add the column ``broker_share`` (NaN where there is no broker data) to every candidate."""
    trades = trades.copy()
    values: dict[tuple[str, str], float] = {}
    for day, group in trades.groupby("date"):
        for symbol, share in broker_net_share(db, str(day), sorted(set(group["symbol"]))).items():
            values[(str(day), symbol)] = share
    trades["broker_share"] = [values.get((str(day), symbol), float("nan")) for day, symbol in zip(trades["date"], trades["symbol"])]
    return trades


# ---------------------------------------------------------------------- gate
def _record(table: pd.DataFrame, setup: str, r_rule: float, period: str) -> evidence.Record:
    chosen = table[(table["setup"] == setup) & (table["r_rule"] == r_rule)] if len(table) else table
    if not len(chosen):
        return evidence.Record(period, 0, None, None, None, None, None)
    row = chosen.iloc[0]

    def number(key: str) -> float | None:
        value = row.get(key)
        if value is None or pd.isna(value):
            return None
        return 99.0 if math.isinf(float(value)) else float(value)

    return evidence.Record(period, int(row["trades"]), number("win_rate"), number("avg_win_pct"), number("avg_loss_pct"),
                           number("expectancy_pct"), number("profit_factor"))


def setup_evidence(buys: pd.DataFrame, cut: str, periods: tuple[str, str], capital: float, r_rule: float) -> dict[str, evidence.Evidence]:
    """Each setup's record in both periods, counted exactly as the normal backtest counts it."""
    recent, earlier = buys[buys["date"] >= cut], buys[buys["date"] < cut]
    recent_table = report.setup_table(recent, capital) if len(recent) else pd.DataFrame()
    earlier_table = report.setup_table(earlier, capital) if len(earlier) else pd.DataFrame()
    return {
        key: evidence.Evidence(key, _record(recent_table, key, r_rule, periods[0]), _record(earlier_table, key, r_rule, periods[1]))
        for key in sorted(set(buys["setup"]))
    }


def _pick_passes(numbers: dict[str, Any]) -> bool:
    factor = numbers.get("profit_factor")
    return (numbers.get("finished", 0) >= int(setting("backtest", "gate_min_trades", default=50))
            and factor is not None and factor >= float(setting("backtest", "gate_min_profit_factor", default=1.2)))


def evaluate(folder: Path, db: Any) -> dict[str, Any]:
    """Read a finished backtest and answer the three questions. Changes nothing."""
    trades = pd.read_csv(folder / "trades.csv", low_memory=False)
    regimes = pd.read_csv(folder / "regimes.csv")
    rules = [float(value) for value in setting("backtest", "r_rules_to_compare", default=[0.0])]
    r_rule = rules[0] if rules and rules[0] in set(trades["r_rule"]) else float(trades["r_rule"].iloc[0])
    trades = add_broker_share(trades[trades["r_rule"] == r_rule], db)
    start, end = str(regimes["date"].min()), str(regimes["date"].max())
    cut = (date.fromisoformat(end) - timedelta(days=round(365.25 * float(setting("backtest", "out_of_sample_years", default=2))))).isoformat()
    periods = (f"{max(cut, start)} to {end}", f"{start} to {cut}")
    capital = load_profile(db).total_capital
    confirmed = trades[trades["broker_share"] >= min_share()]
    buys = trades[trades["is_buy"] == True]  # noqa: E712
    plain = setup_evidence(buys, cut, periods, capital, r_rule)
    filtered = setup_evidence(confirmed[confirmed["is_buy"] == True], cut, periods, capital, r_rule)  # noqa: E712
    empty = lambda key: evidence.Evidence(key, evidence.Record(periods[0], 0, None, None, None, None, None),  # noqa: E731
                                          evidence.Record(periods[1], 0, None, None, None, None, None))
    tested = [key for key in NAMES if key != "broker_accumulation" and setting("setups", key, default=False) is True]
    all_days = sorted(regimes["date"])
    picks = {
        label: alert_backtest.top_picks(part, db, source=folder, all_days=all_days)
        for label, part in (("plain", trades), ("filtered", confirmed))
    }
    top_pick = {
        label: {"recent": alert_backtest.summarise(chosen[chosen["date"] >= cut]), "earlier": alert_backtest.summarise(chosen[chosen["date"] < cut])}
        for label, chosen in picks.items()
    }
    return {
        "periods": periods, "min_share": min_share(),
        "broker_days": int(db.scalar("SELECT COUNT(DISTINCT date) FROM broker_flow_daily") or 0),
        "accumulation": plain.get("broker_accumulation") or empty("broker_accumulation"),
        "filters": {key: {"plain": plain.get(key) or empty(key), "filtered": filtered.get(key) or empty(key)} for key in tested},
        "top_pick": top_pick,
        "top_pick_passed": _pick_passes(top_pick["filtered"]["recent"]) and _pick_passes(top_pick["filtered"]["earlier"]),
    }


# --------------------------------------------------------------------- apply
def _record_yaml(record: evidence.Record) -> str:
    if not record.trades:
        return f'{{period: "{record.period}", trades: 0}}'
    value = lambda item: "null" if item is None else f"{item:g}"  # noqa: E731
    return (f'{{period: "{record.period}", trades: {record.trades}, win_rate: {value(record.win_rate)}, avg_win_pct: {value(record.avg_win_pct)}, '
            f'avg_loss_pct: {value(record.avg_loss_pct)}, expectancy_pct: {value(record.expectancy_pct)}, profit_factor: {value(record.profit_factor)}}}')


def _put_evidence(text: str, key: str, item: evidence.Evidence) -> str:
    """Replace (or add) one setup's two lines in the text of an evidence file."""
    block = f"  {key}:\n    recent: {_record_yaml(item.recent)}\n    earlier: {_record_yaml(item.earlier)}\n"
    pattern = re.compile(rf"(?m)^  {re.escape(key)}:\n(?:    .*\n?)+")
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    return text.rstrip("\n") + "\n" + block


def _set_engine_line(text: str, key: str, value: str, note: str) -> str:
    """Change one ``key: value`` line of config/engine.yaml, keeping the rest of the file as it is."""
    pattern = re.compile(rf"(?m)^([ \t]+{re.escape(key)}:)[^\n]*$")
    if not pattern.search(text):
        raise RuntimeError(f"config/engine.yaml has no line '{key}:'")
    return pattern.sub(lambda match: f"{match.group(1)} {value}    # {note}", text, count=1)


def apply(result: dict[str, Any], today: date, evidence_path: Path | None = None, engine_path: Path | None = None,
          backup_folder: Path | None = None) -> list[str]:
    """Switch on what passed the gate, and only that. Returns one line per change (empty = nothing changed)."""
    evidence_path, engine_path = evidence_path or evidence.EVIDENCE_PATH, engine_path or ENGINE_CONFIG_PATH
    evidence_text, engine_text = evidence_path.read_text(encoding="utf-8"), engine_path.read_text(encoding="utf-8")
    note = f"switched on by the broker study on {today.isoformat()}: passed the gate"
    changes: list[str] = []
    if result["accumulation"].proven:
        evidence_text = _put_evidence(evidence_text, "broker_accumulation", result["accumulation"])
        engine_text = _set_engine_line(engine_text, "broker_accumulation", "true", note)
        changes.append("Broker accumulation setup is ON: its candidates can now be graded BUY.")
    passed = [key for key, pair in result["filters"].items() if pair["filtered"].proven and not pair["plain"].proven]
    for key in passed:
        evidence_text = _put_evidence(evidence_text, key + SUFFIX, result["filters"][key]["filtered"])
        changes.append(f"{NAMES.get(key, key)} WITH broker buying can now be graded BUY (without it: still SPECULATIVE).")
    if passed:
        engine_text = _set_engine_line(engine_text, "filter_setups", "[" + ", ".join(passed) + "]", note)
    if result["top_pick_passed"]:
        engine_text = _set_engine_line(engine_text, "filter_top_pick", "true", note)
        changes.append("TOP PICK now also needs broker buying.")
    if changes:
        backup_folder = backup_folder or FOLDER
        backup_folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(evidence_path, backup_folder / "evidence_before.yaml")
        shutil.copy2(engine_path, backup_folder / "engine_before.yaml")
        evidence_path.write_text(evidence_text, encoding="utf-8")
        engine_path.write_text(engine_text, encoding="utf-8")
        evidence._all.cache_clear()
        engine_config.cache_clear()
    return changes


# ---------------------------------------------------------------------- text
def _short(record: evidence.Record | None) -> str:
    if record is None or not record.trades or record.profit_factor is None:
        return "no trades"
    return f"{record.trades} trades, {record.win_rate:.0f}% won, {record.expectancy_pct:+.1f}% per trade, profit factor {record.profit_factor:.2f}"


def _pick_short(numbers: dict[str, Any]) -> str:
    if not numbers.get("finished"):
        return f"{numbers.get('alerts', 0)} alerts, none finished"
    return f"{numbers['finished']} trades, {numbers['win_rate']:.0f}% won, {numbers['expectancy_pct']:+.1f}% per trade, profit factor {numbers['profit_factor']}"


def summary_text(result: dict[str, Any], changes: list[str], sample: bool = False) -> str:
    """The short Telegram message (plain text)."""
    verdict = lambda ok: "PASSES" if ok else "does not pass"  # noqa: E731
    accumulation = result["accumulation"]
    lines = [
        "📊 Broker study" + (" (SAMPLE: plumbing check only, not a result)" if sample else " finished"),
        f"Broker history used: {result['broker_days']:,} days. After fees and tax. Gate: profit factor 1.2+ over 50+ trades in BOTH periods.",
        "",
        f"1) Broker accumulation setup: {verdict(accumulation.proven)}",
        f"   last 2 years: {_short(accumulation.recent)}",
        f"   earlier: {_short(accumulation.earlier)}",
        "",
        f"2) Existing setups, only when the top 5 brokers net bought {result['min_share']:g}%+ of the volume in 10 days:",
    ]
    for key, pair in result["filters"].items():
        better = pair["filtered"].proven and not pair["plain"].proven
        lines.append(f"   {NAMES.get(key, key)}: {verdict(better)}")
        lines.append(f"     last 2 years: {_short(pair['filtered'].recent)} (without the filter: {_short(pair['plain'].recent)})")
        lines.append(f"     earlier: {_short(pair['filtered'].earlier)}")
    pick = result["top_pick"]
    lines += [
        "",
        f"3) TOP PICK with the same filter: {verdict(result['top_pick_passed'])}",
        f"   last 2 years: {_pick_short(pick['filtered']['recent'])} (without: {_pick_short(pick['plain']['recent'])})",
        f"   earlier: {_pick_short(pick['filtered']['earlier'])}",
        "",
    ]
    if sample:
        lines.append("Nothing was changed.")
    elif changes:
        lines += ["Switched ON (only what passed):", *[f"• {line}" for line in changes],
                  "The old settings are saved in reports/broker/evidence_before.yaml and engine_before.yaml."]
    else:
        lines.append("Nothing passed, so nothing was switched on. Broker data stays what it was: part of the score and the operator-risk warning.")
    lines.append("Tables: reports/broker/SUMMARY.md and BROKER_STUDY.md")
    return "\n".join(lines)


def result_line(result: dict[str, Any], changes: list[str], today: date) -> str:
    """One line for /status."""
    if changes:
        return f"Broker backtest {today.isoformat()}: switched on: " + " ".join(changes)
    return f"Broker backtest {today.isoformat()}: nothing passed the gate; all broker extras stay off."


def run_and_apply(db: Any, sample: bool = False, backtest: bool = True, change: bool = True) -> str:
    """Backtest, evaluate, switch on what passed, write the report. Returns the Telegram text."""
    folder = run_backtest(sample) if backtest else FOLDER
    result = evaluate(folder, db)
    today = date.today()
    changes = apply(result, today) if change and not sample else []
    text = summary_text(result, changes, sample)
    (folder / "BROKER_STUDY.md").write_text("# Broker study\n\n```\n" + text + "\n```\n", encoding="utf-8")
    if change and not sample:
        db.set_setting(STUDY_RESULT, result_line(result, changes, today))
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Broker study: accumulation setup, broker buying as a filter, TOP PICK.")
    parser.add_argument("--sample", action="store_true", help="quick plumbing check (30 stocks, one year); changes nothing")
    parser.add_argument("--no-backtest", action="store_true", help="evaluate reports/broker as it is; changes nothing")
    parser.add_argument("--apply", action="store_true", help="switch on what passes (what the bot does by itself)")
    arguments = parser.parse_args()
    from nepse_bot.db.store import get_database

    print(run_and_apply(get_database(), sample=arguments.sample, backtest=not arguments.no_backtest, change=arguments.apply))


if __name__ == "__main__":
    main()
