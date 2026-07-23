"""Typed provider capabilities, tiers, declarations, and tier-error classification.

Phase D declares what each provider can supply as **typed data**, never inferring
it from a provider's name or from mere key possession. This is the mechanism that
keeps the plan honest about the BALLDONTLIE tiers: a valid ``NBA_DATA_API_KEY``
grants whatever the *account tier* allows, and a GOAT-only endpoint accessed on a
lower tier is a **capability unavailable for the current subscription tier**, not
an invalid key or an application bug.

Nothing here performs I/O. The declarations are static, deterministic, and
re-verified against live docs by the ``provider-audit`` command
(`PHASE_D_IMPLEMENTATION_PLAN.md` §10) before any backfill.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Optional


class ProviderCapability(str, enum.Enum):
    """A kind of data a provider may or may not supply."""

    TEAMS = "teams"
    PLAYERS = "players"
    GAMES = "games"
    SCHEDULES = "schedules"
    GAME_RESULTS = "game_results"
    TEAM_STATISTICS = "team_statistics"
    PLAYER_STATISTICS = "player_statistics"
    ADVANCED_STATISTICS = "advanced_statistics"
    INNING_LINES = "inning_lines"
    QUARTER_LINES = "quarter_lines"
    INJURIES = "injuries"
    PROBABLE_PITCHERS = "probable_pitchers"
    LINEUPS = "lineups"
    CONFIRMED_PREGAME_STARTERS = "confirmed_pregame_starters"
    PLAYS = "plays"
    SUBSTITUTIONS = "substitutions"
    CORRECTION_TIMESTAMPS = "correction_timestamps"
    VENUES = "venues"
    HISTORICAL_DEPTH = "historical_depth"
    LIVE_AVAILABILITY = "live_availability"


class CapabilityState(str, enum.Enum):
    """The availability of a capability for a provider at a given tier.

    ``UNKNOWN_UNTIL_AUDITED`` is the honest default before ``provider-audit`` has
    verified a claim against live docs; a missing capability is recorded as one
    of these states, never fabricated as supported.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PAID_TIER_REQUIRED = "paid_tier_required"
    BEST_EFFORT = "best_effort"
    UNAVAILABLE = "unavailable"
    UNKNOWN_UNTIL_AUDITED = "unknown_until_audited"
    PROVIDER_HISTORY_LIMITED = "provider_history_limited"


class BalldontlieTier(str, enum.Enum):
    """BALLDONTLIE account tiers. The selected project tier is GOAT."""

    FREE = "free"
    ALL_STAR = "all_star"
    GOAT = "goat"


#: The tier the Phase D NBA path is designed for. Never inferred from a key.
SELECTED_BALLDONTLIE_TIER: BalldontlieTier = BalldontlieTier.GOAT


# --------------------------------------------------------------------------- #
# Provider names (stable strings recorded in the corpus)
# --------------------------------------------------------------------------- #
PROVIDER_MLB_STATSAPI = "mlb_statsapi"
PROVIDER_BALLDONTLIE = "balldontlie"
PROVIDER_NWS = "nws"
PROVIDER_OPEN_METEO = "open_meteo"


@dataclass(frozen=True)
class CapabilityDeclaration:
    """One provider's declared capabilities at a given tier.

    ``tier`` is ``None`` for providers that are not tiered (StatsAPI, NWS,
    Open-Meteo). ``notes`` explains a non-obvious state (e.g. why a licensing
    limitation is recorded as a note, not a capability).
    """

    provider: str
    tier: Optional[str]
    states: Mapping[ProviderCapability, CapabilityState]
    notes: Optional[Mapping[ProviderCapability, str]] = None

    def state(self, capability: ProviderCapability) -> CapabilityState:
        """The declared state, or ``UNKNOWN_UNTIL_AUDITED`` if undeclared."""

        return self.states.get(capability, CapabilityState.UNKNOWN_UNTIL_AUDITED)

    def is_available(self, capability: ProviderCapability) -> bool:
        """Whether an ingestor may request this capability now.

        ``SUPPORTED``, ``BEST_EFFORT`` and ``PROVIDER_HISTORY_LIMITED`` are
        requestable (the caller records the resulting state); everything else --
        including ``PAID_TIER_REQUIRED`` and ``UNAVAILABLE`` -- is not.
        """

        return self.state(capability) in _REQUESTABLE_STATES


