"""Kalshi repository: stale-metadata protection, transition-aware order-book
deduplication, trade idempotency, and point-in-time queries.

These drive the repository directly with controlled ``observed_at`` values so
the as-of / backfill / tie-break semantics can be pinned without depending on
the wall clock (which an end-to-end ingest cannot).
"""

from __future__ import annotations

import sqlite3

from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.kalshi import (
    SqliteKalshiRepository,
    UpsertOutcome,
    orderbook_content_hash,
    trade_content_hash,
)
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)

T0 = "2026-07-22T18:00:00.000000Z"
T1 = "2026-07-22T19:00:00.000000Z"
T2 = "2026-07-22T20:00:00.000000Z"


def _raw(conn: sqlite3.Connection) -> tuple[str, str]:
    run = SqliteIngestionRunRepository(conn).start(
        command="ingest-kalshi", provider="kalshi_public", operation="list_markets",
        args_json="{}", started_monotonic_ns=0, tool_version="test",
    )
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run.run_id, provider="kalshi_public", endpoint="/markets", request_params_json="{}",
        http_status=200, response_headers_json="{}", requested_at=T0, received_at=T0, elapsed_ns=1,
        body="{}", content_hash=response_content_hash(
            provider="kalshi_public", endpoint="/markets", request_params={}, body="{}"
        ),
    )
    return run.run_id, raw.raw_response_id


def _repo(conn: sqlite3.Connection) -> tuple[SqliteKalshiRepository, str, str, str]:
    repo = SqliteKalshiRepository(conn)
    run_id, raw_id = _raw(conn)
    # A raw response hash for provenance on snapshots.
    raw_hash = conn.execute(
        "SELECT content_hash FROM raw_responses WHERE raw_response_id = ?", (raw_id,)
    ).fetchone()[0]
    return repo, run_id, raw_id, str(raw_hash)


def _extra_raw(conn: sqlite3.Connection, run_id: str, marker: str) -> tuple[str, str]:
    """A second raw response under the same run, to exercise current provenance."""

    body = '{"m":"%s"}' % marker
    ch = response_content_hash(
        provider="kalshi_public", endpoint="/markets", request_params={}, body=body
    )
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run_id, provider="kalshi_public", endpoint="/markets", request_params_json="{}",
        http_status=200, response_headers_json="{}", requested_at=T0, received_at=T0, elapsed_ns=1,
        body=body, content_hash=ch,
    )
    return raw.raw_response_id, ch


def _append_book(repo, run_id, raw_id, raw_hash, *, yes, no, observed_at, market="MKT-1"):
    ch = orderbook_content_hash(yes_bids=yes, no_bids=no)
    return repo.append_orderbook_snapshot(
        market_ticker=market, yes_bids=yes, no_bids=no, observed_at=observed_at, run_id=run_id,
        raw_response_id=raw_id, raw_response_hash=raw_hash, content_hash=ch,
    )


