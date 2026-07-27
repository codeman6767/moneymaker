"""CLI entry points for D5A matching: ``match-games`` and ``matching-review``.

Both operate only on rows already in the database -- no network request is ever
made. ``match-games`` resolves teams, venues and official games for a bounded
provider/date/game filter and records complete decisions; ``--dry-run`` persists
nothing. ``matching-review`` safely lists unresolved / ambiguous decisions.

Exit codes match the project convention:
* ``0`` -- ran cleanly; unresolved/ambiguous rows are honestly recorded;
* ``1`` -- an active validation/persistence defect, or a blocking orientation /
  identity contradiction (the corpus is unfit);
* ``2`` -- a read-only startup invariant failed (raised to the caller);
* ``3`` -- the database is missing, unmigrated, or invalid.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from ..config import Settings, load_settings
from ..db.engine import Database, table_exists, transaction
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.matching import SqliteMatchingRepository
from .kalshi import MatchKalshiResult, MatchKalshiService, series_ticker_for_sport
from .model import MATCHER_VERSION
from .players_service import MatchPlayersResult, MatchPlayersService
from .service import MatchGamesResult, MatchGamesService, resolve_provider_for_sport
from .sportsbook import MatchSportsbookResult, MatchSportsbookService, sport_key_for_arg

Printer = Callable[[str], None]

EXIT_ACTIVE_FAILURE = 1
EXIT_DATABASE_ERROR = 3

_GUARD_TABLE = "entity_match_decisions"


def _db_ready(path: Path, out: Printer) -> Optional[int]:
    if not path.exists():
        out(f"[FAILED ] database not found at {path}; run 'python -m sports_quant db-init'")
        return EXIT_DATABASE_ERROR
    with Database(path).connection() as conn:
        if not table_exists(conn, _GUARD_TABLE):
            out(
                f"[FAILED ] database at {path} is not migrated for Phase D; "
                "run 'python -m sports_quant db-init'"
            )
            return EXIT_DATABASE_ERROR
    return None


def run_match_games(
    settings: Optional[Settings] = None,
    *,
    source: str = "official",
    sport: Optional[str] = None,
    provider: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    provider_game_id: Optional[str] = None,
    provider_event_id: Optional[str] = None,
    unmatched_only: bool = False,
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: Printer = print,
) -> int:
    """Resolve official games, or (``--source sportsbook``) sportsbook events, locally."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    if source == "sportsbook":
        return _run_sportsbook(
            settings, sport=sport, from_date=from_date, to_date=to_date,
            provider_event_id=provider_event_id, unmatched_only=unmatched_only,
            database_path=database_path, dry_run=dry_run, as_json=as_json, out=out,
        )

    resolved_provider = provider or (resolve_provider_for_sport(sport) if sport else None)
    if resolved_provider is None:
        out("[FAILED ] one of --sport {mlb,nba} or --provider is required")
        return EXIT_ACTIVE_FAILURE
    if provider_game_id is None and from_date is None:
        out("[FAILED ] one of --from (with optional --to) or --provider-game-id is required")
        return EXIT_ACTIVE_FAILURE
    effective_to = to_date if to_date is not None else from_date

    path = database_path if database_path is not None else settings.resolved_database_path()
    code = _db_ready(path, out)
    if code is not None:
        return code

    database = Database(path)
    with database.connection() as conn:
        if dry_run:
            service = MatchGamesService(conn, dry_run=True)
            result = service.match_range(
                provider=resolved_provider, from_date=from_date, to_date=effective_to,
                provider_game_id=provider_game_id,
            )
        else:
            try:
                result = _run_persisted(
                    conn, resolved_provider, from_date, effective_to, provider_game_id
                )
            except Exception as exc:  # noqa: BLE001 - surface as an active failure, roll back
                out(f"[FAILED ] {type(exc).__name__}: {exc}")
                return EXIT_ACTIVE_FAILURE

    _report(result, resolved_provider, out, as_json=as_json)
    return EXIT_ACTIVE_FAILURE if result.needs_failure_exit else 0


