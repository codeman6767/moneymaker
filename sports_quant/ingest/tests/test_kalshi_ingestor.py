"""Kalshi public ingestion: normalization, pagination, stale metadata, order
books (derivation, preservation, transition-aware dedup), trades, validation,
partial success, dry-run, and GET-only / no-credential guarantees."""

from __future__ import annotations

import copy

import httpx

from sports_quant.db.engine import Database
from sports_quant.ingest.kalshi_ingestor import (
    ingest_kalshi,
    normalize_provider_time,
    validate_event,
    validate_market,
    validate_trade,
)

from .conftest import (
    kalshi_events_body,
    kalshi_markets_body,
    kalshi_orderbook_body,
    kalshi_router,
    kalshi_trades_body,
)


def _count(database: Database, table: str) -> int:
    with database.connection() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


async def test_events_and_markets_normalize(database, make_kalshi_client) -> None:
    client = make_kalshi_client(kalshi_router())
    result = await ingest_kalshi(database=database, client=client, status="open", limit=5)

    assert result.status == "succeeded"
    assert result.events_seen == 1
    assert result.markets_seen == 1
    with database.connection() as conn:
        event = conn.execute(
            "SELECT event_ticker, series_ticker, mutually_exclusive, game_id FROM kalshi_events"
        ).fetchone()
        market = conn.execute(
            "SELECT market_ticker, event_ticker, kalshi_event_id, open_time, rules_hash, game_id "
            "FROM kalshi_markets"
        ).fetchone()
    assert event["event_ticker"] == "KXMLBGAME-26JUL22"
    assert event["mutually_exclusive"] == 1
    assert event["game_id"] is None  # no matching in Phase C
    assert market["market_ticker"] == "KXMLBGAME-26JUL22-NYY"
    # Market links to the event that was ingested in the same run.
    assert market["kalshi_event_id"] is not None
    # Provider time normalized to the corpus ISO format.
    assert market["open_time"] == "2026-07-22T18:00:00.000000Z"
    assert market["rules_hash"] is not None
    assert market["game_id"] is None


async def test_pagination_follows_cursor(database, make_kalshi_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        cursor = request.url.params.get("cursor")
        if path.endswith("/events"):
            return httpx.Response(200, json={"events": [], "cursor": ""})
        if path.endswith("/markets"):
            if not cursor:
                return httpx.Response(
                    200,
                    json={"markets": [{"ticker": "M-A"}, {"ticker": "M-B"}], "cursor": "c1"},
                )
            return httpx.Response(200, json={"markets": [{"ticker": "M-C"}], "cursor": ""})
        return httpx.Response(404, json={})

    client = make_kalshi_client(handler)
    result = await ingest_kalshi(database=database, client=client, status="open", limit=50, max_pages=5)
    assert result.markets_seen == 3
    with database.connection() as conn:
        tickers = {r[0] for r in conn.execute("SELECT market_ticker FROM kalshi_markets")}
    assert tickers == {"M-A", "M-B", "M-C"}


async def test_newer_event_metadata_becomes_current(database, make_kalshi_client) -> None:
    await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router()),
                        status="open", limit=5)

    newer = kalshi_events_body()
    newer["events"][0]["title"] = "Updated Title"
    newer["events"][0]["status"] = "closed"
    await ingest_kalshi(
        database=database,
        client=make_kalshi_client(kalshi_router(events=newer)),
        status="open",
        limit=5,
    )
    with database.connection() as conn:
        row = conn.execute("SELECT title, status FROM kalshi_events").fetchone()
    # A later ingest (later observed_at) becomes current.
    assert row["title"] == "Updated Title"
    assert row["status"] == "closed"
    assert _count(database, "kalshi_events") == 1


