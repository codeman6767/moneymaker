"""Feature-facing point-in-time (as-of) reads for Phase E1 (tasks §5, §7, §12).

Every read here requires an explicit :class:`~sports_quant.pit.models.Cutoff` and
uses TRANSACTION TIME (``observed_at`` / ``decided_at`` / ``detected_at``), never
provider publication time, ``created_at``/``updated_at``/``ingested_at``, nor any
mutable current-state column. There is deliberately NO "latest without cutoff"
convenience API. This module is offline and database-read-only: it constructs no
provider client, imports no execution/gateway code, and makes no network request.
The one canonical latest-as-of algorithm (:func:`latest_as_of`) is registry-gated
so only ``asof_filtered`` tables can be read this way (fail-closed).

Closing lines are NOT here -- they live only in
:mod:`sports_quant.pit.evaluation_only`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from ..db.engine import Database
from ..db.repositories.data_quality import DataQualityIssue, SqliteDataQualityRepository
from ..db.repositories.games import GameStatusRecord, SqliteGameRepository
from ..db.repositories.kalshi import SqliteKalshiRepository
from ..db.repositories.matching import SqliteMatchingRepository
from ..db.repositories.sportsbook import SportsbookPriceSnapshot, SqliteSportsbookRepository
from .models import Cutoff, LinkAsOf, MatchDecisionView, Observation
from .registry import require_asof

__all__ = [
    "read_only_connection",
    "latest_as_of",
    "AsOfReader",
    "deterministic_json",
]

KALSHI_PUBLIC_PROVIDER = "kalshi_public"


@contextmanager
def read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the corpus for feature-facing reads with writes disabled.

    The engine has no native read-only mode, so this wraps the standard
    connection and sets ``PRAGMA query_only = ON`` -- any INSERT/UPDATE/DELETE
    raises at the SQLite level, guaranteeing feature-facing PIT reads cannot
    mutate the corpus (task §12).
    """

    conn = Database(path).connect()
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def latest_as_of(
    conn: sqlite3.Connection,
    *,
    table: str,
    cutoff: Cutoff,
    anchor_where: str,
    anchor_params: Sequence[Any] = (),
    columns: str = "*",
    extra_where: Optional[str] = None,
    extra_params: Sequence[Any] = (),
) -> Optional[sqlite3.Row]:
    """The ONE canonical latest-as-of selection for append-only snapshot tables.

    Contract (task §5): keep only rows whose transaction time ``observed_at`` is
    ``<= cutoff``; take the maximum ``observed_at`` in that filtered set; break an
    equal-timestamp tie with the table's stable monotonic id (descending); return
    at most one row and never a future row. Ordering is always explicit; SQLite
    insertion order is never relied upon. ``updated_at`` / ``created_at`` /
    ``ingested_at`` / provider publication time are never used. The ``table`` must
    be classified ``asof_filtered`` (fail-closed via the registry)."""

    entry = require_asof(table)
    obs = entry.observed_at_column
    if not obs:
        raise ValueError(
            f"{table!r} has no observed_at column; it is only reachable via its parent "
            f"({entry.via_parent}) as-of read")
    where = f"({anchor_where}) AND {obs} <= ?"
    params: list[Any] = [*anchor_params, cutoff.iso]
    if extra_where:
        where += f" AND ({extra_where})"
        params.extend(extra_params)
    sql = (  # noqa: S608 - table/columns come from the static registry, not user input
        f"SELECT {columns} FROM {table} WHERE {where} "
        f"ORDER BY {obs} DESC, {entry.id_column} DESC LIMIT 1"
    )
    return conn.execute(sql, params).fetchone()


def deterministic_json(items: Any) -> str:
    """Byte-stable JSON for determinism assertions (sorted keys, str fallback)."""

    def _norm(obj: Any) -> Any:
        if hasattr(obj, "as_dict"):
            return obj.as_dict()
        if isinstance(obj, Observation):
            return obj.as_dict()
        return obj

    if isinstance(items, (list, tuple)):
        payload = [_norm(x) for x in items]
    else:
        payload = _norm(items)
    return json.dumps(payload, sort_keys=True, default=str)


