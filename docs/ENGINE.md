# The analysis engine (Stage 3, revised in Stage 4b)

One engine now produces every verdict. It reads only the bot's database and
only completed daily bars. During market hours its answers are based on the
previous close.

All numbers below live in `config/engine.yaml`.

## What happens in a scan

1. **Market regime** - BULL, NEUTRAL or BEAR from the NEPSE index (versus its
   50- and 200-day averages and their slopes), 10-day breadth, the share of
   stocks above their 50-day average, and the turnover trend.
   The label is shown for information: in backtests it did not predict the
   following weeks. A candidate needs a stock score of 65 in every market.
   When 75% or more of stocks are above their 50-day average the market is
   "overheated": the bot warns and suggests 1 new position a day instead of 2.
2. **Sector strength** - each sector index is rated strong, neutral or weak.
3. **Universe filter** - a stock is IGNORED if it trades under Rs 50 lakh a day
   on average, traded on fewer than 80% of the last 60 days, has fewer than 120
   trading days of history, or is suspended.
4. **Score out of 100** - six parts:

   | Part | Weight | What it looks at |
   |---|---|---|
   | Trend | 25 | Price vs 20/50/200-day averages, their order and slope, weekly trend, higher highs and lows |
   | Momentum | 15 | RSI zone, MACD direction, 20- and 60-day move vs other stocks |
   | Relative strength | 15 | Rank among all stocks; 60-day return vs NEPSE and vs its sector |
   | Volume and broker flow | 15 | Up-day vs down-day turnover, money flow, top brokers' net buying |
   | Fundamentals | 20 | P/E, P/B, earnings growth, ROE, dividend yield, plus NPL and capital adequacy for lenders - all compared **within the sector** |
   | Setup quality | 10 | A triggered setup, how calm the stock is, how tight the recent range is |

   **Missing data** counts as a neutral 50. It never adds points and never
   blocks the analysis; it lowers the confidence level instead.
5. **Setups** - each is a separate strategy: trend pullback, base breakout,
   relative-strength leader, value + turn, broker accumulation (inactive until
   10 days of broker history exist) and event run-up (switched off).
6. **Trade plan rules** (redesigned in Stage 4b, see docs/RESEARCH.md) - a setup becomes a plan if:
   - the entry is at most 3% above the last close (buy the next day near the open);
   - the stop is 2.5x the stock's average daily range below the entry, at least 4%;
   - that stop is not wider than 12.5% (otherwise the stock is too volatile);
   - the position is at least your minimum size, in whole board lots.

   There is no fixed profit target. The exit is a trailing stop (the highest
   price since you bought minus 3x the average daily range) or 40 trading days,
   whichever comes first. The next resistance is shown for information.
7. **Verdict**
   - **STRONG BUY / BUY** - only for a setup that passed the backtest gate
     (profit factor 1.2 or more over 50 or more trades, in both test periods).
   - **SPECULATIVE** - a valid setup and plan, score 65 or more, but the setup
     has not proved itself. The report shows its real backtest record.
   - **WATCH** - a good stock without a valid entry today.
   - **AVOID** - downtrend, operator risk, or weak fundamentals with a weak trend.
   - **IGNORE** - illiquid, too new, or suspended.

   The engine never says SELL about a stock you do not hold.

## What blocks a BUY even when a plan exists

- the score is below 65;
- the stock is overextended (more than 15% above its 20-day average, or RSI above 75);
- a book closure is within 5 trading days;
- an operator-risk pattern is detected;
- the database is behind the last completed trading day.

## What a "real resistance" means here

A peak that stood out for 10 trading days on each side, the most recent high
before today, the 52-week high, or a big round number at least 3% away.
Today's own high is not counted. These levels are shown for reference only;
the plan does not sell at them.

## Not available yet

- **Promoter lock-in expiry flags**: no data source provides the dates.
- **Broker-based checks** (operator risk by broker share, broker accumulation
  setup): they switch on automatically once enough daily broker history exists.
- **Backtest statistics per setup**: Stage 4.
- **HOLD / TRIM / SELL for your own positions**: Stage 5.
