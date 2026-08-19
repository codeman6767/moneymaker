-- Migration f022: Stage-A plan/acquisition provenance + corpus evidence lanes.
--
-- Authority
-- ---------
-- `STAGE_A_CORPUS_DIGEST_AND_ACQUISITION_MANIFEST_ARCHITECTURE.md` as reconciled
-- at de7a48a, and its independent review, which is authoritative wherever the
-- two disagree.
--
-- THE CENTRAL CORRECTION THIS MIGRATION IS BUILT AROUND
-- ----------------------------------------------------
-- `reconstruction_corpus_versions` is CONTENT-ADDRESSED: f018 declares
-- `UNIQUE (semantic_digest)` with the comment "The semantic digest IS the corpus
-- identity", and the digest is computed over a fixed field set that already
-- includes `market_evidence_digest`.
--
-- So evidence lanes are NOT appended to an existing corpus. The E0 flow is
--
--     official corpus C1  +  certified E0 lane digest D  ->  NEW corpus C2
--
-- where C2.market_evidence_digest = D, C2 supersedes C1 via f018's existing
-- supersession semantics, and C1 is never touched. Every audit and crosswalk
-- bound to C1 keeps its exact meaning forever. The lane binding rows below carry
-- the per-lane detail (provider, namespace generation, policy versions,
-- acquisition membership) that a single digest column cannot express.
--
-- WHAT THIS MIGRATION DOES NOT DO
-- -------------------------------
-- It registers no linking provider, widens no attested generation, creates no
-- identity audit, no crosswalk, and no canonical game. It declares no real
-- Stage-A plan and acquires nothing. The schema may DESCRIBE a future secondary
-- provider; it does not AUTHORIZE one.
--
-- Additive only. No existing table, column, trigger, index or row is modified,
-- except one `ALTER TABLE ... ADD COLUMN` on `identity_audit_records`, which
-- SQLite performs without rewriting rows and which defaults to NULL, so every
-- existing audit is byte-identical afterwards.
--
-- APPEND-ONLY HARDENING
-- ---------------------
-- Every new table below uses the f021 pattern from day one: BEFORE UPDATE and
-- BEFORE DELETE guards PLUS a content-aware BEFORE INSERT guard. The BEFORE
-- INSERT guard is what actually stops `REPLACE` / `INSERT OR REPLACE`, because
-- SQLite's REPLACE conflict resolution performs an implicit DELETE that does not
-- fire DELETE triggers unless `PRAGMA recursive_triggers` is ON -- and a pragma
-- is per-connection, so the guarantee cannot live there. Each guard compares
-- CONTENT (not bare key existence) so that a legitimate idempotent
-- `INSERT OR IGNORE` of an identical row stays a no-op rather than becoming a
-- hard error; `RAISE(ABORT)` is not suppressed by `OR IGNORE`.

