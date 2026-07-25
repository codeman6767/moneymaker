"""Shared value types and pinned constants for D5A matching.

The scores and the acceptance threshold are the documented values from
``ENTITY_MATCHING.md`` §3.3 / §4.2, stored on every decision so a later change
to a constant does not silently reinterpret an old decision. Scores are fixed
decimals compared with ``>=`` -- there is no floating-point tie-breaking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: Bumped when the matching rules change, so a decision made by an older matcher
#: is identifiable after a rules change (ENTITY_MATCHING.md §7).
MATCHER_VERSION = "d5a-1"

#: Acceptance threshold, stored per decision. ENTITY_MATCHING.md §3.3.
THRESHOLD = 0.85

# Resolution statuses -- identical vocabulary to intel.player_matching.MatchStatus
# and db.normalize.AliasMatchStatus, on purpose.
MATCHED = "matched"
AMBIGUOUS = "ambiguous"
UNMATCHED = "unmatched"

# -- Team / player / venue alias tier scores (strongest first). -------------- #
SCORE_EXACT_PROVIDER_ID = 1.00
SCORE_EXACT_ALIAS = 0.99
SCORE_NORMALIZED_ALIAS = 0.95
SCORE_NORMALIZED_ALIAS_UNSCOPED = 0.90

# Tier labels (also used as the decision ``method`` and candidate ``tier``).
TIER_EXACT_PROVIDER_ID = "exact_provider_id"
TIER_EXACT_ALIAS = "exact_alias"
TIER_NORMALIZED_ALIAS = "normalized_alias"
TIER_NORMALIZED_ALIAS_UNSCOPED = "normalized_alias_unscoped"

# -- Player-specific evidence tiers. ----------------------------------------- #
TIER_TEAM_NAME = "team_normalized_name"
TIER_LEAGUE_NAME = "league_normalized_name"
SCORE_TEAM_NAME = 0.95
SCORE_LEAGUE_NAME = 0.90

# -- Game tier scores. ENTITY_MATCHING.md §4.2. ------------------------------ #
SCORE_OFFICIAL_KEY = 1.00
SCORE_SCHEDULE_EXACT = 0.95
SCORE_SCHEDULE_WINDOW = 0.88
SCORE_SCHEDULE_SWAPPED = 0.85

TIER_OFFICIAL_KEY = "official_key_exact"
TIER_SCHEDULE_EXACT = "schedule_key_exact"
TIER_SCHEDULE_WINDOW = "schedule_key_window"
TIER_SCHEDULE_SWAPPED = "schedule_key_swapped"

# Local-date resolution tiers (ENTITY_MATCHING.md §4 / task §8), strongest first.
LOCALDATE_ACTUAL_VENUE = "actual_venue_tz"
LOCALDATE_PROVIDER_LOCAL = "provider_local_date"
LOCALDATE_HOME_VENUE = "home_venue_tz"
LOCALDATE_UTC_FALLBACK = "utc_fallback"

#: A UTC-fallback local date cannot claim the confidence of a real venue-local
#: match; it caps the achievable game score below an exact schedule match.
UTC_FALLBACK_CONFIDENCE_CAP = 0.88


@dataclass(frozen=True)
class Candidate:
    """One canonical entity considered during a resolution (winner or loser)."""

    entity_id: str
    score: float
    tier: str
    method: Optional[str] = None
    evidence: Optional[str] = None


@dataclass(frozen=True)
class Resolution:
    """The deterministic result of resolving one provider reference.

    ``status`` is ``MATCHED`` / ``AMBIGUOUS`` / ``UNMATCHED``. ``entity_id`` is
    populated only when ``MATCHED``; callers must branch on ``status``. The
    ``candidates`` are already sorted by canonical id, so the decision reads and
    persists identically on every run.
    """

    status: str
    method: str
    score: float
    tier: str
    entity_id: Optional[str] = None
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    reason: Optional[str] = None
    needs_review: bool = False
    #: True when the winning tier resolved through an ``is_ambiguous`` alias row
    #: (drives DQ-MATCH-006). Only meaningful on an AMBIGUOUS result.
    via_ambiguous_alias: bool = False
    #: True when an EXISTING exact provider-id link points at a canonical entity
    #: in the wrong league/sport. A blocking integrity conflict: the crosswalk is
    #: not silently trusted at 1.00, nor silently repaired (drives DQ-MATCH-014 /
    #: DQ-MATCH-015 and a ``rejected`` decision).
    scope_conflict: bool = False
    season_scoped: bool = False
    season_validity_verified: bool = False

    @property
    def is_matched(self) -> bool:
        return self.status == MATCHED

    def outcome(self) -> str:
        """Map the resolution status to an ``entity_match_decisions`` outcome."""

        if self.status == MATCHED:
            return "accepted"
        if self.status == AMBIGUOUS:
            return "ambiguous"
        return "no_candidate"
