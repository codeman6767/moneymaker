"""Precomputed probability surfaces and empirical lookup tables.

A probability surface is a dense grid of model win-probabilities over the most
influential state dimensions (score differential x game phase), precomputed
once so a coarse lookup is a single array index -- no model call needed for a
sanity check or a fallback. The empirical lookup table is the observed home-win
rate in the same buckets, used to validate that both the surface and the model
track reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import features
from .datasets import GameStateDataset

F32 = np.float32

# Bucketing: score differential clamped to [-SD_CAP, SD_CAP]; phase is inning
# (1..9) for MLB or period (1..4, capped) for NBA.
SD_CAP = 15


def _sd_index(sd: int) -> int:
    return int(np.clip(sd, -SD_CAP, SD_CAP)) + SD_CAP  # 0 .. 2*SD_CAP


@dataclass
class ProbabilitySurface:
    """Dense (phase x score_diff) grid of model win-probabilities."""

    sport: str
    grid: np.ndarray  # (n_phase, 2*SD_CAP+1) float32
    min_phase: int

    def lookup(self, phase: int, score_diff: int) -> float:
        pi = int(np.clip(phase - self.min_phase, 0, self.grid.shape[0] - 1))
        return float(self.grid[pi, _sd_index(score_diff)])


def build_surface(
    sport: str,
    proba_fn: Callable[[np.ndarray], np.ndarray],
    *,
    n_phase: int,
) -> ProbabilitySurface:
    """Precompute a surface by evaluating the model on representative states.

    Each cell is a canonical state (neutral prior, mid-game defaults) at the
    given phase and score differential, so the grid isolates the score/phase
    effect.
    """

    sds = list(range(-SD_CAP, SD_CAP + 1))
    rows = []
    buf = np.empty(features.MLB_SPEC.size if sport == "mlb" else features.NBA_SPEC.size, dtype=F32)
    for phase in range(1, n_phase + 1):
        X = np.empty((len(sds), buf.shape[0]), dtype=F32)
        for j, sd in enumerate(sds):
            # MLB and NBA states are distinct types with distinct vector
            # functions; keep them in separate variables so neither can be
            # passed to the other sport's encoder.
            if sport == "mlb":
                mlb_state = features.MLBLiveState(
                    exp_runs_home=4.5, exp_runs_away=4.5, inning=phase, half="top",
                    outs=1, on_1b=False, on_2b=False, on_3b=False,
                    score_home=max(0, sd), score_away=max(0, -sd),
                )
                features.mlb_vector(mlb_state, out=buf)
            else:
                seconds_remaining = (4 - min(phase, 4)) * 12 * 60 + 6 * 60
                nba_state = features.NBALiveState(
                    exp_margin=0.0, exp_total=220.0, period=min(phase, 4),
                    seconds_remaining=seconds_remaining,
                    score_home=max(0, sd), score_away=max(0, -sd),
                )
                features.nba_vector(nba_state, out=buf)
            X[j] = buf
        rows.append(proba_fn(X))
    grid = np.stack(rows, axis=0).astype(F32)
    return ProbabilitySurface(sport=sport, grid=grid, min_phase=1)


@dataclass
class EmpiricalLookupTable:
    """Observed home-win rate by (phase, score_diff) bucket."""

    sport: str
    rate: np.ndarray   # (n_phase, 2*SD_CAP+1) float32; nan where unseen
    count: np.ndarray  # (n_phase, 2*SD_CAP+1) int32
    min_phase: int

    def lookup(self, phase: int, score_diff: int) -> float:
        pi = int(np.clip(phase - self.min_phase, 0, self.rate.shape[0] - 1))
        return float(self.rate[pi, _sd_index(score_diff)])


def build_empirical_table(dataset: GameStateDataset, *, n_phase: int) -> EmpiricalLookupTable:
    rate = np.full((n_phase, 2 * SD_CAP + 1), np.nan, dtype=F32)
    count = np.zeros((n_phase, 2 * SD_CAP + 1), dtype=np.int32)
    total = np.zeros_like(rate)
    for sd, phase, y in zip(dataset.score_diff, dataset.phase, dataset.y, strict=True):
        pi = int(np.clip(phase - 1, 0, n_phase - 1))
        si = _sd_index(int(sd))
        count[pi, si] += 1
        total[pi, si] = (0.0 if np.isnan(total[pi, si]) else total[pi, si]) + y
    seen = count > 0
    rate[seen] = total[seen] / count[seen]
    return EmpiricalLookupTable(sport=dataset.sport, rate=rate, count=count, min_phase=1)