-- ==========================================================================
-- 1. stage_a_plans -- WHAT was declared.
--
-- Plan identity is separate from execution identity (review D6). A plan may be
-- executed more than once: an aborted run restarted from scratch, an independent
-- reproduction months later, the same plan against a different database. Making
-- `acquisition_id` a function of `plan_digest` would make those unrepresentable.
--
-- `plan_digest` is the SEMANTIC identity and is computed by the Stage-A manifest
-- module over league/provider/namespace/sport, the official parent corpus
-- provenance, the policy versions, the exact sorted bucket set, the exact target
-- set and the target->bucket mapping. It contains NO machine-local path: the
-- same logical plan checked out at C:\repo or /home/x/repo has one identity.
-- `manifest_path` below is convenience provenance ONLY and is never hashed.
-- ==========================================================================
CREATE TABLE stage_a_plans (
    plan_id                       TEXT PRIMARY KEY,
    -- Semantic identity. Recomputed by the verifier from the committed manifest.
    plan_digest                   TEXT NOT NULL,
    -- Source-control binding. The CONTENT digest is identity; the commit SHA
    -- binds history. Neither is a trusted wall-clock: git commit dates are
    -- attacker-settable (GIT_COMMITTER_DATE), so this is tamper-EVIDENCE, not
    -- proof of temporal ordering. See the header of the acquisition table.
    manifest_commit_sha           TEXT NOT NULL,
    manifest_content_digest       TEXT NOT NULL,
    -- NON-SEMANTIC pointer. May rot; never enters any digest.
    manifest_path                 TEXT NOT NULL,
    manifest_format_version       TEXT NOT NULL,
    plan_policy_version           TEXT NOT NULL,
    league_id                     TEXT NOT NULL REFERENCES leagues(league_id),
    provider                      TEXT NOT NULL,
    namespace_generation          TEXT NOT NULL,
    sport_key                     TEXT NOT NULL,
    -- The OFFICIAL parent corpus this plan's targets were drawn from. Lane
    -- attachment later refuses unless the parent corpus matches BOTH of these
    -- (review D14): without them, a lane planned from corpus C1 could be
    -- attached to an unrelated corpus Cx with the same league.
    official_source_corpus_digest TEXT NOT NULL,
    official_target_set_digest    TEXT NOT NULL,
    -- Decision-horizon and flooring contract that produced the bucket set.
    decision_horizon_minutes      INTEGER NOT NULL,
    bucket_floor_seconds          INTEGER NOT NULL,
    acquisition_policy_version    TEXT NOT NULL,
    projection_policy_version     TEXT NOT NULL,
    cost_policy_version           TEXT NOT NULL,
    created_at                    TEXT NOT NULL,

    CONSTRAINT sap_id_prefix CHECK (plan_id LIKE 'sap\_%' ESCAPE '\'),
    CONSTRAINT sap_digest_present CHECK (TRIM(plan_digest) <> ''),
    CONSTRAINT sap_commit_present CHECK (TRIM(manifest_commit_sha) <> ''),
    CONSTRAINT sap_content_digest_present CHECK (TRIM(manifest_content_digest) <> ''),
    CONSTRAINT sap_path_present CHECK (TRIM(manifest_path) <> ''),
    CONSTRAINT sap_format_present CHECK (TRIM(manifest_format_version) <> ''),
    CONSTRAINT sap_policy_present CHECK (TRIM(plan_policy_version) <> ''),
    CONSTRAINT sap_provider_present CHECK (TRIM(provider) <> ''),
    CONSTRAINT sap_generation_present CHECK (TRIM(namespace_generation) <> ''),
    CONSTRAINT sap_sport_present CHECK (TRIM(sport_key) <> ''),
    CONSTRAINT sap_official_source_present CHECK (TRIM(official_source_corpus_digest) <> ''),
    CONSTRAINT sap_official_target_present CHECK (TRIM(official_target_set_digest) <> ''),
    CONSTRAINT sap_horizon_positive CHECK (decision_horizon_minutes > 0),
    CONSTRAINT sap_floor_positive CHECK (bucket_floor_seconds > 0),
    CONSTRAINT sap_acq_policy_present CHECK (TRIM(acquisition_policy_version) <> ''),
    CONSTRAINT sap_proj_policy_present CHECK (TRIM(projection_policy_version) <> ''),
    CONSTRAINT sap_cost_policy_present CHECK (TRIM(cost_policy_version) <> ''),
    CONSTRAINT sap_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- The plan digest IS the plan identity, exactly as the corpus semantic
    -- digest is the corpus identity.
    CONSTRAINT sap_digest_unique UNIQUE (plan_digest)
);

CREATE INDEX idx_sap_league_provider ON stage_a_plans (league_id, provider);

-- ==========================================================================
-- 2. stage_a_plan_targets -- the DB-provable target population (review D13).
--
-- The reviewed architecture bound the target set into `plan_digest` but kept it
-- only in a committed file, while presenting certification as a DATABASE
-- property. The database could then not prove target completeness at all.
--
-- The attack this closes: 239 targets map onto 160 buckets, so by pigeonhole
-- many buckets serve more than one target. Dropping one target from a shared
-- bucket leaves the sorted bucket set BYTE-IDENTICAL -- a target silently
-- disappears from the declared population while the bucket set is unchanged.
--
-- These are already-preserved OFFICIAL canonical games. Recording one here maps
-- no provider event to a canonical game: Stage A stays identity-free.
-- ==========================================================================
CREATE TABLE stage_a_plan_targets (
    plan_id             TEXT NOT NULL REFERENCES stage_a_plans(plan_id),
    canonical_game_id   TEXT NOT NULL REFERENCES games(game_id),
    requested_at_bucket TEXT NOT NULL,
    created_at          TEXT NOT NULL,

    CONSTRAINT sapt_bucket_present CHECK (TRIM(requested_at_bucket) <> ''),
    CONSTRAINT sapt_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- Exactly one bucket per target: a target may not appear twice, and may not
    -- map to two buckets.
    PRIMARY KEY (plan_id, canonical_game_id)
);

CREATE INDEX idx_sapt_bucket ON stage_a_plan_targets (plan_id, requested_at_bucket);

-- ==========================================================================
-- 3. stage_a_planned_buckets -- the CLOSED set of authorized request buckets.
--
-- A requested bucket is what WE asked the provider for. It is NOT the provider
-- snapshot timestamp: the provider answers with the nearest snapshot at or
-- before the request, so the two differ routinely and are never conflated.
-- ==========================================================================
CREATE TABLE stage_a_planned_buckets (
    plan_id             TEXT NOT NULL REFERENCES stage_a_plans(plan_id),
    requested_at_bucket TEXT NOT NULL,
    created_at          TEXT NOT NULL,

    CONSTRAINT sapb_bucket_present CHECK (TRIM(requested_at_bucket) <> ''),
    CONSTRAINT sapb_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    PRIMARY KEY (plan_id, requested_at_bucket)
);

-- ==========================================================================
-- 4. stage_a_acquisitions -- ONE EXECUTION of a plan.
--
-- `plan_id` is deliberately NOT unique (review D6). Resume of the same execution
-- reuses one acquisition; an independent later reproduction of the same plan
-- gets a distinct acquisition with its own request provenance.
--
-- `registered_at` replaces the reviewed `declared_at`, which was deleted because
-- it was caller-supplied, directly backdatable, and therefore proved nothing.
-- `registered_at` has one job: it is the instant every ordinary (non-probe)
-- attempt's cited `raw_responses.requested_at` must be at or after.
--
-- TRUST BOUNDARY, STATED HONESTLY. This is TAMPER-EVIDENCE, not cryptographic
-- proof of temporal ordering. An operator with direct SQL write access to this
-- database can construct a self-consistent history. Nothing here claims more.
-- `registered_at` is NOT part of semantic plan identity and never enters a
-- digest, so replay stays deterministic.
-- ==========================================================================
CREATE TABLE stage_a_acquisitions (
    acquisition_id             TEXT PRIMARY KEY,
    plan_id                    TEXT NOT NULL REFERENCES stage_a_plans(plan_id),
    acquisition_policy_version TEXT NOT NULL,
    -- Projection policy lives at the ACQUISITION (review D11): a lane may
    -- aggregate several acquisitions, so a single lane-level value could not
    -- describe the union. The gate requires uniformity across a lane's members.
    projection_policy_version  TEXT NOT NULL,
    request_budget             INTEGER NOT NULL,
    credit_budget              INTEGER NOT NULL,
    registered_at              TEXT NOT NULL,
    created_at                 TEXT NOT NULL,

    CONSTRAINT sga_id_prefix CHECK (acquisition_id LIKE 'sga\_%' ESCAPE '\'),
    CONSTRAINT sga_acq_policy_present CHECK (TRIM(acquisition_policy_version) <> ''),
    CONSTRAINT sga_proj_policy_present CHECK (TRIM(projection_policy_version) <> ''),
    CONSTRAINT sga_request_budget_nonneg CHECK (request_budget >= 0),
    CONSTRAINT sga_credit_budget_nonneg CHECK (credit_budget >= 0),
    CONSTRAINT sga_registered_iso CHECK (registered_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sga_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
);

CREATE INDEX idx_sga_plan ON stage_a_acquisitions (plan_id);

-- ==========================================================================
-- 5. stage_a_probe_registrations -- makes probe reuse machine-verifiable.
--
-- Without this object, `REUSED_PROBE_RESPONSE` is a GENERAL bypass of
-- plan-before-network wearing a specific name (review D9): `raw_responses` has
-- no probe classification, so nothing distinguishes the one legitimate
-- capability probe from any arbitrary pre-plan response chosen after outcomes
-- were known.
--
-- A registration means ONLY: "this exact preserved response was an
-- independently documented capability probe that MAY be considered for Stage-A
-- reuse under the strict gate." It carries NO identity semantics: not that an
-- audit was accepted, not that the event id is stable, not that any event maps
-- to a canonical game, and not that the provider is trusted for identity.
-- ==========================================================================
CREATE TABLE stage_a_probe_registrations (
    probe_registration_id TEXT PRIMARY KEY,
    raw_response_id       TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    probe_policy_version  TEXT NOT NULL,
    -- The committed probe report this registration rests on.
    probe_report_commit_sha TEXT NOT NULL,
    probe_report_path       TEXT NOT NULL,
    registered_at         TEXT NOT NULL,
    created_at            TEXT NOT NULL,

    CONSTRAINT spr_id_prefix CHECK (probe_registration_id LIKE 'spr\_%' ESCAPE '\'),
    CONSTRAINT spr_policy_present CHECK (TRIM(probe_policy_version) <> ''),
    CONSTRAINT spr_commit_present CHECK (TRIM(probe_report_commit_sha) <> ''),
    CONSTRAINT spr_path_present CHECK (TRIM(probe_report_path) <> ''),
    CONSTRAINT spr_registered_iso CHECK (registered_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT spr_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- One registration per preserved response.
    CONSTRAINT spr_raw_response_unique UNIQUE (raw_response_id)
);

-- ==========================================================================
-- 6. stage_a_request_attempts -- the honest per-attempt outcome ledger.
--
-- The reviewed reconciliation balanced PLANNED BUCKETS against a SUM OF OUTCOME
-- COUNTS while permitting N attempts per bucket. That cannot hold once any
-- bucket is retried (review D4). Two reconciliations replace it, both derived by
-- the verifier: attempt-level (every attempt classified, none discarded) and
-- terminal-bucket-level (every planned bucket gets exactly one derived terminal
-- state). FIRST-PASS POLICY: retries are FORBIDDEN -- see the trigger below.
-- `attempt_ordinal` is retained structurally so a future EXPLICITLY VERSIONED
-- retry policy needs no schema change, and so a failed attempt can never be
-- erased by a later success.
--
-- `raw_response_id` rules are fail-closed and enforced by CHECK: an outcome that
-- carries an HTTP response REQUIRES the preserved exchange (a provider failure
-- with a body must never discard its raw evidence), and an outcome where no
-- transport completed MUST be NULL. No outcome leaves it optional.
-- ==========================================================================
CREATE TABLE stage_a_request_attempts (
    attempt_id          TEXT PRIMARY KEY,
    acquisition_id      TEXT NOT NULL REFERENCES stage_a_acquisitions(acquisition_id),
    requested_at_bucket TEXT NOT NULL,
    attempt_ordinal     INTEGER NOT NULL,
    outcome             TEXT NOT NULL,
    raw_response_id     TEXT REFERENCES raw_responses(raw_response_id),
    created_at          TEXT NOT NULL,

    CONSTRAINT sat_id_prefix CHECK (attempt_id LIKE 'sat\_%' ESCAPE '\'),
    CONSTRAINT sat_bucket_present CHECK (TRIM(requested_at_bucket) <> ''),
    CONSTRAINT sat_ordinal_positive CHECK (attempt_ordinal >= 1),
    CONSTRAINT sat_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT sat_outcome_known CHECK (outcome IN (
        'success_full_snapshot',
        'success_empty_data',
        'reused_probe_response',
        'malformed_wrapper',
        'projection_rejected_snapshot',
        'http_or_provider_failure',
        'entitlement_or_auth_failure',
        'quota_blocked',
        'budget_blocked',
        'transport_failure')),
    -- Outcomes that preserved an HTTP exchange REQUIRE the raw response.
    CONSTRAINT sat_response_required CHECK (
        outcome NOT IN (
            'success_full_snapshot', 'success_empty_data', 'reused_probe_response',
            'malformed_wrapper', 'projection_rejected_snapshot',
            'http_or_provider_failure', 'entitlement_or_auth_failure')
        OR raw_response_id IS NOT NULL),
    -- Outcomes where no transport completed MUST NOT cite one.
    CONSTRAINT sat_response_forbidden CHECK (
        outcome NOT IN ('quota_blocked', 'budget_blocked', 'transport_failure')
        OR raw_response_id IS NULL),
    CONSTRAINT sat_attempt_unique UNIQUE (acquisition_id, requested_at_bucket, attempt_ordinal)
);

-- One preserved response may be cited at most once within an acquisition, so it
-- cannot be counted for two buckets nor as both an ordinary success and a reuse.
-- Deliberately scoped PER ACQUISITION, not globally: the same probe response may
-- legitimately be reused by two independent acquisitions of the same plan, which
-- is honest shared provenance rather than double counting.
CREATE UNIQUE INDEX idx_sat_response_once_per_acquisition
    ON stage_a_request_attempts (acquisition_id, raw_response_id)
    WHERE raw_response_id IS NOT NULL;

CREATE INDEX idx_sat_bucket ON stage_a_request_attempts (acquisition_id, requested_at_bucket);

-- ==========================================================================
-- 7. corpus_evidence_lane_bindings -- "this FIXED corpus contains this exact
--    certified evidence lane."
--
-- The lane carries what a single digest column cannot: provider, namespace
-- generation, the digest policy that resolved the evidence, and (through the
-- join table below) which acquisitions compose it.
--
-- `acquisition_set_digest` commits to the sorted member-acquisition set, so BOTH
-- directions of the reviewed omission attack fail recomputation: a lane citing
-- {A} whose evidence digest covers A+B, and a lane citing {A,B} whose evidence
-- digest covers only A.
--
-- A lane digest is NEVER trusted because it was inserted. A trigger cannot
-- compute SHA-256 over evidence rows, and append-only only prevents REWRITING a
-- forged value. Before a lane may back an ACCEPTED audit the deterministic
-- verifier must recompute it from exact evidence membership.
-- ==========================================================================
CREATE TABLE corpus_evidence_lane_bindings (
    lane_binding_id           TEXT PRIMARY KEY,
    corpus_version_id         TEXT NOT NULL
        REFERENCES reconstruction_corpus_versions(corpus_version_id),
    evidence_lane             TEXT NOT NULL,
    provider                  TEXT NOT NULL,
    namespace_generation      TEXT NOT NULL,
    league_id                 TEXT NOT NULL REFERENCES leagues(league_id),
    digest_policy_version     TEXT NOT NULL,
    -- The lane's own evidence fingerprint.
    lane_evidence_digest      TEXT NOT NULL,
    acquisition_set_digest    TEXT NOT NULL,
    -- Derived and required uniform across every member acquisition.
    projection_policy_version TEXT NOT NULL,
    created_at                TEXT NOT NULL,

    CONSTRAINT eln_id_prefix CHECK (lane_binding_id LIKE 'eln\_%' ESCAPE '\'),
    CONSTRAINT eln_lane_known CHECK (evidence_lane IN (
        'official_identity', 'market_events_e0')),
    CONSTRAINT eln_provider_present CHECK (TRIM(provider) <> ''),
    CONSTRAINT eln_generation_present CHECK (TRIM(namespace_generation) <> ''),
    CONSTRAINT eln_digest_policy_present CHECK (TRIM(digest_policy_version) <> ''),
    CONSTRAINT eln_evidence_digest_present CHECK (TRIM(lane_evidence_digest) <> ''),
    CONSTRAINT eln_acq_set_digest_present CHECK (TRIM(acquisition_set_digest) <> ''),
    CONSTRAINT eln_proj_policy_present CHECK (TRIM(projection_policy_version) <> ''),
    CONSTRAINT eln_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    -- One lane of a given kind per corpus.
    CONSTRAINT eln_lane_unique UNIQUE (corpus_version_id, evidence_lane)
);

CREATE INDEX idx_eln_corpus ON corpus_evidence_lane_bindings (corpus_version_id);

-- ==========================================================================
-- 8. corpus_evidence_lane_acquisitions -- lane <-> many acquisitions.
--
-- Replaces the reviewed single `acquisition_manifest_id` column, which
-- contradicted the architecture's own statement that several acquisitions may
-- feed one lane (review D5).
-- ==========================================================================
CREATE TABLE corpus_evidence_lane_acquisitions (
    lane_binding_id TEXT NOT NULL REFERENCES corpus_evidence_lane_bindings(lane_binding_id),
    acquisition_id  TEXT NOT NULL REFERENCES stage_a_acquisitions(acquisition_id),
    created_at      TEXT NOT NULL,

    CONSTRAINT elna_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    PRIMARY KEY (lane_binding_id, acquisition_id)
);

-- ==========================================================================
-- 9. identity_audit_records gains a lane reference.
--
-- SQLite performs ADD COLUMN without rewriting rows, and the column defaults to
-- NULL, so every existing audit is unchanged and remains valid.
--
-- NULL is permitted ONLY for the legacy official path. The independent review
-- REPRODUCED the bypass this closes: a new linking-provider audit
-- (provider='the_odds_api', namespace_verified=1, verdict='accepted') that cites
-- no lane and declares the official corpus digest was ACCEPTED, and its
-- crosswalk inserted cleanly. Nullable-for-legacy is nullable-for-everyone
-- unless a provider-conditional guard forbids it -- see trg_ida_lane_required.
-- ==========================================================================
ALTER TABLE identity_audit_records
    ADD COLUMN lane_binding_id TEXT
        REFERENCES corpus_evidence_lane_bindings(lane_binding_id);

-- ==========================================================================
-- Append-only guards. Three per table, matching the hardened f021 pattern.
-- ==========================================================================
CREATE TRIGGER trg_sap_no_update BEFORE UPDATE ON stage_a_plans
BEGIN SELECT RAISE(ABORT, 'stage_a_plans is append-only'); END;
CREATE TRIGGER trg_sap_no_delete BEFORE DELETE ON stage_a_plans
BEGIN SELECT RAISE(ABORT, 'stage_a_plans is append-only'); END;
CREATE TRIGGER trg_sap_no_replace BEFORE INSERT ON stage_a_plans
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM stage_a_plans
    WHERE plan_id = NEW.plan_id AND plan_digest IS NOT NEW.plan_digest)
BEGIN
    SELECT RAISE(ABORT,
        'stage_a_plans is append-only; refusing to replace a plan with different content');
END;

CREATE TRIGGER trg_sapt_no_update BEFORE UPDATE ON stage_a_plan_targets
BEGIN SELECT RAISE(ABORT, 'stage_a_plan_targets is append-only'); END;
CREATE TRIGGER trg_sapt_no_delete BEFORE DELETE ON stage_a_plan_targets
BEGIN SELECT RAISE(ABORT, 'stage_a_plan_targets is append-only'); END;
CREATE TRIGGER trg_sapt_no_replace BEFORE INSERT ON stage_a_plan_targets
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM stage_a_plan_targets
    WHERE plan_id = NEW.plan_id AND canonical_game_id = NEW.canonical_game_id
      AND requested_at_bucket IS NOT NEW.requested_at_bucket)
BEGIN
    SELECT RAISE(ABORT,
        'stage_a_plan_targets is append-only; refusing to remap a declared target to a different bucket');
END;

CREATE TRIGGER trg_sapb_no_update BEFORE UPDATE ON stage_a_planned_buckets
BEGIN SELECT RAISE(ABORT, 'stage_a_planned_buckets is append-only'); END;
CREATE TRIGGER trg_sapb_no_delete BEFORE DELETE ON stage_a_planned_buckets
BEGIN SELECT RAISE(ABORT, 'stage_a_planned_buckets is append-only'); END;
CREATE TRIGGER trg_sapb_no_replace BEFORE INSERT ON stage_a_planned_buckets
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM stage_a_planned_buckets
    WHERE plan_id = NEW.plan_id AND requested_at_bucket = NEW.requested_at_bucket
      AND created_at IS NOT NEW.created_at)
BEGIN
    SELECT RAISE(ABORT,
        'stage_a_planned_buckets is append-only; refusing to replace a declared bucket');
END;

CREATE TRIGGER trg_sga_no_update BEFORE UPDATE ON stage_a_acquisitions
BEGIN SELECT RAISE(ABORT, 'stage_a_acquisitions is append-only'); END;
CREATE TRIGGER trg_sga_no_delete BEFORE DELETE ON stage_a_acquisitions
BEGIN SELECT RAISE(ABORT, 'stage_a_acquisitions is append-only'); END;
CREATE TRIGGER trg_sga_no_replace BEFORE INSERT ON stage_a_acquisitions
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM stage_a_acquisitions
    WHERE acquisition_id = NEW.acquisition_id
      AND (plan_id IS NOT NEW.plan_id
           OR projection_policy_version IS NOT NEW.projection_policy_version
           OR registered_at IS NOT NEW.registered_at))
BEGIN
    SELECT RAISE(ABORT,
        'stage_a_acquisitions is append-only; refusing to replace an acquisition with different content');
END;

CREATE TRIGGER trg_spr_no_update BEFORE UPDATE ON stage_a_probe_registrations
BEGIN SELECT RAISE(ABORT, 'stage_a_probe_registrations is append-only'); END;
CREATE TRIGGER trg_spr_no_delete BEFORE DELETE ON stage_a_probe_registrations
BEGIN SELECT RAISE(ABORT, 'stage_a_probe_registrations is append-only'); END;
CREATE TRIGGER trg_spr_no_replace BEFORE INSERT ON stage_a_probe_registrations
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM stage_a_probe_registrations
    WHERE probe_registration_id = NEW.probe_registration_id
      AND (raw_response_id IS NOT NEW.raw_response_id
           OR probe_report_commit_sha IS NOT NEW.probe_report_commit_sha))
BEGIN
    SELECT RAISE(ABORT,
        'stage_a_probe_registrations is append-only; refusing to repoint a registration at different evidence');
END;

CREATE TRIGGER trg_sat_no_update BEFORE UPDATE ON stage_a_request_attempts
BEGIN SELECT RAISE(ABORT, 'stage_a_request_attempts is append-only'); END;
CREATE TRIGGER trg_sat_no_delete BEFORE DELETE ON stage_a_request_attempts
BEGIN SELECT RAISE(ABORT, 'stage_a_request_attempts is append-only'); END;
CREATE TRIGGER trg_sat_no_replace BEFORE INSERT ON stage_a_request_attempts
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM stage_a_request_attempts
    WHERE attempt_id = NEW.attempt_id
      AND (outcome IS NOT NEW.outcome
           OR raw_response_id IS NOT NEW.raw_response_id
           OR requested_at_bucket IS NOT NEW.requested_at_bucket))
BEGIN
    SELECT RAISE(ABORT,
        'stage_a_request_attempts is append-only; refusing to rewrite a recorded outcome');
END;

CREATE TRIGGER trg_eln_no_update BEFORE UPDATE ON corpus_evidence_lane_bindings
BEGIN SELECT RAISE(ABORT, 'corpus_evidence_lane_bindings is append-only'); END;
CREATE TRIGGER trg_eln_no_delete BEFORE DELETE ON corpus_evidence_lane_bindings
BEGIN SELECT RAISE(ABORT, 'corpus_evidence_lane_bindings is append-only'); END;
CREATE TRIGGER trg_eln_no_replace BEFORE INSERT ON corpus_evidence_lane_bindings
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM corpus_evidence_lane_bindings
    WHERE lane_binding_id = NEW.lane_binding_id
      AND (lane_evidence_digest IS NOT NEW.lane_evidence_digest
           OR acquisition_set_digest IS NOT NEW.acquisition_set_digest))
