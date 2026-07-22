"""League and season repositories."""

from __future__ import annotations

import sqlite3
from typing import Optional, Protocol

from ..ids import league_id as make_league_id
from ..ids import season_id as make_season_id
from ..models import League, Season
from ..schema import utc_now_iso
from .base import Repository


class LeagueRepositoryProtocol(Protocol):
    """Operations Phase A needs from a league store."""

    def upsert(self, *, code: str, name: str, sport: str) -> League: ...

    def get(self, league_id: str) -> Optional[League]: ...

    def get_by_code(self, code: str) -> Optional[League]: ...

    def list_all(self) -> list[League]: ...

    def count(self) -> int: ...


class SqliteLeagueRepository(Repository):
    """League storage. The canonical ``league_id`` is derived from the code."""

    _COLUMNS = "league_id, code, name, sport, created_at, updated_at"

    def upsert(self, *, code: str, name: str, sport: str) -> League:
        """Insert a league, or return the existing row unchanged.

        Idempotent by design: re-seeding must never duplicate or clobber. The
        canonical id is deterministic, so a re-run resolves to the same row.
        """

        lid = make_league_id(code)
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(code) DO NOTHING",
            (lid, code, name, sport, now, now),
        )
        existing = self.get_by_code(code)
        if existing is None:  # pragma: no cover - unreachable after the insert
            raise RuntimeError(f"league {code!r} vanished immediately after upsert")
        return existing

    def get(self, league_id: str) -> Optional[League]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM leagues WHERE league_id = ?", (league_id,)
        )
        return None if row is None else self._to_model(row)

    def get_by_code(self, code: str) -> Optional[League]:
        row = self._fetch_one(f"SELECT {self._COLUMNS} FROM leagues WHERE code = ?", (code,))
        return None if row is None else self._to_model(row)

    def list_all(self) -> list[League]:
        return [
            self._to_model(r)
            for r in self._fetch_all(f"SELECT {self._COLUMNS} FROM leagues ORDER BY code")
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM leagues")

    @staticmethod
    def _to_model(row: sqlite3.Row) -> League:
        return League(
            league_id=str(row["league_id"]),
            code=str(row["code"]),
            name=str(row["name"]),
            sport=str(row["sport"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class SeasonRepositoryProtocol(Protocol):
    """Operations Phase A needs from a season store."""

    def upsert(
        self,
        *,
        league_code: str,
        league_id: str,
        year: int,
        phase: str,
        label: str,
        start_date: str,
        end_date: Optional[str] = None,
    ) -> Season: ...

    def get(self, season_id: str) -> Optional[Season]: ...

    def find(self, *, league_id: str, year: int, phase: str) -> Optional[Season]: ...

    def list_for_league(self, league_id: str) -> list[Season]: ...

    def count(self) -> int: ...


class SqliteSeasonRepository(Repository):
    """Season storage. Seasons are not seeded; Phase D populates them."""

    _COLUMNS = (
        "season_id, league_id, year, label, phase, start_date, end_date, created_at, updated_at"
    )

    def upsert(
        self,
        *,
        league_code: str,
        league_id: str,
        year: int,
        phase: str,
        label: str,
        start_date: str,
        end_date: Optional[str] = None,
    ) -> Season:
        sid = make_season_id(league_code, year, phase)
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO seasons "
            "(season_id, league_id, year, label, phase, start_date, end_date, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(league_id, year, phase) DO NOTHING",
            (sid, league_id, year, label, phase, start_date, end_date, now, now),
        )
        existing = self.find(league_id=league_id, year=year, phase=phase)
        if existing is None:  # pragma: no cover - unreachable after the insert
            raise RuntimeError(f"season {sid!r} vanished immediately after upsert")
        return existing

    def get(self, season_id: str) -> Optional[Season]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM seasons WHERE season_id = ?", (season_id,)
        )
        return None if row is None else self._to_model(row)

    def find(self, *, league_id: str, year: int, phase: str) -> Optional[Season]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM seasons "
            "WHERE league_id = ? AND year = ? AND phase = ?",
            (league_id, year, phase),
        )
        return None if row is None else self._to_model(row)

    def list_for_league(self, league_id: str) -> list[Season]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM seasons WHERE league_id = ? "
                "ORDER BY year, phase",
                (league_id,),
            )
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM seasons")

    def _to_model(self, row: sqlite3.Row) -> Season:
        return Season(
            season_id=str(row["season_id"]),
            league_id=str(row["league_id"]),
            year=int(row["year"]),
            label=str(row["label"]),
            phase=str(row["phase"]),
            start_date=str(row["start_date"]),
            end_date=self._opt_str(row, "end_date"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
