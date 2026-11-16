# Progress

Upgrade of the NEPSE Telegram bot in stages. The owner reviews each stage and says "go" for the next.
Working rules: `CLAUDE.md`. Note: `NEPSE_BOT_UPGRADE_PROMPT_v2.md` is not in this folder (please add it).

## Where things stand (2026-11-14)

| Stage | What | Status |
|---|---|---|
| 1 | Stabilise (Mon-Fri calendar, safe saving, tests) | done, 4e4b832 |
| 2 | Data layer: own SQLite database `data/nepse.sqlite3`, daily ingest | done, 8d2072d |
| 3 | One analysis engine, new short command list | done, 1289907 |
| 4 | Backtest, gate, live journal, `/report` | done, 6091ee9 |
| 4b | Research: can the signals work? Redesign of exits, grades, honest stats | done, eada7f1 |
| 5 | News/events, calendar, position manager, risk rules, daily messages, targets | done, 3b85b90 |
| 5b | Fixes from live testing + automatic alerts | done, this commit |
| Final | Parts 1-7 below (broker data, lock-in, targets, chart, watchlist, protection, re-test, reliability, cleanup, guide) | done, except the broker backtest (data still downloading) |
| Future ideas | Move to the cloud; optional AI answers for `/ask` | not started |

## Final upgrade, Parts 1-7 (2026-11-14): PROJECT COMPLETE, with one open item

**Open item (Part 1): the broker-accumulation backtest. Now fully automatic (2026-11-14).** The bot downloads the
broker history by itself in the background (about 45 hours of PC-on time in total; progress in `/status`), continues
where it stopped after sleep / shutdown / restart / lost internet, then runs the broker study by itself and sends the
result. Nothing to start by hand. All broker features stay OFF unless that study passes the gate. Details below.
**Owner step (Part 5):** run `install_startup_task.ps1` once in an Administrator PowerShell.
User guide: `HOW_TO_USE.md`.

The owner's 7-part request (broker data, lock-in expiry, reference target as information, chart/watchlist/
protection/monthly re-test, laptop reliability, cleanup + /selftest, HOW_TO_USE.md). Do the parts in order;
after each: tests, commit, update this table.

