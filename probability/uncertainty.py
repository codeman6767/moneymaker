"""Uncertainty and out-of-distribution detection.

Two signals accompany every live prediction:

* **Uncertainty** -- the spread of the bootstrap ensemble's probabilities. Wide
  spread means the model disagrees with itself and the point estimate is soft.
* **Out-of-distribution flag** -- whether the feature vector falls outside the
  range the model was trained on. Fitted from training-data per-feature bounds
  (plus the spec's hard bounds); a live vector beyond them is flagged so callers
  can distrust the number rather than extrapolate blindly.

Both are cheap: the OOD check is a couple of vectorized comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .features import FeatureSpec

F32 = np.float32


@dataclass
class OODDetector:
    """Flags feature vectors outside the trained feature ranges."""

    lo: np.ndarray  # (F,) float32
    hi: np.ndarray  # (F,) float32

    @classmethod
    def fit(cls, X: np.ndarray, spec: FeatureSpec, margin: float = 0.1) -> "OODDetector":
        data_lo = X.min(axis=0)
        data_hi = X.max(axis=0)
        span = np.maximum(data_hi - data_lo, 1e-6)
        lo = data_lo - margin * span
        hi = data_hi + margin * span
        # Never allow the learned range to exceed the spec's hard bounds.
        lo = np.maximum(lo, spec.lo)
        hi = np.minimum(hi, spec.hi)
        return cls(lo=lo.astype(F32), hi=hi.astype(F32))

    def flags(self, X: np.ndarray) -> np.ndarray:
        """Boolean per-row OOD flag for a batch."""

        below = X < self.lo
        above = X > self.hi
        return np.any(below | above, axis=1)

    def score(self, x: np.ndarray) -> float:
        """How far outside the range, in units of range width (0 == inside)."""

        width = np.maximum(self.hi - self.lo, 1e-6)
        below = np.maximum(self.lo - x, 0.0) / width
        above = np.maximum(x - self.hi, 0.0) / width
        return float(np.max(below + above))

    def is_ood(self, x: np.ndarray) -> bool:
        return bool(np.any(x < self.lo) or np.any(x > self.hi))
