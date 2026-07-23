"""Kalshi public-data ingestion service.

Fetches Kalshi's **public, unauthenticated** GET surface through the existing
``KalshiClient`` (no key, no private key, no signing), preserves each raw
response before parsing, then writes events, markets, append-only order-book
snapshots + ladder levels, and append-only public trades through the typed
repository, recording one ingestion run.

Guarantees:

* **GET-only, public data.** The transport policy is the only network path; no
  account/order/fill surface is imported or reachable.
* **Persist-before-parse.** Every page's bytes are committed before any
  normalized row is derived, so a parse failure never loses the response.
* **Idempotent.** Re-running writes zero new order-book snapshots when the book
  is unchanged, and zero new trades for already-seen trade identities.
* **Partial data is safe.** A malformed event/market/level/trade is rejected and
  counted, never fabricated, and never aborts the run.
* **No matching.** ``game_id`` is never attached in Phase C.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.models import RawResponse
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.kalshi import (
    SqliteKalshiRepository,
    orderbook_content_hash,
    trade_content_hash,
)
from ..db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from ..db.schema import (
    KALSHI_PRICE_MAX,
    KALSHI_PRICE_MIN,
    KALSHI_PUBLIC_PROVIDER,
    to_iso,
)
from ..providers.kalshi import KalshiClient, KalshiOrderBook
from ..providers.raw_exchange import RawExchange
from .runner import sanitize_error

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "ingest-kalshi"
#: A safe finite default: never sweep the whole exchange, and never fan every
#: order book out by default.
DEFAULT_LIMIT = 20
_MAX_REPORTED_REJECTIONS = 20


# --------------------------------------------------------------------------- #
# Provider-timestamp normalization
# --------------------------------------------------------------------------- #
def normalize_provider_time(value: Any) -> Optional[str]:
    """Normalize a provider timestamp to the corpus ISO format, or ``None``.

    Accepts an RFC3339 string or a unix-seconds number. An unparseable value
    returns ``None`` (recorded as a quality issue by the caller) rather than
    fabricating a time -- the verbatim original is always preserved in the raw
    response regardless.
    """

    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never a timestamp
        return None
    if isinstance(value, (int, float)):
        try:
            return to_iso(datetime.fromtimestamp(float(value), tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return to_iso(parsed)
    return None


def _valid_price(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (
        KALSHI_PRICE_MIN <= value <= KALSHI_PRICE_MAX
    )


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass
class KalshiIngestResult:
    """Sanitized outcome of one ``ingest-kalshi`` run, safe to print."""

    dry_run: bool
    status: str
    run_id: Optional[str] = None
    requests_made: int = 0
    events_seen: int = 0
    events_rejected: int = 0
    markets_seen: int = 0
    markets_rejected: int = 0
    orderbook_snapshots_inserted: int = 0
    orderbook_snapshots_duplicate: int = 0
    orderbook_levels_inserted: int = 0
    orderbooks_rejected: int = 0
    trades_inserted: int = 0
    trades_duplicate: int = 0
    trades_rejected: int = 0
    orderbooks_truncated_at: Optional[int] = None
    records_received: int = 0
    records_normalized: int = 0
    records_inserted: int = 0
    records_deduplicated: int = 0
    records_rejected: int = 0
    rejections: list[str] = field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def note(self, reason: str) -> None:
        if len(self.rejections) < _MAX_REPORTED_REJECTIONS:
            self.rejections.append(reason)


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
async def ingest_kalshi(
    *,
    database: Database,
    client: KalshiClient,
    status: Optional[str] = "open",
    event_ticker: Optional[str] = None,
    market_ticker: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    include_orderbooks: bool = False,
    include_trades: bool = False,
    max_pages: int = 1,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> KalshiIngestResult:
    """Ingest Kalshi public events, markets, and optionally books and trades.

    ``--dry-run`` performs the external GETs and normalization but writes
    nothing to the database -- not the run, not the raw response, not a single
    normalized row (documented in ``DATA_FOUNDATION_PLAN.md``).
    """

    args_json = canonical_json(
        {
            "status": status,
            "event_ticker": event_ticker,
            "market_ticker": market_ticker,
            "limit": limit,
            "include_orderbooks": include_orderbooks,
            "include_trades": include_trades,
            "max_pages": max_pages,
            "dry_run": dry_run,
        }
    )

    if dry_run:
        return await _ingest_dry_run(
            client=client, status=status, event_ticker=event_ticker,
            market_ticker=market_ticker, limit=limit, include_orderbooks=include_orderbooks,
            include_trades=include_trades, max_pages=max_pages,
        )

    return await _ingest_persisting(
        database=database, client=client, status=status, event_ticker=event_ticker,
        market_ticker=market_ticker, limit=limit, include_orderbooks=include_orderbooks,
        include_trades=include_trades, max_pages=max_pages, args_json=args_json,
        tool_version=tool_version,
    )


@dataclass
class _Ctx:
    """Shared per-run wiring for the ingest helpers."""

    conn: Any
    kalshi: SqliteKalshiRepository
    raw_repo: SqliteRawResponseRepository
    run_id: str
    result: KalshiIngestResult


def _store_raw(ctx: _Ctx, exchange: RawExchange) -> RawResponse:
    content_hash = response_content_hash(
        provider=KALSHI_PUBLIC_PROVIDER,
        endpoint=exchange.endpoint,
        request_params=exchange.request_params,
        body=exchange.body,
    )
    return ctx.raw_repo.store(
        run_id=ctx.run_id,
        provider=KALSHI_PUBLIC_PROVIDER,
        endpoint=exchange.endpoint,
        request_params_json=canonical_json(exchange.request_params),
        http_status=exchange.http_status,
        response_headers_json=canonical_json(exchange.response_headers),
        requested_at=to_iso(exchange.requested_at),
        received_at=to_iso(exchange.received_at),
        elapsed_ns=exchange.elapsed_ns,
        body=exchange.body,
        content_hash=content_hash,
        content_type=exchange.content_type,
    )


def _ingest_events(ctx: _Ctx, page_items: list[dict[str, Any]], raw: RawResponse) -> None:
    for event in page_items:
        ctx.result.records_received += 1
        ticker = str(event.get("event_ticker") or "").strip()
        if not ticker:
            ctx.result.records_rejected += 1
            ctx.result.events_rejected += 1
            ctx.result.note("event missing event_ticker")
            continue
        me = event.get("mutually_exclusive")
        ctx.kalshi.upsert_event(
            event_ticker=ticker,
            raw_response_id=raw.raw_response_id,
            observed_at=raw.received_at,
            series_ticker=_opt_str(event.get("series_ticker")),
            title=_opt_str(event.get("title")),
            sub_title=_opt_str(event.get("sub_title") or event.get("subtitle")),
            category=_opt_str(event.get("category")),
            status=_opt_str(event.get("status")),
            mutually_exclusive=None if me is None else bool(me),
        )
        ctx.result.events_seen += 1
        ctx.result.records_normalized += 1
        ctx.result.records_inserted += 1


def _ingest_markets(ctx: _Ctx, page_items: list[dict[str, Any]], raw: RawResponse) -> None:
    for market in page_items:
        ctx.result.records_received += 1
        ticker = str(market.get("ticker") or market.get("market_ticker") or "").strip()
        if not ticker:
            ctx.result.records_rejected += 1
            ctx.result.markets_rejected += 1
            ctx.result.note("market missing ticker")
            continue
        event_ticker = _opt_str(market.get("event_ticker"))
        kalshi_event_id = None
        if event_ticker:
            owning = ctx.kalshi.get_event_by_ticker(event_ticker)
            kalshi_event_id = owning.kalshi_event_id if owning else None
        rules_primary = _opt_str(market.get("rules_primary"))
        ctx.kalshi.upsert_market(
            market_ticker=ticker,
            raw_response_id=raw.raw_response_id,
            observed_at=raw.received_at,
            event_ticker=event_ticker,
            kalshi_event_id=kalshi_event_id,
            series_ticker=_opt_str(market.get("series_ticker")),
            title=_opt_str(market.get("title")),
            subtitle=_opt_str(market.get("subtitle") or market.get("sub_title")),
            yes_sub_title=_opt_str(market.get("yes_sub_title")),
            no_sub_title=_opt_str(market.get("no_sub_title")),
            status=_opt_str(market.get("status")),
            open_time=normalize_provider_time(market.get("open_time")),
            close_time=normalize_provider_time(market.get("close_time")),
            expiration_time=normalize_provider_time(
                market.get("expiration_time") or market.get("expected_expiration_time")
            ),
            settlement_time=normalize_provider_time(market.get("settlement_time")),
            result=_opt_str(market.get("result")),
            rules_primary=rules_primary,
            rules_secondary=_opt_str(market.get("rules_secondary")),
            rules_hash=_rules_hash(rules_primary, _opt_str(market.get("rules_secondary"))),
        )
        ctx.result.markets_seen += 1
        ctx.result.records_normalized += 1
        ctx.result.records_inserted += 1


def _ingest_orderbook(ctx: _Ctx, market_ticker: str, body: dict[str, Any], raw: RawResponse) -> None:
    ctx.result.records_received += 1
    try:
        book = KalshiOrderBook.from_raw(market_ticker, body)
    except (TypeError, ValueError):
        ctx.result.records_rejected += 1
        ctx.result.orderbooks_rejected += 1
        ctx.result.note(f"order book for {market_ticker} could not be parsed")
        return
    if not _valid_ladder(book.yes_bids) or not _valid_ladder(book.no_bids):
        ctx.result.records_rejected += 1
        ctx.result.orderbooks_rejected += 1
        ctx.result.note(f"order book for {market_ticker} has an invalid level")
        return
    if _has_duplicate_prices(book.yes_bids) or _has_duplicate_prices(book.no_bids):
        ctx.result.records_rejected += 1
        ctx.result.orderbooks_rejected += 1
        ctx.result.note(f"order book for {market_ticker} has duplicate price levels")
        return

    content_hash = orderbook_content_hash(yes_bids=book.yes_bids, no_bids=book.no_bids)
    market = ctx.kalshi.get_market_by_ticker(market_ticker)
    snapshot, inserted = ctx.kalshi.append_orderbook_snapshot(
        market_ticker=market_ticker,
        yes_bids=book.yes_bids,
        no_bids=book.no_bids,
        observed_at=raw.received_at,
        run_id=ctx.run_id,
        raw_response_id=raw.raw_response_id,
        raw_response_hash=raw.content_hash,
        content_hash=content_hash,
        kalshi_market_id=market.kalshi_market_id if market else None,
        provider_timestamp=None,
    )
    ctx.result.records_normalized += 1
    if inserted:
        ctx.result.orderbook_snapshots_inserted += 1
        ctx.result.orderbook_levels_inserted += len(book.yes_bids) + len(book.no_bids)
        ctx.result.records_inserted += 1
    else:
        ctx.result.orderbook_snapshots_duplicate += 1
        ctx.result.records_deduplicated += 1


def _ingest_trades(ctx: _Ctx, default_ticker: str, page_items: list[dict[str, Any]], raw: RawResponse) -> None:
    for trade in page_items:
        ctx.result.records_received += 1
        market_ticker = str(trade.get("ticker") or default_ticker or "").strip()
        if not market_ticker:
            ctx.result.records_rejected += 1
            ctx.result.trades_rejected += 1
            ctx.result.note("trade missing market ticker")
            continue
        yes_price = trade.get("yes_price")
        no_price = trade.get("no_price")
        if yes_price is not None and not _valid_price(yes_price):
            ctx.result.records_rejected += 1
            ctx.result.trades_rejected += 1
            ctx.result.note(f"trade with invalid yes_price {yes_price!r}")
            continue
        if no_price is not None and not _valid_price(no_price):
            ctx.result.records_rejected += 1
            ctx.result.trades_rejected += 1
            ctx.result.note(f"trade with invalid no_price {no_price!r}")
            continue
        count = trade.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            ctx.result.records_rejected += 1
            ctx.result.trades_rejected += 1
            ctx.result.note(f"trade with invalid count {count!r}")
            continue

        taker_side = _opt_str(trade.get("taker_side"))
        if taker_side is not None and taker_side not in ("yes", "no"):
            taker_side = None
        provider_trade_id = _opt_str(trade.get("trade_id"))
        trade_time = normalize_provider_time(trade.get("created_time") or trade.get("trade_time"))
        content_hash = trade_content_hash(
            provider_trade_id=provider_trade_id,
            market_ticker=market_ticker,
            trade_time=trade_time,
            yes_price=yes_price,
            no_price=no_price,
            count=count,
            taker_side=taker_side,
        )
        market = ctx.kalshi.get_market_by_ticker(market_ticker)
        _trade, inserted = ctx.kalshi.append_trade(
            market_ticker=market_ticker,
            count=count,
            observed_at=raw.received_at,
            run_id=ctx.run_id,
            raw_response_id=raw.raw_response_id,
            content_hash=content_hash,
            provider_trade_id=provider_trade_id,
            kalshi_market_id=market.kalshi_market_id if market else None,
            taker_side=taker_side,
            yes_price=yes_price if yes_price is not None else None,
            no_price=no_price if no_price is not None else None,
            trade_time=trade_time,
            provider_timestamp=trade_time,
        )
        ctx.result.records_normalized += 1
        if inserted:
            ctx.result.trades_inserted += 1
            ctx.result.records_inserted += 1
        else:
            ctx.result.trades_duplicate += 1
            ctx.result.records_deduplicated += 1


async def _ingest_persisting(
    *,
    database: Database,
    client: KalshiClient,
    status: Optional[str],
    event_ticker: Optional[str],
    market_ticker: Optional[str],
    limit: int,
    include_orderbooks: bool,
    include_trades: bool,
    max_pages: int,
    args_json: str,
    tool_version: str,
) -> KalshiIngestResult:
    import time

    result = KalshiIngestResult(dry_run=False, status="succeeded")
    started_monotonic_ns = time.monotonic_ns()

    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=_COMMAND,
                provider=KALSHI_PUBLIC_PROVIDER,
                operation="list_markets",
                args_json=args_json,
                started_monotonic_ns=started_monotonic_ns,
                tool_version=tool_version,
            )
        result.run_id = run.run_id
        ctx = _Ctx(
            conn=conn,
            kalshi=SqliteKalshiRepository(conn),
            raw_repo=SqliteRawResponseRepository(conn),
            run_id=run.run_id,
            result=result,
        )

        try:
            await _run_ingestion(
                ctx=ctx, client=client, status=status, event_ticker=event_ticker,
                market_ticker=market_ticker, limit=limit,
                include_orderbooks=include_orderbooks, include_trades=include_trades,
                max_pages=max_pages, result=result,
            )
        except Exception as exc:  # noqa: BLE001 - classify, never leak
            _finish_failed(conn, runs, run.run_id, exc, started_monotonic_ns, result)
            return result

        terminal = _terminal_status(result)
        with transaction(conn):
            runs.complete(
                run.run_id,
                status=terminal,
                duration_ns=time.monotonic_ns() - started_monotonic_ns,
                requests_made=result.requests_made,
                records_received=result.records_received,
                records_normalized=result.records_normalized,
                records_inserted=result.records_inserted,
                records_deduplicated=result.records_deduplicated,
                records_rejected=result.records_rejected,
            )
        result.status = terminal
    return result


async def _run_ingestion(
    *,
    ctx: _Ctx,
    client: KalshiClient,
    status: Optional[str],
    event_ticker: Optional[str],
    market_ticker: Optional[str],
    limit: int,
    include_orderbooks: bool,
    include_trades: bool,
    max_pages: int,
    result: KalshiIngestResult,
) -> None:
    # 1) Events (skipped when targeting a single market).
    if market_ticker is None:
        events_page = await client.fetch_events(
            status=status, limit=limit, max_pages=max_pages
        )
        result.requests_made += len(events_page.exchanges)
        for body, exchange in zip(events_page.page_bodies, events_page.exchanges, strict=True):
            with transaction(ctx.conn):
                raw = _store_raw(ctx, exchange)
            with transaction(ctx.conn):
                _ingest_events(ctx, body.get("events", []) or [], raw)

    # 2) Markets.
    markets_page = await client.fetch_markets(
        status=None if market_ticker else status,
        event_ticker=event_ticker,
        tickers=market_ticker,
        limit=limit,
        max_pages=max_pages,
    )
    result.requests_made += len(markets_page.exchanges)
    for body, exchange in zip(markets_page.page_bodies, markets_page.exchanges, strict=True):
        with transaction(ctx.conn):
            raw = _store_raw(ctx, exchange)
        with transaction(ctx.conn):
            _ingest_markets(ctx, body.get("markets", []) or [], raw)

    market_tickers = [
        str(m.get("ticker") or m.get("market_ticker") or "").strip()
        for m in markets_page.items
    ]
    market_tickers = [t for t in market_tickers if t][:limit]

    # 3) Order books -- bounded by `limit`, never the whole exchange.
    if include_orderbooks and market_tickers:
        if len(markets_page.items) > limit:
            result.orderbooks_truncated_at = limit
        for ticker in market_tickers:
            body, exchange = await client.fetch_market_orderbook_raw(ticker)
            result.requests_made += 1
            with transaction(ctx.conn):
                raw = _store_raw(ctx, exchange)  # bytes preserved before parsing
            with transaction(ctx.conn):
                _ingest_orderbook(ctx, ticker, body, raw)

    # 4) Public trades -- bounded likewise.
    if include_trades and market_tickers:
        for ticker in market_tickers:
            trades_page = await client.fetch_trades(ticker=ticker, limit=limit, max_pages=max_pages)
            result.requests_made += len(trades_page.exchanges)
            for body, exchange in zip(
                trades_page.page_bodies, trades_page.exchanges, strict=True
            ):
                with transaction(ctx.conn):
                    raw = _store_raw(ctx, exchange)
                with transaction(ctx.conn):
                    _ingest_trades(ctx, ticker, body.get("trades", []) or [], raw)


def _finish_failed(
    conn: Any,
    runs: SqliteIngestionRunRepository,
    run_id: str,
    exc: BaseException,
    started_monotonic_ns: int,
    result: KalshiIngestResult,
) -> None:
    import time

    error_type, error_message = sanitize_error(exc)
    result.status = "failed"
    result.error_type = error_type
    result.error_message = error_message
    with transaction(conn):
        runs.complete(
            run_id,
            status="failed",
            duration_ns=time.monotonic_ns() - started_monotonic_ns,
            requests_made=result.requests_made,
            records_received=result.records_received,
            records_normalized=result.records_normalized,
            records_inserted=result.records_inserted,
            records_deduplicated=result.records_deduplicated,
            records_rejected=result.records_rejected,
            error_type=error_type,
            error_message=error_message,
        )


async def _ingest_dry_run(
    *,
    client: KalshiClient,
    status: Optional[str],
    event_ticker: Optional[str],
    market_ticker: Optional[str],
    limit: int,
    include_orderbooks: bool,
    include_trades: bool,
    max_pages: int,
) -> KalshiIngestResult:
    """Fetch and normalize, persist nothing. Reports the counts a real run would."""

    result = KalshiIngestResult(dry_run=True, status="succeeded")
    try:
        if market_ticker is None:
            events_page = await client.fetch_events(status=status, limit=limit, max_pages=max_pages)
            result.requests_made += len(events_page.exchanges)
            for event in events_page.items:
                result.records_received += 1
                if str(event.get("event_ticker") or "").strip():
                    result.events_seen += 1
                    result.records_normalized += 1
                else:
                    result.events_rejected += 1
                    result.records_rejected += 1

        markets_page = await client.fetch_markets(
            status=None if market_ticker else status, event_ticker=event_ticker,
            tickers=market_ticker, limit=limit, max_pages=max_pages,
        )
        result.requests_made += len(markets_page.exchanges)
        market_tickers = []
        for market in markets_page.items:
            result.records_received += 1
            ticker = str(market.get("ticker") or market.get("market_ticker") or "").strip()
            if ticker:
                result.markets_seen += 1
                result.records_normalized += 1
                market_tickers.append(ticker)
            else:
                result.markets_rejected += 1
                result.records_rejected += 1
        market_tickers = market_tickers[:limit]

        if include_orderbooks and market_tickers:
            for ticker in market_tickers:
                _book, _exchange = await client.fetch_market_orderbook(ticker)
                result.requests_made += 1
                result.records_received += 1
                result.records_normalized += 1
        if include_trades and market_tickers:
            for ticker in market_tickers:
                trades_page = await client.fetch_trades(
                    ticker=ticker, limit=limit, max_pages=max_pages
                )
                result.requests_made += len(trades_page.exchanges)
                result.records_received += len(trades_page.items)
                result.records_normalized += len(trades_page.items)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result

    if result.records_rejected:
        result.status = "partially_succeeded"
    return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _rules_hash(primary: Optional[str], secondary: Optional[str]) -> Optional[str]:
    import hashlib

    if primary is None and secondary is None:
        return None
    payload = canonical_json({"primary": primary, "secondary": secondary})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_ladder(levels: list[tuple[int, int]]) -> bool:
    for price, quantity in levels:
        if not (KALSHI_PRICE_MIN <= price <= KALSHI_PRICE_MAX):
            return False
        if quantity < 0:
            return False
    return True


def _has_duplicate_prices(levels: list[tuple[int, int]]) -> bool:
    prices = [price for price, _ in levels]
    return len(prices) != len(set(prices))


def _terminal_status(result: KalshiIngestResult) -> str:
    if result.records_rejected and result.records_normalized:
        return "partially_succeeded"
    if result.records_rejected and not result.records_normalized and result.records_received:
        return "partially_succeeded"
    return "succeeded"
