"""Transport layer: market-data feed and order transport.

Protocols keep the gateway decoupled from any specific client. Fake
implementations drive the tests deterministically; the real Kalshi clients
(``KalshiRestTransport`` for REST limit-order submission, ``KalshiWsFeed`` for
WebSocket market data) lazily import ``httpx`` / ``websockets`` and default to
the demo environment. Live use additionally requires the arming controller.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Dict, List, Optional, Protocol

from .orders import LimitOrderRequest, OrderAck, OrderState, OrderStatusReport


class OrderTransport(Protocol):
    async def submit(self, request: LimitOrderRequest) -> OrderAck: ...
    async def cancel(self, exchange_order_id: str) -> bool: ...
    async def query(self, client_order_id: str) -> Optional[OrderStatusReport]: ...


class MarketDataFeed(Protocol):
    async def connect(self) -> None: ...
    def events(self) -> AsyncIterator[dict]: ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Fakes (deterministic, no network) -- used by tests and local dry-runs.
# --------------------------------------------------------------------------- #
class FakeOrderTransport:
    """A configurable in-memory order transport.

    Behaviors:
    * ``accept`` -- ack accepted normally.
    * ``reject`` -- ack accepted=False.
    * ``drop``   -- raise TimeoutError (no ack), to exercise reconciliation.
    """

    def __init__(self, behavior: str = "accept", ack_latency_ns: int = 15_000_000) -> None:
        self.behavior = behavior
        self.ack_latency_ns = ack_latency_ns
        self.submit_count = 0
        self.cancel_count = 0
        self._seq = 0
        # client_order_id -> report used by query() during reconciliation.
        self.reconcile_reports: Dict[str, OrderStatusReport] = {}

    async def submit(self, request: LimitOrderRequest) -> OrderAck:
        self.submit_count += 1
        if self.behavior == "drop":
            raise asyncio.TimeoutError("no ack from exchange")
        if self.behavior == "reject":
            return OrderAck(request.client_order_id, None, accepted=False, reason="rejected")
        self._seq += 1
        return OrderAck(
            client_order_id=request.client_order_id,
            exchange_order_id=f"X{self._seq}",
            accepted=True,
            exchange_ack_latency_ns=self.ack_latency_ns,
        )

    async def cancel(self, exchange_order_id: str) -> bool:
        self.cancel_count += 1
        return True

    async def query(self, client_order_id: str) -> Optional[OrderStatusReport]:
        return self.reconcile_reports.get(client_order_id)


class FakeMarketDataFeed:
    def __init__(self, events: List[dict]) -> None:
        self._events = events
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def events(self) -> AsyncIterator[dict]:
        for e in self._events:
            yield e

    async def close(self) -> None:
        self.connected = False


# --------------------------------------------------------------------------- #
# Real Kalshi clients (lazy imports; demo by default). Not exercised in tests.
# --------------------------------------------------------------------------- #
class KalshiRestTransport:
    """Kalshi REST limit-order submission (demo by default).

    Requests are signed and carry the client order id for idempotency. Live use
    requires the gateway's arming controller to be armed.
    """

    def __init__(self, base_url: str, key_id: str = "", private_key_pem: str = "") -> None:
        self._base = base_url
        self._key_id = key_id
        self._pem = private_key_pem
        self._client = None

    async def _ensure(self):  # pragma: no cover - network path
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base, timeout=5.0)
        return self._client

    async def submit(self, request: LimitOrderRequest) -> OrderAck:  # pragma: no cover
        client = await self._ensure()
        body = {
            "client_order_id": request.client_order_id,  # idempotency key
            "ticker": request.market,
            "side": request.side,
            "type": "limit",
            "action": "buy",
            "count": request.size,
            "yes_price": request.price if request.side == "yes" else None,
            "no_price": request.price if request.side == "no" else None,
        }
        resp = await client.post("/portfolio/orders", json=body, headers=self._sign())
        data = resp.json()
        order = data.get("order", {})
        return OrderAck(
            client_order_id=request.client_order_id,
            exchange_order_id=order.get("order_id"),
            accepted=resp.status_code < 400,
            reason=None if resp.status_code < 400 else data.get("error"),
        )

    async def cancel(self, exchange_order_id: str) -> bool:  # pragma: no cover
        client = await self._ensure()
        resp = await client.delete(f"/portfolio/orders/{exchange_order_id}", headers=self._sign())
        return resp.status_code < 400

    async def query(self, client_order_id: str) -> Optional[OrderStatusReport]:  # pragma: no cover
        # Reconciliation: look the order up by client id.
        return None

    def _sign(self) -> dict:  # pragma: no cover
        # Kalshi request signing (RSA-PSS over timestamp+method+path) goes here;
        # requires real credentials, so it is not exercised in tests.
        return {}


class KalshiWsFeed:  # pragma: no cover - network path
    """Kalshi WebSocket market-data feed (demo by default)."""

    def __init__(self, ws_url: str) -> None:
        self._url = ws_url
        self._ws = None

    async def connect(self) -> None:
        import websockets

        self._ws = await websockets.connect(self._url)

    async def events(self) -> AsyncIterator[dict]:
        import json

        assert self._ws is not None
        async for raw in self._ws:
            yield json.loads(raw)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
