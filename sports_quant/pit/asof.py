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
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from ..db.repositories.data_quality import DataQualityIssue, SqliteDataQualityRepository
from ..db.repositories.games import GameStatusRecord
from ..db.repositories.kalshi import SqliteKalshiRepository
from ..db.repositories.matching import SqliteMatchingRepository
from ..db.repositories.sportsbook import SportsbookPriceSnapshot, SqliteSportsbookRepository
from .models import (
    Cutoff,
    GameScheduleState,
    LinkAsOf,
    MarketIdentity,
    MatchDecisionView,
    Observation,
)
from .registry import ForbiddenColumnError, assert_selectable, require_asof

__all__ = [
    "read_only_connection",
    "latest_as_of",
    "AsOfReader",
    "AsOfAmbiguityError",
    "deterministic_json",
]

KALSHI_PUBLIC_PROVIDER = "kalshi_public"


class AsOfAmbiguityError(RuntimeError):
    """Raised when equal-``observed_at`` rows at one anchor carry MATERIALLY
    DIFFERENT content (distinct ``content_hash``).

    A generated ULID / insertion order is not a valid semantic winner across a
    fresh rebuild, so a genuine conflict fails closed rather than silently picking
    one. The table, anchor, and observed timestamp are reported; the conflicting
    values are deliberately NOT exposed as a feature.
    """

    def __init__(self, *, table: str, anchor: str, anchor_params: Sequence[Any],
                 observed_at: str, distinct: int) -> None:
        self.table = table
        self.anchor = anchor
        self.anchor_params = tuple(anchor_params)
        self.observed_at = observed_at
        super().__init__(
            f"as-of ambiguity in {table!r} at observed_at={observed_at!r} for anchor "
            f"{anchor!r} params={self.anchor_params!r}: {distinct} distinct content hashes share "
            "the same maximum timestamp; refusing to pick a non-deterministic winner")


# A public as-of WHERE fragment must be a positive-allowlist conjunction (AND-only)
# of the exact comparison forms the typed accessors use: ``ident = ?`` /
# ``ident = <int>`` / ``ident = '<literal>'`` / ``ident IS [NOT] NULL``. Anything
# else -- OR, other operators (LIKE/GLOB/</>), quoted identifiers, functions,
# parentheses/subqueries, commas, COLLATE, comments, statement breaks -- fails
# closed, so a future caller cannot broaden the predicate or smuggle an unreviewed
# join through the generic surface (task §9). A blocklist is deliberately NOT used
# (it leaked OR/LIKE/quoted-identifier/comma bypasses).
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_LITERAL = r"(?:\?|-?\d+|'[^']*')"
_CONDITION = rf"(?:{_IDENT}\s*=\s*{_LITERAL}|{_IDENT}\s+IS(?:\s+NOT)?\s+NULL)"
_ALLOWED_FRAGMENT = re.compile(
    rf"^\s*{_CONDITION}(?:\s+AND\s+{_CONDITION})*\s*$", re.IGNORECASE)


def _validate_fragment(fragment: str) -> None:
    if _ALLOWED_FRAGMENT.fullmatch(fragment) is None:
        raise ValueError(
            f"unsafe as-of WHERE fragment {fragment!r}: only an AND-conjunction of "
            "`col = ?` / `col = <int>` / `col = '<literal>'` / `col IS [NOT] NULL` is permitted")


