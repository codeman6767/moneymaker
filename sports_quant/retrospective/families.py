"""The reviewed Lane-R feature-family taxonomy, as code.

``reconstructed_input_provenance.feature_family`` is a free TEXT column: f018
deliberately does not constrain it, because the family vocabulary is a research
decision that would otherwise need a migration every time it moved. That leaves a
gap the reader must close -- nothing in the database stops a caller certifying
``"lineups"`` and reading it back as a feature.

This module is that gap's fix. It is the code half of the taxonomy in
`HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` §"Per-family classification", and the
reader consults it *before* it looks at provenance at all, so a FORWARD_ONLY
family is refused structurally rather than by caller convention.

Why a closed vocabulary
-----------------------
An unknown family is refused rather than defaulted. The alternative -- treat
anything unrecognized as EVENT_DERIVED, or as forbidden-but-warn -- means a typo
in a family name silently changes what leakage rules apply to it. A family this
build does not know is a family whose availability argument nobody has reviewed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final, Mapping

from .provenance import AvailabilityBasis, RetrospectiveProvenanceError

__all__ = [
    "FEATURE_FAMILIES",
    "FamilyClass",
    "FeatureFamily",
    "ForwardOnlyFamilyError",
    "UnknownFeatureFamilyError",
    "lookup_family",
]


class UnknownFeatureFamilyError(RetrospectiveProvenanceError):
    """A family name this build has no reviewed classification for."""


class ForwardOnlyFamilyError(RetrospectiveProvenanceError):
    """A FORWARD_ONLY family was requested from the retrospective lane."""


class FamilyClass(str, enum.Enum):
    """How a family may be used, per the reviewed taxonomy.

    ``FORWARD_ONLY`` exists here -- unlike in ``AvailabilityBasis``, which has no
    such member -- precisely so the reader can *name* the refusal. The basis enum
    describes how something IS available; this enum describes whether it may be
    read at all.
    """

    #: Timeless identity. Admissible, and NOT wall-clock gated.
    STATIC_IDENTITY = "static_identity"
    #: Derived from a completed prior event, gated by completion + rule lag.
    EVENT_DERIVED = "event_derived"
    #: A provider-stamped historical snapshot, gated by that stamp.
    VERSIONED_SNAPSHOT = "versioned_snapshot"
    #: The outcome. Returnable ONLY when explicitly asked for as a label.
    LABEL_ONLY = "label_only"
    #: No trustworthy retrospective availability evidence. Never returnable.
    FORWARD_ONLY = "forward_only"


#: Which availability bases a family may legitimately be certified under. A
#: certification whose basis is not in this set is a contradiction between the
#: family's reviewed nature and its claimed availability argument, and fails
#: closed rather than being believed.
_ADMISSIBLE_BASES: Final[dict[FamilyClass, frozenset[AvailabilityBasis]]] = {
    FamilyClass.STATIC_IDENTITY: frozenset({AvailabilityBasis.STATIC_IDENTITY}),
    FamilyClass.EVENT_DERIVED: frozenset({AvailabilityBasis.EVENT_DERIVED}),
    FamilyClass.VERSIONED_SNAPSHOT: frozenset({AvailabilityBasis.VERSIONED_SNAPSHOT}),
    FamilyClass.LABEL_ONLY: frozenset(),          # a label carries no basis
    FamilyClass.FORWARD_ONLY: frozenset(),        # unreachable by construction
}


@dataclass(frozen=True)
class FeatureFamily:
    """One reviewed family and the rules that follow from its classification."""

    name: str
    classification: FamilyClass
    #: ``None`` means league-neutral. Otherwise the family exists for one league
    #: only, and asking for it in the other league is a category error.
    league_id: str | None
    note: str

    @property
    def admissible_bases(self) -> frozenset[AvailabilityBasis]:
        return _ADMISSIBLE_BASES[self.classification]

    @property
    def is_feature(self) -> bool:
        """May this family ever be returned as a predictive input?"""

        return self.classification in (
            FamilyClass.STATIC_IDENTITY,
            FamilyClass.EVENT_DERIVED,
            FamilyClass.VERSIONED_SNAPSHOT,
        )


def _f(name: str, classification: FamilyClass, note: str,
       league_id: str | None = None) -> FeatureFamily:
    return FeatureFamily(name=name, classification=classification,
                         league_id=league_id, note=note)


#: The reviewed taxonomy. Adding a family is a code change, reviewed as such.
FEATURE_FAMILIES: Final[Mapping[str, FeatureFamily]] = {
    f.name: f for f in (
        # -- league-neutral ---------------------------------------------------
        _f("static_identity", FamilyClass.STATIC_IDENTITY,
           "Stable official game/team/player ids, resolved through the TEAM-A "
           "crosswalk under a corpus-scoped G5 audit. Timeless by nature."),
        _f("target_schedule_anchor", FamilyClass.VERSIONED_SNAPSHOT,
           "The target game's existence anchored to a market snapshot at or "
           "before the cutoff, replacing the local schedule gate."),
        _f("prior_results", FamilyClass.EVENT_DERIVED,
           "Outcomes of PRIOR completed games. Never the target's own."),
        _f("team_rolling_stats", FamilyClass.EVENT_DERIVED,
           "Self-derived from prior completed per-game events; never a "
           "season-to-date aggregate that could contain the target."),
        _f("rest_schedule_density", FamilyClass.EVENT_DERIVED,
           "Calendar arithmetic over the prior schedule; back-to-backs."),
        _f("sportsbook_moneyline", FamilyClass.VERSIONED_SNAPSHOT,
           "Provider-stamped odds snapshot at or before the cutoff."),
        _f("kalshi_market", FamilyClass.VERSIONED_SNAPSHOT,
           "Candlestick at or before the cutoff. Depth is NOT reconstructable "
           "(gate G2); this family is availability-plausible, not proven."),
        _f("final_result", FamilyClass.LABEL_ONLY,
           "The settled outcome. A target, never a feature."),

        # -- MLB --------------------------------------------------------------
        _f("pitcher_rolling_stats", FamilyClass.EVENT_DERIVED,
           "Prior-appearance derived.", "lg_mlb"),
        _f("batter_rolling_stats", FamilyClass.EVENT_DERIVED,
           "Prior-appearance derived.", "lg_mlb"),
        _f("bullpen_prior_usage", FamilyClass.EVENT_DERIVED,
           "PRIOR-game appearances only. Same-day bullpen availability is "
           "forward-only and is a different family.", "lg_mlb"),
        _f("weather_forecast", FamilyClass.VERSIONED_SNAPSHOT,
           "Archived forecast run at or before the cutoff. Archive depth is "
           "shallower than the odds archive (gate G3).", "lg_mlb"),
        _f("probable_pitchers", FamilyClass.FORWARD_ONLY,
           "Historical probables are not reconstructable; what exists is "
           "collection-time state.", "lg_mlb"),

        # -- NBA --------------------------------------------------------------
        _f("player_rolling_stats", FamilyClass.EVENT_DERIVED,
           "Prior-game derived.", "lg_nba"),
        _f("advanced_rolling_stats", FamilyClass.EVENT_DERIVED,
           "From prior games only.", "lg_nba"),
        _f("plays_derived_stats", FamilyClass.EVENT_DERIVED,
           "Derived from preserved play events of prior games.", "lg_nba"),

        # -- forward-only, both leagues ---------------------------------------
        _f("lineups", FamilyClass.FORWARD_ONLY,
           "The merged March lineups are AUGUST-observed. This is the exact "
           "case that made Lane R necessary; it may never be a pregame feature."),
        _f("injuries", FamilyClass.FORWARD_ONLY,
           "Collection-time state with no retrospective availability evidence."),
        _f("rosters", FamilyClass.FORWARD_ONLY,
           "Current state only."),
    )
}


def lookup_family(name: str) -> FeatureFamily:
    """Resolve a family name, failing closed on anything unreviewed."""

    try:
        return FEATURE_FAMILIES[name]
    except KeyError:
        raise UnknownFeatureFamilyError(
            f"feature family {name!r} has no reviewed retrospective "
            f"classification in this build (known: {sorted(FEATURE_FAMILIES)}). "
            "An unclassified family is one whose availability argument nobody "
            "has reviewed, so it is refused rather than assumed safe."
        ) from None
