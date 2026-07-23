-- Migration b004: ingestion runs, raw provider responses, and the
-- game_status_history provenance foreign key.
--
-- Version 004: migration numbers are a single global sequence and the phase
-- letter is cosmetic (DATA_ARCHITECTURE.md §3.1). Restarting at b001 would
-- collide with a001.
--
-- Part 1 -- `ingestion_runs`. One row per invocation of an ingest command,
-- recording what was asked for, what came back, and how it ended. This is the
-- table that makes "why does the corpus contain this?" answerable months
-- later. It is deliberately NOT append-only: a run is opened as 'started' and
-- closed with its counters and terminal status, which is a mutation of the
-- same row. Its immutable content is the raw responses it produced.
--
-- Part 2 -- `raw_responses`. The bytes exactly as the provider sent them,
-- written BEFORE anything is normalized, so a parse failure loses nothing and
-- a re-parse months later is possible. Append-only, enforced by triggers.
--
-- Part 3 -- `game_status_history.raw_response_id` gains its foreign key.
-- a002 left it nullable with no FK because `raw_responses` did not exist yet.
-- It stays nullable (Phase A's `record_status()` and the not-yet-built
-- official-provider ingestion write status rows with no owning response), but
-- a value that *is* supplied must now name a real row -- a dangling provenance
-- pointer is exactly the unauditable corpus this design exists to prevent.
--
-- Credential safety (DATA_ARCHITECTURE.md §4.3): `endpoint` stores a path and
-- never a query string; `request_params_json` is masked by parameter name;
-- `response_headers_json` is an allow-list, so no authorization header can be
-- stored; `error_message` is sanitized before it arrives here.

