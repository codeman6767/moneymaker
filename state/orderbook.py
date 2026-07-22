"""Live Kalshi order-book state.

Kalshi binary markets publish resting *bids* on two sides -- ``yes`` and ``no``
-- at integer prices in cents (1..99), plus quantities. Because a No bid at
price ``n`` is economically an offer to sell Yes at ``100 - n`` (and vice
versa), the executable *asks* are derived:

* executable Yes ask  = ``100 - best_no_bid``   (cheapest way to buy Yes)
* executable No ask    = ``100 - best_yes_bid``  (cheapest way to buy No)

An order book must be snapshotted before deltas can be trusted, so
``require_snapshot_first`` is True: a delta arriving with no baseline triggers a
snapshot-recovery request rather than corrupting the book.

Complexity: each level update is an O(1) dict write. The best price per side is
maintained incrementally; only when the current best level is removed do we
rescan, and Kalshi prices are bounded to 1..99 so that rescan is O(1)-bounded.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from streaming.event_envelope import EventEnvelope

from .base import DataQuality, LiveState

SIDES = ("yes", "no")
MIN_PRICE = 1
MAX_PRICE = 99


class OrderBookState(LiveState):
    kind = "kalshi_orderbook"
    require_snapshot_first = True

    def __init__(self, entity_id: str) -> None:
        super().__init__(entity_id)
        self.market: str = entity_id
        # price (int cents) -> quantity, per side.
        self.levels: Dict[str, Dict[int, int]] = {"yes": {}, "no": {}}
        # Incrementally-maintained best (highest) bid price per side.
        self._best: Dict[str, Optional[int]] = {"yes": None, "no": None}

    # -- Level mutation -------------------------------------------------------
    def _set_level(self, side: str, price: int, qty: int) -> None:
        """Set an absolute quantity at a price. qty <= 0 removes the level."""

        if price < MIN_PRICE or price > MAX_PRICE:
            self.quality |= DataQuality.OUT_OF_RANGE
            return
        book = self.levels[side]
        if qty <= 0:
            existed = book.pop(price, None) is not None
            if existed and self._best[side] == price:
                self._recompute_best(side)
        else:
            book[price] = qty
            # New/raised level at or above current best updates best in O(1).
            if self._best[side] is None or price > self._best[side]:
                self._best[side] = price

    def _recompute_best(self, side: str) -> None:
        book = self.levels[side]
        # Bounded to <=99 keys, so this is O(1)-bounded even in the worst case.
        self._best[side] = max(book) if book else None

    # -- Handlers -------------------------------------------------------------
    def _apply_event(self, envelope: EventEnvelope) -> None:
        # A "delta" carries changes for one or both sides. Two accepted shapes:
        #   {"yes": [[price, qty], ...], "no": [[price, qty], ...]}
        #   {"side": "yes", "price": p, "quantity": q}
        p = envelope.payload
        if "side" in p and "price" in p:
            self._set_level(p["side"], int(p["price"]), int(p.get("quantity", 0)))
            return
        for side in SIDES:
            for price, qty in p.get(side, []):
                self._set_level(side, int(price), int(qty))

    def _apply_snapshot(self, envelope: EventEnvelope) -> None:
        s = envelope.payload
        if "market" in s:
            self.market = s["market"]
        self.levels = {"yes": {}, "no": {}}
        self._best = {"yes": None, "no": None}
        for side in SIDES:
            for price, qty in s.get(side, []):
                self._set_level(side, int(price), int(qty))

    def _apply_correction(self, envelope: EventEnvelope) -> None:
        # A correction restates absolute levels for the prices it names.
        self._apply_event(envelope)

    # -- Derived quantities ---------------------------------------------------
    @property
    def best_yes_bid(self) -> Optional[int]:
        return self._best["yes"]

    @property
    def best_no_bid(self) -> Optional[int]:
        return self._best["no"]

    @property
    def executable_yes_ask(self) -> Optional[int]:
        """Cheapest executable price to BUY Yes, derived from best No bid."""

        return None if self._best["no"] is None else MAX_PRICE + 1 - self._best["no"]

    @property
    def executable_no_ask(self) -> Optional[int]:
        """Cheapest executable price to BUY No, derived from best Yes bid."""

        return None if self._best["yes"] is None else MAX_PRICE + 1 - self._best["yes"]

    def _content(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "levels": {
                side: {str(price): qty for price, qty in sorted(self.levels[side].items())}
                for side in SIDES
            },
            "best_yes_bid": self.best_yes_bid,
            "best_no_bid": self.best_no_bid,
            "executable_yes_ask": self.executable_yes_ask,
            "executable_no_ask": self.executable_no_ask,
            "last_snapshot_sequence": self.last_snapshot_sequence,
            "last_delta_sequence": self.last_delta_sequence,
        }
