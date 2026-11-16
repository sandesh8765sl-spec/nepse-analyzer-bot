"""Database tables.

Plain SQL that works on SQLite today and on PostgreSQL later (standard types,
``ON CONFLICT`` upserts, no SQLite-only features). Each entry in MIGRATIONS is
applied once, in order; add new entries at the end, never edit old ones.
"""

from __future__ import annotations

MIGRATIONS: list[str] = [
    # ------------------------------------------------------------------ 1
    """
    CREATE TABLE securities (
        symbol            TEXT PRIMARY KEY,
        name              TEXT,
        sector            TEXT,
        instrument_type   TEXT,
        status            TEXT,            -- active / suspended / delisted
        is_promoter       INTEGER DEFAULT 0,
        nepse_id          INTEGER,
        first_trade_date  TEXT,            -- earliest price we hold (stands in for listing date)
        updated_at        TEXT
    );

    CREATE TABLE daily_prices (
        symbol      TEXT NOT NULL,
        date        TEXT NOT NULL,         -- YYYY-MM-DD, Nepal date
        open        REAL, high REAL, low REAL, close REAL,
        volume      REAL,
        turnover    REAL,
        trades      INTEGER,
        adj_factor  REAL DEFAULT 1.0,      -- multiply raw prices by this to get adjusted prices
        adj_open    REAL, adj_high REAL, adj_low REAL, adj_close REAL,
        source      TEXT,
        PRIMARY KEY (symbol, date)
    );
    CREATE INDEX idx_daily_prices_date ON daily_prices (date);

    CREATE TABLE corporate_actions (
        symbol             TEXT NOT NULL,
        action_type        TEXT NOT NULL,  -- bonus / cash_dividend / right / split / merger
        fiscal_year        TEXT NOT NULL DEFAULT '',
        ratio              REAL,           -- bonus: percent; right: new shares per share held
        amount             REAL,           -- cash dividend percent; right: issue price
        announcement_date  TEXT,
        book_closure_date  TEXT,
        status             TEXT,
        source             TEXT,
        source_id          TEXT NOT NULL DEFAULT '',
        updated_at         TEXT,
        PRIMARY KEY (symbol, action_type, fiscal_year, source_id)
    );
    CREATE INDEX idx_corporate_actions_book ON corporate_actions (book_closure_date);

    CREATE TABLE fundamentals (
        symbol                   TEXT NOT NULL,
        fiscal_year              TEXT NOT NULL,   -- e.g. 2083/2084 (Bikram Sambat)
        quarter                  INTEGER NOT NULL,
        period_end_date          TEXT,
        publication_date         TEXT,            -- first date this report may be used
        publication_date_source  TEXT,            -- observed / estimated
        eps                      REAL,
        eps_annualized           REAL,
        book_value               REAL,
        roe                      REAL,
        roa                      REAL,
        net_profit               REAL,
        distributable_profit     REAL,
        paidup_capital           REAL,
        npl                      REAL,
        car                      REAL,
        cd_ratio                 REAL,
        extra_json               TEXT,            -- every other field the source supplied
        source                   TEXT,
        updated_at               TEXT,
        PRIMARY KEY (symbol, fiscal_year, quarter)
    );

    CREATE TABLE broker_flow_daily (
        date         TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        broker       INTEGER NOT NULL,
        buy_qty      REAL DEFAULT 0, sell_qty REAL DEFAULT 0,
        buy_amount   REAL DEFAULT 0, sell_amount REAL DEFAULT 0,
        PRIMARY KEY (date, symbol, broker)
    );
    CREATE INDEX idx_broker_flow_symbol ON broker_flow_daily (symbol, date);

    CREATE TABLE index_daily (
        index_code  TEXT NOT NULL,         -- NEPSE, BANKING, HYDROPOWER ...
        date        TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        volume REAL, turnover REAL, trades INTEGER,
        is_sector   INTEGER DEFAULT 0,
        source      TEXT,
        PRIMARY KEY (index_code, date)
    );

    CREATE TABLE market_breadth_daily (
        date            TEXT PRIMARY KEY,
        advancers       INTEGER, decliners INTEGER, unchanged INTEGER,
        traded_symbols  INTEGER,
        turnover        REAL,
        pct_above_ma50  REAL,
        pct_above_ma200 REAL
    );

    CREATE TABLE liquidity_metrics (
        symbol              TEXT NOT NULL,
        as_of_date          TEXT NOT NULL,
        avg_turnover_20     REAL,
        avg_trades_20       REAL,
        traded_days_pct_60  REAL,
        PRIMARY KEY (symbol, as_of_date)
    );

    CREATE TABLE news (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT,
        title         TEXT NOT NULL,
        source        TEXT,
        url           TEXT,
        published_at  TEXT,
        sentiment     TEXT,
        kind          TEXT DEFAULT 'news',  -- news / announcement
        UNIQUE (source, url, title)
    );
    CREATE INDEX idx_news_symbol ON news (symbol, published_at);

    CREATE TABLE signals (
        id          TEXT PRIMARY KEY,
        symbol      TEXT NOT NULL,
        created_at  TEXT,
        setup       TEXT,
        verdict     TEXT,
        score       REAL,
        entry       REAL, stop_loss REAL, target_1 REAL, target_2 REAL,
        status      TEXT,
        outcome_price REAL,
        closed_at   TEXT,
        plan_json   TEXT
    );

    CREATE TABLE positions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        opened_at   TEXT,
        quantity    REAL,
        avg_price   REAL,
        stop_loss   REAL, target_1 REAL, target_2 REAL,
        status      TEXT DEFAULT 'open',
        closed_at   TEXT,
        notes       TEXT
    );

    CREATE TABLE trades (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id  INTEGER,
        symbol       TEXT NOT NULL,
        side         TEXT NOT NULL,        -- buy / sell
        trade_date   TEXT,
        quantity     REAL,
        price        REAL,
        fees         REAL,
        tax          REAL,
        net_amount   REAL,
        notes        TEXT
    );

    CREATE TABLE settings (
        key         TEXT PRIMARY KEY,
        value       TEXT,
        updated_at  TEXT
    );

    -- Bookkeeping -------------------------------------------------------
    CREATE TABLE ingest_days (
        date        TEXT PRIMARY KEY,
        status      TEXT NOT NULL,         -- ok / closed / failed
        price_rows  INTEGER DEFAULT 0,
        source      TEXT,
        detail      TEXT,
        updated_at  TEXT
    );

    CREATE TABLE source_runs (
        source      TEXT NOT NULL,
        dataset     TEXT NOT NULL,
        date        TEXT NOT NULL,         -- trading day the run was for
        ok          INTEGER NOT NULL,
        rows        INTEGER DEFAULT 0,
        detail      TEXT,
        updated_at  TEXT,
        PRIMARY KEY (source, dataset, date)
    );

    CREATE TABLE data_quality_flags (
        symbol      TEXT NOT NULL,
        date        TEXT NOT NULL,
        kind        TEXT NOT NULL,
        detail      TEXT,
        created_at  TEXT,
        PRIMARY KEY (symbol, date, kind)
    );
    """,
    # ------------------------------------------------------------------ 2
    """
    CREATE TABLE alerts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL,        -- early / confirmed / cancelled / buy / top_pick / setup / position
        symbol       TEXT NOT NULL,
        day          TEXT NOT NULL,        -- trading day the alert belongs to
        event        TEXT NOT NULL DEFAULT '',   -- position alerts: stop_hit, target ... (one per stock per event per day)
        status       TEXT,                 -- sent / pending / confirmed / cancelled
        sent_at      TEXT,
        detail_json  TEXT,
        UNIQUE (kind, symbol, day, event)
    );
    CREATE INDEX idx_alerts_symbol ON alerts (symbol, day);
    """,
    # ------------------------------------------------------------------ 3
    """
    CREATE TABLE lockin_unlocks (
        symbol          TEXT NOT NULL,
        holder_type     TEXT NOT NULL,     -- Promoter / MutualFund
        unlock_date     TEXT NOT NULL,     -- the day the locked shares may be sold
        allotment_date  TEXT,
        locked_shares   REAL,
        total_shares    REAL,
        source          TEXT,
        updated_at      TEXT,
        PRIMARY KEY (symbol, holder_type, unlock_date)
    );
    CREATE INDEX idx_lockin_date ON lockin_unlocks (unlock_date);
    """,
]
