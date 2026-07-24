-- Migration d011: Phase D2 MLB official-data snapshots.
--
-- Version 011: migration numbers are a single global forward-only sequence;
-- a001..d010 are immutable and are NOT edited. This migration adds the
-- append-only official-observation tables D2 ingests from MLB StatsAPI.
--
-- Design notes (documented deviations honoured from PHASE_D_IMPLEMENTATION_PLAN):
--   * NO second canonical game/team/player/season/venue/league table is created.
--     Official game identity is anchored on `provider_game_references`
--     (UNIQUE (provider, provider_game_id) from d009) via `game_ref_id`; the
--     canonical `games` row + linkage is a Phase D5 matching concern, so the
--     snapshots carry provider ids plus NULLABLE canonical ids. One MLB gamePk
--     therefore maps to exactly one provider game reference (its official
--     identity), and a reschedule keeps that identity while appending a new
--     schedule observation.
--   * Every table is APPEND-ONLY with transition-aware dedup
--     `UNIQUE (<anchor…>, observed_at, content_hash)` and BEFORE UPDATE/DELETE
--     triggers, mirroring b006 / c007 / d009. `observed_at` (=
--     raw_responses.received_at) is the point-in-time cutoff; a provider
--     timestamp is never the cross-provider cutoff.
--   * A provider correction is a NEW observation (results carry `is_correction`),
--     never an overwrite. Current state is derived from the newest observation.
--   * Missing values stay NULL (never coerced to zero); contradictions are
--     recorded as data_quality_issues by the ingestor, not silently repaired.

