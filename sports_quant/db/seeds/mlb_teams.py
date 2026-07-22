"""Canonical MLB league and team seed data.

Static, source-controlled, non-secret, and usable with no network access. The
data is deterministic: the same tuple order and the same deterministic team ids
on every run, so a rebuilt corpus is diffable against the original.

``extra_aliases`` holds additional handles a provider might use. City and
nickname aliases are derived automatically by the loader, so they are not
repeated here. Aliases that resolve to more than one team in the league (the
bare "chicago", "new york" and "los angeles") are flagged ambiguous by the
loader from the data itself rather than being hand-marked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True)
class TeamSeed:
    """One franchise: canonical fields plus any extra alias strings."""

    abbreviation: str
    city: str
    nickname: str
    extra_aliases: tuple[str, ...] = field(default_factory=tuple)
    #: Additional city strings a source may use for this franchise, beyond the
    #: canonical ``city``. These exist so genuine ambiguity is detectable: the
    #: Clippers brand themselves "LA", but a feed writing "Los Angeles" could
    #: mean them or the Lakers, and that must resolve to AMBIGUOUS rather than
    #: silently to whichever team happens to own the canonical city string.
    extra_cities: tuple[str, ...] = field(default_factory=tuple)

    @property
    def canonical_name(self) -> str:
        # The Athletics carry no city in their current canonical name.
        return f"{self.city} {self.nickname}".strip() if self.city else self.nickname


LEAGUE_CODE: Final = "MLB"
LEAGUE_NAME: Final = "Major League Baseball"
LEAGUE_SPORT: Final = "baseball"

#: All 30 current MLB franchises, ordered by league then division for review.
MLB_TEAMS: Final[tuple[TeamSeed, ...]] = (
    # --- American League East ---
    TeamSeed("BAL", "Baltimore", "Orioles", ("BLT",)),
    TeamSeed("BOS", "Boston", "Red Sox", ("BSN",)),
    TeamSeed("NYY", "New York", "Yankees", ("NYA", "NY Yankees")),
    TeamSeed("TB", "Tampa Bay", "Rays", ("TBR", "TBA", "Tampa Bay Devil Rays")),
    TeamSeed("TOR", "Toronto", "Blue Jays", ("TBJ",)),
    # --- American League Central ---
    TeamSeed("CWS", "Chicago", "White Sox", ("CHW", "CHA", "Chi White Sox")),
    TeamSeed("CLE", "Cleveland", "Guardians", ("CLV", "Cleveland Indians")),
    TeamSeed("DET", "Detroit", "Tigers", ()),
    TeamSeed("KC", "Kansas City", "Royals", ("KCR", "KCA")),
    TeamSeed("MIN", "Minnesota", "Twins", ()),
    # --- American League West ---
    TeamSeed("HOU", "Houston", "Astros", ()),
    TeamSeed("LAA", "Los Angeles", "Angels", ("ANA", "Anaheim Angels", "LA Angels")),
    # The Athletics dropped their city qualifier after leaving Oakland; the
    # historical Oakland handles remain as aliases so older rows still resolve.
    TeamSeed("ATH", "", "Athletics", ("OAK", "A's", "As", "Oakland Athletics", "Oakland A's")),
    TeamSeed("SEA", "Seattle", "Mariners", ()),
    TeamSeed("TEX", "Texas", "Rangers", ()),
    # --- National League East ---
    TeamSeed("ATL", "Atlanta", "Braves", ()),
    TeamSeed("MIA", "Miami", "Marlins", ("FLA", "Florida Marlins")),
    TeamSeed("NYM", "New York", "Mets", ("NYN", "NY Mets")),
    TeamSeed("PHI", "Philadelphia", "Phillies", ()),
    TeamSeed("WSH", "Washington", "Nationals", ("WAS", "WSN", "Montreal Expos")),
    # --- National League Central ---
    TeamSeed("CHC", "Chicago", "Cubs", ("CHN", "Chi Cubs")),
    TeamSeed("CIN", "Cincinnati", "Reds", ()),
    TeamSeed("MIL", "Milwaukee", "Brewers", ()),
    TeamSeed("PIT", "Pittsburgh", "Pirates", ()),
    TeamSeed("STL", "St. Louis", "Cardinals", ("SLN", "Saint Louis Cardinals")),
    # --- National League West ---
    TeamSeed("ARI", "Arizona", "Diamondbacks", ("AZ", "D-backs", "Dbacks")),
    TeamSeed("COL", "Colorado", "Rockies", ()),
    TeamSeed("LAD", "Los Angeles", "Dodgers", ("LAN", "LA Dodgers")),
    TeamSeed("SD", "San Diego", "Padres", ("SDP", "SDN")),
    TeamSeed("SF", "San Francisco", "Giants", ("SFG", "SFN")),
)
