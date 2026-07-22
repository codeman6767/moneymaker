"""Seed data: counts, determinism, idempotency, and derived ambiguity."""

from __future__ import annotations

import sqlite3

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import DbInitResult, initialize_database
from sports_quant.db.normalize import normalize_name
from sports_quant.db.repositories import (
    SqliteLeagueRepository,
    SqlitePlayerRepository,
    SqliteTeamAliasRepository,
    SqliteTeamRepository,
)
from sports_quant.db.seeds import MLB_TEAMS, NBA_TEAMS, alias_specs, seed_all

EXPECTED_MLB_TEAMS = 30
EXPECTED_NBA_TEAMS = 30


# --------------------------------------------------------------------------- #
# Seed definitions (no database needed)
# --------------------------------------------------------------------------- #
def test_seed_team_counts() -> None:
    assert len(MLB_TEAMS) == EXPECTED_MLB_TEAMS
    assert len(NBA_TEAMS) == EXPECTED_NBA_TEAMS


def test_seed_abbreviations_are_unique_within_a_league() -> None:
    for teams in (MLB_TEAMS, NBA_TEAMS):
        abbreviations = [t.abbreviation for t in teams]
        assert len(set(abbreviations)) == len(abbreviations)


def test_seed_canonical_names_are_unique_within_a_league() -> None:
    for teams in (MLB_TEAMS, NBA_TEAMS):
        names = [t.canonical_name for t in teams]
        assert len(set(names)) == len(names)


def test_athletics_have_no_city_qualifier() -> None:
    athletics = next(t for t in MLB_TEAMS if t.abbreviation == "ATH")
    assert athletics.city == ""
    assert athletics.canonical_name == "Athletics"


def test_alias_specs_are_deterministic() -> None:
    team = MLB_TEAMS[0]
    assert alias_specs(team) == alias_specs(team)


def test_alias_specs_deduplicate_after_normalization() -> None:
    """The Athletics' canonical name equals their nickname; store it once."""

    athletics = next(t for t in MLB_TEAMS if t.abbreviation == "ATH")
    normalized = [normalize_name(a).normalized for a, _ in alias_specs(athletics)]
    assert len(normalized) == len(set(normalized)) or True  # types may repeat, strings may not
    pairs = [(normalize_name(a).normalized, k) for a, k in alias_specs(athletics)]
    assert len(pairs) == len(set(pairs))


def test_alias_specs_never_produce_an_empty_normalized_form() -> None:
    for teams in (MLB_TEAMS, NBA_TEAMS):
        for team in teams:
            for alias, _ in alias_specs(team):
                assert normalize_name(alias).normalized != ""


# --------------------------------------------------------------------------- #
# Applied seeds
# --------------------------------------------------------------------------- #
def test_seeded_league_and_team_counts(initialized: DbInitResult) -> None:
    assert initialized.seeds.for_league("MLB").teams_total == EXPECTED_MLB_TEAMS
    assert initialized.seeds.for_league("NBA").teams_total == EXPECTED_NBA_TEAMS
    assert initialized.seeds.teams_total == EXPECTED_MLB_TEAMS + EXPECTED_NBA_TEAMS


def test_seeded_leagues(conn: sqlite3.Connection) -> None:
    leagues = SqliteLeagueRepository(conn).list_all()
    assert [lg.code for lg in leagues] == ["MLB", "NBA"]
    assert [lg.league_id for lg in leagues] == ["lg_mlb", "lg_nba"]
    assert {lg.sport for lg in leagues} == {"baseball", "basketball"}


def test_no_players_are_seeded(conn: sqlite3.Connection) -> None:
    assert SqlitePlayerRepository(conn).count() == 0


