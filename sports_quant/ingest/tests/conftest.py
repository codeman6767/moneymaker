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
