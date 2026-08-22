-- Migration f023: corpus target-population binding.
--
-- Authority
-- ---------
-- `CORPUS_TARGET_POPULATION_BINDING_ARCHITECTURE.md` as reconciled at fd9ce6e,
-- and `CORPUS_TARGET_POPULATION_BINDING_ARCHITECTURE_INDEPENDENT_REVIEW.md`,
-- which is authoritative wherever the two disagree.
--
-- THE PROBLEM THIS CLOSES
-- -----------------------
-- At v22 `reconstruction_corpus_versions.target_set_digest` is a free-text
-- caller-supplied label with no production derivation (the retrospective runner
-- defaults it to the literal string 'identity-audit-no-targets'), and NO relation
-- in the whole schema enumerates corpus -> canonical games. So "which games is
-- this corpus about?" is answerable only by querying the `games` table, which is
-- a property of the DATABASE rather than of the CORPUS: two faithful copies of
-- one content-addressed corpus were shown to yield 1 and 6 members for the same
-- scope query. A target population that is not portable is not evidence.
--
-- WHY THREE TABLES AND NOT TWO
-- ----------------------------
-- The architecture originally proposed membership + run bindings, closed by the
-- rule "no member may be inserted for a corpus that already exists". The
-- independent review PROVED that rule unimplementable: an ordinary foreign key
-- requires the parent corpus to exist before a child can be inserted, at which
-- point a literal "corpus already exists" trigger fires and membership becomes
-- uninsertable in every case. SQLite triggers have no predicate for "this parent
-- was created earlier in my savepoint".
--
-- The replacement is CONSTRUCT-THEN-SEAL. Membership and run bindings are
-- insertable only while the corpus is UNSEALED; the seal row is inserted last in
-- the same transaction and closes both permanently. An unsealed corpus is open by
-- construction, so absence of a seal is a HARD verifier failure, never a warning.
--
-- WHY THE SEAL CARRIES THE MANIFEST BINDING
-- -----------------------------------------
-- Binding run ids alone proves only "these targets came from these runs" -- never
-- "these are ALL the runs the acquisition required". A caller who binds R1+R2 and
-- omits R3 produces membership that is internally perfect, moving denominator
-- shrinkage one layer earlier. The seal therefore commits the PRECOMMITTED
-- acquisition manifest hash and plan version, from which the required run set is
-- derived; the bound run rows are the RESOLUTION of that manifest, not the claim.
--
-- WHAT THIS MIGRATION DOES NOT DO
-- -------------------------------
-- Additive only. No existing table, column, index, trigger or row is modified.
-- It creates no corpus, no membership, no target-bound March corpus. It registers
-- no linking provider, widens no attested generation, creates no crosswalk and no
-- canonical game. It declares no Stage-A plan and acquires nothing.
--
-- APPEND-ONLY HARDENING
-- ---------------------
-- Every table below uses the f021 pattern from day one: BEFORE UPDATE and BEFORE
-- DELETE guards PLUS a content-aware BEFORE INSERT guard. The BEFORE INSERT guard
-- is what actually stops `REPLACE` / `INSERT OR REPLACE`, because SQLite's REPLACE
-- conflict resolution performs an implicit DELETE that does not fire DELETE
-- triggers unless `PRAGMA recursive_triggers` is ON -- and a pragma is
-- per-connection, so the guarantee cannot live there.

-- ==========================================================================
-- 1. reconstruction_corpus_targets -- the exact member set.
--
-- Undetectable without it: a target omitted from the corpus. At v22 nothing
-- distinguishes "this corpus is about 239 games" from "about 238", so a dropped
-- target leaves no trace anywhere.
--
-- `game_id` is the canonical surrogate, never a provider id. Note honestly that
-- `games.game_id` is a random ULID, so the content address is over THIS
-- database's surrogates: a byte-copy is portable, a rebuild from identical raw
-- evidence is not. Resolution from provider evidence happens in the projection
-- policy, which refuses -- never drops -- an unresolved provider game.
-- ==========================================================================
CREATE TABLE reconstruction_corpus_targets (
    corpus_version_id TEXT NOT NULL
        REFERENCES reconstruction_corpus_versions(corpus_version_id),
    game_id           TEXT NOT NULL REFERENCES games(game_id),
    created_at        TEXT NOT NULL,
    PRIMARY KEY (corpus_version_id, game_id),
    CONSTRAINT rct_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
) WITHOUT ROWID;

