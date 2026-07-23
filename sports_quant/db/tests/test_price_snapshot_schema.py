"""Schema-level guarantees for sportsbook_price_snapshots after migration b006.

These inspect the live SQLite schema (``sqlite_master`` / ``PRAGMA``) rather than
going through the repository, so they prove the *database* enforces the
transition-aware uniqueness rule, append-only protection, foreign keys, and the
price CHECK constraints -- independently of any Python-side logic.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.engine import foreign_keys_enabled

_TS = "2026-07-22T18:00:00.000000Z"


def _table_sql(conn: sqlite3.Connection) -> str:
    return str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sportsbook_price_snapshots'"
        ).fetchone()[0]
    )


def _seed_one_snapshot(conn: sqlite3.Connection) -> str:
    """Insert a minimal run -> raw -> event -> market -> outcome -> snapshot.

    Returns the snapshot id. Uses the seeded ``lg_mlb`` league is unnecessary;
    the event's league_id is left NULL (nullable in the schema).
    """

    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, status, "
        "requested_at, started_at, started_monotonic_ns, tool_version, created_at) VALUES "
        "('run_s', 'ingest-odds', 'the_odds_api', 'get_odds', '{}', 'started', ?, ?, 0, 't', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "UPDATE ingestion_runs SET status = 'succeeded', completed_at = ?, duration_ns = 1 "
        "WHERE run_id = 'run_s'",
        (_TS,),
    )
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, requested_at, received_at, "
        "elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) VALUES "
        "('raw_s', 'run_s', 'the_odds_api', '/v4/sports/baseball_mlb/odds', '{}', 200, '{}', ?, "
        "?, 1, '[]', 2, 'bh', 'chr', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO sportsbook_events (sb_event_id, provider, provider_event_id, sport_key, "
        "commence_time, home_team_raw, away_team_raw, raw_response_id, first_observed_at, "
        "last_observed_at, created_at, updated_at) VALUES "
        "('sbe_s', 'the_odds_api', 'e1', 'baseball_mlb', ?, 'NYY', 'BOS', 'raw_s', ?, ?, ?, ?)",
        (_TS, _TS, _TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO sportsbook_markets (sb_market_id, sb_event_id, bookmaker_key, market_key, "
        "raw_response_id, first_observed_at, last_observed_at, created_at, updated_at) VALUES "
        "('sbm_s', 'sbe_s', 'dk', 'h2h', 'raw_s', ?, ?, ?, ?)",
        (_TS, _TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO sportsbook_outcomes (sb_outcome_id, sb_market_id, outcome_name, "
        "provider_outcome_name, outcome_role, point_key, created_at) VALUES "
        "('sbo_s', 'sbm_s', 'nyy', 'NYY', 'home', '', ?)",
        (_TS,),
    )
    conn.execute(
        "INSERT INTO sportsbook_price_snapshots (snapshot_id, sb_outcome_id, price_american, "
        "observed_at, ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, "
        "created_at) VALUES ('sbp_s', 'sbo_s', -110, ?, ?, 'raw_s', 'chr', 'run_s', 'ch1', ?)",
        (_TS, _TS, _TS),
    )
    return "sbp_s"


def test_old_global_unique_rule_is_gone(conn: sqlite3.Connection) -> None:
    sql = _table_sql(conn)
    # The old two-column rule must not be present as its own constraint.
    assert "UNIQUE (sb_outcome_id, content_hash)" not in sql


def test_new_transition_aware_unique_rule_exists(conn: sqlite3.Connection) -> None:
    sql = _table_sql(conn)
    assert "UNIQUE (sb_outcome_id, observed_at, content_hash)" in sql


def test_the_unique_index_covers_observed_at(conn: sqlite3.Connection) -> None:
    """SQLite backs an inline UNIQUE with an auto-index over the three columns."""

    index_rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'sportsbook_price_snapshots'"
    ).fetchall()
    covering = []
    for (name,) in index_rows:
        cols = [r["name"] for r in conn.execute(f"PRAGMA index_info('{name}')").fetchall()]
        if cols == ["sb_outcome_id", "observed_at", "content_hash"]:
            covering.append(name)
    assert covering, "no unique index over (sb_outcome_id, observed_at, content_hash)"


def test_update_remains_blocked(conn: sqlite3.Connection) -> None:
    _seed_one_snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE sportsbook_price_snapshots SET price_american = -200 WHERE snapshot_id = 'sbp_s'"
        )


def test_delete_remains_blocked(conn: sqlite3.Connection) -> None:
    _seed_one_snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM sportsbook_price_snapshots WHERE snapshot_id = 'sbp_s'")


def test_append_only_triggers_still_present(conn: sqlite3.Connection) -> None:
    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'sportsbook_price_snapshots'"
        ).fetchall()
    }
    assert {"trg_sb_price_snapshots_no_update", "trg_sb_price_snapshots_no_delete"} <= triggers


def test_foreign_keys_remain_enabled(conn: sqlite3.Connection) -> None:
    assert foreign_keys_enabled(conn) is True
    # And a dangling reference is actually rejected (FKs enforced, not just on).
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sportsbook_price_snapshots (snapshot_id, sb_outcome_id, price_american, "
            "observed_at, ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, "
            "created_at) VALUES ('sbp_x', 'sbo_missing', -110, ?, ?, 'raw_missing', 'h', "
            "'run_missing', 'c', ?)",
            (_TS, _TS, _TS),
        )


def test_price_check_constraints_remain_effective(conn: sqlite3.Connection) -> None:
    _seed_one_snapshot(conn)

    # Malformed American price (magnitude < 100) is rejected by the CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sportsbook_price_snapshots (snapshot_id, sb_outcome_id, price_american, "
            "observed_at, ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, "
            "created_at) VALUES ('sbp_bad', 'sbo_s', 50, ?, ?, 'raw_s', 'chr', 'run_s', 'c2', ?)",
            (_TS, _TS, _TS),
        )

    # A malformed observed_at (not the ISO shape) is rejected by the CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sportsbook_price_snapshots (snapshot_id, sb_outcome_id, price_american, "
            "observed_at, ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, "
            "created_at) VALUES ('sbp_bad2', 'sbo_s', -110, 'not-a-timestamp', ?, 'raw_s', 'chr', "
            "'run_s', 'c3', ?)",
            (_TS, _TS),
        )

    # A decimal price <= 1.0 is rejected by the CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sportsbook_price_snapshots (snapshot_id, sb_outcome_id, price_american, "
            "price_decimal, observed_at, ingested_at, raw_response_id, raw_response_hash, run_id, "
            "content_hash, created_at) VALUES ('sbp_bad3', 'sbo_s', -110, 0.5, ?, ?, 'raw_s', "
            "'chr', 'run_s', 'c4', ?)",
            (_TS, _TS, _TS),
        )
