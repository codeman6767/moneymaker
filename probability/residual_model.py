"""Fast live residual win-probability models.

The model is a logistic regression whose feature 0 is the pregame prior logit,
so it learns a *residual* adjustment to the prior from live state. Training is
chronological (fit on the past only). Inference is a single ``float32`` matrix
op -- cheap enough for the hot path and trivially exportable to ONNX.

A small bootstrap **ensemble** is trained alongside the champion to produce
predictive uncertainty (spread across ensemble members); the champion itself is
the model fit on all training data and is the one exported to ONNX.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
from sklearn.linear_model import LogisticRegression

F32 = np.float32


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


@dataclass
class LinearModel:
    """A bare logistic model: p = sigmoid(x . w + b). float32, no sklearn at
    inference time."""

    weights: np.ndarray  # (F,) float32
    bias: float

    def proba(self, X: np.ndarray) -> np.ndarray:
        z = X.astype(np.float32) @ self.weights + np.float32(self.bias)
        return _sigmoid(z)


def _fit_linear(X: np.ndarray, y: np.ndarray, C: float) -> LinearModel:
    clf = LogisticRegression(C=C, max_iter=2000)
    clf.fit(X, y)
    return LinearModel(weights=clf.coef_[0].astype(F32), bias=float(clf.intercept_[0]))


@dataclass
class ResidualWinProbModel:
    """Champion model plus a bootstrap ensemble for uncertainty."""

    sport: str
    champion: LinearModel
    ensemble: List[LinearModel]

    def proba(self, X: np.ndarray) -> np.ndarray:
        return self.champion.proba(X)

    def proba_one(self, x: np.ndarray) -> float:
        return float(self.champion.proba(x.reshape(1, -1))[0])

    def ensemble_proba(self, X: np.ndarray) -> np.ndarray:
        """(K, N) matrix of ensemble member probabilities."""

        return np.stack([m.proba(X) for m in self.ensemble], axis=0)

    def ensemble_std(self, X: np.ndarray) -> np.ndarray:
        if not self.ensemble:
            return np.zeros(X.shape[0], dtype=np.float64)
        return self.ensemble_proba(X).std(axis=0)

    # -- Persistence (fixed-size arrays; no pickling of sklearn objects) ------
    def save(self, path: str) -> None:
        np.savez(
            path,
            sport=self.sport,
            champion_w=self.champion.weights,
            champion_b=np.float32(self.champion.bias),
            ensemble_w=np.stack([m.weights for m in self.ensemble]) if self.ensemble else np.empty((0,)),
            ensemble_b=np.array([m.bias for m in self.ensemble], dtype=F32),
        )

    @classmethod
    def load(cls, path: str) -> "ResidualWinProbModel":
        data = np.load(path, allow_pickle=False)
        champion = LinearModel(weights=data["champion_w"].astype(F32), bias=float(data["champion_b"]))
        ew = data["ensemble_w"]
        eb = data["ensemble_b"]
        ensemble = [LinearModel(weights=ew[i].astype(F32), bias=float(eb[i])) for i in range(len(eb))]
        return cls(sport=str(data["sport"]), champion=champion, ensemble=ensemble)


def _logloss(p: np.ndarray, y: np.ndarray) -> float:
    eps = 1e-12
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def train_champion(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    sport: str,
    candidate_C: tuple = (0.3, 1.0, 3.0),
    ensemble_size: int = 8,
    seed: int = 0,
) -> ResidualWinProbModel:
    """Fit candidate models, select the champion by validation log-loss, and
    train a bootstrap ensemble at the champion's regularization."""

    best: Optional[LinearModel] = None
    best_C = candidate_C[0]
    best_loss = np.inf
    for C in candidate_C:
        cand = _fit_linear(X_train, y_train, C)
        loss = _logloss(cand.proba(X_val), y_val)
        if loss < best_loss:
            best_loss, best, best_C = loss, cand, C

    rng = np.random.default_rng(seed)
    ensemble: List[LinearModel] = []
    n = X_train.shape[0]
    for _ in range(ensemble_size):
        idx = rng.integers(0, n, size=n)  # bootstrap resample
        ensemble.append(_fit_linear(X_train[idx], y_train[idx], best_C))

    assert best is not None
    return ResidualWinProbModel(sport=sport, champion=best, ensemble=ensemble)
