"""Typed repositories for the Phase A tables.

Every repository is a ``Protocol`` plus a SQLite implementation, following the
``tracking/base.py`` pattern already used in this repository. The Protocol is
what keeps a later PostgreSQL implementation additive rather than a rewrite.

All SQL lives here. Nothing outside this package writes a query, so a schema
change has exactly one blast radius.
"""

from __future__ import annotations

from .base import Repository, RepositoryError, to_db_bool
from .games import (
    GameRepositoryProtocol,
    SqliteGameRepository,
    status_content_hash,
)
from .leagues import (
    LeagueRepositoryProtocol,
    SeasonRepositoryProtocol,
    SqliteLeagueRepository,
    SqliteSeasonRepository,
)
from .players import (
    PlayerAliasRepositoryProtocol,
    PlayerRepositoryProtocol,
    SqlitePlayerAliasRepository,
    SqlitePlayerRepository,
)
from .teams import (
    SqliteTeamAliasRepository,
    SqliteTeamRepository,
    TeamAliasRepositoryProtocol,
    TeamRepositoryProtocol,
)

__all__ = [
    "GameRepositoryProtocol",
    "LeagueRepositoryProtocol",
    "PlayerAliasRepositoryProtocol",
    "PlayerRepositoryProtocol",
    "Repository",
    "RepositoryError",
    "SeasonRepositoryProtocol",
    "SqliteGameRepository",
    "SqliteLeagueRepository",
    "SqlitePlayerAliasRepository",
    "SqlitePlayerRepository",
    "SqliteSeasonRepository",
    "SqliteTeamAliasRepository",
    "SqliteTeamRepository",
    "TeamAliasRepositoryProtocol",
    "TeamRepositoryProtocol",
    "status_content_hash",
    "to_db_bool",
]
