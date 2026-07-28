"""F1A request/credit gate — adversarial unit tests (fully offline, mocked).

Every test drives the real transport chokepoint
(:meth:`sports_quant.providers.base_provider.BaseProviderClient._get`) with an
``httpx.MockTransport`` whose handler counts how many transport calls actually
occurred, proving budget reservations gate the socket, not just the caller.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from sports_quant.ingest.cost_policies import build_balldontlie_policy, build_mlb_policy
from sports_quant.providers.base_provider import BaseProviderClient, ProviderError
from sports_quant.request_control import (
    BudgetExhausted,
    CreditBudget,
    EndpointCostPolicy,
    LimitType,
    RequestBudget,
    RequestGate,
)


class _Client(BaseProviderClient):
    provider_name = "balldontlie"


def _gated_client(gate: RequestGate, handler, *, provider: str = "balldontlie") -> _Client:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://test.invalid")
    c = _Client(base_url="http://test.invalid", policy=None, client=client,  # type: ignore[arg-type]
                gate=gate, league="nba", sleep=_no_sleep)
    c.provider_name = provider
    return c


async def _no_sleep(_seconds: float) -> None:  # retries must not actually wait
    return None


def _counter_handler(calls: list[int], *, status: int = 200):
    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"ok": True})
    return handler


def _nba_gate(*, max_requests: int, max_credits: int | None) -> RequestGate:
    return RequestGate(
        request_budget=RequestBudget(max_requests=max_requests),
        credit_budget=CreditBudget(applicable=True, max_credits=max_credits),
        cost_policy=build_balldontlie_policy(),
    )


def _mlb_gate(*, max_requests: int) -> RequestGate:
    return RequestGate(
        request_budget=RequestBudget(max_requests=max_requests),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=build_mlb_policy(),
    )


def test_zero_request_budget_makes_zero_transport_calls() -> None:
    calls: list[int] = []
    gate = _nba_gate(max_requests=0, max_credits=100)
    c = _gated_client(gate, _counter_handler(calls))

    async def go() -> None:
        with pytest.raises(BudgetExhausted) as exc:
            await c._get("/v1/games", endpoint_family="games")
        assert exc.value.limit_type is LimitType.REQUEST
        await c.aclose()

    asyncio.run(go())
    assert calls == []  # transport never touched
    assert gate.usage.attempted_requests == 0
    assert gate.usage.blocked_requests == 1


def test_request_cap_stops_before_prohibited_call() -> None:
    calls: list[int] = []
    gate = _nba_gate(max_requests=2, max_credits=100)
    c = _gated_client(gate, _counter_handler(calls))

    async def go() -> None:
        await c._get("/v1/games", endpoint_family="games")
        await c._get("/v1/games", endpoint_family="games")
        with pytest.raises(BudgetExhausted):
            await c._get("/v1/games", endpoint_family="games")
        await c.aclose()

    asyncio.run(go())
    assert sum(calls) == 2  # third never hit the socket
    assert gate.usage.attempted_requests == 2
    assert gate.usage.successful_responses == 2


def test_retry_consumes_budget() -> None:
    # 503 forces a retry; with only 1 request slot the retry reservation fails.
    calls: list[int] = []
    gate = _nba_gate(max_requests=1, max_credits=100)
    c = _gated_client(gate, _counter_handler(calls, status=503))

    async def go() -> None:
        with pytest.raises(BudgetExhausted) as exc:
            await c._get("/v1/games", endpoint_family="games")
        assert exc.value.limit_type is LimitType.REQUEST
        await c.aclose()

    asyncio.run(go())
    assert sum(calls) == 1  # initial 503 used the only slot; retry blocked pre-socket
    assert gate.usage.attempted_requests == 1
    assert gate.usage.retry_attempts == 0  # retry never got to reserve a slot


def test_retry_succeeds_when_budget_allows() -> None:
    seq = [503, 200]
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(seq[len(calls) - 1], json={"ok": True})

    gate = _nba_gate(max_requests=2, max_credits=100)
    c = _gated_client(gate, handler)

    async def go() -> None:
        await c._get("/v1/games", endpoint_family="games")
        await c.aclose()

    asyncio.run(go())
    assert sum(calls) == 2
    assert gate.usage.retry_attempts == 1
    assert gate.usage.reserved_credits == 2  # initial + retry each reserve a credit


def test_unknown_nba_credit_cost_fails_closed() -> None:
    calls: list[int] = []
    gate = _nba_gate(max_requests=100, max_credits=100)
    c = _gated_client(gate, _counter_handler(calls))

    async def go() -> None:
        with pytest.raises(BudgetExhausted) as exc:
            await c._get("/v1/mystery-endpoint", endpoint_family=None)  # classifies unknown
        assert exc.value.limit_type is LimitType.UNKNOWN_CREDIT_COST
        await c.aclose()

    asyncio.run(go())
    assert calls == []  # unknown credit cost never touches the socket


def test_mlb_request_cap_without_fabricated_credits() -> None:
    calls: list[int] = []
    gate = _mlb_gate(max_requests=1)
    c = _gated_client(gate, _counter_handler(calls), provider="mlb_statsapi")

    async def go() -> None:
        await c._get("/schedule", endpoint_family="schedule")
        with pytest.raises(BudgetExhausted):
            await c._get("/schedule", endpoint_family="schedule")
        await c.aclose()

    asyncio.run(go())
    assert sum(calls) == 1
    assert gate.usage.credits_applicable is False
    assert gate.usage.credit_header_status == "not_applicable"
    assert gate.usage.reserved_credits == 0  # no fabricated credit accounting


def test_credit_limit_while_requests_remain() -> None:
    calls: list[int] = []
    gate = _nba_gate(max_requests=100, max_credits=1)  # requests ample, credits tight
    c = _gated_client(gate, _counter_handler(calls))

    async def go() -> None:
        await c._get("/v1/games", endpoint_family="games")  # 1 credit
        with pytest.raises(BudgetExhausted) as exc:
            await c._get("/v1/games", endpoint_family="games")  # would be 2nd credit
        assert exc.value.limit_type is LimitType.CREDIT
        await c.aclose()

    asyncio.run(go())
    assert sum(calls) == 1


def test_exact_boundary_one_request() -> None:
    calls: list[int] = []
    gate = _nba_gate(max_requests=1, max_credits=100)
    c = _gated_client(gate, _counter_handler(calls))

    async def go() -> None:
        await c._get("/v1/games", endpoint_family="games")  # exactly fills the budget
        with pytest.raises(BudgetExhausted):
            await c._get("/v1/games", endpoint_family="games")
        await c.aclose()

    asyncio.run(go())
    assert sum(calls) == 1


def test_single_request_costs_more_than_whole_budget() -> None:
    # A policy where one call costs 5 credits but the cap is 3 -> never sent.
    calls: list[int] = []
    policy = EndpointCostPolicy(
        provider="balldontlie", version="t", credit_applicable=True,
        costs={"games": 5}, classifier=lambda _p: "games",
    )
    gate = RequestGate(
        request_budget=RequestBudget(max_requests=100),
        credit_budget=CreditBudget(applicable=True, max_credits=3),
        cost_policy=policy,
    )
    c = _gated_client(gate, _counter_handler(calls))

    async def go() -> None:
        with pytest.raises(BudgetExhausted) as exc:
            await c._get("/v1/games", endpoint_family="games")
        assert exc.value.limit_type is LimitType.CREDIT
        await c.aclose()

    asyncio.run(go())
    assert calls == []


def test_transport_error_after_reservation_is_visible() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    gate = _nba_gate(max_requests=5, max_credits=100)  # 1 initial + up to retries
    c = _gated_client(gate, handler)

    async def go() -> None:
        with pytest.raises(ProviderError):
            await c._get("/v1/games", endpoint_family="games")
        await c.aclose()

    asyncio.run(go())
    # initial + 3 retries all reserved and all failed the socket.
    assert gate.usage.attempted_requests == 4
    assert gate.usage.failed_responses == 1
    assert gate.usage.retry_attempts == 3


def test_gate_reservation_is_concurrency_safe() -> None:
    import threading

    gate = _nba_gate(max_requests=50, max_credits=1000)
    from sports_quant.request_control import RequestUnit

    unit = RequestUnit(provider="balldontlie", league="nba", endpoint_family="games")
    errors: list[BudgetExhausted] = []

    def worker() -> None:
        for _ in range(20):
            try:
                gate.reserve(unit)
            except BudgetExhausted as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 5*20 = 100 attempts, cap 50 -> exactly 50 reserved, 50 blocked, no oversell.
    assert gate.usage.attempted_requests == 50
    assert gate.usage.blocked_requests == 50
    assert gate.usage.reserved_credits == 50
