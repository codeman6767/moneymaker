-- Migration f020: typed, append-only historical market EVENT observations.
--
-- Why this table has to exist
-- ---------------------------
-- `NBA_HISTORICAL_EVENT_CANONICAL_IDENTITY_ARCHITECTURE.md` §11 proved that no
-- v19 table can honestly hold a historical market event observation:
--
--   * `game_schedule_snapshots.game_ref_id` is NOT NULL against
--     `provider_game_references`, so writing a sportsbook event there would
--     first require minting an OFFICIAL game reference for a secondary
--     provider -- exactly the authority §5 forbids.
--   * `sportsbook_events` is mutable current-state (upserted, no trigger stops
--     an in-place UPDATE) and carries no provider snapshot instant at all.
--   * `raw_responses` is genuinely append-only but untyped; auditing from it
--     would make payload-parsing code part of identity evidence and would turn
--     completeness reconciliation into a full JSON scan.
--
-- So the identity audit that v19's own crosswalk triggers demand has nothing to
-- read. This migration supplies the input, and nothing else.
--
-- What this table is, and is NOT
-- ------------------------------
-- One row means exactly: *this provider reported this provider event id, with
-- these verbatim labels and this contemporaneous commence time, in the
-- historical snapshot it returned for this requested bucket.*
--
-- It does NOT mean "this is canonical game G". There is deliberately no
-- `canonical_game_id` column and no `match_decision_id` column: Stage-A
-- acquisition is identity-free BY CONSTRUCTION, and the cleanest enforcement of
-- that is that there is nowhere to record an identity claim. There are likewise
-- no price/odds columns (this is E0 evidence, not E1) and no event-status
-- column (the provider's events payload exposes no such field, so one would be
-- invented evidence).
--
-- Uniqueness deliberately preserves contradictions
-- ------------------------------------------------
-- The key includes `observation_content_hash`. Two CONTENT-DISTINCT
-- observations for the same event id at the same provider snapshot instant must
-- BOTH survive, because that contradiction is precisely what a G5-style
-- identity audit exists to detect. Deduplicating on
-- (event id, snapshot timestamp) alone would silently destroy the evidence the
-- audit is for. `game_schedule_snapshots` already uses this shape:
-- `UNIQUE (game_ref_id, observed_at, content_hash)`.
--
-- Five clocks, kept apart
-- -----------------------
--   requested_at_bucket          what WE asked the provider for
--   provider_snapshot_timestamp  the instant the provider ANSWERED with
--   commence_time                the contemporaneous event field in that
--                                snapshot; NULL is real evidence, not missing
--                                data, and is never backfilled from a
--                                scheduled/final/current value
--   observed_at                  our observation/materialization clock
--   created_at                   the DB record clock
--
-- The provider answers with the nearest snapshot at or before the request, so
-- the first two differ routinely. Neither of our two clocks is ever backdated
-- to a provider instant.
--
-- Not registered in the official-provider source digest
-- -----------------------------------------------------
-- This migration does NOT change `source_corpus_digest` for any existing
-- corpus. That was measured, not assumed: registering a fourth table in the
-- global audited set changes the recomputed digest of a corpus holding ZERO
-- market rows, which would invalidate every accepted audit and crosswalk
-- binding derived under v19. The audited source set is instead selected by
-- PROVIDER CLASS (see `retrospective/sources.py`), which the digest was already
-- partitioned by: it refuses any provider absent from `PROVIDER_LEAGUES`, and
-- every audited query filters `WHERE provider = ?`.
--
-- Additive only. No existing table, trigger, index or row is touched, so a v16,
-- v17, v18 or v19 corpus stays readable and byte-identical.

