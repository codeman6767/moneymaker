"""Typed MLB StatsAPI game-status mapping.

MLB StatsAPI describes a game's state with three overlapping fields on its
``status`` object: ``detailedState`` (the most granular, e.g. "Pre-Game",
"Warmup", "In Progress", "Postponed"), ``codedGameState`` (a single letter), and
``abstractGameState`` (coarse: Preview / Live / Final). This module is the one
place that maps them to the canonical :data:`schema.MAPPED_GAME_STATUSES`.

An **unknown or unmapped** provider status is never guessed into "final" or
"scheduled": it maps to the explicit ``unknown`` state, preserves the original
provider string, and is flagged so the ingestor can record a data-quality issue.
Nothing here performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

# detailedState (lower-cased, stripped) -> canonical status.
_DETAILED: dict[str, str] = {
    "scheduled": "scheduled",
    "pre-game": "pregame",
    "pregame": "pregame",
    "warmup": "warmup",
    "in progress": "in_progress",
    "manager challenge": "in_progress",
    "delayed": "delayed",
    "delayed start": "delayed",
    "postponed": "postponed",
    "suspended": "suspended",
    "final": "final",
    "game over": "final",
    "completed early": "final",
    "completed early: rain": "final",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

# abstractGameState (coarse) -> canonical, used only as a fallback.
_ABSTRACT: dict[str, str] = {
    "preview": "scheduled",
    "live": "in_progress",
    "final": "final",
}


@dataclass(frozen=True)
class MappedStatus:
    """The canonical mapping plus the preserved provider strings.

    ``is_unknown`` is True when no provider field could be mapped -- the caller
    records a data-quality issue and keeps ``canonical == 'unknown'``.
    """

    canonical: str
    detailed_state: Optional[str]
    coded_state: Optional[str]
    abstract_state: Optional[str]
    is_unknown: bool


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def map_mlb_status(status: Optional[Mapping[str, Any]]) -> MappedStatus:
    """Map an MLB StatsAPI ``status`` object to a canonical status.

    Prefers ``detailedState`` (most granular), then ``abstractGameState`` as a
    coarse fallback. An unrecognized status yields ``unknown`` (never guessed).
    """

    if not isinstance(status, Mapping):
        return MappedStatus("unknown", None, None, None, True)

    detailed = _clean(status.get("detailedState"))
    coded = _clean(status.get("codedGameState"))
    abstract = _clean(status.get("abstractGameState"))

    canonical: Optional[str] = None
    if detailed is not None:
        canonical = _DETAILED.get(detailed.lower())
    if canonical is None and abstract is not None:
        canonical = _ABSTRACT.get(abstract.lower())

    if canonical is None:
        return MappedStatus("unknown", detailed, coded, abstract, True)
    return MappedStatus(canonical, detailed, coded, abstract, False)