def _run_persisted(
    conn,  # type: ignore[no-untyped-def]
    provider: str,
    from_date: Optional[str],
    to_date: Optional[str],
    provider_game_id: Optional[str],
) -> MatchGamesResult:
    runs = SqliteIngestionRunRepository(conn)
    started = time.monotonic_ns()
    with transaction(conn):
        run = runs.start(
            command="match-games", provider=provider, operation="match",
            args_json=json.dumps(
                {"from": from_date, "to": to_date, "provider_game_id": provider_game_id},
                sort_keys=True,
            ),
            started_monotonic_ns=started, tool_version=MATCHER_VERSION,
        )
        service = MatchGamesService(conn, dry_run=False, run_id=run.run_id)
        result = service.match_range(
            provider=provider, from_date=from_date, to_date=to_date,
            provider_game_id=provider_game_id,
        )
        runs.complete(
            run.run_id,
            status="partially_succeeded" if result.needs_failure_exit else "succeeded",
            duration_ns=time.monotonic_ns() - started,
            records_inserted=result.counters.canonical_games_created,
            records_updated=result.counters.provider_references_linked,
        )
        result.run_id = run.run_id
    return result


def _report(result: MatchGamesResult, provider: str, out: Printer, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "command": "match-games",
            "provider": provider,
            "dry_run": result.dry_run,
            "status": result.status,
            "run_id": result.run_id,
            "games_considered": result.games_considered,
            **result.counters.as_dict(),
        }
        out(json.dumps(payload, sort_keys=True))
        return
    prefix = "[DRY-RUN] " if result.dry_run else ""
    label = "" if result.dry_run else f" (run {result.run_id})"
    c = result.counters
    out(f"{prefix}match-games [{provider}]{label}: {result.games_considered} games considered")
    out(
        f"  decisions: {c.decisions_evaluated} "
        f"(accepted {c.accepted}, ambiguous {c.ambiguous}, "
        f"no-candidate {c.no_candidate}, rejected {c.rejected})"
    )
    out(
        f"  candidates: {c.candidates_recorded}; review required: {c.manual_review_required}; "
        f"references linked: {c.provider_references_linked}"
    )
    out(
        f"  canonical games created: {c.canonical_games_created}; "
        f"unchanged: {c.canonical_entities_unchanged}"
    )
    out(f"  data-quality: {c.dq_issues} issues ({c.blocking_issues} blocking)")
    status = "BLOCKED" if result.needs_failure_exit else result.status.upper()
    out(f"[{status}] {result.status}")


# --------------------------------------------------------------------------- #
# match-games --source sportsbook (D5B1)
# --------------------------------------------------------------------------- #
def _run_sportsbook(
    settings: Settings,
    *,
    sport: Optional[str],
    from_date: Optional[str],
    to_date: Optional[str],
    provider_event_id: Optional[str],
    unmatched_only: bool,
    database_path: Optional[Path],
    dry_run: bool,
    as_json: bool,
    out: Printer,
) -> int:
    sport_key = sport_key_for_arg(sport) if sport else None
    if sport is not None and sport_key is None:
        out(f"[FAILED ] unsupported --sport {sport!r} for sportsbook matching")
        return EXIT_ACTIVE_FAILURE
    effective_to = to_date if to_date is not None else from_date
    path = database_path if database_path is not None else settings.resolved_database_path()
    code = _db_ready(path, out)
    if code is not None:
        return code

    database = Database(path)
    with database.connection() as conn:
        if dry_run:
            result: MatchSportsbookResult = MatchSportsbookService(conn, dry_run=True).match_range(
                sport_key=sport_key, from_date=from_date, to_date=effective_to,
                provider_event_id=provider_event_id, unmatched_only=unmatched_only,
            )
        else:
            try:
                result = _run_sportsbook_persisted(
                    conn, sport_key, from_date, effective_to, provider_event_id, unmatched_only)
            except Exception as exc:  # noqa: BLE001 - surface as an active failure, roll back
                out(f"[FAILED ] {type(exc).__name__}: {exc}")
                return EXIT_ACTIVE_FAILURE

    _report_sportsbook(result, out, as_json=as_json)
    return EXIT_ACTIVE_FAILURE if result.needs_failure_exit else 0


