"""Phase D5A follow-up review: league season intervals, resolved-local-slate
doubleheaders, knowledge-time home-venue evidence, and non-swallowed link errors.

Isolated temporary corpora only; no provider client.
"""

from __future__ import annotations

import sqlite3

from sports_quant.db.engine import transaction
from sports_quant.db.repositories.games import SqliteGameRepository
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.matching.players_service import MatchPlayersService
from sports_quant.matching.season import in_season, league_code_from_id, season_bounds
from sports_quant.matching.service import MatchGamesService

from .conftest import (
    raw_response,
    seed_player,
    seed_player_ref,
    seed_roster,
    seed_schedule,
    seed_team,
    seed_venue,
)
from .test_phase_d5a_matching import MLB, NBA, _create_canonical, _match, _mlb_setup, _settings


# --------------------------------------------------------------------------- #
# §2 League season intervals
# --------------------------------------------------------------------------- #
def test_season_bounds_mlb_and_nba() -> None:
    assert season_bounds("MLB", 2025) == ("2025-01-01", "2025-12-31")
    assert season_bounds("NBA", 2025) == ("2025-07-01", "2026-06-30")
    assert league_code_from_id("lg_nba") == "NBA"


def test_in_season_boundaries() -> None:
    assert in_season("MLB", 2025, "2025-06-01") and not in_season("MLB", 2025, "2026-06-01")
    # NBA 2025-26 spans Oct 2025 .. Jun 2026.
    assert in_season("NBA", 2025, "2025-10-15")  # October 2025
    assert in_season("NBA", 2025, "2026-01-10") and in_season("NBA", 2025, "2026-04-15")
    assert not in_season("NBA", 2025, "2025-06-01")  # previous season
    assert not in_season("NBA", 2025, "2026-07-15")  # following season


def _players(conn: sqlite3.Connection, *, provider: str, **kw):  # type: ignore[no-untyped-def]
    with transaction(conn):
        return MatchPlayersService(conn).match_range(provider=provider, **kw)


def _nba_roster_case(conn: sqlite3.Connection, roster_date: str) -> str:
    a = seed_player(conn, league_code="NBA", full_name="NBA Twin", aliases=[("nt", "provider", NBA)])
    seed_player(conn, league_code="NBA", full_name="NBA Twin", aliases=[("nt", "provider", NBA)])
    team = seed_team(conn, league_code="NBA", abbreviation="TNX", canonical_name="NBA X",
                     city="NX", nickname="NXs")
    seed_roster(conn, provider=NBA, provider_team_id="900", team_id=team,
                provider_player_id="nt", player_id=a, roster_date=roster_date)
    seed_player_ref(conn, provider=NBA, provider_player_id="nt")
    return a


def test_nba_october_evidence_accepted(conn: sqlite3.Connection) -> None:
    a = _nba_roster_case(conn, "2025-10-20")  # NBA 2025-26 opening
    r = _players(conn, provider=NBA, season_year=2025)
    ref = SqliteProviderReferenceRepository(conn).get("player", NBA, "nt")
    assert r.counters.accepted == 1 and ref is not None and ref.canonical_id == a


def test_nba_spring_evidence_accepted(conn: sqlite3.Connection) -> None:
    a = _nba_roster_case(conn, "2026-04-05")  # April 2026 still NBA 2025-26
    r = _players(conn, provider=NBA, season_year=2025)
    ref = SqliteProviderReferenceRepository(conn).get("player", NBA, "nt")
    assert r.counters.accepted == 1 and ref is not None and ref.canonical_id == a


def test_nba_following_season_evidence_excluded(conn: sqlite3.Connection) -> None:
    _nba_roster_case(conn, "2026-11-01")  # NBA 2026-27 -> outside 2025-26
    r = _players(conn, provider=NBA, season_year=2025)
    # No season-valid team tier; two same-name players -> ambiguous, not linked.
    assert r.counters.ambiguous == 1 and r.counters.provider_references_linked == 0


def test_nba_preceding_season_evidence_excluded(conn: sqlite3.Connection) -> None:
    _nba_roster_case(conn, "2025-05-01")  # NBA 2024-25 -> outside 2025-26
    r = _players(conn, provider=NBA, season_year=2025)
    assert r.counters.ambiguous == 1 and r.counters.provider_references_linked == 0


