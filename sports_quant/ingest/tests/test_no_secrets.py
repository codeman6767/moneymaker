"""Data-preservation and credential-safety guarantees for Phase B.

The load-bearing test here is the whole-database secret sweep: with a sentinel
key configured, ingest against a mock transport and scan *every TEXT column of
every table* for the sentinel. A whole-database sweep (rather than a per-column
assertion) means a newly added column is covered the day it is created, not the
day someone remembers to test it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sports_quant.db.engine import Database
from sports_quant.ingest.odds_ingestor import ingest_odds
from sports_quant.redaction import STORABLE_RESPONSE_HEADERS

from .conftest import SENTINEL_KEY, mlb_payload


async def _ingest(database: Database, make_client, client_for) -> None:
    await ingest_odds(
        database=database, client=make_client(client_for(mlb_payload())), sport="mlb"
    )


def _all_text_values(conn: sqlite3.Connection):
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall():
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            for column, value in zip(columns, row, strict=True):
                yield table, column, value


async def test_whole_database_contains_no_sentinel_key(
    database: Database, make_client, client_for
) -> None:
    await _ingest(database, make_client, client_for)

    offenders = []
    with database.connection() as conn:
        for table, column, value in _all_text_values(conn):
            if isinstance(value, str) and SENTINEL_KEY in value:
                offenders.append(f"{table}.{column}")
    assert not offenders, f"API key leaked into stored columns: {offenders}"


async def test_authorization_header_is_never_stored(
    database: Database, make_client, client_for
) -> None:
    await _ingest(database, make_client, client_for)

    with database.connection() as conn:
        headers_rows = [
            json.loads(r[0])
            for r in conn.execute("SELECT response_headers_json FROM raw_responses")
        ]
    assert headers_rows
    for headers in headers_rows:
        assert "authorization" not in headers
        assert "set-cookie" not in headers
        # Only allow-listed names survive.
        assert set(headers).issubset(STORABLE_RESPONSE_HEADERS)
        # But the useful ones are kept.
        assert "x-requests-remaining" in headers


async def test_request_params_are_sanitized(
    database: Database, make_client, client_for
) -> None:
    await _ingest(database, make_client, client_for)
    with database.connection() as conn:
        params = [
            json.loads(r[0]) for r in conn.execute("SELECT request_params_json FROM raw_responses")
        ]
    assert params
    for entry in params:
        # apiKey is masked by name, never carrying its value.
        assert entry.get("apiKey") == "***REDACTED***"
        assert SENTINEL_KEY not in json.dumps(entry)


async def test_endpoint_never_stores_a_query_string(
    database: Database, make_client, client_for
) -> None:
    await _ingest(database, make_client, client_for)
    with database.connection() as conn:
        endpoints = [r[0] for r in conn.execute("SELECT endpoint FROM raw_responses")]
    assert endpoints
    for endpoint in endpoints:
        assert "?" not in endpoint
        assert endpoint.startswith("/v4/sports/")


async def test_raw_responses_are_preserved_and_immutable(
    database: Database, make_client, client_for
) -> None:
    await _ingest(database, make_client, client_for)
    with database.connection() as conn:
        row = conn.execute(
            "SELECT raw_response_id, body, http_method, http_status FROM raw_responses"
        ).fetchone()
        assert row is not None
        assert row["http_method"] == "GET"
        assert row["http_status"] == 200
        assert "New York Yankees" in row["body"]  # preserved verbatim

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE raw_responses SET body = 'x' WHERE raw_response_id = ?",
                (row["raw_response_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM raw_responses WHERE raw_response_id = ?", (row["raw_response_id"],)
            )


async def test_price_snapshots_are_append_only(
    database: Database, make_client, client_for
) -> None:
    await _ingest(database, make_client, client_for)
    with database.connection() as conn:
        snap_id = conn.execute(
            "SELECT snapshot_id FROM sportsbook_price_snapshots LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE sportsbook_price_snapshots SET price_american = 1 "
                "WHERE snapshot_id = ?",
                (snap_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM sportsbook_price_snapshots WHERE snapshot_id = ?", (snap_id,)
            )


async def test_normalized_rows_trace_back_to_a_raw_response(
    database: Database, make_client, client_for
) -> None:
    await _ingest(database, make_client, client_for)
    with database.connection() as conn:
        for table in ("sportsbook_events", "sportsbook_markets", "sportsbook_price_snapshots"):
            dangling = conn.execute(
                f"SELECT COUNT(*) FROM {table} t "
                "LEFT JOIN raw_responses r ON t.raw_response_id = r.raw_response_id "
                "WHERE r.raw_response_id IS NULL"
            ).fetchone()[0]
            assert dangling == 0, f"{table} has rows with no raw response"

        # The two-link contract: every price's raw_response_hash resolves to a
        # real raw response content_hash.
        bad_hash = conn.execute(
            "SELECT COUNT(*) FROM sportsbook_price_snapshots s "
            "LEFT JOIN raw_responses r ON s.raw_response_hash = r.content_hash "
            "WHERE r.content_hash IS NULL"
        ).fetchone()[0]
    assert bad_hash == 0


async def test_get_only_storage_rejects_a_non_get_method(database: Database) -> None:
    """The storage layer is independently incapable of recording a write verb."""

    from sports_quant.db.engine import transaction
    from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
    from sports_quant.db.repositories.raw_responses import (
        SqliteRawResponseRepository,
        response_content_hash,
    )

    with database.connection() as conn:
        with transaction(conn):
            run = SqliteIngestionRunRepository(conn).start(
                command="ingest-odds",
                provider="the_odds_api",
                operation="get_odds",
                args_json="{}",
                started_monotonic_ns=0,
                tool_version="test",
            )
        repo = SqliteRawResponseRepository(conn)
        with pytest.raises(Exception, match="GET"):
            with transaction(conn):
                repo.store(
                    run_id=run.run_id,
                    provider="the_odds_api",
                    endpoint="/v4/sports/baseball_mlb/odds",
                    request_params_json="{}",
                    http_status=200,
                    response_headers_json="{}",
                    requested_at="2026-07-22T00:00:00.000000Z",
                    received_at="2026-07-22T00:00:00.000000Z",
                    elapsed_ns=1,
                    body="[]",
                    content_hash=response_content_hash(
                        provider="the_odds_api",
                        endpoint="/v4/sports/baseball_mlb/odds",
                        request_params={},
                        body="[]",
                    ),
                    http_method="POST",
                )


def test_no_order_or_execution_code_reachable_from_ingest() -> None:
    """The ingestion package must not import the quarantined execution lane."""

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "import gateway" in text or "from gateway" in text:
            offenders.append(str(path.name))
    assert not offenders, f"ingest imports the quarantined gateway: {offenders}"
