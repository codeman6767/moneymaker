-- Migration f018: retrospective (Lane R) research provenance foundation.
--
-- Why this exists
-- ---------------
-- `F1_HISTORICAL_PIT_FEASIBILITY_REVIEW.md` established that Moneymaker's
-- transaction-time corpus cannot answer "what was knowable before this game"
-- for games it observed after they started: 239/239 NBA and 400/400 MLB games
-- were first observed AFTER their scheduled start, so a strict `observed_at <=
-- cutoff` dataset yields zero rows. That is the correct refusal, and it is not
-- a matcher defect.
--
-- `HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` (as amended by its independent
-- review and by `G5_PROVIDER_ID_STABILITY_REVIEW.md`) replaces the impossible
-- ask -- "pretend we were running in 2021" -- with an honest one: a SECOND,
-- clearly-labelled provenance lane in which each input is certified against a
-- stated availability basis, and in which the corpus itself is a versioned,
-- digest-identified object. This migration is only the storage contract for
-- that lane. It contains no reader, no builder, no audit engine and no market
-- client, and it changes nothing about the existing strict-PIT path.
--
-- What is deliberately NOT here
-- -----------------------------
-- * `availability_confidence` -- removed by the independent review. Eligibility
--   is binary; an ordinal "how sure are we" column invites a soft threshold to
--   be tuned until coverage looks good. Confidence belongs in the written
--   evidence grade, not in a sortable column.
-- * A materialized `effective_at` -- the review's rule is that availability is
--   DERIVED (`source_event_completed_at` + a versioned rule), not stored. A
--   stored copy is a second source of truth that silently goes stale when the
--   rule changes, and it is exactly the field a future bug would quietly
--   backdate. VERSIONED_HISTORICAL is the one case where a timestamp IS stored
--   (`source_snapshot_at`), because the provider published it -- it is source
--   evidence, not our derivation.
-- * Any `ignore_pit` / override / bypass column. There is no escape hatch.
-- * Feature VALUES. This lane certifies provenance; it is not a feature store.
--
-- Append-only, throughout
-- -----------------------
-- Every table here carries BEFORE UPDATE and BEFORE DELETE abort triggers, in
-- the same style as `raw_responses`, `game_status_history` and the e017
-- identity tables. A scientific provenance record that can be edited after the
-- fact proves nothing. Supersession therefore APPENDS a new corpus version and
-- points at the old one; the old one stays byte-identical and every experiment
-- attributed to it remains attributable.
--
-- Namespace safety
-- ----------------
-- G5 closed with a corpus-scoped, fail-closed identity contract keyed on
-- `(league, provider, entity_type, provider_id)`. Neither BALLDONTLIE nor MLB
-- StatsAPI documents global permanent non-reuse, so nothing here assumes it.
-- `namespace_generation` (the provider's API generation, e.g. `v1`) is carried
-- explicitly and is NEVER inferred from the shape of an id, so BALLDONTLIE v1
-- and v2 identifiers can never be silently equated. An unverified generation is
-- representable and is refused an ACCEPTED audit verdict.
--
-- No column here holds a credential, a URL, an authorization header, or a raw
-- provider body: findings carry stable codes plus a digest.

-- ==========================================================================
-- 1. Reconstruction corpus versions.
--
-- One row = one reproducible retrospective reconstruction context. The digest
-- inputs follow the architecture's §19 reproducibility rule: source corpus
-- fingerprint, static identity map, availability policy version, cutoff policy,
-- feature/evidence registry, target set, market snapshot evidence set, and the
-- G1 variant. A change in ANY of them is a different corpus version.
--
-- Columns that only a later F1-R execution can fill (`market_evidence_digest`,
-- `evidence_registry_digest`, `static_identity_map_digest`, `code_version`) are
-- nullable rather than back-filled with a placeholder, because a placeholder
-- would enter the semantic digest and make two different corpora look equal.
-- ==========================================================================
CREATE TABLE reconstruction_corpus_versions (
    corpus_version_id             TEXT PRIMARY KEY,
    -- Which lane this corpus belongs to. `strict_forward_pit` is accepted here
    -- so a forward corpus can be described with the same vocabulary, but a
    -- reconstructed input may never cite one (see table 5).
    provenance_class              TEXT NOT NULL,
    league_id                     TEXT NOT NULL REFERENCES leagues(league_id),
    -- The versioned deterministic policy that produced this reconstruction.
    reconstruction_policy_version TEXT NOT NULL,
    -- The cutoff policy is separate from the reconstruction policy: two corpora
    -- can share a builder and differ only in where the decision line is drawn.
    cutoff_policy_id              TEXT NOT NULL,
    cutoff_policy_version         TEXT NOT NULL,
    -- Fingerprint of the exact source evidence the reconstruction read.
    source_corpus_digest          TEXT NOT NULL,
    -- Fingerprint of the target (label) set, kept separate so a target-set
    -- change is visible even when the source corpus is unchanged.
    target_set_digest             TEXT NOT NULL,
    -- NULL until the corresponding artefact exists. See the header note.
    evidence_registry_digest      TEXT,
    static_identity_map_digest    TEXT,
    market_evidence_digest        TEXT,
    -- G1-B is the core immutable-fact baseline; G1-A adds correction-sensitive
    -- box-score detail and may never be reported as transaction-time-exact.
    -- They are separate variants by construction, never silently merged.
    g1_variant                    TEXT NOT NULL,
    -- Repository revision, where the build environment can supply one.
    code_version                  TEXT,
    -- Deterministic digest over the semantic fields above. Excludes created_at
    -- (audit wall-clock, not evidence) and excludes the surrogate id.
    semantic_digest               TEXT NOT NULL,
    -- Supersession APPENDS. The superseded row is never touched.
    supersedes_corpus_version_id  TEXT
        REFERENCES reconstruction_corpus_versions(corpus_version_id),
    created_at                    TEXT NOT NULL,
    CONSTRAINT rcv_id_prefix CHECK (corpus_version_id LIKE 'rcv\_%' ESCAPE '\'),
    CONSTRAINT rcv_provenance_class CHECK (provenance_class IN (
        'strict_forward_pit', 'reconstructed_research', 'label_only_retrospective')),
    CONSTRAINT rcv_g1_variant CHECK (g1_variant IN ('g1_b_core', 'g1_a_extended')),
    CONSTRAINT rcv_policy_present CHECK (TRIM(reconstruction_policy_version) <> ''),
    CONSTRAINT rcv_cutoff_id_present CHECK (TRIM(cutoff_policy_id) <> ''),
    CONSTRAINT rcv_cutoff_version_present CHECK (TRIM(cutoff_policy_version) <> ''),
    CONSTRAINT rcv_source_digest_present CHECK (TRIM(source_corpus_digest) <> ''),
    CONSTRAINT rcv_target_digest_present CHECK (TRIM(target_set_digest) <> ''),
    -- A NULL means "not applicable / not yet produced". An empty string would
    -- be an assertion that the artefact exists and is empty, which is false.
    CONSTRAINT rcv_evidence_digest_nonempty
        CHECK (evidence_registry_digest IS NULL OR TRIM(evidence_registry_digest) <> ''),
    CONSTRAINT rcv_identity_digest_nonempty
        CHECK (static_identity_map_digest IS NULL OR TRIM(static_identity_map_digest) <> ''),
    CONSTRAINT rcv_market_digest_nonempty
        CHECK (market_evidence_digest IS NULL OR TRIM(market_evidence_digest) <> ''),
    CONSTRAINT rcv_code_version_nonempty
        CHECK (code_version IS NULL OR TRIM(code_version) <> ''),
    CONSTRAINT rcv_semantic_digest_present CHECK (TRIM(semantic_digest) <> ''),
    -- A corpus cannot supersede itself; combined with append-only insertion and
    -- the self-referencing FK, the supersession graph is acyclic by
    -- construction (an edge can only point at a row that already existed).
    CONSTRAINT rcv_no_self_supersede
        CHECK (supersedes_corpus_version_id IS NULL
               OR supersedes_corpus_version_id <> corpus_version_id),
    CONSTRAINT rcv_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- The semantic digest IS the corpus identity. Two rows with the same digest
    -- would be the same corpus recorded twice, which would make "which corpus
    -- produced this experiment" ambiguous.
    CONSTRAINT rcv_semantic_digest_unique UNIQUE (semantic_digest)
);

CREATE INDEX idx_rcv_league ON reconstruction_corpus_versions (league_id, created_at);
CREATE INDEX idx_rcv_supersedes
    ON reconstruction_corpus_versions (supersedes_corpus_version_id);

CREATE TRIGGER trg_rcv_no_update
BEFORE UPDATE ON reconstruction_corpus_versions
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_versions is append-only');
END;

CREATE TRIGGER trg_rcv_no_delete
BEFORE DELETE ON reconstruction_corpus_versions
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_versions is append-only');
END;

-- Enforced here as well as by the FK, because SQLite honours foreign keys only
-- when `PRAGMA foreign_keys` is on and this invariant is what keeps the
-- supersession graph acyclic.
CREATE TRIGGER trg_rcv_supersedes_exists
BEFORE INSERT ON reconstruction_corpus_versions
FOR EACH ROW WHEN NEW.supersedes_corpus_version_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'superseded corpus version does not exist')
    WHERE NOT EXISTS (SELECT 1 FROM reconstruction_corpus_versions
                      WHERE corpus_version_id = NEW.supersedes_corpus_version_id);
END;

-- ==========================================================================
-- 2. Identity audit records (G5).
--
-- One row = the result of running the corpus-scoped identity-consistency audit
-- for one (league, provider, namespace generation, entity type) over one exact
-- source corpus. The audit ENGINE is not implemented here; this is the contract
-- its result must satisfy.
--
-- The G5 review's central point is encoded structurally: an ACCEPTED verdict
-- requires zero collisions AND a verified namespace generation. There is no way
-- to record "accepted, but with three collisions we decided to ignore".
-- ==========================================================================
CREATE TABLE identity_audit_records (
    identity_audit_id     TEXT PRIMARY KEY,
    league_id             TEXT NOT NULL REFERENCES leagues(league_id),
    provider              TEXT NOT NULL,
    -- The provider's API generation, carried explicitly. Never inferred from an
    -- id value: BALLDONTLIE v1 and v2 ids may or may not share a namespace and
    -- the documentation does not say, so they are kept distinguishable.
    namespace_generation  TEXT NOT NULL,
    -- 0 when the generation could not be established from primary evidence.
    namespace_verified    INTEGER NOT NULL,
    entity_type           TEXT NOT NULL,
    -- The exact evidence the audit ran over. An audit is only ever a statement
    -- about this corpus; a narrower window's pass never transfers to a wider
    -- one (G5 review §16), and that is why the digest is part of the identity.
    source_corpus_digest  TEXT NOT NULL,
    audit_policy_version  TEXT NOT NULL,
    distinct_ids          INTEGER NOT NULL,
    total_observations    INTEGER NOT NULL,
    collision_count       INTEGER NOT NULL,
    flagged_count         INTEGER NOT NULL DEFAULT 0,
    verdict               TEXT NOT NULL,
    semantic_digest       TEXT NOT NULL,
    -- Honest audit wall-clock. This is AUDIT time and is never reused as an
    -- availability, effective, or decision time.
    created_at            TEXT NOT NULL,
    CONSTRAINT ida_id_prefix CHECK (identity_audit_id LIKE 'ida\_%' ESCAPE '\'),
    CONSTRAINT ida_provider_present CHECK (TRIM(provider) <> ''),
    CONSTRAINT ida_namespace_present CHECK (TRIM(namespace_generation) <> ''),
    CONSTRAINT ida_namespace_verified_bool CHECK (namespace_verified IN (0, 1)),
    CONSTRAINT ida_entity_type CHECK (entity_type IN ('game', 'team', 'player')),
    CONSTRAINT ida_source_digest_present CHECK (TRIM(source_corpus_digest) <> ''),
    CONSTRAINT ida_policy_present CHECK (TRIM(audit_policy_version) <> ''),
    CONSTRAINT ida_counts_nonneg CHECK (
        distinct_ids >= 0 AND total_observations >= 0
        AND collision_count >= 0 AND flagged_count >= 0),
    -- Every distinct id must have been observed at least once.
    CONSTRAINT ida_observations_cover_ids CHECK (total_observations >= distinct_ids),
    CONSTRAINT ida_collisions_within_ids CHECK (collision_count <= distinct_ids),
    CONSTRAINT ida_verdict CHECK (verdict IN (
        'accepted', 'rejected_collision', 'rejected_namespace_unverified')),
    -- The fail-closed core of G5, enforced by the database rather than by the
    -- caller remembering to check.
    CONSTRAINT ida_accepted_is_clean CHECK (
        verdict <> 'accepted' OR (collision_count = 0 AND namespace_verified = 1)),
    CONSTRAINT ida_collision_verdict_has_collisions CHECK (
        verdict <> 'rejected_collision' OR collision_count > 0),
    CONSTRAINT ida_namespace_verdict_is_unverified CHECK (
        verdict <> 'rejected_namespace_unverified' OR namespace_verified = 0),
    CONSTRAINT ida_semantic_digest_present CHECK (TRIM(semantic_digest) <> ''),
    CONSTRAINT ida_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT ida_semantic_digest_unique UNIQUE (semantic_digest)
);

CREATE INDEX idx_ida_namespace ON identity_audit_records
    (league_id, provider, namespace_generation, entity_type, source_corpus_digest);
CREATE INDEX idx_ida_verdict ON identity_audit_records (verdict, created_at);

CREATE TRIGGER trg_ida_no_update
BEFORE UPDATE ON identity_audit_records
BEGIN
    SELECT RAISE(ABORT, 'identity_audit_records is append-only');
END;

CREATE TRIGGER trg_ida_no_delete
BEFORE DELETE ON identity_audit_records
BEGIN
    SELECT RAISE(ABORT, 'identity_audit_records is append-only');
END;

CREATE TRIGGER trg_ida_league_present
BEFORE INSERT ON identity_audit_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'identity_audit_records.league_id must exist')
    WHERE NOT EXISTS (SELECT 1 FROM leagues WHERE league_id = NEW.league_id);
END;

-- ==========================================================================
-- 3. Identity audit findings.
--
-- One row = one specific thing the audit found, at the granularity the reviewed
-- severity model needs: a player collision excludes that player, a team
-- collision excludes the games depending on that franchise, a game collision
-- excludes that game, and a namespace problem blocks the league.
--
-- `exclusion_scope` records the BLAST RADIUS of the finding as observed. What to
-- DO about a given blast radius (how many player exclusions escalate to a corpus
-- refusal, say) is versioned policy and stays in code -- storing a threshold
-- here would freeze one policy into old evidence.
--
-- `detail_json` is sanitized structured metadata (counts, codes, digests) only.
-- Raw provider bodies live in `raw_responses` and are not copied here.
-- ==========================================================================
CREATE TABLE identity_audit_findings (
    finding_id            TEXT PRIMARY KEY,
    identity_audit_id     TEXT NOT NULL
        REFERENCES identity_audit_records(identity_audit_id),
    -- The namespace key is repeated rather than only inherited, so a finding is
    -- self-describing in an export and cannot be silently re-attributed.
    league_id             TEXT NOT NULL REFERENCES leagues(league_id),
    provider              TEXT NOT NULL,
    namespace_generation  TEXT NOT NULL,
    entity_type           TEXT NOT NULL,
    -- NULL for a namespace-level finding, which is about the generation itself
    -- rather than about any particular id.
    provider_id           TEXT,
    severity              TEXT NOT NULL,
    finding_code          TEXT NOT NULL,
    classification        TEXT NOT NULL,
    exclusion_scope       TEXT NOT NULL,
    -- Sanitized structured detail; canonical JSON, no provider bodies.
    detail_json           TEXT NOT NULL DEFAULT '{}',
    detail_digest         TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    CONSTRAINT idf_id_prefix CHECK (finding_id LIKE 'idf\_%' ESCAPE '\'),
    CONSTRAINT idf_provider_present CHECK (TRIM(provider) <> ''),
    CONSTRAINT idf_namespace_present CHECK (TRIM(namespace_generation) <> ''),
    CONSTRAINT idf_entity_type CHECK (entity_type IN ('game', 'team', 'player')),
    CONSTRAINT idf_provider_id_nonempty
        CHECK (provider_id IS NULL OR TRIM(provider_id) <> ''),
    CONSTRAINT idf_severity CHECK (severity IN ('info', 'warning', 'blocking')),
    CONSTRAINT idf_code_present CHECK (TRIM(finding_code) <> ''),
    CONSTRAINT idf_classification CHECK (classification IN (
        -- Two provider ids resolve to genuinely different entities: fatal.
        'identity_collision',
        -- A secondary signal (e.g. an unexpected name change) worth a human
        -- look. Detection only -- a name NEVER overrides a stable id, and never
        -- merges two ids (G5 review section 8).
        'name_variance',
        -- The provider's API generation could not be established.
        'namespace_unverified',
        -- The evidence for this id is too thin to audit either way.
        'insufficient_evidence',
        -- Legitimate mutation, recorded so "we looked and it was fine" is
        -- evidence rather than silence: renames, relocations, reschedules.
        'legitimate_mutation')),
    CONSTRAINT idf_exclusion_scope CHECK (exclusion_scope IN (
        'none', 'entity', 'dependent_games', 'league_namespace', 'corpus')),
    -- A collision is never merely informational, and a legitimate mutation
    -- never excludes anything. These two are the reviewed severity model's
    -- load-bearing edges, so they are enforced rather than trusted.
    CONSTRAINT idf_collision_is_blocking
        CHECK (classification <> 'identity_collision' OR severity = 'blocking'),
    CONSTRAINT idf_mutation_excludes_nothing
        CHECK (classification <> 'legitimate_mutation'
               OR (severity = 'info' AND exclusion_scope = 'none')),
    -- A namespace-level problem is about the generation, not an id, and its
    -- reach is the league (or wider) by definition.
    CONSTRAINT idf_namespace_scope CHECK (
        classification <> 'namespace_unverified'
        OR (provider_id IS NULL AND exclusion_scope IN ('league_namespace', 'corpus'))),
    -- An entity-scoped exclusion must name the entity it excludes.
    CONSTRAINT idf_entity_scope_has_id CHECK (
        exclusion_scope NOT IN ('entity', 'dependent_games') OR provider_id IS NOT NULL),
    -- Only a team can take games down with it.
    CONSTRAINT idf_dependent_games_is_team CHECK (
        exclusion_scope <> 'dependent_games' OR entity_type = 'team'),
    CONSTRAINT idf_detail_json_object CHECK (detail_json LIKE '{%}'),
    CONSTRAINT idf_detail_digest_present CHECK (TRIM(detail_digest) <> ''),
    CONSTRAINT idf_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- One finding per (audit, entity, code): a replay writes nothing new.
    CONSTRAINT idf_unique UNIQUE (
        identity_audit_id, entity_type, provider_id, finding_code, detail_digest)
);

CREATE INDEX idx_idf_audit ON identity_audit_findings (identity_audit_id, severity);
CREATE INDEX idx_idf_entity ON identity_audit_findings
    (league_id, provider, namespace_generation, entity_type, provider_id);

CREATE TRIGGER trg_idf_no_update
BEFORE UPDATE ON identity_audit_findings
BEGIN
    SELECT RAISE(ABORT, 'identity_audit_findings is append-only');
END;

CREATE TRIGGER trg_idf_no_delete
BEFORE DELETE ON identity_audit_findings
BEGIN
    SELECT RAISE(ABORT, 'identity_audit_findings is append-only');
END;

-- A finding must belong to the namespace its parent audit examined. Without
-- this, an NBA finding could be filed under an MLB audit and the audit's clean
-- verdict would look like it covered evidence it never saw.
CREATE TRIGGER trg_idf_namespace_matches_audit
BEFORE INSERT ON identity_audit_findings
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'identity_audit_findings namespace must match its audit record')
    WHERE NOT EXISTS (
        SELECT 1 FROM identity_audit_records
        WHERE identity_audit_id = NEW.identity_audit_id
          AND league_id = NEW.league_id
          AND provider = NEW.provider
          AND namespace_generation = NEW.namespace_generation
          AND entity_type = NEW.entity_type);
END;

-- ==========================================================================
-- 4. Static crosswalk provenance.
--
-- One row = "within corpus version X, provider key (league, provider,
-- namespace, entity_type, provider_id) denotes canonical entity Y, and audit Z
-- cleared that namespace".
--
-- This is the STATIC_IDENTITY basis, and it is the only basis that needs no
-- effective timestamp: the id is present in the historical evidence row itself
-- and passed the corpus-scoped consistency audit, so dereferencing it adds no
-- information that was unavailable at decision time (architecture section 5;
-- G5 review section 3).
--
-- `curated_at` is AUDIT time. It is not backdated, and it is emphatically not a
-- reused `decided_at`: a matcher wall-clock is not a historical effective time.
-- Nothing in this table is derived from a name, a jersey number, a position, a
-- current roster, a team affiliation, or any outcome.
-- ==========================================================================
CREATE TABLE static_crosswalk_provenance (
    crosswalk_id           TEXT PRIMARY KEY,
    corpus_version_id      TEXT NOT NULL
        REFERENCES reconstruction_corpus_versions(corpus_version_id),
    league_id              TEXT NOT NULL REFERENCES leagues(league_id),
    provider               TEXT NOT NULL,
    namespace_generation   TEXT NOT NULL,
    entity_type            TEXT NOT NULL,
    provider_id            TEXT NOT NULL,
    -- The canonical entity. Which table this points at is decided by
    -- `entity_type`, and the triggers below enforce that -- an NBA player
    -- crosswalk cannot bind to a team id.
    canonical_entity_id    TEXT NOT NULL,
    identity_audit_id      TEXT NOT NULL
        REFERENCES identity_audit_records(identity_audit_id),
    -- The audit's digest is copied so the binding survives an export and so a
    -- mismatch is detectable without a join.
    identity_audit_digest  TEXT NOT NULL,
    provenance_policy_version TEXT NOT NULL,
    semantic_digest        TEXT NOT NULL,
    curated_at             TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    CONSTRAINT xwk_id_prefix CHECK (crosswalk_id LIKE 'xwk\_%' ESCAPE '\'),
    CONSTRAINT xwk_provider_present CHECK (TRIM(provider) <> ''),
    CONSTRAINT xwk_namespace_present CHECK (TRIM(namespace_generation) <> ''),
    CONSTRAINT xwk_entity_type CHECK (entity_type IN ('game', 'team', 'player')),
    CONSTRAINT xwk_provider_id_present CHECK (TRIM(provider_id) <> ''),
    CONSTRAINT xwk_canonical_present CHECK (TRIM(canonical_entity_id) <> ''),
    CONSTRAINT xwk_audit_digest_present CHECK (TRIM(identity_audit_digest) <> ''),
    CONSTRAINT xwk_policy_present CHECK (TRIM(provenance_policy_version) <> ''),
    CONSTRAINT xwk_semantic_digest_present CHECK (TRIM(semantic_digest) <> ''),
    CONSTRAINT xwk_curated_iso CHECK (curated_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT xwk_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- One canonical answer per provider key per corpus version. A second,
    -- different answer is a contradiction, not a newer opinion; a genuinely
    -- different answer belongs to a NEW corpus version.
    CONSTRAINT xwk_key_unique UNIQUE (
        corpus_version_id, league_id, provider, namespace_generation,
        entity_type, provider_id),
    CONSTRAINT xwk_semantic_digest_unique UNIQUE (semantic_digest)
);

CREATE INDEX idx_xwk_lookup ON static_crosswalk_provenance
    (corpus_version_id, league_id, provider, namespace_generation, entity_type,
     provider_id);
CREATE INDEX idx_xwk_canonical ON static_crosswalk_provenance
    (entity_type, canonical_entity_id);
CREATE INDEX idx_xwk_audit ON static_crosswalk_provenance (identity_audit_id);

CREATE TRIGGER trg_xwk_no_update
BEFORE UPDATE ON static_crosswalk_provenance
BEGIN
    SELECT RAISE(ABORT, 'static_crosswalk_provenance is append-only');
END;

CREATE TRIGGER trg_xwk_no_delete
BEFORE DELETE ON static_crosswalk_provenance
BEGIN
    SELECT RAISE(ABORT, 'static_crosswalk_provenance is append-only');
END;

-- The crosswalk must cite an audit that ACCEPTED the exact same namespace. A
-- rejected audit, or an audit of a different league/provider/generation/entity
-- type, proves nothing about this key.
CREATE TRIGGER trg_xwk_audit_accepted_and_matching
BEFORE INSERT ON static_crosswalk_provenance
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'static crosswalk requires an ACCEPTED identity audit for the same namespace')
    WHERE NOT EXISTS (
        SELECT 1 FROM identity_audit_records
        WHERE identity_audit_id = NEW.identity_audit_id
          AND verdict = 'accepted'
          AND league_id = NEW.league_id
          AND provider = NEW.provider
          AND namespace_generation = NEW.namespace_generation
          AND entity_type = NEW.entity_type
          AND semantic_digest = NEW.identity_audit_digest);
END;

-- Entity-type correctness. `canonical_entity_id` must exist in the table the
-- entity type names, AND in the same league -- so an MLB provider key can never
-- bind to NBA canonical identity.
CREATE TRIGGER trg_xwk_team_target_valid
BEFORE INSERT ON static_crosswalk_provenance
FOR EACH ROW WHEN NEW.entity_type = 'team'
BEGIN
    SELECT RAISE(ABORT, 'team crosswalk must bind an existing team in the same league')
    WHERE NOT EXISTS (SELECT 1 FROM teams
                      WHERE team_id = NEW.canonical_entity_id
                        AND league_id = NEW.league_id);
END;

CREATE TRIGGER trg_xwk_player_target_valid
BEFORE INSERT ON static_crosswalk_provenance
FOR EACH ROW WHEN NEW.entity_type = 'player'
BEGIN
    SELECT RAISE(ABORT, 'player crosswalk must bind an existing player in the same league')
    WHERE NOT EXISTS (SELECT 1 FROM players
                      WHERE player_id = NEW.canonical_entity_id
                        AND league_id = NEW.league_id);
END;

CREATE TRIGGER trg_xwk_game_target_valid
BEFORE INSERT ON static_crosswalk_provenance
FOR EACH ROW WHEN NEW.entity_type = 'game'
BEGIN
    SELECT RAISE(ABORT, 'game crosswalk must bind an existing game in the same league')
    WHERE NOT EXISTS (SELECT 1 FROM games
                      WHERE game_id = NEW.canonical_entity_id
                        AND league_id = NEW.league_id);
END;

-- ==========================================================================
-- 5. Reconstructed input provenance.
--
-- One row = "for target game G in corpus version X, input family F is (or is
-- not) admissible, on basis B, under rule R at policy version P, from this
-- source evidence".
--
-- This is a CERTIFICATION record, not a feature store: no feature value is
-- stored. The builder that would compute values is not implemented, and
-- deliberately so -- the provenance contract has to be reviewable before
-- anything starts producing rows that depend on it.
--
-- The per-basis obligations from the architecture (as amended) are CHECK
-- constraints rather than caller discipline:
--
--   static_identity   -> a crosswalk, and NO timestamps at all
--   event_derived     -> source_event_completed_at + a rule id, and no snapshot
--   versioned_snapshot-> source_snapshot_at (provider-published evidence)
--
-- There is no `effective_at` column: for EVENT_DERIVED, availability is derived
-- as `source_event_completed_at + rule(availability_rule_id)`, and the rule's
-- implementation digest is stored so a later code change cannot silently
-- reinterpret an accepted corpus.
-- ==========================================================================
CREATE TABLE reconstructed_input_provenance (
    input_provenance_id       TEXT PRIMARY KEY,
    corpus_version_id         TEXT NOT NULL
        REFERENCES reconstruction_corpus_versions(corpus_version_id),
    -- The target the input is being certified FOR, by provider key. Canonical
    -- game identity is reached through the crosswalk, never assumed here.
    league_id                 TEXT NOT NULL REFERENCES leagues(league_id),
    provider                  TEXT NOT NULL,
    namespace_generation      TEXT NOT NULL,
    provider_game_id          TEXT NOT NULL,
    -- e.g. 'rest_days', 'team_rolling_core'. A family, not a column name: the
    -- certification is about a class of inputs sharing one availability story.
    feature_family            TEXT NOT NULL,
    provenance_class          TEXT NOT NULL,
    -- NULL only for label_only_retrospective, which is a label and therefore
    -- has no availability story to tell.
    availability_basis        TEXT,
    availability_rule_id      TEXT,
    -- Digest of the rule's code-defined implementation at certification time.
    -- This is what makes "the rule did not silently change" checkable.
    availability_rule_digest  TEXT,
    -- Pointer to the documenting evidence for the availability claim (the
    -- architecture's `availability_source`): a stable citation key, never a URL
    -- with credentials and never a raw body.
    availability_source       TEXT,
    reconstruction_policy_version TEXT NOT NULL,
    -- What was read. A table + row id inside this database, so the claim is
    -- traceable without duplicating the evidence.
    source_evidence_table     TEXT,
    source_evidence_id        TEXT,
    -- EVENT_DERIVED only: the immutable instant the source event completed.
    source_event_completed_at TEXT,
    -- VERSIONED_HISTORICAL only: the provider's own published snapshot instant.
    -- Stored because the PROVIDER published it; it is source evidence, not our
    -- derivation.
    source_snapshot_at        TEXT,
    -- STATIC_IDENTITY only.
    crosswalk_id              TEXT REFERENCES static_crosswalk_provenance(crosswalk_id),
    eligibility               TEXT NOT NULL,
    exclusion_code            TEXT,
    semantic_digest           TEXT NOT NULL,
    created_at                TEXT NOT NULL,
    CONSTRAINT rip_id_prefix CHECK (input_provenance_id LIKE 'rip\_%' ESCAPE '\'),
    CONSTRAINT rip_provider_present CHECK (TRIM(provider) <> ''),
    CONSTRAINT rip_namespace_present CHECK (TRIM(namespace_generation) <> ''),
    CONSTRAINT rip_game_id_present CHECK (TRIM(provider_game_id) <> ''),
    CONSTRAINT rip_family_present CHECK (TRIM(feature_family) <> ''),
    CONSTRAINT rip_policy_present CHECK (TRIM(reconstruction_policy_version) <> ''),
    CONSTRAINT rip_provenance_class CHECK (provenance_class IN (
        'strict_forward_pit', 'reconstructed_research', 'label_only_retrospective')),
    CONSTRAINT rip_availability_basis CHECK (availability_basis IS NULL
        OR availability_basis IN ('static_identity', 'event_derived', 'versioned_snapshot')),
    CONSTRAINT rip_eligibility CHECK (eligibility IN ('eligible', 'excluded')),
    -- FORWARD_ONLY evidence can never be certified as reconstructed research.
    -- The rule is enforced by there being no forward basis to name: a
    -- reconstructed-research row must carry one of the three retrospective
    -- bases, and `strict_forward_pit` may not be recorded in this table at all
    -- (it belongs to the existing AsOfReader path, which this lane never
    -- touches).
    CONSTRAINT rip_not_forward_lane CHECK (provenance_class <> 'strict_forward_pit'),
    CONSTRAINT rip_research_has_basis CHECK (
        provenance_class <> 'reconstructed_research' OR availability_basis IS NOT NULL),
    -- A label is distinguishable from a reconstructed predictive input by
    -- construction: it has no availability basis, no rule and no crosswalk.
    CONSTRAINT rip_label_has_no_basis CHECK (
        provenance_class <> 'label_only_retrospective'
        OR (availability_basis IS NULL AND availability_rule_id IS NULL
            AND availability_rule_digest IS NULL AND crosswalk_id IS NULL
            AND source_event_completed_at IS NULL AND source_snapshot_at IS NULL)),
    -- STATIC_IDENTITY: a crosswalk, and no timestamps. A static identity that
    -- needed an effective time would not be static.
    CONSTRAINT rip_static_shape CHECK (
        availability_basis <> 'static_identity'
        OR (crosswalk_id IS NOT NULL
            AND source_event_completed_at IS NULL
            AND source_snapshot_at IS NULL)),
    -- EVENT_DERIVED: the completion instant and the versioned rule that turns
    -- it into an availability instant.
    CONSTRAINT rip_event_shape CHECK (
        availability_basis <> 'event_derived'
        OR (source_event_completed_at IS NOT NULL
            AND availability_rule_id IS NOT NULL
            AND availability_rule_digest IS NOT NULL
            AND source_snapshot_at IS NULL)),
    -- VERSIONED_HISTORICAL: the provider's published snapshot instant.
    CONSTRAINT rip_versioned_shape CHECK (
        availability_basis <> 'versioned_snapshot'
        OR (source_snapshot_at IS NOT NULL
            AND source_event_completed_at IS NULL)),
    CONSTRAINT rip_rule_id_nonempty
        CHECK (availability_rule_id IS NULL OR TRIM(availability_rule_id) <> ''),
    CONSTRAINT rip_rule_digest_nonempty
        CHECK (availability_rule_digest IS NULL OR TRIM(availability_rule_digest) <> ''),
    -- A rule id without its digest is unverifiable, and a digest without an id
    -- names nothing. They travel together.
    CONSTRAINT rip_rule_pair CHECK (
        (availability_rule_id IS NULL) = (availability_rule_digest IS NULL)),
    CONSTRAINT rip_source_nonempty
        CHECK (availability_source IS NULL OR TRIM(availability_source) <> ''),
    CONSTRAINT rip_evidence_pair CHECK (
        (source_evidence_table IS NULL) = (source_evidence_id IS NULL)),
    CONSTRAINT rip_evidence_table_nonempty
        CHECK (source_evidence_table IS NULL OR TRIM(source_evidence_table) <> ''),
    CONSTRAINT rip_completed_iso CHECK (
        source_event_completed_at IS NULL
        OR source_event_completed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT rip_snapshot_iso CHECK (
        source_snapshot_at IS NULL OR source_snapshot_at LIKE '____-__-__T__:__:__%Z'),
    -- Eligibility and its reason are mutually determined: an eligible input has
    -- nothing to explain, an excluded one must say why.
    CONSTRAINT rip_exclusion_code_shape CHECK (
        (eligibility = 'excluded') = (exclusion_code IS NOT NULL)),
    CONSTRAINT rip_exclusion_code_nonempty
        CHECK (exclusion_code IS NULL OR TRIM(exclusion_code) <> ''),
    CONSTRAINT rip_semantic_digest_present CHECK (TRIM(semantic_digest) <> ''),
    CONSTRAINT rip_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- One certification per (corpus, target, family). A different answer is a
    -- different corpus version, not an overwrite.
    CONSTRAINT rip_target_unique UNIQUE (
        corpus_version_id, league_id, provider, namespace_generation,
        provider_game_id, feature_family),
    CONSTRAINT rip_semantic_digest_unique UNIQUE (semantic_digest)
);

CREATE INDEX idx_rip_corpus ON reconstructed_input_provenance
    (corpus_version_id, feature_family, eligibility);
CREATE INDEX idx_rip_target ON reconstructed_input_provenance
    (corpus_version_id, league_id, provider, namespace_generation, provider_game_id);
CREATE INDEX idx_rip_crosswalk ON reconstructed_input_provenance (crosswalk_id);

CREATE TRIGGER trg_rip_no_update
BEFORE UPDATE ON reconstructed_input_provenance
BEGIN
    SELECT RAISE(ABORT, 'reconstructed_input_provenance is append-only');
END;

CREATE TRIGGER trg_rip_no_delete
BEFORE DELETE ON reconstructed_input_provenance
BEGIN
    SELECT RAISE(ABORT, 'reconstructed_input_provenance is append-only');
END;

-- A certification must belong to the corpus version it names, in that corpus's
-- league. Certifying an NBA input into an MLB corpus would corrupt the corpus
-- digest's meaning.
CREATE TRIGGER trg_rip_corpus_league_matches
BEFORE INSERT ON reconstructed_input_provenance
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'reconstructed input must match its corpus version league')
    WHERE NOT EXISTS (
        SELECT 1 FROM reconstruction_corpus_versions
        WHERE corpus_version_id = NEW.corpus_version_id
          AND league_id = NEW.league_id);
END;

-- A cited crosswalk must belong to the SAME corpus version and namespace. This
-- is what stops a clean crosswalk from one corpus silently vouching for an
-- input in another.
CREATE TRIGGER trg_rip_crosswalk_same_corpus
BEFORE INSERT ON reconstructed_input_provenance
FOR EACH ROW WHEN NEW.crosswalk_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT,
        'cited static crosswalk must belong to the same corpus version and namespace')
    WHERE NOT EXISTS (
        SELECT 1 FROM static_crosswalk_provenance
        WHERE crosswalk_id = NEW.crosswalk_id
          AND corpus_version_id = NEW.corpus_version_id
          AND league_id = NEW.league_id
          AND provider = NEW.provider
          AND namespace_generation = NEW.namespace_generation);
END;
