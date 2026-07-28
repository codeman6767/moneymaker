"""Phase E1 point-in-time (as-of) access + leakage-guard foundation.

Offline and database-read-only. Every feature-facing historical read requires an
explicit UTC :class:`Cutoff` and uses transaction time, never provider time or
mutable current state. A fail-closed :mod:`registry` classifies every future
dataset join. Closing lines live ONLY in :mod:`evaluation_only` and are
intentionally NOT re-exported here, so importing the ``pit`` feature surface can
never reach them. The final ``GameStateDataset`` row builder is Phase E2 and is
not part of this package yet.
"""

from __future__ import annotations

from .asof import (
    AsOfAmbiguityError,
    AsOfReader,
    deterministic_json,
    latest_as_of,
    read_only_connection,
)
from .models import (
    Cutoff,
    GameScheduleState,
    LinkAsOf,
    MarketIdentity,
    MatchDecisionView,
    Observation,
)
from .registry import (
    TABLE_REGISTRY,
    ForbiddenColumnError,
    ForbiddenJoinError,
    TableClass,
    TableEntry,
    UnknownTableError,
    assert_column_readable,
    assert_joinable,
    assert_selectable,
    classify,
    registered_tables,
    require_asof,
)

__all__ = [
    "Cutoff",
    "Observation",
    "MatchDecisionView",
    "LinkAsOf",
    "GameScheduleState",
    "MarketIdentity",
    "AsOfReader",
    "AsOfAmbiguityError",
    "latest_as_of",
    "read_only_connection",
    "deterministic_json",
    "TableClass",
    "TableEntry",
    "TABLE_REGISTRY",
    "classify",
    "require_asof",
    "assert_joinable",
    "assert_column_readable",
    "assert_selectable",
    "registered_tables",
    "UnknownTableError",
    "ForbiddenJoinError",
    "ForbiddenColumnError",
]