BEGIN
    SELECT RAISE(ABORT,
        'corpus_evidence_lane_bindings is append-only; refusing to replace a lane digest');
END;

CREATE TRIGGER trg_elna_no_update BEFORE UPDATE ON corpus_evidence_lane_acquisitions
BEGIN SELECT RAISE(ABORT, 'corpus_evidence_lane_acquisitions is append-only'); END;
CREATE TRIGGER trg_elna_no_delete BEFORE DELETE ON corpus_evidence_lane_acquisitions
BEGIN SELECT RAISE(ABORT, 'corpus_evidence_lane_acquisitions is append-only'); END;
CREATE TRIGGER trg_elna_no_replace BEFORE INSERT ON corpus_evidence_lane_acquisitions
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM corpus_evidence_lane_acquisitions
    WHERE lane_binding_id = NEW.lane_binding_id AND acquisition_id = NEW.acquisition_id
      AND created_at IS NOT NEW.created_at)
BEGIN
    SELECT RAISE(ABORT,
        'corpus_evidence_lane_acquisitions is append-only');
END;

-- ==========================================================================
-- Canonical instant validation for the new provenance clocks. Same technique as
-- f019 D5 / f020: GLOB pins the byte-exact spelling (including the upper-case
-- Z), and the strftime round-trip rejects impossible calendar instants, since
-- SQLite would otherwise normalize 2026-02-30 to 2026-03-02 and store it. IFNULL
-- is essential -- a WHERE evaluating to NULL is not a failure in SQL, which is
-- how month 99 once slipped past a shape-only check.
-- ==========================================================================
CREATE TRIGGER trg_sapt_bucket_is_a_real_instant
BEFORE INSERT ON stage_a_plan_targets FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'requested_at_bucket is not a real instant in the canonical format')
    WHERE NOT (NEW.requested_at_bucket GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.requested_at_bucket, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.requested_at_bucket, 1, 19)), '')
                   = substr(NEW.requested_at_bucket, 1, 19));
