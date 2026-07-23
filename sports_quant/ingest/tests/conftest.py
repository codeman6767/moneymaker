"""Fixtures for the odds-ingestion tests.

Every fixture builds a database under pytest's ``tmp_path`` and every network
call is a ``httpx.MockTransport`` wrapped in the real read-only policy -- so the
GET-only + allow-list enforcement applies exactly as it would live, and no test
ever touches the network or the developer's corpus.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.kalshi import DEFAULT_BASE_URL as KALSHI_BASE_URL
from sports_quant.providers.kalshi import KalshiClient
from sports_quant.providers.odds_api import DEFAULT_BASE_URL, OddsApiClient

#: A sentinel key. The whole-database secret sweep asserts this never lands in
#: any stored column.
SENTINEL_KEY = "sk-sentinel-do-not-store-123"

RESPONSE_HEADERS = {
    "content-type": "application/json",
    "x-requests-remaining": "495",
    "x-requests-used": "5",
    "x-requests-last": "1",
    # An authorization header the allow-list must refuse to store.
    "authorization": "Bearer super-secret-token",
    "set-cookie": "session=abc",
}


def mlb_payload() -> list[dict[str, Any]]:
    """One MLB event with h2h, spreads and totals from one bookmaker."""

    return [
        {
            "id": "mlb-event-1",
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
                        },
                        {
                            "key": "spreads",
                            "last_update": "2026-07-22T22:50:00Z",
                            "outcomes": [
                                {"name": "New York Yankees", "price": -110, "point": -1.5},
                                {"name": "Boston Red Sox", "price": -110, "point": 1.5},
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


def nba_payload() -> list[dict[str, Any]]:
    """One NBA event with an h2h market."""

    return [
        {
            "id": "nba-event-1",
            "sport_key": "basketball_nba",
            "commence_time": "2026-07-23T00:10:00Z",
            "home_team": "Boston Celtics",
            "away_team": "Los Angeles Lakers",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "last_update": "2026-07-22T23:55:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-07-22T23:55:00Z",
                            "outcomes": [
                                {"name": "Boston Celtics", "price": -180},
                                {"name": "Los Angeles Lakers", "price": 155},
                            ],
                        }
                    ],
                }
            ],
        }
    ]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "corpus.db"


@pytest.fixture
def database(db_path: Path) -> Database:
    """A migrated, seeded temporary corpus."""

    initialize_database(db_path)
    return Database(db_path)


ClientFactory = Callable[..., OddsApiClient]


@pytest.fixture
def make_client() -> ClientFactory:
    """Build an ``OddsApiClient`` whose transport returns a scripted response.

    ``handler`` receives the request and returns an ``httpx.Response``. The
    policy wrapper is preserved, so GET-only enforcement is real.
    """

    created: list[OddsApiClient] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        api_key: str = SENTINEL_KEY,
    ) -> OddsApiClient:
        http = build_readonly_client(
            base_url=DEFAULT_BASE_URL,
            policy=ReadOnlyHTTPPolicy.for_odds_api(),
            inner_transport=httpx.MockTransport(handler),
        )
        client = OddsApiClient(api_key, client=http)
        created.append(client)
        return client

    return factory


@pytest.fixture
def client_for() -> Callable[[list[dict[str, Any]]], Callable[[httpx.Request], httpx.Response]]:
    """A convenience returning a 200 handler for a fixed JSON payload."""

    def build(payload: list[dict[str, Any]], *, status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload, headers=RESPONSE_HEADERS)

        return handler

    return build


# --------------------------------------------------------------------------- #
# Kalshi (Phase C)
# --------------------------------------------------------------------------- #
KalshiHandler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def make_kalshi_client() -> Callable[[KalshiHandler], KalshiClient]:
    """Build a ``KalshiClient`` whose transport returns scripted responses.

    The Kalshi read-only policy wrapper is preserved, so GET-only + allow-list
    enforcement (and the account/order/fill blocks) apply exactly as they would
    against the live exchange. No credential is ever configured.
    """

    def factory(handler: KalshiHandler) -> KalshiClient:
        http = build_readonly_client(
            base_url=KALSHI_BASE_URL,
            policy=ReadOnlyHTTPPolicy.for_kalshi(KALSHI_BASE_URL),
            inner_transport=httpx.MockTransport(handler),
        )
        return KalshiClient(base_url=KALSHI_BASE_URL, client=http)

    return factory


def kalshi_events_body() -> dict[str, Any]:
    return {
        "events": [
            {
                "event_ticker": "KXMLBGAME-26JUL22",
                "series_ticker": "KXMLBGAME",
                "title": "Yankees vs Red Sox",
                "sub_title": "Winner",
                "category": "Sports",
                "status": "open",
                "mutually_exclusive": True,
            }
        ],
        "cursor": "",
    }


def kalshi_markets_body() -> dict[str, Any]:
    return {
        "markets": [
            {
                "ticker": "KXMLBGAME-26JUL22-NYY",
                "event_ticker": "KXMLBGAME-26JUL22",
                "series_ticker": "KXMLBGAME",
                "title": "Will the Yankees win?",
                "yes_sub_title": "Yankees win",
                "no_sub_title": "Yankees lose",
                "status": "open",
                "open_time": "2026-07-22T18:00:00Z",
                "close_time": "2026-07-22T23:00:00Z",
                "rules_primary": "Settles Yes if the Yankees win the game.",
            }
        ],
        "cursor": "",
    }


def kalshi_orderbook_body(
    yes: list[list[int]] | None = None, no: list[list[int]] | None = None
) -> dict[str, Any]:
    return {"orderbook": {"yes": yes, "no": no}}


def kalshi_trades_body(trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if trades is None:
        trades = [
            {
                "trade_id": "trd-1",
                "ticker": "KXMLBGAME-26JUL22-NYY",
                "yes_price": 42,
                "no_price": 58,
                "count": 10,
                "taker_side": "yes",
                "created_time": "2026-07-22T18:05:00Z",
            }
        ]
    return {"trades": trades, "cursor": ""}


def kalshi_router(
    *,
    events: dict[str, Any] | None = None,
    markets: dict[str, Any] | None = None,
    orderbook: dict[str, Any] | None = None,
    trades: dict[str, Any] | None = None,
    seen: list[httpx.Request] | None = None,
) -> KalshiHandler:
    """A handler routing each Kalshi path to a supplied body (defaults provided)."""

    events = events if events is not None else kalshi_events_body()
    markets = markets if markets is not None else kalshi_markets_body()
    orderbook = orderbook if orderbook is not None else kalshi_orderbook_body(
        yes=[[42, 100], [40, 50]], no=[[55, 30]]
    )
    trades = trades if trades is not None else kalshi_trades_body()

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path.endswith("/orderbook"):
            return httpx.Response(200, json=orderbook)
        if path.endswith("/markets/trades") or path.endswith("/trades"):
            return httpx.Response(200, json=trades)
        if path.endswith("/events"):
            return httpx.Response(200, json=events)
        if path.endswith("/markets"):
            return httpx.Response(200, json=markets)
        return httpx.Response(404, json={})

    return handler
