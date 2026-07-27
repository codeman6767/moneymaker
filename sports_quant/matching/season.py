"""League-specific season calendar boundaries for D5A matching.

The two providers number a season differently, and an NBA season spans two
calendar years, so a single ``roster_date LIKE '<year>-%'`` rule is wrong. The
convention here is the providers' own, documented explicitly:

* **MLB (MLB StatsAPI)** -- ``season`` is the *calendar year*. The season
  interval is the whole of that year, ``[Y-01-01, Y-12-31]`` (spring training
  through the postseason all fall inside one calendar year).
* **NBA (BALLDONTLIE)** -- ``season`` is the *start year* of a two-year season:
  ``2024`` denotes the **2024-25** season (tip-off October 2024, Finals June
  2025). The interval is ``[Y-07-01, (Y+1)-06-30]`` -- a July-to-June split that
  assigns every date to exactly one NBA season, offseason roster moves included.

The canonical ``seasons`` rows are not yet curated with real start/end dates
(Phase D writes placeholder ``Y-01-01`` / ``NULL``), so this deterministic
convention -- not those placeholders -- is the source of truth for season
membership. Roster-team filtering and alias career-window filtering both go
through :func:`season_bounds`, so they can never disagree about which season a
date belongs to.
"""

from __future__ import annotations


def league_code_from_id(league_id: str) -> str:
    """``'lg_nba'`` -> ``'NBA'``."""

    return league_id.removeprefix("lg_").upper()


def season_bounds(league_code: str, season_year: int) -> tuple[str, str]:
    """Inclusive ``(start_date, end_date)`` ISO dates for a league season year."""

    if league_code.upper() == "NBA":
        return (f"{season_year}-07-01", f"{season_year + 1}-06-30")
    return (f"{season_year}-01-01", f"{season_year}-12-31")


def in_season(league_code: str, season_year: int, date_iso: str) -> bool:
    """Whether an ISO ``YYYY-MM-DD`` date falls within the league season year."""

    lo, hi = season_bounds(league_code, season_year)
    return lo <= date_iso <= hi


def season_year_for(league_code: str, date_iso: str) -> int:
    """The league season *start year* an ISO ``YYYY-MM-DD[...]`` date belongs to.

    MLB numbers a season by its calendar year, so the season year is simply the
    year of the date. An NBA season is numbered by its **start** year and runs
    July -> June (see :func:`season_bounds`): a July-December date belongs to
    that calendar year's season, and a January-June date belongs to the
    *previous* calendar year's season (``2026-04`` is the ``2025``-26 season).

    This is the exact inverse of the July split in :func:`season_bounds`; the
    boundary is defined here once so no caller re-derives it. ``date_iso`` must
    begin with ``YYYY-MM``; callers validate the timestamp before calling, so a
    naive/malformed instant never reaches this function and never produces a
    guessed season.
    """

    year = int(date_iso[:4])
    if league_code.upper() == "NBA":
        month = int(date_iso[5:7])
        return year if month >= 7 else year - 1
    return year