_REQUESTABLE_STATES = frozenset(
    {
        CapabilityState.SUPPORTED,
        CapabilityState.BEST_EFFORT,
        CapabilityState.PROVIDER_HISTORY_LIMITED,
    }
)

_S = CapabilityState
_C = ProviderCapability


# --------------------------------------------------------------------------- #
# MLB StatsAPI declaration
# --------------------------------------------------------------------------- #
MLB_STATSAPI_DECLARATION = CapabilityDeclaration(
    provider=PROVIDER_MLB_STATSAPI,
    tier=None,
    states={
        _C.TEAMS: _S.SUPPORTED,
        _C.PLAYERS: _S.SUPPORTED,
        _C.GAMES: _S.SUPPORTED,
        _C.SCHEDULES: _S.SUPPORTED,
        _C.GAME_RESULTS: _S.SUPPORTED,
        _C.TEAM_STATISTICS: _S.SUPPORTED,
        _C.PLAYER_STATISTICS: _S.SUPPORTED,
        _C.INNING_LINES: _S.SUPPORTED,
        _C.PROBABLE_PITCHERS: _S.SUPPORTED,
        _C.LINEUPS: _S.SUPPORTED,  # posted lineups (not pregame-confirmed)
        _C.VENUES: _S.SUPPORTED,
        _C.HISTORICAL_DEPTH: _S.SUPPORTED,
        _C.LIVE_AVAILABILITY: _S.SUPPORTED,
        _C.CONFIRMED_PREGAME_STARTERS: _S.UNAVAILABLE,
        # No explicit correction timestamps; corrections are detected via changed
        # content hashes on append-only snapshots -> best-effort.
        _C.CORRECTION_TIMESTAMPS: _S.BEST_EFFORT,
        _C.QUARTER_LINES: _S.UNSUPPORTED,  # baseball
        _C.INJURIES: _S.BEST_EFFORT,  # IL moves via /transactions; no clean feed
        _C.PLAYS: _S.SUPPORTED,
        _C.SUBSTITUTIONS: _S.UNSUPPORTED,
    },
)


# --------------------------------------------------------------------------- #
# BALLDONTLIE declarations (per tier)
# --------------------------------------------------------------------------- #
BALLDONTLIE_GOAT_DECLARATION = CapabilityDeclaration(
    provider=PROVIDER_BALLDONTLIE,
    tier=BalldontlieTier.GOAT.value,
    states={
        _C.TEAMS: _S.SUPPORTED,
        _C.PLAYERS: _S.SUPPORTED,
        _C.GAMES: _S.SUPPORTED,
        _C.SCHEDULES: _S.SUPPORTED,
        _C.GAME_RESULTS: _S.SUPPORTED,
        _C.PLAYER_STATISTICS: _S.SUPPORTED,
        _C.TEAM_STATISTICS: _S.SUPPORTED,  # derivable from box scores
        _C.ADVANCED_STATISTICS: _S.SUPPORTED,  # /nba/v1/stats/advanced (GOAT)
        _C.INJURIES: _S.SUPPORTED,
        _C.PLAYS: _S.SUPPORTED,
        _C.QUARTER_LINES: _S.SUPPORTED,  # derivable from box/plays
        _C.LINEUPS: _S.BEST_EFFORT,  # "when available"
        _C.CONFIRMED_PREGAME_STARTERS: _S.UNAVAILABLE,
        _C.CORRECTION_TIMESTAMPS: _S.UNSUPPORTED,
        _C.HISTORICAL_DEPTH: _S.PROVIDER_HISTORY_LIMITED,
        _C.LIVE_AVAILABILITY: _S.BEST_EFFORT,
        _C.SUBSTITUTIONS: _S.BEST_EFFORT,  # from plays where present
        _C.INNING_LINES: _S.UNSUPPORTED,  # basketball
        _C.PROBABLE_PITCHERS: _S.UNSUPPORTED,  # basketball
        _C.VENUES: _S.BEST_EFFORT,
    },
)

# Capabilities that only GOAT unlocks -> paid_tier_required on lower tiers.
_GOAT_ONLY_CAPS = (
    _C.PLAYER_STATISTICS,
    _C.TEAM_STATISTICS,
    _C.ADVANCED_STATISTICS,
    _C.PLAYS,
    _C.QUARTER_LINES,
    _C.LINEUPS,
    _C.SUBSTITUTIONS,
)
# ALL-STAR additionally unlocks player stats + injuries; box/plays/lineups stay paid.
_ALL_STAR_UNLOCKS = (_C.PLAYER_STATISTICS, _C.INJURIES)


