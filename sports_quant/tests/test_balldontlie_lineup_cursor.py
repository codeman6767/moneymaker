"""Cursor support on the BALLDONTLIE lineups request path.

``/v1/lineups`` had no cursor parameter at all, which is why the March 2026 month
run kept only the first 25 rows for 40 games (execution review §8/§9). These
tests pin the request the continuation recovery depends on: the cursor must reach
the provider exactly as issued, exactly once, and a first-page call must be
unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import pytest

from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.balldontlie import BalldontlieClient, next_cursor

#: A sentinel key. No test may cause it to appear in a URL, a stored param or a
#: stored header.
SENTINEL_KEY = "sk-lineup-cursor-must-never-leak"

SEEN: list[httpx.Request] = []


def _client(body: Optional[dict[str, Any]] = None) -> BalldontlieClient:
    payload = body if body is not None else {"data": [], "meta": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        SEEN.append(request)
        return httpx.Response(200, json=payload,
                              headers={"content-type": "application/json"})

    return BalldontlieClient(
        SENTINEL_KEY,
        client=build_readonly_client(
            base_url="https://api.balldontlie.io",
            policy=ReadOnlyHTTPPolicy.for_balldontlie(),
            inner_transport=httpx.MockTransport(handler)))


async def _call(**kw: Any) -> Any:
    del SEEN[:]
    client = _client(kw.pop("body", None))
    try:
        return await client.fetch_lineups(**kw)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# First page stays exactly as it was
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_page_sends_no_cursor_parameter() -> None:
    """Backward compatibility: an ordinary first-page call is unchanged."""

    await _call(game_ids=[18447686])
    request = SEEN[0]
    assert request.url.path == "/v1/lineups"
    assert dict(request.url.params.multi_items()) == {
        "game_ids[]": "18447686", "per_page": "25"}
    assert "cursor" not in request.url.params


@pytest.mark.asyncio
async def test_explicit_none_cursor_is_omitted_not_sent_as_none() -> None:
    await _call(game_ids=[18447686], cursor=None)
    assert "cursor" not in SEEN[0].url.params
    assert "None" not in str(SEEN[0].url)


# --------------------------------------------------------------------------- #
# Continuation page
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_continuation_page_sends_the_cursor_once() -> None:
    await _call(game_ids=[18447686], cursor=5615604)
    request = SEEN[0]
    items = request.url.params.multi_items()
    assert ("cursor", "5615604") in items
    assert [k for k, _ in items].count("cursor") == 1, "cursor must be sent once"
    assert dict(items) == {"game_ids[]": "18447686", "per_page": "25",
                           "cursor": "5615604"}


@pytest.mark.asyncio
async def test_opaque_text_cursor_survives_unmodified() -> None:
    """A cursor is the provider's token; normalizing it would resume elsewhere."""

    opaque = "eyJvZmZzZXQiOjI1fQ"
    await _call(game_ids=[18447686], cursor=opaque)
    assert SEEN[0].url.params["cursor"] == opaque


@pytest.mark.parametrize("cursor,expected", [
    ("a b", "a b"),            # space
    ("a+b", "a+b"),            # plus must survive round-trip, not become a space
    ("a/b=", "a/b="),          # reserved characters
    ("a%20b", "a%20b"),        # already-percent-looking text is NOT re-decoded
])
@pytest.mark.asyncio
async def test_cursor_is_encoded_exactly_once(cursor: str, expected: str) -> None:
    """Double encoding would send a token the provider never issued."""

    await _call(game_ids=[18447686], cursor=cursor)
    assert SEEN[0].url.params["cursor"] == expected


@pytest.mark.asyncio
async def test_cursor_is_never_serialized_as_a_container() -> None:
    """The ``"[18447686]"`` provenance defect must not repeat for cursors."""

    await _call(game_ids=[18447686], cursor=5615604)
    raw = str(SEEN[0].url)
    assert "cursor=5615604" in raw
    # `game_ids%5B%5D` is the legitimate encoded parameter NAME; what must never
    # appear is a bracketed cursor VALUE, i.e. a stringified container.
    value = SEEN[0].url.params["cursor"]
    assert not value.startswith("[") and not value.endswith("]"), value
    assert "cursor=%5B" not in raw
    # ... and the same for the repeated game-id values themselves
    assert SEEN[0].url.params.get_list("game_ids[]") == ["18447686"]


