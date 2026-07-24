"""Team + player game-statistics repositories (append-only, transition-aware).

Anchored on a ``provider_game_references`` row; provider team/player ids stay
provider ids with NULLABLE canonical ids (resolution is D5). Missing values stay
NULL -- only a provider-supplied zero is a zero. Batting and pitching stats are
kept clearly typed via the ``role`` discriminator and separate JSON blocks.
"""

from __future__ import annotations

from typing import Any, Optional

from ..ids import new_player_game_stat_id, new_team_game_stat_id
from ..schema import utc_now_iso
from .base import Repository, RepositoryError
from .observations import ObservationOutcome, append_transition, observation_content_hash


class SqliteTeamGameStatRepository(Repository):
    """Append-only team box lines."""

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        provider_team_id: str,
        home_away: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        team_id: Optional[str] = None,
        runs: Optional[int] = None,
        hits: Optional[int] = None,
        errors: Optional[int] = None,
        at_bats: Optional[int] = None,
        extra: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if home_away not in ("home", "away"):
            raise RepositoryError(f"home_away must be 'home'/'away' (got {home_away!r})")
        content = {
            "provider_team_id": provider_team_id, "home_away": home_away, "runs": runs,
            "hits": hits, "errors": errors, "at_bats": at_bats, "extra": extra,
        }
        content_hash = observation_content_hash(content)
        new_id = new_team_game_stat_id()
        now = utc_now_iso()
        columns = (
            "stat_id", "game_ref_id", "provider", "provider_game_id", "provider_team_id",
            "team_id", "home_away", "runs", "hits", "errors", "at_bats", "extra",
            "provider_timestamp", "published_at", "observed_at", "ingested_at", "run_id",
            "raw_response_id", "raw_response_hash", "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, provider_team_id, team_id,
            home_away, runs, hits, errors, at_bats, extra, provider_timestamp, published_at,
            observed_at, ingested_at, run_id, raw_response_id, raw_response_hash,
            content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="team_game_statistics", id_column="stat_id",
            anchor_where="game_ref_id = ? AND provider_team_id = ?",
            anchor_params=(game_ref_id, provider_team_id),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM team_game_statistics")


class SqlitePlayerGameStatRepository(Repository):
    """Append-only player box lines (batting or pitching)."""

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        provider_player_id: str,
        role: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        player_id: Optional[str] = None,
        provider_team_id: Optional[str] = None,
        team_id: Optional[str] = None,
        is_starter: Optional[bool] = None,
        batting_order: Optional[int] = None,
        position: Optional[str] = None,
        batting_stats: Optional[str] = None,
        pitching_stats: Optional[str] = None,
        extra: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if role not in ("batting", "pitching"):
            raise RepositoryError(f"role must be 'batting'/'pitching' (got {role!r})")
        content = {
            "provider_player_id": provider_player_id, "role": role,
            "provider_team_id": provider_team_id, "is_starter": is_starter,
            "batting_order": batting_order, "position": position,
            "batting_stats": batting_stats, "pitching_stats": pitching_stats, "extra": extra,
        }
        content_hash = observation_content_hash(content)
        new_id = new_player_game_stat_id()
        now = utc_now_iso()
        columns = (
            "stat_id", "game_ref_id", "provider", "provider_game_id", "provider_player_id",
            "player_id", "provider_team_id", "team_id", "role", "is_starter", "batting_order",
            "position", "batting_stats", "pitching_stats", "extra", "provider_timestamp",
            "published_at", "observed_at", "ingested_at", "run_id", "raw_response_id",
            "raw_response_hash", "content_hash", "created_at",
        )
        starter_db = None if is_starter is None else (1 if is_starter else 0)
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, provider_player_id, player_id,
            provider_team_id, team_id, role, starter_db, batting_order, position,
            batting_stats, pitching_stats, extra, provider_timestamp, published_at,
            observed_at, ingested_at, run_id, raw_response_id, raw_response_hash,
            content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="player_game_statistics", id_column="stat_id",
            anchor_where="game_ref_id = ? AND provider_player_id = ? AND role = ?",
            anchor_params=(game_ref_id, provider_player_id, role),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM player_game_statistics")
