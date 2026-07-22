"""Data types for market evaluation: events, snapshots, decisions, orders."""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .latency_trace import LatencyTrace

Ladder = Tuple[Tuple[int, int], ...]  # ascending (price_cents, size)


class Action(str, enum.Enum):
    BET = "bet"
    WATCH = "watch"
    SKIP = "skip"


class SubmitStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    NO_OP = "no_op"                     # nothing to submit (WATCH/SKIP)
    REJECTED_SUPERSEDED = "rejected_superseded"
    REJECTED_PRICE = "rejected_price"
    REJECTED_SEQUENCE = "rejected_sequence"
    REJECTED_STALE = "rejected_stale"


def _hash(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8"))
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class OrderBookView:
    """Ask ladders (ascending price) for each side, plus a content hash."""

    yes_ask_ladder: Ladder = ()
    no_ask_ladder: Ladder = ()
    sequence_ok: bool = True
    book_hash: str = ""

    @staticmethod
    def make(yes_ask_ladder: Sequence[Tuple[int, int]] = (),
             no_ask_ladder: Sequence[Tuple[int, int]] = (),
             sequence_ok: bool = True) -> "OrderBookView":
        yes = tuple(sorted((int(p), int(s)) for p, s in yes_ask_ladder))
        no = tuple(sorted((int(p), int(s)) for p, s in no_ask_ladder))
        return OrderBookView(
            yes_ask_ladder=yes, no_ask_ladder=no, sequence_ok=sequence_ok,
            book_hash=_hash(yes, no),
        )

    def ladder(self, side: str) -> Ladder:
        return self.yes_ask_ladder if side == "yes" else self.no_ask_ladder

    def best_ask(self, side: str) -> Optional[int]:
        lad = self.ladder(side)
        return lad[0][0] if lad else None


@dataclass(frozen=True)
class MarketEvent:
    """A relevant sports / injury / odds / order-book event."""

    event_id: str
    market: str
    event_type: str  # "sports" | "injury" | "odds" | "orderbook"
    sequence: Optional[int]
    event_time: datetime
    received_monotonic_ns: int
    provider_lag_ns: Optional[int] = None
    #: Optional materiality hint (e.g. from the intel detector). None => derive
    #: from state/book hashes.
    material: Optional[bool] = None


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the evaluator needs about a market at event time."""

    market: str
    game_state_hash: str
    market_status: str  # "open" | "paused" | "closed"
    game_sequence_ok: bool
    game_updated_monotonic_ns: int
    order_book_updated_monotonic_ns: int
    feature_vector: np.ndarray
    book: OrderBookView

    @property
    def order_book_hash(self) -> str:
        return self.book.book_hash


@dataclass(frozen=True)
class LimitOrder:
    market: str
    side: str
    limit_price: int
    size: int
    order_type: str = "limit"  # limit orders only


@dataclass
class Decision:
    action: Action
    market: str
    event_id: str
    state_token: int
    game_state_hash: str
    order_book_hash: str
    reasons: List[str] = field(default_factory=list)
    # Trade parameters (populated for BET).
    side: Optional[str] = None
    limit_price: Optional[int] = None
    size: int = 0
    avg_fill_cents: Optional[float] = None
    fair_value_cents: Optional[float] = None
    edge_cents: Optional[float] = None
    fee_per_contract_cents: Optional[float] = None
    slippage_cents: Optional[float] = None
    uncertainty_reserve_cents: Optional[float] = None
    adverse_reserve_cents: Optional[float] = None
    win_prob: Optional[float] = None
    uncertainty_std: Optional[float] = None
    ood: bool = False
    # Risk-reducing cancels to run first, regardless of the action.
    cancels: List[str] = field(default_factory=list)
    trace: Optional[LatencyTrace] = None

    @property
    def is_bet(self) -> bool:
        return self.action is Action.BET


@dataclass
class SubmissionResult:
    status: SubmitStatus
    order: Optional[LimitOrder] = None
    cancels_executed: List[str] = field(default_factory=list)
    reason: Optional[str] = None
