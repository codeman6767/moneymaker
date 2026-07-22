-- Migration a002: games and their append-only status history.
--
-- `games` holds the CURRENT state of a contest. `games.status` and
-- `games.scheduled_start` are deliberately mutable -- they are the only place
-- a game's present state lives. Every value they ever held is preserved in
-- `game_status_history`, which is append-only and enforced by triggers.
--
-- Point-in-time reads must use `game_status_history` as of a cutoff and must
-- never read `games.status` (see POINT_IN_TIME_DATA.md, DQ-PIT-001).

CREATE TABLE games (
    game_id           TEXT PRIMARY KEY,
    league_id         TEXT NOT NULL REFERENCES leagues(league_id),
    season_id         TEXT NOT NULL REFERENCES seasons(season_id),
    home_team_id      TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id      TEXT NOT NULL REFERENCES teams(team_id),
    -- Current scheduled UTC start; updated on a postponement/reschedule.
    scheduled_start   TEXT NOT NULL,
    -- First scheduled start. Written once, never updated, so "was this game
    -- moved?" is answerable without scanning history.
    original_start    TEXT NOT NULL,
    -- Venue-local date. This, not the UTC date, is the doubleheader key: a
    -- 7pm PT game is 03:00 UTC the following day.
    game_date_local   TEXT NOT NULL,
    game_number       INTEGER NOT NULL DEFAULT 1,
    doubleheader_type TEXT,
    venue             TEXT,
    is_neutral_site   INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    -- Provider identity is kept strictly separate from the canonical game_id.
    official_provider TEXT,
    official_game_key TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CONSTRAINT games_teams_distinct CHECK (home_team_id <> away_team_id),
    CONSTRAINT games_status_valid CHECK (status IN (
        'scheduled', 'pregame', 'in_progress', 'final',
        'postponed', 'suspended', 'cancelled', 'rescheduled', 'delayed'
    )),
    CONSTRAINT games_doubleheader_type_valid
        CHECK (doubleheader_type IS NULL OR doubleheader_type IN ('traditional', 'split')),
    CONSTRAINT games_game_number_positive CHECK (game_number >= 1),
    CONSTRAINT games_neutral_site_bool CHECK (is_neutral_site IN (0, 1)),
    CONSTRAINT games_id_prefix CHECK (game_id LIKE 'gm\_%' ESCAPE '\'),
    CONSTRAINT games_scheduled_start_iso CHECK (scheduled_start LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT games_original_start_iso CHECK (original_start LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT games_local_date_iso CHECK (game_date_local LIKE '____-__-__'),
    -- An official key is meaningless without the provider that issued it.
    CONSTRAINT games_official_key_paired CHECK (
        (official_provider IS NULL AND official_game_key IS NULL)
        OR (official_provider IS NOT NULL AND official_game_key IS NOT NULL)
    ),
    CONSTRAINT games_created_at_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT games_updated_at_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

-- The official provider key, when present, is the anchor of truth and survives
-- reschedules. Partial index so the many rows without one do not collide.
CREATE UNIQUE INDEX idx_games_official_key
    ON games (official_provider, official_game_key)
    WHERE official_provider IS NOT NULL;

-- Natural schedule key. game_number separates the halves of a doubleheader.
CREATE UNIQUE INDEX idx_games_natural
    ON games (league_id, game_date_local, home_team_id, away_team_id, game_number);

CREATE INDEX idx_games_season ON games (season_id);
CREATE INDEX idx_games_scheduled ON games (league_id, scheduled_start);

CREATE TABLE game_status_history (
    status_id         TEXT PRIMARY KEY,
    game_id           TEXT NOT NULL REFERENCES games(game_id),
    status            TEXT NOT NULL,
    -- The start time as believed AT THIS OBSERVATION, not as believed now.
    scheduled_start   TEXT NOT NULL,
    detail            TEXT,
    provider          TEXT NOT NULL,
    -- Valid time: when the provider says it became true. Nullable -- many
    -- providers omit it, and inventing one would fabricate provenance.
    provider_timestamp TEXT,
    -- Transaction time: when WE learned it. Never NULL, never back-dated.
    -- This is the point-in-time cutoff column.
    observed_at       TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    -- Provenance. Nullable in Phase A only: `raw_responses` arrives in Phase B,
    -- and Phase A has no ingestion, so no row can reference one yet. Phase B
    -- adds the foreign key and tightens raw_response_hash to NOT NULL.
    raw_response_id   TEXT,
    raw_response_hash TEXT,
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    -- Idempotent re-observation: the same provider reporting identical content
    -- writes one row, not two.
    CONSTRAINT game_status_history_unique UNIQUE (game_id, provider, content_hash),
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

CREATE INDEX idx_game_status_asof ON game_status_history (game_id, observed_at);

-- Append-only enforcement. Convention does not survive a future contributor;
-- a trigger does. An overwritten historical snapshot silently corrupts every
-- dataset built afterwards and is undetectable after the fact (DQ-PIT-008).
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
