-- Migration d012: Phase D3 NBA-specific official-observation tables.
--
-- Version 012: migration numbers are a single global forward-only sequence;
-- a001..d011 are immutable and are NOT edited. This migration adds ONLY the
-- NBA-specific append-only observation tables the D3 BALLDONTLIE (GOAT) ingestor
-- and the offline hoopR importer need. Everything cross-sport is reused from
-- d011: schedule/result snapshots, team/player game statistics, roster and
-- lineup snapshots, and the provider reference crosswalks from d009.
--
-- Design notes (consistent with d011 and PHASE_D_IMPLEMENTATION_PLAN):
--   * NO second canonical game/team/player system. NBA game identity is anchored
--     on `provider_game_references` (UNIQUE (provider, provider_game_id) from
--     d009) via `game_ref_id`; injuries anchor on `provider_player_references`.
--     Canonical resolution stays a Phase D5 concern (nullable canonical ids).
--   * Every table is APPEND-ONLY with transition-aware dedup
--     `UNIQUE (<anchor…>, observed_at, content_hash)` and BEFORE UPDATE/DELETE
--     triggers, mirroring d011. `observed_at` (= raw_responses.received_at) is
--     the point-in-time cutoff; a provider timestamp is never the cutoff and is
--     never fabricated when the response does not supply it.
--   * Missing numeric values stay NULL -- an explicit provider zero is a zero,
--     an absent period/score is NULL, never coerced to 0.
--   * NBA game RESULTS reuse d011 `game_result_snapshots` (home_runs = home
--     score, away_runs = away score, innings_played = period), so the corrected
--     D2 correction semantics apply unchanged. NBA box/player statistics reuse
--     d011 `team_game_statistics` / `player_game_statistics` (typed baseball
--     columns stay NULL; the sport-neutral stat line lives in `extra`; the two
--     CHECK-permitted `role` values are repurposed: 'batting' = the traditional
--     box line, 'pitching' = the advanced-stats line, keeping their transition
--     anchors distinct so re-polls are idempotent, not thrashing).

