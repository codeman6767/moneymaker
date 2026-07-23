-- Migration c008: explicit first-vs-current provenance for the mutable Kalshi
-- current-state tables, and an explicit records_updated ingestion counter.
--
-- Version 008: migration numbers are a single global sequence (§3.1); Phase C
-- ended at 007, so this is 008. c007 is immutable and is NOT edited.
--
-- The problem. `kalshi_events` and `kalshi_markets` are mutable current-state
-- tables: a strictly-newer observation refreshes their title/status/timing/rules
-- columns. But their `raw_response_id` was frozen by the identity trigger, so
-- after an update the current metadata pointed at the response that ORIGINALLY
-- created the row, not the one that supplied the current values. The current
-- metadata was therefore untraceable to its real source.
--
-- The fix. Split the single ambiguous `raw_response_id` into an explicit model:
--   * first_raw_response_id   -- the response that CREATED the entity (immutable)
--   * current_raw_response_id -- the response that supplied the CURRENT metadata
--   * current_raw_response_hash -- content hash of that current response
-- The current pointers move only when a strictly-newer observation becomes
-- current (enforced in the repository); a stale or equal-time backfill leaves
-- them untouched, matching the existing deterministic stale-backfill rule.
--
-- Table-rebuild strategy. `kalshi_events` is referenced by
-- `kalshi_markets.kalshi_event_id`, and `kalshi_markets` by the order-book /
-- trade tables. A DROP+RENAME rebuild would trip a foreign-key violation on the
-- populated live corpus (DROP TABLE implicitly deletes rows a child still
-- references), and `PRAGMA foreign_keys` cannot be toggled inside the migration
-- transaction. So the columns are added and the ambiguous one dropped in place
-- with ALTER TABLE (SQLite >= 3.35) -- which preserves every row, every inbound
-- foreign key, every other index, and every CHECK.
--
-- Part 3 adds `ingestion_runs.records_updated` so a metadata refresh is counted
-- distinctly from a new insert.

-- --------------------------------------------------------------------------
-- Part 1: kalshi_events provenance.
-- --------------------------------------------------------------------------
-- Drop the identity trigger first: it references raw_response_id, and a column
-- cannot be dropped while a trigger references it.
DROP TRIGGER trg_kalshi_events_identity_immutable;

-- New columns are nullable with an FK-to-raw_responses (ADD COLUMN with a
-- REFERENCES clause requires a NULL default while foreign keys are enabled).
ALTER TABLE kalshi_events
    ADD COLUMN first_raw_response_id TEXT REFERENCES raw_responses(raw_response_id);
ALTER TABLE kalshi_events
    ADD COLUMN current_raw_response_id TEXT REFERENCES raw_responses(raw_response_id);
ALTER TABLE kalshi_events
    ADD COLUMN current_raw_response_hash TEXT;

-- Backfill: every existing row's original response becomes both first and
-- current; the current hash is joined from raw_responses.
UPDATE kalshi_events
SET first_raw_response_id = raw_response_id,
    current_raw_response_id = raw_response_id,
    current_raw_response_hash = (
        SELECT r.content_hash FROM raw_responses r
        WHERE r.raw_response_id = kalshi_events.raw_response_id
    );

-- Retire the ambiguous column. Its index must be dropped explicitly first --
-- SQLite refuses DROP COLUMN while an index still references the column.
DROP INDEX idx_kalshi_events_raw;
ALTER TABLE kalshi_events DROP COLUMN raw_response_id;

CREATE INDEX idx_kalshi_events_first_raw ON kalshi_events (first_raw_response_id);
CREATE INDEX idx_kalshi_events_current_raw ON kalshi_events (current_raw_response_id);

-- Identity trigger now freezes first_raw_response_id (the creating response),
-- while current_raw_response_id / current_raw_response_hash remain mutable.
CREATE TRIGGER trg_kalshi_events_identity_immutable
BEFORE UPDATE OF kalshi_event_id, event_ticker, first_observed_at, first_raw_response_id
ON kalshi_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'kalshi_events identity columns are immutable')
    WHERE NEW.kalshi_event_id <> OLD.kalshi_event_id
       OR NEW.event_ticker <> OLD.event_ticker
       OR NEW.first_observed_at <> OLD.first_observed_at
       OR NEW.first_raw_response_id <> OLD.first_raw_response_id;
END;

-- --------------------------------------------------------------------------
-- Part 2: kalshi_markets provenance (identical model).
-- --------------------------------------------------------------------------
DROP TRIGGER trg_kalshi_markets_identity_immutable;

ALTER TABLE kalshi_markets
    ADD COLUMN first_raw_response_id TEXT REFERENCES raw_responses(raw_response_id);
ALTER TABLE kalshi_markets
    ADD COLUMN current_raw_response_id TEXT REFERENCES raw_responses(raw_response_id);
ALTER TABLE kalshi_markets
    ADD COLUMN current_raw_response_hash TEXT;

UPDATE kalshi_markets
SET first_raw_response_id = raw_response_id,
    current_raw_response_id = raw_response_id,
    current_raw_response_hash = (
        SELECT r.content_hash FROM raw_responses r
        WHERE r.raw_response_id = kalshi_markets.raw_response_id
    );

DROP INDEX idx_kalshi_markets_raw;
ALTER TABLE kalshi_markets DROP COLUMN raw_response_id;

CREATE INDEX idx_kalshi_markets_first_raw ON kalshi_markets (first_raw_response_id);
CREATE INDEX idx_kalshi_markets_current_raw ON kalshi_markets (current_raw_response_id);

CREATE TRIGGER trg_kalshi_markets_identity_immutable
BEFORE UPDATE OF kalshi_market_id, market_ticker, first_observed_at, first_raw_response_id
ON kalshi_markets
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'kalshi_markets identity columns are immutable')
    WHERE NEW.kalshi_market_id <> OLD.kalshi_market_id
       OR NEW.market_ticker <> OLD.market_ticker
       OR NEW.first_observed_at <> OLD.first_observed_at
       OR NEW.first_raw_response_id <> OLD.first_raw_response_id;
END;

-- --------------------------------------------------------------------------
-- Part 3: an explicit records_updated ingestion counter.
--
-- The ingestor previously counted every valid event/market as records_inserted
-- even when the row already existed. records_updated now counts a metadata
-- refresh (a strictly-newer observation of an existing entity) distinctly from
-- a genuine insert. Existing rows default to 0.
-- --------------------------------------------------------------------------
ALTER TABLE ingestion_runs
    ADD COLUMN records_updated INTEGER NOT NULL DEFAULT 0 CHECK (records_updated >= 0);
