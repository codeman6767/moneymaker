"""Team and team-alias repositories."""

from __future__ import annotations

import sqlite3
from typing import Optional, Protocol

from ..ids import new_team_alias_id
from ..ids import team_id as make_team_id
from ..models import Team, TeamAlias
from ..normalize import AliasCandidate, AliasResolution, normalize_name, resolve_alias
from ..schema import NO_PROVIDER, SEASON_UNBOUNDED_END, SEASON_UNBOUNDED_START, utc_now_iso
from .base import Repository


class TeamRepositoryProtocol(Protocol):
    """Operations Phase A needs from a team store."""

    def upsert(
        self,
        *,
        league_code: str,
        league_id: str,
        canonical_name: str,
        city: str,
        nickname: str,
        abbreviation: str,
        first_season: Optional[int] = None,
        last_season: Optional[int] = None,
    ) -> Team: ...

    def get(self, team_id: str) -> Optional[Team]: ...

    def get_by_abbreviation(self, *, league_id: str, abbreviation: str) -> Optional[Team]: ...

    def list_for_league(self, league_id: str) -> list[Team]: ...

    def count(self) -> int: ...

    def count_for_league(self, league_id: str) -> int: ...


class SqliteTeamRepository(Repository):
    """Team storage. ``team_id`` is deterministic from (league, abbreviation)."""

    _COLUMNS = (
        "team_id, league_id, canonical_name, city, nickname, abbreviation, "
        "first_season, last_season, created_at, updated_at"
    )

    def upsert(
        self,
        *,
        league_code: str,
        league_id: str,
        canonical_name: str,
        city: str,
        nickname: str,
        abbreviation: str,
        first_season: Optional[int] = None,
        last_season: Optional[int] = None,
    ) -> Team:
        tid = make_team_id(league_code, abbreviation)
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO teams "
            "(team_id, league_id, canonical_name, city, nickname, abbreviation, "
            " first_season, last_season, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(league_id, abbreviation) DO NOTHING",
            (
                tid,
                league_id,
                canonical_name,
                city,
                nickname,
                abbreviation,
                first_season,
                last_season,
                now,
                now,
            ),
        )
        existing = self.get_by_abbreviation(league_id=league_id, abbreviation=abbreviation)
        if existing is None:  # pragma: no cover - unreachable after the insert
            raise RuntimeError(f"team {tid!r} vanished immediately after upsert")
        return existing

    def get(self, team_id: str) -> Optional[Team]:
        row = self._fetch_one(f"SELECT {self._COLUMNS} FROM teams WHERE team_id = ?", (team_id,))
        return None if row is None else self._to_model(row)

    def get_by_abbreviation(self, *, league_id: str, abbreviation: str) -> Optional[Team]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM teams WHERE league_id = ? AND abbreviation = ?",
            (league_id, abbreviation),
        )
        return None if row is None else self._to_model(row)

    def list_for_league(self, league_id: str) -> list[Team]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM teams WHERE league_id = ? ORDER BY abbreviation",
                (league_id,),
            )
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM teams")

    def count_for_league(self, league_id: str) -> int:
        return self._count("SELECT COUNT(*) FROM teams WHERE league_id = ?", (league_id,))

    def _to_model(self, row: sqlite3.Row) -> Team:
        return Team(
            team_id=str(row["team_id"]),
            league_id=str(row["league_id"]),
            canonical_name=str(row["canonical_name"]),
            city=str(row["city"]),
            nickname=str(row["nickname"]),
            abbreviation=str(row["abbreviation"]),
            first_season=self._opt_int(row, "first_season"),
            last_season=self._opt_int(row, "last_season"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class TeamAliasRepositoryProtocol(Protocol):
    """Operations Phase A needs from a team-alias store."""

    def add(
        self,
        *,
        team_id: str,
        league_id: str,
        alias: str,
        alias_type: str,
        provider: str = NO_PROVIDER,
        valid_from_season: int = SEASON_UNBOUNDED_START,
        valid_to_season: int = SEASON_UNBOUNDED_END,
        source: str = "seed",
    ) -> bool: ...

    def list_for_team(self, team_id: str) -> list[TeamAlias]: ...

    def resolve(
        self, raw_name: str, *, league_id: str, provider: Optional[str] = None
    ) -> AliasResolution: ...

    def mark_ambiguous_duplicates(self, league_id: str) -> int: ...

    def count(self) -> int: ...


class SqliteTeamAliasRepository(Repository):
    """Team-alias storage and deterministic resolution.

    Uniqueness is scoped to the *team*, not the league: two teams in one league
    legitimately share an alias ("chicago" -> Cubs and White Sox). That is
    ambiguity to record and refuse at match time, not a write to reject.
    """

    _COLUMNS = (
        "alias_id, team_id, league_id, alias, normalized, alias_type, provider, "
        "valid_from_season, valid_to_season, is_ambiguous, source, created_at"
    )

    def add(
        self,
        *,
        team_id: str,
        league_id: str,
        alias: str,
        alias_type: str,
        provider: str = NO_PROVIDER,
        valid_from_season: int = SEASON_UNBOUNDED_START,
        valid_to_season: int = SEASON_UNBOUNDED_END,
        source: str = "seed",
    ) -> bool:
        """Add an alias. Returns True if a row was inserted, False if it existed.

        Idempotent: re-seeding writes nothing the second time.
        """

        normalized = normalize_name(alias).normalized
        if not normalized:
            raise ValueError(f"alias {alias!r} normalizes to an empty string")
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO team_aliases "
            "(alias_id, team_id, league_id, alias, normalized, alias_type, provider, "
            " valid_from_season, valid_to_season, is_ambiguous, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                new_team_alias_id(),
                team_id,
                league_id,
                alias,
                normalized,
                alias_type,
                provider,
                valid_from_season,
                valid_to_season,
                source,
                utc_now_iso(),
            ),
        )
        return cursor.rowcount > 0

    def list_for_team(self, team_id: str) -> list[TeamAlias]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM team_aliases WHERE team_id = ? "
                "ORDER BY alias_type, normalized",
                (team_id,),
            )
        ]

    def list_for_league(self, league_id: str) -> list[TeamAlias]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM team_aliases WHERE league_id = ? "
                "ORDER BY team_id, alias_type, normalized",
                (league_id,),
            )
        ]

    def resolve(
        self, raw_name: str, *, league_id: str, provider: Optional[str] = None
    ) -> AliasResolution:
        """Resolve a raw team name within one league.

        Returns an explicit ``AMBIGUOUS`` result when the name maps to more than
        one team, or to an alias flagged ambiguous -- never the first row.
        """

        normalized = normalize_name(raw_name).normalized
        rows = self._fetch_all(
            "SELECT team_id, alias, normalized, alias_type, provider, is_ambiguous "
            "FROM team_aliases WHERE league_id = ? AND normalized = ?",
            (league_id, normalized),
        )
        candidates = [
            AliasCandidate(
                entity_id=str(r["team_id"]),
                alias=str(r["alias"]),
                normalized=str(r["normalized"]),
                alias_type=str(r["alias_type"]),
                provider=str(r["provider"]),
                is_ambiguous=bool(r["is_ambiguous"]),
            )
            for r in rows
        ]
        return resolve_alias(raw_name, candidates, provider=provider)

    def mark_ambiguous_duplicates(self, league_id: str) -> int:
        """Flag every alias whose normalized form maps to more than one team.

        Computed from the data rather than hand-maintained: "Chicago" is
        ambiguous in MLB because two Chicago teams exist, and deriving that is
        both deterministic and self-correcting as teams are added.

        Returns the number of rows flagged.
        """

        cursor = self._conn.execute(
            "UPDATE team_aliases SET is_ambiguous = 1 "
            "WHERE league_id = ? AND is_ambiguous = 0 AND normalized IN ("
            "    SELECT normalized FROM team_aliases WHERE league_id = ? "
            "    GROUP BY normalized HAVING COUNT(DISTINCT team_id) > 1"
            ")",
            (league_id, league_id),
        )
        return cursor.rowcount

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM team_aliases")

    def count_for_league(self, league_id: str) -> int:
        return self._count("SELECT COUNT(*) FROM team_aliases WHERE league_id = ?", (league_id,))

    def count_ambiguous(self, league_id: str) -> int:
        return self._count(
            "SELECT COUNT(*) FROM team_aliases WHERE league_id = ? AND is_ambiguous = 1",
            (league_id,),
        )

    def _to_model(self, row: sqlite3.Row) -> TeamAlias:
        return TeamAlias(
            alias_id=str(row["alias_id"]),
            team_id=str(row["team_id"]),
            league_id=str(row["league_id"]),
            alias=str(row["alias"]),
            normalized=str(row["normalized"]),
            alias_type=str(row["alias_type"]),
            provider=str(row["provider"]),
            valid_from_season=int(row["valid_from_season"]),
            valid_to_season=int(row["valid_to_season"]),
            is_ambiguous=self._bool(row, "is_ambiguous"),
            source=str(row["source"]),
            created_at=str(row["created_at"]),
        )


__all__ = [
    "SqliteTeamAliasRepository",
    "SqliteTeamRepository",
    "TeamAliasRepositoryProtocol",
    "TeamRepositoryProtocol",
]