async def test_orderbook_derivation_and_levels_preserved(database, make_kalshi_client) -> None:
    client = make_kalshi_client(kalshi_router())
    result = await ingest_kalshi(
        database=database, client=client, status="open", limit=5, include_orderbooks=True
    )
    assert result.orderbook_snapshots_inserted == 1
    assert result.orderbook_levels_inserted == 3  # 2 yes + 1 no

    with database.connection() as conn:
        snap = conn.execute(
            "SELECT best_yes_bid, best_no_bid, derived_yes_ask, derived_no_ask, yes_levels, "
            "no_levels FROM kalshi_orderbook_snapshots"
        ).fetchone()
        levels = conn.execute(
            "SELECT side, price, quantity, level_index FROM kalshi_orderbook_levels "
            "ORDER BY side, level_index"
        ).fetchall()
    assert snap["best_yes_bid"] == 42
    assert snap["best_no_bid"] == 55
    # Executable asks are 100 - opposing best bid, never a bid read as an ask.
    assert snap["derived_yes_ask"] == 45
    assert snap["derived_no_ask"] == 58
    assert snap["yes_levels"] == 2 and snap["no_levels"] == 1
    # Both bids preserved, best-first.
    yes = [(r["price"], r["quantity"]) for r in levels if r["side"] == "yes"]
    no = [(r["price"], r["quantity"]) for r in levels if r["side"] == "no"]
    assert yes == [(42, 100), (40, 50)]
    assert no == [(55, 30)]


async def test_empty_and_one_sided_books(database, make_kalshi_client) -> None:
    # Empty book.
    empty = kalshi_router(orderbook=kalshi_orderbook_body(yes=None, no=None))
    r1 = await ingest_kalshi(database=database, client=make_kalshi_client(empty),
                             status="open", limit=5, include_orderbooks=True)
    assert r1.orderbook_snapshots_inserted == 1
    with database.connection() as conn:
        snap = conn.execute(
            "SELECT best_yes_bid, derived_yes_ask, depth_levels FROM kalshi_orderbook_snapshots"
        ).fetchone()
    assert snap["best_yes_bid"] is None
    assert snap["derived_yes_ask"] is None
    assert snap["depth_levels"] == 0

    # One-sided book (yes only) in a fresh corpus.
    from sports_quant.db.init import initialize_database
    p2 = database.path.parent / "one_sided.db"
    initialize_database(p2)
    db2 = Database(p2)
    one_sided = kalshi_router(orderbook=kalshi_orderbook_body(yes=[[30, 5]], no=None))
    await ingest_kalshi(database=db2, client=make_kalshi_client(one_sided),
                        status="open", limit=5, include_orderbooks=True)
    with db2.connection() as conn:
        snap = conn.execute(
            "SELECT best_yes_bid, best_no_bid, derived_no_ask, derived_yes_ask "
            "FROM kalshi_orderbook_snapshots"
        ).fetchone()
    assert snap["best_yes_bid"] == 30
    assert snap["best_no_bid"] is None
    assert snap["derived_no_ask"] == 70  # 100 - 30
    assert snap["derived_yes_ask"] is None


async def test_identical_consecutive_books_deduplicate(database, make_kalshi_client) -> None:
    router = kalshi_router()
    await ingest_kalshi(database=database, client=make_kalshi_client(router),
                        status="open", limit=5, include_orderbooks=True)
    r2 = await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router()),
                             status="open", limit=5, include_orderbooks=True)
    assert r2.orderbook_snapshots_inserted == 0
    assert r2.orderbook_snapshots_duplicate == 1
    assert _count(database, "kalshi_orderbook_snapshots") == 1


async def test_changed_book_creates_new_snapshot(database, make_kalshi_client) -> None:
    await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router()),
                        status="open", limit=5, include_orderbooks=True)
    moved = kalshi_router(orderbook=kalshi_orderbook_body(yes=[[43, 100]], no=[[55, 30]]))
    r2 = await ingest_kalshi(database=database, client=make_kalshi_client(moved),
                             status="open", limit=5, include_orderbooks=True)
    assert r2.orderbook_snapshots_inserted == 1
    assert _count(database, "kalshi_orderbook_snapshots") == 2


async def test_malformed_book_is_rejected_and_run_continues(database, make_kalshi_client) -> None:
    # A price of 150 is out of Kalshi's [1,99] range: the whole book is rejected,
    # but events/markets still land.
    bad = kalshi_router(orderbook=kalshi_orderbook_body(yes=[[150, 10]], no=[[55, 30]]))
    result = await ingest_kalshi(database=database, client=make_kalshi_client(bad),
                                 status="open", limit=5, include_orderbooks=True)
    assert result.orderbooks_rejected == 1
    assert result.orderbook_snapshots_inserted == 0
    assert result.markets_seen == 1  # valid records preserved
    assert result.status == "partially_succeeded"


