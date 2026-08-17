-- Migration f021: repairs proven by the independent review of f020.
--
-- Why this is a separate migration
-- --------------------------------
-- `f020_historical_market_event_observations.sql` is already published, applied
-- evidence. It is preserved byte-for-byte and is NOT edited here, for the same
-- reason no earlier migration has ever been edited: the runner records each
-- file's checksum when it applies it, so editing an applied migration makes the
-- live schema silently disagree with its own recorded history. Repairs append.
--
-- Everything here is a TRIGGER. SQLite cannot ADD CONSTRAINT, and rebuilding an
-- append-only table to bolt on a CHECK would rewrite applied schema and destroy
-- the very guarantee being repaired. BEFORE INSERT triggers enforce the same
-- predicates against any writer, which is what the review required.
--
-- ==========================================================================
-- D1. `REPLACE` silently mutated append-only rows.
--
-- Reproduced against v20: with `home_team_raw = 'H'` already stored,
--
--     REPLACE INTO historical_market_event_observations (...) VALUES ('hme_real', ... 'X' ...)
--
-- succeeded and the row read back as 'X'. `INSERT OR REPLACE` did the same.
--
-- The cause is that f020's guards are `BEFORE UPDATE` and `BEFORE DELETE`, and
-- SQLite's REPLACE conflict resolution performs an implicit DELETE that does
-- NOT fire DELETE triggers unless `PRAGMA recursive_triggers` is ON. A pragma is
-- per-connection, so an attacker simply does not set it: the guarantee cannot
-- live there. A BEFORE INSERT existence check fires before conflict resolution
-- and therefore holds against any connection.
--
-- SCOPE. This defect is repository-wide: 32 of 33 append-only tables have only
-- UPDATE/DELETE guards (`reconstructed_input_provenance` is the sole exception,
-- which already had a BEFORE INSERT trigger for other reasons). This migration
-- repairs the five tables the v20 provenance chain actually runs through --
-- the observation table, the raw response it cites, and the three Lane-R
-- provenance tables a fabricated observation would have to pass through to
-- become canonical identity. The remaining 28 are a pre-existing defect of the
-- same class, documented in the review for a dedicated task; sweeping them all
-- up inside a review would be a large unreviewed change.
--
-- No production code anywhere uses `REPLACE INTO` or `INSERT OR REPLACE`
-- (verified), so these guards break no existing writer.
--
-- WHY THE GUARD COMPARES CONTENT RATHER THAN JUST EXISTENCE
-- --------------------------------------------------------
-- A bare `WHEN EXISTS (same primary key)` guard is too blunt, and the test
-- suite proved it: `INSERT OR IGNORE` is a legitimate, widely used idempotent
-- re-insert, and `RAISE(ABORT)` is NOT suppressed by `OR IGNORE` (only
-- `RAISE(IGNORE)` is). Such a guard turns a harmless no-op into a hard error.
--
-- So each guard fires only when a row with that key exists AND the incoming row
-- carries DIFFERENT content. The three cases then behave correctly:
--
--   * identical re-insert  -> trigger silent; the PK conflict is handled by the
--                             caller's chosen mode (`OR IGNORE` skips, a plain
--                             INSERT still errors). Unchanged behaviour.
--   * REPLACE with changed content -> ABORT. The mutation is refused, which is
--                             the whole point of this migration.
--   * `OR IGNORE` with changed content -> ABORT. Correct: that is an attempted
--                             mutation wearing an idempotent-looking spelling,
--                             and silently discarding it would hide a real
--                             evidence conflict.
--
-- Content is compared through each table's existing digest column, which is
-- what already defines "the same row" everywhere else in this schema.
-- ==========================================================================
CREATE TRIGGER trg_hme_no_replace
BEFORE INSERT ON historical_market_event_observations
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM historical_market_event_observations
    WHERE observation_id = NEW.observation_id
      AND observation_content_hash IS NOT NEW.observation_content_hash)
BEGIN
    SELECT RAISE(ABORT,
        'historical_market_event_observations is append-only; refusing to replace an observation with different content');