def _lower_tier_states(
    *, unlocked: tuple[ProviderCapability, ...]
) -> dict[ProviderCapability, CapabilityState]:
    """GOAT states with GOAT-only caps demoted to PAID_TIER_REQUIRED.

    ``unlocked`` names the capabilities this lower tier *does* grant (kept at
    their GOAT state); every other GOAT-only capability becomes
    ``PAID_TIER_REQUIRED``.
    """

    states = dict(BALLDONTLIE_GOAT_DECLARATION.states)
    for cap in _GOAT_ONLY_CAPS:
        if cap not in unlocked:
            states[cap] = CapabilityState.PAID_TIER_REQUIRED
    # Injuries: GOAT-and-ALL-STAR only.
    if _C.INJURIES not in unlocked:
        states[_C.INJURIES] = CapabilityState.PAID_TIER_REQUIRED
    return states


BALLDONTLIE_ALL_STAR_DECLARATION = CapabilityDeclaration(
    provider=PROVIDER_BALLDONTLIE,
    tier=BalldontlieTier.ALL_STAR.value,
    states=_lower_tier_states(unlocked=_ALL_STAR_UNLOCKS),
)

BALLDONTLIE_FREE_DECLARATION = CapabilityDeclaration(
    provider=PROVIDER_BALLDONTLIE,
    tier=BalldontlieTier.FREE.value,
    states=_lower_tier_states(unlocked=()),
)

_BALLDONTLIE_BY_TIER: dict[BalldontlieTier, CapabilityDeclaration] = {
    BalldontlieTier.GOAT: BALLDONTLIE_GOAT_DECLARATION,
    BalldontlieTier.ALL_STAR: BALLDONTLIE_ALL_STAR_DECLARATION,
    BalldontlieTier.FREE: BALLDONTLIE_FREE_DECLARATION,
}


def balldontlie_declaration(tier: BalldontlieTier) -> CapabilityDeclaration:
    """The BALLDONTLIE capability declaration for a tier."""

    return _BALLDONTLIE_BY_TIER[tier]


# --------------------------------------------------------------------------- #
# Weather declarations
# --------------------------------------------------------------------------- #
NWS_DECLARATION = CapabilityDeclaration(
    provider=PROVIDER_NWS,
    tier=None,
    states={
        # Weather forecasts/observations are not in the ProviderCapability
        # catalogue as game data; NWS's relevant states are recorded on
        # LIVE_AVAILABILITY (US forecasts/observations) and HISTORICAL_DEPTH.
        _C.LIVE_AVAILABILITY: _S.SUPPORTED,  # US forecasts + observations
        _C.HISTORICAL_DEPTH: _S.BEST_EFFORT,  # observations; no forecast archive
    },
    notes={_C.LIVE_AVAILABILITY: "US only; non-US locations are UNAVAILABLE"},
)

OPEN_METEO_DECLARATION = CapabilityDeclaration(
    provider=PROVIDER_OPEN_METEO,
    tier=None,
    states={
        _C.LIVE_AVAILABILITY: _S.SUPPORTED,  # forecasts (global)
        # Historical forecasts / previous model runs + reanalysis observations.
        _C.HISTORICAL_DEPTH: _S.SUPPORTED,
    },
    notes={
        _C.LIVE_AVAILABILITY: (
            "commercial usage may require a paid plan -- a licensing limitation, "
            "not a technical capability"
        )
    },
)


# --------------------------------------------------------------------------- #
# Provider-tier error classification
# --------------------------------------------------------------------------- #
class ProviderErrorKind(str, enum.Enum):
    """Distinct, non-overlapping classifications of a provider failure.

    Each kind is separate on purpose. In particular ``TIER_RESTRICTED`` (a
    capability the current subscription tier does not grant) is distinct from
    ``AUTHENTICATION``/``INVALID_KEY`` (a bad/absent key) and from ``FORBIDDEN``
    (a generic 403 with no plan/tier evidence): a tier restriction is only ever
    inferred from explicit plan/subscription evidence, never assumed from a bare
    403, and never mislabelled as an invalid key, network fault, or app bug.
    """

    AUTHENTICATION = "authentication"
    INVALID_KEY = "invalid_key"
    TIER_RESTRICTED = "tier_restricted"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    NETWORK = "network"
    SERVER = "server"
    INVALID_PAYLOAD = "invalid_payload"
    PARSER = "parser"
    UNSUPPORTED = "unsupported"
    UNEXPECTED = "unexpected"