| Part | Status | Exact next step |
|---|---|---|
| 1 Broker data | **Download running; backtest NOT done; setup OFF.** Downloader `sources/chukul.py` + `ingest/broker_backfill.py`. 22 of about 1,120 days in on 2026-11-14 evening. Operator-risk flags and the broker part of the volume score work now. Plumbing check on 11 days only (not a result): 69 trades, profit factor 0.41. Report: `reports/broker/README.md`. | Nothing by hand: the bot finishes the download, runs the broker study and switches on only what passes (see "Broker download" below). When the result message has arrived: write its numbers into `docs/RESEARCH.md`. |
| 2 Lock-in expiry | **done** | Source: ShareHub `/lock-in-period` (promoter + mutual fund), table `lockin_unlocks`, refreshed in the evening update. Shown as a 🔓 warning in /stock, /top10, /buy, /calendar and /positions when within 15 trading days (`config/events.yaml` -> `unlock_flag_trading_days`). Code: `nepse_bot/events/unlocks.py`. ShareHub lists only a few future dates at a time (5 on 2026-11-14). Information only: it does not change a grade. |
| 3 Reference target = information | **done** | `plan.min_reward_risk: 0` (no filter). Grades come only from setup, score, gate and the other rules. A target closer than 1.5x the stop distance shows "⚠️ Resistance close above at X (+Y%)..." in /stock, /top10, /buy and alerts and multiplies the rank by 0.97 (`config/engine.yaml` -> `plan.resistance_*`). TOP PICK no longer filters on it. Early alerts still need a setup graded BUY. Numbers (re-run `python -m nepse_bot.backtest.alerts`, `reports/alerts/backtest.md`): 16 candidates on 2026-11-13 (all SPECULATIVE, all with the warning); 450 of 454 days (99.1%) had at least one plan, 12 a day; TOP PICK 76 alerts in 2 years, 66 finished, 31.8% won, -1.07% per trade, profit factor 0.57 (still a loss). |
| 4 Chart, watchlist, protection, monthly re-test | **done** | `/chart SYMBOL` (`bot/chart.py`: candles, EMA 20/50/200, volume, stop / entry / reference target, or your trailing stop if you hold it). `/watchlist add/remove/show` (`bot/extras.py`); watchlist stocks are in the morning brief and in news / event alerts. Protection mode was NOT there before: added (`portfolio/protection.py`, `config/alerts.yaml` -> `protection`): last 3 sales lost, or portfolio 8% below its peak -> warning + TOP PICK alerts paused 5 sessions. Monthly re-test was NOT there before: added (`backtest/monthly.py`, `/retest`, `/retest run`, `/retest apply`); checked daily at 19:30, runs once per calendar month, writes `reports/monthly/<YYYY-MM>/SUGGESTIONS.md`, changes nothing until `/retest apply`. Not yet run for real (it takes about an hour); first automatic run is due the evening after the bot restarts. |
| 5 Reliability | **done in code; one step is the owner's** | OWNER STEP: run `install_startup_task.ps1` once in an Administrator PowerShell (it could not be run from here: needs admin rights). It registers the bot task (at system start, at logon, 15-minute watchdog), the wake task (10:10, 15:15, 16:55, 17:25, 19:25, 20:10; holds the PC awake 15 min) and switches on Windows wake timers. Until then the old logon-only task stays. In the bot: `catch_up` job every 10 min sends a missed morning brief (until the close), runs a missed data update, sends a missed evening report + alerts (until the next open); never twice (`sent.*` settings). Long jobs ask Windows not to sleep (`utils/keep_awake.py`); the broker download continues where it stopped. Daily backup 20:15 -> `backups/nepse_backup_<date>.zip`, 14 kept, about 18 MB, broker history left out; restore: `docs/RESTORE.md`. DISK: C: had only 4 GB free on 2026-11-14; the broker download stops below 1.5 GB free. |
| 6 Cleanup, /selftest | **done** | About 465 lines of unused code removed (old watchlist / signal-memory methods in `storage/user_store.py`, unused news functions, unused imports); `REVIEW.md` moved to `docs/REVIEW_stage1.md`; old sample reports and one-off logs deleted. Logs: `bot.log` rotates at 5 MB x 5, launcher log capped. Daily housekeeping 06:00 (`utils/maintenance.py`): raw samples, news cache, chart files, log overflow, old monthly re-test tables. `requirements.txt` pinned to the tested versions. `/selftest` (owner only, `bot/selftest.py`; also `python -m nepse_bot.bot.selftest [--dry]`) sends 17 samples marked TEST. Tests: 183 passing. Broker accumulation setup switched OFF in `config/engine.yaml` until its backtest passes (it had started ranking first with no record). |
| 7 HOW_TO_USE.md | **done** | One page: commands with examples, grades, alerts, stop / trailing stop / reference target, logging trades, settings, what to do if the bot stops. |

**Broker download: automatic since 2026-11-14.** Source: Chukul's open trade list (`nepse_bot/sources/chukul.py`),
history from Feb 2022, about 1 request a second, about 45 hours of PC-on time for 1,101 trading days (36 were in on
2026-11-14; all 36 match the price-table volume exactly).

| What | How |
|---|---|
| Starts by itself | A low-priority background thread in the bot (`nepse_bot/ingest/broker_worker.py`), 90 seconds after every bot start. Newest day first. |
| Waits by itself | No internet (checked every 3 minutes); 15:15-17:45 on trading days (data updates, evening scan); the rest of a session in which a live alert check took over 30 seconds; disk below 1.5 GB free (one Telegram message); a download started by hand. A day in progress is dropped at once and started again later. |
| No duplicate or missing days | A day is saved in one step (all or nothing). The list of days to do comes from the database on every step, so nothing is remembered that sleep or a shutdown could lose. |
| Incomplete days | Every saved day is compared with the price table (bought = sold, and at least 99% of the traded volume). An incomplete day is downloaded again and replaced. Days that still differ are counted in `/status`. |
| New days | The daily update saves the new day from NEPSE as before; the worker then checks it and fetches it from Chukul if it is missing or incomplete (also after the history is complete, every 30 minutes). |
| Failed days | Tried again after an hour; left out after 6 failures (counted in the final message). |
| `/status` | "Broker history: X of ~1,101 days (Y%), running / paused (reason), about Z hours of PC-on time left, oldest day so far: DATE". |
| When complete | One Telegram message; then the broker study by itself (`nepse_bot/backtest/broker_study.py`): broker-accumulation setup, broker buying as a filter for each existing setup, and for TOP PICK. Same gate (PF >= 1.2, >= 50 trades, both periods, after fees and tax). Only what passes is switched on; a short result goes to Telegram and to `reports/broker/BROKER_STUDY.md`. |
| Keeps the PC awake | While downloading, Windows does not fall asleep on idle (`keep_pc_awake` in `config/bot.yaml`; the lid and the power button still work). |

