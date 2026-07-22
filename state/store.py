"""Single-writer live-state store.

Ownership model: one writer thread applies events through :meth:`apply`; any
number of readers call :meth:`snapshot`. A re-entrant lock guards every access
so an applied event is either fully visible or not visible at all -- snapshots
are atomic. There are no database calls anywhere on this path.

The store maps an *entity key* (e.g. a game id or market ticker) to a
:class:`LiveState`. A subject-to-factory registry lets the store create the
right state type on first sight of an entity, so a single store can hold MLB
games, NBA games and Kalshi books side by side.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from streaming.event_envelope import EventEnvelope

from .base import ApplyResult, ApplyStatus, LiveState, StateSnapshot
from .mlb import MLBGameState
from .nba import NBAGameState
from .orderbook import OrderBookState

StateFactory = Callable[[str], LiveState]

# Default subject -> state-type routing, aligned with the streaming subjects.
DEFAULT_ROUTES: Dict[str, StateFactory] = {
    "sports.mlb.events": MLBGameState,
    "sports.nba.events": NBAGameState,
    "kalshi.orderbook": OrderBookState,
}


def default_entity_key(envelope: EventEnvelope) -> str:
    """Derive the entity key for an event.

    Prefers an explicit ``game_id`` / ``market`` in the payload, else falls back
    to the envelope's ``stream_key`` so distinct games/markets never collide.
    """

    p = envelope.payload
    for field in ("game_id", "market", "market_ticker", "entity_id"):
        if field in p:
            return str(p[field])
    return envelope.stream_key or f"{envelope.provider}:{envelope.subject}"


class LiveStateStore:
    """Thread-safe container of live states with single-writer semantics."""

    def __init__(
        self,
        routes: Optional[Dict[str, StateFactory]] = None,
        key_fn: Callable[[EventEnvelope], str] = default_entity_key,
    ) -> None:
        self._routes = dict(routes or DEFAULT_ROUTES)
        self._key_fn = key_fn
        self._states: Dict[str, LiveState] = {}
        self._lock = threading.RLock()

    def register_route(self, subject: str, factory: StateFactory) -> None:
        with self._lock:
            self._routes[subject] = factory

    def _get_or_create(self, envelope: EventEnvelope) -> Optional[LiveState]:
        factory = self._routes.get(envelope.subject)
        if factory is None:
            return None
        key = self._key_fn(envelope)
        state = self._states.get(key)
        if state is None:
            state = factory(key)
            self._states[key] = state
        return state

    def apply(self, envelope: EventEnvelope) -> ApplyResult:
        with self._lock:
            state = self._get_or_create(envelope)
            if state is None:
                return ApplyResult(
                    ApplyStatus.REJECTED,
                    entity_id=self._key_fn(envelope),
                    sequence=envelope.sequence,
                    message=f"no route for subject {envelope.subject!r}",
                )
            return state.apply(envelope)

    def snapshot(
        self,
        key: str,
        *,
        now_monotonic_ns: Optional[int] = None,
        staleness_max_age_ns: Optional[int] = None,
    ) -> Optional[StateSnapshot]:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return None
            return state.snapshot(
                now_monotonic_ns=now_monotonic_ns,
                staleness_max_age_ns=staleness_max_age_ns,
            )

    def snapshot_all(
        self, *, staleness_max_age_ns: Optional[int] = None
    ) -> Dict[str, StateSnapshot]:
        with self._lock:
            return {
                key: state.snapshot(staleness_max_age_ns=staleness_max_age_ns)
                for key, state in self._states.items()
            }

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._states.keys())

    def get(self, key: str) -> Optional[LiveState]:
        with self._lock:
            return self._states.get(key)
