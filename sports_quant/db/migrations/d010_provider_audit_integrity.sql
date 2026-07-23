-- Migration d010: Phase D1 integrity repair.
--
-- Version 010: migration numbers are a single global sequence (§3.1). d009 is
-- immutable and is NOT edited; these corrections land additively here. Later
-- planned Phase D migrations therefore begin at d011.
--
-- Three integrity fixes:
--   1. provider_capabilities gains evidence columns so a *declared* capability
--      is never persisted as an *externally observed* one, and every observed
--      state names the exact probe/endpoint/status/raw-response that verified it.
--      The table is already fully append-only (d009 triggers), so the new
--      columns are immutable by extension and audit history is preserved.
--   2. Provider venue identity: the same non-empty (provider, provider_venue_id)
--      may not map to two canonical venues (a partial unique index), so a
--      provider id can never silently point at an arbitrary venue.
--   3. data_quality_issues core/evidence fields become immutable; only the
--      resolution fields (resolved_at, resolution_note) may change.

-- ==========================================================================
-- 1. Evidence-backed capability observations.
--
-- declared_state  : what documentation/config expects (declared-only metadata).
-- observed_state  : what a probe actually verified at verified_at (NULL if not
--                   externally probed).
-- is_observed     : 1 only when an exact probe verified this capability; 0 for
--                   declared-only rows. A static declaration is thus never
--                   persisted as an observation.
-- probe_name/endpoint/http_status/error_kind/verified_at : the evidence trail.
-- ADD COLUMN with a column CHECK is permitted; the append-only triggers on this
-- table do not fire for ALTER.
-- ==========================================================================
ALTER TABLE provider_capabilities
    ADD COLUMN declared_state TEXT;
ALTER TABLE provider_capabilities
    ADD COLUMN observed_state TEXT;
ALTER TABLE provider_capabilities
    ADD COLUMN is_observed INTEGER NOT NULL DEFAULT 0 CHECK (is_observed IN (0, 1));
ALTER TABLE provider_capabilities
    ADD COLUMN probe_name TEXT;
ALTER TABLE provider_capabilities
    ADD COLUMN endpoint TEXT;
ALTER TABLE provider_capabilities
    ADD COLUMN http_status INTEGER;
ALTER TABLE provider_capabilities
    ADD COLUMN error_kind TEXT;
ALTER TABLE provider_capabilities
    ADD COLUMN verified_at TEXT;

-- Point-in-time index that includes the observed flag, so "what was externally
-- observed as of T?" is a cheap scan distinct from declared-only beliefs.
CREATE INDEX idx_provider_capabilities_observed_flag
    ON provider_capabilities (provider, capability, is_observed, observed_at);

-- ==========================================================================
-- 2. Provider venue identity. A given provider's venue id maps to at most one
-- canonical venue. Partial (non-empty id + non-empty provider) so the many
-- alias rows without a provider id are unaffected.
-- ==========================================================================
CREATE UNIQUE INDEX idx_venue_aliases_provider_id
    ON venue_aliases (provider, provider_venue_id)
    WHERE provider_venue_id IS NOT NULL AND provider <> '';

-- ==========================================================================
-- 3. data_quality_issues immutability. Only resolved_at / resolution_note may
-- change; every identity/evidence/provenance field is frozen. Also non-deletable
-- -- an issue is resolved, never erased.
-- ==========================================================================
CREATE TRIGGER trg_data_quality_issues_resolution_only_update
BEFORE UPDATE ON data_quality_issues
FOR EACH ROW
WHEN (
    NEW.issue_id <> OLD.issue_id
    OR NEW.severity <> OLD.severity
    OR NEW.rule_code <> OLD.rule_code
    OR NEW.entity_type <> OLD.entity_type
    OR NEW.entity_id IS NOT OLD.entity_id
    OR NEW.provider IS NOT OLD.provider
    OR NEW.description <> OLD.description
    OR NEW.detail_json IS NOT OLD.detail_json
    OR NEW.run_id IS NOT OLD.run_id
    OR NEW.raw_response_id IS NOT OLD.raw_response_id
    OR NEW.detected_at <> OLD.detected_at
    OR NEW.created_at <> OLD.created_at
)
BEGIN
    SELECT RAISE(ABORT,
        'data_quality_issues core fields are immutable; only resolution fields may change');
END;

CREATE TRIGGER trg_data_quality_issues_no_delete
BEFORE DELETE ON data_quality_issues
BEGIN
    SELECT RAISE(ABORT, 'data_quality_issues records are not deletable (resolve, do not erase)');
END;
