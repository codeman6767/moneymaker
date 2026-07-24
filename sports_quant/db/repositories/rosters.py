"""Roster snapshot repository (append-only, transition-aware, point-in-time).

Anchored on a ``provider_team_references`` row. A roster observation belongs to
its actual observation time; nothing here reinterprets a current roster as an
earlier one.
"""

from __future__ import annotations

from typing import Any, Optional

from ..ids import new_roster_snapshot_id
from ..schema import utc_now_iso
from .base import Repository
from .observations import ObservationOutcome, append_transition, observation_content_hash


class SqliteRosterRepository(Repository):
    """Append-only roster membership observations."""

    def append(
        self,
        *,
        team_ref_id: str,
        provider: str,
        provider_team_id: str,
        provider_player_id: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        player_id: Optional[str] = None,
        roster_date: Optional[str] = None,
        roster_status: Optional[str] = None,
        jersey_number: Optional[str] = None,
        position: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        content = {
            "provider_player_id": provider_player_id, "roster_date": roster_date,
            "roster_status": roster_status, "jersey_number": jersey_number,
            "position": position,
        }
        content_hash = observation_content_hash(content)
        new_id = new_roster_snapshot_id()
        now = utc_now_iso()
        columns = (
            "roster_id", "team_ref_id", "provider", "provider_team_id", "provider_player_id",
            "player_id", "roster_date", "roster_status", "jersey_number", "position",
            "provider_timestamp", "published_at", "observed_at", "ingested_at", "run_id",
            "raw_response_id", "raw_response_hash", "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, team_ref_id, provider, provider_team_id, provider_player_id, player_id,
            roster_date, roster_status, jersey_number, position, provider_timestamp,
            published_at, observed_at, ingested_at, run_id, raw_response_id,
            raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="roster_snapshots", id_column="roster_id",
            anchor_where="team_ref_id = ? AND provider_player_id = ?",
            anchor_params=(team_ref_id, provider_player_id),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM roster_snapshots")
