"""The committed TEAM-A franchise attestation map, and its digests.

What this is
------------
A **source-controlled constant** stating which canonical franchise each official
provider team id denotes. It is the *only* runtime authority for retrospective
team identity: the crosswalk generator performs an exact dictionary lookup here
and consults no name, alias, abbreviation or nickname at read time.

Why that is not "fuzzy matching in a file"
------------------------------------------
The distinction the architecture turns on is **when** labels are read. The
entries below were established once, offline, by a curation diagnostic
(`sports_quant.retrospective.curation`) whose output a human reviewed and froze
here as a diff. At build time nothing is inferred — the map either contains the
exact provider key or the id is **unresolved**. Re-running the diagnostic is a
*verification* step that must reproduce this file exactly; it is never a
resolution step.

What TEAM-A does and does not claim
-----------------------------------
It curates the **denotation** of an official provider franchise id. It does
**not** prove provider-id permanence or non-reuse: the identity-audit review
recorded that same-league team reuse is undetectable from label evidence, and
attestation inherits, and cannot exceed, that detection power. Reports say
*attested*, never *verified* or *guaranteed stable*.

The T1 invariant
----------------
**One provider key denotes exactly one canonical franchise.** The converse is
**not** required: several provider ids may denote one franchise, which is what a
provider-id transition looks like. The 30 ↔ 30 shape below is an *observation
about the two one-month 2026 corpora*, never a rule — see
``describe_map_shape``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Optional

from streaming.event_envelope import canonical_json

from ..db.ids import team_id
from ..db.seeds.loader import alias_specs
from ..db.seeds.mlb_teams import MLB_TEAMS
from ..db.seeds.nba_teams import NBA_TEAMS
from .provenance import EntityType, ProviderNamespace, RetrospectiveProvenanceError

__all__ = [
    "MAP_FORMAT_VERSION",
    "TEAM_ATTESTATIONS",
    "TEAM_ATTESTATION_POLICY_VERSION",
    "AttestationError",
    "TeamAttestation",
    "attestation_map_digest",
    "attested_canonical_team",
    "canonical_team_seed_digest",
    "describe_map_shape",
]


class AttestationError(RetrospectiveProvenanceError):
    """The committed attestation map cannot be used as asked."""


#: Serialization/format version of the map itself. Bumping it changes the map
#: digest even when no entry changed, which is correct: the *representation* the
#: digest was computed over would be different.
MAP_FORMAT_VERSION: Final = "team-a-map-v1"

#: The curation rules under which these entries were approved. Distinct from the
#: player bootstrap policy and from the game bootstrap policy, because the
#: guarantees differ; a material change to the curation rules requires a new
#: version rather than a silent reinterpretation of committed entries.
TEAM_ATTESTATION_POLICY_VERSION: Final = "g5-team-attestation-v1"


@dataclass(frozen=True)
class TeamAttestation:
    """One reviewed statement: this provider franchise id denotes this franchise."""

    league_id: str
    provider: str
    namespace_generation: str
    provider_team_id: str
    canonical_team_id: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.league_id, self.provider, self.namespace_generation,
                self.provider_team_id)


T = TeamAttestation

#: The committed map.
#:
#: Every entry was established from evidence actually present in an audited
#: corpus: the provider-written name matched exactly one canonical seed alias by
#: exact normalized equality, and at least one secondary corroborating attribute
#: (abbreviation or nickname) from the same observation agreed. Nothing here is
#: speculative -- ids never observed are deliberately absent, and a wider-window
#: reconstruction must surface them explicitly rather than have them guessed.
TEAM_ATTESTATIONS: Final[tuple[TeamAttestation, ...]] = (
    # --- MLB, mlb_statsapi v1 (30 official ids observed in MLB June 2026) ---
    T("lg_mlb", "mlb_statsapi", "v1", "108", "tm_mlb_laa"),  # Los Angeles Angels
    T("lg_mlb", "mlb_statsapi", "v1", "109", "tm_mlb_ari"),  # Arizona Diamondbacks
    T("lg_mlb", "mlb_statsapi", "v1", "110", "tm_mlb_bal"),  # Baltimore Orioles
    T("lg_mlb", "mlb_statsapi", "v1", "111", "tm_mlb_bos"),  # Boston Red Sox
    T("lg_mlb", "mlb_statsapi", "v1", "112", "tm_mlb_chc"),  # Chicago Cubs
    T("lg_mlb", "mlb_statsapi", "v1", "113", "tm_mlb_cin"),  # Cincinnati Reds
    T("lg_mlb", "mlb_statsapi", "v1", "114", "tm_mlb_cle"),  # Cleveland Guardians
    T("lg_mlb", "mlb_statsapi", "v1", "115", "tm_mlb_col"),  # Colorado Rockies
    T("lg_mlb", "mlb_statsapi", "v1", "116", "tm_mlb_det"),  # Detroit Tigers
    T("lg_mlb", "mlb_statsapi", "v1", "117", "tm_mlb_hou"),  # Houston Astros
    T("lg_mlb", "mlb_statsapi", "v1", "118", "tm_mlb_kc"),   # Kansas City Royals
    T("lg_mlb", "mlb_statsapi", "v1", "119", "tm_mlb_lad"),  # Los Angeles Dodgers
    # 120 is the Expos->Nationals franchise; the seed carries "Montreal Expos"
    # as a historical alias of tm_mlb_wsh, so one franchise, not two.
    T("lg_mlb", "mlb_statsapi", "v1", "120", "tm_mlb_wsh"),  # Washington Nationals
    T("lg_mlb", "mlb_statsapi", "v1", "121", "tm_mlb_nym"),  # New York Mets
    T("lg_mlb", "mlb_statsapi", "v1", "133", "tm_mlb_ath"),  # Athletics
    T("lg_mlb", "mlb_statsapi", "v1", "134", "tm_mlb_pit"),  # Pittsburgh Pirates
    T("lg_mlb", "mlb_statsapi", "v1", "135", "tm_mlb_sd"),   # San Diego Padres
    T("lg_mlb", "mlb_statsapi", "v1", "136", "tm_mlb_sea"),  # Seattle Mariners
    T("lg_mlb", "mlb_statsapi", "v1", "137", "tm_mlb_sf"),   # San Francisco Giants
    T("lg_mlb", "mlb_statsapi", "v1", "138", "tm_mlb_stl"),  # St. Louis Cardinals
    T("lg_mlb", "mlb_statsapi", "v1", "139", "tm_mlb_tb"),   # Tampa Bay Rays
    T("lg_mlb", "mlb_statsapi", "v1", "140", "tm_mlb_tex"),  # Texas Rangers
    T("lg_mlb", "mlb_statsapi", "v1", "141", "tm_mlb_tor"),  # Toronto Blue Jays
    T("lg_mlb", "mlb_statsapi", "v1", "142", "tm_mlb_min"),  # Minnesota Twins
    T("lg_mlb", "mlb_statsapi", "v1", "143", "tm_mlb_phi"),  # Philadelphia Phillies
    T("lg_mlb", "mlb_statsapi", "v1", "144", "tm_mlb_atl"),  # Atlanta Braves
    T("lg_mlb", "mlb_statsapi", "v1", "145", "tm_mlb_cws"),  # Chicago White Sox
    T("lg_mlb", "mlb_statsapi", "v1", "146", "tm_mlb_mia"),  # Miami Marlins
    T("lg_mlb", "mlb_statsapi", "v1", "147", "tm_mlb_nyy"),  # New York Yankees
    T("lg_mlb", "mlb_statsapi", "v1", "158", "tm_mlb_mil"),  # Milwaukee Brewers

    # --- NBA, balldontlie v1 (30 official ids observed in NBA March 2026) ---
    T("lg_nba", "balldontlie", "v1", "1", "tm_nba_atl"),   # Atlanta Hawks
    T("lg_nba", "balldontlie", "v1", "2", "tm_nba_bos"),   # Boston Celtics
    T("lg_nba", "balldontlie", "v1", "3", "tm_nba_bkn"),   # Brooklyn Nets
    # 4 is the Bobcats->Hornets franchise, which also holds the 1988-2002
    # Charlotte Hornets history reassigned to it in 2014. 19 is the Pelicans,
    # which relinquished that history: two franchises, not one.
    T("lg_nba", "balldontlie", "v1", "4", "tm_nba_cha"),   # Charlotte Hornets
    T("lg_nba", "balldontlie", "v1", "5", "tm_nba_chi"),   # Chicago Bulls
    T("lg_nba", "balldontlie", "v1", "6", "tm_nba_cle"),   # Cleveland Cavaliers
    T("lg_nba", "balldontlie", "v1", "7", "tm_nba_dal"),   # Dallas Mavericks
    T("lg_nba", "balldontlie", "v1", "8", "tm_nba_den"),   # Denver Nuggets
    T("lg_nba", "balldontlie", "v1", "9", "tm_nba_det"),   # Detroit Pistons
    T("lg_nba", "balldontlie", "v1", "10", "tm_nba_gsw"),  # Golden State Warriors
    T("lg_nba", "balldontlie", "v1", "11", "tm_nba_hou"),  # Houston Rockets
    T("lg_nba", "balldontlie", "v1", "12", "tm_nba_ind"),  # Indiana Pacers
    T("lg_nba", "balldontlie", "v1", "13", "tm_nba_lac"),  # LA Clippers
    T("lg_nba", "balldontlie", "v1", "14", "tm_nba_lal"),  # Los Angeles Lakers
    T("lg_nba", "balldontlie", "v1", "15", "tm_nba_mem"),  # Memphis Grizzlies
    T("lg_nba", "balldontlie", "v1", "16", "tm_nba_mia"),  # Miami Heat
    T("lg_nba", "balldontlie", "v1", "17", "tm_nba_mil"),  # Milwaukee Bucks
    T("lg_nba", "balldontlie", "v1", "18", "tm_nba_min"),  # Minnesota Timberwolves
    T("lg_nba", "balldontlie", "v1", "19", "tm_nba_nop"),  # New Orleans Pelicans
    T("lg_nba", "balldontlie", "v1", "20", "tm_nba_nyk"),  # New York Knicks
    # 21 continues the Seattle SuperSonics franchise; the Sonics name and
    # banners stayed in Seattle by settlement, so the seed's historical alias
    # records continuity of the FRANCHISE, not of the branding.
    T("lg_nba", "balldontlie", "v1", "21", "tm_nba_okc"),  # Oklahoma City Thunder
    T("lg_nba", "balldontlie", "v1", "22", "tm_nba_orl"),  # Orlando Magic
    T("lg_nba", "balldontlie", "v1", "23", "tm_nba_phi"),  # Philadelphia 76ers
    T("lg_nba", "balldontlie", "v1", "24", "tm_nba_phx"),  # Phoenix Suns
    T("lg_nba", "balldontlie", "v1", "25", "tm_nba_por"),  # Portland Trail Blazers
    T("lg_nba", "balldontlie", "v1", "26", "tm_nba_sac"),  # Sacramento Kings
    T("lg_nba", "balldontlie", "v1", "27", "tm_nba_sas"),  # San Antonio Spurs
    T("lg_nba", "balldontlie", "v1", "28", "tm_nba_tor"),  # Toronto Raptors
    T("lg_nba", "balldontlie", "v1", "29", "tm_nba_uta"),  # Utah Jazz
    T("lg_nba", "balldontlie", "v1", "30", "tm_nba_was"),  # Washington Wizards
)


def _validate_t1(entries: tuple[TeamAttestation, ...]) -> dict[
        tuple[str, str, str, str], str]:
    """Index the map, enforcing T1 and nothing stronger.

    Refuses a provider key that denotes two franchises. Deliberately does NOT
    require canonical-target injectivity: several provider ids denoting one
    franchise is a legitimate provider-id transition, and an earlier version of
    the architecture tests wrongly pinned the observed 1:1 shape as policy.
    """

    index: dict[tuple[str, str, str, str], str] = {}
    for entry in entries:
        existing = index.get(entry.key)
        if existing is not None and existing != entry.canonical_team_id:
            raise AttestationError(
                f"provider key {entry.key} is attested to both {existing!r} and "
                f"{entry.canonical_team_id!r}; one provider key denotes exactly one "
                "canonical franchise (T1)"
            )
        index[entry.key] = entry.canonical_team_id
    return index


_INDEX: Final[dict[tuple[str, str, str, str], str]] = _validate_t1(TEAM_ATTESTATIONS)


def attested_canonical_team(
    namespace: ProviderNamespace, provider_team_id: str
) -> Optional[str]:
    """Exact lookup. ``None`` means UNRESOLVED -- never a nearest match.

    No name, alias, abbreviation or nickname is consulted. An id absent from the
    committed map is unresolved by definition; adding it is a reviewed source
    change, not something this function may infer.
    """

    if namespace.entity_type is not EntityType.TEAM:
        raise AttestationError(
            f"the team attestation map answers TEAM keys only, not "
            f"{namespace.entity_type.value!r}")
    return _INDEX.get(
        (namespace.league_id, namespace.provider, namespace.generation,
         provider_team_id))


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #
def canonical_team_seed_digest() -> str:
    """Semantic digest of the canonical franchise seed (review repair RV5).

    Canonical team ids are abbreviation-derived and the seed also carries the
    historical aliases the curation relied on, so a later seed edit would
    otherwise silently change what an already-built corpus's attestation *means*.
    Binding this digest into the map digest makes such an edit change the corpus
    version instead.

    Covers, per franchise: league, canonical id, abbreviation, canonical name,
    city, nickname and the full alias set with its alias types. Sorted
    throughout, so insertion order, dict order and traversal order cannot affect
    it while any semantic change does.
    """

    leagues = (("MLB", "lg_mlb", MLB_TEAMS), ("NBA", "lg_nba", NBA_TEAMS))
    payload: list[dict[str, object]] = []
    for code, league_id, seeds in leagues:
        for seed in seeds:
            payload.append({
                "league_id": league_id,
                "canonical_team_id": team_id(code, seed.abbreviation),
                "abbreviation": seed.abbreviation,
                "canonical_name": seed.canonical_name,
                "city": seed.city,
                "nickname": seed.nickname,
                # Alias TYPE included: a name moving from `historical` to `full`
                # is a franchise-semantics change even if the string is the same.
                "aliases": sorted([alias, kind] for alias, kind in alias_specs(seed)),
            })
    payload.sort(key=lambda row: (str(row["league_id"]),
                                  str(row["canonical_team_id"])))
    return hashlib.sha256(
        canonical_json({"kind": "canonical_team_seed", "version": "seed-v1",
                        "franchises": payload}).encode("utf-8")
    ).hexdigest()


def attestation_map_digest() -> str:
    """Semantic digest of the committed map (review repairs RV1/RV5).

    Binds the map format, the attestation policy, the canonical-team seed digest
    and every entry. This value is what a reconstruction corpus stores in
    ``reconstruction_corpus_versions.static_identity_map_digest``, and what the
    crosswalk generator requires the corpus to declare before it writes anything.

    Order-independent: entries are sorted by their key. Reformatting this file,
    reordering the tuple or adding a comment does not change it; changing a
    mapping, the policy, the format version or the seed semantics does.
    """

    entries = sorted(
        [e.league_id, e.provider, e.namespace_generation, e.provider_team_id,
         e.canonical_team_id]
        for e in TEAM_ATTESTATIONS
    )
    return hashlib.sha256(
        canonical_json({
            "kind": "team_attestation_map",
            "map_format_version": MAP_FORMAT_VERSION,
            "attestation_policy_version": TEAM_ATTESTATION_POLICY_VERSION,
            "canonical_team_seed_digest": canonical_team_seed_digest(),
            "entries": entries,
        }).encode("utf-8")
    ).hexdigest()


def describe_map_shape() -> dict[str, object]:
    """Report the map's shape. Reporting only -- none of this is policy.

    In particular ``distinct_canonical_targets`` may legitimately be smaller than
    ``entries`` when a provider-id transition puts two ids on one franchise.
    """

    by_league: dict[str, int] = {}
    for entry in TEAM_ATTESTATIONS:
        by_league[entry.league_id] = by_league.get(entry.league_id, 0) + 1
    return {
        "entries": len(TEAM_ATTESTATIONS),
        "entries_by_league": dict(sorted(by_league.items())),
        "distinct_provider_keys": len(_INDEX),
        "distinct_canonical_targets": len({e.canonical_team_id
                                           for e in TEAM_ATTESTATIONS}),
        "map_format_version": MAP_FORMAT_VERSION,
        "attestation_policy_version": TEAM_ATTESTATION_POLICY_VERSION,
    }
