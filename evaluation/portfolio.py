"""Portfolio limits and risk-reducing cancels.

Enforces per-market and gross position limits and an order-size cap. Risk-
reducing cancels (pulling resting risk-increasing orders) are always permitted
and are prioritized by the evaluator, even when new orders are blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .decision import LimitOrder


@dataclass(frozen=True)
class PortfolioLimits:
    max_position_per_market: int = 100
    max_gross_contracts: int = 1000
    max_order_size: int = 50


@dataclass
class Portfolio:
    limits: PortfolioLimits = field(default_factory=PortfolioLimits)
    positions: Dict[str, int] = field(default_factory=dict)  # market -> contracts held
    open_orders: Dict[str, LimitOrder] = field(default_factory=dict)  # order_id -> order

    def gross(self) -> int:
        return sum(abs(v) for v in self.positions.values())

    def allowed_size(self, market: str, size: int) -> int:
        """Largest size we may add for ``market`` without breaching limits."""

        room_market = self.limits.max_position_per_market - self.positions.get(market, 0)
        room_gross = self.limits.max_gross_contracts - self.gross()
        return max(0, min(size, room_market, room_gross, self.limits.max_order_size))

    def at_limit(self, market: str) -> bool:
        return self.allowed_size(market, 1) == 0

    def apply_fill(self, order: LimitOrder) -> None:
        self.positions[order.market] = self.positions.get(order.market, 0) + order.size

    def register_open(self, order_id: str, order: LimitOrder) -> None:
        self.open_orders[order_id] = order

    def risk_reducing_cancels(self, market: str) -> List[str]:
        """Open (risk-increasing) orders for the market that should be pulled."""

        return [oid for oid, o in self.open_orders.items() if o.market == market]

    def cancel(self, order_id: str) -> None:
        self.open_orders.pop(order_id, None)
