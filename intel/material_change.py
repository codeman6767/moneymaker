"""Material-change detection, model-input deltas and alerting.

The detector is the piece that decides when something *materially* changed and a
prediction should be re-run (requirement 10): it only emits a
:class:`~intel.base.MaterialChange` when a material input actually moved -- a
duplicate or no-op observation returns ``None`` and triggers nothing.

For each change it estimates the change in active probability and expected
minutes (requirement 11), captures the full before/after model inputs
(requirement 12), and produces an :class:`~intel.base.Alert` (requirement 13).
Unconfirmed information (e.g. social) can trigger a prediction but is never
marked auto-actionable (requirement 9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .base import (
    Alert,
    ChangeType,
    MaterialChange,
    PlayerRef,
    PlayerStatus,
    Severity,
    SourceMeta,
    SourceType,
    StatusSnapshot,
)
from .confidence import is_actionable
from .history import StatusHistory

# Status -> probability the player is active. None means "unknown / no estimate".
STATUS_ACTIVE_PROB: Dict[PlayerStatus, Optional[float]] = {
    PlayerStatus.AVAILABLE: 0.99,
    PlayerStatus.CONFIRMED_STARTER: 0.99,
    PlayerStatus.PROBABLE: 0.90,
    PlayerStatus.PROBABLE_STARTER: 0.85,
    PlayerStatus.QUESTIONABLE: 0.50,
    PlayerStatus.GAME_TIME_DECISION: 0.50,
    PlayerStatus.DOUBTFUL: 0.25,
    PlayerStatus.OUT: 0.0,
    PlayerStatus.SCRATCHED: 0.0,
    PlayerStatus.UNKNOWN: None,
}

_UNAVAILABLE = {PlayerStatus.OUT, PlayerStatus.SCRATCHED}
_AVAILABLE_ISH = {
    PlayerStatus.AVAILABLE,
    PlayerStatus.PROBABLE,
    PlayerStatus.PROBABLE_STARTER,
    PlayerStatus.CONFIRMED_STARTER,
}

# A scratch/ruling within this window of game time is a "late" one.
LATE_WINDOW = timedelta(minutes=90)
# Two differing observations within this window are treated as conflicting.
CONFLICT_WINDOW = timedelta(hours=12)


@dataclass
class Projection:
    """The projected inputs a prediction was built on, per subject."""

    subject_key: str
    projected_status: PlayerStatus = PlayerStatus.UNKNOWN
    projected_minutes: Optional[float] = None
    projected_starter: bool = False
    role: Optional[str] = None
    prediction_time: Optional[datetime] = None
    game_time: Optional[datetime] = None
    projected_lineup_keys: Optional[frozenset] = None


def active_probability(status: PlayerStatus) -> Optional[float]:
    return STATUS_ACTIVE_PROB.get(status)


def expected_minutes(
    status: PlayerStatus,
    *,
    projected_minutes: Optional[float],
    snapshot_minutes: Optional[float],
    minutes_restriction: Optional[float],
) -> Optional[float]:
    """Probability-weighted expected minutes. Returns None if no minutes basis
    exists (we do not invent a baseline)."""

    base = snapshot_minutes if snapshot_minutes is not None else projected_minutes
    if base is None:
        # OUT/SCRATCHED collapse to zero even without a baseline.
        return 0.0 if status in _UNAVAILABLE else None
    if minutes_restriction is not None:
        base = min(base, minutes_restriction)
    ap = active_probability(status)
    if ap is None:
        return base
    return round(base * ap, 2)


class MaterialChangeDetector:
    """Detects material changes against history and projections."""

    def __init__(self, history: Optional[StatusHistory] = None) -> None:
        self.history = history or StatusHistory()
        self._projections: Dict[str, Projection] = {}

    def set_projection(self, projection: Projection) -> None:
        self._projections[projection.subject_key] = projection

    # -- Status ingestion -----------------------------------------------------
    def ingest(self, snapshot: StatusSnapshot, now: datetime) -> Optional[MaterialChange]:
        subject = snapshot.subject_key
        projection = self._projections.get(subject)
        before = self.history.latest(subject)
        other = self.history.latest_from_other_source(subject, snapshot.source.source_id)

        # Append-only; an identical observation (same content hash) is a no-op
        # and triggers nothing (duplicate report).
        appended = self.history.append(snapshot)
        if not appended:
            return None

        # Reference inputs "before" this observation: prior snapshot else the
        # projection the prediction was built on.
        before_status, before_minutes_basis, before_restriction, before_conf, before_source = (
            self._before_reference(before, projection)
        )

        ap_before = active_probability(before_status)
        ap_after = active_probability(snapshot.status)
        em_before = expected_minutes(
            before_status,
            projected_minutes=projection.projected_minutes if projection else None,
            snapshot_minutes=(before.expected_minutes if before else before_minutes_basis),
            minutes_restriction=before_restriction,
        )
        em_after = expected_minutes(
            snapshot.status,
            projected_minutes=projection.projected_minutes if projection else None,
            snapshot_minutes=snapshot.expected_minutes,
            minutes_restriction=snapshot.minutes_restriction,
        )

        official_confirmation = bool(
            snapshot.source.confirmed
            and before is not None
            and before.status == snapshot.status
            and before.confidence < snapshot.confidence
        )

        conflict, conflicting = self._detect_conflict(snapshot, other)

        restriction_changed = (
            snapshot.minutes_restriction is not None
            and snapshot.minutes_restriction != before_restriction
        )

        status_changed = before_status != snapshot.status
        if not (status_changed or restriction_changed or official_confirmation):
            # Nothing material moved.
            return None

        change_type = self._classify(snapshot, before_status, projection, restriction_changed)

        published_after_prediction = bool(
            projection
            and projection.prediction_time is not None
            and snapshot.source.published_at > projection.prediction_time
        )

        return MaterialChange(
            change_type=change_type,
            subject_key=subject,
            player=snapshot.player,
            before=before,
            after=snapshot,
            active_probability_before=ap_before,
            active_probability_after=ap_after,
            expected_minutes_before=em_before,
            expected_minutes_after=em_after,
            confidence=snapshot.confidence,
            detected_at=now,
            requires_confirmation=not is_actionable(snapshot.confidence, snapshot.source),
            official_confirmation=official_confirmation,
            conflict=conflict,
            conflicting_sources=conflicting,
            published_after_prediction=published_after_prediction,
            model_inputs_before=self._inputs(
                before_status, ap_before, em_before, before_restriction, before_conf, before_source
            ),
            model_inputs_after=self._inputs(
                snapshot.status, ap_after, em_after, snapshot.minutes_restriction,
                snapshot.confidence, snapshot.source
            ),
        )

    def _before_reference(self, before, projection):
        if before is not None:
            return (
                before.status,
                before.expected_minutes,
                before.minutes_restriction,
                before.confidence,
                before.source,
            )
        if projection is not None:
            return (projection.projected_status, projection.projected_minutes, None, None, None)
        return (PlayerStatus.UNKNOWN, None, None, None, None)

    def _detect_conflict(self, snapshot: StatusSnapshot, other: Optional[StatusSnapshot]):
        if other is None or other.status == snapshot.status:
            return False, ()
        gap = abs((snapshot.source.published_at - other.source.published_at).total_seconds())
        if gap <= CONFLICT_WINDOW.total_seconds():
            return True, (other.source.source_id, snapshot.source.source_id)
        return False, ()

    def _classify(
        self,
        snapshot: StatusSnapshot,
        before_status: PlayerStatus,
        projection: Optional[Projection],
        restriction_changed: bool,
    ) -> ChangeType:
        role = snapshot.raw.get("role") if snapshot.raw else None
        if role == "starting_pitcher" and snapshot.status is PlayerStatus.SCRATCHED:
            return ChangeType.STARTING_PITCHER_SCRATCHED
        if snapshot.status in _UNAVAILABLE and before_status not in _UNAVAILABLE:
            return ChangeType.PLAYER_RULED_OUT
        if snapshot.status in _AVAILABLE_ISH and before_status in _UNAVAILABLE | {PlayerStatus.DOUBTFUL}:
            return ChangeType.PLAYER_BECAME_AVAILABLE
        if restriction_changed:
            return ChangeType.MINUTES_RESTRICTION
        if (
            projection is not None
            and not projection.projected_starter
            and snapshot.status is PlayerStatus.CONFIRMED_STARTER
        ):
            return ChangeType.UNEXPECTED_STARTER
        return ChangeType.STATUS_CHANGE

    @staticmethod
    def _inputs(status, ap, em, restriction, confidence, source: Optional[SourceMeta]) -> Dict[str, Any]:
        return {
            "status": status.value if status else None,
            "active_probability": ap,
            "expected_minutes": em,
            "minutes_restriction": restriction,
            "confidence": confidence,
            "source_id": source.source_id if source else None,
            "source_type": source.source_type.value if source else None,
            "confirmed": source.confirmed if source else None,
        }

    # -- Lineup and game-level changes ---------------------------------------
    def confirmed_lineup_change(
        self, lineup_obs, now: datetime
    ) -> Optional[MaterialChange]:
        """Compare a confirmed lineup to the projected one for its subject."""

        if not lineup_obs.confirmed:
            return None
        subject = f"{lineup_obs.game_id}:lineup:{lineup_obs.team}"
        projection = self._projections.get(subject)
        projected = projection.projected_lineup_keys if projection else None
        confirmed_keys = lineup_obs.starter_keys
        if projected is None or confirmed_keys == projected:
            return None

        unexpected = confirmed_keys - projected
        published_after_prediction = bool(
            projection
            and projection.prediction_time is not None
            and lineup_obs.published_at > projection.prediction_time
        )
        return MaterialChange(
            change_type=ChangeType.CONFIRMED_DIFFERS_FROM_PROJECTED,
            subject_key=subject,
            player=None,
            before=None,
            after=_lineup_pseudo_snapshot(lineup_obs, subject),
            active_probability_before=None,
            active_probability_after=None,
            expected_minutes_before=None,
            expected_minutes_after=None,
            confidence=0.95,
            detected_at=now,
            requires_confirmation=False,
            official_confirmation=True,
            published_after_prediction=published_after_prediction,
            model_inputs_before={"projected_starters": sorted(projected)},
            model_inputs_after={
                "confirmed_starters": sorted(confirmed_keys),
                "unexpected_starters": sorted(unexpected),
                "missing_from_confirmed": sorted(projected - confirmed_keys),
            },
        )

    def game_postponed(
        self, game_id: str, source: SourceMeta, now: datetime
    ) -> MaterialChange:
        subject = f"{game_id}:game_status"
        snap = _game_status_snapshot(game_id, subject, source, "postponed")
        self.history.append(snap)
        return MaterialChange(
            change_type=ChangeType.GAME_POSTPONED,
            subject_key=subject,
            player=None,
            before=None,
            after=snap,
            active_probability_before=None,
            active_probability_after=None,
            expected_minutes_before=None,
            expected_minutes_after=None,
            confidence=snap.confidence,
            detected_at=now,
            official_confirmation=source.confirmed,
            model_inputs_before={"game_status": "scheduled"},
            model_inputs_after={"game_status": "postponed"},
        )

    # -- Alerts ---------------------------------------------------------------
    def to_alert(self, change: MaterialChange, now: datetime, *, game_time: Optional[datetime] = None) -> Alert:
        actionable = not change.requires_confirmation and not change.conflict
        severity = self._severity(change, game_time)
        late = _is_late(change.after.source.published_at, game_time)
        msg = self._message(change, late)
        return Alert(
            alert_id=f"{change.change_type.value}:{change.after.snapshot_id}",
            change=change,
            severity=severity,
            message=msg,
            actionable=actionable,
            created_at=now,
        )

    def _severity(self, change: MaterialChange, game_time: Optional[datetime]) -> Severity:
        if change.requires_confirmation:
            return Severity.INFO
        critical_types = {
            ChangeType.STARTING_PITCHER_SCRATCHED,
            ChangeType.PLAYER_RULED_OUT,
            ChangeType.GAME_POSTPONED,
            ChangeType.CONFIRMED_DIFFERS_FROM_PROJECTED,
            ChangeType.UNEXPECTED_STARTER,
        }
        if change.change_type in critical_types:
            return Severity.CRITICAL
        if _is_late(change.after.source.published_at, game_time):
            return Severity.CRITICAL
        return Severity.WARNING

    @staticmethod
    def _message(change: MaterialChange, late: bool) -> str:
        who = change.player.full_name if change.player else change.subject_key
        prefix = "LATE " if late else ""
        return f"{prefix}{change.change_type.value}: {who} (conf={change.confidence:.2f})"


def _is_late(published_at: datetime, game_time: Optional[datetime]) -> bool:
    if game_time is None:
        return False
    return timedelta(0) <= (game_time - published_at) <= LATE_WINDOW


def _lineup_pseudo_snapshot(lineup_obs, subject: str) -> StatusSnapshot:
    from .base import make_snapshot

    return make_snapshot(
        subject_key=subject,
        player=PlayerRef(full_name=f"{lineup_obs.team} lineup", team=lineup_obs.team),
        status=PlayerStatus.CONFIRMED_STARTER,
        source=SourceMeta(
            source_id="lineup_confirm",
            source_type=SourceType.LICENSED_DATA,
            published_at=lineup_obs.published_at,
            retrieved_at=lineup_obs.retrieved_at,
            confirmed=True,
        ),
        confidence=0.95,
        raw={"starters": [p.key() for p in lineup_obs.starters]},
    )


def _game_status_snapshot(game_id: str, subject: str, source: SourceMeta, status_text: str) -> StatusSnapshot:
    from .base import make_snapshot

    return make_snapshot(
        subject_key=subject,
        player=PlayerRef(full_name=f"game {game_id}"),
        status=PlayerStatus.UNKNOWN,
        source=source,
        confidence=0.98 if source.confirmed else 0.7,
        reason=status_text,
        raw={"game_status": status_text},
    )
