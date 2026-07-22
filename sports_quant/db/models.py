"""Typed row models for the Phase A tables.

Frozen dataclasses rather than raw ``sqlite3.Row`` mappings, so a caller gets a
checked attribute instead of a string key lookup that fails at runtime. Kept in
one module so repositories can reference each other's models without an import
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .schema import NO_PROVIDER, SEASON_UNBOUNDED_END, SEASON_UNBOUNDED_START


@dataclass(frozen=True)
class League:
    league_id: str
    code: str
    name: str
    sport: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Season:
    season_id: str
    league_id: str
    year: int
    label: str
    phase: str
    start_date: str
    end_date: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Team:
    team_id: str
    league_id: str
    canonical_name: str
    city: str
    nickname: str
    abbreviation: str
    first_season: Optional[int]
    last_season: Optional[int]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TeamAlias:
    alias_id: str
    team_id: str
    league_id: str
    alias: str
    normalized: str
    alias_type: str
    provider: str = NO_PROVIDER
    valid_from_season: int = SEASON_UNBOUNDED_START
    valid_to_season: int = SEASON_UNBOUNDED_END
    is_ambiguous: bool = False
    source: str = "seed"
    created_at: str = ""


@dataclass(frozen=True)
class Player:
    player_id: str
    league_id: str
    full_name: str
    first_name: Optional[str]
    last_name: Optional[str]
    suffix: Optional[str]
    birth_date: Optional[str]
    primary_position: Optional[str]
    debut_date: Optional[str]
    final_game_date: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlayerAlias:
    alias_id: str
    player_id: str
    league_id: str
    alias: str
    normalized: str
    suffix: str = ""
    alias_type: str = "full"
    provider: str = NO_PROVIDER
    is_ambiguous: bool = False
    source: str = "seed"
    created_at: str = ""


@dataclass(frozen=True)
class Game:
    game_id: str
    league_id: str
    season_id: str
    home_team_id: str
    away_team_id: str
    scheduled_start: str
    original_start: str
    game_date_local: str
    game_number: int
    doubleheader_type: Optional[str]
    venue: Optional[str]
    is_neutral_site: bool
    status: str
    official_provider: Optional[str]
    official_game_key: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GameStatusRecord:
    """One append-only observation of a game's status.

    ``observed_at`` is the point-in-time cutoff column: when *we* learned this.
    ``provider_timestamp`` is when the provider says it became true, and is
    nullable because many providers omit it.
    """

    status_id: str
    game_id: str
    status: str
    scheduled_start: str
    provider: str
    observed_at: str
    ingested_at: str
    content_hash: str
    detail: Optional[str] = None
    provider_timestamp: Optional[str] = None
    raw_response_id: Optional[str] = None
    raw_response_hash: Optional[str] = None
    created_at: str = ""
