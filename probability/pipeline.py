"""End-to-end train-and-build pipeline for a sport's live probability model.

Ties the pieces together in the required order: build the chronological
historical dataset, split by time, train the champion + ensemble on the past,
fit the OOD detector, then evaluate calibration and approximation error against
the reference on the held-out (future) test slice, and precompute the surface and
empirical table. Returns everything as immutable-ish artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import features
from .calibration import CalibrationReport, calibration_report
from .datasets import GameStateDataset, build_mlb_dataset, build_nba_dataset
from .inference import InferenceEngine
from .reference import ApproxReport, approximation_report
from .residual_model import ResidualWinProbModel, train_champion
from .surfaces import (
    EmpiricalLookupTable,
    ProbabilitySurface,
    build_empirical_table,
    build_surface,
)
from .uncertainty import OODDetector

_SPEC = {"mlb": features.MLB_SPEC, "nba": features.NBA_SPEC}
_N_PHASE = {"mlb": 9, "nba": 4}


@dataclass
class Artifacts:
    sport: str
    model: ResidualWinProbModel
    engine: InferenceEngine
    ood: OODDetector
    calibration: CalibrationReport
    approximation: ApproxReport
    surface: ProbabilitySurface
    empirical: EmpiricalLookupTable
    train: GameStateDataset
    test: GameStateDataset


def train_and_build(
    sport: str,
    *,
    n: int = 8000,
    seed: Optional[int] = None,
    budget_ns: int = 1_000_000,
) -> Artifacts:
    spec = _SPEC[sport]
    if sport == "mlb":
        dataset = build_mlb_dataset(n=n, seed=7 if seed is None else seed)
    else:
        dataset = build_nba_dataset(n=n, seed=11 if seed is None else seed)

    # Chronological: train on the earliest 70%, test on the latest 30%.
    train, test = dataset.chronological_split(0.7)
    # Within training, hold out the most recent 20% for champion selection.
    fit, val = train.chronological_split(0.8)

    model = train_champion(fit.X, fit.y, val.X, val.y, sport=sport)
    ood = OODDetector.fit(train.X, spec)
    engine = InferenceEngine(model, spec, ood, budget_ns=budget_ns)

    test_probs = model.proba(test.X)
    calibration = calibration_report(test_probs, test.y)
    approximation = approximation_report(test_probs, test.true_prob)

    surface = build_surface(sport, model.proba, n_phase=_N_PHASE[sport])
    empirical = build_empirical_table(train, n_phase=_N_PHASE[sport])

    return Artifacts(
        sport=sport, model=model, engine=engine, ood=ood,
        calibration=calibration, approximation=approximation,
        surface=surface, empirical=empirical, train=train, test=test,
    )
