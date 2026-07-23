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
from .ingestion_runs import (
    IngestionRunRepositoryProtocol,
    SqliteIngestionRunRepository,
)
from .kalshi import (
    KalshiRepositoryProtocol,
    SqliteKalshiRepository,
    orderbook_content_hash,
    trade_content_hash,
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
from .raw_responses import (
    RawResponseRepositoryProtocol,
    SqliteRawResponseRepository,
    body_hash,
    response_content_hash,
)
from .sportsbook import (
    SportsbookRepositoryProtocol,
    SqliteSportsbookRepository,
    point_key,
    price_content_hash,
)
from .teams import (
    SqliteTeamAliasRepository,
    SqliteTeamRepository,
    TeamAliasRepositoryProtocol,
    TeamRepositoryProtocol,
)

__all__ = [
    "GameRepositoryProtocol",
    "IngestionRunRepositoryProtocol",
    "KalshiRepositoryProtocol",
    "LeagueRepositoryProtocol",
    "PlayerAliasRepositoryProtocol",
    "PlayerRepositoryProtocol",
    "RawResponseRepositoryProtocol",
    "Repository",
    "RepositoryError",
    "SeasonRepositoryProtocol",
    "SportsbookRepositoryProtocol",
    "SqliteGameRepository",
    "SqliteIngestionRunRepository",
    "SqliteKalshiRepository",
    "SqliteLeagueRepository",
    "SqlitePlayerAliasRepository",
    "SqlitePlayerRepository",
    "SqliteRawResponseRepository",
    "SqliteSeasonRepository",
    "SqliteSportsbookRepository",
    "SqliteTeamAliasRepository",
    "SqliteTeamRepository",
    "TeamAliasRepositoryProtocol",
    "TeamRepositoryProtocol",
    "body_hash",
    "orderbook_content_hash",
    "point_key",
    "price_content_hash",
    "response_content_hash",
    "status_content_hash",
    "to_db_bool",
    "trade_content_hash",
]