END;

CREATE TRIGGER trg_sapb_bucket_is_a_real_instant
BEFORE INSERT ON stage_a_planned_buckets FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'requested_at_bucket is not a real instant in the canonical format')
    WHERE NOT (NEW.requested_at_bucket GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.requested_at_bucket, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.requested_at_bucket, 1, 19)), '')
                   = substr(NEW.requested_at_bucket, 1, 19));
END;

CREATE TRIGGER trg_sga_registered_at_is_a_real_instant
BEFORE INSERT ON stage_a_acquisitions FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'registered_at is not a real instant in the canonical format')
    WHERE NOT (NEW.registered_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.registered_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.registered_at, 1, 19)), '')
                   = substr(NEW.registered_at, 1, 19));
END;

-- ==========================================================================
-- Identity columns must be TEXT. A BLOB passes GLOB and length() but is a
-- different value that no TEXT lookup will ever match -- evidence present in the
-- table and invisible to every query. `typeof()` is the only predicate that
-- distinguishes them (f021 D2).
-- ==========================================================================
CREATE TRIGGER trg_sap_identity_columns_are_text
BEFORE INSERT ON stage_a_plans FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'stage_a_plans identity columns must be TEXT')
    WHERE typeof(NEW.plan_digest) <> 'text'
       OR typeof(NEW.manifest_commit_sha) <> 'text'
       OR typeof(NEW.manifest_content_digest) <> 'text'
       OR typeof(NEW.provider) <> 'text'
       OR typeof(NEW.namespace_generation) <> 'text'
       OR typeof(NEW.official_source_corpus_digest) <> 'text'
       OR typeof(NEW.official_target_set_digest) <> 'text';
