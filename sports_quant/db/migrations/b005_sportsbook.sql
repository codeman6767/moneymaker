-- Migration b005: sportsbook events, markets, outcome identities, and the
-- append-only point-in-time price snapshots.
--
-- The four-level split (event -> market -> outcome -> price snapshot) is the
-- whole point of this migration. An outcome's *identity* -- this bookmaker's
-- Over 8.5 on this event -- is stable for days while its *price* changes every
-- few minutes. Collapsing the two would re-store the identity on every poll and
-- turn "price history for this line" into a string-matching problem instead of
-- an indexed range scan.
--
-- Nothing here places, prices, or simulates a bet. These are observations of a
-- public price feed.

-- --------------------------------------------------------------------------
-- Events.
--
-- `game_id` stays NULL in Phase B. Linking a sportsbook event to a canonical
-- game is a recorded match decision (ENTITY_MATCHING.md), and Phase B
-- deliberately performs no fuzzy matching -- an unlinked event is honest,
-- a wrongly linked one inverts the sign of every price derived from it.
-- `league_id` *is* populated, from the static sport_key map; that is a provider
-- enum lookup, not a name match.
-- --------------------------------------------------------------------------
CREATE TABLE sportsbook_events (
    sb_event_id       TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    -- The provider's own event id. Never used as a canonical identifier.
    provider_event_id TEXT NOT NULL,
    league_id         TEXT REFERENCES leagues(league_id),
    sport_key         TEXT NOT NULL,
    commence_time     TEXT NOT NULL,
    -- Team names exactly as the provider wrote them. Normalization happens at
    -- match time, against these preserved strings.
    home_team_raw     TEXT NOT NULL,
    away_team_raw     TEXT NOT NULL,
    -- NULL until Phase D matching; see ENTITY_MATCHING.md §5.
    game_id           TEXT REFERENCES games(game_id),
    -- The response that first produced this row. Each price observation
    -- carries its own raw_response_id, so provenance is never inferred.
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    first_observed_at TEXT NOT NULL,
    last_observed_at  TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CONSTRAINT sportsbook_events_id_prefix CHECK (sb_event_id LIKE 'sbe\_%' ESCAPE '\'),
    CONSTRAINT sportsbook_events_unique UNIQUE (provider, provider_event_id),
    CONSTRAINT sportsbook_events_provider_event_id_present CHECK (provider_event_id <> ''),
    CONSTRAINT sportsbook_events_sport_key_present CHECK (sport_key <> ''),
    CONSTRAINT sportsbook_events_commence_iso CHECK (commence_time LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sportsbook_events_first_observed_iso
        CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sportsbook_events_last_observed_iso
        CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sportsbook_events_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sportsbook_events_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_sb_events_league_commence ON sportsbook_events (league_id, commence_time);
CREATE INDEX idx_sb_events_unmatched ON sportsbook_events (game_id, commence_time);
CREATE INDEX idx_sb_events_raw ON sportsbook_events (raw_response_id);

-- Provider identity is fixed at creation. commence_time is deliberately
-- mutable current state -- a provider moves a game and we must follow -- but
-- the identity it is keyed by cannot drift underneath its markets.
CREATE TRIGGER trg_sportsbook_events_identity_immutable
BEFORE UPDATE OF provider, provider_event_id, sport_key, first_observed_at ON sportsbook_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'sportsbook_events identity columns are immutable')
    WHERE NEW.provider <> OLD.provider
       OR NEW.provider_event_id <> OLD.provider_event_id
       OR NEW.sport_key <> OLD.sport_key
       OR NEW.first_observed_at <> OLD.first_observed_at;
END;

-- --------------------------------------------------------------------------
-- Markets.
--
-- market_key is CHECK-constrained to the keys Phase B supports. An unsupported
-- key is a validation refusal at the ingestor *and* an impossibility at the
-- storage layer; a later phase that adds player props adds a migration, which
-- is the visible, reviewable way to widen the corpus.
-- --------------------------------------------------------------------------
CREATE TABLE sportsbook_markets (
    sb_market_id      TEXT PRIMARY KEY,
    sb_event_id       TEXT NOT NULL REFERENCES sportsbook_events(sb_event_id),
    bookmaker_key     TEXT NOT NULL,
    bookmaker_title   TEXT,
    market_key        TEXT NOT NULL,
    -- The provider's own update times, preserved separately: a bookmaker-level
    -- and a market-level clock that do not always agree.
    bookmaker_last_update TEXT,
    market_last_update    TEXT,
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    first_observed_at TEXT NOT NULL,
    last_observed_at  TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CONSTRAINT sportsbook_markets_id_prefix CHECK (sb_market_id LIKE 'sbm\_%' ESCAPE '\'),
    CONSTRAINT sportsbook_markets_unique UNIQUE (sb_event_id, bookmaker_key, market_key),
    CONSTRAINT sportsbook_markets_bookmaker_present CHECK (bookmaker_key <> ''),
    CONSTRAINT sportsbook_markets_key_supported CHECK (market_key IN ('h2h', 'spreads', 'totals')),
    CONSTRAINT sportsbook_markets_bookmaker_update_iso CHECK (
        bookmaker_last_update IS NULL OR bookmaker_last_update LIKE '____-__-__T__:__:__%Z'
    ),
    CONSTRAINT sportsbook_markets_market_update_iso CHECK (
        market_last_update IS NULL OR market_last_update LIKE '____-__-__T__:__:__%Z'
    ),
    CONSTRAINT sportsbook_markets_first_observed_iso
        CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sportsbook_markets_last_observed_iso
        CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sportsbook_markets_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sportsbook_markets_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_sb_markets_event ON sportsbook_markets (sb_event_id, market_key);
CREATE INDEX idx_sb_markets_bookmaker ON sportsbook_markets (bookmaker_key, market_key);

CREATE TRIGGER trg_sportsbook_markets_identity_immutable
BEFORE UPDATE OF sb_event_id, bookmaker_key, market_key, first_observed_at ON sportsbook_markets
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'sportsbook_markets identity columns are immutable')
    WHERE NEW.sb_event_id <> OLD.sb_event_id
       OR NEW.bookmaker_key <> OLD.bookmaker_key
       OR NEW.market_key <> OLD.market_key
       OR NEW.first_observed_at <> OLD.first_observed_at;
END;

-- --------------------------------------------------------------------------
-- Outcome identities.
--
-- The line is part of the identity. "Over 8.5" and "Over 9.5" are different
-- contracts, not one contract at two prices: a bet on one does not settle like
-- a bet on the other. Keying identity by (market, name, point) therefore keeps
-- "price history for this line" a single indexed scan, and stops a line move
-- from masquerading as a price move on the same contract.
--
-- A changed *price* never creates a new identity -- prices live entirely in
-- sportsbook_price_snapshots.
--
-- point_key is a NOT NULL text rendering of the point ('' when there is none)
-- because SQLite treats two NULLs as distinct inside a UNIQUE constraint,
-- which would let an h2h outcome insert again on every poll.
-- --------------------------------------------------------------------------
CREATE TABLE sportsbook_outcomes (
    sb_outcome_id     TEXT PRIMARY KEY,
    sb_market_id      TEXT NOT NULL REFERENCES sportsbook_markets(sb_market_id),
    -- Normalized name (db/normalize.py), used for identity and lookup.
    outcome_name      TEXT NOT NULL,
    -- Exactly what the provider wrote, preserved for matching and audit.
    provider_outcome_name TEXT NOT NULL,
    -- 'home'/'away' for team outcomes, 'over'/'under' for totals, 'draw' where
    -- a provider supplies one, 'unknown' when the role cannot be determined --
    -- recorded rather than dropped, because a silently dropped outcome is
    -- missing data nobody notices.
    outcome_role      TEXT NOT NULL,
    point             REAL,
    point_key         TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT sportsbook_outcomes_id_prefix CHECK (sb_outcome_id LIKE 'sbo\_%' ESCAPE '\'),
    CONSTRAINT sportsbook_outcomes_unique UNIQUE (sb_market_id, outcome_name, point_key),
    CONSTRAINT sportsbook_outcomes_name_present CHECK (provider_outcome_name <> ''),
    CONSTRAINT sportsbook_outcomes_role_valid CHECK (outcome_role IN (
        'home', 'away', 'over', 'under', 'draw', 'unknown'
    )),
    CONSTRAINT sportsbook_outcomes_point_key_paired CHECK (
        (point IS NULL AND point_key = '') OR (point IS NOT NULL AND point_key <> '')
    ),
    CONSTRAINT sportsbook_outcomes_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_sb_outcomes_market ON sportsbook_outcomes (sb_market_id, outcome_role);

-- Not fully append-only: Phase D sets matching columns on these rows. The
-- identity itself, however, is fixed the moment it is created.
CREATE TRIGGER trg_sportsbook_outcomes_identity_immutable
BEFORE UPDATE OF sb_market_id, outcome_name, provider_outcome_name, point, point_key
ON sportsbook_outcomes
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'sportsbook_outcomes identity columns are immutable')
    WHERE NEW.sb_market_id <> OLD.sb_market_id
       OR NEW.outcome_name <> OLD.outcome_name
       OR NEW.provider_outcome_name <> OLD.provider_outcome_name
       OR NEW.point_key <> OLD.point_key
       OR (NEW.point IS NULL) <> (OLD.point IS NULL)
       OR (NEW.point IS NOT NULL AND OLD.point IS NOT NULL AND NEW.point <> OLD.point);
END;

-- --------------------------------------------------------------------------
-- Point-in-time price snapshots. Append-only, always.
--
-- UNIQUE (sb_outcome_id, content_hash) is what makes re-ingestion idempotent:
-- content_hash covers the *observation* (price, line, and the provider's own
-- update times) and deliberately excludes observed_at, so polling an unchanged
-- price twice writes one row, while a genuinely new provider observation
-- writes a new one. A later price appends; an older backfill appends and does
-- not disturb what is already there.
--
-- price_american is stored exactly as the provider sent it. price_decimal and
-- implied_probability are exact arithmetic transforms of it, stored for
-- convenience. implied_probability is the RAW, vig-inclusive number -- no
-- de-vigging happens anywhere in Phase B.
-- --------------------------------------------------------------------------
CREATE TABLE sportsbook_price_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    sb_outcome_id     TEXT NOT NULL REFERENCES sportsbook_outcomes(sb_outcome_id),
    price_american    INTEGER NOT NULL,
    price_decimal     REAL,
    implied_probability REAL,
    point             REAL,
    bookmaker_last_update TEXT,
    market_last_update    TEXT,
    -- Valid time: the provider's own clock. Nullable, never invented.
    provider_timestamp TEXT,
    -- Transaction time: when the bytes arrived. The point-in-time cutoff.
    observed_at       TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    -- Two provenance links, deliberately: the id is the fast join, the hash
    -- survives an export or a corpus merge where ids are renumbered.
    raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    run_id            TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    content_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT sb_price_snapshots_id_prefix CHECK (snapshot_id LIKE 'sbp\_%' ESCAPE '\'),
    CONSTRAINT sb_price_snapshots_unique UNIQUE (sb_outcome_id, content_hash),
    -- American odds are never in (-100, 100); a value there is malformed, not
    -- a long shot.
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

-- The as-of query shape: (outcome, observed_at) with a descending scan.
CREATE INDEX idx_sb_price_asof ON sportsbook_price_snapshots (sb_outcome_id, observed_at);
CREATE INDEX idx_sb_price_raw ON sportsbook_price_snapshots (raw_response_id);
CREATE INDEX idx_sb_price_run ON sportsbook_price_snapshots (run_id);

-- DQ-PIT-008: an overwritten historical price silently corrupts every dataset
-- built afterwards and is undetectable after the fact.
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
