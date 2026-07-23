"""``ingest-odds`` CLI: exit codes, dry-run, key never printed, GET-only.

The command must never place or simulate an order, never print the Odds API
key, use GET only, treat an out-of-season sport as a successful zero-event run,
and exit non-zero only on a genuine active failure.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sports_quant.cli import run_ingest_odds
from sports_quant.config import PRODUCTION_KALSHI_REST_URL, Settings
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.odds_api import DEFAULT_BASE_URL, OddsApiClient

API_KEY = "ingest-cli-key-do-not-log"

PAYLOAD = [
    {
        "id": "mlb-1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-07-22T23:05:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-07-22T22:50:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-07-22T22:50:00Z",
                        "outcomes": [
                            {"name": "New York Yankees", "price": -140},
                            {"name": "Boston Red Sox", "price": 120},
                        ],
                    }
                ],
            }
        ],
    }
]


def _settings() -> Settings:
    return Settings(
        odds_api_key=SecretStr(API_KEY),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=True,
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )


def _client(handler, seen: list[httpx.Request] | None = None) -> OddsApiClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    http = build_readonly_client(
        base_url=DEFAULT_BASE_URL,
        policy=ReadOnlyHTTPPolicy.for_odds_api(),
        inner_transport=httpx.MockTransport(wrapped),
    )
    return OddsApiClient(API_KEY, client=http)


def test_success_exit_zero_and_key_never_printed(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    seen: list[httpx.Request] = []
    client = _client(lambda r: httpx.Response(200, json=PAYLOAD), seen)
    lines: list[str] = []

    code = run_ingest_odds(
        _settings(), sport="mlb", database_path=db, out=lines.append, client=client
    )

    assert code == 0
    output = "\n".join(lines)
    assert API_KEY not in output
    assert "price snapshots" in output
    # Every request was a GET.
    assert seen and {r.method for r in seen} == {"GET"}


def test_active_failure_exits_one(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    client = _client(lambda r: httpx.Response(500, json={"message": "boom"}))
    lines: list[str] = []

    code = run_ingest_odds(
        _settings(), sport="mlb", database_path=db, out=lines.append, client=client
    )

    assert code == 1
    assert "FAILED" in "\n".join(lines)
    assert API_KEY not in "\n".join(lines)


def test_no_games_available_exits_zero(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    client = _client(lambda r: httpx.Response(200, json=[]))
    lines: list[str] = []

    code = run_ingest_odds(
        _settings(), sport="nba", database_path=db, out=lines.append, client=client
    )
    assert code == 0
    assert "0 available events" in "\n".join(lines)


def test_dry_run_needs_no_database_and_persists_nothing(tmp_path: Path) -> None:
    # Deliberately point at a path with no database file.
    db = tmp_path / "absent.db"
    client = _client(lambda r: httpx.Response(200, json=PAYLOAD))
    lines: list[str] = []

    code = run_ingest_odds(
        _settings(), sport="mlb", database_path=db, dry_run=True, out=lines.append, client=client
    )

    assert code == 0
    assert not db.exists()
    assert "DRY-RUN" in "\n".join(lines)


def test_missing_database_exits_three(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    client = _client(lambda r: httpx.Response(200, json=PAYLOAD))
    lines: list[str] = []

    code = run_ingest_odds(
        _settings(), sport="mlb", database_path=db, out=lines.append, client=client
    )
    assert code == 3
    assert "db-init" in "\n".join(lines)


def test_unmigrated_database_exits_three(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # a file with no schema
    client = _client(lambda r: httpx.Response(200, json=PAYLOAD))
    lines: list[str] = []

    code = run_ingest_odds(
        _settings(), sport="mlb", database_path=db, out=lines.append, client=client
    )
    assert code == 3


def test_missing_key_is_a_skip(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    settings = Settings(
        odds_api_key=SecretStr(""),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=True,
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )
    lines: list[str] = []
    code = run_ingest_odds(settings, sport="mlb", database_path=db, out=lines.append)
    assert code == 0
    assert "not configured" in "\n".join(lines)


@pytest.mark.parametrize("read_only_flag", [False])
def test_read_only_violation_exits_two(tmp_path: Path, read_only_flag: bool) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    settings = Settings(
        odds_api_key=SecretStr(API_KEY),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=read_only_flag,
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )
    from sports_quant.config import ReadOnlyStartupError

    with pytest.raises(ReadOnlyStartupError):
        run_ingest_odds(settings, sport="mlb", database_path=db, out=lambda _s: None)
