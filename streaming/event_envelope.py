"""Versioned event envelope for the low-latency streaming backbone.

Every event that crosses the streaming backbone is wrapped in an
:class:`EventEnvelope`. The envelope is transport-agnostic (it does not know
about NATS, files, or replay) and carries everything needed for:

* latency accounting -- provider, network, internal and exchange delays are
  reconstructable from the timestamps below;
* deduplication -- a content hash identifies "the same event content" across
  redelivery *and* re-capture;
* sequence validation -- an optional provider ``sequence`` number per logical
  ``stream_key``;
* correction handling -- explicit linkage back to a superseded event.

Per the repository rules (see ``CLAUDE.md``): every event carries provider,
event, receipt and monotonic timestamps, and we never conflate the four delay
components.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# Semantic version of the envelope schema. Bump the MAJOR component for any
# breaking change to the fields that participate in the content hash.
SCHEMA_VERSION = "1.0.0"

# Versions a consumer in this build knows how to process. Anything outside this
# set must be routed to the dead-letter path rather than processed.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0.0"})

NS_PER_SECOND = 1_000_000_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def canonical_json(data: Any) -> str:
    """Deterministic JSON encoding used for content hashing.

    Keys are sorted and separators are tight so the same logical content always
    produces the same bytes regardless of dict insertion order.
    """

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _delta_ns(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[int]:
    if later is None or earlier is None:
        return None
    return int((later - earlier).total_seconds() * NS_PER_SECOND)


class EventEnvelope(BaseModel):
    """Versioned, self-describing wrapper around a single provider event."""

    # extra="forbid" makes an unexpected field a validation error, which is how
    # a subtly incompatible producer surfaces as a schema problem early.
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION

    # Identity ----------------------------------------------------------------
    # envelope_id is unique per *capture*. Two independent captures of the same
    # provider event (e.g. from a snapshot recovery) share a content_hash but
    # have different envelope_ids.
    envelope_id: str = Field(default_factory=_new_id)
    subject: str
    provider: str
    event_type: str

    # Logical stream this event is sequenced within. Defaults to
    # ``provider:subject`` when not supplied explicitly.
    stream_key: Optional[str] = None
    # Provider-assigned monotonic sequence number within ``stream_key``.
    sequence: Optional[int] = None

    # Timestamps (all four required by CLAUDE.md) -----------------------------
    # When the event actually happened, according to the provider.
    event_time: datetime
    # When the provider says it sent the event (provider clock).
    provider_sent_at: Optional[datetime] = None
    # When we received it (our wall clock). Useful for logs/audits; NOT used for
    # latency math that must be robust to wall-clock adjustments.
    received_at: datetime = Field(default_factory=_utcnow)
    # Monotonic clock reading at receipt. This is the anchor for all internal
    # latency measurement -- monotonic clocks never jump backwards.
    received_monotonic_ns: int = Field(default_factory=time.monotonic_ns)

    # Correction linkage ------------------------------------------------------
    is_correction: bool = False
    corrects_sequence: Optional[int] = None
    corrects_envelope_id: Optional[str] = None

    # Body --------------------------------------------------------------------
    payload: dict[str, Any] = Field(default_factory=dict)

    # Cached content hash. Populated automatically; callers should not set it.
    content_hash: Optional[str] = None

    def model_post_init(self, _context: Any) -> None:
        if self.stream_key is None:
            self.stream_key = f"{self.provider}:{self.subject}"
        if self.content_hash is None:
            self.content_hash = self.compute_content_hash()

    # -- Content hashing ------------------------------------------------------
    def _content_fields(self) -> dict[str, Any]:
        """Fields that define event *content* (identity for dedup).

        Deliberately excludes receipt-specific fields (``envelope_id``,
        ``received_at``, ``received_monotonic_ns``) so that a redelivery or an
        independent re-capture of the same event hashes identically.
        """

        return {
            "schema_version": self.schema_version,
            "subject": self.subject,
            "provider": self.provider,
            "event_type": self.event_type,
            "stream_key": self.stream_key,
            "sequence": self.sequence,
            "event_time": self.event_time.isoformat(),
            "is_correction": self.is_correction,
            "corrects_sequence": self.corrects_sequence,
            "payload": self.payload,
        }

    def compute_content_hash(self) -> str:
        return hashlib.sha256(canonical_json(self._content_fields()).encode("utf-8")).hexdigest()

    # -- Latency accounting ---------------------------------------------------
    def provider_delay_ns(self) -> Optional[int]:
        """Delay inside the provider: event happened -> provider sent it."""

        return _delta_ns(self.provider_sent_at, self.event_time)

    def network_delay_ns(self) -> Optional[int]:
        """Delay on the wire: provider sent it -> we received it."""

        return _delta_ns(self.received_at, self.provider_sent_at)

    def ingest_delay_ns(self) -> Optional[int]:
        """Total observed delay: event happened -> we received it.

        This is provider + network combined. It intentionally does NOT include
        any internal processing time, which must be measured separately with a
        monotonic clock (see :meth:`internal_delay_ns`).
        """

        return _delta_ns(self.received_at, self.event_time)

    def internal_delay_ns(self, processed_monotonic_ns: int) -> int:
        """Internal processing delay: receipt -> a later processing point.

        Measured purely on the monotonic clock, so it is immune to wall-clock
        adjustments. ``processed_monotonic_ns`` is a reading from
        :func:`time.monotonic_ns` taken at the processing point.
        """

        return processed_monotonic_ns - self.received_monotonic_ns

    def latency_breakdown(self, processed_monotonic_ns: Optional[int] = None) -> dict[str, Optional[int]]:
        """Return the four delay components, in nanoseconds.

        ``exchange_delay_ns`` is only meaningful on the execution path (order
        submitted -> exchange acknowledged) and is populated by the execution
        service, not here; it is included as ``None`` so callers always see the
        full four-part accounting the rules require.
        """

        internal = (
            self.internal_delay_ns(processed_monotonic_ns)
            if processed_monotonic_ns is not None
            else None
        )
        return {
            "provider_delay_ns": self.provider_delay_ns(),
            "network_delay_ns": self.network_delay_ns(),
            "internal_delay_ns": internal,
            "exchange_delay_ns": None,
        }

    # -- Serialization --------------------------------------------------------
    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> "EventEnvelope":
        return cls.model_validate_json(data)

    @classmethod
    def create(
        cls,
        *,
        subject: str,
        provider: str,
        event_type: str,
        event_time: datetime,
        payload: Optional[dict[str, Any]] = None,
        sequence: Optional[int] = None,
        stream_key: Optional[str] = None,
        provider_sent_at: Optional[datetime] = None,
        is_correction: bool = False,
        corrects_sequence: Optional[int] = None,
        corrects_envelope_id: Optional[str] = None,
    ) -> "EventEnvelope":
        """Stamp receipt timestamps and build an envelope at ingest time.

        Prefer this over the raw constructor at the edge of the system: it fixes
        ``received_at`` and ``received_monotonic_ns`` to *now* so downstream
        latency math is anchored to the moment of capture.
        """

        return cls(
            subject=subject,
            provider=provider,
            event_type=event_type,
            event_time=event_time,
            payload=payload or {},
            sequence=sequence,
            stream_key=stream_key,
            provider_sent_at=provider_sent_at,
            received_at=_utcnow(),
            received_monotonic_ns=time.monotonic_ns(),
            is_correction=is_correction,
            corrects_sequence=corrects_sequence,
            corrects_envelope_id=corrects_envelope_id,
        )
