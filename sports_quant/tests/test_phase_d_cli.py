"""Phase D1 CLI: provider-audit and ingest-venues exit codes / dry-run / JSON."""

from __future__ import annotations

from pathlib import Path

import httpx
from pydantic import SecretStr

from sports_quant.cli import run_ingest_venues, run_provider_audit
from sports_quant.config import PRODUCTION_KALSHI_REST_URL, Settings
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.provider_audit import (
    build_balldontlie_probes,
    build_mlb_statsapi_probes,
    declaration_for,
)
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.providers.capabilities import (
    PROVIDER_BALLDONTLIE,
    PROVIDER_MLB_STATSAPI,
    BalldontlieTier,
)
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

VENUES = {"venues": [{"id": 1, "name": "Park", "location": {"city": "X"}, "fieldInfo": {}}]}


def _settings() -> Settings:
    return Settings(
        odds_api_key=SecretStr(""),
        nba_data_api_key=SecretStr(""),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=True,
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )


def _mlb_client(status: int = 200) -> MlbStatsApiClient:
    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json=VENUES, headers={"content-type": "application/json"})
        ),
    )
    return MlbStatsApiClient(client=http)


def _decl():
    return declaration_for(PROVIDER_MLB_STATSAPI, balldontlie_tier=BalldontlieTier.GOAT)


def test_ingest_venues_success(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_venues(_settings(), database_path=db, out=lines.append, client=_mlb_client())
    assert code == 0
    assert "venues: 1 seen" in "\n".join(lines)


def test_ingest_venues_dry_run_needs_no_db(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    lines: list[str] = []
    code = run_ingest_venues(
        _settings(), database_path=db, dry_run=True, out=lines.append, client=_mlb_client()
    )
    assert code == 0
    assert not db.exists()
    assert "DRY-RUN" in "\n".join(lines)


def test_ingest_venues_missing_db_exits_three(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    lines: list[str] = []
    code = run_ingest_venues(_settings(), database_path=db, out=lines.append, client=_mlb_client())
    assert code == 3
    assert "db-init" in "\n".join(lines)


def test_ingest_venues_http_failure_exits_one(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    lines: list[str] = []
    code = run_ingest_venues(
        _settings(), database_path=db, out=lines.append, client=_mlb_client(status=500)
    )
    assert code == 1
    assert "FAILED" in "\n".join(lines)


def test_provider_audit_success_and_json(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    client = _mlb_client()
    lines: list[str] = []
    code = run_provider_audit(
        _settings(), provider=PROVIDER_MLB_STATSAPI, database_path=db, as_json=True,
        out=lines.append, probes=build_mlb_statsapi_probes(client), declaration=_decl(),
        client_to_close=client,
    )
    assert code == 0
    import json

    payload = json.loads(lines[0])
    assert payload["provider"] == PROVIDER_MLB_STATSAPI
    assert payload["status"] == "succeeded"
    assert payload["capabilities"]  # capability list present
    assert payload["observed_count"] > 0
    # Only probed groups are observed; declared-only capabilities are also present.
    assert payload["declared_only_count"] > 0
    assert any(c["is_observed"] for c in payload["capabilities"])
    assert any(not c["is_observed"] for c in payload["capabilities"])


def _bdl_client(handler) -> BalldontlieClient:
    http = build_readonly_client(
        base_url="https://api.balldontlie.io",
        policy=ReadOnlyHTTPPolicy.for_balldontlie(),
        inner_transport=httpx.MockTransport(handler),
    )
    return BalldontlieClient("sk-cli-test", client=http, max_retries=0)


def _bdl_decl():
    return declaration_for(PROVIDER_BALLDONTLIE, balldontlie_tier=BalldontlieTier.GOAT)


def test_provider_audit_partial_failure_exits_one(tmp_path: Path) -> None:
    """teams succeeds but games 5xx's after retries -> partially_failed -> exit 1."""

    db = tmp_path / "corpus.db"
    initialize_database(db)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/games":
            return httpx.Response(500, json={"error": "boom"},
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, json={"data": [{"id": 1}]},
                              headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    lines: list[str] = []
    code = run_provider_audit(
        _settings(), provider=PROVIDER_BALLDONTLIE, database_path=db, as_json=True,
        out=lines.append, probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
        client_to_close=client,
    )
    assert code == 1
    import json

    payload = json.loads(lines[0])
    assert payload["status"] == "partially_failed"
    assert payload["active_failure"] is True
    assert payload["probes_succeeded"] > 0


def test_provider_audit_total_failure_exits_one(tmp_path: Path) -> None:
    """A 401 everywhere -> failed -> exit 1, authenticated false."""

    db = tmp_path / "corpus.db"
    initialize_database(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"},
                              headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    lines: list[str] = []
    code = run_provider_audit(
        _settings(), provider=PROVIDER_BALLDONTLIE, database_path=db, as_json=True,
        out=lines.append, probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
        client_to_close=client,
    )
    assert code == 1
    import json

    payload = json.loads(lines[0])
    assert payload["status"] == "failed"
    assert payload["authenticated"] is False


def test_provider_audit_honest_skips_exit_zero(tmp_path: Path) -> None:
    """A completed audit with only supported/tier/skipped outcomes exits 0."""

    db = tmp_path / "corpus.db"
    initialize_database(db)

    def handler(request: httpx.Request) -> httpx.Response:
        # 200 everywhere but empty games -> plays/lineups/advanced/box skip honestly.
        return httpx.Response(200, json={"data": []}, headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    lines: list[str] = []
    code = run_provider_audit(
        _settings(), provider=PROVIDER_BALLDONTLIE, database_path=db, as_json=True,
        out=lines.append, probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
        client_to_close=client,
    )
    assert code == 0
    import json

    payload = json.loads(lines[0])
    assert payload["status"] == "succeeded"
    assert payload["active_failure"] is False
    assert payload["probes_skipped"] >= 1  # dependent probes skipped honestly


def test_provider_audit_dry_run_persists_nothing(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    client = _mlb_client()
    lines: list[str] = []
    code = run_provider_audit(
        _settings(), provider=PROVIDER_MLB_STATSAPI, database_path=db, dry_run=True,
        out=lines.append, probes=build_mlb_statsapi_probes(client), declaration=_decl(),
        client_to_close=client,
    )
    assert code == 0
    assert "DRY-RUN" in "\n".join(lines)
    from sports_quant.db.engine import Database

    with Database(db).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_capabilities").fetchone()[0] == 0
