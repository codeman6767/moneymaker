"""Event-driven market evaluation (Module 6).

Turns each relevant sports/injury/odds/order-book event into a BET / WATCH /
SKIP decision, orchestrating the streaming, state, intel and probability modules
behind a single pipeline with debounce, coalescing, supersession and pre-submit
revalidation. See ``CLAUDE.md``: limit orders only, never act on an outdated
state version, reject on excessive provider lag, and speed never bypasses risk
gates.
"""

from .decision import (
    MAX_PRICE_CENTS,
    MIN_PRICE_CENTS,
    VALID_SIDES,
    Action,
    Decision,
    LimitOrder,
    MarketEvent,
    MarketSnapshot,
    OrderBookView,
    SubmissionResult,
    SubmitStatus,
    ValidatedTrade,
    validate_trade,
)
from .evaluator import EvaluationConfig, MarketEvaluator
from .latency_trace import LatencyTrace
from .portfolio import Portfolio, PortfolioLimits
from .pricing import (
    FeeModel,
    SideQuote,
    adverse_reserve_cents,
    quote_side,
    uncertainty_reserve_cents,
    walk_ladder,
)

__all__ = [
    "Action",
    "Decision",
    "MarketEvent",
    "MarketSnapshot",
    "OrderBookView",
    "LimitOrder",
    "SubmissionResult",
    "SubmitStatus",
    "ValidatedTrade",
    "validate_trade",
    "VALID_SIDES",
    "MIN_PRICE_CENTS",
    "MAX_PRICE_CENTS",
    "MarketEvaluator",
    "EvaluationConfig",
    "LatencyTrace",
    "Portfolio",
    "PortfolioLimits",
    "FeeModel",
    "SideQuote",
    "quote_side",
    "walk_ladder",
    "uncertainty_reserve_cents",
    "adverse_reserve_cents",
]
