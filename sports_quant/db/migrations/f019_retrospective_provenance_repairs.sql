-- Migration f019: repairs proven by the independent review of f018.
--
-- Why this is a separate migration
-- --------------------------------
-- `f018_retrospective_provenance.sql` is already published, applied evidence. It
-- is preserved byte-for-byte and is NOT edited here, for the same reason no
-- earlier migration has ever been edited: the migration runner records each
-- file's checksum when it applies it, so editing an applied migration makes the
-- live schema silently disagree with its own recorded history. Repairs append.
--
-- Everything here is a TRIGGER rather than a table change. SQLite cannot ADD
-- CONSTRAINT, and rebuilding the five f018 tables to bolt on CHECKs would
-- rewrite applied schema and destroy the append-only guarantee mid-migration.
-- BEFORE INSERT triggers enforce exactly the same predicates against any writer,
-- which is what the review actually required.
--
-- What the review proved
-- ----------------------
-- Five defects, each reproduced through the repository API AND through direct
-- SQL before being repaired:
--
--   D1  A static crosswalk could cite an ACCEPTED identity audit taken over a
--       DIFFERENT source corpus. A clean one-month audit could therefore vouch
--       for a five-season reconstruction -- exactly the transfer
--       `G5_PROVIDER_ID_STABILITY_REVIEW.md` §16 says must never happen.
--   D2  An ACCEPTED audit (collision_count = 0) could later acquire a BLOCKING
--       `identity_collision` finding. The audit then simultaneously meant
--       "accepted, zero collisions" and "contains a blocking collision", and
--       crosswalks already built from it survived unchallenged.
--   D3  An ELIGIBLE reconstructed input could be certified with no source
--       evidence pointer at all. A completion timestamp is not proof that the
--       source event exists, and a provider snapshot instant is evidence about
--       availability, not a pointer to the data used.
--   D4  `source_evidence_table` accepted any string -- a nonexistent table, a
--       mutable current-state table, SQL-shaped junk, or the provenance table
--       citing itself.
--   D5  Timestamp CHECKs were shape-only, so month 99, day 99, hour 77, Feb 30,
--       a lower-case `z` and an offset-bearing string were all storable. These
--       are TEXT columns compared lexicographically, so such a row would
--       mis-order against every well-formed one.
--
-- A sixth, narrower gap is also closed: cross-league supersession was refused by
-- the repository but not by the database.

-- ==========================================================================
-- D1. A crosswalk's audit must have examined the corpus's own source evidence.
--
-- f018 already required the cited audit to be accepted and to match the
-- namespace exactly. It did not require the audit's `source_corpus_digest` to
-- equal the corpus version's. Both are recorded, so the check is a join away.
-- ==========================================================================
CREATE TRIGGER trg_xwk_audit_corpus_binding
BEFORE INSERT ON static_crosswalk_provenance
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'static crosswalk cites an identity audit taken over a different source corpus; a narrower audit never transfers to a wider reconstruction')
    WHERE (SELECT source_corpus_digest FROM identity_audit_records
           WHERE identity_audit_id = NEW.identity_audit_id)
      IS NOT (SELECT source_corpus_digest FROM reconstruction_corpus_versions
              WHERE corpus_version_id = NEW.corpus_version_id);
END;

-- ==========================================================================
-- D2. An accepted audit may not acquire findings that contradict its summary.
--
-- The contract chosen is the narrowest coherent one: an ACCEPTED audit is a
-- completed statement that the namespace is clean over that corpus, so it may
-- carry flags (a `warning` / `name_variance` finding with no exclusion reach --
-- that is what `flagged_count` is for) and benign `legitimate_mutation` records,
-- but never a finding that asserts the opposite of its own verdict.
--
-- Contradiction is deliberately defined by REACH and CLASS, not by severity
-- alone: `blocking`, `identity_collision`, `namespace_unverified`, and any
-- exclusion scope other than `none` all assert something an accepted audit
-- denies.
--
-- Nothing is mutated to achieve this: the finding is refused, and the correct
-- way to record newly discovered trouble is a NEW audit over the corpus (which
-- yields a new digest, and therefore a new corpus version for anything that
-- depends on it). Accepted evidence is never rewritten.
-- ==========================================================================
CREATE TRIGGER trg_idf_accepted_audit_no_contradiction
BEFORE INSERT ON identity_audit_findings
FOR EACH ROW WHEN (
    NEW.severity = 'blocking'
    OR NEW.classification IN ('identity_collision', 'namespace_unverified')
    OR NEW.exclusion_scope <> 'none')
