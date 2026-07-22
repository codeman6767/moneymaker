"""Season-aware team-alias resolution.

Aliases carry ``valid_from_season`` / ``valid_to_season``. Before this patch
resolution ignored them entirely. Now a caller may pass ``season_year`` to
restrict the lookup, and -- just as importantly -- the result reports whether
that scoping actually verified anything, so an unbounded seed alias is never
mistaken for a curated one.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.normalize import AliasMatchStatus
from sports_quant.db.repositories import SqliteTeamAliasRepository
from sports_quant.db.schema import SEASON_UNBOUNDED_END, SEASON_UNBOUNDED_START


@pytest.fixture
def aliases(conn: sqlite3.Connection) -> SqliteTeamAliasRepository:
    return SqliteTeamAliasRepository(conn)


@pytest.fixture
def bounded_alias(aliases: SqliteTeamAliasRepository, mlb_league_id: str) -> str:
    """A curated historical alias valid only for 1903-1912."""

    aliases.add(
        team_id="tm_mlb_nyy", league_id=mlb_league_id, alias="New York Highlanders",
        alias_type="historical", valid_from_season=1903, valid_to_season=1912,
        source="manual",
    )
    return "New York Highlanders"


def test_alias_resolves_inside_its_validity_range(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str, bounded_alias: str
) -> None:
    result = aliases.resolve(bounded_alias, league_id=mlb_league_id, season_year=1910)
    assert result.status is AliasMatchStatus.MATCHED
    assert result.matched_id() == "tm_mlb_nyy"


@pytest.mark.parametrize("year", [1902, 1913, 2026])
def test_alias_is_excluded_outside_its_validity_range(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str, bounded_alias: str, year: int
) -> None:
    result = aliases.resolve(bounded_alias, league_id=mlb_league_id, season_year=year)
    assert result.status is AliasMatchStatus.UNMATCHED
    assert result.matched_id() is None


def test_boundary_years_are_inclusive(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str, bounded_alias: str
) -> None:
    for year in (1903, 1912):
        assert aliases.resolve(
            bounded_alias, league_id=mlb_league_id, season_year=year
        ).status is AliasMatchStatus.MATCHED


def test_unscoped_resolution_still_finds_a_bounded_alias(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str, bounded_alias: str
) -> None:
    """Omitting season_year preserves the previous behaviour."""

    result = aliases.resolve(bounded_alias, league_id=mlb_league_id)
    assert result.status is AliasMatchStatus.MATCHED
    assert result.season_scoped is False


def test_unscoped_resolution_does_not_claim_validity_was_checked(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str, bounded_alias: str
) -> None:
    result = aliases.resolve(bounded_alias, league_id=mlb_league_id)
    assert result.season_year is None
    assert result.season_scoped is False
    assert result.season_validity_verified is False


def test_scoped_resolution_reports_the_season_it_used(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str, bounded_alias: str
) -> None:
    result = aliases.resolve(bounded_alias, league_id=mlb_league_id, season_year=1910)
    assert result.season_year == 1910
    assert result.season_scoped is True
    assert result.season_validity_verified is True


def test_seeded_aliases_are_unbounded_and_report_unverified(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str
) -> None:
    """Seeded historical names are stored unbounded, awaiting Phase D curation.

    They still resolve for any season -- but ``season_validity_verified`` is
    False, so a caller is told the match does not prove the alias was actually
    in use that year.
    """

    result = aliases.resolve("Cleveland Indians", league_id=mlb_league_id, season_year=1910)
    assert result.status is AliasMatchStatus.MATCHED
    assert result.season_scoped is True
    assert result.season_validity_verified is False


def test_seed_aliases_carry_the_unbounded_sentinels(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str
) -> None:
    for alias in aliases.list_for_team("tm_mlb_cle"):
        assert alias.valid_from_season == SEASON_UNBOUNDED_START
        assert alias.valid_to_season == SEASON_UNBOUNDED_END


def test_season_scoping_can_disambiguate(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str
) -> None:
    """Two teams sharing a name in different eras resolve cleanly per season.

    This is the payoff of season scoping: a name that is ambiguous overall is
    unambiguous once the season is known.
    """

    aliases.add(team_id="tm_mlb_nyy", league_id=mlb_league_id, alias="Gotham Nine",
                alias_type="historical", valid_from_season=1900, valid_to_season=1950,
                source="manual")
    aliases.add(team_id="tm_mlb_nym", league_id=mlb_league_id, alias="Gotham Nine",
                alias_type="historical", valid_from_season=1962, valid_to_season=2000,
                source="manual")

    assert aliases.resolve("Gotham Nine", league_id=mlb_league_id).status is (
        AliasMatchStatus.AMBIGUOUS
    )
    assert aliases.resolve(
        "Gotham Nine", league_id=mlb_league_id, season_year=1925
    ).matched_id() == "tm_mlb_nyy"
    assert aliases.resolve(
        "Gotham Nine", league_id=mlb_league_id, season_year=1975
    ).matched_id() == "tm_mlb_nym"


def test_season_scoping_composes_with_provider_scoping(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str
) -> None:
    aliases.add(team_id="tm_mlb_nyy", league_id=mlb_league_id, alias="Bombers",
                alias_type="provider", provider="the_odds_api",
                valid_from_season=2000, valid_to_season=2100,
                source="provider_observed")
    inside = aliases.resolve("Bombers", league_id=mlb_league_id,
                             provider="the_odds_api", season_year=2026)
    assert inside.matched_id() == "tm_mlb_nyy"
    outside = aliases.resolve("Bombers", league_id=mlb_league_id,
                              provider="the_odds_api", season_year=1999)
    assert outside.status is AliasMatchStatus.UNMATCHED


def test_season_scoping_does_not_resurrect_an_unknown_name(
    aliases: SqliteTeamAliasRepository, mlb_league_id: str
) -> None:
    result = aliases.resolve("Springfield Isotopes", league_id=mlb_league_id,
                             season_year=2026)
    assert result.status is AliasMatchStatus.UNMATCHED
    assert result.season_validity_verified is False
