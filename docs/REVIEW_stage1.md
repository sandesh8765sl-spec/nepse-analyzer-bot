# NEPSE Analyzer Bot - code review (13 Nov 2026) and Stage 1 status

This file records the read-only review done before the upgrade, with one
correction, and what Stage 1 did about each finding.

## Correction to the original review

The original review said NEPSE trades **Sunday-Thursday** and called the
Monday-Friday check in `/currentmarket` a bug. **That was wrong.** NEPSE has
traded **Monday-Friday** since 6 April 2026. The bot's own logs confirm it:
between June and November 2026, live market data appeared on Fridays and never
on Sundays. The real bug was the opposite one: every *scheduled job* ran
Sunday-Thursday, so jobs ran on closed Sundays and skipped open Fridays.

## Findings and their status after Stage 1

| # | Finding | Status |
|---|---|---|
| 1 | Fundamentals unreadable since 25 Sep 2026 (ShareHub changed its page; the page heading was read as the fiscal year). The buy path was dead because fundamentals were a hard gate. | **Fixed.** Reader repaired with two independent methods and fixture tests. Missing fundamentals no longer block analysis and never add points. |
| 2 | ShareSansar security token cached forever, causing HTTP 419 errors that killed whole scans (21 times in the logs). | **Fixed.** On 419/403 the session, token and company cache are dropped and the request is retried once. |
| 3 | Hard-coded weekdays in several places, inconsistent with each other. | **Fixed.** One trading calendar, driven by `config/market.yaml` and `config/holidays.yaml`. |
| 4 | Scheduled jobs silently skipped while the laptop sleeps. | **Partly.** The 16:00 scanner now still runs if the laptop wakes within 3 hours. Full catch-up comes with the data layer (Stage 2) and reports (Stage 5). |
| 5 | "Close below breakdown level" sell rule could never fire. | **Removed.** |
| 6 | No access control. | **Fixed.** Allowlist; strangers get one refusal. |
| 7 | Saved data written in place; an unreadable file was replaced by an empty one. | **Fixed.** Atomic writes; damaged files are renamed `*.corrupt-<timestamp>` and the owner is alerted. |
| 8 | Full-market scan built a new fetcher (and web session) per symbol. | **Fixed.** One shared fetcher, one session per worker thread. |
| 9 | Every analysis during market hours wiped the scan cache; caches were not tied to a date. | **Fixed.** Caches expire by trading date and time. |
| 10 | Research cache never deleted expired entries. | **Fixed.** Pruned on load and daily. |
| 11 | 32 MB log that never rotated. | **Fixed.** `logs/bot.log`, 5 MB x 5 files. |
| 12 | Startup script matched any `main.py` process. | **Fixed.** Matches this project's full path. |

## Still open (later stages)

- Targets far too close to the entry, and no fees or tax in the numbers (Stage 3, trade plan rules).
- Two different scoring systems; a missing SMA200 still adds points in the scanner's score (Stage 3, one engine).
- Signals computed on partial intraday bars when run during the session (Stage 3; the four intraday auto-scans are already switched off).
- NepseAlpha is dead and is only kept as a fallback that fails fast (dropped in Stage 2).
- ShareSansar's own company page has never yielded fundamentals; ShareHub is the only working source (Stage 2 adds more).
- No holiday list existed. `config/holidays.yaml` is now there but must be filled in by the owner.
- `main.py` is still one very large file (split in Stage 7).
