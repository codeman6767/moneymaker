"""Shared machinery for in-memory live state (Module 2).

Every live state model (MLB game, NBA game, Kalshi order book) is a mutable
object owned by a *single writer* and guarded by the store's lock. Reads happen
through :meth:`LiveState.snapshot`, which returns a deeply-frozen, immutable
:class:`StateSnapshot` -- so a reader never observes a half-applied event and
snapshots are atomic.

Common concerns handled here, once, for all three domains:

* sequence-gap detection and snapshot-recovery signalling,
* correction handling,
* content-addressed state hashes,
* staleness checks,
* data-quality flags.

No method here performs I/O -- event application never touches a database or
the network (a hard rule for the hot path; see ``CLAUDE.md``).
"""

from __future__ import annotations

import enum
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional

from streaming.event_envelope import EventEnvelope, canonical_json


def now_ns() -> int:
    """Monotonic clock reading. All latency/staleness math uses this."""

    return time.monotonic_ns()


class ApplyStatus(str, enum.Enum):
    #: Event applied and advanced the sequence.
    APPLIED = "applied"
    #: Already-seen or older sequence; nothing changed.
    DUPLICATE = "duplicate"
    #: Sequence gap detected; event NOT applied, snapshot recovery required.
    GAP_DETECTED = "gap_detected"
    #: A correction restated existing state.
    CORRECTION_APPLIED = "correction_applied"
    #: A full snapshot replaced state and reset the sequence baseline.
    SNAPSHOT_APPLIED = "snapshot_applied"
    #: Applied without sequence tracking (unsequenced stream).
    UNSEQUENCED = "unsequenced"
    #: Event rejected (e.g. failed validation); nothing changed.
    REJECTED = "rejected"


class DataQuality(enum.IntFlag):
    """Bit flags describing the trustworthiness of the current state."""

    OK = 0
    SEQUENCE_GAP = 1
    STALE = 2
    MISSING_FIELD = 4
    OUT_OF_RANGE = 8
    RECOVERING = 16


@dataclass
class ApplyResult:
    status: ApplyStatus
    entity_id: str
    sequence: Optional[int] = None
    #: True when a snapshot must be requested from the provider to recover.
    needs_snapshot: bool = False
    missing_from: Optional[int] = None
    missing_to: Optional[int] = None
    quality: DataQuality = DataQuality.OK
    message: Optional[str] = None

    @property
    def applied(self) -> bool:
        return self.status in (
            ApplyStatus.APPLIED,
            ApplyStatus.CORRECTION_APPLIED,
            ApplyStatus.SNAPSHOT_APPLIED,
            ApplyStatus.UNSEQUENCED,
        )


def _deep_freeze(value: Any) -> Any:
    """Recursively convert containers into immutable equivalents."""

    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(v) for v in value)
    return value


def compute_state_hash(content: Mapping[str, Any]) -> str:
    """Deterministic content hash of a state's semantic fields."""

    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StateSnapshot:
    """Immutable, atomic view of a live state at a point in time."""

    entity_id: str
    kind: str
    content: Mapping[str, Any]
    sequence: Optional[int]
    last_snapshot_sequence: Optional[int]
    last_delta_sequence: Optional[int]
    correction_count: int
    applied_count: int
    awaiting_snapshot: bool
    data_quality: DataQuality
    last_event_time: Optional[datetime]
    captured_monotonic_ns: int
    state_hash: str

    def get(self, key: str, default: Any = None) -> Any:
        return self.content.get(key, default)

    @property
    def healthy(self) -> bool:
        return self.data_quality == DataQuality.OK


