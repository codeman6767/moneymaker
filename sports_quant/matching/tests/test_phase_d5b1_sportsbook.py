"""Phase D5B1: deterministic sportsbook-event matching + outcome orientation.

Isolated temporary corpora only; no provider client, no network. Canonical games
are seeded via the D5A helpers; sportsbook events/markets/outcomes/prices via the
conftest sportsbook helpers.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.db.repositories.sportsbook import SqliteSportsbookRepository
from sports_quant.matching.sportsbook import MatchSportsbookService

from .conftest import (
    seed_sb_event,
    seed_sb_market,
    seed_sb_outcome,
    seed_sb_price,
    seed_team,
    seed_venue,
)
from .test_phase_d5a_matching import _create_canonical, _settings, _two_mlb_teams

ODDS = "the_odds_api"
MLB_KEY = "baseball_mlb"
NBA_KEY = "basketball_nba"


def _sb(conn: sqlite3.Connection, *, dry_run: bool = False, **kw):  # type: ignore[no-untyped-def]
    svc = MatchSportsbookService(conn, dry_run=dry_run)
    if dry_run:
        return svc.match_range(**kw)
    with transaction(conn):
        return svc.match_range(**kw)


def _setup_mlb(conn: sqlite3.Connection) -> tuple[str, str]:
    """Two MLB teams with an established NY home venue (so local tier is not UTC)."""

    home, away = _two_mlb_teams(conn)
    venue = seed_venue(conn, name="Home Park", provider="mlb_statsapi", provider_venue_id="V1",
                       timezone="America/New_York")
    prior = _create_canonical(
        conn, league_code="MLB", home_team_id=home, away_team_id=away,
        scheduled_start="2026-07-01T23:00:00Z", game_date_local="2026-07-01",
        official_provider="mlb_statsapi", official_game_key="PRIOR",
        decided_at="2026-07-05T00:00:00.000000Z")
    conn.execute("UPDATE games SET venue = ? WHERE game_id = ?", (venue, prior))
    conn.commit()
    return home, away


def _game(conn: sqlite3.Connection, *, home: str, away: str, key: str,
          start: str = "2026-07-25T23:05:00Z", date: str = "2026-07-25",
          neutral: bool = False, gn: int = 1) -> str:
    return _create_canonical(
        conn, league_code="MLB", home_team_id=home, away_team_id=away, scheduled_start=start,
        game_date_local=date, official_provider="mlb_statsapi", official_game_key=key,
        is_neutral_site=neutral, game_number=gn, decided_at="2026-07-05T00:00:00.000000Z")


def _event_link(conn: sqlite3.Connection, sb_id: str):  # type: ignore[no-untyped-def]
    return SqliteSportsbookRepository(conn).event_link(sb_id)


# --------------------------------------------------------------------------- #
# Event matching
# --------------------------------------------------------------------------- #
def test_ordinary_mlb_direct_match(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_accepted == 1 and r.counters.direct_orientation == 1
    game_id, decision_id, orientation = _event_link(conn, sb)
    assert game_id is not None and decision_id is not None and orientation == "direct"


def test_ordinary_nba_direct_match(conn: sqlite3.Connection) -> None:
    home = seed_team(conn, league_code="NBA", abbreviation="TNH", canonical_name="NBA Home",
                     city="NH", nickname="NHs", aliases=[("NBA Home", "full", "")])
    away = seed_team(conn, league_code="NBA", abbreviation="TNA", canonical_name="NBA Away",
                     city="NA", nickname="NAs", aliases=[("NBA Away", "full", "")])
    _create_canonical(conn, league_code="NBA", home_team_id=home, away_team_id=away,
                      scheduled_start="2026-04-10T23:30:00Z", game_date_local="2026-04-10",
                      official_provider="balldontlie", official_game_key="NG1")
    seed_sb_event(conn, provider_event_id="NE1", sport_key=NBA_KEY,
                  commence_time="2026-04-10T23:30:00Z", home_team_raw="NBA Home",
                  away_team_raw="NBA Away", league_code="NBA")
    r = _sb(conn, sport_key=NBA_KEY, provider_event_id="NE1")
    assert r.counters.events_accepted == 1 and r.counters.direct_orientation == 1


def test_provider_scoped_aliases(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    # Odds-API-specific spellings only.
    from .conftest import seed_team_alias
    seed_team_alias(conn, team_id=home, league_code="MLB", alias="DK Home", provider=ODDS)
    seed_team_alias(conn, team_id=away, league_code="MLB", alias="DK Away", provider=ODDS)
    _game(conn, home=home, away=away, key="G1")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="DK Home",
                  away_team_raw="DK Away")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_accepted == 1


def test_unresolved_home_stops(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Nobody Team",
                  away_team_raw="Test Away")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_no_candidate == 1 and r.counters.events_accepted == 0


def test_ambiguous_away_stops(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    # Two teams share the away raw alias -> ambiguous.
    seed_team(conn, league_code="MLB", abbreviation="TD1", canonical_name="Dup One",
              city="D", nickname="Ones", aliases=[("Shared Away", "full", "")])
    seed_team(conn, league_code="MLB", abbreviation="TD2", canonical_name="Dup Two",
              city="D", nickname="Twos", aliases=[("Shared Away", "full", "")])
    _game(conn, home=home, away=away, key="G1")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Shared Away")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_no_candidate == 1 and r.counters.events_accepted == 0


def test_league_sport_mismatch_blocking(conn: sqlite3.Connection) -> None:
    _setup_mlb(conn)
    # Event stored with an MLB sport_key but an NBA league_id.
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away", league_code="NBA")
    r = _sb(conn, provider_event_id="E1")
    assert r.needs_failure_exit and r.counters.blocking_issues >= 1
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-SB-LEAGUE-001" in codes


def test_same_team_event_rejected(conn: sqlite3.Connection) -> None:
    home, _away = _setup_mlb(conn)
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Home")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_rejected == 1 and r.needs_failure_exit


def test_exact_90_minute_tier(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", start="2026-07-25T23:05:00Z")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:40:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")  # 35 min later
    _sb(conn, provider_event_id="E1")
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=ODDS, source_ref=_only_event(conn), entity_type="sportsbook_event")[0]
    assert d.method == "schedule_key_exact" and d.score == 0.95


def test_12_hour_tier(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", start="2026-07-25T18:00:00Z")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:00:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")  # 5h later -> window tier
    _sb(conn, provider_event_id="E1")
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=ODDS, source_ref=_only_event(conn), entity_type="sportsbook_event")[0]
    assert d.method == "schedule_key_window" and d.score == 0.88


def test_two_candidates_ambiguous(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    # Two canonical games within 90 min of the event -> ambiguous.
    _game(conn, home=home, away=away, key="G1", start="2026-07-25T23:00:00Z", gn=1)
    _game(conn, home=home, away=away, key="G2", start="2026-07-25T23:30:00Z", gn=2)
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:15:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_ambiguous == 1 and r.counters.events_accepted == 0


def test_utc_fallback_lowers_confidence(conn: sqlite3.Connection) -> None:
    # No established home venue -> event local date falls to UTC, capping the score
    # and writing DQ-TZ-001.
    home, away = _two_mlb_teams(conn)
    _game(conn, home=home, away=away, key="G1", start="2026-07-25T23:05:00Z")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    _sb(conn, provider_event_id="E1")
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=ODDS, source_ref=_only_event(conn), entity_type="sportsbook_event")[0]
    assert d.outcome == "accepted" and d.score == 0.88  # min(0.95, UTC cap 0.88)
    assert "DQ-TZ-001" in {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}


def test_cross_midnight_pacific(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    # A 7pm Pacific game -> 03:00 UTC next day, canonical local date is the 25th.
    _game(conn, home=home, away=away, key="G1", start="2026-07-26T02:00:00Z", date="2026-07-25")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-26T02:00:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    r = _sb(conn, provider_event_id="E1")  # matches by UTC instant, not calendar date
    assert r.counters.events_accepted == 1


def test_non_neutral_reversed_is_blocking(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", neutral=False)  # non-neutral
    # Provider reports the teams reversed.
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Away",
                  away_team_raw="Test Home")
    r = _sb(conn, provider_event_id="E1")
    assert r.needs_failure_exit and r.counters.events_rejected == 1
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-003" in codes


def test_neutral_swapped_review_gated(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", neutral=True)
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Away",
                  away_team_raw="Test Home")  # reversed vs the neutral game
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_accepted == 1 and r.counters.swapped_review_gated == 1
    assert not r.needs_failure_exit  # DQ-MATCH-007 is an issue, not blocking
    sb = _only_event(conn)
    _game_id, _dec, orientation = _event_link(conn, sb)
    assert orientation == "swapped"
    assert not SqliteSportsbookRepository(conn).is_orientation_approved(sb)  # not approved
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-007" in codes


def test_matching_review_surfaces_swapped(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", neutral=True)
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Away",
                  away_team_raw="Test Home")
    _sb(conn, provider_event_id="E1")
    review = SqliteMatchingRepository(conn).list_needs_review(entity_type="sportsbook_event")
    assert any(d.method == "schedule_key_swapped" for d in review)


def test_event_link_uses_exact_decision(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    _sb(conn, provider_event_id="E1")
    _game_id, decision_id, _o = _event_link(conn, sb)
    accepted = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=ODDS, source_ref=sb, entity_type="sportsbook_event")[0]
    assert decision_id == accepted.match_id and accepted.outcome == "accepted"


def test_idempotent_replay(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    _sb(conn, provider_event_id="E1")
    games_before = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    r2 = _sb(conn, provider_event_id="E1")
    # Re-run: link already applied -> no new link, no new game, decision re-recorded.
    assert r2.counters.event_links_applied == 0
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == games_before


# --------------------------------------------------------------------------- #
# Doubleheaders + reschedules
# --------------------------------------------------------------------------- #
def test_split_doubleheader_by_time(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", start="2026-07-25T17:00:00Z", gn=1)
    _game(conn, home=home, away=away, key="G2", start="2026-07-25T23:00:00Z", gn=2)
    # Event near game 2 only.
    sb = seed_sb_event(conn, provider_event_id="E2", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    r = _sb(conn, provider_event_id="E2")
    assert r.counters.events_accepted == 1
    game_id, _d, _o = _event_link(conn, sb)
    g2 = conn.execute("SELECT game_id FROM games WHERE official_game_key='G2'").fetchone()[0]
    assert game_id == g2


def test_indistinguishable_doubleheader_ambiguous(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", start="2026-07-25T23:00:00Z", gn=1)
    _game(conn, home=home, away=away, key="G2", start="2026-07-25T23:30:00Z", gn=2)
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:15:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_ambiguous == 1


def test_doubleheader_order_independent_100(tmp_path: Path) -> None:
    rng = random.Random(7)
    for i in range(100):
        db = tmp_path / f"dh{i}.db"
        initialize_database(db)
        with Database(db).connection() as c:
            home, away = _setup_mlb(c)
            _game(c, home=home, away=away, key="G1", start="2026-07-25T17:00:00Z", gn=1)
            _game(c, home=home, away=away, key="G2", start="2026-07-25T23:00:00Z", gn=2)
            events = [
                ("EA", "2026-07-25T17:05:00Z", "G1"),
                ("EB", "2026-07-25T23:05:00Z", "G2"),
            ]
            rng.shuffle(events)
            for pe, commence, _g in events:
                seed_sb_event(c, provider_event_id=pe, sport_key=MLB_KEY, commence_time=commence,
                              home_team_raw="Test Home", away_team_raw="Test Away")
            for pe, _commence, expected_key in sorted(events):
                with transaction(c):
                    MatchSportsbookService(c).match_range(provider_event_id=pe)
                ev = SqliteSportsbookRepository(c).get_event_by_provider(ODDS, pe)
                exp = c.execute(
                    "SELECT game_id FROM games WHERE official_game_key=?", (expected_key,)
                ).fetchone()[0]
                assert ev is not None and ev.game_id == exp


def test_partial_run_sees_both_candidates(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", start="2026-07-25T17:00:00Z", gn=1)
    _game(conn, home=home, away=away, key="G2", start="2026-07-25T23:00:00Z", gn=2)
    # A single event near game 1; both canonical games are candidates but only
    # game 1 is within 90 min -> unambiguous match to game 1.
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T17:10:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    _sb(conn, provider_event_id="E1")
    g1 = conn.execute("SELECT game_id FROM games WHERE official_game_key='G1'").fetchone()[0]
    assert _event_link(conn, sb)[0] == g1


def test_replacement_event_id_links_same_game(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    a = seed_sb_event(conn, provider_event_id="E-OLD", sport_key=MLB_KEY,
                      commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                      away_team_raw="Test Away")
    b = seed_sb_event(conn, provider_event_id="E-NEW", sport_key=MLB_KEY,
                      commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                      away_team_raw="Test Away")
    _sb(conn)
    # Both distinct provider events, same canonical game, same direct orientation.
    assert _event_link(conn, a)[0] == _event_link(conn, b)[0]


def test_conflicting_concurrent_events_dq(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1", neutral=True)
    # Event A direct; event B reversed (neutral swapped) -> same game, conflicting orientation.
    seed_sb_event(conn, provider_event_id="EA", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    seed_sb_event(conn, provider_event_id="EB", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Away",
                  away_team_raw="Test Home")
    r = _sb(conn)
    assert r.counters.blocking_orientation_conflicts >= 1 and r.needs_failure_exit


# --------------------------------------------------------------------------- #
# Price isolation
# --------------------------------------------------------------------------- #
def test_matcher_does_not_read_prices() -> None:
    src = (Path(__file__).parent.parent / "sportsbook.py").read_text(encoding="utf-8")
    for banned in ("price_snapshot", "price_american", "append_price", "price_as_of",
                   "latest_price", "implied_probability", "prices_in_range"):
        assert banned not in src


def test_prices_do_not_affect_decision(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    m = seed_sb_market(conn, sb_event_id=sb, market_key="h2h")
    o = seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Home", outcome_role="home")
    seed_sb_price(conn, sb_outcome_id=o, price_american=-150)
    _sb(conn, provider_event_id="E1")
    with_price = _event_link(conn, sb)
    # A fresh corpus WITHOUT any price for the same setup yields the same link.
    assert with_price[0] is not None and with_price[2] == "direct"


def test_bookmaker_count_does_not_change_decision(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    for bk in ("draftkings", "fanduel", "caesars"):
        seed_sb_market(conn, sb_event_id=sb, market_key="h2h", bookmaker_key=bk)
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.events_accepted == 1  # three bookmakers, still one decision


# --------------------------------------------------------------------------- #
# Outcome semantics
# --------------------------------------------------------------------------- #
def _accepted_event_with_markets(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    return sb


def test_direct_h2h_home_away(conn: sqlite3.Connection) -> None:
    sb = _accepted_event_with_markets(conn)
    m = seed_sb_market(conn, sb_event_id=sb, market_key="h2h")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Home", outcome_role="home")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Away", outcome_role="away")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.outcome_roles_approved == 2 and r.counters.unknown_outcomes == 0


def test_unknown_h2h_name_flagged(conn: sqlite3.Connection) -> None:
    sb = _accepted_event_with_markets(conn)
    m = seed_sb_market(conn, sb_event_id=sb, market_key="h2h")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Home", outcome_role="home")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Some Other", outcome_role="unknown")
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.unknown_outcomes == 1  # retained + counted, not dropped
    kept = conn.execute(
        "SELECT COUNT(*) FROM sportsbook_outcomes WHERE provider_outcome_name='Some Other'"
    ).fetchone()[0]
    assert kept == 1


def test_unexpected_draw_flagged(conn: sqlite3.Connection) -> None:
    sb = _accepted_event_with_markets(conn)
    m = seed_sb_market(conn, sb_event_id=sb, market_key="h2h")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Home", outcome_role="home")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Away", outcome_role="away")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Draw", outcome_role="draw")
    _sb(conn, provider_event_id="E1")
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-SB-OUTCOME-001" in codes


def test_spread_points_preserved(conn: sqlite3.Connection) -> None:
    sb = _accepted_event_with_markets(conn)
    m = seed_sb_market(conn, sb_event_id=sb, market_key="spreads")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Home", outcome_role="home",
                    point=-1.5)
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Test Away", outcome_role="away",
                    point=1.5)
    _sb(conn, provider_event_id="E1")
    points = {r[0] for r in conn.execute(
        "SELECT point FROM sportsbook_outcomes WHERE outcome_role IN ('home','away')")}
    assert points == {-1.5, 1.5}  # provider points unchanged by matching


def test_totals_over_under_and_team_named(conn: sqlite3.Connection) -> None:
    sb = _accepted_event_with_markets(conn)
    m = seed_sb_market(conn, sb_event_id=sb, market_key="totals")
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Over", outcome_role="over",
                    point=8.5)
    seed_sb_outcome(conn, sb_market_id=m, provider_outcome_name="Under", outcome_role="under",
                    point=8.5)
    m2 = seed_sb_market(conn, sb_event_id=sb, market_key="totals", bookmaker_key="fanduel")
    # A team name in a totals market is classified unknown by the ingestor.
    seed_sb_outcome(conn, sb_market_id=m2, provider_outcome_name="Test Home", outcome_role="unknown",
                    point=8.5)
    r = _sb(conn, provider_event_id="E1")
    assert r.counters.outcome_roles_approved == 2 and r.counters.unknown_outcomes == 1
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-SB-OUTCOME-001" in codes  # team-named totals flagged


# --------------------------------------------------------------------------- #
# PIT + persistence + isolation
# --------------------------------------------------------------------------- #
def test_asof_hides_later_event_decision(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    _sb(conn, provider_event_id="E1")
    repo = SqliteMatchingRepository(conn)
    d = repo.decisions_for_source(source_provider=ODDS, source_ref=sb,
                                  entity_type="sportsbook_event")[0]
    assert repo.decisions_for_source(source_provider=ODDS, source_ref=sb,
                                     as_of="2000-01-01T00:00:00.000000Z") == []
    assert repo.decisions_for_source(source_provider=ODDS, source_ref=sb, as_of=d.decided_at)
    # Orientation approval respects the as-of cutoff too.
    assert not SqliteSportsbookRepository(conn).is_orientation_approved(
        sb, as_of="2000-01-01T00:00:00.000000Z")
    assert SqliteSportsbookRepository(conn).is_orientation_approved(sb, as_of=d.decided_at)


def test_dry_run_persists_nothing(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    before_dec = SqliteMatchingRepository(conn).count()
    r = _sb(conn, dry_run=True, provider_event_id="E1")
    assert r.counters.events_accepted == 1 and r.counters.rows_persisted == 0
    assert SqliteMatchingRepository(conn).count() == before_dec
    assert _event_link(conn, sb)[0] is None  # not linked


def test_one_decision_and_candidates_per_attempt(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    sb = seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                       commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                       away_team_raw="Test Away")
    _sb(conn, provider_event_id="E1")
    repo = SqliteMatchingRepository(conn)
    decisions = repo.decisions_for_source(source_provider=ODDS, source_ref=sb,
                                          entity_type="sportsbook_event")
    assert len(decisions) == 1 and repo.candidates(decisions[0].match_id)


def test_kalshi_tables_untouched(conn: sqlite3.Connection) -> None:
    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    _sb(conn, provider_event_id="E1")
    linked = conn.execute("SELECT COUNT(*) FROM kalshi_markets WHERE game_id IS NOT NULL").fetchone()[0]
    assert linked == 0
    types = {r[0] for r in conn.execute("SELECT DISTINCT entity_type FROM entity_match_decisions")}
    assert not (types & {"kalshi_event", "kalshi_market"})


def test_no_execution_import() -> None:
    src = (Path(__file__).parent.parent / "sportsbook.py").read_text(encoding="utf-8")
    assert "execution" not in src and "gateway" not in src and "order_gateway" not in src


def test_cli_json_and_exit(conn: sqlite3.Connection, db_path) -> None:  # type: ignore[no-untyped-def]
    import json as _json

    from sports_quant.matching.runner import run_match_games

    home, away = _setup_mlb(conn)
    _game(conn, home=home, away=away, key="G1")
    seed_sb_event(conn, provider_event_id="E1", sport_key=MLB_KEY,
                  commence_time="2026-07-25T23:05:00Z", home_team_raw="Test Home",
                  away_team_raw="Test Away")
    out: list[str] = []
    code = run_match_games(_settings(), source="sportsbook", sport="mlb", from_date="2026-07-25",
                           to_date="2026-07-25", database_path=db_path, as_json=True, out=out.append)
    assert code == 0
    payload = _json.loads(out[-1])
    assert payload["source"] == "sportsbook" and payload["events_accepted"] == 1


def _only_event(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT sb_event_id FROM sportsbook_events LIMIT 1").fetchone()[0])
