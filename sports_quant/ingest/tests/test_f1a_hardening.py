"""F1A hardening regressions: content-digest identity, checkpoint durability,
staged transport reporting (all offline)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.ingest.checkpoint import (
    Checkpoint,
    CheckpointError,
    load_checkpoint,
    write_checkpoint,
)
from sports_quant.ingest.cost_policies import build_mlb_policy
from sports_quant.ingest.scratch_db import classify_scratch_db
from sports_quant.providers.base_provider import BaseProviderClient
from sports_quant.request_control import (
    BudgetExhausted,
    CreditBudget,
    RequestBudget,
    RequestGate,
)


def _v16(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    initialize_database(p)
    return p


def _add_run(db: Path, tool_version: str = "t") -> None:
    with Database(db).connection() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, "
            "status, requested_at, started_at, completed_at, started_monotonic_ns, "
            "tool_version, created_at) VALUES (?, 'ingest','mlb_statsapi','op','{}','succeeded',"
            "'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z',"
            "'2026-01-01T00:00:00.000000Z',0,?,'2026-01-01T00:00:00.000000Z')",
            (f"run_{tool_version}", tool_version))


# --- content-digest identity (#4) ----------------------------------------- #
def test_same_row_count_different_content_differs(tmp_path: Path) -> None:
    a, b = _v16(tmp_path, "a.db"), _v16(tmp_path, "b.db")
    _add_run(a, "toolA")
    _add_run(b, "toolB")  # same row count (1), different content
    fa = classify_scratch_db(a).fingerprint
    fb = classify_scratch_db(b).fingerprint
    assert fa is not None and fb is not None
    assert fa != fb  # content change detected despite equal counts


def test_digest_is_deterministic_for_same_db(tmp_path: Path) -> None:
    # The same database digested twice is identical (deterministic recompute),
    # which is what resume relies on. (Two separately-initialized DBs legitimately
    # differ because seed rows carry init-time timestamps.)
    db = _v16(tmp_path, "a.db")
    _add_run(db, "same")
    assert classify_scratch_db(db).fingerprint == classify_scratch_db(db).fingerprint


def test_content_replacement_after_classification_detected(tmp_path: Path) -> None:
    db = _v16(tmp_path, "s.db")
    _add_run(db, "v1")
    fp1 = classify_scratch_db(db).fingerprint
    with Database(db).connection() as conn:  # update a value, keep row count
        conn.execute("UPDATE ingestion_runs SET tool_version='v2'")
    fp2 = classify_scratch_db(db).fingerprint
    assert fp1 != fp2  # substitution/mutation at equal row counts is caught


def test_wal_committed_content_included(tmp_path: Path) -> None:
    db = _v16(tmp_path, "w.db")
    fp0 = classify_scratch_db(db).fingerprint
    conn = Database(db).connect()  # WAL journal mode; leave a committed, uncheckpointed row
    try:
        conn.execute(
            "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, "
            "status, requested_at, started_at, completed_at, started_monotonic_ns, "
            "tool_version, created_at) VALUES ('run_wal','ingest','mlb_statsapi','op','{}',"
            "'succeeded','2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z',"
            "'2026-01-01T00:00:00.000000Z',0,'t','2026-01-01T00:00:00.000000Z')")
        conn.commit()
        assert (Path(f"{db}-wal")).exists()  # committed but uncheckpointed
        fp1 = classify_scratch_db(db).fingerprint
        assert fp1 != fp0  # WAL-resident committed content is included
    finally:
        conn.close()


# --- checkpoint durability (C3) -------------------------------------------- #
def _ckpt(**over):  # type: ignore[no-untyped-def]
    base = dict(manifest_hash="H", plan_version="f1a-plan-v1", provider="balldontlie",
                league="nba", date_range="2026-01-05", families=("games",), scratch_db="s.db",
                scratch_fingerprint="FP", schema_version=16, request_cap=20, credit_cap=20)
    base.update(over)
    return Checkpoint(**base)  # type: ignore[arg-type]


def test_concurrent_writers_leave_a_valid_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "c.ckpt"
    write_checkpoint(path, _ckpt(completed_identities=["seed"]))

    def worker(n: int) -> None:
        for i in range(15):
            write_checkpoint(path, _ckpt(completed_identities=[f"{n}-{i}"]))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    loaded = load_checkpoint(path)  # always a valid checkpoint, never a torn file
    assert loaded.schema_version == 16
    assert not list(tmp_path.glob("c.ckpt.tmp-*"))  # no stray temp files


def test_hostile_checkpoint_json_fails_closed(tmp_path: Path) -> None:
    dup = tmp_path / "dup.ckpt"
    dup.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(dup)

    bad_root = tmp_path / "arr.ckpt"
    bad_root.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(bad_root)

    truncated = tmp_path / "trunc.ckpt"
    truncated.write_text('{"checkpoint_format_version":', encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(truncated)


def test_checkpoint_latest_write_wins_no_rollback(tmp_path: Path) -> None:
    path = tmp_path / "c.ckpt"
    write_checkpoint(path, _ckpt(completed_identities=["a"], state="in_progress"))
    write_checkpoint(path, _ckpt(completed_identities=["a", "b"], state="completed"))
    loaded = load_checkpoint(path)
    assert loaded.state == "completed" and set(loaded.completed_identities) == {"a", "b"}


# --- staged transport reporting (C1) --------------------------------------- #
class _Mlb(BaseProviderClient):
    provider_name = "mlb_statsapi"


def _mlb_gate(max_requests: int) -> RequestGate:
    return RequestGate(request_budget=RequestBudget(max_requests=max_requests),
                       credit_budget=CreditBudget(applicable=False), cost_policy=build_mlb_policy())


def test_network_occurred_only_on_real_transport() -> None:
    # Zero budget -> reservation blocks BEFORE any send -> no network reported.
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"ok": True})

    gate = _mlb_gate(0)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t.invalid")
    c = _Mlb(base_url="http://t.invalid", policy=None, client=http, gate=gate,  # type: ignore[arg-type]
             league="mlb")

    async def go() -> None:
        with pytest.raises(BudgetExhausted):
            await c._get("/schedule", endpoint_family="schedule")
        await c.aclose()

    asyncio.run(go())
    assert gate.usage.reserved_attempts == 0  # blocked before reserving
    assert gate.usage.network_occurred is False
    assert gate.usage.transport_starts == 0
    assert calls == []


def test_staged_counters_advance_on_success() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    gate = _mlb_gate(5)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t.invalid")
    c = _Mlb(base_url="http://t.invalid", policy=None, client=http, gate=gate,  # type: ignore[arg-type]
             league="mlb")

    async def go() -> None:
        await c._get("/schedule", endpoint_family="schedule")
        await c.aclose()

    asyncio.run(go())
    u = gate.usage
    assert u.reserved_attempts == 1
    assert u.transport_starts == 1 and u.network_occurred is True
    assert u.responses_received == 1 and u.parse_successes == 1 and u.successful_responses == 1
