"""Deterministic, offline seed data for canonical leagues and teams."""

from __future__ import annotations

from .loader import LeagueSeedResult, SeedResult, alias_specs, seed_all, seed_league
from .mlb_teams import MLB_TEAMS, TeamSeed
from .nba_teams import NBA_TEAMS

__all__ = [
    "MLB_TEAMS",
    "NBA_TEAMS",
    "LeagueSeedResult",
    "SeedResult",
    "TeamSeed",
    "alias_specs",
    "seed_all",
    "seed_league",
]
