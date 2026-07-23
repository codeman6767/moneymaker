"""Typed asynchronous adapter for Kalshi's public REST API.

Read-only and unauthenticated. No API key, no private key, no signing: only the
public-data GET surface is reachable, and every request is additionally forced
through :mod:`sports_quant.http_policy`.

Public methods:

* :meth:`KalshiClient.list_events`
* :meth:`KalshiClient.list_markets`
* :meth:`KalshiClient.get_market`
* :meth:`KalshiClient.get_market_orderbook`
* :meth:`KalshiClient.get_trades`
* :meth:`KalshiClient.list_series`
* :meth:`KalshiClient.exchange_status`

Order books get careful treatment. Kalshi publishes resting *bids* on two sides
(``yes`` and ``no``) at integer cent prices. A No bid at price ``n`` is
economically an offer to sell Yes at ``100 - n`` (and vice versa), so the
executable *asks* are derived from the opposing side's best bid. Returned bids
are **never** treated as asks directly; every price/quantity level is preserved,
and empty books yield ``None`` for the derived asks.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from .raw_exchange import RawExchange, build_exchange

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Kalshi binary-market prices are integer cents in [1, 99]; a Yes and its No
# complement sum to 100 cents.
PRICE_COMPLEMENT = 100


class KalshiPage(BaseModel):
    """One or more pages of a paginated list endpoint.

    ``items`` aggregates every record fetched; ``cursor`` is the cursor returned
    by the last page (empty/``None`` means the listing is exhausted);
    ``raw_pages`` preserves each raw page payload untouched.
    """

    model_config = ConfigDict(extra="ignore")
    items: list[dict[str, Any]] = Field(default_factory=list)
    cursor: Optional[str] = None
    raw_pages: list[dict[str, Any]] = Field(default_factory=list)


class KalshiOrderBook(BaseModel):
    """A parsed Kalshi order book with derived executable asks.

    Levels are ``(price_cents, quantity)`` pairs. ``yes_bids`` / ``no_bids`` are
    the resting bids exactly as returned (sorted best-first); the asks are
    *derived*, never read directly from the wire.
    """

    model_config = ConfigDict(extra="ignore")
    market_ticker: str
    yes_bids: list[tuple[int, int]] = Field(default_factory=list)
    no_bids: list[tuple[int, int]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_raw(cls, market_ticker: str, raw: dict[str, Any]) -> "KalshiOrderBook":
        book = raw.get("orderbook") or {}

        def levels(side: str) -> list[tuple[int, int]]:
            rows = book.get(side) or []  # may be null for an empty side
            parsed = [(int(price), int(qty)) for price, qty in rows]
            # Preserve every level; present best (highest) bid first.
            return sorted(parsed, key=lambda lvl: lvl[0], reverse=True)

        return cls(
            market_ticker=market_ticker,
            yes_bids=levels("yes"),
            no_bids=levels("no"),
            raw=raw,
        )

    @property
    def best_yes_bid(self) -> Optional[int]:
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> Optional[int]:
        return self.no_bids[0][0] if self.no_bids else None

    @property
    def executable_yes_ask(self) -> Optional[int]:
        """Cheapest executable price to BUY Yes, derived from the best No bid."""

        return None if self.best_no_bid is None else PRICE_COMPLEMENT - self.best_no_bid

    @property
    def executable_no_ask(self) -> Optional[int]:
        """Cheapest executable price to BUY No, derived from the best Yes bid."""

        return None if self.best_yes_bid is None else PRICE_COMPLEMENT - self.best_yes_bid


class KalshiCapturedPage(BaseModel):
    """A paginated Kalshi listing plus the sanitized raw exchange of each page.

    The ingestion lane needs the verbatim body of every page preserved *before*
    normalization, so each page carries its own :class:`RawExchange`. ``items``
    aggregates the parsed records across pages; ``cursor`` is the final cursor
    (``None`` when the listing is exhausted).
    """

    model_config = ConfigDict(extra="ignore")
    items: list[dict[str, Any]] = Field(default_factory=list)
    cursor: Optional[str] = None
    exchanges: list[RawExchange] = Field(default_factory=list)
    #: The parsed page bodies, one per exchange, aligned by index.
    page_bodies: list[dict[str, Any]] = Field(default_factory=list)


class KalshiClient:
    """Async, read-only, unauthenticated adapter for Kalshi public REST."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        default_page_limit: int = 100,
    ) -> None:
        self._base_url = base_url
        self._owns_client = client is None
        self._client = client or build_readonly_client(
            base_url=base_url,
            policy=ReadOnlyHTTPPolicy.for_kalshi(base_url),
        )
        self.default_page_limit = default_page_limit

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- Internal helpers -----------------------------------------------------
    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = await self._client.get(path, params=clean)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}

    async def _paginate(
        self,
        path: str,
        item_key: str,
        *,
        params: Optional[dict[str, Any]] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiPage:
        params = dict(params or {})
        params["limit"] = limit or self.default_page_limit
        page = KalshiPage()
        for _ in range(max(1, max_pages)):
            if cursor:
                params["cursor"] = cursor
            raw = await self._get(path, params)
            page.raw_pages.append(raw)
            page.items.extend(raw.get(item_key, []) or [])
            cursor = raw.get("cursor") or None
            page.cursor = cursor
            if not cursor:
                break
        return page

    # -- Public endpoints -----------------------------------------------------
    async def list_events(
        self,
        *,
        status: Optional[str] = None,
        series_ticker: Optional[str] = None,
        with_nested_markets: Optional[bool] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiPage:
        params: dict[str, Any] = {"status": status, "series_ticker": series_ticker}
        if with_nested_markets is not None:
            params["with_nested_markets"] = str(with_nested_markets).lower()
        return await self._paginate(
            "/events", "events", params=params, limit=limit, max_pages=max_pages, cursor=cursor
        )

    async def list_markets(
        self,
        *,
        status: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        tickers: Optional[str] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiPage:
        params: dict[str, Any] = {
            "status": status,
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "tickers": tickers,
        }
        return await self._paginate(
            "/markets", "markets", params=params, limit=limit, max_pages=max_pages, cursor=cursor
        )

    async def get_market(self, ticker: str) -> dict[str, Any]:
        raw = await self._get(f"/markets/{ticker}")
        return raw.get("market", raw)

    async def get_market_orderbook(self, ticker: str, *, depth: Optional[int] = None) -> KalshiOrderBook:
        params = {"depth": depth} if depth is not None else None
        raw = await self._get(f"/markets/{ticker}/orderbook", params)
        return KalshiOrderBook.from_raw(ticker, raw)

    async def get_trades(
        self,
        *,
        ticker: Optional[str] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiPage:
        params: dict[str, Any] = {"ticker": ticker}
        return await self._paginate(
            "/markets/trades", "trades", params=params, limit=limit, max_pages=max_pages, cursor=cursor
        )

    async def list_series(
        self,
        *,
        category: Optional[str] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiPage:
        params: dict[str, Any] = {"category": category}
        return await self._paginate(
            "/series", "series", params=params, limit=limit, max_pages=max_pages, cursor=cursor
        )

    async def exchange_status(self) -> dict[str, Any]:
        return await self._get("/exchange/status")

    # -- Captured endpoints (raw exchange preserved for the corpus) -----------
    #
    # These mirror the parsing methods above but additionally return the
    # sanitized :class:`RawExchange` for each HTTP response, so the ingestion
    # lane can persist the verbatim bytes before normalizing them. They are the
    # same GET requests through the same policy-wrapped transport -- no second
    # client, no credential, no signing.
    async def _get_captured(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> tuple[dict[str, Any], RawExchange]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        requested_at = datetime.now(timezone.utc)
        started_ns = time.monotonic_ns()
        response = await self._client.get(path, params=clean)
        exchange = build_exchange(
            path=path,
            params=clean,
            response=response,
            requested_at=requested_at,
            elapsed_ns=time.monotonic_ns() - started_ns,
        )
        response.raise_for_status()
        data = response.json()
        return (data if isinstance(data, dict) else {"data": data}), exchange

    async def _paginate_captured(
        self,
        path: str,
        item_key: str,
        *,
        params: Optional[dict[str, Any]] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiCapturedPage:
        params = dict(params or {})
        params["limit"] = limit or self.default_page_limit
        page = KalshiCapturedPage()
        for _ in range(max(1, max_pages)):
            if cursor:
                params["cursor"] = cursor
            raw, exchange = await self._get_captured(path, params)
            page.exchanges.append(exchange)
            page.page_bodies.append(raw)
            page.items.extend(raw.get(item_key, []) or [])
            cursor = raw.get("cursor") or None
            page.cursor = cursor
            if not cursor:
                break
        return page

    async def fetch_exchange_status(self) -> tuple[dict[str, Any], RawExchange]:
        return await self._get_captured("/exchange/status")

    async def fetch_events(
        self,
        *,
        status: Optional[str] = None,
        series_ticker: Optional[str] = None,
        with_nested_markets: Optional[bool] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiCapturedPage:
        params: dict[str, Any] = {"status": status, "series_ticker": series_ticker}
        if with_nested_markets is not None:
            params["with_nested_markets"] = str(with_nested_markets).lower()
        return await self._paginate_captured(
            "/events", "events", params=params, limit=limit, max_pages=max_pages, cursor=cursor
        )

    async def fetch_markets(
        self,
        *,
        status: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        tickers: Optional[str] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiCapturedPage:
        params: dict[str, Any] = {
            "status": status,
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "tickers": tickers,
        }
        return await self._paginate_captured(
            "/markets", "markets", params=params, limit=limit, max_pages=max_pages, cursor=cursor
        )

    async def fetch_market_orderbook(
        self, ticker: str, *, depth: Optional[int] = None
    ) -> tuple[KalshiOrderBook, RawExchange]:
        book, exchange = await self.fetch_market_orderbook_raw(ticker, depth=depth)
        return KalshiOrderBook.from_raw(ticker, book), exchange

    async def fetch_market_orderbook_raw(
        self, ticker: str, *, depth: Optional[int] = None
    ) -> tuple[dict[str, Any], RawExchange]:
        """Fetch a market's order book and return the raw payload unparsed.

        The ingestion lane needs the exact bytes preserved before parsing, so a
        single malformed book cannot lose its response -- the parse into
        :class:`KalshiOrderBook` happens downstream, defensively.
        """

        params = {"depth": depth} if depth is not None else None
        return await self._get_captured(f"/markets/{ticker}/orderbook", params)

    async def fetch_trades(
        self,
        *,
        ticker: Optional[str] = None,
        limit: Optional[int] = None,
        max_pages: int = 1,
        cursor: Optional[str] = None,
    ) -> KalshiCapturedPage:
        params: dict[str, Any] = {"ticker": ticker}
        return await self._paginate_captured(
            "/markets/trades", "trades", params=params, limit=limit, max_pages=max_pages,
            cursor=cursor,
        )
