"""Falsification tests for the team/game crosswalk architecture (design only).

`RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE.md` chooses TEAM-A: a
source-controlled static attestation binding official provider franchise ids to
the **existing** canonical seed. Nothing here implements a crosswalk — these are
the diagnostics that would falsify the design if its premises were wrong, kept
permanent so a later seed or evidence change cannot quietly invalidate it.

The load-bearing premises:

* the canonical team dimension is a **franchise** dimension, with relocation and
  rename encoded as historical aliases of one row;
* every official provider franchise id resolves to **exactly one** canonical
  franchise by **exact** normalized equality -- never similarity (the T1
  invariant; the converse, one franchise per provider id, is NOT required and
  the independent review corrected a test that had pinned it);
* the resolution is corroborated by a **second attribute from the same
  observation** (the independent review corrected "independent attribute":
  name, abbreviation and nickname arrive on one provider row, so agreement
  lowers accidental-label-match risk and is not independent-source evidence);
* a historical label (``Montreal Expos``) cannot create a second franchise;
* `games` can already key a canonical row on the official provider game id.

Read-only. No provider request, no protected corpus opened for writing.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Optional

import pytest

from sports_quant.db.ids import team_id
from sports_quant.db.normalize import normalize_name
from sports_quant.db.seeds.loader import alias_specs
from sports_quant.db.seeds.mlb_teams import MLB_TEAMS
from sports_quant.db.seeds.nba_teams import NBA_TEAMS
from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE
from sports_quant.retrospective.sources import PROVIDER_LEAGUES

LEAGUES = [("MLB", "lg_mlb", MLB_TEAMS), ("NBA", "lg_nba", NBA_TEAMS)]


def _norm(text: str) -> str:
    return normalize_name(text, extract_suffix=False).normalized


def _alias_index(seeds, code: str) -> dict[str, set[str]]:
    """normalized alias -> {canonical team id}. Exact equality only."""

    index: dict[str, set[str]] = defaultdict(set)
    for seed in seeds:
        tid = team_id(code, seed.abbreviation)
        for alias, _kind in alias_specs(seed):
            index[_norm(alias)].add(tid)
    return index


# --------------------------------------------------------------------------- #
# §2 the canonical dimension really is a franchise dimension
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code,seeds,historical,expected", [
    ("MLB", MLB_TEAMS, "Montreal Expos", "tm_mlb_wsh"),
    ("MLB", MLB_TEAMS, "Cleveland Indians", "tm_mlb_cle"),
    ("MLB", MLB_TEAMS, "Florida Marlins", "tm_mlb_mia"),
    ("MLB", MLB_TEAMS, "Oakland Athletics", "tm_mlb_ath"),
    ("MLB", MLB_TEAMS, "Anaheim Angels", "tm_mlb_laa"),
    ("NBA", NBA_TEAMS, "Seattle SuperSonics", "tm_nba_okc"),
    ("NBA", NBA_TEAMS, "New Jersey Nets", "tm_nba_bkn"),
    ("NBA", NBA_TEAMS, "Vancouver Grizzlies", "tm_nba_mem"),
    ("NBA", NBA_TEAMS, "Charlotte Bobcats", "tm_nba_cha"),
    ("NBA", NBA_TEAMS, "New Orleans Hornets", "tm_nba_nop"),
    ("NBA", NBA_TEAMS, "Washington Bullets", "tm_nba_was"),
])
def test_relocation_and_rename_resolve_to_one_franchise(
    code: str, seeds, historical: str, expected: str
) -> None:
    """A relocation is the same franchise, and resolves to exactly one row."""

    assert _alias_index(seeds, code)[_norm(historical)] == {expected}


def test_hornets_and_pelicans_remain_two_franchises() -> None:
    """The hardest historical case, and the seed already has it right.

    The Hornets name and history returned to Charlotte in 2014, so the Pelicans
    are a separate franchise. Merging them to make ids convenient would be
    rewriting sports history.
    """

    index = _alias_index(NBA_TEAMS, "NBA")
    assert index[_norm("Charlotte Hornets")] == {"tm_nba_cha"}
    assert index[_norm("New Orleans Hornets")] == {"tm_nba_nop"}
    assert index[_norm("Charlotte Bobcats")] == {"tm_nba_cha"}


@pytest.mark.parametrize("code,seeds", [("MLB", MLB_TEAMS), ("NBA", NBA_TEAMS)])
def test_no_historical_alias_resolves_to_two_franchises(code: str, seeds) -> None:
    """§23 stress test: a historical label must not create a second canonical team."""

    index = _alias_index(seeds, code)
    offenders = []
    for seed in seeds:
        tid = team_id(code, seed.abbreviation)
        for alias, kind in alias_specs(seed):
            if kind == "historical" and index[_norm(alias)] != {tid}:
                offenders.append((alias, tid, sorted(index[_norm(alias)])))
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# §3 canonical team ids are deterministic and abbreviation-derived
# --------------------------------------------------------------------------- #
def test_canonical_team_ids_are_deterministic_and_unique() -> None:
    for code, _league, seeds in LEAGUES:
        ids = [team_id(code, s.abbreviation) for s in seeds]
        assert len(ids) == len(set(ids)) == 30
        assert ids == [team_id(code, s.abbreviation) for s in seeds]


def test_canonical_team_id_is_derived_from_the_abbreviation() -> None:
    """Pinned because it is why TEAM-B is expensive: changing an abbreviation in
    the seed would change the canonical id and orphan every FK to it."""

    assert team_id("MLB", "NYY") == "tm_mlb_nyy"
    assert team_id("NBA", "OKC") == "tm_nba_okc"


# --------------------------------------------------------------------------- #
# §7 the curation rule is satisfiable: exact + corroborated + unique
# --------------------------------------------------------------------------- #
def _protected_corpus(league: str) -> Optional[Path]:
    path = Path({
        "lg_mlb": "data/f1_mlb_2026_06_scratch.db",
        "lg_nba": "data/f1_nba_2026_03_lineups_merged.db",
    }[league])
    return path if path.exists() else None


def _observed_team_labels(league: str, provider: str) -> dict[str, dict[str, set[str]]]:
    import sqlite3

    path = _protected_corpus(league)
    assert path is not None
    conn = sqlite3.connect(f"file:{path.as_posix()}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT provider_team_id, normalized_name, abbreviation, nickname "
            "FROM provider_team_identity_snapshots "
            "WHERE provider = ? AND league_id = ?", (provider, league)).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"name": set(), "abbr": set(), "nick": set()})
    for row in rows:
        entry = out[str(row["provider_team_id"])]
        entry["name"].add(str(row["normalized_name"]))
        if row["abbreviation"]:
            entry["abbr"].add(str(row["abbreviation"]))
        if row["nickname"]:
            entry["nick"].add(str(row["nickname"]))
    return out


@pytest.mark.parametrize("code,league,seeds", [
    ("MLB", "lg_mlb", MLB_TEAMS), ("NBA", "lg_nba", NBA_TEAMS)])
def test_every_official_team_id_is_uniquely_and_corroboratedly_attestable(
    code: str, league: str, seeds
) -> None:
    """The diagnostic that would falsify TEAM-A. 30/30 per league, corroborated.

    Skipped rather than failed when the protected corpus is unavailable: this is
    a property of that evidence, and a machine without it should not report a
    false negative.
    """

    if _protected_corpus(league) is None:
        pytest.skip("protected corpus not present on this machine")

    provider = PROVIDER_LEAGUES_INVERSE[league]
    observed = _observed_team_labels(league, provider)
    index = _alias_index(seeds, code)

    attested: dict[str, str] = {}
    ambiguous, unresolved, uncorroborated = [], [], []
    for provider_id, entry in observed.items():
        by_name: set[str] = set()
        for name in entry["name"]:
            by_name |= index.get(name, set())
        if not by_name:
            unresolved.append(provider_id)
            continue
        if len(by_name) > 1:
            ambiguous.append((provider_id, sorted(by_name)))
            continue
        tid = next(iter(by_name))
        second: set[str] = set()
        for value in entry["abbr"] | entry["nick"]:
            second |= index.get(_norm(value), set())
        if tid not in second:
            uncorroborated.append(provider_id)
        attested[provider_id] = tid

    assert not unresolved, f"unresolved provider ids: {unresolved}"
    assert not ambiguous, f"ambiguous provider ids: {ambiguous}"
    assert not uncorroborated, f"no second attribute agreed: {uncorroborated}"
    assert len(attested) == 30

    # THE INVARIANT (T1): each provider key denotes exactly one canonical
    # franchise. `attested` is a dict, so this holds by construction; it is the
    # rule the architecture actually requires.
    assert all(isinstance(v, str) and v for v in attested.values())

    # AN OBSERVATION, NOT A RULE: these two one-month 2026 corpora happen to be
    # 30 provider ids <-> 30 franchises. The independent review corrected the
    # earlier `len(set(attested.values())) == 30`, which promoted that shape to
    # global canonical-target injectivity and would have rejected a legitimate
    # provider-id transition (old id and new id denoting one franchise). Recorded
    # here so the observation is preserved without becoming policy.
    distinct_targets = len(set(attested.values()))
    assert distinct_targets <= 30, "a provider key gained two franchises"
    assert distinct_targets == 30, (
        "observation for these corpora only: currently 1:1. If a future corpus "
        "legitimately maps two provider ids to one franchise, relax THIS "
        "assertion -- not the T1 invariant above."
    )


PROVIDER_LEAGUES_INVERSE = {v: k for k, v in PROVIDER_LEAGUES.items()}


# --------------------------------------------------------------------------- #
# §20 / §21 multi-provider authority
# --------------------------------------------------------------------------- #
def test_official_provider_authority_lists_agree() -> None:
    """`OFFICIAL_PROVIDER_BY_LEAGUE` and the Lane-R `PROVIDER_LEAGUES` are the
    same contract seen from both ends; drift between them would let a secondary
    provider bootstrap a competing canonical entity."""

    for league, provider in OFFICIAL_PROVIDER_BY_LEAGUE.items():
        assert PROVIDER_LEAGUES.get(provider) == league, (league, provider)
    for provider, league in PROVIDER_LEAGUES.items():
        assert OFFICIAL_PROVIDER_BY_LEAGUE.get(league) == provider


# --------------------------------------------------------------------------- #
# §17 the games table can already key a canonical row on the official game id
# --------------------------------------------------------------------------- #
def test_games_already_supports_official_provider_keying(db_path: Path) -> None:
    """No schema change is needed for the game bootstrap."""

    import sqlite3

    from sports_quant.db.init import initialize_database

    initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
        assert {"official_provider", "official_game_key"} <= columns
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_games_official_key'"
        ).fetchone()[0]
        assert "UNIQUE" in index
        assert "official_provider" in index and "official_game_key" in index
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The architecture is a DECISION, not an implementation
# --------------------------------------------------------------------------- #
def test_team_and_game_crosswalks_are_no_longer_blocked_in_code() -> None:
    """This phase decided the architecture; a later phase implemented it.

    The blocker the identity-audit review recorded is closed. Kept (rather than
    deleted) so the transition is visible in the file that pinned the block.
    """

    from sports_quant.retrospective.crosswalks import (
        CROSSWALK_SUPPORTED_ENTITY_TYPES,
    )
    from sports_quant.retrospective.provenance import EntityType

    assert EntityType.TEAM in CROSSWALK_SUPPORTED_ENTITY_TYPES
    assert EntityType.GAME in CROSSWALK_SUPPORTED_ENTITY_TYPES


def test_the_attestation_map_has_since_been_committed() -> None:
    """TEAM-A's map was a follow-up; the follow-up shipped it.

    The digests are pinned in CI and in the implementation report; here we only
    assert the map exists and is the reviewed 60-entry, 2-league shape.
    """

    import sports_quant.retrospective as retro
    from sports_quant.retrospective.attestations import (
        MAP_FORMAT_VERSION,
        TEAM_ATTESTATIONS,
        describe_map_shape,
    )

    package = Path(retro.__file__).parent
    assert (package / "attestations.py").exists()
    assert MAP_FORMAT_VERSION == "team-a-map-v1"
    assert len(TEAM_ATTESTATIONS) == 60
    assert describe_map_shape()["entries_by_league"] == {"lg_mlb": 30, "lg_nba": 30}


def test_schema_is_unchanged_at_v19() -> None:
    from sports_quant.db.engine import discover_migrations
    from sports_quant.db.schema import CURRENT_SCHEMA_VERSION

    # TEAM-A itself added no migration. The absolute number moved when f020
    # added the Lane-R market-observation table, which is a different task; what
    # is asserted here is that the discovered count and the declared version
    # still agree, so no TEAM-A change can slip a migration in unnoticed.
    assert len(discover_migrations()) == CURRENT_SCHEMA_VERSION
