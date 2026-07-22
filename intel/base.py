"""Core models for injury / lineup / material-news intelligence (Module 4).

Design invariants (from the requirements and ``CLAUDE.md``):

* **Append-only history.** A status is never overwritten. Every observation is a
  distinct, immutable :class:`StatusSnapshot`; corrections and later reports are
  *appended*, preserving the full timeline.
* **Provenance on every observation.** Each snapshot records the source, the
  source's own publication time, and our retrieval time -- three separate
  timestamps that must not be conflated.
* **No fabricated certainty.** Player matching is deterministic and returns
  "ambiguous" rather than guessing; unconfirmed social information is never
  automatically actionable.

Nothing here does I/O; adapters (which do) build these models.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def content_hash(data: Any) -> str:
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


class SourceType(str, enum.Enum):
    """Ordered loosely by trust; see :mod:`intel.confidence` for scores."""

    OFFICIAL_LEAGUE = "official_league"   # e.g. NBA injury report
    OFFICIAL_TEAM = "official_team"        # team announcement
    LICENSED_DATA = "licensed_data"        # licensed lineup / data provider
    BEAT_REPORTER = "beat_reporter"        # authorized news / credentialed beat
    SOCIAL = "social"                       # authorized social feed
    PROJECTION = "projection"               # our own projected lineup/minutes


class PlayerStatus(str, enum.Enum):
    AVAILABLE = "available"
    PROBABLE = "probable"
    QUESTIONABLE = "questionable"
    DOUBTFUL = "doubtful"
    GAME_TIME_DECISION = "game_time_decision"
    OUT = "out"
    # Pitcher / lineup specific.
    PROBABLE_STARTER = "probable_starter"
    CONFIRMED_STARTER = "confirmed_starter"
    SCRATCHED = "scratched"
    UNKNOWN = "unknown"


class ChangeType(str, enum.Enum):
    STARTING_PITCHER_SCRATCHED = "starting_pitcher_scratched"
    PLAYER_RULED_OUT = "player_ruled_out"
    PLAYER_BECAME_AVAILABLE = "player_became_available"
    MINUTES_RESTRICTION = "minutes_restriction"
    CONFIRMED_DIFFERS_FROM_PROJECTED = "confirmed_differs_from_projected"
    UNEXPECTED_STARTER = "unexpected_starter"
    GAME_POSTPONED = "game_postponed"
    STATUS_CHANGE = "status_change"  # generic material status move


@dataclass(frozen=True)
class PlayerRef:
    """Identity of a player. ``player_id`` is a provider-stable id when known."""

    full_name: str
    team: Optional[str] = None
    player_id: Optional[str] = None
    normalized: Optional[str] = None  # filled in by the matcher

    def key(self) -> str:
        if self.player_id:
            return f"pid:{self.player_id}"
        return f"name:{self.normalized or self.full_name.lower()}|team:{self.team or '?'}"


@dataclass(frozen=True)
class SourceMeta:
    """Provenance for one observation."""

    source_id: str
    source_type: SourceType
    published_at: datetime   # when the SOURCE published it
    retrieved_at: datetime   # when WE fetched it
    reference: Optional[str] = None  # url / doc id
    confirmed: bool = False  # official confirmation flag


@dataclass(frozen=True)
class StatusSnapshot:
    """An immutable, append-only observation of a subject's status."""

    snapshot_id: str
    subject_key: str
    player: PlayerRef
    status: PlayerStatus
    source: SourceMeta
    confidence: float
    reason: Optional[str] = None
    expected_minutes: Optional[float] = None
    minutes_restriction: Optional[float] = None
    is_correction: bool = False
    content_hash: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def make_snapshot(
    *,
    subject_key: str,
    player: PlayerRef,
    status: PlayerStatus,
    source: SourceMeta,
    confidence: float,
    reason: Optional[str] = None,
    expected_minutes: Optional[float] = None,
    minutes_restriction: Optional[float] = None,
    is_correction: bool = False,
    raw: Optional[Dict[str, Any]] = None,
) -> StatusSnapshot:
    """Build a snapshot, computing its content hash and a deterministic id.

    The content hash covers the *observation content* (subject, status, reason,
    minutes, source publication time) so identical re-reports hash identically
    while a genuine change (or a re-publish) differs.
    """

    ch = content_hash(
        {
            "subject_key": subject_key,
            "player": player.key(),
            "status": status.value,
            "reason": reason,
            "expected_minutes": expected_minutes,
            "minutes_restriction": minutes_restriction,
            "source_id": source.source_id,
            "published_at": source.published_at.isoformat(),
        }
    )
    snapshot_id = f"{source.source_id}:{ch[:16]}"
    return StatusSnapshot(
        snapshot_id=snapshot_id,
        subject_key=subject_key,
        player=player,
        status=status,
        source=source,
        confidence=confidence,
        reason=reason,
        expected_minutes=expected_minutes,
        minutes_restriction=minutes_restriction,
        is_correction=is_correction,
        content_hash=ch,
        raw=raw or {},
    )


@dataclass(frozen=True)
class MaterialChange:
    """A detected, materially-relevant change with before/after model inputs."""

    change_type: ChangeType
    subject_key: str
    player: Optional[PlayerRef]
    before: Optional[StatusSnapshot]
    after: StatusSnapshot
    active_probability_before: Optional[float]
    active_probability_after: Optional[float]
    expected_minutes_before: Optional[float]
    expected_minutes_after: Optional[float]
    confidence: float
    detected_at: datetime
    requires_confirmation: bool = False
    official_confirmation: bool = False
    conflict: bool = False
    conflicting_sources: tuple[str, ...] = ()
    published_after_prediction: bool = False
    model_inputs_before: Dict[str, Any] = field(default_factory=dict)
    model_inputs_after: Dict[str, Any] = field(default_factory=dict)

    @property
    def active_probability_delta(self) -> Optional[float]:
        if self.active_probability_before is None or self.active_probability_after is None:
            return None
        return self.active_probability_after - self.active_probability_before


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    """A human/machine-facing alert derived from a material change."""

    alert_id: str
    change: MaterialChange
    severity: Severity
    message: str
    #: False when the change rests on unconfirmed information (e.g. social) and
    #: must not be auto-traded.
    actionable: bool
    created_at: datetime
