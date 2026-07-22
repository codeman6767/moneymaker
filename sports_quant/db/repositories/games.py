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
    """Hash of the *state* an observation reports.

    Covers the reported state only, deliberately excluding ``observed_at``: a
    re-poll returning an unchanged state is not new information.

    Note that this is a state hash, not an observation identity. Two
    observations of the same state at different times hash identically **on
    purpose** -- that is what lets the repository detect "nothing changed". It
    is emphatically *not* a global uniqueness key: a game that goes
    ``delayed -> in_progress -> delayed`` reports the same state twice, and both
    must be recorded. See :meth:`SqliteGameRepository.record_status`.

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
        """Append a status observation and refresh the game's current state.

        Returns True when a history row was appended, False when the
        observation reported no change.

        **Transition-aware deduplication.** The observation is compared against
        the one immediately *preceding* it in time from the same provider. An
        unchanged re-poll is skipped; a genuine return to an earlier state
        (``delayed -> in_progress -> delayed``, an ordinary rain delay that
        resumes and re-delays) is appended, because its predecessor differs.
        Comparing against the whole history instead would silently drop that
        third observation.

        **Stale backfill cannot regress current state.** Current state is
        recomputed from the newest observation by ``(observed_at, status_id)``
        after every insert, not copied from the row just written. A late-arriving
        observation of an *earlier* moment is preserved in history but does not
        overwrite a newer state.

        ``observed_at`` is when *we* learned this -- the point-in-time cutoff
        column. It is never back-dated to the provider's timestamp.

        The history insert and the current-state recomputation share one
        transaction, so the two can never diverge.
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
            if self._is_unchanged_from_predecessor(
                game_id=game_id,
                provider=provider,
                observed_at=observed_at,
                content_hash=content,
            ):
                return False

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
                self._refresh_current_state(game_id, now)
        return inserted

    def _is_unchanged_from_predecessor(
        self, *, game_id: str, provider: str, observed_at: str, content_hash: str
    ) -> bool:
        """Whether this observation reports the same state as the one before it.

        "Before it" means the latest observation from the same provider at or
        before ``observed_at`` -- so a backfilled row is compared against its
        own temporal neighbour rather than against the newest row overall.
        """

        row = self._fetch_one(
            "SELECT content_hash FROM game_status_history "
            "WHERE game_id = ? AND provider = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, status_id DESC LIMIT 1",
            (game_id, provider, observed_at),
        )
        return row is not None and str(row["content_hash"]) == content_hash

    def _refresh_current_state(self, game_id: str, now: str) -> None:
        """Set the game's current state from its newest observation.

        Ordered by ``observed_at`` then ``status_id``; ULIDs are creation-
        ordered, so observations sharing a timestamp resolve deterministically
        to the most recently recorded one.

        ``original_start`` is never touched -- it is the anchor for "was this
        game moved?", and migration a003 makes that immutability a database
        rule rather than a convention.
        """

        latest = self._fetch_one(
            "SELECT status, scheduled_start FROM game_status_history WHERE game_id = ? "
            "ORDER BY observed_at DESC, status_id DESC LIMIT 1",
            (game_id,),
        )
        if latest is None:  # pragma: no cover - a row was just inserted
            return
        self._conn.execute(
            "UPDATE games SET status = ?, scheduled_start = ?, updated_at = ? WHERE game_id = ?",
            (str(latest["status"]), str(latest["scheduled_start"]), now, game_id),
        )

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
