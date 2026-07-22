"""Fill simulation.

Given the book at the moment an order reaches the exchange (plus staleness and
market-status flags computed by the backtester), resolve the outcome. This is
where the required execution scenarios live: miss the price, partial fill, no
fill, market suspension, stale decision, cancellation, and the price movement
during transit that drives them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from evaluation.decision import OrderBookView
from evaluation.pricing import FeeModel, walk_ladder


class FillOutcome(str, enum.Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    MISS = "miss"              # price moved beyond the approved limit in transit
    NO_FILL = "no_fill"        # no liquidity on that side
    SUSPENDED = "suspended"    # market not open at arrival
    STALE = "stale"            # superseded by a newer event before arrival


@dataclass(frozen=True)
class FillResult:
    outcome: FillOutcome
    requested_size: int
    filled_size: int
    avg_price_cents: Optional[float]
    best_ask_at_arrival: Optional[int]
    cancelled: bool  # an order we actively pulled rather than left resting

    @property
    def is_fill(self) -> bool:
        return self.outcome in (FillOutcome.FILLED, FillOutcome.PARTIAL)


def resolve_fill(
    *,
    side: str,
    limit_price: int,
    size: int,
    view: Optional[OrderBookView],
    status_at_arrival: str,
    is_stale: bool,
) -> FillResult:
    # Precedence: stale and suspended orders are cancelled before they can fill.
    if is_stale:
        return FillResult(FillOutcome.STALE, size, 0, None, None, cancelled=True)
    if status_at_arrival != "open":
        return FillResult(FillOutcome.SUSPENDED, size, 0, None, None, cancelled=True)

    ladder = view.ladder(side) if view is not None else ()
    if not ladder:
        return FillResult(FillOutcome.NO_FILL, size, 0, None, None, cancelled=False)

    best = ladder[0][0]
    if best > limit_price:
        # Price ran away in transit; the limit no longer touches the book.
        return FillResult(FillOutcome.MISS, size, 0, None, best, cancelled=False)

    filled, avg = walk_ladder(ladder, size, limit_price)
    if filled == 0:
        return FillResult(FillOutcome.MISS, size, 0, avg, best, cancelled=False)
    outcome = FillOutcome.FILLED if filled >= size else FillOutcome.PARTIAL
    return FillResult(outcome, size, filled, avg, best, cancelled=False)


def expected_profit_cents(fair_value_cents: float, fill: FillResult, fee_model: FeeModel) -> float:
    """Model-expected edge captured, net of fees. Zero when nothing fills."""

    if not fill.is_fill or fill.avg_price_cents is None:
        return 0.0
    gross = (fair_value_cents - fill.avg_price_cents) * fill.filled_size
    fees = fee_model.total_cents(fill.avg_price_cents, fill.filled_size)
    return gross - fees


def clv_cents(closing_price: Optional[int], fill: FillResult) -> Optional[float]:
    """Closing-line value per contract: closing price minus our fill price."""

    if not fill.is_fill or fill.avg_price_cents is None or closing_price is None:
        return None
    return closing_price - fill.avg_price_cents