END;

CREATE TRIGGER trg_eln_identity_columns_are_text
BEFORE INSERT ON corpus_evidence_lane_bindings FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'corpus_evidence_lane_bindings identity columns must be TEXT')
    WHERE typeof(NEW.lane_evidence_digest) <> 'text'
       OR typeof(NEW.acquisition_set_digest) <> 'text'
       OR typeof(NEW.provider) <> 'text'
       OR typeof(NEW.namespace_generation) <> 'text'
       OR typeof(NEW.digest_policy_version) <> 'text';
END;

-- ==========================================================================
-- PLAN MEMBERSHIP CLOSURE.
--
-- The fetch-then-declare attack: acquire a convenient response, notice a bucket
-- is missing, then insert that bucket afterwards. Once ANY attempt exists under
-- ANY acquisition of a plan, the plan's target and bucket membership is CLOSED.
-- ==========================================================================
CREATE TRIGGER trg_sapb_membership_closed_once_attempted
BEFORE INSERT ON stage_a_planned_buckets FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'plan bucket membership is closed: an acquisition attempt already exists for this plan')
    WHERE EXISTS (
        SELECT 1 FROM stage_a_request_attempts a
        JOIN stage_a_acquisitions q ON q.acquisition_id = a.acquisition_id
        WHERE q.plan_id = NEW.plan_id);