def _run_sportsbook_persisted(
    conn,  # type: ignore[no-untyped-def]
    sport_key: Optional[str],
    from_date: Optional[str],
    to_date: Optional[str],
    provider_event_id: Optional[str],
    unmatched_only: bool,
) -> MatchSportsbookResult:
    runs = SqliteIngestionRunRepository(conn)
    started = time.monotonic_ns()
    with transaction(conn):
        run = runs.start(
            command="match-games", provider="the_odds_api", operation="match_sportsbook",
            args_json=json.dumps(
                {"sport_key": sport_key, "from": from_date, "to": to_date,
                 "provider_event_id": provider_event_id, "unmatched_only": unmatched_only},
                sort_keys=True),
            started_monotonic_ns=started, tool_version=MATCHER_VERSION,
        )
        result = MatchSportsbookService(conn, dry_run=False, run_id=run.run_id).match_range(
            sport_key=sport_key, from_date=from_date, to_date=to_date,
            provider_event_id=provider_event_id, unmatched_only=unmatched_only)
        runs.complete(
            run.run_id,
            status="partially_succeeded" if result.needs_failure_exit else "succeeded",
            duration_ns=time.monotonic_ns() - started,
            records_updated=result.counters.event_links_applied)
        result.run_id = run.run_id
    return result


def _report_sportsbook(
    result: MatchSportsbookResult, out: Printer, *, as_json: bool
) -> None:
    if as_json:
        out(json.dumps({
            "command": "match-games", "source": "sportsbook", "provider": "the_odds_api",
            "dry_run": result.dry_run, "status": result.status, "run_id": result.run_id,
            **result.counters.as_dict(),
        }, sort_keys=True))
        return
    c = result.counters
    prefix = "[DRY-RUN] " if result.dry_run else ""
    out(f"{prefix}match-games [sportsbook/the_odds_api]: {c.events_considered} events considered")
    out(
        f"  events: accepted {c.events_accepted} (direct {c.direct_orientation}, "
        f"swapped-review {c.swapped_review_gated}), already-linked {c.events_already_linked}, "
        f"ambiguous {c.events_ambiguous}, no-candidate {c.events_no_candidate}, "
        f"rejected {c.events_rejected}"
    )
    out(
        f"  links applied: {c.event_links_applied}; outcomes checked: {c.outcome_rows_checked} "
        f"(approved {c.outcome_roles_approved}, unknown {c.unknown_outcomes})"
    )
    out(
        f"  data-quality: {c.dq_issues} issues ({c.blocking_issues} blocking); "
        f"orientation conflicts: {c.blocking_orientation_conflicts}"
    )
    status = "BLOCKED" if result.needs_failure_exit else result.status.upper()
    out(f"[{status}] {result.status}")


