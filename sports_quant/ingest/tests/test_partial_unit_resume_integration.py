"""Integrated partial-unit resume: the June game-824011 case, end to end.

The June 2026 MLB month run lost both roster requests for game 824011 when the
host resumed from suspension. The review repaired two things that this test holds
together through the real ingestor + gate + checkpoint stack:

1. a ``partially_failed`` unit stays INCOMPLETE and resumable (it used to be
   checkpointed as complete, hiding the gap behind a ``completed`` state); and
2. the logical run remembers the first process's failure after a later process
   succeeds (the checkpoint used to be overwritten with the resume's zeros).

The roster endpoint fails on the first process and succeeds on the resume, so the
missing family data is genuinely added later while the append-only rows already
persisted are not duplicated. Nothing sleeps for real and no socket is opened.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from sports_quant.db.init import initialize_database
from sports_quant.ingest.checkpoint import load_checkpoint
from sports_quant.ingest.f1a import emit_plan, run_pilot_cli
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
from sports_quant.request_control import RequestGate

PK = 824011
HOME, AWAY = 108, 133
DATE = "2026-06-28"


@pytest.fixture()
def f1b_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    monkeypatch.delenv("MONEYMAKER_ALLOW_UNGATED_INGEST", raising=False)


def _game() -> dict[str, Any]:
    from sports_quant.ingest.tests.test_phase_d2_mlb import game as mlb_game

    return mlb_game(game_pk=PK, official_date=DATE, home_team=HOME, away_team=AWAY)


def _factory(seen: list[str], roster_ok: Callable[[], bool]) -> Any:
    """Real client over a mock transport whose roster endpoint is switchable."""

    from sports_quant.ingest.tests.test_phase_d2_mlb import boxscore, linescore
    from sports_quant.ingest.tests.test_phase_d2_mlb import schedule as mlb_schedule

    def handler(request: httpx.Request) -> httpx.Response:
        p = re.sub(r"^/api/v1", "", request.url.path)
        seen.append(p)
        ct = {"content-type": "application/json"}
        if p.endswith("/schedule"):
            return httpx.Response(200, json=mlb_schedule(_game(), date=DATE),
                                  headers=ct)
        if p.endswith("/boxscore"):
            return httpx.Response(200, json=boxscore(home_team=HOME, away_team=AWAY),
                                  headers=ct)
        if p.endswith("/linescore"):
            return httpx.Response(200, json=linescore(), headers=ct)
        if "/roster" in p:
            if not roster_ok():
                # Exactly the June failure shape: a terminal server error after
                # the unit has already persisted its other families.
                return httpx.Response(503, json={"message": "unavailable"},
                                      headers=ct)
            return httpx.Response(200, json={"roster": [
                {"person": {"id": 660271, "fullName": "Test Player"},
                 "jerseyNumber": "17", "position": {"abbreviation": "P"},
                 "status": {"description": "Active"}}]}, headers=ct)
        return httpx.Response(404, json={}, headers=ct)

    def factory(gate: RequestGate) -> MlbStatsApiClient:
        return MlbStatsApiClient(
            gate=gate, league="mlb",
            client=httpx.AsyncClient(base_url="https://statsapi.mlb.com/api/v1",
                                     transport=httpx.MockTransport(handler)))

    return factory


def _plan(tmp_path: Path) -> Path:
    manifest = tmp_path / "m.json"
    lines: list[str] = []
    rc = emit_plan(league="mlb", from_date=DATE, to_date=DATE,
                   includes=("box", "inning", "results", "rosters"),
                   max_games=5, max_retries=0, rate_per_min=600,
                   scratch_db=str(tmp_path / "s.db"),
                   checkpoint=str(tmp_path / "s.ckpt"),
                   request_cap=200, out=lines.append, manifest_out=manifest)
    assert rc == 0, "\n".join(lines)
    return manifest


def _counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("game_schedule_snapshots", "game_result_snapshots",
                          "mlb_inning_lines", "team_game_statistics",
                          "player_game_statistics", "roster_snapshots",
                          "raw_responses")}
    finally:
        con.close()


def test_partial_unit_fails_resumes_and_keeps_both_outcomes(
    tmp_path: Path, f1b_authorized: None
) -> None:
    manifest = _plan(tmp_path)
    db, ckpt = tmp_path / "s.db", tmp_path / "s.ckpt"
    initialize_database(db)
    seen: list[str] = []
    roster_works = {"ok": False}

    # -- process 1: rosters fail terminally --------------------------------- #
    lines1: list[str] = []
    rc1 = run_pilot_cli(league="mlb", manifest_path=manifest, scratch_db=db,
                        checkpoint=ckpt, resume=False, as_json=True,
                        out=lines1.append,
                        client_factory=_factory(seen, lambda: roster_works["ok"]))
    p1 = json.loads([ln for ln in lines1 if ln.startswith("{")][-1])
    assert rc1 != 0, "a unit that lost a declared family must not report success"
    assert p1["failed"] is True
    ck1 = load_checkpoint(ckpt)
    # The unit is NOT complete, so the gap cannot hide behind a completed state.
    assert ck1.state == "failed"
    assert ck1.usage["failed_responses"] >= 1
    after1 = _counts(db)
    # The families that DID persist are durable even though the unit failed.
    assert after1["game_schedule_snapshots"] == 1
    assert after1["roster_snapshots"] == 0            # the missing family
    process_1_failures = ck1.usage["failed_responses"]
    process_1_successes = ck1.usage["successful_responses"]
    assert process_1_successes > 0

    # -- process 2: the provider recovers; the resume completes the unit ----- #
    roster_works["ok"] = True
    lines2: list[str] = []
    rc2 = run_pilot_cli(league="mlb", manifest_path=manifest, scratch_db=db,
                        checkpoint=ckpt, resume=True, as_json=True,
                        out=lines2.append,
                        client_factory=_factory(seen, lambda: roster_works["ok"]))
    p2 = json.loads([ln for ln in lines2 if ln.startswith("{")][-1])
    assert rc2 == 0, "\n".join(lines2)
    assert p2["success"] is True
    assert p2["performed_new_work"] is True
    ck2 = load_checkpoint(ckpt)
    assert ck2.state == "completed"

    # The missing family data was added on resume.
    after2 = _counts(db)
    assert after2["roster_snapshots"] > 0

    # Append-only rows already persisted were not duplicated by the retry.
    for table in ("game_schedule_snapshots", "game_result_snapshots",
                  "mlb_inning_lines", "team_game_statistics",
                  "player_game_statistics"):
        assert after2[table] == after1[table], f"{table} was persisted twice"

    # -- the logical run remembers BOTH outcomes ---------------------------- #
    assert len(ck2.process_usage) == 2
    assert ck2.usage["failed_responses"] == process_1_failures, (
        "a successful resume erased the earlier process's terminal failure")
    assert ck2.usage["successful_responses"] > process_1_successes
    assert ck2.prior_usage()["failed_responses"] == process_1_failures
    assert ck2.current_process_usage()["failed_responses"] == 0
    # Completion does not claim that every process individually succeeded.
    assert p2["prior_process_usage"]["failed_responses"] == process_1_failures
    assert p2["current_process_usage"]["failed_responses"] == 0

    # -- process 3: a completed resume is now a true no-op ------------------ #
    before = ckpt.read_bytes()
    seen_before = len(seen)
    lines3: list[str] = []
    rc3 = run_pilot_cli(league="mlb", manifest_path=manifest, scratch_db=db,
                        checkpoint=ckpt, resume=True, as_json=True,
                        out=lines3.append,
                        client_factory=_factory(seen, lambda: True))
    p3 = json.loads([ln for ln in lines3 if ln.startswith("{")][-1])
    assert rc3 == 0
    assert len(seen) == seen_before, "a no-work resume issued a request"
    assert ckpt.read_bytes() == before, "a no-work resume rewrote the checkpoint"
    assert p3["performed_new_work"] is False
    assert p3["checkpoint_mutated"] is False
    assert p3["usage"]["failed_responses"] == process_1_failures
    assert _counts(db) == after2
