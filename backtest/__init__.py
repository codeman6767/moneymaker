"""Event-replay and latency backtesting (Module 7).

Deterministically replays timestamped events, applies configurable per-stage
delays, simulates realistic fill outcomes, and produces latency analytics
(profit / edge decay / fill rate / CLV / decision count by latency, break-even
latency, and provider-lag / internal-latency distributions).

Every report leads with a data-quality grade, and the strategy is never labeled
execution-valid when historical order-book or event timestamps are missing.
"""

from .backtester import (
    BacktestConfig,
    BacktestReport,
    DecisionPoint,
    EdgeStrategy,
    ReplayBacktester,
    Strategy,
    StrategyDecision,
)
from .book_timeline import MarketTimeline, build_timelines
from .data_quality import DataQualityReport, grade_dataset
from .events import DECISION_TRIGGERS, MARKET_DATA, EventType, ReplayEvent, to_ns
from .fill_model import (
    FillOutcome,
    FillResult,
    clv_cents,
    expected_profit_cents,
    resolve_fill,
)
from .latency_model import (
    EXCHANGE_STAGES,
    INTERNAL_STAGES,
    PROVIDER_STAGES,
    STAGES,
    LatencyModel,
    LatencySample,
    StageDelay,
)
from .metrics import LatencyMetrics, break_even_latency, build_distributions

__all__ = [
    # events
    "EventType",
    "ReplayEvent",
    "to_ns",
    "DECISION_TRIGGERS",
    "MARKET_DATA",
    # latency
    "StageDelay",
    "LatencyModel",
    "LatencySample",
    "STAGES",
    "PROVIDER_STAGES",
    "INTERNAL_STAGES",
    "EXCHANGE_STAGES",
    # timeline
    "MarketTimeline",
    "build_timelines",
    # fills
    "FillOutcome",
    "FillResult",
    "resolve_fill",
    "expected_profit_cents",
    "clv_cents",
    # data quality
    "DataQualityReport",
    "grade_dataset",
    # metrics
    "LatencyMetrics",
    "break_even_latency",
    "build_distributions",
    # backtester
    "ReplayBacktester",
    "BacktestConfig",
    "BacktestReport",
    "EdgeStrategy",
    "Strategy",
    "StrategyDecision",
    "DecisionPoint",
]
