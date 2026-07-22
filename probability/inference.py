"""In-memory live inference engine.

Loads the champion (and its uncertainty ensemble) once at startup and serves
predictions from preallocated ``float32`` arrays. There is no pandas, no
per-event allocation of variable-size structures, and no database access on this
path -- only NumPy math (the default, benchmarked low-overhead runtime) or,
optionally, ONNX Runtime.

Each prediction returns the win probability, an uncertainty band (from the
ensemble), and an out-of-distribution flag. Inference latency is recorded in a
monotonic-clock histogram so p50/p95/p99 can be checked against the configured
budget (an acceptance criterion).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

import numpy as np

from streaming.latency import LatencyRegistry, monotonic_ns

if TYPE_CHECKING:
    # Type-only: ONNX Runtime stays an optional dependency, imported lazily in
    # `_init_onnx` and never required for the default numpy backend.
    from onnxruntime import InferenceSession

from .features import FeatureSpec
from .residual_model import ResidualWinProbModel
from .uncertainty import OODDetector

F32 = np.float32
Z95 = 1.959963984540054  # 95% normal quantile


@dataclass(frozen=True)
class PredictionResult:
    win_probability: float
    uncertainty_std: float
    lower: float
    upper: float
    ood_flag: bool
    ood_score: float


class InferenceEngine:
    """Single-load, in-memory win-probability inference."""

    def __init__(
        self,
        model: ResidualWinProbModel,
        spec: FeatureSpec,
        ood: OODDetector,
        *,
        budget_ns: int = 1_000_000,  # 1 ms default local-inference budget
        backend: str = "numpy",
        onnx_path: Optional[str] = None,
        latency: Optional[LatencyRegistry] = None,
    ) -> None:
        self.model = model
        self.spec = spec
        self.ood = ood
        self.budget_ns = budget_ns
        self.backend = backend
        self.latency = latency or LatencyRegistry()
        # Preallocated single-row buffer; reused every call (no per-event alloc).
        self._buf = np.empty((1, spec.size), dtype=F32)
        self._session: Optional["InferenceSession"] = None
        self._input_name: Optional[str] = None
        if backend == "onnx":
            self._init_onnx(onnx_path)

    def _init_onnx(self, onnx_path: Optional[str]) -> None:
        import onnxruntime as ort  # lazy; only when ONNX backend requested

        if onnx_path is None:
            raise ValueError("backend='onnx' requires onnx_path")
        # Load the model exactly once, here at startup.
        session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        if not inputs:
            raise ValueError(f"ONNX model at {onnx_path!r} declares no inputs")
        self._session = session
        self._input_name = inputs[0].name

    def _require_onnx(self) -> Tuple["InferenceSession", str]:
        """Return the loaded ONNX session and input name, or fail clearly.

        The session is loaded once in ``_init_onnx``; this proves it is present
        rather than letting a misconfigured engine fail on ``None``.
        """

        if self._session is None or self._input_name is None:
            raise RuntimeError(
                "ONNX backend selected but no session is loaded; "
                "construct the engine with backend='onnx' and a valid onnx_path"
            )
        return self._session, self._input_name

    def _champion_proba(self, X: np.ndarray) -> np.ndarray:
        if self.backend == "onnx":
            session, input_name = self._require_onnx()
            out = session.run(None, {input_name: X.astype(F32)})[0]
            return out.reshape(-1)
        return self.model.champion.proba(X)

    def predict_vector(self, x: np.ndarray) -> PredictionResult:
        """Predict from an already-vectorized, fixed-size feature array."""

        t0 = monotonic_ns()
        # Copy into the preallocated buffer; enforce shape/dtype.
        self._buf[0, :] = x
        p = float(self._champion_proba(self._buf)[0])
        std = float(self.model.ensemble_std(self._buf)[0])
        ood_flag = self.ood.is_ood(self._buf[0])
        ood_score = self.ood.score(self._buf[0])
        lower = max(0.0, p - Z95 * std)
        upper = min(1.0, p + Z95 * std)
        self.latency.record("inference_ns", monotonic_ns() - t0)
        return PredictionResult(
            win_probability=p,
            uncertainty_std=std,
            lower=lower,
            upper=upper,
            ood_flag=bool(ood_flag),
            ood_score=ood_score,
        )

    # -- Latency reporting ----------------------------------------------------
    def latency_snapshot(self):
        return self.latency.histogram("inference_ns").snapshot()

    def within_budget(self) -> bool:
        snap = self.latency_snapshot()
        return snap.p99_ns is None or snap.p99_ns <= self.budget_ns

    @classmethod
    def from_files(
        cls,
        model_path: str,
        spec: FeatureSpec,
        ood: OODDetector,
        **kwargs,
    ) -> "InferenceEngine":
        """Load the model once from disk at process startup."""

        model = ResidualWinProbModel.load(model_path)
        return cls(model, spec, ood, **kwargs)