def test_nba_trade_between_seasons_does_not_leak(conn: sqlite3.Connection) -> None:
    a = seed_player(conn, league_code="NBA", full_name="Move Guy", aliases=[("mg", "provider", NBA)])
    b = seed_player(conn, league_code="NBA", full_name="Move Guy", aliases=[("mg", "provider", NBA)])
    old = seed_team(conn, league_code="NBA", abbreviation="TNO", canonical_name="NBA Old",
                    city="NO", nickname="NOs")
    new = seed_team(conn, league_code="NBA", abbreviation="TNN", canonical_name="NBA New",
                    city="NN", nickname="NNs")
    seed_roster(conn, provider=NBA, provider_team_id="950", team_id=old, provider_player_id="mg",
                player_id=a, roster_date="2025-11-01")  # NBA 2025-26
    seed_roster(conn, provider=NBA, provider_team_id="951", team_id=new, provider_player_id="mg",
                player_id=b, roster_date="2026-11-01")  # NBA 2026-27
    seed_player_ref(conn, provider=NBA, provider_player_id="mg")
    r = _players(conn, provider=NBA, season_year=2025)
    ref = SqliteProviderReferenceRepository(conn).get("player", NBA, "mg")
    assert r.counters.accepted == 1 and ref is not None and ref.canonical_id == a  # old team


def test_conflicting_same_nba_season_teams_omit_tier(conn: sqlite3.Connection) -> None:
    a = seed_player(conn, league_code="NBA", full_name="Conf Guy", aliases=[("cg", "provider", NBA)])
    seed_player(conn, league_code="NBA", full_name="Conf Guy", aliases=[("cg", "provider", NBA)])
    t1 = seed_team(conn, league_code="NBA", abbreviation="TC3", canonical_name="C3", city="C3", nickname="C3s")
    t2 = seed_team(conn, league_code="NBA", abbreviation="TC4", canonical_name="C4", city="C4", nickname="C4s")
    # Both within NBA 2025-26 (Nov 2025 and Feb 2026) -> conflict -> omit team tier.
    seed_roster(conn, provider=NBA, provider_team_id="960", team_id=t1, provider_player_id="cg",
                player_id=a, roster_date="2025-11-01")
    seed_roster(conn, provider=NBA, provider_team_id="961", team_id=t2, provider_player_id="cg",
                player_id=a, roster_date="2026-02-01")
    seed_player_ref(conn, provider=NBA, provider_player_id="cg")
    r = _players(conn, provider=NBA, season_year=2025)
    assert r.counters.ambiguous == 1 and r.counters.provider_references_linked == 0


# --------------------------------------------------------------------------- #
# §3 Resolved-local-slate doubleheaders (no provider local date)
# --------------------------------------------------------------------------- #
def test_dh_no_provider_date_actual_venue_tz(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)  # venue V1 tz America/New_York
    # Two games, NO provider local date, both resolve to the same NY local slate.
    for pgid, start in (("D1", "2026-07-25T21:00:00Z"), ("D2", "2026-07-26T00:30:00Z")):
        seed_schedule(conn, provider=MLB, provider_game_id=pgid, home_provider_team_id="101",
                      away_provider_team_id="102", scheduled_start=start, season=2026,
                      game_date_local=None, venue_provider_id="V1")
    # Process the later game first -> numbering must still come from resolved order.
    _match(conn, provider_game_id="D2")
    _match(conn, provider_game_id="D1")
    games = SqliteGameRepository(conn)
    d1 = games.find_by_official_key(official_provider=MLB, official_game_key="D1")
    d2 = games.find_by_official_key(official_provider=MLB, official_game_key="D2")
    # 21:00Z = 17:00 EDT (25th); 00:30Z = 20:30 EDT (25th): same NY slate, ranked.
    assert d1 is not None and d2 is not None
    assert d1.game_date_local == "2026-07-25" and d2.game_date_local == "2026-07-25"
    assert d1.game_number == 1 and d2.game_number == 2


