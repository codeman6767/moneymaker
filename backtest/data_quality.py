"""Data-quality grading for a replay dataset.

The hard rule (from the requirements): the strategy is **not** execution-valid if
historical order-book or event timestamps are missing. This grader enforces
that -- ``execution_valid`` is False whenever those are absent -- and assigns a
prominent letter grade that every report must surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .events import EventType, ReplayEvent


@dataclass(frozen=True)
class DataQualityReport:
    grade: str            # "A".."F"
    execution_valid: bool
    score: float          # 0..1
    issues: List[str] = field(default_factory=list)   # execution-invalidating
    notes: List[str] = field(default_factory=list)    # non-fatal caveats

    def banner(self) -> str:
        valid = "EXECUTION-VALID" if self.execution_valid else "NOT EXECUTION-VALID"
        head = f"DATA QUALITY: {self.grade}  [{valid}]"
        if self.issues:
            head += "  | issues: " + "; ".join(self.issues)
        return head


def grade_dataset(events: List[ReplayEvent]) -> DataQualityReport:
    issues: List[str] = []
    notes: List[str] = []

    total = len(events)
    ob_events = [e for e in events if e.event_type in (EventType.OB_SNAPSHOT, EventType.OB_DELTA)]
    trade_events = [e for e in events if e.event_type == EventType.TRADE]
    status_events = [e for e in events if e.event_type == EventType.MARKET_STATUS]

    # -- Execution-invalidating conditions -----------------------------------
    untimed_events = [e for e in events if e.event_time_ns is None]
    if untimed_events:
        issues.append(f"{len(untimed_events)}/{total} events missing timestamps")

    if not ob_events:
        issues.append("no historical order-book events")
    else:
        untimed_ob = [e for e in ob_events if e.event_time_ns is None]
        if untimed_ob:
            issues.append(f"{len(untimed_ob)}/{len(ob_events)} order-book events missing timestamps")

    execution_valid = not issues

    # -- Non-fatal caveats (cap the grade but stay valid) --------------------
    if not trade_events:
        notes.append("no trade prints; CLV falls back to final book")
    if not status_events:
        notes.append("no market-status events; assuming continuously open")
    untimed_publish = [e for e in ob_events if e.publish_time_ns is None]
    if ob_events and untimed_publish:
        notes.append("order-book publication times absent; provider lag is modeled, not measured")

    # -- Score / grade --------------------------------------------------------
    if not execution_valid:
        # Grade cannot exceed D when execution-invalid; F if no usable book/times.
        grade = "F" if (not ob_events or untimed_events) else "D"
        return DataQualityReport(grade=grade, execution_valid=False, score=0.0,
                                 issues=issues, notes=notes)

    score = 1.0 - 0.1 * len(notes)
    if score >= 0.95:
        grade = "A"
    elif score >= 0.85:
        grade = "B"
    elif score >= 0.7:
        grade = "C"
    else:
        grade = "D"
    return DataQualityReport(grade=grade, execution_valid=True, score=score,
                             issues=issues, notes=notes)
