"""Repository behaviour: CRUD, constraints, foreign keys, point-in-time reads."""

from __future__ import annotations

import sqlite3
from typing import Optional

import pytest

from sports_quant.db.engine import transaction
from sports_quant.db.models import Game
from sports_quant.db.normalize import AliasMatchStatus
from sports_quant.db.repositories import (
    SqliteGameRepository,
    SqliteLeagueRepository,
    SqlitePlayerAliasRepository,
    SqlitePlayerRepository,
    SqliteSeasonRepository,
    SqliteTeamAliasRepository,
    SqliteTeamRepository,
    status_content_hash,
)

T0 = "2026-07-01T00:00:00.000000Z"
T1 = "2026-07-02T00:00:00.000000Z"
T2 = "2026-07-03T00:00:00.000000Z"
START = "2026-07-04T23:05:00.000000Z"
MOVED = "2026-07-05T23:05:00.000000Z"


@pytest.fixture
def season(conn: sqlite3.Connection, mlb_league_id: str) -> str:
    repo = SqliteSeasonRepository(conn)
    return repo.upsert(
        league_code="MLB",
        league_id=mlb_league_id,
        year=2026,
        phase="regular",
        label="2026",
        start_date="2026-03-26",
    ).season_id


# --------------------------------------------------------------------------- #
# Leagues and seasons
# --------------------------------------------------------------------------- #
def test_leagues_are_seeded_once(conn: sqlite3.Connection) -> None:
    repo = SqliteLeagueRepository(conn)
    assert repo.count() == 2
    assert {lg.code for lg in repo.list_all()} == {"MLB", "NBA"}


def test_league_upsert_is_idempotent(conn: sqlite3.Connection) -> None:
    repo = SqliteLeagueRepository(conn)
    before = repo.count()
    again = repo.upsert(code="MLB", name="Major League Baseball", sport="baseball")
    assert repo.count() == before
    assert again.league_id == "lg_mlb"


def test_duplicate_league_code_is_rejected(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at, updated_at) "
            "VALUES ('lg_other', 'MLB', 'Duplicate', 'baseball', ?, ?)",
            (T0, T0),
        )


def test_unsupported_league_code_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at, updated_at) "
            "VALUES ('lg_nfl', 'NFL', 'Football', 'football', ?, ?)",
            (T0, T0),
        )


