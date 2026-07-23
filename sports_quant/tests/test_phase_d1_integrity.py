"""Phase D1 integrity repair: streaming size guard, base-URL pinning, allowlist.

Unit-level guards that need no database. Every HTTP interaction is mocked; no
live call is made.
"""

from __future__ import annotations

import httpx
import pytest

from sports_quant.config import _pinned_url_violation
from sports_quant.http_policy import (
    ReadOnlyHTTPPolicy,
    ReadOnlyPolicyError,
    build_readonly_client,
)
from sports_quant.providers.base_provider import ProviderError, ProviderErrorKind
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient


def _client(handler, *, max_body_bytes: int = 1_000_000) -> MlbStatsApiClient:
    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    return MlbStatsApiClient(client=http, max_body_bytes=max_body_bytes)


def _json_body_of_length(n: int) -> bytes:
    """A valid JSON object whose serialized length is exactly ``n`` bytes."""

    filler = n - len('{"x":""}')
    assert filler >= 0
    return b'{"x":"' + b"a" * filler + b'"}'


# --------------------------------------------------------------------------- #
# Streamed size guard (bytes counted, aborted before full buffering)
# --------------------------------------------------------------------------- #
async def test_small_body_is_accepted() -> None:
    body = _json_body_of_length(64)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = _client(handler, max_body_bytes=1024)
    try:
        resp = await client.fetch_venues()
    finally:
        await client.aclose()
    assert resp.exchange.http_status == 200


async def test_body_exactly_at_limit_is_accepted() -> None:
    limit = 128
    body = _json_body_of_length(limit)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = _client(handler, max_body_bytes=limit)
    try:
        resp = await client.fetch_venues()
    finally:
        await client.aclose()
    # Exactly at the cap is allowed; one more byte would not be.
    assert len(resp.exchange.body) == limit


async def test_body_over_limit_is_refused_and_not_stored() -> None:
    limit = 128
    body = _json_body_of_length(limit + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = _client(handler, max_body_bytes=limit)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_venues()
    finally:
        await client.aclose()
    err = excinfo.value
    assert err.kind is ProviderErrorKind.UNEXPECTED
    # An oversized response never produces an exchange -> nothing can be stored.
    assert err.exchange is None


async def test_oversized_body_with_missing_content_length_is_refused() -> None:
    """A missing/misleading Content-Length cannot smuggle an oversized body.

    Bytes are counted from the stream itself, so a response that omits
    Content-Length is still aborted once it exceeds the cap.
    """

    limit = 128
    big = _json_body_of_length(limit * 4)

    def handler(request: httpx.Request) -> httpx.Response:
        resp = httpx.Response(200, content=big, headers={"content-type": "application/json"})
        # Strip the length header so only actual streamed bytes gate the read.
        resp.headers.pop("content-length", None)
        return resp

    client = _client(handler, max_body_bytes=limit)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_venues()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.UNEXPECTED
    assert excinfo.value.exchange is None


async def test_oversized_body_with_understated_content_length_is_refused() -> None:
    """A Content-Length that lies (claims tiny) does not defeat the byte counter."""

    limit = 128
    big = _json_body_of_length(limit * 4)

    def handler(request: httpx.Request) -> httpx.Response:
        resp = httpx.Response(200, content=big, headers={"content-type": "application/json"})
        resp.headers["content-length"] = "10"  # deliberately misleading
        return resp

    client = _client(handler, max_body_bytes=limit)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_venues()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.UNEXPECTED


class _ExplodingStream(httpx.AsyncByteStream):
    """A body stream that fails if iterated -- proves the body was never read."""

    async def __aiter__(self):
        raise AssertionError("body must not be read when Content-Length exceeds the cap")
        yield b""  # pragma: no cover - unreachable, satisfies the generator type

    async def aclose(self) -> None:
        return None


async def test_declared_oversized_content_length_rejected_before_reading() -> None:
    """An honestly-declared oversized Content-Length is refused before any read."""

    limit = 128

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": str(limit * 10)},
            stream=_ExplodingStream(),
        )

    client = _client(handler, max_body_bytes=limit)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_venues()  # _ExplodingStream would raise if read
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.UNEXPECTED
    assert excinfo.value.exchange is None


async def test_oversized_multi_chunk_body_aborted_mid_stream() -> None:
    """Many small chunks that together exceed the cap are aborted mid-stream."""

    limit = 100

    class _Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(50):
                yield b"a" * 10  # 500 bytes total, well over the 100 cap

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        # No content-length -> layer 2 (byte counting) must catch it.
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Chunks())

    client = _client(handler, max_body_bytes=limit)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_venues()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.UNEXPECTED
    assert excinfo.value.exchange is None


async def test_oversized_single_chunk_body_aborted() -> None:
    """A single chunk larger than the cap is aborted on the first read."""

    limit = 50

    class _OneBigChunk(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * (limit * 4)

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=_OneBigChunk()
        )

    client = _client(handler, max_body_bytes=limit)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_venues()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.UNEXPECTED


