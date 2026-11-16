# NEPSE Analyzer Bot

A personal Telegram bot that analyses stocks on the **Nepal Stock Exchange (NEPSE)**. Every day it collects market
data, scores each listed stock, and sends trade plans, alerts and reports to Telegram.

The bot **only gives advice**. It never places orders. The owner makes every trade by hand.

> Honest note from the backtests: over the last two years **no setup made money after costs**. The bot says so in
> its own messages and treats its picks as ranked research, not as buy signals. See [`docs/RESEARCH.md`](docs/RESEARCH.md).

## What it does

- **Daily data pipeline**: collects prices, volumes, fundamentals, announcements and broker history from public
  NEPSE data sources into a local SQLite database, including split and bonus adjustments.
- **Analysis engine**: market regime, a 0–100 score for each stock, entry setups, and trade plans with a buy range,
  stop-loss, position size, reference target and evidence grade.
- **Backtesting and research**: replays past market days, simulates trades with NEPSE fees and tax, and only lets a
  setup reach "BUY" once it is profitable in two separate past periods.
- **Position manager**: logs your trades, moves a trailing stop up, gives HOLD / TRIM / SELL advice, and switches on a
  protection mode after losing streaks.
- **Alerts and reports**: morning brief, evening report, intraday early alerts, price alerts, news and event
  calendar (book closures, lock-in expiries), and a weekly record of how past signals turned out.
- **Reliability**: safe file saving, daily backups, a self-test command, Windows startup tasks, and a background
  download that resumes after the laptop sleeps or goes offline.

## Telegram commands (examples)

| Command | What you get |
|---|---|
| `/stock NABIL` | Full analysis: grade, score, plan, targets, and the setup's past record |
| `/chart NABIL` | Price chart with EMA 20/50/200, volume, stop and target lines |
| `/top10`, `/buy`, `/watch` | Best-ranked stocks, today's candidates, stocks to wait for |
| `/bought NABIL 100 520`, `/positions` | Log a trade; see holdings with trailing stops and profit after fees |
| `/report`, `/retest` | How past signals performed; monthly re-test of the setups |

The full guide is in [`HOW_TO_USE.md`](HOW_TO_USE.md).

## Project structure

```
nepse_bot/
  bot/        Telegram commands, report text, scheduled jobs
  engine/     scoring, setups, trade plans, exits, evidence grades
  ingest/     daily data updates and broker-history backfill
  sources/    data source parsers (NEPSE, ShareHub, ShareSansar, Chukul)
  db/         SQLite schema and storage
  backtest/   replay, trade simulation, research studies
  portfolio/  fees and tax, positions, trade journal, protection mode
  events/     news, book closures, lock-in expiries
  alerts/     automatic alert rules and messages
config/       every threshold the engine uses (YAML)
tests/        204 automated tests with saved sample data
docs/         design notes, backtest method, research findings
```

## Tech stack

Python 3.11 · pandas · NumPy · SQLite · python-telegram-bot · requests / BeautifulSoup · matplotlib ·
backtesting.py · pytest

## Running it

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
copy .env.example .env                                         # add your own Telegram bot token and chat ID
venv\Scripts\python.exe main.py
```

To run the tests: `venv\Scripts\python.exe -m pytest -q`

## How it was built

I am a civil engineer, not a professional programmer. I designed this bot and directed its development in stages
with AI coding assistants (Claude and Codex). At each stage I set the requirements, tested the bot on live market
data, and reviewed the backtest results before approving the next stage. The stage-by-stage log is in
[`PROGRESS.md`](PROGRESS.md), and the working rules I gave the assistants are in [`CLAUDE.md`](CLAUDE.md).

## Disclaimer

This is a personal research project, not financial advice. Market data comes from public websites and may be
delayed or wrong. Use at your own risk.
