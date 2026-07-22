"""Live probability updates (Module 5).

Fast, calibrated live win-probability that approximates the full Monte Carlo
simulator (the reference model in the research lane). Pregame priors come from
the research lane; a lightweight residual model adjusts them from live state.
The champion exports to ONNX; in-memory inference runs on fixed-size NumPy
arrays, returns uncertainty and OOD flags, and is measured against a latency
budget.

See ``CLAUDE.md``: no DB/model-load/large-simulation on the hot path; preload
immutable artifacts; monotonic latency measurement.
"""

from . import pregame_prior
from .calibration import CalibrationReport, brier_score, calibration_report
from .datasets import GameStateDataset, build_mlb_dataset, build_nba_dataset
from .features import (
    MLB_SPEC,
    NBA_SPEC,
    FeatureSpec,
    MLBLiveState,
    NBALiveState,
    mlb_vector,
    nba_vector,
)
from .inference import InferenceEngine, PredictionResult
from .onnx_export import (
    build_onnx_model,
    export_to_onnx,
    onnx_available,
    onnxruntime_available,
)
from .pipeline import Artifacts, train_and_build
from .reference import (
    DEFAULT_THRESHOLDS,
    AnalyticReference,
    ApproxReport,
    ApproxThresholds,
    approximation_report,
)
from .residual_model import (
    LinearModel,
    ResidualWinProbModel,
    train_champion,
)
from .surfaces import (
    EmpiricalLookupTable,
    ProbabilitySurface,
    build_empirical_table,
    build_surface,
)
from .uncertainty import OODDetector

__all__ = [
    "pregame_prior",
    # features
    "FeatureSpec",
    "MLB_SPEC",
    "NBA_SPEC",
    "MLBLiveState",
    "NBALiveState",
    "mlb_vector",
    "nba_vector",
    # datasets
    "GameStateDataset",
    "build_mlb_dataset",
    "build_nba_dataset",
    # model
    "LinearModel",
    "ResidualWinProbModel",
    "train_champion",
    # calibration
    "CalibrationReport",
    "calibration_report",
    "brier_score",
    # reference / approximation
    "AnalyticReference",
    "ApproxReport",
    "ApproxThresholds",
    "DEFAULT_THRESHOLDS",
    "approximation_report",
    # surfaces
    "ProbabilitySurface",
    "EmpiricalLookupTable",
    "build_surface",
    "build_empirical_table",
    # uncertainty
    "OODDetector",
    # inference
    "InferenceEngine",
    "PredictionResult",
    # onnx
    "onnx_available",
    "onnxruntime_available",
    "build_onnx_model",
    "export_to_onnx",
    # pipeline
    "Artifacts",
    "train_and_build",
]