def test_season_upsert_is_idempotent(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    repo = SqliteSeasonRepository(conn)
    first = repo.upsert(
        league_code="MLB", league_id=mlb_league_id, year=2026, phase="regular",
        label="2026", start_date="2026-03-26",
    )
    second = repo.upsert(
        league_code="MLB", league_id=mlb_league_id, year=2026, phase="regular",
        label="2026", start_date="2026-03-26",
    )
    assert first.season_id == second.season_id == "sn_mlb_2026_regular"
    assert repo.count() == 1


def test_season_phases_are_separate_rows(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    repo = SqliteSeasonRepository(conn)
    repo.upsert(league_code="MLB", league_id=mlb_league_id, year=2026, phase="regular",
                label="2026", start_date="2026-03-26")
    repo.upsert(league_code="MLB", league_id=mlb_league_id, year=2026, phase="postseason",
                label="2026 postseason", start_date="2026-10-01")
    assert repo.count() == 2


def test_season_end_before_start_is_rejected(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date, "
            "end_date, created_at, updated_at) VALUES "
            "('sn_bad', ?, 2026, '2026', 'regular', '2026-09-01', '2026-03-01', ?, ?)",
            (mlb_league_id, T0, T0),
        )


def test_season_requires_an_existing_league(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date, "
            "created_at, updated_at) VALUES "
            "('sn_orphan', 'lg_does_not_exist', 2026, '2026', 'regular', '2026-03-26', ?, ?)",
            (T0, T0),
        )


# --------------------------------------------------------------------------- #
# Teams and team aliases
# --------------------------------------------------------------------------- #
def test_team_counts_per_league(conn: sqlite3.Connection, mlb_league_id: str,
                                nba_league_id: str) -> None:
    repo = SqliteTeamRepository(conn)
    assert repo.count_for_league(mlb_league_id) == 30
    assert repo.count_for_league(nba_league_id) == 30
    assert repo.count() == 60


def test_team_lookup_by_abbreviation(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    repo = SqliteTeamRepository(conn)
    team = repo.get_by_abbreviation(league_id=mlb_league_id, abbreviation="NYY")
    assert team is not None
    assert team.team_id == "tm_mlb_nyy"
    assert team.canonical_name == "New York Yankees"


def test_same_abbreviation_in_two_leagues_is_allowed(
    conn: sqlite3.Connection, mlb_league_id: str, nba_league_id: str
) -> None:
    repo = SqliteTeamRepository(conn)
    mlb_bos = repo.get_by_abbreviation(league_id=mlb_league_id, abbreviation="BOS")
    nba_bos = repo.get_by_abbreviation(league_id=nba_league_id, abbreviation="BOS")
    assert mlb_bos is not None and nba_bos is not None
    assert mlb_bos.team_id != nba_bos.team_id


def test_duplicate_team_abbreviation_within_a_league_is_rejected(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname, "
            "abbreviation, created_at, updated_at) VALUES "
            "('tm_dupe', ?, 'Another Team', 'Nowhere', 'Team', 'NYY', ?, ?)",
            (mlb_league_id, T0, T0),
        )


def test_duplicate_team_canonical_name_within_a_league_is_rejected(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname, "
            "abbreviation, created_at, updated_at) VALUES "
            "('tm_dupe', ?, 'New York Yankees', 'New York', 'Yankees', 'ZZZ', ?, ?)",
            (mlb_league_id, T0, T0),
        )


def test_alias_add_is_idempotent(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    repo = SqliteTeamAliasRepository(conn)
    assert repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id,
                    alias="Bronx Bombers", alias_type="nickname") is True
    assert repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id,
                    alias="Bronx Bombers", alias_type="nickname") is False


def test_alias_normalization_makes_spelling_variants_equal(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    repo = SqliteTeamAliasRepository(conn)
    repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id,
             alias="Bronx Bombers", alias_type="nickname")
    # Different spacing/case/punctuation normalizes to the same key, so this is
    # the same alias and is not stored twice.
    assert repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id,
                    alias="  bronx   BOMBERS ", alias_type="nickname") is False


def test_provider_specific_aliases_coexist(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    """One provider's spelling must not block another's."""

    repo = SqliteTeamAliasRepository(conn)
    assert repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id, alias="NY Yanks",
                    alias_type="provider", provider="the_odds_api",
                    source="provider_observed") is True
    assert repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id, alias="NY Yanks",
                    alias_type="provider", provider="kalshi",
                    source="provider_observed") is True
    aliases = {(a.alias, a.provider) for a in repo.list_for_team("tm_mlb_nyy")}
    assert ("NY Yanks", "the_odds_api") in aliases
    assert ("NY Yanks", "kalshi") in aliases