def test_dh_no_provider_date_home_venue_tz(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)  # teams THM/TAW + venue V1 seeded
    # Establish an LA home venue via a prior game known before the target cutoff.
    la = seed_venue(conn, name="LA Home", provider=MLB, provider_venue_id="VLAH",
                    timezone="America/Los_Angeles")
    prior = _create_canonical(conn, league_code="MLB", home_team_id="tm_mlb_thm",
                              away_team_id="tm_mlb_taw", scheduled_start="2026-07-01T02:00:00Z",
                              game_date_local="2026-06-30", official_provider=MLB,
                              official_game_key="PRI", decided_at="2026-07-05T00:00:00.000000Z")
    conn.execute("UPDATE games SET venue = ? WHERE game_id = ?", (la, prior))
    conn.commit()
    # Two later games, no venue, no provider date -> home-venue LA tz groups the slate.
    for pgid, start in (("E1", "2026-07-26T02:00:00Z"), ("E2", "2026-07-26T05:00:00Z")):
        seed_schedule(conn, provider=MLB, provider_game_id=pgid, home_provider_team_id="101",
                      away_provider_team_id="102", scheduled_start=start, season=2026,
                      game_date_local=None, observed_at="2026-07-20T18:00:00.000000Z")
    _match(conn, provider_game_id="E2")
    _match(conn, provider_game_id="E1")
    games = SqliteGameRepository(conn)
    e1 = games.find_by_official_key(official_provider=MLB, official_game_key="E1")
    e2 = games.find_by_official_key(official_provider=MLB, official_game_key="E2")
    # 02:00Z and 05:00Z = 19:00 & 22:00 PDT (25th): same LA slate, ranked 1 and 2.
    assert e1 is not None and e2 is not None and e1.game_date_local == "2026-07-25"
    assert e1.game_number == 1 and e2.game_number == 2


