"""Phase E2 point-in-time training-corpus quality assessment (offline, read-only).

`rules` scans a migrated corpus (and, where relevant, the E2 historical dataset)
for point-in-time / leakage defects and emits typed :class:`~sports_quant.quality.report.Finding`
objects; `report` grades them with the ``backtest.data_quality`` vocabulary
(A-F letter grade, ``execution_valid``, blocking/issue/note severities) under a
corpus-specific policy. Nothing here makes a network request, ingests, mutates
the corpus, or performs feature engineering / modelling.
"""

from __future__ import annotations

from .report import (
    Finding,
    QualityReport,
    Severity,
    grade_findings,
)

__all__ = ["Finding", "QualityReport", "Severity", "grade_findings"]
