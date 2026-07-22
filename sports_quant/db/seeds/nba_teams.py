"""Canonical NBA league and team seed data.

Same contract as the MLB seed: static, source-controlled, non-secret,
deterministic, and usable with no network access.
"""

from __future__ import annotations

from typing import Final

from .mlb_teams import TeamSeed

LEAGUE_CODE: Final = "NBA"
LEAGUE_NAME: Final = "National Basketball Association"
LEAGUE_SPORT: Final = "basketball"

#: All 30 current NBA franchises, ordered by conference then division.
NBA_TEAMS: Final[tuple[TeamSeed, ...]] = (
    # --- Eastern Conference: Atlantic ---
    TeamSeed("BOS", "Boston", "Celtics", ()),
    TeamSeed("BKN", "Brooklyn", "Nets", ("BRK", "NJN", "New Jersey Nets")),
    TeamSeed("NYK", "New York", "Knicks", ("NY Knicks", "New York Knickerbockers")),
    TeamSeed("PHI", "Philadelphia", "76ers", ("PHL", "Sixers")),
    TeamSeed("TOR", "Toronto", "Raptors", ()),
    # --- Eastern Conference: Central ---
    TeamSeed("CHI", "Chicago", "Bulls", ()),
    TeamSeed("CLE", "Cleveland", "Cavaliers", ("CAVS",)),
    TeamSeed("DET", "Detroit", "Pistons", ()),
    TeamSeed("IND", "Indiana", "Pacers", ()),
    TeamSeed("MIL", "Milwaukee", "Bucks", ()),
    # --- Eastern Conference: Southeast ---
    TeamSeed("ATL", "Atlanta", "Hawks", ()),
    TeamSeed("CHA", "Charlotte", "Hornets", ("CHO", "Charlotte Bobcats")),
    TeamSeed("MIA", "Miami", "Heat", ()),
    TeamSeed("ORL", "Orlando", "Magic", ()),
    TeamSeed("WAS", "Washington", "Wizards", ("WSH", "Washington Bullets")),
    # --- Western Conference: Northwest ---
    TeamSeed("DEN", "Denver", "Nuggets", ()),
    TeamSeed("MIN", "Minnesota", "Timberwolves", ("Wolves",)),
    TeamSeed("OKC", "Oklahoma City", "Thunder", ("SEA", "Seattle SuperSonics")),
    TeamSeed("POR", "Portland", "Trail Blazers", ("Blazers", "Portland Trailblazers")),
    TeamSeed("UTA", "Utah", "Jazz", ("UTH",)),
    # --- Western Conference: Pacific ---
    TeamSeed("GSW", "Golden State", "Warriors", ("GS",)),
    # The Clippers' canonical name uses "LA", not "Los Angeles" -- that is the
    # franchise's own branding, and it is what distinguishes them from the
    # Lakers in a provider feed.
    TeamSeed(
        "LAC",
        "LA",
        "Clippers",
        ("Los Angeles Clippers", "San Diego Clippers"),
        extra_cities=("Los Angeles",),
    ),
    TeamSeed("LAL", "Los Angeles", "Lakers", ("LA Lakers",)),
    TeamSeed("PHX", "Phoenix", "Suns", ("PHO",)),
    TeamSeed("SAC", "Sacramento", "Kings", ()),
    # --- Western Conference: Southwest ---
    TeamSeed("DAL", "Dallas", "Mavericks", ("Mavs",)),
    TeamSeed("HOU", "Houston", "Rockets", ()),
    TeamSeed("MEM", "Memphis", "Grizzlies", ("VAN", "Vancouver Grizzlies")),
    TeamSeed("NOP", "New Orleans", "Pelicans", ("NOH", "New Orleans Hornets")),
    TeamSeed("SAS", "San Antonio", "Spurs", ("SA",)),
)
