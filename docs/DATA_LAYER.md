# The bot's own database (Stage 2)

The bot now keeps its own copy of market data in `data/nepse.sqlite3`, so that
analysis no longer depends on a website answering at the moment you ask.

## Where each kind of data comes from

| Data | First choice | If that fails |
|---|---|---|
| Each day's prices (open, high, low, close, volume, turnover, trades) | NEPSE's own website (via NepseUnofficialApi) | ShareSansar daily table, then ShareHub |
| Price history older than one year | ShareHub data feed | - |
| NEPSE index and sector indices | ShareHub data feed | NEPSE (today's value only) |
| Company list, sector, status | ShareHub + NEPSE, merged | either one alone |
| Bonus, cash dividend, right shares | ShareHub data feed | - |
| Quarterly reports (fundamentals) | ShareHub data feed | ShareSansar (latest quarter only) |
| Broker flow (floorsheet) | NEPSE's own website | - |
| Company announcements | ShareHub data feed | - |

NepseAlpha is no longer used for the database.

## When it updates

- **15:20 and 17:00** on trading days (times in `config/bot.yaml`).
- **45 seconds after every start**, it fetches any trading days it missed
  (for example while the laptop was asleep).
- **By hand:** send `/dataupdate` in Telegram, or run
  `python -m nepse_bot.ingest daily`.

Only completed sessions are stored. Today's unfinished session is never
written to the database.

## Useful commands (run from the project folder)

```
venv\Scripts\python.exe -m nepse_bot.ingest status      what the database holds
venv\Scripts\python.exe -m nepse_bot.ingest daily       run the evening update now
venv\Scripts\python.exe -m nepse_bot.ingest backfill    download history (resumes if stopped)
```

## Adjusted prices

When a company issues bonus or right shares, its price drops on the first day
the shares trade without that entitlement. Nobody lost money, but an indicator
would see a crash. So every price **before** that day is multiplied by an
adjustment factor. Indicators and backtests use the adjusted prices; you are
always shown the raw prices.

- Bonus of B%: factor = 1 / (1 + B/100).
- Right share of R new shares per share held at issue price S, with the share
  at P the day before: factor = ((P + R x S) / (1 + R)) / P.
- Cash dividends are not adjusted for.

## Data-quality flags

The bot records anything that does not add up in `data_quality_flags`:

- `unexplained_gap` - a one-day fall larger than NEPSE's daily price limit
  allows (10% before 20 April 2026, 15% after) with no known corporate action.
  Usually a bonus, right, split or merger missing from the source.
- `gap_after_book_closure` - a fall of more than 10% within five trading days
  of a book closure that no adjustment explains.
- `action_without_gap` - a large bonus or right whose expected price drop
  could not be found near its book-closure date.

## Alerts you may receive

- A source failed to deliver the same data **two trading days in a row**.
- A field came back **empty for more than half** of the companies.
- A normal trading day had **no trading** according to two sources: the bot
  marks it as a holiday and tells you how to undo that.
- A saved-data file was damaged (from Stage 1).

## Known limits

- **Quarterly history is thin.** The free ShareHub feed gives each company's
  latest quarter plus the same quarter in earlier years (about 10 data points,
  not 40). Other quarters fill in as the year goes on, because the bot stores
  each new quarter when it appears. Full quarterly history is a paid ShareHub
  feature.
- **Publication dates are estimated** as quarter end + 35 days for old
  reports. Reports the bot sees appear from now on get their real first-seen date.
- **Broker flow has no history.** NEPSE publishes the floorsheet for the
  current day only, and hides broker numbers while the session is running.
  The table fills from the first evening update onward.
- **"Listing date"** is stored as the first date a price exists in the database.