END;

CREATE TRIGGER trg_sapt_membership_closed_once_attempted
BEFORE INSERT ON stage_a_plan_targets FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'plan target membership is closed: an acquisition attempt already exists for this plan')
    WHERE EXISTS (
        SELECT 1 FROM stage_a_request_attempts a
        JOIN stage_a_acquisitions q ON q.acquisition_id = a.acquisition_id
        WHERE q.plan_id = NEW.plan_id);
END;

-- A declared target must point at a bucket the plan actually declared, and at a
-- game in the plan's own league. A cross-league or undeclared-bucket target is
-- refused at the database.
CREATE TRIGGER trg_sapt_target_is_consistent
BEFORE INSERT ON stage_a_plan_targets FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'plan target cites a bucket that the plan did not declare')
    WHERE NOT EXISTS (
        SELECT 1 FROM stage_a_planned_buckets
        WHERE plan_id = NEW.plan_id AND requested_at_bucket = NEW.requested_at_bucket);

    SELECT RAISE(ABORT, 'plan target is a game from a different league than the plan')
    WHERE (SELECT league_id FROM games WHERE game_id = NEW.canonical_game_id)
      IS NOT (SELECT league_id FROM stage_a_plans WHERE plan_id = NEW.plan_id);
END;

-- ==========================================================================
-- ATTEMPT INTEGRITY.
-- ==========================================================================
CREATE TRIGGER trg_sat_bucket_must_be_planned
BEFORE INSERT ON stage_a_request_attempts FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'request attempt cites a bucket outside the declared plan')
    WHERE NOT EXISTS (
        SELECT 1 FROM stage_a_planned_buckets b
        JOIN stage_a_acquisitions q ON q.plan_id = b.plan_id
        WHERE q.acquisition_id = NEW.acquisition_id
          AND b.requested_at_bucket = NEW.requested_at_bucket);
END;

-- FIRST-PASS POLICY: retries are forbidden. A second attempt for a bucket is
-- refused rather than silently accepted, so the terminal classification of every
-- planned bucket is unambiguous. A future retry policy replaces this trigger
-- under its own explicit version.
CREATE TRIGGER trg_sat_first_pass_forbids_retries
BEFORE INSERT ON stage_a_request_attempts FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'Stage-A first pass forbids retries: this bucket already has an attempt')
    WHERE EXISTS (
        SELECT 1 FROM stage_a_request_attempts
        WHERE acquisition_id = NEW.acquisition_id
          AND requested_at_bucket = NEW.requested_at_bucket);
