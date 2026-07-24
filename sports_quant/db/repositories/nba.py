"""NBA-specific append-only observation repositories (Phase D3).

Migration d012 supplies quarter/period lines, injury snapshots, and play
snapshots; the d013 repair adds sport-correct NBA game results, team statistics,
and player statistics (points/period and ``stat_group IN ('traditional',
'advanced')`` -- never baseball runs/innings or batting/pitching). All follow the
shared transition-aware append-only discipline (see :mod:`.observations`): a new
row is written only when its ``content_hash`` differs from the immediate temporal
predecessor for the same anchor, so exact replays collapse, ``A -> B -> A`` keeps
all three, and an out-of-order backfill is compared only against its own temporal
neighbour (never regressing current state, always derived from the newest
``observed_at``).

Only the cross-sport schedule and lineup data still reuse d011 repositories; the
baseball-named result/box tables are MLB-only again.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..ids import (
    new_injury_snapshot_id,
    new_nba_player_stat_id,
    new_nba_result_id,
    new_nba_team_stat_id,
    new_play_snapshot_id,
    new_quarter_line_id,
)
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
        return_estimate: Optional[str] = None,
        is_correction: bool = False,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if not status.strip():
            raise RepositoryError("status must be non-blank ('unknown' for a missing status)")
        content = {
            "status": status, "description": description, "reason": reason,
            "return_date": return_date, "return_estimate": return_estimate,
            "provider_team_id": provider_team_id, "game_ref_id": game_ref_id,
        }
        content_hash = observation_content_hash(content)
        new_id = new_injury_snapshot_id()
        now = utc_now_iso()
        columns = (
            "injury_id", "player_ref_id", "provider", "provider_player_id", "player_id",
            "provider_team_id", "team_id", "game_ref_id", "status", "description", "reason",
            "return_date", "return_estimate", "is_correction", "provider_timestamp",
            "published_at", "observed_at", "ingested_at", "run_id", "raw_response_id",
            "raw_response_hash", "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, player_ref_id, provider, provider_player_id, player_id, provider_team_id,
            team_id, game_ref_id, status, description, reason, return_date, return_estimate,
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


# --------------------------------------------------------------------------- #
# NBA-typed results + team/player statistics (d013 repair)
# --------------------------------------------------------------------------- #
def _nba_result_is_correction(
    predecessor: Optional[sqlite3.Row],
    *,
    home_points: Optional[int],
    away_points: Optional[int],
    period: Optional[int],
    winning_side: Optional[str],
) -> bool:
    """Whether a new NBA result *revises* a previously-observed one.

    Mirrors the corrected D2 result semantics on NBA fields: a correction requires
    a substantive value to change. Specifically:

    * a previously-``final`` observation is a correction only when a substantive
      value (home/away points, period, or winning side) actually differs -- a
      status-wording-only change is not; or
    * (for any predecessor) a cumulative points total *decreased* while both the
      old and new values are present.

    Increases, period advances, and status-only transitions return False. No
    predecessor -> the first observation, never a correction.
    """

    if predecessor is None:
        return False
    if str(predecessor["mapped_status"]) == "final":
        for column, new in (("home_points", home_points), ("away_points", away_points),
                            ("period", period)):
            old = predecessor[column]
            old_int = None if old is None else int(old)
            if old_int != new:
                return True
        old_side = predecessor["winning_side"]
        old_side_str = None if old_side is None else str(old_side)
        return old_side_str != winning_side
    for old, new in ((predecessor["home_points"], home_points),
                     (predecessor["away_points"], away_points)):
        if old is not None and new is not None and int(new) < int(old):
            return True  # a cumulative points total went backwards -> a revision
    return False


class SqliteNbaResultRepository(Repository):
    """Append-only NBA game results (POINTS + PERIOD), with ``is_correction``.

    NBA scores are points and the game clock unit is a period -- never runs or
    innings. Correction detection compares the substantive cumulative points and
    the winner against the immediate temporal predecessor, so normal live
    progression (rising score, advancing period, status advancing) is not a
    correction while a previously-final revision or a points decrease is.
    """

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        mapped_status: str,
        home_points: Optional[int] = None,
        away_points: Optional[int] = None,
        period: Optional[int] = None,
        winning_side: Optional[str] = None,
        result_detail: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome, bool]:
        content = {
            "home_points": home_points, "away_points": away_points, "period": period,
            "winning_side": winning_side, "mapped_status": mapped_status,
            "result_detail": result_detail,
        }
        content_hash = observation_content_hash(content)
        predecessor = self._fetch_one(
            "SELECT content_hash, mapped_status, home_points, away_points, period, winning_side "
            "FROM nba_game_results WHERE game_ref_id = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, result_id DESC LIMIT 1",
            (game_ref_id, observed_at),
        )
        if predecessor is not None and str(predecessor["content_hash"]) == content_hash:
            return None, ObservationOutcome.UNCHANGED, False
        is_correction = _nba_result_is_correction(
            predecessor, home_points=home_points, away_points=away_points, period=period,
            winning_side=winning_side,
        )
        new_id = new_nba_result_id()
        now = utc_now_iso()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO nba_game_results "
            "(result_id, game_ref_id, provider, provider_game_id, home_points, away_points, "
            " period, winning_side, mapped_status, result_detail, is_correction, "
            " provider_timestamp, published_at, observed_at, ingested_at, run_id, "
            " raw_response_id, raw_response_hash, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id, game_ref_id, provider, provider_game_id, home_points, away_points,
                period, winning_side, mapped_status, result_detail, 1 if is_correction else 0,
                provider_timestamp, published_at, observed_at, ingested_at, run_id,
                raw_response_id, raw_response_hash, content_hash, now,
            ),
        )
        if cursor.rowcount == 0:  # exact replay backstopped by the UNIQUE constraint
            return None, ObservationOutcome.UNCHANGED, False
        return new_id, ObservationOutcome.INSERTED, is_correction

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM nba_game_results")


class SqliteNbaTeamStatRepository(Repository):
    """Append-only NBA team box lines (POINTS + sport-neutral JSON ``stats``)."""

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
        points: Optional[int] = None,
        stats: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if home_away not in ("home", "away"):
            raise RepositoryError(f"home_away must be 'home'/'away' (got {home_away!r})")
        content = {"provider_team_id": provider_team_id, "home_away": home_away,
                   "points": points, "stats": stats}
        content_hash = observation_content_hash(content)
        new_id = new_nba_team_stat_id()
        now = utc_now_iso()
        columns = (
            "stat_id", "game_ref_id", "provider", "provider_game_id", "provider_team_id",
            "team_id", "home_away", "points", "stats", "provider_timestamp", "published_at",
            "observed_at", "ingested_at", "run_id", "raw_response_id", "raw_response_hash",
            "content_hash", "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, provider_team_id, team_id,
            home_away, points, stats, provider_timestamp, published_at, observed_at,
            ingested_at, run_id, raw_response_id, raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="nba_team_statistics", id_column="stat_id",
            anchor_where="game_ref_id = ? AND provider_team_id = ?",
            anchor_params=(game_ref_id, provider_team_id),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM nba_team_statistics")


class SqliteNbaPlayerStatRepository(Repository):
    """Append-only NBA player box lines.

    ``stat_group`` is an NBA-appropriate discriminator (``'traditional'`` or
    ``'advanced'``) -- never baseball ``'batting'``/``'pitching'``. The two groups
    keep distinct transition anchors so re-polls stay idempotent.
    """

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        provider_player_id: str,
        stat_group: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        player_id: Optional[str] = None,
        provider_team_id: Optional[str] = None,
        team_id: Optional[str] = None,
        position: Optional[str] = None,
        is_starter: Optional[bool] = None,
        points: Optional[int] = None,
        stats: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        if stat_group not in ("traditional", "advanced"):
            raise RepositoryError(
                f"stat_group must be 'traditional'/'advanced' (got {stat_group!r})"
            )
        content = {
            "provider_player_id": provider_player_id, "stat_group": stat_group,
            "provider_team_id": provider_team_id, "position": position,
            "is_starter": is_starter, "points": points, "stats": stats,
        }
        content_hash = observation_content_hash(content)
        new_id = new_nba_player_stat_id()
        now = utc_now_iso()
        columns = (
            "stat_id", "game_ref_id", "provider", "provider_game_id", "provider_player_id",
            "player_id", "provider_team_id", "team_id", "stat_group", "position", "is_starter",
            "points", "stats", "provider_timestamp", "published_at", "observed_at",
            "ingested_at", "run_id", "raw_response_id", "raw_response_hash", "content_hash",
            "created_at",
        )
        starter_db = None if is_starter is None else (1 if is_starter else 0)
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, provider_player_id, player_id,
            provider_team_id, team_id, stat_group, position, starter_db, points, stats,
            provider_timestamp, published_at, observed_at, ingested_at, run_id,
            raw_response_id, raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="nba_player_statistics", id_column="stat_id",
            anchor_where="game_ref_id = ? AND provider_player_id = ? AND stat_group = ?",
            anchor_params=(game_ref_id, provider_player_id, stat_group),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM nba_player_statistics")
