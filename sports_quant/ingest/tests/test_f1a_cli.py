"""F1A CLI + guarded-pilot integration tests (offline; sockets sentineled)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from sports_quant.cli import main
from sports_quant.db.init import initialize_database
from sports_quant.ingest.f1a import EXIT_BUDGET_EXHAUSTED, run_pilot_cli
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
from sports_quant.request_control import RequestGate


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real socket use raises immediately (mocked transports are unaffected)."""

    def boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _scratch(tmp_path: Path, name: str = "scratch.db") -> Path:
    path = tmp_path / name
    initialize_database(path)
    return path


def _mlb_factory(calls: list[int]):
    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"dates": []})  # empty schedule

    def factory(gate: RequestGate) -> MlbStatsApiClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://mlb.invalid")
        return MlbStatsApiClient(client=http, gate=gate, league="mlb", max_retries=0)

    return factory


# --- --plan : zero network ------------------------------------------------- #
def test_plan_mode_makes_no_network(no_network, capsys: pytest.CaptureFixture) -> None:
    rc = main(["ingest-mlb", "--plan", "--from", "2026-07-01", "--to", "2026-07-30", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["network_occurred"] is False
    assert payload["executable"] is True
    assert payload["expected_schema_version"] == 16
    assert "manifest_hash" in payload


def test_plan_mode_nba_needs_bounds(no_network, capsys: pytest.CaptureFixture) -> None:
    rc = main(["ingest-nba", "--plan", "--from", "2026-01-05", "--to", "2026-01-05",
               "--include", "plays", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["executable"] is False  # per-game plays unbounded without --max-games/pages
    assert payload["unresolved_bounds"]


def test_plan_hash_deterministic(no_network, capsys: pytest.CaptureFixture) -> None:
    args = ["ingest-nba", "--plan", "--from", "2026-01-05", "--to", "2026-01-05",
            "--max-pages", "5", "--json"]
    main(args)
    h1 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["manifest_hash"]
    main(args)
    h2 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["manifest_hash"]
    assert h1 == h2


# --- guard failures (before any client/DB) --------------------------------- #
def test_pilot_requires_request_cap(tmp_path: Path) -> None:
    rc = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-02",
                       includes=(), request_cap=None, scratch_db=_scratch(tmp_path),
                       checkpoint=None, out=lambda _s: None)
    assert rc == 2


def test_nba_pilot_requires_credit_cap(tmp_path: Path) -> None:
    rc = run_pilot_cli(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                       includes=(), request_cap=100, credit_cap=None, max_pages=5,
                       scratch_db=_scratch(tmp_path), checkpoint=None, out=lambda _s: None)
    assert rc == 2


def test_pilot_rejects_unbounded_fanout(tmp_path: Path) -> None:
    rc = run_pilot_cli(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                       includes=("plays",), request_cap=100, credit_cap=100,
                       scratch_db=_scratch(tmp_path), checkpoint=None, out=lambda _s: None)
    assert rc == 2  # per-game plays unbounded


def test_pilot_rejects_cap_below_max(tmp_path: Path) -> None:
    rc = run_pilot_cli(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                       includes=(), request_cap=1, credit_cap=100, max_pages=5,
                       scratch_db=_scratch(tmp_path), checkpoint=None, out=lambda _s: None)
    assert rc == 2  # required cap (5*retry) exceeds 1


def test_pilot_requires_scratch_db() -> None:
    rc = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-02",
                       includes=(), request_cap=100, scratch_db=None, checkpoint=None,
                       out=lambda _s: None)
    assert rc == 2


def test_resume_requires_checkpoint(tmp_path: Path) -> None:
    rc = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-02",
                       includes=(), request_cap=100, scratch_db=_scratch(tmp_path),
                       checkpoint=None, resume=True, out=lambda _s: None)
    assert rc == 2


def test_new_scratch_db_rejected(tmp_path: Path) -> None:
    rc = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-02",
                       includes=(), request_cap=100, scratch_db=tmp_path / "missing.db",
                       checkpoint=tmp_path / "c.ckpt", out=lambda _s: None)
    assert rc == 2  # must db-init first; ingestion never migrates


# --- guarded live pilot (mocked transport; sockets sentineled) ------------- #
def test_pilot_cap_below_min_rejected_zero_transport(tmp_path: Path) -> None:
    # request_cap below the plan's conservative maximum is a usage error, caught
    # BEFORE any client/DB work -> exit 2, transport never reached.
    calls: list[int] = []
    db = _scratch(tmp_path)
    ckpt = tmp_path / "p.ckpt"
    rc = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-01",
                       includes=(), request_cap=0, scratch_db=db, checkpoint=ckpt,
                       out=lambda _s: None, client_factory=_mlb_factory(calls))
    assert rc == 2
    assert calls == []  # transport never reached


def test_truncated_run_maps_to_budget_exit_code(tmp_path: Path, monkeypatch) -> None:
    # A runtime truncation (actual exceeded the cap) maps to EXIT_BUDGET_EXHAUSTED.
    from sports_quant.ingest import f1a as f1a_mod
    from sports_quant.ingest.pilot import PilotResult

    def fake_run_pilot(**_kw):  # type: ignore[no-untyped-def]
        return PilotResult(success=False, truncated=True, completed=1, skipped=0,
                           exhaustion={"limit_type": "request", "cap": 4}, usage={
                               "attempted_requests": 4}, checkpoint_state="truncated",
                           network_occurred=True, database_mutated=True)

    monkeypatch.setattr(f1a_mod, "run_pilot", fake_run_pilot)
    rc = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-01",
                       includes=(), request_cap=10, scratch_db=_scratch(tmp_path),
                       checkpoint=tmp_path / "p.ckpt", out=lambda _s: None,
                       client_factory=_mlb_factory([]))
    assert rc == EXIT_BUDGET_EXHAUSTED


def test_pilot_happy_path_completes(tmp_path: Path) -> None:
    calls: list[int] = []
    db = _scratch(tmp_path)
    ckpt = tmp_path / "p.ckpt"
    rc = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-01",
                       includes=(), request_cap=10, scratch_db=db, checkpoint=ckpt,
                       out=lambda _s: None, client_factory=_mlb_factory(calls))
    assert rc == 0
    assert sum(calls) >= 1  # schedule was fetched via the mocked transport
    # A completed resume performs zero transport calls.
    calls2: list[int] = []
    rc2 = run_pilot_cli(league="mlb", from_date="2026-07-01", to_date="2026-07-01",
                        includes=(), request_cap=10, scratch_db=db, checkpoint=ckpt,
                        resume=True, out=lambda _s: None, client_factory=_mlb_factory(calls2))
    assert rc2 == 0
    assert calls2 == []  # nothing re-fetched on completed resume
