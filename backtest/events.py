"""Replay event types for latency backtesting.

A single :class:`ReplayEvent` carries every event kind we replay, each stamped
with a provider event time (and, when the historical source has it, a separate
provider *publication* time). Order-book events carry pre-normalized ask ladders
(the same representation Module 6 consumes), so the backtester stays focused on
latency and fill mechanics rather than venue-specific book encoding.

Missing timestamps are represented as ``None`` and are what the data-quality
grader keys on: without them the strategy cannot be labeled execution-valid.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

Ladder = Tuple[Tuple[int, int], ...]  # ascending (price_cents, size)


class EventType(str, enum.Enum):
    SPORTS = "sports_event"
    CORRECTION = "event_correction"
    GAME_CLOCK = "game_clock"
    INJURY = "injury_update"
    LINEUP = "lineup_update"
    ODDS = "bookmaker_odds"
    OB_SNAPSHOT = "ob_snapshot"
    OB_DELTA = "ob_delta"
    TRADE = "trade"
    MARKET_STATUS = "market_status"


# Events that trigger a (re)evaluation / decision.
DECISION_TRIGGERS = frozenset(
    {EventType.SPORTS, EventType.CORRECTION, EventType.GAME_CLOCK, EventType.INJURY,
     EventType.LINEUP, EventType.ODDS}
)
# Events that build the market-data timeline.
MARKET_DATA = frozenset(
    {EventType.OB_SNAPSHOT, EventType.OB_DELTA, EventType.TRADE, EventType.MARKET_STATUS}
)


def to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


@dataclass(frozen=True)
class ReplayEvent:
    event_id: str
    event_type: EventType
    market: Optional[str]
    #: Provider event time (when it happened). None => missing (data-quality hit).
    event_time_ns: Optional[int]
    #: Provider publication time, if the historical source recorded it.
    publish_time_ns: Optional[int] = None
    sequence: Optional[int] = None
    is_correction: bool = False
    # Order-book payload (absolute ask ladders per side).
    yes_ask_ladder: Optional[Ladder] = None
    no_ask_ladder: Optional[Ladder] = None
    # Trade payload.
    trade_side: Optional[str] = None
    trade_price: Optional[int] = None
    # Market-status payload: "open" | "paused" | "closed" | "suspended".
    market_status: Optional[str] = None
    payload: dict = field(default_factory=dict)
