"""Phase D3 NBA ingestion (BALLDONTLIE GOAT), mocked transports only.

Contract-shaped BALLDONTLIE payloads; no live call, no real key. This module is
collected and executed WITHOUT the optional ``pyarrow`` package -- the offline
hoopR/Parquet tests live in ``test_phase_d3_hoopr.py`` and skip when pyarrow is
absent. These tests pin the D3 correctness guarantees after the d013 repair:
sport-correct NBA storage (points/period, stat groups -- never runs/innings or
batting/pitching), deterministic box-score matching by (date, home, visitor),
exact injury return-estimate preservation, corrections, append-only, provenance,
capability honesty, zero-persistence dry-run, no secret leakage, and unchanged
D2 MLB behaviour.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.base import RepositoryError
from sports_quant.db.repositories.nba import (
    SqliteNbaPlayerStatRepository,
    SqliteNbaResultRepository,
    SqlitePlaySnapshotRepository,
    SqliteQuarterLineRepository,
)
from sports_quant.db.repositories.observations import ObservationOutcome
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.nba_ingestor import ingest_injuries, ingest_nba
from sports_quant.providers.balldontlie import BalldontlieClient

SENTINEL = "sk-nba-d3-sentinel-do-not-store"
ALL_NBA = ("results", "box", "player-stats", "advanced", "quarters", "plays", "lineups")


# --------------------------------------------------------------------------- #
# Fixture builders (contract-shaped BALLDONTLIE payloads)
# --------------------------------------------------------------------------- #
def game(
    *,
    gid: int = 18444208,
    status: str = "Final",
    home_score: Optional[int] = 120,
    visitor_score: Optional[int] = 110,
    period: Optional[int] = 4,
    date: str = "2024-04-09",
    home_team: int = 2,
    visitor_team: int = 14,
) -> dict[str, Any]:
    return {
        "id": gid, "date": date, "season": 2023, "status": status, "period": period,
        "postseason": False, "home_team_score": home_score, "visitor_team_score": visitor_score,
        "home_team": {"id": home_team, "abbreviation": "BOS", "full_name": "Boston Celtics"},
        "visitor_team": {"id": visitor_team, "abbreviation": "LAL", "full_name": "LA Lakers"},
    }


def four_periods() -> list[dict[str, Any]]:
    return [
        {"period": 1, "home": 30, "away": 28},
        {"period": 2, "home": 28, "away": 25},
        {"period": 3, "home": 32, "away": 30},
        {"period": 4, "home": 30, "away": 27},
    ]


def box_object(
    *,
    date: str = "2024-04-09",
    home_team: int = 2,
    visitor_team: int = 14,
    home_score: Optional[int] = 120,
    visitor_score: Optional[int] = 110,
    periods: Optional[list[dict[str, Any]]] = None,
    include_id: Optional[int] = None,
) -> dict[str, Any]:
    """A contract-shaped /v1/box_scores object. NO top-level game id by default
    (the documented response may omit it), so association is by date + team ids."""

    obj: dict[str, Any] = {
        "date": date,
        "home_team_score": home_score, "visitor_team_score": visitor_score,
        "home_team": {"id": home_team, "abbreviation": "BOS",
                      "players": [{"player": {"id": 111}, "pts": 30, "min": "34"}]},
        "visitor_team": {"id": visitor_team, "abbreviation": "LAL",
                         "players": [{"player": {"id": 222}, "pts": 25, "min": "36"}]},
    }
    obj["periods"] = periods if periods is not None else four_periods()
    if include_id is not None:
        obj["id"] = include_id
    return obj


def page(data: list[dict[str, Any]], next_cursor: Optional[int] = None) -> dict[str, Any]:
    return {"data": data, "meta": {"next_cursor": next_cursor}}


def stat_row(pid: int = 111, gid: int = 18444208, pts: int = 30) -> dict[str, Any]:
    return {"id": 1, "pts": pts, "reb": 5, "ast": 7, "min": "34",
            "player": {"id": pid}, "team": {"id": 2}, "game": {"id": gid}}


def adv_row(pid: int = 111, gid: int = 18444208) -> dict[str, Any]:
    return {"id": 9, "pie": 0.15, "pace": 98.5, "off_rating": 118.2,
            "player": {"id": pid}, "team": {"id": 2}, "game": {"id": gid}}


def play_row(pid: int, *, ptype: str = "shot", text: str = "makes shot",
             event_num: int = 1) -> dict[str, Any]:
    return {"id": 5000 + event_num, "period": 1, "clock": "11:34", "type": ptype,
            "text": text, "team": {"id": 2}, "player": {"id": pid}, "event_num": event_num}


def lineups_body(gid: int = 18444208) -> dict[str, Any]:
    return {"data": [{"game_id": gid, "team": {"id": 2}, "players": [
        {"player": {"id": 111}, "position": "G"},
        {"player": {"id": 333}, "position": "F"},
    ]}]}


def injury_row(pid: int = 111, status: Optional[str] = "Out",
               return_date: Optional[str] = "2024-04-15",
               description: Optional[str] = "Knee") -> dict[str, Any]:
    row: dict[str, Any] = {"player": {"id": pid, "team_id": 2}}
    if status is not None:
        row["status"] = status
    if return_date is not None:
        row["return_date"] = return_date
    if description is not None:
        row["description"] = description
    return row


def _bdl(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> BalldontlieClient:
    http = build_readonly_client(
        base_url="https://api.balldontlie.io",
        policy=ReadOnlyHTTPPolicy.for_balldontlie(),
        inner_transport=httpx.MockTransport(handler),
    )
    return BalldontlieClient(SENTINEL, client=http, **kwargs)


def router(
    *,
    games: Optional[dict[str, Any]] = None,
    game_by_id: Optional[dict[str, Any]] = None,
    box: Optional[list[dict[str, Any]]] = None,
    stats: Optional[dict[str, Any]] = None,
    advanced: Optional[dict[str, Any]] = None,
    plays: Optional[dict[str, Any]] = None,
    lineups: Optional[dict[str, Any]] = None,
    injuries: Optional[dict[str, Any]] = None,
) -> BalldontlieClient:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/v1/games":
            body: Any = games if games is not None else page([game()])
        elif p.startswith("/v1/games/"):
            body = {"data": game_by_id if game_by_id is not None else game()}
        elif p == "/v1/box_scores":
            body = {"data": box if box is not None else [box_object()]}
        elif p == "/v1/stats":
            body = stats if stats is not None else page([stat_row()])
        elif p == "/nba/v1/stats/advanced":
            body = advanced if advanced is not None else page([adv_row()])
        elif p == "/v1/plays":
            body = plays if plays is not None else page([play_row(111)])
        elif p == "/v1/lineups":
            body = lineups if lineups is not None else lineups_body()
        elif p == "/v1/player_injuries":
            body = injuries if injuries is not None else page([injury_row()])
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    return _bdl(handler)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    p = tmp_path / "corpus.db"
    initialize_database(p)
    return Database(p)


def _count(db: Database, table: str, where: str = "") -> int:
    with db.connection() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0])


async def _run(db: Database, client: BalldontlieClient, **kwargs: Any) -> Any:
    try:
        return await ingest_nba(database=db, client=client, **kwargs)
    finally:
        await client.aclose()


async def _run_injuries(db: Database, client: BalldontlieClient, **kwargs: Any) -> Any:
    try:
        return await ingest_injuries(database=db, client=client, **kwargs)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Migration v13 + append-only triggers on NBA-typed tables
# --------------------------------------------------------------------------- #
def test_migration_v13_tables_exist(db: Database) -> None:
    with db.connection() as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 13
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"nba_game_results", "nba_team_statistics", "nba_player_statistics"} <= names
        cols = {r[1] for r in conn.execute("PRAGMA table_info(injury_snapshots)")}
        assert "return_estimate" in cols


async def test_nba_typed_tables_are_append_only(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    for table in ("nba_game_results", "nba_team_statistics", "nba_player_statistics",
                  "nba_quarter_lines", "play_snapshots"):
        with db.connection() as conn:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(f"UPDATE {table} SET provider='x'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(f"DELETE FROM {table}")


# --------------------------------------------------------------------------- #
# Normal sweep + --game-id
# --------------------------------------------------------------------------- #
async def test_normal_one_day_sweep(db: Database) -> None:
    r = await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    assert r.status == "succeeded"
    assert r.games_received == 1
    assert _count(db, "game_schedule_snapshots") == 1
    assert _count(db, "nba_game_results") == 1
    assert _count(db, "nba_team_statistics") == 2
    assert _count(db, "nba_player_statistics", "WHERE stat_group='traditional'") == 1
    assert _count(db, "nba_player_statistics", "WHERE stat_group='advanced'") == 1
    assert _count(db, "nba_quarter_lines") == 8  # 4 periods x 2 sides
    assert _count(db, "play_snapshots") == 1
    assert _count(db, "lineup_snapshots") == 1
    assert _count(db, "lineup_players") == 2


async def test_game_id_ingestion(db: Database) -> None:
    r = await _run(db, router(), game_id=18444208, includes=("results", "quarters"))
    assert r.status == "succeeded"
    assert r.games_received == 1
    assert _count(db, "nba_game_results") == 1


# --------------------------------------------------------------------------- #
# Cursor pagination, repeated-cursor protection, truncation
# --------------------------------------------------------------------------- #
async def test_cursor_pagination_across_pages(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        cur = request.url.params.get("cursor")
        if p == "/v1/games":
            body: Any = page([game()])
        elif p == "/v1/plays":
            body = (page([play_row(111, event_num=1)], next_cursor=1) if cur is None
                    else page([play_row(222, event_num=2)], next_cursor=None))
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09", includes=("plays",))
    assert _count(db, "play_snapshots") == 2


async def test_repeated_cursor_is_stopped(db: Database) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/v1/games":
            body: Any = page([game()])
        elif p == "/v1/plays":
            calls["n"] += 1
            body = page([play_row(111, event_num=calls["n"])], next_cursor=5)  # same cursor forever
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    r = await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("plays",), max_pages=50)
    assert r.status == "succeeded"
    assert calls["n"] <= 2


async def test_truncation_is_reported(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        cur = request.url.params.get("cursor")
        if p == "/v1/games":
            body: Any = page([game()])
        elif p == "/v1/plays":
            n = 0 if cur is None else int(cur)
            body = page([play_row(111, event_num=n + 1)], next_cursor=n + 1)  # always more
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    r = await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("plays",), max_pages=3)
    assert r.records_truncated >= 1
    assert any("truncated" in t for t in r.truncations)


# --------------------------------------------------------------------------- #
# Provenance + reference creation
# --------------------------------------------------------------------------- #
async def test_every_output_traces_to_a_raw_response(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    tables = ("game_schedule_snapshots", "nba_game_results", "nba_team_statistics",
              "nba_player_statistics", "nba_quarter_lines", "play_snapshots", "lineup_snapshots")
    with db.connection() as conn:
        for table in tables:
            dangling = conn.execute(
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN raw_responses r "
                "ON t.raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
            ).fetchone()[0]
            assert dangling == 0, f"{table} has rows without raw provenance"
            bad_hash = conn.execute(
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN raw_responses r "
                "ON t.raw_response_hash = r.content_hash WHERE r.content_hash IS NULL"
            ).fetchone()[0]
            assert bad_hash == 0, f"{table} has a raw_response_hash with no matching content_hash"


async def test_provider_references_created(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    assert _count(db, "provider_game_references", "WHERE provider_game_id='18444208'") == 1
    assert _count(db, "provider_team_references") == 2
    # Box is TEAM-level only (player stats come from /v1/stats): players referenced
    # are 111 (stats/advanced/plays/lineup) and 333 (lineup).
    assert _count(db, "provider_player_references") == 2


# --------------------------------------------------------------------------- #
# Box-score deterministic matching (repair 3)
# --------------------------------------------------------------------------- #
async def test_box_without_game_id_matches_by_date_and_teams(db: Database) -> None:
    # box_object() has NO top-level id; matching is by (date, home, visitor).
    r = await _run(db, router(box=[box_object()]), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("box",))
    assert r.status == "succeeded"
    assert _count(db, "nba_team_statistics") == 2
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-NBA-BOX-001'") == 0


async def test_box_with_genuine_game_id_still_matches(db: Database) -> None:
    await _run(db, router(box=[box_object(include_id=18444208)]),
               from_date="2024-04-09", to_date="2024-04-09", includes=("box",))
    assert _count(db, "nba_team_statistics") == 2


async def test_several_games_same_date_match_the_right_one(db: Database) -> None:
    games = page([game(gid=1, home_team=2, visitor_team=14),
                  game(gid=2, home_team=5, visitor_team=6)])
    box = [box_object(home_team=2, visitor_team=14, home_score=120, visitor_score=110),
           box_object(home_team=5, visitor_team=6, home_score=99, visitor_score=101)]
    await _run(db, router(games=games, box=box), from_date="2024-04-09", to_date="2024-04-09",
               includes=("box",))
    with db.connection() as conn:
        # Game 1's home team (2) carries 120; game 2's home team (5) carries 99 -- no cross-attach.
        g1 = conn.execute(
            "SELECT points FROM nba_team_statistics WHERE provider_game_id='1' AND home_away='home'"
        ).fetchone()[0]
        g2 = conn.execute(
            "SELECT points FROM nba_team_statistics WHERE provider_game_id='2' AND home_away='home'"
        ).fetchone()[0]
    assert g1 == 120 and g2 == 99


async def test_box_no_matching_schedule_game_is_rejected(db: Database) -> None:
    # Schedule game is (2 vs 14); the only box object is a different matchup.
    box = [box_object(home_team=99, visitor_team=98)]
    r = await _run(db, router(box=box), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("box",))
    assert _count(db, "nba_team_statistics") == 0
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-NBA-BOX-001'") == 1
    assert r.records_rejected >= 1


async def test_box_ambiguous_match_is_rejected(db: Database) -> None:
    # Two box objects with the SAME (date, home, visitor) key -> ambiguous.
    box = [box_object(home_score=120, visitor_score=110),
           box_object(home_score=118, visitor_score=110)]
    r = await _run(db, router(box=box), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("box",))
    assert _count(db, "nba_team_statistics") == 0
    assert _count(db, "data_quality_issues", "WHERE rule_code='DQ-NBA-BOX-001'") == 1
    assert r.records_rejected >= 1


async def test_box_rows_carry_box_response_provenance(db: Database) -> None:
    await _run(db, router(box=[box_object()]), from_date="2024-04-09", to_date="2024-04-09",
               includes=("box",))
    with db.connection() as conn:
        raw_ids = {r[0] for r in conn.execute("SELECT raw_response_id FROM nba_team_statistics")}
        for raw_id in raw_ids:
            endpoint = conn.execute(
                "SELECT endpoint FROM raw_responses WHERE raw_response_id=?", (raw_id,)
            ).fetchone()[0]
            assert endpoint == "/v1/box_scores"


async def test_box_dry_run_persists_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "never.db"
    r = await _run(Database(missing), router(box=[box_object()]), from_date="2024-04-09",
                   to_date="2024-04-09", includes=("box", "quarters"), dry_run=True)
    assert r.rows_persisted == 0 and r.team_stat_observations == 2 and r.quarter_observations == 8
    assert not missing.exists()


# --------------------------------------------------------------------------- #
# NBA result correction semantics (nba_game_results)
# --------------------------------------------------------------------------- #
async def test_live_progression_produces_no_corrections(db: Database) -> None:
    for status, hs, vs, period in [("2024-04-09T23:30:00Z", None, None, 0),
                                   ("2nd Qtr", 50, 48, 2),
                                   ("Final", 120, 110, 4)]:
        await _run(db, router(games=page([game(status=status, home_score=hs,
                                               visitor_score=vs, period=period)])),
                   from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "nba_game_results", "WHERE is_correction=1") == 0


async def test_in_progress_to_final_same_score_no_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="4th Qtr", home_score=120,
                                           visitor_score=110, period=4)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final", home_score=120,
                                           visitor_score=110, period=4)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "nba_game_results", "WHERE is_correction=1") == 0


async def test_final_score_revision_is_a_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="Final", home_score=120, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final", home_score=118, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "nba_game_results", "WHERE is_correction=1") == 1


async def test_winner_revision_is_a_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="Final", home_score=120, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final", home_score=108, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "nba_game_results", "WHERE is_correction=1") == 1


async def test_status_only_change_is_not_a_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="Final", home_score=120, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final/OT wording", home_score=120,
                                           visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "nba_game_results", "WHERE is_correction=1") == 0


# --------------------------------------------------------------------------- #
# Quarter lines derived from box: missing != zero, overtime supported
# --------------------------------------------------------------------------- #
async def test_missing_quarter_is_not_stored_as_zero(db: Database) -> None:
    periods = [{"period": 1, "home": 30, "away": 28}, {"period": 2, "home": 0}]  # away MISSING
    box = [box_object(periods=periods)]
    await _run(db, router(box=box), from_date="2024-04-09", to_date="2024-04-09",
               includes=("quarters",))
    with db.connection() as conn:
        rows = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT period, side, points FROM nba_quarter_lines")}
    assert rows == {(1, "home"): 30, (1, "away"): 28, (2, "home"): 0}
    assert (2, "away") not in rows


async def test_overtime_periods_supported(db: Database) -> None:
    periods = four_periods() + [{"period": 5, "home": 12, "away": 10}]
    await _run(db, router(box=[box_object(periods=periods)]),
               from_date="2024-04-09", to_date="2024-04-09", includes=("quarters",))
    assert _count(db, "nba_quarter_lines", "WHERE period=5") == 2


# --------------------------------------------------------------------------- #
# Transition semantics on new nba repos (deterministic observed_at)
# --------------------------------------------------------------------------- #
def _seed_ref(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, status, "
        "requested_at, started_at, started_monotonic_ns, requests_made, records_received, "
        "records_normalized, records_inserted, records_deduplicated, records_rejected, "
        "tool_version, created_at) VALUES ('run_t','c','balldontlie','o','{}','started',"
        "'2024-01-01T00:00:00.000000Z','2024-01-01T00:00:00.000000Z',0,0,0,0,0,0,0,'t',"
        "'2024-01-01T00:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_method, http_status, response_headers_json, requested_at, "
        "received_at, elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) VALUES "
        "('raw_t','run_t','balldontlie','/v1/games','{}','GET',200,'{}',"
        "'2024-01-01T00:00:00.000000Z','2024-01-01T00:00:00.000000Z',0,'{}',2,'h','c',"
        "'2024-01-01T00:00:00.000000Z')"
    )
    refs = SqliteProviderReferenceRepository(conn)
    gref, _ = refs.upsert(kind="game", provider="balldontlie", provider_entity_id="1",
                          raw_response_id="raw_t", raw_response_hash="c",
                          observed_at="2024-01-01T00:00:00.000000Z")
    return gref.reference_id


def _t(n: int) -> str:
    return f"2024-04-09T{n:02d}:00:00.000000Z"


def test_quarter_identical_replay_unchanged(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref = _seed_ref(conn)
        repo = SqliteQuarterLineRepository(conn)
        kw: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1", period=1,
            side="home", points=30, ingested_at=_t(1), run_id="run_t", raw_response_id="raw_t",
            raw_response_hash="c")
        _id1, o1 = repo.append(observed_at=_t(1), **kw)
        _id2, o2 = repo.append(observed_at=_t(2), **kw)
        assert o1 is ObservationOutcome.INSERTED and o2 is ObservationOutcome.UNCHANGED
        assert repo.count() == 1


def test_quarter_change_appends_and_a_b_a_preserved(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref = _seed_ref(conn)
        repo = SqliteQuarterLineRepository(conn)
        base: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1", period=1,
            side="home", ingested_at=_t(1), run_id="run_t", raw_response_id="raw_t",
            raw_response_hash="c")
        for pts, ts in ((30, 1), (28, 2), (30, 3)):  # A -> B -> A
            repo.append(points=pts, observed_at=_t(ts), **base)
        assert repo.count() == 3


def test_play_out_of_order_does_not_regress(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref = _seed_ref(conn)
        repo = SqlitePlaySnapshotRepository(conn)
        base: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1",
            play_identity="p1", ingested_at=_t(1), run_id="run_t", raw_response_id="raw_t",
            raw_response_hash="c")
        repo.append(description="corrected text", observed_at=_t(5), **base)
        _id, out = repo.append(description="original text", observed_at=_t(2), **base)
        assert out is ObservationOutcome.INSERTED
        assert repo.count() == 2
        newest = conn.execute(
            "SELECT description FROM play_snapshots ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()[0]
        assert newest == "corrected text"


def test_nba_result_first_obs_and_out_of_order_no_regress(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref = _seed_ref(conn)
        repo = SqliteNbaResultRepository(conn)
        base: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1", ingested_at=_t(1),
            run_id="run_t", raw_response_id="raw_t", raw_response_hash="c")
        # First (final) observation is never a correction.
        _i1, o1, c1 = repo.append(observed_at=_t(5), mapped_status="final", home_points=120,
                                  away_points=110, period=4, winning_side="home", **base)
        assert o1 is ObservationOutcome.INSERTED and c1 is False
        # An out-of-order EARLIER observation is kept as history, is not a correction
        # (its own temporal predecessor is none), and does not regress current state.
        _i2, o2, c2 = repo.append(observed_at=_t(2), mapped_status="in_progress", home_points=50,
                                  away_points=48, period=2, winning_side="home", **base)
        assert o2 is ObservationOutcome.INSERTED and c2 is False
        assert repo.count() == 2
        newest = conn.execute(
            "SELECT home_points, mapped_status FROM nba_game_results "
            "ORDER BY observed_at DESC LIMIT 1").fetchone()
        assert (newest[0], newest[1]) == (120, "final")  # newest state unchanged


def test_nba_in_progress_score_decrease_is_a_correction(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref = _seed_ref(conn)
        repo = SqliteNbaResultRepository(conn)
        base: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1", ingested_at=_t(1),
            run_id="run_t", raw_response_id="raw_t", raw_response_hash="c")
        repo.append(observed_at=_t(1), mapped_status="in_progress", home_points=50,
                    away_points=48, period=2, winning_side="home", **base)
        # A cumulative points total going backwards (even while in-progress) is a revision.
        _i, out, corr = repo.append(observed_at=_t(2), mapped_status="in_progress",
                                    home_points=48, away_points=48, period=2,
                                    winning_side="tie", **base)
        assert out is ObservationOutcome.INSERTED and corr is True


# --------------------------------------------------------------------------- #
# NBA semantic-storage regression (repair 5)
# --------------------------------------------------------------------------- #
async def test_nba_ingest_writes_no_mlb_rows(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    # NBA data must NEVER land in the baseball-named d011 tables.
    assert _count(db, "game_result_snapshots") == 0
    assert _count(db, "team_game_statistics") == 0
    assert _count(db, "player_game_statistics") == 0
    assert _count(db, "mlb_inning_lines") == 0


async def test_nba_rows_use_points_period_and_stat_groups(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    with db.connection() as conn:
        result_cols = {r[1] for r in conn.execute("PRAGMA table_info(nba_game_results)")}
        assert {"home_points", "away_points", "period"} <= result_cols
        assert "home_runs" not in result_cols and "innings_played" not in result_cols
        row = conn.execute(
            "SELECT home_points, away_points, period FROM nba_game_results").fetchone()
        assert (row[0], row[1], row[2]) == (120, 110, 4)
        groups = {r[0] for r in conn.execute("SELECT DISTINCT stat_group FROM nba_player_statistics")}
        assert groups <= {"traditional", "advanced"}
        assert "batting" not in groups and "pitching" not in groups


def test_nba_player_repo_rejects_baseball_role(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref = _seed_ref(conn)
        repo = SqliteNbaPlayerStatRepository(conn)
        with pytest.raises(RepositoryError, match="traditional"):
            repo.append(
                game_ref_id=gref, provider="balldontlie", provider_game_id="1",
                provider_player_id="7", stat_group="batting", observed_at=_t(1),
                ingested_at=_t(1), run_id="run_t", raw_response_id="raw_t", raw_response_hash="c",
            )


# --------------------------------------------------------------------------- #
# Injuries: missing == unknown; exact return-estimate preservation (repair 4)
# --------------------------------------------------------------------------- #
async def test_missing_injury_status_is_unknown_not_healthy(db: Database) -> None:
    injuries = page([injury_row(status=None, return_date=None, description=None)])
    await _run_injuries(db, router(injuries=injuries), date="2024-04-09")
    with db.connection() as conn:
        statuses = {r[0] for r in conn.execute("SELECT status FROM injury_snapshots")}
    assert statuses == {"unknown"}
    assert "healthy" not in statuses and "active" not in statuses


async def test_injury_non_iso_estimate_preserved_without_fabricated_year(db: Database) -> None:
    injuries = page([injury_row(return_date="Nov 17")])
    await _run_injuries(db, router(injuries=injuries), date="2024-04-09")
    with db.connection() as conn:
        est, parsed = conn.execute(
            "SELECT return_estimate, return_date FROM injury_snapshots").fetchone()
    assert est == "Nov 17"  # exact provider text preserved
    assert parsed is None    # no fabricated ISO year


async def test_injury_full_iso_estimate_preserved_and_parsed(db: Database) -> None:
    injuries = page([injury_row(return_date="2024-11-17")])
    await _run_injuries(db, router(injuries=injuries), date="2024-04-09")
    with db.connection() as conn:
        est, parsed = conn.execute(
            "SELECT return_estimate, return_date FROM injury_snapshots").fetchone()
    assert est == "2024-11-17" and parsed == "2024-11-17"


async def test_injury_missing_estimate_is_null(db: Database) -> None:
    injuries = page([injury_row(return_date=None)])
    await _run_injuries(db, router(injuries=injuries), date="2024-04-09")
    with db.connection() as conn:
        est, parsed = conn.execute(
            "SELECT return_estimate, return_date FROM injury_snapshots").fetchone()
    assert est is None and parsed is None


async def test_injury_changed_estimate_appends_replay_unchanged(db: Database) -> None:
    c1 = router(injuries=page([injury_row(return_date="Nov 17")]))
    await _run_injuries(db, c1, date="2024-04-09")
    c2 = router(injuries=page([injury_row(return_date="Nov 24")]))  # changed estimate
    await _run_injuries(db, c2, date="2024-04-09")
    assert _count(db, "injury_snapshots") == 2
    c3 = router(injuries=page([injury_row(return_date="Nov 24")]))  # identical replay
    await _run_injuries(db, c3, date="2024-04-09")
    assert _count(db, "injury_snapshots") == 2  # unchanged


async def test_injury_rows_carry_provenance(db: Database) -> None:
    await _run_injuries(db, router(), date="2024-04-09")
    with db.connection() as conn:
        dangling = conn.execute(
            "SELECT COUNT(*) FROM injury_snapshots i LEFT JOIN raw_responses r "
            "ON i.raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
        ).fetchone()[0]
    assert dangling == 0


async def test_injury_status_transitions_are_append_only(db: Database) -> None:
    for status in ("Questionable", "Out", "Questionable"):  # A -> B -> A
        c = router(injuries=page([injury_row(status=status, return_date=None, description=None)]))
        await _run_injuries(db, c, date="2024-04-09")
    assert _count(db, "injury_snapshots") == 3


# --------------------------------------------------------------------------- #
# Lineups never confirm starters; plays honest when absent
# --------------------------------------------------------------------------- #
async def test_lineups_never_confirm_pregame_starters(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=("lineups",))
    assert _count(db, "lineup_snapshots") == 1
    assert _count(db, "lineup_snapshots", "WHERE is_confirmed=1") == 0


async def test_plays_absent_recorded_honestly(db: Database) -> None:
    r = await _run(db, router(plays=page([])), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("plays",))
    assert r.status == "succeeded"
    assert r.play_observations == 0
    assert _count(db, "play_snapshots") == 0


async def test_substitution_plays_are_best_effort_flagged(db: Database) -> None:
    plays = page([play_row(111, ptype="shot", text="makes shot", event_num=1),
                  play_row(222, ptype="substitution", text="enters the game", event_num=2)])
    await _run(db, router(plays=plays), from_date="2024-04-09", to_date="2024-04-09",
               includes=("plays",))
    assert _count(db, "play_snapshots", "WHERE is_substitution=1") == 1


# --------------------------------------------------------------------------- #
# Tier restriction, auth failure, malformed required response
# --------------------------------------------------------------------------- #
async def test_tier_restricted_optional_endpoint_is_capability_unavailable(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/v1/games":
            return httpx.Response(200, json=page([game()]),
                                  headers={"content-type": "application/json"})
        if p == "/v1/plays":
            return httpx.Response(403, json={"error": "upgrade required to access plays"},
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, json={"data": []},
                              headers={"content-type": "application/json"})

    r = await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("results", "plays"))
    assert r.status == "succeeded"
    assert r.active_failures == 0
    assert r.capabilities_unavailable >= 1
    assert _count(db, "nba_game_results") == 1


async def test_authentication_failure_is_a_genuine_failure(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"},
                              headers={"content-type": "application/json"})

    r = await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("results",))
    assert r.status == "failed"
    assert r.error_type is not None


async def test_malformed_required_response_fails_honestly(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all",
                              headers={"content-type": "application/json"})

    r = await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09")
    assert r.status == "failed"


# --------------------------------------------------------------------------- #
# Dry-run persists nothing; persisted ingestion idempotent
# --------------------------------------------------------------------------- #
async def test_dry_run_creates_no_database_and_persists_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "never_created.db"
    r = await _run(Database(missing), router(), from_date="2024-04-09", to_date="2024-04-09",
                   includes=ALL_NBA, dry_run=True)
    assert r.dry_run and r.status == "succeeded"
    assert r.observations_normalized > 0
    assert r.rows_persisted == 0
    assert r.run_id is None
    assert not missing.exists()


async def test_persisted_ingestion_is_idempotent(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    before = _count(db, "play_snapshots") + _count(db, "nba_game_results") \
        + _count(db, "nba_quarter_lines") + _count(db, "nba_player_statistics")
    r2 = await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    assert r2.records_inserted == 0 and r2.records_changed == 0
    assert r2.records_unchanged > 0
    after = _count(db, "play_snapshots") + _count(db, "nba_game_results") \
        + _count(db, "nba_quarter_lines") + _count(db, "nba_player_statistics")
    assert before == after


# --------------------------------------------------------------------------- #
# CLI JSON + exit codes
# --------------------------------------------------------------------------- #
def _settings(tmp_path: Path) -> Any:
    from sports_quant.config import Settings
    return Settings(database_path=str(tmp_path / "corpus.db"))


def test_cli_json_and_exit_codes(tmp_path: Path) -> None:
    from sports_quant.cli import run_ingest_nba

    dbp = tmp_path / "corpus.db"
    initialize_database(dbp)
    settings = _settings(tmp_path)
    lines: list[str] = []
    code = run_ingest_nba(
        settings, from_date="2024-04-09", to_date="2024-04-09", includes=("results",),
        database_path=dbp, as_json=True, out=lines.append, client=router(),
    )
    assert code == 0
    payload = json.loads(lines[-1])
    assert payload["command"] == "ingest-nba" and payload["status"] == "succeeded"

    missing = tmp_path / "missing.db"
    code3 = run_ingest_nba(settings, from_date="2024-04-09", database_path=missing,
                           out=lambda _s: None, client=router())
    assert code3 == 3

    code1 = run_ingest_nba(settings, from_date="2024-04-09", game_id=1, database_path=dbp,
                           out=lambda _s: None, client=router())
    assert code1 == 1


# --------------------------------------------------------------------------- #
# Secrets never appear in output or persistence
# --------------------------------------------------------------------------- #
async def test_no_secret_in_database_or_output(db: Database) -> None:
    r = await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    assert SENTINEL not in json.dumps(r.__dict__, default=str)
    with db.connection() as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in conn.execute(f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, str):
                        assert SENTINEL not in value, f"secret leaked into {table}"
        for (headers,) in conn.execute("SELECT response_headers_json FROM raw_responses"):
            assert "authorization" not in json.loads(headers)


# --------------------------------------------------------------------------- #
# D2 MLB behaviour remains unchanged
# --------------------------------------------------------------------------- #
async def test_d2_mlb_ingestion_still_works(db: Database) -> None:
    from sports_quant.ingest.mlb_ingestor import ingest_mlb
    from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"dates": [{"date": "2024-04-09", "games": [{
            "gamePk": 745804, "gameType": "R", "season": "2024", "officialDate": "2024-04-09",
            "gameDate": "2024-04-09T23:05:00Z",
            "status": {"abstractGameState": "Final", "codedGameState": "F", "detailedState": "Final"},
            "teams": {"home": {"team": {"id": 133}}, "away": {"team": {"id": 147}}},
            "venue": {"id": 10}, "gameNumber": 1, "doubleHeader": "N",
        }]}]}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    client = MlbStatsApiClient(client=http)
    try:
        r = await ingest_mlb(database=db, client=client, from_date="2024-04-09")
    finally:
        await client.aclose()
    assert r.status == "succeeded"
    assert _count(db, "game_schedule_snapshots") == 1
    assert _count(db, "nba_game_results") == 0  # MLB never writes NBA tables