BEGIN
    SELECT RAISE(ABORT, 'refusing to append a contradictory finding to an ACCEPTED identity audit; record a new audit over the corpus instead of rewriting an accepted verdict')
    WHERE EXISTS (SELECT 1 FROM identity_audit_records
                  WHERE identity_audit_id = NEW.identity_audit_id
                    AND verdict = 'accepted');
END;

-- A flag still has to be counted. An accepted audit that carries a `warning`
-- finding while declaring `flagged_count = 0` is understating its own evidence.
CREATE TRIGGER trg_idf_flag_must_be_counted
BEFORE INSERT ON identity_audit_findings
FOR EACH ROW WHEN NEW.severity = 'warning'
BEGIN
    SELECT RAISE(ABORT, 'audit understates flagged_count; the summary must already account for its own warning findings')
    WHERE EXISTS (SELECT 1 FROM identity_audit_records
                  WHERE identity_audit_id = NEW.identity_audit_id
                    AND flagged_count <= (
                        SELECT COUNT(*) FROM identity_audit_findings
                        WHERE identity_audit_id = NEW.identity_audit_id
                          AND severity = 'warning'));
END;

-- ==========================================================================
-- D3. An eligible reconstructed input must point at concrete preserved evidence.
--
-- Per basis, the minimum that makes the certification reproducible:
--
--   static_identity   -> the crosswalk IS the evidence (f018 already requires
--                        it), and the crosswalk in turn cites an accepted audit
--                        over this corpus (D1). No further pointer is needed.
--   event_derived     -> a completion timestamp proves nothing on its own; the
--                        row must name the preserved observation the fact was
--                        derived from.
--   versioned_snapshot-> the provider's stamp is evidence about AVAILABILITY,
--                        not a pointer to the data used; the row must name the
--                        preserved snapshot.
--   label_only        -> a label must not become an untraceable assertion just
--                        because it is not a predictive input.
--
-- EXCLUDED rows are deliberately exempt: "this input was not admissible" is
-- frequently a statement that the evidence does not exist, and demanding a
-- pointer to absent evidence would force a fabricated one.
-- ==========================================================================
CREATE TRIGGER trg_rip_eligible_needs_evidence
BEFORE INSERT ON reconstructed_input_provenance
FOR EACH ROW WHEN NEW.eligibility = 'eligible'
BEGIN
    SELECT RAISE(ABORT, 'an eligible reconstructed input must cite preserved source evidence; a timestamp alone is not proof that the source data exists')
    WHERE NEW.source_evidence_id IS NULL
      AND NEW.availability_basis IS NOT 'static_identity';
END;

-- ==========================================================================
-- D4. Source evidence must be an append-only observation, named from a fixed
-- allowlist.
--
-- The list is exactly the append-only observation tables that record facts
-- about games, participants, conditions or markets. Deliberately excluded:
-- mutable current-state and link tables (a reconstruction may not derive a fact
-- from something that can change under it), matcher/DQ plumbing
-- (`entity_match_decisions`, `data_quality_issues` -- conclusions, not
-- observations), canonical dimensions, and the v18/v19 provenance tables
-- themselves (provenance citing provenance is a loop, not evidence).
--
-- The database enforces the NAME here; the repository additionally verifies the
-- row exists in that table, because SQLite cannot resolve a table name held in a
-- column.
-- ==========================================================================
CREATE TRIGGER trg_rip_evidence_table_allowed
BEFORE INSERT ON reconstructed_input_provenance
FOR EACH ROW WHEN NEW.source_evidence_table IS NOT NULL
BEGIN
    SELECT RAISE(ABORT,
        'source_evidence_table is not an allowed append-only observation table')
    WHERE NEW.source_evidence_table NOT IN (
        'game_result_snapshots', 'game_schedule_snapshots', 'game_status_history',
        'injury_snapshots', 'lineup_players', 'lineup_snapshots',
        'mlb_inning_lines', 'nba_game_results', 'nba_player_statistics',
        'nba_quarter_lines', 'nba_team_statistics', 'play_snapshots',
        'player_game_statistics', 'probable_pitcher_snapshots', 'raw_responses',
        'roster_snapshots', 'sportsbook_price_snapshots', 'team_game_statistics',
        'weather_snapshots');
