"""Phase D5B2: deterministic Kalshi event + game-winner market matching.

Isolated temporary corpora only; no provider client, no network. Ticker/rules
forms match the VERIFIED current public Kalshi contract (see ``kalshi_fixtures``
and ``ENTITY_MATCHING.md`` §6): MLB ``KXMLBGAME-{YYMONDD}{HHMM}{AWAY}{HOME}`` with
a venue-local clock and ``A vs B`` titles; NBA ``KXNBAGAME-{YYMONDD}{AWAY}{HOME}``
date-only with ``[Game N: ]A at B`` titles. Strings labelled SYNTHETIC are unit
edge cases only, never a provider-contract claim.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sports_quant.db.engine import transaction
from sports_quant.db.repositories.kalshi import SqliteKalshiRepository
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.matching import kalshi_parse as kp
from sports_quant.matching.kalshi import MatchKalshiService, _local_clock_to_utc

from . import kalshi_fixtures as fx
from .conftest import (
    seed_kalshi_event,
    seed_kalshi_market,
    seed_team,
    seed_venue,
    set_kalshi_market_rules,
)
from .test_phase_d5a_matching import _create_canonical

KALSHI = "kalshi_public"
MLB_SERIES = "KXMLBGAME"
NBA_SERIES = "KXNBAGAME"

# Real-contract MLB: San Diego (away) at Los Angeles Dodgers (home), 19:10 PT.
_MLB_EVENT = "KXMLBGAME-25JUL041910SDLAD"
_MLB_MARKET = "KXMLBGAME-25JUL041910SDLAD-LAD"
_MLB_TITLE = "San Diego Padres vs Los Angeles Dodgers"
_MLB_RULES = ("If Los Angeles Dodgers wins the San Diego Padres vs Los Angeles Dodgers "
              "professional baseball game originally scheduled for Jul 4, 2025 at 7:10 PM PDT, "
              "then the market resolves to Yes.")


def _kal(conn: sqlite3.Connection, *, dry_run: bool = False, **kw):  # type: ignore[no-untyped-def]
    svc = MatchKalshiService(conn, dry_run=dry_run)
    if dry_run:
        return svc.match_range(**kw)
    with transaction(conn):
        return svc.match_range(**kw)


def _ticker(conn: sqlite3.Connection, kmk: str) -> str:
    m = SqliteKalshiRepository(conn).get_market(kmk)
    assert m is not None  # noqa: S101
    return m.market_ticker


def _team(conn: sqlite3.Connection, *, league: str, abbr: str, name: str, code: str) -> str:
    return seed_team(conn, league_code=league, abbreviation=abbr, canonical_name=name,
                     city=name, nickname=abbr,
                     aliases=[(code, "provider", KALSHI), (name, "full", KALSHI)])


def _mlb_teams(conn: sqlite3.Connection) -> tuple[str, str]:
    dodgers = _team(conn, league="MLB", abbr="LAD", name="Los Angeles Dodgers", code="LAD")
    padres = _team(conn, league="MLB", abbr="SD", name="San Diego Padres", code="SD")
    return dodgers, padres


def _dodgers_home_setup(conn: sqlite3.Connection) -> tuple[str, str, str]:
    """Dodgers (home) vs Padres (away), 2025-07-04 19:10 PT at Dodger Stadium
    (America/Los_Angeles). Returns (dodgers, padres, game_id)."""

    dodgers, padres = _mlb_teams(conn)
    venue = seed_venue(conn, name="Dodger Stadium", provider="mlb_statsapi",
                       provider_venue_id="LAV", timezone="America/Los_Angeles")
    gid = _create_canonical(
        conn, league_code="MLB", home_team_id=dodgers, away_team_id=padres,
        scheduled_start="2025-07-05T02:10:00Z", game_date_local="2025-07-04",
        official_provider="mlb_statsapi", official_game_key="G1",
        decided_at="2025-07-01T00:00:00.000000Z")
    conn.execute("UPDATE games SET venue = ? WHERE game_id = ?", (venue, gid))
    conn.commit()
    return dodgers, padres, gid


def _seed_event_and_market(conn: sqlite3.Connection, *, event_ticker: str = _MLB_EVENT,
                           market_ticker: str = _MLB_MARKET, title: str = _MLB_TITLE,
                           yes_sub: str = "Los Angeles Dodgers",
                           no_sub: str = "Los Angeles Dodgers",
                           rules: str = _MLB_RULES) -> tuple[str, str]:
    kev = seed_kalshi_event(conn, event_ticker=event_ticker, series_ticker=MLB_SERIES,
                            title=title)
    kmk = seed_kalshi_market(conn, market_ticker=market_ticker, event_ticker=event_ticker,
                             series_ticker=MLB_SERIES, kalshi_event_id=kev, title=title,
                             yes_sub_title=yes_sub, no_sub_title=no_sub, rules_primary=rules,
                             close_time="2025-07-07T02:00:00Z")
    return kev, kmk


# --------------------------------------------------------------------------- #
# Parser unit tests (§11 1-8, 13-23)
# --------------------------------------------------------------------------- #
_MLB_CODES = frozenset({"LAD", "SD", "AZ", "PIT", "BAL", "DET", "NYY"})
_NBA_CODES = frozenset({"NYK", "SAS", "LAL", "BOS"})


def test_mlb_event_ticker_with_clock() -> None:
    e = kp.parse_event_ticker("KXMLBGAME-26JUL271840AZPIT", _MLB_CODES)
    assert isinstance(e, kp.ParsedEventTicker)
    assert e.series.parser_version == "kmlb-2" and e.game_date_local == "2026-07-27"
    assert e.local_clock == "18:40" and e.away_code == "AZ" and e.home_code == "PIT"


def test_mlb_ticker_malformed_hour_and_minute() -> None:
    assert isinstance(kp.parse_event_ticker("KXMLBGAME-26JUL272540AZPIT", _MLB_CODES), kp.ParseError)
    assert isinstance(kp.parse_event_ticker("KXMLBGAME-26JUL271870AZPIT", _MLB_CODES), kp.ParseError)


def test_mlb_ticker_missing_clock_rejected() -> None:
    # Date-only MLB (the old synthetic shape) must FAIL the current MLB parser.
    assert isinstance(kp.parse_event_ticker("KXMLBGAME-26JUL27AZPIT", _MLB_CODES), kp.ParseError)


def test_mlb_ambiguous_team_split_rejected() -> None:
    # 'AZPIT' with only {AZ,PIT,A,ZP...} -> here A/ZPIT invalid; but two valid splits reject.
    codes = frozenset({"AZ", "PIT", "AZP", "IT"})
    assert kp.split_team_codes("AZPIT", codes) is None  # AZ+PIT and AZP+IT both valid


def test_nba_date_only_ticker() -> None:
    n = kp.parse_event_ticker("KXNBAGAME-26JUN13NYKSAS", _NBA_CODES)
    assert isinstance(n, kp.ParsedEventTicker)
    assert n.series.parser_version == "knba-1" and n.local_clock is None
    assert n.away_code == "NYK" and n.home_code == "SAS"


def test_nba_unexpected_time_segment_rejected() -> None:
    assert isinstance(kp.parse_event_ticker("KXNBAGAME-26JUN131840NYKSAS", _NBA_CODES),
                      kp.ParseError)


def test_market_ticker_ancestry_and_subject() -> None:
    e = kp.parse_event_ticker("KXMLBGAME-26JUL271840AZPIT", _MLB_CODES)
    assert isinstance(e, kp.ParsedEventTicker)
    ok = kp.parse_market_ticker("KXMLBGAME-26JUL271840AZPIT-AZ", e)
    assert isinstance(ok, kp.ParsedMarketTicker) and ok.yes_code == "AZ"
    assert isinstance(kp.parse_market_ticker("KXMLBGAME-26JUL271840BALDET-BAL", e), kp.ParseError)


def test_title_vs_and_at_and_game_prefix() -> None:
    vs = kp.parse_title_teams("Arizona vs Pittsburgh")
    assert isinstance(vs, kp.TitleTeams) and vs.away_name is None
    at = kp.parse_title_teams("Game 5: New York at San Antonio")
    assert isinstance(at, kp.TitleTeams) and at.away_name == "New York" and at.home_name == "San Antonio"
    win = kp.parse_title_teams("Arizona vs Pittsburgh Winner?")  # market title suffix stripped
    assert isinstance(win, kp.TitleTeams) and set(win.names) == {"Arizona", "Pittsburgh"}
    assert isinstance(kp.parse_title_teams("Arizona game"), kp.ParseError)


def test_rules_current_mlb_and_nba() -> None:
    m = kp.parse_rules_yes_subject(fx.MLB_AZ_PIT.rules_primary)
    assert isinstance(m, kp.ParsedRules)
    assert m.yes_name == "Arizona" and set(m.names) == {"Arizona", "Pittsburgh"}
    assert m.scheduled_date == "2026-07-27" and m.local_clock == "18:40" and m.tz_abbrev == "EDT"
    n = kp.parse_rules_yes_subject(fx.NBA_NYK_SAS.rules_primary)
    assert isinstance(n, kp.ParsedRules)
    assert n.yes_name == "San Antonio" and n.away_name == "New York" and n.home_name == "San Antonio"
    assert n.scheduled_date == "2026-06-13" and n.local_clock is None


def test_rules_without_the_before_yes_and_game_prefix() -> None:
    # Current wording has no 'the' before the Yes team and an optional 'Game N:'.
    r = kp.parse_rules_yes_subject(
        "If Boston wins the Game 2: Philadelphia at Boston professional basketball game "
        "originally scheduled for May 6, 2026, then the market resolves to Yes.")
    assert isinstance(r, kp.ParsedRules) and r.yes_name == "Boston"
    assert r.away_name == "Philadelphia" and r.home_name == "Boston"


def test_rules_natural_language_clock_conversions() -> None:
    for text, want in (("12:00 AM", "00:00"), ("12:00 PM", "12:00"), ("1:05 PM", "13:05")):
        r = kp.parse_rules_yes_subject(
            f"If Arizona wins the Arizona vs Pittsburgh professional baseball game originally "
            f"scheduled for Jul 27, 2026 at {text} EDT, then the market resolves to Yes.")
        assert isinstance(r, kp.ParsedRules) and r.local_clock == want


# --------------------------------------------------------------------------- #
# Timezone conversion (§11 9-12)
# --------------------------------------------------------------------------- #
def test_local_clock_to_utc_venue_conversion() -> None:
    # 19:10 America/Los_Angeles on 2025-07-04 (PDT) -> 02:10 UTC next day.
    utc, err = _local_clock_to_utc("2025-07-04", "19:10", "America/Los_Angeles", "PDT")
    assert err is None and utc is not None
    assert utc.strftime("%Y-%m-%dT%H:%M:%SZ") == "2025-07-05T02:10:00Z"


def test_local_clock_never_utc() -> None:
    # Same wall clock in ET is a different instant than in PT -> proves not UTC.
    et, _ = _local_clock_to_utc("2026-07-27", "18:40", "America/New_York", "EDT")
    pt, _ = _local_clock_to_utc("2026-07-27", "18:40", "America/Los_Angeles", "PDT")
    assert et is not None and pt is not None and et != pt


def test_tz_abbreviation_mismatch_rejected() -> None:
    # EDT claimed but the venue is Pacific -> mismatch rejected.
    _utc, err = _local_clock_to_utc("2026-07-27", "18:40", "America/Los_Angeles", "EDT")
    assert err is not None


# --------------------------------------------------------------------------- #
# End-to-end matching on the real contract
# --------------------------------------------------------------------------- #
def test_mlb_clean_match_tier1_ticker_time(conn: sqlite3.Connection) -> None:
    dodgers, _p, gid = _dodgers_home_setup(conn)
    kev, kmk = _seed_event_and_market(conn)
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.events_accepted == 1 and r.counters.markets_accepted == 1
    repo = SqliteKalshiRepository(conn)
    assert repo.event_link(kev)[0] == gid
    g, _dec, yes, mhash, sem = repo.market_link(kmk)
    assert g == gid and yes == dodgers and sem == "game_winner" and mhash is not None
    assert repo.is_kalshi_market_orientation_approved(kmk)
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kev, entity_type="kalshi_event")[0]
    assert d.method == "kalshi_ticker_time" and d.score == 0.97  # venue-local clock -> Tier 1


def test_mlb_no_venue_falls_back_to_date_only(conn: sqlite3.Connection) -> None:
    # Same MLB market but the canonical game has no venue tz -> no Tier-1 time,
    # conservative date-only Tier 2.
    dodgers, padres = _mlb_teams(conn)
    gid = _create_canonical(
        conn, league_code="MLB", home_team_id=dodgers, away_team_id=padres,
        scheduled_start="2025-07-05T02:10:00Z", game_date_local="2025-07-04",
        official_provider="mlb_statsapi", official_game_key="G1",
        decided_at="2025-07-01T00:00:00.000000Z")
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kmk, entity_type="kalshi_market")
    # No venue tz -> no exact time; conservative date-only Tier 2 (0.92).
    assert any(x.method == "kalshi_date" and x.matched_entity_id == gid for x in d)


def test_nba_date_only_end_to_end(conn: sqlite3.Connection) -> None:
    nyk = _team(conn, league="NBA", abbr="NYK", name="New York", code="NYK")
    sas = _team(conn, league="NBA", abbr="SAS", name="San Antonio", code="SAS")
    gid = _create_canonical(
        conn, league_code="NBA", home_team_id=sas, away_team_id=nyk,
        scheduled_start="2026-06-14T00:00:00Z", game_date_local="2026-06-13",
        official_provider="balldontlie", official_game_key="NG1",
        decided_at="2026-06-01T00:00:00.000000Z")
    kev = seed_kalshi_event(conn, event_ticker=fx.NBA_NYK_SAS.event_ticker,
                            series_ticker=NBA_SERIES, title=fx.NBA_NYK_SAS.title)
    seed_kalshi_market(conn, market_ticker=fx.NBA_NYK_SAS.market_ticker,
                       event_ticker=fx.NBA_NYK_SAS.event_ticker, series_ticker=NBA_SERIES,
                       kalshi_event_id=kev, title=fx.NBA_NYK_SAS.title,
                       yes_sub_title=fx.NBA_NYK_SAS.yes_sub_title,
                       no_sub_title=fx.NBA_NYK_SAS.no_sub_title,
                       rules_primary=fx.NBA_NYK_SAS.rules_primary)
    r = _kal(conn, series_ticker=NBA_SERIES)
    assert r.counters.events_accepted == 1 and r.counters.markets_accepted == 1
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kev, entity_type="kalshi_event")[0]
    assert d.method == "kalshi_date" and d.score == 0.92 and d.matched_entity_id == gid
    repo = SqliteKalshiRepository(conn)
    m = repo.get_market_by_ticker(fx.NBA_NYK_SAS.market_ticker)
    assert m is not None
    assert repo.market_link(m.kalshi_market_id)[2] == sas  # Yes = San Antonio (home)


def test_audited_mlb_fixture_end_to_end(conn: sqlite3.Connection) -> None:
    az = _team(conn, league="MLB", abbr="AZ", name="Arizona", code="AZ")
    pit = _team(conn, league="MLB", abbr="PIT", name="Pittsburgh", code="PIT")
    venue = seed_venue(conn, name="PNC Park", provider="mlb_statsapi", provider_venue_id="PNC",
                       timezone="America/New_York")
    gid = _create_canonical(
        conn, league_code="MLB", home_team_id=pit, away_team_id=az,
        scheduled_start="2026-07-27T22:40:00Z", game_date_local="2026-07-27",
        official_provider="mlb_statsapi", official_game_key="AZPIT",
        decided_at="2026-07-01T00:00:00.000000Z")
    conn.execute("UPDATE games SET venue=? WHERE game_id=?", (venue, gid))
    conn.commit()
    kev = seed_kalshi_event(conn, event_ticker=fx.MLB_AZ_PIT.event_ticker,
                            series_ticker=MLB_SERIES, title=fx.MLB_AZ_PIT.title)
    seed_kalshi_market(conn, market_ticker=fx.MLB_AZ_PIT.market_ticker,
                       event_ticker=fx.MLB_AZ_PIT.event_ticker, series_ticker=MLB_SERIES,
                       kalshi_event_id=kev, title=fx.MLB_AZ_PIT.title,
                       yes_sub_title=fx.MLB_AZ_PIT.yes_sub_title,
                       no_sub_title=fx.MLB_AZ_PIT.no_sub_title,
                       rules_primary=fx.MLB_AZ_PIT.rules_primary)
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.events_accepted == 1 and r.counters.markets_accepted == 1
    assert SqliteKalshiRepository(conn).event_link(kev)[0] == gid
    _ = (az, pit)


# --------------------------------------------------------------------------- #
# Title / rules disagreement, no_sub_title, unsupported semantics
# --------------------------------------------------------------------------- #
def test_reversed_at_title_rejected(conn: sqlite3.Connection) -> None:
    # NBA 'at' orientation reversed vs the ticker (ticker away=NYK, home=SAS).
    nyk = _team(conn, league="NBA", abbr="NYK", name="New York", code="NYK")
    sas = _team(conn, league="NBA", abbr="SAS", name="San Antonio", code="SAS")
    _create_canonical(conn, league_code="NBA", home_team_id=sas, away_team_id=nyk,
                      scheduled_start="2026-06-14T00:00:00Z", game_date_local="2026-06-13",
                      official_provider="balldontlie", official_game_key="NG1",
                      decided_at="2026-06-01T00:00:00.000000Z")
    seed_kalshi_event(conn, event_ticker="KXNBAGAME-26JUN13NYKSAS", series_ticker=NBA_SERIES,
                      title="San Antonio at New York")  # reversed: home named as away
    r = _kal(conn, series_ticker=NBA_SERIES)
    assert r.counters.events_accepted == 0 and r.counters.events_rejected >= 1
    _ = (nyk, sas)


def test_no_sub_title_equal_yes_is_accepted(conn: sqlite3.Connection) -> None:
    # Current public Kalshi sets no_sub_title == the Yes-subject team; must accept.
    _dodgers_home_setup(conn)
    _seed_event_and_market(conn, no_sub="Los Angeles Dodgers")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 1


def test_no_sub_title_unrelated_rejected(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _team(conn, league="MLB", abbr="NYY", name="New York Yankees", code="NYY")
    _seed_event_and_market(conn, no_sub="New York Yankees")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1


def test_rules_ticker_time_disagreement_rejected(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    bad_rules = _MLB_RULES.replace("7:10 PM PDT", "9:10 PM PDT")  # rules clock != ticker 19:10
    _seed_event_and_market(conn, rules=bad_rules)
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1


def test_rules_team_disagreement_rejected(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    bad = ("If Los Angeles Dodgers wins the New York Yankees vs Los Angeles Dodgers professional "
           "baseball game originally scheduled for Jul 4, 2025 at 7:10 PM PDT, then the market "
           "resolves to Yes.")
    _team(conn, league="MLB", abbr="NYY", name="New York Yankees", code="NYY")
    _seed_event_and_market(conn, rules=bad)
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1


def test_unsupported_semantic_not_malformed(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    kev = seed_kalshi_event(conn, event_ticker=_MLB_EVENT, series_ticker=MLB_SERIES,
                            title=_MLB_TITLE)
    seed_kalshi_market(conn, market_ticker=f"{_MLB_EVENT}-T85", event_ticker=_MLB_EVENT,
                       series_ticker=MLB_SERIES, kalshi_event_id=kev, title=_MLB_TITLE,
                       rules_primary="A totals proposition.")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.unsupported_semantics == 1 and r.counters.markets_accepted == 0


def test_indistinguishable_doubleheader_ambiguous(conn: sqlite3.Connection) -> None:
    # Two same-slate Dodgers/Padres games; date-only NBA-style ambiguity for MLB
    # when the ticker time cannot single one out (both share the venue tz slate).
    dodgers, padres = _mlb_teams(conn)
    for key, gn in (("G1", 1), ("G2", 2)):
        _create_canonical(conn, league_code="MLB", home_team_id=dodgers, away_team_id=padres,
                          scheduled_start="2025-07-05T02:10:00Z", game_date_local="2025-07-04",
                          official_provider="mlb_statsapi", official_game_key=key, game_number=gn,
                          decided_at="2025-07-01T00:00:00.000000Z")
    seed_kalshi_event(conn, event_ticker=_MLB_EVENT, series_ticker=MLB_SERIES, title=_MLB_TITLE)
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.events_ambiguous == 1 and r.counters.events_accepted == 0


# --------------------------------------------------------------------------- #
# Atomicity / replay / rules-hash / PIT / isolation / dry-run (preserved)
# --------------------------------------------------------------------------- #
def test_direct_service_market_link_failure_rolls_back(conn: sqlite3.Connection) -> None:
    import pytest

    from sports_quant.db.repositories.references import LinkOutcome
    from sports_quant.matching.linkatomic import MatchLinkError
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    svc = MatchKalshiService(conn)
    svc._kal.link_market_game = lambda **_kw: LinkOutcome.CONFLICT  # type: ignore[assignment,method-assign]  # noqa: E501
    with pytest.raises(MatchLinkError):
        svc.match_range(series_ticker=MLB_SERIES)  # not wrapped in transaction()
    accepted = [d for d in SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kmk, entity_type="kalshi_market")
        if d.outcome == "accepted"]
    assert accepted == []
    assert SqliteKalshiRepository(conn).market_link(kmk) == (None, None, None, None, None)


def test_replay_is_stable(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    before = SqliteMatchingRepository(conn).count()
    r2 = _kal(conn, series_ticker=MLB_SERIES)
    assert r2.counters.events_already_linked == 1 and r2.counters.markets_already_linked == 1
    assert r2.counters.events_accepted == 0 and r2.counters.markets_accepted == 0
    assert SqliteMatchingRepository(conn).count() == before


def test_rules_hash_change_invalidates(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    repo = SqliteKalshiRepository(conn)
    assert repo.is_kalshi_market_orientation_approved(kmk)
    matched_before = repo.market_link(kmk)[3]
    # A benign trailing change keeps the settlement template parseable (same
    # orientation) but changes rules_hash, so orientation must be invalidated.
    set_kalshi_market_rules(
        conn, market_ticker=_ticker(conn, kmk),
        rules_primary=_MLB_RULES.replace("then the market resolves to Yes.",
                                         "then the market resolves to Yes (rev 2)."),
        observed_at="2026-08-01T00:00:00.000000Z")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.rules_hash_conflicts >= 1 and r.needs_failure_exit
    assert not repo.is_kalshi_market_orientation_approved(kmk)
    assert repo.market_link(kmk)[3] == matched_before  # matched hash unchanged
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kmk, entity_type="kalshi_market")[0]
    flagged = SqliteMatchingRepository(conn).get(d.match_id)
    assert flagged is not None and flagged.needs_manual_review  # flagged for review
    assert flagged.reviewed_by is None and flagged.reviewed_at is None  # no human reviewer


def test_pit_as_of(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    repo = SqliteKalshiRepository(conn)
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kmk, entity_type="kalshi_market")[0]
    assert not repo.is_kalshi_market_orientation_approved(kmk, as_of="2000-01-01T00:00:00.000000Z")
    assert repo.is_kalshi_market_orientation_approved(kmk, as_of=d.decided_at)
    m = repo.get_market(kmk)
    assert m is not None and d.raw_response_id == m.current_raw_response_id


def _seed_book_and_trade(conn: sqlite3.Connection, market_ticker: str) -> None:
    from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
    from sports_quant.db.repositories.kalshi import (
        SqliteKalshiRepository,
        orderbook_content_hash,
        trade_content_hash,
    )

    from .conftest import raw_response
    rid, rhash = raw_response(conn, marker=f"book:{market_ticker}")
    run = SqliteIngestionRunRepository(conn).start(
        command="seed", provider="kalshi_public", operation="seed", args_json="{}",
        started_monotonic_ns=0, tool_version="t")
    repo = SqliteKalshiRepository(conn)
    with transaction(conn):
        repo.append_orderbook_snapshot(
            market_ticker=market_ticker, yes_bids=[(55, 10)], no_bids=[(44, 8)],
            observed_at="2025-07-04T12:00:00.000000Z", run_id=run.run_id, raw_response_id=rid,
            raw_response_hash=rhash,
            content_hash=orderbook_content_hash(yes_bids=[(55, 10)], no_bids=[(44, 8)]))
        repo.append_trade(
            market_ticker=market_ticker, count=3, observed_at="2025-07-04T12:00:00.000000Z",
            run_id=run.run_id, raw_response_id=rid, yes_price=55, taker_side="yes",
            content_hash=trade_content_hash(provider_trade_id="t1", market_ticker=market_ticker,
                                            trade_time=None, yes_price=55, no_price=None, count=3,
                                            taker_side="yes"))


def test_sql_trace_and_result_isolation(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn, no_sub="Los Angeles Dodgers")
    # A misleading settled result must not affect matching.
    conn.execute("UPDATE kalshi_markets SET result='no' WHERE kalshi_market_id=?", (kmk,))
    conn.commit()
    _seed_book_and_trade(conn, _ticker(conn, kmk))
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        with transaction(conn):
            MatchKalshiService(conn).match_range(series_ticker=MLB_SERIES)
    finally:
        conn.set_trace_callback(None)
    for banned in ("kalshi_orderbook_snapshots", "kalshi_orderbook_levels", "kalshi_public_trades"):
        assert not any(banned in s for s in statements), banned
    assert SqliteKalshiRepository(conn).market_link(kmk)[0] is not None  # matched despite 'no'


def test_dry_run_persists_nothing(conn: sqlite3.Connection) -> None:
    import hashlib
    _dodgers_home_setup(conn)
    _seed_event_and_market(conn)

    def _dump() -> str:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()

    before = _dump()
    r = _kal(conn, dry_run=True, series_ticker=MLB_SERIES)
    assert r.counters.events_accepted == 1 and r.counters.markets_accepted == 1
    assert r.counters.rows_persisted == 0 and _dump() == before


def test_no_execution_import() -> None:
    src = (Path(__file__).parent.parent / "kalshi.py").read_text(encoding="utf-8")
    assert "execution" not in src and "gateway" not in src


def test_cli_json_and_exit(conn: sqlite3.Connection, db_path) -> None:  # type: ignore[no-untyped-def]
    import json as _json

    from sports_quant.matching.runner import run_match_markets

    from .test_phase_d5a_matching import _settings
    _dodgers_home_setup(conn)
    _seed_event_and_market(conn)
    out: list[str] = []
    code = run_match_markets(_settings(), series_ticker=MLB_SERIES, database_path=db_path,
                             as_json=True, out=out.append)
    assert code == 0
    payload = _json.loads(out[-1])
    assert payload["provider"] == "kalshi_public" and payload["markets_accepted"] == 1
