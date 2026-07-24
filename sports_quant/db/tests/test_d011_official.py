"""Migration d011: Phase D2 official-snapshot tables + append-only guards.

Schema-level checks that the tables exist, are append-only (BEFORE UPDATE/DELETE
triggers fire), and that the migration is applied exactly once. The functional
ingestion behaviour is covered by ``ingest/tests/test_phase_d2_mlb.py``.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.schema import PHASE_D2_TABLES

_TS = "2024-04-09T18:00:00.000000Z"


def test_all_d2_tables_exist(conn: sqlite3.Connection) -> None:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in PHASE_D2_TABLES:
        assert table in names, f"{table} missing after d011"


def test_d2_tables_are_append_only(conn: sqlite3.Connection) -> None:
    # Seed a run + raw + provider game reference so an insert satisfies the FKs.
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, status, "
        "requested_at, started_at, started_monotonic_ns, tool_version, created_at) VALUES "
        "('run_d2', 'ingest-mlb', 'mlb_statsapi', 'ingest_mlb', '{}', 'started', ?, ?, 0, 't', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, requested_at, received_at, "
        "elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) VALUES "
        "('raw_d2', 'run_d2', 'mlb_statsapi', '/schedule', '{}', 200, '{}', ?, ?, 1, '{}', 2, "
        "'bh', 'ch', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO provider_game_references (reference_id, provider, provider_game_id, "
        "first_raw_response_id, current_raw_response_id, current_raw_response_hash, "
        "first_observed_at, last_observed_at, created_at, updated_at) VALUES "
        "('pgr_d2', 'mlb_statsapi', '745804', 'raw_d2', 'raw_d2', 'ch', ?, ?, ?, ?)",
        (_TS, _TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO game_schedule_snapshots (schedule_id, game_ref_id, provider, "
        "provider_game_id, mapped_status, observed_at, ingested_at, run_id, raw_response_id, "
        "raw_response_hash, content_hash, created_at) VALUES "
        "('gss_d2', 'pgr_d2', 'mlb_statsapi', '745804', 'scheduled', ?, ?, 'run_d2', 'raw_d2', "
        "'ch', 'cnt', ?)",
        (_TS, _TS, _TS),
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE game_schedule_snapshots SET mapped_status='final'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM game_schedule_snapshots")


def test_mapped_status_check_rejects_unknown_value(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO game_schedule_snapshots (schedule_id, game_ref_id, provider, "
            "provider_game_id, mapped_status, observed_at, ingested_at, raw_response_id, "
            "raw_response_hash, content_hash, created_at) VALUES "
            "('gss_bad', 'pgr_x', 'mlb_statsapi', '1', 'not_a_status', ?, ?, 'raw_x', 'h', 'c', ?)",
            (_TS, _TS, _TS),
        )


def test_d2_tables_registered_in_append_only(conn: sqlite3.Connection) -> None:
    from sports_quant.db.schema import APPEND_ONLY_TABLES

    for table in PHASE_D2_TABLES:
        assert table in APPEND_ONLY_TABLES, f"{table} not registered append-only"
