"""Typed value objects for the Phase E1 point-in-time layer.

Everything here is a small, immutable, deterministic value object. There is one
strict :class:`Cutoff` type (task §4) and a handful of frozen read results. No
database, network, provider, or execution imports live in this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Optional

from ..db.schema import from_iso, to_iso

__all__ = [
    "Cutoff",
    "Observation",
    "MatchDecisionView",
    "LinkAsOf",
]


@dataclass(frozen=True)
class Cutoff:
    """A single strict, timezone-aware UTC point-in-time cutoff (task §4).

    The wrapped ``datetime`` is always timezone-aware and normalized to UTC.
    Naive datetimes and unparseable strings are rejected; local time is never
    assumed. ``iso`` serializes to the corpus canonical microsecond ``...Z``
    format so that a string comparison ``observed_at <= cutoff.iso`` is exactly
    chronological against the fixed-width stored timestamps. Equality/hash derive
    from the instant, so two cutoffs for the same instant compare equal
    deterministically.
    """

    _dt: datetime

    def __post_init__(self) -> None:
        if self._dt.tzinfo is None or self._dt.utcoffset() is None:
            raise ValueError("Cutoff datetime must be timezone-aware (naive rejected)")
        if self._dt.tzinfo != timezone.utc:
            # Normalize to UTC so equality and serialization are canonical.
            object.__setattr__(self, "_dt", self._dt.astimezone(timezone.utc))

    # -- constructors -------------------------------------------------------- #
    @staticmethod
    def parse(text: str) -> "Cutoff":
        """Parse an ISO-8601 UTC/tz-aware timestamp. Naive/invalid are rejected."""

        if not isinstance(text, str) or not text.strip():
            raise ValueError("cutoff must be a non-empty ISO-8601 UTC timestamp string")
        try:
            return Cutoff(from_iso(text))  # corpus canonical %Y-%m-%dT%H:%M:%S.%fZ
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"not a valid ISO-8601 timestamp: {text!r}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"cutoff must be timezone-aware (naive rejected): {text!r}")
        return Cutoff(parsed)

    @staticmethod
    def from_datetime(dt: datetime) -> "Cutoff":
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("cutoff datetime must be timezone-aware (naive rejected)")
        return Cutoff(dt)

    # -- accessors ----------------------------------------------------------- #
    @property
    def iso(self) -> str:
        """Canonical corpus serialization (``2026-07-24T18:00:00.000000Z``)."""

        return to_iso(self._dt)

    @property
    def datetime(self) -> datetime:
        return self._dt

    def __str__(self) -> str:
        return self.iso

    def __repr__(self) -> str:
        return f"Cutoff({self.iso!r})"


def _freeze(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(k): row[k] for k in row.keys()})


@dataclass(frozen=True)
class Observation:
    """One immutable as-of observation row from an ``asof_filtered`` table.

    ``fields`` is a read-only mapping of the selected columns; ``observed_at`` and
    ``row_id`` are the transaction-time and stable tie-break identifier used to
    select it. ``as_dict`` gives a plain, key-sorted dict for deterministic
    serialization.
    """

    table: str
    observed_at: Optional[str]
    row_id: Optional[str]
    fields: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def get(self, column: str) -> Any:
        return self.fields[column]

    def as_dict(self) -> dict[str, Any]:
        return {k: self.fields[k] for k in sorted(self.fields)}


@dataclass(frozen=True)
class MatchDecisionView:
    """A match decision as it was known at a cutoff (task §7 match decisions).

    Immutable decision facts (``outcome``/``matched_entity_id``/``decided_at``)
    are the row as stored, already filtered to ``decided_at <= cutoff``. The
    mutable review columns are exposed only when the review was *completed by* the
    cutoff (``reviewed_at`` is a real transaction time): a later manual review is
    invisible. The mutable ``needs_manual_review`` flag has no timeline and is
    deliberately NOT presented as historical truth.
    """

    match_id: str
    entity_type: str
    source_provider: str
    source_ref: str
    outcome: str
    method: str
    score: float
    decided_at: str
    matched_entity_id: Optional[str] = None
    review_completed_by_cutoff: bool = False
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "entity_type": self.entity_type,
            "source_provider": self.source_provider,
            "source_ref": self.source_ref,
            "outcome": self.outcome,
            "method": self.method,
            "score": self.score,
            "decided_at": self.decided_at,
            "matched_entity_id": self.matched_entity_id,
            "review_completed_by_cutoff": self.review_completed_by_cutoff,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
        }


@dataclass(frozen=True)
class LinkAsOf:
    """A canonical game link proven historically at a cutoff (sportsbook/Kalshi).

    Returned ONLY when the provider event/market's orientation is approved as of
    the cutoff through the accepted decision + DQ/review timeline; ``details``
    carries provider-specific proven facts (e.g. Kalshi ``yes_team_id``).
    """

    game_id: str
    match_decision_id: str
    details: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"game_id": self.game_id,
                               "match_decision_id": self.match_decision_id}
        for k in sorted(self.details):
            out[k] = self.details[k]
        return out