def run_match_players(
    settings: Optional[Settings] = None,
    *,
    sport: Optional[str] = None,
    provider: Optional[str] = None,
    provider_player_id: Optional[str] = None,
    season: Optional[int] = None,
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: Printer = print,
) -> int:
    """Resolve unresolved provider-player references for a bounded local scope."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    resolved_provider = provider or (resolve_provider_for_sport(sport) if sport else None)
    if resolved_provider is None:
        out("[FAILED ] one of --sport {mlb,nba} or --provider is required")
        return EXIT_ACTIVE_FAILURE

    path = database_path if database_path is not None else settings.resolved_database_path()
    code = _db_ready(path, out)
    if code is not None:
        return code

    database = Database(path)
    with database.connection() as conn:
        if dry_run:
            result: MatchPlayersResult = MatchPlayersService(conn, dry_run=True).match_range(
                provider=resolved_provider, provider_player_id=provider_player_id,
                season_year=season,
            )
        else:
            try:
                result = _run_players_persisted(
                    conn, resolved_provider, provider_player_id, season
                )
            except Exception as exc:  # noqa: BLE001 - surface as an active failure, roll back
                out(f"[FAILED ] {type(exc).__name__}: {exc}")
                return EXIT_ACTIVE_FAILURE

    _report_players(result, resolved_provider, out, as_json=as_json)
    return EXIT_ACTIVE_FAILURE if result.needs_failure_exit else 0


def _run_players_persisted(
    conn,  # type: ignore[no-untyped-def]
    provider: str,
    provider_player_id: Optional[str],
    season: Optional[int],
) -> MatchPlayersResult:
    runs = SqliteIngestionRunRepository(conn)
    started = time.monotonic_ns()
    with transaction(conn):
        run = runs.start(
            command="match-players", provider=provider, operation="match",
            args_json=json.dumps(
                {"provider_player_id": provider_player_id, "season": season}, sort_keys=True),
            started_monotonic_ns=started, tool_version=MATCHER_VERSION,
        )
        service = MatchPlayersService(conn, dry_run=False, run_id=run.run_id)
        result = service.match_range(
            provider=provider, provider_player_id=provider_player_id, season_year=season)
        runs.complete(
            run.run_id,
            status="partially_succeeded" if result.needs_failure_exit else "succeeded",
            duration_ns=time.monotonic_ns() - started,
            records_updated=result.counters.provider_references_linked,
        )
        result.run_id = run.run_id
    return result


def _report_players(
    result: MatchPlayersResult, provider: str, out: Printer, *, as_json: bool
) -> None:
    if as_json:
        out(json.dumps({
            "command": "match-players", "provider": provider, "dry_run": result.dry_run,
            "status": result.status, "run_id": result.run_id,
            "references_considered": result.references_considered, **result.counters.as_dict(),
        }, sort_keys=True))
        return
    c = result.counters
    prefix = "[DRY-RUN] " if result.dry_run else ""
    out(f"{prefix}match-players [{provider}]: {result.references_considered} references considered")
    out(
        f"  decisions: {c.decisions_evaluated} (accepted {c.accepted}, ambiguous {c.ambiguous}, "
        f"no-candidate {c.no_candidate}, rejected {c.rejected}); linked {c.provider_references_linked}"
    )
    out(f"  data-quality: {c.dq_issues} issues ({c.blocking_issues} blocking)")
    status = "BLOCKED" if result.needs_failure_exit else result.status.upper()
    out(f"[{status}] {result.status}")


def run_match_markets(
    settings: Optional[Settings] = None,
    *,
    provider: str = "kalshi_public",
    sport: Optional[str] = None,
    series_ticker: Optional[str] = None,
    event_ticker: Optional[str] = None,
    market_ticker: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    unmatched_only: bool = False,
    database_path: Optional[Path] = None,
    dry_run: bool = False,
    as_json: bool = False,
    out: Printer = print,
) -> int:
    """Resolve public Kalshi events + game-winner markets to canonical games (local)."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    if provider != "kalshi_public":
        out(f"[FAILED ] unsupported --provider {provider!r} for match-markets")
        return EXIT_ACTIVE_FAILURE
    resolved_series = series_ticker or (series_ticker_for_sport(sport) if sport else None)

    path = database_path if database_path is not None else settings.resolved_database_path()
    code = _db_ready(path, out)
    if code is not None:
        return code

    with Database(path).connection() as conn:
        if dry_run:
            result: MatchKalshiResult = MatchKalshiService(conn, dry_run=True).match_range(
                series_ticker=resolved_series, event_ticker=event_ticker,
                market_ticker=market_ticker, from_date=from_date, to_date=to_date,
                unmatched_only=unmatched_only,
            )
        else:
            try:
                result = _run_kalshi_persisted(
                    conn, resolved_series, event_ticker, market_ticker, from_date, to_date,
                    unmatched_only)
            except Exception as exc:  # noqa: BLE001 - surface as an active failure, roll back
                out(f"[FAILED ] {type(exc).__name__}: {exc}")
                return EXIT_ACTIVE_FAILURE

    _report_kalshi(result, out, as_json=as_json)
    return EXIT_ACTIVE_FAILURE if result.needs_failure_exit else 0


