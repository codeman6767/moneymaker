"""Kalshi public-data adapter: order-book derivation, pagination, GET-only."""

from __future__ import annotations

import httpx

from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.kalshi import DEFAULT_BASE_URL, KalshiClient, KalshiOrderBook


def _client_with_handler(handler) -> KalshiClient:
    http = build_readonly_client(
        base_url=DEFAULT_BASE_URL,
        policy=ReadOnlyHTTPPolicy.for_kalshi(DEFAULT_BASE_URL),
        inner_transport=httpx.MockTransport(handler),
    )
    return KalshiClient(base_url=DEFAULT_BASE_URL, client=http)


def test_orderbook_asks_derived_from_opposing_bids() -> None:
    raw = {
        "orderbook": {
            "yes": [[42, 100], [40, 50]],  # bids to BUY Yes
            "no": [[55, 30], [50, 10]],    # bids to BUY No
        }
    }
    book = KalshiOrderBook.from_raw("MKT-1", raw)

    # Bids are preserved (best-first) and never treated as asks.
    assert book.best_yes_bid == 42
    assert book.best_no_bid == 55
    assert book.yes_bids == [(42, 100), (40, 50)]
    assert book.no_bids == [(55, 30), (50, 10)]

    # Executable asks are derived from the *opposing* side's best bid.
    assert book.executable_yes_ask == 100 - 55  # == 45
    assert book.executable_no_ask == 100 - 42   # == 58

    # A derived ask must not equal the same side's best bid (i.e. not a bid).
    assert book.executable_yes_ask != book.best_yes_bid
    assert book.executable_no_ask != book.best_no_bid


def test_empty_orderbook_yields_no_asks() -> None:
    book = KalshiOrderBook.from_raw("MKT-EMPTY", {"orderbook": {"yes": None, "no": None}})
    assert book.yes_bids == []
    assert book.no_bids == []
    assert book.best_yes_bid is None
    assert book.executable_yes_ask is None
    assert book.executable_no_ask is None


async def test_get_market_orderbook_is_get_only_and_parsed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={"orderbook": {"yes": [[10, 5]], "no": [[80, 7]]}})

    kalshi = _client_with_handler(handler)
    try:
        book = await kalshi.get_market_orderbook("MKT-2")
    finally:
        await kalshi.aclose()

    assert seen == ["GET"]
    assert book.best_yes_bid == 10
    assert book.executable_yes_ask == 20  # 100 - 80


async def test_pagination_follows_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        if not cursor:
            return httpx.Response(200, json={"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "c1"})
        return httpx.Response(200, json={"markets": [{"ticker": "C"}], "cursor": ""})

    kalshi = _client_with_handler(handler)
    try:
        page = await kalshi.list_markets(status="open", max_pages=5)
    finally:
        await kalshi.aclose()

    assert [m["ticker"] for m in page.items] == ["A", "B", "C"]
    assert page.cursor is None  # exhausted
    assert len(page.raw_pages) == 2


async def test_exchange_status_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/exchange/status")
        return httpx.Response(200, json={"exchange_active": True, "trading_active": False})

    kalshi = _client_with_handler(handler)
    try:
        status = await kalshi.exchange_status()
    finally:
        await kalshi.aclose()
    assert status["exchange_active"] is True
    assert status["trading_active"] is False
