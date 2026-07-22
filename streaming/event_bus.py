"""Transport-agnostic event bus, idempotent processor and dead-letter handling.

The bus interface (:class:`EventBus`) hides the transport so the same processing
pipeline runs over NATS JetStream in production and over an in-memory bus in
tests and replay. The heavy lifting lives in :class:`EventProcessor`, which
wires the transport-neutral components together:

    schema check -> WAL persist -> dedup -> correction -> sequence -> handler

and turns handler failures into retries and, past a limit, dead letters.

Delivery contract (how ``process`` communicates back to a transport):

* ``ProcessAction.ACK``  -- done; do not redeliver.
* ``ProcessAction.RETRY`` -- transient failure; redeliver later (until the
  transport's max-deliveries, after which it becomes a dead letter).
* ``ProcessAction.TERM`` -- permanently unprocessable; already dead-lettered,
  never redeliver.

Ack ordering: the durable side effects that make redelivery safe -- the WAL
append and the dedup ``mark_processed`` -- happen *before* we return ACK, i.e.
before the transport is told to drop the message. A crash after the mark but
before the ack simply causes a redelivery that dedup then absorbs.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, List, Optional

from .correction_handler import CorrectionHandler, CorrectionStatus
from .deduplicator import Deduplicator
from .event_envelope import SUPPORTED_SCHEMA_VERSIONS, EventEnvelope
from .latency import LatencyRegistry, monotonic_ns, record_envelope_latencies
from .replay import RawEventStore
from .sequence_tracker import SequenceStatus, SequenceTracker, StreamHealth

# Canonical subjects carried by the backbone. Kept here so producers,
# consumers, and stream configuration all agree on one spelling.
SUBJECTS: tuple[str, ...] = (
    "sports.mlb.events",
    "sports.nba.events",
    "sports.injuries",
    "sports.lineups",
    "odds.updates",
    "kalshi.orderbook",
    "kalshi.trades",
    "kalshi.market_status",
    "execution.orders",
    "execution.fills",
)


class ProcessAction(str, Enum):
    ACK = "ack"
    RETRY = "retry"
    TERM = "term"


class ProcessStatus(str, Enum):
    DELIVERED = "delivered"
    DUPLICATE = "duplicate"
    BUFFERED_GAP = "buffered_gap"
    CORRECTION = "correction"
    SCHEMA_MISMATCH = "schema_mismatch"
    DEAD_LETTER = "dead_letter"
    RETRY = "retry"


@dataclass
class MessageContext:
    """Transport-level metadata handed to the processor for one delivery."""

    redelivered: bool = False
    delivery_count: int = 1
    stream_sequence: Optional[int] = None


@dataclass
class Delivery:
    """Handler-level metadata. The handler never acks; the processor does."""

    envelope: EventEnvelope
    is_correction: bool = False
    is_replay: bool = False
    sequence_status: Optional[SequenceStatus] = None
    stream_health: StreamHealth = StreamHealth.HEALTHY
    redelivered: bool = False


# A user handler applies the (idempotent) state change for one delivered event.
EventHandler = Callable[[Delivery], Awaitable[None]]


@dataclass
class ProcessResult:
    action: ProcessAction
    status: ProcessStatus
    delivered: int = 0
    reason: Optional[str] = None


@dataclass
class DeadLetter:
    envelope: EventEnvelope
    reason: str
    delivery_count: int


class DeadLetterQueue:
    """Collects events that could not be processed.

    Durable persistence is delegated to an optional :class:`RawEventStore`; the
    in-memory list is always kept so callers/tests can inspect what failed.
    """

    def __init__(self, store: Optional[RawEventStore] = None) -> None:
        self._store = store
        self._items: List[DeadLetter] = []

    def put(self, envelope: EventEnvelope, reason: str, delivery_count: int) -> None:
        self._items.append(DeadLetter(envelope, reason, delivery_count))
        if self._store is not None:
            self._store.append(envelope)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> List[DeadLetter]:
        return list(self._items)


class EventProcessor:
    """Idempotent, gap-aware, dead-letter-capable processing pipeline."""

    def __init__(
        self,
        handler: EventHandler,
        *,
        deduplicator: Optional[Deduplicator] = None,
        sequence_tracker: Optional[SequenceTracker] = None,
        correction_handler: Optional[CorrectionHandler] = None,
        latency: Optional[LatencyRegistry] = None,
        raw_store: Optional[RawEventStore] = None,
        dead_letter: Optional[DeadLetterQueue] = None,
        persist_before_ack: bool = False,
        max_deliveries: int = 5,
    ) -> None:
        self._handler = handler
        self.deduplicator = deduplicator or Deduplicator()
        self.sequence_tracker = sequence_tracker or SequenceTracker()
        self.correction_handler = correction_handler or CorrectionHandler()
        self.latency = latency or LatencyRegistry()
        self.raw_store = raw_store
        # Explicit None check: DeadLetterQueue defines __len__, so an empty
        # queue is falsy -- ``dead_letter or ...`` would wrongly discard it.
        self.dead_letter = dead_letter if dead_letter is not None else DeadLetterQueue()
        self.persist_before_ack = persist_before_ack
        self.max_deliveries = max_deliveries

    async def process(self, envelope: EventEnvelope, ctx: MessageContext) -> ProcessResult:
        start_ns = monotonic_ns()

        # 1. Schema gate. An unknown schema version is never processed; it goes
        #    straight to the dead-letter path so a bad producer can't corrupt
        #    state or wedge the stream.
        if envelope.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            reason = f"unsupported schema_version {envelope.schema_version!r}"
            self.dead_letter.put(envelope, reason, ctx.delivery_count)
            return ProcessResult(ProcessAction.TERM, ProcessStatus.SCHEMA_MISMATCH, reason=reason)

        # 2. Write-ahead: get the raw event on durable storage before we take
        #    any action that could lead to an ack.
        if self.persist_before_ack and self.raw_store is not None:
            self.raw_store.append(envelope)

        # 3. Dedup. Idempotency guard for at-least-once redelivery and
        #    re-capture. A correction has a different content hash, so it is not
        #    suppressed here.
        if self.deduplicator.is_duplicate(envelope):
            record_envelope_latencies(self.latency, envelope, start_ns)
            return ProcessResult(ProcessAction.ACK, ProcessStatus.DUPLICATE)

        # 3b. Transport redelivery of an event that was NOT yet processed
        #     successfully (i.e. a prior attempt naked/failed). Ordering and
        #     correction status were already classified on the first delivery,
        #     and the sequence tracker would now wrongly flag this as a
        #     duplicate sequence -- so re-attempt the handler directly. Dedup
        #     still guarantees the state change lands at most once.
        if ctx.redelivered:
            result = await self._deliver(
                [envelope],
                ctx,
                is_correction=envelope.is_correction,
                sequence_status=None,
                stream_health=self.sequence_tracker.health(envelope.stream_key or ""),
                start_ns=start_ns,
            )
            return result or ProcessResult(
                ProcessAction.ACK, ProcessStatus.DELIVERED, delivered=1
            )

        # 4. Correction. Same sequence, different content supersedes the prior
        #    version and is delivered as a replace.
        correction = self.correction_handler.observe(envelope)
        if correction.is_correction:
            result = await self._deliver(
                [envelope],
                ctx,
                is_correction=True,
                sequence_status=None,
                stream_health=self.sequence_tracker.health(envelope.stream_key or ""),
                start_ns=start_ns,
            )
            return result or ProcessResult(
                ProcessAction.ACK, ProcessStatus.CORRECTION, delivered=1
            )

        # 5. Sequence validation and in-order buffering.
        observed = self.sequence_tracker.observe(envelope)

        if observed.status is SequenceStatus.DUPLICATE:
            record_envelope_latencies(self.latency, envelope, start_ns)
            return ProcessResult(ProcessAction.ACK, ProcessStatus.DUPLICATE)

        if observed.status is SequenceStatus.GAP:
            # The stream is now unhealthy. We ack (the buffered event is held in
            # the tracker for later flush); the unhealthy state is what stops
            # downstream trading, per CLAUDE.md.
            record_envelope_latencies(self.latency, envelope, start_ns)
            return ProcessResult(
                ProcessAction.ACK,
                ProcessStatus.BUFFERED_GAP,
                reason=f"gap; missing={observed.missing}",
            )

        # IN_ORDER / OUT_OF_ORDER / UNSEQUENCED: deliver the ready run in order.
        result = await self._deliver(
            observed.ready,
            ctx,
            is_correction=False,
            sequence_status=observed.status,
            stream_health=observed.health,
            start_ns=start_ns,
        )
        return result or ProcessResult(
            ProcessAction.ACK, ProcessStatus.DELIVERED, delivered=len(observed.ready)
        )

    async def _deliver(
        self,
        envelopes: List[EventEnvelope],
        ctx: MessageContext,
        *,
        is_correction: bool,
        sequence_status: Optional[SequenceStatus],
        stream_health: StreamHealth,
        start_ns: int,
    ) -> Optional[ProcessResult]:
        """Deliver a run of ready envelopes to the handler in order.

        Returns a ``ProcessResult`` only on a failure path (retry/dead-letter);
        ``None`` on success so the caller applies its own success result.
        """

        for env in envelopes:
            delivery = Delivery(
                envelope=env,
                is_correction=is_correction,
                sequence_status=sequence_status,
                stream_health=stream_health,
                redelivered=ctx.redelivered,
            )
            try:
                await self._handler(delivery)
            except Exception as exc:  # noqa: BLE001 - transport decides retry vs term
                if ctx.delivery_count >= self.max_deliveries:
                    reason = f"handler failed after {ctx.delivery_count} deliveries: {exc}"
                    self.dead_letter.put(env, reason, ctx.delivery_count)
                    return ProcessResult(ProcessAction.TERM, ProcessStatus.DEAD_LETTER, reason=reason)
                return ProcessResult(
                    ProcessAction.RETRY, ProcessStatus.RETRY, reason=str(exc)
                )

            # Success: mark durable-idempotent *before* the ack the caller will
            # issue, so a redelivery is safely recognised as a duplicate.
            self.deduplicator.mark_processed(env)
            record_envelope_latencies(self.latency, env, monotonic_ns())

        # Record end-to-end internal processing time for this delivery.
        self.latency.record("processing_ns", monotonic_ns() - start_ns)
        return None


class EventBus(abc.ABC):
    """Abstract transport for publishing and subscribing to envelopes."""

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def publish(self, envelope: EventEnvelope) -> None: ...

    @abc.abstractmethod
    async def subscribe(self, subject: str, processor: EventProcessor) -> None: ...


def subject_matches(pattern: str, subject: str) -> bool:
    """NATS-style subject matching supporting ``*`` and ``>`` wildcards."""

    p_tokens = pattern.split(".")
    s_tokens = subject.split(".")
    for i, p in enumerate(p_tokens):
        if p == ">":
            return True
        if i >= len(s_tokens):
            return False
        if p == "*":
            continue
        if p != s_tokens[i]:
            return False
    return len(p_tokens) == len(s_tokens)


class InMemoryEventBus(EventBus):
    """In-process bus that faithfully models at-least-once redelivery.

    On a ``RETRY`` result it redelivers the same envelope with an incremented
    delivery count (and ``redelivered=True``) until either the processor acks or
    the delivery count hits ``max_deliveries`` and the processor terms it. This
    lets tests exercise redelivery, restart and dead-letter paths without a
    broker.
    """

    def __init__(self, max_deliveries: int = 5) -> None:
        self._subscriptions: List[tuple[str, EventProcessor]] = []
        self._max_deliveries = max_deliveries
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def subscribe(self, subject: str, processor: EventProcessor) -> None:
        self._subscriptions.append((subject, processor))

    async def publish(self, envelope: EventEnvelope) -> None:
        for pattern, processor in self._subscriptions:
            if subject_matches(pattern, envelope.subject):
                await self._deliver_with_retries(processor, envelope)

    async def deliver(
        self, processor: EventProcessor, envelope: EventEnvelope, *, redelivered: bool
    ) -> ProcessResult:
        """Deliver once with an explicit redelivered flag (test hook)."""

        ctx = MessageContext(
            redelivered=redelivered, delivery_count=2 if redelivered else 1
        )
        return await processor.process(envelope, ctx)

    async def _deliver_with_retries(
        self, processor: EventProcessor, envelope: EventEnvelope
    ) -> ProcessResult:
        delivery_count = 0
        while True:
            delivery_count += 1
            ctx = MessageContext(
                redelivered=delivery_count > 1,
                delivery_count=delivery_count,
            )
            result = await processor.process(envelope, ctx)
            if result.action is ProcessAction.RETRY and delivery_count < self._max_deliveries:
                continue
            return result
