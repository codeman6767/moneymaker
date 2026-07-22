"""Probability calibration metrics.

A live win probability is only useful if it is calibrated: among events assigned
p, roughly a fraction p should be wins. We measure this with the Brier score and
Expected Calibration Error (ECE), and expose a reliability table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    count: int
    mean_pred: float
    mean_actual: float


@dataclass(frozen=True)
class CalibrationReport:
    brier: float
    ece: float
    bins: tuple[ReliabilityBin, ...]

    def is_calibrated(self, *, max_ece: float = 0.05, max_brier: float = 0.25) -> bool:
        return self.ece <= max_ece and self.brier <= max_brier


def brier_score(probs: np.ndarray, outcomes: np.ndarray) -> float:
    p = probs.astype(np.float64)
    y = outcomes.astype(np.float64)
    return float(np.mean((p - y) ** 2))


def calibration_report(probs: np.ndarray, outcomes: np.ndarray, n_bins: int = 10) -> CalibrationReport:
    p = probs.astype(np.float64)
    y = outcomes.astype(np.float64)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: List[ReliabilityBin] = []
    ece = 0.0
    n = len(p)
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        # Last bin is closed on the right so p == 1.0 is included.
        mask = (p >= lo) & (p < hi) if i < n_bins - 1 else (p >= lo) & (p <= hi)
        count = int(mask.sum())
        if count == 0:
            bins.append(ReliabilityBin(lo, hi, 0, float("nan"), float("nan")))
            continue
        mean_pred = float(p[mask].mean())
        mean_actual = float(y[mask].mean())
        bins.append(ReliabilityBin(lo, hi, count, mean_pred, mean_actual))
        ece += (count / n) * abs(mean_pred - mean_actual)

    return CalibrationReport(brier=brier_score(p, y), ece=float(ece), bins=tuple(bins))