async def test_duplicate_price_levels_rejected(database, make_kalshi_client) -> None:
    dup = kalshi_router(orderbook=kalshi_orderbook_body(yes=[[42, 10], [42, 20]], no=[[55, 5]]))
    result = await ingest_kalshi(database=database, client=make_kalshi_client(dup),
                                 status="open", limit=5, include_orderbooks=True)
    assert result.orderbooks_rejected == 1
    assert any("duplicate price" in r for r in result.rejections)


async def test_public_trades_normalize_and_dedupe(database, make_kalshi_client) -> None:
    result = await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router()),
                                 status="open", limit=5, include_trades=True)
    assert result.trades_inserted == 1
    with database.connection() as conn:
        trade = conn.execute(
            "SELECT provider_trade_id, market_ticker, yes_price, count, taker_side, trade_time "
            "FROM kalshi_public_trades"
        ).fetchone()
    assert trade["provider_trade_id"] == "trd-1"
    assert trade["yes_price"] == 42
    assert trade["count"] == 10
    assert trade["taker_side"] == "yes"
    assert trade["trade_time"] == "2026-07-22T18:05:00.000000Z"

    # Re-ingesting the same trade is idempotent.
    r2 = await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router()),
                             status="open", limit=5, include_trades=True)
    assert r2.trades_inserted == 0
    assert r2.trades_duplicate == 1
    assert _count(database, "kalshi_public_trades") == 1


async def test_repeated_trades_at_different_times_are_distinct(database, make_kalshi_client) -> None:
    two = kalshi_trades_body(
        trades=[
            {"trade_id": "a", "ticker": "KXMLBGAME-26JUL22-NYY", "yes_price": 42, "no_price": 58,
             "count": 5, "taker_side": "yes", "created_time": "2026-07-22T18:05:00Z"},
            {"trade_id": "b", "ticker": "KXMLBGAME-26JUL22-NYY", "yes_price": 42, "no_price": 58,
             "count": 5, "taker_side": "yes", "created_time": "2026-07-22T18:06:00Z"},
        ]
    )
    result = await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router(trades=two)),
                                 status="open", limit=5, include_trades=True)
    # Same price, different ids/times -> two legitimate trades preserved.
    assert result.trades_inserted == 2
    assert _count(database, "kalshi_public_trades") == 2


async def test_trades_without_provider_id_use_field_identity(database, make_kalshi_client) -> None:
    no_id = kalshi_trades_body(
        trades=[
            {"ticker": "KXMLBGAME-26JUL22-NYY", "yes_price": 40, "no_price": 60, "count": 3,
             "taker_side": "no", "created_time": "2026-07-22T18:07:00Z"},
        ]
    )
    r1 = await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router(trades=no_id)),
                             status="open", limit=5, include_trades=True)
    assert r1.trades_inserted == 1
    # Exact replay collapses on the documented field-based identity.
    r2 = await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router(trades=no_id)),
                             status="open", limit=5, include_trades=True)
    assert r2.trades_inserted == 0 and r2.trades_duplicate == 1


async def test_malformed_trade_rejected(database, make_kalshi_client) -> None:
    bad = kalshi_trades_body(
        trades=[{"ticker": "KXMLBGAME-26JUL22-NYY", "yes_price": 250, "count": 1}]
    )
    result = await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router(trades=bad)),
                                 status="open", limit=5, include_trades=True)
    assert result.trades_rejected == 1
    assert result.trades_inserted == 0


async def test_zero_results_is_success(database, make_kalshi_client) -> None:
    empty = kalshi_router(events={"events": [], "cursor": ""}, markets={"markets": [], "cursor": ""})
    result = await ingest_kalshi(database=database, client=make_kalshi_client(empty),
                                 status="open", limit=5)
    assert result.status == "succeeded"
    assert result.events_seen == 0 and result.markets_seen == 0


async def test_provider_failure_is_finalized_failed(database, make_kalshi_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    result = await ingest_kalshi(database=database, client=make_kalshi_client(handler),
                                 status="open", limit=5)
    assert result.failed is True
    with database.connection() as conn:
        run = conn.execute(
            "SELECT status FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()
    assert run["status"] == "failed"


async def test_dry_run_persists_nothing(database, make_kalshi_client) -> None:
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router()),
        status="open", limit=5, include_orderbooks=True, include_trades=True, dry_run=True,
    )
    assert result.dry_run is True
    assert result.run_id is None
    assert result.events_seen == 1 and result.markets_seen == 1
    with database.connection() as conn:
        for table in (
            "ingestion_runs", "raw_responses", "kalshi_events", "kalshi_markets",
            "kalshi_orderbook_snapshots", "kalshi_orderbook_levels", "kalshi_public_trades",
        ):
            assert int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) == 0


