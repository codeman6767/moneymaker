"""Schema-level guarantees for the Kalshi tables (migration c007).

Inspects ``sqlite_master`` / ``PRAGMA`` to prove append-only protection, foreign
keys, price CHECK constraints, and -- the load-bearing safety check -- that no
account/balance/position/fill/order column exists anywhere in the Kalshi schema.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.engine import foreign_keys_enabled
from sports_quant.db.schema import PHASE_C_TABLES

_TS = "2026-07-22T18:00:00.000000Z"

# Substrings that would signal an account-scoped (non-public) surface.
_ACCOUNT_TOKENS = ("balance", "position", "portfolio", "fill", "account", "order")


def _seed_book_and_trade(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, status, "
        "requested_at, started_at, started_monotonic_ns, tool_version, created_at) VALUES "
        "('run_k', 'ingest-kalshi', 'kalshi_public', 'list_markets', '{}', 'started', ?, ?, 0, "
        "'t', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "UPDATE ingestion_runs SET status='succeeded', completed_at=?, duration_ns=1 "
        "WHERE run_id='run_k'",
        (_TS,),
    )
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, requested_at, received_at, "
        "elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) VALUES "
        "('raw_k', 'run_k', 'kalshi_public', '/markets', '{}', 200, '{}', ?, ?, 1, '{}', 2, 'bh', "
        "'chr', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO kalshi_orderbook_snapshots (snapshot_id, market_ticker, best_yes_bid, "
        "yes_levels, no_levels, depth_levels, observed_at, ingested_at, run_id, raw_response_id, "
        "raw_response_hash, content_hash, created_at) VALUES "
        "('kob_s', 'MKT-1', 42, 1, 0, 1, ?, ?, 'run_k', 'raw_k', 'chr', 'ch1', ?)",
        (_TS, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO kalshi_orderbook_levels (level_id, snapshot_id, side, price, quantity, "
        "level_index, created_at) VALUES ('kol_s', 'kob_s', 'yes', 42, 100, 0, ?)",
        (_TS,),
    )
    conn.execute(
        "INSERT INTO kalshi_public_trades (trade_id, market_ticker, count, observed_at, "
        "ingested_at, run_id, raw_response_id, content_hash, created_at) VALUES "
        "('ktr_s', 'MKT-1', 5, ?, ?, 'run_k', 'raw_k', 'tc1', ?)",
        (_TS, _TS, _TS),
    )
    return "kob_s"


def test_no_account_scoped_column_exists_anywhere(conn: sqlite3.Connection) -> None:
    """The public corpus must never carry an account/order/fill/position column."""

    offenders = []
    for table in PHASE_C_TABLES:
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
            name = row[1].lower()
            if any(tok in name for tok in _ACCOUNT_TOKENS):
                offenders.append(f"{table}.{name}")
    assert not offenders, f"account-scoped columns present: {offenders}"


def test_orderbook_snapshot_is_append_only(conn: sqlite3.Connection) -> None:
    _seed_book_and_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE kalshi_orderbook_snapshots SET best_yes_bid = 1 WHERE snapshot_id = 'kob_s'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM kalshi_orderbook_snapshots WHERE snapshot_id = 'kob_s'")


def test_orderbook_levels_are_append_only(conn: sqlite3.Connection) -> None:
    _seed_book_and_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE kalshi_orderbook_levels SET quantity = 1 WHERE level_id = 'kol_s'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM kalshi_orderbook_levels WHERE level_id = 'kol_s'")


def test_public_trades_are_append_only(conn: sqlite3.Connection) -> None:
    _seed_book_and_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE kalshi_public_trades SET count = 1 WHERE trade_id = 'ktr_s'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM kalshi_public_trades WHERE trade_id = 'ktr_s'")


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    assert foreign_keys_enabled(conn) is True
    _seed_book_and_trade(conn)
    # A level referencing a non-existent snapshot is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kalshi_orderbook_levels (level_id, snapshot_id, side, price, quantity, "
            "level_index, created_at) VALUES ('kol_x', 'kob_missing', 'yes', 42, 1, 0, ?)",
            (_TS,),
        )


def test_price_check_constraints_effective(conn: sqlite3.Connection) -> None:
    _seed_book_and_trade(conn)
    # Level price out of [1,99].
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kalshi_orderbook_levels (level_id, snapshot_id, side, price, quantity, "
            "level_index, created_at) VALUES ('kol_b', 'kob_s', 'yes', 150, 1, 1, ?)",
            (_TS,),
        )
    # Negative quantity.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kalshi_orderbook_levels (level_id, snapshot_id, side, price, quantity, "
            "level_index, created_at) VALUES ('kol_c', 'kob_s', 'yes', 40, -1, 1, ?)",
            (_TS,),
        )
    # Best-bid out of range on a snapshot.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kalshi_orderbook_snapshots (snapshot_id, market_ticker, best_yes_bid, "
            "observed_at, ingested_at, run_id, raw_response_id, raw_response_hash, content_hash, "
            "created_at) VALUES ('kob_bad', 'MKT-1', 200, ?, ?, 'run_k', 'raw_k', 'chr', 'chx', ?)",
            (_TS, _TS, _TS),
        )
    # Trade yes_price out of range.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kalshi_public_trades (trade_id, market_ticker, yes_price, count, "
            "observed_at, ingested_at, run_id, raw_response_id, content_hash, created_at) VALUES "
            "('ktr_bad', 'MKT-1', 250, 1, ?, ?, 'run_k', 'raw_k', 'tcx', ?)",
            (_TS, _TS, _TS),
        )


def test_duplicate_price_level_rejected_by_unique(conn: sqlite3.Connection) -> None:
    _seed_book_and_trade(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kalshi_orderbook_levels (level_id, snapshot_id, side, price, quantity, "
            "level_index, created_at) VALUES ('kol_dup', 'kob_s', 'yes', 42, 999, 1, ?)",
            (_TS,),
        )
