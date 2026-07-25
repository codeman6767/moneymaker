"""Phase D5A: deterministic canonical entity + official-game matching tests.

Offline/mocked only: a fresh migrated corpus seeded through the real
repositories, no provider client. Covers normalization/alias tiers, player
resolution, venue + venue-aware local date, official-game canonicalization and
its hard cases, decision/candidate completeness, point-in-time visibility, and
the CLI.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.engine import transaction
from sports_quant.db.repositories.games import SqliteGameRepository
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.matching.localdate import InvalidTimezoneError, resolve_local_date
from sports_quant.matching.model import AMBIGUOUS, MATCHED, UNMATCHED
from sports_quant.matching.players import PlayerResolver
from sports_quant.matching.service import MatchGamesService
from sports_quant.matching.teams import TeamResolver
from sports_quant.matching.venues import VenueResolver

from .conftest import (
    mark_team_ambiguous,
    raw_response,
    seed_player,
    seed_roster,
    seed_schedule,
    seed_team,
    seed_team_alias,
    seed_venue,
)

MLB = "mlb_statsapi"
NBA = "balldontlie"


def _match(conn: sqlite3.Connection, *, dry_run: bool = False, provider: str = MLB, **kw):  # type: ignore[no-untyped-def]
    svc = MatchGamesService(conn, dry_run=dry_run)
    if dry_run:
        return svc.match_range(provider=provider, **kw)
    with transaction(conn):
        return svc.match_range(provider=provider, **kw)


def _two_mlb_teams(conn: sqlite3.Connection) -> tuple[str, str]:
    home = seed_team(
        conn, league_code="MLB", abbreviation="THM", canonical_name="Test Home",
        city="Homecity", nickname="Homers",
        aliases=[("Test Home", "full", ""), ("101", "provider", MLB)],
    )
    away = seed_team(
        conn, league_code="MLB", abbreviation="TAW", canonical_name="Test Away",
        city="Awaycity", nickname="Aways",
        aliases=[("Test Away", "full", ""), ("102", "provider", MLB)],
    )
    return home, away


# --------------------------------------------------------------------------- #
# 1-9. Normalization + team alias tiers
# --------------------------------------------------------------------------- #
def test_one_canonical_normalizer_is_used() -> None:
    from intel.player_matching import normalize_name as intel_norm
    from sports_quant.db.normalize import normalize_name as db_norm

    for name in ("Ronald Acuña Jr.", "St. Louis", "N.Y.", "Shai Gilgeous-Alexander"):
        assert intel_norm(name) == db_norm(name).normalized


def test_provider_scoped_team_alias(conn: sqlite3.Connection) -> None:
    tid = seed_team(
        conn, league_code="MLB", abbreviation="THM", canonical_name="Test Home",
        city="Homecity", nickname="Homers", aliases=[("HOMERS", "provider", MLB)],
    )
    res = TeamResolver(conn).resolve(
        provider=MLB, provider_team_id="HOMERS", league_id="lg_mlb"
    )
    assert res.status == MATCHED and res.entity_id == tid
    assert res.tier == "exact_alias" and res.score == 0.99


def test_normalized_team_alias_scoped(conn: sqlite3.Connection) -> None:
    tid = seed_team(
        conn, league_code="MLB", abbreviation="THM", canonical_name="Test Home",
        city="Homecity", nickname="Homers", aliases=[("Test Home", "provider", MLB)],
    )
    res = TeamResolver(conn).resolve(
        provider=MLB, provider_team_id="ignored", raw_name="test  home", league_id="lg_mlb"
    )
    assert res.status == MATCHED and res.entity_id == tid
    assert res.tier == "normalized_alias" and res.score == 0.95


def test_unscoped_same_league_alias(conn: sqlite3.Connection) -> None:
    tid = seed_team(
        conn, league_code="MLB", abbreviation="THM", canonical_name="Test Home",
        city="Homecity", nickname="Homers", aliases=[("Test Home", "full", "")],
    )
    res = TeamResolver(conn).resolve(
        provider="other_provider", provider_team_id="x", raw_name="Test Home", league_id="lg_mlb"
    )
    assert res.status == MATCHED and res.entity_id == tid
    assert res.tier == "normalized_alias_unscoped" and res.score == 0.90


def test_league_mismatch_is_unmatched(conn: sqlite3.Connection) -> None:
    seed_team(
        conn, league_code="MLB", abbreviation="THM", canonical_name="Test Home",
        city="Homecity", nickname="Homers", aliases=[("Test Home", "full", "")],
    )
    res = TeamResolver(conn).resolve(
        provider="", provider_team_id="x", raw_name="Test Home", league_id="lg_nba"
    )
    assert res.status == UNMATCHED


def test_bare_city_alias_is_ambiguous(conn: sqlite3.Connection) -> None:
    seed_team(
        conn, league_code="MLB", abbreviation="TC1", canonical_name="North City Ones",
        city="North City", nickname="Ones", aliases=[("North City", "city", "")],
    )
    seed_team(
        conn, league_code="MLB", abbreviation="TC2", canonical_name="North City Twos",
        city="North City", nickname="Twos", aliases=[("North City", "city", "")],
    )
    mark_team_ambiguous(conn, "MLB")
    res = TeamResolver(conn).resolve(
        provider="", provider_team_id="x", raw_name="North City", league_id="lg_mlb"
    )
    assert res.status == AMBIGUOUS
    assert len(res.candidates) == 2
    assert res.via_ambiguous_alias  # resolved through an is_ambiguous row


def test_candidate_order_deterministic_under_shuffle(conn: sqlite3.Connection) -> None:
    seed_team(
        conn, league_code="MLB", abbreviation="TC1", canonical_name="Shared One",
        city="Shared", nickname="Ones", aliases=[("Shared", "city", "")],
    )
    seed_team(
        conn, league_code="MLB", abbreviation="TC2", canonical_name="Shared Two",
        city="Shared", nickname="Twos", aliases=[("Shared", "city", "")],
    )
    resolver = TeamResolver(conn)
    first = resolver.resolve(provider="", provider_team_id="x", raw_name="Shared", league_id="lg_mlb")
    for _ in range(100):
        again = resolver.resolve(
            provider="", provider_team_id="x", raw_name="Shared", league_id="lg_mlb"
        )
        assert [c.entity_id for c in again.candidates] == [c.entity_id for c in first.candidates]
    assert [c.entity_id for c in first.candidates] == sorted(
        c.entity_id for c in first.candidates
    )


def test_curated_season_window_inside_and_outside(conn: sqlite3.Connection) -> None:
    tid = seed_team(
        conn, league_code="MLB", abbreviation="THI", canonical_name="Historic Club",
        city="Old Town", nickname="Historics",
    )
    seed_team_alias(
        conn, team_id=tid, league_code="MLB", alias="Old Name", alias_type="historical",
        valid_from=1950, valid_to=1960,
    )
    resolver = TeamResolver(conn)
    inside = resolver.resolve(
        provider="", provider_team_id="x", raw_name="Old Name", league_id="lg_mlb", season_year=1955
    )
    assert inside.status == MATCHED and inside.season_validity_verified
    outside = resolver.resolve(
        provider="", provider_team_id="x", raw_name="Old Name", league_id="lg_mlb", season_year=1975
    )
    assert outside.status == UNMATCHED


def test_unbounded_season_alias_reports_unverified(conn: sqlite3.Connection) -> None:
    seed_team(
        conn, league_code="MLB", abbreviation="THU", canonical_name="Unbounded Club",
        city="Nowhere", nickname="Unbounds", aliases=[("Unbounded Club", "full", "")],
    )
    res = TeamResolver(conn).resolve(
        provider="", provider_team_id="x", raw_name="Unbounded Club", league_id="lg_mlb",
        season_year=2026,
    )
    assert res.status == MATCHED
    assert res.season_scoped and not res.season_validity_verified


# --------------------------------------------------------------------------- #
# 10-19. Players
# --------------------------------------------------------------------------- #
def test_exact_provider_player_link(conn: sqlite3.Connection) -> None:
    pid = seed_player(conn, league_code="MLB", full_name="Alex Batter")
    rid, rhash = raw_response(conn, marker="player-link")
    with transaction(conn):
        refs = SqliteProviderReferenceRepository(conn)
        refs.upsert(
            kind="player", provider=MLB, provider_entity_id="P9", raw_response_id=rid,
            raw_response_hash=rhash, observed_at="2026-07-24T18:00:00.000000Z",
        )
        # A prior accepted decision linked this reference to the canonical player.
        conn.execute(
            "UPDATE provider_player_references SET player_id = ? "
            "WHERE provider = ? AND provider_player_id = 'P9' AND player_id IS NULL",
            (pid, MLB),
        )
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="P9", league_id="lg_mlb", raw_name="Whoever"
    )
    assert res.status == MATCHED and res.entity_id == pid and res.tier == "exact_provider_id"


def test_player_team_plus_name(conn: sqlite3.Connection) -> None:
    p1 = seed_player(conn, league_code="MLB", full_name="Sammy Same",
                     aliases=[("Sammy Same", "full", "")])
    p2 = seed_player(conn, league_code="MLB", full_name="Sammy Same",
                     aliases=[("Sammy Same", "full", "")])
    team = seed_team(conn, league_code="MLB", abbreviation="TP1", canonical_name="Team One",
                     city="One", nickname="Ones")
    seed_roster(conn, provider=MLB, provider_team_id="500", team_id=team,
                provider_player_id="p1", player_id=p1)
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Sammy Same",
        team_id=team,
    )
    assert res.status == MATCHED and res.entity_id == p1 and res.tier == "team_normalized_name"
    assert p2 != p1


def test_player_league_plus_name(conn: sqlite3.Connection) -> None:
    pid = seed_player(conn, league_code="MLB", full_name="Unique Guy",
                      aliases=[("Unique Guy", "full", "")])
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Unique Guy"
    )
    assert res.status == MATCHED and res.entity_id == pid and res.tier == "league_normalized_name"


def test_binding_suffix(conn: sqlite3.Connection) -> None:
    son = seed_player(conn, league_code="MLB", full_name="Vlad Guerrero Jr.", suffix="jr",
                      aliases=[("Vlad Guerrero Jr.", "full", "")])
    dad = seed_player(conn, league_code="MLB", full_name="Vlad Guerrero",
                      aliases=[("Vlad Guerrero", "full", "")])
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Vlad Guerrero Jr."
    )
    assert res.status == MATCHED and res.entity_id == son and res.entity_id != dad


def test_omitted_suffix_one_candidate(conn: sqlite3.Connection) -> None:
    only = seed_player(conn, league_code="MLB", full_name="Ronald Acuna", suffix="jr",
                       aliases=[("Ronald Acuna", "full", "")])
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Ronald Acuna"
    )
    assert res.status == MATCHED and res.entity_id == only


def test_omitted_suffix_two_candidates_ambiguous(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="Ken Griffey", suffix="jr",
                aliases=[("Ken Griffey", "full", "")])
    seed_player(conn, league_code="MLB", full_name="Ken Griffey", suffix="sr",
                aliases=[("Ken Griffey", "full", "")])
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Ken Griffey"
    )
    assert res.status == AMBIGUOUS and len(res.candidates) == 2


def test_same_name_different_teams(conn: sqlite3.Connection) -> None:
    a = seed_player(conn, league_code="MLB", full_name="Jose Twin", aliases=[("Jose Twin", "full", "")])
    b = seed_player(conn, league_code="MLB", full_name="Jose Twin", aliases=[("Jose Twin", "full", "")])
    ta = seed_team(conn, league_code="MLB", abbreviation="TX1", canonical_name="Team X1", city="X1", nickname="X1s")
    tb = seed_team(conn, league_code="MLB", abbreviation="TX2", canonical_name="Team X2", city="X2", nickname="X2s")
    seed_roster(conn, provider=MLB, provider_team_id="800", team_id=ta, provider_player_id="pa", player_id=a)
    seed_roster(conn, provider=MLB, provider_team_id="801", team_id=tb, provider_player_id="pb", player_id=b)
    resolver = PlayerResolver(conn)
    no_team = resolver.resolve(provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Jose Twin")
    assert no_team.status == AMBIGUOUS
    with_team = resolver.resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Jose Twin", team_id=tb
    )
    assert with_team.status == MATCHED and with_team.entity_id == b


def test_birth_date_collision_breaker(conn: sqlite3.Connection) -> None:
    a = seed_player(conn, league_code="MLB", full_name="Twin Birth", birth_date="1990-01-01",
                    aliases=[("Twin Birth", "full", "")])
    seed_player(conn, league_code="MLB", full_name="Twin Birth", birth_date="1992-02-02",
                aliases=[("Twin Birth", "full", "")])
    resolver = PlayerResolver(conn)
    res = resolver.resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Twin Birth",
        birth_date="1990-01-01",
    )
    assert res.status == MATCHED and res.entity_id == a
    # Without the birth date, the same pair stays ambiguous (never invented).
    assert resolver.resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Twin Birth"
    ).status == AMBIGUOUS


def test_unknown_player_unmatched_no_creation(conn: sqlite3.Connection) -> None:
    before = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="x", league_id="lg_mlb", raw_name="Nobody Here"
    )
    assert res.status == UNMATCHED
    after = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    assert after == before  # no canonical player invented from a name


# --------------------------------------------------------------------------- #
# 20-26. Venues + local date
# --------------------------------------------------------------------------- #
def test_local_date_actual_venue_tz() -> None:
    res = resolve_local_date(
        scheduled_start="2026-07-26T02:05:00Z", actual_venue_tz="America/Los_Angeles",
        provider_local_date="2026-07-26",
    )
    assert res.game_date_local == "2026-07-25" and res.tier == "actual_venue_tz"


def test_local_date_provider_fallback() -> None:
    res = resolve_local_date(scheduled_start="2026-07-26T02:05:00Z", provider_local_date="2026-07-25")
    assert res.game_date_local == "2026-07-25" and res.tier == "provider_local_date"


def test_local_date_home_venue_fallback() -> None:
    res = resolve_local_date(
        scheduled_start="2026-07-26T02:05:00Z", home_venue_tz="America/New_York"
    )
    assert res.tier == "home_venue_tz" and res.game_date_local == "2026-07-25"


def test_local_date_utc_fallback_and_confidence() -> None:
    res = resolve_local_date(scheduled_start="2026-07-26T02:05:00Z")
    assert res.tier == "utc_fallback" and res.game_date_local == "2026-07-26"
    assert res.dq_code == "DQ-TZ-001" and res.confidence_cap < 0.95


def test_local_date_cross_utc_boundary() -> None:
    # 7pm Pacific on the 25th is 02:00 UTC on the 26th; must stay on the 25th.
    res = resolve_local_date(
        scheduled_start="2026-07-26T02:00:00Z", actual_venue_tz="America/Los_Angeles"
    )
    assert res.game_date_local == "2026-07-25"


def test_invalid_timezone_does_not_become_utc() -> None:
    with pytest.raises(InvalidTimezoneError):
        resolve_local_date(scheduled_start="2026-07-26T02:00:00Z", actual_venue_tz="Not/AZone")


def test_venue_provider_id_and_distinct_neutral(conn: sqlite3.Connection) -> None:
    home = seed_venue(conn, name="Home Park", provider=MLB, provider_venue_id="V1",
                      timezone="America/New_York")
    neutral = seed_venue(conn, name="London Stadium", provider=MLB, provider_venue_id="V2",
                         timezone="Europe/London")
    resolver = VenueResolver(conn)
    assert resolver.resolve(provider=MLB, provider_venue_id="V1").entity_id == home
    assert resolver.resolve(provider=MLB, provider_venue_id="V2").entity_id == neutral
    assert home != neutral  # neutral/international venue stays distinct


def test_venue_coordinate_contradiction(conn: sqlite3.Connection) -> None:
    from sports_quant.db.repositories.venues import SqliteVenueRepository

    vid = seed_venue(conn, name="Coord Park", provider=MLB, provider_venue_id="VC",
                     latitude=40.0, longitude=-75.0, timezone="America/New_York")
    venue = SqliteVenueRepository(conn).get(vid)
    assert venue is not None
    resolver = VenueResolver(conn)
    assert resolver.contradictions(venue, latitude=40.05, longitude=-75.02) == []
    conflicts = resolver.contradictions(venue, latitude=34.0, longitude=-118.0)
    assert any(c.field == "coordinates" for c in conflicts)


# --------------------------------------------------------------------------- #
# 27-43. Official-game canonicalization + hard cases
# --------------------------------------------------------------------------- #
def _mlb_setup(conn: sqlite3.Connection) -> tuple[str, str]:
    home, away = _two_mlb_teams(conn)
    seed_venue(conn, name="Home Park", provider=MLB, provider_venue_id="V1",
               timezone="America/New_York")
    return home, away


def _nba_setup(conn: sqlite3.Connection) -> tuple[str, str]:
    home = seed_team(conn, league_code="NBA", abbreviation="TNH", canonical_name="NBA Home",
                     city="NBAH", nickname="Homers", aliases=[("201", "provider", NBA)])
    away = seed_team(conn, league_code="NBA", abbreviation="TNA", canonical_name="NBA Away",
                     city="NBAA", nickname="Aways", aliases=[("202", "provider", NBA)])
    seed_venue(conn, name="NBA Arena", provider=NBA, provider_venue_id="NV1",
               timezone="America/Chicago")
    return home, away


def _create_canonical(
    conn: sqlite3.Connection, *, league_code: str, home_team_id: str, away_team_id: str,
    scheduled_start: str, game_date_local: str, official_provider=None, official_game_key=None,
    is_neutral_site: bool = False, game_number: int = 1,
) -> str:
    from sports_quant.db.repositories.leagues import SqliteSeasonRepository

    lg = f"lg_{league_code.lower()}"
    with transaction(conn):
        SqliteSeasonRepository(conn).upsert(
            league_code=league_code, league_id=lg, year=2026, phase="regular",
            label="x", start_date="2026-01-01",
        )
        game = SqliteGameRepository(conn).create(
            league_id=lg, season_id=f"sn_{league_code.lower()}_2026_regular",
            home_team_id=home_team_id, away_team_id=away_team_id, scheduled_start=scheduled_start,
            game_date_local=game_date_local, game_number=game_number,
            is_neutral_site=is_neutral_site, official_provider=official_provider,
            official_game_key=official_game_key,
        )
    return game.game_id


def test_ordinary_mlb_game_creates_one(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1", game_type="R")
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 1
    assert SqliteGameRepository(conn).count() == 1
    ref = SqliteProviderReferenceRepository(conn).get("game", MLB, "G1")
    assert ref is not None and ref.canonical_id is not None
    assert r.counters.provider_references_linked == 1


def test_ordinary_nba_game_creates_one(conn: sqlite3.Connection) -> None:
    _nba_setup(conn)
    seed_schedule(conn, provider=NBA, provider_game_id="NG1", home_provider_team_id="201",
                  away_provider_team_id="202", scheduled_start="2026-04-10T23:30:00Z",
                  season=2026, game_date_local="2026-04-10", venue_provider_id="NV1")
    r = _match(conn, provider=NBA, from_date="2026-04-10", to_date="2026-04-10")
    assert r.counters.canonical_games_created == 1 and r.counters.accepted >= 1


def test_official_key_replay_idempotent(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1", game_type="R")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    r2 = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r2.counters.canonical_games_created == 0
    assert r2.counters.canonical_entities_unchanged == 1
    assert SqliteGameRepository(conn).count() == 1


def test_schedule_exact_ninety_minute_match(conn: sqlite3.Connection) -> None:
    home, away = _mlb_setup(conn)
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-25T23:05:00Z", game_date_local="2026-07-25")
    seed_schedule(conn, provider=MLB, provider_game_id="G30", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:35:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 0 and r.counters.accepted >= 1
    decisions = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=MLB, source_ref="G30", entity_type="game")
    assert decisions[-1].method == "schedule_key_exact"


def test_schedule_window_twelve_hour_match(conn: sqlite3.Connection) -> None:
    home, away = _mlb_setup(conn)
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-25T20:05:00Z", game_date_local="2026-07-25")
    seed_schedule(conn, provider=MLB, provider_game_id="G31", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:35:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    decisions = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=MLB, source_ref="G31", entity_type="game")
    assert decisions[-1].method == "schedule_key_window" and decisions[-1].score == 0.88
    assert r.counters.canonical_games_created == 0


def test_postponed_then_rescheduled_preserves_identity(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1",
                  mapped_status="scheduled", observed_at="2026-07-20T18:00:00.000000Z")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    # A later observation reschedules the same official game to a new date.
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-27T23:05:00Z",
                  season=2026, game_date_local="2026-07-27", venue_provider_id="V1",
                  mapped_status="rescheduled", observed_at="2026-07-26T18:00:00.000000Z")
    r = _match(conn, provider_game_id="G1")
    assert r.counters.canonical_games_created == 0  # same official key -> one game
    assert SqliteGameRepository(conn).count() == 1


def test_neutral_site_direct_orientation(conn: sqlite3.Connection) -> None:
    home, away = _mlb_setup(conn)
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-25T23:05:00Z", game_date_local="2026-07-25",
                      official_provider=MLB, official_game_key="G34", is_neutral_site=True)
    seed_schedule(conn, provider=MLB, provider_game_id="G34", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, provider_game_id="G34")
    assert r.counters.accepted >= 1 and r.counters.blocking_issues == 0


def test_neutral_site_swapped_flagged(conn: sqlite3.Connection) -> None:
    home, away = _mlb_setup(conn)
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-25T23:05:00Z", game_date_local="2026-07-25",
                      official_provider=MLB, official_game_key="G35", is_neutral_site=True)
    # Provider reports the opposite home/away for the neutral-site game.
    seed_schedule(conn, provider=MLB, provider_game_id="G35", home_provider_team_id="102",
                  away_provider_team_id="101", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, provider_game_id="G35")
    assert r.counters.accepted >= 1 and r.counters.manual_review_required >= 1
    assert r.needs_failure_exit is False  # DQ-MATCH-007 is an issue, not blocking
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-007" in codes


def test_doubleheader_by_game_number(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    for pgid, gn, start in (("G36A", 1, "2026-07-25T17:05:00Z"), ("G36B", 2, "2026-07-25T23:05:00Z")):
        seed_schedule(conn, provider=MLB, provider_game_id=pgid, home_provider_team_id="101",
                      away_provider_team_id="102", scheduled_start=start, season=2026,
                      game_date_local="2026-07-25", venue_provider_id="V1", game_number=gn)
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 2
    assert SqliteGameRepository(conn).count() == 2


def test_doubleheader_separated_by_start(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    for pgid, start in (("G37A", "2026-07-25T17:05:00Z"), ("G37B", "2026-07-25T23:05:00Z")):
        seed_schedule(conn, provider=MLB, provider_game_id=pgid, home_provider_team_id="101",
                      away_provider_team_id="102", scheduled_start=start, season=2026,
                      game_date_local="2026-07-25", venue_provider_id="V1")  # no game_number
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 2  # separated by >90 min -> game 1 and 2


def test_doubleheader_indistinguishable_ambiguous(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    for pgid, start in (("G38A", "2026-07-25T23:05:00Z"), ("G38B", "2026-07-25T23:35:00Z")):
        seed_schedule(conn, provider=MLB, provider_game_id=pgid, home_provider_team_id="101",
                      away_provider_team_id="102", scheduled_start=start, season=2026,
                      game_date_local="2026-07-25", venue_provider_id="V1")  # 30 min apart, no number
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 1  # first creates
    assert r.counters.ambiguous >= 1  # the second is indistinguishable


def test_missing_team_stops_matching(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)  # teams 101/102 exist; 999 does not
    seed_schedule(conn, provider=MLB, provider_game_id="G39", home_provider_team_id="101",
                  away_provider_team_id="999", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 0 and r.counters.no_candidate >= 1
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-010" in codes


def test_ambiguous_team_stops_matching(conn: sqlite3.Connection) -> None:
    home, _away = _mlb_setup(conn)
    # Two teams share the away provider id string -> ambiguous away resolution.
    seed_team(conn, league_code="MLB", abbreviation="TC1", canonical_name="Dup One",
              city="Dup", nickname="Ones", aliases=[("777", "provider", MLB)])
    seed_team(conn, league_code="MLB", abbreviation="TC2", canonical_name="Dup Two",
              city="Dup", nickname="Twos", aliases=[("777", "provider", MLB)])
    seed_schedule(conn, provider=MLB, provider_game_id="G40", home_provider_team_id="101",
                  away_provider_team_id="777", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 0
    assert home is not None


def test_conflicting_orientation_is_blocking(conn: sqlite3.Connection) -> None:
    home, away = _mlb_setup(conn)
    # A NON-neutral canonical game with the official key already exists...
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-25T23:05:00Z", game_date_local="2026-07-25",
                      official_provider=MLB, official_game_key="G42", is_neutral_site=False)
    # ...but the provider now reports the teams swapped -> DQ-MATCH-003 blocking.
    seed_schedule(conn, provider=MLB, provider_game_id="G42", home_provider_team_id="102",
                  away_provider_team_id="101", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, provider_game_id="G42")
    assert r.counters.blocking_issues >= 1 and r.needs_failure_exit
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-003" in codes


def test_same_team_home_away_is_blocking(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G41", home_provider_team_id="101",
                  away_provider_team_id="101", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, provider_game_id="G41")
    assert r.counters.blocking_issues >= 1 and r.counters.rejected >= 1


def test_results_do_not_affect_matching_source(conn: sqlite3.Connection) -> None:
    # Structural: the matcher never reads a result/score table.
    from pathlib import Path as _P

    src = (_P(__file__).parent.parent / "service.py").read_text(encoding="utf-8")
    for banned in (
        "game_result_snapshots", "nba_game_results", "SqliteResultRepository",
        "sportsbook_events", "kalshi_markets", "SqliteSportsbookRepository",
        "SqliteKalshiRepository",
    ):
        assert banned not in src


# --------------------------------------------------------------------------- #
# 44-58. Decision completeness, PIT, isolation, dry-run, CLI
# --------------------------------------------------------------------------- #
def test_one_decision_per_invocation_and_candidates(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    repo = SqliteMatchingRepository(conn)
    game_decisions = repo.decisions_for_source(
        source_provider=MLB, source_ref="G1", entity_type="game")
    assert len(game_decisions) == 1
    d = game_decisions[0]
    assert d.outcome == "accepted" and d.matched_entity_id is not None
    assert repo.candidates(d.match_id)  # candidate child rows exist


def test_losers_preserved_in_candidates(conn: sqlite3.Connection) -> None:
    seed_team(conn, league_code="MLB", abbreviation="TC1", canonical_name="Loser One",
              city="LC", nickname="Ones", aliases=[("shared name", "full", "")])
    seed_team(conn, league_code="MLB", abbreviation="TC2", canonical_name="Loser Two",
              city="LC", nickname="Twos", aliases=[("shared name", "full", "")])
    res = TeamResolver(conn).resolve(
        provider="", provider_team_id="x", raw_name="Shared Name", league_id="lg_mlb")
    assert res.status == AMBIGUOUS and len(res.candidates) == 2  # both retained


def test_ambiguous_requires_review_accepted_names_entity(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    rows = conn.execute(
        "SELECT outcome, matched_entity_id, needs_manual_review, rejection_reason "
        "FROM entity_match_decisions"
    ).fetchall()
    for outcome, matched, review, reason in rows:
        if outcome == "accepted":
            assert matched is not None
        else:
            assert reason is not None and review == 1


def test_decision_evidence_immutable_review_mutable(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    repo = SqliteMatchingRepository(conn)
    mid = repo.decisions_for_source(source_provider=MLB, source_ref="G1", entity_type="game")[0].match_id
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute("UPDATE entity_match_decisions SET score = 0.1 WHERE match_id = ?", (mid,))
    reviewed = repo.mark_reviewed(mid, reviewed_by="tester", needs_manual_review=False)
    assert reviewed.reviewed_by == "tester"


def test_as_of_decision_read_hides_future(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    repo = SqliteMatchingRepository(conn)
    d = repo.decisions_for_source(source_provider=MLB, source_ref="G1", entity_type="game")[0]
    assert repo.decisions_for_source(
        source_provider=MLB, source_ref="G1", as_of="2000-01-01T00:00:00.000000Z") == []
    assert repo.decisions_for_source(
        source_provider=MLB, source_ref="G1", as_of=d.decided_at)


def test_out_of_order_link_does_not_regress(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    refs = SqliteProviderReferenceRepository(conn)
    ref = refs.get("game", MLB, "G1")
    assert ref is not None and ref.canonical_id is not None
    original = ref.canonical_id
    with transaction(conn):
        _r, outcome = refs.link_canonical(
            kind="game", provider=MLB, provider_entity_id="G1", canonical_id="gm_other",
            match_decision_id="mtc_x")
    from sports_quant.db.repositories.references import LinkOutcome
    assert outcome == LinkOutcome.CONFLICT
    after = refs.get("game", MLB, "G1")
    assert after is not None and after.canonical_id == original  # unchanged


def test_dry_run_persists_nothing(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    before_games = SqliteGameRepository(conn).count()
    before_dec = SqliteMatchingRepository(conn).count()
    r = _match(conn, dry_run=True, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.decisions_evaluated >= 1  # computed
    assert SqliteGameRepository(conn).count() == before_games  # persisted nothing
    assert SqliteMatchingRepository(conn).count() == before_dec


def test_repeated_run_no_duplicate_game(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert SqliteGameRepository(conn).count() == 1


def test_no_sportsbook_or_kalshi_decisions(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    types = {row[0] for row in conn.execute("SELECT DISTINCT entity_type FROM entity_match_decisions")}
    assert types <= {"team", "venue", "game"}
    assert not (types & {"sportsbook_event", "kalshi_event", "kalshi_market"})


def test_matching_package_does_not_import_execution() -> None:
    from pathlib import Path as _P

    pkg = _P(__file__).parent.parent
    for py in pkg.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "execution" not in src and "order_gateway" not in src and "gateway" not in src


def _settings():  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from sports_quant.config import PRODUCTION_KALSHI_REST_URL, Settings

    return Settings(
        odds_api_key=SecretStr(""), nba_data_api_key=SecretStr(""),
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL, kalshi_environment="production",
        read_only_mode=True, order_submission_enabled=False, paper_trading=False,
        live_trading=False, manual_live_arming=False,
    )


def test_cli_match_games_json_and_exit_code(conn: sqlite3.Connection, db_path) -> None:  # type: ignore[no-untyped-def]
    import json as _json

    from sports_quant.matching.runner import run_match_games

    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    out: list[str] = []
    code = run_match_games(
        _settings(), sport="mlb", from_date="2026-07-25", to_date="2026-07-25",
        database_path=db_path, as_json=True, out=out.append,
    )
    assert code == 0
    payload = _json.loads(out[-1])
    assert payload["command"] == "match-games" and payload["dry_run"] is False
    assert payload["canonical_games_created"] == 1 and payload["run_id"] is not None


def test_cli_missing_database_exit_3(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sports_quant.matching.runner import run_match_games

    out: list[str] = []
    code = run_match_games(
        _settings(), sport="mlb", from_date="2026-07-25", database_path=tmp_path / "nope.db",
        out=out.append,
    )
    assert code == 3
