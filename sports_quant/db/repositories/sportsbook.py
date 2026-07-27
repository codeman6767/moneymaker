"""Sportsbook repository: events, markets, outcome identities, price snapshots.

The four levels are stored and queried separately on purpose. An outcome's
*identity* is stable for days; its *price* changes every few minutes. Event and
market and outcome rows are upserted (idempotent identity); price observations
are appended, never overwritten, and deduplicated on ``content_hash`` so
re-ingesting an unchanged price is a no-op.

Point-in-time reads filter on ``observed_at`` -- the transaction-time cutoff --
and never on the provider's own timestamp, which can be back-dated
(POINT_IN_TIME_DATA.md §2.1). "Latest price known at or before T" is
:meth:`SqliteSportsbookRepository.price_as_of`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional, Protocol

from streaming.event_envelope import canonical_json

from ..ids import (
    new_sb_event_id,
    new_sb_market_id,
    new_sb_outcome_id,
    new_sb_price_snapshot_id,
)
from ..models import (
    SportsbookEvent,
    SportsbookMarket,
    SportsbookOutcome,
    SportsbookPriceSnapshot,
)
from ..schema import utc_now_iso
from .base import Repository
from .references import LinkOutcome


def point_key(point: Optional[float]) -> str:
    """Text rendering of a line used in the outcome-identity uniqueness key.

    ``''`` when there is no line. A NOT NULL key is required because SQLite
    treats two NULLs as distinct inside a UNIQUE constraint, which would let an
    h2h outcome insert again on every poll. ``repr`` keeps ``8.5`` and ``9.0``
    distinct and stable.
    """

    return "" if point is None else repr(float(point))


def price_content_hash(
    *,
    price_american: int,
    point: Optional[float],
    bookmaker_last_update: Optional[str],
    market_last_update: Optional[str],
    provider_timestamp: Optional[str],
) -> str:
    """Content of a *price observation*, deliberately excluding ``observed_at``.

    Covers the reported price, line, and the provider's own update times only.
    Two observations with identical content hash identically **on purpose** --
    that is what lets the repository detect "nothing changed" against the
    immediate temporal predecessor. It is *not* a global uniqueness key: a line
    that reverts to an earlier price (``-110 -> -120 -> -110``) reports the same
    content twice, and both must be recorded. The append/collapse decision is
    made in :meth:`SqliteSportsbookRepository.append_price_snapshot`, and the
    ``UNIQUE (sb_outcome_id, observed_at, content_hash)`` constraint keeps an
    exact-duplicate observation idempotent.
    """

    payload = {
        "price_american": price_american,
        "point": point,
        "bookmaker_last_update": bookmaker_last_update,
        "market_last_update": market_last_update,
        "provider_timestamp": provider_timestamp,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class SportsbookRepositoryProtocol(Protocol):
    """Operations the odds ingestor and the historical queries need."""

    def upsert_event(
        self,
        *,
        provider: str,
        provider_event_id: str,
        sport_key: str,
        commence_time: str,
        home_team_raw: str,
        away_team_raw: str,
        raw_response_id: str,
        observed_at: str,
        league_id: Optional[str] = None,
    ) -> SportsbookEvent: ...

    def upsert_market(
        self,
        *,
        sb_event_id: str,
        bookmaker_key: str,
        market_key: str,
        raw_response_id: str,
        observed_at: str,
        bookmaker_title: Optional[str] = None,
        bookmaker_last_update: Optional[str] = None,
        market_last_update: Optional[str] = None,
    ) -> SportsbookMarket: ...

    def upsert_outcome(
        self,
        *,
        sb_market_id: str,
        outcome_name: str,
        provider_outcome_name: str,
        outcome_role: str,
        point: Optional[float] = None,
    ) -> SportsbookOutcome: ...

    def append_price_snapshot(
        self,
        *,
        sb_outcome_id: str,
        price_american: int,
        observed_at: str,
        raw_response_id: str,
        raw_response_hash: str,
        run_id: str,
        content_hash: str,
        price_decimal: Optional[float] = None,
        implied_probability: Optional[float] = None,
        point: Optional[float] = None,
        bookmaker_last_update: Optional[str] = None,
        market_last_update: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
    ) -> tuple[Optional[SportsbookPriceSnapshot], bool]: ...


class SqliteSportsbookRepository(Repository):
    """Sportsbook storage and its point-in-time reads."""

    _EVENT_COLUMNS = (
        "sb_event_id, provider, provider_event_id, league_id, sport_key, commence_time, "
        "home_team_raw, away_team_raw, game_id, raw_response_id, first_observed_at, "
        "last_observed_at, created_at, updated_at"
    )
    _MARKET_COLUMNS = (
        "sb_market_id, sb_event_id, bookmaker_key, bookmaker_title, market_key, "
        "bookmaker_last_update, market_last_update, raw_response_id, first_observed_at, "
        "last_observed_at, created_at, updated_at"
    )
    _OUTCOME_COLUMNS = (
        "sb_outcome_id, sb_market_id, outcome_name, provider_outcome_name, outcome_role, "
        "point, point_key, created_at"
    )
    _SNAPSHOT_COLUMNS = (
        "snapshot_id, sb_outcome_id, price_american, price_decimal, implied_probability, "
        "point, bookmaker_last_update, market_last_update, provider_timestamp, observed_at, "
        "ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, created_at"
    )

    # -- Events --------------------------------------------------------------
    def upsert_event(
        self,
        *,
        provider: str,
        provider_event_id: str,
        sport_key: str,
        commence_time: str,
        home_team_raw: str,
        away_team_raw: str,
        raw_response_id: str,
        observed_at: str,
        league_id: Optional[str] = None,
    ) -> SportsbookEvent:
        """Insert an event, or refresh the mutable current-state of an existing one.

        Identity is ``(provider, provider_event_id)``. The surrogate id, the
        creating response, and ``first_observed_at`` never change. ``game_id``
        stays untouched here: linking to a canonical game is Phase D.

        **Stale backfill cannot regress current metadata.** Mutable current
        state (``commence_time``, the team strings, ``last_observed_at``) is
        refreshed *only* when the incoming observation is strictly newer than
        the row's ``last_observed_at``. An older backfill is preserved through
        its raw response and price snapshots but never overwrites newer current
        metadata. On an **equal** ``observed_at`` the existing value is retained
        -- a deterministic tie-break: raw responses are replayed in a fixed
        order, so "first-recorded wins" reproduces identically on a rebuild.
        This mirrors the ``game_status_history`` rule (POINT_IN_TIME_DATA §4).
        """

        existing = self.get_event_by_provider(provider, provider_event_id)
        now = utc_now_iso()
        if existing is None:
            sb_event_id = new_sb_event_id()
            self._conn.execute(
                "INSERT INTO sportsbook_events "
                "(sb_event_id, provider, provider_event_id, league_id, sport_key, "
                " commence_time, home_team_raw, away_team_raw, game_id, raw_response_id, "
                " first_observed_at, last_observed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    sb_event_id,
                    provider,
                    provider_event_id,
                    league_id,
                    sport_key,
                    commence_time,
                    home_team_raw,
                    away_team_raw,
                    raw_response_id,
                    observed_at,
                    observed_at,
                    now,
                    now,
                ),
            )
            fetched = self.get_event(sb_event_id)
            assert fetched is not None  # noqa: S101 - just inserted
            return fetched

        # Strictly newer observation: advance current state. Older-or-equal:
        # leave every mutable column untouched (no regression, deterministic).
        if observed_at > existing.last_observed_at:
            self._conn.execute(
                "UPDATE sportsbook_events SET "
                "league_id = ?, commence_time = ?, home_team_raw = ?, away_team_raw = ?, "
                "last_observed_at = ?, updated_at = ? WHERE sb_event_id = ?",
                (
                    league_id if league_id is not None else existing.league_id,
                    commence_time,
                    home_team_raw,
                    away_team_raw,
                    observed_at,
                    now,
                    existing.sb_event_id,
                ),
            )
        refreshed = self.get_event(existing.sb_event_id)
        assert refreshed is not None  # noqa: S101
        return refreshed

    def get_event(self, sb_event_id: str) -> Optional[SportsbookEvent]:
        row = self._fetch_one(
            f"SELECT {self._EVENT_COLUMNS} FROM sportsbook_events WHERE sb_event_id = ?",
            (sb_event_id,),
        )
        return None if row is None else self._to_event(row)

    def get_event_by_provider(
        self, provider: str, provider_event_id: str
    ) -> Optional[SportsbookEvent]:
        row = self._fetch_one(
            f"SELECT {self._EVENT_COLUMNS} FROM sportsbook_events "
            "WHERE provider = ? AND provider_event_id = ?",
            (provider, provider_event_id),
        )
        return None if row is None else self._to_event(row)

    def list_events(self, *, league_id: Optional[str] = None) -> list[SportsbookEvent]:
        if league_id is None:
            rows = self._fetch_all(
                f"SELECT {self._EVENT_COLUMNS} FROM sportsbook_events "
                "ORDER BY commence_time, sb_event_id"
            )
        else:
            rows = self._fetch_all(
                f"SELECT {self._EVENT_COLUMNS} FROM sportsbook_events WHERE league_id = ? "
                "ORDER BY commence_time, sb_event_id",
                (league_id,),
            )
        return [self._to_event(r) for r in rows]

    def count_events(self) -> int:
        return self._count("SELECT COUNT(*) FROM sportsbook_events")

    # -- D5B1 matching reads/writes ------------------------------------------
    def list_events_for_matching(
        self,
        *,
        provider: Optional[str] = None,
        league_id: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        provider_event_id: Optional[str] = None,
        unmatched_only: bool = False,
    ) -> list[SportsbookEvent]:
        """Bounded, deterministic list of events for a matching run.

        Filters are ANDed; ``from_date``/``to_date`` bound the ``commence_time``
        calendar date inclusively. Ordered by ``(commence_time, sb_event_id)`` so
        a run is reproducible regardless of physical row order.
        """

        sql = f"SELECT {self._EVENT_COLUMNS} FROM sportsbook_events WHERE 1 = 1"  # noqa: S608
        params: list[object] = []
        if provider is not None:
            sql += " AND provider = ?"
            params.append(provider)
        if league_id is not None:
            sql += " AND league_id = ?"
            params.append(league_id)
        if provider_event_id is not None:
            sql += " AND provider_event_id = ?"
            params.append(provider_event_id)
        if from_date is not None:
            sql += " AND substr(commence_time, 1, 10) >= ?"
            params.append(from_date)
        if to_date is not None:
            sql += " AND substr(commence_time, 1, 10) <= ?"
            params.append(to_date)
        if unmatched_only:
            sql += " AND game_id IS NULL"
        sql += " ORDER BY commence_time, sb_event_id"
        return [self._to_event(r) for r in self._fetch_all(sql, tuple(params))]

    def event_link(
        self, sb_event_id: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """``(game_id, match_decision_id, orientation)`` for an event, any may be None."""

        row = self._fetch_one(
            "SELECT game_id, match_decision_id, orientation FROM sportsbook_events "
            "WHERE sb_event_id = ?",
            (sb_event_id,),
        )
        if row is None:
            return (None, None, None)
        return (
            self._opt_str(row, "game_id"),
            self._opt_str(row, "match_decision_id"),
            self._opt_str(row, "orientation"),
        )

    def events_linked_to_game(
        self, game_id: str
    ) -> list[tuple[str, Optional[str], Optional[str]]]:
        """Every ``(sb_event_id, orientation, match_decision_id)`` linked to a game."""

        rows = self._fetch_all(
            "SELECT sb_event_id, orientation, match_decision_id FROM sportsbook_events "
            "WHERE game_id = ? ORDER BY sb_event_id",
            (game_id,),
        )
        return [
            (str(r["sb_event_id"]), self._opt_str(r, "orientation"),
             self._opt_str(r, "match_decision_id"))
            for r in rows
        ]

    def link_game(
        self, *, sb_event_id: str, game_id: str, match_decision_id: str, orientation: str
    ) -> LinkOutcome:
        """Link an event to a canonical game with its exact decision + orientation.

        NULL -> value once (``LINKED``); an identical re-link is idempotent
        (``ALREADY_LINKED``); a different game is a ``CONFLICT`` left untouched --
        the d015 trigger enforces the same immutability at the database. Real
        constraint/DB errors propagate (never swallowed).
        """

        current_game, _decision, _orient = self.event_link(sb_event_id)
        if current_game is None:
            self._conn.execute(
                "UPDATE sportsbook_events SET game_id = ?, match_decision_id = ?, "
                "orientation = ?, updated_at = ? WHERE sb_event_id = ?",
                (game_id, match_decision_id, orientation, utc_now_iso(), sb_event_id),
            )
            return LinkOutcome.LINKED
        if current_game == game_id:
            return LinkOutcome.ALREADY_LINKED
        return LinkOutcome.CONFLICT

    #: Blocking rule codes whose unresolved presence must fail orientation readiness.
    _READINESS_BLOCKING_RULES = ("DQ-MATCH-003", "DQ-MATCH-006", "DQ-SB-LEAGUE-001")

    def is_orientation_approved(self, sb_event_id: str, *, as_of: Optional[str] = None) -> bool:
        """Whether an event's team-outcome orientation is canonically approved.

        Fail-closed. True ONLY when every one of these holds:

        * the event is linked with ``orientation = 'direct'``;
        * its supporting decision is ``accepted`` and not review-gated -- and,
          when ``as_of`` is given, was ``decided_at <= as_of``;
        * the decision and the link agree on the game (``matched_entity_id ==
          game_id``) -- no link-integrity conflict;
        * no OTHER sportsbook event is linked to the same game with a different
          typed orientation (an unresolved cross-event orientation conflict);
        * no unresolved blocking identity/orientation data-quality issue
          (``DQ-MATCH-003``/``DQ-MATCH-006``/``DQ-SB-LEAGUE-001``) is scoped to
          this event.

        A neutral-site swapped match (review-gated) is never approved. A direct
        orientation string alone is not enough when any of the above is violated.
        A pricing consumer uses this to exclude unapproved or blocking
        orientation before interpreting any price.
        """

        row = self._fetch_one(
            "SELECT e.orientation AS orientation, e.game_id AS game_id, "
            "d.matched_entity_id AS matched_entity_id, d.outcome AS outcome, "
            "d.needs_manual_review AS review, d.decided_at AS decided_at "
            "FROM sportsbook_events e JOIN entity_match_decisions d "
            "ON e.match_decision_id = d.match_id WHERE e.sb_event_id = ?",
            (sb_event_id,),
        )
        if row is None:
            return False
        if as_of is not None and str(row["decided_at"]) > as_of:
            return False
        game_id = self._opt_str(row, "game_id")
        if not (
            str(row["orientation"]) == "direct"
            and str(row["outcome"]) == "accepted"
            and int(row["review"]) == 0
            and game_id is not None
            and self._opt_str(row, "matched_entity_id") == game_id
        ):
            return False
        # A different event linked to the same game with an incompatible
        # orientation makes this game's orientation contested -> not safe.
        for other_id, other_orient, _dec in self.events_linked_to_game(game_id):
            if other_id != sb_event_id and other_orient is not None and other_orient != "direct":
                return False
        # Any unresolved blocking identity/orientation issue on this event fails closed.
        placeholders = ", ".join("?" for _ in self._READINESS_BLOCKING_RULES)
        blocking = self._fetch_one(
            "SELECT 1 FROM data_quality_issues WHERE severity = 'blocking' "
            "AND resolved_at IS NULL AND entity_type = 'sportsbook_event' AND entity_id = ? "
            f"AND rule_code IN ({placeholders}) LIMIT 1",  # noqa: S608 - fixed rule tuple
            (sb_event_id, *self._READINESS_BLOCKING_RULES),
        )
        return blocking is None

    # -- Markets -------------------------------------------------------------
    def upsert_market(
        self,
        *,
        sb_event_id: str,
        bookmaker_key: str,
        market_key: str,
        raw_response_id: str,
        observed_at: str,
        bookmaker_title: Optional[str] = None,
        bookmaker_last_update: Optional[str] = None,
        market_last_update: Optional[str] = None,
    ) -> SportsbookMarket:
        """Insert a market, or refresh an existing one's provider update times.

        Same stale-backfill protection as :meth:`upsert_event`: the bookmaker
        title and the bookmaker/market update times are refreshed only when the
        incoming observation is strictly newer than ``last_observed_at``. An
        older backfill is preserved through its snapshots but does not regress
        the current market metadata; an equal ``observed_at`` retains the
        existing value (deterministic under ordered replay).
        """

        existing = self.get_market_by_key(sb_event_id, bookmaker_key, market_key)
        now = utc_now_iso()
        if existing is None:
            sb_market_id = new_sb_market_id()
            self._conn.execute(
                "INSERT INTO sportsbook_markets "
                "(sb_market_id, sb_event_id, bookmaker_key, bookmaker_title, market_key, "
                " bookmaker_last_update, market_last_update, raw_response_id, "
                " first_observed_at, last_observed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sb_market_id,
                    sb_event_id,
                    bookmaker_key,
                    bookmaker_title,
                    market_key,
                    bookmaker_last_update,
                    market_last_update,
                    raw_response_id,
                    observed_at,
                    observed_at,
                    now,
                    now,
                ),
            )
            fetched = self.get_market(sb_market_id)
            assert fetched is not None  # noqa: S101
            return fetched

        if observed_at > existing.last_observed_at:
            self._conn.execute(
                "UPDATE sportsbook_markets SET "
                "bookmaker_title = ?, bookmaker_last_update = ?, market_last_update = ?, "
                "last_observed_at = ?, updated_at = ? WHERE sb_market_id = ?",
                (
                    bookmaker_title if bookmaker_title is not None else existing.bookmaker_title,
                    bookmaker_last_update,
                    market_last_update,
                    observed_at,
                    now,
                    existing.sb_market_id,
                ),
            )
        refreshed = self.get_market(existing.sb_market_id)
        assert refreshed is not None  # noqa: S101
        return refreshed

    def get_market(self, sb_market_id: str) -> Optional[SportsbookMarket]:
        row = self._fetch_one(
            f"SELECT {self._MARKET_COLUMNS} FROM sportsbook_markets WHERE sb_market_id = ?",
            (sb_market_id,),
        )
        return None if row is None else self._to_market(row)

    def get_market_by_key(
        self, sb_event_id: str, bookmaker_key: str, market_key: str
    ) -> Optional[SportsbookMarket]:
        row = self._fetch_one(
            f"SELECT {self._MARKET_COLUMNS} FROM sportsbook_markets "
            "WHERE sb_event_id = ? AND bookmaker_key = ? AND market_key = ?",
            (sb_event_id, bookmaker_key, market_key),
        )
        return None if row is None else self._to_market(row)

    def list_markets_for_event(self, sb_event_id: str) -> list[SportsbookMarket]:
        return [
            self._to_market(r)
            for r in self._fetch_all(
                f"SELECT {self._MARKET_COLUMNS} FROM sportsbook_markets WHERE sb_event_id = ? "
                "ORDER BY bookmaker_key, market_key",
                (sb_event_id,),
            )
        ]

    def count_markets(self) -> int:
        return self._count("SELECT COUNT(*) FROM sportsbook_markets")

    # -- Outcomes ------------------------------------------------------------
    def upsert_outcome(
        self,
        *,
        sb_market_id: str,
        outcome_name: str,
        provider_outcome_name: str,
        outcome_role: str,
        point: Optional[float] = None,
    ) -> SportsbookOutcome:
        """Insert an outcome identity, or return the existing one unchanged.

        The identity is ``(market, normalized name, point_key)``. A changed
        price is never a new outcome -- prices live in the snapshot table -- so
        this is a pure identity upsert with no mutable columns to refresh.
        """

        pk = point_key(point)
        existing = self.get_outcome_by_identity(sb_market_id, outcome_name, pk)
        if existing is not None:
            return existing

        sb_outcome_id = new_sb_outcome_id()
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO sportsbook_outcomes "
            "(sb_outcome_id, sb_market_id, outcome_name, provider_outcome_name, "
            " outcome_role, point, point_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sb_outcome_id,
                sb_market_id,
                outcome_name,
                provider_outcome_name,
                outcome_role,
                point,
                pk,
                now,
            ),
        )
        fetched = self.get_outcome(sb_outcome_id)
        assert fetched is not None  # noqa: S101
        return fetched

    def get_outcome(self, sb_outcome_id: str) -> Optional[SportsbookOutcome]:
        row = self._fetch_one(
            f"SELECT {self._OUTCOME_COLUMNS} FROM sportsbook_outcomes WHERE sb_outcome_id = ?",
            (sb_outcome_id,),
        )
        return None if row is None else self._to_outcome(row)

    def get_outcome_by_identity(
        self, sb_market_id: str, outcome_name: str, pk: str
    ) -> Optional[SportsbookOutcome]:
        row = self._fetch_one(
            f"SELECT {self._OUTCOME_COLUMNS} FROM sportsbook_outcomes "
            "WHERE sb_market_id = ? AND outcome_name = ? AND point_key = ?",
            (sb_market_id, outcome_name, pk),
        )
        return None if row is None else self._to_outcome(row)

    def list_outcomes_for_market(self, sb_market_id: str) -> list[SportsbookOutcome]:
        return [
            self._to_outcome(r)
            for r in self._fetch_all(
                f"SELECT {self._OUTCOME_COLUMNS} FROM sportsbook_outcomes WHERE sb_market_id = ? "
                "ORDER BY outcome_role, outcome_name, point_key",
                (sb_market_id,),
            )
        ]

    def count_outcomes(self) -> int:
        return self._count("SELECT COUNT(*) FROM sportsbook_outcomes")

    # -- Price snapshots -----------------------------------------------------
    def append_price_snapshot(
        self,
        *,
        sb_outcome_id: str,
        price_american: int,
        observed_at: str,
        raw_response_id: str,
        raw_response_hash: str,
        run_id: str,
        content_hash: str,
        price_decimal: Optional[float] = None,
        implied_probability: Optional[float] = None,
        point: Optional[float] = None,
        bookmaker_last_update: Optional[str] = None,
        market_last_update: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
    ) -> tuple[Optional[SportsbookPriceSnapshot], bool]:
        """Append a price observation. Returns ``(snapshot, inserted)``.

        **Transition-aware deduplication.** The observation is compared against
        its *immediate temporal predecessor* -- the latest snapshot for this
        outcome at or before ``observed_at`` -- not against the whole history.
        It is skipped only when its ``content_hash`` equals that predecessor's,
        so:

        * a consecutive unchanged re-poll collapses (predecessor is identical);
        * a changed price appends;
        * a reversal ``-110 -> -120 -> -110`` keeps all three, because the third
          differs from its predecessor ``-120`` even though it equals the first;
        * an exact replay of an existing observation is idempotent (its
          predecessor is itself, and the ``UNIQUE (sb_outcome_id, observed_at,
          content_hash)`` constraint is the backstop);
        * a backfill is compared against its own temporal neighbour, so a
          repeated backfill is idempotent while a genuinely new earlier
          observation appends.

        No historical row is ever updated or deleted -- the table is append-only.
        """

        predecessor = self._fetch_one(
            "SELECT content_hash FROM sportsbook_price_snapshots "
            "WHERE sb_outcome_id = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            (sb_outcome_id, observed_at),
        )
        if predecessor is not None and str(predecessor["content_hash"]) == content_hash:
            # Unchanged from the immediately preceding observation: no new
            # information. Return that predecessor row for reference.
            existing = self._fetch_one(
                f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
                "WHERE sb_outcome_id = ? AND observed_at <= ? AND content_hash = ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                (sb_outcome_id, observed_at, content_hash),
            )
            return (None if existing is None else self._to_snapshot(existing)), False

        snapshot_id = new_sb_price_snapshot_id()
        now = utc_now_iso()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO sportsbook_price_snapshots "
            "(snapshot_id, sb_outcome_id, price_american, price_decimal, implied_probability, "
            " point, bookmaker_last_update, market_last_update, provider_timestamp, "
            " observed_at, ingested_at, raw_response_id, raw_response_hash, run_id, "
            " content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                sb_outcome_id,
                price_american,
                price_decimal,
                implied_probability,
                point,
                bookmaker_last_update,
                market_last_update,
                provider_timestamp,
                observed_at,
                now,
                raw_response_id,
                raw_response_hash,
                run_id,
                content_hash,
                now,
            ),
        )
        if cursor.rowcount == 0:
            # The predecessor differed, but an exact (outcome, observed_at,
            # content) row already exists -- e.g. a reversal replayed at the same
            # observation time. The UNIQUE constraint makes this idempotent.
            existing = self._fetch_one(
                f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
                "WHERE sb_outcome_id = ? AND observed_at = ? AND content_hash = ?",
                (sb_outcome_id, observed_at, content_hash),
            )
            return (None if existing is None else self._to_snapshot(existing)), False

        inserted = self._fetch_one(
            f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        )
        assert inserted is not None  # noqa: S101
        return self._to_snapshot(inserted), True

    def price_as_of(
        self, sb_outcome_id: str, as_of: str
    ) -> Optional[SportsbookPriceSnapshot]:
        """The latest price for an outcome observed at or before ``as_of``.

        Filters on ``observed_at`` -- the transaction-time cutoff -- never on
        the provider timestamp. Ties break by ``snapshot_id`` (a monotonic
        ULID), so a rebuild yields the identical answer.
        """

        row = self._fetch_one(
            f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
            "WHERE sb_outcome_id = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            (sb_outcome_id, as_of),
        )
        return None if row is None else self._to_snapshot(row)

    def latest_price(self, sb_outcome_id: str) -> Optional[SportsbookPriceSnapshot]:
        """The most recent price for an outcome across all observations."""

        row = self._fetch_one(
            f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
            "WHERE sb_outcome_id = ? ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            (sb_outcome_id,),
        )
        return None if row is None else self._to_snapshot(row)

    def prices_in_range(
        self, sb_outcome_id: str, *, start: str, end: str
    ) -> list[SportsbookPriceSnapshot]:
        """Every price observation for an outcome in ``[start, end]``, chronological."""

        return [
            self._to_snapshot(r)
            for r in self._fetch_all(
                f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
                "WHERE sb_outcome_id = ? AND observed_at >= ? AND observed_at <= ? "
                "ORDER BY observed_at, snapshot_id",
                (sb_outcome_id, start, end),
            )
        ]

    def list_snapshots_for_outcome(
        self, sb_outcome_id: str
    ) -> list[SportsbookPriceSnapshot]:
        return [
            self._to_snapshot(r)
            for r in self._fetch_all(
                f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
                "WHERE sb_outcome_id = ? ORDER BY observed_at, snapshot_id",
                (sb_outcome_id,),
            )
        ]

    def count_snapshots(self) -> int:
        return self._count("SELECT COUNT(*) FROM sportsbook_price_snapshots")

    # -- Mapping -------------------------------------------------------------
    def _to_event(self, row: sqlite3.Row) -> SportsbookEvent:
        return SportsbookEvent(
            sb_event_id=str(row["sb_event_id"]),
            provider=str(row["provider"]),
            provider_event_id=str(row["provider_event_id"]),
            sport_key=str(row["sport_key"]),
            commence_time=str(row["commence_time"]),
            home_team_raw=str(row["home_team_raw"]),
            away_team_raw=str(row["away_team_raw"]),
            raw_response_id=str(row["raw_response_id"]),
            first_observed_at=str(row["first_observed_at"]),
            last_observed_at=str(row["last_observed_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            league_id=self._opt_str(row, "league_id"),
            game_id=self._opt_str(row, "game_id"),
        )

    def _to_market(self, row: sqlite3.Row) -> SportsbookMarket:
        return SportsbookMarket(
            sb_market_id=str(row["sb_market_id"]),
            sb_event_id=str(row["sb_event_id"]),
            bookmaker_key=str(row["bookmaker_key"]),
            market_key=str(row["market_key"]),
            raw_response_id=str(row["raw_response_id"]),
            first_observed_at=str(row["first_observed_at"]),
            last_observed_at=str(row["last_observed_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            bookmaker_title=self._opt_str(row, "bookmaker_title"),
            bookmaker_last_update=self._opt_str(row, "bookmaker_last_update"),
            market_last_update=self._opt_str(row, "market_last_update"),
        )

    def _to_outcome(self, row: sqlite3.Row) -> SportsbookOutcome:
        point = row["point"]
        return SportsbookOutcome(
            sb_outcome_id=str(row["sb_outcome_id"]),
            sb_market_id=str(row["sb_market_id"]),
            outcome_name=str(row["outcome_name"]),
            provider_outcome_name=str(row["provider_outcome_name"]),
            outcome_role=str(row["outcome_role"]),
            point_key=str(row["point_key"]),
            created_at=str(row["created_at"]),
            point=None if point is None else float(point),
        )

    def _to_snapshot(self, row: sqlite3.Row) -> SportsbookPriceSnapshot:
        return SportsbookPriceSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            sb_outcome_id=str(row["sb_outcome_id"]),
            price_american=int(row["price_american"]),
            observed_at=str(row["observed_at"]),
            ingested_at=str(row["ingested_at"]),
            raw_response_id=str(row["raw_response_id"]),
            raw_response_hash=str(row["raw_response_hash"]),
            run_id=str(row["run_id"]),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
            price_decimal=self._opt_float(row, "price_decimal"),
            implied_probability=self._opt_float(row, "implied_probability"),
            point=self._opt_float(row, "point"),
            bookmaker_last_update=self._opt_str(row, "bookmaker_last_update"),
            market_last_update=self._opt_str(row, "market_last_update"),
            provider_timestamp=self._opt_str(row, "provider_timestamp"),
        )

    @staticmethod
    def _opt_float(row: sqlite3.Row, column: str) -> Optional[float]:
        value = row[column]
        return None if value is None else float(value)
