"""Idempotent application of the canonical league and team seeds.

Everything here is deterministic and offline. Running it twice writes nothing
the second time and destroys nothing the first time -- which is what makes
``db-init`` safe to re-run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from ..normalize import normalize_name
from ..repositories.leagues import SqliteLeagueRepository
from ..repositories.teams import SqliteTeamAliasRepository, SqliteTeamRepository
from .mlb_teams import LEAGUE_CODE as MLB_CODE
from .mlb_teams import LEAGUE_NAME as MLB_NAME
from .mlb_teams import LEAGUE_SPORT as MLB_SPORT
from .mlb_teams import MLB_TEAMS, TeamSeed
from .nba_teams import LEAGUE_CODE as NBA_CODE
from .nba_teams import LEAGUE_NAME as NBA_NAME
from .nba_teams import LEAGUE_SPORT as NBA_SPORT
from .nba_teams import NBA_TEAMS


@dataclass(frozen=True)
class LeagueSeedResult:
    """What one league's seed run did."""

    league_code: str
    league_id: str
    teams_total: int
    teams_created: int
    aliases_created: int
    aliases_flagged_ambiguous: int


@dataclass(frozen=True)
class SeedResult:
    """Aggregate outcome of :func:`seed_all`."""

    leagues: tuple[LeagueSeedResult, ...]

    @property
    def teams_total(self) -> int:
        return sum(lg.teams_total for lg in self.leagues)

    @property
    def teams_created(self) -> int:
        return sum(lg.teams_created for lg in self.leagues)

    @property
    def aliases_created(self) -> int:
        return sum(lg.aliases_created for lg in self.leagues)

    def for_league(self, code: str) -> LeagueSeedResult:
        for league in self.leagues:
            if league.league_code == code:
                return league
        raise KeyError(f"no seed result for league {code!r}")


def alias_specs(seed: TeamSeed) -> list[tuple[str, str]]:
    """Every ``(alias, alias_type)`` pair implied by a team seed.

    Derived rather than hand-listed so a new team cannot be added with a
    forgotten alias. Duplicates after normalization are dropped -- the
    Athletics' canonical name equals their nickname, and a team whose city is
    also its abbreviation would otherwise collide with itself.
    """

    specs: list[tuple[str, str]] = [
        (seed.abbreviation, "abbreviation"),
        (seed.nickname, "nickname"),
        (seed.canonical_name, "full"),
    ]
    if seed.city:
        specs.append((seed.city, "city"))
    for extra_city in seed.extra_cities:
        specs.append((extra_city, "city"))
    for extra in seed.extra_aliases:
        # A short all-caps token is a provider abbreviation; anything longer is
        # a former or alternate full name.
        kind = "abbreviation" if extra.isupper() and len(extra) <= 4 else "historical"
        specs.append((extra, kind))

    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for alias, kind in specs:
        if not alias.strip():
            continue
        key = (normalize_name(alias).normalized, kind)
        if key in seen or not key[0]:
            continue
        seen.add(key)
        unique.append((alias, kind))
    return unique


def seed_league(
    conn: sqlite3.Connection,
    *,
    code: str,
    name: str,
    sport: str,
    teams: Sequence[TeamSeed],
) -> LeagueSeedResult:
    """Seed one league and its teams. Idempotent."""

    leagues = SqliteLeagueRepository(conn)
    team_repo = SqliteTeamRepository(conn)
    alias_repo = SqliteTeamAliasRepository(conn)

    league = leagues.upsert(code=code, name=name, sport=sport)

    teams_created = 0
    aliases_created = 0
    for seed in teams:
        before = team_repo.count_for_league(league.league_id)
        team = team_repo.upsert(
            league_code=code,
            league_id=league.league_id,
            canonical_name=seed.canonical_name,
            city=seed.city,
            nickname=seed.nickname,
            abbreviation=seed.abbreviation,
        )
        if team_repo.count_for_league(league.league_id) > before:
            teams_created += 1

        for alias, alias_type in alias_specs(seed):
            if alias_repo.add(
                team_id=team.team_id,
                league_id=league.league_id,
                alias=alias,
                alias_type=alias_type,
                source="seed",
            ):
                aliases_created += 1

    # Derive ambiguity from the data: "chicago" is ambiguous in MLB because two
    # Chicago teams exist. Computing it beats hand-marking -- it is
    # deterministic and self-correcting as teams change.
    flagged = alias_repo.mark_ambiguous_duplicates(league.league_id)

    return LeagueSeedResult(
        league_code=code,
        league_id=league.league_id,
        teams_total=team_repo.count_for_league(league.league_id),
        teams_created=teams_created,
        aliases_created=aliases_created,
        aliases_flagged_ambiguous=flagged,
    )


def seed_all(conn: sqlite3.Connection) -> SeedResult:
    """Seed both leagues, their teams, and their aliases.

    The caller supplies the connection and owns the transaction, so seeding
    composes into one atomic unit with the migrations that precede it.
    """

    return SeedResult(
        leagues=(
            seed_league(conn, code=MLB_CODE, name=MLB_NAME, sport=MLB_SPORT, teams=MLB_TEAMS),
            seed_league(conn, code=NBA_CODE, name=NBA_NAME, sport=NBA_SPORT, teams=NBA_TEAMS),
        )
    )
