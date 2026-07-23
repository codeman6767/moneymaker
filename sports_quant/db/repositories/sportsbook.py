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
    """Identity of a *price observation*, deliberately excluding ``observed_at``.

    Two polls returning the same price and the same provider update times are
    the same observation and collapse to one row; a genuinely new price, or the
    same price the provider re-stamps with a later ``last_update``, is a new
    observation and appends. Excluding ``observed_at`` is what makes an
    unchanged re-poll idempotent.
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

        Identity is ``(provider, provider_event_id)``. On a repeat sighting the
        commence time, team strings and ``last_observed_at`` are refreshed --
        a provider legitimately moves a game -- while the surrogate id, the
        creating response, and ``first_observed_at`` never change. ``game_id``
        stays untouched here: linking to a canonical game is Phase D.
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

        # Refresh mutable current state. last_observed_at advances only forward.
        latest_observed = max(existing.last_observed_at, observed_at)
        self._conn.execute(
            "UPDATE sportsbook_events SET "
            "league_id = ?, commence_time = ?, home_team_raw = ?, away_team_raw = ?, "
            "last_observed_at = ?, updated_at = ? WHERE sb_event_id = ?",
            (
                league_id if league_id is not None else existing.league_id,
                commence_time,
                home_team_raw,
                away_team_raw,
                latest_observed,
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
        """Insert a market, or refresh an existing one's provider update times."""

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

        latest_observed = max(existing.last_observed_at, observed_at)
        self._conn.execute(
            "UPDATE sportsbook_markets SET "
            "bookmaker_title = ?, bookmaker_last_update = ?, market_last_update = ?, "
            "last_observed_at = ?, updated_at = ? WHERE sb_market_id = ?",
            (
                bookmaker_title if bookmaker_title is not None else existing.bookmaker_title,
                bookmaker_last_update,
                market_last_update,
                latest_observed,
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

        Idempotent on ``(sb_outcome_id, content_hash)`` via ``INSERT OR
        IGNORE``: re-ingesting an unchanged price writes nothing and returns
        ``inserted=False``. A later price, or an older backfill, appends a new
        row without touching what is already stored -- the table is append-only.
        """

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
            existing = self._fetch_one(
                f"SELECT {self._SNAPSHOT_COLUMNS} FROM sportsbook_price_snapshots "
                "WHERE sb_outcome_id = ? AND content_hash = ?",
                (sb_outcome_id, content_hash),
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