def test_dh_equal_observed_at_deterministic(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    # Two schedule snapshots for the SAME provider game with equal observed_at but
    # different scheduled_start: the latest is chosen by a stable tie-break, not row order.
    seed_schedule(conn, provider=MLB, provider_game_id="EQ", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T17:00:00Z", season=2026,
                  game_date_local="2026-07-25", venue_provider_id="V1",
                  observed_at="2026-07-24T18:00:00.000000Z")
    seed_schedule(conn, provider=MLB, provider_game_id="EQ", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:00:00Z", season=2026,
                  game_date_local="2026-07-25", venue_provider_id="V1",
                  observed_at="2026-07-24T18:00:00.000000Z")
    svc = MatchGamesService(conn)
    n1 = svc._slate_game_number(
        provider=MLB, provider_home_pid="101", provider_away_pid="102", provider_game_id="EQ",
        target_local_date="2026-07-25", home_team_id="tm_mlb_thm", schedule_game_number=None)
    n2 = svc._slate_game_number(
        provider=MLB, provider_home_pid="101", provider_away_pid="102", provider_game_id="EQ",
        target_local_date="2026-07-25", home_team_id="tm_mlb_thm", schedule_game_number=None)
    assert n1 == n2 == 1  # single provider game -> one game, deterministic


# --------------------------------------------------------------------------- #
# §4 Knowledge-time home-venue bound
# --------------------------------------------------------------------------- #
def test_home_game_matched_after_cutoff_is_ignored(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    la = seed_venue(conn, name="Late LA", provider=MLB, provider_venue_id="VLL",
                    timezone="America/Los_Angeles")
    # Prior-by-event-time game, but its match decision is dated AFTER the target cutoff.
    prior = _create_canonical(conn, league_code="MLB", home_team_id="tm_mlb_thm",
                              away_team_id="tm_mlb_taw", scheduled_start="2026-07-01T02:00:00Z",
                              game_date_local="2026-06-30", official_provider=MLB,
                              official_game_key="LATEDEC",
                              decided_at="2026-08-01T00:00:00.000000Z")  # after target cutoff
    conn.execute("UPDATE games SET venue = ? WHERE game_id = ?", (la, prior))
    conn.commit()
    seed_schedule(conn, provider=MLB, provider_game_id="TGT", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:00:00Z", season=2026,
                  game_date_local=None, observed_at="2026-07-20T18:00:00.000000Z")
    _match(conn, provider_game_id="TGT")
    g = SqliteGameRepository(conn).find_by_official_key(official_provider=MLB, official_game_key="TGT")
    # The evidence was not known by the target cutoff -> conservative UTC fallback.
    assert g is not None and g.game_date_local == "2026-07-26"
    assert "DQ-TZ-001" in {r[0] for r in conn.execute("SELECT rule_code FROM data_quality_issues")}


def test_unreviewed_neutral_swapped_not_home_evidence(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    la = seed_venue(conn, name="Swap LA", provider=MLB, provider_venue_id="VSL",
                    timezone="America/Los_Angeles")
    # Prior game whose ONLY supporting decision is a neutral-swapped (review-gated) match.
    prior = _create_canonical(conn, league_code="MLB", home_team_id="tm_mlb_thm",
                              away_team_id="tm_mlb_taw", scheduled_start="2026-07-01T02:00:00Z",
                              game_date_local="2026-06-30", official_provider=MLB,
                              official_game_key="SWAP", decided_at="2026-07-05T00:00:00.000000Z",
                              decision_method="schedule_key_swapped")
    conn.execute("UPDATE games SET venue = ? WHERE game_id = ?", (la, prior))
    conn.commit()
    seed_schedule(conn, provider=MLB, provider_game_id="TGT2", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:00:00Z", season=2026,
                  game_date_local=None, observed_at="2026-07-20T18:00:00.000000Z")
    _match(conn, provider_game_id="TGT2")
    g = SqliteGameRepository(conn).find_by_official_key(official_provider=MLB, official_game_key="TGT2")
    assert g is not None and g.game_date_local == "2026-07-26"  # swapped evidence excluded -> UTC


# --------------------------------------------------------------------------- #
# §5 Non-swallowed link errors
# --------------------------------------------------------------------------- #
def _unlinked_team_ref(conn: sqlite3.Connection, provider: str, provider_team_id: str) -> None:
    rid, rhash = raw_response(conn, marker=f"teamref:{provider_team_id}")
    with transaction(conn):
        SqliteProviderReferenceRepository(conn).upsert(
            kind="team", provider=provider, provider_entity_id=provider_team_id,
            raw_response_id=rid, raw_response_hash=rhash, observed_at="2026-07-24T18:00:00.000000Z")


def test_expected_team_link_succeeds(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    _unlinked_team_ref(conn, MLB, "101")
    _unlinked_team_ref(conn, MLB, "102")
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z", season=2026,
                  game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, provider_game_id="G1")
    home_ref = SqliteProviderReferenceRepository(conn).get("team", MLB, "101")
    assert home_ref is not None and home_ref.canonical_id == "tm_mlb_thm"
    # game + both teams linked.
    assert r.counters.provider_references_linked >= 3


def test_missing_team_reference_does_not_silently_pass(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)  # no team refs seeded
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z", season=2026,
                  game_date_local="2026-07-25", venue_provider_id="V1")
    r = _match(conn, provider_game_id="G1")
    # Team decisions are still recorded (accepted); the absent crosswalk is an
    # explicit skip, not counted as linked, and never a swallowed exception.
    team_decisions = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=MLB, source_ref="101", entity_type="team")
    assert team_decisions and team_decisions[0].outcome == "accepted"
    assert r.needs_failure_exit is False  # missing optional crosswalk is not blocking


def test_repository_link_failure_becomes_command_failure(conn, db_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sports_quant.db.repositories import references as refs_mod
    from sports_quant.matching.runner import run_match_players

    seed_player(conn, league_code="MLB", full_name="P1", aliases=[("p1", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="p1")

    def _boom(self, **kwargs):  # noqa: ANN001, ANN003
        raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(refs_mod.SqliteProviderReferenceRepository, "link_canonical", _boom)
    out: list[str] = []
    code = run_match_players(_settings(), sport="mlb", database_path=db_path, out=out.append)
    assert code == 1  # the failure is NOT swallowed -> command failure


def test_dry_run_players_creates_no_link(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="P1", aliases=[("p1", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="p1")
    before = SqliteMatchingRepository(conn).count()
    MatchPlayersService(conn, dry_run=True).match_range(provider=MLB)
    assert SqliteMatchingRepository(conn).count() == before
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "p1")
    assert ref is not None and ref.canonical_id is None
