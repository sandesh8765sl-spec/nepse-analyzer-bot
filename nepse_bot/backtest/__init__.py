"""Stage 4: replay the live engine on history and measure each setup after costs.

* ``replay``   - runs the real engine on every past trading day and collects its plans.
* ``simulate`` - plays one plan forward on the following days (using backtesting.py).
* ``report``   - turns the trades into the per-setup tables and the pass/fail gate.

Run it with:  python -m nepse_bot.backtest --help
"""
