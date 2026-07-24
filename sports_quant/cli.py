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
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .config import ReadOnlyStartupError, Settings, load_settings
from .db.engine import Database, DatabaseError, table_exists
from .db.init import initialize_database
from .ingest.kalshi_ingestor import DEFAULT_LIMIT, KalshiIngestResult, ingest_kalshi
from .ingest.mlb_ingestor import (
    VALID_INCLUDES,
    MlbIngestResult,
    ingest_lineups,
    ingest_mlb,
)
from .ingest.odds_ingestor import OddsIngestResult, ingest_odds
from .ingest.provider_audit import (
    SUPPORTED_AUDIT_PROVIDERS,
    CapabilityProbe,
    ProviderAuditResult,
    audit_provider,
    build_balldontlie_probes,
    build_mlb_statsapi_probes,
    build_nws_probes,
    build_open_meteo_probes,
    declaration_for,
)
from .ingest.venues_ingestor import VenueIngestResult, ingest_venues
from .providers.balldontlie import BalldontlieClient
from .providers.capabilities import (
    PROVIDER_BALLDONTLIE,
    PROVIDER_MLB_STATSAPI,
    PROVIDER_NWS,
    PROVIDER_OPEN_METEO,
    BalldontlieTier,
)
from .providers.kalshi import KalshiClient
from .providers.mlb_statsapi import MlbStatsApiClient
from .providers.nws import NwsClient
from .providers.odds_api import (
    MLB_SPORT_KEY,
    NBA_SPORT_KEY,
    CreditHeaders,
    OddsApiClient,
    OddsApiResult,
)
from .providers.open_meteo import OpenMeteoClient

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


# --------------------------------------------------------------------------- #
# Phase D1: provider-audit and ingest-venues
# --------------------------------------------------------------------------- #
def _db_ready_or_exit(path: Path, table: str, out: Printer) -> Optional[int]:
    """Return an exit code if the DB is missing/unmigrated, else ``None``."""

    if not path.exists():
        out(f"[FAILED ] database not found at {path}; run 'python -m sports_quant db-init'")
        return EXIT_DATABASE_ERROR
    with Database(path).connection() as conn:
        if not table_exists(conn, table):
            out(
                f"[FAILED ] database at {path} is not migrated for Phase D; "
                "run 'python -m sports_quant db-init'"
            )
            return EXIT_DATABASE_ERROR
    return None


def _make_audit_probes(
    provider: str, settings: Settings
) -> tuple[list[CapabilityProbe], Any, Any]:
    """Build ``(probes, client, declaration)`` for a provider audit.

    One minimal approved GET **per capability group** is issued; each probe
    verifies only its own group. The BALLDONTLIE tier comes from settings
    (selected GOAT). Returns the client so the caller can close it, and the
    static declaration so unprobed capabilities are persisted as declared-only.
    """

    tier = BalldontlieTier(settings.nba_data_tier)
    declaration = declaration_for(provider, balldontlie_tier=tier)
    if provider == PROVIDER_MLB_STATSAPI:
        client: Any = MlbStatsApiClient(base_url=settings.mlb_stats_api_base_url)
        probes = build_mlb_statsapi_probes(client)
    elif provider == PROVIDER_BALLDONTLIE:
        client = BalldontlieClient(settings.nba_data_api_key)
        probes = build_balldontlie_probes(client)
    elif provider == PROVIDER_NWS:
        client = NwsClient(base_url=settings.nws_base_url)
        probes = build_nws_probes(client)
    elif provider == PROVIDER_OPEN_METEO:
        client = OpenMeteoClient(base_url=settings.open_meteo_base_url)
        probes = build_open_meteo_probes(client)
    else:  # pragma: no cover - guarded by the argparse choices
        raise ValueError(f"unsupported audit provider {provider!r}")
    return probes, client, declaration


