"""Game repository, including the append-only status history.

``games`` holds current state; ``game_status_history`` holds every state it ever
had. A postponement updates ``games.scheduled_start`` *and* appends a history
row, so "what did we believe at time T?" stays answerable after the fact.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional, Protocol

from streaming.event_envelope import canonical_json

from ..engine import transaction
from ..ids import new_game_id, new_game_status_id
from ..models import Game, GameStatusRecord
from ..schema import utc_now_iso
from .base import Repository, to_db_bool


def status_content_hash(
    *,
    status: str,
    scheduled_start: str,
    detail: Optional[str],
    provider_timestamp: Optional[str],
) -> str:
    """Content hash of one status observation.

    Covers the observation's *content* only, so the same provider re-reporting
    an unchanged status hashes identically and is skipped rather than appended
    twice. Deliberately excludes ``observed_at`` -- a re-poll is not new
    information.

    Uses the shared ``canonical_json`` from ``streaming.event_envelope`` rather
    than a fourth in-repo canonicalizer: two canonicalizers that disagree
    produce two hashes for the same content and silently defeat deduplication.
    """

    payload = {
        "status": status,
        "scheduled_start": scheduled_start,
        "detail": detail,
        "provider_timestamp": provider_timestamp,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class GameRepositoryProtocol(Protocol):
    """Operations Phase A needs from a game store."""

    def create(
        self,
        *,
        league_id: str,
        season_id: str,
        home_team_id: str,
        away_team_id: str,
        scheduled_start: str,
        game_date_local: str,
        status: str = "scheduled",
        game_number: int = 1,
        doubleheader_type: Optional[str] = None,
        venue: Optional[str] = None,
        is_neutral_site: bool = False,
        official_provider: Optional[str] = None,
        official_game_key: Optional[str] = None,
    ) -> Game: ...

    def get(self, game_id: str) -> Optional[Game]: ...

    def list_for_season(self, season_id: str) -> list[Game]: ...

    def record_status(
        self,
        *,
        game_id: str,
        status: str,
        scheduled_start: str,
        provider: str,
        observed_at: str,
        detail: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
    ) -> bool: ...

    def status_history(self, game_id: str) -> list[GameStatusRecord]: ...

    def status_as_of(self, game_id: str, as_of: str) -> Optional[GameStatusRecord]: ...

    def count(self) -> int: ...


class SqliteGameRepository(Repository):
    """Game storage plus append-only status history."""

    _COLUMNS = (
        "game_id, league_id, season_id, home_team_id, away_team_id, scheduled_start, "
        "original_start, game_date_local, game_number, doubleheader_type, venue, "
        "is_neutral_site, status, official_provider, official_game_key, created_at, updated_at"
    )

    _HISTORY_COLUMNS = (
        "status_id, game_id, status, scheduled_start, detail, provider, provider_timestamp, "
        "observed_at, ingested_at, raw_response_id, raw_response_hash, content_hash, created_at"
    )

    def create(
        self,
        *,
        league_id: str,
        season_id: str,
        home_team_id: str,
        away_team_id: str,
        scheduled_start: str,
        game_date_local: str,
        status: str = "scheduled",
        game_number: int = 1,
        doubleheader_type: Optional[str] = None,
        venue: Optional[str] = None,
        is_neutral_site: bool = False,
        official_provider: Optional[str] = None,
        official_game_key: Optional[str] = None,
    ) -> Game:
        """Create a game.

        ``original_start`` is set from ``scheduled_start`` here and never
        updated again, so a later reschedule stays visible without scanning
        history.
        """

        gid = new_game_id()
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO games "
            "(game_id, league_id, season_id, home_team_id, away_team_id, scheduled_start, "
            " original_start, game_date_local, game_number, doubleheader_type, venue, "
            " is_neutral_site, status, official_provider, official_game_key, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gid,
                league_id,
                season_id,
                home_team_id,
                away_team_id,
                scheduled_start,
                scheduled_start,
                game_date_local,
                game_number,
                doubleheader_type,
                venue,
                to_db_bool(is_neutral_site),
                status,
                official_provider,
                official_game_key,
                now,
                now,
            ),
        )
        created = self.get(gid)
        if created is None:  # pragma: no cover - unreachable after the insert
            raise RuntimeError(f"game {gid!r} vanished immediately after insert")
        return created

    def get(self, game_id: str) -> Optional[Game]:
        row = self._fetch_one(f"SELECT {self._COLUMNS} FROM games WHERE game_id = ?", (game_id,))
        return None if row is None else self._to_model(row)

    def list_for_season(self, season_id: str) -> list[Game]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM games WHERE season_id = ? "
                "ORDER BY scheduled_start, game_number",
                (season_id,),
            )
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM games")

    # -- Status transitions --------------------------------------------------
    def record_status(
        self,
        *,
        game_id: str,
        status: str,
        scheduled_start: str,
        provider: str,
        observed_at: str,
        detail: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
    ) -> bool:
        """Append a status observation and update the game's current state.

        Two writes, one transaction: the history row and the current-state
        update must not diverge. Returns True when a history row was appended,
        False when this exact observation was already recorded.

        ``observed_at`` is when *we* learned this -- the point-in-time cutoff
        column. It is never back-dated to the provider's timestamp.
        """

        content = status_content_hash(
            status=status,
            scheduled_start=scheduled_start,
            detail=detail,
            provider_timestamp=provider_timestamp,
        )
        now = utc_now_iso()

        # transaction() joins an already-open transaction rather than nesting,
        # so a caller batching several games still gets one atomic unit.
        with transaction(self._conn):
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO game_status_history "
                "(status_id, game_id, status, scheduled_start, detail, provider, "
                " provider_timestamp, observed_at, ingested_at, raw_response_id, "
                " raw_response_hash, content_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    new_game_status_id(),
                    game_id,
                    status,
                    scheduled_start,
                    detail,
                    provider,
                    provider_timestamp,
                    observed_at,
                    now,
                    content,
                    now,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                self._conn.execute(
                    "UPDATE games SET status = ?, scheduled_start = ?, updated_at = ? "
                    "WHERE game_id = ?",
                    (status, scheduled_start, now, game_id),
                )
        return inserted

    def status_history(self, game_id: str) -> list[GameStatusRecord]:
        """Every status observation for a game, oldest observation first."""

        return [
            self._to_status_model(r)
            for r in self._fetch_all(
                f"SELECT {self._HISTORY_COLUMNS} FROM game_status_history WHERE game_id = ? "
                "ORDER BY observed_at, status_id",
                (game_id,),
            )
        ]

    def status_as_of(self, game_id: str, as_of: str) -> Optional[GameStatusRecord]:
        """The latest status observed at or before ``as_of``.

        This is the point-in-time accessor. It filters on ``observed_at`` --
        never on ``provider_timestamp``, which a provider can back-date, and
        never on ``games.status``, which reflects now rather than then.

        Ties on ``observed_at`` break by ``status_id``; ULIDs are creation-
        ordered, so a rebuild yields the same answer.
        """

        row = self._fetch_one(
            f"SELECT {self._HISTORY_COLUMNS} FROM game_status_history "
            "WHERE game_id = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, status_id DESC LIMIT 1",
            (game_id, as_of),
        )
        return None if row is None else self._to_status_model(row)

    def count_status_records(self) -> int:
        return self._count("SELECT COUNT(*) FROM game_status_history")

    # -- Mapping -------------------------------------------------------------
    def _to_model(self, row: sqlite3.Row) -> Game:
        return Game(
            game_id=str(row["game_id"]),
            league_id=str(row["league_id"]),
            season_id=str(row["season_id"]),
            home_team_id=str(row["home_team_id"]),
            away_team_id=str(row["away_team_id"]),
            scheduled_start=str(row["scheduled_start"]),
            original_start=str(row["original_start"]),
            game_date_local=str(row["game_date_local"]),
            game_number=int(row["game_number"]),
            doubleheader_type=self._opt_str(row, "doubleheader_type"),
            venue=self._opt_str(row, "venue"),
            is_neutral_site=self._bool(row, "is_neutral_site"),
            status=str(row["status"]),
            official_provider=self._opt_str(row, "official_provider"),
            official_game_key=self._opt_str(row, "official_game_key"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _to_status_model(self, row: sqlite3.Row) -> GameStatusRecord:
        return GameStatusRecord(
            status_id=str(row["status_id"]),
            game_id=str(row["game_id"]),
            status=str(row["status"]),
            scheduled_start=str(row["scheduled_start"]),
            detail=self._opt_str(row, "detail"),
            provider=str(row["provider"]),
            provider_timestamp=self._opt_str(row, "provider_timestamp"),
            observed_at=str(row["observed_at"]),
            ingested_at=str(row["ingested_at"]),
            raw_response_id=self._opt_str(row, "raw_response_id"),
            raw_response_hash=self._opt_str(row, "raw_response_hash"),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
        )
