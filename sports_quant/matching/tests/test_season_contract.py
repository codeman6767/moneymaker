"""One MLB/NBA season-year contract (task §6).

The integer is always the season's START year: MLB ``2026`` = the 2026 season;
NBA ``2025`` = the 2025-26 season. ``schema.season_label``,
``season.season_year_for``, and ``season.season_bounds`` must agree, and an
official-game match must create season rows under the same convention.
"""

from __future__ import annotations

import sqlite3

from sports_quant.db.schema import season_label
from sports_quant.matching.season import in_season, season_bounds, season_year_for

from .conftest import seed_schedule
from .test_phase_d5a_matching import NBA, _match, _nba_setup


def test_mlb_regular_label() -> None:
    assert season_label("MLB", 2026, "regular") == "2026"


def test_mlb_postseason_label() -> None:
    assert season_label("MLB", 2026, "postseason") == "2026 postseason"


def test_nba_regular_label_uses_start_year() -> None:
    assert season_label("NBA", 2025, "regular") == "2025-26"


def test_nba_postseason_label_preserves_start_year() -> None:
    assert season_label("NBA", 2025, "postseason") == "2025-26 postseason"


def test_january_2026_is_nba_season_2025() -> None:
    assert season_year_for("NBA", "2026-01-15") == 2025


def test_june_july_boundary() -> None:
    assert season_year_for("NBA", "2026-06-30") == 2025  # end of 2025-26
    assert season_year_for("NBA", "2026-07-01") == 2026  # start of 2026-27


def test_round_trip_consistency() -> None:
    # For both leagues, the start year that season_year_for assigns to a date
    # must place that date inside season_bounds, and season_label must render the
    # same start year -- the three helpers cannot disagree.
    for league, date in (("MLB", "2026-07-24"), ("NBA", "2026-04-10"), ("NBA", "2025-11-01")):
        y = season_year_for(league, date)
        lo, hi = season_bounds(league, y)
        assert lo <= date <= hi and in_season(league, y, date)
        label = season_label(league, y, "regular")
        assert label.startswith(str(y))  # start year leads the label


def test_official_created_season_row_uses_start_year(conn: sqlite3.Connection) -> None:
    # An NBA April-2026 game belongs to season 2025 (start year); the created
    # season row must carry that integer and the 2025-26 label.
    _nba_setup(conn)
    seed_schedule(conn, provider=NBA, provider_game_id="NG1", home_provider_team_id="201",
                  away_provider_team_id="202", scheduled_start="2026-04-10T23:30:00Z",
                  season=2025, game_date_local="2026-04-10", venue_provider_id="NV1")
    _match(conn, provider=NBA, from_date="2026-04-10", to_date="2026-04-10")
    row = conn.execute(
        "SELECT year, label FROM seasons WHERE league_id='lg_nba'").fetchone()
    assert row is not None and int(row["year"]) == 2025 and row["label"] == "2025-26"
