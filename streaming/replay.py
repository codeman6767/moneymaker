"""Durable raw event store and deterministic historical replay.

Two responsibilities live here:

1. :class:`RawEventStore` -- an append-only, durable log of raw envelopes. The
   event bus writes to it *before acknowledging* a message when configured to
   (``persist_before_ack``), which is our write-ahead log: an event is on disk
   before we tell the transport we're done with it.

2. :class:`Replayer` -- feeds stored events back through a processing pipeline
   in a deterministic order. Determinism is the contract: the same stored input
   always yields the same delivery order (and therefore the same resulting
   state), which is what makes replay usable for backtests and incident
   reconstruction.

Ordering key: events are replayed sorted by ``(event_time, stream_key,
sequence, envelope_id)``. Every component is stable and present on every
envelope, so ties break identically on every run regardless of the order the
events were written to disk.
"""

from __future__ import annotations

import os
import threading
from typing import Awaitable, Callable, Iterable, Iterator, List, Optional, Protocol

from .event_envelope import EventEnvelope


class RawEventStore(Protocol):
    def append(self, envelope: EventEnvelope) -> None: ...

    def read_all(self) -> Iterator[EventEnvelope]: ...

    def close(self) -> None: ...


def replay_sort_key(envelope: EventEnvelope):
    """Total, stable ordering over stored envelopes.

    ``sequence`` may be ``None`` for unsequenced streams; sort those ahead of
    numbered ones deterministically by substituting -1.
    """

    return (
        envelope.event_time,
        envelope.stream_key or "",
        envelope.sequence if envelope.sequence is not None else -1,
        envelope.envelope_id,
    )


class InMemoryRawEventStore:
    """Non-durable raw store, useful for tests and replay of an in-memory run."""

    def __init__(self) -> None:
        self._events: List[EventEnvelope] = []
        self._lock = threading.Lock()

    def append(self, envelope: EventEnvelope) -> None:
        with self._lock:
            self._events.append(envelope)

    def read_all(self) -> Iterator[EventEnvelope]:
        with self._lock:
            return iter(list(self._events))

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


class JsonlRawEventStore:
    """Durable append-only raw store: one JSON envelope per line.

    ``append`` flushes and ``fsync``s so a crash immediately after a write (and
    before an ack) still leaves the event on disk -- the write-ahead guarantee.
    """

    def __init__(self, path: str, *, fsync: bool = True) -> None:
        self._path = path
        self._fsync = fsync
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        # Line-buffered append mode; created if absent.
        self._fh = open(path, "a", encoding="utf-8")

    def append(self, envelope: EventEnvelope) -> None:
        line = envelope.to_json()
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())

    def read_all(self) -> Iterator[EventEnvelope]:
        # Read from an independent handle so concurrent appends are unaffected.
        events: List[EventEnvelope] = []
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(EventEnvelope.from_json(line))
        return iter(events)

    def close(self) -> None:
        with self._lock:
            self._fh.close()


# A replay pipeline consumes a single envelope and returns nothing; it should be
# idempotent so that re-running a replay does not double-apply state.
ReplayPipeline = Callable[[EventEnvelope], Awaitable[None]]


class Replayer:
    """Deterministically replays stored events through a pipeline."""

    def __init__(self, store: RawEventStore) -> None:
        self._store = store

    def ordered_events(
        self, subjects: Optional[Iterable[str]] = None
    ) -> List[EventEnvelope]:
        subject_filter = set(subjects) if subjects is not None else None
        events = [
            e
            for e in self._store.read_all()
            if subject_filter is None or e.subject in subject_filter
        ]
        events.sort(key=replay_sort_key)
        return events

    async def replay(
        self,
        pipeline: ReplayPipeline,
        subjects: Optional[Iterable[str]] = None,
    ) -> int:
        """Replay all stored events in deterministic order.

        Returns the number of events replayed.
        """

        count = 0
        for envelope in self.ordered_events(subjects):
            await pipeline(envelope)
            count += 1
        return count
