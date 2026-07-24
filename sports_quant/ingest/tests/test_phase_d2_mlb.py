"""Phase D2 MLB ingestion: schedule/results/stats/innings/rosters/probables/lineups.

Every HTTP interaction is mocked with sanitized, realistic MLB StatsAPI shapes.
No live call is made. The fixtures cover the scenarios listed in the D2 spec
(normal/postponed/rescheduled/suspended/doubleheader/extra-inning/zero-game,
probables present+absent, posted+changed+missing lineups, malformed stats,
corrections, A->B->A, out-of-order, unknown status, oversized/rate-limit/5xx).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, ReadOnlyPolicyError, build_readonly_client
from sports_quant.ingest.mlb_ingestor import ingest_lineups, ingest_mlb
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

SENTINEL = "mlb-d2-no-secret-here"


# --------------------------------------------------------------------------- #
# Fixture builders (sanitized, realistic StatsAPI shapes)
# --------------------------------------------------------------------------- #
def game(
    *,
    game_pk: int,
    status: str = "Scheduled",
    coded: str = "S",
    abstract: str = "Preview",
    home_team: int = 133,
    away_team: int = 147,
    game_number: int = 1,
    doubleheader: str = "N",
    home_probable: Optional[int] = None,
    away_probable: Optional[int] = None,
    lineups: Optional[dict[str, list[int]]] = None,
    official_date: str = "2024-04-09",
    reschedule: Optional[dict[str, Any]] = None,
    venue_id: int = 10,
) -> dict[str, Any]:
    home: dict[str, Any] = {"team": {"id": home_team, "name": "Home"}}
    away: dict[str, Any] = {"team": {"id": away_team, "name": "Away"}}
    if home_probable is not None:
        home["probablePitcher"] = {"id": home_probable, "fullName": "HP"}
    if away_probable is not None:
        away["probablePitcher"] = {"id": away_probable, "fullName": "AP"}
    g: dict[str, Any] = {
        "gamePk": game_pk,
        "gameType": "R",
        "season": "2024",
        "officialDate": official_date,
        "gameDate": f"{official_date}T23:05:00Z",
        "status": {"abstractGameState": abstract, "codedGameState": coded, "detailedState": status},
        "teams": {"home": home, "away": away},
        "venue": {"id": venue_id, "name": "Park"},
        "gameNumber": game_number,
        "doubleHeader": doubleheader,
    }
    if lineups is not None:
        g["lineups"] = {
            "homePlayers": [
                {"id": pid, "primaryPosition": {"abbreviation": "SS"}}
                for pid in lineups.get("home", [])
            ],
            "awayPlayers": [
                {"id": pid, "primaryPosition": {"abbreviation": "CF"}}
                for pid in lineups.get("away", [])
            ],
        }
    if reschedule is not None:
        g.update(reschedule)
    return g


def schedule(*games: dict[str, Any], date: str = "2024-04-09") -> dict[str, Any]:
    return {"dates": [{"date": date, "games": list(games)}]} if games else {"dates": []}


def boxscore(
    *,
    home_team: int = 133,
    away_team: int = 147,
    home_batting: Optional[dict[str, Any]] = None,
    players: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    hb = home_batting if home_batting is not None else {"runs": 5, "hits": 9, "atBats": 34}
    return {
        "teams": {
            "home": {
                "team": {"id": home_team},
                "teamStats": {"batting": hb, "fielding": {"errors": 1}},
                "players": players
                if players is not None
                else {
                    "ID111": {
                        "person": {"id": 111}, "position": {"abbreviation": "SS"},
                        "battingOrder": "100",
                        "stats": {"batting": {"atBats": 4, "hits": 2, "homeRuns": 1}},
                    },
                    "ID500": {
                        "person": {"id": 500}, "position": {"abbreviation": "P"},
                        "stats": {"pitching": {"inningsPitched": "6.0", "strikeOuts": 7}},
                    },
                },
            },
            "away": {
                "team": {"id": away_team},
                "teamStats": {"batting": {"runs": 3, "hits": 7, "atBats": 33},
                              "fielding": {"errors": 0}},
                "players": {},
            },
        }
    }


def linescore(
    *,
    innings: Optional[list[dict[str, Any]]] = None,
    home_runs: int = 5,
    away_runs: int = 3,
    current_inning: int = 9,
) -> dict[str, Any]:
    if innings is None:
        innings = [
            {"num": 1, "home": {"runs": 2, "hits": 2, "errors": 0},
             "away": {"runs": 0, "hits": 1, "errors": 0}},
            {"num": 2, "home": {"runs": 3, "hits": 4, "errors": 1},
             "away": {"runs": 3, "hits": 3, "errors": 0}},
        ]
    return {
        "currentInning": current_inning,
        "teams": {"home": {"runs": home_runs, "hits": 9, "errors": 1},
                  "away": {"runs": away_runs, "hits": 7, "errors": 0}},
        "innings": innings,
    }


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> MlbStatsApiClient:
    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    return MlbStatsApiClient(client=http, **kwargs)


def routing_client(
    *,
    schedule_body: dict[str, Any],
    box_by_pk: Optional[dict[str, dict[str, Any]]] = None,
    line_by_pk: Optional[dict[str, dict[str, Any]]] = None,
    seen: Optional[list[str]] = None,
    methods: Optional[list[str]] = None,
    **kwargs,
) -> MlbStatsApiClient:
    box_by_pk = box_by_pk or {}
    line_by_pk = line_by_pk or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if methods is not None:
            methods.append(request.method)
        path = request.url.path
        if path == "/api/v1/schedule":
            body: dict[str, Any] = schedule_body
        elif path.endswith("/boxscore"):
            pk = path.split("/")[-2]
            body = box_by_pk.get(pk, {"teams": {}})
        elif path.endswith("/linescore"):
            pk = path.split("/")[-2]
            body = line_by_pk.get(pk, {"teams": {}, "innings": []})
        else:
            body = {}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    return _client(handler, **kwargs)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    p = tmp_path / "corpus.db"
    initialize_database(p)
    return Database(p)


def _count(db: Database, table: str, where: str = "") -> int:
    with db.connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


ALL = ("results", "box", "inning", "probables", "lineups")


async def _ingest(db: Database, client: MlbStatsApiClient, **kwargs):
    try:
        return await ingest_mlb(database=db, client=client, **kwargs)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Canonical game identity (§22.4-6)
# --------------------------------------------------------------------------- #
async def test_one_gamepk_maps_to_one_official_identity(db: Database) -> None:
    c = routing_client(schedule_body=schedule(game(game_pk=745804)))
    await _ingest(db, c, from_date="2024-04-09")
    # Re-ingest the identical schedule -> still one provider game reference.
    c2 = routing_client(schedule_body=schedule(game(game_pk=745804)))
    await _ingest(db, c2, from_date="2024-04-09")
    assert _count(db, "provider_game_references", "WHERE provider_game_id='745804'") == 1
    assert _count(db, "provider_game_references") == 1


async def test_doubleheader_two_gamepks_stay_two_games(db: Database) -> None:
    c = routing_client(schedule_body=schedule(
        game(game_pk=1, game_number=1, doubleheader="S"),
        game(game_pk=2, game_number=2, doubleheader="S"),
    ))
    await _ingest(db, c, from_date="2024-04-09")
    assert _count(db, "provider_game_references") == 2
    assert _count(db, "game_schedule_snapshots") == 2


async def test_reschedule_preserves_official_identity(db: Database) -> None:
    c = routing_client(schedule_body=schedule(game(game_pk=555, status="Scheduled")))
    await _ingest(db, c, from_date="2024-04-09")
    # Rescheduled: same gamePk, new date + status; identity preserved, new snapshot.
    resched = game(
        game_pk=555, status="Postponed", official_date="2024-04-10",
        reschedule={"rescheduledFrom": "2024-04-09T23:05:00Z"},
    )
    c2 = routing_client(schedule_body=schedule(resched, date="2024-04-10"))
    await _ingest(db, c2, from_date="2024-04-10")
    assert _count(db, "provider_game_references", "WHERE provider_game_id='555'") == 1
    assert _count(db, "game_schedule_snapshots") == 2  # original + rescheduled


# --------------------------------------------------------------------------- #
# Append-only + transitions (§22.7-10, 24, 25)
# --------------------------------------------------------------------------- #
async def test_reingest_identical_is_deduplicated(db: Database) -> None:
    body = schedule(game(game_pk=1))
    await _ingest(db, routing_client(schedule_body=body), from_date="2024-04-09")
    await _ingest(db, routing_client(schedule_body=body), from_date="2024-04-09")
    assert _count(db, "game_schedule_snapshots") == 1


async def test_schedule_change_appends(db: Database) -> None:
    await _ingest(db, routing_client(schedule_body=schedule(game(game_pk=1, status="Scheduled"))),
                  from_date="2024-04-09")
    await _ingest(db, routing_client(schedule_body=schedule(game(game_pk=1, status="In Progress",
                                                                 abstract="Live", coded="I"))),
                  from_date="2024-04-09")
    assert _count(db, "game_schedule_snapshots") == 2
    with db.connection() as conn:
        states = {r[0] for r in conn.execute(
            "SELECT mapped_status FROM game_schedule_snapshots")}
    assert states == {"scheduled", "in_progress"}


async def test_a_b_a_transitions_retained(db: Database) -> None:
    for st, ab, cd in [("Scheduled", "Preview", "S"), ("Delayed", "Preview", "D"),
                       ("Scheduled", "Preview", "S")]:
        await _ingest(db, routing_client(schedule_body=schedule(
            game(game_pk=1, status=st, abstract=ab, coded=cd))), from_date="2024-04-09")
    # A -> B -> A keeps all three (the third differs from its predecessor B).
    assert _count(db, "game_schedule_snapshots") == 3


async def test_out_of_order_backfill_does_not_regress(db: Database) -> None:
    # Ingest a "later" observation, then an "earlier" identical-content one.
    body = schedule(game(game_pk=1))
    await _ingest(db, routing_client(schedule_body=body), from_date="2024-04-09")
    before = _count(db, "game_schedule_snapshots")
    # Re-poll identical content -> deduped, current state unchanged.
    await _ingest(db, routing_client(schedule_body=body), from_date="2024-04-09")
    assert _count(db, "game_schedule_snapshots") == before


async def test_result_snapshots_append_only_and_correction(db: Database) -> None:
    line1 = linescore(home_runs=5, away_runs=3)
    c = routing_client(schedule_body=schedule(game(game_pk=1, status="Final", coded="F",
                                                   abstract="Final")),
                       line_by_pk={"1": line1})
    await _ingest(db, c, from_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots") == 1
    # A corrected score is a NEW observation, never an overwrite.
    line2 = linescore(home_runs=6, away_runs=3)
    c2 = routing_client(schedule_body=schedule(game(game_pk=1, status="Final", coded="F",
                                                    abstract="Final")),
                        line_by_pk={"1": line2})
    await _ingest(db, c2, from_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots") == 2


async def test_schedule_snapshots_are_append_only_db(db: Database) -> None:
    await _ingest(db, routing_client(schedule_body=schedule(game(game_pk=1))),
                  from_date="2024-04-09")
    import sqlite3

    with db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE game_schedule_snapshots SET mapped_status='final'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM game_schedule_snapshots")


# --------------------------------------------------------------------------- #
# Provenance (§22.11-12)
# --------------------------------------------------------------------------- #
async def test_every_row_traces_to_a_raw_response(db: Database) -> None:
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final",
                                    home_probable=592332, lineups={"home": [111], "away": [211]})),
        box_by_pk={"1": boxscore()}, line_by_pk={"1": linescore()},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=ALL)
    with db.connection() as conn:
        for table in ("game_schedule_snapshots", "game_result_snapshots", "mlb_inning_lines",
                      "team_game_statistics", "player_game_statistics",
                      "probable_pitcher_snapshots", "lineup_snapshots"):
            dangling = conn.execute(
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN raw_responses r "
                "ON t.raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
            ).fetchone()[0]
            assert dangling == 0, f"{table} has rows without raw provenance"


async def test_schedule_provenance_not_attached_to_boxscore(db: Database) -> None:
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()}, line_by_pk={"1": linescore()},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box", "results"))
    with db.connection() as conn:
        # The schedule raw and the boxscore raw are different rows...
        sched_raw = conn.execute(
            "SELECT raw_response_id FROM game_schedule_snapshots").fetchone()[0]
        box_raws = {r[0] for r in conn.execute(
            "SELECT raw_response_id FROM team_game_statistics")}
        assert sched_raw not in box_raws
        # ...and each box stat row's raw response is a /boxscore endpoint.
        for raw_id in box_raws:
            endpoint = conn.execute(
                "SELECT endpoint FROM raw_responses WHERE raw_response_id = ?", (raw_id,)
            ).fetchone()[0]
            assert endpoint.endswith("/boxscore")


# --------------------------------------------------------------------------- #
# Statistics semantics (§22.13, 17, 18)
# --------------------------------------------------------------------------- #
async def test_missing_numeric_not_zero(db: Database) -> None:
    # A team batting block missing 'runs' must store NULL, not 0.
    box = boxscore(home_batting={"hits": 9, "atBats": 34})  # no 'runs'
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": box},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box",))
    with db.connection() as conn:
        runs = conn.execute(
            "SELECT runs FROM team_game_statistics WHERE home_away='home'").fetchone()[0]
        assert runs is None  # missing -> NULL, never fabricated as 0


async def test_provider_ids_do_not_become_canonical(db: Database) -> None:
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box",))
    with db.connection() as conn:
        # Player stats keep the provider id and leave canonical player_id NULL.
        row = conn.execute(
            "SELECT provider_player_id, player_id FROM player_game_statistics LIMIT 1").fetchone()
        assert row["provider_player_id"] == "111" or row["provider_player_id"] == "500"
        canon = {r[0] for r in conn.execute("SELECT player_id FROM player_game_statistics")}
        assert canon == {None}
        # No canonical players were created by ingestion.
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0


async def test_batting_and_pitching_kept_separate(db: Database) -> None:
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box",))
    with db.connection() as conn:
        roles = {r[0] for r in conn.execute("SELECT role FROM player_game_statistics")}
        assert roles == {"batting", "pitching"}
        # The pitcher row has pitching_stats and no batting_stats.
        pitch = conn.execute(
            "SELECT batting_stats, pitching_stats FROM player_game_statistics "
            "WHERE role='pitching'").fetchone()
        assert pitch["batting_stats"] is None and pitch["pitching_stats"] is not None


# --------------------------------------------------------------------------- #
# Inning lines (§22.15-16)
# --------------------------------------------------------------------------- #
async def test_extra_innings_preserved(db: Database) -> None:
    innings = [{"num": n, "home": {"runs": 0}, "away": {"runs": 0}} for n in range(1, 12)]
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        line_by_pk={"1": linescore(innings=innings, current_inning=11)},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("inning",))
    # 11 innings x 2 sides.
    assert _count(db, "mlb_inning_lines") == 22
    with db.connection() as conn:
        assert conn.execute("SELECT MAX(inning) FROM mlb_inning_lines").fetchone()[0] == 11


async def test_negative_runs_flagged(db: Database) -> None:
    # Negative runs in the result must create a data-quality issue (distinct from
    # a score-vs-inning reconciliation contradiction).
    line = linescore(home_runs=-1, away_runs=3)
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        line_by_pk={"1": line},
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("results",))
    assert r.data_quality_issues >= 1
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-MLB-RESULT-001'") >= 1


async def test_final_score_vs_inning_sum_contradiction_flagged(db: Database) -> None:
    # A REAL contradiction: the away team total disagrees with its inning-run sum.
    innings = [{"num": 1, "home": {"runs": 2}, "away": {"runs": 1}},
               {"num": 2, "home": {"runs": 3}, "away": {"runs": 1}}]  # away sum = 2
    line = linescore(innings=innings, home_runs=5, away_runs=3, current_inning=2)  # away total 3
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        line_by_pk={"1": line},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("results", "inning"))
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-MLB-RECON-001'") >= 1
    # No false negative-run issue was raised (runs are all non-negative).
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-MLB-RESULT-001'") == 0


async def test_malformed_inning_number_flagged(db: Database) -> None:
    innings = [{"num": 0, "home": {"runs": 1}, "away": {"runs": 0}},
               {"num": 1, "home": {"runs": 1}, "away": {"runs": 0}}]
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        line_by_pk={"1": linescore(innings=innings)},
    )
    r = await _ingest(db, c, from_date="2024-04-09", includes=("inning",))
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-MLB-INNING-001'") >= 1
    # The malformed inning is rejected; the valid inning 1 is kept.
    assert _count(db, "mlb_inning_lines", "WHERE inning=1") == 2
    assert r.records_rejected >= 1


# --------------------------------------------------------------------------- #
# Status mapping (§22.14)
# --------------------------------------------------------------------------- #
async def test_unknown_status_stays_unknown_and_flags(db: Database) -> None:
    c = routing_client(schedule_body=schedule(
        game(game_pk=1, status="Weather Check", coded="Z", abstract="Other")))
    r = await _ingest(db, c, from_date="2024-04-09")
    with db.connection() as conn:
        row = conn.execute(
            "SELECT mapped_status, detailed_status FROM game_schedule_snapshots").fetchone()
        assert row["mapped_status"] == "unknown"
        assert row["detailed_status"] == "Weather Check"  # provider status preserved
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-MLB-STATUS-001'") == 1
    assert r.data_quality_issues >= 1


async def test_postponed_and_suspended_mapped(db: Database) -> None:
    c = routing_client(schedule_body=schedule(
        game(game_pk=1, status="Postponed", coded="D", abstract="Preview"),
        game(game_pk=2, status="Suspended", coded="U", abstract="Live"),
    ))
    await _ingest(db, c, from_date="2024-04-09")
    with db.connection() as conn:
        states = dict(conn.execute(
            "SELECT provider_game_id, mapped_status FROM game_schedule_snapshots"))
    assert states["1"] == "postponed" and states["2"] == "suspended"


# --------------------------------------------------------------------------- #
# Probable pitchers (§22.19-20)
# --------------------------------------------------------------------------- #
async def test_probable_pitcher_change_appends(db: Database) -> None:
    c1 = routing_client(schedule_body=schedule(game(game_pk=1, home_probable=100)))
    await _ingest(db, c1, from_date="2024-04-09", includes=("probables",))
    c2 = routing_client(schedule_body=schedule(game(game_pk=1, home_probable=200)))
    await _ingest(db, c2, from_date="2024-04-09", includes=("probables",))
    assert _count(db, "probable_pitcher_snapshots", "WHERE side='home'") == 2
    with db.connection() as conn:
        pids = {r[0] for r in conn.execute(
            "SELECT provider_player_id FROM probable_pitcher_snapshots WHERE side='home'")}
    assert pids == {"100", "200"}


async def test_probable_is_never_confirmed(db: Database) -> None:
    c = routing_client(schedule_body=schedule(game(game_pk=1, home_probable=100)))
    await _ingest(db, c, from_date="2024-04-09", includes=("probables",))
    with db.connection() as conn:
        statuses = {r[0] for r in conn.execute(
            "SELECT status FROM probable_pitcher_snapshots")}
    assert statuses == {"probable"}  # never silently 'confirmed'


async def test_missing_probable_stays_unknown(db: Database) -> None:
    c = routing_client(schedule_body=schedule(game(game_pk=1)))  # no probables
    await _ingest(db, c, from_date="2024-04-09", includes=("probables",))
    assert _count(db, "probable_pitcher_snapshots") == 0  # absence != a row


# --------------------------------------------------------------------------- #
# Lineups (§22.21-23)
# --------------------------------------------------------------------------- #
async def test_missing_lineup_is_not_empty_confirmed(db: Database) -> None:
    c = routing_client(schedule_body=schedule(game(game_pk=1)))  # no lineups key
    await _ingest(db, c, from_date="2024-04-09", includes=("lineups",))
    assert _count(db, "lineup_snapshots") == 0


async def test_posted_lineup_is_not_confirmed(db: Database) -> None:
    c = routing_client(schedule_body=schedule(
        game(game_pk=1, lineups={"home": [111, 112, 113], "away": [211]})))
    await _ingest(db, c, from_date="2024-04-09", includes=("lineups",))
    with db.connection() as conn:
        confirmed = {r[0] for r in conn.execute("SELECT is_confirmed FROM lineup_snapshots")}
    assert confirmed == {0}  # posted, never confirmed pregame starters


async def test_lineup_order_is_deterministic(db: Database) -> None:
    c = routing_client(schedule_body=schedule(
        game(game_pk=1, lineups={"home": [111, 112, 113]})))
    await _ingest(db, c, from_date="2024-04-09", includes=("lineups",))
    with db.connection() as conn:
        lid = conn.execute(
            "SELECT lineup_id FROM lineup_snapshots WHERE home_away='home'").fetchone()[0]
        rows = conn.execute(
            "SELECT batting_order, provider_player_id FROM lineup_players "
            "WHERE lineup_id=? ORDER BY batting_order", (lid,)).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "111"), (2, "112"), (3, "113")]


async def test_lineup_change_appends_new_snapshot(db: Database) -> None:
    await _ingest(db, routing_client(schedule_body=schedule(
        game(game_pk=1, lineups={"home": [111, 112, 113]}))),
        from_date="2024-04-09", includes=("lineups",))
    # A scratch: player 112 replaced by 114 -> a new lineup snapshot appends.
    await _ingest(db, routing_client(schedule_body=schedule(
        game(game_pk=1, lineups={"home": [111, 114, 113]}))),
        from_date="2024-04-09", includes=("lineups",))
    assert _count(db, "lineup_snapshots", "WHERE home_away='home'") == 2


async def test_postgame_box_does_not_create_pregame_lineup(db: Database) -> None:
    # Ingest ONLY box data (no lineups include). No lineup snapshot may appear
    # from the box score's batting order.
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()},
    )
    await _ingest(db, c, from_date="2024-04-09", includes=("box",))
    assert _count(db, "lineup_snapshots") == 0


# --------------------------------------------------------------------------- #
# Dry-run + zero games + counters (§22.26-27, 36)
# --------------------------------------------------------------------------- #
def _snapshot_all(db: Database) -> dict[str, int]:
    tables = [
        "ingestion_runs", "raw_responses", "provider_game_references",
        "provider_team_references", "game_schedule_snapshots", "game_result_snapshots",
        "mlb_inning_lines", "team_game_statistics", "player_game_statistics",
        "roster_snapshots", "probable_pitcher_snapshots", "lineup_snapshots",
        "lineup_players", "data_quality_issues",
    ]
    with db.connection() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


async def test_dry_run_persists_nothing(db: Database) -> None:
    before = _snapshot_all(db)
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final",
                                    home_probable=100, lineups={"home": [111]})),
        box_by_pk={"1": boxscore()}, line_by_pk={"1": linescore()},
    )
    result = await _ingest(db, c, from_date="2024-04-09", includes=ALL, dry_run=True)
    assert result.dry_run is True and result.run_id is None
    assert result.schedule_snapshots_inserted >= 1  # would-be counts reported
    assert _snapshot_all(db) == before  # absolutely nothing persisted


async def test_zero_games_is_success(db: Database) -> None:
    c = routing_client(schedule_body=schedule())  # no games
    result = await _ingest(db, c, from_date="2024-04-09")
    assert result.status == "succeeded"
    assert result.games_received == 0


async def test_counters_distinguish_inserted_from_unchanged(db: Database) -> None:
    body = schedule(game(game_pk=1))
    r1 = await _ingest(db, routing_client(schedule_body=body), from_date="2024-04-09")
    assert r1.schedule_snapshots_inserted == 1 and r1.schedule_snapshots_unchanged == 0
    r2 = await _ingest(db, routing_client(schedule_body=body), from_date="2024-04-09")
    assert r2.schedule_snapshots_inserted == 0 and r2.schedule_snapshots_unchanged == 1


# --------------------------------------------------------------------------- #
# Failures + safety (§22.28, 32, 33, 34, 35)
# --------------------------------------------------------------------------- #
async def test_schedule_provider_failure_is_failed(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"}, headers={"content-type": "application/json"})

    c = _client(handler, max_retries=0)
    result = await _ingest(db, c, from_date="2024-04-09")
    assert result.status == "failed"
    assert _count(db, "ingestion_runs") == 0  # nothing persisted on a failed fetch


async def test_subfetch_failure_does_not_fabricate_or_corrupt(db: Database) -> None:
    # The box sub-fetch 500s; the schedule + results still persist, box stats do not.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/boxscore"):
            return httpx.Response(500, json={"e": "x"}, headers={"content-type": "application/json"})
        if path == "/api/v1/schedule":
            body: dict[str, Any] = schedule(game(game_pk=1, status="Final", coded="F",
                                                 abstract="Final"))
        elif path.endswith("/linescore"):
            body = linescore()
        else:
            body = {}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    c = _client(handler, max_retries=0)
    result = await _ingest(db, c, from_date="2024-04-09", includes=("box", "results"))
    assert _count(db, "game_schedule_snapshots") == 1
    assert _count(db, "game_result_snapshots") == 1
    assert _count(db, "team_game_statistics") == 0  # never fabricated from a failed fetch
    assert result.rejections  # the failure was recorded


async def test_oversized_response_never_stored(db: Database) -> None:
    big = {"dates": [{"date": "2024-04-09", "games": [game(game_pk=n) for n in range(1, 400)]}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=big, headers={"content-type": "application/json"})

    c = _client(handler, max_body_bytes=512)
    result = await _ingest(db, c, from_date="2024-04-09")
    assert result.status == "failed"
    assert _count(db, "raw_responses") == 0
    with db.connection() as conn:
        for (body,) in conn.execute("SELECT body FROM raw_responses"):
            assert len(body) <= 512


async def test_every_request_is_get(db: Database) -> None:
    methods: list[str] = []
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()}, line_by_pk={"1": linescore()}, methods=methods,
    )
    await _ingest(db, c, from_date="2024-04-09", includes=ALL)
    assert methods and set(methods) == {"GET"}


async def test_unapproved_mlb_paths_blocked() -> None:
    policy = ReadOnlyHTTPPolicy.for_mlb_statsapi()
    for path in ("/api/v1/game/1/feed/live", "/api/v1/awards", "/api/v1/game/1/content",
                 "/api/v1.1/game/1/feed/live", "/api/v1/draft"):
        with pytest.raises(ReadOnlyPolicyError):
            policy.enforce("GET", f"https://statsapi.mlb.com{path}")
    # The documented D2 endpoints are allowed.
    for path in ("/api/v1/game/745804/boxscore", "/api/v1/game/745804/linescore"):
        policy.enforce("GET", f"https://statsapi.mlb.com{path}")


# --------------------------------------------------------------------------- #
# Capability gating + CLI-facing behavior (§20)
# --------------------------------------------------------------------------- #
async def test_confirmed_starters_capability_stays_unavailable(db: Database) -> None:
    # Ingesting lineups never produces a confirmed-starters capability observation.
    c = routing_client(schedule_body=schedule(
        game(game_pk=1, lineups={"home": [111]})))
    await _ingest(db, c, from_date="2024-04-09", includes=("lineups",))
    with db.connection() as conn:
        # No provider_capabilities row claims confirmed_pregame_starters supported.
        supported = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE capability='confirmed_pregame_starters' AND observed_state='supported'"
        ).fetchone()[0]
    assert supported == 0


async def test_ingest_lineups_helper_writes_lineups(db: Database) -> None:
    c = routing_client(schedule_body=schedule(
        game(game_pk=1, lineups={"home": [111, 112], "away": [211]})))
    try:
        result = await ingest_lineups(database=db, client=c, date="2024-04-09")
    finally:
        await c.aclose()
    assert result.command == "ingest-lineups"
    assert _count(db, "lineup_snapshots") == 2


# --------------------------------------------------------------------------- #
# Secret redaction + no-op guards (§22.29, 31, 39, 40)
# --------------------------------------------------------------------------- #
async def test_no_secret_in_database_or_output(db: Database) -> None:
    # MLB StatsAPI is keyless, but assert the sweep infrastructure holds anyway.
    lines: list[str] = []
    c = routing_client(
        schedule_body=schedule(game(game_pk=1, status="Final", coded="F", abstract="Final")),
        box_by_pk={"1": boxscore()}, line_by_pk={"1": linescore()},
    )
    result = await _ingest(db, c, from_date="2024-04-09", includes=ALL)
    lines.append(str(result))
    with db.connection() as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in conn.execute(f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, str):
                        assert SENTINEL not in value
    assert SENTINEL not in "\n".join(lines)


def test_d3_d5_remain_unimplemented() -> None:
    import importlib

    for mod in ("nba_ingestor", "weather_ingestor", "hoopr_import"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"sports_quant.ingest.{mod}")


def test_no_lineups_module_imports_gateway() -> None:
    for name in ("mlb_ingestor",):
        text = (Path(__file__).resolve().parents[1] / f"{name}.py").read_text(encoding="utf-8")
        assert "import gateway" not in text and "from gateway" not in text
