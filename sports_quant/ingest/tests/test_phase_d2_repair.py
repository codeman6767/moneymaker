"""Phase D2 integrity repair: rosters, player references, active-failure status,
correction detection, complete dry-run counters, inning reconciliation, and
missing half-innings. Every HTTP interaction is mocked; no live call is made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.mlb_ingestor import ingest_mlb
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

# Reuse the sanitized fixture builders from the main D2 test module.
from .test_phase_d2_mlb import boxscore, game, linescore, schedule


@pytest.fixture
def db(tmp_path: Path) -> Database:
    p = tmp_path / "corpus.db"
    initialize_database(p)
    return Database(p)


def _count(db: Database, table: str, where: str = "") -> int:
    with db.connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def roster(*players: dict[str, Any]) -> dict[str, Any]:
    return {"roster": list(players)}


def rperson(pid: Optional[int], *, jersey: str = "9", pos: str = "P", status: str = "Active"):
    person: dict[str, Any] = {} if pid is None else {"id": pid, "fullName": f"P{pid}"}
    return {"person": person, "jerseyNumber": jersey, "position": {"abbreviation": pos},
            "status": {"code": "A", "description": status}}


def mlb_client(
    *,
    schedule_body: dict[str, Any],
    box_by_pk: Optional[dict[str, Any]] = None,
    line_by_pk: Optional[dict[str, Any]] = None,
    roster_by_team: Optional[dict[str, Any]] = None,
    fail_paths: Optional[set[str]] = None,
    seen: Optional[list[str]] = None,
    **kwargs,
) -> MlbStatsApiClient:
    box_by_pk = box_by_pk or {}
    line_by_pk = line_by_pk or {}
    roster_by_team = roster_by_team or {}
    fail_paths = fail_paths or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        if path in fail_paths:
            return httpx.Response(500, json={"e": 1}, headers={"content-type": "application/json"})
        if path == "/api/v1/schedule":
            body: dict[str, Any] = schedule_body
        elif path.endswith("/boxscore"):
            body = box_by_pk.get(path.split("/")[-2], {"teams": {}})
        elif path.endswith("/linescore"):
            body = line_by_pk.get(path.split("/")[-2], {"teams": {}, "innings": []})
        elif path.endswith("/roster"):
            body = roster_by_team.get(path.split("/")[-2], {"roster": []})
        else:
            body = {}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    return MlbStatsApiClient(client=http, **kwargs)


async def _ingest(db: Database, client: MlbStatsApiClient, **kwargs):
    try:
        return await ingest_mlb(database=db, client=client, **kwargs)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Roster ingestion (§11.1-5, §2)
# --------------------------------------------------------------------------- #
async def test_include_rosters_accepted_and_persisted(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, home_team=133, away_team=147)),
        roster_by_team={"133": roster(rperson(600), rperson(601)), "147": roster(rperson(700))},
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("rosters",))
    assert r.status == "succeeded"
    assert r.roster_requests == 2
    assert r.roster_observations_inserted == 3
    assert r.roster_players_received == 3
    assert _count(db, "roster_snapshots") == 3


async def test_roster_fetched_once_per_team_in_doubleheader(db: Database) -> None:
    seen: list[str] = []
    c = mlb_client(
        schedule_body=schedule(
            game(game_pk=1, game_number=1, doubleheader="S", home_team=133, away_team=147),
            game(game_pk=2, game_number=2, doubleheader="S", home_team=133, away_team=147),
        ),
        roster_by_team={"133": roster(rperson(600)), "147": roster(rperson(700))},
        seen=seen,
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("rosters",))
    # Two unique teams -> exactly two roster requests despite two games.
    assert r.roster_requests == 2
    assert sum(1 for p in seen if p.endswith("/roster")) == 2


async def test_roster_reingest_dedupes_and_change_appends(db: Database) -> None:
    body = schedule(game(game_pk=1, home_team=133, away_team=147))
    await _ingest(db, mlb_client(schedule_body=body,
                  roster_by_team={"133": roster(rperson(600)), "147": roster(rperson(700))}),
                  from_date="2024-04-09", includes=("rosters",))
    # Identical re-ingest -> no new rows.
    r2 = await _ingest(db, mlb_client(schedule_body=body,
                       roster_by_team={"133": roster(rperson(600)), "147": roster(rperson(700))}),
                       from_date="2024-04-09", includes=("rosters",))
    assert r2.roster_observations_inserted == 0
    assert r2.roster_observations_unchanged >= 1
    # A roster change (600 -> 602 on team 133) appends a new observation.
    r3 = await _ingest(db, mlb_client(schedule_body=body,
                       roster_by_team={"133": roster(rperson(602)), "147": roster(rperson(700))}),
                       from_date="2024-04-09", includes=("rosters",))
    assert r3.roster_observations_inserted >= 1


async def test_roster_missing_player_id_rejected_not_fabricated(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, home_team=133, away_team=147)),
        roster_by_team={"133": roster(rperson(600), rperson(None)), "147": roster()},
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("rosters",))
    assert r.roster_records_rejected == 1  # the id-less player
    assert r.roster_observations_inserted == 1  # only the valid one


async def test_roster_failure_does_not_fabricate_empty_roster(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, home_team=133, away_team=147)),
        roster_by_team={"147": roster(rperson(700))},
        fail_paths={"/api/v1/teams/133/roster"}, max_retries=0,
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("rosters",))
    assert r.status == "partially_failed"  # a requested roster fetch failed
    assert r.has_active_failure
    # Team 133's roster was NOT fabricated as empty; only team 147 persisted.
    assert _count(db, "roster_snapshots") == 1


# --------------------------------------------------------------------------- #
# Provider player references (§11.6-9, §3)
# --------------------------------------------------------------------------- #
async def test_box_players_create_provider_references(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box",))
    with db.connection() as conn:
        refs = {r[0]: r[1] for r in conn.execute(
            "SELECT provider_player_id, first_raw_response_id FROM provider_player_references")}
        assert set(refs) == {"111", "500"}  # box players from the boxscore fixture
        # Their provenance is the boxscore response, not the schedule.
        for raw_id in refs.values():
            endpoint = conn.execute(
                "SELECT endpoint FROM raw_responses WHERE raw_response_id=?", (raw_id,)
            ).fetchone()[0]
            assert endpoint.endswith("/boxscore")


async def test_probable_and_lineup_players_create_references(db: Database) -> None:
    c = mlb_client(schedule_body=schedule(
        game(game_pk=1, home_probable=900, lineups={"home": [111, 112]})))
    await _ingest(db, c, from_date="2024-04-09", includes=("probables", "lineups"))
    with db.connection() as conn:
        refs = {r[0]: r[1] for r in conn.execute(
            "SELECT provider_player_id, first_raw_response_id FROM provider_player_references")}
        assert {"900", "111", "112"} <= set(refs)
        # Probable + lineup player provenance is the schedule response.
        for raw_id in refs.values():
            endpoint = conn.execute(
                "SELECT endpoint FROM raw_responses WHERE raw_response_id=?", (raw_id,)
            ).fetchone()[0]
            assert endpoint == "/schedule"


async def test_roster_players_create_references_with_roster_provenance(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, home_team=133, away_team=147)),
        roster_by_team={"133": roster(rperson(600)), "147": roster(rperson(700))},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("rosters",))
    with db.connection() as conn:
        for pid in ("600", "700"):
            raw_id = conn.execute(
                "SELECT first_raw_response_id FROM provider_player_references "
                "WHERE provider_player_id=?", (pid,)).fetchone()[0]
            endpoint = conn.execute(
                "SELECT endpoint FROM raw_responses WHERE raw_response_id=?", (raw_id,)
            ).fetchone()[0]
            assert endpoint.endswith("/roster")


async def test_duplicate_player_reuses_same_reference(db: Database) -> None:
    # Player 600 appears in both the roster and (as 111/500 differ) -- re-ingest
    # the same roster twice; the provider reference is reused, not duplicated.
    body = schedule(game(game_pk=1, home_team=133, away_team=147))
    rb = {"133": roster(rperson(600)), "147": roster(rperson(700))}
    await _ingest(db, mlb_client(schedule_body=body, roster_by_team=rb),
                  from_date="2024-04-09", includes=("rosters",))
    await _ingest(db, mlb_client(schedule_body=body, roster_by_team=rb),
                  from_date="2024-04-09", includes=("rosters",))
    assert _count(db, "provider_player_references", "WHERE provider_player_id='600'") == 1


async def test_provider_player_id_never_becomes_canonical(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()},
        roster_by_team={"133": roster(rperson(600))},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box", "rosters"))
    with db.connection() as conn:
        # No canonical players created; every reference's canonical id is NULL.
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
        canon = {r[0] for r in conn.execute("SELECT player_id FROM provider_player_references")}
        assert canon == {None}


# --------------------------------------------------------------------------- #
# Active-failure status + exit codes (§11.10-13, §4)
# --------------------------------------------------------------------------- #
async def test_failed_box_fetch_is_partially_failed(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        line_by_pk={"1": linescore()}, fail_paths={"/api/v1/game/1/boxscore"}, max_retries=0,
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("box", "results"))
    assert r.status == "partially_failed"
    assert r.needs_failure_exit is True
    # Schedule + results still persisted; box stats did not.
    assert _count(db, "game_schedule_snapshots") == 1
    assert _count(db, "team_game_statistics") == 0


async def test_failed_linescore_fetch_is_partially_failed(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        fail_paths={"/api/v1/game/1/linescore"}, max_retries=0,
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("results",))
    assert r.status == "partially_failed" and r.needs_failure_exit


async def test_missing_optional_fields_is_not_active_failure(db: Database) -> None:
    # A valid response that simply lacks probables/lineups is NOT a failure.
    c = mlb_client(schedule_body=schedule(game(game_pk=1)))  # no probables/lineups
    r = await _ingest(db, c, from_date="2024-04-09", includes=("probables", "lineups"))
    assert r.status == "succeeded"
    assert r.active_failures == 0
    assert r.needs_failure_exit is False


async def test_malformed_data_rejection_is_not_active_failure(db: Database) -> None:
    # A malformed game (missing gamePk) in an otherwise valid response is a
    # data-quality rejection (exit 0), not an active provider failure.
    body = {"dates": [{"date": "2024-04-09", "games": [{"gameType": "R"}]}]}  # no gamePk
    c = mlb_client(schedule_body=body)
    r = await _ingest(db, c, from_date="2024-04-09")
    assert r.records_rejected == 1
    assert r.active_failures == 0
    assert r.status == "succeeded"


# --------------------------------------------------------------------------- #
# Correction detection (§11.14-16, §5)
# --------------------------------------------------------------------------- #
async def test_corrected_result_sets_is_correction_and_counter(db: Database) -> None:
    g = game(game_pk=1, status="Final", coded="F", abstract="Final")
    await _ingest(db, mlb_client(schedule_body=schedule(g),
                  line_by_pk={"1": linescore(home_runs=5, away_runs=3)}),
                  from_date="2024-04-09", includes=("results",))
    r2 = await _ingest(db, mlb_client(schedule_body=schedule(g),
                       line_by_pk={"1": linescore(home_runs=6, away_runs=3)}),
                       from_date="2024-04-09", includes=("results",))
    assert r2.corrections_appended == 1
    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM game_result_snapshots WHERE is_correction=1").fetchone()[0] == 1
        # The first (original) observation is immutable and not a correction.
        assert conn.execute(
            "SELECT COUNT(*) FROM game_result_snapshots WHERE is_correction=0").fetchone()[0] == 1


async def test_first_result_and_identical_replay_are_not_corrections(db: Database) -> None:
    g = game(game_pk=1, status="Final", coded="F", abstract="Final")
    line = linescore(home_runs=5, away_runs=3)
    r1 = await _ingest(db, mlb_client(schedule_body=schedule(g), line_by_pk={"1": line}),
                       from_date="2024-04-09", includes=("results",))
    assert r1.corrections_appended == 0  # first observation
    r2 = await _ingest(db, mlb_client(schedule_body=schedule(g), line_by_pk={"1": line}),
                       from_date="2024-04-09", includes=("results",))
    assert r2.corrections_appended == 0  # identical replay -> no new row, no correction
    assert _count(db, "game_result_snapshots") == 1


async def test_a_b_a_result_transitions_retained(db: Database) -> None:
    g = game(game_pk=1, status="Final", coded="F", abstract="Final")
    for hr in (5, 6, 5):  # A -> B -> A
        await _ingest(db, mlb_client(schedule_body=schedule(g),
                      line_by_pk={"1": linescore(home_runs=hr, away_runs=3)}),
                      from_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots") == 3


# --------------------------------------------------------------------------- #
# Dry-run truthful counters (§11.17-18, §6)
# --------------------------------------------------------------------------- #
def _snapshot(db: Database) -> dict[str, int]:
    tables = ["ingestion_runs", "raw_responses", "provider_game_references",
              "provider_team_references", "provider_player_references",
              "game_schedule_snapshots", "game_result_snapshots", "mlb_inning_lines",
              "team_game_statistics", "player_game_statistics", "roster_snapshots",
              "probable_pitcher_snapshots", "lineup_snapshots", "lineup_players",
              "data_quality_issues"]
    with db.connection() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


async def test_dry_run_reports_full_counts_and_persists_nothing(db: Database) -> None:
    before = _snapshot(db)
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final",
                                    home_probable=900, home_team=133, away_team=147)),
        box_by_pk={"1": boxscore()}, line_by_pk={"1": linescore()},
        roster_by_team={"133": roster(rperson(600)), "147": roster(rperson(700))},
    )
    r = await _ingest(db, c, from_date="2024-04-09",
                      includes=("results", "box", "inning", "probables", "rosters"), dry_run=True)
    # Would-be counts for successfully-parsed responses are NOT left at zero.
    assert r.result_snapshots_inserted == 1
    assert r.inning_lines_inserted == 4
    assert r.team_statistics_inserted == 2
    assert r.player_statistics_inserted >= 1
    assert r.roster_observations_inserted == 2
    assert r.provider_references_created > 0
    assert r.run_id is None
    # Absolutely nothing persisted.
    assert _snapshot(db) == before


# --------------------------------------------------------------------------- #
# Inning reconciliation + missing half-innings (§11.19-24, §7-8)
# --------------------------------------------------------------------------- #
async def test_consistent_scores_are_not_flagged(db: Database) -> None:
    innings = [{"num": 1, "home": {"runs": 2}, "away": {"runs": 1}},
               {"num": 2, "home": {"runs": 3}, "away": {"runs": 2}}]  # home 5, away 3
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        line_by_pk={"1": linescore(innings=innings, home_runs=5, away_runs=3, current_inning=2)},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("results", "inning"))
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-MLB-RECON-001'") == 0


async def test_omitted_bottom_half_not_fabricated(db: Database) -> None:
    # Home team does not bat in the bottom of the 9th (no 'home' object).
    innings = [{"num": 8, "home": {"runs": 1}, "away": {"runs": 0}},
               {"num": 9, "away": {"runs": 0}}]  # missing home half
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        line_by_pk={"1": linescore(innings=innings, current_inning=9)},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("inning",))
    assert _count(db, "mlb_inning_lines", "WHERE inning=9 AND side='home'") == 0
    assert _count(db, "mlb_inning_lines", "WHERE inning=9 AND side='away'") == 1


async def test_explicit_zero_half_is_stored_as_zero(db: Database) -> None:
    innings = [{"num": 1, "home": {"runs": 0, "hits": 0, "errors": 0},
                "away": {"runs": 1}}]
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="In Progress", coded="I", abstract="Live")),
        line_by_pk={"1": linescore(innings=innings, current_inning=1)},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("inning",))
    with db.connection() as conn:
        runs = conn.execute(
            "SELECT runs FROM mlb_inning_lines WHERE inning=1 AND side='home'").fetchone()[0]
        assert runs == 0  # explicit zero, not a missing half


async def test_suspended_and_incomplete_games_representable(db: Database) -> None:
    c = mlb_client(schedule_body=schedule(
        game(game_pk=1, status="Suspended", coded="U", abstract="Live"),
        game(game_pk=2, status="In Progress", coded="I", abstract="Live"),
    ))
    await _ingest(db, c, from_date="2024-04-09")
    with db.connection() as conn:
        states = dict(conn.execute(
            "SELECT provider_game_id, mapped_status FROM game_schedule_snapshots"))
    assert states["1"] == "suspended" and states["2"] == "in_progress"


# --------------------------------------------------------------------------- #
# Transaction + provenance integrity (§9)
# --------------------------------------------------------------------------- #
async def test_failed_box_leaves_no_player_refs_from_that_response(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        fail_paths={"/api/v1/game/1/boxscore"}, max_retries=0,
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box",))
    # No player stats and no player references from the failed box response.
    assert _count(db, "player_game_statistics") == 0
    assert _count(db, "provider_player_references") == 0


async def test_raw_responses_remain_for_audit_and_no_orphan_provenance(db: Database) -> None:
    c = mlb_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final",
                                    home_probable=900)),
        box_by_pk={"1": boxscore()}, line_by_pk={"1": linescore()},
        roster_by_team={"133": roster(rperson(600))},
    )
    await _ingest(db, c, from_date="2024-04-09",
                  includes=("results", "box", "inning", "probables", "rosters"))
    with db.connection() as conn:
        # Every normalized row links to a real raw response (no orphan provenance).
        for table in ("game_schedule_snapshots", "game_result_snapshots", "mlb_inning_lines",
                      "team_game_statistics", "player_game_statistics", "roster_snapshots",
                      "probable_pitcher_snapshots"):
            orphan = conn.execute(
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN raw_responses r "
                "ON t.raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
            ).fetchone()[0]
            assert orphan == 0, f"{table} has orphan provenance"
        # Box stats never carry the schedule response's id.
        sched_raw = conn.execute(
            "SELECT raw_response_id FROM game_schedule_snapshots").fetchone()[0]
        box_raws = {r[0] for r in conn.execute(
            "SELECT raw_response_id FROM player_game_statistics")}
        assert sched_raw not in box_raws


async def test_one_failed_game_does_not_corrupt_another(db: Database) -> None:
    # Two games; the first game's linescore 500s, the second's is fine.
    c = mlb_client(
        schedule_body=schedule(
            game(game_pk=1, status="Final", coded="F", abstract="Final"),
            game(game_pk=2, status="Final", coded="F", abstract="Final"),
        ),
        line_by_pk={"2": linescore()},
        fail_paths={"/api/v1/game/1/linescore"}, max_retries=0,
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("results",))
    assert r.status == "partially_failed"
    # Game 2's result persisted; both schedule snapshots persisted.
    assert _count(db, "game_schedule_snapshots") == 2
    assert _count(db, "game_result_snapshots") == 1


def test_no_live_network_and_gateway_isolation() -> None:
    text = (Path(__file__).resolve().parents[1] / "mlb_ingestor.py").read_text(encoding="utf-8")
    assert "import gateway" not in text and "from gateway" not in text


def test_d3_d5_remain_unimplemented() -> None:
    import importlib

    for mod in ("nba_ingestor", "weather_ingestor", "hoopr_import"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"sports_quant.ingest.{mod}")


# Keep the imported builders referenced so linters see them as used.
_FIXTURE_BUILDERS: tuple[Callable[..., Any], ...] = (game, schedule, boxscore, linescore)