# --------------------------------------------------------------------------- #
# Events / markets: stale-metadata protection + first/current provenance
# --------------------------------------------------------------------------- #
def test_older_event_backfill_does_not_regress(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    _e, o1 = repo.upsert_event(event_ticker="EV-1", raw_response_id=raw_id,
                               raw_response_hash=raw_hash, observed_at=T2, title="Newest",
                               status="closed")
    assert o1 is UpsertOutcome.INSERTED
    stale_id, stale_hash = _extra_raw(conn, run_id, "stale")
    after, o2 = repo.upsert_event(event_ticker="EV-1", raw_response_id=stale_id,
                                  raw_response_hash=stale_hash, observed_at=T0, title="Stale",
                                  status="open")
    assert o2 is UpsertOutcome.UNCHANGED
    assert after.title == "Newest"
    assert after.status == "closed"
    assert after.last_observed_at == T2
    # Current provenance is NOT moved by a stale backfill.
    assert after.current_raw_response_id == raw_id
    assert after.current_raw_response_hash == raw_hash
    assert repo.count_events() == 1


def test_newer_event_updates_current_provenance(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    created, o1 = repo.upsert_event(event_ticker="EV-1", raw_response_id=raw_id,
                                    raw_response_hash=raw_hash, observed_at=T1, title="First")
    assert o1 is UpsertOutcome.INSERTED
    new_id, new_hash = _extra_raw(conn, run_id, "newer")
    a, o2 = repo.upsert_event(event_ticker="EV-1", raw_response_id=new_id,
                              raw_response_hash=new_hash, observed_at=T2, title="Second")
    assert o2 is UpsertOutcome.UPDATED
    assert a.title == "Second"
    # first_raw_response_id is immutable; current moves to the newer response.
    assert a.first_raw_response_id == created.first_raw_response_id == raw_id
    assert a.current_raw_response_id == new_id
    assert a.current_raw_response_hash == new_hash
    # Equal observed_at retains the earlier-recorded value and does not change.
    eq_id, eq_hash = _extra_raw(conn, run_id, "equal")
    b, o3 = repo.upsert_event(event_ticker="EV-1", raw_response_id=eq_id,
                              raw_response_hash=eq_hash, observed_at=T2, title="Third")
    assert o3 is UpsertOutcome.UNCHANGED
    assert b.title == "Second"
    assert b.current_raw_response_id == new_id


def test_older_market_backfill_does_not_regress(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    repo.upsert_market(market_ticker="MKT-1", raw_response_id=raw_id, raw_response_hash=raw_hash,
                       observed_at=T2, status="closed", title="Newest")
    stale_id, stale_hash = _extra_raw(conn, run_id, "mstale")
    after, outcome = repo.upsert_market(market_ticker="MKT-1", raw_response_id=stale_id,
                                        raw_response_hash=stale_hash, observed_at=T0,
                                        status="open", title="Stale")
    assert outcome is UpsertOutcome.UNCHANGED
    assert after.title == "Newest"
    assert after.status == "closed"
    assert after.last_observed_at == T2
    assert after.current_raw_response_id == raw_id
    assert repo.count_markets() == 1


def test_newer_market_updates_current_provenance(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    repo.upsert_market(market_ticker="MKT-1", raw_response_id=raw_id, raw_response_hash=raw_hash,
                       observed_at=T1, title="First")
    new_id, new_hash = _extra_raw(conn, run_id, "mnew")
    a, outcome = repo.upsert_market(market_ticker="MKT-1", raw_response_id=new_id,
                                    raw_response_hash=new_hash, observed_at=T2, title="Second")
    assert outcome is UpsertOutcome.UPDATED
    assert a.title == "Second"
    assert a.first_raw_response_id == raw_id
    assert a.current_raw_response_id == new_id
    assert a.current_raw_response_hash == new_hash


def test_current_provenance_joins_to_raw_response(conn: sqlite3.Connection) -> None:
    """The current metadata is always traceable to a real raw response."""

    repo, run_id, raw_id, raw_hash = _repo(conn)
    repo.upsert_event(event_ticker="EV-1", raw_response_id=raw_id, raw_response_hash=raw_hash,
                      observed_at=T1, title="First")
    new_id, new_hash = _extra_raw(conn, run_id, "join")
    repo.upsert_event(event_ticker="EV-1", raw_response_id=new_id, raw_response_hash=new_hash,
                      observed_at=T2, title="Second")
    row = conn.execute(
        "SELECT e.current_raw_response_hash, r.content_hash "
        "FROM kalshi_events e JOIN raw_responses r "
        "ON e.current_raw_response_id = r.raw_response_id WHERE e.event_ticker = 'EV-1'"
    ).fetchone()
    assert row is not None
    assert row[0] == row[1] == new_hash
    # No dangling current pointer anywhere.
    dangling = conn.execute(
        "SELECT COUNT(*) FROM kalshi_events e LEFT JOIN raw_responses r "
        "ON e.current_raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
    ).fetchone()[0]
    assert dangling == 0


# --------------------------------------------------------------------------- #
# Order books: transition-aware dedup
# --------------------------------------------------------------------------- #
def test_unchanged_consecutive_book_collapses(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    _s, first = _append_book(repo, run_id, raw_id, raw_hash, yes=[[42, 5]], no=[[55, 3]], observed_at=T0)
    _s2, second = _append_book(repo, run_id, raw_id, raw_hash, yes=[[42, 5]], no=[[55, 3]], observed_at=T1)
    assert first is True
    assert second is False
    assert repo.count_orderbook_snapshots() == 1


def test_changed_book_appends(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    _append_book(repo, run_id, raw_id, raw_hash, yes=[[42, 5]], no=[[55, 3]], observed_at=T0)
    _s, inserted = _append_book(repo, run_id, raw_id, raw_hash, yes=[[43, 5]], no=[[55, 3]], observed_at=T1)
    assert inserted is True
    assert repo.count_orderbook_snapshots() == 2


def test_book_reversal_preserved(conn: sqlite3.Connection) -> None:
    """A -> B -> A order-book sequence keeps all three states."""

    repo, run_id, raw_id, raw_hash = _repo(conn)
    a = [[42, 5]]
    b = [[43, 5]]
    assert _append_book(repo, run_id, raw_id, raw_hash, yes=a, no=[[55, 3]], observed_at=T0)[1]
    assert _append_book(repo, run_id, raw_id, raw_hash, yes=b, no=[[55, 3]], observed_at=T1)[1]
    assert _append_book(repo, run_id, raw_id, raw_hash, yes=a, no=[[55, 3]], observed_at=T2)[1]
    assert repo.count_orderbook_snapshots() == 3


def test_exact_book_replay_is_idempotent(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    _append_book(repo, run_id, raw_id, raw_hash, yes=[[42, 5]], no=[[55, 3]], observed_at=T0)
    _s, again = _append_book(repo, run_id, raw_id, raw_hash, yes=[[42, 5]], no=[[55, 3]], observed_at=T0)
    assert again is False
    assert repo.count_orderbook_snapshots() == 1


def test_older_book_backfill_preserved_and_idempotent(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    _append_book(repo, run_id, raw_id, raw_hash, yes=[[43, 5]], no=[[55, 3]], observed_at=T2)
    # Older, genuinely-different book backfilled.
    _s, inserted = _append_book(repo, run_id, raw_id, raw_hash, yes=[[41, 9]], no=[[55, 3]], observed_at=T0)
    assert inserted is True
    assert repo.count_orderbook_snapshots() == 2
    # Repeating the same backfill does nothing.
    _s2, again = _append_book(repo, run_id, raw_id, raw_hash, yes=[[41, 9]], no=[[55, 3]], observed_at=T0)
    assert again is False
    assert repo.count_orderbook_snapshots() == 2


def test_orderbook_as_of_and_latest(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    _append_book(repo, run_id, raw_id, raw_hash, yes=[[42, 5]], no=[[55, 3]], observed_at=T0)
    _append_book(repo, run_id, raw_id, raw_hash, yes=[[43, 5]], no=[[54, 3]], observed_at=T2)

    at_t1 = repo.orderbook_as_of("MKT-1", T1)
    assert at_t1 is not None and at_t1.best_yes_bid == 42
    at_t2 = repo.orderbook_as_of("MKT-1", T2)
    assert at_t2 is not None and at_t2.best_yes_bid == 43
    assert repo.orderbook_as_of("MKT-1", "2026-07-22T17:00:00.000000Z") is None

    latest = repo.latest_orderbook("MKT-1")
    assert latest is not None and latest.best_yes_bid == 43
    # Derived asks stored on the row.
    assert latest.derived_yes_ask == 100 - 54
    assert latest.derived_no_ask == 100 - 43


def test_orderbook_levels_full_ladder(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    snap, _ = _append_book(
        repo, run_id, raw_id, raw_hash, yes=[[42, 100], [40, 50]], no=[[55, 30], [50, 10]],
        observed_at=T0,
    )
    assert snap is not None
    levels = repo.orderbook_levels(snap.snapshot_id)
    yes = [(lv.price, lv.quantity) for lv in levels if lv.side == "yes"]
    no = [(lv.price, lv.quantity) for lv in levels if lv.side == "no"]
    assert yes == [(42, 100), (40, 50)]
    assert no == [(55, 30), (50, 10)]
    assert repo.count_orderbook_levels() == 4


def test_equal_observed_at_book_tie_break_is_deterministic(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, raw_hash = _repo(conn)
    _append_book(repo, run_id, raw_id, raw_hash, yes=[[42, 5]], no=[[55, 3]], observed_at=T1)
    # Different book at the SAME observed_at -> distinct content, both stored.
    _append_book(repo, run_id, raw_id, raw_hash, yes=[[43, 5]], no=[[55, 3]], observed_at=T1)
    assert repo.count_orderbook_snapshots() == 2
    a = repo.orderbook_as_of("MKT-1", T1)
    b = repo.orderbook_as_of("MKT-1", T1)
    assert a is not None and b is not None and a.snapshot_id == b.snapshot_id


# --------------------------------------------------------------------------- #
# Trades: idempotency + range query
# --------------------------------------------------------------------------- #
def _append_trade(repo, run_id, raw_id, *, provider_trade_id, observed_at, yes_price=42,
                  count=5, market="MKT-1", trade_time=None):
    ch = trade_content_hash(
        provider_trade_id=provider_trade_id, market_ticker=market, trade_time=trade_time,
        yes_price=yes_price, no_price=None, count=count, taker_side="yes",
    )
    return repo.append_trade(
        market_ticker=market, count=count, observed_at=observed_at, run_id=run_id,
        raw_response_id=raw_id, content_hash=ch, provider_trade_id=provider_trade_id,
        yes_price=yes_price, taker_side="yes", trade_time=trade_time,
    )


def test_trade_idempotent_on_provider_id(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, _h = _repo(conn)
    _t, first = _append_trade(repo, run_id, raw_id, provider_trade_id="X1", observed_at=T0)
    _t2, second = _append_trade(repo, run_id, raw_id, provider_trade_id="X1", observed_at=T1)
    assert first is True
    assert second is False  # same trade id -> idempotent even at a later observed_at
    assert repo.count_trades() == 1


def test_distinct_trades_coexist(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, _h = _repo(conn)
    _append_trade(repo, run_id, raw_id, provider_trade_id="X1", observed_at=T0, trade_time=T0)
    _append_trade(repo, run_id, raw_id, provider_trade_id="X2", observed_at=T0, trade_time=T1)
    assert repo.count_trades() == 2


def test_trades_in_range(conn: sqlite3.Connection) -> None:
    repo, run_id, raw_id, _h = _repo(conn)
    _append_trade(repo, run_id, raw_id, provider_trade_id="X1", observed_at=T0)
    _append_trade(repo, run_id, raw_id, provider_trade_id="X2", observed_at=T1)
    _append_trade(repo, run_id, raw_id, provider_trade_id="X3", observed_at=T2)
    window = repo.trades_in_range("MKT-1", start=T0, end=T1)
    assert {t.provider_trade_id for t in window} == {"X1", "X2"}
