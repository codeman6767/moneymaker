"""Frozen policy registries for Stage-A evidence lanes.

Why a lane's ``digest_policy_version`` is not enough on its own
--------------------------------------------------------------
A lane binding records WHICH digest policy produced its evidence fingerprint.
But the live source-table selection in :mod:`sports_quant.retrospective.sources`
is derived from MUTABLE software registries (``PROVIDER_LEAGUES``,
``REGISTERED_LINKING_PROVIDERS``, ``_DIGEST_COLUMNS``). Registering a linking
provider, adding a table, or adding an E1 lane later would change what those
registries resolve to -- and an accepted lane recorded months earlier would then
silently re-verify under a DIFFERENT source contract while still carrying the
same version string.

That is a reproducibility hole: the version string would name a policy whose
meaning had changed underneath it.

This module closes it by pinning each frozen policy version to an EXACT table
and column set, captured as data rather than derived at call time. A frozen
policy is immutable by construction. Changing the source contract requires
minting a NEW version here; it can never redefine an old one. The paired CI
invariant (``test_v22_stage_a_provenance.py``) asserts the live registry still
agrees with the frozen snapshot for the current version, so an edit to
``sources.py`` that would change an existing policy's meaning fails the build
instead of silently rewriting history.

This module performs no network and no database I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping

from .historical_events_projection import PROJECTION_POLICY_VERSION


class PolicyRegistryError(RuntimeError):
    """An unknown or unusable policy version. Always fails closed."""


@dataclass(frozen=True)
class FrozenDigestPolicy:
    """An immutable snapshot of exactly what one digest policy version covers."""

    version: str
    #: table -> the exact ordered semantic columns digested for that table.
    tables: Mapping[str, tuple[str, ...]]

    def table_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.tables))

    def columns_for(self, table: str) -> tuple[str, ...]:
        try:
            return self.tables[table]
        except KeyError:
            raise PolicyRegistryError(
                f"table {table!r} is not part of frozen digest policy "
                f"{self.version!r}; a changed source set requires a NEW policy "
                f"version, never a redefinition of this one."
            ) from None


#: The OFFICIAL-provider source contract as it stood when v22 froze it. These are
#: the three tables every existing corpus digest was computed over, captured
#: verbatim so that a v18/v19/v20/v21 corpus stays byte-identically verifiable.
OFFICIAL_SOURCE_DIGEST_POLICY_V1: Final = FrozenDigestPolicy(
    version="official-source-v1",
    tables={
        "game_schedule_snapshots": (
            "provider", "provider_game_id", "season", "game_type",
            "game_date_local", "scheduled_start", "home_provider_team_id",
            "away_provider_team_id", "venue_provider_id", "mapped_status",
            "game_number", "doubleheader_code", "reschedule_info", "observed_at",
        ),
        "provider_player_identity_snapshots": (
            "provider", "provider_player_id", "league_id", "full_name",
            "normalized_name", "suffix", "first_name", "last_name", "birth_date",
            "position", "provider_team_id", "observed_at",
        ),
        "provider_team_identity_snapshots": (
            "provider", "provider_team_id", "league_id", "full_name",
            "normalized_name", "abbreviation", "city", "nickname", "observed_at",
        ),
    },
)

#: The MARKET-EVENTS (E0) lane contract. `raw_response_id` is deliberately absent:
#: it is database-local, so digesting it would make a transported corpus hash
#: differently from the one it was copied from. `observed_at` is likewise absent,
#: matching the accepted v20 decision that it is not semantic observation content.
MARKET_EVENTS_E0_DIGEST_POLICY_V1: Final = FrozenDigestPolicy(
    version="market-events-e0-v1",
    tables={
        "historical_market_event_observations": (
            "league_id", "provider", "namespace_generation", "sport_key",
            "provider_event_id", "requested_at_bucket",
            "provider_snapshot_timestamp", "commence_time", "home_team_raw",
            "away_team_raw", "observation_content_hash",
        ),
    },
)

_FROZEN_DIGEST_POLICIES: Final[dict[str, FrozenDigestPolicy]] = {
    OFFICIAL_SOURCE_DIGEST_POLICY_V1.version: OFFICIAL_SOURCE_DIGEST_POLICY_V1,
    MARKET_EVENTS_E0_DIGEST_POLICY_V1.version: MARKET_EVENTS_E0_DIGEST_POLICY_V1,
}

#: Which frozen digest policy each evidence lane uses.
LANE_DIGEST_POLICIES: Final[dict[str, str]] = {
    "official_identity": OFFICIAL_SOURCE_DIGEST_POLICY_V1.version,
    "market_events_e0": MARKET_EVENTS_E0_DIGEST_POLICY_V1.version,
}


def resolve_digest_policy(version: str) -> FrozenDigestPolicy:
    """Resolve a frozen digest policy, or refuse.

    No normalization: a case variant or padded string is a different version and
    is refused rather than repaired into one that happens to exist.
    """

    try:
        return _FROZEN_DIGEST_POLICIES[version]
    except (KeyError, TypeError):
        known = ", ".join(sorted(_FROZEN_DIGEST_POLICIES))
        raise PolicyRegistryError(
            f"unknown digest policy version {version!r}; frozen versions are: "
            f"{known}. Refusing to verify a lane under an undeclared source "
            f"contract."
        ) from None


def digest_policy_for_lane(evidence_lane: str) -> FrozenDigestPolicy:
    try:
        version = LANE_DIGEST_POLICIES[evidence_lane]
    except (KeyError, TypeError):
        known = ", ".join(sorted(LANE_DIGEST_POLICIES))
        raise PolicyRegistryError(
            f"unknown evidence lane {evidence_lane!r}; known lanes are: {known}"
        ) from None
    return resolve_digest_policy(version)


#: Projection policy versions this build can certify evidence under. A lane whose
#: acquisitions name anything else is refused rather than assumed compatible --
#: the reviewed failure mode is "manifest says v1, acquisition says v2, verifier
#: assumes v1".
KNOWN_PROJECTION_POLICY_VERSIONS: Final[frozenset[str]] = frozenset(
    {PROJECTION_POLICY_VERSION})

#: Acquisition policy versions this build can certify under.
STAGE_A_ACQUISITION_POLICY_VERSION: Final = "stage-a-acquisition-v1"
KNOWN_ACQUISITION_POLICY_VERSIONS: Final[frozenset[str]] = frozenset(
    {STAGE_A_ACQUISITION_POLICY_VERSION})

#: The probe-registration contract version.
STAGE_A_PROBE_POLICY_VERSION: Final = "stage-a-probe-v1"
KNOWN_PROBE_POLICY_VERSIONS: Final[frozenset[str]] = frozenset(
    {STAGE_A_PROBE_POLICY_VERSION})


def require_projection_policy(version: str) -> str:
    if version not in KNOWN_PROJECTION_POLICY_VERSIONS:
        known = ", ".join(sorted(KNOWN_PROJECTION_POLICY_VERSIONS))
        raise PolicyRegistryError(
            f"unknown projection policy version {version!r}; known versions are: "
            f"{known}")
    return version


def require_acquisition_policy(version: str) -> str:
    if version not in KNOWN_ACQUISITION_POLICY_VERSIONS:
        known = ", ".join(sorted(KNOWN_ACQUISITION_POLICY_VERSIONS))
        raise PolicyRegistryError(
            f"unknown acquisition policy version {version!r}; known versions are: "
            f"{known}")
    return version


def require_probe_policy(version: str) -> str:
    if version not in KNOWN_PROBE_POLICY_VERSIONS:
        known = ", ".join(sorted(KNOWN_PROBE_POLICY_VERSIONS))
        raise PolicyRegistryError(
            f"unknown probe policy version {version!r}; known versions are: {known}")
    return version
