"""Canonical single-day manifest execution (offline; mocked transport, sockets sentineled).

A manifest records a single day as the bare date -- the planner's ``_range_key``
collapses ``from == to`` -- so the manifest-to-execution boundary must expand it back
to an INCLUSIVE ``(d, d)`` pair. It previously returned ``(d, None)``, which made
``BalldontlieClient.fetch_games`` raise ``start_date and end_date must be provided
together`` BEFORE transport, rendering every canonical single-day NBA manifest
unexecutable (the committed ``pilots/f1b/nba_skeleton.manifest.json``).

The regression here drives the REAL F1B orchestration (``run_pilot_cli``) and the REAL
``BalldontlieClient`` request construction against a mocked HTTP transport -- not the
``_FakeExecutor`` used by ``test_f1a_pilot``, which never reaches client validation and
is exactly why the defect shipped. Nothing here loads a real key, touches the
development corpus, or opens a socket.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.checkpoint import load_checkpoint
from sports_quant.ingest.f1a import (
    _F1B_AUTHORIZED_ENV,
    EXIT_USAGE,
    _parse_date_range,
    run_pilot_cli,
)
from sports_quant.ingest.manifest import canonical_json, load_and_validate, plan_hash
from sports_quant.ingest.planning import Bounds, build_plan
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.request_control import RequestGate

#: A stand-in key. Never a real credential; asserted absent from every artifact.
SENTINEL = "sk-nba-single-day-sentinel-do-not-store"

REPO = Path(__file__).resolve().parents[3]
NBA_MANIFEST = REPO / "pilots" / "f1b" / "nba_skeleton.manifest.json"
MLB_MANIFEST = REPO / "pilots" / "f1b" / "mlb_skeleton.manifest.json"

#: The committed artifacts are pinned: this repair must not perturb their bytes.
NBA_SHA256 = "6fe6dc37ec4d5868c7f456ba231d4b8c0f6edbda940fdba6f3f41acbb4b1f446"
MLB_SHA256 = "fa28695b043eb38da3de13c1a49dd24adef022d83f40d870e495968351c4cf3b"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict sentinel for synchronous, zero-network tests (repo convention)."""

    def boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outbound-access sentinel for tests that drive ``asyncio.run``.

    ``socket.socket`` itself cannot be stubbed wholesale here: the Windows proactor
    event loop builds an internal loopback self-pipe, so patching the constructor (or
    ``connect`` unconditionally) fails the test for a reason unrelated to network
    access -- which is why the existing suite applies the strict sentinel only to
    synchronous ``--plan`` tests. Instead every name resolution is blocked and every
    connect to a NON-loopback address is blocked, so a real call to
    ``api.balldontlie.io`` cannot happen while asyncio's loopback plumbing still works.
    """

    real_connect = socket.socket.connect

    def boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("external network access attempted in an offline test")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            raise AssertionError(
                f"external connect to {host!r} attempted in an offline test")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
def f1b_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_F1B_AUTHORIZED_ENV, "1")


def _game(gid: int, *, date: str = "2026-01-05") -> dict[str, Any]:
    return {
        "id": gid, "date": date, "season": 2025, "status": "Final", "period": 4,
        "postseason": False, "home_team_score": 112, "visitor_team_score": 104,
        "home_team": {"id": 2, "abbreviation": "BOS", "full_name": "Boston Celtics"},
        "visitor_team": {"id": 14, "abbreviation": "LAL", "full_name": "Los Angeles Lakers"},
    }


def _factory(seen: list[httpx.Request], *, games: Optional[dict[str, Any]] = None):
    """Build a REAL BalldontlieClient over a mocked transport, gated by the pilot."""

    body = games if games is not None else {
        "data": [_game(1_000_001), _game(1_000_002)],
        "meta": {"next_cursor": None},          # single page -> exactly one request
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/games":
            return httpx.Response(200, json=body,
                                  headers={"content-type": "application/json"})
        # Any other path would be an out-of-scope (rich) call: fail loudly.
        return httpx.Response(500, json={"error": f"unexpected path {request.url.path}"})

    def factory(gate: RequestGate) -> BalldontlieClient:
        http = build_readonly_client(
            base_url="https://api.balldontlie.io",
            policy=ReadOnlyHTTPPolicy.for_balldontlie(),
            inner_transport=httpx.MockTransport(handler))
        return BalldontlieClient(SENTINEL, client=http, gate=gate, league="nba")

    return factory


# --------------------------------------------------------------------------- #
# The live-path regression: committed single-day manifest -> mocked /v1/games
# --------------------------------------------------------------------------- #
def test_committed_single_day_nba_manifest_reaches_games_transport(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    """The exact committed NBA manifest must execute and send BOTH range endpoints."""

    db, ckpt = tmp_path / "nba.db", tmp_path / "nba.ckpt"
    initialize_database(db)
    seen: list[httpx.Request] = []
    lines: list[str] = []

    rc = run_pilot_cli(league="nba", manifest_path=NBA_MANIFEST, scratch_db=db,
                       checkpoint=ckpt, out=lines.append,
                       client_factory=_factory(seen))

    assert rc == 0, "\n".join(lines)

    # 1. Exactly ONE transport request for a single-page response, and it is /v1/games.
    assert len(seen) == 1, [str(r.url) for r in seen]
    assert seen[0].url.path == "/v1/games"
    assert seen[0].method == "GET"

    # 2. BOTH inclusive range endpoints are present and equal (the repaired behaviour).
    params = dict(seen[0].url.params)
    assert params["start_date"] == "2026-01-05"
    assert params["end_date"] == "2026-01-05"

    # 3. No rich endpoint was called (skeleton stage, families == ["games"]).
    rich = ("/v1/box_scores", "/v1/stats", "/nba/v1/stats/advanced", "/v1/plays",
            "/v1/lineups", "/v1/player_injuries", "/v1/players")
    assert not [r for r in seen if r.url.path in rich]
    assert {r.url.path for r in seen} == {"/v1/games"}

    # 4. The request stayed budget-gated: the gate accounted exactly one attempt
    #    against the manifest's cap of 8, and reports the rate contract.
    ck = load_checkpoint(ckpt)
    assert ck.state == "completed"
    assert ck.request_cap == 8
    assert ck.usage["attempted_requests"] == 1
    assert ck.usage["transport_starts"] == 1
    assert ck.usage["network_occurred"] is True
    assert ck.usage["credits_applicable"] is False
    assert ck.usage["configured_rate_per_min"] == 60
    assert ck.usage["provider_rate_limit_per_min"] == 600
    assert ck.usage["http_429s"] == 0

    # 5. The key never reaches the URL, the operator output, the manifest, the
    #    checkpoint, or any persisted raw response.
    assert SENTINEL not in str(seen[0].url)
    assert "start_date" in str(seen[0].url)          # the URL really was built
    assert SENTINEL not in "\n".join(lines)
    assert SENTINEL not in NBA_MANIFEST.read_text(encoding="utf-8")
    assert SENTINEL not in ckpt.read_text(encoding="utf-8")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("select endpoint, request_params_json, response_headers_json, "
                           "body from raw_responses").fetchall()
        assert rows, "the skeleton unit must have persisted its raw response"
        blob = json.dumps(rows)
        assert SENTINEL not in blob
        assert "authorization" not in blob.lower()
        # The sanitized endpoint is a path only -- never a URL with a query string.
        assert all(r[0].startswith("/") and "?" not in r[0] for r in rows)
    finally:
        con.close()


def test_old_parser_semantics_raise_before_transport(no_external_network: None) -> None:
    """The pre-repair contract must fail: `to_date=None` raises before any request.

    This is the defect the regression above pins. Passing only ``start_date`` (what
    ``(d, None)`` produced) must still be refused by the client -- the BALLDONTLIE
    requirement that both endpoints travel together is deliberately NOT weakened.
    """

    import asyncio

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [], "meta": {"next_cursor": None}})

    http = build_readonly_client(
        base_url="https://api.balldontlie.io",
        policy=ReadOnlyHTTPPolicy.for_balldontlie(),
        inner_transport=httpx.MockTransport(handler))
    client = BalldontlieClient(SENTINEL, client=http, require_gate=False)

    async def go() -> None:
        with pytest.raises(ValueError, match="must be provided together"):
            await client.fetch_games(start_date="2026-01-05", end_date=None)
        await client.aclose()

    asyncio.run(go())
    assert seen == []  # raised BEFORE transport, exactly as the pilot failure did


# --------------------------------------------------------------------------- #
# Parser contract
# --------------------------------------------------------------------------- #
def test_single_day_parser_expands_to_inclusive_pair() -> None:
    assert _parse_date_range("2026-01-05") == ("2026-01-05", "2026-01-05")


def test_multi_day_parser_unchanged() -> None:
    assert _parse_date_range("2026-07-20..2026-07-21") == ("2026-07-20", "2026-07-21")
    assert _parse_date_range("2026-01-05..2026-01-05") == ("2026-01-05", "2026-01-05")


@pytest.mark.parametrize("bad", [
    "",                        # empty
    "..",                      # empty both sides
    "2026-01-05..",            # empty end
    "..2026-01-05",            # empty start
    "notadate",                # malformed
    "20260105",                # not the canonical YYYY-MM-DD shape
    "2026-13-01",              # not a real calendar date
    "2026-02-30",              # not a real calendar date
    "2026-01-06..2026-01-05",  # reversed
])
def test_invalid_ranges_fail_closed(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_date_range(bad)


def test_pilot_refuses_malformed_manifest_range_before_side_effects(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    """A canonical manifest carrying an impossible date fails closed, zero transport."""

    body = json.loads(NBA_MANIFEST.read_text(encoding="utf-8"))
    body["date_range"] = "2026-13-01"
    body["plan_body"]["date_range"] = "2026-13-01"
    tampered = tmp_path / "bad.json"
    tampered.write_text(canonical_json(body), encoding="utf-8")

    db, ckpt = tmp_path / "nba.db", tmp_path / "nba.ckpt"
    initialize_database(db)
    seen: list[httpx.Request] = []
    lines: list[str] = []

    rc = run_pilot_cli(league="nba", manifest_path=tampered, scratch_db=db,
                       checkpoint=ckpt, out=lines.append,
                       client_factory=_factory(seen))

    assert rc == EXIT_USAGE
    assert seen == []                 # no client work, no transport
    assert not ckpt.exists()          # no checkpoint written
    assert any("rejected" in line for line in lines), lines


# --------------------------------------------------------------------------- #
# The committed artifacts must be untouched and still rebuild to the same hash
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("league,provider,path,sha", [
    ("nba", "balldontlie", NBA_MANIFEST, NBA_SHA256),
    ("mlb", "mlb_statsapi", MLB_MANIFEST, MLB_SHA256),
])
def test_committed_manifest_bytes_and_plan_hash_unchanged(
    league: str, provider: str, path: Path, sha: str
) -> None:
    import hashlib

    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == sha, f"{path.name} bytes changed"

    m = load_and_validate(path, expected_league=league, expected_provider=provider)
    assert m.manifest_hash() == sha        # manifest_hash IS the canonical byte digest

    # Rebuilding the plan through the repaired parsing boundary reproduces the hash.
    from_date, to_date = _parse_date_range(m.date_range)
    bounds = Bounds(max_games=m.max_games, max_pages=m.max_pages,
                    max_records=m.max_records, max_retries=m.max_retries,
                    rate_per_min=m.plan_body.get("bounds", {}).get("rate_per_min"))
    rebuilt = build_plan(league=league, from_date=from_date, to_date=to_date,
                         families=m.families, stage=m.stage, bounds=bounds)
    assert plan_hash(rebuilt) == m.computed_plan_hash()
    # The canonical representation of a single day stays the bare date.
    assert rebuilt.date_range == m.date_range
