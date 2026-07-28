"""``data-status``: an offline, read-only corpus status report (Phase E2).

Reports schema version, per-table row counts, observation transaction-time
coverage, latest ingestion/audit run per provider, unresolved provider
references, unmatched sportsbook/Kalshi events, pending manual reviews, and open
data-quality issues. League/since scoping is applied ONLY where a record can be
scoped honestly; everything else is reported globally with an explicit
"not attributable" note. No secrets, credentials, or raw payloads are emitted.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .report_access import EXIT_OK, Printer, with_readonly_corpus

# Observation tables carrying an `observed_at` transaction-time column.
_OBSERVATION_TABLES: tuple[str, ...] = (
    "game_status_history", "game_schedule_snapshots", "game_result_snapshots",
    "mlb_inning_lines", "team_game_statistics", "player_game_statistics", "roster_snapshots",
    "probable_pitcher_snapshots", "lineup_snapshots", "nba_quarter_lines", "injury_snapshots",
    "play_snapshots", "nba_game_results", "nba_team_statistics", "nba_player_statistics",
    "weather_snapshots", "sportsbook_price_snapshots", "kalshi_orderbook_snapshots",
    "kalshi_public_trades",
)
_KALSHI_SERIES_BY_LEAGUE = {"MLB": "KXMLBGAME", "NBA": "KXNBAGAME"}


def _all_tables(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def build_status_report(
    conn: sqlite3.Connection, *, league: Optional[str] = None, since: Optional[str] = None,
) -> dict[str, Any]:
    """Build the deterministic status report dict (documented, machine-readable)."""

    league_code = league.upper() if league else None
    league_id = f"lg_{league_code.lower()}" if league_code else None
    notes: list[str] = []

    table_row_counts = {t: _count(conn, f"SELECT COUNT(*) FROM {t}")  # noqa: S608 - names from sqlite_master
                        for t in _all_tables(conn)}

    observation_coverage: dict[str, dict[str, Any]] = {}
    for t in _OBSERVATION_TABLES:
        row = conn.execute(  # noqa: S608 - t from the fixed _OBSERVATION_TABLES tuple
            f"SELECT COUNT(*), MIN(observed_at), MAX(observed_at) FROM {t}").fetchone()
        observation_coverage[t] = {"rows": int(row[0]), "earliest_observed_at": row[1],
                                   "latest_observed_at": row[2]}
    if league_code or since:
        notes.append("observation_coverage is global; per-league/since scoping of "
                     "provider-keyed observations is not attributable without a match decision")

    # Latest ingestion/audit run per provider (global; ingestion_runs has no league).
    provider_runs: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT provider, status, command, operation, sport, started_at FROM ingestion_runs "
        "ORDER BY provider, started_at DESC, run_id DESC"
    ).fetchall():
        prov = str(r["provider"])
        if prov not in provider_runs:  # first per provider = latest (ordered DESC)
            provider_runs[prov] = {"status": r["status"], "command": r["command"],
                                   "operation": r["operation"], "sport": r["sport"],
                                   "started_at": r["started_at"]}
    if league_code:
        notes.append("provider run status is provider-scoped; ingestion_runs carries no league")

    unresolved_references = {
        "game": _count(conn, "SELECT COUNT(*) FROM provider_game_references WHERE game_id IS NULL"),
        "team": _count(conn, "SELECT COUNT(*) FROM provider_team_references WHERE team_id IS NULL"),
        "player": _count(conn,
                         "SELECT COUNT(*) FROM provider_player_references WHERE player_id IS NULL"),
    }
    if league_code:
        notes.append("unresolved provider references are not league-attributable (no league until "
                     "matched)")

    sb_sql = "SELECT COUNT(*) FROM sportsbook_events WHERE game_id IS NULL"
    sb_params: tuple[Any, ...] = ()
    if league_id:
        sb_sql += " AND league_id = ?"
        sb_params = (league_id,)
    if since:
        sb_sql += " AND commence_time >= ?"
        sb_params = (*sb_params, since)
    unmatched_sportsbook_events = _count(conn, sb_sql, sb_params)

    kalshi_series = ([_KALSHI_SERIES_BY_LEAGUE[league_code]] if league_code
                     else list(_KALSHI_SERIES_BY_LEAGUE.values()))
    placeholders = ", ".join("?" for _ in kalshi_series)
    unmatched_kalshi = {
        "events": _count(conn,  # noqa: S608 - placeholders are bound params
            f"SELECT COUNT(*) FROM kalshi_events WHERE game_id IS NULL AND series_ticker IN "
            f"({placeholders})", tuple(kalshi_series)),
        "markets": _count(conn,  # noqa: S608
            f"SELECT COUNT(*) FROM kalshi_markets WHERE game_id IS NULL AND series_ticker IN "
            f"({placeholders})", tuple(kalshi_series)),
    }

    pending_manual_review = _count(
        conn, "SELECT COUNT(*) FROM entity_match_decisions WHERE needs_manual_review = 1")
    if league_code:
        notes.append("pending manual reviews are not league-attributable")

    open_dq = {sev: _count(
        conn, "SELECT COUNT(*) FROM data_quality_issues WHERE resolved_at IS NULL AND severity = ?",
        (sev,)) for sev in ("blocking", "issue", "note")}
    if league_code:
        notes.append("open data-quality issues carry no league_id; reported globally")

    games_sql = "SELECT COUNT(*) FROM games"
    games_params: tuple[Any, ...] = ()
    conds = []
    if league_id:
        conds.append("league_id = ?")
        games_params = (*games_params, league_id)
    if since:
        conds.append("game_date_local >= ?")
        games_params = (*games_params, since)
    if conds:
        games_sql += " WHERE " + " AND ".join(conds)

    return {
        "command": "data-status",
        "schema_version": 16,
        "league": league_code,
        "since": since,
        "canonical_games": _count(conn, games_sql, games_params),
        "table_row_counts": table_row_counts,
        "observation_coverage": observation_coverage,
        "provider_runs": provider_runs,
        "unresolved_references": unresolved_references,
        "unmatched_sportsbook_events": unmatched_sportsbook_events,
        "unmatched_kalshi": unmatched_kalshi,
        "pending_manual_review": pending_manual_review,
        "open_data_quality": open_dq,
        "scope_notes": notes,
    }


def _emit(report: dict[str, Any], *, as_json: bool, out: Printer) -> None:
    if as_json:
        out(json.dumps(report, sort_keys=True))
        return
    out(f"data-status  schema v{report['schema_version']}  "
        f"league={report['league'] or 'ALL'}  since={report['since'] or 'ALL'}")
    out(f"  canonical games: {report['canonical_games']}")
    out(f"  unmatched sportsbook events: {report['unmatched_sportsbook_events']}")
    out(f"  unmatched kalshi: events={report['unmatched_kalshi']['events']} "
        f"markets={report['unmatched_kalshi']['markets']}")
    ur = report["unresolved_references"]
    out(f"  unresolved refs: game={ur['game']} team={ur['team']} player={ur['player']}")
    out(f"  pending manual review: {report['pending_manual_review']}")
    dq = report["open_data_quality"]
    out(f"  open DQ: blocking={dq['blocking']} issue={dq['issue']} note={dq['note']}")
    for note in report["scope_notes"]:
        out(f"  note: {note}")


def run_data_status(
    *, league: Optional[str] = None, since: Optional[str] = None,
    database_path: Optional[Path] = None, as_json: bool = False, out: Printer = print,
) -> int:
    """Produce the status report. Exit 0 when produced (any corpus quality);
    exit 3 for a missing/unmigrated/corrupt/unsupported database."""

    def work(conn: sqlite3.Connection) -> int:
        report = build_status_report(conn, league=league, since=since)
        _emit(report, as_json=as_json, out=out)
        return EXIT_OK

    return with_readonly_corpus(database_path, out, work)