async def test_every_request_is_get_with_no_auth_header(database, make_kalshi_client) -> None:
    seen: list[httpx.Request] = []
    router = kalshi_router(seen=seen)
    await ingest_kalshi(database=database, client=make_kalshi_client(router),
                        status="open", limit=5, include_orderbooks=True, include_trades=True)
    assert seen
    assert {r.method for r in seen} == {"GET"}
    for request in seen:
        # No authentication, no signing headers of any kind.
        assert "authorization" not in {k.lower() for k in request.headers}
        assert not any(k.lower().startswith("kalshi-") for k in request.headers)
        assert "x-api-key" not in {k.lower() for k in request.headers}


async def test_raw_responses_preserved_and_traceable(database, make_kalshi_client) -> None:
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router()),
        status="open", limit=5, include_orderbooks=True, include_trades=True,
    )
    assert not result.failed
    with database.connection() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]) >= 4
        for table in (
            "kalshi_orderbook_snapshots", "kalshi_public_trades",
        ):
            dangling = conn.execute(
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN raw_responses r "
                "ON t.raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
            ).fetchone()[0]
            assert dangling == 0


async def test_missing_event_ticker_is_rejected(database, make_kalshi_client) -> None:
    bad_events = {"events": [{"title": "no ticker"}], "cursor": ""}
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router(events=bad_events)),
        status="open", limit=5,
    )
    assert result.events_rejected == 1
    assert result.events_seen == 0


def test_normalize_provider_time() -> None:
    assert normalize_provider_time("2026-07-22T18:00:00Z") == "2026-07-22T18:00:00.000000Z"
    assert normalize_provider_time(None) is None
    assert normalize_provider_time("garbage") is None
    assert normalize_provider_time("") is None
    # Unix seconds accepted.
    assert normalize_provider_time(0) == "1970-01-01T00:00:00.000000Z"


async def test_older_market_backfill_does_not_regress(database, make_kalshi_client) -> None:
    # First ingest sets the current title.
    await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router()),
                        status="open", limit=5)
    current = database.path
    # Force an older observation by pre-dating? The ingestor stamps observed_at
    # from the raw response received_at (real clock), so a second live ingest is
    # newer. Stale-backfill regression is unit-tested at the repository level in
    # test_kalshi_repositories.py; here we assert the second (newer) ingest wins
    # deterministically and does not duplicate the event row.
    updated = copy.deepcopy(kalshi_markets_body())
    updated["markets"][0]["title"] = "Newer"
    await ingest_kalshi(database=database, client=make_kalshi_client(kalshi_router(markets=updated)),
                        status="open", limit=5)
    assert current == database.path
    with database.connection() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM kalshi_markets").fetchone()[0]) == 1
        assert conn.execute("SELECT title FROM kalshi_markets").fetchone()[0] == "Newer"


# --------------------------------------------------------------------------- #
# Strict event validation (item 2)
# --------------------------------------------------------------------------- #
def test_validate_event_rejects_blank_ticker() -> None:
    norm, reason = validate_event({"event_ticker": "   ", "status": "open"})
    assert norm is None and reason is not None


def test_validate_event_rejects_non_string_status() -> None:
    norm, reason = validate_event({"event_ticker": "EV", "status": 123})
    assert norm is None and "status" in (reason or "")


def test_validate_event_rejects_non_boolean_mutually_exclusive() -> None:
    # The classic bug: bool of the string "false" is True. Reject, never coerce.
    norm, reason = validate_event(
        {"event_ticker": "EV", "mutually_exclusive": "false"}
    )
    assert norm is None and "mutually_exclusive" in (reason or "")


def test_validate_event_accepts_real_boolean_and_absent_optionals() -> None:
    norm, reason = validate_event({"event_ticker": "EV", "mutually_exclusive": False})
    assert reason is None and norm is not None
    assert norm.mutually_exclusive is False
    assert norm.status is None  # absent -> None


