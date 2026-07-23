-- Migration c007: Kalshi public events, markets, order-book snapshots and
-- levels, and public trades.
--
-- Version 007: migration numbers are a single global sequence and the phase
-- letter is cosmetic (DATA_ARCHITECTURE.md §3.1). Phase B ended at 006, so
-- Phase C continues at 007.
--
-- All data here is PUBLIC and read-only. There is no account, balance,
-- position, fill, or order column anywhere in this migration -- the public
-- trade feed records anonymous market-wide prints, never our fills, because we
-- have none. A Phase C test asserts this against PRAGMA table_info.
--
-- Order books deserve care. Kalshi publishes resting BIDS on two sides (yes and
-- no); the executable Yes ask is 100 - best No bid, and vice versa. The derived
-- asks are stored, never a wire ask, and every ladder level is preserved in
-- kalshi_orderbook_levels (separate from the snapshot metadata), so no consumer
-- can read a bid as an ask.
--
-- Snapshots and trades are append-only (enforced by triggers). Order books use
-- transition-aware deduplication -- UNIQUE (market_ticker, observed_at,
-- content_hash) plus an immediate-temporal-predecessor comparison in the
-- repository -- so a book that returns to an earlier state is preserved, while
-- an unchanged re-poll collapses. Trades are immutable events keyed by their
-- provider identity (or a documented field-based identity when the provider
-- supplies none), so re-ingesting the same trade is idempotent.
--
-- Reuses ingestion_runs and raw_responses; no duplicate audit tables.

