"""Throughput and latency benchmarks for live-state updates.

Per ``CLAUDE.md``: fast paths report latency histograms with p50/p95/p99/max,
and all latency measurement uses monotonic clocks. This module reuses the
streaming :class:`LatencyRegistry` so the numbers here are directly comparable
to the transport-layer measurements.

Run directly:

    python -m state.benchmark
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from streaming.event_envelope import EventEnvelope
from streaming.latency import LatencyRegistry, monotonic_ns

from .orderbook import OrderBookState

BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class BenchmarkResult:
    name: str
    events: int
    wall_ns: int
    p50_ns: int
    p95_ns: int
    p99_ns: int
    max_ns: int

    @property
    def events_per_second(self) -> float:
        return self.events / (self.wall_ns / 1_000_000_000) if self.wall_ns else 0.0

    def __str__(self) -> str:
        return (
            f"{self.name}: {self.events} events, "
            f"{self.events_per_second:,.0f} ev/s | "
            f"p50={self.p50_ns}ns p95={self.p95_ns}ns "
            f"p99={self.p99_ns}ns max={self.max_ns}ns"
        )


def _bench(name: str, events: int, make_event: Callable[[int], EventEnvelope], apply) -> BenchmarkResult:
    registry = LatencyRegistry()
    hist = registry.histogram(name)
    # Pre-build events so we time apply(), not construction.
    prepared = [make_event(i) for i in range(events)]
    wall_start = monotonic_ns()
    for env in prepared:
        t0 = monotonic_ns()
        apply(env)
        hist.record(monotonic_ns() - t0)
    wall = monotonic_ns() - wall_start
    snap = hist.snapshot()
    return BenchmarkResult(
        name=name,
        events=events,
        wall_ns=wall,
        p50_ns=snap.p50_ns or 0,
        p95_ns=snap.p95_ns or 0,
        p99_ns=snap.p99_ns or 0,
        max_ns=snap.max_ns or 0,
    )


def benchmark_orderbook_deltas(events: int = 100_000) -> BenchmarkResult:
    book = OrderBookState("KXTEST")
    # Seed with a snapshot so deltas are trusted.
    book.apply(
        EventEnvelope.create(
            subject="kalshi.orderbook",
            provider="kalshi",
            event_type="snapshot",
            event_time=BASE_TIME,
            sequence=0,
            payload={
                "market": "KXTEST",
                "yes": [[40, 100], [41, 200]],
                "no": [[55, 150], [56, 250]],
            },
        )
    )

    def make_event(i: int) -> EventEnvelope:
        price = 30 + (i % 60)  # stays within 1..99
        side = "yes" if i % 2 == 0 else "no"
        return EventEnvelope.create(
            subject="kalshi.orderbook",
            provider="kalshi",
            event_type="delta",
            event_time=BASE_TIME,
            sequence=i + 1,
            payload={"side": side, "price": price, "quantity": (i % 10) + 1},
        )

    return _bench("orderbook_delta_apply", events, make_event, book.apply)


def run_benchmarks() -> list[BenchmarkResult]:
    results = [benchmark_orderbook_deltas()]
    for r in results:
        print(r)
    return results


if __name__ == "__main__":
    run_benchmarks()
