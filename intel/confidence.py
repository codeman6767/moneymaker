"""Source-confidence scoring.

Assigns a deterministic confidence in [0, 1] to an observation based on its
source type and whether it is officially confirmed. Confidence gates
actionability: unconfirmed social/beat information scores below
:data:`ACTIONABLE_THRESHOLD`, so it can inform predictions but is never
auto-traded (requirement 9).
"""

from __future__ import annotations

from .base import SourceMeta, SourceType

# Base trust per source type.
BASE_CONFIDENCE = {
    SourceType.OFFICIAL_LEAGUE: 0.98,
    SourceType.OFFICIAL_TEAM: 0.95,
    SourceType.LICENSED_DATA: 0.90,
    SourceType.BEAT_REPORTER: 0.70,
    SourceType.SOCIAL: 0.40,
    SourceType.PROJECTION: 0.50,
}

# A confirmed observation is treated as authoritative.
CONFIRMED_CONFIDENCE = 0.99

# At/above this, an observation may drive automated action (subject to all other
# risk gates); below it, the change is advisory only.
ACTIONABLE_THRESHOLD = 0.85


def score_source(source: SourceMeta) -> float:
    if source.confirmed:
        return CONFIRMED_CONFIDENCE
    return BASE_CONFIDENCE.get(source.source_type, 0.5)


def is_actionable(confidence: float, source: SourceMeta) -> bool:
    """Whether an observation is strong enough to act on automatically.

    Confirmed official sources are always actionable; otherwise the confidence
    must clear the threshold. Social is never actionable unless confirmed.
    """

    if source.confirmed:
        return True
    if source.source_type is SourceType.SOCIAL:
        return False
    return confidence >= ACTIONABLE_THRESHOLD
