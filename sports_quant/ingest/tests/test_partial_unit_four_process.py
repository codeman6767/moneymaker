"""Four-process partial-unit recovery through the real runner and ingestors.

The June 2026 shape, driven end to end against mocked transports for both leagues:

1. process 1 persists some families and loses one terminally;
2. process 2 resumes and fails again on the same family;
3. process 3 resumes and succeeds, adding only the missing data;
4. process 4 is a completed no-work resume and changes no byte.

Across all four the logical run must retain every failure, retry and success, the
unit must end up in ``recovered_identities``, no append-only observation may be
duplicated, and current-process reporting must stay distinct from the totals.

Offline only: the transport is an ``httpx.MockTransport`` and nothing sleeps.
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
from sports_quant.request_control import RequestGate
from sports_quant.usage_provenance import PROCESS_ID_KEY

MLB_PK, MLB_HOME, MLB_AWAY, MLB_DATE = 824011, 108, 133, "2026-06-28"
NBA_ID, NBA_DATE = 18400123, "2026-03-11"


@pytest.fixture()
def f1b_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    monkeypatch.delenv("MONEYMAKER_ALLOW_UNGATED_INGEST", raising=False)


# --------------------------------------------------------------------------- #
# MLB
# --------------------------------------------------------------------------- #
def _mlb_factory(seen: list[str], roster_ok: Callable[[], bool]) -> Any:
    from sports_quant.ingest.tests.test_phase_d2_mlb import boxscore, linescore
    from sports_quant.ingest.tests.test_phase_d2_mlb import game as mlb_game
    from sports_quant.ingest.tests.test_phase_d2_mlb import schedule as mlb_schedule
    from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

    def handler(request: httpx.Request) -> httpx.Response:
        path = re.sub(r"^/api/v1", "", request.url.path)
        seen.append(path)
        ct = {"content-type": "application/json"}
        if path.endswith("/schedule"):
            one = mlb_game(game_pk=MLB_PK, official_date=MLB_DATE,
                           home_team=MLB_HOME, away_team=MLB_AWAY)
            return httpx.Response(200, json=mlb_schedule(one, date=MLB_DATE),
                                  headers=ct)
        if path.endswith("/boxscore"):
            return httpx.Response(200, json=boxscore(home_team=MLB_HOME,
                                                     away_team=MLB_AWAY), headers=ct)
        if path.endswith("/linescore"):
            return httpx.Response(200, json=linescore(), headers=ct)
        if "/roster" in path:
            if not roster_ok():
                return httpx.Response(503, json={"message": "unavailable"}, headers=ct)
            return httpx.Response(200, json={"roster": [
                {"person": {"id": 660271, "fullName": "Test Player"},
                 "jerseyNumber": "17", "position": {"abbreviation": "P"},
                 "status": {"description": "Active"}}]}, headers=ct)
        return httpx.Response(404, json={}, headers=ct)

    def factory(gate: RequestGate) -> Any:
        return MlbStatsApiClient(
            gate=gate, league="mlb",
            client=httpx.AsyncClient(base_url="https://statsapi.mlb.com/api/v1",
                                     transport=httpx.MockTransport(handler)))

    return factory


def _mlb_plan(tmp_path: Path) -> Path:
    manifest = tmp_path / "m.json"
    lines: list[str] = []
    rc = emit_plan(league="mlb", from_date=MLB_DATE, to_date=MLB_DATE,
                   includes=("box", "inning", "results", "rosters"),
                   max_games=5, max_retries=0, rate_per_min=600,
                   scratch_db=str(tmp_path / "s.db"),
                   checkpoint=str(tmp_path / "s.ckpt"), request_cap=200,
                   out=lines.append, manifest_out=manifest)
    assert rc == 0, "\n".join(lines)
    return manifest


def _counts(db: Path, tables: tuple[str, ...]) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables}
    finally:
        con.close()


MLB_TABLES = ("game_schedule_snapshots", "game_result_snapshots", "mlb_inning_lines",
              "team_game_statistics", "player_game_statistics", "roster_snapshots",
              "raw_responses")


def _payload(lines: list[str]) -> dict[str, Any]:
    return json.loads([ln for ln in lines if ln.startswith("{")][-1])


def test_mlb_four_process_partial_unit_recovery(tmp_path: Path,
                                               f1b_authorized: None) -> None:
    manifest = _mlb_plan(tmp_path)
    db, ckpt = tmp_path / "s.db", tmp_path / "s.ckpt"
    initialize_database(db)
    seen: list[str] = []
    roster = {"ok": False}

    def run(resume: bool) -> dict[str, Any]:
        lines: list[str] = []
        run_pilot_cli(league="mlb", manifest_path=manifest, scratch_db=db,
                      checkpoint=ckpt, resume=resume, as_json=True,
                      out=lines.append,
                      client_factory=_mlb_factory(seen, lambda: roster["ok"]))
        return _payload(lines)

    # -- 1. some families persist, the roster family is lost terminally ------ #
    p1 = run(False)
    assert p1["failed"] is True
    ck1 = load_checkpoint(ckpt)
    assert ck1.state == "failed", "a short unit must not be checkpointed complete"
    after1 = _counts(db, MLB_TABLES)
    assert after1["game_schedule_snapshots"] == 1     # persisted families survive
    assert after1["roster_snapshots"] == 0            # the lost family
    fails1 = ck1.usage["failed_responses"]
    succ1 = ck1.usage["successful_responses"]
    assert fails1 >= 1 and succ1 > 0
    assert len(ck1.process_usage) == 1

    # -- 2. a resume that fails again on the same family -------------------- #
    p2 = run(True)
    assert p2["failed"] is True
    ck2 = load_checkpoint(ckpt)
    assert ck2.state == "failed"
    assert len(ck2.process_usage) == 2
    fails2 = ck2.usage["failed_responses"]
    assert fails2 == fails1 + ck2.current_process_usage()["failed_responses"]
    assert fails2 > fails1, "the second failure did not add"
    assert ck2.prior_usage()["failed_responses"] == fails1
    # No append-only observation was duplicated by the retry.
    for table in MLB_TABLES[:-1]:
        assert _counts(db, MLB_TABLES)[table] == after1[table], table

    # -- 3. the provider recovers; only missing data is added --------------- #
    roster["ok"] = True
    p3 = run(True)
    assert p3["success"] is True
    assert p3["performed_new_work"] is True
    ck3 = load_checkpoint(ckpt)
    assert ck3.state == "completed"
    assert len(ck3.process_usage) == 3
    after3 = _counts(db, MLB_TABLES)
    assert after3["roster_snapshots"] > 0             # the missing family arrived
    for table in ("game_schedule_snapshots", "game_result_snapshots",
                  "mlb_inning_lines", "team_game_statistics",
                  "player_game_statistics"):
        assert after3[table] == after1[table], f"{table} was persisted twice"
    # Every earlier failure and retry is still there.
    assert ck3.usage["failed_responses"] == fails2
    assert ck3.usage["successful_responses"] > succ1
    assert ck3.current_process_usage()["failed_responses"] == 0
    assert p3["prior_process_usage"]["failed_responses"] == fails2
    # The unit is recorded as recovered, not as a first-time completion.
    game_unit = [i for i in ck3.completed_identities if str(MLB_PK) in i]
    assert game_unit, "the game unit is not in the completed set"
    assert sorted(ck3.recovered_identities) == sorted(game_unit)
    assert p3["recovered_count"] == 1
    assert p3["initially_completed"] == len(ck3.completed_identities) - 1
    assert p3["unresolved_count"] == 0
    ids = [e[PROCESS_ID_KEY] for e in ck3.process_usage]
    assert len(set(ids)) == 3

    # -- 4. a completed no-work resume changes no byte ---------------------- #
    before = ckpt.read_bytes()
    seen_before = len(seen)
    p4 = run(True)
    assert p4["performed_new_work"] is False
    assert p4["checkpoint_mutated"] is False
    assert ckpt.read_bytes() == before
    assert len(seen) == seen_before, "a no-work resume issued a request"
    assert _counts(db, MLB_TABLES) == after3
    assert p4["usage"]["failed_responses"] == fails2
    assert p4["current_process_usage"] == {}
    assert p4["recovered_count"] == 1
    assert load_checkpoint(ckpt).usage == ck3.usage


# --------------------------------------------------------------------------- #
# NBA
# --------------------------------------------------------------------------- #
def _nba_factory(seen: list[str], stats_ok: Callable[[], bool]) -> Any:
    from sports_quant.providers.balldontlie import BalldontlieClient

    game = {
        "id": NBA_ID, "date": NBA_DATE, "datetime": f"{NBA_DATE}T23:30:00.000Z",
        "season": 2025, "postseason": False, "status": "Final", "period": 4,
        "time": None, "home_team_score": 101, "visitor_team_score": 99,
        "home_team": {"id": 2, "abbreviation": "BOS", "city": "Boston",
                      "name": "Celtics", "full_name": "Boston Celtics",
                      "conference": "East", "division": "Atlantic"},
        "visitor_team": {"id": 20, "abbreviation": "NYK", "city": "New York",
                         "name": "Knicks", "full_name": "New York Knicks",
                         "conference": "East", "division": "Atlantic"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        ct = {"content-type": "application/json"}
        if path.endswith("/games") or "/games/" in path:
            body = ({"data": game} if "/games/" in path
                    else {"data": [game], "meta": {"next_cursor": None}})
            return httpx.Response(200, json=body, headers=ct)
        if path.endswith("/stats"):
            if not stats_ok():
                return httpx.Response(503, json={"message": "unavailable"},
                                      headers=ct)
            # Reuse the shapes the working NBA pilot fixtures use, so this test
            # exercises real normalization rather than an invented payload.
            from sports_quant.ingest.tests.test_f1_month_pilots import (
                _nba_player,
                _nba_team,
            )
            return httpx.Response(200, json={"data": [
                {"player": _nba_player(600001, 2), "team": _nba_team(2),
                 "game": {"id": NBA_ID}, "pts": 20, "min": "32"}],
                "meta": {}}, headers=ct)
        return httpx.Response(200, json={"data": [], "meta": {"next_cursor": None}},
                              headers=ct)

    def factory(gate: RequestGate) -> Any:
        return BalldontlieClient(
            "test-key", gate=gate, league="nba",
            client=httpx.AsyncClient(base_url="https://api.balldontlie.io",
                                     transport=httpx.MockTransport(handler)))

    return factory


#: The stats family''s observable evidence with this mock: a successful /v1/stats
#: response introduces a provider player reference, so its absence/presence is the
#: "missing family data" signal this test drives.
NBA_TABLES = ("game_schedule_snapshots", "provider_player_references",
              "raw_responses")


def test_nba_four_process_partial_unit_recovery(tmp_path: Path,
                                               f1b_authorized: None) -> None:
    manifest = tmp_path / "m.json"
    lines: list[str] = []
    rc = emit_plan(league="nba", from_date=NBA_DATE, to_date=NBA_DATE,
                   includes=("stats",), max_games=2, max_pages=2, max_records=50,
                   max_retries=0, rate_per_min=600,
                   scratch_db=str(tmp_path / "s.db"),
                   checkpoint=str(tmp_path / "s.ckpt"), request_cap=200,
                   out=lines.append, manifest_out=manifest)
    assert rc == 0, "\n".join(lines)
    db, ckpt = tmp_path / "s.db", tmp_path / "s.ckpt"
    initialize_database(db)
    seen: list[str] = []
    stats = {"ok": False}

    def run(resume: bool) -> dict[str, Any]:
        out: list[str] = []
        run_pilot_cli(league="nba", manifest_path=manifest, scratch_db=db,
                      checkpoint=ckpt, resume=resume, as_json=True, out=out.append,
                      client_factory=_nba_factory(seen, lambda: stats["ok"]))
        return _payload(out)

    p1 = run(False)
    ck1 = load_checkpoint(ckpt)
    assert p1["failed"] is True and ck1.state == "failed"
    after1 = _counts(db, NBA_TABLES)
    assert after1["game_schedule_snapshots"] >= 1
    assert after1["provider_player_references"] == 0
    fails1 = ck1.usage["failed_responses"]
    assert fails1 >= 1

    p2 = run(True)
    ck2 = load_checkpoint(ckpt)
    assert p2["failed"] is True
    assert len(ck2.process_usage) == 2
    assert ck2.usage["failed_responses"] > fails1
    assert ck2.prior_usage()["failed_responses"] == fails1
    fails2 = ck2.usage["failed_responses"]

    stats["ok"] = True
    p3 = run(True)
    ck3 = load_checkpoint(ckpt)
    assert p3["success"] is True and ck3.state == "completed"
    assert len(ck3.process_usage) == 3
    after3 = _counts(db, NBA_TABLES)
    assert after3["provider_player_references"] > 0
    assert after3["game_schedule_snapshots"] == after1["game_schedule_snapshots"]
    assert ck3.usage["failed_responses"] == fails2
    assert ck3.current_process_usage()["failed_responses"] == 0
    assert p3["recovered_count"] >= 1
    assert ck3.recovered_identities

    before = ckpt.read_bytes()
    seen_before = len(seen)
    p4 = run(True)
    assert p4["performed_new_work"] is False
    assert p4["checkpoint_mutated"] is False
    assert ckpt.read_bytes() == before
    assert len(seen) == seen_before
    assert p4["usage"]["failed_responses"] == fails2
    assert _counts(db, NBA_TABLES) == after3

