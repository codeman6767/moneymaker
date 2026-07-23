-- Migration b006: transition-aware price-snapshot deduplication.
--
-- b005 shipped UNIQUE (sb_outcome_id, content_hash) on
-- sportsbook_price_snapshots, where content_hash covers the price observation
-- and deliberately excludes observed_at. That globally deduplicates *prices*
-- rather than *transitions*: a line that goes -110 -> -120 -> -110 produces a
-- third observation whose content hashes identically to the first, so
-- `INSERT OR IGNORE` silently discarded it and a real reversal was lost. The
-- failure is worst exactly when the provider omits its own timestamps, because
-- then nothing else distinguishes the two -110 observations. This is the same
-- defect a003 fixed for game_status_history, and it gets the same fix.
--
-- The uniqueness becomes UNIQUE (sb_outcome_id, observed_at, content_hash):
-- the same price at a *different* observation time is storable, while an exact
-- duplicate observation (same outcome, same observed_at, same content) is still
-- rejected -- keeping exact replay idempotent. Logical "nothing changed"
-- collapse moves into the repository, which compares an observation against its
-- immediate temporal predecessor rather than against the whole history (so a
-- reversal appends and an unchanged re-poll does not).
--
-- SQLite cannot drop an inline UNIQUE, so the table is rebuilt: drop the
-- append-only triggers, create the replacement, copy every row, drop, rename,
-- recreate the indexes and the triggers. Nothing references this table by
-- foreign key, so no dependent constraint is disturbed. No historical row is
-- modified or deleted -- the rows are copied verbatim.

DROP TRIGGER trg_sb_price_snapshots_no_update;
DROP TRIGGER trg_sb_price_snapshots_no_delete;

CREATE TABLE sportsbook_price_snapshots_v2 (
    snapshot_id       TEXT PRIMARY KEY,
    sb_outcome_id     TEXT NOT NULL REFERENCES sportsbook_outcomes(sb_outcome_id),
    price_american    INTEGER NOT NULL,
    price_decimal     REAL,
    implied_probability REAL,
    point             REAL,
    bookmaker_last_update TEXT,
    market_last_update    TEXT,
    provider_timestamp TEXT,
    observed_at       TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    run_id            TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT sb_price_snapshots_id_prefix CHECK (snapshot_id LIKE 'sbp\_%' ESCAPE '\'),
    -- Transition-aware: an exact-duplicate observation is rejected; the same
    -- price at a later observed_at is a new, storable observation.
    CONSTRAINT sb_price_snapshots_unique UNIQUE (sb_outcome_id, observed_at, content_hash),
    CONSTRAINT sb_price_snapshots_american_valid
        CHECK (price_american <= -100 OR price_american >= 100),
    CONSTRAINT sb_price_snapshots_decimal_valid
        CHECK (price_decimal IS NULL OR price_decimal > 1.0),
    CONSTRAINT sb_price_snapshots_implied_valid CHECK (
        implied_probability IS NULL
        OR (implied_probability > 0.0 AND implied_probability < 1.0)
    ),
    CONSTRAINT sb_price_snapshots_provider_ts_iso CHECK (
        provider_timestamp IS NULL OR provider_timestamp LIKE '____-__-__T__:__:__%Z'
    ),
    CONSTRAINT sb_price_snapshots_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sb_price_snapshots_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sb_price_snapshots_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

INSERT INTO sportsbook_price_snapshots_v2 (
    snapshot_id, sb_outcome_id, price_american, price_decimal, implied_probability,
    point, bookmaker_last_update, market_last_update, provider_timestamp, observed_at,
    ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, created_at
)
SELECT
    snapshot_id, sb_outcome_id, price_american, price_decimal, implied_probability,
    point, bookmaker_last_update, market_last_update, provider_timestamp, observed_at,
    ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, created_at
FROM sportsbook_price_snapshots;

DROP TABLE sportsbook_price_snapshots;

ALTER TABLE sportsbook_price_snapshots_v2 RENAME TO sportsbook_price_snapshots;

-- The as-of scan and the predecessor lookup both key on (outcome, observed_at).
CREATE INDEX idx_sb_price_asof ON sportsbook_price_snapshots (sb_outcome_id, observed_at);
CREATE INDEX idx_sb_price_raw ON sportsbook_price_snapshots (raw_response_id);
CREATE INDEX idx_sb_price_run ON sportsbook_price_snapshots (run_id);

CREATE TRIGGER trg_sb_price_snapshots_no_update
BEFORE UPDATE ON sportsbook_price_snapshots
BEGIN
    SELECT RAISE(ABORT, 'sportsbook_price_snapshots is append-only');
END;

CREATE TRIGGER trg_sb_price_snapshots_no_delete
BEFORE DELETE ON sportsbook_price_snapshots
BEGIN
    SELECT RAISE(ABORT, 'sportsbook_price_snapshots is append-only');
END;
