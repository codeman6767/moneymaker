"""The Odds API adapter: normalization, credit headers, caching, redaction."""

from __future__ import annotations

import httpx
import pytest

from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.odds_api import DEFAULT_BASE_URL, OddsApiClient
from sports_quant.redaction import sanitize_url

API_KEY = "test-key-do-not-log"

MLB_PAYLOAD = [
    {
        "id": "abc123",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
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
                    },
                    {
                        "key": "totals",
                        "last_update": "2026-07-22T22:50:00Z",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 8.5},
                            {"name": "Under", "price": -110, "point": 8.5},
                        ],
                    },
                ],
            }
        ],
    }
]

RESPONSE_HEADERS = {
    "x-requests-remaining": "495",
    "x-requests-used": "5",
    "x-requests-last": "1",
}


def _client_with_handler(handler) -> OddsApiClient:
    http = build_readonly_client(
        base_url=DEFAULT_BASE_URL,
        policy=ReadOnlyHTTPPolicy.for_odds_api(),
        inner_transport=httpx.MockTransport(handler),
    )
    return OddsApiClient(API_KEY, client=http)


async def test_normalizes_events_and_captures_credit_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MLB_PAYLOAD, headers=RESPONSE_HEADERS)

    odds = _client_with_handler(handler)
    result = await odds.get_mlb_odds()

    assert result.credits.requests_remaining == "495"
    assert result.credits.requests_used == "5"
    assert result.credits.requests_last == "1"

    # Raw preserved verbatim.
    assert result.raw == MLB_PAYLOAD

    (event,) = result.events
    assert event.provider_event_id == "abc123"
    assert event.sport_key == "baseball_mlb"
    assert event.home_team == "New York Yankees"
    assert event.away_team == "Boston Red Sox"
    assert event.commence_time is not None

    (bookmaker,) = event.bookmakers
    assert bookmaker.key == "draftkings"
    assert bookmaker.last_update is not None  # bookmaker update time

    market_keys = {m.key for m in bookmaker.markets}
    assert market_keys == {"h2h", "totals"}
    totals = next(m for m in bookmaker.markets if m.key == "totals")
    over = next(o for o in totals.outcomes if o.name == "Over")
    assert over.price == -110
    assert over.point == 8.5


async def test_duplicate_requests_use_cache() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=MLB_PAYLOAD, headers=RESPONSE_HEADERS)

    odds = _client_with_handler(handler)
    first = await odds.get_mlb_odds()
    second = await odds.get_mlb_odds()

    # Only one network round-trip; the second call is served from cache.
    assert calls["count"] == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.events == first.events


async def test_error_message_does_not_leak_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    odds = _client_with_handler(handler)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await odds.get_mlb_odds()
    # The key is masked; "REDACTED" survives URL-encoding of the marker.
    assert API_KEY not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)


def test_sanitize_url_masks_api_key() -> None:
    url = f"https://api.the-odds-api.com/v4/sports?apiKey={API_KEY}&regions=us"
    sanitized = sanitize_url(url)
    assert API_KEY not in sanitized
    assert "regions=us" in sanitized