CREATE TABLE historical_market_event_observations (
    -- Deterministic: `hme_` + a digest of the semantic content. Replay
    -- reproduces it exactly; it depends on no rowid, no wall clock and no
    -- database-local id, so a transported corpus reproduces every id.
    observation_id              TEXT PRIMARY KEY,
    league_id                   TEXT NOT NULL REFERENCES leagues(league_id),
    -- The namespace-qualified SECONDARY provider. Storing a value here grants
    -- no official-provider authority: this table is never consulted by the game
    -- bootstrap, writes no column of `games`, and appears in no official
    -- provider registry.
    provider                    TEXT NOT NULL,
    namespace_generation        TEXT NOT NULL,
    -- The provider's own sport key, verbatim. Multi-sport providers do not
    -- guarantee event ids are unique across sports, so this is part of what
    -- makes the namespace explicit rather than assumed.
    sport_key                   TEXT NOT NULL,
    -- Stored byte-for-byte. Never trimmed, case-folded or Unicode-normalized
    -- into validity -- the CHECK below rejects instead, because a repaired id
    -- is a different id wearing the right shape.
    provider_event_id           TEXT NOT NULL,
    requested_at_bucket         TEXT NOT NULL,
    provider_snapshot_timestamp TEXT NOT NULL,
    -- NULL is a real observation: the provider listed the event and supplied no
    -- commence time. It is never replaced by a scheduled, final or current
    -- value.
    commence_time               TEXT,
    home_team_raw               TEXT NOT NULL,
    away_team_raw               TEXT NOT NULL,
    -- The PORTABLE identity of this observation. `raw_response_id` below is
    -- database-local and cannot serve that role across reconstruction DBs.
    observation_content_hash    TEXT NOT NULL,
    -- Database-local pointer to the preserved exchange. RESTRICT is implicit:
    -- `raw_responses` is append-only, so the parent can never be deleted.
    raw_response_id             TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    observed_at                 TEXT NOT NULL,
    created_at                  TEXT NOT NULL,

    CONSTRAINT hme_id_prefix CHECK (observation_id LIKE 'hme\_%' ESCAPE '\'),
    CONSTRAINT hme_provider_present CHECK (TRIM(provider) <> ''),
    CONSTRAINT hme_generation_present CHECK (TRIM(namespace_generation) <> ''),
    CONSTRAINT hme_sport_key_present CHECK (TRIM(sport_key) <> ''),
    -- Exact lowercase 32-hex, enforced at the DATABASE so direct SQL cannot
    -- introduce a case-flipped, whitespace-padded or Unicode-confusable id that
    -- would coexist as a distinct key pointing at different evidence. GLOB is
    -- case-sensitive and byte-exact; LIKE and a bare length check are not.
    CONSTRAINT hme_event_id_format CHECK (
        provider_event_id GLOB
            '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
            || '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
            || '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
            || '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
        AND length(provider_event_id) = 32
    ),
    CONSTRAINT hme_home_present CHECK (TRIM(home_team_raw) <> ''),
    CONSTRAINT hme_away_present CHECK (TRIM(away_team_raw) <> ''),
    CONSTRAINT hme_content_hash_present CHECK (TRIM(observation_content_hash) <> ''),

    -- Contradiction-preserving. See the header note.
    CONSTRAINT hme_observation_unique UNIQUE (
        provider, namespace_generation, provider_event_id,
        provider_snapshot_timestamp, observation_content_hash)
);

CREATE INDEX idx_hme_namespace
    ON historical_market_event_observations (provider, namespace_generation, league_id);

CREATE INDEX idx_hme_event
    ON historical_market_event_observations (provider, provider_event_id);

CREATE INDEX idx_hme_bucket
    ON historical_market_event_observations (provider, requested_at_bucket);

-- ==========================================================================
-- Append-only. An observation is a record of what a provider said at an
-- instant; there is no such thing as correcting it after the fact. A later,
-- different answer is a NEW observation, which is exactly why the uniqueness
-- key carries the content hash.
-- ==========================================================================
CREATE TRIGGER trg_hme_no_update
BEFORE UPDATE ON historical_market_event_observations
BEGIN
    SELECT RAISE(ABORT, 'historical_market_event_observations is append-only');
END;

CREATE TRIGGER trg_hme_no_delete
BEFORE DELETE ON historical_market_event_observations
BEGIN
    SELECT RAISE(ABORT, 'historical_market_event_observations is append-only');
END;

-- ==========================================================================
-- Every stored timestamp must be a real instant in the one canonical spelling,
-- not merely ISO-shaped. Same technique as f019 D5: GLOB pins the format
-- byte-exactly (including the upper-case `Z`), and the strftime round-trip
-- rejects impossible calendar instants, since SQLite would otherwise normalize
-- 2026-02-30 to 2026-03-02 and store it. IFNULL is essential -- a WHERE that
-- evaluates to NULL is not a failure in SQL, which is how month 99 once slipped
-- through a shape-only check. Hour 24 is legal ISO-8601 end-of-day but is never
-- emitted by `utc_now_iso`, so it is excluded to keep one spelling per instant.
--
-- These are TEXT columns compared lexicographically. A malformed value would
-- mis-order against every well-formed one, so this fails closed rather than
-- storing something that sorts wrongly for the rest of the corpus's life.
-- ==========================================================================
CREATE TRIGGER trg_hme_timestamps_are_real_instants
BEFORE INSERT ON historical_market_event_observations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'requested_at_bucket is not a real instant in the canonical format')
    WHERE NOT (NEW.requested_at_bucket GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.requested_at_bucket, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.requested_at_bucket, 1, 19)), '')
                   = substr(NEW.requested_at_bucket, 1, 19));

    SELECT RAISE(ABORT,
        'provider_snapshot_timestamp is not a real instant in the canonical format')
    WHERE NOT (NEW.provider_snapshot_timestamp GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.provider_snapshot_timestamp, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.provider_snapshot_timestamp, 1, 19)), '')
                   = substr(NEW.provider_snapshot_timestamp, 1, 19));

    SELECT RAISE(ABORT,
        'commence_time is not a real instant in the canonical format')
    WHERE NEW.commence_time IS NOT NULL
      AND NOT (NEW.commence_time GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.commence_time, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.commence_time, 1, 19)), '')
                   = substr(NEW.commence_time, 1, 19));

    SELECT RAISE(ABORT,
        'observed_at is not a real instant in the canonical format')
    WHERE NOT (NEW.observed_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.observed_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.observed_at, 1, 19)), '')
                   = substr(NEW.observed_at, 1, 19));

    SELECT RAISE(ABORT,
        'created_at is not a real instant in the canonical format')
    WHERE NOT (NEW.created_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.created_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.created_at, 1, 19)), '')
                   = substr(NEW.created_at, 1, 19));
