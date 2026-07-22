"""The execution lane is quarantined: real exchange transports refuse to run."""

from __future__ import annotations

import pytest

from gateway.orders import LimitOrderRequest
from gateway.quarantine import (
    EXECUTION_QUARANTINED,
    ExecutionQuarantinedError,
    ensure_execution_allowed,
)
from gateway.transport import KalshiRestTransport, KalshiWsFeed


def test_execution_is_quarantined_by_default() -> None:
    assert EXECUTION_QUARANTINED is True
    with pytest.raises(ExecutionQuarantinedError):
        ensure_execution_allowed()


async def test_real_order_submission_is_blocked() -> None:
    transport = KalshiRestTransport(base_url="https://external-api.kalshi.com/trade-api/v2")
    req = LimitOrderRequest("coid-1", "MKT-1", "yes", 50, 1)
    with pytest.raises(ExecutionQuarantinedError):
        await transport.submit(req)
    with pytest.raises(ExecutionQuarantinedError):
        await transport.cancel("X1")


async def test_real_execution_feed_is_blocked() -> None:
    feed = KalshiWsFeed("wss://external-api.kalshi.com/trade-api/ws/v2")
    with pytest.raises(ExecutionQuarantinedError):
        await feed.connect()
