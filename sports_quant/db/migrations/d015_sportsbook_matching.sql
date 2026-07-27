-- Migration d015: sportsbook event -> canonical game link (D5B1).
--
-- Phase B left `sportsbook_events.game_id` NULL because linking a sportsbook
-- event to a canonical game is a *recorded* match decision, not a name guess.
-- D5B1 makes that link, and it needs two facts durably on the event that b005
-- has nowhere to hold:
--
--   * the EXACT accepted `entity_match_decisions.match_id` from the attempt that
--     produced the link (recovering it later via a "latest decision" query would
--     be non-deterministic when several decisions share a source ref); and
--   * the DIRECT vs neutral-site SWAPPED orientation, as a typed, unambiguous
--     value a pricing consumer can read without re-deriving it from a match
--     method string or an opaque JSON blob.
--
-- This is the smallest structure that satisfies those requirements. It adds no
-- Kalshi structure, does not touch the append-only price history, and rebuilds
-- nothing. `game_id` remains the current-state link (mutable NULL -> value once);
-- the three link columns move together and never silently re-point to a
-- different game -- a conflicting rematch must go through a new decision and the
-- blocking DQ path, exactly like the provider-reference crosswalks.

ALTER TABLE sportsbook_events
    ADD COLUMN match_decision_id TEXT REFERENCES entity_match_decisions(match_id);

-- Typed orientation of the accepted link. NULL until linked. 'direct' means the
-- provider's home/away agree with the canonical game; 'swapped' is a neutral-site
-- team-swapped match, which stays review-gated (see the decision's
-- needs_manual_review) and is never treated as orientation-approved pricing data.
ALTER TABLE sportsbook_events
    ADD COLUMN orientation TEXT
    CHECK (orientation IS NULL OR orientation IN ('direct', 'swapped'));

-- The three link columns are set together and, once set, the canonical game link
-- is immutable (a re-match to a different game is refused here and handled as a
-- blocking DQ conflict in the matcher). Re-applying the identical link is allowed
-- so a replay stays idempotent.
CREATE TRIGGER trg_sportsbook_events_link_integrity
BEFORE UPDATE OF game_id, match_decision_id, orientation ON sportsbook_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'sportsbook event game_id, match_decision_id and orientation must be set together')
    WHERE ((NEW.game_id IS NULL) + (NEW.match_decision_id IS NULL) + (NEW.orientation IS NULL))
          NOT IN (0, 3);
    SELECT RAISE(ABORT, 'sportsbook event canonical game link is immutable once set')
    WHERE OLD.game_id IS NOT NULL AND NEW.game_id IS NOT OLD.game_id;
END;

-- Bounded as-of / linkage lookups by the exact decision.
CREATE INDEX idx_sb_events_decision ON sportsbook_events (match_decision_id);
