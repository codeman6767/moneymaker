"""Typed repositories for the Phase A tables.

Every repository is a ``Protocol`` plus a SQLite implementation, following the
``tracking/base.py`` pattern already used in this repository. The Protocol is
what keeps a later PostgreSQL implementation additive rather than a rewrite.

All SQL lives here. Nothing outside this package writes a query, so a schema
change has exactly one blast radius.
"""

from __future__ import annotations

from .base import Repository, RepositoryError, to_db_bool
from .capabilities import (
    CapabilityRepositoryProtocol,
    SqliteCapabilityRepository,
    capability_content_hash,
)
from .data_quality import (
    DataQualityRepositoryProtocol,
    SqliteDataQualityRepository,
)
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
    UpsertOutcome,
    orderbook_content_hash,
    trade_content_hash,
)
from .leagues import (
    LeagueRepositoryProtocol,
    SeasonRepositoryProtocol,
    SqliteLeagueRepository,
    SqliteSeasonRepository,
)
from .matching import (
    CandidateInput,
    MatchingRepositoryProtocol,
    SqliteMatchingRepository,
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
from .references import (
    ProviderReferenceRepositoryProtocol,
    SqliteProviderReferenceRepository,
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
from .venues import (
    SqliteVenueRepository,
    VenueRepositoryProtocol,
    validate_venue_fields,
)

__all__ = [
    "CandidateInput",
    "CapabilityRepositoryProtocol",
    "DataQualityRepositoryProtocol",
    "GameRepositoryProtocol",
    "IngestionRunRepositoryProtocol",
    "KalshiRepositoryProtocol",
    "LeagueRepositoryProtocol",
    "MatchingRepositoryProtocol",
    "PlayerAliasRepositoryProtocol",
    "PlayerRepositoryProtocol",
    "ProviderReferenceRepositoryProtocol",
    "RawResponseRepositoryProtocol",
    "Repository",
    "RepositoryError",
    "SeasonRepositoryProtocol",
    "SportsbookRepositoryProtocol",
    "SqliteCapabilityRepository",
    "SqliteDataQualityRepository",
    "SqliteGameRepository",
    "SqliteIngestionRunRepository",
    "SqliteKalshiRepository",
    "SqliteLeagueRepository",
    "SqliteMatchingRepository",
    "SqlitePlayerAliasRepository",
    "SqlitePlayerRepository",
    "SqliteProviderReferenceRepository",
    "SqliteRawResponseRepository",
    "SqliteSeasonRepository",
    "SqliteSportsbookRepository",
    "SqliteTeamAliasRepository",
    "SqliteTeamRepository",
    "SqliteVenueRepository",
    "TeamAliasRepositoryProtocol",
    "TeamRepositoryProtocol",
    "UpsertOutcome",
    "VenueRepositoryProtocol",
    "body_hash",
    "capability_content_hash",
    "orderbook_content_hash",
    "point_key",
    "price_content_hash",
    "response_content_hash",
    "status_content_hash",
    "to_db_bool",
    "trade_content_hash",
    "validate_venue_fields",
]
