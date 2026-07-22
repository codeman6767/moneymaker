"""Latency analytics: curves, distributions and break-even latency."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np


def _dist(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "count": int(arr.size),
        "mean": float(arr.mean()),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "max": float(arr.max()),
    }


def break_even_latency(latencies_ns: List[int], profit: List[float]) -> Optional[float]:
    """Latency at which mean expected profit crosses zero (linear interp).

    Returns ``None`` if profit never goes negative across the tested range;
    returns the first latency if it starts already unprofitable.
    """

    for i, y in enumerate(profit):
        if y < 0:
            if i == 0:
                return float(latencies_ns[0])
            x0, x1 = latencies_ns[i - 1], latencies_ns[i]
            y0, y1 = profit[i - 1], profit[i]
            if y0 == y1:
                return float(x1)
            frac = y0 / (y0 - y1)
            return float(x0 + frac * (x1 - x0))
    return None


@dataclass
class LatencyMetrics:
    latencies_ns: List[int]
    profit_by_latency: List[float]          # mean expected profit (cents) / decision
    edge_decay: List[float]                 # mean available edge (cents)
    fill_rate_by_latency: List[float]
    clv_by_latency: List[float]             # mean CLV (cents) over filled orders
    decision_count_by_latency: List[int]
    break_even_latency_ns: Optional[float]
    provider_lag_dist: Dict[str, Optional[float]]
    internal_latency_dist: Dict[str, Optional[float]]

    def as_dict(self) -> dict:
        return {
            "latencies_ns": self.latencies_ns,
            "profit_by_latency": self.profit_by_latency,
            "edge_decay": self.edge_decay,
            "fill_rate_by_latency": self.fill_rate_by_latency,
            "clv_by_latency": self.clv_by_latency,
            "decision_count_by_latency": self.decision_count_by_latency,
            "break_even_latency_ns": self.break_even_latency_ns,
            "provider_lag_dist": self.provider_lag_dist,
            "internal_latency_dist": self.internal_latency_dist,
        }


def build_distributions(provider_lags: List[float], internals: List[float]):
    return _dist(provider_lags), _dist(internals)
