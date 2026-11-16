# What the Stage 4b research found (2026-11-14)

Question: can the bot's signals be made to work, or can we prove honestly that they can't?

Data: 209,092 stock-days (every liquid stock, every trading day, 2020-01-07 to 2026-11-13),
built by replaying the real engine so that each day only sees what was known that day.
"Tuning period" = before 2024-11-13. "Out-of-sample" = the last 2 years, kept aside.
Detailed tables: `reports/stage4b/` (01 score study, 02 strategy lab, 03 consistency by year, final/SUMMARY.md).

## Short answer

**No tested rule has a positive expectancy after costs that holds up outside the period it was tuned on.**
The bot is therefore a ranking and research tool with honest statistics, not a signal generator.
Every candidate it shows is labelled SPECULATIVE until a setup proves itself, in backtests or in the live journal.

## The owner's four observations

1. *"Exits cut winners short while losers run."* Partly right. In the old plan 58% of all trades hit the
   stop within about 5 days (average -6%), 26% reached target 1 (average +9 to +13%) and the rest died at the
   15-day time stop. The bigger problem was the tight stop: a 4-6% stop sits inside two days of normal movement
   for a NEPSE stock. But wider stops and trailing exits did not turn the result positive (see below).
2. *"The 15-day time stop conflicts with a 20-30 day hold."* Right. Winners needed 23-27 sessions on average.
   The time stop is gone; the plan now allows up to 40 sessions.
3. *"18 trades is too few."* Right. The redesigned rules trade about 15 times as often, and the studies below
   use hundreds of trades and 200,000 stock-days.
4. *"It may be the market regime, not the setups."* Right, and this is the main finding. Every rule made money
   in 2020, 2021 and 2024 (rising market) and lost in 2022, 2023, 2025 and 2026. The setups add little beyond
   "being in the market", and they cost commission and tax.

## Study 1: does the score predict returns?

- Stocks scoring 65-75 beat the average stock by about **0.5% over 20 trading days**, in both periods.
  Stocks scoring 75-85 did the same in the last 2 years but lagged by 0.9% in the tuning period.
  A round trip costs about 0.75% plus 5% tax on gains. The score ranks slightly better than chance, but
  not by enough to pay for trading.
- No part of the score ranks stocks reliably. In the tuning period the rank correlation with the next
  20 days was about zero for every input (trend, momentum, relative strength, volume, fundamentals).
  In the last 2 years cheap stocks (low P/B, low P/E) and calm stocks did better - but they did not in the
  tuning period, so this cannot be trusted yet.
- Year by year, the best 20% of stocks by any input beat the average in some years and lagged in others.
  Nothing has the same sign in more than 5 of 7 years, and nothing by more than about 1% per 20 days.
- **Decision: the weights were NOT changed.** Re-weighting toward what worked in the last 2 years would be
  fitting to noise. The score stays as a quality ranking and is described as such.

## Study 2: market regime and timing

- The BULL / NEUTRAL / BEAR label did not predict the following weeks. After a BULL day the average
  stock rose over the next 20 days in 2020, 2021 and 2024 and fell in 2022, 2023, 2025 and 2026.
- Simple timing rules on the NEPSE index (above its 50- or 200-day average, rising over 20 or 60 days)
  all made money in the tuning period and all lost about 10% in the last 2 years, worse than just holding (-3%).
- The one pattern that held in 5 of 7 years: when more than about 75-80% of stocks are already above
  their 50-day average ("overheated"), buying does badly over the next month.
- **Decision:** the label no longer changes the score a stock needs. The bot warns when the market is
  overheated and suggests 1 new position a day instead of 2. The data does not support a "go fully to cash" rule.

## Study 3: entry rules x exit styles (the strategy lab)

Ten entry rules (the four setups, top score, 60-day momentum, value in an uptrend, buying a washed-out market,
and others) x three exit styles (trailing stop with a 40-day limit; hold 20 days with a stop; half off at
+3 ATR then trail), traded with real position limits and all costs. About 100-270 trades per combination and period.

