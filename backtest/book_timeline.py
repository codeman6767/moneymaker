"""Per-market order-book / status / trade timeline for point-in-time lookup.

Built once from the replay events, it answers "what did the book / market status
look like at time t?" via a binary search, which the fill simulator uses to find
the book at the moment an order would reach the exchange -- capturing price
movement during transit.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from evaluation.decision import OrderBookView

from .events import EventType, ReplayEvent, TimedMarketEvent, as_timed_market_event

Ladder = Tuple[Tuple[int, int], ...]


@dataclass
class MarketTimeline:
    times: List[int] = field(default_factory=list)          # sorted event times
    views: List[OrderBookView] = field(default_factory=list)  # book after each point
    statuses: List[str] = field(default_factory=list)        # status at each point
    trades: List[Tuple[int, str, int]] = field(default_factory=list)  # (t, side, price)
    final_view: Optional[OrderBookView] = None

    def _idx_at(self, t: int) -> int:
        # Last point with time <= t.
        return bisect.bisect_right(self.times, t) - 1

    def view_at(self, t: int) -> Optional[OrderBookView]:
        i = self._idx_at(t)
        return self.views[i] if i >= 0 else None

    def status_at(self, t: int) -> str:
        i = self._idx_at(t)
        return self.statuses[i] if i >= 0 else "open"

    def closing_price(self, side: str) -> Optional[int]:
        # Prefer the last trade on the requested side; else the final best ask.
        for _t, s, price in reversed(self.trades):
            if s == side:
                return price
        if self.final_view is not None:
            return self.final_view.best_ask(side)
        return None


def build_timelines(events: List[ReplayEvent]) -> Dict[str, MarketTimeline]:
    """Construct per-market timelines from market-data events."""

    # Group market-data events by market, ordered by event time (skip untimed).
    by_market: Dict[str, List[TimedMarketEvent]] = {}
    for event in events:
        timed = as_timed_market_event(event)
        if timed is None:
            continue
        if event.event_type in (EventType.OB_SNAPSHOT, EventType.OB_DELTA, EventType.TRADE, EventType.MARKET_STATUS):
            by_market.setdefault(timed.market, []).append(timed)

    timelines: Dict[str, MarketTimeline] = {}
    for market, evs in by_market.items():
        evs.sort(key=lambda t: t.event_time_ns)
        tl = MarketTimeline()
        cur_yes: Ladder = ()
        cur_no: Ladder = ()
        cur_status = "open"
        for timed in evs:
            e = timed.event
            if e.event_type == EventType.OB_SNAPSHOT:
                cur_yes = e.yes_ask_ladder or ()
                cur_no = e.no_ask_ladder or ()
            elif e.event_type == EventType.OB_DELTA:
                # Absolute ladders per present side replace the current side.
                if e.yes_ask_ladder is not None:
                    cur_yes = e.yes_ask_ladder
                if e.no_ask_ladder is not None:
                    cur_no = e.no_ask_ladder
            elif e.event_type == EventType.MARKET_STATUS:
                cur_status = e.market_status or cur_status
            elif e.event_type == EventType.TRADE:
                if e.trade_side is not None and e.trade_price is not None:
                    tl.trades.append((timed.event_time_ns, e.trade_side, e.trade_price))
            view = OrderBookView.make(yes_ask_ladder=cur_yes, no_ask_ladder=cur_no)
            tl.times.append(timed.event_time_ns)
            tl.views.append(view)
            tl.statuses.append(cur_status)
        tl.final_view = OrderBookView.make(yes_ask_ladder=cur_yes, no_ask_ladder=cur_no)
        timelines[market] = tl
    return timelines
