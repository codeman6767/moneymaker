"""Tests for live probability updates (Module 5)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from probability import (
    DEFAULT_THRESHOLDS,
    InferenceEngine,
    MLB_SPEC,
    NBA_SPEC,
    build_mlb_dataset,
    build_nba_dataset,
    onnx_available,
    onnxruntime_available,
    pregame_prior,
)
from probability.onnx_export import export_to_onnx
from probability.surfaces import SD_CAP

PROBABILITY_SRC = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Pregame-prior integration
# --------------------------------------------------------------------------- #
def test_pregame_prior_integration():
    assert pregame_prior.mlb_prior_prob(4.5, 4.5) == pytest.approx(0.5)
    assert pregame_prior.mlb_prior_prob(6.0, 4.0) > 0.5
    assert pregame_prior.nba_prior_prob(0.0) == pytest.approx(0.5)
    assert pregame_prior.nba_prior_prob(6.0) > 0.6
    # The prior is feature 0 of the live vector.
    ds = build_mlb_dataset(n=10)
    assert MLB_SPEC.names[0] == "prior_logit"
    assert ds.X.dtype == np.float32


# --------------------------------------------------------------------------- #
# Datasets: chronological, fixed-size, no leakage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("builder,spec", [(build_mlb_dataset, MLB_SPEC), (build_nba_dataset, NBA_SPEC)])
def test_datasets_are_chronological_and_fixed_size(builder, spec):
    ds = builder(n=2000)
    # Fixed-size float32 vectors.
    assert ds.X.shape == (2000, spec.size)
    assert ds.X.dtype == np.float32
    # Timestamps are monotonically increasing.
    assert np.all(np.diff(ds.timestamps) >= 0)
    # Chronological split: every training row precedes every test row in time.
    train, test = ds.chronological_split(0.7)
    assert train.timestamps.max() < test.timestamps.min()
    assert len(train) + len(test) == len(ds)


# --------------------------------------------------------------------------- #
# Calibration (acceptance: live predictions are calibrated)
# --------------------------------------------------------------------------- #
def test_mlb_calibrated(mlb_artifacts):
    assert mlb_artifacts.calibration.is_calibrated()
    assert mlb_artifacts.calibration.ece <= 0.05


def test_nba_calibrated(nba_artifacts):
    assert nba_artifacts.calibration.is_calibrated()
    assert nba_artifacts.calibration.ece <= 0.05


# --------------------------------------------------------------------------- #
# Approximation error vs the reference simulator (acceptance: within thresholds)
# --------------------------------------------------------------------------- #
def test_mlb_approximation_within_thresholds(mlb_artifacts):
    assert mlb_artifacts.approximation.within(DEFAULT_THRESHOLDS)


def test_nba_approximation_within_thresholds(nba_artifacts):
    assert nba_artifacts.approximation.within(DEFAULT_THRESHOLDS)


# --------------------------------------------------------------------------- #
# Comparison with empirical lookup tables
# --------------------------------------------------------------------------- #
def test_surface_matches_empirical_in_central_buckets(mlb_artifacts):
    surface = mlb_artifacts.surface
    empirical = mlb_artifacts.empirical
    # Compare where the empirical bucket has support.
    checked = 0
    for phase in (3, 5, 7):
        for sd in (-3, 0, 3):
            emp = empirical.lookup(phase, sd)
            if np.isnan(emp) or empirical.count[phase - 1, sd + SD_CAP] < 20:
                continue
            assert abs(surface.lookup(phase, sd) - emp) < 0.15
            checked += 1
    assert checked > 0


def test_surface_monotonic_in_score_diff_and_leverage(mlb_artifacts):
    surface = mlb_artifacts.surface
    # More positive score differential -> higher home win prob, at fixed phase.
    row = surface.grid[4]  # phase 5
    assert np.all(np.diff(row) >= -1e-4)
    # Leverage: a given lead late is worth more than the same lead early.
    assert surface.lookup(9, 5) > surface.lookup(1, 5)


# --------------------------------------------------------------------------- #
# In-memory inference: uncertainty + OOD + fixed-size buffer
# --------------------------------------------------------------------------- #
def test_inference_returns_uncertainty_and_bands(mlb_artifacts):
    engine = mlb_artifacts.engine
    x = mlb_artifacts.test.X[0]
    result = engine.predict_vector(x)
    assert 0.0 <= result.win_probability <= 1.0
    assert result.uncertainty_std >= 0.0
    assert result.lower <= result.win_probability <= result.upper
    assert result.ood_flag is False  # an in-distribution row


def test_inference_flags_out_of_distribution(mlb_artifacts):
    engine = mlb_artifacts.engine
    x = mlb_artifacts.test.X[0].copy()
    x[7] = 100.0  # score_diff_scaled wildly outside the trained range
    result = engine.predict_vector(x)
    assert result.ood_flag is True
    assert result.ood_score > 0.0


def test_inference_uses_fixed_size_reused_buffer(mlb_artifacts):
    engine = mlb_artifacts.engine
    buf_id = id(engine._buf)
    for i in range(50):
        engine.predict_vector(mlb_artifacts.test.X[i])
    # No per-event reallocation: same fixed-size buffer object throughout.
    assert id(engine._buf) == buf_id
    assert engine._buf.shape == (1, MLB_SPEC.size)
    assert engine._buf.dtype == np.float32


# --------------------------------------------------------------------------- #
# Latency (acceptance: p99 below budget) + measured percentiles
# --------------------------------------------------------------------------- #
def test_inference_latency_within_budget(nba_artifacts):
    engine = nba_artifacts.engine
    X = nba_artifacts.test.X
    for i in range(3000):
        engine.predict_vector(X[i % X.shape[0]])
    snap = engine.latency_snapshot()
    assert snap.count >= 3000
    assert snap.p50_ns is not None and snap.p95_ns is not None and snap.p99_ns is not None
    assert engine.within_budget()  # p99 <= configured budget


# --------------------------------------------------------------------------- #
# Load once at startup
# --------------------------------------------------------------------------- #
def test_model_loads_once_from_file(mlb_artifacts, tmp_path):
    path = str(tmp_path / "champion.npz")
    mlb_artifacts.model.save(path)
    engine = InferenceEngine.from_files(path, MLB_SPEC, mlb_artifacts.ood)
    x = mlb_artifacts.test.X[3]
    reloaded = engine.predict_vector(x).win_probability
    original = mlb_artifacts.model.proba_one(x)
    assert reloaded == pytest.approx(original, abs=1e-6)


# --------------------------------------------------------------------------- #
# No pandas / no database on the probability code path
# --------------------------------------------------------------------------- #
def test_no_pandas_or_database_imports_in_package():
    forbidden = ("import pandas", "psycopg", "sqlite3")
    for py in PROBABILITY_SRC.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{py.name} references {token!r} on the inference path"


# --------------------------------------------------------------------------- #
# ONNX export (skipped cleanly when onnx isn't installed)
# --------------------------------------------------------------------------- #
def test_onnx_export_and_parity(mlb_artifacts, tmp_path):
    if not onnx_available():
        with pytest.raises((ImportError, ModuleNotFoundError)):
            export_to_onnx(mlb_artifacts.model.champion, str(tmp_path / "m.onnx"))
        pytest.skip("onnx not installed; export correctly raises")

    path = export_to_onnx(mlb_artifacts.model.champion, str(tmp_path / "m.onnx"))
    assert Path(path).exists()

    if not onnxruntime_available():
        pytest.skip("onnxruntime not installed; graph built and checked only")

    engine = InferenceEngine(
        mlb_artifacts.model, MLB_SPEC, mlb_artifacts.ood, backend="onnx", onnx_path=path
    )
    x = mlb_artifacts.test.X[5]
    onnx_p = engine.predict_vector(x).win_probability
    numpy_p = mlb_artifacts.model.proba_one(x)
    assert onnx_p == pytest.approx(numpy_p, abs=1e-5)