def _report_audit(result: ProviderAuditResult, out: Printer, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "command": "provider-audit",
            "provider": result.provider,
            "tier": result.tier,
            "dry_run": result.dry_run,
            "status": result.status,
            "run_id": result.run_id,
            "requests_made": result.requests_made,
            "authenticated": result.authenticated,
            "auth_applicable": result.auth_applicable,
            "active_failure": result.has_active_failure,
            "active_failures": result.active_failures,
            "probes_attempted": result.probes_attempted,
            "probes_succeeded": result.probes_succeeded,
            "probes_skipped": result.probes_skipped,
            "tier_restricted": result.tier_restricted,
            "capabilities_recorded": result.capabilities_recorded,
            "observed_count": result.observed_count,
            "declared_only_count": result.declared_only_count,
            "issues_recorded": result.issues_recorded,
            "capabilities": [
                {
                    "capability": o.capability,
                    "state": o.state,
                    "is_observed": o.is_observed,
                    "observed_state": o.observed_state,
                    "declared_state": o.declared_state,
                    "probe_name": o.probe_name,
                    "endpoint": o.endpoint,
                    "http_status": o.http_status,
                    "error_kind": o.error_kind,
                }
                for o in result.observations
            ],
            "error_type": result.error_type,
            "error_message": result.error_message,
        }
        out(json.dumps(payload, sort_keys=True))
        return
    prefix = "[DRY-RUN] " if result.dry_run else ""
    out(f"{prefix}provider-audit --provider {result.provider} (tier={result.tier})")
    if result.status == "failed":
        out(f"[FAILED ] {result.error_type}: {result.error_message}")
        return
    auth_display = "n/a" if not result.auth_applicable else str(result.authenticated)
    out(f"  requests: {result.requests_made} (GET-only, one probe per group)")
    out(
        f"  probes: {result.probes_succeeded} succeeded, "
        f"{result.probes_skipped} skipped, {result.active_failures} active failure(s)"
    )
    out(f"  authenticated: {auth_display}  tier-restricted: {result.tier_restricted}")
    out(
        f"  capabilities recorded: {result.capabilities_recorded} "
        f"(observed: {result.observed_count}, declared-only: {result.declared_only_count})  "
        f"data-quality notes: {result.issues_recorded}"
    )
    for obs in result.observations:
        if obs.is_observed:
            marker = f"observed via {obs.probe_name} -> {obs.http_status}"
        else:
            marker = "declared-only" + (f" (probe {obs.probe_name} inconclusive)" if obs.probe_name else "")
        out(f"    - {obs.capability}: {obs.state}  [{marker}]")
    if result.status == "partially_failed":
        out(
            f"[PARTIAL] partially_failed -- an active failure occurred "
            f"({result.error_type}: {result.error_message})"
        )
    else:
        out("[OK     ] succeeded")


