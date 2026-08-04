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
        # The anchor must include ``roster_date``: a roster snapshot's state is per
        # (team, player, DATE), and ``roster_date`` is part of the content hash. With
        # the date left out of the anchor, observing 06-25 and then re-observing
        # 06-24 compared 06-24's content against 06-25's and read the difference as
        # a state change, appending a duplicate row -- so the row count depended on
        # the order games were ingested (found by the F1 June-2026 month review,
        # where a doubleheader made two units fetch the same team-day roster).
        outcome = append_transition(
            self._conn, table="roster_snapshots", id_column="roster_id",
            anchor_where=("team_ref_id = ? AND provider_player_id = ? "
                          "AND roster_date IS ?"),
            anchor_params=(team_ref_id, provider_player_id, roster_date),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM roster_snapshots")
