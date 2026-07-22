"""Configurable latency model for the eight pipeline stages.

Each stage has a mean delay and optional jitter; sampling is deterministic given
a seeded NumPy generator. The stages roll up into the three components the rest
of the system reports separately (never conflated, per ``CLAUDE.md``):

* provider lag   = provider_publication + network_delivery
* internal       = decoding + state_update + inference + risk_checks + order_submission
* exchange       = exchange_acknowledgement
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


STAGES: tuple = (
    "provider_publication",
    "network_delivery",
    "decoding",
    "state_update",
    "inference",
    "risk_checks",
    "order_submission",
    "exchange_acknowledgement",
)

PROVIDER_STAGES = ("provider_publication", "network_delivery")
INTERNAL_STAGES = ("decoding", "state_update", "inference", "risk_checks", "order_submission")
EXCHANGE_STAGES = ("exchange_acknowledgement",)


@dataclass(frozen=True)
class StageDelay:
    mean_ns: int
    jitter_ns: int = 0  # +/- uniform jitter

    def sample(self, rng) -> int:
        if self.jitter_ns <= 0:
            return self.mean_ns
        return int(max(0, self.mean_ns + rng.integers(-self.jitter_ns, self.jitter_ns + 1)))


@dataclass
class LatencySample:
    stages: Dict[str, int]

    @property
    def provider_lag_ns(self) -> int:
        return sum(self.stages[s] for s in PROVIDER_STAGES)

    @property
    def internal_ns(self) -> int:
        return sum(self.stages[s] for s in INTERNAL_STAGES)

    @property
    def exchange_ns(self) -> int:
        return sum(self.stages[s] for s in EXCHANGE_STAGES)

    @property
    def total_ns(self) -> int:
        return sum(self.stages.values())


def _default_delays() -> Dict[str, StageDelay]:
    # Plausible defaults (ns); every one is overridable.
    return {
        "provider_publication": StageDelay(50_000_000, 20_000_000),   # 50ms
        "network_delivery": StageDelay(30_000_000, 15_000_000),       # 30ms
        "decoding": StageDelay(200_000, 100_000),                     # 0.2ms
        "state_update": StageDelay(150_000, 50_000),
        "inference": StageDelay(300_000, 100_000),
        "risk_checks": StageDelay(100_000, 50_000),
        "order_submission": StageDelay(5_000_000, 2_000_000),         # 5ms
        "exchange_acknowledgement": StageDelay(20_000_000, 10_000_000),  # 20ms
    }


@dataclass
class LatencyModel:
    delays: Dict[str, StageDelay] = field(default_factory=_default_delays)

    def __post_init__(self) -> None:
        missing = set(STAGES) - set(self.delays)
        if missing:
            raise ValueError(f"latency model missing stages: {sorted(missing)}")

    def sample(self, rng) -> LatencySample:
        return LatencySample({s: self.delays[s].sample(rng) for s in STAGES})

    def mean_total_ns(self) -> int:
        return sum(d.mean_ns for d in self.delays.values())
