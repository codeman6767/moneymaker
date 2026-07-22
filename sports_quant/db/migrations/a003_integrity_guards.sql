-- Migration a003: cross-table integrity guards and status-history dedup fix.
--
-- Migrations a001 and a002 are immutable once applied; this migration corrects
-- their gaps additively.
--
-- Part 1 -- league consistency. Foreign keys prove that a referenced row
-- exists, but not that it belongs to the same league. Without these triggers an
-- MLB game can reference an NBA season or an NBA team and the database accepts
-- it. That is a silent, corpus-poisoning error: the row looks well-formed, and
-- every downstream join inherits the mistake. Repository validation alone is
-- not enough -- anything holding a connection can write, so the check belongs
-- in the database.
--
-- Part 2 -- status-history deduplication. a002 declared
-- UNIQUE (game_id, provider, content_hash) with a hash that excludes
-- observed_at, which globally deduplicates *states* rather than *transitions*.
-- A game that goes delayed -> in_progress -> delayed (an ordinary rain delay
-- that resumes and re-delays) silently loses the third observation, because it
-- hashes identically to the first. The table is rebuilt here with
-- UNIQUE (game_id, provider, observed_at, content_hash); the repository skips
-- an observation only when it is unchanged from the one immediately preceding
-- it in time. See DATA_ARCHITECTURE.md §3.4.1.

-- --------------------------------------------------------------------------
-- Part 1a: games must agree with their season and both teams on league.
-- --------------------------------------------------------------------------
CREATE TRIGGER trg_games_league_consistency_insert
BEFORE INSERT ON games
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'games.league_id must match the league of games.season_id')
    WHERE NEW.league_id <> (SELECT league_id FROM seasons WHERE season_id = NEW.season_id);

    SELECT RAISE(ABORT, 'games.league_id must match the league of games.home_team_id')
    WHERE NEW.league_id <> (SELECT league_id FROM teams WHERE team_id = NEW.home_team_id);

    SELECT RAISE(ABORT, 'games.league_id must match the league of games.away_team_id')
    WHERE NEW.league_id <> (SELECT league_id FROM teams WHERE team_id = NEW.away_team_id);
END;

-- Scoped to the columns that can break the invariant, so an ordinary status
-- update does not pay for three subqueries.
CREATE TRIGGER trg_games_league_consistency_update
BEFORE UPDATE OF league_id, season_id, home_team_id, away_team_id ON games
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'games.league_id must match the league of games.season_id')
    WHERE NEW.league_id <> (SELECT league_id FROM seasons WHERE season_id = NEW.season_id);

    SELECT RAISE(ABORT, 'games.league_id must match the league of games.home_team_id')
    WHERE NEW.league_id <> (SELECT league_id FROM teams WHERE team_id = NEW.home_team_id);

    SELECT RAISE(ABORT, 'games.league_id must match the league of games.away_team_id')
    WHERE NEW.league_id <> (SELECT league_id FROM teams WHERE team_id = NEW.away_team_id);
END;

-- --------------------------------------------------------------------------
-- Part 1b: games.original_start is written once and never changed.
--
-- It is the anchor for "was this game moved?". A reschedule updates
-- scheduled_start; anything that rewrote original_start would erase the only
-- record that a move happened.
-- --------------------------------------------------------------------------
CREATE TRIGGER trg_games_original_start_immutable
BEFORE UPDATE OF original_start ON games
FOR EACH ROW
WHEN NEW.original_start <> OLD.original_start
BEGIN
    SELECT RAISE(ABORT, 'games.original_start is immutable once the game is created');
END;

-- --------------------------------------------------------------------------
-- Part 1c: alias league denormalization must agree with the owning entity.
--
-- team_aliases.league_id and player_aliases.league_id are denormalized for
-- indexed lookup. A row whose league disagrees with its team/player would be
-- invisible to a correctly scoped query while still existing -- the worst kind
-- of data error, because nothing surfaces it.
-- --------------------------------------------------------------------------
CREATE TRIGGER trg_team_aliases_league_consistency_insert
BEFORE INSERT ON team_aliases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'team_aliases.league_id must match the league of the referenced team')
    WHERE NEW.league_id <> (SELECT league_id FROM teams WHERE team_id = NEW.team_id);
END;

CREATE TRIGGER trg_team_aliases_league_consistency_update
BEFORE UPDATE OF league_id, team_id ON team_aliases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'team_aliases.league_id must match the league of the referenced team')
    WHERE NEW.league_id <> (SELECT league_id FROM teams WHERE team_id = NEW.team_id);
END;

CREATE TRIGGER trg_player_aliases_league_consistency_insert
BEFORE INSERT ON player_aliases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'player_aliases.league_id must match the league of the referenced player')
    WHERE NEW.league_id <> (SELECT league_id FROM players WHERE player_id = NEW.player_id);
END;

CREATE TRIGGER trg_player_aliases_league_consistency_update
BEFORE UPDATE OF league_id, player_id ON player_aliases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'player_aliases.league_id must match the league of the referenced player')
    WHERE NEW.league_id <> (SELECT league_id FROM players WHERE player_id = NEW.player_id);
END;

-- --------------------------------------------------------------------------
-- Part 2: rebuild game_status_history with transition-aware uniqueness.
--
-- SQLite cannot drop a UNIQUE constraint declared inline, so the table is
-- rebuilt. The append-only triggers are dropped first and recreated at the end;
-- DROP TABLE does not fire row triggers, but dropping them explicitly keeps the
-- intent visible. Nothing references this table by foreign key, so no dependent
-- constraint is disturbed.
-- --------------------------------------------------------------------------
DROP TRIGGER trg_game_status_history_no_update;
DROP TRIGGER trg_game_status_history_no_delete;

CREATE TABLE game_status_history_v2 (
    status_id         TEXT PRIMARY KEY,
    game_id           TEXT NOT NULL REFERENCES games(game_id),
    status            TEXT NOT NULL,
    scheduled_start   TEXT NOT NULL,
    detail            TEXT,
    provider          TEXT NOT NULL,
    provider_timestamp TEXT,
    observed_at       TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    raw_response_id   TEXT,
    raw_response_hash TEXT,
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    -- Exact-duplicate idempotency: the same provider reporting the same state
    -- at the same observation time writes one row. A genuine return to an
    -- earlier state at a *later* observation time is a new transition and is
    -- appended (the repository suppresses only no-change re-polls).
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

INSERT INTO game_status_history_v2 (
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

ALTER TABLE game_status_history_v2 RENAME TO game_status_history;

CREATE INDEX idx_game_status_asof ON game_status_history (game_id, observed_at);

-- The predecessor lookup in record_status() scans by (game, provider, time).
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
