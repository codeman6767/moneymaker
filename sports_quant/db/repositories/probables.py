"""Probable-pitcher snapshot repository (append-only, transition-aware).

One announcement timeline per game/side: a change appends a new observation and
never overwrites the previous probable. ``status`` is ``probable`` unless the
provider explicitly supplies ``confirmed``/``scratched``. Missing probable data
is simply not recorded (it stays unknown), never fabricated.
"""

from __future__ import annotations

from typing import Any, Optional

from ..ids import new_probable_pitcher_id
from ..schema import utc_now_iso
from .base import Repository, RepositoryError
from .observations import ObservationOutcome, append_transition, observation_content_hash

_STATUSES = ("probable", "confirmed", "scratched")


class SqliteProbablePitcherRepository(Repository):
    """Append-only probable-pitcher observations."""

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        side: str,
        provider_player_id: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        player_id: Optional[str] = None,
        status: str = "probable",
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if side not in ("home", "away"):
            raise RepositoryError(f"side must be 'home'/'away' (got {side!r})")
        if status not in _STATUSES:
            raise RepositoryError(f"status must be one of {_STATUSES} (got {status!r})")
        content = {
            "side": side, "provider_player_id": provider_player_id, "status": status,
        }
        content_hash = observation_content_hash(content)
        new_id = new_probable_pitcher_id()
        now = utc_now_iso()
        columns = (
            "probable_id", "game_ref_id", "provider", "provider_game_id", "side",
            "provider_player_id", "player_id", "status", "provider_timestamp", "published_at",
            "observed_at", "ingested_at", "run_id", "raw_response_id", "raw_response_hash",
            "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, side, provider_player_id,
            player_id, status, provider_timestamp, published_at, observed_at, ingested_at,
            run_id, raw_response_id, raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="probable_pitcher_snapshots", id_column="probable_id",
            anchor_where="game_ref_id = ? AND side = ?", anchor_params=(game_ref_id, side),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM probable_pitcher_snapshots")
