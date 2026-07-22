"""Per-decision latency trace.

Every evaluation records a complete trace: the four delay components required by
``CLAUDE.md`` (provider / network / internal processing / -- exchange is added
by the execution service) plus a per-stage breakdown of internal processing.
All measurement uses the monotonic clock. Per-decision traces roll up into a
:class:`~streaming.latency.LatencyRegistry` so p50/p95/p99 are available across
decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from streaming.latency import monotonic_ns


@dataclass
class LatencyTrace:
    """Accumulates stage durations for a single decision."""

    event_id: str
    #: provider event time -> our receipt, in ns (provider + network combined).
    provider_lag_ns: Optional[int] = None
    start_ns: int = field(default_factory=monotonic_ns)
    stages: List[Tuple[str, int]] = field(default_factory=list)
    total_ns: Optional[int] = None
    _last_ns: int = field(default=0)

    def __post_init__(self) -> None:
        self._last_ns = self.start_ns

    def mark(self, stage: str) -> None:
        """Record the elapsed time since the previous mark as ``stage``."""

        now = monotonic_ns()
        self.stages.append((stage, now - self._last_ns))
        self._last_ns = now

    def finish(self) -> int:
        self.total_ns = monotonic_ns() - self.start_ns
        return self.total_ns

    def as_dict(self) -> Dict[str, object]:
        return {
            "event_id": self.event_id,
            "provider_lag_ns": self.provider_lag_ns,
            "internal_total_ns": self.total_ns,
            "stages": {name: dur for name, dur in self.stages},
        }
