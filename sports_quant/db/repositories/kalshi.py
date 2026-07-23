"""Kalshi public-data repository: events, markets, order books, and trades.

All data is public and read-only. There is no account, balance, position, fill,
or order surface here -- the trade table records anonymous market-wide prints,
never our fills, because we have none.

Order books are stored as a metadata snapshot plus its ladder levels
(``kalshi_orderbook_levels``), keyed by a content hash over the full ladders.
They are deduplicated **transition-aware**: an observation is appended only when
it differs from its immediate temporal predecessor, so an unchanged re-poll
collapses while a book that returns to an earlier state is preserved. Derived
executable asks are computed from the opposing side's best bid
(``100 - best_no_bid``); a returned bid is never treated as an ask.

Trades are immutable events keyed by ``(market_ticker, content_hash)``, where the
content hash uses the provider trade id when present and otherwise a documented
field-based identity -- so re-ingesting the same trade is idempotent while a
genuinely different trade (different id, time, price, or side) appends.

Point-in-time reads filter on ``observed_at`` (our transaction-time clock) and
break ties by ``snapshot_id`` / ``trade_id`` (monotonic ULIDs), so a rebuild
yields identical answers and nothing observed after the cutoff is exposed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Optional, Protocol

from streaming.event_envelope import canonical_json

from ..ids import (
    new_kalshi_book_id,
    new_kalshi_event_id,
    new_kalshi_level_id,
    new_kalshi_market_id,
    new_kalshi_trade_id,
)
from ..models import (
    KalshiEvent,
    KalshiMarket,
    KalshiOrderbookLevel,
    KalshiOrderbookSnapshot,
    KalshiPublicTrade,
)
from ..schema import KALSHI_PRICE_COMPLEMENT, utc_now_iso
from .base import Repository, to_db_bool

Level = tuple[int, int]  # (price_cents, quantity)


# --------------------------------------------------------------------------- #
# Content hashing
# --------------------------------------------------------------------------- #
def orderbook_content_hash(*, yes_bids: list[Level], no_bids: list[Level]) -> str:
    """Identity of an order-book *state*: the full yes/no ladders.

    Excludes ``observed_at`` on purpose, so two observations of the same book at
    different times hash identically and the repository can detect "nothing
    changed" against the immediate predecessor. Levels are sorted so the hash is
    independent of provider ordering.
    """

    payload = {
        "yes": sorted([[int(p), int(q)] for p, q in yes_bids]),
        "no": sorted([[int(p), int(q)] for p, q in no_bids]),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def trade_content_hash(
    *,
    provider_trade_id: Optional[str],
    market_ticker: str,
    trade_time: Optional[str],
    yes_price: Optional[int],
    no_price: Optional[int],
    count: int,
    taker_side: Optional[str],
) -> str:
    """Deterministic identity of a public trade.

    When the provider supplies a stable trade id, that id (scoped by market) is
    the identity -- the strongest available. When it does not, the identity is a
    documented function of the provider-supplied fields only
    (``market_ticker, trade_time, yes_price, no_price, count, taker_side``); no
    id is ever invented. Either way, two genuinely different trades (different
    time, price, side, or id) hash differently and both persist, while an exact
    replay of one trade collapses.
    """

    payload: dict[str, Any]
    if provider_trade_id:
        payload = {"market_ticker": market_ticker, "provider_trade_id": provider_trade_id}
    else:
        payload = {
            "market_ticker": market_ticker,
            "trade_time": trade_time,
            "yes_price": yes_price,
            "no_price": no_price,
            "count": count,
            "taker_side": taker_side,
        }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class KalshiRepositoryProtocol(Protocol):
    """Operations the Kalshi ingestor and the historical queries need."""

    def upsert_event(self, *, event_ticker: str, raw_response_id: str, observed_at: str,
                     **meta: object) -> KalshiEvent: ...

    def upsert_market(self, *, market_ticker: str, raw_response_id: str, observed_at: str,
                      **meta: object) -> KalshiMarket: ...

    def append_orderbook_snapshot(
        self, *, market_ticker: str, yes_bids: list[Level], no_bids: list[Level],
        observed_at: str, run_id: str, raw_response_id: str, raw_response_hash: str,
        content_hash: str, kalshi_market_id: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
    ) -> tuple[Optional[KalshiOrderbookSnapshot], bool]: ...

    def append_trade(
        self, *, market_ticker: str, count: int, observed_at: str, run_id: str,
        raw_response_id: str, content_hash: str, **fields: object,
    ) -> tuple[Optional[KalshiPublicTrade], bool]: ...


class SqliteKalshiRepository(Repository):
    """Kalshi public storage and its point-in-time reads."""

    _EVENT_COLUMNS = (
        "kalshi_event_id, event_ticker, series_ticker, title, sub_title, category, status, "
        "mutually_exclusive, game_id, raw_response_id, first_observed_at, last_observed_at, "
        "created_at, updated_at"
    )
    _MARKET_COLUMNS = (
        "kalshi_market_id, market_ticker, event_ticker, kalshi_event_id, series_ticker, title, "
        "subtitle, yes_sub_title, no_sub_title, status, open_time, close_time, expiration_time, "
        "settlement_time, result, rules_primary, rules_secondary, rules_hash, game_id, "
        "raw_response_id, first_observed_at, last_observed_at, created_at, updated_at"
    )
    _BOOK_COLUMNS = (
        "snapshot_id, kalshi_market_id, market_ticker, best_yes_bid, best_no_bid, "
        "derived_yes_ask, derived_no_ask, yes_levels, no_levels, depth_levels, "
        "provider_timestamp, observed_at, ingested_at, run_id, raw_response_id, "
        "raw_response_hash, content_hash, created_at"
    )
    _LEVEL_COLUMNS = "level_id, snapshot_id, side, price, quantity, level_index, created_at"
    _TRADE_COLUMNS = (
        "trade_id, provider_trade_id, kalshi_market_id, market_ticker, taker_side, yes_price, "
        "no_price, count, trade_time, provider_timestamp, observed_at, ingested_at, run_id, "
        "raw_response_id, content_hash, created_at"
    )

    # -- Events --------------------------------------------------------------
    def upsert_event(
        self,
        *,
        event_ticker: str,
        raw_response_id: str,
        observed_at: str,
        series_ticker: Optional[str] = None,
        title: Optional[str] = None,
        sub_title: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
        mutually_exclusive: Optional[bool] = None,
    ) -> KalshiEvent:
        """Insert an event, or refresh its mutable current-state if newer.

        Identity is ``event_ticker``. Mutable metadata (title, status, ...) and
        ``last_observed_at`` are refreshed only when ``observed_at`` is strictly
        newer than the stored value; an older backfill is preserved but never
        regresses newer current metadata (equal ``observed_at`` retains the
        earlier-recorded value -- deterministic under ordered replay).
        """

        existing = self.get_event_by_ticker(event_ticker)
        now = utc_now_iso()
        me = None if mutually_exclusive is None else to_db_bool(mutually_exclusive)
        if existing is None:
            kalshi_event_id = new_kalshi_event_id()
            self._conn.execute(
                "INSERT INTO kalshi_events "
                "(kalshi_event_id, event_ticker, series_ticker, title, sub_title, category, "
                " status, mutually_exclusive, game_id, raw_response_id, first_observed_at, "
                " last_observed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    kalshi_event_id, event_ticker, series_ticker, title, sub_title, category,
                    status, me, raw_response_id, observed_at, observed_at, now, now,
                ),
            )
            fetched = self.get_event(kalshi_event_id)
            assert fetched is not None  # noqa: S101
            return fetched

        if observed_at > existing.last_observed_at:
            self._conn.execute(
                "UPDATE kalshi_events SET series_ticker = ?, title = ?, sub_title = ?, "
                "category = ?, status = ?, mutually_exclusive = ?, last_observed_at = ?, "
                "updated_at = ? WHERE kalshi_event_id = ?",
                (series_ticker, title, sub_title, category, status, me, observed_at, now,
                 existing.kalshi_event_id),
            )
        refreshed = self.get_event(existing.kalshi_event_id)
        assert refreshed is not None  # noqa: S101
        return refreshed

    def get_event(self, kalshi_event_id: str) -> Optional[KalshiEvent]:
        row = self._fetch_one(
            f"SELECT {self._EVENT_COLUMNS} FROM kalshi_events WHERE kalshi_event_id = ?",
            (kalshi_event_id,),
        )
        return None if row is None else self._to_event(row)

    def get_event_by_ticker(self, event_ticker: str) -> Optional[KalshiEvent]:
        row = self._fetch_one(
            f"SELECT {self._EVENT_COLUMNS} FROM kalshi_events WHERE event_ticker = ?",
            (event_ticker,),
        )
        return None if row is None else self._to_event(row)

    def count_events(self) -> int:
        return self._count("SELECT COUNT(*) FROM kalshi_events")

    # -- Markets -------------------------------------------------------------
    def upsert_market(
        self,
        *,
        market_ticker: str,
        raw_response_id: str,
        observed_at: str,
        event_ticker: Optional[str] = None,
        kalshi_event_id: Optional[str] = None,
        series_ticker: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        yes_sub_title: Optional[str] = None,
        no_sub_title: Optional[str] = None,
        status: Optional[str] = None,
        open_time: Optional[str] = None,
        close_time: Optional[str] = None,
        expiration_time: Optional[str] = None,
        settlement_time: Optional[str] = None,
        result: Optional[str] = None,
        rules_primary: Optional[str] = None,
        rules_secondary: Optional[str] = None,
        rules_hash: Optional[str] = None,
    ) -> KalshiMarket:
        """Insert a market, or refresh its mutable current-state if newer.

        Same stale-backfill protection as :meth:`upsert_event`. ``game_id`` is
        never attached in Phase C.
        """

        existing = self.get_market_by_ticker(market_ticker)
        now = utc_now_iso()
        if existing is None:
            kalshi_market_id = new_kalshi_market_id()
            self._conn.execute(
                "INSERT INTO kalshi_markets "
                "(kalshi_market_id, market_ticker, event_ticker, kalshi_event_id, series_ticker, "
                " title, subtitle, yes_sub_title, no_sub_title, status, open_time, close_time, "
                " expiration_time, settlement_time, result, rules_primary, rules_secondary, "
                " rules_hash, game_id, raw_response_id, first_observed_at, last_observed_at, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, "
                " ?, ?)",
                (
                    kalshi_market_id, market_ticker, event_ticker, kalshi_event_id, series_ticker,
                    title, subtitle, yes_sub_title, no_sub_title, status, open_time, close_time,
                    expiration_time, settlement_time, result, rules_primary, rules_secondary,
                    rules_hash, raw_response_id, observed_at, observed_at, now, now,
                ),
            )
            fetched = self.get_market(kalshi_market_id)
            assert fetched is not None  # noqa: S101
            return fetched

        if observed_at > existing.last_observed_at:
            self._conn.execute(
                "UPDATE kalshi_markets SET event_ticker = ?, kalshi_event_id = ?, "
                "series_ticker = ?, title = ?, subtitle = ?, yes_sub_title = ?, no_sub_title = ?, "
                "status = ?, open_time = ?, close_time = ?, expiration_time = ?, "
                "settlement_time = ?, result = ?, rules_primary = ?, rules_secondary = ?, "
                "rules_hash = ?, last_observed_at = ?, updated_at = ? WHERE kalshi_market_id = ?",
                (
                    event_ticker if event_ticker is not None else existing.event_ticker,
                    kalshi_event_id if kalshi_event_id is not None else existing.kalshi_event_id,
                    series_ticker, title, subtitle, yes_sub_title, no_sub_title, status,
                    open_time, close_time, expiration_time, settlement_time, result,
                    rules_primary, rules_secondary, rules_hash, observed_at, now,
                    existing.kalshi_market_id,
                ),
            )
        refreshed = self.get_market(existing.kalshi_market_id)
        assert refreshed is not None  # noqa: S101
        return refreshed

    def get_market(self, kalshi_market_id: str) -> Optional[KalshiMarket]:
        row = self._fetch_one(
            f"SELECT {self._MARKET_COLUMNS} FROM kalshi_markets WHERE kalshi_market_id = ?",
            (kalshi_market_id,),
        )
        return None if row is None else self._to_market(row)

    def get_market_by_ticker(self, market_ticker: str) -> Optional[KalshiMarket]:
        row = self._fetch_one(
            f"SELECT {self._MARKET_COLUMNS} FROM kalshi_markets WHERE market_ticker = ?",
            (market_ticker,),
        )
        return None if row is None else self._to_market(row)

    def list_markets_for_event(self, kalshi_event_id: str) -> list[KalshiMarket]:
        return [
            self._to_market(r)
            for r in self._fetch_all(
                f"SELECT {self._MARKET_COLUMNS} FROM kalshi_markets WHERE kalshi_event_id = ? "
                "ORDER BY market_ticker",
                (kalshi_event_id,),
            )
        ]

    def count_markets(self) -> int:
        return self._count("SELECT COUNT(*) FROM kalshi_markets")

    # -- Order books ---------------------------------------------------------
    def append_orderbook_snapshot(
        self,
        *,
        market_ticker: str,
        yes_bids: list[Level],
        no_bids: list[Level],
        observed_at: str,
        run_id: str,
        raw_response_id: str,
        raw_response_hash: str,
        content_hash: str,
        kalshi_market_id: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
    ) -> tuple[Optional[KalshiOrderbookSnapshot], bool]:
        """Append an order-book observation and its ladder. Returns ``(snap, inserted)``.

        Transition-aware: skipped only when unchanged from its immediate temporal
        predecessor. A book returning to an earlier state appends (differs from
        its predecessor); an exact replay at the same ``observed_at`` is
        idempotent (``UNIQUE (market_ticker, observed_at, content_hash)``). The
        snapshot row and its levels are written in one transaction.
        """

        predecessor = self._fetch_one(
            "SELECT content_hash FROM kalshi_orderbook_snapshots "
            "WHERE market_ticker = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            (market_ticker, observed_at),
        )
        if predecessor is not None and str(predecessor["content_hash"]) == content_hash:
            existing = self._fetch_one(
                f"SELECT {self._BOOK_COLUMNS} FROM kalshi_orderbook_snapshots "
                "WHERE market_ticker = ? AND observed_at <= ? AND content_hash = ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                (market_ticker, observed_at, content_hash),
            )
            return (None if existing is None else self._to_book(existing)), False

        snapshot_id = new_kalshi_book_id()
        now = utc_now_iso()
        best_yes = yes_bids[0][0] if yes_bids else None
        best_no = no_bids[0][0] if no_bids else None
        derived_yes_ask = None if best_no is None else KALSHI_PRICE_COMPLEMENT - best_no
        derived_no_ask = None if best_yes is None else KALSHI_PRICE_COMPLEMENT - best_yes

        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO kalshi_orderbook_snapshots "
            "(snapshot_id, kalshi_market_id, market_ticker, best_yes_bid, best_no_bid, "
            " derived_yes_ask, derived_no_ask, yes_levels, no_levels, depth_levels, "
            " provider_timestamp, observed_at, ingested_at, run_id, raw_response_id, "
            " raw_response_hash, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id, kalshi_market_id, market_ticker, best_yes, best_no,
                derived_yes_ask, derived_no_ask, len(yes_bids), len(no_bids),
                len(yes_bids) + len(no_bids), provider_timestamp, observed_at, now, run_id,
                raw_response_id, raw_response_hash, content_hash, now,
            ),
        )
        if cursor.rowcount == 0:
            existing = self._fetch_one(
                f"SELECT {self._BOOK_COLUMNS} FROM kalshi_orderbook_snapshots "
                "WHERE market_ticker = ? AND observed_at = ? AND content_hash = ?",
                (market_ticker, observed_at, content_hash),
            )
            return (None if existing is None else self._to_book(existing)), False

        for side, bids in (("yes", yes_bids), ("no", no_bids)):
            for index, (price, quantity) in enumerate(bids):
                self._conn.execute(
                    "INSERT INTO kalshi_orderbook_levels "
                    "(level_id, snapshot_id, side, price, quantity, level_index, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (new_kalshi_level_id(), snapshot_id, side, price, quantity, index, now),
                )

        inserted = self._fetch_one(
            f"SELECT {self._BOOK_COLUMNS} FROM kalshi_orderbook_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        )
        assert inserted is not None  # noqa: S101
        return self._to_book(inserted), True

    def orderbook_as_of(
        self, market_ticker: str, as_of: str
    ) -> Optional[KalshiOrderbookSnapshot]:
        """The latest order book for a market observed at or before ``as_of``."""

        row = self._fetch_one(
            f"SELECT {self._BOOK_COLUMNS} FROM kalshi_orderbook_snapshots "
            "WHERE market_ticker = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            (market_ticker, as_of),
        )
        return None if row is None else self._to_book(row)

    def latest_orderbook(self, market_ticker: str) -> Optional[KalshiOrderbookSnapshot]:
        row = self._fetch_one(
            f"SELECT {self._BOOK_COLUMNS} FROM kalshi_orderbook_snapshots "
            "WHERE market_ticker = ? ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            (market_ticker,),
        )
        return None if row is None else self._to_book(row)

    def orderbook_levels(self, snapshot_id: str) -> list[KalshiOrderbookLevel]:
        """The full ladder for a snapshot, both sides, best-first per side."""

        return [
            self._to_level(r)
            for r in self._fetch_all(
                f"SELECT {self._LEVEL_COLUMNS} FROM kalshi_orderbook_levels WHERE snapshot_id = ? "
                "ORDER BY side, level_index",
                (snapshot_id,),
            )
        ]

    def count_orderbook_snapshots(self) -> int:
        return self._count("SELECT COUNT(*) FROM kalshi_orderbook_snapshots")

    def count_orderbook_levels(self) -> int:
        return self._count("SELECT COUNT(*) FROM kalshi_orderbook_levels")

    # -- Trades --------------------------------------------------------------
    def append_trade(
        self,
        *,
        market_ticker: str,
        count: int,
        observed_at: str,
        run_id: str,
        raw_response_id: str,
        content_hash: str,
        provider_trade_id: Optional[str] = None,
        kalshi_market_id: Optional[str] = None,
        taker_side: Optional[str] = None,
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        trade_time: Optional[str] = None,
        provider_timestamp: Optional[str] = None,
    ) -> tuple[Optional[KalshiPublicTrade], bool]:
        """Append a public trade. Idempotent on ``(market_ticker, content_hash)``.

        A trade is an immutable historical event, so re-ingesting the same trade
        (same identity) writes nothing. Two genuinely different trades that share
        a price but differ in id/time/side hash differently and both persist.
        """

        trade_id = new_kalshi_trade_id()
        now = utc_now_iso()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO kalshi_public_trades "
            "(trade_id, provider_trade_id, kalshi_market_id, market_ticker, taker_side, "
            " yes_price, no_price, count, trade_time, provider_timestamp, observed_at, "
            " ingested_at, run_id, raw_response_id, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id, provider_trade_id, kalshi_market_id, market_ticker, taker_side,
                yes_price, no_price, count, trade_time, provider_timestamp, observed_at, now,
                run_id, raw_response_id, content_hash, now,
            ),
        )
        if cursor.rowcount == 0:
            existing = self._fetch_one(
                f"SELECT {self._TRADE_COLUMNS} FROM kalshi_public_trades "
                "WHERE market_ticker = ? AND content_hash = ?",
                (market_ticker, content_hash),
            )
            return (None if existing is None else self._to_trade(existing)), False

        inserted = self._fetch_one(
            f"SELECT {self._TRADE_COLUMNS} FROM kalshi_public_trades WHERE trade_id = ?",
            (trade_id,),
        )
        assert inserted is not None  # noqa: S101
        return self._to_trade(inserted), True

    def trades_in_range(
        self, market_ticker: str, *, start: str, end: str
    ) -> list[KalshiPublicTrade]:
        """Public trades for a market with ``observed_at`` in ``[start, end]``."""

        return [
            self._to_trade(r)
            for r in self._fetch_all(
                f"SELECT {self._TRADE_COLUMNS} FROM kalshi_public_trades "
                "WHERE market_ticker = ? AND observed_at >= ? AND observed_at <= ? "
                "ORDER BY observed_at, trade_id",
                (market_ticker, start, end),
            )
        ]

    def list_trades_for_market(self, market_ticker: str) -> list[KalshiPublicTrade]:
        return [
            self._to_trade(r)
            for r in self._fetch_all(
                f"SELECT {self._TRADE_COLUMNS} FROM kalshi_public_trades WHERE market_ticker = ? "
                "ORDER BY observed_at, trade_id",
                (market_ticker,),
            )
        ]

    def count_trades(self) -> int:
        return self._count("SELECT COUNT(*) FROM kalshi_public_trades")

    # -- Mapping -------------------------------------------------------------
    def _to_event(self, row: sqlite3.Row) -> KalshiEvent:
        me = row["mutually_exclusive"]
        return KalshiEvent(
            kalshi_event_id=str(row["kalshi_event_id"]),
            event_ticker=str(row["event_ticker"]),
            raw_response_id=str(row["raw_response_id"]),
            first_observed_at=str(row["first_observed_at"]),
            last_observed_at=str(row["last_observed_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            series_ticker=self._opt_str(row, "series_ticker"),
            title=self._opt_str(row, "title"),
            sub_title=self._opt_str(row, "sub_title"),
            category=self._opt_str(row, "category"),
            status=self._opt_str(row, "status"),
            mutually_exclusive=None if me is None else bool(me),
            game_id=self._opt_str(row, "game_id"),
        )

    def _to_market(self, row: sqlite3.Row) -> KalshiMarket:
        return KalshiMarket(
            kalshi_market_id=str(row["kalshi_market_id"]),
            market_ticker=str(row["market_ticker"]),
            raw_response_id=str(row["raw_response_id"]),
            first_observed_at=str(row["first_observed_at"]),
            last_observed_at=str(row["last_observed_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            event_ticker=self._opt_str(row, "event_ticker"),
            kalshi_event_id=self._opt_str(row, "kalshi_event_id"),
            series_ticker=self._opt_str(row, "series_ticker"),
            title=self._opt_str(row, "title"),
            subtitle=self._opt_str(row, "subtitle"),
            yes_sub_title=self._opt_str(row, "yes_sub_title"),
            no_sub_title=self._opt_str(row, "no_sub_title"),
            status=self._opt_str(row, "status"),
            open_time=self._opt_str(row, "open_time"),
            close_time=self._opt_str(row, "close_time"),
            expiration_time=self._opt_str(row, "expiration_time"),
            settlement_time=self._opt_str(row, "settlement_time"),
            result=self._opt_str(row, "result"),
            rules_primary=self._opt_str(row, "rules_primary"),
            rules_secondary=self._opt_str(row, "rules_secondary"),
            rules_hash=self._opt_str(row, "rules_hash"),
            game_id=self._opt_str(row, "game_id"),
        )

    def _to_book(self, row: sqlite3.Row) -> KalshiOrderbookSnapshot:
        return KalshiOrderbookSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            market_ticker=str(row["market_ticker"]),
            observed_at=str(row["observed_at"]),
            ingested_at=str(row["ingested_at"]),
            run_id=str(row["run_id"]),
            raw_response_id=str(row["raw_response_id"]),
            raw_response_hash=str(row["raw_response_hash"]),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
            yes_levels=int(row["yes_levels"]),
            no_levels=int(row["no_levels"]),
            depth_levels=int(row["depth_levels"]),
            kalshi_market_id=self._opt_str(row, "kalshi_market_id"),
            best_yes_bid=self._opt_int(row, "best_yes_bid"),
            best_no_bid=self._opt_int(row, "best_no_bid"),
            derived_yes_ask=self._opt_int(row, "derived_yes_ask"),
            derived_no_ask=self._opt_int(row, "derived_no_ask"),
            provider_timestamp=self._opt_str(row, "provider_timestamp"),
        )

    def _to_level(self, row: sqlite3.Row) -> KalshiOrderbookLevel:
        return KalshiOrderbookLevel(
            level_id=str(row["level_id"]),
            snapshot_id=str(row["snapshot_id"]),
            side=str(row["side"]),
            price=int(row["price"]),
            quantity=int(row["quantity"]),
            level_index=int(row["level_index"]),
            created_at=str(row["created_at"]),
        )

    def _to_trade(self, row: sqlite3.Row) -> KalshiPublicTrade:
        return KalshiPublicTrade(
            trade_id=str(row["trade_id"]),
            market_ticker=str(row["market_ticker"]),
            count=int(row["count"]),
            observed_at=str(row["observed_at"]),
            ingested_at=str(row["ingested_at"]),
            run_id=str(row["run_id"]),
            raw_response_id=str(row["raw_response_id"]),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
            provider_trade_id=self._opt_str(row, "provider_trade_id"),
            kalshi_market_id=self._opt_str(row, "kalshi_market_id"),
            taker_side=self._opt_str(row, "taker_side"),
            yes_price=self._opt_int(row, "yes_price"),
            no_price=self._opt_int(row, "no_price"),
            trade_time=self._opt_str(row, "trade_time"),
            provider_timestamp=self._opt_str(row, "provider_timestamp"),
        )