def _run_kalshi_persisted(
    conn,  # type: ignore[no-untyped-def]
    series_ticker: Optional[str],
    event_ticker: Optional[str],
    market_ticker: Optional[str],
    from_date: Optional[str],
    to_date: Optional[str],
    unmatched_only: bool,
) -> MatchKalshiResult:
    runs = SqliteIngestionRunRepository(conn)
    started = time.monotonic_ns()
    with transaction(conn):
        run = runs.start(
            command="match-markets", provider="kalshi_public", operation="match_kalshi",
            args_json=json.dumps(
                {"series_ticker": series_ticker, "event_ticker": event_ticker,
                 "market_ticker": market_ticker, "from": from_date, "to": to_date,
                 "unmatched_only": unmatched_only}, sort_keys=True),
            started_monotonic_ns=started, tool_version=MATCHER_VERSION,
        )
        result = MatchKalshiService(conn, dry_run=False, run_id=run.run_id).match_range(
            series_ticker=series_ticker, event_ticker=event_ticker, market_ticker=market_ticker,
            from_date=from_date, to_date=to_date, unmatched_only=unmatched_only)
        runs.complete(
            run.run_id,
            status="partially_succeeded" if result.needs_failure_exit else "succeeded",
            duration_ns=time.monotonic_ns() - started,
            records_updated=result.counters.events_linked + result.counters.markets_linked)
        result.run_id = run.run_id
    return result


def _report_kalshi(result: MatchKalshiResult, out: Printer, *, as_json: bool) -> None:
    if as_json:
        out(json.dumps({
            "command": "match-markets", "provider": "kalshi_public", "dry_run": result.dry_run,
            "status": result.status, "run_id": result.run_id, **result.counters.as_dict(),
        }, sort_keys=True))
        return
    c = result.counters
    prefix = "[DRY-RUN] " if result.dry_run else ""
    out(f"{prefix}match-markets [kalshi_public]: {c.events_considered} events, "
        f"{c.markets_considered} markets considered")
    out(f"  events: accepted {c.events_accepted}, linked {c.events_linked}, already-linked "
        f"{c.events_already_linked}, ambiguous {c.events_ambiguous}, no-candidate "
        f"{c.events_no_candidate}, rejected {c.events_rejected}")
    out(f"  markets: accepted {c.markets_accepted}, linked {c.markets_linked}, yes-teams "
        f"{c.yes_teams_resolved}, ambiguous {c.markets_ambiguous}, no-candidate "
        f"{c.markets_no_candidate}, rejected {c.markets_rejected}")
    out(f"  unsupported: series {c.unsupported_series}, semantics {c.unsupported_semantics}; "
        f"rules-hash conflicts {c.rules_hash_conflicts}")
    out(f"  data-quality: {c.dq_issues} issues ({c.blocking_issues} blocking)")
    status = "BLOCKED" if result.needs_failure_exit else result.status.upper()
    out(f"[{status}] {result.status}")


def run_matching_review(
    settings: Optional[Settings] = None,
    *,
    entity_type: Optional[str] = None,
    limit: int = 100,
    database_path: Optional[Path] = None,
    as_json: bool = False,
    out: Printer = print,
) -> int:
    """List unresolved / ambiguous match decisions awaiting review (read-only)."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    path = database_path if database_path is not None else settings.resolved_database_path()
    code = _db_ready(path, out)
    if code is not None:
        return code

    database = Database(path)
    with database.connection() as conn:
        decisions = SqliteMatchingRepository(conn).list_needs_review(
            entity_type=entity_type, limit=limit
        )
    rows = [
        {
            "match_id": d.match_id,
            "entity_type": d.entity_type,
            "source_provider": d.source_provider,
            "source_ref": d.source_ref,
            "outcome": d.outcome,
            "rejection_reason": d.rejection_reason,
            "decided_at": d.decided_at,
        }
        for d in decisions
    ]
    if as_json:
        out(json.dumps({"command": "matching-review", "open_items": rows}, sort_keys=True))
        return 0
    out(f"matching-review: {len(rows)} open item(s)")
    for r in rows:
        out(
            f"  [{r['entity_type']}] {r['source_provider']}:{r['source_ref']} "
            f"-> {r['outcome']} ({r['rejection_reason'] or 'n/a'})"
        )
    return 0