-- ==========================================================================
-- 1. Game schedule snapshots. One append-only observation of the official
-- schedule state per changed content. Probable pitchers are recorded here ONLY
-- when that exact schedule response supplied them; absence stays NULL.
-- ==========================================================================
CREATE TABLE game_schedule_snapshots (
    schedule_id        TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    season             INTEGER,
    game_type          TEXT,
    game_date_local    TEXT,
    scheduled_start    TEXT,
    home_provider_team_id TEXT,
    away_provider_team_id TEXT,
    home_team_id       TEXT REFERENCES teams(team_id),
    away_team_id       TEXT REFERENCES teams(team_id),
    venue_provider_id  TEXT,
    venue_id           TEXT REFERENCES venues(venue_id),
    status_code        TEXT,
    detailed_status    TEXT,
    mapped_status      TEXT NOT NULL,
    game_number        INTEGER,
    doubleheader_code  TEXT,
    reschedule_info    TEXT,
    home_probable_pitcher_id TEXT,
    away_probable_pitcher_id TEXT,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT gss_id_prefix CHECK (schedule_id LIKE 'gss\_%' ESCAPE '\'),
    CONSTRAINT gss_provider_present CHECK (provider <> ''),
    CONSTRAINT gss_provider_game_present CHECK (provider_game_id <> ''),
    CONSTRAINT gss_mapped_status_valid CHECK (mapped_status IN (
        'scheduled', 'pregame', 'warmup', 'in_progress', 'delayed',
        'postponed', 'suspended', 'final', 'cancelled', 'rescheduled', 'unknown'
    )),
    CONSTRAINT gss_game_number_positive CHECK (game_number IS NULL OR game_number >= 1),
    CONSTRAINT gss_local_date_iso CHECK (game_date_local IS NULL OR game_date_local LIKE '____-__-__'),
    CONSTRAINT gss_scheduled_iso
        CHECK (scheduled_start IS NULL OR scheduled_start LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT gss_published_iso
        CHECK (published_at IS NULL OR published_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT gss_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT gss_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT gss_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT gss_transition_unique UNIQUE (game_ref_id, observed_at, content_hash)
);

CREATE INDEX idx_gss_asof ON game_schedule_snapshots (game_ref_id, observed_at);
CREATE INDEX idx_gss_provider_game ON game_schedule_snapshots (provider, provider_game_id, observed_at);

CREATE TRIGGER trg_gss_no_update
BEFORE UPDATE ON game_schedule_snapshots
BEGIN
    SELECT RAISE(ABORT, 'game_schedule_snapshots is append-only');
END;
CREATE TRIGGER trg_gss_no_delete
BEFORE DELETE ON game_schedule_snapshots
BEGIN
    SELECT RAISE(ABORT, 'game_schedule_snapshots is append-only');
END;

-- ==========================================================================
-- 2. Game result snapshots. Final/intermediate results, separate from schedule.
-- Negative/contradictory values are NOT blocked here (the ingestor records a
-- data-quality issue instead); only structural constraints are enforced.
-- ==========================================================================
CREATE TABLE game_result_snapshots (
    result_id          TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    home_runs          INTEGER,
    away_runs          INTEGER,
    home_hits          INTEGER,
    away_hits          INTEGER,
    home_errors        INTEGER,
    away_errors        INTEGER,
    innings_played     INTEGER,
    winning_side       TEXT,
    mapped_status      TEXT NOT NULL,
    result_detail      TEXT,
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
    CONSTRAINT grs_id_prefix CHECK (result_id LIKE 'grs\_%' ESCAPE '\'),
    CONSTRAINT grs_provider_present CHECK (provider <> ''),
    CONSTRAINT grs_provider_game_present CHECK (provider_game_id <> ''),
    CONSTRAINT grs_winning_side_valid
        CHECK (winning_side IS NULL OR winning_side IN ('home', 'away', 'tie')),
    CONSTRAINT grs_mapped_status_valid CHECK (mapped_status IN (
        'scheduled', 'pregame', 'warmup', 'in_progress', 'delayed',
        'postponed', 'suspended', 'final', 'cancelled', 'rescheduled', 'unknown'
    )),
    CONSTRAINT grs_correction_bool CHECK (is_correction IN (0, 1)),
    CONSTRAINT grs_published_iso
        CHECK (published_at IS NULL OR published_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT grs_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT grs_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT grs_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT grs_transition_unique UNIQUE (game_ref_id, observed_at, content_hash)
);

CREATE INDEX idx_grs_asof ON game_result_snapshots (game_ref_id, observed_at);

CREATE TRIGGER trg_grs_no_update
BEFORE UPDATE ON game_result_snapshots
BEGIN
    SELECT RAISE(ABORT, 'game_result_snapshots is append-only');
END;
CREATE TRIGGER trg_grs_no_delete
BEFORE DELETE ON game_result_snapshots
BEGIN
    SELECT RAISE(ABORT, 'game_result_snapshots is append-only');
END;

-- ==========================================================================
-- 3. MLB inning lines. One child row per (game, inning, side, observation).
-- Extra-inning and shortened games are supported (no nine-inning assumption).
-- ==========================================================================
CREATE TABLE mlb_inning_lines (
    line_id            TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    inning             INTEGER NOT NULL,
    side               TEXT NOT NULL,
    runs               INTEGER,
    hits               INTEGER,
    errors             INTEGER,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT mil_id_prefix CHECK (line_id LIKE 'mil\_%' ESCAPE '\'),
    CONSTRAINT mil_provider_present CHECK (provider <> ''),
    CONSTRAINT mil_side_valid CHECK (side IN ('home', 'away')),
    CONSTRAINT mil_inning_positive CHECK (inning >= 1),
    CONSTRAINT mil_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT mil_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT mil_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT mil_transition_unique UNIQUE (game_ref_id, inning, side, observed_at, content_hash)
);

CREATE INDEX idx_mil_game ON mlb_inning_lines (game_ref_id, observed_at, inning, side);

CREATE TRIGGER trg_mil_no_update
BEFORE UPDATE ON mlb_inning_lines
BEGIN
    SELECT RAISE(ABORT, 'mlb_inning_lines is append-only');
END;
CREATE TRIGGER trg_mil_no_delete
BEFORE DELETE ON mlb_inning_lines
BEGIN
    SELECT RAISE(ABORT, 'mlb_inning_lines is append-only');
END;

-- ==========================================================================
-- 4. Team game statistics. Typed key columns + canonical-JSON extra. Missing
-- values stay NULL; only a provider-supplied zero is a zero.
-- ==========================================================================
CREATE TABLE team_game_statistics (
    stat_id            TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    provider_team_id   TEXT NOT NULL,
    team_id            TEXT REFERENCES teams(team_id),
    home_away          TEXT NOT NULL,
    runs               INTEGER,
    hits               INTEGER,
    errors             INTEGER,
    at_bats            INTEGER,
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
    CONSTRAINT tgs_id_prefix CHECK (stat_id LIKE 'tgs\_%' ESCAPE '\'),
    CONSTRAINT tgs_provider_present CHECK (provider <> ''),
    CONSTRAINT tgs_provider_team_present CHECK (provider_team_id <> ''),
    CONSTRAINT tgs_home_away_valid CHECK (home_away IN ('home', 'away')),
    CONSTRAINT tgs_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT tgs_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT tgs_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT tgs_transition_unique
        UNIQUE (game_ref_id, provider_team_id, observed_at, content_hash)
);

CREATE INDEX idx_tgs_game ON team_game_statistics (game_ref_id, observed_at);
CREATE INDEX idx_tgs_team ON team_game_statistics (provider, provider_team_id, observed_at);

CREATE TRIGGER trg_tgs_no_update
BEFORE UPDATE ON team_game_statistics
BEGIN
    SELECT RAISE(ABORT, 'team_game_statistics is append-only');
END;
CREATE TRIGGER trg_tgs_no_delete
BEFORE DELETE ON team_game_statistics
BEGIN
    SELECT RAISE(ABORT, 'team_game_statistics is append-only');
END;

-- ==========================================================================
-- 5. Player game statistics. Batting and pitching kept clearly typed (role +
-- separate JSON blocks). Provider player ids stay provider ids -- canonical
-- resolution is D5, so player_id/team_id are nullable.
-- ==========================================================================
CREATE TABLE player_game_statistics (
    stat_id            TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    player_id          TEXT REFERENCES players(player_id),
    provider_team_id   TEXT,
    team_id            TEXT REFERENCES teams(team_id),
    role               TEXT NOT NULL,
    is_starter         INTEGER,
    batting_order      INTEGER,
    position           TEXT,
    batting_stats      TEXT,
    pitching_stats     TEXT,
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
    CONSTRAINT pgs_id_prefix CHECK (stat_id LIKE 'pgs\_%' ESCAPE '\'),
    CONSTRAINT pgs_provider_present CHECK (provider <> ''),
    CONSTRAINT pgs_provider_player_present CHECK (provider_player_id <> ''),
    CONSTRAINT pgs_role_valid CHECK (role IN ('batting', 'pitching')),
    CONSTRAINT pgs_starter_bool CHECK (is_starter IS NULL OR is_starter IN (0, 1)),
    CONSTRAINT pgs_batting_order_positive
        CHECK (batting_order IS NULL OR batting_order >= 1),
    CONSTRAINT pgs_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pgs_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pgs_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pgs_transition_unique
        UNIQUE (game_ref_id, provider_player_id, role, observed_at, content_hash)
);

CREATE INDEX idx_pgs_game ON player_game_statistics (game_ref_id, observed_at);
CREATE INDEX idx_pgs_player ON player_game_statistics (provider, provider_player_id, observed_at);

CREATE TRIGGER trg_pgs_no_update
BEFORE UPDATE ON player_game_statistics
BEGIN
    SELECT RAISE(ABORT, 'player_game_statistics is append-only');
END;
CREATE TRIGGER trg_pgs_no_delete
BEFORE DELETE ON player_game_statistics
BEGIN
    SELECT RAISE(ABORT, 'player_game_statistics is append-only');
END;

-- ==========================================================================
-- 6. Roster snapshots. Point-in-time team membership; anchored on a provider
-- team reference. A roster observation belongs to its actual observation.
-- ==========================================================================
CREATE TABLE roster_snapshots (
    roster_id          TEXT PRIMARY KEY,
    team_ref_id        TEXT NOT NULL REFERENCES provider_team_references(reference_id),
    provider           TEXT NOT NULL,
    provider_team_id   TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    player_id          TEXT REFERENCES players(player_id),
    roster_date        TEXT,
    roster_status      TEXT,
    jersey_number      TEXT,
    position           TEXT,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT ros_id_prefix CHECK (roster_id LIKE 'ros\_%' ESCAPE '\'),
    CONSTRAINT ros_provider_present CHECK (provider <> ''),
    CONSTRAINT ros_provider_player_present CHECK (provider_player_id <> ''),
    CONSTRAINT ros_roster_date_iso CHECK (roster_date IS NULL OR roster_date LIKE '____-__-__'),
    CONSTRAINT ros_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ros_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ros_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ros_transition_unique
        UNIQUE (team_ref_id, provider_player_id, observed_at, content_hash)
);

CREATE INDEX idx_ros_team ON roster_snapshots (team_ref_id, observed_at);

CREATE TRIGGER trg_ros_no_update
BEFORE UPDATE ON roster_snapshots
BEGIN
    SELECT RAISE(ABORT, 'roster_snapshots is append-only');
END;
CREATE TRIGGER trg_ros_no_delete
BEFORE DELETE ON roster_snapshots
BEGIN
    SELECT RAISE(ABORT, 'roster_snapshots is append-only');
END;

-- ==========================================================================
-- 7. Probable-pitcher snapshots. One announcement timeline; a change appends a
-- new row and never overwrites. 'confirmed' requires explicit provider evidence.
-- ==========================================================================
CREATE TABLE probable_pitcher_snapshots (
    probable_id        TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    side               TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    player_id          TEXT REFERENCES players(player_id),
    status             TEXT NOT NULL DEFAULT 'probable',
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT pps_id_prefix CHECK (probable_id LIKE 'pps\_%' ESCAPE '\'),
    CONSTRAINT pps_provider_present CHECK (provider <> ''),
    CONSTRAINT pps_provider_player_present CHECK (provider_player_id <> ''),
    CONSTRAINT pps_side_valid CHECK (side IN ('home', 'away')),
    CONSTRAINT pps_status_valid CHECK (status IN ('probable', 'confirmed', 'scratched')),
    CONSTRAINT pps_published_iso
        CHECK (published_at IS NULL OR published_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pps_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pps_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pps_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pps_transition_unique UNIQUE (game_ref_id, side, observed_at, content_hash)
);

CREATE INDEX idx_pps_game ON probable_pitcher_snapshots (game_ref_id, side, observed_at);

CREATE TRIGGER trg_pps_no_update
BEFORE UPDATE ON probable_pitcher_snapshots
BEGIN
    SELECT RAISE(ABORT, 'probable_pitcher_snapshots is append-only');
END;
CREATE TRIGGER trg_pps_no_delete
BEFORE DELETE ON probable_pitcher_snapshots
BEGIN
    SELECT RAISE(ABORT, 'probable_pitcher_snapshots is append-only');
END;

-- ==========================================================================
-- 8. Lineup snapshots + ordered lineup players. A posted lineup is one parent
-- observation; a change appends a new parent. `is_confirmed` is set only when
-- the provider explicitly supplies confirmation.
-- ==========================================================================
CREATE TABLE lineup_snapshots (
    lineup_id          TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    provider_team_id   TEXT NOT NULL,
    team_id            TEXT REFERENCES teams(team_id),
    home_away          TEXT,
    is_confirmed       INTEGER NOT NULL DEFAULT 0,
    confirmed_at       TEXT,
    player_count       INTEGER NOT NULL DEFAULT 0,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT lns_id_prefix CHECK (lineup_id LIKE 'lns\_%' ESCAPE '\'),
    CONSTRAINT lns_provider_present CHECK (provider <> ''),
    CONSTRAINT lns_provider_team_present CHECK (provider_team_id <> ''),
    CONSTRAINT lns_home_away_valid CHECK (home_away IS NULL OR home_away IN ('home', 'away')),
    CONSTRAINT lns_confirmed_bool CHECK (is_confirmed IN (0, 1)),
    CONSTRAINT lns_player_count_non_negative CHECK (player_count >= 0),
    CONSTRAINT lns_confirmed_at_iso
        CHECK (confirmed_at IS NULL OR confirmed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT lns_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT lns_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT lns_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT lns_transition_unique
        UNIQUE (game_ref_id, provider_team_id, observed_at, content_hash)
);

CREATE INDEX idx_lns_game ON lineup_snapshots (game_ref_id, provider_team_id, observed_at);

CREATE TRIGGER trg_lns_no_update
BEFORE UPDATE ON lineup_snapshots
BEGIN
    SELECT RAISE(ABORT, 'lineup_snapshots is append-only');
END;
CREATE TRIGGER trg_lns_no_delete
BEFORE DELETE ON lineup_snapshots
BEGIN
    SELECT RAISE(ABORT, 'lineup_snapshots is append-only');
END;

CREATE TABLE lineup_players (
    lineup_player_id   TEXT PRIMARY KEY,
    lineup_id          TEXT NOT NULL REFERENCES lineup_snapshots(lineup_id),
    batting_order      INTEGER NOT NULL,
    provider_player_id TEXT NOT NULL,
    player_id          TEXT REFERENCES players(player_id),
    position           TEXT,
    is_starter         INTEGER,
    created_at         TEXT NOT NULL,
    CONSTRAINT lnp_id_prefix CHECK (lineup_player_id LIKE 'lnp\_%' ESCAPE '\'),
    CONSTRAINT lnp_provider_player_present CHECK (provider_player_id <> ''),
    CONSTRAINT lnp_batting_order_positive CHECK (batting_order >= 1),
    CONSTRAINT lnp_starter_bool CHECK (is_starter IS NULL OR is_starter IN (0, 1)),
    CONSTRAINT lnp_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT lnp_order_unique UNIQUE (lineup_id, batting_order)
);

CREATE INDEX idx_lnp_lineup ON lineup_players (lineup_id, batting_order);

CREATE TRIGGER trg_lnp_no_update
BEFORE UPDATE ON lineup_players
BEGIN
    SELECT RAISE(ABORT, 'lineup_players is append-only');
END;
CREATE TRIGGER trg_lnp_no_delete
BEFORE DELETE ON lineup_players
BEGIN
    SELECT RAISE(ABORT, 'lineup_players is append-only');
END;
