"""Benchmarked execution gateway (Module 8).

Phase 1 (implemented): Python asyncio gateway for Kalshi demo -- WebSocket
market data and REST limit-order submission -- with a full safety envelope
(demo by default, explicit arming for live, local token budget, idempotent
unique client order ids, timeout reconciliation, partial fills, cancel/replace,
market-pause handling, automatic disarm) and per-stage latency benchmarking.

Phase 2 (Rust/Tokio) and Phase 3 (Kalshi FIX) are gated and NOT implemented
here -- see ``PHASES.md``.
"""

from .arming import ArmError, ArmingController, LIVE_ARM_TOKEN
from .benchmark import E2E, GatewayReport, LatencyBenchmark, Stage, StageTimer
from .client_ids import ClientOrderIdFactory, IdempotencyRegistry
from .config import GatewayConfig
from .gateway import ExecutionGateway, Strategy
from .limits import (
    DEFAULT_LIMITS,
    KalshiLimits,
    KalshiLimitsProvider,
    LimitsProvider,
    StaticLimitsProvider,
)
from .orders import (
    Fill,
    LimitOrderRequest,
    OrderAck,
    OrderIntent,
    OrderRecord,
    OrderState,
    OrderStatusReport,
)
from .token_budget import TokenBucket, TokenBudgetManager
from .transport import (
    FakeMarketDataFeed,
    FakeOrderTransport,
    KalshiRestTransport,
    KalshiWsFeed,
    MarketDataFeed,
    OrderTransport,
)

__all__ = [
    "GatewayConfig",
    "ExecutionGateway",
    "Strategy",
    # arming
    "ArmingController",
    "ArmError",
    "LIVE_ARM_TOKEN",
    # limits / budget
    "KalshiLimits",
    "DEFAULT_LIMITS",
    "LimitsProvider",
    "StaticLimitsProvider",
    "KalshiLimitsProvider",
    "TokenBucket",
    "TokenBudgetManager",
    # ids
    "ClientOrderIdFactory",
    "IdempotencyRegistry",
    # orders
    "OrderIntent",
    "LimitOrderRequest",
    "OrderAck",
    "OrderRecord",
    "OrderState",
    "OrderStatusReport",
    "Fill",
    # benchmark
    "Stage",
    "StageTimer",
    "LatencyBenchmark",
    "GatewayReport",
    "E2E",
    # transport
    "OrderTransport",
    "MarketDataFeed",
    "FakeOrderTransport",
    "FakeMarketDataFeed",
    "KalshiRestTransport",
    "KalshiWsFeed",
]