-- --------------------------------------------------------------------------
-- Part 1: ingestion runs.
-- --------------------------------------------------------------------------
CREATE TABLE ingestion_runs (
    run_id            TEXT PRIMARY KEY,
    -- The CLI command that opened the run: 'ingest-odds', later 'ingest-kalshi'.
    command           TEXT NOT NULL,
    provider          TEXT NOT NULL,
    -- League/sport scope of the run; NULL for provider-wide operations.
    sport             TEXT,
    -- The provider operation invoked, e.g. 'get_odds'.
    operation         TEXT NOT NULL,
    -- SANITIZED invocation arguments, canonical JSON. Never a secret value.
    args_json         TEXT NOT NULL,
    status            TEXT NOT NULL,
    -- When the run was asked for (process start), when work actually began,
    -- and when it finished. Kept separate so queue time is measurable rather
    -- than folded into execution time.
    requested_at      TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    -- Durations come from the monotonic clock, never from subtracting two
    -- wall-clocks (CLAUDE.md: latency accounting uses monotonic clocks).
    started_monotonic_ns INTEGER NOT NULL,
    duration_ns       INTEGER,
    requests_made     INTEGER NOT NULL DEFAULT 0,
    -- Provider records seen, normalized, written, skipped as already-known,
    -- and refused by validation. Kept as five separate counters because
    -- "1000 received, 0 inserted" and "0 received" are different incidents.
    records_received  INTEGER NOT NULL DEFAULT 0,
    records_normalized INTEGER NOT NULL DEFAULT 0,
    records_inserted  INTEGER NOT NULL DEFAULT 0,
    records_deduplicated INTEGER NOT NULL DEFAULT 0,
    records_rejected  INTEGER NOT NULL DEFAULT 0,
    -- Exception class name only, and a SANITIZED message.
    error_type        TEXT,
    error_message     TEXT,
    tool_version      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT ingestion_runs_id_prefix CHECK (run_id LIKE 'run\_%' ESCAPE '\'),
    CONSTRAINT ingestion_runs_status_valid CHECK (status IN (
        'started', 'succeeded', 'partially_succeeded', 'failed'
    )),
    -- A finished run must say when it finished; an open one must not pretend to.
    CONSTRAINT ingestion_runs_completion_paired CHECK (
        (status = 'started' AND completed_at IS NULL)
        OR (status <> 'started' AND completed_at IS NOT NULL)
    ),
    CONSTRAINT ingestion_runs_counts_non_negative CHECK (
        requests_made >= 0
        AND records_received >= 0
        AND records_normalized >= 0
        AND records_inserted >= 0
        AND records_deduplicated >= 0
        AND records_rejected >= 0
    ),
    CONSTRAINT ingestion_runs_duration_non_negative
        CHECK (duration_ns IS NULL OR duration_ns >= 0),
    CONSTRAINT ingestion_runs_requested_iso CHECK (requested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ingestion_runs_started_iso CHECK (started_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ingestion_runs_completed_iso
        CHECK (completed_at IS NULL OR completed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ingestion_runs_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_ingestion_runs_provider ON ingestion_runs (provider, started_at);
CREATE INDEX idx_ingestion_runs_command ON ingestion_runs (command, started_at);
CREATE INDEX idx_ingestion_runs_status ON ingestion_runs (status, started_at);

-- --------------------------------------------------------------------------
-- Part 2: raw provider responses.
--
-- NOT deduplicated on content_hash. Two fetches returning identical bytes are
-- two distinct observations, each belonging to its own run; collapsing them
-- would leave the second run unable to name the response it actually received
-- without a further link table. content_hash is indexed, so identical-content
-- detection stays a cheap lookup -- it is simply not a uniqueness rule.
-- Idempotency is enforced where it matters, on the derived price snapshots.
-- --------------------------------------------------------------------------
CREATE TABLE raw_responses (
    raw_response_id   TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    provider          TEXT NOT NULL,
    -- SANITIZED request path. Never a full URL, never a query string: the
    -- Odds API key travels as ?apiKey=, so a stored URL would store the key.
    endpoint          TEXT NOT NULL,
    -- SANITIZED request parameters, canonical JSON. Masked by parameter name.
    request_params_json TEXT NOT NULL,
    -- The read-only transport policy already refuses every other verb; this
    -- makes the storage layer independently incapable of recording one, so a
    -- future bug cannot quietly persist evidence of a write request.
    http_method       TEXT NOT NULL DEFAULT 'GET',
    http_status       INTEGER NOT NULL,
    -- SANITIZED allow-listed headers, canonical JSON. An authorization header
    -- is not on the allow-list and therefore cannot appear here.
    response_headers_json TEXT NOT NULL,
    content_type      TEXT,
    requested_at      TEXT NOT NULL,
    -- When the bytes arrived. Every fact derived from this response inherits
    -- this value as its observed_at (POINT_IN_TIME_DATA.md §2).
    received_at       TEXT NOT NULL,
    elapsed_ns        INTEGER NOT NULL,
    body              TEXT NOT NULL,
    body_bytes        INTEGER NOT NULL,
    -- sha256 of the body alone.
    body_hash         TEXT NOT NULL,
    -- sha256 over (provider, endpoint, params, body) -- identical content from
    -- a different endpoint is not the same response.
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT raw_responses_id_prefix CHECK (raw_response_id LIKE 'raw\_%' ESCAPE '\'),
    CONSTRAINT raw_responses_get_only CHECK (http_method = 'GET'),
    CONSTRAINT raw_responses_endpoint_has_no_query CHECK (endpoint NOT LIKE '%?%'),
    CONSTRAINT raw_responses_status_range CHECK (http_status BETWEEN 100 AND 599),
    CONSTRAINT raw_responses_elapsed_non_negative CHECK (elapsed_ns >= 0),
    CONSTRAINT raw_responses_body_bytes_non_negative CHECK (body_bytes >= 0),
    CONSTRAINT raw_responses_requested_iso CHECK (requested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT raw_responses_received_iso CHECK (received_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT raw_responses_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_raw_responses_dedup ON raw_responses (content_hash);
CREATE INDEX idx_raw_responses_provider ON raw_responses (provider, requested_at);
CREATE INDEX idx_raw_responses_run ON raw_responses (run_id);

-- Preserved bytes that can be rewritten are not preserved bytes.
CREATE TRIGGER trg_raw_responses_no_update
BEFORE UPDATE ON raw_responses
BEGIN
    SELECT RAISE(ABORT, 'raw_responses is append-only');
END;

CREATE TRIGGER trg_raw_responses_no_delete
BEFORE DELETE ON raw_responses
BEGIN
    SELECT RAISE(ABORT, 'raw_responses is append-only');
END;

-- --------------------------------------------------------------------------
-- Part 3: game_status_history gains the raw-response foreign key.
--
-- SQLite cannot add a constraint to an existing table, so the table is rebuilt
-- exactly as a003 did: drop the append-only triggers, create the replacement,
-- copy every row, drop, rename, recreate indexes and triggers. Nothing
-- references this table by foreign key, so no dependent constraint is
-- disturbed.
--
-- raw_response_id stays NULLABLE and raw_response_hash stays nullable. The
-- original plan tightened both to NOT NULL here; that would break the working
-- `record_status()` contract, because Phase A creates status rows from
-- schedule data with no owning provider response and the official-provider
-- ingestion that will supply one is Phase D work. A column made NOT NULL
-- before it has a producer is filled with an invented value, which is worse
-- than an honest NULL.
-- --------------------------------------------------------------------------
DROP TRIGGER trg_game_status_history_no_update;
DROP TRIGGER trg_game_status_history_no_delete;

CREATE TABLE game_status_history_v3 (
    status_id         TEXT PRIMARY KEY,
    game_id           TEXT NOT NULL REFERENCES games(game_id),
    status            TEXT NOT NULL,
    scheduled_start   TEXT NOT NULL,
    detail            TEXT,
    provider          TEXT NOT NULL,
    provider_timestamp TEXT,
    observed_at       TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    raw_response_id   TEXT REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT,
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT game_status_history_unique
        UNIQUE (game_id, provider, observed_at, content_hash),
    CONSTRAINT game_status_history_status_valid CHECK (status IN (
        'scheduled', 'pregame', 'in_progress', 'final',
        'postponed', 'suspended', 'cancelled', 'rescheduled', 'delayed'
    )),
    CONSTRAINT game_status_history_scheduled_iso
        CHECK (scheduled_start LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT game_status_history_provider_ts_iso
        CHECK (provider_timestamp IS NULL OR provider_timestamp LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT game_status_history_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT game_status_history_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT game_status_history_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

INSERT INTO game_status_history_v3 (
    status_id, game_id, status, scheduled_start, detail, provider,
    provider_timestamp, observed_at, ingested_at, raw_response_id,
    raw_response_hash, content_hash, created_at
)
SELECT
    status_id, game_id, status, scheduled_start, detail, provider,
    provider_timestamp, observed_at, ingested_at, raw_response_id,
    raw_response_hash, content_hash, created_at
FROM game_status_history;

DROP TABLE game_status_history;

ALTER TABLE game_status_history_v3 RENAME TO game_status_history;

CREATE INDEX idx_game_status_asof ON game_status_history (game_id, observed_at);

CREATE INDEX idx_game_status_provider_asof
    ON game_status_history (game_id, provider, observed_at);

CREATE TRIGGER trg_game_status_history_no_update
BEFORE UPDATE ON game_status_history
BEGIN
    SELECT RAISE(ABORT, 'game_status_history is append-only');
END;

CREATE TRIGGER trg_game_status_history_no_delete
BEFORE DELETE ON game_status_history
BEGIN
    SELECT RAISE(ABORT, 'game_status_history is append-only');
END;
