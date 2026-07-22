"""Tests for the benchmarked execution gateway (Module 8, Phase 1)."""

from __future__ import annotations

import pytest

from gateway import (
    LIVE_ARM_TOKEN,
    ArmError,
    ArmingController,
    ClientOrderIdFactory,
    ExecutionGateway,
    FakeOrderTransport,
    Fill,
    GatewayConfig,
    GatewayReport,
    KalshiLimits,
    LimitOrderRequest,
    OrderIntent,
    OrderState,
    Stage,
    StaticLimitsProvider,
)

MARKET = "KX1"


def make_strategy(market=MARKET, threshold=60, size=10):
    def strat(book, status):
        ask = book.best_ask("yes")
        if ask is not None and ask <= threshold:
            return OrderIntent(market=market, side="yes", price=ask + 1, size=size)
        return None
    return strat


def ob_event(market=MARKET, yes=((55, 50),), no=((50, 50),)):
    return {"type": "orderbook", "market": market, "yes": yes, "no": no}


def build_gateway(config=None, transport=None, limits=None, arming=None, strategy=None):
    cfg = config or GatewayConfig()
    tr = transport or FakeOrderTransport()
    provider = StaticLimitsProvider(limits) if limits is not None else StaticLimitsProvider()
    return ExecutionGateway(cfg, tr, strategy or make_strategy(),
                            arming=arming, limits_provider=provider)


# --------------------------------------------------------------------------- #
# Startup + demo bet
# --------------------------------------------------------------------------- #
async def test_startup_queries_limits():
    gw = build_gateway()
    await gw.startup()
    assert gw.limits is not None
    assert gw.token_budget is not None


async def test_demo_bet_acked_by_default():
    gw = build_gateway()
    await gw.startup()
    record = await gw.process_event(ob_event())
    assert record is not None
    assert record.state is OrderState.ACKED
    assert record.exchange_order_id is not None
    assert gw.config.is_demo


# --------------------------------------------------------------------------- #
# Unique client ids + idempotency
# --------------------------------------------------------------------------- #
def test_client_order_ids_are_unique():
    f = ClientOrderIdFactory()
    ids = {f.new() for _ in range(2000)}
    assert len(ids) == 2000


async def test_idempotent_resubmission():
    transport = FakeOrderTransport()
    gw = build_gateway(transport=transport)
    await gw.startup()
    req = LimitOrderRequest("fixed-coid", MARKET, "yes", 56, 10)
    ack1 = await gw.submit_limit_order(req)
    ack2 = await gw.submit_limit_order(req)
    assert ack1 is ack2                # cached, identical ack
    assert transport.submit_count == 1  # exchange hit once


# --------------------------------------------------------------------------- #
# Partial-fill processing
# --------------------------------------------------------------------------- #
async def test_partial_then_full_fill():
    gw = build_gateway()
    await gw.startup()
    rec = await gw.process_event(ob_event())
    coid = rec.request.client_order_id

    await gw.on_fill(Fill(coid, 4, 56))
    assert rec.state is OrderState.PARTIALLY_FILLED
    assert rec.filled_size == 4
    assert rec.remaining == 6

    await gw.on_fill(Fill(coid, 6, 56))
    assert rec.state is OrderState.FILLED
    assert rec.remaining == 0


# --------------------------------------------------------------------------- #
# Timeout reconciliation
# --------------------------------------------------------------------------- #
async def test_timeout_reconciliation_marks_timed_out():
    transport = FakeOrderTransport(behavior="drop")
    gw = build_gateway(config=GatewayConfig(order_timeout_ns=0, max_consecutive_failures=99),
                       transport=transport)
    await gw.startup()
    rec = await gw.process_event(ob_event())
    assert rec.state is OrderState.SUBMITTED  # no ack received
    reconciled = await gw.reconcile_timeouts()
    assert rec.request.client_order_id in reconciled
    assert rec.state is OrderState.TIMED_OUT


async def test_timeout_reconciliation_recovers_from_query():
    from gateway import OrderStatusReport

    transport = FakeOrderTransport(behavior="drop")
    gw = build_gateway(config=GatewayConfig(order_timeout_ns=0, max_consecutive_failures=99),
                       transport=transport)
    await gw.startup()
    rec = await gw.process_event(ob_event())
    coid = rec.request.client_order_id
    transport.reconcile_reports[coid] = OrderStatusReport(coid, OrderState.FILLED, 10)
    await gw.reconcile_timeouts()
    assert rec.state is OrderState.FILLED
    assert rec.filled_size == 10


# --------------------------------------------------------------------------- #
# Cancel and replace
# --------------------------------------------------------------------------- #
async def test_cancel_and_replace():
    transport = FakeOrderTransport()
    gw = build_gateway(transport=transport)
    await gw.startup()
    rec = await gw.process_event(ob_event())
    coid = rec.request.client_order_id

    new_rec = await gw.cancel_replace(coid, new_price=57)
    assert rec.state is OrderState.CANCELED
    assert transport.cancel_count == 1
    assert new_rec is not None and new_rec.state is OrderState.ACKED
    assert new_rec.request.price == 57
    assert transport.submit_count == 2


