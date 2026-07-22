"""A tiny monotonic-clock TTL cache for provider responses.

The Odds API bills per request, so identical development requests should not
burn credits. This cache is intentionally minimal: an in-memory, TTL-bounded
map keyed by the *sanitized* request signature (secrets never enter the key).
It uses a monotonic clock so expiry is immune to wall-clock adjustments.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class ResponseCache:
    """In-memory TTL cache. Not shared across processes; safe for a single loop."""

    def __init__(self, ttl_seconds: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict[str, tuple[float, object]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[object]:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_at, value = entry
        if self._clock() - stored_at > self._ttl:
            del self._store[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: object) -> None:
        self._store[key] = (self._clock(), value)

    def clear(self) -> None:
        self._store.clear()

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None
