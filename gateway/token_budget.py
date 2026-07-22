"""Local token-budget manager (client-side rate limiting).

Mirrors Kalshi's limits locally so we throttle ourselves before the exchange
does. Token buckets refill on the monotonic clock; a rejected consume is
recorded as a rate-limit event for the benchmark report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from streaming.latency import monotonic_ns

from .limits import KalshiLimits


@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_ns: int

    def try_consume(self, cost: float, now_ns: int) -> bool:
        elapsed_s = max(0, now_ns - self.last_ns) / 1_000_000_000.0
        self.tokens = min(self.capacity, self.tokens + elapsed_s * self.refill_per_sec)
        self.last_ns = now_ns
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class TokenBudgetManager:
    def __init__(self, buckets: Dict[str, TokenBucket]) -> None:
        self._buckets = buckets
        self.rate_limit_events = 0

    @classmethod
    def from_limits(cls, limits: KalshiLimits, *, now_ns: int | None = None) -> "TokenBudgetManager":
        t0 = now_ns if now_ns is not None else monotonic_ns()
        return cls({
            "read": TokenBucket(limits.read_burst, limits.read_rate_per_sec, limits.read_burst, t0),
            "write": TokenBucket(limits.write_burst, limits.write_rate_per_sec, limits.write_burst, t0),
        })

    def consume(self, category: str, cost: float = 1.0, *, now_ns: int | None = None) -> bool:
        now = now_ns if now_ns is not None else monotonic_ns()
        bucket = self._buckets.get(category)
        if bucket is None:
            return True
        ok = bucket.try_consume(cost, now)
        if not ok:
            self.rate_limit_events += 1
        return ok

    def available(self, category: str) -> float:
        b = self._buckets.get(category)
        return b.tokens if b else float("inf")
