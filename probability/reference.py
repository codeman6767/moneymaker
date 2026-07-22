"""Reference model and approximation-error accounting.

The full Monte Carlo simulator (research lane) is the *reference* the fast model
approximates. That simulator is external and slow; this module defines the thin
interface the comparison uses (:class:`ReferenceModel`) plus:

* :class:`AnalyticReference` -- a deterministic, closed-form win-probability used
  to generate synthetic historical data and to measure approximation error in
  tests. In production this is replaced by a wrapper over the real MC simulator.
* documented approximation-error thresholds the fast model must stay within.

Because the synthetic data is generated from :class:`AnalyticReference`, the fast
model's error against it is a clean, reproducible proxy for "how well does the
fast model track the reference simulator".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .features import MLB_SPEC, NBA_SPEC

F32 = np.float32


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


@dataclass(frozen=True)
class GenerativeTruth:
    """Linear-logit truth over a feature layout: p = sigmoid(w . x + b)."""

    weights: np.ndarray
    bias: float

    def prob(self, X: np.ndarray) -> np.ndarray:
        return sigmoid(X.astype(np.float64) @ self.weights.astype(np.float64) + self.bias)


# Truth weights are aligned to the feature layouts in features.py. Most live
# features have zero true weight; the prior and (leverage-weighted) score
# differential dominate -- a realistic shape for a win-probability surface.
_mlb_w = np.zeros(MLB_SPEC.size, dtype=F32)
_mlb_w[0] = 1.0    # prior_logit
_mlb_w[2] = 0.05   # slight edge to home batting in the bottom half
_mlb_w[7] = 1.2    # score_diff_scaled
_mlb_w[8] = 2.5    # score_diff_leverage (matters more late)
MLB_TRUE = GenerativeTruth(weights=_mlb_w, bias=0.1)

_nba_w = np.zeros(NBA_SPEC.size, dtype=F32)
_nba_w[0] = 1.0    # prior_logit
_nba_w[4] = 1.5    # score_diff_scaled
_nba_w[5] = 3.5    # score_diff_leverage
_nba_w[6] = 0.05   # possession
_nba_w[8] = 0.05   # home timeouts
_nba_w[9] = -0.05  # away timeouts
_nba_w[11] = 0.2   # lineup strength diff
_nba_w[12] = 0.3   # availability diff
NBA_TRUE = GenerativeTruth(weights=_nba_w, bias=0.0)

TRUTH = {"mlb": MLB_TRUE, "nba": NBA_TRUE}


class ReferenceModel(Protocol):
    def prob(self, X: np.ndarray) -> np.ndarray: ...


class AnalyticReference:
    """Closed-form reference win-probability for a sport."""

    def __init__(self, sport: str) -> None:
        self.sport = sport
        self.truth = TRUTH[sport]

    def prob(self, X: np.ndarray) -> np.ndarray:
        return self.truth.prob(X)


@dataclass(frozen=True)
class ApproxThresholds:
    """Documented approximation-error budget vs the reference model.

    These are the acceptance thresholds: the fast model must track the reference
    within them across the test distribution.
    """

    mean_abs_error: float = 0.03
    p99_abs_error: float = 0.08
    max_abs_error: float = 0.12


DEFAULT_THRESHOLDS = ApproxThresholds()


@dataclass(frozen=True)
class ApproxReport:
    mean_abs_error: float
    p95_abs_error: float
    p99_abs_error: float
    max_abs_error: float

    def within(self, thresholds: ApproxThresholds = DEFAULT_THRESHOLDS) -> bool:
        return (
            self.mean_abs_error <= thresholds.mean_abs_error
            and self.p99_abs_error <= thresholds.p99_abs_error
            and self.max_abs_error <= thresholds.max_abs_error
        )


def approximation_report(model_probs: np.ndarray, reference_probs: np.ndarray) -> ApproxReport:
    err = np.abs(model_probs.astype(np.float64) - reference_probs.astype(np.float64))
    return ApproxReport(
        mean_abs_error=float(np.mean(err)),
        p95_abs_error=float(np.percentile(err, 95)),
        p99_abs_error=float(np.percentile(err, 99)),
        max_abs_error=float(np.max(err)),
    )
