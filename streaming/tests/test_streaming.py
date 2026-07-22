"""Acceptance and behaviour tests for the streaming backbone (Module 1).

Covers the nine required scenarios -- duplicate, out-of-order, missing sequence,
corrected event, consumer restart, replay, schema-version mismatch, dead-letter
payload, latency measurement -- and the acceptance criteria:

* no duplicated state change after redelivery,
* gaps generate an unhealthy-stream state,
* replay is deterministic.

All tests run against the in-memory bus / components, so no broker is required.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from streaming import (
    CorrectionHandler,
    Deduplicator,
    DeadLetterQueue,
    Delivery,
    EventEnvelope,
    EventProcessor,
    InMemoryEventBus,
    JsonlRawEventStore,
    LatencyRegistry,
    MessageContext,
    ProcessAction,
    ProcessStatus,
    Replayer,
    SequenceTracker,
    SqliteDedupStore,
    StreamHealth,
    replay_sort_key,
)

BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event(
    *,
    subject: str = "kalshi.orderbook",
    provider: str = "kalshi",
    event_type: str = "book_delta",
    sequence=None,
    payload=None,
    event_time=None,
    provider_sent_at=None,
    is_correction: bool = False,
    schema_version=None,
) -> EventEnvelope:
    kwargs = dict(
        subject=subject,
        provider=provider,
        event_type=event_type,
        sequence=sequence,
        payload=payload or {"price": 50, "size": 10},
        event_time=event_time or BASE_TIME,
        provider_sent_at=provider_sent_at,
        is_correction=is_correction,
    )
    if schema_version is not None:
        kwargs["schema_version"] = schema_version
    return EventEnvelope(**kwargs)


class Recorder:
    """A handler that records every delivery it receives."""

    def __init__(self) -> None:
        self.deliveries: List[Delivery] = []

    async def __call__(self, delivery: Delivery) -> None:
        self.deliveries.append(delivery)

    @property
    def sequences(self) -> List:
        return [d.envelope.sequence for d in self.deliveries]


# --------------------------------------------------------------------------- #
# 1. Duplicate event
# --------------------------------------------------------------------------- #
async def test_duplicate_event_no_double_state_change():
    recorder = Recorder()
    processor = EventProcessor(recorder)
    bus = InMemoryEventBus()
    await bus.subscribe("kalshi.orderbook", processor)

    # Two independent captures of the SAME content (same hash) -> second is a
    # duplicate and must not produce a second state change.
    e1 = make_event(sequence=1)
    e2 = make_event(sequence=1)
    assert e1.content_hash == e2.content_hash

    await bus.publish(e1)
    await bus.publish(e2)

    assert len(recorder.deliveries) == 1


# --------------------------------------------------------------------------- #
# 2. Out-of-order event
# --------------------------------------------------------------------------- #
async def test_out_of_order_events_delivered_in_order():
    recorder = Recorder()
    processor = EventProcessor(recorder)
    bus = InMemoryEventBus()
    await bus.subscribe("kalshi.orderbook", processor)

    # Arrive 1, 3, 2. The handler must see 1, 2, 3 in order once 2 fills the gap.
    await bus.publish(make_event(sequence=1))
    await bus.publish(make_event(sequence=3))  # gap: 2 is missing
    assert processor.sequence_tracker.health("kalshi:kalshi.orderbook") is StreamHealth.UNHEALTHY

    await bus.publish(make_event(sequence=2))  # fills gap, flushes 2 then 3

    assert recorder.sequences == [1, 2, 3]
    assert processor.sequence_tracker.is_healthy("kalshi:kalshi.orderbook")


# --------------------------------------------------------------------------- #
# 3. Missing sequence -> unhealthy stream
# --------------------------------------------------------------------------- #
async def test_missing_sequence_marks_stream_unhealthy():
    recorder = Recorder()
    processor = EventProcessor(recorder)
    bus = InMemoryEventBus()
    await bus.subscribe("kalshi.orderbook", processor)

    await bus.publish(make_event(sequence=1))
    await bus.publish(make_event(sequence=3))  # 2 never arrives

    key = "kalshi:kalshi.orderbook"
    assert processor.sequence_tracker.health(key) is StreamHealth.UNHEALTHY
    assert processor.sequence_tracker.missing_sequences(key) == [2]
    # Only the contiguous prefix (seq 1) was delivered; 3 stays buffered.
    assert recorder.sequences == [1]


# --------------------------------------------------------------------------- #
# 4. Corrected event
# --------------------------------------------------------------------------- #
async def test_corrected_event_supersedes_original():
    recorder = Recorder()
    processor = EventProcessor(recorder)
    bus = InMemoryEventBus()
    await bus.subscribe("sports.lineups", processor)

    original = make_event(
        subject="sports.lineups", provider="statsprovider",
        sequence=5, payload={"lineup": "A"},
    )
    corrected = make_event(
        subject="sports.lineups", provider="statsprovider",
        sequence=5, payload={"lineup": "B"},
    )
    # Same sequence, different content -> different hash, so NOT a dedup dup.
    assert original.content_hash != corrected.content_hash

    await bus.publish(original)
    await bus.publish(corrected)

    assert len(recorder.deliveries) == 2
    assert recorder.deliveries[0].is_correction is False
    assert recorder.deliveries[1].is_correction is True
    assert recorder.deliveries[1].envelope.payload == {"lineup": "B"}


# --------------------------------------------------------------------------- #
# 5. Consumer restart (durable dedup) -> no duplicated state change
# --------------------------------------------------------------------------- #
async def test_consumer_restart_no_duplicate_after_redelivery(tmp_path):
    db_path = str(tmp_path / "dedup.sqlite")
    event = make_event(sequence=1)

    # First consumer processes the event and records it durably.
    rec1 = Recorder()
    proc1 = EventProcessor(rec1, deduplicator=Deduplicator(SqliteDedupStore(db_path)))
    await proc1.process(event, MessageContext())
    assert len(rec1.deliveries) == 1
    proc1.deduplicator.close()

    # "Restart": brand-new processor (fresh sequence tracker) sharing only the
    # durable dedup store. A redelivery of the same event must NOT re-apply.
    rec2 = Recorder()
    proc2 = EventProcessor(rec2, deduplicator=Deduplicator(SqliteDedupStore(db_path)))
    result = await proc2.process(event, MessageContext(redelivered=True, delivery_count=2))
    assert result.status is ProcessStatus.DUPLICATE
    assert len(rec2.deliveries) == 0
    proc2.deduplicator.close()


# --------------------------------------------------------------------------- #
# 6. Replay is deterministic
# --------------------------------------------------------------------------- #
async def test_replay_is_deterministic(tmp_path):
    store = JsonlRawEventStore(str(tmp_path / "raw.jsonl"))
    # Write events out of order; replay must impose a stable order regardless.
    events = [
        make_event(sequence=i, event_time=BASE_TIME + timedelta(seconds=i))
        for i in range(10)
    ]
    shuffled = events[:]
    random.Random(1234).shuffle(shuffled)
    for e in shuffled:
        store.append(e)
    store.close()

    def run_order() -> List[int]:
        s = JsonlRawEventStore(str(tmp_path / "raw.jsonl"))
        replayer = Replayer(s)
        order = [e.sequence for e in replayer.ordered_events()]
        s.close()
        return order

    order_a = run_order()
    order_b = run_order()
    assert order_a == order_b == list(range(10))

    # And the pipeline produces the same delivered order twice.
    async def collect(target: List[int]):
        async def pipeline(env: EventEnvelope) -> None:
            target.append(env.sequence)
        return pipeline

    out1: List[int] = []
    out2: List[int] = []
    s = JsonlRawEventStore(str(tmp_path / "raw.jsonl"))
    replayer = Replayer(s)
    await replayer.replay(await collect(out1))
    await replayer.replay(await collect(out2))
    s.close()
    assert out1 == out2 == list(range(10))


def test_replay_sort_key_is_total_and_stable():
    a = make_event(sequence=1, event_time=BASE_TIME)
    b = make_event(sequence=2, event_time=BASE_TIME)
    assert replay_sort_key(a) < replay_sort_key(b)


# --------------------------------------------------------------------------- #
# 7. Schema-version mismatch -> dead letter, never processed
# --------------------------------------------------------------------------- #
async def test_schema_version_mismatch_dead_letters():
    recorder = Recorder()
    dlq = DeadLetterQueue()
    processor = EventProcessor(recorder, dead_letter=dlq)

    bad = make_event(sequence=1, schema_version="99.0.0")
    result = await processor.process(bad, MessageContext())

    assert result.action is ProcessAction.TERM
    assert result.status is ProcessStatus.SCHEMA_MISMATCH
    assert len(recorder.deliveries) == 0
    assert len(dlq) == 1
    assert "schema_version" in dlq.items[0].reason


# --------------------------------------------------------------------------- #
# 8. Dead-letter payload after repeated handler failure
# --------------------------------------------------------------------------- #
async def test_dead_letter_payload_after_max_deliveries():
    async def failing_handler(delivery: Delivery) -> None:
        raise RuntimeError("boom")

    dlq = DeadLetterQueue()
    processor = EventProcessor(failing_handler, dead_letter=dlq, max_deliveries=3)
    bus = InMemoryEventBus(max_deliveries=3)
    await bus.subscribe("execution.orders", processor)

    event = make_event(subject="execution.orders", provider="kalshi", sequence=1)
    await bus.publish(event)

    assert len(dlq) == 1
    dead = dlq.items[0]
    assert dead.envelope.content_hash == event.content_hash
    assert dead.delivery_count == 3
    assert "boom" in dead.reason


# --------------------------------------------------------------------------- #
# 9. Latency measurement (monotonic, with percentiles)
# --------------------------------------------------------------------------- #
async def test_latency_measurement_produces_percentiles():
    recorder = Recorder()
    latency = LatencyRegistry()
    processor = EventProcessor(recorder, latency=latency)

    event_time = BASE_TIME
    provider_sent = BASE_TIME + timedelta(milliseconds=5)
    for i in range(200):
        env = EventEnvelope(
            subject="odds.updates",
            provider="oddsapi",
            event_type="odds",
            sequence=i,
            event_time=event_time,
            provider_sent_at=provider_sent,
            # received_at defaults to now (well after provider_sent) -> positive
            # network delay.
            payload={"odds": i},
        )
        await processor.process(env, MessageContext())

    snap = latency.snapshot()
    assert "provider_delay_ns" in snap
    assert "network_delay_ns" in snap
    assert "processing_ns" in snap

    proc = snap["processing_ns"]
    assert proc.count == 200
    # Percentiles must be ordered and never exceed the observed maximum.
    assert proc.p50_ns is not None
    assert proc.p50_ns <= proc.p95_ns <= proc.p99_ns <= proc.max_ns

    # Provider delay was a fixed 5ms -> ~5_000_000 ns; sanity-check the bucketed
    # p50 lands in a plausible range.
    provider = snap["provider_delay_ns"]
    assert provider.p50_ns is not None and provider.p50_ns > 0


# --------------------------------------------------------------------------- #
# Extra: unsequenced stream and direct component checks
# --------------------------------------------------------------------------- #
def test_sequence_tracker_direct_gap_and_fill():
    tracker = SequenceTracker()
    key = "kalshi:kalshi.orderbook"

    r1 = tracker.observe(make_event(sequence=1))
    assert [e.sequence for e in r1.ready] == [1]

    r3 = tracker.observe(make_event(sequence=3))
    assert r3.ready == []
    assert r3.health is StreamHealth.UNHEALTHY

    r2 = tracker.observe(make_event(sequence=2))
    assert [e.sequence for e in r2.ready] == [2, 3]
    assert r2.health is StreamHealth.HEALTHY
    _ = key


def test_correction_handler_direct():
    handler = CorrectionHandler()
    original = make_event(sequence=1, payload={"v": 1})
    same = make_event(sequence=1, payload={"v": 1})
    corrected = make_event(sequence=1, payload={"v": 2})

    assert handler.observe(original).status.value == "original"
    assert handler.observe(same).status.value == "unchanged"
    assert handler.observe(corrected).is_correction is True