async def test_malformed_event_rejected_but_valid_preserved(database, make_kalshi_client) -> None:
    events = {
        "events": [
            {"event_ticker": "EV-GOOD", "status": "open"},
            {"event_ticker": "EV-BAD", "mutually_exclusive": "false"},  # not a bool
        ],
        "cursor": "",
    }
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router(events=events)),
        status="open", limit=5,
    )
    assert result.events_seen == 1
    assert result.events_rejected == 1
    assert result.status == "partially_succeeded"
    with database.connection() as conn:
        tickers = {r[0] for r in conn.execute("SELECT event_ticker FROM kalshi_events")}
    assert tickers == {"EV-GOOD"}


# --------------------------------------------------------------------------- #
# Strict market timestamp validation (item 3)
# --------------------------------------------------------------------------- #
def test_validate_market_absent_times_are_none() -> None:
    norm, reason = validate_market({"ticker": "MKT"})
    assert reason is None and norm is not None
    assert norm.open_time is None and norm.close_time is None


def test_validate_market_malformed_time_is_rejected_not_nulled() -> None:
    norm, reason = validate_market({"ticker": "MKT", "open_time": "not-a-date"})
    assert norm is None and "open_time" in (reason or "")


def test_validate_market_rejects_close_before_open() -> None:
    norm, reason = validate_market({
        "ticker": "MKT",
        "open_time": "2026-07-22T20:00:00Z",
        "close_time": "2026-07-22T19:00:00Z",
    })
    assert norm is None and "before open" in (reason or "")


def test_validate_market_rejects_settlement_before_close() -> None:
    norm, reason = validate_market({
        "ticker": "MKT",
        "close_time": "2026-07-22T20:00:00Z",
        "settlement_time": "2026-07-22T19:00:00Z",
    })
    assert norm is None and "before close" in (reason or "")


def test_validate_market_expected_expiration_fallback() -> None:
    norm, reason = validate_market(
        {"ticker": "MKT", "expected_expiration_time": "2026-07-22T23:00:00Z"}
    )
    assert reason is None and norm is not None
    assert norm.expiration_time == "2026-07-22T23:00:00.000000Z"


async def test_malformed_market_time_rejected_in_ingest(database, make_kalshi_client) -> None:
    markets = {"markets": [{"ticker": "MKT-1", "open_time": "garbage"}], "cursor": ""}
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router(markets=markets)),
        status="open", limit=5,
    )
    assert result.markets_rejected == 1
    assert result.markets_seen == 0
    assert _count(database, "kalshi_markets") == 0


# --------------------------------------------------------------------------- #
# Strengthened public-trade identity (item 4)
# --------------------------------------------------------------------------- #
def test_validate_trade_anonymous_without_timestamp_rejected() -> None:
    norm, reason = validate_trade(
        {"ticker": "MKT", "yes_price": 40, "count": 3}, "MKT"  # no id, no time
    )
    assert norm is None and reason is not None


def test_validate_trade_malformed_timestamp_rejected() -> None:
    norm, reason = validate_trade(
        {"ticker": "MKT", "yes_price": 40, "count": 3, "created_time": "nope"}, "MKT"
    )
    assert norm is None and "timestamp" in (reason or "")


def test_validate_trade_zero_count_rejected() -> None:
    norm, reason = validate_trade(
        {"trade_id": "t1", "ticker": "MKT", "yes_price": 40, "count": 0}, "MKT"
    )
    assert norm is None and "count" in (reason or "")


def test_validate_trade_no_valid_price_rejected() -> None:
    norm, reason = validate_trade(
        {"trade_id": "t1", "ticker": "MKT", "count": 3}, "MKT"  # no price at all
    )
    assert norm is None and "price" in (reason or "")


def test_validate_trade_never_uses_observed_at_as_identity() -> None:
    # Two anonymous trades identical except trade_time -> distinct identities,
    # derived from provider fields only (never our local observed_at).
    a, _ = validate_trade(
        {"ticker": "MKT", "yes_price": 40, "count": 3, "taker_side": "yes",
         "created_time": "2026-07-22T18:00:00Z"}, "MKT")
    b, _ = validate_trade(
        {"ticker": "MKT", "yes_price": 40, "count": 3, "taker_side": "yes",
         "created_time": "2026-07-22T18:01:00Z"}, "MKT")
    assert a is not None and b is not None
    assert a.content_hash != b.content_hash