-- ==========================================================================
-- 1. NBA quarter/period lines. One append-only row per (game, period, side,
-- observation). ONLY periods the provider actually supplied become rows; a
-- missing period is neither a row nor a fabricated zero, and an explicit 0 is a
-- real 0 (points is NULLABLE so "missing" and "zero" stay distinguishable).
-- Regulation quarters (1..4) AND overtime periods (5+) are supported -- there is
-- no four-period assumption.
-- ==========================================================================
CREATE TABLE nba_quarter_lines (
    line_id            TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    period             INTEGER NOT NULL,
    side               TEXT NOT NULL,
    points             INTEGER,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT nql_id_prefix CHECK (line_id LIKE 'nql\_%' ESCAPE '\'),
    CONSTRAINT nql_provider_present CHECK (provider <> ''),
    CONSTRAINT nql_provider_game_present CHECK (provider_game_id <> ''),
    CONSTRAINT nql_side_valid CHECK (side IN ('home', 'away')),
    CONSTRAINT nql_period_positive CHECK (period >= 1),
    CONSTRAINT nql_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nql_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nql_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nql_transition_unique
        UNIQUE (game_ref_id, period, side, observed_at, content_hash)
);

CREATE INDEX idx_nql_game ON nba_quarter_lines (game_ref_id, observed_at, period, side);

CREATE TRIGGER trg_nql_no_update
BEFORE UPDATE ON nba_quarter_lines
BEGIN
    SELECT RAISE(ABORT, 'nba_quarter_lines is append-only');
END;
CREATE TRIGGER trg_nql_no_delete
BEFORE DELETE ON nba_quarter_lines
BEGIN
    SELECT RAISE(ABORT, 'nba_quarter_lines is append-only');
END;

-- ==========================================================================
-- 2. Injury snapshots. Append-only observations of the provider's actual injury
-- report, anchored on a provider_player_references row. ABSENCE OF A ROW IS NEVER
-- "HEALTHY" -- it is simply unobserved; a supplied-but-missing status is recorded
-- as 'unknown', never as active/available/probable/etc. A changed status or
-- description appends a new observation; an identical replay writes nothing;
-- A -> B -> A keeps all three. `is_correction` is only ever set from genuine
-- provider evidence (default 0); dates/medical conclusions are never invented.
-- game_ref_id is NULLABLE because an injury report is not game-scoped.
-- ==========================================================================
CREATE TABLE injury_snapshots (
    injury_id          TEXT PRIMARY KEY,
    player_ref_id      TEXT NOT NULL REFERENCES provider_player_references(reference_id),
    provider           TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    player_id          TEXT REFERENCES players(player_id),
    provider_team_id   TEXT,
    team_id            TEXT REFERENCES teams(team_id),
    game_ref_id        TEXT REFERENCES provider_game_references(reference_id),
    status             TEXT NOT NULL DEFAULT 'unknown',
    description        TEXT,
    reason             TEXT,
    return_date        TEXT,
    is_correction      INTEGER NOT NULL DEFAULT 0,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT inj_id_prefix CHECK (injury_id LIKE 'inj\_%' ESCAPE '\'),
    CONSTRAINT inj_provider_present CHECK (provider <> ''),
    CONSTRAINT inj_provider_player_present CHECK (provider_player_id <> ''),
    CONSTRAINT inj_status_present CHECK (status <> ''),
    CONSTRAINT inj_correction_bool CHECK (is_correction IN (0, 1)),
    CONSTRAINT inj_return_date_iso CHECK (return_date IS NULL OR return_date LIKE '____-__-__'),
    CONSTRAINT inj_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT inj_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT inj_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT inj_transition_unique
        UNIQUE (player_ref_id, observed_at, content_hash)
);

CREATE INDEX idx_inj_player ON injury_snapshots (player_ref_id, observed_at);
CREATE INDEX idx_inj_provider_player
    ON injury_snapshots (provider, provider_player_id, observed_at);

CREATE TRIGGER trg_inj_no_update
BEFORE UPDATE ON injury_snapshots
BEGIN
    SELECT RAISE(ABORT, 'injury_snapshots is append-only');
END;
CREATE TRIGGER trg_inj_no_delete
BEFORE DELETE ON injury_snapshots
BEGIN
    SELECT RAISE(ABORT, 'injury_snapshots is append-only');
END;

-- ==========================================================================
-- 3. Play snapshots. Append-only play-by-play observations (GOAT plays, and the
-- offline hoopR historical plays/possessions/substitutions boundary). Play
-- identity is the provider's own play id when one genuinely exists; otherwise a
-- deterministic provider-game-scoped identity derived from stable supplied
-- sequence fields (`play_identity`). A correction or any changed play content is
-- a NEW observation; an identical replay writes nothing. Substitutions are
-- best-effort: `is_substitution` is set ONLY when a play genuinely evidences one,
-- never inferred from lineup or endpoint access.
-- ==========================================================================
CREATE TABLE play_snapshots (
    play_id            TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    provider_play_id   TEXT,
    play_identity      TEXT NOT NULL,
    period             INTEGER,
    play_sequence      INTEGER,
    clock              TEXT,
    event_type         TEXT,
    description        TEXT,
    provider_team_id   TEXT,
    provider_player_id TEXT,
    is_substitution    INTEGER NOT NULL DEFAULT 0,
    extra              TEXT,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT ply_id_prefix CHECK (play_id LIKE 'ply\_%' ESCAPE '\'),
    CONSTRAINT ply_provider_present CHECK (provider <> ''),
    CONSTRAINT ply_provider_game_present CHECK (provider_game_id <> ''),
    CONSTRAINT ply_identity_present CHECK (play_identity <> ''),
    CONSTRAINT ply_period_positive CHECK (period IS NULL OR period >= 1),
    CONSTRAINT ply_substitution_bool CHECK (is_substitution IN (0, 1)),
    CONSTRAINT ply_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ply_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ply_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ply_transition_unique
        UNIQUE (game_ref_id, play_identity, observed_at, content_hash)
);

CREATE INDEX idx_ply_game ON play_snapshots (game_ref_id, observed_at, play_sequence);

CREATE TRIGGER trg_ply_no_update
BEFORE UPDATE ON play_snapshots
BEGIN
    SELECT RAISE(ABORT, 'play_snapshots is append-only');
END;
CREATE TRIGGER trg_ply_no_delete
BEFORE DELETE ON play_snapshots
BEGIN
    SELECT RAISE(ABORT, 'play_snapshots is append-only');
END;
