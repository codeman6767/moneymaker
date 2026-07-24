-- Migration d013: Phase D3 correctness repairs (forward-only).
--
-- Version 013: migration numbers are a single global forward-only sequence;
-- a001..d012 are immutable and are NOT edited. This migration makes NBA
-- observations sport-correct and preserves the provider's exact injury return
-- estimate, without disturbing any MLB table or the d012 NBA-specific tables.
--
-- Two repairs:
--   1. `injury_snapshots.return_estimate` -- the provider's EXACT return-estimate
--      text (e.g. "Nov 17"), preserved verbatim. The existing `return_date`
--      column now holds ONLY an unambiguous parsed ISO date (or NULL when the
--      provider value is ambiguous / not a full date); no year is ever
--      fabricated. Added with ALTER TABLE ADD COLUMN, which is DDL and does not
--      trip the append-only BEFORE UPDATE/DELETE triggers.
--   2. NBA-typed observation tables `nba_game_results`, `nba_team_statistics`,
--      `nba_player_statistics`. NBA game results / box statistics no longer reuse
--      the baseball-named d011 columns (`home_runs`/`away_runs`/`innings_played`)
--      or the CHECK-only `role IN ('batting','pitching')` values. NBA points are
--      points, the game period is a period, and player statistics carry an
--      NBA-appropriate `stat_group IN ('traditional','advanced')`. No downstream
--      consumer can mistake an NBA row for baseball batting/pitching or
--      runs/innings. Identity still anchors on `provider_game_references` (no
--      second canonical game system); every row is append-only, transition-aware,
--      and carries exact raw-response provenance.

-- ==========================================================================
-- 1. Preserve the exact provider injury return estimate.
-- ==========================================================================
ALTER TABLE injury_snapshots ADD COLUMN return_estimate TEXT;

-- ==========================================================================
-- 2a. NBA game results. Home/away POINTS and the current PERIOD (never runs /
-- innings). Correction detection compares substantive cumulative points and the
-- winner; a normal scheduled -> in_progress -> final progression, a rising score,
-- and a period advancing are NOT corrections.
-- ==========================================================================
CREATE TABLE nba_game_results (
    result_id          TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    home_points        INTEGER,
    away_points        INTEGER,
    period             INTEGER,
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
    CONSTRAINT nbr_id_prefix CHECK (result_id LIKE 'nbr\_%' ESCAPE '\'),
    CONSTRAINT nbr_provider_present CHECK (provider <> ''),
    CONSTRAINT nbr_provider_game_present CHECK (provider_game_id <> ''),
    CONSTRAINT nbr_winning_side_valid
        CHECK (winning_side IS NULL OR winning_side IN ('home', 'away', 'tie')),
    CONSTRAINT nbr_period_positive CHECK (period IS NULL OR period >= 1),
    CONSTRAINT nbr_mapped_status_valid CHECK (mapped_status IN (
        'scheduled', 'pregame', 'warmup', 'in_progress', 'delayed',
        'postponed', 'suspended', 'final', 'cancelled', 'rescheduled', 'unknown'
    )),
    CONSTRAINT nbr_correction_bool CHECK (is_correction IN (0, 1)),
    CONSTRAINT nbr_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nbr_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nbr_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nbr_transition_unique UNIQUE (game_ref_id, observed_at, content_hash)
);

CREATE INDEX idx_nbr_asof ON nba_game_results (game_ref_id, observed_at);

CREATE TRIGGER trg_nbr_no_update
BEFORE UPDATE ON nba_game_results
BEGIN
    SELECT RAISE(ABORT, 'nba_game_results is append-only');
END;
CREATE TRIGGER trg_nbr_no_delete
BEFORE DELETE ON nba_game_results
BEGIN
    SELECT RAISE(ABORT, 'nba_game_results is append-only');
END;

-- ==========================================================================
-- 2b. NBA team statistics. Team POINTS plus the sport-neutral stat line in
-- canonical-JSON `stats`. No baseball columns; missing values stay NULL.
-- ==========================================================================
CREATE TABLE nba_team_statistics (
    stat_id            TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    provider_team_id   TEXT NOT NULL,
    team_id            TEXT REFERENCES teams(team_id),
    home_away          TEXT NOT NULL,
    points             INTEGER,
    stats              TEXT,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT nts_id_prefix CHECK (stat_id LIKE 'nts\_%' ESCAPE '\'),
    CONSTRAINT nts_provider_present CHECK (provider <> ''),
    CONSTRAINT nts_provider_team_present CHECK (provider_team_id <> ''),
    CONSTRAINT nts_home_away_valid CHECK (home_away IN ('home', 'away')),
    CONSTRAINT nts_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nts_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nts_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nts_transition_unique
        UNIQUE (game_ref_id, provider_team_id, observed_at, content_hash)
);

CREATE INDEX idx_nts_game ON nba_team_statistics (game_ref_id, observed_at);
CREATE INDEX idx_nts_team ON nba_team_statistics (provider, provider_team_id, observed_at);

CREATE TRIGGER trg_nts_no_update
BEFORE UPDATE ON nba_team_statistics
BEGIN
    SELECT RAISE(ABORT, 'nba_team_statistics is append-only');
END;
CREATE TRIGGER trg_nts_no_delete
BEFORE DELETE ON nba_team_statistics
BEGIN
    SELECT RAISE(ABORT, 'nba_team_statistics is append-only');
END;

-- ==========================================================================
-- 2c. NBA player statistics. `stat_group IN ('traditional','advanced')` -- an
-- NBA-appropriate discriminator, never baseball 'batting'/'pitching'. The
-- sport-neutral stat line lives in canonical-JSON `stats`; the two stat groups
-- keep distinct transition anchors so re-polls are idempotent, not thrashing.
-- ==========================================================================
CREATE TABLE nba_player_statistics (
    stat_id            TEXT PRIMARY KEY,
    game_ref_id        TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    player_id          TEXT REFERENCES players(player_id),
    provider_team_id   TEXT,
    team_id            TEXT REFERENCES teams(team_id),
    stat_group         TEXT NOT NULL,
    position           TEXT,
    is_starter         INTEGER,
    points             INTEGER,
    stats              TEXT,
    provider_timestamp TEXT,
    published_at       TEXT,
    observed_at        TEXT NOT NULL,
    ingested_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT nps_id_prefix CHECK (stat_id LIKE 'nps\_%' ESCAPE '\'),
    CONSTRAINT nps_provider_present CHECK (provider <> ''),
    CONSTRAINT nps_provider_player_present CHECK (provider_player_id <> ''),
    CONSTRAINT nps_stat_group_valid CHECK (stat_group IN ('traditional', 'advanced')),
    CONSTRAINT nps_starter_bool CHECK (is_starter IS NULL OR is_starter IN (0, 1)),
    CONSTRAINT nps_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nps_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nps_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT nps_transition_unique
        UNIQUE (game_ref_id, provider_player_id, stat_group, observed_at, content_hash)
);

CREATE INDEX idx_nps_game ON nba_player_statistics (game_ref_id, observed_at);
CREATE INDEX idx_nps_player ON nba_player_statistics (provider, provider_player_id, observed_at);

CREATE TRIGGER trg_nps_no_update
BEFORE UPDATE ON nba_player_statistics
BEGIN
    SELECT RAISE(ABORT, 'nba_player_statistics is append-only');
END;
CREATE TRIGGER trg_nps_no_delete
BEFORE DELETE ON nba_player_statistics
BEGIN
    SELECT RAISE(ABORT, 'nba_player_statistics is append-only');
END;
