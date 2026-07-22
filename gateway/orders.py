"""Order models and lifecycle (limit orders only).

Tracks an order through submission, acknowledgement, partial fills, and terminal
states, so partial-fill processing and timeout reconciliation have a single
source of truth per order.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import List, Optional


class OrderState(str, enum.Enum):
    NEW = "new"
    SUBMITTED = "submitted"       # sent, no ack yet
    ACKED = "acked"               # exchange accepted, resting
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"       # never confirmed within the timeout


TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}


@dataclass(frozen=True)
class OrderIntent:
    market: str
    side: str      # "yes" | "no"
    price: int     # limit price in cents
    size: int


@dataclass(frozen=True)
class LimitOrderRequest:
    client_order_id: str
    market: str
    side: str
    price: int
    size: int
    order_type: str = "limit"  # limit orders only


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    exchange_order_id: Optional[str]
    accepted: bool
    reason: Optional[str] = None
    #: Transport-reported time from send to exchange ack (ns), for benchmarking.
    exchange_ack_latency_ns: Optional[int] = None


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    size: int
    price: int


@dataclass(frozen=True)
class OrderStatusReport:
    client_order_id: str
    state: OrderState
    filled_size: int


@dataclass
class OrderRecord:
    request: LimitOrderRequest
    state: OrderState = OrderState.NEW
    exchange_order_id: Optional[str] = None
    filled_size: int = 0
    fill_cost_cents: int = 0
    submitted_monotonic_ns: Optional[int] = None
    fills: List[Fill] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.request.size - self.filled_size

    @property
    def avg_fill_price(self) -> Optional[float]:
        return self.fill_cost_cents / self.filled_size if self.filled_size else None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def apply_fill(self, fill: Fill) -> None:
        self.filled_size += fill.size
        self.fill_cost_cents += fill.size * fill.price
        self.fills.append(fill)
        if self.filled_size >= self.request.size:
            self.state = OrderState.FILLED
        else:
            self.state = OrderState.PARTIALLY_FILLED
