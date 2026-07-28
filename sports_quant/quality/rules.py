"""Offline, read-only point-in-time corpus quality rules (Phase E2).

Each rule PROVES a defect from existing append-only history (never fabricates a
finding merely because optional data is absent) and emits typed
:class:`~sports_quant.quality.report.Finding` objects. Rules are pure reads: no
network, no mutation, no ingestion. Severity policy:

* ``blocking`` -- a proven leakage / determinism defect that makes affected rows
  untrustworthy (a result knowable pregame; equal-time conflicting content).
* ``issue`` -- a material quality deficiency (e.g. a current forecast whose PIT
  eligibility is unprovable, so it cannot be a feature).
* ``note`` -- a transparent limitation / missing optional coverage (a game with no
  provable final label; a game lacking an official identity to attach observations).

Findings are report-only: they are NEVER upserted into ``data_quality_issues``
(that table's core columns are immutable and it has no ``(rule_code, entity)``
uniqueness constraint for a race-safe idempotent upsert under schema v16). Repeated
runs therefore create no duplicates and mutate nothing. Pre-existing OPEN
``data_quality_issues`` rows can be surfaced separately (``source='open_dq_issue'``)
for context and never affect the E2 grade.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from ..pit.asof import AsOfAmbiguityError, AsOfReader
from ..pit.dataset import _feature_cutoff
from ..pit.models import Cutoff
from ..pit.registry import TABLE_REGISTRY, TableClass
from .report import Finding, Severity

__all__ = ["run_rules", "open_dq_findings", "conflict_scan_tables"]

_LABEL_HORIZON = Cutoff.parse("9999-12-31T23:59:59.000000Z")
_LEAGUE_CODES = {"mlb": "MLB", "nba": "NBA"}
_UNIQUE_RE = re.compile(r"UNIQUE\s*\(([^)]*)\)", re.IGNORECASE)


def conflict_scan_tables(conn: sqlite3.Connection) -> list[tuple[str, tuple[str, ...]]]:
    """Every append-only observation table to scan for equal-time conflicts, DERIVED
    from the registry + schema so a newly-added append-only table cannot silently
    escape the audit. A table qualifies when it is ``asof_filtered`` with an
    ``observed_at`` transaction column, a stable id, and a ``content_hash`` tie key.
    Its anchor = the table's ``UNIQUE(...)`` columns minus (observed_at,
    content_hash); tables without such a UNIQUE are skipped (reported by the caller
    as a coverage gap)."""

    out: list[tuple[str, tuple[str, ...]]] = []
    for name in sorted(TABLE_REGISTRY):
        entry = TABLE_REGISTRY[name]
        if (entry.classification is not TableClass.ASOF_FILTERED or not entry.observed_at_column
                or not entry.content_column):
            continue
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone()
        if row is None or row[0] is None:
            continue
        anchor: tuple[str, ...] = ()
        for m in _UNIQUE_RE.finditer(str(row[0])):
            cols = [c.strip() for c in m.group(1).split(",")]
            if entry.observed_at_column in cols and entry.content_column in cols:
                anchor = tuple(c for c in cols
                               if c not in (entry.observed_at_column, entry.content_column))
                break
        if anchor:
            out.append((name, anchor))
    return out


def _games(conn: sqlite3.Connection, league: Optional[str]) -> list[sqlite3.Row]:
    sql = ("SELECT game_id, league_id, official_provider, official_game_key "
           "FROM games")
    params: list[object] = []
    if league is not None:
        sql += " WHERE league_id = ?"
        params.append(f"lg_{league.lower()}")
    sql += " ORDER BY game_id"
    return list(conn.execute(sql, params).fetchall())


def _result_leaks(conn: sqlite3.Connection, league: Optional[str]) -> list[Finding]:
    """DQ-PIT-001 (blocking): a FINAL result observed at/before the game's
    scheduled-start cutoff would leak the outcome into a pregame row."""

    reader = AsOfReader(conn, _LABEL_HORIZON)
    out: list[Finding] = []
    for g in _games(conn, league):
        op, ok = g["official_provider"], g["official_game_key"]
        if op is None or ok is None:
            continue
        game_id = str(g["game_id"])
        league_code = _league_of(str(g["league_id"]))
        ref = reader.game_provider_reference(game_id=game_id, official_provider=str(op),
                                            official_game_key=str(ok))
        if ref is None:
            continue
        sport = "mlb" if league_code == "MLB" else "nba"
        try:
            t_cut = _feature_cutoff(conn, ref)  # same policy as the dataset builder
        except AsOfAmbiguityError:
            continue  # reported by DQ-PIT-008
        if t_cut is None:
            continue
        table = "game_result_snapshots" if sport == "mlb" else "nba_game_results"
        leaked = conn.execute(  # noqa: S608 - table from a fixed literal set
            f"SELECT COUNT(*) FROM {table} WHERE game_ref_id = ? AND mapped_status = 'final' "
            "AND winning_side IN ('home','away') AND observed_at <= ?",
            (ref, t_cut.iso)).fetchone()[0]
        if leaked:
            out.append(Finding(
                rule_code="DQ-PIT-001", severity=Severity.BLOCKING, entity_type="game",
                entity_id=game_id, league=league_code,
                message=(f"{leaked} final result observation(s) known at/before the scheduled "
                         "start; the outcome would leak into a pregame row")))
    return out


def _equal_time_conflicts(conn: sqlite3.Connection) -> list[Finding]:
    """DQ-PIT-008 (blocking): equal-``observed_at`` rows at one anchor with distinct
    content -- append-only history the as-of layer must (and does) fail closed on;
    a corpus containing them cannot deterministically resolve those states."""

    out: list[Finding] = []
    for table, anchor in conflict_scan_tables(conn):
        cols = ", ".join(anchor)
        rows = conn.execute(  # noqa: S608 - table/anchor from registry-derived conflict_scan_tables
            f"SELECT {cols}, observed_at, COUNT(DISTINCT content_hash) AS c FROM {table} "
            f"GROUP BY {cols}, observed_at HAVING c > 1 ORDER BY {cols}, observed_at").fetchall()
        for r in rows:
            anchor_val = "|".join(str(r[c]) for c in anchor)
            out.append(Finding(
                rule_code="DQ-PIT-008", severity=Severity.BLOCKING, entity_type=table,
                entity_id=f"{anchor_val}@{r['observed_at']}", league=None,
                message=(f"{r['c']} distinct contents share observed_at={r['observed_at']} at "
                         f"anchor ({cols})=({anchor_val}) in {table}; equal-time conflict")))
    return out


def _weather_eligibility(conn: sqlite3.Connection) -> list[Finding]:
    """DQ-PIT-011 (issue): a current_forecast whose ``pit_eligible`` is unknown
    (NULL) cannot be used as a pregame feature (eligibility is never guessed)."""

    n = conn.execute(
        "SELECT COUNT(*) FROM weather_snapshots WHERE weather_kind = 'current_forecast' "
        "AND pit_eligible IS NULL").fetchone()[0]
    if not n:
        return []
    return [Finding(rule_code="DQ-PIT-011", severity=Severity.ISSUE, entity_type="weather_snapshots",
                    entity_id=None, league=None,
                    message=(f"{n} current_forecast weather row(s) with pit_eligible=NULL; "
                             "eligibility is unprovable so they are excluded from features"))]


def _unlabeled_games(conn: sqlite3.Connection, league: Optional[str]) -> list[Finding]:
    """E2-LABEL-UNAVAILABLE (note): a game with result observations but no provable
    final home/away winner (unfinished / tie / conflicting) yields no training row --
    a transparent limitation, not a defect."""

    reader = AsOfReader(conn, _LABEL_HORIZON)
    out: list[Finding] = []
    for g in _games(conn, league):
        op, ok = g["official_provider"], g["official_game_key"]
        if op is None or ok is None:
            continue
        game_id = str(g["game_id"])
        league_code = _league_of(str(g["league_id"]))
        ref = reader.game_provider_reference(game_id=game_id, official_provider=str(op),
                                            official_game_key=str(ok))
        if ref is None:
            continue
        sport = "mlb" if league_code == "MLB" else "nba"
        table = "game_result_snapshots" if sport == "mlb" else "nba_game_results"
        total = conn.execute(  # noqa: S608 - table from a fixed literal set
            f"SELECT COUNT(*) FROM {table} WHERE game_ref_id = ?", (ref,)).fetchone()[0]
        if not total:
            continue
        try:
            result = reader.game_result(ref) if sport == "mlb" else reader.nba_game_result(ref)
        except AsOfAmbiguityError:
            continue  # reported by DQ-PIT-008
        if result is None or result.get("mapped_status") != "final" \
                or result.get("winning_side") not in ("home", "away"):
            out.append(Finding(
                rule_code="E2-LABEL-UNAVAILABLE", severity=Severity.NOTE, entity_type="game",
                entity_id=game_id, league=league_code,
                message="game has result observations but no provable final home/away label"))
    return out


def _missing_identity(conn: sqlite3.Connection, league: Optional[str]) -> list[Finding]:
    """E2-IDENTITY-MISSING (note): a canonical game without an official provider
    identity cannot attach provider observations, so it produces no rows."""

    out: list[Finding] = []
    for g in _games(conn, league):
        if g["official_provider"] is None or g["official_game_key"] is None:
            out.append(Finding(
                rule_code="E2-IDENTITY-MISSING", severity=Severity.NOTE, entity_type="game",
                entity_id=str(g["game_id"]), league=_league_of(str(g["league_id"])),
                message="game lacks an official provider identity; no observations can attach"))
    return out


def run_rules(conn: sqlite3.Connection, *, league: Optional[str] = None,
              rule_code: Optional[str] = None) -> list[Finding]:
    """Run every corpus rule (read-only) and return deterministically-ordered
    findings. ``league`` ('mlb'/'nba') scopes game-attributed rules; unattributable
    table-scan findings (``league=None``) are always included. ``rule_code`` filters
    to one rule."""

    if league is not None and league.lower() not in _LEAGUE_CODES:
        raise ValueError(f"unsupported league {league!r}")
    findings: list[Finding] = []
    findings += _result_leaks(conn, league)
    findings += _equal_time_conflicts(conn)
    findings += _weather_eligibility(conn)
    findings += _unlabeled_games(conn, league)
    findings += _missing_identity(conn, league)
    if rule_code is not None:
        findings = [f for f in findings if f.rule_code == rule_code]
    return sorted(findings, key=lambda f: (-f.severity.rank, f.rule_code, f.entity_type or "",
                                           f.entity_id or ""))


def open_dq_findings(conn: sqlite3.Connection, *, league: Optional[str] = None,
                     rule_code: Optional[str] = None) -> list[Finding]:
    """Pre-existing OPEN ``data_quality_issues`` rows as context findings
    (``source='open_dq_issue'``). These are reported separately and never graded."""

    from ..db.repositories.data_quality import SqliteDataQualityRepository
    issues = SqliteDataQualityRepository(conn).find_open(rule_code=rule_code, limit=1000)
    sev = {"blocking": Severity.BLOCKING, "issue": Severity.ISSUE, "note": Severity.NOTE}
    out = [Finding(rule_code=i.rule_code, severity=sev.get(i.severity, Severity.NOTE),
                   entity_type=i.entity_type, entity_id=i.entity_id, league=None,
                   message=i.description, source="open_dq_issue") for i in issues]
    _ = league  # DQ issues carry no league_id; not league-attributable (kept as-is)
    return sorted(out, key=lambda f: (-f.severity.rank, f.rule_code, f.entity_id or ""))


def _league_of(league_id: str) -> Optional[str]:
    return {"lg_mlb": "MLB", "lg_nba": "NBA"}.get(league_id)
