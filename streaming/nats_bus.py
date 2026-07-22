"""NATS JetStream implementation of :class:`EventBus`.

JetStream is the chosen transport for the first implementation. It gives us,
without adding Kafka or Redis (rule 13 in ``CLAUDE.md``):

* durable, replicated storage of raw events (file storage) -- so the broker
  itself is a persisted log;
* at-least-once delivery with explicit ack, redelivery on ``nak``, and a
  configurable ``max_deliver`` after which a message is terminated;
* per-consumer redelivery counts (``num_delivered``) that map cleanly onto the
  processor's retry/dead-letter contract.

``nats`` (the ``nats-py`` package) is imported lazily so this module -- and the
rest of the backbone -- imports fine in environments where the client isn't
installed (e.g. unit tests that use the in-memory bus).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .event_bus import EventBus, EventProcessor, MessageContext, ProcessAction
from .event_envelope import EventEnvelope

if TYPE_CHECKING:
    # Type-only imports: `nats-py` stays an optional runtime dependency, so
    # these names must never be imported at run time. They give the client and
    # JetStream attributes real declared types instead of a bare `None`.
    from nats.aio.client import Client as NatsClient
    from nats.aio.msg import Msg
    from nats.js import JetStreamContext

DEFAULT_STREAM_NAME = "MONEYMAKER"

# The dead-letter subject. The processor also keeps its own DLQ; publishing here
# gives operators a durable, inspectable JetStream stream of poison messages.
DEAD_LETTER_SUBJECT = "dlq.events"


class NatsEventBus(EventBus):
    """Publishes and consumes envelopes over NATS JetStream."""

    def __init__(
        self,
        servers: str | List[str] = "nats://localhost:4222",
        *,
        stream_name: str = DEFAULT_STREAM_NAME,
        subjects: Optional[List[str]] = None,
        max_deliveries: int = 5,
        ack_wait_seconds: float = 30.0,
    ) -> None:
        self._servers = servers
        self._stream_name = stream_name
        # A stream captures the canonical subjects plus the dead-letter subject.
        from .event_bus import SUBJECTS

        self._subjects = list(subjects or SUBJECTS) + [DEAD_LETTER_SUBJECT]
        self._max_deliveries = max_deliveries
        self._ack_wait_seconds = ack_wait_seconds
        self._nc: Optional["NatsClient"] = None
        self._js: Optional["JetStreamContext"] = None

    # -- Connection state -----------------------------------------------------
    def _require_js(self, operation: str) -> "JetStreamContext":
        """Return the JetStream context, or explain that connect() is required.

        Every JetStream call goes through here so an un-connected bus fails with
        a clear error instead of an ``AttributeError`` on ``None``.
        """

        if self._js is None:
            raise RuntimeError(
                f"NatsEventBus.{operation} requires an active connection; "
                "call connect() first"
            )
        return self._js

    async def connect(self) -> None:
        # Lazy import: only require nats-py when actually connecting.
        import nats

        nc = await nats.connect(self._servers)
        if nc is None:
            raise RuntimeError(f"NATS connection to {self._servers!r} returned no client")
        js = nc.jetstream()
        if js is None:
            raise RuntimeError("NATS client returned no JetStream context")
        self._nc = nc
        self._js = js
        await self._ensure_stream()

    async def _ensure_stream(self) -> None:
        from nats.js.api import RetentionPolicy, StorageType, StreamConfig
        from nats.js.errors import NotFoundError

        js = self._require_js("_ensure_stream")
        config = StreamConfig(
            name=self._stream_name,
            subjects=self._subjects,
            # File storage => raw events are durable in the broker itself.
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
        )
        try:
            await js.update_stream(config=config)
        except NotFoundError:
            await js.add_stream(config=config)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._js = None

    async def publish(self, envelope: EventEnvelope) -> None:
        js = self._require_js("publish")
        # Msg-Id enables JetStream's own publish-side dedup window as a second
        # line of defence; content_hash is stable across identical content.
        headers = {"Nats-Msg-Id": envelope.content_hash or envelope.envelope_id}
        await js.publish(
            envelope.subject,
            envelope.to_json().encode("utf-8"),
            headers=headers,
        )

    async def subscribe(self, subject: str, processor: EventProcessor) -> None:
        js = self._require_js("subscribe")

        from nats.js.api import ConsumerConfig, DeliverPolicy

        durable = "c_" + subject.replace(".", "_")

        async def _on_message(msg: "Msg") -> None:
            await self._handle(msg, processor)

        await js.subscribe(
            subject,
            durable=durable,
            cb=_on_message,
            manual_ack=True,
            config=ConsumerConfig(
                durable_name=durable,
                deliver_policy=DeliverPolicy.ALL,
                max_deliver=self._max_deliveries,
                ack_wait=self._ack_wait_seconds,
            ),
        )

    async def _handle(self, msg: "Msg", processor: EventProcessor) -> None:
        meta = msg.metadata
        delivery_count = int(getattr(meta, "num_delivered", 1) or 1)
        ctx = MessageContext(
            redelivered=delivery_count > 1,
            delivery_count=delivery_count,
            stream_sequence=getattr(getattr(meta, "sequence", None), "stream", None),
        )

        try:
            envelope = EventEnvelope.from_json(msg.data)
        except Exception as exc:  # noqa: BLE001 - unparseable -> dead letter
            processor.dead_letter.put(
                _unparseable_placeholder(msg.subject), f"decode error: {exc}", delivery_count
            )
            await self._publish_dead_letter(msg.data)
            await msg.term()
            return

        result = await processor.process(envelope, ctx)
        if result.action is ProcessAction.ACK:
            await msg.ack()
        elif result.action is ProcessAction.RETRY:
            # nak lets JetStream redeliver; max_deliver bounds the retries.
            await msg.nak()
        else:  # TERM: permanently unprocessable, already dead-lettered.
            await self._publish_dead_letter(msg.data)
            await msg.term()

    async def _publish_dead_letter(self, data: bytes) -> None:
        # Best-effort: a dead letter must never mask the original failure, so a
        # disconnected bus is skipped rather than raising here.
        if self._js is not None:
            await self._js.publish(DEAD_LETTER_SUBJECT, data)


def _unparseable_placeholder(subject: str) -> EventEnvelope:
    """A stand-in envelope for a message we could not decode.

    Lets the dead-letter queue record *something* typed even when the payload
    itself is not a valid envelope.
    """

    from datetime import datetime, timezone

    return EventEnvelope(
        subject=subject,
        provider="unknown",
        event_type="undecodable",
        event_time=datetime.now(timezone.utc),
        payload={"error": "undecodable payload"},
    )