def test_provider_alias_type_requires_a_provider(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO team_aliases (alias_id, team_id, league_id, alias, normalized, "
            "alias_type, provider, source, created_at) VALUES "
            "('tal_bad', 'tm_mlb_nyy', ?, 'X', 'x', 'provider', '', 'seed', ?)",
            (mlb_league_id, T0),
        )


def test_two_teams_may_share_an_alias(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    """Shared aliases are ambiguity to record, not a write to reject."""

    repo = SqliteTeamAliasRepository(conn)
    assert repo.add(team_id="tm_mlb_nyy", league_id=mlb_league_id,
                    alias="Gotham", alias_type="nickname") is True
    assert repo.add(team_id="tm_mlb_nym", league_id=mlb_league_id,
                    alias="Gotham", alias_type="nickname") is True


def test_resolve_matches_abbreviation_and_nickname(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    repo = SqliteTeamAliasRepository(conn)
    for raw in ("NYY", "Yankees", "New York Yankees", "nyy", " yankees "):
        result = repo.resolve(raw, league_id=mlb_league_id)
        assert result.status is AliasMatchStatus.MATCHED, raw
        assert result.matched_id() == "tm_mlb_nyy", raw


def test_resolve_is_league_scoped(conn: sqlite3.Connection, mlb_league_id: str,
                                  nba_league_id: str) -> None:
    repo = SqliteTeamAliasRepository(conn)
    assert repo.resolve("Celtics", league_id=nba_league_id).matched_id() == "tm_nba_bos"
    assert repo.resolve("Celtics", league_id=mlb_league_id).status is AliasMatchStatus.UNMATCHED


def test_resolve_refuses_a_shared_city(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    repo = SqliteTeamAliasRepository(conn)
    for city in ("Chicago", "New York", "Los Angeles"):
        result = repo.resolve(city, league_id=mlb_league_id)
        assert result.status is AliasMatchStatus.AMBIGUOUS, city
        assert result.matched_id() is None, city


def test_resolve_refuses_shared_city_in_the_nba(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    """The Clippers brand as "LA", but "Los Angeles" could still mean either."""

    result = SqliteTeamAliasRepository(conn).resolve("Los Angeles", league_id=nba_league_id)
    assert result.status is AliasMatchStatus.AMBIGUOUS


def test_resolve_unknown_name_is_unmatched(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    result = SqliteTeamAliasRepository(conn).resolve("Springfield Isotopes",
                                                     league_id=mlb_league_id)
    assert result.status is AliasMatchStatus.UNMATCHED


def test_historical_team_names_resolve(conn: sqlite3.Connection, mlb_league_id: str,
                                       nba_league_id: str) -> None:
    aliases = SqliteTeamAliasRepository(conn)
    assert aliases.resolve("Cleveland Indians", league_id=mlb_league_id).matched_id() == (
        "tm_mlb_cle"
    )
    assert aliases.resolve("Washington Bullets", league_id=nba_league_id).matched_id() == (
        "tm_nba_was"
    )
    assert aliases.resolve("Oakland Athletics", league_id=mlb_league_id).matched_id() == (
        "tm_mlb_ath"
    )


def test_alias_requires_an_existing_team(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO team_aliases (alias_id, team_id, league_id, alias, normalized, "
            "alias_type, source, created_at) VALUES "
            "('tal_orphan', 'tm_does_not_exist', ?, 'X', 'x', 'nickname', 'seed', ?)",
            (mlb_league_id, T0),
        )


# --------------------------------------------------------------------------- #
# Players and player aliases
# --------------------------------------------------------------------------- #
def test_no_players_are_seeded(conn: sqlite3.Connection) -> None:
    """The corpus must never contain a fabricated person."""

    assert SqlitePlayerRepository(conn).count() == 0


def test_create_player(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    repo = SqlitePlayerRepository(conn)
    player = repo.create(
        league_id=mlb_league_id,
        full_name="Ronald Acuna",
        first_name="Ronald",
        last_name="Acuna",
        suffix="Jr.",
        birth_date="1997-12-18",
        primary_position="OF",
    )
    assert player.player_id.startswith("pl_")
    fetched = repo.get(player.player_id)
    assert fetched is not None
    assert fetched.full_name == "Ronald Acuna"
    # The suffix is stored separately from the name, which is what makes
    # "Griffey Jr." distinguishable from "Griffey".
    assert fetched.suffix == "Jr."


def test_player_requires_an_existing_league(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO players (player_id, league_id, full_name, created_at, updated_at) "
            "VALUES ('pl_orphan', 'lg_does_not_exist', 'Nobody', ?, ?)",
            (T0, T0),
        )


def test_player_alias_round_trip(conn: sqlite3.Connection, mlb_league_id: str) -> None:
    players = SqlitePlayerRepository(conn)
    aliases = SqlitePlayerAliasRepository(conn)
    player = players.create(league_id=mlb_league_id, full_name="Ronald Acuna Jr.")

    assert aliases.add(player_id=player.player_id, league_id=mlb_league_id,
                       alias="Ronald Acuña Jr.", alias_type="accent_stripped") is True
    # Same normalized form and suffix -> already recorded.
    assert aliases.add(player_id=player.player_id, league_id=mlb_league_id,
                       alias="ronald acuna jr", alias_type="accent_stripped") is False

    resolved = aliases.resolve("Ronald Acuña Jr.", league_id=mlb_league_id)
    assert resolved.matched_id() == player.player_id


def test_player_alias_suffix_is_stored_separately(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    players = SqlitePlayerRepository(conn)
    aliases = SqlitePlayerAliasRepository(conn)
    player = players.create(league_id=mlb_league_id, full_name="Ken Griffey", suffix="Jr.")
    aliases.add(player_id=player.player_id, league_id=mlb_league_id, alias="Ken Griffey Jr.")
    stored = aliases.list_for_player(player.player_id)
    assert len(stored) == 1
    assert stored[0].normalized == "ken griffey"
    assert stored[0].suffix == "jr"


def test_two_players_sharing_a_name_resolve_as_ambiguous(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    """The real two-Jalen-Williamses case, end to end through the database."""

    players = SqlitePlayerRepository(conn)
    aliases = SqlitePlayerAliasRepository(conn)
    first = players.create(league_id=nba_league_id, full_name="Jalen Williams")
    second = players.create(league_id=nba_league_id, full_name="Jalen Williams")
    aliases.add(player_id=first.player_id, league_id=nba_league_id, alias="Jalen Williams")
    aliases.add(player_id=second.player_id, league_id=nba_league_id, alias="Jalen Williams")

    result = aliases.resolve("Jalen Williams", league_id=nba_league_id)
    assert result.status is AliasMatchStatus.AMBIGUOUS
    assert result.matched_id() is None

    flagged = aliases.mark_ambiguous_duplicates(nba_league_id)
    assert flagged == 2
    # Still ambiguous after flagging, now for the recorded-flag reason.
    assert aliases.resolve("Jalen Williams", league_id=nba_league_id).status is (
        AliasMatchStatus.AMBIGUOUS
    )


def test_generations_are_not_marked_ambiguous_against_each_other(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    players = SqlitePlayerRepository(conn)
    aliases = SqlitePlayerAliasRepository(conn)
    senior = players.create(league_id=mlb_league_id, full_name="Ken Griffey", suffix="Sr.")
    junior = players.create(league_id=mlb_league_id, full_name="Ken Griffey", suffix="Jr.")
    aliases.add(player_id=senior.player_id, league_id=mlb_league_id, alias="Ken Griffey Sr.")
    aliases.add(player_id=junior.player_id, league_id=mlb_league_id, alias="Ken Griffey Jr.")

    assert aliases.mark_ambiguous_duplicates(mlb_league_id) == 0
    assert aliases.resolve("Ken Griffey Jr.", league_id=mlb_league_id).matched_id() == (
        junior.player_id
    )


# --------------------------------------------------------------------------- #
# Games
# --------------------------------------------------------------------------- #
def _game(
    conn: sqlite3.Connection,
    mlb_league_id: str,
    season_id: str,
    *,
    home_team_id: str = "tm_mlb_nyy",
    away_team_id: str = "tm_mlb_bos",
    scheduled_start: str = START,
    game_date_local: str = "2026-07-04",
    status: str = "scheduled",
    game_number: int = 1,
    doubleheader_type: Optional[str] = None,
    official_provider: Optional[str] = None,
    official_game_key: Optional[str] = None,
) -> Game:
    return SqliteGameRepository(conn).create(
        league_id=mlb_league_id,
        season_id=season_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        scheduled_start=scheduled_start,
        game_date_local=game_date_local,
        status=status,
        game_number=game_number,
        doubleheader_type=doubleheader_type,
        official_provider=official_provider,
        official_game_key=official_game_key,
    )


def test_create_valid_game(conn: sqlite3.Connection, mlb_league_id: str, season: str) -> None:
    game = _game(conn, mlb_league_id, season)
    assert game.game_id.startswith("gm_")
    assert game.status == "scheduled"
    assert game.original_start == START
    assert game.game_number == 1
    assert game.is_neutral_site is False


def test_same_team_game_is_rejected(conn: sqlite3.Connection, mlb_league_id: str,
                                    season: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _game(conn, mlb_league_id, season, away_team_id="tm_mlb_nyy")


def test_invalid_game_status_is_rejected(conn: sqlite3.Connection, mlb_league_id: str,
                                         season: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _game(conn, mlb_league_id, season, status="rained_out_probably")


def test_malformed_timestamp_is_rejected(conn: sqlite3.Connection, mlb_league_id: str,
                                         season: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _game(conn, mlb_league_id, season, scheduled_start="2026/07/04 23:05")


def test_game_requires_existing_teams(conn: sqlite3.Connection, mlb_league_id: str,
                                      season: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _game(conn, mlb_league_id, season, away_team_id="tm_mlb_does_not_exist")


def test_doubleheader_needs_distinct_game_numbers(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    _game(conn, mlb_league_id, season, game_number=1)
    _game(conn, mlb_league_id, season, game_number=2, doubleheader_type="split")
    # A third game on the same slate reusing game_number 1 is the natural-key
    # collision the index exists to prevent.
    with pytest.raises(sqlite3.IntegrityError):
        _game(conn, mlb_league_id, season, game_number=1)


def test_official_key_must_be_paired_with_a_provider(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _game(conn, mlb_league_id, season, official_game_key="12345")


def test_official_key_is_unique_per_provider(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    _game(conn, mlb_league_id, season, official_provider="mlb_statsapi",
          official_game_key="777")
    with pytest.raises(sqlite3.IntegrityError):
        _game(conn, mlb_league_id, season, game_number=2,
              official_provider="mlb_statsapi", official_game_key="777")


# --------------------------------------------------------------------------- #
# Game status history
# --------------------------------------------------------------------------- #
def test_record_status_appends_and_updates_current_state(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    repo = SqliteGameRepository(conn)
    game = _game(conn, mlb_league_id, season)

    assert repo.record_status(game_id=game.game_id, status="scheduled",
                              scheduled_start=START, provider="test", observed_at=T0) is True
    assert repo.record_status(game_id=game.game_id, status="postponed",
                              scheduled_start=START, provider="test", observed_at=T1,
                              detail="rain") is True

    current = repo.get(game.game_id)
    assert current is not None
    assert current.status == "postponed"
    assert len(repo.status_history(game.game_id)) == 2


def test_record_status_is_idempotent(conn: sqlite3.Connection, mlb_league_id: str,
                                     season: str) -> None:
    repo = SqliteGameRepository(conn)
    game = _game(conn, mlb_league_id, season)
    assert repo.record_status(game_id=game.game_id, status="scheduled",
                              scheduled_start=START, provider="test", observed_at=T0) is True
    # Re-polling unchanged content is not new information.
    assert repo.record_status(game_id=game.game_id, status="scheduled",
                              scheduled_start=START, provider="test", observed_at=T1) is False
    assert len(repo.status_history(game.game_id)) == 1


def test_postponed_then_rescheduled_preserves_the_original_start(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    repo = SqliteGameRepository(conn)
    game = _game(conn, mlb_league_id, season)

    repo.record_status(game_id=game.game_id, status="scheduled", scheduled_start=START,
                       provider="test", observed_at=T0)
    repo.record_status(game_id=game.game_id, status="postponed", scheduled_start=START,
                       provider="test", observed_at=T1, detail="rain")
    repo.record_status(game_id=game.game_id, status="rescheduled", scheduled_start=MOVED,
                       provider="test", observed_at=T2, detail="moved to 7/5")

    current = repo.get(game.game_id)
    assert current is not None
    assert current.status == "rescheduled"
    assert current.scheduled_start == MOVED
    # original_start is written once and never updated, so "was this moved?"
    # is answerable without scanning history.
    assert current.original_start == START


def test_status_as_of_reads_the_past_not_the_present(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    """The point-in-time accessor: what did we know at T1, not what we know now."""

    repo = SqliteGameRepository(conn)
    game = _game(conn, mlb_league_id, season)
    repo.record_status(game_id=game.game_id, status="scheduled", scheduled_start=START,
                       provider="test", observed_at=T0)
    repo.record_status(game_id=game.game_id, status="postponed", scheduled_start=START,
                       provider="test", observed_at=T2, detail="rain")

    at_t1 = repo.status_as_of(game.game_id, T1)
    assert at_t1 is not None
    assert at_t1.status == "scheduled"

    at_t2 = repo.status_as_of(game.game_id, T2)
    assert at_t2 is not None
    assert at_t2.status == "postponed"


def test_status_as_of_before_any_observation_is_none(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    repo = SqliteGameRepository(conn)
    game = _game(conn, mlb_league_id, season)
    repo.record_status(game_id=game.game_id, status="scheduled", scheduled_start=START,
                       provider="test", observed_at=T2)
    assert repo.status_as_of(game.game_id, T0) is None


def test_status_as_of_ignores_a_backdated_provider_timestamp(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    """DQ-PIT-004 in miniature: observation time governs, not the provider's clock.

    A provider publishing at T2 a fact it timestamps T0 was not knowable to us
    at T1, and an as-of query at T1 must not return it.
    """

    repo = SqliteGameRepository(conn)
    game = _game(conn, mlb_league_id, season)
    repo.record_status(game_id=game.game_id, status="postponed", scheduled_start=START,
                       provider="test", observed_at=T2, provider_timestamp=T0)
    assert repo.status_as_of(game.game_id, T1) is None
    assert repo.status_as_of(game.game_id, T2) is not None


def test_status_history_requires_an_existing_game(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO game_status_history (status_id, game_id, status, scheduled_start, "
            "provider, observed_at, ingested_at, content_hash, created_at) VALUES "
            "('gst_orphan', 'gm_does_not_exist', 'scheduled', ?, 'test', ?, ?, 'h', ?)",
            (START, T0, T0, T0),
        )


def test_status_content_hash_is_stable_and_content_sensitive() -> None:
    first = status_content_hash(
        status="scheduled", scheduled_start=START, detail=None, provider_timestamp=None
    )
    same = status_content_hash(
        status="scheduled", scheduled_start=START, detail=None, provider_timestamp=None
    )
    different = status_content_hash(
        status="postponed", scheduled_start=START, detail=None, provider_timestamp=None
    )
    assert first == same
    assert first != different


def test_multi_step_status_write_rolls_back_as_a_unit(
    conn: sqlite3.Connection, mlb_league_id: str, season: str
) -> None:
    """The history append and the current-state update must not diverge."""

    repo = SqliteGameRepository(conn)
    game = _game(conn, mlb_league_id, season)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            repo.record_status(game_id=game.game_id, status="final",
                               scheduled_start=START, provider="test", observed_at=T1)
            raise RuntimeError("boom")

    current = repo.get(game.game_id)
    assert current is not None
    assert current.status == "scheduled"
    assert repo.status_history(game.game_id) == []
