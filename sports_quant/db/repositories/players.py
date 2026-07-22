"""Player and player-alias repositories.

No players are seeded. A player row is created only from a real observed
source, so the corpus never contains a fabricated person.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Protocol

from ..ids import new_player_alias_id, new_player_id
from ..models import Player, PlayerAlias
from ..normalize import (
    NO_SUFFIX,
    AliasCandidate,
    AliasResolution,
    normalize_name,
    resolve_alias,
)
from ..schema import NO_PROVIDER, utc_now_iso
from .base import Repository


class PlayerRepositoryProtocol(Protocol):
    """Operations Phase A needs from a player store."""

    def create(
        self,
        *,
        league_id: str,
        full_name: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        suffix: Optional[str] = None,
        birth_date: Optional[str] = None,
        primary_position: Optional[str] = None,
        debut_date: Optional[str] = None,
        final_game_date: Optional[str] = None,
    ) -> Player: ...

    def get(self, player_id: str) -> Optional[Player]: ...

    def list_for_league(self, league_id: str) -> list[Player]: ...

    def count(self) -> int: ...


class SqlitePlayerRepository(Repository):
    """Player storage. ``player_id`` is a surrogate ULID.

    Surrogate rather than name-derived because players change names, and an id
    derived from a mutable key changes when reality does -- silently orphaning
    every foreign key that pointed at it.
    """

    _COLUMNS = (
        "player_id, league_id, full_name, first_name, last_name, suffix, birth_date, "
        "primary_position, debut_date, final_game_date, created_at, updated_at"
    )

    def create(
        self,
        *,
        league_id: str,
        full_name: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        suffix: Optional[str] = None,
        birth_date: Optional[str] = None,
        primary_position: Optional[str] = None,
        debut_date: Optional[str] = None,
        final_game_date: Optional[str] = None,
    ) -> Player:
        """Create a player.

        ``suffix`` is stored separately from ``full_name``: "Ken Griffey Jr."
        and "Ken Griffey" are different people, and only a separate suffix makes
        that decidable at match time.
        """

        pid = new_player_id()
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO players "
            "(player_id, league_id, full_name, first_name, last_name, suffix, birth_date, "
            " primary_position, debut_date, final_game_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                league_id,
                full_name,
                first_name,
                last_name,
                suffix,
                birth_date,
                primary_position,
                debut_date,
                final_game_date,
                now,
                now,
            ),
        )
        created = self.get(pid)
        if created is None:  # pragma: no cover - unreachable after the insert
            raise RuntimeError(f"player {pid!r} vanished immediately after insert")
        return created

    def get(self, player_id: str) -> Optional[Player]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM players WHERE player_id = ?", (player_id,)
        )
        return None if row is None else self._to_model(row)

    def list_for_league(self, league_id: str) -> list[Player]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM players WHERE league_id = ? "
                "ORDER BY full_name, player_id",
                (league_id,),
            )
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM players")

    def _to_model(self, row: sqlite3.Row) -> Player:
        return Player(
            player_id=str(row["player_id"]),
            league_id=str(row["league_id"]),
            full_name=str(row["full_name"]),
            first_name=self._opt_str(row, "first_name"),
            last_name=self._opt_str(row, "last_name"),
            suffix=self._opt_str(row, "suffix"),
            birth_date=self._opt_str(row, "birth_date"),
            primary_position=self._opt_str(row, "primary_position"),
            debut_date=self._opt_str(row, "debut_date"),
            final_game_date=self._opt_str(row, "final_game_date"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class PlayerAliasRepositoryProtocol(Protocol):
    """Operations Phase A needs from a player-alias store."""

    def add(
        self,
        *,
        player_id: str,
        league_id: str,
        alias: str,
        alias_type: str = "full",
        provider: str = NO_PROVIDER,
        source: str = "manual",
    ) -> bool: ...

    def list_for_player(self, player_id: str) -> list[PlayerAlias]: ...

    def resolve(
        self, raw_name: str, *, league_id: str, provider: Optional[str] = None
    ) -> AliasResolution: ...

    def mark_ambiguous_duplicates(self, league_id: str) -> int: ...

    def count(self) -> int: ...


class SqlitePlayerAliasRepository(Repository):
    """Player-alias storage and deterministic resolution."""

    _COLUMNS = (
        "alias_id, player_id, league_id, alias, normalized, suffix, alias_type, "
        "provider, is_ambiguous, source, created_at"
    )

    def add(
        self,
        *,
        player_id: str,
        league_id: str,
        alias: str,
        alias_type: str = "full",
        provider: str = NO_PROVIDER,
        source: str = "manual",
    ) -> bool:
        """Add an alias. Returns True if inserted, False if it already existed.

        The generational suffix is split out of ``alias`` by the shared
        normalizer, so "Ronald Acuna Jr." and "Ronald Acuna" store the same
        normalized form with different suffix values.
        """

        parsed = normalize_name(alias)
        if not parsed.normalized:
            raise ValueError(f"alias {alias!r} normalizes to an empty string")
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO player_aliases "
            "(alias_id, player_id, league_id, alias, normalized, suffix, alias_type, "
            " provider, is_ambiguous, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                new_player_alias_id(),
                player_id,
                league_id,
                alias,
                parsed.normalized,
                parsed.suffix,
                alias_type,
                provider,
                source,
                utc_now_iso(),
            ),
        )
        return cursor.rowcount > 0

    def list_for_player(self, player_id: str) -> list[PlayerAlias]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM player_aliases WHERE player_id = ? "
                "ORDER BY alias_type, normalized",
                (player_id,),
            )
        ]

    def resolve(
        self, raw_name: str, *, league_id: str, provider: Optional[str] = None
    ) -> AliasResolution:
        """Resolve a raw player name within one league.

        Two players sharing a normalized name (there are two Jalen Williamses)
        yield ``AMBIGUOUS``, never a guess.
        """

        parsed = normalize_name(raw_name)
        rows = self._fetch_all(
            "SELECT player_id, alias, normalized, suffix, alias_type, provider, is_ambiguous "
            "FROM player_aliases WHERE league_id = ? AND normalized = ?",
            (league_id, parsed.normalized),
        )
        candidates = [
            AliasCandidate(
                entity_id=str(r["player_id"]),
                alias=str(r["alias"]),
                normalized=str(r["normalized"]),
                alias_type=str(r["alias_type"]),
                provider=str(r["provider"]),
                suffix=str(r["suffix"]) or NO_SUFFIX,
                is_ambiguous=bool(r["is_ambiguous"]),
            )
            for r in rows
        ]
        return resolve_alias(raw_name, candidates, provider=provider)

    def mark_ambiguous_duplicates(self, league_id: str) -> int:
        """Flag aliases whose (normalized, suffix) maps to more than one player.

        The suffix participates so "Ken Griffey Jr." is not marked ambiguous
        merely because "Ken Griffey" exists.
        """

        cursor = self._conn.execute(
            "UPDATE player_aliases SET is_ambiguous = 1 "
            "WHERE league_id = ? AND is_ambiguous = 0 AND (normalized, suffix) IN ("
            "    SELECT normalized, suffix FROM player_aliases WHERE league_id = ? "
            "    GROUP BY normalized, suffix HAVING COUNT(DISTINCT player_id) > 1"
            ")",
            (league_id, league_id),
        )
        return cursor.rowcount

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM player_aliases")

    def _to_model(self, row: sqlite3.Row) -> PlayerAlias:
        return PlayerAlias(
            alias_id=str(row["alias_id"]),
            player_id=str(row["player_id"]),
            league_id=str(row["league_id"]),
            alias=str(row["alias"]),
            normalized=str(row["normalized"]),
            suffix=str(row["suffix"]),
            alias_type=str(row["alias_type"]),
            provider=str(row["provider"]),
            is_ambiguous=self._bool(row, "is_ambiguous"),
            source=str(row["source"]),
            created_at=str(row["created_at"]),
        )
