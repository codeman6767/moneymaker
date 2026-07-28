"""Phase E2 historical point-in-time row layer (offline, read-only).

Builds real pregame training ROWS from persisted canonical games and append-only
observations, using ONLY the E1 feature-facing accessors, the fail-closed
safe-join registry, and explicit UTC cutoffs. There is no feature engineering
here: a row carries identity, a deterministic pregame cutoff, the eventual
home-win label (the only permitted future value), and cutoff-known ``score_diff``
/ ``phase`` (both 0 for a pregame cutoff). Feature vectors are a later phase.

Row-generation policy (default: one pregame row per proven game)
----------------------------------------------------------------
For each canonical game with an official provider identity:

* ``game_ref_id`` is resolved via :meth:`AsOfReader.game_provider_reference`,
  which is gated on the accepted ``entity_type='game'`` decision being decided by
  the cutoff (a link learned later is invisible).
* The feature cutoff ``t_cut`` is the game's SCHEDULED START, read from the
  append-only schedule snapshot as of the label horizon (an actual corpus
  observation value, never synthesized).
* The label is the final result observation (MLB ``game_result_snapshots`` / NBA
  ``nba_game_results``, correction-aware and fail-closed on equal-time conflicts)
  whose ``observed_at`` is STRICTLY AFTER ``t_cut``; the result must also be
  invisible when read as of ``t_cut`` (leakage guard). Non-final / tie / missing
  / conflicting results yield no label and the game is excluded (never fabricated).

All timestamps compared are transaction times (``observed_at`` / ``decided_at``);
no provider-publication/ingestion time, generated id, mutable current-state field,
or current canonical link is ever ordered on or read as a feature. Rows are
ordered by ``(timestamp, official_provider, official_game_key)`` -- all
deterministic -- so output is byte-identical across equivalent fresh rebuilds
(the ULID ``game_id`` is deliberately excluded from ordering and serialization).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from .asof import AsOfAmbiguityError, AsOfReader
from .models import Cutoff
from .registry import assert_selectable

if TYPE_CHECKING:  # avoid a hard runtime dependency of the row layer on probability/
    from probability.datasets import GameStateDataset

__all__ = ["HistoricalRow", "HistoricalDataset", "build_historical_dataset"]

# A far-future horizon for the correction-aware label read (reads the newest,
# i.e. corrected, result observation regardless of when the correction landed).
_LABEL_HORIZON = Cutoff.parse("9999-12-31T23:59:59.000000Z")
_LEAGUE_TO_SPORT = {"MLB": "mlb", "NBA": "nba"}
_GAMES_IDENTITY_COLUMNS = ("game_id", "league_id", "home_team_id", "away_team_id",
                           "game_date_local", "official_provider", "official_game_key")


@dataclass(frozen=True)
class HistoricalRow:
    """One pregame point-in-time row. ``winning_side`` and ``label_observed_at`` are
    LABEL provenance (the permitted future value), never features; ``score_diff``
    and ``phase`` are cutoff-known state (0 pregame). ``game_id`` is a ULID kept for
    in-corpus reference but excluded from :meth:`as_dict` so serialization is
    rebuild-stable."""

    game_id: str
    sport: str
    league_code: str
    home_team_id: str
    away_team_id: str
    official_provider: str
    official_game_key: str
    cutoff: str
    timestamp: int
    label: int
    score_diff: int
    phase: int
    winning_side: str
    label_observed_at: str

    def as_dict(self) -> dict[str, Any]:
        # Deterministic fields only (no ULID game_id) -> byte-identical across rebuilds.
        return {
            "sport": self.sport,
            "league_code": self.league_code,
            "home_team_id": self.home_team_id,
            "away_team_id": self.away_team_id,
            "official_provider": self.official_provider,
            "official_game_key": self.official_game_key,
            "cutoff": self.cutoff,
            "timestamp": self.timestamp,
            "label": self.label,
            "score_diff": self.score_diff,
            "phase": self.phase,
            "winning_side": self.winning_side,
            "label_observed_at": self.label_observed_at,
        }


@dataclass(frozen=True)
class HistoricalDataset:
    """A deterministic, chronologically-ordered set of historical PIT rows."""

    sport: str
    rows: tuple[HistoricalRow, ...]

    def __len__(self) -> int:
        return len(self.rows)

    def labels(self) -> np.ndarray:
        return np.array([r.label for r in self.rows], dtype=np.int8)

    def timestamps(self) -> np.ndarray:
        return np.array([r.timestamp for r in self.rows], dtype=np.int64)

    def serialize(self) -> str:
        """Byte-stable JSON of the row set (sorted keys, deterministic order)."""

        return json.dumps({"sport": self.sport, "rows": [r.as_dict() for r in self.rows]},
                          sort_keys=True)

    def to_game_state_dataset(self) -> "GameStateDataset":
        """Convert to the existing :class:`GameStateDataset` interface WITHOUT
        fabricating data: a zero-column feature matrix (features are a later phase)
        and an explicit-unavailable ``true_prob`` (all-NaN float64). Labels are the
        real home-win outcomes; ``timestamps``/``score_diff``/``phase`` are the
        cutoff-known values. Preserves chronological splitting; no object arrays."""

        from probability.datasets import GameStateDataset

        n = len(self.rows)
        ds = GameStateDataset(
            sport=self.sport,
            X=np.zeros((n, 0), dtype=np.float32),          # no features yet (honest)
            y=self.labels(),
            timestamps=self.timestamps(),
            true_prob=np.full(n, np.nan, dtype=np.float64),  # explicitly unavailable
            score_diff=np.array([r.score_diff for r in self.rows], dtype=np.int32),
            phase=np.array([r.phase for r in self.rows], dtype=np.int32),
        )
        lengths = {ds.X.shape[0], ds.y.shape[0], ds.timestamps.shape[0],
                   ds.true_prob.shape[0], ds.score_diff.shape[0], ds.phase.shape[0]}
        if lengths != {n}:
            raise ValueError(f"inconsistent GameStateDataset array lengths: {lengths} != {{{n}}}")
        return ds


def build_historical_dataset(
    conn: sqlite3.Connection, *, league: str, since: Optional[str] = None,
) -> HistoricalDataset:
    """Build the pregame historical dataset for ``league`` ('mlb'|'nba'), offline.

    ``since`` (a ``YYYY-MM-DD`` local date) bounds games by their immutable
    ``game_date_local``. Games without a provable label at a strictly-later
    transaction time, without a known game<->reference correspondence at the
    cutoff, with a leaked/conflicting result, or missing an official identity are
    excluded (never fabricated)."""

    league_code = league.strip().upper()
    if league_code not in _LEAGUE_TO_SPORT:
        raise ValueError(f"unsupported league {league!r}; expected mlb or nba")
    sport = _LEAGUE_TO_SPORT[league_code]
    league_id = f"lg_{league_code.lower()}"

    assert_selectable("games", list(_GAMES_IDENTITY_COLUMNS))  # registry-traceable identity read
    sql = (  # noqa: S608 - columns are the static registry-validated identity list
        f"SELECT {', '.join(_GAMES_IDENTITY_COLUMNS)} FROM games WHERE league_id = ?")
    params: list[Any] = [league_id]
    if since is not None:
        sql += " AND game_date_local >= ?"
        params.append(since)
    games = conn.execute(sql, params).fetchall()

    label_reader = AsOfReader(conn, _LABEL_HORIZON)
    rows: list[HistoricalRow] = []
    for g in games:
        try:
            row = _build_row(conn, g, sport=sport, league_code=league_code,
                             label_reader=label_reader)
        except AsOfAmbiguityError:
            # Equal-time conflicting result/schedule -> exclude, fail closed. The
            # quality layer independently reports the conflict as a finding.
            continue
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r.timestamp, r.official_provider, r.official_game_key))
    return HistoricalDataset(sport=sport, rows=tuple(rows))


def _build_row(
    conn: sqlite3.Connection, g: sqlite3.Row, *, sport: str, league_code: str,
    label_reader: AsOfReader,
) -> Optional[HistoricalRow]:
    game_id = str(g["game_id"])
    op, ok = g["official_provider"], g["official_game_key"]
    if op is None or ok is None:
        return None  # no official identity -> observations cannot be attributed
    op, ok = str(op), str(ok)

    ref_id = label_reader.game_provider_reference(game_id=game_id, official_provider=op,
                                                 official_game_key=ok)
    if ref_id is None:
        return None  # no provable game<->reference correspondence

    result = (label_reader.game_result(ref_id) if sport == "mlb"
              else label_reader.nba_game_result(ref_id))
    if result is None:
        return None
    winning_side = result.get("winning_side")
    if result.get("mapped_status") != "final" or winning_side not in ("home", "away"):
        return None  # unfinished / tie / unresolved -> no fabricated label

    schedule = label_reader.official_schedule(ref_id)
    if schedule is None:
        return None
    scheduled_start = schedule.get("scheduled_start")
    if scheduled_start is None:
        return None
    t_cut = Cutoff.parse(str(scheduled_start))
    t_result = Cutoff.parse(str(result.observed_at))
    if not t_result.datetime > t_cut.datetime:
        return None  # label must be known STRICTLY after the feature cutoff

    feat = AsOfReader(conn, t_cut)
    # The game<->reference correspondence must be known by the FEATURE cutoff.
    if feat.game_provider_reference(game_id=game_id, official_provider=op,
                                   official_game_key=ok) != ref_id:
        return None
    # The final result must be INVISIBLE at the feature cutoff (leakage guard).
    pre = feat.game_result(ref_id) if sport == "mlb" else feat.nba_game_result(ref_id)
    if pre is not None:
        return None

    return HistoricalRow(
        game_id=game_id, sport=sport, league_code=league_code,
        home_team_id=str(g["home_team_id"]), away_team_id=str(g["away_team_id"]),
        official_provider=op, official_game_key=ok, cutoff=t_cut.iso,
        timestamp=int(t_cut.datetime.timestamp()),
        label=1 if winning_side == "home" else 0,
        score_diff=0, phase=0,  # pregame: no in-game state known at the scheduled start
        winning_side=str(winning_side), label_observed_at=str(result.observed_at))