Rules: `config/bot.yaml` -> `ingest.broker_backfill`; the filter rule: `config/engine.yaml` -> `broker_filter`.
NOT yet seen for real: the completion message and the study on the full history (the download needs about 43 more hours).

## Stage 5b in short (2026-11-14)

**Fixes from live testing**

| What | Now |
|---|---|
| Candidates with no upside | A plan whose reference target is less than 1.5x the stop distance above the entry becomes WATCH: "buy only on a close above [resistance] on volume". Rule: `config/engine.yaml` -> `plan.min_reward_risk`. |
| Partial-profit level | One rule everywhere (/stock, /buy, alerts, /positions): the reference target. |
| Positions | `/bought SYMBOL QTY PRICE DATE` checks the date against the database (non-trading days are refused); tax uses real holding days; stop and target of a holding come from the highest price since purchase and the current price; unknown dates are asked for (`/bought date SYMBOL YYYY-MM-DD`). |
| Events | Known upcoming events (right share, bonus, dividend, AGM, book closure) stay listed until they happen, with the expected price after the book closure, in `/stock` and `/calendar`. |
| Fundamentals | Companies still on an older quarter are re-checked first; "Others" and sectors with fewer than 5 comparable companies use the all-market median. |
| Plain questions | "why is X falling", "news X", "what should I watch today", "how is the market" (`nepse_bot/bot/ask.py`). |

(Superseded in Part 3: the "no upside" rule is now off; the reference target is information only.)

**Automatic alerts** (`nepse_bot/alerts/`, rules in `config/alerts.yaml`, switches: `/settings alerts`)

- Evening, after the scan: STRONG BUY / BUY alert with the full plan; at most one TOP PICK (speculative); CONFIRMED or CANCELLED for each early alert; a one-time message when a setup is promoted or demoted.
- During the session (every 5 minutes, after 12:30): EARLY alerts, at most 2 a day, only for setups graded BUY.
- Holdings: stop hit or within 2%, raise the stop, target reached, time limit near, news and events. One alert per stock per event per day.
- If the PC was asleep, evening alerts go out on wake while still valid for the next session. Early alerts are never sent late.
- Early alerts have their own row in `/report`; after 20 finished ones the bot says whether to keep them.

**Honest numbers (backtest, last 2 years, after fees and tax)** - `reports/alerts/backtest.md`

- STRONG BUY / BUY alerts: 0 (no setup passes the gate). Early alerts are tied to the same grade, so they are silent too until a setup is proven. Not backtested (no intraday history).
- TOP PICK: 9 alerts in 2 years, 6 finished, 1 won, -2.2% per trade. Rare, and too few cases to prove anything.
- The "no upside" rule removes about 98% of candidates: on 92% of days no stock keeps a plan. The few that pass did no better
  (21% won, -3.2% per trade against 29% and -2.2% for the rest). Lower `plan.min_reward_risk` to get candidates back.
- Fundamentals refresh on 2026-11-14: 11 of 102 lagging companies had a newer quarter (NTC among them); the other 91, PHCL included, have not published one.

## Stage 5 in short (2026-11-14)

**Part A, last research test: FAILED, as documented.** A long-horizon value + quality portfolio (10 stocks,
held 3-12 months, monthly or quarterly, with and without the "overheated" pause) made +35% to +38% in
Aug 2022 - Nov 2024 but lost 0.6% to 4.2% in the last 2 years (profit factor 0.75-0.96; the gate needs 1.2
in both periods). About level with the index (-3.1%), better than the average liquid stock (-9.5%), still a
loss. No `/longterm` list was added and no variations were tried. Details: `docs/RESEARCH.md`, `reports/stage5/longterm.md`.

**Part B, new in the bot**

