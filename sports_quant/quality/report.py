"""Typed findings + corpus grading for the Phase E2 quality assessment.

Reuses the ``backtest.data_quality`` vocabulary -- an A-F letter grade, an
``execution_valid`` flag, and blocking/issue/note severities -- but with a
CORPUS-specific policy rather than the execution-replay policy: a point-in-time
training corpus is "execution-valid" (trustworthy for its claimed use) iff it has
NO blocking finding (a blocking finding is a proven leakage / determinism /
identity defect that makes a row untrustworthy). Grading is pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

__all__ = ["Severity", "Finding", "QualityReport", "grade_findings", "REPRESENTATIVE_EXAMPLES"]

#: Max representative examples surfaced per rule code (deterministic prefix).
REPRESENTATIVE_EXAMPLES = 3


class Severity(str, Enum):
    """Corpus-finding severity, aligned with ``schema.DATA_QUALITY_SEVERITIES``."""

    BLOCKING = "blocking"   # the row/corpus cannot be trusted for its claimed use
    ISSUE = "issue"         # a material quality deficiency
    NOTE = "note"           # a transparent limitation / missing optional coverage

    @property
    def rank(self) -> int:
        return {"blocking": 3, "issue": 2, "note": 1}[self.value]


@dataclass(frozen=True)
class Finding:
    """One newly-detected E2 quality finding (never persisted; report-only).

    ``source`` distinguishes a finding NEWLY DETECTED by an E2 rule (``"e2_rule"``)
    from a pre-existing open ``data_quality_issues`` row surfaced for context
    (``"open_dq_issue"``); the two are reported separately and never conflated.
    """

    rule_code: str
    severity: Severity
    message: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    league: Optional[str] = None
    source: str = "e2_rule"

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_code": self.rule_code,
            "severity": self.severity.value,
            "message": self.message,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "league": self.league,
            "source": self.source,
        }


def _sort_key(f: Finding) -> tuple[int, str, str, str]:
    # Deterministic order: severity desc, then rule_code, entity_type, entity_id.
    return (-f.severity.rank, f.rule_code, f.entity_type or "", f.entity_id or "")


@dataclass(frozen=True)
class QualityReport:
    grade: str                                  # "A".."F"
    execution_valid: bool
    score: float                                # 0..1
    counts: dict[str, int] = field(default_factory=dict)      # severity -> count
    by_rule: dict[str, int] = field(default_factory=dict)     # rule_code -> count
    examples: dict[str, list[dict[str, object]]] = field(default_factory=dict)

    def banner(self) -> str:
        valid = "PIT-VALID" if self.execution_valid else "NOT PIT-VALID"
        return (f"DATA QUALITY: {self.grade}  [{valid}]  score={self.score:.2f}  "
                f"blocking={self.counts.get('blocking', 0)} issue={self.counts.get('issue', 0)} "
                f"note={self.counts.get('note', 0)}")

    def as_dict(self) -> dict[str, object]:
        return {
            "grade": self.grade,
            "execution_valid": self.execution_valid,
            "score": round(self.score, 4),
            "counts": {s.value: self.counts.get(s.value, 0) for s in Severity},
            "by_rule": {k: self.by_rule[k] for k in sorted(self.by_rule)},
            "examples": {k: self.examples[k] for k in sorted(self.examples)},
        }


def grade_findings(findings: list[Finding]) -> QualityReport:
    """Grade E2-rule findings deterministically (corpus policy).

    ``execution_valid`` is False iff any blocking finding exists. Score: a blocking
    finding forces 0.0/grade F; otherwise ``1 - 0.1*issues - 0.02*notes`` (floored
    at 0). Only findings with ``source == 'e2_rule'`` count toward the grade;
    pre-existing open ``data_quality_issues`` rows are reported for context but do
    not alter the E2 grade. Deterministic across finding order."""

    graded = [f for f in findings if f.source == "e2_rule"]
    counts = {s.value: sum(1 for f in graded if f.severity is s) for s in Severity}
    by_rule: dict[str, int] = {}
    buckets: dict[str, list[Finding]] = {}
    for f in graded:
        by_rule[f.rule_code] = by_rule.get(f.rule_code, 0) + 1
        buckets.setdefault(f.rule_code, []).append(f)

    n_block, n_issue, n_note = counts["blocking"], counts["issue"], counts["note"]
    execution_valid = n_block == 0
    if not execution_valid:
        grade, score = "F", 0.0
    else:
        score = max(0.0, 1.0 - 0.1 * n_issue - 0.02 * n_note)
        grade = ("A" if score >= 0.95 else "B" if score >= 0.85 else "C"
                 if score >= 0.70 else "D" if score >= 0.50 else "F")

    examples = {
        rule: [f.as_dict() for f in sorted(fs, key=_sort_key)[:REPRESENTATIVE_EXAMPLES]]
        for rule, fs in buckets.items()
    }
    return QualityReport(grade=grade, execution_valid=execution_valid, score=score,
                         counts=counts, by_rule=by_rule, examples=examples)
