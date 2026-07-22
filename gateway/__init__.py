"""Benchmarked execution gateway (Module 8) -- QUARANTINED.

The project is now a strictly read-only recommendation engine. This module is
preserved for reference but its execution functionality is disabled: the real
Kalshi network transports call :func:`gateway.quarantine.ensure_execution_allowed`
before any exchange contact, which raises in read-only mode. Nothing here is
imported on the read-only application's startup path (see ``sports_quant``).

Phase 1 (as built): Python asyncio gateway for Kalshi -- WebSocket market data
and REST limit-order submission -- with a full safety envelope and per-stage
latency benchmarking. Phase 2 (Rust/Tokio) and Phase 3 (Kalshi FIX) were gated
and never implemented. See ``READ_ONLY_ARCHITECTURE.md``.
"""

from .arming import ArmError, ArmingController, LIVE_ARM_TOKEN
from .benchmark import E2E, GatewayReport, LatencyBenchmark, Stage, StageTimer
from .client_ids import ClientOrderIdFactory, IdempotencyRegistry
from .config import GatewayConfig
from .gateway import ExecutionGateway, Strategy
from .quarantine import EXECUTION_QUARANTINED, ExecutionQuarantinedError, ensure_execution_allowed
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
    # quarantine
    "EXECUTION_QUARANTINED",
    "ExecutionQuarantinedError",
    "ensure_execution_allowed",
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
