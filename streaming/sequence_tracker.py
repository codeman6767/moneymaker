"""Per-stream sequence validation with in-order buffering.

Provider streams (order books, game state, odds) carry monotonic sequence
numbers per logical stream. This tracker classifies each arriving sequence and
buffers out-of-order arrivals so downstream handlers only ever see a contiguous,
in-order sequence.

Crucially, it exposes a *health* state per stream. Per ``CLAUDE.md`` we never
trade from an order book or game state with an unresolved sequence gap, so a gap
must be observable, not silently swallowed. A stream is ``UNHEALTHY`` from the
moment a gap is detected until every missing sequence in it has arrived.

Restart note: this tracker is in-memory. Buffered-but-undelivered events are not
crash-durable here; recovery of those events is the job of the durable raw store
plus deterministic replay (see ``replay.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .event_envelope import EventEnvelope


class SequenceStatus(str, Enum):
    #: Arrived exactly at the expected next position (possibly flushing buffer).
    IN_ORDER = "in_order"
    #: Ahead of the expected position -- one or more sequences are missing.
    GAP = "gap"
    #: Behind the expected position, filling a previously recorded gap.
    OUT_OF_ORDER = "out_of_order"
    #: Already seen this sequence number (with identical content class).
    DUPLICATE = "duplicate"
    #: The stream has no sequence numbers; ordering is not tracked.
    UNSEQUENCED = "unsequenced"


class StreamHealth(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass
class ObserveResult:
    status: SequenceStatus
    health: StreamHealth
    #: Envelopes that are now safe to deliver, in strict sequence order. May be
    #: empty (gap/duplicate) or contain several (a late arrival that flushes a
    #: run of buffered events).
    ready: List[EventEnvelope] = field(default_factory=list)
    #: Missing sequence numbers still outstanding for the stream.
    missing: List[int] = field(default_factory=list)


@dataclass
class _StreamState:
    # Next sequence number we expect to deliver (the contiguous low watermark).
    expected_next: Optional[int] = None
    # Sequences we have observed but not yet delivered, buffered by number.
    buffered: Dict[int, EventEnvelope] = field(default_factory=dict)
    # Sequences we know are missing (detected via a gap) and awaiting arrival.
    missing: set[int] = field(default_factory=set)
    # Every sequence number ever observed, for duplicate detection.
    seen: set[int] = field(default_factory=set)


class SequenceTracker:
    """Classifies sequences per ``stream_key`` and enforces in-order delivery."""

    def __init__(self) -> None:
        self._streams: Dict[str, _StreamState] = {}

    def _state(self, stream_key: str) -> _StreamState:
        st = self._streams.get(stream_key)
        if st is None:
            st = _StreamState()
            self._streams[stream_key] = st
        return st

    def observe(self, envelope: EventEnvelope) -> ObserveResult:
        seq = envelope.sequence
        key = envelope.stream_key or f"{envelope.provider}:{envelope.subject}"

        if seq is None:
            # Unsequenced streams are delivered as they arrive; ordering and
            # gaps are meaningless here.
            return ObserveResult(
                status=SequenceStatus.UNSEQUENCED,
                health=StreamHealth.HEALTHY,
                ready=[envelope],
            )

        st = self._state(key)

        # First event on this stream anchors the expected position.
        if st.expected_next is None:
            st.expected_next = seq

        # Duplicate: we have already accounted for this sequence.
        if seq in st.seen or seq in st.buffered:
            return ObserveResult(
                status=SequenceStatus.DUPLICATE,
                health=self._health(st),
                missing=sorted(st.missing),
            )

        if seq < st.expected_next:
            # Behind the watermark. Either it fills a known gap, or it is a
            # stale re-arrival of something already delivered.
            if seq in st.missing:
                st.missing.discard(seq)
                st.seen.add(seq)
                # A late fill below the watermark is already "in the past" for
                # delivery ordering; deliver it immediately as out-of-order.
                return ObserveResult(
                    status=SequenceStatus.OUT_OF_ORDER,
                    health=self._health(st),
                    ready=[envelope],
                    missing=sorted(st.missing),
                )
            # Below watermark and not missing -> already delivered; treat as a
            # duplicate to avoid re-applying a state change.
            return ObserveResult(
                status=SequenceStatus.DUPLICATE,
                health=self._health(st),
                missing=sorted(st.missing),
            )

        if seq == st.expected_next:
            st.seen.add(seq)
            st.missing.discard(seq)
            ready = [envelope]
            st.expected_next = seq + 1
            # Flush any buffered contiguous run that this arrival unblocks.
            self._flush_contiguous(st, ready)
            return ObserveResult(
                status=SequenceStatus.IN_ORDER,
                health=self._health(st),
                ready=ready,
                missing=sorted(st.missing),
            )

        # seq > expected_next: a gap. Buffer the arrival and record the hole.
        for missing_seq in range(st.expected_next, seq):
            if missing_seq not in st.seen:
                st.missing.add(missing_seq)
        st.buffered[seq] = envelope
        st.seen.add(seq)
        return ObserveResult(
            status=SequenceStatus.GAP,
            health=self._health(st),
            missing=sorted(st.missing),
        )

    def _flush_contiguous(self, st: _StreamState, ready: List[EventEnvelope]) -> None:
        while st.expected_next in st.buffered:
            nxt = st.buffered.pop(st.expected_next)
            st.missing.discard(st.expected_next)
            ready.append(nxt)
            st.expected_next += 1

    @staticmethod
    def _health(st: _StreamState) -> StreamHealth:
        return StreamHealth.UNHEALTHY if st.missing else StreamHealth.HEALTHY

    # -- Introspection --------------------------------------------------------
    def health(self, stream_key: str) -> StreamHealth:
        st = self._streams.get(stream_key)
        return StreamHealth.HEALTHY if st is None else self._health(st)

    def is_healthy(self, stream_key: str) -> bool:
        return self.health(stream_key) is StreamHealth.HEALTHY

    def missing_sequences(self, stream_key: str) -> List[int]:
        st = self._streams.get(stream_key)
        return sorted(st.missing) if st else []

    def expected_next(self, stream_key: str) -> Optional[int]:
        st = self._streams.get(stream_key)
        return st.expected_next if st else None
