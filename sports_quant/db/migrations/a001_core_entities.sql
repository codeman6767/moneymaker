-- Migration a001: core reference entities.
--
-- Leagues, seasons, teams, players and their alias tables. These are the
-- canonical entities every later phase links to. No provider identifier is a
-- primary key anywhere here: provider-specific naming lives exclusively in the
-- alias tables, scoped by `provider`.
--
-- Timestamp columns are ISO-8601 UTC TEXT (see sports_quant.db.schema); the
-- LIKE patterns below reject anything that is not in that shape, because a
-- corpus whose timestamps are not lexicographically sortable cannot answer a
-- point-in-time question.

CREATE TABLE leagues (
    league_id      TEXT PRIMARY KEY,
    code           TEXT NOT NULL,
    name           TEXT NOT NULL,
    sport          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    -- One row per canonical league.
    CONSTRAINT leagues_code_unique UNIQUE (code),
    CONSTRAINT leagues_code_supported CHECK (code IN ('MLB', 'NBA')),
    CONSTRAINT leagues_sport_valid CHECK (sport IN ('baseball', 'basketball')),
    CONSTRAINT leagues_id_prefix CHECK (league_id LIKE 'lg\_%' ESCAPE '\'),
    CONSTRAINT leagues_created_at_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT leagues_updated_at_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE TABLE seasons (
    season_id      TEXT PRIMARY KEY,
    league_id      TEXT NOT NULL REFERENCES leagues(league_id),
    year           INTEGER NOT NULL,
    label          TEXT NOT NULL,
    phase          TEXT NOT NULL,
    start_date     TEXT NOT NULL,
    end_date       TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CONSTRAINT seasons_unique UNIQUE (league_id, year, phase),
    CONSTRAINT seasons_phase_valid CHECK (phase IN ('preseason', 'regular', 'postseason')),
    -- A plausible-year bound catches transposed digits at write time.
    CONSTRAINT seasons_year_range CHECK (year BETWEEN 1870 AND 2200),
    CONSTRAINT seasons_start_date_iso CHECK (start_date LIKE '____-__-__'),
    CONSTRAINT seasons_end_date_iso CHECK (end_date IS NULL OR end_date LIKE '____-__-__'),
    -- A season may still be in progress (end_date NULL), but may never end
    -- before it started.
    CONSTRAINT seasons_dates_ordered CHECK (end_date IS NULL OR end_date >= start_date),
    CONSTRAINT seasons_id_prefix CHECK (season_id LIKE 'sn\_%' ESCAPE '\'),
    CONSTRAINT seasons_created_at_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT seasons_updated_at_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_seasons_league_year ON seasons (league_id, year);

CREATE TABLE teams (
    team_id        TEXT PRIMARY KEY,
    league_id      TEXT NOT NULL REFERENCES leagues(league_id),
    canonical_name TEXT NOT NULL,
    city           TEXT NOT NULL,
    nickname       TEXT NOT NULL,
    abbreviation   TEXT NOT NULL,
    -- Franchise validity window. NULL first_season = "always"; NULL
    -- last_season = "still active".
    first_season   INTEGER,
    last_season    INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    -- No duplicate canonical team within a league, by either handle.
    CONSTRAINT teams_abbreviation_unique UNIQUE (league_id, abbreviation),
    CONSTRAINT teams_name_unique UNIQUE (league_id, canonical_name),
    CONSTRAINT teams_seasons_ordered
        CHECK (first_season IS NULL OR last_season IS NULL OR last_season >= first_season),
    CONSTRAINT teams_id_prefix CHECK (team_id LIKE 'tm\_%' ESCAPE '\'),
    CONSTRAINT teams_created_at_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT teams_updated_at_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_teams_league ON teams (league_id);

-- Alias tables.
--
-- `provider`, `valid_from_season` and `valid_to_season` are NOT NULL with
-- sentinel defaults ('' / 0 / 9999) rather than nullable. In SQLite two NULLs
-- are distinct inside a UNIQUE constraint, so nullable columns would silently
-- permit exact duplicate seed rows on every re-run -- defeating idempotency.
-- Sentinels keep the uniqueness check total.
--
-- The uniqueness key is scoped to `team_id`, NOT to `league_id`. Two teams in
-- one league legitimately share an alias ("chicago" -> Cubs and White Sox);
-- that is genuine ambiguity to be recorded and refused at match time, not a
-- constraint violation to be rejected at write time.

CREATE TABLE team_aliases (
    alias_id          TEXT PRIMARY KEY,
    team_id           TEXT NOT NULL REFERENCES teams(team_id),
    league_id         TEXT NOT NULL REFERENCES leagues(league_id),
    alias             TEXT NOT NULL,
    normalized        TEXT NOT NULL,
    alias_type        TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT '',
    valid_from_season INTEGER NOT NULL DEFAULT 0,
    valid_to_season   INTEGER NOT NULL DEFAULT 9999,
    is_ambiguous      INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT team_aliases_unique
        UNIQUE (team_id, normalized, alias_type, provider, valid_from_season),
    CONSTRAINT team_aliases_type_valid CHECK (alias_type IN (
        'abbreviation', 'city', 'nickname', 'full', 'historical', 'provider'
    )),
    CONSTRAINT team_aliases_provider_present
        CHECK (alias_type <> 'provider' OR provider <> ''),
    CONSTRAINT team_aliases_source_valid
        CHECK (source IN ('seed', 'manual', 'provider_observed')),
    CONSTRAINT team_aliases_is_ambiguous_bool CHECK (is_ambiguous IN (0, 1)),
    CONSTRAINT team_aliases_normalized_nonempty CHECK (normalized <> ''),
    CONSTRAINT team_aliases_seasons_ordered CHECK (valid_to_season >= valid_from_season),
    CONSTRAINT team_aliases_created_at_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_team_aliases_lookup ON team_aliases (league_id, normalized);
CREATE INDEX idx_team_aliases_team ON team_aliases (team_id);

CREATE TABLE players (
    player_id        TEXT PRIMARY KEY,
    league_id        TEXT NOT NULL REFERENCES leagues(league_id),
    full_name        TEXT NOT NULL,
    first_name       TEXT,
    last_name        TEXT,
    -- Stored separately from full_name: "Ken Griffey Jr." and "Ken Griffey"
    -- are different people, and only a separate suffix makes that decidable.
    suffix           TEXT,
    birth_date       TEXT,
    primary_position TEXT,
    debut_date       TEXT,
    final_game_date  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CONSTRAINT players_id_prefix CHECK (player_id LIKE 'pl\_%' ESCAPE '\'),
    CONSTRAINT players_full_name_nonempty CHECK (full_name <> ''),
    CONSTRAINT players_birth_date_iso CHECK (birth_date IS NULL OR birth_date LIKE '____-__-__'),
    CONSTRAINT players_debut_date_iso CHECK (debut_date IS NULL OR debut_date LIKE '____-__-__'),
    CONSTRAINT players_final_date_iso
        CHECK (final_game_date IS NULL OR final_game_date LIKE '____-__-__'),
    CONSTRAINT players_career_ordered
        CHECK (debut_date IS NULL OR final_game_date IS NULL OR final_game_date >= debut_date),
    CONSTRAINT players_created_at_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT players_updated_at_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_players_league ON players (league_id);

CREATE TABLE player_aliases (
    alias_id     TEXT PRIMARY KEY,
    player_id    TEXT NOT NULL REFERENCES players(player_id),
    league_id    TEXT NOT NULL REFERENCES leagues(league_id),
    alias        TEXT NOT NULL,
    normalized   TEXT NOT NULL,
    -- Normalized generational suffix ('jr'/'sr'/'ii'/...) or '' when absent.
    -- A suffix present in the input is binding at match time.
    suffix       TEXT NOT NULL DEFAULT '',
    alias_type   TEXT NOT NULL,
    provider     TEXT NOT NULL DEFAULT '',
    is_ambiguous INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    CONSTRAINT player_aliases_unique
        UNIQUE (player_id, normalized, suffix, alias_type, provider),
    CONSTRAINT player_aliases_type_valid CHECK (alias_type IN (
        'full', 'short', 'nickname', 'accent_stripped', 'suffix_variant', 'provider'
    )),
    CONSTRAINT player_aliases_provider_present
        CHECK (alias_type <> 'provider' OR provider <> ''),
    CONSTRAINT player_aliases_source_valid
        CHECK (source IN ('seed', 'manual', 'provider_observed')),
    CONSTRAINT player_aliases_is_ambiguous_bool CHECK (is_ambiguous IN (0, 1)),
    CONSTRAINT player_aliases_normalized_nonempty CHECK (normalized <> ''),
    CONSTRAINT player_aliases_created_at_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_player_aliases_lookup ON player_aliases (league_id, normalized);
CREATE INDEX idx_player_aliases_player ON player_aliases (player_id);
