"""Latency histograms for the streaming fast path.

Per ``CLAUDE.md``:

* all fast paths must expose latency histograms with p50, p95, p99 and max;
* all latency measurement must use monotonic clocks.

This module deliberately depends only on the standard library so it can sit on
the hot path with predictable overhead. It uses fixed exponential buckets
(bounded memory, no per-sample allocation) while tracking exact ``count``,
``min``, ``max`` and ``sum`` so those never drift from bucketing.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

NS_PER_MS = 1_000_000


def monotonic_ns() -> int:
    """The one clock this module trusts. Never wall-clock time here."""

    return time.monotonic_ns()


def _default_bucket_bounds_ns() -> List[int]:
    """1-2-5 exponential upper bounds from 1 microsecond to 100 seconds."""

    bounds: List[int] = []
    unit = 1_000  # 1 microsecond in ns
    while unit <= 100 * 1_000_000_000:
        for mult in (1, 2, 5):
            bounds.append(unit * mult)
        unit *= 10
    return bounds


@dataclass
class LatencySnapshot:
    name: str
    count: int
    min_ns: Optional[int]
    max_ns: Optional[int]
    sum_ns: int
    p50_ns: Optional[int]
    p95_ns: Optional[int]
    p99_ns: Optional[int]

    @property
    def mean_ns(self) -> Optional[float]:
        return self.sum_ns / self.count if self.count else None

    def as_dict(self) -> Dict[str, Optional[float]]:
        return {
            "name": self.name,
            "count": self.count,
            "min_ns": self.min_ns,
            "max_ns": self.max_ns,
            "mean_ns": self.mean_ns,
            "p50_ns": self.p50_ns,
            "p95_ns": self.p95_ns,
            "p99_ns": self.p99_ns,
        }


class LatencyHistogram:
    """A bounded-memory histogram of nanosecond latencies."""

    def __init__(self, name: str, bucket_bounds_ns: Optional[List[int]] = None) -> None:
        self.name = name
        # Sorted, ascending upper bounds; a final implicit +inf bucket catches
        # anything above the top bound.
        self._bounds: List[int] = sorted(bucket_bounds_ns or _default_bucket_bounds_ns())
        self._counts: List[int] = [0] * (len(self._bounds) + 1)
        self._count = 0
        self._sum = 0
        self._min: Optional[int] = None
        self._max: Optional[int] = None

    def record(self, value_ns: int) -> None:
        # Latencies are non-negative; clock skew can make a *derived* delay
        # negative, so clamp defensively rather than corrupt the histogram.
        if value_ns < 0:
            value_ns = 0
        idx = self._bucket_index(value_ns)
        self._counts[idx] += 1
        self._count += 1
        self._sum += value_ns
        if self._min is None or value_ns < self._min:
            self._min = value_ns
        if self._max is None or value_ns > self._max:
            self._max = value_ns

    def _bucket_index(self, value_ns: int) -> int:
        # Linear scan is fine: the bound list is short (~30 entries) and this
        # keeps behaviour obvious. bisect would work too.
        for i, bound in enumerate(self._bounds):
            if value_ns <= bound:
                return i
        return len(self._bounds)

    def percentile(self, pct: float) -> Optional[int]:
        """Approximate percentile as the upper bound of the containing bucket.

        Returns ``max_ns`` exactly for the top of the distribution so the
        reported percentile never exceeds the observed maximum.
        """

        if self._count == 0:
            return None
        target = pct / 100.0 * self._count
        cumulative = 0
        for i, c in enumerate(self._counts):
            cumulative += c
            if cumulative >= target:
                if i < len(self._bounds):
                    bound = self._bounds[i]
                    # Never report a percentile above the true max.
                    return min(bound, self._max) if self._max is not None else bound
                return self._max
        return self._max

    def snapshot(self) -> LatencySnapshot:
        return LatencySnapshot(
            name=self.name,
            count=self._count,
            min_ns=self._min,
            max_ns=self._max,
            sum_ns=self._sum,
            p50_ns=self.percentile(50),
            p95_ns=self.percentile(95),
            p99_ns=self.percentile(99),
        )

    def reset(self) -> None:
        self._counts = [0] * (len(self._bounds) + 1)
        self._count = 0
        self._sum = 0
        self._min = None
        self._max = None


@dataclass
class LatencyRegistry:
    """A named collection of histograms."""

    _histograms: Dict[str, LatencyHistogram] = field(default_factory=dict)

    def histogram(self, name: str) -> LatencyHistogram:
        hist = self._histograms.get(name)
        if hist is None:
            hist = LatencyHistogram(name)
            self._histograms[name] = hist
        return hist

    def record(self, name: str, value_ns: int) -> None:
        self.histogram(name).record(value_ns)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        """Time a block on the monotonic clock and record it under ``name``."""

        start = monotonic_ns()
        try:
            yield
        finally:
            self.record(name, monotonic_ns() - start)

    def snapshot(self) -> Dict[str, LatencySnapshot]:
        return {name: h.snapshot() for name, h in self._histograms.items()}

    def snapshot_dict(self) -> Dict[str, Dict[str, Optional[float]]]:
        return {name: snap.as_dict() for name, snap in self.snapshot().items()}


def record_envelope_latencies(
    registry: LatencyRegistry,
    envelope,
    processed_monotonic_ns: Optional[int] = None,
) -> None:
    """Record the delay components of an envelope into ``registry``.

    Kept separate from :class:`LatencyHistogram` so the histogram stays free of
    any envelope import (and stays cheap to reason about on the hot path).
    """

    breakdown = envelope.latency_breakdown(processed_monotonic_ns)
    for component, value in breakdown.items():
        if value is not None:
            registry.record(component, value)
