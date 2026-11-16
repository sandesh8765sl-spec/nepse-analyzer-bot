# Backups and how to restore one

## What is backed up

Every day at 20:15 (or right after the bot starts, if today's backup is missing) the bot writes one file:

    backups\nepse_backup_YYYY-MM-DD.zip

It holds the database (your positions, trades, signal journal, settings, prices, fundamentals, news),
your own files in `data\` (watchlist, price alerts) and `config\holidays.yaml` and `config\evidence.yaml`.
The newest 14 files are kept; older ones are removed. One file is about 25 MB.

Not in a backup:

- **Broker history.** It is public data and would make each backup ten times larger. The bot downloads it again
  by itself in the background after a restore (about 45 hours of PC-on time; progress in `/status`).
- **The `.env` file** (your Telegram token). Keep your own copy of it somewhere safe.

The backups are on the same disk as the bot. If the laptop's disk fails, they are lost too: now and then copy
the newest zip file to a USB stick or a cloud folder, or set `backup: folder:` in `config\bot.yaml` to a folder
on another drive.

## Make a backup now

    venv\Scripts\python.exe -m nepse_bot.utils.backup

List the backups you have:

    venv\Scripts\python.exe -m nepse_bot.utils.backup list

## Restore a backup

1. **Stop the bot.** Open Task Scheduler, right-click "NEPSE Analyzer Bot" and choose End (or Disable), then end
   any `python.exe` running `main.py` in Task Manager. Otherwise the launcher starts it again after 20 seconds.
2. In a terminal in the bot's folder, with the date of the backup you want:

       venv\Scripts\python.exe -m nepse_bot.utils.backup restore 2026-11-14

3. Start the bot again: in Task Scheduler, Enable and Run "NEPSE Analyzer Bot" (or double-click `start_bot.bat`).
4. Send `/status` and `/positions` in Telegram to check.

Nothing is deleted by a restore. The files that were replaced are moved to `data\before_restore_<date_time>\`.
To undo a restore, stop the bot and copy those files back into `data\`.

## Restore by hand (if the command does not work)

Stop the bot, open the zip file, and copy the files in its `data` folder into the bot's `data` folder
(delete `nepse.sqlite3-wal` and `nepse.sqlite3-shm` there first, if present). Start the bot.
