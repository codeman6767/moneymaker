"""In-memory live state (Module 2).

Single-writer, sequence-tracked, atomically-snapshottable state models for MLB
games, NBA games and Kalshi order books. Event application is O(1)/O(log n) and
never performs I/O; snapshots are immutable and content-hashed.

See ``CLAUDE.md`` for the rules upheld here (no hot-path DB/model loads,
sequence-gap safety, monotonic measurement).
"""

from .base import (
    ApplyResult,
    ApplyStatus,
    DataQuality,
    LiveState,
    StateSnapshot,
    compute_state_hash,
)
from .benchmark import BenchmarkResult, run_benchmarks
from .mlb import MLBGameState
from .nba import NBAGameState
from .orderbook import OrderBookState
from .store import LiveStateStore, default_entity_key

__all__ = [
    "ApplyResult",
    "ApplyStatus",
    "DataQuality",
    "LiveState",
    "StateSnapshot",
    "compute_state_hash",
    "MLBGameState",
    "NBAGameState",
    "OrderBookState",
    "LiveStateStore",
    "default_entity_key",
    "BenchmarkResult",
    "run_benchmarks",
]
