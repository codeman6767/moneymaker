"""Ingestion-run lifecycle and error sanitization."""

from __future__ import annotations

import httpx

from sports_quant.db.engine import Database, transaction
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.ingest.odds_ingestor import ingest_odds
from sports_quant.ingest.runner import RunCounters, sanitize_error

from .conftest import SENTINEL_KEY, mlb_payload


def test_sanitize_error_masks_a_secret_in_a_message() -> None:
    exc = RuntimeError(f"connection to https://api.the-odds-api.com/x?apiKey={SENTINEL_KEY} failed")
    error_type, message = sanitize_error(exc, [SENTINEL_KEY])
    assert error_type == "RuntimeError"
    assert SENTINEL_KEY not in message


def test_run_lifecycle_records_started_then_terminal(database: Database) -> None:
    with database.connection() as conn:
        repo = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = repo.start(
                command="ingest-odds",
                provider="the_odds_api",
                operation="get_odds",
                args_json="{}",
                started_monotonic_ns=1000,
                tool_version="test",
                sport="mlb",
            )
        assert run.status == "started"
        assert run.completed_at is None

        with transaction(conn):
            done = repo.complete(
                run.run_id,
                status="succeeded",
                duration_ns=500,
                requests_made=1,
                records_received=6,
                records_normalized=6,
                records_inserted=6,
            )
    assert done.status == "succeeded"
    assert done.completed_at is not None
    assert done.records_inserted == 6


def test_run_counters_default_to_zero() -> None:
    counters = RunCounters()
    assert counters.records_received == 0
    assert counters.records_inserted == 0


async def test_partial_run_status_is_recorded(
    database: Database, make_client, client_for
) -> None:
    """A run that stored some records and rejected others is partially_succeeded."""

    payload = mlb_payload()
    payload[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 42  # malformed
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert result.status == "partially_succeeded"
    with database.connection() as conn:
        status = conn.execute(
            "SELECT status FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()[0]
    assert status == "partially_succeeded"


async def test_network_failure_records_sanitized_run(
    database: Database, make_client
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    result = await ingest_odds(database=database, client=make_client(handler), sport="mlb")
    assert result.status == "failed"
    with database.connection() as conn:
        run = conn.execute(
            "SELECT status, error_type, error_message FROM ingestion_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    assert run["status"] == "failed"
    assert run["error_type"] is not None
    assert SENTINEL_KEY not in (run["error_message"] or "")
