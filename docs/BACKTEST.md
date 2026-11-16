# Backtest and live journal (Stage 4)

## What the backtest does

It replays the bot's real analysis engine on every past trading day and then
follows each trade plan forward on the days that came after.

1. **Replay** (`nepse_bot/backtest/replay.py`). For each past day the engine
   runs exactly as it does live, seeing only data dated that day or earlier.
   Every setup is evaluated on its own.
2. **Simulation** (`nepse_bot/backtest/simulate.py`). Each plan is played
   forward with the `backtesting.py` library, which handles order fills:
   - buy at the **next trading day's open** (Stage 4 plans used a limit
     order that fills only if that day's low reaches the limit);
   - **no selling before T+2**;
   - the stop-loss sells at the stop, or at the open if the stock gaps below
     it; if the stop and target are touched on the same day the stop is
     assumed to come first;
   - the stop then follows the price up (highest price since entry minus
     3x the average daily range) and the trade is closed after 40 sessions;
   - quantities are whole board lots from the plan.
3. **Costs** come from `config/market.yaml`: commission by tier, SEBON fee, DP
   charge per sell day and capital gains tax on each profitable sale (losses
   do not reduce tax). Every money figure in the reports is net of all four.
4. **Tables** (`nepse_bot/backtest/report.py`). The most recent 2 years are
   the out-of-sample test; older data is the tuning period. A signal that
   repeats while its trade is still open is counted once.

Run it:

    python -m nepse_bot.backtest --sample    # 30 liquid stocks, 1 year (about 2 minutes)
    python -m nepse_bot.backtest             # everything (about 30 minutes)
    python -m nepse_bot.backtest --reuse     # rebuild the tables from the saved signals

Results are written to `reports/backtest/` (or the folder given with `--out`); start with `SUMMARY.md`. The command
never writes to the database and never changes `config/engine.yaml`.

## The gate

A setup is "proven" only if it has a profit factor of at least 1.2 over at
least 50 trades in BOTH the tuning period and the out-of-sample period (the
numbers are in the `backtest:` section of `config/engine.yaml`). Profit factor
= money won / money lost. Only proven setups can be shown as BUY or STRONG
BUY; the rest are SPECULATIVE and show their record from `config/evidence.yaml`.

To refresh the record after changing a rule: run the full backtest, then copy
`reports/<folder>/evidence.yaml` to `config/evidence.yaml` and restart the bot.

## Research tools (Stage 4b)

    python -m nepse_bot.backtest.panel    # one row per stock per day with everything the engine knew (30 min)
    python -m nepse_bot.backtest.study    # does the score predict returns? which inputs? which market states?
    python -m nepse_bot.backtest.lab      # entry rules x exit styles, after costs

Findings: `docs/RESEARCH.md`.

## Known limits (read before trusting the numbers)

- **Turnover is missing in the database before February 2023.** For those
  days the backtest estimates it as volume x closing price. The live bot is
  not affected.
- **Fundamentals are thin before 2022** and old reports carry an estimated
  publication date (quarter end + 35 days). The "Value + turn" setup therefore
  has little real history.
- **Broker data exists for a few days only**, so "Broker accumulation" cannot
  be tested at all yet.
- **Book-closure dates**: the database does not record when each closure was
  announced, so the replay may "know" a closure slightly earlier than the
  market did. This affects "Event run-up" most; treat its result with care.
- **Tax and fees** use today's rates for all years (the capital gains tax
  changed three times in 2026).
- **Price-limit days**: if a stock is locked at its lower limit you cannot
  really sell. The simulation sells at that day's price anyway; the summary
  counts how often an exit fell on a day when the price did not move.
- Days a stock did not trade are skipped, so "T+2" and "15 sessions" count the
  stock's own trading days.

## The live journal

Every BUY signal is logged with its full plan (table `signals`). After each
data update the journal follows every open signal with the daily high and low,
using the same simulation as the backtest: not filled, stop, target 1,
target 2, time stop or still open.

- `/report` shows the results per setup.
- If a setup's profit factor over its last 20 finished signals drops below
  1.0, it is switched off automatically and you get a Telegram message.
  `/report enable <setup>` switches it back on.