def run_provider_audit(
    settings: Optional[Settings] = None,
    *,
    provider: str,
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: Printer = print,
    probes: Optional[list[CapabilityProbe]] = None,
    declaration: Any = None,
    client_to_close: Any = None,
) -> int:
    """Audit one provider's capabilities/tier. GET-only; ``--dry-run`` persists nothing."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    path = database_path if database_path is not None else settings.resolved_database_path()
    if not dry_run:
        code = _db_ready_or_exit(path, "provider_capabilities", out)
        if code is not None:
            return code
    database = Database(path)

    owns_client = probes is None
    if probes is None:
        probes, client_to_close, declaration = _make_audit_probes(provider, settings)
    elif declaration is None:
        declaration = declaration_for(
            provider, balldontlie_tier=BalldontlieTier(settings.nba_data_tier)
        )

    async def _run() -> ProviderAuditResult:
        try:
            return await audit_provider(
                database=database,
                provider=provider,
                probes=probes,
                declaration=declaration,
                dry_run=dry_run,
            )
        finally:
            if owns_client and client_to_close is not None:
                await client_to_close.aclose()

    result = asyncio.run(_run())
    _report_audit(result, out, as_json=as_json)
    # Exit 1 for any genuine active failure -- a failed run OR a partially-failed
    # one where some probes succeeded but another actively failed.
    return EXIT_ACTIVE_FAILURE if result.needs_failure_exit else 0


def _report_venues(result: VenueIngestResult, out: Printer, *, as_json: bool) -> None:
    if as_json:
        out(
            json.dumps(
                {
                    "command": "ingest-venues",
                    "dry_run": result.dry_run,
                    "status": result.status,
                    "run_id": result.run_id,
                    "requests_made": result.requests_made,
                    "venues_seen": result.venues_seen,
                    "venues_inserted": result.venues_inserted,
                    "venues_updated": result.venues_updated,
                    "venues_unchanged": result.venues_unchanged,
                    "venues_rejected": result.venues_rejected,
                    "aliases_inserted": result.aliases_inserted,
                    "aliases_unchanged": result.aliases_unchanged,
                    "aliases_conflict": result.aliases_conflict,
                    "error_type": result.error_type,
                    "error_message": result.error_message,
                },
                sort_keys=True,
            )
        )
        return
    prefix = "[DRY-RUN] " if result.dry_run else ""
    label = "" if result.dry_run else f" (run {result.run_id})"
    out(f"{prefix}ingest-venues{label}")
    if result.status == "failed":
        out(f"[FAILED ] {result.error_type}: {result.error_message}")
        return
    out(f"  requests: {result.requests_made} (MLB StatsAPI, GET-only, no key)")
    out(
        f"  venues: {result.venues_seen} seen "
        f"(new {result.venues_inserted}, updated {result.venues_updated}, "
        f"unchanged {result.venues_unchanged}, rejected {result.venues_rejected})"
    )
    if not result.dry_run:
        out(
            f"  aliases: {result.aliases_inserted} inserted, "
            f"{result.aliases_unchanged} unchanged, {result.aliases_conflict} conflict"
        )
    if result.rejections:
        out(f"  rejection reasons: {', '.join(sorted(set(result.rejections)))}")
    label2 = {"succeeded": "OK     ", "partially_succeeded": "PARTIAL"}.get(
        result.status, result.status.upper()
    )
    out(f"[{label2}] {result.status}")


def run_ingest_venues(
    settings: Optional[Settings] = None,
    *,
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: Printer = print,
    client: Optional[MlbStatsApiClient] = None,
) -> int:
    """Seed venues from MLB StatsAPI. GET-only, no key; ``--dry-run`` persists nothing."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    path = database_path if database_path is not None else settings.resolved_database_path()
    if not dry_run:
        code = _db_ready_or_exit(path, "venues", out)
        if code is not None:
            return code
    database = Database(path)

    owns_client = client is None

    async def _run() -> VenueIngestResult:
        mlb = client if client is not None else MlbStatsApiClient(
            base_url=settings.mlb_stats_api_base_url
        )
        try:
            return await ingest_venues(database=database, client=mlb, dry_run=dry_run)
        finally:
            if owns_client:
                await mlb.aclose()

    result = asyncio.run(_run())
    _report_venues(result, out, as_json=as_json)
    return EXIT_ACTIVE_FAILURE if result.failed else 0


# --------------------------------------------------------------------------- #
# Phase D2: ingest-mlb / ingest-lineups
# --------------------------------------------------------------------------- #
def _mlb_json(result: MlbIngestResult) -> dict[str, Any]:
    return {
        "command": result.command,
        "dry_run": result.dry_run,
        "status": result.status,
        "run_id": result.run_id,
        "requests_made": result.requests_made,
        "raw_responses_received": result.raw_responses_received,
        "games_received": result.games_received,
        "games_inserted": result.games_inserted,
        "games_unchanged": result.games_unchanged,
        "schedule_snapshots_inserted": result.schedule_snapshots_inserted,
        "schedule_snapshots_unchanged": result.schedule_snapshots_unchanged,
        "result_snapshots_inserted": result.result_snapshots_inserted,
        "team_statistics_inserted": result.team_statistics_inserted,
        "player_statistics_inserted": result.player_statistics_inserted,
        "inning_lines_inserted": result.inning_lines_inserted,
        "roster_observations_inserted": result.roster_observations_inserted,
        "probable_pitchers_inserted": result.probable_pitchers_inserted,
        "lineups_inserted": result.lineups_inserted,
        "lineup_players_inserted": result.lineup_players_inserted,
        "corrections_appended": result.corrections_appended,
        "records_rejected": result.records_rejected,
        "data_quality_issues": result.data_quality_issues,
        "capabilities_unavailable": result.capabilities_unavailable,
        "error_type": result.error_type,
        "error_message": result.error_message,
    }