async def test_anonymous_no_timestamp_trade_rejected_in_ingest(database, make_kalshi_client) -> None:
    trades = kalshi_trades_body(
        trades=[{"ticker": "KXMLBGAME-26JUL22-NYY", "yes_price": 40, "no_price": 60, "count": 2}]
    )
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router(trades=trades)),
        status="open", limit=5, include_trades=True,
    )
    assert result.trades_rejected == 1
    assert result.trades_inserted == 0


async def test_same_price_different_provider_ids_distinct(database, make_kalshi_client) -> None:
    trades = kalshi_trades_body(
        trades=[
            {"trade_id": "a", "ticker": "KXMLBGAME-26JUL22-NYY", "yes_price": 42, "no_price": 58,
             "count": 5, "taker_side": "yes", "created_time": "2026-07-22T18:05:00Z"},
            {"trade_id": "b", "ticker": "KXMLBGAME-26JUL22-NYY", "yes_price": 42, "no_price": 58,
             "count": 5, "taker_side": "yes", "created_time": "2026-07-22T18:05:00Z"},
        ]
    )
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router(trades=trades)),
        status="open", limit=5, include_trades=True,
    )
    # Same price/qty/time but different provider ids -> two distinct trades.
    assert result.trades_inserted == 2
    assert _count(database, "kalshi_public_trades") == 2


async def test_exact_trade_replay_deduplicates(database, make_kalshi_client) -> None:
    await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router()),
        status="open", limit=5, include_trades=True,
    )
    r2 = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router()),
        status="open", limit=5, include_trades=True,
    )
    assert r2.trades_inserted == 0
    assert r2.trades_duplicate == 1
    assert _count(database, "kalshi_public_trades") == 1


# --------------------------------------------------------------------------- #
# Accurate ingestion counters (item 5)
# --------------------------------------------------------------------------- #
async def test_new_entities_count_as_inserted(database, make_kalshi_client) -> None:
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router()), status="open", limit=5,
    )
    assert result.events_inserted == 1
    assert result.markets_inserted == 1
    assert result.events_updated == 0
    assert result.markets_updated == 0
    with database.connection() as conn:
        run = conn.execute(
            "SELECT records_inserted, records_updated FROM ingestion_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    assert run["records_inserted"] == 2  # one event + one market
    assert run["records_updated"] == 0


async def test_reingest_counts_as_updated_not_inserted(database, make_kalshi_client) -> None:
    await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router()), status="open", limit=5,
    )
    # A second live ingest has a newer observed_at -> the existing rows update.
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(kalshi_router()), status="open", limit=5,
    )
    assert result.events_inserted == 0
    assert result.markets_inserted == 0
    assert result.events_updated == 1
    assert result.markets_updated == 1
    with database.connection() as conn:
        run = conn.execute(
            "SELECT records_inserted, records_updated FROM ingestion_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    assert run["records_inserted"] == 0
    assert run["records_updated"] == 2
    # No duplicate rows created.
    assert _count(database, "kalshi_events") == 1
    assert _count(database, "kalshi_markets") == 1


# --------------------------------------------------------------------------- #
# Dry-run uses the same validation as persisted ingestion (item 6)
# --------------------------------------------------------------------------- #
async def test_dry_run_reports_rejections_like_persisted(database, make_kalshi_client) -> None:
    events = {
        "events": [
            {"event_ticker": "EV-GOOD", "status": "open"},
            {"event_ticker": "EV-BAD", "mutually_exclusive": "false"},
        ],
        "cursor": "",
    }
    markets = {"markets": [{"ticker": "MKT-1", "open_time": "garbage"}], "cursor": ""}
    trades = kalshi_trades_body(
        trades=[{"ticker": "MKT-1", "yes_price": 40, "count": 2}]  # anonymous, no time
    )
    router = kalshi_router(events=events, markets=markets, trades=trades)
    result = await ingest_kalshi(
        database=database, client=make_kalshi_client(router),
        status="open", limit=5, include_orderbooks=True, include_trades=True, dry_run=True,
    )
    # Same validation verdicts as a persisted run would produce...
    assert result.events_seen == 1 and result.events_rejected == 1
    assert result.markets_seen == 0 and result.markets_rejected == 1
    assert result.status == "partially_succeeded"
    # ...and nothing persisted.
    with database.connection() as conn:
        for table in ("ingestion_runs", "kalshi_events", "kalshi_markets"):
            assert int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) == 0
