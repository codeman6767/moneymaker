"""Lineup snapshot repository: parent observation + ordered player children.

Append-only, transition-aware on the parent (a changed lineup appends a new
parent + children; an unchanged re-poll collapses). The parent's content hash
covers the ordered players, so any change to the batting order appends. Batting
order is deterministic (children keyed by ``batting_order``). ``is_confirmed`` is
set only when the provider explicitly supplies confirmation -- lineup endpoint
access alone never confirms starters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..ids import new_lineup_player_id, new_lineup_snapshot_id
from ..schema import utc_now_iso
from .base import Repository
from .observations import ObservationOutcome, append_transition, observation_content_hash


@dataclass(frozen=True)
class LineupPlayerInput:
    """One ordered lineup entry as supplied by the provider."""

    batting_order: int
    provider_player_id: str
    position: Optional[str] = None
    is_starter: Optional[bool] = None
    player_id: Optional[str] = None


class SqliteLineupRepository(Repository):
    """Append-only posted-lineup observations with ordered players."""

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        provider_team_id: str,
        players: list[LineupPlayerInput],
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        team_id: Optional[str] = None,
        home_away: Optional[str] = None,
        is_confirmed: bool = False,
        confirmed_at: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome, int]:
        ordered = sorted(players, key=lambda p: p.batting_order)
        content = {
            "provider_team_id": provider_team_id,
            "home_away": home_away,
            "is_confirmed": is_confirmed,
            "players": [
                {
                    "batting_order": p.batting_order,
                    "provider_player_id": p.provider_player_id,
                    "position": p.position,
                    "is_starter": p.is_starter,
                }
                for p in ordered
            ],
        }
        content_hash = observation_content_hash(content)
        new_id = new_lineup_snapshot_id()
        now = utc_now_iso()
        columns = (
            "lineup_id", "game_ref_id", "provider", "provider_game_id", "provider_team_id",
            "team_id", "home_away", "is_confirmed", "confirmed_at", "player_count",
            "provider_timestamp", "published_at", "observed_at", "ingested_at", "run_id",
            "raw_response_id", "raw_response_hash", "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, provider_team_id, team_id,
            home_away, 1 if is_confirmed else 0, confirmed_at, len(ordered),
            provider_timestamp, published_at, observed_at, ingested_at, run_id,
            raw_response_id, raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="lineup_snapshots", id_column="lineup_id",
            anchor_where="game_ref_id = ? AND provider_team_id = ?",
            anchor_params=(game_ref_id, provider_team_id),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        if outcome is not ObservationOutcome.INSERTED:
            return None, outcome, 0

        inserted = 0
        for p in ordered:
            starter_db = None if p.is_starter is None else (1 if p.is_starter else 0)
            self._conn.execute(
                "INSERT INTO lineup_players "
                "(lineup_player_id, lineup_id, batting_order, provider_player_id, player_id, "
                " position, is_starter, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_lineup_player_id(), new_id, p.batting_order, p.provider_player_id,
                    p.player_id, p.position, starter_db, now,
                ),
            )
            inserted += 1
        return new_id, outcome, inserted

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM lineup_snapshots")

    def count_players(self) -> int:
        return self._count("SELECT COUNT(*) FROM lineup_players")
