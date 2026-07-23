"""The Odds API ingestion service.

Fetches current odds for one sport through the **existing** ``OddsApiClient``
(never a second client), preserves the raw response before parsing, then writes
events, markets, outcome identities and append-only price snapshots through the
typed repositories, recording one ingestion run.

Guarantees carried by this module:

* **GET-only, public data.** The transport policy is the only network path; no
  order surface is imported.
* **No secret is ever persisted.** The adapter hands over an already-sanitized
  :class:`~sports_quant.providers.odds_api.RawExchange`; this module adds no
  path that could reintroduce the key.
* **Persist-before-parse.** The raw bytes are committed in their own
  transaction before any normalized row is derived, so a parse failure never
  loses the response.
* **Idempotent.** Re-running with unchanged prices writes zero new snapshots;
  a changed price appends a new snapshot; an older backfill is preserved.
* **Partial data is safe.** A malformed event or outcome is rejected and
  counted, never fabricated, and never aborts the whole run.
* **De-vigging is not performed.** ``implied_probability`` is the raw,
  vig-inclusive transform of the American price; Phase B computes no fair value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.models import RawResponse
from ..db.normalize import normalized_key
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from ..db.repositories.sportsbook import (
    SqliteSportsbookRepository,
    price_content_hash,
)
from ..db.schema import (
    SPORT_ARG_TO_SPORT_KEY,
    SPORT_KEY_TO_LEAGUE_CODE,
    SUPPORTED_MARKET_KEYS,
    THE_ODDS_API_PROVIDER,
    to_iso,
)
from ..providers.odds_api import (
    CreditHeaders,
    OddsApiClient,
    OddsApiHTTPError,
    RawExchange,
    normalize_event,
)
from .runner import RunCounters, sanitize_error

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "ingest-odds"
_OPERATION = "get_odds"
_MAX_REPORTED_REJECTIONS = 20


# --------------------------------------------------------------------------- #
# American-odds arithmetic (exact transforms; the original price is preserved)
# --------------------------------------------------------------------------- #
def is_valid_american(price: float) -> bool:
    """American odds are integers whose magnitude is at least 100.

    A value in ``(-100, 100)`` (or a non-integer) is malformed -- not a long
    shot -- and is rejected rather than coerced.
    """

    if not float(price).is_integer():
        return False
    value = int(price)
    return value <= -100 or value >= 100


def american_to_decimal(price: int) -> float:
    """Exact decimal-odds transform of an American price."""

    if price > 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / abs(price)


def american_to_implied(price: int) -> float:
    """Raw, vig-inclusive implied probability of an American price.

    No de-vigging: this is the book's quoted probability including its margin,
    stored for convenience. Fair-value estimation is a later phase.
    """

    if price > 0:
        return 100.0 / (price + 100.0)
    return abs(price) / (abs(price) + 100.0)


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass
class OddsIngestResult:
    """Sanitized outcome of one ``ingest-odds`` run, safe to print."""

    sport: str
    sport_key: str
    dry_run: bool
    status: str
    run_id: Optional[str] = None
    requests_made: int = 0
    events_seen: int = 0
    events_rejected: int = 0
    markets_seen: int = 0
    outcomes_seen: int = 0
    snapshots_inserted: int = 0
    snapshots_duplicate: int = 0
    records_received: int = 0
    records_normalized: int = 0
    records_rejected: int = 0
    rejections: list[str] = field(default_factory=list)
    credits: Optional[CreditHeaders] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
async def ingest_odds(
    *,
    database: Database,
    client: OddsApiClient,
    sport: str,
    markets: Optional[str] = None,
    regions: str = "us",
    bookmakers: Optional[str] = None,
    commence_from: Optional[str] = None,
    commence_to: Optional[str] = None,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> OddsIngestResult:
    """Ingest current odds for one sport.

    ``sport`` is the CLI argument (``'mlb'`` / ``'nba'``). ``--dry-run``
    performs the external GET and normalization but writes nothing to the
    database -- not the run, not the raw response, not a single normalized row.
    """

    sport_key = SPORT_ARG_TO_SPORT_KEY.get(sport.lower())
    if sport_key is None:
        raise ValueError(f"unsupported sport {sport!r}; expected one of {sorted(SPORT_ARG_TO_SPORT_KEY)}")
    league_code = SPORT_KEY_TO_LEAGUE_CODE.get(sport_key)

    args_json = canonical_json(
        {
            "sport": sport,
            "markets": markets or "h2h,spreads,totals",
            "regions": regions,
            "bookmakers": bookmakers,
            "commence_from": commence_from,
            "commence_to": commence_to,
            "dry_run": dry_run,
        }
    )

    if dry_run:
        return await _ingest_dry_run(
            client=client,
            sport=sport,
            sport_key=sport_key,
            markets=markets,
            regions=regions,
            bookmakers=bookmakers,
            commence_from=commence_from,
            commence_to=commence_to,
        )

    return await _ingest_persisting(
        database=database,
        client=client,
        sport=sport,
        sport_key=sport_key,
        league_code=league_code,
        markets=markets,
        regions=regions,
        bookmakers=bookmakers,
        commence_from=commence_from,
        commence_to=commence_to,
        args_json=args_json,
        tool_version=tool_version,
    )


async def _fetch(
    *,
    client: OddsApiClient,
    sport_key: str,
    markets: Optional[str],
    regions: str,
    bookmakers: Optional[str],
    commence_from: Optional[str],
    commence_to: Optional[str],
) -> tuple[list[dict[str, Any]], CreditHeaders, RawExchange]:
    return await client.fetch_odds_raw(
        sport_key,
        regions=regions,
        markets=markets,
        bookmakers=bookmakers,
        commence_time_from=commence_from,
        commence_time_to=commence_to,
    )


async def _ingest_dry_run(
    *,
    client: OddsApiClient,
    sport: str,
    sport_key: str,
    markets: Optional[str],
    regions: str,
    bookmakers: Optional[str],
    commence_from: Optional[str],
    commence_to: Optional[str],
) -> OddsIngestResult:
    """Fetch and normalize, persist nothing. Reports the counts a real run would."""

    result = OddsIngestResult(sport=sport, sport_key=sport_key, dry_run=True, status="succeeded")
    try:
        raw, credits, _exchange = await _fetch(
            client=client,
            sport_key=sport_key,
            markets=markets,
            regions=regions,
            bookmakers=bookmakers,
            commence_from=commence_from,
            commence_to=commence_to,
        )
    except OddsApiHTTPError as exc:
        # A completed 4xx/5xx round-trip still counts as one request.
        result.requests_made = 1
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - classify, never leak
        # No response arrived, so no request completed: requests_made stays 0.
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result

    result.requests_made = 1
    result.credits = credits
    counters = RunCounters()
    for raw_event in raw:
        _normalize_and_count_only(
            raw_event, expected_sport_key=sport_key, result=result, counters=counters
        )
    _apply_counters(result, counters)
    if result.records_rejected and result.records_normalized == 0 and result.records_received:
        result.status = "partially_succeeded"
    elif result.records_rejected:
        result.status = "partially_succeeded"
    return result


async def _ingest_persisting(
    *,
    database: Database,
    client: OddsApiClient,
    sport: str,
    sport_key: str,
    league_code: Optional[str],
    markets: Optional[str],
    regions: str,
    bookmakers: Optional[str],
    commence_from: Optional[str],
    commence_to: Optional[str],
    args_json: str,
    tool_version: str,
) -> OddsIngestResult:
    import time

    result = OddsIngestResult(sport=sport, sport_key=sport_key, dry_run=False, status="succeeded")
    started_monotonic_ns = time.monotonic_ns()

    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=_COMMAND,
                provider=THE_ODDS_API_PROVIDER,
                operation=_OPERATION,
                args_json=args_json,
                started_monotonic_ns=started_monotonic_ns,
                tool_version=tool_version,
                sport=sport,
            )
        result.run_id = run.run_id

        # -- Fetch (network; outside any DB transaction) --------------------
        try:
            raw, credits, exchange = await _fetch(
                client=client,
                sport_key=sport_key,
                markets=markets,
                regions=regions,
                bookmakers=bookmakers,
                commence_from=commence_from,
                commence_to=commence_to,
            )
        except OddsApiHTTPError as exc:
            # A 4xx/5xx is a completed round-trip: the request WAS made, so it
            # counts as one. Its bytes still carry a body, preserved here under
            # this run so a later re-parse is possible, then the run fails.
            result.requests_made = 1
            _persist_failed_exchange(conn, run_id=run.run_id, exchange=exc.exchange)
            _finish_failed(conn, runs, run.run_id, exc, started_monotonic_ns, result)
            return result
        except Exception as exc:  # noqa: BLE001
            # A failure before any HTTP response arrived (connect/DNS/timeout):
            # no request completed, so requests_made stays 0.
            _finish_failed(conn, runs, run.run_id, exc, started_monotonic_ns, result)
            return result

        result.requests_made = 1
        result.credits = credits

        # -- Persist the raw response BEFORE parsing (own transaction) ------
        content_hash = response_content_hash(
            provider=THE_ODDS_API_PROVIDER,
            endpoint=exchange.endpoint,
            request_params=exchange.request_params,
            body=exchange.body,
        )
        with transaction(conn):
            raw_response = SqliteRawResponseRepository(conn).store(
                run_id=run.run_id,
                provider=THE_ODDS_API_PROVIDER,
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

        # -- Normalize and write derived rows (own transaction) -------------
        observed_at = raw_response.received_at
        counters = RunCounters(requests_made=1)
        try:
            with transaction(conn):
                sportsbook = SqliteSportsbookRepository(conn)
                league_id = _resolve_league_id(conn, league_code)
                for raw_event in raw:
                    _ingest_event(
                        raw_event,
                        sportsbook=sportsbook,
                        raw_response=raw_response,
                        run_id=run.run_id,
                        observed_at=observed_at,
                        league_id=league_id,
                        expected_sport_key=sport_key,
                        result=result,
                        counters=counters,
                    )
        except Exception as exc:  # noqa: BLE001 - unexpected write failure
            _finish_failed(conn, runs, run.run_id, exc, started_monotonic_ns, result)
            return result

        _apply_counters(result, counters)
        status = _terminal_status(counters)
        with transaction(conn):
            runs.complete(
                run.run_id,
                status=status,
                duration_ns=time.monotonic_ns() - started_monotonic_ns,
                requests_made=counters.requests_made,
                records_received=counters.records_received,
                records_normalized=counters.records_normalized,
                records_inserted=counters.records_inserted,
                records_deduplicated=counters.records_deduplicated,
                records_rejected=counters.records_rejected,
            )
        result.status = status
    return result


def _resolve_league_id(conn: Any, league_code: Optional[str]) -> Optional[str]:
    if league_code is None:
        return None
    row = conn.execute(
        "SELECT league_id FROM leagues WHERE code = ?", (league_code,)
    ).fetchone()
    return None if row is None else str(row["league_id"])


def _persist_failed_exchange(conn: Any, *, run_id: str, exchange: RawExchange) -> None:
    content_hash = response_content_hash(
        provider=THE_ODDS_API_PROVIDER,
        endpoint=exchange.endpoint,
        request_params=exchange.request_params,
        body=exchange.body,
    )
    with transaction(conn):
        SqliteRawResponseRepository(conn).store(
            run_id=run_id,
            provider=THE_ODDS_API_PROVIDER,
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


def _finish_failed(
    conn: Any,
    runs: SqliteIngestionRunRepository,
    run_id: str,
    exc: BaseException,
    started_monotonic_ns: int,
    result: OddsIngestResult,
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
            error_type=error_type,
            error_message=error_message,
        )


# --------------------------------------------------------------------------- #
# Per-event normalization
# --------------------------------------------------------------------------- #
def _reject(result: OddsIngestResult, counters: RunCounters, reason: str, *, count: int = 1) -> None:
    counters.records_received += count
    counters.records_rejected += count
    if len(result.rejections) < _MAX_REPORTED_REJECTIONS:
        result.rejections.append(reason)


def _count_event_observations(raw_event: dict[str, Any]) -> int:
    total = 0
    for bm in raw_event.get("bookmakers", []) or []:
        for mk in bm.get("markets", []) or []:
            total += len(mk.get("outcomes", []) or [])
    return total


def _validate_event(
    raw_event: dict[str, Any], *, expected_sport_key: str
) -> tuple[Optional[Any], Optional[str]]:
    """Normalize and validate one event.

    Returns ``(NormalizedEvent, None)`` or ``(None, reason)``. Team names are
    validated here rather than defaulted to empty strings: a blank team, or two
    teams that normalize identically, is a corrupt event -- storing it with
    ``home_team_raw = ''`` would fabricate a row that no later matcher could
    resolve and that silently pollutes every join. ``expected_sport_key`` is the
    key of the endpoint we actually requested; a payload whose ``sport_key``
    disagrees is rejected rather than stored under the wrong league.
    """

    try:
        event = normalize_event(raw_event)
    except Exception as exc:  # noqa: BLE001 - a malformed record, not a crash
        return None, f"event normalization failed: {type(exc).__name__}"
    if not event.provider_event_id:
        return None, "missing provider event id"
    if not event.sport_key:
        return None, "missing sport key"
    if event.sport_key != expected_sport_key:
        return None, (
            f"sport_key mismatch: payload {event.sport_key!r} != "
            f"requested {expected_sport_key!r}"
        )
    if event.commence_time is None:
        return None, "invalid or missing commence time"
    if event.home_team is None or not event.home_team.strip():
        return None, "missing or blank home team"
    if event.away_team is None or not event.away_team.strip():
        return None, "missing or blank away team"
    if normalized_key(event.home_team) == normalized_key(event.away_team):
        return None, "home and away teams are identical after normalization"
    return event, None


def _ingest_event(
    raw_event: dict[str, Any],
    *,
    sportsbook: SqliteSportsbookRepository,
    raw_response: RawResponse,
    run_id: str,
    observed_at: str,
    league_id: Optional[str],
    expected_sport_key: str,
    result: OddsIngestResult,
    counters: RunCounters,
) -> None:
    event, reason = _validate_event(raw_event, expected_sport_key=expected_sport_key)
    if event is None:
        assert reason is not None  # noqa: S101
        _reject(result, counters, reason, count=max(1, _count_event_observations(raw_event)))
        result.events_rejected += 1
        return

    # Validation has proven both names are present and distinct.
    home_norm = normalized_key(event.home_team)
    away_norm = normalized_key(event.away_team)

    sb_event = sportsbook.upsert_event(
        provider=THE_ODDS_API_PROVIDER,
        provider_event_id=event.provider_event_id,
        sport_key=event.sport_key,
        commence_time=to_iso(event.commence_time),
        home_team_raw=event.home_team,
        away_team_raw=event.away_team,
        raw_response_id=raw_response.raw_response_id,
        observed_at=observed_at,
        league_id=league_id,
    )
    result.events_seen += 1

    for bookmaker in event.bookmakers:
        if not bookmaker.key:
            _reject(result, counters, "missing bookmaker key",
                    count=max(1, sum(len(m.outcomes) for m in bookmaker.markets)))
            continue
        bm_last = to_iso(bookmaker.last_update) if bookmaker.last_update else None
        for market in bookmaker.markets:
            if market.key not in SUPPORTED_MARKET_KEYS:
                _reject(result, counters, f"unsupported market key {market.key!r}",
                        count=max(1, len(market.outcomes)))
                continue
            mk_last = to_iso(market.last_update) if market.last_update else None
            sb_market = sportsbook.upsert_market(
                sb_event_id=sb_event.sb_event_id,
                bookmaker_key=bookmaker.key,
                market_key=market.key,
                raw_response_id=raw_response.raw_response_id,
                observed_at=observed_at,
                bookmaker_title=bookmaker.title,
                bookmaker_last_update=bm_last,
                market_last_update=mk_last,
            )
            result.markets_seen += 1
            for outcome in market.outcomes:
                _ingest_outcome(
                    outcome=outcome,
                    market_key=market.key,
                    home_norm=home_norm,
                    away_norm=away_norm,
                    sb_market_id=sb_market.sb_market_id,
                    bookmaker_last_update=bm_last,
                    market_last_update=mk_last,
                    sportsbook=sportsbook,
                    raw_response=raw_response,
                    run_id=run_id,
                    observed_at=observed_at,
                    result=result,
                    counters=counters,
                )


def _classify_role(market_key: str, name: str, home_norm: str, away_norm: str) -> str:
    key = normalized_key(name)
    if market_key == "totals":
        if key == "over":
            return "over"
        if key == "under":
            return "under"
        return "unknown"
    # h2h and spreads carry team names; h2h may also carry a draw.
    if key and key == home_norm:
        return "home"
    if key and key == away_norm:
        return "away"
    if key == "draw":
        return "draw"
    return "unknown"


def _ingest_outcome(
    *,
    outcome: Any,
    market_key: str,
    home_norm: str,
    away_norm: str,
    sb_market_id: str,
    bookmaker_last_update: Optional[str],
    market_last_update: Optional[str],
    sportsbook: SqliteSportsbookRepository,
    raw_response: RawResponse,
    run_id: str,
    observed_at: str,
    result: OddsIngestResult,
    counters: RunCounters,
) -> None:
    counters.records_received += 1
    name = outcome.name or ""
    if not name.strip():
        counters.records_rejected += 1
        _append_reason(result, "outcome without a name")
        return
    if market_key in ("spreads", "totals") and outcome.point is None:
        counters.records_rejected += 1
        _append_reason(result, f"{market_key} outcome missing required point")
        return
    if outcome.price is None or not is_valid_american(outcome.price):
        counters.records_rejected += 1
        _append_reason(result, f"malformed American odds: {outcome.price!r}")
        return

    price_american = int(outcome.price)
    role = _classify_role(market_key, name, home_norm, away_norm)
    point = float(outcome.point) if outcome.point is not None else None

    sb_outcome = sportsbook.upsert_outcome(
        sb_market_id=sb_market_id,
        outcome_name=normalized_key(name),
        provider_outcome_name=name,
        outcome_role=role,
        point=point,
    )
    result.outcomes_seen += 1
    counters.records_normalized += 1

    content_hash = price_content_hash(
        price_american=price_american,
        point=point,
        bookmaker_last_update=bookmaker_last_update,
        market_last_update=market_last_update,
        provider_timestamp=market_last_update,
    )
    _snapshot, inserted = sportsbook.append_price_snapshot(
        sb_outcome_id=sb_outcome.sb_outcome_id,
        price_american=price_american,
        price_decimal=american_to_decimal(price_american),
        implied_probability=american_to_implied(price_american),
        point=point,
        bookmaker_last_update=bookmaker_last_update,
        market_last_update=market_last_update,
        provider_timestamp=market_last_update,
        observed_at=observed_at,
        raw_response_id=raw_response.raw_response_id,
        raw_response_hash=raw_response.content_hash,
        run_id=run_id,
        content_hash=content_hash,
    )
    if inserted:
        counters.records_inserted += 1
    else:
        counters.records_deduplicated += 1


def _append_reason(result: OddsIngestResult, reason: str) -> None:
    if len(result.rejections) < _MAX_REPORTED_REJECTIONS:
        result.rejections.append(reason)


def _normalize_and_count_only(
    raw_event: dict[str, Any],
    *,
    expected_sport_key: str,
    result: OddsIngestResult,
    counters: RunCounters,
) -> None:
    """Dry-run path: run the same validation, count, but write nothing."""

    event, reason = _validate_event(raw_event, expected_sport_key=expected_sport_key)
    if event is None:
        assert reason is not None  # noqa: S101
        _reject(result, counters, reason, count=max(1, _count_event_observations(raw_event)))
        result.events_rejected += 1
        return

    home_norm = normalized_key(event.home_team)
    away_norm = normalized_key(event.away_team)
    result.events_seen += 1
    for bookmaker in event.bookmakers:
        if not bookmaker.key:
            _reject(result, counters, "missing bookmaker key",
                    count=max(1, sum(len(m.outcomes) for m in bookmaker.markets)))
            continue
        for market in bookmaker.markets:
            if market.key not in SUPPORTED_MARKET_KEYS:
                _reject(result, counters, f"unsupported market key {market.key!r}",
                        count=max(1, len(market.outcomes)))
                continue
            result.markets_seen += 1
            for outcome in market.outcomes:
                counters.records_received += 1
                name = outcome.name or ""
                if not name.strip():
                    counters.records_rejected += 1
                    continue
                if market.key in ("spreads", "totals") and outcome.point is None:
                    counters.records_rejected += 1
                    continue
                if outcome.price is None or not is_valid_american(outcome.price):
                    counters.records_rejected += 1
                    continue
                _ = _classify_role(market.key, name, home_norm, away_norm)
                result.outcomes_seen += 1
                counters.records_normalized += 1


def _apply_counters(result: OddsIngestResult, counters: RunCounters) -> None:
    result.records_received = counters.records_received
    result.records_normalized = counters.records_normalized
    result.records_rejected = counters.records_rejected
    result.snapshots_inserted = counters.records_inserted
    result.snapshots_duplicate = counters.records_deduplicated


def _terminal_status(counters: RunCounters) -> str:
    """A run that rejected some records but stored others is partial, not clean."""

    if counters.records_rejected and counters.records_normalized:
        return "partially_succeeded"
    if counters.records_rejected and not counters.records_normalized and counters.records_received:
        return "partially_succeeded"
    return "succeeded"