-- --------------------------------------------------------------------------
-- Events. event_ticker is the stable provider identity. game_id (Phase D) is
-- created nullable and stays NULL in Phase C -- no fuzzy game matching here.
-- --------------------------------------------------------------------------
CREATE TABLE kalshi_events (
    kalshi_event_id   TEXT PRIMARY KEY,
    event_ticker      TEXT NOT NULL,
    series_ticker     TEXT,
    title             TEXT,
    sub_title         TEXT,
    category          TEXT,
    status            TEXT,
    mutually_exclusive INTEGER,
    game_id           TEXT REFERENCES games(game_id),
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    first_observed_at TEXT NOT NULL,
    last_observed_at  TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CONSTRAINT kalshi_events_id_prefix CHECK (kalshi_event_id LIKE 'kev\_%' ESCAPE '\'),
    CONSTRAINT kalshi_events_ticker_unique UNIQUE (event_ticker),
    CONSTRAINT kalshi_events_ticker_present CHECK (event_ticker <> ''),
    CONSTRAINT kalshi_events_mutually_exclusive_bool
        CHECK (mutually_exclusive IS NULL OR mutually_exclusive IN (0, 1)),
    CONSTRAINT kalshi_events_first_observed_iso
        CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_events_last_observed_iso
        CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_events_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_events_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_kalshi_events_series ON kalshi_events (series_ticker);
CREATE INDEX idx_kalshi_events_raw ON kalshi_events (raw_response_id);

-- Provider identity and the creating response are fixed at creation; commence
-- metadata (title, status, ...) is mutable current-state refreshed only by a
-- strictly-newer observation (enforced in the repository).
CREATE TRIGGER trg_kalshi_events_identity_immutable
BEFORE UPDATE OF kalshi_event_id, event_ticker, first_observed_at, raw_response_id
ON kalshi_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'kalshi_events identity columns are immutable')
    WHERE NEW.kalshi_event_id <> OLD.kalshi_event_id
       OR NEW.event_ticker <> OLD.event_ticker
       OR NEW.first_observed_at <> OLD.first_observed_at
       OR NEW.raw_response_id <> OLD.raw_response_id;
END;

-- --------------------------------------------------------------------------
-- Markets. market_ticker is the stable provider identity. event_ticker is the
-- provider's event reference (always preserved); kalshi_event_id is the
-- internal FK, set only when the owning event row exists -- a market can be
-- ingested without its event.
-- --------------------------------------------------------------------------
CREATE TABLE kalshi_markets (
    kalshi_market_id  TEXT PRIMARY KEY,
    market_ticker     TEXT NOT NULL,
    event_ticker      TEXT,
    kalshi_event_id   TEXT REFERENCES kalshi_events(kalshi_event_id),
    series_ticker     TEXT,
    title             TEXT,
    subtitle          TEXT,
    yes_sub_title     TEXT,
    no_sub_title      TEXT,
    status            TEXT,
    open_time         TEXT,
    close_time        TEXT,
    expiration_time   TEXT,
    settlement_time   TEXT,
    result            TEXT,
    rules_primary     TEXT,
    rules_secondary   TEXT,
    rules_hash        TEXT,
    game_id           TEXT REFERENCES games(game_id),
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    first_observed_at TEXT NOT NULL,
    last_observed_at  TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CONSTRAINT kalshi_markets_id_prefix CHECK (kalshi_market_id LIKE 'kmk\_%' ESCAPE '\'),
    CONSTRAINT kalshi_markets_ticker_unique UNIQUE (market_ticker),
    CONSTRAINT kalshi_markets_ticker_present CHECK (market_ticker <> ''),
    CONSTRAINT kalshi_markets_open_iso
        CHECK (open_time IS NULL OR open_time LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_markets_close_iso
        CHECK (close_time IS NULL OR close_time LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_markets_expiration_iso
        CHECK (expiration_time IS NULL OR expiration_time LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_markets_settlement_iso
        CHECK (settlement_time IS NULL OR settlement_time LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_markets_first_observed_iso
        CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_markets_last_observed_iso
        CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_markets_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_markets_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_kalshi_markets_event ON kalshi_markets (kalshi_event_id);
CREATE INDEX idx_kalshi_markets_event_ticker ON kalshi_markets (event_ticker);
CREATE INDEX idx_kalshi_markets_series ON kalshi_markets (series_ticker);
CREATE INDEX idx_kalshi_markets_raw ON kalshi_markets (raw_response_id);

CREATE TRIGGER trg_kalshi_markets_identity_immutable
BEFORE UPDATE OF kalshi_market_id, market_ticker, first_observed_at, raw_response_id
ON kalshi_markets
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'kalshi_markets identity columns are immutable')
    WHERE NEW.kalshi_market_id <> OLD.kalshi_market_id
       OR NEW.market_ticker <> OLD.market_ticker
       OR NEW.first_observed_at <> OLD.first_observed_at
       OR NEW.raw_response_id <> OLD.raw_response_id;
END;

-- --------------------------------------------------------------------------
-- Order-book snapshots. Metadata only; the ladder lives in
-- kalshi_orderbook_levels. Derived asks are stored (100 - opposing best bid),
-- never a wire ask. Append-only; transition-aware dedup on
-- (market_ticker, observed_at, content_hash).
-- --------------------------------------------------------------------------
CREATE TABLE kalshi_orderbook_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    kalshi_market_id  TEXT REFERENCES kalshi_markets(kalshi_market_id),
    market_ticker     TEXT NOT NULL,
    best_yes_bid      INTEGER,
    best_no_bid       INTEGER,
    derived_yes_ask   INTEGER,
    derived_no_ask    INTEGER,
    yes_levels        INTEGER NOT NULL DEFAULT 0,
    no_levels         INTEGER NOT NULL DEFAULT 0,
    depth_levels      INTEGER NOT NULL DEFAULT 0,
    provider_timestamp TEXT,
    observed_at       TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    run_id            TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT kalshi_book_id_prefix CHECK (snapshot_id LIKE 'kob\_%' ESCAPE '\'),
    CONSTRAINT kalshi_book_unique UNIQUE (market_ticker, observed_at, content_hash),
    CONSTRAINT kalshi_book_market_present CHECK (market_ticker <> ''),
    CONSTRAINT kalshi_book_best_yes CHECK (best_yes_bid IS NULL OR best_yes_bid BETWEEN 1 AND 99),
    CONSTRAINT kalshi_book_best_no CHECK (best_no_bid IS NULL OR best_no_bid BETWEEN 1 AND 99),
    CONSTRAINT kalshi_book_yes_ask
        CHECK (derived_yes_ask IS NULL OR derived_yes_ask BETWEEN 1 AND 99),
    CONSTRAINT kalshi_book_no_ask
        CHECK (derived_no_ask IS NULL OR derived_no_ask BETWEEN 1 AND 99),
    CONSTRAINT kalshi_book_counts_non_negative
        CHECK (yes_levels >= 0 AND no_levels >= 0 AND depth_levels >= 0),
    CONSTRAINT kalshi_book_provider_ts_iso
        CHECK (provider_timestamp IS NULL OR provider_timestamp LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_book_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_book_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_book_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_kalshi_book_asof ON kalshi_orderbook_snapshots (market_ticker, observed_at);
CREATE INDEX idx_kalshi_book_market ON kalshi_orderbook_snapshots (kalshi_market_id, observed_at);
CREATE INDEX idx_kalshi_book_raw ON kalshi_orderbook_snapshots (raw_response_id);
CREATE INDEX idx_kalshi_book_run ON kalshi_orderbook_snapshots (run_id);

CREATE TRIGGER trg_kalshi_book_no_update
BEFORE UPDATE ON kalshi_orderbook_snapshots
BEGIN
    SELECT RAISE(ABORT, 'kalshi_orderbook_snapshots is append-only');
END;

CREATE TRIGGER trg_kalshi_book_no_delete
BEFORE DELETE ON kalshi_orderbook_snapshots
BEGIN
    SELECT RAISE(ABORT, 'kalshi_orderbook_snapshots is append-only');
END;

-- --------------------------------------------------------------------------
-- Order-book levels. One row per ladder level, belonging to one immutable
-- snapshot. UNIQUE (snapshot_id, side, price) rejects duplicate price levels
-- with conflicting quantities. Append-only, like the snapshot it belongs to.
-- --------------------------------------------------------------------------
CREATE TABLE kalshi_orderbook_levels (
    level_id          TEXT PRIMARY KEY,
    snapshot_id       TEXT NOT NULL REFERENCES kalshi_orderbook_snapshots(snapshot_id),
    side              TEXT NOT NULL,
    price             INTEGER NOT NULL,
    quantity          INTEGER NOT NULL,
    level_index       INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT kalshi_level_id_prefix CHECK (level_id LIKE 'kol\_%' ESCAPE '\'),
    CONSTRAINT kalshi_level_side CHECK (side IN ('yes', 'no')),
    CONSTRAINT kalshi_level_price CHECK (price BETWEEN 1 AND 99),
    CONSTRAINT kalshi_level_qty CHECK (quantity >= 0),
    CONSTRAINT kalshi_level_index CHECK (level_index >= 0),
    CONSTRAINT kalshi_level_unique UNIQUE (snapshot_id, side, price),
    CONSTRAINT kalshi_level_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_kalshi_levels_snapshot ON kalshi_orderbook_levels (snapshot_id, side, level_index);

CREATE TRIGGER trg_kalshi_levels_no_update
BEFORE UPDATE ON kalshi_orderbook_levels
BEGIN
    SELECT RAISE(ABORT, 'kalshi_orderbook_levels is append-only');
END;

CREATE TRIGGER trg_kalshi_levels_no_delete
BEFORE DELETE ON kalshi_orderbook_levels
BEGIN
    SELECT RAISE(ABORT, 'kalshi_orderbook_levels is append-only');
END;

-- --------------------------------------------------------------------------
-- Public trades. Anonymous market-wide prints -- NOT account fills. Keyed by
-- (market_ticker, content_hash): content_hash uses the provider trade id when
-- present, else a documented field-based identity, so re-ingesting the same
-- trade is idempotent while a genuinely different trade appends. Append-only.
-- --------------------------------------------------------------------------
CREATE TABLE kalshi_public_trades (
    trade_id          TEXT PRIMARY KEY,
    provider_trade_id TEXT,
    kalshi_market_id  TEXT REFERENCES kalshi_markets(kalshi_market_id),
    market_ticker     TEXT NOT NULL,
    taker_side        TEXT,
    yes_price         INTEGER,
    no_price          INTEGER,
    count             INTEGER NOT NULL,
    trade_time        TEXT,
    provider_timestamp TEXT,
    observed_at       TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    run_id            TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT kalshi_trade_id_prefix CHECK (trade_id LIKE 'ktr\_%' ESCAPE '\'),
    CONSTRAINT kalshi_trade_unique UNIQUE (market_ticker, content_hash),
    CONSTRAINT kalshi_trade_market_present CHECK (market_ticker <> ''),
    CONSTRAINT kalshi_trade_side CHECK (taker_side IS NULL OR taker_side IN ('yes', 'no')),
    CONSTRAINT kalshi_trade_yes CHECK (yes_price IS NULL OR yes_price BETWEEN 1 AND 99),
    CONSTRAINT kalshi_trade_no CHECK (no_price IS NULL OR no_price BETWEEN 1 AND 99),
    CONSTRAINT kalshi_trade_count CHECK (count >= 0),
    CONSTRAINT kalshi_trade_time_iso
        CHECK (trade_time IS NULL OR trade_time LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_trade_provider_ts_iso
        CHECK (provider_timestamp IS NULL OR provider_timestamp LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_trade_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_trade_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT kalshi_trade_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_kalshi_trades_asof ON kalshi_public_trades (market_ticker, observed_at);
CREATE INDEX idx_kalshi_trades_market ON kalshi_public_trades (kalshi_market_id, trade_time);
CREATE INDEX idx_kalshi_trades_provider_id ON kalshi_public_trades (provider_trade_id);
CREATE INDEX idx_kalshi_trades_raw ON kalshi_public_trades (raw_response_id);
CREATE INDEX idx_kalshi_trades_run ON kalshi_public_trades (run_id);

CREATE TRIGGER trg_kalshi_trades_no_update
BEFORE UPDATE ON kalshi_public_trades
BEGIN
    SELECT RAISE(ABORT, 'kalshi_public_trades is append-only');
END;

CREATE TRIGGER trg_kalshi_trades_no_delete
BEFORE DELETE ON kalshi_public_trades
BEGIN
    SELECT RAISE(ABORT, 'kalshi_public_trades is append-only');
END;