class LiveState:
    """Base class for single-writer, sequence-tracked live state."""

    kind: str = "state"
    #: Order books cannot trust deltas before a snapshot; games can baseline off
    #: their first observed event.
    require_snapshot_first: bool = False

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.last_sequence: Optional[int] = None
        self.last_snapshot_sequence: Optional[int] = None
        self.last_delta_sequence: Optional[int] = None
        self.last_event_time: Optional[datetime] = None
        self.last_update_monotonic_ns: Optional[int] = None
        self.awaiting_snapshot: bool = False
        self.correction_count: int = 0
        self.applied_count: int = 0
        self.quality: DataQuality = DataQuality.OK

    # -- Hooks a subclass implements -----------------------------------------
    def _apply_event(self, envelope: EventEnvelope) -> None:
        raise NotImplementedError

    def _apply_snapshot(self, envelope: EventEnvelope) -> None:
        raise NotImplementedError

    def _apply_correction(self, envelope: EventEnvelope) -> None:
        # Default: a correction restates fields exactly like a normal event.
        self._apply_event(envelope)

    def _content(self) -> dict[str, Any]:
        raise NotImplementedError

    def _is_snapshot(self, envelope: EventEnvelope) -> bool:
        return envelope.event_type == "snapshot"

    def _is_correction(self, envelope: EventEnvelope) -> bool:
        return envelope.is_correction or envelope.event_type == "correction"

    # -- Core apply -----------------------------------------------------------
    def apply(self, envelope: EventEnvelope) -> ApplyResult:
        is_snapshot = self._is_snapshot(envelope)
        is_correction = self._is_correction(envelope)
        seq = envelope.sequence

        if is_snapshot:
            self._apply_snapshot(envelope)
            if seq is not None:
                self.last_sequence = seq
                self.last_snapshot_sequence = seq
            self.awaiting_snapshot = False
            self.quality &= ~DataQuality.SEQUENCE_GAP
            self.quality &= ~DataQuality.RECOVERING
            self.applied_count += 1
            self._touch(envelope)
            return ApplyResult(
                ApplyStatus.SNAPSHOT_APPLIED, self.entity_id, seq, quality=self.quality
            )

        if is_correction:
            # A correction restates existing content; it does not advance the
            # sequence baseline, and it is allowed even while awaiting a
            # snapshot (it can only improve accuracy).
            self._apply_correction(envelope)
            self.correction_count += 1
            self.applied_count += 1
            self._touch(envelope)
            return ApplyResult(
                ApplyStatus.CORRECTION_APPLIED, self.entity_id, seq, quality=self.quality
            )

        if seq is None:
            self._apply_event(envelope)
            self.applied_count += 1
            self._touch(envelope)
            return ApplyResult(ApplyStatus.UNSEQUENCED, self.entity_id, None, quality=self.quality)

        # First sequenced delta with no baseline.
        if self.last_sequence is None:
            if self.require_snapshot_first:
                self.awaiting_snapshot = True
                self.quality |= DataQuality.SEQUENCE_GAP | DataQuality.RECOVERING
                return ApplyResult(
                    ApplyStatus.GAP_DETECTED,
                    self.entity_id,
                    seq,
                    needs_snapshot=True,
                    quality=self.quality,
                    message="no snapshot baseline yet",
                )
            self._apply_event(envelope)
            self.last_sequence = seq
            self.last_delta_sequence = seq
            self.applied_count += 1
            self._touch(envelope)
            return ApplyResult(ApplyStatus.APPLIED, self.entity_id, seq, quality=self.quality)

        # Duplicate / stale re-arrival.
        if seq <= self.last_sequence:
            return ApplyResult(ApplyStatus.DUPLICATE, self.entity_id, seq, quality=self.quality)

        # Contiguous next.
        if seq == self.last_sequence + 1:
            self._apply_event(envelope)
            self.last_sequence = seq
            self.last_delta_sequence = seq
            self.applied_count += 1
            self._touch(envelope)
            return ApplyResult(ApplyStatus.APPLIED, self.entity_id, seq, quality=self.quality)

        # Gap: seq > last + 1. Do NOT apply -- hold state and demand a snapshot.
        missing_from = self.last_sequence + 1
        missing_to = seq - 1
        self.awaiting_snapshot = True
        self.quality |= DataQuality.SEQUENCE_GAP | DataQuality.RECOVERING
        return ApplyResult(
            ApplyStatus.GAP_DETECTED,
            self.entity_id,
            seq,
            needs_snapshot=True,
            missing_from=missing_from,
            missing_to=missing_to,
            quality=self.quality,
            message=f"gap; missing {missing_from}..{missing_to}",
        )

    def _touch(self, envelope: EventEnvelope) -> None:
        self.last_event_time = envelope.event_time
        self.last_update_monotonic_ns = now_ns()

    # -- Staleness ------------------------------------------------------------
    def is_stale(self, now_monotonic_ns: int, max_age_ns: int) -> bool:
        if self.last_update_monotonic_ns is None:
            return True
        return (now_monotonic_ns - self.last_update_monotonic_ns) > max_age_ns

    # -- Snapshot -------------------------------------------------------------
    def snapshot(
        self,
        *,
        now_monotonic_ns: Optional[int] = None,
        staleness_max_age_ns: Optional[int] = None,
    ) -> StateSnapshot:
        content = self._content()
        quality = self.quality
        captured = now_monotonic_ns if now_monotonic_ns is not None else now_ns()
        if staleness_max_age_ns is not None and self.is_stale(captured, staleness_max_age_ns):
            quality |= DataQuality.STALE
        return StateSnapshot(
            entity_id=self.entity_id,
            kind=self.kind,
            content=_deep_freeze(content),
            sequence=self.last_sequence,
            last_snapshot_sequence=self.last_snapshot_sequence,
            last_delta_sequence=self.last_delta_sequence,
            correction_count=self.correction_count,
            applied_count=self.applied_count,
            awaiting_snapshot=self.awaiting_snapshot,
            data_quality=quality,
            last_event_time=self.last_event_time,
            captured_monotonic_ns=captured,
            state_hash=compute_state_hash(content),
        )
