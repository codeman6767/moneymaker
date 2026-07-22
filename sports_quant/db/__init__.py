"""Historical-corpus database layer (Phase A).

SQLite storage for the canonical entities every later phase links to: leagues,
seasons, teams, players, their alias tables, games, and the append-only game
status history.

**This package is research-lane and ingestion-lane only.** ``CLAUDE.md``
forbids querying a database on the hot decision path, so nothing in
``probability/``, ``state/``, ``evaluation/`` or ``gateway/`` may import it --
an isolation test enforces that, mirroring the existing check that keeps
``sqlite3`` out of ``probability/``.

Nothing here places, cancels or manages a bet, and no network call is made.
"""

from __future__ import annotations

from .engine import (
    Database,
    DatabaseError,
    Migration,
    MigrationChecksumError,
    MigrationError,
    MigrationResult,
    configure_connection,
    discover_migrations,
    foreign_keys_enabled,
    table_exists,
    transaction,
)
from .init import DbInitResult, initialize_database
from .models import (
    Game,
    GameStatusRecord,
    League,
    Player,
    PlayerAlias,
    Season,
    Team,
    TeamAlias,
)
from .normalize import (
    AliasCandidate,
    AliasMatchStatus,
    AliasResolution,
    NormalizedName,
    normalize_name,
    normalized_key,
    resolve_alias,
)

__all__ = [
    "AliasCandidate",
    "AliasMatchStatus",
    "AliasResolution",
    "Database",
    "DatabaseError",
    "DbInitResult",
    "Game",
    "GameStatusRecord",
    "League",
    "Migration",
    "MigrationChecksumError",
    "MigrationError",
    "MigrationResult",
    "NormalizedName",
    "Player",
    "PlayerAlias",
    "Season",
    "Team",
    "TeamAlias",
    "configure_connection",
    "discover_migrations",
    "foreign_keys_enabled",
    "initialize_database",
    "normalize_name",
    "normalized_key",
    "resolve_alias",
    "table_exists",
    "transaction",
]
