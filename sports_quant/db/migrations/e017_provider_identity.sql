-- Migration e017: append-only structured provider identity observations.
--
-- Why this exists
-- ---------------
-- The F1 canonical entity-matching pilot returned 0% coverage. The refusal was
-- correct, and the cause was a missing bootstrap, not a matcher defect:
--
--   * `game_schedule_snapshots` records `home_provider_team_id` /
--     `away_provider_team_id` and no team name; `provider_team_references`
--     records ids and provenance and no name either. So
--     `MatchGamesService` had no name to hand `TeamResolver`, which then fell
--     back to matching the numeric provider id against the alias table and
--     correctly found nothing.
--   * `provider_player_references` likewise records ids only, and the canonical
--     `players` / `player_aliases` tables start empty, so every provider player
--     had an empty candidate pool.
--
-- Provider-written names DO exist -- inside `raw_responses` bodies. Parsing raw
-- JSON at match time is not acceptable (unauditable, and it would couple the
-- matcher to every provider's payload shape), so ingestion now lands the names
-- it already receives into two typed, append-only observation tables. Matching
-- reads those.
--
-- Design notes
-- ------------
-- Append-only, enforced by triggers, exactly like `raw_responses` and
-- `game_status_history`. A provider that renames a franchise or corrects a
-- player's spelling produces a NEW observation; history is never rewritten.
--
-- `content_hash` covers the semantic identity fields and EXCLUDES `observed_at`,
-- and the uniqueness key is `(provider, entity id, observed_at, content_hash)` --
-- one row per (observation time, content). This is the same lesson migration
-- a003 already applied to `game_status_history`: keying uniqueness on the
-- content hash ALONE deduplicates *states* rather than *observations*, and the
-- surviving row then keeps whichever `observed_at` happened to be written
-- first. That makes the stored timestamp -- and therefore every later
-- `latest identity as of T` answer -- depend on raw-response processing order,
-- which is precisely the nondeterminism this table must not have.
--
-- With time in the key:
--
--   * the final row set is a pure function of the input observations, whatever
--     order they are replayed in;
--   * re-ingesting or replaying the same response inserts nothing, because both
--     the time and the content repeat;
--   * a changed provider-written name appends, because the content differs;
--   * an unchanged identity seen later by another endpoint family appends one
--     honest row saying "still called this, at this later time" rather than
--     silently backdating or forward-dating the earlier one.
--
-- Latest-identity selection is `ORDER BY observed_at DESC, content_hash DESC`.
-- The hash tie-break exists so an equal-timestamp conflict resolves by a stable
-- property of the data rather than by insertion order or rowid; the repository
-- additionally surfaces such a conflict rather than hiding it.
--
-- No column here holds a credential, an authorization header, or a URL: the
-- provenance is a `raw_response_id` plus hashes, and `raw_responses` already
-- stores only sanitized allow-listed headers.

-- --------------------------------------------------------------------------
-- Team identity observations.
-- --------------------------------------------------------------------------
CREATE TABLE provider_team_identity_snapshots (
    identity_id       TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    provider_team_id  TEXT NOT NULL,
    league_id         TEXT NOT NULL REFERENCES leagues(league_id),
    -- The provider-written full/display name, stored exactly as supplied.
    full_name         TEXT NOT NULL,
    -- normalize_name() output, so alias lookup uses one implementation.
    normalized_name   TEXT NOT NULL,
    -- Recorded ONLY when the provider genuinely supplied them. A NULL here
    -- means "not supplied", never "unknown, guess something".
    abbreviation      TEXT,
    city              TEXT,
    nickname          TEXT,
    observed_at       TEXT NOT NULL,
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT pti_id_prefix CHECK (identity_id LIKE 'pti\_%' ESCAPE '\'),
    CONSTRAINT pti_provider_present CHECK (provider <> ''),
    CONSTRAINT pti_team_id_present CHECK (provider_team_id <> ''),
    -- A nameless identity observation is worthless and would let an empty
    -- string masquerade as evidence, so it is refused at the database.
    CONSTRAINT pti_full_name_present CHECK (TRIM(full_name) <> ''),
    CONSTRAINT pti_normalized_present CHECK (normalized_name <> ''),
    CONSTRAINT pti_abbreviation_nonempty
        CHECK (abbreviation IS NULL OR TRIM(abbreviation) <> ''),
    CONSTRAINT pti_city_nonempty CHECK (city IS NULL OR TRIM(city) <> ''),
    CONSTRAINT pti_nickname_nonempty CHECK (nickname IS NULL OR TRIM(nickname) <> ''),
    CONSTRAINT pti_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT pti_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- One row per (observation time, content): an exact replay is a no-op, and
    -- the stored timestamp never depends on processing order.
    CONSTRAINT pti_content_unique
        UNIQUE (provider, provider_team_id, observed_at, content_hash)
);

CREATE INDEX idx_pti_lookup
    ON provider_team_identity_snapshots (provider, provider_team_id, observed_at DESC);
CREATE INDEX idx_pti_normalized
    ON provider_team_identity_snapshots (league_id, normalized_name);
CREATE INDEX idx_pti_raw ON provider_team_identity_snapshots (raw_response_id);

CREATE TRIGGER trg_pti_no_update
BEFORE UPDATE ON provider_team_identity_snapshots
BEGIN
    SELECT RAISE(ABORT, 'provider_team_identity_snapshots is append-only');
END;

CREATE TRIGGER trg_pti_no_delete
BEFORE DELETE ON provider_team_identity_snapshots
BEGIN
    SELECT RAISE(ABORT, 'provider_team_identity_snapshots is append-only');
END;

-- An identity observation must agree with the league of the provider it came
-- from being a real league row; the FK covers existence, this covers the
-- observation never being attributed to a league the raw response cannot
-- belong to (checked in the repository, asserted here for anything else
-- holding a connection).
CREATE TRIGGER trg_pti_league_present
BEFORE INSERT ON provider_team_identity_snapshots
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'provider_team_identity_snapshots.league_id must exist')
    WHERE NOT EXISTS (SELECT 1 FROM leagues WHERE league_id = NEW.league_id);
END;

-- --------------------------------------------------------------------------
-- Player identity observations.
-- --------------------------------------------------------------------------
CREATE TABLE provider_player_identity_snapshots (
    identity_id        TEXT PRIMARY KEY,
    provider           TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    league_id          TEXT NOT NULL REFERENCES leagues(league_id),
    full_name          TEXT NOT NULL,
    normalized_name    TEXT NOT NULL,
    -- Normalized generational suffix via the shared normalizer, or ''. Stored
    -- separately because "Ken Griffey Jr." and "Ken Griffey" are two people and
    -- only a separate suffix makes that decidable.
    suffix             TEXT NOT NULL DEFAULT '',
    -- All five NULLable fields are written only when genuinely supplied.
    -- MLB StatsAPI supplies `fullName` and no name parts, so first_name /
    -- last_name stay NULL for MLB rather than being split out of full_name.
    first_name         TEXT,
    last_name          TEXT,
    birth_date         TEXT,
    position           TEXT,
    provider_team_id   TEXT,
    observed_at        TEXT NOT NULL,
    raw_response_id    TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT ppi_id_prefix CHECK (identity_id LIKE 'ppi\_%' ESCAPE '\'),
    CONSTRAINT ppi_provider_present CHECK (provider <> ''),
    CONSTRAINT ppi_player_id_present CHECK (provider_player_id <> ''),
    CONSTRAINT ppi_full_name_present CHECK (TRIM(full_name) <> ''),
    CONSTRAINT ppi_normalized_present CHECK (normalized_name <> ''),
    CONSTRAINT ppi_first_name_nonempty
        CHECK (first_name IS NULL OR TRIM(first_name) <> ''),
    CONSTRAINT ppi_last_name_nonempty CHECK (last_name IS NULL OR TRIM(last_name) <> ''),
    CONSTRAINT ppi_position_nonempty CHECK (position IS NULL OR TRIM(position) <> ''),
    CONSTRAINT ppi_team_id_nonempty
        CHECK (provider_team_id IS NULL OR TRIM(provider_team_id) <> ''),
    CONSTRAINT ppi_birth_date_iso CHECK (birth_date IS NULL OR birth_date LIKE '____-__-__'),
    CONSTRAINT ppi_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ppi_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ppi_content_unique
        UNIQUE (provider, provider_player_id, observed_at, content_hash)
);

CREATE INDEX idx_ppi_lookup
    ON provider_player_identity_snapshots (provider, provider_player_id, observed_at DESC);
CREATE INDEX idx_ppi_normalized
    ON provider_player_identity_snapshots (league_id, normalized_name, suffix);
CREATE INDEX idx_ppi_raw ON provider_player_identity_snapshots (raw_response_id);

CREATE TRIGGER trg_ppi_no_update
BEFORE UPDATE ON provider_player_identity_snapshots
BEGIN
    SELECT RAISE(ABORT, 'provider_player_identity_snapshots is append-only');
END;

CREATE TRIGGER trg_ppi_no_delete
BEFORE DELETE ON provider_player_identity_snapshots
BEGIN
    SELECT RAISE(ABORT, 'provider_player_identity_snapshots is append-only');
END;

CREATE TRIGGER trg_ppi_league_present
BEFORE INSERT ON provider_player_identity_snapshots
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'provider_player_identity_snapshots.league_id must exist')
    WHERE NOT EXISTS (SELECT 1 FROM leagues WHERE league_id = NEW.league_id);
END;
