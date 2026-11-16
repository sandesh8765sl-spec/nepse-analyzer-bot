"""Entry point for the NEPSE Analyzer Bot.

The program itself lives in the ``nepse_bot`` package:

* ``nepse_bot/bot``       - Telegram commands, reports and scheduled jobs
* ``nepse_bot/engine``    - the analysis engine (regime, scoring, setups, trade plans)
* ``nepse_bot/ingest``    - daily data updates into the database
* ``nepse_bot/sources``   - the websites the data comes from
* ``nepse_bot/db``        - the database
* ``nepse_bot/portfolio`` - fees and tax
"""

from nepse_bot.bot.app import main

if __name__ == "__main__":
    main()
