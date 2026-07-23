-- Migration d009: Phase D provider infrastructure.
--
-- Version 009: migration numbers are a single global sequence (§3.1); Phase C
-- ended at 008, so Phase D begins at 009. Migrations a001..c008 are immutable
-- and are NOT edited.
--
-- This migration adds the cross-cutting foundations every later Phase D stage
-- needs: provider id crosswalks, canonical venues + aliases, the entity-match
-- decision log with a normalized candidate child table, data-quality issues,
-- and typed provider-capability snapshots. It ingests NO historical data and
-- creates NO second canonical league/team/player/game table -- it references
-- the existing canonical entities.
--
-- Conventions reused from Phases A-C:
--   * ISO-8601 UTC TEXT timestamps with a LIKE CHECK on every timestamp column.
--   * First/current raw-response provenance (c008) on mutable current-state
--     tables (references, venues): first_raw_response_id is immutable; current_*
--     moves only when a strictly-newer observation wins (enforced in the repo).
--   * Append-only snapshots (match_candidates, provider_capabilities) with
--     BEFORE UPDATE/DELETE triggers.
--   * entity_match_decisions is append-only EXCEPT its review columns, enforced
--     by a column-scoped trigger.
--   * Identity-immutability triggers freeze the columns that define a row.

-- ==========================================================================
-- Provider id crosswalks. (provider, provider_entity_id) is the stable identity;
-- the canonical id is nullable until a match decision links it. A partial UNIQUE
-- and an immutability guard prevent a provider id from silently moving between
-- incompatible canonical entities.
-- ==========================================================================
CREATE TABLE provider_team_references (
    reference_id       TEXT PRIMARY KEY,
    provider           TEXT NOT NULL,
    provider_team_id   TEXT NOT NULL,
    team_id            TEXT REFERENCES teams(team_id),
    match_decision_id  TEXT REFERENCES entity_match_decisions(match_id),
    first_raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_id TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_hash TEXT NOT NULL,
    first_observed_at  TEXT NOT NULL,
    last_observed_at   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CONSTRAINT provider_team_ref_id_prefix CHECK (reference_id LIKE 'ptr\_%' ESCAPE '\'),
    CONSTRAINT provider_team_ref_unique UNIQUE (provider, provider_team_id),
    CONSTRAINT provider_team_ref_provider_present CHECK (provider <> ''),
    CONSTRAINT provider_team_ref_provider_id_present CHECK (provider_team_id <> ''),
    CONSTRAINT provider_team_ref_first_observed_iso
        CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_team_ref_last_observed_iso
        CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_team_ref_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_team_ref_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_provider_team_ref_team ON provider_team_references (team_id);
CREATE INDEX idx_provider_team_ref_lookup ON provider_team_references (provider, provider_team_id);

-- Identity + first-provenance immutable; once a team_id is set, it may not be
-- silently re-pointed to a different team (a re-match must go through review).
CREATE TRIGGER trg_provider_team_ref_identity_immutable
BEFORE UPDATE OF reference_id, provider, provider_team_id, first_raw_response_id, first_observed_at, team_id
ON provider_team_references
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'provider_team_references identity columns are immutable')
    WHERE NEW.reference_id <> OLD.reference_id
       OR NEW.provider <> OLD.provider
       OR NEW.provider_team_id <> OLD.provider_team_id
       OR NEW.first_raw_response_id <> OLD.first_raw_response_id
       OR NEW.first_observed_at <> OLD.first_observed_at
       OR (OLD.team_id IS NOT NULL AND NEW.team_id IS NOT OLD.team_id);
END;

CREATE TABLE provider_player_references (
    reference_id       TEXT PRIMARY KEY,
    provider           TEXT NOT NULL,
    provider_player_id TEXT NOT NULL,
    player_id          TEXT REFERENCES players(player_id),
    match_decision_id  TEXT REFERENCES entity_match_decisions(match_id),
    first_raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_id TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_hash TEXT NOT NULL,
    first_observed_at  TEXT NOT NULL,
    last_observed_at   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CONSTRAINT provider_player_ref_id_prefix CHECK (reference_id LIKE 'ppr\_%' ESCAPE '\'),
    CONSTRAINT provider_player_ref_unique UNIQUE (provider, provider_player_id),
    CONSTRAINT provider_player_ref_provider_present CHECK (provider <> ''),
    CONSTRAINT provider_player_ref_provider_id_present CHECK (provider_player_id <> ''),
    CONSTRAINT provider_player_ref_first_observed_iso
        CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_player_ref_last_observed_iso
        CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_player_ref_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_player_ref_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_provider_player_ref_player ON provider_player_references (player_id);
CREATE INDEX idx_provider_player_ref_lookup
    ON provider_player_references (provider, provider_player_id);

CREATE TRIGGER trg_provider_player_ref_identity_immutable
BEFORE UPDATE OF reference_id, provider, provider_player_id, first_raw_response_id, first_observed_at, player_id
ON provider_player_references
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'provider_player_references identity columns are immutable')
    WHERE NEW.reference_id <> OLD.reference_id
       OR NEW.provider <> OLD.provider
       OR NEW.provider_player_id <> OLD.provider_player_id
       OR NEW.first_raw_response_id <> OLD.first_raw_response_id
       OR NEW.first_observed_at <> OLD.first_observed_at
       OR (OLD.player_id IS NOT NULL AND NEW.player_id IS NOT OLD.player_id);
END;

CREATE TABLE provider_game_references (
    reference_id       TEXT PRIMARY KEY,
    provider           TEXT NOT NULL,
    provider_game_id   TEXT NOT NULL,
    game_id            TEXT REFERENCES games(game_id),
    match_decision_id  TEXT REFERENCES entity_match_decisions(match_id),
    first_raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_id TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_hash TEXT NOT NULL,
    first_observed_at  TEXT NOT NULL,
    last_observed_at   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CONSTRAINT provider_game_ref_id_prefix CHECK (reference_id LIKE 'pgr\_%' ESCAPE '\'),
    CONSTRAINT provider_game_ref_unique UNIQUE (provider, provider_game_id),
    CONSTRAINT provider_game_ref_provider_present CHECK (provider <> ''),
    CONSTRAINT provider_game_ref_provider_id_present CHECK (provider_game_id <> ''),
    CONSTRAINT provider_game_ref_first_observed_iso
        CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_game_ref_last_observed_iso
        CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_game_ref_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_game_ref_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_provider_game_ref_game ON provider_game_references (game_id);
CREATE INDEX idx_provider_game_ref_lookup ON provider_game_references (provider, provider_game_id);

CREATE TRIGGER trg_provider_game_ref_identity_immutable
BEFORE UPDATE OF reference_id, provider, provider_game_id, first_raw_response_id, first_observed_at, game_id
ON provider_game_references
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'provider_game_references identity columns are immutable')
    WHERE NEW.reference_id <> OLD.reference_id
       OR NEW.provider <> OLD.provider
       OR NEW.provider_game_id <> OLD.provider_game_id
       OR NEW.first_raw_response_id <> OLD.first_raw_response_id
       OR NEW.first_observed_at <> OLD.first_observed_at
       OR (OLD.game_id IS NOT NULL AND NEW.game_id IS NOT OLD.game_id);
END;

-- ==========================================================================
-- Venues. Canonical venue records; mutable current-state with c008 provenance.
-- ==========================================================================
CREATE TABLE venues (
    venue_id           TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    normalized_name    TEXT NOT NULL,
    city               TEXT,
    country            TEXT,
    latitude           REAL,
    longitude          REAL,
    timezone           TEXT,
    roof_type          TEXT,
    is_outdoor         INTEGER,
    first_raw_response_id   TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_id TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    current_raw_response_hash TEXT NOT NULL,
    first_observed_at  TEXT NOT NULL,
    last_observed_at   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CONSTRAINT venues_id_prefix CHECK (venue_id LIKE 'ven\_%' ESCAPE '\'),
    CONSTRAINT venues_name_present CHECK (name <> ''),
    CONSTRAINT venues_normalized_unique UNIQUE (normalized_name),
    CONSTRAINT venues_roof_type_valid CHECK (
        roof_type IS NULL OR roof_type IN ('open', 'retractable', 'dome', 'fixed', 'indoor')
    ),
    CONSTRAINT venues_outdoor_bool CHECK (is_outdoor IS NULL OR is_outdoor IN (0, 1)),
    CONSTRAINT venues_latitude_range CHECK (latitude IS NULL OR (latitude BETWEEN -90.0 AND 90.0)),
    CONSTRAINT venues_longitude_range
        CHECK (longitude IS NULL OR (longitude BETWEEN -180.0 AND 180.0)),
    CONSTRAINT venues_first_observed_iso CHECK (first_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT venues_last_observed_iso CHECK (last_observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT venues_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT venues_updated_iso CHECK (updated_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_venues_normalized ON venues (normalized_name);

CREATE TRIGGER trg_venues_identity_immutable
BEFORE UPDATE OF venue_id, normalized_name, first_raw_response_id, first_observed_at ON venues
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'venues identity columns are immutable')
    WHERE NEW.venue_id <> OLD.venue_id
       OR NEW.normalized_name <> OLD.normalized_name
       OR NEW.first_raw_response_id <> OLD.first_raw_response_id
       OR NEW.first_observed_at <> OLD.first_observed_at;
END;

-- Venue aliases. Provider venue strings -> canonical venue (mirrors team_aliases).
CREATE TABLE venue_aliases (
    alias_id           TEXT PRIMARY KEY,
    venue_id           TEXT NOT NULL REFERENCES venues(venue_id),
    provider           TEXT NOT NULL DEFAULT '',
    provider_venue_id  TEXT,
    alias              TEXT NOT NULL,
    normalized         TEXT NOT NULL,
    source             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT venue_aliases_id_prefix CHECK (alias_id LIKE 'val\_%' ESCAPE '\'),
    CONSTRAINT venue_aliases_unique UNIQUE (venue_id, normalized, provider),
    CONSTRAINT venue_aliases_alias_present CHECK (alias <> ''),
    CONSTRAINT venue_aliases_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_venue_aliases_lookup ON venue_aliases (normalized, provider);
CREATE INDEX idx_venue_aliases_venue ON venue_aliases (venue_id);

-- ==========================================================================
-- Entity match decisions + normalized candidates.
-- Append-only EXCEPT the manual-review columns.
-- ==========================================================================
CREATE TABLE entity_match_decisions (
    match_id           TEXT PRIMARY KEY,
    entity_type        TEXT NOT NULL,
    source_provider    TEXT NOT NULL,
    source_ref         TEXT NOT NULL,
    matched_entity_id  TEXT,
    outcome            TEXT NOT NULL,
    method             TEXT NOT NULL,
    score              REAL NOT NULL,
    threshold          REAL NOT NULL,
    rejection_reason   TEXT,
    needs_manual_review INTEGER NOT NULL DEFAULT 0,
    reviewed_by        TEXT,
    reviewed_at        TEXT,
    matcher_version    TEXT NOT NULL,
    raw_response_id    TEXT REFERENCES raw_responses(raw_response_id),
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    decided_at         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT match_decisions_id_prefix CHECK (match_id LIKE 'mtc\_%' ESCAPE '\'),
    CONSTRAINT match_decisions_entity_type_valid CHECK (entity_type IN (
        'team', 'player', 'game', 'venue', 'sportsbook_event', 'kalshi_event', 'kalshi_market'
    )),
    CONSTRAINT match_decisions_outcome_valid CHECK (outcome IN (
        'accepted', 'rejected', 'ambiguous', 'no_candidate', 'manual_override'
    )),
    CONSTRAINT match_decisions_score_range CHECK (score BETWEEN 0.0 AND 1.0),
    CONSTRAINT match_decisions_threshold_range CHECK (threshold BETWEEN 0.0 AND 1.0),
    CONSTRAINT match_decisions_review_bool CHECK (needs_manual_review IN (0, 1)),
    -- An accepted decision must name an entity; a non-accepted one must say why.
    CONSTRAINT match_decisions_accept_names CHECK (
        outcome <> 'accepted' OR matched_entity_id IS NOT NULL
    ),
    CONSTRAINT match_decisions_reject_reason CHECK (
        outcome = 'accepted' OR rejection_reason IS NOT NULL
    ),
    CONSTRAINT match_decisions_decided_iso CHECK (decided_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT match_decisions_reviewed_iso
        CHECK (reviewed_at IS NULL OR reviewed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT match_decisions_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_match_decisions_review ON entity_match_decisions (needs_manual_review, entity_type);
CREATE INDEX idx_match_decisions_source ON entity_match_decisions (source_provider, source_ref);
CREATE INDEX idx_match_decisions_entity ON entity_match_decisions (entity_type, matched_entity_id);

-- Append-only EXCEPT review columns: an UPDATE may touch only
-- needs_manual_review / reviewed_by / reviewed_at; every other column is frozen.
CREATE TRIGGER trg_match_decisions_review_only_update
BEFORE UPDATE ON entity_match_decisions
FOR EACH ROW
WHEN (
    NEW.match_id <> OLD.match_id
    OR NEW.entity_type <> OLD.entity_type
    OR NEW.source_provider <> OLD.source_provider
    OR NEW.source_ref <> OLD.source_ref
    OR NEW.matched_entity_id IS NOT OLD.matched_entity_id
    OR NEW.outcome <> OLD.outcome
    OR NEW.method <> OLD.method
    OR NEW.score <> OLD.score
    OR NEW.threshold <> OLD.threshold
    OR NEW.rejection_reason IS NOT OLD.rejection_reason
    OR NEW.matcher_version <> OLD.matcher_version
    OR NEW.raw_response_id IS NOT OLD.raw_response_id
    OR NEW.run_id IS NOT OLD.run_id
    OR NEW.decided_at <> OLD.decided_at
    OR NEW.created_at <> OLD.created_at
)
BEGIN
    SELECT RAISE(ABORT,
        'entity_match_decisions is append-only except its review columns');
END;

CREATE TRIGGER trg_match_decisions_no_delete
BEFORE DELETE ON entity_match_decisions
BEGIN
    SELECT RAISE(ABORT, 'entity_match_decisions is append-only');
END;

-- One normalized row per candidate considered (incl. the losers). Append-only.
CREATE TABLE match_candidates (
    candidate_id       TEXT PRIMARY KEY,
    match_id           TEXT NOT NULL REFERENCES entity_match_decisions(match_id),
    candidate_entity_id TEXT,
    score              REAL NOT NULL,
    tier               TEXT NOT NULL,
    method             TEXT,
    evidence           TEXT,
    rank               INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT match_candidates_id_prefix CHECK (candidate_id LIKE 'mcn\_%' ESCAPE '\'),
    CONSTRAINT match_candidates_score_range CHECK (score BETWEEN 0.0 AND 1.0),
    CONSTRAINT match_candidates_rank_non_negative CHECK (rank >= 0),
    CONSTRAINT match_candidates_unique UNIQUE (match_id, rank),
    CONSTRAINT match_candidates_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_match_candidates_decision ON match_candidates (match_id, rank);

CREATE TRIGGER trg_match_candidates_no_update
BEFORE UPDATE ON match_candidates
BEGIN
    SELECT RAISE(ABORT, 'match_candidates is append-only');
END;

CREATE TRIGGER trg_match_candidates_no_delete
BEFORE DELETE ON match_candidates
BEGIN
    SELECT RAISE(ABORT, 'match_candidates is append-only');
END;

-- ==========================================================================
-- Data-quality issues. Mutable: a resolution can be recorded. Reuses the
-- backtest severity vocabulary. Supports DQ-CAP-* / DQ-TZ-001 rule codes.
-- ==========================================================================
CREATE TABLE data_quality_issues (
    issue_id           TEXT PRIMARY KEY,
    severity           TEXT NOT NULL,
    rule_code          TEXT NOT NULL,
    entity_type        TEXT NOT NULL,
    entity_id          TEXT,
    provider           TEXT,
    description        TEXT NOT NULL,
    detail_json        TEXT,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT REFERENCES raw_responses(raw_response_id),
    detected_at        TEXT NOT NULL,
    resolved_at        TEXT,
    resolution_note    TEXT,
    created_at         TEXT NOT NULL,
    CONSTRAINT data_quality_id_prefix CHECK (issue_id LIKE 'dqi\_%' ESCAPE '\'),
    CONSTRAINT data_quality_severity_valid CHECK (severity IN ('blocking', 'issue', 'note')),
    CONSTRAINT data_quality_rule_present CHECK (rule_code <> ''),
    CONSTRAINT data_quality_detected_iso CHECK (detected_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT data_quality_resolved_iso
        CHECK (resolved_at IS NULL OR resolved_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT data_quality_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_data_quality_open ON data_quality_issues (severity, resolved_at);
CREATE INDEX idx_data_quality_rule ON data_quality_issues (rule_code, detected_at);

-- ==========================================================================
-- Provider capability snapshots. Append-only historical audit: what the system
-- believed a provider (at a tier) could supply, when. Never overwritten, so an
-- earlier prediction cutoff can see the capability picture that held then.
-- ==========================================================================
CREATE TABLE provider_capabilities (
    capability_id      TEXT PRIMARY KEY,
    provider           TEXT NOT NULL,
    tier               TEXT,
    capability         TEXT NOT NULL,
    state              TEXT NOT NULL,
    detail             TEXT,
    observed_at        TEXT NOT NULL,
    run_id             TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id    TEXT REFERENCES raw_responses(raw_response_id),
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT provider_capabilities_id_prefix CHECK (capability_id LIKE 'cap\_%' ESCAPE '\'),
    CONSTRAINT provider_capabilities_provider_present CHECK (provider <> ''),
    CONSTRAINT provider_capabilities_state_valid CHECK (state IN (
        'supported', 'unsupported', 'paid_tier_required', 'best_effort',
        'unavailable', 'unknown_until_audited', 'provider_history_limited'
    )),
    -- Idempotent per observation: the same (provider, tier, capability, state)
    -- at the same observed_at writes one row.
    CONSTRAINT provider_capabilities_unique
        UNIQUE (provider, tier, capability, observed_at, content_hash),
    CONSTRAINT provider_capabilities_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT provider_capabilities_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_provider_capabilities_asof
    ON provider_capabilities (provider, capability, observed_at);
CREATE INDEX idx_provider_capabilities_run ON provider_capabilities (run_id);

CREATE TRIGGER trg_provider_capabilities_no_update
BEFORE UPDATE ON provider_capabilities
BEGIN
    SELECT RAISE(ABORT, 'provider_capabilities is append-only');
END;

CREATE TRIGGER trg_provider_capabilities_no_delete
BEFORE DELETE ON provider_capabilities
BEGIN
    SELECT RAISE(ABORT, 'provider_capabilities is append-only');
END;
