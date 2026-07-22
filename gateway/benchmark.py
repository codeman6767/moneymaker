"""Stage-by-stage latency benchmarking for the execution gateway.

Records the nine required stages plus the end-to-end event-to-acknowledgement
latency, all on the monotonic clock. The report exposes p50/p90/p95/p99/max and
sample counts per stage, plus failures / reconnects / rate-limit events.

The sub-second claim is gated: :meth:`GatewayReport.claims_sub_second` only
returns True when the end-to-end latency has a statistically meaningful sample
and its p99 is under the threshold -- so the system is never described as
sub-second on the strength of a handful of lucky samples.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, Optional

from streaming.latency import LatencyRegistry, monotonic_ns


class Stage(str, enum.Enum):
    WS_RECEIPT = "ws_event_receipt"
    PARSE = "parse"
    ORDER_BOOK_UPDATE = "order_book_update"
    INFERENCE = "inference"
    DECISION = "decision"
    SIGNING = "signing"
    NETWORK_SUBMISSION = "network_submission"
    EXCHANGE_ACK = "exchange_acknowledgement"
    FILL_NOTIFICATION = "fill_notification"


E2E = "event_to_acknowledgement"


class StageTimer:
    """Threads through a single event, recording per-stage deltas."""

    def __init__(self, benchmark: "LatencyBenchmark", receipt_ns: Optional[int] = None) -> None:
        self._bench = benchmark
        self.receipt_ns = receipt_ns if receipt_ns is not None else monotonic_ns()
        self._last = self.receipt_ns

    def mark(self, stage: Stage, *, now_ns: Optional[int] = None) -> int:
        now = now_ns if now_ns is not None else monotonic_ns()
        self._bench.record(stage, now - self._last)
        self._last = now
        return now

    def record_stage(self, stage: Stage, duration_ns: int) -> None:
        self._bench.record(stage, duration_ns)

    def record_e2e(self, ack_ns: Optional[int] = None) -> int:
        now = ack_ns if ack_ns is not None else monotonic_ns()
        e2e = now - self.receipt_ns
        self._bench.record_e2e(e2e)
        return e2e


def _stats(hist) -> Dict[str, Optional[float]]:
    snap = hist.snapshot()
    return {
        "count": snap.count,
        "p50": snap.p50_ns,
        "p90": hist.percentile(90),
        "p95": snap.p95_ns,
        "p99": snap.p99_ns,
        "max": snap.max_ns,
    }


@dataclass
class GatewayReport:
    stages: Dict[str, Dict[str, Optional[float]]]
    e2e: Dict[str, Optional[float]]
    failures: int
    reconnects: int
    rate_limit_events: int
    sub_second_threshold_ns: int
    min_samples: int

    def claims_sub_second(self) -> bool:
        count = self.e2e.get("count") or 0
        p99 = self.e2e.get("p99")
        if count < self.min_samples or p99 is None:
            return False
        return p99 < self.sub_second_threshold_ns

    def latency_claim(self) -> str:
        if self.claims_sub_second():
            return (f"sub-second event-to-ack confirmed: p99="
                    f"{self.e2e['p99']}ns over {self.e2e['count']} samples")
        count = self.e2e.get("count") or 0
        if count < self.min_samples:
            return (f"insufficient sample ({count}<{self.min_samples}); "
                    f"NO latency claim made")
        return (f"NOT sub-second: p99={self.e2e.get('p99')}ns "
                f">= {self.sub_second_threshold_ns}ns")


@dataclass
class LatencyBenchmark:
    registry: LatencyRegistry = field(default_factory=LatencyRegistry)
    failures: int = 0
    reconnects: int = 0
    rate_limit_events: int = 0
    sub_second_threshold_ns: int = 1_000_000_000
    min_samples: int = 100

    def record(self, stage: Stage, duration_ns: int) -> None:
        self.registry.record(stage.value, max(0, duration_ns))

    def record_e2e(self, duration_ns: int) -> None:
        self.registry.record(E2E, max(0, duration_ns))

    def note_failure(self) -> None:
        self.failures += 1

    def note_reconnect(self) -> None:
        self.reconnects += 1

    def set_rate_limit_events(self, n: int) -> None:
        self.rate_limit_events = n

    def timer(self, receipt_ns: Optional[int] = None) -> StageTimer:
        return StageTimer(self, receipt_ns)

    def report(self) -> GatewayReport:
        snap = self.registry.snapshot()
        stages = {
            s.value: _stats(self.registry.histogram(s.value))
            for s in Stage
            if s.value in snap
        }
        e2e_hist = self.registry.histogram(E2E)
        e2e = _stats(e2e_hist)
        return GatewayReport(
            stages=stages,
            e2e=e2e,
            failures=self.failures,
            reconnects=self.reconnects,
            rate_limit_events=self.rate_limit_events,
            sub_second_threshold_ns=self.sub_second_threshold_ns,
            min_samples=self.min_samples,
        )
