"""Phase D3 NBA ingestion: mocked BALLDONTLIE GOAT + offline hoopR fixtures.

Every HTTP interaction is mocked with contract-shaped BALLDONTLIE payloads; no
live call is made and no real key is used. hoopR import runs against a small
synthetic Parquet fixture. These tests pin the D3 guarantees: required outputs
are produced, conditional outputs are recorded (never fabricated), tier gates are
capability-unavailable (not failures), corrections follow the corrected D2
semantics, append-only + provenance hold, ``--dry-run`` persists nothing, secrets
never leak, and D2 MLB behaviour is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.nba import (
    SqliteInjurySnapshotRepository,
    SqlitePlaySnapshotRepository,
    SqliteQuarterLineRepository,
)
from sports_quant.db.repositories.observations import ObservationOutcome
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.hoopr_import import import_hoopr_parquet
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
    periods: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    g: dict[str, Any] = {
        "id": gid, "date": date, "season": 2023, "status": status, "period": period,
        "postseason": False, "home_team_score": home_score, "visitor_team_score": visitor_score,
        "home_team": {"id": home_team, "abbreviation": "BOS", "full_name": "Boston Celtics"},
        "visitor_team": {"id": visitor_team, "abbreviation": "LAL", "full_name": "LA Lakers"},
    }
    if periods is not None:
        g["periods"] = periods
    return g


def four_quarters() -> list[dict[str, Any]]:
    return [
        {"period": 1, "home": 30, "away": 28},
        {"period": 2, "home": 28, "away": 25},
        {"period": 3, "home": 32, "away": 30},
        {"period": 4, "home": 30, "away": 27},
    ]


def page(data: list[dict[str, Any]], next_cursor: Optional[int] = None) -> dict[str, Any]:
    return {"data": data, "meta": {"next_cursor": next_cursor}}


def box_game(gid: int = 18444208, date: str = "2024-04-09") -> dict[str, Any]:
    g = game(gid=gid, date=date)
    g["home_team"] = {"id": 2, "abbreviation": "BOS", "players": [
        {"player": {"id": 111}, "pts": 30, "reb": 5, "ast": 7, "min": "34", "starter": True},
    ]}
    g["visitor_team"] = {"id": 14, "abbreviation": "LAL", "players": [
        {"player": {"id": 222}, "pts": 25, "reb": 8, "ast": 6, "min": "36"},
    ]}
    return g


def stat_row(pid: int = 111, gid: int = 18444208) -> dict[str, Any]:
    return {"id": 1, "pts": 30, "reb": 5, "ast": 7, "min": "34",
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
    seen: Optional[list[str]] = None,
) -> BalldontlieClient:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if seen is not None:
            seen.append(p)
        if p == "/v1/games":
            body: Any = games if games is not None else page([game()])
        elif p.startswith("/v1/games/"):
            body = {"data": game_by_id if game_by_id is not None else game()}
        elif p == "/v1/box_scores":
            body = {"data": box if box is not None else [box_game()]}
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


# --------------------------------------------------------------------------- #
# 1-2. Migration v12 + append-only triggers
# --------------------------------------------------------------------------- #
def test_migration_v12_tables_exist_and_are_append_only(db: Database) -> None:
    with db.connection() as conn:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_versions"
        ).fetchone()[0]
        assert version == 12
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"nba_quarter_lines", "injury_snapshots", "play_snapshots"} <= names


def test_d012_tables_reject_update_and_delete(db: Database) -> None:
    # Seed one row into each via a provider game/player reference.
    with db.connection() as conn:
        with transaction(conn):
            refs = SqliteProviderReferenceRepository(conn)
            # a raw response is required by FK; create a minimal run + raw.
            conn.execute(
                "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, "
                "status, requested_at, started_at, started_monotonic_ns, requests_made, "
                "records_received, records_normalized, records_inserted, records_deduplicated, "
                "records_rejected, tool_version, created_at) VALUES "
                "('run_x','c','balldontlie','o','{}','started','2024-01-01T00:00:00.000000Z',"
                "'2024-01-01T00:00:00.000000Z',0,0,0,0,0,0,0,'t','2024-01-01T00:00:00.000000Z')"
            )
            conn.execute(
                "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
                "request_params_json, http_method, http_status, response_headers_json, "
                "requested_at, received_at, elapsed_ns, body, body_bytes, body_hash, "
                "content_hash, created_at) VALUES ('raw_x','run_x','balldontlie','/v1/games','{}',"
                "'GET',200,'{}','2024-01-01T00:00:00.000000Z','2024-01-01T00:00:00.000000Z',0,'{}',"
                "2,'h','c','2024-01-01T00:00:00.000000Z')"
            )
            gref, _ = refs.upsert(kind="game", provider="balldontlie", provider_entity_id="1",
                                  raw_response_id="raw_x", raw_response_hash="c",
                                  observed_at="2024-01-01T00:00:00.000000Z")
            pref, _ = refs.upsert(kind="player", provider="balldontlie", provider_entity_id="7",
                                  raw_response_id="raw_x", raw_response_hash="c",
                                  observed_at="2024-01-01T00:00:00.000000Z")
            obs = "2024-01-01T00:00:00.000000Z"
            SqliteQuarterLineRepository(conn).append(
                game_ref_id=gref.reference_id, provider="balldontlie", provider_game_id="1",
                period=1, side="home", points=30, observed_at=obs, ingested_at=obs, run_id="run_x",
                raw_response_id="raw_x", raw_response_hash="c")
            SqlitePlaySnapshotRepository(conn).append(
                game_ref_id=gref.reference_id, provider="balldontlie", provider_game_id="1",
                play_identity="p1", observed_at=obs, ingested_at=obs, run_id="run_x",
                raw_response_id="raw_x", raw_response_hash="c")
            SqliteInjurySnapshotRepository(conn).append(
                player_ref_id=pref.reference_id, provider="balldontlie", provider_player_id="7",
                status="Out", observed_at=obs, ingested_at=obs, run_id="run_x",
                raw_response_id="raw_x", raw_response_hash="c")
    for table in ("nba_quarter_lines", "play_snapshots", "injury_snapshots"):
        with db.connection() as conn:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(f"UPDATE {table} SET provider='x'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(f"DELETE FROM {table}")


# --------------------------------------------------------------------------- #
# 3-4. Normal sweep + --game-id
# --------------------------------------------------------------------------- #
async def test_normal_one_day_sweep(db: Database) -> None:
    r = await _run(db, router(games=page([game(periods=four_quarters())])),
                   from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    assert r.status == "succeeded"
    assert r.games_received == 1
    assert _count(db, "game_schedule_snapshots") == 1
    assert _count(db, "game_result_snapshots") == 1
    assert _count(db, "team_game_statistics") == 2
    assert _count(db, "player_game_statistics", "WHERE role='batting'") == 1
    assert _count(db, "player_game_statistics", "WHERE role='pitching'") == 1  # advanced bucket
    assert _count(db, "nba_quarter_lines") == 8  # 4 periods x 2 sides
    assert _count(db, "play_snapshots") == 1
    assert _count(db, "lineup_snapshots") == 1
    assert _count(db, "lineup_players") == 2


async def test_game_id_ingestion(db: Database) -> None:
    r = await _run(db, router(), game_id=18444208, includes=("results", "quarters"))
    assert r.status == "succeeded"
    assert r.games_received == 1
    assert _count(db, "game_result_snapshots") == 1


# --------------------------------------------------------------------------- #
# 5-7. Pagination, repeated-cursor protection, truncation
# --------------------------------------------------------------------------- #
async def test_cursor_pagination_across_pages(db: Database) -> None:
    # Two play pages for one game.
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        cur = request.url.params.get("cursor")
        if p == "/v1/games":
            body: Any = page([game()])
        elif p == "/v1/plays":
            if cur is None:
                body = page([play_row(111, event_num=1)], next_cursor=1)
            else:
                body = page([play_row(222, event_num=2)], next_cursor=None)
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    r = await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("plays",))
    assert r.status == "succeeded"
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
    # The loop guard stops after the repeated cursor; it never spins to max_pages.
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
# 8-9. Provenance + reference creation
# --------------------------------------------------------------------------- #
async def test_every_output_traces_to_a_raw_response(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    tables = ("game_schedule_snapshots", "game_result_snapshots", "team_game_statistics",
              "player_game_statistics", "nba_quarter_lines", "play_snapshots", "lineup_snapshots")
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
    # Box is TEAM-level only (player stats come from /v1/stats): players referenced are
    # 111 (stats/advanced/plays/lineup) and 333 (lineup). Player 222 appears only in the
    # box player array, which the team-level box path deliberately does not persist.
    assert _count(db, "provider_player_references") == 2


# --------------------------------------------------------------------------- #
# 10-13. Transition semantics on the NEW nba repos (deterministic observed_at)
# --------------------------------------------------------------------------- #
def _seed_ref(conn: sqlite3.Connection) -> tuple[str, str]:
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
    return gref.reference_id, "raw_t"


def _t(n: int) -> str:
    return f"2024-04-09T{n:02d}:00:00.000000Z"


def test_quarter_identical_replay_unchanged(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, raw = _seed_ref(conn)
        repo = SqliteQuarterLineRepository(conn)
        kw: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1", period=1,
            side="home", points=30, ingested_at=_t(1), run_id="run_t", raw_response_id=raw,
            raw_response_hash="c")
        _id1, o1 = repo.append(observed_at=_t(1), **kw)
        _id2, o2 = repo.append(observed_at=_t(2), **kw)
        assert o1 is ObservationOutcome.INSERTED and o2 is ObservationOutcome.UNCHANGED
        assert repo.count() == 1


def test_quarter_change_appends_and_a_b_a_preserved(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, raw = _seed_ref(conn)
        repo = SqliteQuarterLineRepository(conn)
        base: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1", period=1,
            side="home", ingested_at=_t(1), run_id="run_t", raw_response_id=raw,
            raw_response_hash="c")
        for pts, ts in ((30, 1), (28, 2), (30, 3)):  # A -> B -> A
            repo.append(points=pts, observed_at=_t(ts), **base)
        assert repo.count() == 3  # all three retained


def test_play_out_of_order_does_not_regress(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, raw = _seed_ref(conn)
        repo = SqlitePlaySnapshotRepository(conn)
        base: dict[str, Any] = dict(
            game_ref_id=gref, provider="balldontlie", provider_game_id="1",
            play_identity="p1", ingested_at=_t(1), run_id="run_t", raw_response_id=raw,
            raw_response_hash="c")
        # A later observation, then an EARLIER out-of-order backfill with different content.
        repo.append(description="corrected text", observed_at=_t(5), **base)
        _id, out = repo.append(description="original text", observed_at=_t(2), **base)
        # The earlier observation is kept as a historical row (compared only against its
        # own temporal predecessor), and it does NOT regress current state.
        assert out is ObservationOutcome.INSERTED
        assert repo.count() == 2
        newest = conn.execute(
            "SELECT description FROM play_snapshots ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()[0]
        assert newest == "corrected text"  # current state unchanged by the backfill


# --------------------------------------------------------------------------- #
# 14-17. NBA result correction semantics (reused D2 result repo)
# --------------------------------------------------------------------------- #
async def test_live_progression_produces_no_corrections(db: Database) -> None:
    for status, hs, vs, period in [("2024-04-09T23:30:00Z", None, None, 0),
                                   ("2nd Qtr", 50, 48, 2),
                                   ("Final", 120, 110, 4)]:
        await _run(db, router(games=page([game(status=status, home_score=hs,
                                                    visitor_score=vs, period=period)])),
                       from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots", "WHERE is_correction=1") == 0


async def test_in_progress_to_final_same_score_no_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="4th Qtr", home_score=120,
                                           visitor_score=110, period=4)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final", home_score=120,
                                           visitor_score=110, period=4)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots", "WHERE is_correction=1") == 0


async def test_final_score_revision_is_a_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="Final", home_score=120, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final", home_score=118,
                                            visitor_score=110)])),
                   from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots", "WHERE is_correction=1") == 1


async def test_winner_revision_is_a_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="Final", home_score=120, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final", home_score=108, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots", "WHERE is_correction=1") == 1


async def test_status_only_change_is_not_a_correction(db: Database) -> None:
    await _run(db, router(games=page([game(status="Final", home_score=120, visitor_score=110)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    await _run(db, router(games=page([game(status="Final/OT wording", home_score=120,
                                            visitor_score=110)])),
                   from_date="2024-04-09", to_date="2024-04-09", includes=("results",))
    assert _count(db, "game_result_snapshots", "WHERE is_correction=1") == 0


# --------------------------------------------------------------------------- #
# 18-19. Quarter lines: missing != zero, overtime supported
# --------------------------------------------------------------------------- #
async def test_missing_quarter_is_not_stored_as_zero(db: Database) -> None:
    periods = [
        {"period": 1, "home": 30, "away": 28},
        {"period": 2, "home": 0},  # away MISSING (not played/supplied); home explicit 0
    ]
    await _run(db, router(games=page([game(periods=periods)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("quarters",))
    with db.connection() as conn:
        rows = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT period, side, points FROM nba_quarter_lines")}
    assert rows == {(1, "home"): 30, (1, "away"): 28, (2, "home"): 0}  # no (2, away) row
    assert (2, "away") not in rows  # missing stays missing, never a fabricated 0


async def test_overtime_periods_supported(db: Database) -> None:
    periods = four_quarters() + [{"period": 5, "home": 12, "away": 10}]  # OT
    await _run(db, router(games=page([game(periods=periods, period=5)])),
               from_date="2024-04-09", to_date="2024-04-09", includes=("quarters",))
    assert _count(db, "nba_quarter_lines", "WHERE period=5") == 2


# --------------------------------------------------------------------------- #
# 20-21. Injuries: missing == unknown (never healthy), append-only transitions
# --------------------------------------------------------------------------- #
async def test_missing_injury_status_is_unknown_not_healthy(db: Database) -> None:
    injuries = page([injury_row(status=None, return_date=None, description=None)])
    client = router(injuries=injuries)
    try:
        await ingest_injuries(database=db, client=client, date="2024-04-09")
    finally:
        await client.aclose()
    with db.connection() as conn:
        statuses = {row[0] for row in conn.execute("SELECT status FROM injury_snapshots")}
    assert statuses == {"unknown"}
    assert "healthy" not in statuses and "active" not in statuses


async def test_injury_status_transitions_are_append_only(db: Database) -> None:
    for status in ("Questionable", "Out", "Questionable"):  # A -> B -> A
        c = router(injuries=page([injury_row(status=status, return_date=None, description=None)]))
        await ingest_injuries(database=db, client=c, date="2024-04-09")
        await c.aclose()
    assert _count(db, "injury_snapshots") == 3  # all three retained
    with db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE injury_snapshots SET status='x'")


# --------------------------------------------------------------------------- #
# 22-23. Lineups never fabricate confirmed starters; plays honest when absent
# --------------------------------------------------------------------------- #
async def test_lineups_never_confirm_pregame_starters(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=("lineups",))
    assert _count(db, "lineup_snapshots") == 1
    assert _count(db, "lineup_snapshots", "WHERE is_confirmed=1") == 0  # never confirmed


async def test_plays_absent_recorded_honestly(db: Database) -> None:
    r = await _run(db, router(plays=page([])), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("plays",))
    assert r.status == "succeeded"  # empty plays is not a failure
    assert r.play_observations == 0
    assert _count(db, "play_snapshots") == 0


# --------------------------------------------------------------------------- #
# 24-26. Tier restriction, auth failure, malformed required response
# --------------------------------------------------------------------------- #
async def test_tier_restricted_optional_endpoint_is_capability_unavailable(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/v1/games":
            return httpx.Response(200, json=page([game()]),
                                  headers={"content-type": "application/json"})
        if p == "/v1/plays":  # GOAT-only endpoint gated on this tier
            return httpx.Response(403, json={"error": "upgrade required to access plays"},
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, json={"data": []},
                              headers={"content-type": "application/json"})

    r = await _run(db, _bdl(handler), from_date="2024-04-09", to_date="2024-04-09",
                   includes=("results", "plays"))
    assert r.status == "succeeded"  # NOT a failure
    assert r.active_failures == 0
    assert r.capabilities_unavailable >= 1
    assert _count(db, "game_result_snapshots") == 1  # unrelated group still ingested


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
# 27-28. Dry-run persists nothing; persisted ingestion idempotent
# --------------------------------------------------------------------------- #
async def test_dry_run_creates_no_database_and_persists_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "never_created.db"
    database = Database(missing)
    r = await _run(database, router(), from_date="2024-04-09", to_date="2024-04-09",
                   includes=ALL_NBA, dry_run=True)
    assert r.dry_run and r.status == "succeeded"
    assert r.observations_normalized > 0  # normalization ran
    assert r.rows_persisted == 0
    assert r.run_id is None
    assert not missing.exists()  # no database file created


async def test_persisted_ingestion_is_idempotent(db: Database) -> None:
    await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    total_after_first = _count(db, "play_snapshots") + _count(db, "game_result_snapshots") \
        + _count(db, "nba_quarter_lines") + _count(db, "player_game_statistics")
    r2 = await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    assert r2.records_inserted == 0 and r2.records_changed == 0
    assert r2.records_unchanged > 0
    total_after_second = _count(db, "play_snapshots") + _count(db, "game_result_snapshots") \
        + _count(db, "nba_quarter_lines") + _count(db, "player_game_statistics")
    assert total_after_first == total_after_second


# --------------------------------------------------------------------------- #
# 29-31. hoopR offline Parquet import
# --------------------------------------------------------------------------- #
def _write_pbp(path: Path, *, sub_text: str = "substitution") -> None:
    table = pa.table({
        "game_id": ["401585", "401585", "401585"],
        "sequence_number": [1, 2, 3],
        "period_number": [1, 1, 1],
        "clock_display_value": ["11:34", "10:00", "9:12"],
        "type_text": ["Made Shot", sub_text, "Rebound"],
        "text": ["X makes shot", "Y enters", "Z rebound"],
        "team_id": ["2", "2", "14"],
        "athlete_id_1": ["111", "222", "333"],
    })
    pq.write_table(table, path)


def test_hoopr_import_succeeds(db: Database, tmp_path: Path) -> None:
    f = tmp_path / "pbp.parquet"
    _write_pbp(f)
    r = import_hoopr_parquet(database=db, path=f)
    assert r.status == "succeeded"
    assert r.rows_read == 3 and r.records_inserted == 3
    assert r.file_sha256 is not None
    with db.connection() as conn:
        providers = {row[0] for row in conn.execute("SELECT DISTINCT provider FROM play_snapshots")}
        subs = conn.execute("SELECT COUNT(*) FROM play_snapshots WHERE is_substitution=1").fetchone()[0]
    assert providers == {"hoopr"}  # no mixing with live balldontlie
    assert subs == 1


def test_hoopr_duplicate_import_is_idempotent(db: Database, tmp_path: Path) -> None:
    f = tmp_path / "pbp.parquet"
    _write_pbp(f)
    import_hoopr_parquet(database=db, path=f)
    r2 = import_hoopr_parquet(database=db, path=f)
    assert r2.records_inserted == 0 and r2.records_changed == 0
    assert r2.records_unchanged == 3
    assert _count(db, "play_snapshots") == 3


def test_hoopr_unsupported_schema_is_rejected(db: Database, tmp_path: Path) -> None:
    bad = tmp_path / "bad.parquet"
    pq.write_table(pa.table({"foo": [1], "bar": [2]}), bad)
    r = import_hoopr_parquet(database=db, path=bad)
    assert r.status == "failed"
    assert r.error_type == "HooprImportError"
    # unsupported schema NAME
    good = tmp_path / "pbp.parquet"
    _write_pbp(good)
    r2 = import_hoopr_parquet(database=db, path=good, schema="nope")
    assert r2.status == "failed"


def test_hoopr_dry_run_persists_nothing(db: Database, tmp_path: Path) -> None:
    f = tmp_path / "pbp.parquet"
    _write_pbp(f)
    r = import_hoopr_parquet(database=db, path=f, dry_run=True)
    assert r.observations_normalized == 3 and r.rows_persisted == 0
    assert _count(db, "play_snapshots") == 0


# --------------------------------------------------------------------------- #
# 32. CLI JSON + exit codes
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

    # Exit 3 when the database is missing/unmigrated (non-dry-run).
    missing = tmp_path / "missing.db"
    code3 = run_ingest_nba(settings, from_date="2024-04-09", database_path=missing,
                           out=lambda _s: None, client=router())
    assert code3 == 3

    # Exit 1 for a bad argument combination.
    code1 = run_ingest_nba(settings, from_date="2024-04-09", game_id=1, database_path=dbp,
                           out=lambda _s: None, client=router())
    assert code1 == 1


# --------------------------------------------------------------------------- #
# 33. Secrets never appear in output or persistence
# --------------------------------------------------------------------------- #
async def test_no_secret_in_database_or_output(db: Database) -> None:
    r = await _run(db, router(), from_date="2024-04-09", to_date="2024-04-09", includes=ALL_NBA)
    text = json.dumps(r.__dict__, default=str)
    assert SENTINEL not in text
    with db.connection() as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in conn.execute(f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, str):
                        assert SENTINEL not in value, f"secret leaked into {table}"
        # No authorization header is ever stored.
        for (headers,) in conn.execute("SELECT response_headers_json FROM raw_responses"):
            assert "authorization" not in json.loads(headers)


# --------------------------------------------------------------------------- #
# 34. D2 MLB behaviour remains unchanged
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
    assert _count(db, "nba_quarter_lines") == 0  # MLB never writes NBA tables