END;

CREATE TRIGGER trg_raw_responses_no_replace
BEFORE INSERT ON raw_responses
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM raw_responses
    WHERE raw_response_id = NEW.raw_response_id
      AND (body_hash IS NOT NEW.body_hash
           OR content_hash IS NOT NEW.content_hash
           OR body IS NOT NEW.body))
BEGIN
    SELECT RAISE(ABORT,
        'raw_responses is append-only; refusing to replace a preserved payload with different content');
END;

CREATE TRIGGER trg_ida_no_replace
BEFORE INSERT ON identity_audit_records
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM identity_audit_records
    WHERE identity_audit_id = NEW.identity_audit_id
      AND semantic_digest IS NOT NEW.semantic_digest)
BEGIN
    SELECT RAISE(ABORT,
        'identity_audit_records is append-only; refusing to replace a row with different content');
END;

CREATE TRIGGER trg_xwk_no_replace
BEFORE INSERT ON static_crosswalk_provenance
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM static_crosswalk_provenance
    WHERE crosswalk_id = NEW.crosswalk_id
      AND semantic_digest IS NOT NEW.semantic_digest)
BEGIN
    SELECT RAISE(ABORT,
        'static_crosswalk_provenance is append-only; refusing to replace a row with different content');
END;

CREATE TRIGGER trg_rcv_no_replace
BEFORE INSERT ON reconstruction_corpus_versions
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM reconstruction_corpus_versions
    WHERE corpus_version_id = NEW.corpus_version_id
      AND semantic_digest IS NOT NEW.semantic_digest)
BEGIN
    SELECT RAISE(ABORT,
        'reconstruction_corpus_versions is append-only; refusing to replace a row with different content');
END;

-- ==========================================================================
-- D2. A BLOB bypassed the exact event-id format contract.
--
-- Reproduced against v20: inserting the BLOB b'be25eb82b82629d959c1e5ccb8dcc1e7'
-- was ACCEPTED and stored with `typeof = 'blob'`. f020's CHECK is
--
--     provider_event_id GLOB '<32 hex classes>' AND length(provider_event_id) = 32
--
-- and both halves pass for that blob: GLOB coerces for comparison, and
-- `length()` on a blob returns its byte count. SQLite also exempts BLOBs from
-- TEXT-affinity conversion, so the value stays a blob.
--
-- That matters beyond tidiness. The reviewed contract is EXACT lowercase ASCII
-- hex with byte-for-byte storage, and the resolver's whole guarantee is exact
-- key equality. A blob row does not compare equal to the corresponding TEXT
-- parameter, so it is invisible to `for_event` -- evidence that is present in
-- the table and absent from every lookup, which is the worst of both.
--
-- `typeof()` is the only predicate that distinguishes them.
-- ==========================================================================
CREATE TRIGGER trg_hme_event_id_is_text
BEFORE INSERT ON historical_market_event_observations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'provider_event_id must be TEXT; a BLOB passes GLOB and length() but is not the provider id')
    WHERE typeof(NEW.provider_event_id) <> 'text';

    -- The same coercion trap applies to the other columns whose exact bytes are
    -- part of the observation's semantic identity: a BLOB here would digest and
    -- compare differently from the TEXT the provider actually sent.
    SELECT RAISE(ABORT,
        'observation identity columns must be TEXT')
    WHERE typeof(NEW.provider) <> 'text'
       OR typeof(NEW.namespace_generation) <> 'text'
       OR typeof(NEW.sport_key) <> 'text'
       OR typeof(NEW.home_team_raw) <> 'text'
       OR typeof(NEW.away_team_raw) <> 'text'
       OR typeof(NEW.observation_content_hash) <> 'text'
       OR typeof(NEW.requested_at_bucket) <> 'text'
       OR typeof(NEW.provider_snapshot_timestamp) <> 'text'
       OR (NEW.commence_time IS NOT NULL AND typeof(NEW.commence_time) <> 'text');
END;
