"""Phase E1 focused-repair tests (tasks §2-§9, §12).

Covers the games current-state contract, sportsbook market classification,
rebuild-deterministic equal-timestamp handling, review-gated identity, true
SQLite read-only mode, alias isolation, full registry/schema coverage, and the
narrowed generic-SQL surface.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.pit import (
    AsOfAmbiguityError,
    AsOfReader,
    Cutoff,
    ForbiddenColumnError,
    ForbiddenJoinError,
    UnknownTableError,
    assert_column_readable,
    assert_joinable,
    assert_selectable,
    classify,
    latest_as_of,
    read_only_connection,
    registered_tables,
)

from .conftest import (
    CUTOFF,
    T1,
    T2,
    Ctx,
    link_sb_event,
    seed_sb_outcome_ctx,
    seed_status,
)

_FAR = "2999-01-01T00:00:00.000000Z"


def _reader(conn: sqlite3.Connection, at: str = CUTOFF) -> AsOfReader:
    return AsOfReader(conn, Cutoff.parse(at))


def _sb_event_id(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT sb_event_id FROM sportsbook_events LIMIT 1").fetchone()[0])


# --------------------------------------------------------------------------- #
# §2 games current-state contract
# --------------------------------------------------------------------------- #
def test_games_status_and_scheduled_start_direct_reads_rejected() -> None:
    for col in ("status", "scheduled_start", "updated_at"):
        with pytest.raises(ForbiddenColumnError):
            assert_column_readable("games", col)
    with pytest.raises(ForbiddenJoinError):
        assert_selectable("games", ["*"])  # star bypass blocked (mixed table)
    assert_column_readable("games", "original_start")  # immutable, fine
    assert_column_readable("games", "home_team_id")


def test_schedule_state_status_and_start_same_asof_row(conn: sqlite3.Connection,
                                                       ctx: Ctx) -> None:
    # Original schedule at T1; a reschedule (new scheduled_start) observed at T2.
    seed_status(conn, game_id=ctx.game_id, status="scheduled", observed_at=T1)
    conn_start_2 = "2026-08-01T05:10:00Z"
    with transaction(conn):
        from sports_quant.db.repositories.games import SqliteGameRepository
        SqliteGameRepository(conn).record_status(
            game_id=ctx.game_id, status="postponed", scheduled_start=conn_start_2,
            provider="mlb_statsapi", observed_at=T2)
    before = _reader(conn, CUTOFF).game_schedule_state(ctx.game_id)
    after = _reader(conn, T2).game_schedule_state(ctx.game_id)
    assert before is not None and before.status == "scheduled"  # original, before reschedule
    assert before.scheduled_start != conn_start_2               # future reschedule not leaked
    assert after is not None and after.status == "postponed" and after.scheduled_start == conn_start_2
    # Status and scheduled_start came from the SAME observation.
    assert after.status_id is not None and before.status_id != after.status_id


# --------------------------------------------------------------------------- #
# §3 sportsbook market classification
# --------------------------------------------------------------------------- #
def test_sportsbook_market_mutable_metadata_rejected() -> None:
    for col in ("bookmaker_title", "bookmaker_last_update", "market_last_update",
                "raw_response_id", "first_observed_at", "last_observed_at", "updated_at",
                "created_at"):
        with pytest.raises(ForbiddenColumnError):
            assert_column_readable("sportsbook_markets", col)
    for col in ("sb_market_id", "sb_event_id", "bookmaker_key", "market_key"):
        assert_column_readable("sportsbook_markets", col)  # structural allowlist
    with pytest.raises(ForbiddenJoinError):
        assert_selectable("sportsbook_markets", ["*"])


def test_structural_market_identity_accessor(conn: sqlite3.Connection, ctx: Ctx) -> None:
    outcome_id = seed_sb_outcome_ctx(conn)
    sb_market_id = str(conn.execute(
        "SELECT sb_market_id FROM sportsbook_outcomes WHERE sb_outcome_id=?",
        (outcome_id,)).fetchone()[0])
    ident = _reader(conn).sportsbook_market_identity(sb_market_id)
    assert ident is not None
    assert set(ident.as_dict()) == {"sb_market_id", "sb_event_id", "bookmaker_key", "market_key"}
    assert ident.market_key == "h2h"


# --------------------------------------------------------------------------- #
# §4 rebuild-deterministic equal-timestamp handling (fresh DBs, 100 orders)
# --------------------------------------------------------------------------- #
def _fresh_db(tmp_path: Path, idx: int) -> Path:
    p = tmp_path / f"c{idx}.db"
    initialize_database(p)
    return p


def _insert_stats(conn: sqlite3.Connection, rows: list[tuple[str, int, str]]) -> None:
    # rows: list of (content_hash, runs, observed_at) at one anchor. Direct insert
    # with FKs off (we only read this one table, never join it). PRAGMA
    # foreign_keys is a no-op inside a transaction, so set it in autocommit first.
    # (The schema's UNIQUE(anchor, observed_at, content_hash) forbids feature-
    # identical equal-time duplicates, so an equal-time multi-row set is always a
    # genuine conflict.)
    from sports_quant.db.ids import new_team_game_stat_id
    conn.execute("PRAGMA foreign_keys = OFF")
    for content_hash, runs, observed_at in rows:
        conn.execute(
            "INSERT INTO team_game_statistics (stat_id, game_ref_id, provider, "
            "provider_game_id, provider_team_id, home_away, runs, observed_at, ingested_at, "
            "raw_response_id, raw_response_hash, content_hash, created_at) "
            "VALUES (?, 'GR', 'p', 'G', 'PT', 'home', ?, ?, ?, 'rr', 'rh', ?, ?)",
            (new_team_game_stat_id(), runs, observed_at, observed_at, content_hash, observed_at))
    conn.commit()


def _select(path: Path) -> object:
    cutoff = Cutoff.parse(CUTOFF)
    with read_only_connection(path) as ro:
        try:
            row = latest_as_of(ro, table="team_game_statistics", cutoff=cutoff,
                               anchor_where="game_ref_id = ? AND provider_team_id = ?",
                               anchor_params=("GR", "PT"))
            return None if row is None else (row["content_hash"], row["runs"])
        except AsOfAmbiguityError:
            return "AMBIGUOUS"


_T_EARLY = "2026-07-05T00:00:00.000000Z"


def test_shuffled_insertion_and_fresh_ulids_pick_same_latest_over_100_fresh_dbs(
        tmp_path: Path) -> None:
    # Distinct timestamps in shuffled insertion order across 100 FRESH databases
    # (fresh ULIDs each rebuild): the winner is always the max observed_at before
    # the cutoff, never influenced by ULID or insertion order.
    results = set()
    for i in range(100):
        p = _fresh_db(tmp_path, i)
        with Database(p).connection() as conn:
            rows = [("Hearly", 1, _T_EARLY), ("Hwin", 5, T1), ("Hfuture", 9, T2)]
            random.Random(i).shuffle(rows)
            _insert_stats(conn, rows)
        results.add(_select(p))
    assert results == {("Hwin", 5)}  # latest-before-cutoff, stable across every rebuild


def test_equal_time_conflicting_rows_fail_closed_over_100_fresh_dbs(tmp_path: Path) -> None:
    outcomes = set()
    for i in range(100):
        p = _fresh_db(tmp_path, i + 1000)
        with Database(p).connection() as conn:
            rows = [("HA", 3, T1), ("HB", 9, T1)]  # conflicting content, same instant
            random.Random(i).shuffle(rows)
            _insert_stats(conn, rows)
        outcomes.add(_select(p))
    assert outcomes == {"AMBIGUOUS"}  # never a ULID/insertion-order winner


def test_distinct_timestamps_still_select_latest_before_cutoff(conn: sqlite3.Connection,
                                                               ctx: Ctx) -> None:
    from .conftest import seed_team_stat
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=2)
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T2,
                   runs=8)  # future
    obs = _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and obs.get("runs") == 2  # inner-aggregate trap stays fixed


# --------------------------------------------------------------------------- #
# §5 review-gated identity
# --------------------------------------------------------------------------- #
def test_flagged_unreviewed_decision_hidden(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_sb_outcome_ctx(conn)
    ev = _sb_event_id(conn)
    link_sb_event(conn, sb_event_id=ev, game_id=ctx.game_id, orientation="direct",
                  needs_review=True)  # accepted but review-gated, never reviewed
    reader = _reader(conn, _FAR)
    assert reader.sportsbook_event_game(ev) is None
    assert reader.matched_entity(source_provider="the_odds_api", source_ref=ev,
                                 entity_type="sportsbook_event") is None


def test_review_completed_after_cutoff_hidden_before_and_shown_after(conn: sqlite3.Connection,
                                                                     ctx: Ctx) -> None:
    seed_sb_outcome_ctx(conn)
    ev = _sb_event_id(conn)
    dec_id = link_sb_event(conn, sb_event_id=ev, game_id=ctx.game_id, orientation="direct",
                           needs_review=True)
    with transaction(conn):
        SqliteMatchingRepository(conn).mark_reviewed(dec_id, reviewed_by="alice")
        # Stamp the review's transaction time far in the future (review columns are
        # the only mutable columns; the trigger permits this).
        conn.execute("UPDATE entity_match_decisions SET reviewed_at=? WHERE match_id=?",
                     ("2030-01-01T00:00:00.000000Z", dec_id))
    before = AsOfReader(conn, Cutoff.parse("2029-01-01T00:00:00.000000Z"))
    after = AsOfReader(conn, Cutoff.parse("2031-01-01T00:00:00.000000Z"))
    assert before.sportsbook_event_game(ev) is None       # review completed after cutoff
    assert after.sportsbook_event_game(ev) is not None     # review completed by cutoff


# --------------------------------------------------------------------------- #
# §6 true read-only mode
# --------------------------------------------------------------------------- #
def test_read_only_opens_existing_and_blocks_writes(db_path: Path) -> None:
    with read_only_connection(db_path) as ro:
        assert ro.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 60
        assert int(ro.execute("PRAGMA query_only").fetchone()[0]) == 1  # defense in depth
        for stmt in ("INSERT INTO leagues (league_id, code, name, created_at, updated_at) "
                     "VALUES ('x','x','x','t','t')",
                     "UPDATE teams SET updated_at='z'", "DELETE FROM teams",
                     "CREATE TABLE zz (a)", "PRAGMA user_version = 5"):
            with pytest.raises((sqlite3.OperationalError, sqlite3.DatabaseError)):
                ro.execute(stmt)


def test_read_only_missing_db_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        with read_only_connection(missing):
            pass
    assert not missing.exists()


def test_read_only_missing_parent_not_created(tmp_path: Path) -> None:
    nested = tmp_path / "no_such_dir" / "c.db"
    with pytest.raises(FileNotFoundError):
        with read_only_connection(nested):
            pass
    assert not (tmp_path / "no_such_dir").exists()


def test_read_only_creates_no_sidecars_and_hash_unchanged(db_path: Path) -> None:
    d = db_path.parent
    before_list = sorted(x.name for x in d.iterdir())
    before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with read_only_connection(db_path) as ro:
        ro.execute("SELECT COUNT(*) FROM games").fetchone()
    after_list = sorted(x.name for x in d.iterdir())
    assert after_list == before_list  # no -wal / -shm / journal sidecar appeared
    assert not any(n.endswith(("-wal", "-shm", "-journal")) for n in after_list)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_hash


# --------------------------------------------------------------------------- #
# §7 alias isolation
# --------------------------------------------------------------------------- #
def test_alias_tables_not_feature_joinable() -> None:
    for alias in ("team_aliases", "player_aliases", "venue_aliases"):
        assert classify(alias).classification.value == "unsupported"
        with pytest.raises(ForbiddenJoinError):
            assert_joinable({alias})


def test_late_alias_cannot_leak_backward(conn: sqlite3.Connection, ctx: Ctx) -> None:
    from .conftest import seed_sb_outcome_ctx as _mk
    _mk(conn)
    ev = _sb_event_id(conn)
    # No accepted decision before an early cutoff -> identity unresolved regardless
    # of any alias curation (aliases are not a pit feature source at all).
    early = _reader(conn, "2000-01-01T00:00:00.000000Z")
    assert early.matched_entity(source_provider="the_odds_api", source_ref=ev,
                                entity_type="sportsbook_event") is None
    # Adding a team alias now does not make aliases joinable nor create identity.
    conn.execute(
        "INSERT INTO team_aliases (alias_id, team_id, league_id, alias, normalized, alias_type, "
        "provider, is_ambiguous, source, created_at) VALUES ('al_x', ?, 'lg_mlb', 'X', 'x', "
        "'provider', 'the_odds_api', 0, 'seed', ?)", (ctx.home_team_id, T2))
    conn.commit()
    with pytest.raises(ForbiddenJoinError):
        assert_joinable({"team_aliases"})
    assert early.matched_entity(source_provider="the_odds_api", source_ref=ev,
                                entity_type="sportsbook_event") is None


# --------------------------------------------------------------------------- #
# §8 registry exactly covers schema v16
# --------------------------------------------------------------------------- #
def test_registry_exactly_covers_schema_v16(db_path: Path) -> None:
    with Database(db_path).connection() as conn:
        actual = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
    registered = set(registered_tables())
    assert registered == actual, {"missing": actual - registered, "extra": registered - actual}
    assert len(registered_tables()) == len(set(registered_tables()))  # each exactly once
    with pytest.raises(UnknownTableError):
        classify("some_future_table")


# --------------------------------------------------------------------------- #
# §8/§9 restricted-column + generic-SQL boundary
# --------------------------------------------------------------------------- #
def test_restricted_column_enforcement() -> None:
    assert_selectable("sportsbook_markets", ["market_key", "sb_event_id"])   # ok
    with pytest.raises(ForbiddenColumnError):
        assert_selectable("sportsbook_markets", ["bookmaker_title"])
    with pytest.raises(ForbiddenJoinError):
        assert_selectable("sportsbook_events", ["game_id"])  # forbidden_current_state table
    assert_selectable("team_game_statistics", ["*"])  # plain asof table: * allowed


def test_unsafe_generic_sql_fragment_rejected(conn: sqlite3.Connection) -> None:
    cutoff = Cutoff.parse(CUTOFF)
    for bad in ("game_ref_id = ? ; DROP TABLE teams",
                "game_ref_id = ? OR 1=1 -- x",
                "game_ref_id IN (SELECT game_ref_id FROM game_result_snapshots)",
                "game_ref_id = ? JOIN teams t"):
        with pytest.raises(ValueError):
            latest_as_of(conn, table="team_game_statistics", cutoff=cutoff, anchor_where=bad,
                         anchor_params=("x",))