class AsOfReader:
    """Read-only, cutoff-bound accessors for future Phase E row construction.

    Construct with a connection and a fixed :class:`Cutoff`; every method answers
    "what was known by this cutoff". No method exposes a closing line, a mutable
    current-state link, or a latest-without-cutoff read.
    """

    def __init__(self, conn: sqlite3.Connection, cutoff: Cutoff) -> None:
        self._conn = conn
        self._cutoff = cutoff
        self._games = SqliteGameRepository(conn)
        self._sb = SqliteSportsbookRepository(conn)
        self._kal = SqliteKalshiRepository(conn)
        self._match = SqliteMatchingRepository(conn)
        self._dq = SqliteDataQualityRepository(conn)

    @property
    def cutoff(self) -> Cutoff:
        return self._cutoff

    # -- generic asof observation ------------------------------------------- #
    def observation(
        self, table: str, *, anchor_where: str, anchor_params: Sequence[Any] = (),
        extra_where: Optional[str] = None, extra_params: Sequence[Any] = (),
    ) -> Optional[Observation]:
        """Latest as-of row of any ``asof_filtered`` table, wrapped immutably."""

        entry = require_asof(table)
        row = latest_as_of(self._conn, table=table, cutoff=self._cutoff,
                           anchor_where=anchor_where, anchor_params=anchor_params,
                           extra_where=extra_where, extra_params=extra_params)
        if row is None:
            return None
        fields = {k: row[k] for k in row.keys()}
        return Observation(table=table, observed_at=fields.get(entry.observed_at_column or ""),
                           row_id=fields.get(entry.id_column or ""), fields=_as_mapping(fields))

    # -- game state / official results -------------------------------------- #
    def game_status(self, game_id: str) -> Optional[GameStatusRecord]:
        """Game status as of cutoff (via the append-only status log, never
        ``games.status``)."""

        return self._games.status_as_of(game_id, self._cutoff.iso)

    def official_schedule(self, game_ref_id: str) -> Optional[Observation]:
        return self.observation("game_schedule_snapshots", anchor_where="game_ref_id = ?",
                                anchor_params=(game_ref_id,))

    def game_result(self, game_ref_id: str) -> Optional[Observation]:
        """Official MLB result ONLY when observed by cutoff (label, not a pregame
        feature). A result observed after the cutoff is invisible."""

        return self.observation("game_result_snapshots", anchor_where="game_ref_id = ?",
                                anchor_params=(game_ref_id,))

    def nba_game_result(self, game_ref_id: str) -> Optional[Observation]:
        return self.observation("nba_game_results", anchor_where="game_ref_id = ?",
                                anchor_params=(game_ref_id,))

    # -- MLB/NBA official observations -------------------------------------- #
    def team_game_statistics(self, game_ref_id: str, team_id: str,
                             *, table: str = "team_game_statistics") -> Optional[Observation]:
        return self.observation(table, anchor_where="game_ref_id = ? AND team_id = ?",
                                anchor_params=(game_ref_id, team_id))

    def player_game_statistics(self, game_ref_id: str, player_id: str,
                               *, table: str = "player_game_statistics") -> Optional[Observation]:
        return self.observation(table, anchor_where="game_ref_id = ? AND player_id = ?",
                                anchor_params=(game_ref_id, player_id))

    def roster_membership(self, team_ref_id: str, player_id: str) -> Optional[Observation]:
        """Roster membership as-of. The caller must additionally constrain the
        result to the game's league season (roster_snapshots carries no season)."""

        return self.observation("roster_snapshots",
                                anchor_where="team_ref_id = ? AND player_id = ?",
                                anchor_params=(team_ref_id, player_id))

    def probable_starter(self, game_ref_id: str, side: str) -> Optional[Observation]:
        return self.observation("probable_pitcher_snapshots",
                                anchor_where="game_ref_id = ? AND side = ?",
                                anchor_params=(game_ref_id, side))

    def lineup(self, game_ref_id: str, team_id: str) -> Optional[Observation]:
        """Lineup snapshot as-of. A confirmed lineup is visible only once its
        ``observed_at`` is <= cutoff; earlier reads see the projected/unconfirmed
        snapshot or nothing."""

        return self.observation("lineup_snapshots",
                                anchor_where="game_ref_id = ? AND team_id = ?",
                                anchor_params=(game_ref_id, team_id))

    def injury(self, player_ref_id: str) -> Optional[Observation]:
        """Injury snapshot as-of by TRANSACTION time (``observed_at``). A
        ``published_at`` before the cutoff never makes an injury visible while its
        ``observed_at`` is still after the cutoff."""

        return self.observation("injury_snapshots", anchor_where="player_ref_id = ?",
                                anchor_params=(player_ref_id,))

    # -- sportsbook --------------------------------------------------------- #
    def sportsbook_price(self, sb_outcome_id: str) -> Optional[SportsbookPriceSnapshot]:
        """Latest sportsbook price for an outcome as of cutoff."""

        return self._sb.price_as_of(sb_outcome_id, self._cutoff.iso)

    def sportsbook_event_game(self, sb_event_id: str) -> Optional[LinkAsOf]:
        """The canonical game a sportsbook event maps to, ONLY when its orientation
        is approved as of the cutoff (accepted direct-orientation decision +
        DQ/review timeline). Neutral swapped / review-gated events are excluded;
        the current ``sportsbook_events.game_id`` alone is never trusted."""

        if not self._sb.is_orientation_approved(sb_event_id, as_of=self._cutoff.iso):
            return None
        game_id, decision_id, orientation = self._sb.event_link(sb_event_id)
        if game_id is None or decision_id is None:
            return None
        return LinkAsOf(game_id=game_id, match_decision_id=decision_id,
                        details=_as_mapping({"orientation": orientation}))

    # -- kalshi ------------------------------------------------------------- #
    def kalshi_market_orientation(self, kalshi_market_id: str) -> Optional[LinkAsOf]:
        """Kalshi market game + Yes-team orientation, ONLY through the fail-closed
        as-of readiness method. A later rules conflict (DQ active at cutoff) blocks;
        a current market link alone is not historical truth."""

        if not self._kal.is_kalshi_market_orientation_approved(
                kalshi_market_id, as_of=self._cutoff.iso):
            return None
        game_id, decision_id, yes_team_id, _hash, semantic = self._kal.market_link(kalshi_market_id)
        if game_id is None or decision_id is None:
            return None
        return LinkAsOf(game_id=game_id, match_decision_id=decision_id,
                        details=_as_mapping({"yes_team_id": yes_team_id,
                                             "market_semantic": semantic}))

    def kalshi_event_game(self, kalshi_event_id: str) -> Optional[LinkAsOf]:
        """The canonical game a Kalshi event maps to, only through an accepted event
        decision with ``decided_at <= cutoff`` (order-book/trade reads stay
        separate from this identity read)."""

        decision = self.accepted_decision(
            source_provider=KALSHI_PUBLIC_PROVIDER, source_ref=kalshi_event_id,
            entity_type="kalshi_event")
        if decision is None or decision.matched_entity_id is None:
            return None
        return LinkAsOf(game_id=decision.matched_entity_id, match_decision_id=decision.match_id)

    # -- match decisions ---------------------------------------------------- #
    def decisions(self, *, source_provider: str, source_ref: str,
                  entity_type: Optional[str] = None) -> list[MatchDecisionView]:
        """All match decisions for a source known by cutoff (``decided_at <=
        cutoff``), each with review visible only if completed by the cutoff."""

        rows = self._match.decisions_for_source(
            source_provider=source_provider, source_ref=source_ref, entity_type=entity_type,
            as_of=self._cutoff.iso)
        return [self._decision_view(d) for d in rows]

    def accepted_decision(self, *, source_provider: str, source_ref: str,
                          entity_type: Optional[str] = None) -> Optional[MatchDecisionView]:
        """The most recent accepted decision known by cutoff, or None (a decision
        decided after the cutoff is invisible, so the source stays unresolved)."""

        accepted = [d for d in self.decisions(source_provider=source_provider,
                                              source_ref=source_ref, entity_type=entity_type)
                    if d.outcome == "accepted"]
        return accepted[-1] if accepted else None

    def _decision_view(self, d: Any) -> MatchDecisionView:  # d: MatchDecision
        cutoff_iso = self._cutoff.iso
        completed = (d.reviewed_by is not None and d.reviewed_at is not None
                     and d.reviewed_at <= cutoff_iso)
        return MatchDecisionView(
            match_id=d.match_id, entity_type=d.entity_type, source_provider=d.source_provider,
            source_ref=d.source_ref, outcome=d.outcome, method=d.method, score=d.score,
            decided_at=d.decided_at, matched_entity_id=d.matched_entity_id,
            review_completed_by_cutoff=completed,
            reviewed_by=d.reviewed_by if completed else None,
            reviewed_at=d.reviewed_at if completed else None)

    # -- data quality ------------------------------------------------------- #
    def active_data_quality(
        self, *, rule_code: Optional[str] = None, severity: Optional[str] = None,
        entity_type: Optional[str] = None, provider: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> list[DataQualityIssue]:
        """DQ issues that were ACTIVE at the cutoff (detected by, not resolved by)."""

        return self._dq.list_active_at(
            as_of=self._cutoff.iso, rule_code=rule_code, severity=severity,
            entity_type=entity_type, provider=provider, entity_id=entity_id)

    # -- weather ------------------------------------------------------------ #
    def weather_pregame_forecast(
        self, game_ref_id: str, *, forecast_mode: str, valid_time: Optional[str] = None,
    ) -> Optional[Observation]:
        """Pregame weather FEATURE: the latest row that is simultaneously a
        ``current_forecast``, observed by the cutoff, and ``pit_eligible = 1``
        (task §7). Station observations, reanalysis, ``pit_eligible`` NULL/0
        historical forecasts, and forecasts observed after the cutoff are all
        excluded. Eligibility is never inferred from endpoint or provider name."""

        vt_clause = "valid_time IS NULL" if valid_time is None else "valid_time = ?"
        vt_params: tuple[Any, ...] = () if valid_time is None else (valid_time,)
        return self.observation(
            "weather_snapshots", anchor_where="game_ref_id = ?", anchor_params=(game_ref_id,),
            extra_where=("weather_kind = 'current_forecast' AND pit_eligible = 1 "
                         f"AND forecast_mode = ? AND {vt_clause}"),
            extra_params=(forecast_mode, *vt_params))


def _as_mapping(row: dict[str, Any]) -> Any:
    from types import MappingProxyType
    return MappingProxyType(dict(row))