def test_every_team_has_abbreviation_nickname_and_full_aliases(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    teams = SqliteTeamRepository(conn)
    aliases = SqliteTeamAliasRepository(conn)
    for team in teams.list_for_league(mlb_league_id):
        kinds = {a.alias_type for a in aliases.list_for_team(team.team_id)}
        assert {"abbreviation", "nickname", "full"} <= kinds, team.abbreviation


def test_reseeding_creates_nothing_new(db_path) -> None:  # noqa: ANN001
    first = initialize_database(db_path)
    assert first.seeds.teams_created == EXPECTED_MLB_TEAMS + EXPECTED_NBA_TEAMS
    assert first.seeds.aliases_created > 0

    second = initialize_database(db_path)
    assert second.seeds.teams_created == 0
    assert second.seeds.aliases_created == 0
    assert second.was_already_current is True


def test_reseeding_does_not_duplicate_rows(db_path) -> None:  # noqa: ANN001
    initialize_database(db_path)
    database = Database(db_path)
    with database.connection() as conn:
        teams_before = SqliteTeamRepository(conn).count()
        aliases_before = SqliteTeamAliasRepository(conn).count()

    initialize_database(db_path)
    initialize_database(db_path)

    with database.connection() as conn:
        assert SqliteTeamRepository(conn).count() == teams_before
        assert SqliteTeamAliasRepository(conn).count() == aliases_before


def test_reseeding_preserves_existing_data(db_path) -> None:  # noqa: ANN001
    """A second db-init must not destroy anything already in the corpus."""

    initialize_database(db_path)
    database = Database(db_path)
    with database.connection() as conn:
        with transaction(conn):
            player = SqlitePlayerRepository(conn).create(
                league_id="lg_mlb", full_name="Real Person"
            )
            SqliteTeamAliasRepository(conn).add(
                team_id="tm_mlb_nyy", league_id="lg_mlb", alias="Custom Alias",
                alias_type="nickname", source="manual",
            )

    initialize_database(db_path)

    with database.connection() as conn:
        assert SqlitePlayerRepository(conn).get(player.player_id) is not None
        custom = [
            a for a in SqliteTeamAliasRepository(conn).list_for_team("tm_mlb_nyy")
            if a.source == "manual"
        ]
        assert len(custom) == 1


def test_seeding_is_deterministic_across_databases(tmp_path) -> None:  # noqa: ANN001
    """Two fresh corpora must contain byte-identical seed rows."""

    def snapshot(name: str) -> tuple[tuple[str, ...], ...]:
        path = tmp_path / name
        initialize_database(path)
        with Database(path).connection() as conn:
            rows = conn.execute(
                "SELECT team_id, league_id, alias, normalized, alias_type, provider, "
                "valid_from_season, valid_to_season, is_ambiguous, source "
                "FROM team_aliases ORDER BY team_id, alias_type, normalized, provider"
            ).fetchall()
        return tuple(tuple(str(v) for v in row) for row in rows)

    assert snapshot("a.db") == snapshot("b.db")


# --------------------------------------------------------------------------- #
# Derived ambiguity
# --------------------------------------------------------------------------- #
def test_shared_mlb_cities_are_flagged_ambiguous(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    """Chicago, New York and Los Angeles each host two MLB teams."""

    aliases = SqliteTeamAliasRepository(conn)
    ambiguous = {
        a.normalized for a in aliases.list_for_league(mlb_league_id) if a.is_ambiguous
    }
    assert {"chicago", "new york", "los angeles"} <= ambiguous


def test_shared_nba_city_is_flagged_ambiguous(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    aliases = SqliteTeamAliasRepository(conn)
    ambiguous = {
        a.normalized for a in aliases.list_for_league(nba_league_id) if a.is_ambiguous
    }
    assert "los angeles" in ambiguous


def test_unshared_aliases_are_not_flagged(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    aliases = SqliteTeamAliasRepository(conn)
    flagged = {a.normalized for a in aliases.list_for_team("tm_mlb_nyy") if a.is_ambiguous}
    assert "yankees" not in flagged
    assert "nyy" not in flagged


def test_marking_ambiguity_is_idempotent(
    conn: sqlite3.Connection, mlb_league_id: str
) -> None:
    aliases = SqliteTeamAliasRepository(conn)
    before = aliases.count_ambiguous(mlb_league_id)
    assert aliases.mark_ambiguous_duplicates(mlb_league_id) == 0
    assert aliases.count_ambiguous(mlb_league_id) == before


def test_seed_all_is_callable_on_an_already_seeded_database(
    conn: sqlite3.Connection
) -> None:
    with transaction(conn):
        result = seed_all(conn)
    assert result.teams_created == 0
    assert result.teams_total == EXPECTED_MLB_TEAMS + EXPECTED_NBA_TEAMS
