"""``data-quality``: offline point-in-time corpus quality report (Phase E2).

Runs the E2 corpus rules read-only, grades them (A-F / ``execution_valid`` /
severity counts / by-rule / deterministic examples), reports pre-existing OPEN
``data_quality_issues`` SEPARATELY (never conflated with, and never affecting, the
E2 grade), and -- with ``--review`` -- groups pending manual reviews. Findings are
NOT persisted (no duplicate rows on repeated runs; the corpus is never mutated).

Exit: ``0`` when no E2 finding at or above ``--fail-on`` exists; ``1`` when the
threshold is met/exceeded; ``3`` for a missing/unmigrated/corrupt/unsupported db.
Default threshold is ``blocking``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from ..report_access import EXIT_OK, EXIT_THRESHOLD, Printer, with_readonly_corpus
from .report import Severity, grade_findings
from .rules import open_dq_findings, run_rules

__all__ = ["run_data_quality", "build_quality_payload"]


def _review_groups(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT entity_type, COUNT(*) AS c FROM entity_match_decisions "
        "WHERE needs_manual_review = 1 GROUP BY entity_type ORDER BY entity_type").fetchall()
    return {str(r["entity_type"]): int(r["c"]) for r in rows}


def build_quality_payload(
    conn: sqlite3.Connection, *, league: Optional[str] = None, rule_code: Optional[str] = None,
    review: bool = False,
) -> dict[str, Any]:
    """The deterministic data-quality report payload."""

    findings = run_rules(conn, league=league, rule_code=rule_code)
    report = grade_findings(findings)
    opened = open_dq_findings(conn, league=league, rule_code=rule_code)
    payload: dict[str, Any] = {
        "command": "data-quality",
        "league": league.upper() if league else None,
        "rule": rule_code,
        "grade": report.grade,
        "execution_valid": report.execution_valid,
        "score": round(report.score, 4),
        "counts": report.as_dict()["counts"],
        "by_rule": report.as_dict()["by_rule"],
        "examples": report.as_dict()["examples"],
        "e2_findings": [f.as_dict() for f in findings],
        "open_data_quality_issues": [f.as_dict() for f in opened],
    }
    if review:
        payload["pending_manual_review"] = _review_groups(conn)
    return payload


def _emit(payload: dict[str, Any], *, as_json: bool, out: Printer) -> None:
    if as_json:
        out(json.dumps(payload, sort_keys=True))
        return
    valid = "PIT-VALID" if payload["execution_valid"] else "NOT PIT-VALID"
    c = payload["counts"]
    out(f"data-quality  {payload['grade']}  [{valid}]  score={payload['score']:.2f}  "
        f"league={payload['league'] or 'ALL'}")
    out(f"  findings: blocking={c['blocking']} issue={c['issue']} note={c['note']}")
    for rule in sorted(payload["by_rule"]):
        out(f"    {rule}: {payload['by_rule'][rule]}")
    out(f"  pre-existing open data_quality_issues: {len(payload['open_data_quality_issues'])} "
        "(context only; not graded)")
    if "pending_manual_review" in payload:
        groups = payload["pending_manual_review"]
        out(f"  pending manual review: {sum(groups.values())} "
            f"({', '.join(f'{k}={v}' for k, v in sorted(groups.items())) or 'none'})")


def run_data_quality(
    *, league: Optional[str] = None, rule_code: Optional[str] = None, review: bool = False,
    fail_on: str = "blocking", database_path: Optional[Path] = None, as_json: bool = False,
    out: Printer = print,
) -> int:
    threshold = Severity(fail_on).rank

    def work(conn: sqlite3.Connection) -> int:
        payload = build_quality_payload(conn, league=league, rule_code=rule_code, review=review)
        _emit(payload, as_json=as_json, out=out)
        breached = any(Severity(f["severity"]).rank >= threshold for f in payload["e2_findings"])
        return EXIT_THRESHOLD if breached else EXIT_OK

    return with_readonly_corpus(database_path, out, work)
