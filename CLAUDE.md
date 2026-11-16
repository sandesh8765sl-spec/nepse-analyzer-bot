# Working rules for this project

The owner is not a programmer. Plain language, no jargon without a one-line explanation.

## Efficiency (the owner pays for every token)

- Short replies. Summary tables only in chat; details go to files under `reports/` or `docs/`.
- Read `PROGRESS.md` first. Do not read the whole codebase: search, then read only the part you need.
- Never print large files, logs or data dumps. Never re-read a file you already read.
- Long jobs: run on a small sample first (`--sample`), then the full job ONCE, in the background.
- The PC has about 8 GB of memory: at most 6 worker processes for backtests.

## How to work

- Work in stages; stop after each and wait for the owner's "go". Commit at the end of each stage.
- Run all tests before every commit: `venv\Scripts\python.exe -m pytest -q`.
- Never print, log or commit secrets (`.env`, tokens, chat IDs). `data/` and `logs/` stay out of git.
- No automatic trading, ever. The bot only advises.
- Never delete the owner's saved data.
- Say so plainly when something does not work or the evidence is weak. Honest numbers over nice numbers.
- Every number the engine decides with lives in `config/` (engine.yaml, market.yaml, evidence.yaml, alerts.yaml).

## Where things are

- `nepse_bot/engine/` - the one analysis engine (scoring, setups, trade plans, evidence grades).
- `nepse_bot/backtest/` - replay, trade simulation, research studies. `python -m nepse_bot.backtest --help`.
- `nepse_bot/portfolio/journal.py` - live record of every signal; `/report` in Telegram.
- `nepse_bot/bot/` - Telegram commands and report text.
- `nepse_bot/portfolio/positions.py` - the owner's holdings: trailing stop, HOLD/TRIM/SELL, risk rules. Commands in `bot/manage.py`, text in `bot/briefs.py`.
- `nepse_bot/events/engine.py` - news and events (types in `config/events.yaml`) and the book-closure calendar.
- `nepse_bot/engine/exits.py` - exit styles and reference targets (`config/exits.yaml`).
- `nepse_bot/alerts/` - automatic alerts: filters (`rules.py`), message text (`text.py`), what is due and what was sent (`service.py`). Rules in `config/alerts.yaml`; Telegram side in `bot/alerting.py`. Samples: `python -m nepse_bot.alerts.samples`.
- `nepse_bot/bot/ask.py` - plain-text questions.
- `nepse_bot/bot/extras.py` - `/chart`, `/watchlist`, `/retest`; `bot/chart.py` draws the picture; `bot/selftest.py` is `/selftest`.
- `nepse_bot/events/unlocks.py` - promoter / mutual fund lock-in expiry. `nepse_bot/portfolio/protection.py` - protection mode.
- `nepse_bot/backtest/monthly.py` - monthly re-test (suggests only). `nepse_bot/sources/chukul.py` + `ingest/broker_backfill.py` - broker history.
- `nepse_bot/ingest/broker_worker.py` - the broker history as an automatic background download inside the bot (pauses, resume, /status line; rules in `config/bot.yaml` -> `ingest.broker_backfill`). `nepse_bot/backtest/broker_study.py` - runs by itself when the download is complete; switches a broker feature on only if the gate is passed.
- `nepse_bot/utils/` - `backup.py` (daily backup, restore: `docs/RESTORE.md`), `maintenance.py` (housekeeping), `keep_awake.py`.
- `HOW_TO_USE.md` - the owner's one-page guide. `install_startup_task.ps1` - Windows tasks (run as Administrator).
- `docs/RESEARCH.md` - what the Stage 4b research found. `docs/BACKTEST.md` - how the backtest works.
- Database: `data/nepse.sqlite3` (the live bot uses it; backtests only read it).

## Restarting the bot

Stop the python processes running `main.py`; the `start_bot.ps1` launcher loop (scheduled task
"NEPSE Analyzer Bot") starts it again after 20 seconds. Check `logs/bot.log` for "Application started".