CREATE INDEX idx_rct_game ON reconstruction_corpus_targets(game_id);

-- Membership may only be written while the corpus is UNSEALED. This is the
-- closure mechanism that replaces the unimplementable "corpus already exists"
-- rule: it discriminates on an explicit finalization row, which a trigger CAN
-- observe, rather than on parent existence, which it cannot use.
CREATE TRIGGER trg_rct_closed_after_seal
BEFORE INSERT ON reconstruction_corpus_targets
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_targets: membership is sealed')
    WHERE EXISTS (SELECT 1 FROM reconstruction_corpus_target_seals
                  WHERE corpus_version_id = NEW.corpus_version_id);
END;

-- A target must belong to the corpus's own league. Without this a caller could
-- pad an NBA corpus with MLB games and the member digest would still verify.
CREATE TRIGGER trg_rct_league_matches
BEFORE INSERT ON reconstruction_corpus_targets
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_targets: game league differs from corpus league')
    WHERE (SELECT league_id FROM games WHERE game_id = NEW.game_id)
       IS NOT (SELECT league_id FROM reconstruction_corpus_versions
               WHERE corpus_version_id = NEW.corpus_version_id);
END;

CREATE TRIGGER trg_rct_no_update BEFORE UPDATE ON reconstruction_corpus_targets
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_targets is append-only');
END;

CREATE TRIGGER trg_rct_no_delete BEFORE DELETE ON reconstruction_corpus_targets
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_targets is append-only');
END;

-- Content-aware: stops REPLACE / INSERT OR REPLACE / upsert-as-rewrite. An
-- identical INSERT OR IGNORE stays a no-op because the PK already rejects it
-- without reaching a RAISE.
CREATE TRIGGER trg_rct_no_replace
BEFORE INSERT ON reconstruction_corpus_targets
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_targets is append-only (REPLACE)')
    WHERE EXISTS (SELECT 1 FROM reconstruction_corpus_targets
                  WHERE corpus_version_id = NEW.corpus_version_id
                    AND game_id = NEW.game_id);
END;

-- ==========================================================================
-- 2. reconstruction_corpus_target_runs -- the acquisition runs membership
--    derives from.
--
-- Undetectable without it: membership asserted but not re-derivable, so a forged
-- member set cannot be caught by recomputation. The run set also scopes the
-- listing evidence, so adding unrelated responses to the database cannot change
-- what the bound acquisition returned.
-- ==========================================================================
CREATE TABLE reconstruction_corpus_target_runs (
    corpus_version_id TEXT NOT NULL
        REFERENCES reconstruction_corpus_versions(corpus_version_id),
    run_id            TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    created_at        TEXT NOT NULL,
    PRIMARY KEY (corpus_version_id, run_id),
    CONSTRAINT rctr_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
) WITHOUT ROWID;

CREATE INDEX idx_rctr_run ON reconstruction_corpus_target_runs(run_id);

-- Run bindings close with the same seal. Without this a later binding could
-- silently re-explain an existing corpus's provenance without changing its
-- identity -- the review's proved content-addressing violation.
CREATE TRIGGER trg_rctr_closed_after_seal
BEFORE INSERT ON reconstruction_corpus_target_runs
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_target_runs: run bindings are sealed')
    WHERE EXISTS (SELECT 1 FROM reconstruction_corpus_target_seals
                  WHERE corpus_version_id = NEW.corpus_version_id);
END;

CREATE TRIGGER trg_rctr_no_update BEFORE UPDATE ON reconstruction_corpus_target_runs
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_target_runs is append-only');
END;

CREATE TRIGGER trg_rctr_no_delete BEFORE DELETE ON reconstruction_corpus_target_runs
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_target_runs is append-only');
END;

CREATE TRIGGER trg_rctr_no_replace
BEFORE INSERT ON reconstruction_corpus_target_runs
BEGIN
    SELECT RAISE(ABORT, 'reconstruction_corpus_target_runs is append-only (REPLACE)')
    WHERE EXISTS (SELECT 1 FROM reconstruction_corpus_target_runs
                  WHERE corpus_version_id = NEW.corpus_version_id
                    AND run_id = NEW.run_id);
END;