| What | Command | Where the rules live |
|---|---|---|
| Your holdings: trailing stop, HOLD / TRIM / SELL with reasons, net profit after fees and tax, book-closure and tax notes | `/bought`, `/sold`, `/positions` (`/portfolio` still works) | `config/engine.yaml` -> `plan`, `portfolio_risk` |
| Portfolio risk: number of positions, sector share, single-stock share, rupees at risk if every stop is hit | inside `/positions`, the briefs and `/stock` | `portfolio_risk` |
| News and events: one type per item, usual price effect, fresh only, duplicates merged, one net statement per stock | `/news SYMBOL`, briefs | `config/events.yaml` |
| Calendar: book closures (with the last day to buy), holidays | `/calendar` | database |
| Morning brief 10:15, evening report 17:30 (trading days), weekly summary Friday 18:00 | `/brief`, `/evening`, `/report week` | `config/bot.yaml` -> `reports` |

**Part C, targets.** `/stock` now shows reference targets (next major resistance and 52-week high, each with %
and net rupees), the typical outcome of past winners for that setup, and a "consider booking partial profit"
level. `/top10` and `/buy` show the reference target briefly. Exit style is a setting:
`/settings exit trailing | target | hybrid`. Default is **trailing** (best in 2020-2024 and over the whole
backtest). Target and hybrid lost a little less in the last 2 years but made far less before; `/settings exit`
says so with the numbers (`config/exits.yaml`). All three lost money in the last 2 years.

**Small fixes.** The star is now "📊 full data". "Act on at most N new positions per day" appears only when a
setup is graded BUY.

## Things to know

- The bot records only what you tell it with `/bought` and `/sold`. It never places an order.
- One portfolio for the whole bot (the owner's). Sold or removed positions are kept in the database, never deleted.
- A second `/bought` of the same stock is averaged in; the holding period for tax counts from the first purchase.
- The "usual price effect" of an event type is general market experience, not measured by this bot.
- The spec file `NEPSE_BOT_UPGRADE_PROMPT_v2.md` is still not in this folder; Stage 5 was built from the owner's message.
- Tests: 205 passing.

## Stage 4b result in one paragraph
After testing the score on 209,092 stock-days and 30 entry-rule x exit-style combinations, **nothing has a
positive expectancy after costs outside the period it was tuned on**. Every rule makes money in rising years
(2020, 2021, 2024) and loses in the others. The bot is now a ranking and research tool: it always shows ranked
candidates with a plan, each labelled SPECULATIVE with its real backtest record, and reserves BUY / STRONG BUY
for setups that pass the gate (none does today). Full findings: `docs/RESEARCH.md`. Tables: `reports/stage4b/`.

Out-of-sample (Nov 2024 - Nov 2026, NEPSE -3.1%), trades the owner could really take, net of costs and tax:

| | Trades | Win rate | Avg win / loss | Profit factor | Net profit | Max drawdown |
|---|---|---|---|---|---|---|
| Before (Stage 4 rules) | 18 | 27.8% | Rs 6,968 / -4,661 | 0.58 | Rs -25,747 | Rs 29,751 |
| After (Stage 4b rules) | 122 | 30.3% | Rs 6,942 / -5,227 | 0.58 | Rs -187,465 | Rs 265,134 |

## Decisions made in Stage 4b (the owner delegated the thresholds)

- Stop = 2.5 x average daily range (ATR), at least 4%; no plan if it would exceed 12.5% (too volatile).
- Exit = trailing stop (highest price since entry minus 3 x ATR) or 40 sessions. No fixed target, no 15-day time stop.
- Score needed for a candidate: 65 in every market. The BULL/NEUTRAL/BEAR label is information only.
- "Overheated" market (75%+ of stocks above their 50-day average): warning and 1 new position a day instead of 2.
- Score weights NOT changed: no input ranked stocks reliably in both periods, so re-weighting would be guesswork.
- Gate for BUY: profit factor >= 1.2 over >= 50 trades in BOTH the tuning and the out-of-sample period.
- New verdict SPECULATIVE; `/top10` always lists ten names; every candidate shows its setup's record
  (`config/evidence.yaml`, written by `python -m nepse_bot.backtest`).
- Setups on: trend pullback, base breakout, RS leader, value + turn. Off: event run-up. Broker accumulation waits for data.

## Next step

The project is complete apart from the two items at the top of this file (finish the broker download and its backtest; run the installer as Administrator).
Future ideas, only on the owner's "go": move the bot to the cloud (no dependence on the laptop), optional AI answers for `/ask`. Also worth doing:
1. Use `/bought` for real positions for a few weeks and check the HOLD / TRIM / SELL advice against what you would do.
2. Let the live journal (`/report`, `/report week`) run: it is the cheapest honest test of whether any setup works now.
3. Known data gaps: turnover missing before Feb 2023, fundamentals only from late 2022, broker data still building up.