- Tuning period: profit factors 0.6 to 1.4; most of the profit came from 2020-2021.
- Out-of-sample: **every one of the 30 combinations lost money** (profit factor 0.4 to 1.0).
- Momentum ranking (often suggested for NEPSE) was the worst out-of-sample.
- Only one detail was the same in both periods: calm stocks (average daily range under 2.5% of price) did
  better and jumpy ones (over 5%) lost in both. The plan now refuses stocks that would need a stop wider than 12.5%.

## What was changed in the bot

- **Stop:** 2.5 x the stock's average daily range below the entry (was: under price structure, 4-10%).
- **Exit:** a trailing stop (highest price since entry minus 3 x average daily range) and a 40-session
  limit. No fixed target, no 15-day time stop, no selling half. Chosen on the tuning period only, where it
  was the best exit style for all four setups.
- **Too volatile:** no plan when the stop would be wider than 12.5%.
- **Score needed:** 65 in every market state (was 70 / 75 / 85).
- **Grades:** STRONG BUY and BUY are reserved for setups that pass the gate. Everything else with a valid
  plan is SPECULATIVE and shows its real record: trades, win rate, average win and loss, average result per
  trade after fees and tax.
- **The gate:** profit factor at least 1.2 over at least 50 trades, in BOTH the tuning period and the
  out-of-sample period. (50 trades is about a year of signals for one setup; with fewer, a profit factor is
  mostly luck. Requiring both periods stops a setup that only worked in one market from being called proven.)
- `/top10` always shows ten ranked names; `/buy` shows the candidates with full plans and records.

## What would change this verdict

- A setup whose live journal (`/report`) shows a profit factor above 1.2 over 50 or more finished signals.
- New information the engine does not have yet: broker accumulation (needs months of broker data),
  announcement dates for dividends and book closures, sector liquidity or interest-rate data.
- A market that trends again. The rules are roughly break-even or better in rising years; that is market
  direction, not skill, and the bot cannot tell in advance which kind of year it is.

## Final check: the redesigned engine, replayed on all 1,543 days

Trades the owner could really have taken (2 new a day, 5 open, Rs 1 lakh each), after all costs and tax:

| | Trades | Win rate | Avg win | Avg loss | Profit factor | Net profit | Max drawdown | NEPSE index |
|---|---|---|---|---|---|---|---|---|
| Before (Stage 4 rules), last 2 years | 18 | 27.8% | Rs 6,968 | Rs -4,661 | 0.58 | Rs -25,747 | Rs 29,751 | -3.1% |
| After (Stage 4b rules), last 2 years | 122 | 30.3% | Rs 6,942 | Rs -5,227 | 0.58 | Rs -187,465 | Rs 265,134 | -3.1% |
| After, tuning period 2020 - Nov 2024 | 231 | 37.7% | Rs 10,640 | Rs -5,789 | 1.11 | Rs +92,144 | Rs 297,574 | +125.9% |

- The average win is larger than the average loss (as it already was for the few old trades), but only 30%
  of trades win, so the profit factor is unchanged at 0.58. Changing the exits did not create an edge.
- Because the new rules trade about 7 times as often, following every candidate over the last 2 years would
  have lost far more rupees than the old rules (which hardly traded). **More signals of a losing rule is worse, not better.**
- In the tuning period the same rules made Rs 92,144 while the index rose 126%: far less than simply holding.
- Per setup (every stock, no position limit) the picture is the same: profit factors of 1.7 to 2.4 in
  2020-2024 and 0.3 to 0.6 in the last 2 years. No setup passes the gate; all are graded SPECULATIVE.
- Year by year: 2022 Rs -143k, 2023 +2k, 2024 +87k, 2025 -104k, 2026 -150k.

## Stage 5 addendum (2026-11-14): one last test, a long-horizon value + quality portfolio

