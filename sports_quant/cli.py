"""Command-line entry points for the read-only engine.

Currently exposes ``providers-check``: a safe, GET-only smoke test that
confirms both public-data providers are reachable and reports sanitized record
counts plus Odds API credit headers.

Guarantees:

* It never places, cancels or simulates an order -- no execution module is
  imported here, and every request is forced through the GET-only transport
  policy in :mod:`sports_quant.http_policy`.
* It never prints the Odds API key; only its presence is reported.
* Each check is classified as **ok**, **skipped** (inactive / out of season /
  not configured) or **failed**. The process exits non-zero only when something
  that was genuinely active failed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config import ReadOnlyStartupError, Settings, load_settings
from .db.engine import Database, DatabaseError, table_exists
from .db.init import initialize_database
from .ingest.kalshi_ingestor import DEFAULT_LIMIT, KalshiIngestResult, ingest_kalshi
from .ingest.odds_ingestor import OddsIngestResult, ingest_odds
from .providers.kalshi import KalshiClient
from .providers.odds_api import (
    MLB_SPORT_KEY,
    NBA_SPORT_KEY,
    CreditHeaders,
    OddsApiClient,
    OddsApiResult,
)

Printer = Callable[[str], None]


class CheckStatus(str, Enum):
    """Outcome of a single provider check."""

    OK = "ok"
    #: Nothing was wrong -- the league is out of season, or the provider is not
    #: configured. Never contributes to a non-zero exit code.
    SKIPPED = "skipped"
    #: Something active genuinely failed. Forces a non-zero exit code.
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    """One named check plus its status and a sanitized human-readable detail."""

    name: str
    status: CheckStatus
    detail: str

    def line(self) -> str:
        return f"[{self.status.value.upper():7}] {self.name}: {self.detail}"


def _fmt_credits(credits: CreditHeaders) -> str:
    return (
        f"requests-remaining={credits.requests_remaining} "
        f"requests-used={credits.requests_used} "
        f"requests-last={credits.requests_last}"
    )


def _describe(exc: BaseException) -> str:
    """Render an exception safely (provider adapters already sanitize URLs)."""

    return f"{type(exc).__name__}: {exc}"


async def _check_sport(
    *,
    label: str,
    active: Optional[bool],
    fetch: Callable[[], Awaitable[OddsApiResult]],
) -> CheckResult:
    """Fetch odds for one sport.

    An out-of-season league is a **successful skip**, not a failure. A league
    the provider reports as active that then fails to fetch *is* a failure.
    """

    if active is False:
        return CheckResult(
            f"odds/{label}", CheckStatus.SKIPPED, "not active (out of season)"
        )
    try:
        result = await fetch()
    except Exception as exc:  # noqa: BLE001 -- classify, do not crash the check
        return CheckResult(f"odds/{label}", CheckStatus.FAILED, _describe(exc))
    return CheckResult(
        f"odds/{label}",
        CheckStatus.OK,
        f"{len(result.events)} events | {_fmt_credits(result.credits)}",
    )


async def _check_odds_api(settings: Settings) -> list[CheckResult]:
    """Check The Odds API: key presence, sports list, then MLB/NBA odds."""

    if not settings.has_odds_api_key():
        return [
            CheckResult(
                "odds/api-key",
                CheckStatus.SKIPPED,
                "not configured -- set ODDS_API_KEY in .env (value never displayed)",
            )
        ]

    results = [
        CheckResult("odds/api-key", CheckStatus.OK, "present (value not displayed)")
    ]
    odds = OddsApiClient(settings.odds_api_key)
    try:
        try:
            sports = await odds.get_sports()
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult("odds/sports", CheckStatus.FAILED, _describe(exc)))
            return results

        active_keys = {s.key for s in sports.sports if s.active}
        results.append(
            CheckResult(
                "odds/sports",
                CheckStatus.OK,
                f"{len(sports.sports)} listed | {_fmt_credits(sports.credits)}",
            )
        )
        results.append(
            await _check_sport(
                label="MLB",
                active=MLB_SPORT_KEY in active_keys,
                fetch=odds.get_mlb_odds,
            )
        )
        results.append(
            await _check_sport(
                label="NBA",
                active=NBA_SPORT_KEY in active_keys,
                fetch=odds.get_nba_odds,
            )
        )
    finally:
        await odds.aclose()
    return results


async def _check_kalshi(settings: Settings) -> list[CheckResult]:
    """Check Kalshi public REST: exchange status, then five open markets."""

    results: list[CheckResult] = []
    kalshi = KalshiClient(base_url=settings.kalshi_public_rest_url)
    try:
        try:
            status = await kalshi.exchange_status()
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult("kalshi/status", CheckStatus.FAILED, _describe(exc)))
        else:
            results.append(
                CheckResult(
                    "kalshi/status",
                    CheckStatus.OK,
                    f"exchange_active={status.get('exchange_active')} "
                    f"trading_active={status.get('trading_active')}",
                )
            )

        try:
            markets = await kalshi.list_markets(status="open", limit=5)
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult("kalshi/markets", CheckStatus.FAILED, _describe(exc)))
        else:
            results.append(
                CheckResult(
                    "kalshi/markets",
                    CheckStatus.OK,
                    f"retrieved {len(markets.items)} open markets (requested 5)",
                )
            )
    finally:
        await kalshi.aclose()
    return results


async def run_providers_check(
    settings: Optional[Settings] = None,
    *,
    out: Printer = print,
) -> int:
    """Run the read-only provider check.

    Returns ``0`` when nothing active failed and ``1`` when at least one active
    provider genuinely failed. Out-of-season leagues and unconfigured providers
    are reported as successful skips.
    """

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    out("Read-only provider check (GET-only; no orders are placed or simulated)")
    out(f"  Kalshi environment: {settings.kalshi_environment}")
    out(f"  Kalshi public REST: {settings.kalshi_public_rest_url}")

    results = await _check_odds_api(settings)
    results += await _check_kalshi(settings)

    for result in results:
        out(result.line())

    counts = {
        status: sum(1 for r in results if r.status is status) for status in CheckStatus
    }
    out(
        f"Summary: {counts[CheckStatus.OK]} ok, "
        f"{counts[CheckStatus.SKIPPED]} skipped (inactive), "
        f"{counts[CheckStatus.FAILED]} failed."
    )
    return 1 if counts[CheckStatus.FAILED] else 0


# Exit code reserved for database problems: missing, unmigrated, or a schema
# checksum mismatch. Distinct from 1 (a provider genuinely failed) and 2 (the
# read-only startup invariants were violated).
EXIT_DATABASE_ERROR = 3


def run_db_init(
    settings: Optional[Settings] = None,
    *,
    database_path: Optional[Path] = None,
    out: Printer = print,
) -> int:
    """Create, migrate and seed the local corpus database.

    Offline: no network call is made and no provider client is constructed.
    Safe to run repeatedly -- migrations apply only when pending and seeding is
    idempotent, so a second run adds nothing and destroys nothing.
    """

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    path = database_path if database_path is not None else settings.resolved_database_path()

    out("Database initialization (offline; no provider request is made)")
    out(f"  Database: {path}")

    try:
        result = initialize_database(path)
    except DatabaseError as exc:
        out(f"[FAILED ] {exc}")
        return EXIT_DATABASE_ERROR

    if result.created_database:
        out("[OK     ] database file created")
    else:
        out("[OK     ] database file already present")

    if result.applied:
        for migration in result.applied:
            out(f"[OK     ] applied migration {migration.version:03d} {migration.name}")
    else:
        out("[SKIPPED] migrations: schema already current")

    out(f"  Schema version: {result.schema_version}")

    for league in result.seeds.leagues:
        out(
            f"[OK     ] {league.league_code}: {league.teams_total} teams "
            f"({league.teams_created} new), {league.aliases_created} new aliases, "
            f"{league.aliases_flagged_ambiguous} flagged ambiguous"
        )

    if result.was_already_current:
        out("Database already up to date; nothing was changed.")
    else:
        out(f"Database ready: {result.seeds.teams_total} teams across "
            f"{len(result.seeds.leagues)} leagues.")
    return 0


# Exit code reserved for a genuine failure of something active (a fetch or a
# write that should have worked). Distinct from a clean skip (0).
EXIT_ACTIVE_FAILURE = 1


def _report_ingest(result: OddsIngestResult, out: Printer) -> None:
    """Print a sanitized ingestion summary. Never displays the API key."""

    if result.dry_run:
        out(f"[DRY-RUN] ingest-odds --sport {result.sport} (no rows persisted)")
    else:
        out(f"ingest-odds --sport {result.sport} (run {result.run_id})")

    if result.status == "failed":
        out(f"[FAILED ] {result.error_type}: {result.error_message}")
        return

    if result.credits is not None:
        out(f"  credits: {_fmt_credits(result.credits)}")
    if result.events_seen == 0 and result.events_rejected == 0:
        out("  0 available events (no games available; not a failure)")
    out(
        f"  events={result.events_seen} (rejected {result.events_rejected}) "
        f"markets={result.markets_seen} outcomes={result.outcomes_seen}"
    )
    out(
        f"  price snapshots: {result.snapshots_inserted} new, "
        f"{result.snapshots_duplicate} duplicate (unchanged)"
    )
    out(
        f"  records: received={result.records_received} normalized={result.records_normalized} "
        f"rejected={result.records_rejected}"
    )
    if result.rejections:
        shown = ", ".join(sorted(set(result.rejections)))
        out(f"  rejection reasons: {shown}")
    status_label = {
        "succeeded": "OK     ",
        "partially_succeeded": "PARTIAL",
    }.get(result.status, result.status.upper())
    out(f"[{status_label}] {result.status}")


def run_ingest_odds(
    settings: Optional[Settings] = None,
    *,
    sport: str,
    database_path: Optional[Path] = None,
    markets: Optional[str] = None,
    regions: str = "us",
    bookmakers: Optional[str] = None,
    commence_from: Optional[str] = None,
    commence_to: Optional[str] = None,
    dry_run: bool = False,
    out: Printer = print,
    client: Optional[OddsApiClient] = None,
) -> int:
    """Ingest current Odds API prices for one sport into the corpus.

    Read-only and GET-only. Returns ``0`` on success (including an out-of-season
    sport, which is simply zero available events), ``1`` on a genuine active
    failure, ``2`` on a read-only startup violation, and ``3`` when the database
    is missing or unmigrated. The API key is never printed.
    """

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    owns_client = client is None
    if owns_client and not settings.has_odds_api_key():
        out(
            "[SKIPPED] Odds API key not configured -- set ODDS_API_KEY in .env "
            "(value never displayed); nothing was ingested"
        )
        return 0

    path = database_path if database_path is not None else settings.resolved_database_path()

    # A persisting run needs a migrated corpus; a dry run touches no database.
    if not dry_run:
        if not path.exists():
            out(f"[FAILED ] database not found at {path}; run 'python -m sports_quant db-init'")
            return EXIT_DATABASE_ERROR
        db_check = Database(path)
        with db_check.connection() as conn:
            if not table_exists(conn, "sportsbook_price_snapshots"):
                out(
                    f"[FAILED ] database at {path} is not migrated for Phase B; "
                    "run 'python -m sports_quant db-init'"
                )
                return EXIT_DATABASE_ERROR

    database = Database(path)

    async def _run() -> OddsIngestResult:
        odds = client if client is not None else OddsApiClient(settings.odds_api_key)
        try:
            return await ingest_odds(
                database=database,
                client=odds,
                sport=sport,
                markets=markets,
                regions=regions,
                bookmakers=bookmakers,
                commence_from=commence_from,
                commence_to=commence_to,
                dry_run=dry_run,
            )
        finally:
            if owns_client:
                await odds.aclose()

    result = asyncio.run(_run())
    _report_ingest(result, out)
    return EXIT_ACTIVE_FAILURE if result.failed else 0


def _report_kalshi(result: KalshiIngestResult, out: Printer) -> None:
    """Print a sanitized Kalshi ingestion summary. No credential is ever shown."""

    if result.dry_run:
        out("[DRY-RUN] ingest-kalshi (no rows persisted)")
    else:
        out(f"ingest-kalshi (run {result.run_id})")

    if result.status == "failed":
        out(f"[FAILED ] {result.error_type}: {result.error_message}")
        return

    out(f"  requests: {result.requests_made} (GET-only, public, unauthenticated)")
    if result.events_seen == 0 and result.markets_seen == 0:
        out("  0 matching events/markets (not a failure)")
    out(
        f"  events={result.events_seen} "
        f"(new {result.events_inserted}, updated {result.events_updated}, "
        f"rejected {result.events_rejected}) "
        f"markets={result.markets_seen} "
        f"(new {result.markets_inserted}, updated {result.markets_updated}, "
        f"rejected {result.markets_rejected})"
    )
    out(
        f"  order books: {result.orderbook_snapshots_inserted} new, "
        f"{result.orderbook_snapshots_duplicate} unchanged, "
        f"{result.orderbook_levels_inserted} levels "
        f"(rejected {result.orderbooks_rejected})"
    )
    out(
        f"  public trades: {result.trades_inserted} new, "
        f"{result.trades_duplicate} duplicate (rejected {result.trades_rejected})"
    )
    if result.orderbooks_truncated_at is not None:
        out(
            f"  NOTE: order-book fan-out truncated at --limit={result.orderbooks_truncated_at}; "
            "more markets were available (partial sweep, not a complete one)"
        )
    if result.rejections:
        shown = ", ".join(sorted(set(result.rejections)))
        out(f"  rejection reasons: {shown}")
    status_label = {
        "succeeded": "OK     ",
        "partially_succeeded": "PARTIAL",
    }.get(result.status, result.status.upper())
    out(f"[{status_label}] {result.status}")


def run_ingest_kalshi(
    settings: Optional[Settings] = None,
    *,
    status: Optional[str] = "open",
    event_ticker: Optional[str] = None,
    market_ticker: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    include_orderbooks: bool = False,
    include_trades: bool = False,
    max_pages: int = 1,
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    out: Printer = print,
    client: Optional[KalshiClient] = None,
) -> int:
    """Ingest Kalshi public events/markets (and optionally books/trades).

    Read-only, GET-only, unauthenticated -- no Kalshi credential is ever required
    or displayed. Returns ``0`` on success (including zero matching markets or a
    closed exchange), ``1`` on a genuine provider/parse/persist failure, ``2`` on
    a read-only startup violation, and ``3`` when the database is missing or
    unmigrated.
    """

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    path = database_path if database_path is not None else settings.resolved_database_path()

    if not dry_run:
        if not path.exists():
            out(f"[FAILED ] database not found at {path}; run 'python -m sports_quant db-init'")
            return EXIT_DATABASE_ERROR
        db_check = Database(path)
        with db_check.connection() as conn:
            if not table_exists(conn, "kalshi_orderbook_snapshots"):
                out(
                    f"[FAILED ] database at {path} is not migrated for Phase C; "
                    "run 'python -m sports_quant db-init'"
                )
                return EXIT_DATABASE_ERROR

    database = Database(path)
    owns_client = client is None

    async def _run() -> KalshiIngestResult:
        kalshi = client if client is not None else KalshiClient(
            base_url=settings.kalshi_public_rest_url
        )
        try:
            return await ingest_kalshi(
                database=database,
                client=kalshi,
                status=status,
                event_ticker=event_ticker,
                market_ticker=market_ticker,
                limit=limit,
                include_orderbooks=include_orderbooks,
                include_trades=include_trades,
                max_pages=max_pages,
                dry_run=dry_run,
            )
        finally:
            if owns_client:
                await kalshi.aclose()

    result = asyncio.run(_run())
    _report_kalshi(result, out)
    return EXIT_ACTIVE_FAILURE if result.failed else 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI dispatch.

    Usage::

        python -m sports_quant providers-check
        python -m sports_quant db-init
    """

    import argparse

    parser = argparse.ArgumentParser(prog="sports_quant", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("providers-check", help="Read-only reachability check for both data providers")
    db_init = sub.add_parser(
        "db-init", help="Create, migrate and seed the local historical corpus database"
    )
    db_init.add_argument(
        "--db",
        dest="database_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override DATABASE_PATH for this run",
    )

    ingest = sub.add_parser(
        "ingest-odds", help="Fetch and store current MLB/NBA sportsbook odds (GET-only)"
    )
    ingest.add_argument("--sport", required=True, choices=("mlb", "nba"), help="Sport to ingest")
    ingest.add_argument(
        "--markets",
        default=None,
        help="Comma-separated market keys (default: h2h,spreads,totals)",
    )
    ingest.add_argument("--regions", default="us", help="Odds region (default: us)")
    ingest.add_argument(
        "--bookmakers", default=None, help="Comma-separated bookmaker keys to restrict to"
    )
    ingest.add_argument(
        "--commence-from",
        dest="commence_from",
        default=None,
        metavar="ISO8601",
        help="Only events commencing at or after this UTC time",
    )
    ingest.add_argument(
        "--commence-to",
        dest="commence_to",
        default=None,
        metavar="ISO8601",
        help="Only events commencing at or before this UTC time",
    )
    ingest.add_argument(
        "--db",
        dest="database_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override DATABASE_PATH for this run",
    )
    ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform the GET and normalization but persist nothing",
    )

    kalshi = sub.add_parser(
        "ingest-kalshi",
        help="Fetch and store Kalshi PUBLIC events/markets/books/trades (GET-only, no credential)",
    )
    kalshi.add_argument(
        "--status", default="open", help="Kalshi status filter (default: open)"
    )
    kalshi.add_argument(
        "--event-ticker", dest="event_ticker", default=None, help="Restrict markets to one event"
    )
    kalshi.add_argument(
        "--market-ticker", dest="market_ticker", default=None, help="Ingest a single market"
    )
    kalshi.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Safe finite bound on events/markets/books (default: {DEFAULT_LIMIT})",
    )
    kalshi.add_argument(
        "--max-pages", dest="max_pages", type=int, default=1, help="Max pages per listing"
    )
    kalshi.add_argument(
        "--include-orderbooks",
        dest="include_orderbooks",
        action="store_true",
        help="Also fetch each market's public order book (bounded by --limit)",
    )
    kalshi.add_argument(
        "--include-trades",
        dest="include_trades",
        action="store_true",
        help="Also fetch each market's public trades (bounded by --limit)",
    )
    kalshi.add_argument(
        "--db",
        dest="database_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override DATABASE_PATH for this run",
    )
    kalshi.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform the GETs and normalization but persist nothing",
    )

    args = parser.parse_args(argv)

    if args.command == "providers-check":
        try:
            return asyncio.run(run_providers_check())
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    if args.command == "db-init":
        try:
            return run_db_init(database_path=args.database_path)
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    if args.command == "ingest-odds":
        try:
            return run_ingest_odds(
                sport=args.sport,
                database_path=args.database_path,
                markets=args.markets,
                regions=args.regions,
                bookmakers=args.bookmakers,
                commence_from=args.commence_from,
                commence_to=args.commence_to,
                dry_run=args.dry_run,
            )
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    if args.command == "ingest-kalshi":
        try:
            return run_ingest_kalshi(
                status=args.status,
                event_ticker=args.event_ticker,
                market_ticker=args.market_ticker,
                limit=args.limit,
                include_orderbooks=args.include_orderbooks,
                include_trades=args.include_trades,
                max_pages=args.max_pages,
                database_path=args.database_path,
                dry_run=args.dry_run,
            )
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error raises SystemExit