# --------------------------------------------------------------------------- #
# Market pause handling
# --------------------------------------------------------------------------- #
async def test_market_pause_blocks_and_cancels():
    transport = FakeOrderTransport()
    gw = build_gateway(transport=transport)
    await gw.startup()
    rec = await gw.process_event(ob_event())  # resting order
    assert rec.state is OrderState.ACKED

    gw._handle_status = gw._handle_status  # (documenting: status flows via process_event)
    await gw.process_event({"type": "status", "market": MARKET, "status": "paused"})
    canceled = await gw.cancel_resting(MARKET)
    assert canceled == 1
    assert rec.state is OrderState.CANCELED

    # New orders are blocked while paused.
    blocked = await gw.process_event(ob_event())
    assert blocked is None


# --------------------------------------------------------------------------- #
# Arming: demo by default, no live order without explicit arming, auto-disarm
# --------------------------------------------------------------------------- #
async def test_live_order_refused_without_arming():
    arming = ArmingController(environment="live")
    gw = build_gateway(config=GatewayConfig.live(), arming=arming)
    await gw.startup()
    # Direct submit is gated.
    with pytest.raises(ArmError):
        await gw.submit_limit_order(LimitOrderRequest("c1", MARKET, "yes", 56, 10))
    # And the pipeline simply produces no order.
    assert await gw.process_event(ob_event()) is None


def test_arm_requires_correct_token():
    arming = ArmingController(environment="live")
    with pytest.raises(ArmError):
        arming.arm("wrong-token")
    arming.arm(LIVE_ARM_TOKEN)
    assert arming.is_armed


async def test_automatic_disarm_after_consecutive_failures():
    arming = ArmingController(environment="live")
    arming.arm(LIVE_ARM_TOKEN)
    transport = FakeOrderTransport(behavior="drop")  # every submit fails
    gw = build_gateway(config=GatewayConfig.live(), transport=transport, arming=arming)
    await gw.startup()
    for _ in range(3):  # max_consecutive_failures default 3
        await gw.process_event(ob_event())
    assert not arming.is_armed
    assert arming.disarm_reason == "consecutive_failures"


# --------------------------------------------------------------------------- #
# Local token budget / rate-limit events
# --------------------------------------------------------------------------- #
async def test_token_budget_rate_limits_locally():
    limits = KalshiLimits(
        read_rate_per_sec=1.0, read_burst=2,
        write_rate_per_sec=1.0, write_burst=2,
        endpoint_costs={"create_order": ("write", 1)},
    )
    transport = FakeOrderTransport()
    gw = build_gateway(transport=transport, limits=limits)
    await gw.startup()
    acked = 0
    for _ in range(5):
        rec = await gw.process_event(ob_event())
        if rec is not None and rec.state is OrderState.ACKED:
            acked += 1
    assert acked == 2                       # only the burst got through
    assert gw.report().rate_limit_events == 3


# --------------------------------------------------------------------------- #
# Benchmark report + sub-second claim gate
# --------------------------------------------------------------------------- #
def big_limits():
    return KalshiLimits(100000.0, 100000, 100000.0, 100000,
                        {"create_order": ("write", 1)})


async def test_benchmark_report_stages_and_counters():
    gw = build_gateway(limits=big_limits())
    await gw.startup()
    rec = await gw.process_event(ob_event())
    await gw.on_fill(Fill(rec.request.client_order_id, 10, 56))
    gw.on_reconnect()
    report = gw.report()
    for stage in (Stage.PARSE, Stage.ORDER_BOOK_UPDATE, Stage.INFERENCE, Stage.DECISION,
                  Stage.SIGNING, Stage.NETWORK_SUBMISSION, Stage.EXCHANGE_ACK,
                  Stage.FILL_NOTIFICATION):
        assert stage.value in report.stages
        assert report.stages[stage.value]["p90"] is not None
    assert report.reconnects == 1
    assert report.failures == 0


async def test_sub_second_claim_requires_meaningful_sample():
    gw = build_gateway(config=GatewayConfig(min_benchmark_samples=100), limits=big_limits())
    await gw.startup()
    # Too few samples => no claim.
    for _ in range(5):
        await gw.process_event(ob_event())
    assert gw.report().claims_sub_second() is False

    # Enough samples, all fast => sub-second confirmed.
    for _ in range(120):
        await gw.process_event(ob_event())
    report = gw.report()
    assert report.e2e["count"] >= 100
    assert report.claims_sub_second() is True
    assert "sub-second" in report.latency_claim()


def test_not_labeled_sub_second_when_slow():
    # A large-sample but slow distribution must NOT be called sub-second.
    report = GatewayReport(
        stages={}, e2e={"count": 200, "p50": 5e8, "p90": 1.5e9, "p95": 1.8e9, "p99": 2.0e9, "max": 3e9},
        failures=0, reconnects=0, rate_limit_events=0,
        sub_second_threshold_ns=1_000_000_000, min_samples=100,
    )
    assert report.claims_sub_second() is False
    assert "NOT sub-second" in report.latency_claim()