END;

-- PLAN-BEFORE-NETWORK. An ordinary attempt may not cite a response acquired
-- before its acquisition was registered. A pre-registration response is
-- admissible ONLY as `reused_probe_response`, and only when that exact response
-- carries a probe registration -- otherwise the exception would be a general
-- bypass rather than a narrow, evidenced one.
CREATE TRIGGER trg_sat_response_must_follow_registration
BEFORE INSERT ON stage_a_request_attempts FOR EACH ROW
WHEN NEW.raw_response_id IS NOT NULL AND NEW.outcome <> 'reused_probe_response'
BEGIN
    SELECT RAISE(ABORT,
        'ordinary Stage-A attempt cites a raw response acquired before the acquisition was registered')
    WHERE (SELECT requested_at FROM raw_responses WHERE raw_response_id = NEW.raw_response_id)
        < (SELECT registered_at FROM stage_a_acquisitions
           WHERE acquisition_id = NEW.acquisition_id);
END;

CREATE TRIGGER trg_sat_reused_probe_must_be_registered
BEFORE INSERT ON stage_a_request_attempts FOR EACH ROW
WHEN NEW.outcome = 'reused_probe_response'
BEGIN
    SELECT RAISE(ABORT,
        'reused_probe_response cites a raw response with no probe registration')
    WHERE NOT EXISTS (
        SELECT 1 FROM stage_a_probe_registrations
        WHERE raw_response_id = NEW.raw_response_id);
END;

-- ==========================================================================
-- LANE INTEGRITY.
--
-- A lane may only be attached to a corpus whose OFFICIAL provenance matches the
-- plan its evidence was acquired under. Without this, a lane planned from
-- official corpus C1 could be attached to an unrelated corpus Cx that merely
-- shares a league -- the most direct cross-lane mixing route (review D14).
-- ==========================================================================
CREATE TRIGGER trg_elna_member_matches_lane_and_parent
BEFORE INSERT ON corpus_evidence_lane_acquisitions FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'lane member acquisition was planned for a different provider or namespace generation')
    WHERE (SELECT p.provider || '|' || p.namespace_generation
           FROM stage_a_plans p
           JOIN stage_a_acquisitions q ON q.plan_id = p.plan_id
           WHERE q.acquisition_id = NEW.acquisition_id)
      IS NOT (SELECT provider || '|' || namespace_generation
              FROM corpus_evidence_lane_bindings
              WHERE lane_binding_id = NEW.lane_binding_id);

    SELECT RAISE(ABORT,
        'lane member acquisition has a different projection policy than the lane')
    WHERE (SELECT projection_policy_version FROM stage_a_acquisitions
           WHERE acquisition_id = NEW.acquisition_id)
      IS NOT (SELECT projection_policy_version FROM corpus_evidence_lane_bindings
              WHERE lane_binding_id = NEW.lane_binding_id);

    SELECT RAISE(ABORT,
        'lane member acquisition was planned from a different official source corpus than this lane''s parent')
    WHERE (SELECT p.official_source_corpus_digest
           FROM stage_a_plans p
           JOIN stage_a_acquisitions q ON q.plan_id = p.plan_id
           WHERE q.acquisition_id = NEW.acquisition_id)
      IS NOT (SELECT c.source_corpus_digest
              FROM reconstruction_corpus_versions c
              JOIN corpus_evidence_lane_bindings l
                ON l.corpus_version_id = c.corpus_version_id
              WHERE l.lane_binding_id = NEW.lane_binding_id);

    SELECT RAISE(ABORT,
        'lane member acquisition was planned against a different official target set than this lane''s parent')
    WHERE (SELECT p.official_target_set_digest
           FROM stage_a_plans p
           JOIN stage_a_acquisitions q ON q.plan_id = p.plan_id
           WHERE q.acquisition_id = NEW.acquisition_id)
      IS NOT (SELECT c.target_set_digest
              FROM reconstruction_corpus_versions c
              JOIN corpus_evidence_lane_bindings l
                ON l.corpus_version_id = c.corpus_version_id
              WHERE l.lane_binding_id = NEW.lane_binding_id);
END;

-- A market lane must be committed to by its parent corpus's identity: the corpus
-- has to carry this lane's digest in `market_evidence_digest`, which is an input
-- to `semantic_digest`. This is what makes "which evidence is in this corpus" a
-- property of the corpus id rather than of whatever rows happen to exist today.
CREATE TRIGGER trg_eln_parent_corpus_commits_to_the_lane
BEFORE INSERT ON corpus_evidence_lane_bindings FOR EACH ROW
WHEN NEW.evidence_lane = 'market_events_e0'
BEGIN
    SELECT RAISE(ABORT,
        'parent corpus does not commit to this E0 lane digest in market_evidence_digest; attach the lane to the enriched superseding corpus instead')
    WHERE (SELECT market_evidence_digest FROM reconstruction_corpus_versions
           WHERE corpus_version_id = NEW.corpus_version_id)
      IS NOT NEW.lane_evidence_digest;

    SELECT RAISE(ABORT, 'lane league does not match its parent corpus league')
    WHERE (SELECT league_id FROM reconstruction_corpus_versions
           WHERE corpus_version_id = NEW.corpus_version_id)
      IS NOT NEW.league_id;
END;

-- ==========================================================================
-- AUDIT LANE BINDING -- fail closed for non-official providers.
--
-- The official provider set is spelled here rather than read from a mutable
-- software registry, so that adding a linking provider to a Python frozenset can
-- never silently widen what the DATABASE accepts. Widening it requires a new
-- migration, which is a reviewable event.
-- ==========================================================================
CREATE TRIGGER trg_ida_lane_required_for_non_official_providers
BEFORE INSERT ON identity_audit_records FOR EACH ROW
WHEN NEW.lane_binding_id IS NULL
BEGIN
    SELECT RAISE(ABORT,
        'a non-official-provider identity audit must cite an evidence lane binding; the corpus-level source digest path is reserved for legacy official audits')
    WHERE NEW.provider NOT IN ('balldontlie', 'mlb_statsapi');
