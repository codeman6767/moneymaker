"""Phase D5A completeness/correctness repair: player orchestration, the actual
home-venue timezone fallback, and exact-link league-scope validation.

Offline/mocked only, no provider client. Reuses helpers from
``test_phase_d5a_matching`` and the shared conftest.
"""

from __future__ import annotations

import sqlite3

from sports_quant.db.engine import transaction
from sports_quant.db.repositories.games import SqliteGameRepository
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.matching.model import MATCHED, UNMATCHED
from sports_quant.matching.players_service import MatchPlayersService
from sports_quant.matching.teams import TeamResolver

from .conftest import (
    link_player_ref,
    seed_player,
    seed_player_ref,
    seed_roster,
    seed_schedule,
    seed_team,
    seed_venue,
)
from .test_phase_d5a_matching import (
    MLB,
    NBA,
    _create_canonical,
    _match,
    _mlb_setup,
    _settings,
    _two_mlb_teams,
)


def _players(conn: sqlite3.Connection, *, dry_run: bool = False, provider: str = MLB, **kw):  # type: ignore[no-untyped-def]
    svc = MatchPlayersService(conn, dry_run=dry_run)
    if dry_run:
        return svc.match_range(provider=provider, **kw)
    with transaction(conn):
        return svc.match_range(provider=provider, **kw)


def _dq_codes(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}