END;

-- ==========================================================================
-- The cited exchange must be a SUCCESSFUL response from the SAME provider.
--
-- These are the two invariants that can be checked deterministically, offline,
-- from columns that already exist -- and they are the two that matter most: a
-- non-200 body is a failure, and reading an observation out of one is how a
-- failed request silently becomes evidence of market absence. Provider equality
-- stops an observation citing some other provider's payload entirely.
--
-- Deeper validation -- that the body actually contains this exact event id, and
-- that the wrapper timestamp matches `provider_snapshot_timestamp` -- requires
-- parsing the payload, which is the Stage-A acquisition parser's job, not a
-- generic trigger's. That boundary is deliberate: putting a JSON parse in a
-- trigger would make payload shape a schema concern and would silently couple
-- the database to one provider's response format.
-- ==========================================================================
CREATE TRIGGER trg_hme_raw_response_is_a_successful_same_provider_exchange
BEFORE INSERT ON historical_market_event_observations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'observation cites a raw response from a different provider')
    WHERE (SELECT provider FROM raw_responses
           WHERE raw_response_id = NEW.raw_response_id) IS NOT NEW.provider;

    SELECT RAISE(ABORT,
        'observation cites a non-200 raw response; a failed request is not evidence that a market did or did not exist')
    WHERE (SELECT http_status FROM raw_responses
           WHERE raw_response_id = NEW.raw_response_id) <> 200;
END;
