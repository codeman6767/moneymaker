"""Detection and resolution of corrected events.

Providers restate events: an order-book level or a lineup gets corrected after
the fact, re-using the same provider ``sequence`` but with different content.
Because the content differs, the deduplicator does *not* suppress a correction
(different ``content_hash``) -- but the sequence tracker would otherwise flag it
as a duplicate sequence. This handler sits between them: it keeps the current
authoritative content per ``(stream_key, sequence)`` and classifies each arrival
as the original, an exact repeat, or a correction that supersedes a prior
version.

A handler downstream is expected to treat a ``CORRECTION`` as an idempotent
*replace* of the state produced by the superseded event.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from .event_envelope import EventEnvelope


class CorrectionStatus(str, Enum):
    #: Stream/sequence has no correction semantics (no sequence number).
    NOT_APPLICABLE = "not_applicable"
    #: First time we've seen this (stream_key, sequence).
    ORIGINAL = "original"
    #: Same (stream_key, sequence) and identical content as current.
    UNCHANGED = "unchanged"
    #: Same (stream_key, sequence), different content -> supersedes prior.
    CORRECTION = "correction"


@dataclass
class CorrectionResult:
    status: CorrectionStatus
    #: content_hash of the version this correction supersedes, if any.
    supersedes_hash: Optional[str] = None
    #: envelope_id of the superseded version, if known.
    supersedes_envelope_id: Optional[str] = None

    @property
    def is_correction(self) -> bool:
        return self.status is CorrectionStatus.CORRECTION


@dataclass
class _Current:
    content_hash: str
    envelope_id: str


class CorrectionHandler:
    """Tracks the authoritative version of each sequenced event."""

    def __init__(self) -> None:
        self._current: Dict[Tuple[str, int], _Current] = {}

    def observe(self, envelope: EventEnvelope) -> CorrectionResult:
        seq = envelope.sequence
        if seq is None:
            return CorrectionResult(CorrectionStatus.NOT_APPLICABLE)

        key = (envelope.stream_key or f"{envelope.provider}:{envelope.subject}", seq)
        content_hash = envelope.content_hash or envelope.compute_content_hash()
        current = self._current.get(key)

        if current is None:
            # An explicit correction flag for a sequence we never saw still
            # records as the current version; there is nothing to supersede.
            self._current[key] = _Current(content_hash, envelope.envelope_id)
            return CorrectionResult(CorrectionStatus.ORIGINAL)

        if current.content_hash == content_hash and not envelope.is_correction:
            return CorrectionResult(
                CorrectionStatus.UNCHANGED,
                supersedes_hash=current.content_hash,
                supersedes_envelope_id=current.envelope_id,
            )

        # Same sequence, different content (or explicitly flagged): a correction.
        superseded = current
        self._current[key] = _Current(content_hash, envelope.envelope_id)
        return CorrectionResult(
            CorrectionStatus.CORRECTION,
            supersedes_hash=superseded.content_hash,
            supersedes_envelope_id=superseded.envelope_id,
        )

    def current_hash(self, stream_key: str, sequence: int) -> Optional[str]:
        cur = self._current.get((stream_key, sequence))
        return cur.content_hash if cur else None