Idea: hold 10 stocks for 3-12 months. Each month or quarter rank liquid stocks by sector-relative value
(earnings yield, book-to-price) and quality (ROE, EPS growth, cash dividend yield); optionally pause new buying
while the market is overheated. Fundamentals used only from their publication date; commission, fees, capital
gains tax and dividend tax included. Same split and gate as Stage 4b. Rules were fixed in `config/engine.yaml`
(`longterm`) before the run and not adjusted afterwards. Code: `python -m nepse_bot.backtest.longterm`.
Tables: `reports/stage5/longterm.md`.

| Variant | Tuning (Aug 2022 - Nov 2024) | Out-of-sample (Nov 2024 - Nov 2026) |
|---|---|---|
| Monthly | +38.2%, 37 holdings, PF 4.15 | -3.9%, 36 holdings, PF 0.79 |
| Monthly, pause when overheated | +37.2%, 33, PF 5.16 | -0.6%, 36, PF 0.96 |
| Quarterly | +35.0%, 31, PF 4.27 | -0.8%, 30, PF 0.94 |
| Quarterly, pause when overheated | +35.0%, 31, PF 4.27 | -4.2%, 30, PF 0.75 |
| NEPSE index (no costs) | +24.5% | -3.1% |
| Equal-weight basket of liquid stocks (after costs) | +18.2% | -9.5% |

**Verdict: it fails the gate. No `/longterm` list was added, and no further variations were tried.**

- Out-of-sample every variant lost a little (profit factor 0.75-0.96; the gate needs 1.2), and none reached
  50 finished holdings in either period.
- It was about level with the index in the last 2 years and lost less than the average liquid stock (-9.5%).
  That is the same hint as Study 1 (cheap, calm stocks held up better recently), but it is still a loss, and
  two years with 30-36 holdings is far too little to call it skill.
- The overheated pause helped one variant and hurt the other: no evidence either way.
- Weaknesses of this test, all of which flatter it: fundamentals only exist from late 2022 (2 reports a year,
  most publication dates estimated), so the tuning period is short and mostly a rising market; companies that
  were delisted or merged have no fundamentals in the database (survivorship); the index has no dividends.

## Stage 5 addendum: exit styles (trailing / target / hybrid)

Same Stage 4b candidates, same entries and stops, only the exit differs. `python -m nepse_bot.backtest.exits`,
tables in `reports/stage5/exit_styles.md`, numbers the bot uses in `config/exits.yaml`.

| Exit style | 2020 - Nov 2024 (avg per trade, win rate, PF) | Last 2 years |
|---|---|---|
| trailing (stop only) | +2.90%, 42%, 1.78 | -2.34%, 28%, 0.45 |
| target (sell all at the next major resistance) | +0.11%, 62%, 1.04 | -1.03%, 55%, 0.62 |
| hybrid (half at the resistance, trail the rest) | +0.78%, 54%, 1.29 | -1.52%, 45%, 0.48 |

- Trailing is the default: best in the period the rules were chosen on, and best over the whole backtest.
- Target and hybrid win more often but the wins are small. They lost less in the last 2 years and made far
  less in rising years. **All three lost money in the last 2 years**: the exit does not create an edge.
- Past winners in the last 2 years went a median +5% to +6% above the entry (top quarter +9% to +12%).
  These are the "typical outcome" levels in `/stock`.

## Broker data addendum (2026-11-14): history is downloading, the setup is not tested yet

Historical broker data (who bought and sold, per stock and day) is now being downloaded from Chukul's open trade list,
back to February 2022. The site allows about one request a second, so the full history takes about 45 hours of running
time; 22 of about 1,120 days were in on 2026-11-14. Every downloaded day matches the traded volume exactly.

- Working now: the operator-risk flags and the broker part of the volume score.
- Not done: the backtest of the broker-accumulation setup. The gate (50+ trades, profit factor 1.2+, both periods)
  cannot be applied to three weeks of data, so the setup is **switched off**, as an untested setup must be.
- A plumbing check on 11 days (not a result): 69 trades, 36% won, -1.0% per trade, profit factor 0.41, and about 64
  plans a day, which suggests the rule (top-5 brokers net bought 15%+ of volume in a range of 12% or less) is too loose.

Details and the exact command to finish the test: `reports/broker/README.md`.
