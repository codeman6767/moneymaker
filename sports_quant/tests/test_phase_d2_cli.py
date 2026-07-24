"""Phase D2 CLI: ingest-mlb / ingest-lineups exit codes, dry-run, JSON, args."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from sports_quant.cli import run_ingest_lineups, run_ingest_mlb
from sports_quant.config import PRODUCTION_KALSHI_REST_URL, Settings
from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

SCHEDULE = {
    "dates": [{"date": "2024-04-09", "games": [{
        "gamePk": 745804, "gameType": "R", "season": "2024", "officialDate": "2024-04-09",
        "gameDate": "2024-04-09T23:05:00Z",
        "status": {"abstractGameState": "Final", "codedGameState": "F", "detailedState": "Final"},
        "teams": {"home": {"team": {"id": 133}}, "away": {"team": {"id": 147}}},
        "venue": {"id": 10}, "gameNumber": 1, "doubleHeader": "N",
        "lineups": {"homePlayers": [{"id": 111}], "awayPlayers": [{"id": 211}]},
    }]}]
}


def _settings() -> Settings:
    return Settings(
        odds_api_key=SecretStr(""), nba_data_api_key=SecretStr(""),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL, kalshi_environment="production",
        read_only_mode=True, order_submission_enabled=False, paper_trading=False,
        live_trading=False, manual_live_arming=False,
    )


def _client(status: int = 200, body: Any = None) -> MlbStatsApiClient:
    body = SCHEDULE if body is None else body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    return MlbStatsApiClient(client=http, max_retries=0)


def test_ingest_mlb_success_and_json(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_mlb(
        _settings(), from_date="2024-04-09", database_path=db, as_json=True,
        out=lines.append, client=_client(),
    )
    assert code == 0
    payload = json.loads(lines[0])
    assert payload["command"] == "ingest-mlb"
    assert payload["status"] == "succeeded"
    assert payload["games_received"] == 1
    assert payload["schedule_snapshots_inserted"] == 1


def test_ingest_mlb_dry_run_needs_no_db(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    lines: list[str] = []
    code = run_ingest_mlb(
        _settings(), from_date="2024-04-09", database_path=db, dry_run=True,
        out=lines.append, client=_client(),
    )
    assert code == 0
    assert not db.exists()
    assert "DRY-RUN" in "\n".join(lines)


def test_ingest_mlb_missing_db_exits_three(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    lines: list[str] = []
    code = run_ingest_mlb(
        _settings(), from_date="2024-04-09", database_path=db, out=lines.append, client=_client(),
    )
    assert code == 3
    assert "db-init" in "\n".join(lines)


def test_ingest_mlb_provider_failure_exits_one(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_mlb(
        _settings(), from_date="2024-04-09", database_path=db, out=lines.append,
        client=_client(status=500),
    )
    assert code == 1
    assert "FAILED" in "\n".join(lines)


def test_ingest_mlb_zero_games_exits_zero(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_mlb(
        _settings(), from_date="2024-04-09", database_path=db, out=lines.append,
        client=_client(body={"dates": []}),
    )
    assert code == 0


def test_ingest_mlb_active_subfetch_failure_exits_one(tmp_path: Path) -> None:
    # Schedule OK, but the requested box sub-fetch 500s -> partially_failed -> exit 1.
    db = tmp_path / "corpus.db"
    initialize_database(db)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/boxscore"):
            return httpx.Response(500, json={"e": 1}, headers={"content-type": "application/json"})
        return httpx.Response(200, json=SCHEDULE, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    client = MlbStatsApiClient(client=http, max_retries=0)
    lines: list[str] = []
    code = run_ingest_mlb(
        _settings(), from_date="2024-04-09", includes=("box",), database_path=db, as_json=True,
        out=lines.append, client=client,
    )
    assert code == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "partially_failed"
    assert payload["active_failure"] is True


def test_ingest_mlb_incompatible_args_exit_one(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_mlb(
        _settings(), from_date="2024-04-09", game_pk=745804, database_path=db,
        out=lines.append, client=_client(),
    )
    assert code == 1
    assert "cannot be combined" in "\n".join(lines)


def test_ingest_mlb_json_has_no_raw_body(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    run_ingest_mlb(
        _settings(), from_date="2024-04-09", includes=("box",), database_path=db, as_json=True,
        out=lines.append, client=_client(),
    )
    payload = json.loads(lines[0])
    dumped = json.dumps(payload)
    # The JSON is counters/status only -- never a raw response body or payload.
    assert "body" not in payload
    assert "gamePk" not in dumped and "745804" not in dumped and "teams" not in dumped


def test_ingest_lineups_success(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_lineups(
        _settings(), sport="mlb", date="2024-04-09", database_path=db, as_json=True,
        out=lines.append, client=_client(),
    )
    assert code == 0
    payload = json.loads(lines[0])
    assert payload["command"] == "ingest-lineups"
    assert payload["lineups_inserted"] == 2


def test_ingest_lineups_rejects_non_mlb_sport(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_lineups(
        _settings(), sport="nba", date="2024-04-09", database_path=db, out=lines.append,
        client=_client(),
    )
    assert code == 1


def test_ingest_lineups_dry_run_persists_nothing(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    run_ingest_lineups(
        _settings(), sport="mlb", date="2024-04-09", database_path=db, dry_run=True,
        out=lines.append, client=_client(),
    )
    with Database(db).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0] == 0