# --------------------------------------------------------------------------- #
# Player orchestration
# --------------------------------------------------------------------------- #
def test_player_orchestration_mlb_links(conn: sqlite3.Connection) -> None:
    pid = seed_player(conn, league_code="MLB", full_name="P100",
                      aliases=[("P100", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="P100")
    r = _players(conn, provider=MLB)
    assert r.counters.accepted == 1 and r.counters.provider_references_linked == 1
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "P100")
    assert ref is not None and ref.canonical_id == pid


def test_player_orchestration_nba_links(conn: sqlite3.Connection) -> None:
    pid = seed_player(conn, league_code="NBA", full_name="N200",
                      aliases=[("N200", "provider", NBA)])
    seed_player_ref(conn, provider=NBA, provider_player_id="N200")
    r = _players(conn, provider=NBA)
    assert r.counters.accepted == 1 and r.counters.provider_references_linked == 1
    ref = SqliteProviderReferenceRepository(conn).get("player", NBA, "N200")
    assert ref is not None and ref.canonical_id == pid


def test_player_decision_and_candidates_recorded(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="P100", aliases=[("P100", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="P100")
    _players(conn, provider=MLB)
    repo = SqliteMatchingRepository(conn)
    decisions = repo.decisions_for_source(source_provider=MLB, source_ref="P100", entity_type="player")
    assert len(decisions) == 1 and decisions[0].outcome == "accepted"
    assert repo.candidates(decisions[0].match_id)


def test_ambiguous_player_not_linked(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="DUP", aliases=[("DUP", "provider", MLB)])
    seed_player(conn, league_code="MLB", full_name="DUP", aliases=[("DUP", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="DUP")
    r = _players(conn, provider=MLB)
    assert r.counters.ambiguous == 1 and r.counters.provider_references_linked == 0
    assert r.counters.manual_review_required == 1
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "DUP")
    assert ref is not None and ref.canonical_id is None


def test_unknown_player_no_creation_no_link(conn: sqlite3.Connection) -> None:
    seed_player_ref(conn, provider=MLB, provider_player_id="GHOST")
    before = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    r = _players(conn, provider=MLB)
    assert r.counters.no_candidate == 1 and r.counters.provider_references_linked == 0
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == before
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "GHOST")
    assert ref is not None and ref.canonical_id is None


def test_player_omitted_suffix_via_orchestration(conn: sqlite3.Connection) -> None:
    # One canonical suffixed player; provider alias omits the suffix -> permissive.
    pid = seed_player(conn, league_code="MLB", full_name="Ronald Acuna Jr.", suffix="jr",
                      aliases=[("acuna99", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="acuna99")
    r = _players(conn, provider=MLB)
    assert r.counters.accepted == 1
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "acuna99")
    assert ref is not None and ref.canonical_id == pid


def test_player_team_disambiguates_via_orchestration(conn: sqlite3.Connection) -> None:
    a = seed_player(conn, league_code="MLB", full_name="TwinA", aliases=[("tw", "provider", MLB)])
    b = seed_player(conn, league_code="MLB", full_name="TwinB", aliases=[("tw", "provider", MLB)])
    team = seed_team(conn, league_code="MLB", abbreviation="TPZ", canonical_name="Team PZ",
                     city="PZ", nickname="PZs")
    seed_roster(conn, provider=MLB, provider_team_id="900", team_id=team,
                provider_player_id="tw", player_id=a)
    seed_player_ref(conn, provider=MLB, provider_player_id="tw")
    r = _players(conn, provider=MLB)
    assert r.counters.accepted == 1  # roster-derived team resolves the ambiguity
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "tw")
    assert ref is not None and ref.canonical_id == a and b != a


def test_player_dry_run_persists_nothing(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="P100", aliases=[("P100", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="P100")
    before_dec = SqliteMatchingRepository(conn).count()
    r = _players(conn, dry_run=True, provider=MLB)
    assert r.counters.decisions_evaluated == 1  # computed
    assert SqliteMatchingRepository(conn).count() == before_dec  # nothing persisted
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "P100")
    assert ref is not None and ref.canonical_id is None


def test_player_matching_idempotent(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="P100", aliases=[("P100", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="P100")
    _players(conn, provider=MLB)
    # Second run: the reference is now linked, so there is nothing unresolved left.
    r2 = _players(conn, provider=MLB)
    assert r2.references_considered == 0 and r2.counters.decisions_evaluated == 0


def test_player_scope_conflict_blocking(conn: sqlite3.Connection) -> None:
    # A bad crosswalk: an MLB provider-player id linked to an NBA canonical player.
    nba_pid = seed_player(conn, league_code="NBA", full_name="Cross League")
    seed_player_ref(conn, provider=MLB, provider_player_id="X1")
    link_player_ref(conn, provider=MLB, provider_player_id="X1", player_id=nba_pid)
    from sports_quant.matching.players import PlayerResolver

    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="X1", league_id="lg_mlb")
    # The 1.00 exact-link tier is refused because the player is in the wrong league.
    assert res.scope_conflict and res.status == UNMATCHED and res.entity_id is None


def test_player_cli_json_and_exit(conn: sqlite3.Connection, db_path) -> None:  # type: ignore[no-untyped-def]
    import json as _json

    from sports_quant.matching.runner import run_match_players

    seed_player(conn, league_code="MLB", full_name="P100", aliases=[("P100", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="P100")
    out: list[str] = []
    code = run_match_players(
        _settings(), sport="mlb", database_path=db_path, as_json=True, out=out.append)
    assert code == 0
    payload = _json.loads(out[-1])
    assert payload["command"] == "match-players" and payload["accepted"] == 1
    assert payload["provider_references_linked"] == 1


# --------------------------------------------------------------------------- #
# Home-venue timezone fallback
# --------------------------------------------------------------------------- #
def test_actual_venue_tz_precedence_service(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)  # venue V1 tz America/New_York
    seed_schedule(conn, provider=MLB, provider_game_id="GA", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:05:00Z",
                  season=2026, game_date_local=None, venue_provider_id="V1")
    _match(conn, provider_game_id="GA")
    g = SqliteGameRepository(conn).find_by_official_key(
        official_provider=MLB, official_game_key="GA")
    assert g is not None and g.game_date_local == "2026-07-25"  # NY tz, not UTC 07-26


def test_provider_local_date_second_service(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)  # no venue seeded -> venue unresolved
    seed_schedule(conn, provider=MLB, provider_game_id="GB", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:05:00Z",
                  season=2026, game_date_local="2026-07-25")  # provider local date present
    _match(conn, provider_game_id="GB")
    g = SqliteGameRepository(conn).find_by_official_key(
        official_provider=MLB, official_game_key="GB")
    assert g is not None and g.game_date_local == "2026-07-25"
    assert home != away


def test_home_venue_tz_third_service(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    # Establish the home team's ordinary venue (LA tz) via a prior canonical game.
    la_venue = seed_venue(conn, name="LA Park", provider=MLB, provider_venue_id="VLA",
                          timezone="America/Los_Angeles")
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-20T02:00:00Z", game_date_local="2026-07-19",
                      official_provider=MLB, official_game_key="GPRIOR")
    conn.execute("UPDATE games SET venue = ? WHERE official_game_key = 'GPRIOR'", (la_venue,))
    conn.commit()
    # New game: no venue, no provider local date -> tier 3 (home venue tz) must fire.
    seed_schedule(conn, provider=MLB, provider_game_id="GC", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:00:00Z",
                  season=2026, game_date_local=None)
    _match(conn, provider_game_id="GC")
    g = SqliteGameRepository(conn).find_by_official_key(
        official_provider=MLB, official_game_key="GC")
    assert g is not None and g.game_date_local == "2026-07-25"  # LA date, tier-3 home venue


def test_utc_fallback_only_when_all_fail(conn: sqlite3.Connection) -> None:
    _two_mlb_teams(conn)  # no venue, no prior home game
    seed_schedule(conn, provider=MLB, provider_game_id="GD", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:00:00Z",
                  season=2026, game_date_local=None)
    _match(conn, provider_game_id="GD")
    g = SqliteGameRepository(conn).find_by_official_key(
        official_provider=MLB, official_game_key="GD")
    assert g is not None and g.game_date_local == "2026-07-26"  # UTC fallback
    assert "DQ-TZ-001" in _dq_codes(conn)


def test_neutral_venue_not_replaced_by_home(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    # Give the home team an ordinary NY park (via a prior canonical home game), so
    # a home-venue tz genuinely exists and we can prove it is NOT used.
    ny_venue = seed_venue(conn, name="Home NY", provider=MLB, provider_venue_id="VNY",
                          timezone="America/New_York")
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-20T23:00:00Z", game_date_local="2026-07-20",
                      official_provider=MLB, official_game_key="GHN")
    conn.execute("UPDATE games SET venue = ? WHERE official_game_key = 'GHN'", (ny_venue,))
    conn.commit()
    # Actual event venue is an international (London) park with its own tz.
    seed_venue(conn, name="London Stadium", provider=MLB, provider_venue_id="VLDN",
               timezone="Europe/London")
    seed_schedule(conn, provider=MLB, provider_game_id="GE", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T03:30:00Z",
                  season=2026, game_date_local=None, venue_provider_id="VLDN")
    _match(conn, provider_game_id="GE")
    g = SqliteGameRepository(conn).find_by_official_key(
        official_provider=MLB, official_game_key="GE")
    # 03:30 UTC is 04:30 London (25th) but 23:30 NY (24th). The actual London
    # venue must win over the NY home park.
    assert g is not None and g.game_date_local == "2026-07-25"


def test_invalid_home_venue_tz_is_honest(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    bad = seed_venue(conn, name="Bad TZ Park", provider=MLB, provider_venue_id="VBAD",
                     timezone="Not/AZone")
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-20T23:00:00Z", game_date_local="2026-07-20",
                      official_provider=MLB, official_game_key="GBAD")
    conn.execute("UPDATE games SET venue = ? WHERE official_game_key = 'GBAD'", (bad,))
    conn.commit()
    seed_schedule(conn, provider=MLB, provider_game_id="GF", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:00:00Z",
                  season=2026, game_date_local=None)
    r = _match(conn, provider_game_id="GF")
    # Invalid tz is refused honestly (DQ-TZ-001, no silent UTC), no game created.
    assert r.counters.no_candidate >= 1
    assert SqliteGameRepository(conn).find_by_official_key(
        official_provider=MLB, official_game_key="GF") is None
    assert "DQ-TZ-001" in _dq_codes(conn)


# --------------------------------------------------------------------------- #
# Exact-link league-scope validation
# --------------------------------------------------------------------------- #
def _link_team_ref(conn: sqlite3.Connection, provider: str, provider_team_id: str, team_id: str) -> None:
    from .conftest import raw_response

    rid, rhash = raw_response(conn, marker=f"teamref:{provider_team_id}")
    with transaction(conn):
        ref, _ = SqliteProviderReferenceRepository(conn).upsert(
            kind="team", provider=provider, provider_entity_id=provider_team_id,
            raw_response_id=rid, raw_response_hash=rhash, observed_at="2026-07-24T18:00:00.000000Z",
        )
        conn.execute(
            "UPDATE provider_team_references SET team_id = ? WHERE reference_id = ? "
            "AND team_id IS NULL", (team_id, ref.reference_id))


def test_exact_team_link_wrong_league_is_blocking(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    nba_team = seed_team(conn, league_code="NBA", abbreviation="TZZ", canonical_name="Wrong League",
                         city="WL", nickname="WLs")
    _link_team_ref(conn, MLB, "999", nba_team)  # MLB provider id linked to an NBA team
    seed_schedule(conn, provider=MLB, provider_game_id="GX", home_provider_team_id="999",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, provider_game_id="GX")
    assert r.needs_failure_exit and r.counters.blocking_issues >= 1
    assert "DQ-MATCH-014" in _dq_codes(conn)
    assert SqliteGameRepository(conn).count() == 0


def test_exact_valid_team_link_scores_one(conn: sqlite3.Connection) -> None:
    home = seed_team(conn, league_code="MLB", abbreviation="TVL", canonical_name="Valid Link",
                     city="VL", nickname="VLs")
    _link_team_ref(conn, MLB, "555", home)
    res = TeamResolver(conn).resolve(provider=MLB, provider_team_id="555", league_id="lg_mlb")
    assert res.status == MATCHED and res.entity_id == home
    assert res.tier == "exact_provider_id" and res.score == 1.00


def test_d5b_sportsbook_kalshi_untouched(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, provider_game_id="G1")
    seed_player(conn, league_code="MLB", full_name="P100", aliases=[("P100", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="P100")
    _players(conn, provider=MLB)
    types = {row[0] for row in conn.execute("SELECT DISTINCT entity_type FROM entity_match_decisions")}
    assert not (types & {"sportsbook_event", "kalshi_event", "kalshi_market"})
    # Any pre-existing sportsbook/kalshi game links stay NULL (none created here).
    for table, col in (("sportsbook_events", "game_id"), ("kalshi_markets", "game_id")):
        linked = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL"  # noqa: S608
        ).fetchone()[0]
        assert linked == 0
