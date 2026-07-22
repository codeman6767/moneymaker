"""Historical game-state datasets for MLB and NBA.

Each row is one in-game state plus the eventual home-win label. The dataset is
**chronological**: rows carry a monotonically increasing timestamp and the
train/test split is by time, never shuffled across the boundary -- so the model
is only ever trained on the past (requirement: train chronologically, use only
information known at the event timestamp). Feature vectors contain solely
current-state information; the label is the final outcome (the training target),
which is the only permitted "future" value.

The data here is synthetic and generated from :mod:`reference` so tests are
deterministic and self-contained. In production these builders are replaced by
real historical game states with outcomes; the interfaces stay the same.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import features
from .reference import TRUTH

F32 = np.float32


@dataclass
class GameStateDataset:
    sport: str
    X: np.ndarray          # (N, F) float32 -- fixed-size feature vectors
    y: np.ndarray          # (N,) int8 -- 1 if home team won
    timestamps: np.ndarray  # (N,) int64 -- monotonically increasing
    true_prob: np.ndarray  # (N,) float64 -- reference win prob (for error checks)
    score_diff: np.ndarray  # (N,) int32 -- for empirical bucketing
    phase: np.ndarray      # (N,) int32 -- inning (MLB) or period (NBA)

    def __len__(self) -> int:
        return self.X.shape[0]

    def chronological_split(self, train_frac: float = 0.7) -> tuple["GameStateDataset", "GameStateDataset"]:
        """Split by time: earliest ``train_frac`` for training, rest for test."""

        # Ensure ordering by timestamp (already monotonic, but be explicit).
        order = np.argsort(self.timestamps, kind="stable")
        cut = int(len(self) * train_frac)
        tr, te = order[:cut], order[cut:]
        return self._subset(tr), self._subset(te)

    def _subset(self, idx: np.ndarray) -> "GameStateDataset":
        return GameStateDataset(
            sport=self.sport,
            X=self.X[idx],
            y=self.y[idx],
            timestamps=self.timestamps[idx],
            true_prob=self.true_prob[idx],
            score_diff=self.score_diff[idx],
            phase=self.phase[idx],
        )


def build_mlb_dataset(n: int = 8000, seed: int = 7) -> GameStateDataset:
    rng = np.random.default_rng(seed)
    spec = features.MLB_SPEC
    X = np.empty((n, spec.size), dtype=F32)
    score_diff = np.empty(n, dtype=np.int32)
    phase = np.empty(n, dtype=np.int32)

    buf = np.empty(spec.size, dtype=F32)
    for i in range(n):
        inning = int(rng.integers(1, 10))
        half = "bottom" if rng.random() < 0.5 else "top"
        sd = int(rng.integers(-8, 9))
        base_home = int(rng.integers(0, 8))
        state = features.MLBLiveState(
            exp_runs_home=float(rng.uniform(3.0, 6.0)),
            exp_runs_away=float(rng.uniform(3.0, 6.0)),
            inning=inning,
            half=half,
            outs=int(rng.integers(0, 3)),
            on_1b=bool(rng.integers(0, 2)),
            on_2b=bool(rng.integers(0, 2)),
            on_3b=bool(rng.integers(0, 2)),
            score_home=base_home + max(0, sd),
            score_away=base_home + max(0, -sd),
            pitcher_quality=float(rng.uniform(0, 1)),
            bullpen_quality=float(rng.uniform(0, 1)),
            bullpen_availability=float(rng.uniform(0, 1)),
            lineup_position=int(rng.integers(1, 10)),
            modeled_is_home=True,
        )
        features.mlb_vector(state, out=buf)
        X[i] = buf
        score_diff[i] = state.score_home - state.score_away
        phase[i] = inning

    return _finish("mlb", X, score_diff, phase, rng)


def build_nba_dataset(n: int = 8000, seed: int = 11) -> GameStateDataset:
    rng = np.random.default_rng(seed)
    spec = features.NBA_SPEC
    X = np.empty((n, spec.size), dtype=F32)
    score_diff = np.empty(n, dtype=np.int32)
    phase = np.empty(n, dtype=np.int32)

    buf = np.empty(spec.size, dtype=F32)
    for i in range(n):
        period = int(rng.integers(1, 5))
        # Seconds remaining consistent with the period.
        secs_in_period = float(rng.uniform(0, 12 * 60))
        seconds_remaining = (4 - period) * 12 * 60 + secs_in_period
        sd = int(rng.integers(-20, 21))
        base = int(rng.integers(70, 110))
        state = features.NBALiveState(
            exp_margin=float(rng.uniform(-10, 10)),
            exp_total=float(rng.uniform(200, 240)),
            period=period,
            seconds_remaining=seconds_remaining,
            score_home=base + max(0, sd),
            score_away=base + max(0, -sd),
            possession=("home", "away", "none")[int(rng.integers(0, 3))],
            pace=float(rng.uniform(92, 104)),
            timeouts_home=int(rng.integers(0, 8)),
            timeouts_away=int(rng.integers(0, 8)),
            team_fouls_home=int(rng.integers(0, 8)),
            team_fouls_away=int(rng.integers(0, 8)),
            lineup_strength_home=float(rng.uniform(-5, 5)),
            lineup_strength_away=float(rng.uniform(-5, 5)),
            availability_home=float(rng.uniform(0.5, 1.0)),
            availability_away=float(rng.uniform(0.5, 1.0)),
        )
        features.nba_vector(state, out=buf)
        X[i] = buf
        score_diff[i] = state.score_home - state.score_away
        phase[i] = period

    return _finish("nba", X, score_diff, phase, rng)


def _finish(sport: str, X: np.ndarray, score_diff: np.ndarray, phase: np.ndarray, rng) -> GameStateDataset:
    truth = TRUTH[sport]
    p = truth.prob(X)
    y = (rng.random(X.shape[0]) < p).astype(np.int8)
    timestamps = np.arange(X.shape[0], dtype=np.int64)  # chronological order
    return GameStateDataset(
        sport=sport, X=X, y=y, timestamps=timestamps,
        true_prob=p, score_diff=score_diff, phase=phase,
    )
