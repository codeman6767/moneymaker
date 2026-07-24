"""Official game snapshot repositories: schedule, result, inning lines.

Append-only, transition-aware (see :mod:`.observations`). Every row is anchored
on a ``provider_game_references`` row (its official identity) and carries the
universal provenance fields; ``observed_at`` (= ``raw_responses.received_at``) is
the point-in-time cutoff. Missing values stay NULL -- the repositories never
coerce a missing statistic to zero.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..ids import new_inning_line_id, new_result_snapshot_id, new_schedule_snapshot_id
from ..schema import utc_now_iso
from .base import Repository
from .observations import ObservationOutcome, append_transition, observation_content_hash


def _result_is_correction(
    predecessor: Optional[sqlite3.Row],
    *,
    home_runs: Optional[int],
    away_runs: Optional[int],
    home_hits: Optional[int],
    away_hits: Optional[int],
    home_errors: Optional[int],
    away_errors: Optional[int],
) -> bool:
    """Whether a new result revises a previously-observed one (a genuine
    correction) rather than being ordinary live progression.

    True only when: the predecessor was ``final`` (a finished game changed), or a
    cumulative run/hit/error total *decreased* while both values are present.
    Increases, inning advances, and status-only transitions return False. No
    predecessor -> the first observation, never a correction.
    """

    if predecessor is None:
        return False
    if str(predecessor["mapped_status"]) == "final":
        return True  # a previously-final result changed
    for old, new in (
        (predecessor["home_runs"], home_runs),
        (predecessor["away_runs"], away_runs),
        (predecessor["home_hits"], home_hits),
        (predecessor["away_hits"], away_hits),
        (predecessor["home_errors"], home_errors),
        (predecessor["away_errors"], away_errors),
    ):
        if old is not None and new is not None and int(new) < int(old):
            return True  # a cumulative total went backwards -> a revision
    return False


class SqliteScheduleRepository(Repository):
    """Append-only official schedule observations."""

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
        season: Optional[int] = None,
        game_type: Optional[str] = None,
        game_date_local: Optional[str] = None,
        scheduled_start: Optional[str] = None,
        home_provider_team_id: Optional[str] = None,
        away_provider_team_id: Optional[str] = None,
        home_team_id: Optional[str] = None,
        away_team_id: Optional[str] = None,
        venue_provider_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        status_code: Optional[str] = None,
        detailed_status: Optional[str] = None,
        game_number: Optional[int] = None,
        doubleheader_code: Optional[str] = None,
        reschedule_info: Optional[str] = None,
        home_probable_pitcher_id: Optional[str] = None,
        away_probable_pitcher_id: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        content = {
            "season": season,
            "game_type": game_type,
            "game_date_local": game_date_local,
            "scheduled_start": scheduled_start,
            "home_provider_team_id": home_provider_team_id,
            "away_provider_team_id": away_provider_team_id,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "venue_provider_id": venue_provider_id,
            "venue_id": venue_id,
            "status_code": status_code,
            "detailed_status": detailed_status,
            "mapped_status": mapped_status,
            "game_number": game_number,
            "doubleheader_code": doubleheader_code,
            "reschedule_info": reschedule_info,
            "home_probable_pitcher_id": home_probable_pitcher_id,
            "away_probable_pitcher_id": away_probable_pitcher_id,
        }
        content_hash = observation_content_hash(content)
        new_id = new_schedule_snapshot_id()
        now = utc_now_iso()
        columns = (
            "schedule_id", "game_ref_id", "provider", "provider_game_id", "season",
            "game_type", "game_date_local", "scheduled_start", "home_provider_team_id",
            "away_provider_team_id", "home_team_id", "away_team_id", "venue_provider_id",
            "venue_id", "status_code", "detailed_status", "mapped_status", "game_number",
            "doubleheader_code", "reschedule_info", "home_probable_pitcher_id",
            "away_probable_pitcher_id", "provider_timestamp", "published_at", "observed_at",
            "ingested_at", "run_id", "raw_response_id", "raw_response_hash", "content_hash",
            "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, season, game_type,
            game_date_local, scheduled_start, home_provider_team_id, away_provider_team_id,
            home_team_id, away_team_id, venue_provider_id, venue_id, status_code,
            detailed_status, mapped_status, game_number, doubleheader_code, reschedule_info,
            home_probable_pitcher_id, away_probable_pitcher_id, provider_timestamp,
            published_at, observed_at, ingested_at, run_id, raw_response_id,
            raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="game_schedule_snapshots", id_column="schedule_id",
            anchor_where="game_ref_id = ?", anchor_params=(game_ref_id,),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM game_schedule_snapshots")


class SqliteResultRepository(Repository):
    """Append-only official result observations (with ``is_correction``)."""

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
        home_runs: Optional[int] = None,
        away_runs: Optional[int] = None,
        home_hits: Optional[int] = None,
        away_hits: Optional[int] = None,
        home_errors: Optional[int] = None,
        away_errors: Optional[int] = None,
        innings_played: Optional[int] = None,
        winning_side: Optional[str] = None,
        result_detail: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome, bool]:
        """Append a result observation; classify genuine corrections. Returns
        ``(id, outcome, is_correction)``.

        Ordinary live-game progression is **not** a correction. Against the
        immediate temporal predecessor (the latest observation at or before
        ``observed_at``), ``is_correction`` is True only when evidence shows a
        previously-observed value was *revised*:

        * a previously **final** result changed at all; or
        * a cumulative **run** total decreased (both values present); or
        * a cumulative **hit** or **error** total decreased (both present).

        Score/hit/error increases, inning advances, and status-only transitions
        (scheduled→pregame→in_progress→final with a logically-consistent
        cumulative result) are normal progression, never corrections. The first
        observation is never a correction; an identical replay writes no row; an
        out-of-order backfill is compared only to its own temporal predecessor, so
        it is neither mislabeled by a *later* observation nor allowed to regress
        current state (which is always derived from the newest ``observed_at``).
        ``is_correction`` is excluded from the content hash.
        """

        content = {
            "home_runs": home_runs, "away_runs": away_runs, "home_hits": home_hits,
            "away_hits": away_hits, "home_errors": home_errors, "away_errors": away_errors,
            "innings_played": innings_played, "winning_side": winning_side,
            "mapped_status": mapped_status, "result_detail": result_detail,
        }
        content_hash = observation_content_hash(content)
        predecessor = self._fetch_one(
            "SELECT content_hash, mapped_status, home_runs, away_runs, home_hits, away_hits, "
            "home_errors, away_errors FROM game_result_snapshots "
            "WHERE game_ref_id = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, result_id DESC LIMIT 1",
            (game_ref_id, observed_at),
        )
        if predecessor is not None and str(predecessor["content_hash"]) == content_hash:
            return None, ObservationOutcome.UNCHANGED, False  # identical to predecessor
        is_correction = _result_is_correction(
            predecessor, home_runs=home_runs, away_runs=away_runs, home_hits=home_hits,
            away_hits=away_hits, home_errors=home_errors, away_errors=away_errors,
        )
        new_id = new_result_snapshot_id()
        now = utc_now_iso()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO game_result_snapshots "
            "(result_id, game_ref_id, provider, provider_game_id, home_runs, away_runs, "
            " home_hits, away_hits, home_errors, away_errors, innings_played, winning_side, "
            " mapped_status, result_detail, is_correction, provider_timestamp, published_at, "
            " observed_at, ingested_at, run_id, raw_response_id, raw_response_hash, content_hash, "
            " created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id, game_ref_id, provider, provider_game_id, home_runs, away_runs,
                home_hits, away_hits, home_errors, away_errors, innings_played, winning_side,
                mapped_status, result_detail, 1 if is_correction else 0, provider_timestamp,
                published_at, observed_at, ingested_at, run_id, raw_response_id,
                raw_response_hash, content_hash, now,
            ),
        )
        if cursor.rowcount == 0:  # exact replay backstopped by the UNIQUE constraint
            return None, ObservationOutcome.UNCHANGED, False
        return new_id, ObservationOutcome.INSERTED, is_correction

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM game_result_snapshots")


class SqliteInningLineRepository(Repository):
    """Append-only per-inning line rows (one per game/inning/side/observation)."""

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        inning: int,
        side: str,
        observed_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        runs: Optional[int] = None,
        hits: Optional[int] = None,
        errors: Optional[int] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], ObservationOutcome]:
        content = {"inning": inning, "side": side, "runs": runs, "hits": hits, "errors": errors}
        content_hash = observation_content_hash(content)
        new_id = new_inning_line_id()
        now = utc_now_iso()
        columns = (
            "line_id", "game_ref_id", "provider", "provider_game_id", "inning", "side",
            "runs", "hits", "errors", "provider_timestamp", "published_at", "observed_at",
            "ingested_at", "run_id", "raw_response_id", "raw_response_hash", "content_hash",
            "created_at",
        )
        values: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, inning, side, runs, hits,
            errors, provider_timestamp, published_at, observed_at, ingested_at, run_id,
            raw_response_id, raw_response_hash, content_hash, now,
        )
        outcome = append_transition(
            self._conn, table="mlb_inning_lines", id_column="line_id",
            anchor_where="game_ref_id = ? AND inning = ? AND side = ?",
            anchor_params=(game_ref_id, inning, side),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values,
        )
        return (new_id if outcome is ObservationOutcome.INSERTED else None), outcome

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM mlb_inning_lines")
