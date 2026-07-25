"""Phase D5A independent review: determinism, provenance, scope, and PIT.

Covers the repaired defects — provider-scoped player aliases, season-safe roster
evidence, batch-order-independent doubleheader numbering, time-bounded home-venue
timezone, exact-decision linking, and raw/run provenance on decisions. Isolated
temporary corpora only; no provider client.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.games import SqliteGameRepository
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.matching.model import MATCHED, UNMATCHED
from sports_quant.matching.players import PlayerResolver
from sports_quant.matching.players_service import MatchPlayersService
from sports_quant.matching.service import MatchGamesService

from .conftest import (
    seed_player,
    seed_player_ref,
    seed_roster,
    seed_schedule,
    seed_team,
    seed_venue,
)
from .test_phase_d5a_matching import MLB, NBA, _create_canonical, _match, _mlb_setup, _two_mlb_teams


def _players(conn: sqlite3.Connection, *, provider: str = MLB, dry_run: bool = False, **kw):  # type: ignore[no-untyped-def]
    svc = MatchPlayersService(conn, dry_run=dry_run)
    if dry_run:
        return svc.match_range(provider=provider, **kw)
    with transaction(conn):
        return svc.match_range(provider=provider, **kw)


# --------------------------------------------------------------------------- #
# Provider-scoped player aliases
# --------------------------------------------------------------------------- #
def test_other_provider_alias_excluded(conn: sqlite3.Connection) -> None:
    # An MLB player whose only alias is scoped to a DIFFERENT provider.
    seed_player(conn, league_code="MLB", full_name="Scoped Guy",
                aliases=[("scopedguy", "provider", "the_odds_api")])
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="scopedguy", league_id="lg_mlb")
    assert res.status == UNMATCHED  # mlb_statsapi cannot use the_odds_api's alias


def test_provider_neutral_alias_allowed(conn: sqlite3.Connection) -> None:
    pid = seed_player(conn, league_code="MLB", full_name="Neutral Guy",
                      aliases=[("Neutral Guy", "full", "")])  # provider-neutral
    res = PlayerResolver(conn).resolve(
        provider=MLB, provider_player_id="x", raw_name="Neutral Guy", league_id="lg_mlb")
    assert res.status == MATCHED and res.entity_id == pid


def test_identical_provider_id_stays_provider_and_league_scoped(conn: sqlite3.Connection) -> None:
    mlb_pid = seed_player(conn, league_code="MLB", full_name="M100",
                          aliases=[("100", "provider", MLB)])
    nba_pid = seed_player(conn, league_code="NBA", full_name="N100",
                          aliases=[("100", "provider", NBA)])
    mlb = PlayerResolver(conn).resolve(provider=MLB, provider_player_id="100", league_id="lg_mlb")
    nba = PlayerResolver(conn).resolve(provider=NBA, provider_player_id="100", league_id="lg_nba")
    assert mlb.entity_id == mlb_pid and nba.entity_id == nba_pid and mlb_pid != nba_pid


# --------------------------------------------------------------------------- #
# Season-safe roster evidence
# --------------------------------------------------------------------------- #
def test_traded_player_uses_season_team(conn: sqlite3.Connection) -> None:
    a = seed_player(conn, league_code="MLB", full_name="Trade Twin", aliases=[("tt", "provider", MLB)])
    b = seed_player(conn, league_code="MLB", full_name="Trade Twin", aliases=[("tt", "provider", MLB)])
    old_team = seed_team(conn, league_code="MLB", abbreviation="TOL", canonical_name="Old Team",
                         city="Old", nickname="Olds")
    new_team = seed_team(conn, league_code="MLB", abbreviation="TNW", canonical_name="New Team",
                         city="New", nickname="News")
    # Player 'a' is on old_team in 2025 (the requested season) but the provider id
    # 'tt' has a 2026 roster on new_team. Season 2025 must use the 2025 team.
    seed_roster(conn, provider=MLB, provider_team_id="700", team_id=old_team,
                provider_player_id="tt", player_id=a,
                observed_at="2025-04-01T00:00:00.000000Z", roster_date="2025-04-01")
    seed_roster(conn, provider=MLB, provider_team_id="701", team_id=new_team,
                provider_player_id="tt", player_id=b,
                observed_at="2026-04-01T00:00:00.000000Z", roster_date="2026-04-01")
    seed_player_ref(conn, provider=MLB, provider_player_id="tt")
    r = _players(conn, provider=MLB, season_year=2025)
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "tt")
    assert r.counters.accepted == 1 and ref is not None and ref.canonical_id == a  # 2025 team wins


def test_conflicting_season_teams_omits_team_tier(conn: sqlite3.Connection) -> None:
    a = seed_player(conn, league_code="MLB", full_name="Amb Twin", aliases=[("at", "provider", MLB)])
    seed_player(conn, league_code="MLB", full_name="Amb Twin", aliases=[("at", "provider", MLB)])
    t1 = seed_team(conn, league_code="MLB", abbreviation="TQ1", canonical_name="Q1", city="Q1", nickname="Q1s")
    t2 = seed_team(conn, league_code="MLB", abbreviation="TQ2", canonical_name="Q2", city="Q2", nickname="Q2s")
    # The SAME provider id appears on two teams in 2025 -> conflicting -> omit team tier.
    seed_roster(conn, provider=MLB, provider_team_id="710", team_id=t1, provider_player_id="at",
                player_id=a, roster_date="2025-04-01")
    seed_roster(conn, provider=MLB, provider_team_id="711", team_id=t2, provider_player_id="at",
                player_id=a, roster_date="2025-04-02")
    seed_player_ref(conn, provider=MLB, provider_player_id="at")
    r = _players(conn, provider=MLB, season_year=2025)
    # Two same-name players, no usable team tier -> ambiguous, not linked.
    assert r.counters.ambiguous == 1 and r.counters.provider_references_linked == 0


def test_missing_season_evidence_falls_back_to_league(conn: sqlite3.Connection) -> None:
    pid = seed_player(conn, league_code="MLB", full_name="Solo Guy", aliases=[("solo", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="solo")
    r = _players(conn, provider=MLB, season_year=2025)  # no roster at all
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "solo")
    assert r.counters.accepted == 1 and ref is not None and ref.canonical_id == pid


# --------------------------------------------------------------------------- #
# Batch-order-independent doubleheader numbering
# --------------------------------------------------------------------------- #
def _seed_slate(conn: sqlite3.Connection, games: list[tuple[str, str]]) -> None:
    _mlb_setup(conn)
    for pgid, start in games:
        seed_schedule(conn, provider=MLB, provider_game_id=pgid, home_provider_team_id="101",
                      away_provider_team_id="102", scheduled_start=start, season=2026,
                      game_date_local="2026-07-25", venue_provider_id="V1")


def test_dh_later_presented_first_gets_game_2(conn: sqlite3.Connection) -> None:
    # Process the later game first (by provider-game id order it is "GA" but starts later).
    _seed_slate(conn, [("GA", "2026-07-25T23:00:00Z"), ("GB", "2026-07-25T17:00:00Z")])
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    games = SqliteGameRepository(conn)
    ga = games.find_by_official_key(official_provider=MLB, official_game_key="GA")
    gb = games.find_by_official_key(official_provider=MLB, official_game_key="GB")
    assert ga is not None and gb is not None
    assert gb.game_number == 1 and ga.game_number == 2  # earliest start = game 1


def test_dh_number_order_independent_100_shuffles(tmp_path: Path) -> None:
    starts = ["2026-07-25T17:00:00Z", "2026-07-25T20:00:00Z", "2026-07-25T23:00:00Z"]
    ids = ["Ga", "Gb", "Gc"]
    expected = {"Ga": 1, "Gb": 2, "Gc": 3}  # chronological rank
    rng = random.Random(12345)
    for i in range(100):
        order = list(zip(ids, starts, strict=True))
        rng.shuffle(order)
        db = tmp_path / f"dh{i}.db"
        initialize_database(db)
        with Database(db).connection() as c:
            _mlb_setup(c)
            for pgid, ss in order:  # shuffled INSERTION order
                seed_schedule(c, provider=MLB, provider_game_id=pgid, home_provider_team_id="101",
                              away_provider_team_id="102", scheduled_start=ss, season=2026,
                              game_date_local="2026-07-25", venue_provider_id="V1")
            svc = MatchGamesService(c)
            for pgid in rng.sample(ids, len(ids)):  # shuffled query order
                num = svc._slate_game_number(
                    provider=MLB, provider_home_pid="101", provider_away_pid="102",
                    provider_game_id=pgid, target_local_date="2026-07-25",
                    home_team_id="tm_mlb_thm", schedule_game_number=None)
                assert num == expected[pgid]


def test_dh_partial_run_sees_unresolved_sibling(conn: sqlite3.Connection) -> None:
    # Both siblings are in the schedule corpus, but only the LATER one is processed.
    _seed_slate(conn, [("EARLY", "2026-07-25T17:00:00Z"), ("LATE", "2026-07-25T23:00:00Z")])
    _match(conn, provider_game_id="LATE")
    late = SqliteGameRepository(conn).find_by_official_key(
        official_provider=MLB, official_game_key="LATE")
    assert late is not None and late.game_number == 2  # earlier sibling seen in corpus


def test_provider_game_number_overrides_inferred(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    # Provider says the later-starting game is game 1 (a legitimate provider fact).
    seed_schedule(conn, provider=MLB, provider_game_id="P1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:00:00Z", season=2026,
                  game_date_local="2026-07-25", venue_provider_id="V1", game_number=1)
    seed_schedule(conn, provider=MLB, provider_game_id="P2", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T17:00:00Z", season=2026,
                  game_date_local="2026-07-25", venue_provider_id="V1", game_number=2)
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    games = SqliteGameRepository(conn)
    p1 = games.find_by_official_key(official_provider=MLB, official_game_key="P1")
    p2 = games.find_by_official_key(official_provider=MLB, official_game_key="P2")
    assert p1 is not None and p1.game_number == 1  # provider number overrides start order
    assert p2 is not None and p2.game_number == 2


# --------------------------------------------------------------------------- #
# Time-bounded home-venue timezone
# --------------------------------------------------------------------------- #
def test_future_home_game_cannot_supply_tz(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    la = seed_venue(conn, name="Future LA", provider=MLB, provider_venue_id="VF",
                    timezone="America/Los_Angeles")
    # A FUTURE canonical home game (later start) at an LA-tz park.
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-08-01T02:00:00Z", game_date_local="2026-07-31",
                      official_provider=MLB, official_game_key="GFUT")
    conn.execute("UPDATE games SET venue = ? WHERE official_game_key = 'GFUT'", (la,))
    conn.commit()
    # Target game is EARLIER; no actual venue, no provider date -> must NOT use the future tz.
    seed_schedule(conn, provider=MLB, provider_game_id="GT", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-26T02:00:00Z",
                  season=2026, game_date_local=None)
    _match(conn, provider_game_id="GT")
    g = SqliteGameRepository(conn).find_by_official_key(official_provider=MLB, official_game_key="GT")
    assert g is not None and g.game_date_local == "2026-07-26"  # UTC fallback, not LA (07-25)
    assert "DQ-TZ-001" in {r[0] for r in conn.execute("SELECT rule_code FROM data_quality_issues")}


def test_relocation_does_not_leak_backward(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    ny = seed_venue(conn, name="Old NY", provider=MLB, provider_venue_id="VNY2",
                    timezone="America/New_York")
    la = seed_venue(conn, name="New LA", provider=MLB, provider_venue_id="VLA2",
                    timezone="America/Los_Angeles")
    # Prior home game (before target) at NY; a LATER relocation game at LA.
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-07-01T23:00:00Z", game_date_local="2026-07-01",
                      official_provider=MLB, official_game_key="GOLD")
    _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-08-01T02:00:00Z", game_date_local="2026-07-31",
                      official_provider=MLB, official_game_key="GNEW")
    conn.execute("UPDATE games SET venue = ? WHERE official_game_key = 'GOLD'", (ny,))
    conn.execute("UPDATE games SET venue = ? WHERE official_game_key = 'GNEW'", (la,))
    conn.commit()
    # Target between the two: only the prior NY game is valid evidence -> NY date.
    seed_schedule(conn, provider=MLB, provider_game_id="GMID", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-15T03:00:00Z",
                  season=2026, game_date_local=None)
    _match(conn, provider_game_id="GMID")
    g = SqliteGameRepository(conn).find_by_official_key(official_provider=MLB, official_game_key="GMID")
    # 03:00 UTC is 23:00 NY on 07-14 (single prior venue). The later LA relocation
    # must not make the home history ambiguous (which would force UTC 07-15).
    assert g is not None and g.game_date_local == "2026-07-14"


# --------------------------------------------------------------------------- #
# Provenance + exact-decision linking + PIT
# --------------------------------------------------------------------------- #
def test_decision_stores_raw_response_provenance(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, provider_game_id="G1")
    sched_raw = conn.execute(
        "SELECT raw_response_id FROM game_schedule_snapshots WHERE provider_game_id='G1'"
    ).fetchone()[0]
    for entity_type in ("game", "team", "venue"):
        row = conn.execute(
            "SELECT raw_response_id FROM entity_match_decisions WHERE entity_type=? LIMIT 1",
            (entity_type,),
        ).fetchone()
        assert row is not None and row[0] == sched_raw  # source provenance attached


def test_game_link_references_exact_decision(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, provider_game_id="G1")
    ref = SqliteProviderReferenceRepository(conn).get("game", MLB, "G1")
    decision = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=MLB, source_ref="G1", entity_type="game")[0]
    assert ref is not None and ref.match_decision_id == decision.match_id


def test_link_ignores_unrelated_latest_decision(conn: sqlite3.Connection) -> None:
    # A stray decision for the same source_ref with a LATER-sorting match_id: the
    # link must reference the exact accepted decision, not the stray "latest".
    seed_player(conn, league_code="MLB", full_name="Exact Guy", aliases=[("eg", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="eg")
    with transaction(conn):
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, "
            "source_ref, outcome, method, score, threshold, rejection_reason, matcher_version, "
            "decided_at, created_at) VALUES "
            "('mtc_ZZZZZZZZZZZZZZZZZZZZZZZZZZ','player',?, 'eg','rejected','none',0.0,0.85,"
            "'stray','v0','2026-07-24T18:00:00.000000Z','2026-07-24T18:00:00.000000Z')",
            (MLB,))
    _players(conn, provider=MLB)
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "eg")
    accepted = [
        d for d in SqliteMatchingRepository(conn).decisions_for_source(
            source_provider=MLB, source_ref="eg", entity_type="player")
        if d.outcome == "accepted"
    ][0]
    assert ref is not None and ref.match_decision_id == accepted.match_id
    assert ref.match_decision_id != "mtc_ZZZZZZZZZZZZZZZZZZZZZZZZZZ"


def test_asof_read_hides_later_decision_and_link(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1")
    _match(conn, provider_game_id="G1")
    repo = SqliteMatchingRepository(conn)
    d = repo.decisions_for_source(source_provider=MLB, source_ref="G1", entity_type="game")[0]
    # Before the decision existed, the PIT read exposes neither the decision nor
    # the (now-current) provider link.
    assert repo.decisions_for_source(
        source_provider=MLB, source_ref="G1", as_of="2000-01-01T00:00:00.000000Z") == []
    assert repo.decisions_for_source(source_provider=MLB, source_ref="G1", as_of=d.decided_at)
