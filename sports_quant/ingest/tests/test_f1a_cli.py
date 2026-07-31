"""F1A CLI + guarded-pilot integration tests (offline; sockets sentineled).

Covers the repaired contract: legacy ungated ingestion is quarantined; --pilot is
governed by a reviewed --manifest; F1B is disabled by default; and manifest
tampering / unsupported versions / duplicate keys / conflicting args fail closed.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from sports_quant.cli import main
from sports_quant.db.init import initialize_database
from sports_quant.ingest.f1a import (
    _F1B_AUTHORIZED_ENV,
    EXIT_BUDGET_EXHAUSTED,
    emit_plan,
    run_pilot_cli,
)
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
from sports_quant.request_control import RequestGate


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def f1b_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_F1B_AUTHORIZED_ENV, "1")


def _scratch(tmp_path: Path, name: str = "scratch.db") -> Path:
    path = tmp_path / name
    initialize_database(path)
    return path


def _mlb_manifest(tmp_path: Path, name: str = "m.json") -> Path:
    """Generate a real, executable MLB skeleton manifest via zero-network --plan."""

    out = tmp_path / name
    emit_plan(league="mlb", from_date="2026-07-01", to_date="2026-07-01", includes=(),
              manifest_out=out, out=lambda _s: None)
    return out


def _mlb_factory(calls: list[int]):
    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"dates": []})  # empty schedule

    def factory(gate: RequestGate) -> MlbStatsApiClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://mlb.invalid")
        client = MlbStatsApiClient(client=http, gate=gate, league="mlb", max_retries=0)
        # MLB now paces at 30/min. The delay itself is verified with a mocked
        # clock in test_mlb_pacing.py; here the returned wait is swallowed so the
        # fixture still traverses the real pacing chokepoint without sleeping.
        async def _no_wait(_seconds: float) -> None:
            return None

        client._sleep = _no_wait  # noqa: SLF001 - deterministic test pacing
        return client

    return factory


# --- --plan : zero network ------------------------------------------------- #
def test_plan_mode_makes_no_network(no_network, capsys: pytest.CaptureFixture) -> None:
    rc = main(["ingest-mlb", "--plan", "--from", "2026-07-01", "--to", "2026-07-30", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["network_occurred"] is False
    assert payload["executable"] is True
    assert payload["expected_schema_version"] == 16


def test_plan_mode_nba_executable_when_bounded_rate_not_credits(no_network, capsys) -> None:
    # BALLDONTLIE is request-RATE limited, not credit metered: a bounded NBA plan is
    # executable, exposes the rate contract, and fabricates NO credit figures.
    rc = main(["ingest-nba", "--plan", "--from", "2026-01-05", "--to", "2026-01-05",
               "--max-pages", "5", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["executable"] is True
    assert payload["unresolved_bounds"] == []
    assert payload["credits_applicable"] is False
    assert payload["estimated_credits_max"] is None       # never fabricated
    assert payload["provider_rate_limit_per_min"] == 600   # GOAT tier max
    assert payload["configured_rate_per_min"] == 100        # conservative default


def test_plan_mode_nba_unbounded_non_executable(no_network, capsys) -> None:
    rc = main(["ingest-nba", "--plan", "--from", "2026-01-05", "--to", "2026-01-05", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["executable"] is False  # unbounded games-list fan-out
    assert payload["unresolved_bounds"]     # names the missing --max-pages bound


def test_plan_manifest_out_deterministic(no_network, tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    emit_plan(league="mlb", from_date="2026-07-01", to_date="2026-07-01", includes=(),
              manifest_out=a, out=lambda _s: None)
    emit_plan(league="mlb", from_date="2026-07-01", to_date="2026-07-01", includes=(),
              manifest_out=b, out=lambda _s: None)
    assert a.read_text() == b.read_text()  # byte-identical canonical manifest


# --- legacy quarantine + programmatic bypass ------------------------------- #
def test_legacy_ingest_mlb_blocked_by_default() -> None:
    rc = main(["ingest-mlb", "--from", "2026-07-01"])  # no --plan/--pilot
    assert rc == 2  # quarantined


def test_legacy_ingest_nba_dry_run_blocked_by_default() -> None:
    rc = main(["ingest-nba", "--from", "2026-01-05", "--dry-run"])
    assert rc == 2  # networked dry-run is still quarantined


def test_real_mlb_client_without_gate_refuses_transport() -> None:
    # Programmatic bypass: a self-owned real MLB client with no gate must refuse.
    import asyncio

    from sports_quant.providers.base_provider import ProviderError

    async def go() -> None:
        c = MlbStatsApiClient()  # real transport, no gate
        with pytest.raises(ProviderError):
            await c._get("/schedule")
        await c.aclose()

    asyncio.run(go())


# --- F1B authorization boundary -------------------------------------------- #
def test_f1b_disabled_by_default(tmp_path: Path) -> None:
    rc = run_pilot_cli(league="mlb", manifest_path=_mlb_manifest(tmp_path),
                       scratch_db=_scratch(tmp_path), out=lambda _s: None)
    assert rc == 2  # F1B not authorized


# --- manifest-governed guards (F1B authorized for the mocked path) --------- #
def test_pilot_requires_manifest(tmp_path: Path, f1b_authorized) -> None:
    rc = run_pilot_cli(league="mlb", manifest_path=None, scratch_db=_scratch(tmp_path),
                       out=lambda _s: None)
    assert rc == 2


def test_pilot_requires_scratch_db(tmp_path: Path, f1b_authorized) -> None:
    rc = run_pilot_cli(league="mlb", manifest_path=_mlb_manifest(tmp_path), scratch_db=None,
                       out=lambda _s: None)
    assert rc == 2


def test_new_scratch_db_rejected(tmp_path: Path, f1b_authorized) -> None:
    rc = run_pilot_cli(league="mlb", manifest_path=_mlb_manifest(tmp_path),
                       scratch_db=tmp_path / "missing.db", checkpoint=tmp_path / "c.ckpt",
                       out=lambda _s: None)
    assert rc == 2


def test_nba_unbounded_manifest_non_executable_refused(tmp_path: Path, f1b_authorized) -> None:
    # An UNBOUNDED NBA plan (no --max-pages) has unbounded games-list fan-out and is
    # non-executable, so run_pilot_cli must refuse it (no network) rather than run it.
    out = tmp_path / "nba.json"
    emit_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05", includes=(),
              manifest_out=out, out=lambda _s: None)
    rc = run_pilot_cli(league="nba", manifest_path=out, scratch_db=_scratch(tmp_path),
                       out=lambda _s: None)
    assert rc == 2  # unbounded fan-out -> non-executable -> refused


def test_tampered_manifest_rejected(tmp_path: Path, f1b_authorized) -> None:
    m = _mlb_manifest(tmp_path)
    text = m.read_text(encoding="utf-8")
    m.write_text(" " + text, encoding="utf-8")  # leading space -> non-canonical
    rc = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=_scratch(tmp_path),
                       out=lambda _s: None)
    assert rc == 2


def test_unsupported_manifest_version_rejected(tmp_path: Path, f1b_authorized) -> None:
    m = _mlb_manifest(tmp_path)
    body = json.loads(m.read_text(encoding="utf-8"))
    body["manifest_format_version"] = "f1a-manifest-v999"
    m.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    rc = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=_scratch(tmp_path),
                       out=lambda _s: None)
    assert rc == 2


def test_duplicate_keys_manifest_rejected(tmp_path: Path, f1b_authorized) -> None:
    m = tmp_path / "dup.json"
    m.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    rc = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=_scratch(tmp_path),
                       out=lambda _s: None)
    assert rc == 2


def test_provider_mismatch_manifest_rejected(tmp_path: Path, f1b_authorized) -> None:
    # An MLB manifest supplied to an NBA pilot must fail.
    rc = run_pilot_cli(league="nba", manifest_path=_mlb_manifest(tmp_path),
                       scratch_db=_scratch(tmp_path), out=lambda _s: None)
    assert rc == 2


# --- guarded happy path + resume (mocked transport) ------------------------ #
def test_pilot_happy_path_and_completed_resume_zero_calls(tmp_path: Path, f1b_authorized) -> None:
    calls: list[int] = []
    db = _scratch(tmp_path)
    ckpt = tmp_path / "p.ckpt"
    m = _mlb_manifest(tmp_path)
    rc = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                       out=lambda _s: None, client_factory=_mlb_factory(calls))
    assert rc == 0
    assert sum(calls) >= 1
    calls2: list[int] = []
    rc2 = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                        resume=True, out=lambda _s: None, client_factory=_mlb_factory(calls2))
    assert rc2 == 0
    assert calls2 == []  # completed resume performs zero transport


def test_truncated_run_maps_to_budget_exit_code(tmp_path: Path, f1b_authorized, monkeypatch) -> None:
    from sports_quant.ingest import f1a as f1a_mod
    from sports_quant.ingest.pilot import PilotResult

    def fake_run_pilot(**_kw):  # type: ignore[no-untyped-def]
        return PilotResult(success=False, truncated=True, completed=1, skipped=0,
                           exhaustion={"limit_type": "request", "cap": 4},
                           usage={"attempted_requests": 4}, checkpoint_state="truncated",
                           network_occurred=True, database_mutated=True)

    monkeypatch.setattr(f1a_mod, "run_pilot", fake_run_pilot)
    rc = run_pilot_cli(league="mlb", manifest_path=_mlb_manifest(tmp_path),
                       scratch_db=_scratch(tmp_path), checkpoint=tmp_path / "p.ckpt",
                       out=lambda _s: None, client_factory=_mlb_factory([]))
    assert rc == EXIT_BUDGET_EXHAUSTED


# --- CLI dispatch: --pilot arg validation (before any work) ---------------- #
def test_cli_pilot_conflicting_args_rejected(tmp_path: Path, f1b_authorized) -> None:
    rc = main(["ingest-mlb", "--pilot", "--manifest", str(_mlb_manifest(tmp_path)),
               "--scratch-db", str(_scratch(tmp_path)), "--from", "2026-07-01"])
    assert rc == 2  # --from conflicts with the governing manifest


def test_cli_pilot_requires_manifest(tmp_path: Path, f1b_authorized) -> None:
    rc = main(["ingest-mlb", "--pilot", "--scratch-db", str(_scratch(tmp_path))])
    assert rc == 2