def _report_mlb(result: MlbIngestResult, out: Printer, *, as_json: bool) -> None:
    if as_json:
        out(json.dumps(_mlb_json(result), sort_keys=True))
        return
    prefix = "[DRY-RUN] " if result.dry_run else ""
    label = "" if result.dry_run else f" (run {result.run_id})"
    out(f"{prefix}{result.command}{label}")
    if result.status == "failed":
        out(f"[FAILED ] {result.error_type}: {result.error_message}")
        return
    out(f"  requests: {result.requests_made} (MLB StatsAPI, GET-only, no key)")
    out(
        f"  games: {result.games_received} received "
        f"(refs new {result.games_inserted}, unchanged {result.games_unchanged})"
    )
    out(
        f"  schedule: {result.schedule_snapshots_inserted} new / "
        f"{result.schedule_snapshots_unchanged} unchanged; "
        f"results {result.result_snapshots_inserted}, innings {result.inning_lines_inserted}"
    )
    out(
        f"  stats: team {result.team_statistics_inserted}, player {result.player_statistics_inserted}; "
        f"probables {result.probable_pitchers_inserted}; "
        f"lineups {result.lineups_inserted} ({result.lineup_players_inserted} players)"
    )
    out(
        f"  rejected {result.records_rejected}, data-quality {result.data_quality_issues}, "
        f"capabilities-unavailable {result.capabilities_unavailable}"
    )
    label2 = {"succeeded": "OK     ", "partially_succeeded": "PARTIAL"}.get(
        result.status, result.status.upper()
    )
    out(f"[{label2}] {result.status}")


