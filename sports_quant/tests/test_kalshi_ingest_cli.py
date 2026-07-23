"""``ingest-kalshi`` CLI: exit codes, dry-run, zero-results, GET-only, no credential.

The command must never require or display a Kalshi credential, use GET only,
treat zero matching markets as success, and exit non-zero only on a genuine
provider/parse/persist failure.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sports_quant.cli import run_ingest_kalshi
from sports_quant.config import PRODUCTION_KALSHI_REST_URL, Settings
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.kalshi import DEFAULT_BASE_URL, KalshiClient

EVENTS = {"events": [{"event_ticker": "EV-1", "status": "open"}], "cursor": ""}
MARKETS = {"markets": [{"ticker": "MKT-1", "event_ticker": "EV-1", "status": "open"}], "cursor": ""}


def _settings() -> Settings:
    return Settings(
        odds_api_key=SecretStr(""),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=True,
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )


def _client(handler, seen: list[httpx.Request] | None = None) -> KalshiClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    http = build_readonly_client(
        base_url=DEFAULT_BASE_URL,
        policy=ReadOnlyHTTPPolicy.for_kalshi(DEFAULT_BASE_URL),
        inner_transport=httpx.MockTransport(wrapped),
    )
    return KalshiClient(base_url=DEFAULT_BASE_URL, client=http)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/events"):
        return httpx.Response(200, json=EVENTS)
    if path.endswith("/markets"):
        return httpx.Response(200, json=MARKETS)
    return httpx.Response(404, json={})


def test_success_exit_zero_get_only_no_auth(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    seen: list[httpx.Request] = []
    client = _client(_ok_handler, seen)
    lines: list[str] = []

    code = run_ingest_kalshi(
        _settings(), status="open", limit=5, database_path=db, out=lines.append, client=client
    )

    assert code == 0
    assert seen and {r.method for r in seen} == {"GET"}
    for request in seen:
        assert "authorization" not in {k.lower() for k in request.headers}
    output = "\n".join(lines)
    assert "GET-only" in output
    assert "events=1" in output


def test_zero_results_exits_zero(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)

    def empty(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": [], "cursor": ""})
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    lines: list[str] = []
    code = run_ingest_kalshi(
        _settings(), status="open", limit=5, database_path=db, out=lines.append,
        client=_client(empty),
    )
    assert code == 0
    assert "0 matching events/markets" in "\n".join(lines)


def test_provider_failure_exits_one(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    lines: list[str] = []
    code = run_ingest_kalshi(
        _settings(), status="open", limit=5, database_path=db, out=lines.append,
        client=_client(boom),
    )
    assert code == 1
    assert "FAILED" in "\n".join(lines)


def test_dry_run_needs_no_database_and_persists_nothing(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    lines: list[str] = []
    code = run_ingest_kalshi(
        _settings(), status="open", limit=5, database_path=db, dry_run=True, out=lines.append,
        client=_client(_ok_handler),
    )
    assert code == 0
    assert not db.exists()
    assert "DRY-RUN" in "\n".join(lines)


def test_missing_database_exits_three(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    lines: list[str] = []
    code = run_ingest_kalshi(
        _settings(), status="open", limit=5, database_path=db, out=lines.append,
        client=_client(_ok_handler),
    )
    assert code == 3
    assert "db-init" in "\n".join(lines)


def test_read_only_violation_exits_two(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    initialize_database(db)
    settings = Settings(
        odds_api_key=SecretStr(""),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=False,  # violation
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )
    from sports_quant.config import ReadOnlyStartupError

    with pytest.raises(ReadOnlyStartupError):
        run_ingest_kalshi(settings, status="open", database_path=db, out=lambda _s: None,
                          client=_client(_ok_handler))