END;

-- When a lane IS cited, it must actually describe this audit's evidence.
CREATE TRIGGER trg_ida_lane_must_match_the_audit
BEFORE INSERT ON identity_audit_records FOR EACH ROW
WHEN NEW.lane_binding_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'identity audit cites a lane for a different provider, namespace generation or league')
    WHERE (SELECT provider || '|' || namespace_generation || '|' || league_id
           FROM corpus_evidence_lane_bindings
           WHERE lane_binding_id = NEW.lane_binding_id)
      IS NOT (NEW.provider || '|' || NEW.namespace_generation || '|' || NEW.league_id);

    SELECT RAISE(ABORT, 'identity audit digest does not equal its cited lane evidence digest')
    WHERE (SELECT lane_evidence_digest FROM corpus_evidence_lane_bindings
           WHERE lane_binding_id = NEW.lane_binding_id)
      IS NOT NEW.source_corpus_digest;
END;

-- ==========================================================================
-- CROSSWALK BINDING for a lane-backed audit.
--
-- f019's `trg_xwk_audit_corpus_binding` still governs the legacy official path
-- and is NOT modified: it compares the audit's `source_corpus_digest` to the
-- corpus's. That comparison is wrong for a lane-backed audit, whose digest is
-- the LANE's, so this trigger supplies the correct rule for that case and
-- neutralises the legacy one by requiring the lane's corpus to match.
--
-- NOTE ON COMPOSITION: for a lane-backed audit the f019 trigger would still fire
-- and refuse, because the audit's digest is a lane digest and will not equal the
-- corpus's `source_corpus_digest`. That is why the enriched corpus records the
-- lane digest in `market_evidence_digest` and NOT in `source_corpus_digest`, and
-- why a lane-backed crosswalk must cite the corpus the lane belongs to while the
-- audit continues to carry the lane digest. The two rules are therefore composed
-- by the deterministic gate, which is the only component that recomputes digests.
-- ==========================================================================
CREATE TRIGGER trg_xwk_lane_backed_audit_binding
BEFORE INSERT ON static_crosswalk_provenance FOR EACH ROW
WHEN (SELECT lane_binding_id FROM identity_audit_records
      WHERE identity_audit_id = NEW.identity_audit_id) IS NOT NULL
BEGIN
    SELECT RAISE(ABORT,
        'static crosswalk cites a lane-backed audit whose lane belongs to a different corpus version')
    WHERE (SELECT l.corpus_version_id
           FROM corpus_evidence_lane_bindings l
           JOIN identity_audit_records a ON a.lane_binding_id = l.lane_binding_id
           WHERE a.identity_audit_id = NEW.identity_audit_id)
      IS NOT NEW.corpus_version_id;

    SELECT RAISE(ABORT,
        'static crosswalk provider/namespace does not match the cited lane')
    WHERE (SELECT l.provider || '|' || l.namespace_generation
           FROM corpus_evidence_lane_bindings l
           JOIN identity_audit_records a ON a.lane_binding_id = l.lane_binding_id
           WHERE a.identity_audit_id = NEW.identity_audit_id)
      IS NOT (NEW.provider || '|' || NEW.namespace_generation);
END;

-- ==========================================================================
-- RAW-RESPONSE RECEIPT-CLOCK INTEGRITY.
--
-- b004 constrains `requested_at` and `received_at` by SHAPE only
-- (`LIKE '____-__-__T__:__:__%Z'`), with no calendar validity and no ordering.
-- v22 makes `observed_at` equal to `received_at`, so that clock becomes
-- load-bearing and its integrity must be repaired BEFORE anything relies on it.
--
-- b004 is applied evidence and is NOT edited: this is a forward trigger, so
-- existing preserved rows are untouched and remain readable. Rows written before
-- this migration that would fail these predicates keep their bytes; the Stage-A
-- gate re-checks them and fails closed if such a row is proposed for
-- certification.
--
-- This is integrity, not proof of honesty: a caller writing the raw response
-- still controls both clocks. The reviewed trust boundary is tamper-EVIDENCE.
-- ==========================================================================
CREATE TRIGGER trg_raw_responses_receipt_clock_integrity
BEFORE INSERT ON raw_responses FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'raw_responses.requested_at is not a real calendar instant')
    WHERE NOT (substr(NEW.requested_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.requested_at, 1, 19)), '')
                   = substr(NEW.requested_at, 1, 19));

    SELECT RAISE(ABORT, 'raw_responses.received_at is not a real calendar instant')
    WHERE NOT (substr(NEW.received_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.received_at, 1, 19)), '')
                   = substr(NEW.received_at, 1, 19));

    SELECT RAISE(ABORT,
        'raw_responses.received_at precedes requested_at; a response cannot arrive before it was sent')
    WHERE substr(NEW.received_at, 1, 19) < substr(NEW.requested_at, 1, 19);
END;

-- ==========================================================================
-- observed_at OWNERSHIP.
--
-- `observed_at` is when WE first possessed the preserved evidence -- never the
-- historical provider snapshot instant. A March snapshot received in August has
-- an AUGUST `observed_at`. Forcing equality with the cited response's
-- `received_at` removes a free parameter: instead of two independently writable
-- clocks there is one, tied to a row other verifiers already check.
--
-- The v20 decision that `observed_at` is EXCLUDED from the observation content
-- hash is unchanged.
-- ==========================================================================
CREATE TRIGGER trg_hme_observed_at_equals_cited_receipt
BEFORE INSERT ON historical_market_event_observations FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'observation observed_at must equal the cited raw response received_at; it records when WE possessed the evidence, never the provider snapshot instant')
    WHERE (SELECT received_at FROM raw_responses
           WHERE raw_response_id = NEW.raw_response_id)
      IS NOT NEW.observed_at;
END;
