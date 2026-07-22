"""Migration a003: cross-table league consistency enforced by the database.

Foreign keys prove a referenced row *exists*; they cannot prove it belongs to
the same league. Every test here writes raw SQL rather than going through a
repository, because the point is that the **database** rejects the row -- a
guard that only lives in repository code is bypassed by anything else holding a
connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.repositories import (
    SqliteGameRepository,
    SqlitePlayerRepository,
    SqliteSeasonRepository,
    SqliteTeamAliasRepository,
)

T0 = "2026-07-01T00:00:00.000000Z"
START = "2026-07-04T23:05:00.000000Z"


@pytest.fixture
def mlb_season(conn: sqlite3.Connection, mlb_league_id: str) -> str:
    return SqliteSeasonRepository(conn).upsert(
        league_code="MLB", league_id=mlb_league_id, year=2026, phase="regular",
        label="2026", start_date="2026-03-26",
    ).season_id


@pytest.fixture
def nba_season(conn: sqlite3.Connection, nba_league_id: str) -> str:
    return SqliteSeasonRepository(conn).upsert(
        league_code="NBA", league_id=nba_league_id, year=2026, phase="regular",
        label="2025-26", start_date="2025-10-21",
    ).season_id


def _insert_game(
    conn: sqlite3.Connection,
    *,
    league_id: str,
    season_id: str,
    home_team_id: str,
    away_team_id: str,
    game_id: str = "gm_probe",
) -> None:
    conn.execute(
        "INSERT INTO games (game_id, league_id, season_id, home_team_id, away_team_id, "
        "scheduled_start, original_start, game_date_local, game_number, is_neutral_site, "
        "status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-07-04', 1, 0, 'scheduled', ?, ?)",
        (game_id, league_id, season_id, home_team_id, away_team_id, START, START, T0, T0),
    )


# --------------------------------------------------------------------------- #
# games <-> season / teams league consistency
# --------------------------------------------------------------------------- #
def test_mlb_game_cannot_reference_an_nba_season(
    conn: sqlite3.Connection, mlb_league_id: str, nba_season: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="league of games.season_id"):
        _insert_game(
            conn, league_id=mlb_league_id, season_id=nba_season,
            home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        )


def test_mlb_game_cannot_reference_an_nba_home_team(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="league of games.home_team_id"):
        _insert_game(
            conn, league_id=mlb_league_id, season_id=mlb_season,
            home_team_id="tm_nba_bos", away_team_id="tm_mlb_bos",
        )


def test_mlb_game_cannot_reference_an_nba_away_team(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="league of games.away_team_id"):
        _insert_game(
            conn, league_id=mlb_league_id, season_id=mlb_season,
            home_team_id="tm_mlb_nyy", away_team_id="tm_nba_bos",
        )


def test_valid_same_league_game_still_works(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str
) -> None:
    """The guards must not obstruct legitimate writes."""

    game = SqliteGameRepository(conn).create(
        league_id=mlb_league_id, season_id=mlb_season,
        home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        scheduled_start=START, game_date_local="2026-07-04",
    )
    assert game.game_id.startswith("gm_")


def test_valid_nba_game_still_works(
    conn: sqlite3.Connection, nba_league_id: str, nba_season: str
) -> None:
    game = SqliteGameRepository(conn).create(
        league_id=nba_league_id, season_id=nba_season,
        home_team_id="tm_nba_bos", away_team_id="tm_nba_lal",
        scheduled_start=START, game_date_local="2026-07-04",
    )
    assert game.league_id == nba_league_id


def test_update_cannot_move_a_game_to_a_foreign_league_team(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str
) -> None:
    """The guard covers UPDATE, not only INSERT."""

    game = SqliteGameRepository(conn).create(
        league_id=mlb_league_id, season_id=mlb_season,
        home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        scheduled_start=START, game_date_local="2026-07-04",
    )
    with pytest.raises(sqlite3.IntegrityError, match="league of games.home_team_id"):
        conn.execute(
            "UPDATE games SET home_team_id = 'tm_nba_bos' WHERE game_id = ?", (game.game_id,)
        )


def test_update_cannot_move_a_game_to_a_foreign_league_season(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str, nba_season: str
) -> None:
    game = SqliteGameRepository(conn).create(
        league_id=mlb_league_id, season_id=mlb_season,
        home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        scheduled_start=START, game_date_local="2026-07-04",
    )
    with pytest.raises(sqlite3.IntegrityError, match="league of games.season_id"):
        conn.execute(
            "UPDATE games SET season_id = ? WHERE game_id = ?", (nba_season, game.game_id)
        )


def test_ordinary_status_update_is_unaffected(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str
) -> None:
    """The UPDATE guard is column-scoped, so status writes stay cheap."""

    repo = SqliteGameRepository(conn)
    game = repo.create(
        league_id=mlb_league_id, season_id=mlb_season,
        home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        scheduled_start=START, game_date_local="2026-07-04",
    )
    assert repo.record_status(
        game_id=game.game_id, status="final", scheduled_start=START,
        provider="test", observed_at=T0,
    ) is True


# --------------------------------------------------------------------------- #
# games.original_start immutability
# --------------------------------------------------------------------------- #
def test_original_start_cannot_be_changed(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str
) -> None:
    game = SqliteGameRepository(conn).create(
        league_id=mlb_league_id, season_id=mlb_season,
        home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        scheduled_start=START, game_date_local="2026-07-04",
    )
    with pytest.raises(sqlite3.IntegrityError, match="original_start is immutable"):
        conn.execute(
            "UPDATE games SET original_start = '2026-08-01T00:00:00.000000Z' "
            "WHERE game_id = ?",
            (game.game_id,),
        )


def test_writing_the_same_original_start_is_allowed(
    conn: sqlite3.Connection, mlb_league_id: str, mlb_season: str
) -> None:
    """A no-op rewrite is not a change; only a differing value is rejected."""

    game = SqliteGameRepository(conn).create(
        league_id=mlb_league_id, season_id=mlb_season,
        home_team_id="tm_mlb_nyy", away_team_id="tm_mlb_bos",
        scheduled_start=START, game_date_local="2026-07-04",
    )
    conn.execute(
        "UPDATE games SET original_start = ? WHERE game_id = ?", (START, game.game_id)
    )


# --------------------------------------------------------------------------- #
# Alias league consistency
# --------------------------------------------------------------------------- #
def test_team_alias_cannot_use_a_foreign_league(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="team_aliases.league_id must match"):
        conn.execute(
            "INSERT INTO team_aliases (alias_id, team_id, league_id, alias, normalized, "
            "alias_type, provider, valid_from_season, valid_to_season, is_ambiguous, "
            "source, created_at) VALUES "
            "('tal_bad', 'tm_mlb_nyy', ?, 'Bombers', 'bombers', 'nickname', '', 0, 9999, "
            "0, 'manual', ?)",
            (nba_league_id, T0),
        )


def test_team_alias_update_cannot_move_it_to_a_foreign_league(
    conn: sqlite3.Connection, mlb_league_id: str, nba_league_id: str
) -> None:
    repo = SqliteTeamAliasRepository(conn)
    repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id, alias="Bombers",
             alias_type="nickname", source="manual")
    with pytest.raises(sqlite3.IntegrityError, match="team_aliases.league_id must match"):
        conn.execute(
            "UPDATE team_aliases SET league_id = ? WHERE team_id = 'tm_mlb_nyy'",
            (nba_league_id,),
        )


def test_player_alias_cannot_use_a_foreign_league(
    conn: sqlite3.Connection, mlb_league_id: str, nba_league_id: str
) -> None:
    player = SqlitePlayerRepository(conn).create(
        league_id=mlb_league_id, full_name="Real Person"
    )
    with pytest.raises(sqlite3.IntegrityError, match="player_aliases.league_id must match"):
        conn.execute(
            "INSERT INTO player_aliases (alias_id, player_id, league_id, alias, normalized, "
            "suffix, alias_type, provider, is_ambiguous, source, created_at) VALUES "
            "('pal_bad', ?, ?, 'Real Person', 'real person', '', 'full', '', 0, 'manual', ?)",
            (player.player_id, nba_league_id, T0),
        )


def test_player_alias_update_cannot_move_it_to_a_foreign_league(
    conn: sqlite3.Connection, mlb_league_id: str, nba_league_id: str
) -> None:
    from sports_quant.db.repositories import SqlitePlayerAliasRepository

    player = SqlitePlayerRepository(conn).create(
        league_id=mlb_league_id, full_name="Real Person"
    )
    SqlitePlayerAliasRepository(conn).add(
        player_id=player.player_id, league_id=mlb_league_id, alias="Real Person"
    )
    with pytest.raises(sqlite3.IntegrityError, match="player_aliases.league_id must match"):
        conn.execute(
            "UPDATE player_aliases SET league_id = ? WHERE player_id = ?",
            (nba_league_id, player.player_id),
        )


def test_matching_league_aliases_still_insert(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    assert SqliteTeamAliasRepository(conn).add(
        team_id="tm_mlb_nyy", league_id=mlb_league_id, alias="Bombers",
        alias_type="nickname", source="manual",
    ) is True


def test_seed_load_is_unaffected_by_the_alias_guards(conn: sqlite3.Connection) -> None:
    """311 seeded aliases already passed the guards during db-init."""

    assert SqliteTeamAliasRepository(conn).count() == 311
