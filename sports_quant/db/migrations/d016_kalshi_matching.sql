-- Migration d016: Kalshi event/market -> canonical game link (D5B2).
--
-- Version 016: migration numbers are a single global sequence (§3.1); D5B1
-- ended at d015, so this is 016. d001-d015 are immutable and are NOT edited.
--
-- Phase C left `kalshi_events.game_id` / `kalshi_markets.game_id` NULL because
-- linking a Kalshi contract to a canonical game is a *recorded* match decision,
-- not a title guess. D5B2 makes those links, and it needs facts durably on the
-- rows that c007/c008 have nowhere to hold:
--
--   * the EXACT accepted `entity_match_decisions.match_id` from the attempt that
--     produced each link (recovering it later via a "latest decision" query
--     would be non-deterministic when several decisions share a source ref);
--   * for a supported binary game-winner market, the canonical team the Kalshi
--     **Yes** side settles on (`yes_team_id`) -- the sign of every price, which
--     must NOT live only inside an opaque JSON evidence blob;
--   * the exact `rules_hash` the accepted decision was bound to
--     (`matched_rules_hash`), so a later rules change is detectable as an
--     invalidation rather than silently reinterpreted; and
--   * the typed supported semantic (`market_semantic`), so an unsupported
--     proposition can never be read as a moneyline.
--
-- This is the smallest structure that satisfies those requirements. It adds no
-- Kalshi price/order-book/trade schema, rebuilds nothing, and adds no
-- authenticated or account-oriented field. The link columns move together and,
-- once set, never silently re-point to a different game/team/hash -- a
-- conflicting rematch goes through a new decision and the blocking DQ path,
-- exactly like the sportsbook (d015) and provider-reference crosswalks. A cross
-- table rule SQLite cannot express -- the Yes team must be one of the linked
-- game's two teams -- is verified transactionally by the repository.

-- --------------------------------------------------------------------------
-- Kalshi events: the exact accepted event match decision. `game_id` (c007) is
-- the current-state canonical link (mutable NULL -> value once).
-- --------------------------------------------------------------------------
ALTER TABLE kalshi_events
    ADD COLUMN match_decision_id TEXT REFERENCES entity_match_decisions(match_id);

-- game_id and match_decision_id are set together; once set the canonical game
-- link is immutable (a re-match to a different game is refused here and handled
-- as a blocking DQ conflict in the matcher). Re-applying the identical link is
-- allowed so a replay stays idempotent.
CREATE TRIGGER trg_kalshi_events_link_integrity
BEFORE UPDATE OF game_id, match_decision_id ON kalshi_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'kalshi event game_id and match_decision_id must be set together')
    WHERE ((NEW.game_id IS NULL) + (NEW.match_decision_id IS NULL)) NOT IN (0, 2);
    SELECT RAISE(ABORT, 'kalshi event canonical game link is immutable once set')
    WHERE OLD.game_id IS NOT NULL AND NEW.game_id IS NOT OLD.game_id;
END;

CREATE INDEX idx_kalshi_events_decision ON kalshi_events (match_decision_id);
CREATE INDEX idx_kalshi_events_unmatched ON kalshi_events (series_ticker) WHERE game_id IS NULL;

-- --------------------------------------------------------------------------
-- Kalshi markets: the accepted market decision plus its durable settlement
-- semantics. `game_id` (c007) is the current-state canonical link.
--   * match_decision_id  -- the exact accepted market decision
--   * yes_team_id        -- canonical team the Yes side settles on
--   * matched_rules_hash -- the rules_hash that decision was bound to
--   * market_semantic    -- the typed supported semantic (only 'game_winner')
-- --------------------------------------------------------------------------
ALTER TABLE kalshi_markets
    ADD COLUMN match_decision_id TEXT REFERENCES entity_match_decisions(match_id);
ALTER TABLE kalshi_markets
    ADD COLUMN yes_team_id TEXT REFERENCES teams(team_id);
ALTER TABLE kalshi_markets
    ADD COLUMN matched_rules_hash TEXT;
ALTER TABLE kalshi_markets
    ADD COLUMN market_semantic TEXT
    CHECK (market_semantic IS NULL OR market_semantic IN ('game_winner'));

-- All five semantic-link columns are set together (no partial semantic link);
-- once set, the game link, the Yes team, and the matched rules hash are each
-- immutable -- a conflicting rematch, a different Yes team, or a different
-- matched hash must go through a new decision and the blocking DQ path. Setting
-- the identical values again (an idempotent replay) is allowed.
CREATE TRIGGER trg_kalshi_markets_link_integrity
BEFORE UPDATE OF game_id, match_decision_id, yes_team_id, matched_rules_hash, market_semantic
ON kalshi_markets
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'kalshi market semantic link columns must be set together')
    WHERE ((NEW.game_id IS NULL) + (NEW.match_decision_id IS NULL) + (NEW.yes_team_id IS NULL)
           + (NEW.matched_rules_hash IS NULL) + (NEW.market_semantic IS NULL)) NOT IN (0, 5);
    SELECT RAISE(ABORT, 'kalshi market canonical game link is immutable once set')
    WHERE OLD.game_id IS NOT NULL AND NEW.game_id IS NOT OLD.game_id;
    SELECT RAISE(ABORT, 'kalshi market Yes team is immutable once set')
    WHERE OLD.yes_team_id IS NOT NULL AND NEW.yes_team_id IS NOT OLD.yes_team_id;
    SELECT RAISE(ABORT, 'kalshi market matched rules hash is immutable once set')
    WHERE OLD.matched_rules_hash IS NOT NULL
      AND NEW.matched_rules_hash IS NOT OLD.matched_rules_hash;
END;

CREATE INDEX idx_kalshi_markets_decision ON kalshi_markets (match_decision_id);
CREATE INDEX idx_kalshi_markets_unmatched ON kalshi_markets (series_ticker) WHERE game_id IS NULL;