@pytest.mark.parametrize("bad", [[5615604], (5615604,), {"c": 1}, True, "", "   ",
                                 3.5, object()])
@pytest.mark.asyncio
async def test_unusable_cursor_values_are_refused_before_any_request(bad: Any) -> None:
    del SEEN[:]
    client = _client()
    try:
        with pytest.raises(ValueError):
            await client.fetch_lineups(game_ids=[18447686], cursor=bad)
    finally:
        await client.aclose()
    assert SEEN == [], "no request may be issued for an unusable cursor"


# --------------------------------------------------------------------------- #
# Repeated parameters, identity and hashing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_repeated_game_ids_serialize_canonically_with_a_cursor() -> None:
    await _call(game_ids=[11, 22], cursor=99)
    items = sorted(SEEN[0].url.params.multi_items())
    assert items == [("cursor", "99"), ("game_ids[]", "11"), ("game_ids[]", "22"),
                     ("per_page", "25")]


@pytest.mark.asyncio
async def test_stored_params_reconstruct_the_request_and_hash_stably() -> None:
    """Stored provenance must replay to the URL that was actually sent."""

    from sports_quant.db.repositories.raw_responses import response_content_hash

    response = await _call(game_ids=[18447686], cursor=5615604)
    exchange = response.exchange
    assert exchange.request_params["game_ids[]"] == ["18447686"]
    assert exchange.request_params["cursor"] == "5615604"
    assert exchange.request_params["per_page"] == "25"

    rebuilt = sorted(
        (k, v) for k, value in exchange.request_params.items()
        for v in (value if isinstance(value, list) else [value]))
    assert rebuilt == sorted(SEEN[0].url.params.multi_items())

    first = response_content_hash(provider="balldontlie", endpoint="/v1/lineups",
                                  request_params=exchange.request_params, body="{}")
    second = response_content_hash(provider="balldontlie", endpoint="/v1/lineups",
                                   request_params=exchange.request_params, body="{}")
    assert first == second


@pytest.mark.asyncio
async def test_page_one_and_a_continuation_hash_differently() -> None:
    """Two pages of one game must not collapse to a single stored response."""

    from sports_quant.db.repositories.raw_responses import response_content_hash

    page1 = await _call(game_ids=[18447686])
    page2 = await _call(game_ids=[18447686], cursor=5615604)
    h1 = response_content_hash(provider="balldontlie", endpoint="/v1/lineups",
                               request_params=page1.exchange.request_params, body="{}")
    h2 = response_content_hash(provider="balldontlie", endpoint="/v1/lineups",
                               request_params=page2.exchange.request_params, body="{}")
    assert h1 != h2


@pytest.mark.asyncio
async def test_next_cursor_is_returned_to_the_paginator() -> None:
    response = await _call(game_ids=[18447686], cursor=5615604,
                           body={"data": [], "meta": {"next_cursor": 5615629}})
    assert next_cursor(response.data) == 5615629
    terminal = await _call(game_ids=[18447686], cursor=5615629,
                           body={"data": [], "meta": {"per_page": 25}})
    assert next_cursor(terminal.data) is None


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_credential_reaches_the_url_or_the_stored_metadata() -> None:
    response = await _call(game_ids=[18447686], cursor=5615604)
    request = SEEN[0]
    assert SENTINEL_KEY not in str(request.url)
    assert "cursor" in request.url.params and "api_key" not in request.url.params
    exchange = response.exchange
    blob = json.dumps({"p": exchange.request_params, "h": exchange.response_headers,
                       "e": exchange.endpoint})
    assert SENTINEL_KEY not in blob
    assert "authorization" not in {k.lower() for k in exchange.response_headers}


@pytest.mark.asyncio
async def test_other_endpoints_are_unaffected() -> None:
    """The change must be confined to the lineups path."""

    del SEEN[:]
    client = _client()
    try:
        await client.fetch_plays(game_id=18447686, per_page=100)
        await client.fetch_stats(game_ids=[18447686], per_page=100)
    finally:
        await client.aclose()
    assert SEEN[0].url.path == "/v1/plays"
    assert "cursor" not in SEEN[0].url.params
    assert SEEN[1].url.path == "/v1/stats"
    assert "cursor" not in SEEN[1].url.params