def run_ingest_mlb(
    settings: Optional[Settings] = None,
    *,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    game_pk: Optional[int] = None,
    includes: tuple[str, ...] = (),
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: Printer = print,
    client: Optional[MlbStatsApiClient] = None,
) -> int:
    """Ingest MLB official data. GET-only, no key; ``--dry-run`` persists nothing."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    # A game-pk cannot be combined with a date window; validate before any work.
    if game_pk is not None and (from_date is not None or to_date is not None):
        out("[FAILED ] --game-pk cannot be combined with --from/--to")
        return EXIT_ACTIVE_FAILURE
    if (to_date is not None) and (from_date is None):
        out("[FAILED ] --to requires --from")
        return EXIT_ACTIVE_FAILURE

    path = database_path if database_path is not None else settings.resolved_database_path()
    if not dry_run:
        code = _db_ready_or_exit(path, "game_schedule_snapshots", out)
        if code is not None:
            return code
    database = Database(path)

    owns_client = client is None

    async def _run() -> MlbIngestResult:
        mlb = client if client is not None else MlbStatsApiClient(
            base_url=settings.mlb_stats_api_base_url
        )
        try:
            return await ingest_mlb(
                database=database, client=mlb, from_date=from_date, to_date=to_date,
                game_pk=game_pk, includes=includes, dry_run=dry_run,
            )
        finally:
            if owns_client:
                await mlb.aclose()

    result = asyncio.run(_run())
    _report_mlb(result, out, as_json=as_json)
    return EXIT_ACTIVE_FAILURE if result.failed else 0


def run_ingest_lineups(
    settings: Optional[Settings] = None,
    *,
    sport: str = "mlb",
    date: Optional[str] = None,
    game_pk: Optional[int] = None,
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: Printer = print,
    client: Optional[MlbStatsApiClient] = None,
) -> int:
    """Ingest posted lineups. Only ``--sport mlb`` is supported in D2."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    if sport != "mlb":
        out(f"[FAILED ] ingest-lineups supports --sport mlb only (got {sport!r})")
        return EXIT_ACTIVE_FAILURE
    if game_pk is not None and date is not None:
        out("[FAILED ] --game-pk cannot be combined with --date")
        return EXIT_ACTIVE_FAILURE

    path = database_path if database_path is not None else settings.resolved_database_path()
    if not dry_run:
        code = _db_ready_or_exit(path, "lineup_snapshots", out)
        if code is not None:
            return code
    database = Database(path)

    owns_client = client is None

    async def _run() -> MlbIngestResult:
        mlb = client if client is not None else MlbStatsApiClient(
            base_url=settings.mlb_stats_api_base_url
        )
        try:
            return await ingest_lineups(
                database=database, client=mlb, date=date, game_pk=game_pk, dry_run=dry_run,
            )
        finally:
            if owns_client:
                await mlb.aclose()

    result = asyncio.run(_run())
    _report_mlb(result, out, as_json=as_json)
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

    audit = sub.add_parser(
        "provider-audit",
        help="Audit a Phase D provider's capabilities/tier (GET-only, non-destructive)",
    )
    audit.add_argument(
        "--provider", required=True, choices=sorted(SUPPORTED_AUDIT_PROVIDERS),
        help="Provider to audit",
    )
    audit.add_argument("--db", dest="database_path", type=Path, default=None, metavar="PATH")
    audit.add_argument("--dry-run", action="store_true", help="Probe in memory; persist nothing")
    audit.add_argument("--json", dest="as_json", action="store_true", help="Machine-readable output")

    venues = sub.add_parser(
        "ingest-venues", help="Seed venues from MLB StatsAPI (GET-only, no key)"
    )
    venues.add_argument("--db", dest="database_path", type=Path, default=None, metavar="PATH")
    venues.add_argument(
        "--dry-run", action="store_true", help="Fetch + normalize but persist nothing"
    )
    venues.add_argument(
        "--json", dest="as_json", action="store_true", help="Machine-readable output"
    )

    mlb = sub.add_parser(
        "ingest-mlb", help="Ingest MLB official schedule + optional per-game data (GET-only)"
    )
    mlb.add_argument("--from", dest="from_date", default=None, metavar="YYYY-MM-DD")
    mlb.add_argument("--to", dest="to_date", default=None, metavar="YYYY-MM-DD")
    mlb.add_argument("--game-pk", dest="game_pk", type=int, default=None, metavar="PK")
    mlb.add_argument(
        "--include", dest="includes", action="append", choices=list(VALID_INCLUDES),
        default=[], help="Optional per-game group (repeatable): "
        + ", ".join(VALID_INCLUDES),
    )
    mlb.add_argument("--db", dest="database_path", type=Path, default=None, metavar="PATH")
    mlb.add_argument("--dry-run", action="store_true", help="Fetch + normalize but persist nothing")
    mlb.add_argument("--json", dest="as_json", action="store_true", help="Machine-readable output")

    lineups = sub.add_parser(
        "ingest-lineups", help="Ingest posted lineups for a date or game (GET-only)"
    )
    lineups.add_argument("--sport", default="mlb", choices=["mlb"], help="Sport (mlb only in D2)")
    lineups.add_argument("--date", dest="date", default=None, metavar="YYYY-MM-DD")
    lineups.add_argument("--game-pk", dest="game_pk", type=int, default=None, metavar="PK")
    lineups.add_argument("--db", dest="database_path", type=Path, default=None, metavar="PATH")
    lineups.add_argument("--dry-run", action="store_true", help="Fetch + normalize but persist nothing")
    lineups.add_argument("--json", dest="as_json", action="store_true", help="Machine-readable output")

    args = parser.parse_args(argv)

    if args.command == "provider-audit":
        try:
            return run_provider_audit(
                provider=args.provider,
                database_path=args.database_path,
                dry_run=args.dry_run,
                as_json=args.as_json,
            )
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    if args.command == "ingest-venues":
        try:
            return run_ingest_venues(
                database_path=args.database_path,
                dry_run=args.dry_run,
                as_json=args.as_json,
            )
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    if args.command == "ingest-mlb":
        try:
            return run_ingest_mlb(
                from_date=args.from_date,
                to_date=args.to_date,
                game_pk=args.game_pk,
                includes=tuple(dict.fromkeys(args.includes)),
                database_path=args.database_path,
                dry_run=args.dry_run,
                as_json=args.as_json,
            )
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    if args.command == "ingest-lineups":
        try:
            return run_ingest_lineups(
                sport=args.sport,
                date=args.date,
                game_pk=args.game_pk,
                database_path=args.database_path,
                dry_run=args.dry_run,
                as_json=args.as_json,
            )
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

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