@contextmanager
def read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the corpus in TRUE SQLite read-only mode for feature-facing reads.

    Opens ``file:<uri>?immutable=1`` (``uri=True``): the file must already exist
    (a missing database raises and is NOT created, and no parent directory is
    made); the database is treated as immutable so **no ``-wal`` / ``-shm`` /
    journal sidecar is created and no journal mode is changed** (plain ``mode=ro``
    would materialize ``-wal``/``-shm`` for a WAL-header database — hence
    ``immutable=1`` for a checkpointed corpus snapshot). No write-capable
    connection is opened first; ``PRAGMA query_only = ON`` is set as defense in
    depth. Every INSERT/UPDATE/DELETE/DDL/writable-PRAGMA fails; the database bytes
    and directory listing are unchanged after reads (task §6)."""

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"read-only corpus not found (not created): {p}")
    uri = p.resolve().as_uri() + "?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")  # defense in depth (writes already blocked)
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
    extra_where: Optional[str] = None,
    extra_params: Sequence[Any] = (),
) -> Optional[sqlite3.Row]:
    """The ONE canonical latest-as-of selection for append-only snapshot tables.

    Contract (tasks §5, §4): keep only rows whose transaction time ``observed_at``
    is ``<= cutoff``; take the maximum ``observed_at`` in that filtered set; among
    the rows sharing that maximum timestamp at the anchor, resolve the tie by
    CONTENT: if they all share one ``content_hash`` they are semantically identical
    and one is returned deterministically (never chosen by ULID/rowid/insertion
    order); if they carry different content it is a genuine conflict and raises
    :class:`AsOfAmbiguityError` (fail-closed), never exposing either value. Never
    returns a future row; ``updated_at``/``created_at``/``ingested_at``/provider
    time are never used. ``table`` must be ``asof_filtered`` (fail-closed via the
    registry). ``anchor_where``/``extra_where`` are validated (no JOIN/subquery/
    semicolon/comment) and every value is a bound ``?`` parameter (task §9)."""

    entry = require_asof(table)
    obs = entry.observed_at_column
    if not obs:
        raise ValueError(
            f"{table!r} has no observed_at column; it is only reachable via its parent "
            f"({entry.via_parent}) as-of read")
    if not entry.content_column:
        raise ValueError(
            f"{table!r} has no content-hash column; it is not readable via latest_as_of "
            "(use its typed repository accessor)")
    _validate_fragment(anchor_where)
    if extra_where:
        _validate_fragment(extra_where)
    where = f"({anchor_where}) AND {obs} <= ?"
    params: list[Any] = [*anchor_params, cutoff.iso]
    if extra_where:
        where += f" AND ({extra_where})"
        params.extend(extra_params)
    max_row = conn.execute(  # noqa: S608 - table/obs come from the static registry
        f"SELECT MAX({obs}) AS m FROM {table} WHERE {where}", params).fetchone()
    max_obs = None if max_row is None else max_row["m"]
    if max_obs is None:
        return None
    rows = conn.execute(  # noqa: S608 - identifiers are registry-sourced; values are bound
        f"SELECT * FROM {table} WHERE {where} AND {obs} = ? ORDER BY {entry.id_column}",
        [*params, max_obs]).fetchall()
    distinct = {r[entry.content_column] for r in rows}
    if len(distinct) > 1:
        raise AsOfAmbiguityError(table=table, anchor=anchor_where, anchor_params=anchor_params,
                                 observed_at=str(max_obs), distinct=len(distinct))
    return rows[0]  # identical content -> any row serializes identically


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
        """Latest as-of row of an ``asof_filtered`` table, projected to ONLY the
        table's feature-safe columns (task red-flag-2).

        ``Observation.fields`` exposes exactly ``entry.feature_columns`` -- a strict
        subset of the content-hashed semantic columns -- so audit/provenance
        columns (``ingested_at``/``created_at``/``run_id``/``raw_response_*``/the
        surrogate id), provider identifiers, and provider timestamps are never
        surfaced as features, and the result is fully determined by the content
        hash (rebuild-stable, red-flag-6). A table with no feature policy fails
        closed."""

        entry = require_asof(table)
        if entry.feature_columns is None:
            raise ForbiddenColumnError(
                f"table {table!r} has no feature-column policy; it is not readable via "
                "observation() (add an explicit feature allowlist first)")
        row = latest_as_of(self._conn, table=table, cutoff=self._cutoff,
                           anchor_where=anchor_where, anchor_params=anchor_params,
                           extra_where=extra_where, extra_params=extra_params)
        if row is None:
            return None
        available = set(row.keys())
        fields = {c: row[c] for c in sorted(entry.feature_columns) if c in available}
        obs_col = entry.observed_at_column or ""
        return Observation(table=table, observed_at=row[obs_col] if obs_col in available else None,
                           row_id=row[entry.id_column] if entry.id_column in available else None,
                           fields=_as_mapping(fields))

    # -- game state / official results -------------------------------------- #
    def _status_row(self, game_id: str) -> Optional[sqlite3.Row]:
        """The status observation as of cutoff via the CONTENT-HASH fail-closed
        path (not the repository's ULID tie-break): conflicting equal-``observed_at``
        status rows (e.g. two providers disagreeing at the same instant) raise
        :class:`AsOfAmbiguityError` instead of silently picking a generated-id
        winner (red-flag-3)."""

        return latest_as_of(self._conn, table="game_status_history", cutoff=self._cutoff,
                            anchor_where="game_id = ?", anchor_params=(game_id,))

    def game_status(self, game_id: str) -> Optional[GameStatusRecord]:
        """Game status as of cutoff (via the append-only status log, never
        ``games.status``), fail-closed on equal-time conflicts."""

        row = self._status_row(game_id)
        if row is None:
            return None
        return GameStatusRecord(
            status_id=str(row["status_id"]), game_id=str(row["game_id"]),
            status=str(row["status"]), scheduled_start=str(row["scheduled_start"]),
            provider=str(row["provider"]), observed_at=str(row["observed_at"]),
            ingested_at=str(row["ingested_at"]), content_hash=str(row["content_hash"]),
            detail=row["detail"], provider_timestamp=row["provider_timestamp"],
            raw_response_id=row["raw_response_id"], raw_response_hash=row["raw_response_hash"],
            created_at=str(row["created_at"]))

    def game_schedule_state(self, game_id: str) -> Optional[GameScheduleState]:
        """The game's status AND scheduled start as of cutoff, taken TOGETHER from
        the same ``game_status_history`` observation (task §2), via the content-hash
        fail-closed path. Both are mutable current-state on ``games`` and are
        forbidden there; this never combines a historical status with today's
        scheduled start, a future reschedule cannot leak into an earlier cutoff, and
        an equal-time provider conflict fails closed rather than picking a ULID
        winner."""

        row = self._status_row(game_id)
        if row is None:
            return None
        return GameScheduleState(game_id=str(row["game_id"]), status=str(row["status"]),
                                 scheduled_start=str(row["scheduled_start"]),
                                 observed_at=str(row["observed_at"]), status_id=str(row["status_id"]))

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

    def sportsbook_market_identity(self, sb_market_id: str) -> Optional[MarketIdentity]:
        """The IMMUTABLE structural identity of a sportsbook market -- only the
        allowlisted columns (task §3). Mutable title, provider update times, current
        raw-response provenance, and last-observed fields are never returned."""

        assert_selectable("sportsbook_markets",
                          ["sb_market_id", "sb_event_id", "bookmaker_key", "market_key"])
        row = self._conn.execute(
            "SELECT sb_market_id, sb_event_id, bookmaker_key, market_key "
            "FROM sportsbook_markets WHERE sb_market_id = ?", (sb_market_id,)).fetchone()
        if row is None:
            return None
        return MarketIdentity(sb_market_id=str(row["sb_market_id"]),
                              sb_event_id=str(row["sb_event_id"]),
                              bookmaker_key=str(row["bookmaker_key"]),
                              market_key=str(row["market_key"]))

    def sportsbook_event_game(self, sb_event_id: str) -> Optional[LinkAsOf]:
        """The canonical game a sportsbook event maps to, ONLY when its orientation
        is approved as of the cutoff (accepted direct-orientation decision +
        DQ/review timeline) AND any required manual review was validly completed by
        the cutoff. Neutral swapped / review-gated / reviewed-after-cutoff events
        are excluded; the current ``sportsbook_events.game_id`` alone is never
        trusted."""

        if not self._sb.is_orientation_approved(sb_event_id, as_of=self._cutoff.iso):
            return None
        game_id, decision_id, orientation = self._sb.event_link(sb_event_id)
        if game_id is None or decision_id is None or not self._review_ok(decision_id):
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
        if game_id is None or decision_id is None or not self._review_ok(decision_id):
            return None
        return LinkAsOf(game_id=game_id, match_decision_id=decision_id,
                        details=_as_mapping({"yes_team_id": yes_team_id,
                                             "market_semantic": semantic}))

    def kalshi_event_game(self, kalshi_event_id: str) -> Optional[LinkAsOf]:
        """The canonical game a Kalshi event maps to, only through a feature-usable
        accepted event decision valid at the cutoff (order-book/trade reads stay
        separate from this identity read)."""

        entity = self.matched_entity(source_provider=KALSHI_PUBLIC_PROVIDER,
                                     source_ref=kalshi_event_id, entity_type="kalshi_event")
        if entity is None:
            return None
        game_id, decision_id = entity
        return LinkAsOf(game_id=game_id, match_decision_id=decision_id)

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
        """RAW AUDIT view of the most recent accepted decision known by cutoff (a
        decision decided after the cutoff is invisible). This may be displayed for
        reporting but is NOT feature-safe identity -- use :meth:`matched_entity`,
        which additionally enforces the review gate, for a feature-facing link."""

        accepted = [d for d in self.decisions(source_provider=source_provider,
                                              source_ref=source_ref, entity_type=entity_type)
                    if d.outcome == "accepted"]
        return accepted[-1] if accepted else None

    def matched_entity(self, *, source_provider: str, source_ref: str,
                       entity_type: Optional[str] = None) -> Optional[tuple[str, str]]:
        """FEATURE-facing canonical identity ``(matched_entity_id, match_id)`` for a
        provider source, or None. Returned only when an accepted decision is known
        by the cutoff (``decided_at <= cutoff``) AND any required manual review was
        validly completed by the cutoff (:meth:`_review_ok`). This is the single
        gated path used for Kalshi-event and provider-reference identity, so
        ``matched_entity_id`` is never exposed before a required review is valid."""

        raw = [d for d in self._match.decisions_for_source(
                   source_provider=source_provider, source_ref=source_ref,
                   entity_type=entity_type, as_of=self._cutoff.iso)
               if d.outcome == "accepted"]
        if not raw:
            return None
        decision = raw[-1]
        if decision.matched_entity_id is None or not self._review_ok_decision(decision):
            return None
        return decision.matched_entity_id, decision.match_id

    def _review_ok(self, match_id: str) -> bool:
        decision = self._match.get(match_id)
        return decision is not None and self._review_ok_decision(decision)

    def _review_ok_decision(self, decision: Any) -> bool:
        """Conservative, timeline-based review gate (task §5).

        Schema v16 cannot fully reconstruct whether a decision was originally
        review-gated once the mutable review columns change, so the most
        conservative supported rule is used:

        * currently flagged (``needs_manual_review``) -> unavailable (a required
          review is not validly completed);
        * a recorded human review (``reviewed_by`` set) -> available only when its
          ``reviewed_at`` transaction time is ``<= cutoff``;
        * otherwise a clean accept that never required review -> available.
        """

        if decision.needs_manual_review:
            return False
        if decision.reviewed_by is not None:
            return decision.reviewed_at is not None and decision.reviewed_at <= self._cutoff.iso
        return True

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
