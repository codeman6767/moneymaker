"""B1 request-addressable (per-game) resumability tests (offline; mocked MLB).

Drives the REAL per-game pilot executor through run_pilot_cli with a mocked MLB
transport returning 3 games, proving: completed per-game units are skipped with
zero transport on resume; an interrupted/failed unit is retried; a completed
resume performs zero transport; actual attempts never exceed the manifest cap;
and the logical-run budget is not silently refreshed on resume.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from sports_quant.db.init import initialize_database
from sports_quant.ingest.cost_policies import build_mlb_policy
from sports_quant.ingest.f1a import (
    _F1B_AUTHORIZED_ENV,
    EXIT_RUN_FAILED,
    emit_plan,
    run_pilot_cli,
)
from sports_quant.ingest.tests.test_phase_d2_mlb import game, schedule
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
from sports_quant.request_control import (
    BudgetExhausted,
    CreditBudget,
    RequestBudget,
    RequestGate,
    RequestUnit,
)


@pytest.fixture
def f1b_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_F1B_AUTHORIZED_ENV, "1")


def _scratch(tmp_path: Path) -> Path:
    p = tmp_path / "scratch.db"
    initialize_database(p)
    return p


def _rich_manifest(tmp_path: Path, max_games: int = 3) -> Path:
    out = tmp_path / "m.json"
    emit_plan(league="mlb", from_date="2024-04-09", to_date="2024-04-09",
              includes=("box",), max_games=max_games, manifest_out=out, out=lambda _s: None)
    return out


def _factory(box_pks: list[str], sched: list[dict], *, fail_sched_pk: int | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if "boxscore" in path:
            m = re.search(r"/game/(\d+)/boxscore", path)
            box_pks.append(m.group(1) if m else "?")
            return httpx.Response(200, json={"teams": {"home": {}, "away": {}}})
        if "/schedule" in path:
            sched.append(params)
            gpk = params.get("gamePk")
            if gpk:
                # A fatal per-game schedule failure -> the whole single-game ingest
                # fails -> its unit stays incomplete (retried on resume).
                if fail_sched_pk is not None and gpk == str(fail_sched_pk):
                    return httpx.Response(500, json={})
                return httpx.Response(200, json=schedule(
                    game(game_pk=int(gpk), official_date="2024-04-09")))
            return httpx.Response(200, json=schedule(
                game(game_pk=1, official_date="2024-04-09"),
                game(game_pk=2, official_date="2024-04-09"),
                game(game_pk=3, official_date="2024-04-09")))
        return httpx.Response(200, json={})

    def factory(gate: RequestGate) -> MlbStatsApiClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://mlb.invalid")
        return MlbStatsApiClient(client=http, gate=gate, league="mlb", max_retries=0)

    return factory


def test_per_game_happy_path(tmp_path: Path, f1b_authorized) -> None:
    box: list[str] = []
    sched: list[dict] = []
    db, ckpt, m = _scratch(tmp_path), tmp_path / "p.ckpt", _rich_manifest(tmp_path)
    rc = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                       out=lambda _s: None, client_factory=_factory(box, sched))
    assert rc == 0
    assert sorted(box) == ["1", "2", "3"]  # every selected game fetched once
    from sports_quant.ingest.checkpoint import load_checkpoint
    ck = load_checkpoint(ckpt)
    assert ck.state == "completed"
    assert len(ck.completed_identities) == 4  # skeleton + 3 games
    assert sorted(ck.stage_game_ids) == ["1", "2", "3"]


def test_interrupted_game_retried_on_resume(tmp_path: Path, f1b_authorized) -> None:
    box: list[str] = []
    sched: list[dict] = []
    db, ckpt, m = _scratch(tmp_path), tmp_path / "p.ckpt", _rich_manifest(tmp_path)
    # Game 2's single-game schedule fails -> its unit stays incomplete; game 1 durable.
    rc = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                       out=lambda _s: None, client_factory=_factory(box, sched, fail_sched_pk=2))
    assert rc == EXIT_RUN_FAILED
    from sports_quant.ingest.checkpoint import load_checkpoint
    ck = load_checkpoint(ckpt)
    assert ck.state == "failed"
    # skeleton + game 1 durable; game 2 (failed) and game 3 (not reached) not completed.
    assert len(ck.completed_identities) == 2

    # Resume with a healthy transport: skip skeleton + game 1, retry 2 and 3.
    box2: list[str] = []
    sched2: list[dict] = []
    rc2 = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                        resume=True, out=lambda _s: None,
                        client_factory=_factory(box2, sched2))
    assert rc2 == 0
    assert sorted(box2) == ["2", "3"]  # game 1 never re-fetched


def test_completed_resume_zero_transport(tmp_path: Path, f1b_authorized) -> None:
    box: list[str] = []
    sched: list[dict] = []
    db, ckpt, m = _scratch(tmp_path), tmp_path / "p.ckpt", _rich_manifest(tmp_path)
    run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                  out=lambda _s: None, client_factory=_factory(box, sched))
    box2: list[str] = []
    sched2: list[dict] = []
    rc = run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                       resume=True, out=lambda _s: None, client_factory=_factory(box2, sched2))
    assert rc == 0
    assert box2 == [] and sched2 == []  # completed resume: zero transport


def test_actual_requests_never_exceed_cap(tmp_path: Path, f1b_authorized) -> None:
    from sports_quant.ingest.manifest import load_and_validate
    box: list[str] = []
    sched: list[dict] = []
    db, ckpt, m = _scratch(tmp_path), tmp_path / "p.ckpt", _rich_manifest(tmp_path)
    run_pilot_cli(league="mlb", manifest_path=m, scratch_db=db, checkpoint=ckpt,
                  out=lambda _s: None, client_factory=_factory(box, sched))
    manifest = load_and_validate(m, expected_league="mlb", expected_provider="mlb_statsapi")
    actual = len(box) + len(sched)  # every transport GET the executor issued
    assert manifest.request_cap is not None and actual <= manifest.request_cap


# --- logical-run budget across resume (unit-level, deterministic) ---------- #
def test_seed_prior_does_not_grant_fresh_budget() -> None:
    gate = RequestGate(request_budget=RequestBudget(max_requests=6),
                       credit_budget=CreditBudget(applicable=False), cost_policy=build_mlb_policy())
    gate.seed_prior(prior_requests=5, prior_credits=0)  # prior process used 5 of 6
    unit = RequestUnit(provider="mlb_statsapi", league="mlb", endpoint_family="schedule")
    gate.reserve(unit)  # the 6th (last remaining) is allowed
    with pytest.raises(BudgetExhausted):
        gate.reserve(unit)  # a 7th would exceed the LOGICAL-run cap
    assert gate.usage.prior_requests == 5
    assert gate.usage.attempted_requests == 6  # prior(5) + current(1); no fresh budget