END;

-- ==========================================================================
-- D3b / review §8. Documented availability evidence, not "trust me".
--
-- EVENT_DERIVED availability rests on a stated lag whose basis is an assumption;
-- VERSIONED_SNAPSHOT rests on the provider's documented snapshot semantics.
-- Both must name the documenting evidence when the row is admitted as eligible.
-- STATIC_IDENTITY needs none: its availability argument is the timeless-identity
-- rule, which is carried by the crosswalk and its audit.
-- ==========================================================================
CREATE TRIGGER trg_rip_eligible_needs_availability_source
BEFORE INSERT ON reconstructed_input_provenance
FOR EACH ROW WHEN NEW.eligibility = 'eligible'
BEGIN
    SELECT RAISE(ABORT, 'an eligible event-derived or versioned-snapshot input must name the evidence documenting its availability claim')
    WHERE NEW.availability_basis IN ('event_derived', 'versioned_snapshot')
      AND (NEW.availability_source IS NULL OR TRIM(NEW.availability_source) = '');
END;

-- ==========================================================================
-- D5. Timestamps must be real instants in the canonical format, not merely
-- ISO-shaped.
--
-- `GLOB` is case-sensitive and digit-exact, so it pins the format (including the
-- upper-case `Z` that `LIKE '%Z'` let through). The `strftime` round-trip then
-- rejects impossible calendar instants: SQLite NORMALIZES 2026-02-30 to
-- 2026-03-02, so requiring the round-trip to return the input unchanged refuses
-- it. `IFNULL` is essential -- a CHECK/WHERE that evaluates to NULL is not a
-- failure in SQL, which is exactly how month 99 slipped past the f018 shape
-- test. Hour 24 is legal ISO-8601 end-of-day but is never emitted by
-- `utc_now_iso`, so it is excluded to keep one canonical spelling per instant.
-- ==========================================================================
CREATE TRIGGER trg_rip_timestamps_are_real_instants
BEFORE INSERT ON reconstructed_input_provenance
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'source_event_completed_at is not a real instant in the canonical format')
    WHERE NEW.source_event_completed_at IS NOT NULL
      AND NOT (NEW.source_event_completed_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.source_event_completed_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.source_event_completed_at, 1, 19)), '')
                   = substr(NEW.source_event_completed_at, 1, 19));

    SELECT RAISE(ABORT,
        'source_snapshot_at is not a real instant in the canonical format')
    WHERE NEW.source_snapshot_at IS NOT NULL
      AND NOT (NEW.source_snapshot_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.source_snapshot_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.source_snapshot_at, 1, 19)), '')
                   = substr(NEW.source_snapshot_at, 1, 19));
END;

CREATE TRIGGER trg_xwk_curated_at_is_a_real_instant
BEFORE INSERT ON static_crosswalk_provenance
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'curated_at is not a real instant in the canonical format')
    WHERE NOT (NEW.curated_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
               AND substr(NEW.curated_at, 12, 2) <> '24'
               AND IFNULL(strftime('%Y-%m-%dT%H:%M:%S',
                          substr(NEW.curated_at, 1, 19)), '')
                   = substr(NEW.curated_at, 1, 19));
END;

-- ==========================================================================
-- D6. Cross-league supersession was refused by the repository only.
-- ==========================================================================
CREATE TRIGGER trg_rcv_supersede_same_league
BEFORE INSERT ON reconstruction_corpus_versions
FOR EACH ROW WHEN NEW.supersedes_corpus_version_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a corpus version may only supersede one for the same league')
    WHERE (SELECT league_id FROM reconstruction_corpus_versions
           WHERE corpus_version_id = NEW.supersedes_corpus_version_id)
          IS NOT NEW.league_id;
END;