TIER_UNAVAILABLE_MESSAGE = "capability unavailable for current subscription tier"

#: **Explicit multi-token phrases** (and documented structured error codes) in a
#: sanitized response body that specifically indicate a subscription/tier
#: restriction -- as opposed to a generic forbidden. Deliberately NOT bare single
#: tokens like "plan", "tier", "upgrade" or "subscription": those appear in
#: unrelated 403 messages and produced false positives, so a broad word alone is
#: no longer sufficient evidence. Matched case-insensitively as substrings.
_TIER_EVIDENCE = (
    # Explicit upgrade / subscription-access phrasing.
    "upgrade required",
    "upgrade your plan",
    "upgrade to access",
    "please upgrade",
    "requires a higher",          # "requires a higher plan tier"
    "higher plan",
    "higher tier",
    "higher subscription",
    "subscription tier",
    "subscription plan",
    "paid plan",
    "paid subscription",
    "not included in your",       # "...your plan/subscription"
    "not available on your plan",
    "not available on your current plan",
    "not available on your subscription",
    "not in your subscription",
    "requires the goat",
    "goat plan",
    "goat tier",
    "all-star plan",
    "all star plan",
    "all-star tier",
    "upgrade to the goat",
    # Documented structured error codes / machine fields.
    "tier_restricted",
    "tier-restricted",
    "upgrade_required",
    "subscription_required",
    "plan_required",
    "insufficient_tier",
)

#: Words indicating the key itself is bad/invalid (a subtype of authentication).
_INVALID_KEY_EVIDENCE = ("invalid api key", "invalid key", "invalid authorization", "bad api key")


def _has_tier_evidence(body_snippet: str) -> bool:
    """Whether a sanitized body carries explicit plan/tier-restriction evidence.

    Requires one of the curated :data:`_TIER_EVIDENCE` phrases/codes; a broad,
    unrelated use of a single word such as "plan" is intentionally not enough.
    """

    lowered = body_snippet.lower()
    return any(token in lowered for token in _TIER_EVIDENCE)


def classify_http_status(
    status_code: int, *, body_snippet: str = "", provider: Optional[str] = None
) -> ProviderErrorKind:
    """Classify an HTTP failure into a :class:`ProviderErrorKind`.

    ``401`` is authentication (``INVALID_KEY`` when the body says so). A ``403``
    is classified as ``TIER_RESTRICTED`` **only** when the sanitized body carries
    explicit plan/tier/subscription evidence (:data:`_TIER_EVIDENCE`) -- a bare
    403 with no such evidence is ``FORBIDDEN``, never ``paid_tier_required``. This
    holds for **every** provider, including BALLDONTLIE: a GOAT-only endpoint on a
    lower tier answers 403 with a plan message and is TIER_RESTRICTED, while a
    generic 403 is FORBIDDEN. ``body_snippet`` must already be sanitized by the
    caller (no secret ever reaches this function's decisions).
    """

    if status_code == 401:
        lowered = body_snippet.lower()
        if any(token in lowered for token in _INVALID_KEY_EVIDENCE):
            return ProviderErrorKind.INVALID_KEY
        return ProviderErrorKind.AUTHENTICATION
    if status_code == 403:
        if _has_tier_evidence(body_snippet):
            return ProviderErrorKind.TIER_RESTRICTED
        return ProviderErrorKind.FORBIDDEN
    if status_code == 429:
        return ProviderErrorKind.RATE_LIMITED
    if status_code == 404:
        return ProviderErrorKind.NOT_FOUND
    if 500 <= status_code <= 599:
        return ProviderErrorKind.SERVER
    return ProviderErrorKind.UNEXPECTED


def is_tier_restriction(kind: ProviderErrorKind) -> bool:
    """Whether a failure means a capability is unavailable at the current tier.

    A tier restriction must let unrelated supported capabilities continue.
    """

    return kind is ProviderErrorKind.TIER_RESTRICTED
