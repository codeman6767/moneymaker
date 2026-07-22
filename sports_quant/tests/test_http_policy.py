"""Read-only networking policy: the hard safety boundary.

Proves that write verbs are blocked, that account/portfolio/order paths are
blocked, that only approved hosts are accepted, and that the approved public GET
surface is reachable. Enforcement is exercised end-to-end through an
``httpx.AsyncClient`` whose transport wraps a ``MockTransport``, so these are
the same checks a real request would face.
"""

from __future__ import annotations

import httpx
import pytest

from sports_quant.http_policy import (
    ReadOnlyHTTPPolicy,
    ReadOnlyPolicyError,
    build_readonly_client,
)

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
POLICY = ReadOnlyHTTPPolicy.for_kalshi(KALSHI_BASE)


def _url(path: str) -> str:
    return f"https://external-api.kalshi.com/trade-api/v2{path}"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_blocked(method: str) -> None:
    with pytest.raises(ReadOnlyPolicyError):
        POLICY.enforce(method, _url("/markets"))


@pytest.mark.parametrize(
    "path",
    [
        "/portfolio/balance",
        "/portfolio/orders",
        "/portfolio/positions",
        "/portfolio/fills",
        "/account",
    ],
)
def test_account_and_portfolio_paths_blocked(path: str) -> None:
    with pytest.raises(ReadOnlyPolicyError):
        POLICY.enforce("GET", _url(path))


def test_only_approved_hosts_accepted() -> None:
    # Even a would-be-valid path on a non-approved host is rejected.
    with pytest.raises(ReadOnlyPolicyError):
        POLICY.enforce("GET", "https://demo-api.kalshi.co/trade-api/v2/markets")
    with pytest.raises(ReadOnlyPolicyError):
        POLICY.enforce("GET", "https://evil.example.com/trade-api/v2/markets")


@pytest.mark.parametrize(
    "path",
    [
        "/events",
        "/events/SOME-EVENT",
        "/markets",
        "/markets/ABC-24",
        "/markets/ABC-24/orderbook",
        "/markets/trades",
        "/series",
        "/exchange/status",
    ],
)
def test_approved_get_paths_allowed(path: str) -> None:
    # Should not raise.
    POLICY.enforce("GET", _url(path))


def test_unapproved_get_path_blocked() -> None:
    with pytest.raises(ReadOnlyPolicyError):
        POLICY.enforce("GET", _url("/markets/ABC-24/orderbook/history"))


async def test_transport_blocks_post_before_dispatch() -> None:
    dispatched = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        dispatched["count"] += 1
        return httpx.Response(200, json={})

    client = build_readonly_client(
        base_url=KALSHI_BASE,
        policy=POLICY,
        inner_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ReadOnlyPolicyError):
            await client.post("/markets", json={"x": 1})
        # The request never reached the inner transport.
        assert dispatched["count"] == 0
    finally:
        await client.aclose()


async def test_transport_allows_approved_get() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"exchange_active": True})

    client = build_readonly_client(
        base_url=KALSHI_BASE,
        policy=POLICY,
        inner_transport=httpx.MockTransport(handler),
    )
    try:
        resp = await client.get("/exchange/status")
        assert resp.status_code == 200
    finally:
        await client.aclose()