-- ==========================================================================
-- 3. reconstruction_corpus_target_seals -- irreversible finalization.
--
-- Undetectable without it: membership or run bindings extended after creation;
-- which digest policy produced an opaque 64-hex value; and a vacuous empty
-- corpus that would verify trivially.
--
-- Field-by-field, each answering "what failure is undetectable without this?":
--
--   target_set_policy_version
--       A verifier cannot infer a hashing policy from a 64-hex digest, and
--       "try every known policy until one matches" is not a contract.
--   listing_projection_policy_version
--       The raw-listing -> canonical-member mapping is a separate frozen policy
--       from the digest; a change to either must be independently visible.
--   acquisition_completeness_policy_version
--       How "the required run set" and "no cap bound" are decided. Without it a
--       future relaxation of the completeness rule is invisible in old rows.
--   acquisition_manifest_hash + plan_version
--       Without these the bound run set is caller-selected and a required run
--       can be omitted undetectably (the review's first primary attack).
--   member_count
--       Asserted against actual membership at seal time, so a partially written
--       membership cannot be finalized; CHECK > 0 makes an empty target set
--       unsealable while leaving the digest function's generic semantics honest.
--
-- Deliberately NOT stored: S_final (membership and hint evidence are different
-- claims; duplicating it would create a second truth that can disagree), any
-- per-target hint column, and any scope predicate (disproved as non-portable).
-- ==========================================================================
CREATE TABLE reconstruction_corpus_target_seals (
    corpus_version_id                       TEXT PRIMARY KEY
        REFERENCES reconstruction_corpus_versions(corpus_version_id),
    target_set_policy_version               TEXT NOT NULL,
    listing_projection_policy_version       TEXT NOT NULL,
    acquisition_completeness_policy_version TEXT NOT NULL,
    acquisition_manifest_hash               TEXT NOT NULL,
    plan_version                            TEXT NOT NULL,
    member_count                            INTEGER NOT NULL,
    created_at                              TEXT NOT NULL,
    CONSTRAINT rcts_member_count_positive CHECK (member_count > 0),
    CONSTRAINT rcts_target_policy_present
        CHECK (TRIM(target_set_policy_version) <> ''),
    CONSTRAINT rcts_projection_policy_present
        CHECK (TRIM(listing_projection_policy_version) <> ''),
    CONSTRAINT rcts_completeness_policy_present
        CHECK (TRIM(acquisition_completeness_policy_version) <> ''),
    -- A manifest hash is a sha256 hex digest, never a path or a label.
    CONSTRAINT rcts_manifest_hash_hex
        CHECK (LENGTH(acquisition_manifest_hash) = 64
               AND acquisition_manifest_hash NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT rcts_plan_version_present CHECK (TRIM(plan_version) <> ''),
    CONSTRAINT rcts_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z')
) WITHOUT ROWID;

-- The seal asserts the exact membership actually present. A seal that disagrees
-- with its own membership is not evidence, so it fails closed rather than being
-- silently corrected.
CREATE TRIGGER trg_rcts_member_count_matches
BEFORE INSERT ON reconstruction_corpus_target_seals
BEGIN
    SELECT RAISE(ABORT, 'seal member_count disagrees with stored membership')
    WHERE (SELECT COUNT(*) FROM reconstruction_corpus_targets
           WHERE corpus_version_id = NEW.corpus_version_id) <> NEW.member_count;
END;

-- A seal with no run bindings would finalize a corpus whose membership is not
-- re-derivable from anything.
CREATE TRIGGER trg_rcts_requires_run_bindings
BEFORE INSERT ON reconstruction_corpus_target_seals
BEGIN
    SELECT RAISE(ABORT, 'seal requires at least one bound acquisition run')
    WHERE NOT EXISTS (SELECT 1 FROM reconstruction_corpus_target_runs
                      WHERE corpus_version_id = NEW.corpus_version_id);
END;

CREATE TRIGGER trg_rcts_no_update BEFORE UPDATE ON reconstruction_corpus_target_seals
BEGIN
    SELECT RAISE(ABORT, 'a target seal is immutable');
END;

CREATE TRIGGER trg_rcts_no_delete BEFORE DELETE ON reconstruction_corpus_target_seals
BEGIN
    SELECT RAISE(ABORT, 'a target seal is immutable');
END;

CREATE TRIGGER trg_rcts_no_replace
BEFORE INSERT ON reconstruction_corpus_target_seals
BEGIN
    SELECT RAISE(ABORT, 'a target seal is immutable (REPLACE)')
    WHERE EXISTS (SELECT 1 FROM reconstruction_corpus_target_seals
                  WHERE corpus_version_id = NEW.corpus_version_id);
END;
