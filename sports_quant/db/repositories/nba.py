"""NBA-specific append-only observation repositories (Phase D3).

Three tables live in migration d012: quarter/period lines, injury snapshots, and
play snapshots. All follow the shared transition-aware append-only discipline
(see :mod:`.observations`): a new row is written only when its ``content_hash``
differs from the immediate temporal predecessor for the same anchor, so exact
replays collapse, ``A -> B -> A`` keeps all three, and an out-of-order backfill is
compared only against its own temporal neighbour (never regressing current state,
which is always derived from the newest ``observed_at``).

NBA schedule/result/box/roster/lineup data is NOT here -- it reuses the d011
repositories. These three tables cover only what is genuinely NBA-specific.
"""

from __future__ import annotations

from typing import Any, Optional

from ..ids import new_injury_snapshot_id, new_play_snapshot_id, new_quarter_line_id
from ..schema import utc_now_iso
from .base import Repository, RepositoryError
from .observations import ObservationOutcome, append_transition, observation_content_hash


class SqliteQuarterLineRepository(Repository):
    """Append-only per-period line rows (one per game/period/side/observation).

    Only periods the provider actually supplied become rows. ``points`` is
    nullable so an explicit provider zero (a real 0) stays distinct from a missing
    score (NULL); a missing period is simply never appended. Regulation quarters
    and overtime periods are both supported (no four-period assumption).
    """

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        period: int,
        side: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        points: Optional[int] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if side not in ("home", "away"):
            raise RepositoryError(f"side must be 'home'/'away' (got {side!r})")
        if period < 1:
            raise RepositoryError(f"period must be >= 1 (got {period})")
        content = {"period": period, "side": side, "points": points}
        content_hash = observation_content_hash(content)
        new_id = new_quarter_line_id()
        now = utc_now_iso()
        columns = (
            "line_id", "game_ref_id", "provider", "provider_game_id", "period", "side",
            "points", "provider_timestamp", "published_at", "observed_at", "ingested_at",
            "run_id", "raw_response_id", "raw_response_hash", "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, period, side, points,
            provider_timestamp, published_at, observed_at, ingested_at, run_id,
            raw_response_id, raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="nba_quarter_lines", id_column="line_id",
            anchor_where="game_ref_id = ? AND period = ? AND side = ?",
            anchor_params=(game_ref_id, period, side),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM nba_quarter_lines")


class SqliteInjurySnapshotRepository(Repository):
    """Append-only injury observations, anchored on a provider player reference.

    Absence of a row is never "healthy" -- it is unobserved. A supplied-but-missing
    status is stored as the literal ``'unknown'`` (the caller passes it, never a
    fabricated health verdict). A changed status/description/return estimate
    appends a new observation; an identical replay writes nothing; ``A -> B -> A``
    keeps all three; an out-of-order observation is compared to its own temporal
    predecessor and never regresses current state. ``is_correction`` is set only
    from genuine provider evidence (default 0); dates and medical conclusions are
    never invented.
    """

    def append(
        self,
        *,
        player_ref_id: str,
        provider: str,
        provider_player_id: str,
        status: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        player_id: Optional[str] = None,
        provider_team_id: Optional[str] = None,
        team_id: Optional[str] = None,
        game_ref_id: Optional[str] = None,
        description: Optional[str] = None,
        reason: Optional[str] = None,
        return_date: Optional[str] = None,
        is_correction: bool = False,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if not status.strip():
            raise RepositoryError("status must be non-blank ('unknown' for a missing status)")
        content = {
            "status": status, "description": description, "reason": reason,
            "return_date": return_date, "provider_team_id": provider_team_id,
            "game_ref_id": game_ref_id,
        }
        content_hash = observation_content_hash(content)
        new_id = new_injury_snapshot_id()
        now = utc_now_iso()
        columns = (
            "injury_id", "player_ref_id", "provider", "provider_player_id", "player_id",
            "provider_team_id", "team_id", "game_ref_id", "status", "description", "reason",
            "return_date", "is_correction", "provider_timestamp", "published_at", "observed_at",
            "ingested_at", "run_id", "raw_response_id", "raw_response_hash", "content_hash",
            "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, player_ref_id, provider, provider_player_id, player_id, provider_team_id,
            team_id, game_ref_id, status, description, reason, return_date,
            1 if is_correction else 0, provider_timestamp, published_at, observed_at,
            ingested_at, run_id, raw_response_id, raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="injury_snapshots", id_column="injury_id",
            anchor_where="player_ref_id = ?", anchor_params=(player_ref_id,),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM injury_snapshots")


class SqlitePlaySnapshotRepository(Repository):
    """Append-only play-by-play observations, anchored on (game, play identity).

    ``play_identity`` is the provider's own play id when one genuinely exists, else
    a deterministic provider-game-scoped identity the caller derives from stable
    supplied sequence fields. A correction or any changed play content is a new
    observation; an identical replay writes nothing. ``is_substitution`` is set
    only when a play genuinely evidences one (best-effort), never inferred from
    lineup or endpoint access.
    """

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        play_identity: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        provider_play_id: Optional[str] = None,
        period: Optional[int] = None,
        play_sequence: Optional[int] = None,
        clock: Optional[str] = None,
        event_type: Optional[str] = None,
        description: Optional[str] = None,
        provider_team_id: Optional[str] = None,
        provider_player_id: Optional[str] = None,
        is_substitution: bool = False,
        extra: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if not play_identity.strip():
            raise RepositoryError("play_identity must be non-blank")
        content = {
            "provider_play_id": provider_play_id, "period": period,
            "play_sequence": play_sequence, "clock": clock, "event_type": event_type,
            "description": description, "provider_team_id": provider_team_id,
            "provider_player_id": provider_player_id, "is_substitution": is_substitution,
            "extra": extra,
        }
        content_hash = observation_content_hash(content)
        new_id = new_play_snapshot_id()
        now = utc_now_iso()
        columns = (
            "play_id", "game_ref_id", "provider", "provider_game_id", "provider_play_id",
            "play_identity", "period", "play_sequence", "clock", "event_type", "description",
            "provider_team_id", "provider_player_id", "is_substitution", "extra",
            "provider_timestamp", "published_at", "observed_at", "ingested_at", "run_id",
            "raw_response_id", "raw_response_hash", "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, provider_play_id, play_identity,
            period, play_sequence, clock, event_type, description, provider_team_id,
            provider_player_id, 1 if is_substitution else 0, extra, provider_timestamp,
            published_at, observed_at, ingested_at, run_id, raw_response_id, raw_response_hash,
            content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="play_snapshots", id_column="play_id",
            anchor_where="game_ref_id = ? AND play_identity = ?",
            anchor_params=(game_ref_id, play_identity),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM play_snapshots")