# --------------------------------------------------------------------------- #
# Base-URL pinning (config layer; independent of the request policy)
# --------------------------------------------------------------------------- #
def test_pinned_url_accepts_exact_base() -> None:
    assert _pinned_url_violation("mlb_stats_api_base_url", "https://statsapi.mlb.com/api/v1") is None
    assert _pinned_url_violation("nws_base_url", "https://api.weather.gov") is None
    assert _pinned_url_violation("open_meteo_base_url", "https://api.open-meteo.com/v1") is None


@pytest.mark.parametrize(
    "value",
    [
        "http://statsapi.mlb.com/api/v1",          # not https
        "https://user:pass@statsapi.mlb.com/api/v1",  # userinfo
        "https://evil.com/api/v1",                  # wrong host
        "https://statsapi.mlb.com:8443/api/v1",     # explicit port
        "https://statsapi.mlb.com/api/v1?x=1",      # query
        "https://statsapi.mlb.com/api/v1#frag",     # fragment
        "https://statsapi.mlb.com/api/v1evil",      # deceptive prefix (not exact)
        "https://statsapi.mlb.com/api/v1/extra",    # extra path segment
        "https://statsapi.mlb.com/api/%2e%2e/v1",   # percent-encoded path trick
        "https://statsapi.mlb.com/api/v2",          # wrong path
        "https://statsapi.mlb.com//api/v1",         # duplicate leading slash
        "https://statsapi.mlb.com/api//v1",         # duplicate interior slash
        "https://statsapi.mlb.com/api/./v1",        # dot segment
        "https://statsapi.mlb.com/api/v1/../v1",    # dot-dot segment
        "https://statsapi.mlb.com/api/v1/..",       # dot-dot suffix
    ],
)
def test_pinned_url_rejects_deceptive_variants(value: str) -> None:
    assert _pinned_url_violation("mlb_stats_api_base_url", value) is not None


def test_pinned_url_accepts_nws_empty_and_root_path() -> None:
    assert _pinned_url_violation("nws_base_url", "https://api.weather.gov") is None
    assert _pinned_url_violation("nws_base_url", "https://api.weather.gov/") is None
    # A non-root path is rejected for NWS (its pinned base path is empty).
    assert _pinned_url_violation("nws_base_url", "https://api.weather.gov/v1") is not None


# --------------------------------------------------------------------------- #
# BALLDONTLIE allowlist: explicit endpoints only, no wildcard, forbidden blocked
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "/v1/teams",
        "/v1/players",
        "/v1/games",
        "/v1/stats",
        "/v1/box_scores",
        "/v1/player_injuries",
        # Documented GOAT endpoints added in this repair:
        "/v1/plays",
        "/v1/lineups",
        "/nba/v1/stats/advanced",
    ],
)
def test_balldontlie_documented_endpoints_are_allowed(path: str) -> None:
    policy = ReadOnlyHTTPPolicy.for_balldontlie()
    policy.enforce("GET", f"https://api.balldontlie.io{path}")  # no raise


@pytest.mark.parametrize(
    "url",
    [
        "https://api.balldontlie.io/v1/plays?game_id=18444208",
        "https://api.balldontlie.io/v1/lineups?game_ids[]=1&game_ids[]=2",
        "https://api.balldontlie.io/nba/v1/stats/advanced?game_id=1&per_page=25",
    ],
)
def test_balldontlie_query_params_do_not_affect_path_authorization(url: str) -> None:
    # Authorization is on the path only; a query string never grants or denies it.
    ReadOnlyHTTPPolicy.for_balldontlie().enforce("GET", url)  # no raise


@pytest.mark.parametrize(
    "path",
    [
        "/v1/advanced_stats",     # removed: was never a documented endpoint
        "/v1/arbitrary",          # the removed /nba/v1/[a-z_]+ style wildcard
        "/v1/anything_goes",
        "/nba/v1/teams",          # only /nba/v1/stats/advanced is reachable
        "/nba/v1/stats",          # the parent is not itself allowed
        "/v1/account",            # account surface
        "/v1/subscriptions",      # billing surface
        "/v1/user/profile",       # user surface
        "/v1/api-keys",           # key-management surface
    ],
)
def test_balldontlie_undocumented_or_forbidden_paths_blocked(path: str) -> None:
    policy = ReadOnlyHTTPPolicy.for_balldontlie()
    with pytest.raises(ReadOnlyPolicyError):
        policy.enforce("GET", f"https://api.balldontlie.io{path}")


@pytest.mark.parametrize("path", ["/v1/teams", "/v1/plays", "/v1/lineups", "/nba/v1/stats/advanced"])
def test_write_methods_blocked_on_documented_endpoint(path: str) -> None:
    policy = ReadOnlyHTTPPolicy.for_balldontlie()
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        with pytest.raises(ReadOnlyPolicyError):
            policy.enforce(method, f"https://api.balldontlie.io{path}")
