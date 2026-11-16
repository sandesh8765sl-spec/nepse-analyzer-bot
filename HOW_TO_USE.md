# How to use the NEPSE bot

The bot only advises. It never places an order; you place every order yourself.
Honest warning: in backtests **no setup made money after costs over the last 2 years**. Treat the lists as
ranked research, not as buy signals.

## Commands (type them in Telegram)

| Command | Example | What you get |
|---|---|---|
| `/stock` | `/stock NABIL` | Full analysis: grade, score, plan (buy range, stop, quantity), targets, the setup's record |
| `/chart` | `/chart NABIL` | Picture: price, EMA 20/50/200, volume, stop and reference target lines |
| `/top10` | `/top10` | The ten best-ranked stocks today |
| `/buy` | `/buy` | Today's candidates with full plans |
| `/watch` | `/watch` | Good stocks without an entry yet, and the price to wait for |
| `/market`, `/sector` | `/sector hydro` | Market mood, and one sector ranked |
| `/watchlist` | `/watchlist add NABIL NTC`, `/watchlist remove NTC`, `/watchlist` | Your own list. It appears in the morning brief and you get its news and events |
| `/bought`, `/sold` | `/bought NABIL 100 520`, `/sold NABIL 100 560` | Log your trades (see below) |
| `/positions` | `/positions` | Your holdings: HOLD / TRIM / SELL, trailing stop, profit after fees and tax |
| `/calendar` | `/calendar` | Book closures, lock-in expiries, holidays |
| `/news` | `/news NABIL` | Fresh news with its usual price effect |
| `/alert`, `/alerts` | `/alert NABIL above 600` | Your own price alerts |
| `/brief`, `/evening` | `/brief` | The morning brief or evening report on request |
| `/report` | `/report`, `/report week` | How the bot's past signals turned out; weekly summary |
| `/retest` | `/retest`, `/retest apply` | Monthly re-test of the setups: it only suggests; nothing changes until you send `/retest apply` |
| `/settings` | see Settings below | Capital, risk, exit style, alerts |
| `/status`, `/dataupdate`, `/selftest` | `/selftest` | Health check; refresh data now; one sample of every message (marked TEST) |

Plain words also work: "why is NABIL falling", "news NABIL", "how is the market".

## What the grades mean

- **STRONG BUY / BUY**: the stock's setup passed the test in both past periods (profit factor 1.2 or more over 50+
  trades each). None does today, so you will not see these until one is proven.
- **SPECULATIVE** 🧪: a valid setup and plan, but the setup is not proven. An idea to research.
- **TOP PICK**: the single best speculative stock of the day (score 80+, calm stock, healthy sector). Still speculative.
- **WATCH**: good stock, no entry yet. **AVOID**: weak or risky. **IGNORE**: too illiquid or new to judge.
- 📊 = the score rests on complete data. ⚠️ "Resistance close above" = little room to the next ceiling. 🔓 = locked
  promoter or mutual fund shares become sellable within 15 trading days (extra supply).

## What the alerts mean

- **BUY / STRONG BUY alert** (evening): a proven setup qualified at the close. Full plan for the next session.
- **EARLY** (during the session, after 12:30): a proven setup is breaking out now. Buy only part; wait for the evening
  **CONFIRMED** (buy the rest) or **CANCELLED** (it failed: follow the message).
- **TOP PICK** (evening, at most one): the best speculative idea.
- **Position alerts**: stop hit or near, move your stop up, target reached, time limit near, news and events.
- **🛡 Protection mode**: after 3 losing sales in a row, or your portfolio 8% below its peak, the bot warns you and
  pauses TOP PICK alerts for 5 sessions.

## Stop, trailing stop and reference target

- **Stop**: the price at which the idea is wrong. Place a stop-loss order there when you buy. The plan's quantity is
  sized so that being stopped costs about your chosen risk.
- **Trailing stop**: once you hold the stock the stop follows the price up (highest price since you bought, minus
  3x the average daily range). It never moves down. When the bot says "move your stop up to X", change your order.
  A close at or below it means sell.
- **Reference target**: the next major resistance (a past peak). It is information, not a promise and not a filter.
  It is also the "consider selling half" level. After 40 sessions without either, the plan is to sell anyway.

## Logging your trades

- Bought today: `/bought NABIL 100 520`. Bought earlier: `/bought NABIL 100 520 2026-10-01`.
- Sold: `/sold NABIL 100 560` (shows the result after commission, fees and tax). Part sales are fine.
- The bot only knows what you tell it. Without `/bought` there are no position alerts and no trailing stop.

## Settings

- `/settings` shows everything. Change with: `/settings capital 500000`, `/settings risk 1` (percent per trade),
  `/settings min 50000`, `/settings max 100000`, `/settings positions 5`.
- Exit style: `/settings exit trailing` (default), `target` or `hybrid`.
- Alerts on or off: `/settings alerts strong on|off`, `/settings alerts early on|off`, `/settings alerts toppick on|off`.
- Holidays: `/holiday add 2026-12-25 Christmas`.
- Deeper rules (thresholds, times) are in the `config` folder; ask before changing them.

## Broker history (runs by itself)

The bot downloads the broker history in the background whenever the PC is on and online. You do nothing.
`/status` shows the line "Broker history: X of ~1,100 days (Y%), running / paused (reason), about Z hours of PC-on
time left". It waits by itself during the data updates and the evening scan (15:15-17:45 on trading days), without
internet, and when the disk is almost full (one message). After sleep or a restart it continues where it stopped.
When it is complete you get one message, then the bot tests the broker ideas by itself and sends the result;
it switches a broker feature on only if that test is passed.

## If the bot stops answering

1. Wait 1 minute: it restarts itself 20 seconds after a crash, and Windows checks it every 15 minutes.
2. Is the laptop on and online? A shut-down laptop runs nothing. Sleep is fine: missed briefs, reports, alerts and
   data updates are sent when it wakes, if they are still useful.
3. Double-click `start_bot.bat` in the bot's folder. Then send `/status`.
4. Still nothing: open `logs\bot.log` (the last lines say why) and `logs\bot_startup.log`.
5. One-time setup after a Windows reinstall: run `install_startup_task.ps1` in an Administrator PowerShell.
6. Lost or damaged data: restore a daily backup, see `docs\RESTORE.md` (backups are in `backups\`, 14 days kept).
