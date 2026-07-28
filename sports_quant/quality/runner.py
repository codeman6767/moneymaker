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

from ..report_access import (
    EXIT_OK,
    EXIT_THRESHOLD,
    Printer,
    pending_review_counts,
    with_readonly_corpus,
)
from .report import Severity, grade_findings
from .rules import open_dq_findings, run_rules

__all__ = ["run_data_quality", "build_quality_payload"]


def _open_dq_counts(opened: list[Any]) -> dict[str, int]:
    return {s.value: sum(1 for f in opened if f.severity is s) for s in Severity}


def build_quality_payload(
    conn: sqlite3.Connection, *, league: Optional[str] = None, rule_code: Optional[str] = None,
    review: bool = False,
) -> dict[str, Any]:
    """The deterministic data-quality report payload.

    ``execution_valid`` reflects the NEWLY-DETECTED E2 rule findings only. The
    top-level ``corpus_valid`` is stricter (RF6): the corpus is valid only when the
    E2 findings are PIT-valid AND there is no OPEN blocking ``data_quality_issues``
    row -- so the command can never report the corpus valid while a blocking open
    issue exists. Open issues are also reported separately for context."""

    findings = run_rules(conn, league=league, rule_code=rule_code)
    report = grade_findings(findings)
    opened = open_dq_findings(conn, league=league, rule_code=rule_code)
    open_counts = _open_dq_counts(opened)
    payload: dict[str, Any] = {
        "command": "data-quality",
        "league": league.upper() if league else None,
        "rule": rule_code,
        "grade": report.grade,
        "execution_valid": report.execution_valid,          # E2 rule findings only
        "corpus_valid": report.execution_valid and open_counts["blocking"] == 0,
        "score": round(report.score, 4),
        "counts": report.as_dict()["counts"],                # E2 findings by severity
        "open_counts": open_counts,                          # pre-existing open issues by severity
        "by_rule": report.as_dict()["by_rule"],
        "examples": report.as_dict()["examples"],
        "e2_findings": [f.as_dict() for f in findings],
        "open_data_quality_issues": [f.as_dict() for f in opened],
    }
    if review:
        payload["pending_manual_review"] = pending_review_counts(conn)
    return payload


def _emit(payload: dict[str, Any], *, as_json: bool, out: Printer) -> None:
    if as_json:
        out(json.dumps(payload, sort_keys=True))
        return
    valid = "CORPUS-VALID" if payload["corpus_valid"] else "NOT CORPUS-VALID"
    c, oc = payload["counts"], payload["open_counts"]
    out(f"data-quality  {payload['grade']}  [{valid}]  score={payload['score']:.2f}  "
        f"league={payload['league'] or 'ALL'}")
    out(f"  E2 findings: blocking={c['blocking']} issue={c['issue']} note={c['note']} "
        f"(execution_valid={payload['execution_valid']})")
    for rule in sorted(payload["by_rule"]):
        out(f"    {rule}: {payload['by_rule'][rule]}")
    out(f"  pre-existing open data_quality_issues: blocking={oc['blocking']} issue={oc['issue']} "
        f"note={oc['note']} (gate corpus_valid + exit; not the E2 grade)")
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
        # A finding at/above the threshold in EITHER the newly-detected E2 findings
        # OR the pre-existing open data_quality_issues breaches (RF6): an open
        # blocking issue can never pass at the default threshold.
        breached = any(Severity(f["severity"]).rank >= threshold
                       for f in (*payload["e2_findings"], *payload["open_data_quality_issues"]))
        return EXIT_THRESHOLD if breached else EXIT_OK

    return with_readonly_corpus(database_path, out, work)
