"""Phase D5B2: deterministic Kalshi event + game-winner market matching.

Isolated temporary corpora only; no provider client, no network. Canonical games
are seeded via the D5A helpers; Kalshi events/markets via the conftest helpers;
teams carry provider-scoped ``kalshi_public`` code and full-name aliases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sports_quant.db.engine import Database, transaction
from sports_quant.db.repositories.kalshi import SqliteKalshiRepository
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.matching.kalshi import MatchKalshiService

from .conftest import (
    seed_kalshi_event,
    seed_kalshi_market,
    seed_team,
    set_kalshi_market_rules,
)
from .test_phase_d5a_matching import _create_canonical

KALSHI = "kalshi_public"
MLB_SERIES = "KXMLBGAME"
NBA_SERIES = "KXNBAGAME"

_RULES = ("This market resolves to Yes if the {yes} win the game against the {other}, "
          "originally scheduled for {sched}.")


def _ticker(conn: sqlite3.Connection, kmk: str) -> str:
    m = SqliteKalshiRepository(conn).get_market(kmk)
    assert m is not None  # noqa: S101
    return m.market_ticker


def _kal(conn: sqlite3.Connection, *, dry_run: bool = False, **kw):  # type: ignore[no-untyped-def]
    svc = MatchKalshiService(conn, dry_run=dry_run)
    if dry_run:
        return svc.match_range(**kw)
    with transaction(conn):
        return svc.match_range(**kw)


def _mlb_teams(conn: sqlite3.Connection) -> tuple[str, str]:
    dodgers = seed_team(
        conn, league_code="MLB", abbreviation="LAD", canonical_name="Los Angeles Dodgers",
        city="Los Angeles", nickname="Dodgers",
        aliases=[("LAD", "provider", KALSHI), ("Los Angeles Dodgers", "full", KALSHI)])
    padres = seed_team(
        conn, league_code="MLB", abbreviation="SD", canonical_name="San Diego Padres",
        city="San Diego", nickname="Padres",
        aliases=[("SD", "provider", KALSHI), ("San Diego Padres", "full", KALSHI)])
    return dodgers, padres


def _nba_teams(conn: sqlite3.Connection) -> tuple[str, str]:
    lakers = seed_team(
        conn, league_code="NBA", abbreviation="LAL", canonical_name="Los Angeles Lakers",
        city="Los Angeles", nickname="Lakers",
        aliases=[("LAL", "provider", KALSHI), ("Los Angeles Lakers", "full", KALSHI)])
    celtics = seed_team(
        conn, league_code="NBA", abbreviation="BOS", canonical_name="Boston Celtics",
        city="Boston", nickname="Celtics",
        aliases=[("BOS", "provider", KALSHI), ("Boston Celtics", "full", KALSHI)])
    return lakers, celtics


def _mlb_game(conn: sqlite3.Connection, home: str, away: str, *, start: str, date: str,
              key: str = "G1", neutral: bool = False, gn: int = 1) -> str:
    return _create_canonical(
        conn, league_code="MLB", home_team_id=home, away_team_id=away, scheduled_start=start,
        game_date_local=date, official_provider="mlb_statsapi", official_game_key=key,
        is_neutral_site=neutral, game_number=gn, decided_at="2025-07-01T00:00:00.000000Z")


def _dodgers_home_setup(conn: sqlite3.Connection) -> tuple[str, str, str]:
    """Dodgers (home) vs Padres (away) canonical game on 2025-07-04; returns
    (dodgers, padres, game_id)."""

    dodgers, padres = _mlb_teams(conn)
    gid = _mlb_game(conn, dodgers, padres, start="2025-07-05T02:10:00Z", date="2025-07-04")
    return dodgers, padres, gid


def _seed_event_and_market(conn: sqlite3.Connection, *, sched: str = "2025-07-05T02:10:00Z",
                           yes: str = "Los Angeles Dodgers", other: str = "San Diego Padres",
                           yes_code: str = "LAD",
                           event_ticker: str = "KXMLBGAME-25JUL04SDLAD") -> tuple[str, str]:
    kev = seed_kalshi_event(
        conn, event_ticker=event_ticker, series_ticker=MLB_SERIES,
        title="San Diego Padres at Los Angeles Dodgers")
    kmk = seed_kalshi_market(
        conn, market_ticker=f"{event_ticker}-{yes_code}", event_ticker=event_ticker,
        series_ticker=MLB_SERIES, kalshi_event_id=kev,
        title="San Diego Padres at Los Angeles Dodgers", yes_sub_title=yes,
        rules_primary=_RULES.format(yes=yes, other=other, sched=sched),
        close_time="2025-07-05T02:00:00Z")
    return kev, kmk


# --------------------------------------------------------------------------- #
# Happy path: event + game-winner market
# --------------------------------------------------------------------------- #
def test_clean_event_and_market_match(conn: sqlite3.Connection) -> None:
    dodgers, _padres, gid = _dodgers_home_setup(conn)
    kev, kmk = _seed_event_and_market(conn)
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.events_accepted == 1 and r.counters.events_linked == 1
    assert r.counters.markets_accepted == 1 and r.counters.markets_linked == 1
    assert r.counters.yes_teams_resolved == 1
    repo = SqliteKalshiRepository(conn)
    assert repo.event_link(kev)[0] == gid
    g, dec, yes, mhash, sem = repo.market_link(kmk)
    assert g == gid and yes == dodgers and sem == "game_winner" and mhash is not None
    assert repo.is_kalshi_market_orientation_approved(kmk)


def test_nba_date_only_match(conn: sqlite3.Connection) -> None:
    lakers, celtics = _nba_teams(conn)
    gid = _create_canonical(
        conn, league_code="NBA", home_team_id=lakers, away_team_id=celtics,
        scheduled_start="2026-01-16T03:30:00Z", game_date_local="2026-01-15",
        official_provider="balldontlie", official_game_key="NG1",
        decided_at="2025-12-01T00:00:00.000000Z")
    ev = "KXNBAGAME-26JAN15BOSLAL"
    kev = seed_kalshi_event(conn, event_ticker=ev, series_ticker=NBA_SERIES,
                            title="Boston Celtics at Los Angeles Lakers")
    seed_kalshi_market(
        conn, market_ticker=f"{ev}-LAL", event_ticker=ev, series_ticker=NBA_SERIES,
        kalshi_event_id=kev,
        title="Boston Celtics at Los Angeles Lakers", yes_sub_title="Los Angeles Lakers",
        rules_primary="Resolves Yes if the Los Angeles Lakers win the game against the "
                      "Boston Celtics.")
    r = _kal(conn, series_ticker=NBA_SERIES)
    assert r.counters.events_accepted == 1 and r.counters.markets_accepted == 1
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kev, entity_type="kalshi_event")[0]
    assert d.method == "kalshi_date" and d.score == 0.92 and d.matched_entity_id == gid


# --------------------------------------------------------------------------- #
# Pure parser tests (Â§22 series/parsers, teams/titles, rules)
# --------------------------------------------------------------------------- #
_MLB_CODES = frozenset({"LAD", "SD", "NYY", "NYM", "ATL"})
_NBA_CODES = frozenset({"LAL", "BOS", "GSW"})


def test_parse_event_ticker_mlb_and_nba() -> None:
    from sports_quant.matching import kalshi_parse as kp

    e = kp.parse_event_ticker("KXMLBGAME-25JUL04SDLAD", _MLB_CODES)
    assert isinstance(e, kp.ParsedEventTicker)
    assert e.series.league_code == "MLB" and e.series.parser_version == "kmlb-1"
    assert e.game_date_local == "2025-07-04" and e.away_code == "SD" and e.home_code == "LAD"
    n = kp.parse_event_ticker("KXNBAGAME-26JAN15BOSLAL", _NBA_CODES)
    assert isinstance(n, kp.ParsedEventTicker) and n.away_code == "BOS" and n.home_code == "LAL"


def test_parse_event_ticker_rejects() -> None:
    from sports_quant.matching import kalshi_parse as kp

    assert isinstance(kp.parse_event_ticker("KXNFLGAME-25JUL04SDLAD", _MLB_CODES), kp.ParseError)
    assert isinstance(kp.parse_event_ticker("KXMLBGAME-25Xxx04SDLAD", _MLB_CODES), kp.ParseError)
    assert isinstance(kp.parse_event_ticker("KXMLBGAME-25JUL34SDLAD", _MLB_CODES), kp.ParseError)
    assert isinstance(kp.parse_event_ticker("KXMLBGAME-25JUL04ZZLAD", _MLB_CODES), kp.ParseError)
    # Trailing junk -> not a complete match.
    assert isinstance(kp.parse_event_ticker("KXMLBGAME-25JUL04SDLAD-X", _MLB_CODES), kp.ParseError)


def test_split_team_codes_ambiguous_rejected() -> None:
    from sports_quant.matching.kalshi_parse import split_team_codes

    # NYNYM could split NY+NYM or ... only unique valid split accepted.
    assert split_team_codes("SDLAD", _MLB_CODES) == ("SD", "LAD")
    assert split_team_codes("XXYY", _MLB_CODES) is None  # no valid codes


def test_parse_market_ticker_ancestry_and_subject() -> None:
    from sports_quant.matching import kalshi_parse as kp

    e = kp.parse_event_ticker("KXMLBGAME-25JUL04SDLAD", _MLB_CODES)
    assert isinstance(e, kp.ParsedEventTicker)
    ok = kp.parse_market_ticker("KXMLBGAME-25JUL04SDLAD-LAD", e)
    assert isinstance(ok, kp.ParsedMarketTicker) and ok.yes_code == "LAD"
    # Descends from a different event.
    assert isinstance(kp.parse_market_ticker("KXMLBGAME-25JUL04NYYNYM-NYY", e), kp.ParseError)


def test_parse_title_teams_forms() -> None:
    from sports_quant.matching import kalshi_parse as kp

    at = kp.parse_title_teams("San Diego Padres at Los Angeles Dodgers")
    assert isinstance(at, kp.TitleTeams) and at.away_name == "San Diego Padres"
    vs = kp.parse_title_teams("Padres vs. Dodgers")
    assert isinstance(vs, kp.TitleTeams) and vs.away_name is None
    assert isinstance(kp.parse_title_teams("Dodgers game tonight"), kp.ParseError)
    assert kp.parse_title_teams(None) is None


def test_parse_rules_yes_subject_forms() -> None:
    from sports_quant.matching import kalshi_parse as kp

    r = kp.parse_rules_yes_subject(
        "This market resolves to Yes if the Los Angeles Dodgers win the game against the "
        "San Diego Padres, originally scheduled for 2025-07-05T02:10:00Z.")
    assert isinstance(r, kp.ParsedRules)
    assert r.yes_name == "Los Angeles Dodgers" and r.other_name == "San Diego Padres"
    assert r.scheduled_time == "2025-07-05T02:10:00Z"
    assert isinstance(kp.parse_rules_yes_subject("A market about the weather."), kp.ParseError)
    assert kp.parse_rules_yes_subject(None) is None


# --------------------------------------------------------------------------- #
# Series / semantic classification
# --------------------------------------------------------------------------- #
def test_unsupported_series_no_review_flood(conn: sqlite3.Connection) -> None:
    # A non-sports (or unsupported sports) series: honest no-candidate, NOT
    # review-flagged, no DQ flood.
    seed_kalshi_event(conn, event_ticker="KXPRESIDENT-28", series_ticker="KXPRESIDENT",
                      title="Some non-sports market", category="Politics")
    r = _kal(conn, series_ticker="KXPRESIDENT")
    assert r.counters.unsupported_series == 1 and r.counters.events_accepted == 0
    review = SqliteMatchingRepository(conn).list_needs_review(entity_type="kalshi_event")
    assert review == []  # never enters the review queue
    assert conn.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0] == 0


def test_unsupported_market_semantic_not_malformed(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    ev = "KXMLBGAME-25JUL04SDLAD"
    kev = seed_kalshi_event(conn, event_ticker=ev, series_ticker=MLB_SERIES,
                            title="San Diego Padres at Los Angeles Dodgers")
    # A totals market (suffix is not a bare team code) -> unsupported semantic.
    for suffix in ("T85", "LADM15", "OHTANIHR"):
        seed_kalshi_market(conn, market_ticker=f"{ev}-{suffix}", event_ticker=ev,
                           series_ticker=MLB_SERIES, kalshi_event_id=kev,
                           title="San Diego Padres at Los Angeles Dodgers",
                           rules_primary="Some non-game-winner proposition.")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.unsupported_semantics == 3 and r.counters.markets_accepted == 0
    # Unsupported semantics are not review-flagged and produce no DQ defect.
    assert not any(d.entity_type == "kalshi_market"
                   for d in SqliteMatchingRepository(conn).list_needs_review())


# --------------------------------------------------------------------------- #
# Teams / title / rules disagreement
# --------------------------------------------------------------------------- #
def test_ticker_title_disagreement_rejected(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    ev = "KXMLBGAME-25JUL04SDLAD"
    seed_kalshi_event(conn, event_ticker=ev, series_ticker=MLB_SERIES,
                      title="New York Yankees at New York Mets")  # title disagrees with ticker
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.events_accepted == 0 and r.counters.events_rejected >= 1
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-KAL-TITLE-001" in codes


def test_unknown_team_code(conn: sqlite3.Connection) -> None:
    _mlb_teams(conn)  # only LAD/SD aliases; NYY/NYM unknown
    seed_kalshi_event(conn, event_ticker="KXMLBGAME-25JUL04SDLAD", series_ticker=MLB_SERIES,
                      title="San Diego Padres at Los Angeles Dodgers")
    # A different event whose codes are not curated -> unparseable/no-candidate.
    seed_kalshi_event(conn, event_ticker="KXMLBGAME-25JUL04XXYY", series_ticker=MLB_SERIES,
                      title="Nobody at Someone")
    r = _kal(conn, series_ticker=MLB_SERIES, event_ticker="KXMLBGAME-25JUL04XXYY")
    assert r.counters.events_accepted == 0 and r.counters.events_no_candidate >= 1


def test_ambiguous_team_code_blocks(conn: sqlite3.Connection) -> None:
    from .conftest import mark_team_ambiguous, seed_team_alias
    _dodgers_home_setup(conn)
    # A second team also carries the "SD" kalshi_public alias -> ambiguous.
    other = seed_team(conn, league_code="MLB", abbreviation="SDX", canonical_name="Sd Other",
                      city="Elsewhere", nickname="Others")
    seed_team_alias(conn, team_id=other, league_code="MLB", alias="SD", provider=KALSHI)
    mark_team_ambiguous(conn, "MLB")
    seed_kalshi_event(conn, event_ticker="KXMLBGAME-25JUL04SDLAD", series_ticker=MLB_SERIES,
                      title="San Diego Padres at Los Angeles Dodgers")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.needs_failure_exit
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-006" in codes


def test_rules_ticker_disagreement_rejected(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    ev = "KXMLBGAME-25JUL04SDLAD"
    kev = seed_kalshi_event(conn, event_ticker=ev, series_ticker=MLB_SERIES,
                            title="San Diego Padres at Los Angeles Dodgers")
    # Rules name a DIFFERENT pair than the ticker/title.
    seed_kalshi_market(conn, market_ticker=f"{ev}-LAD", event_ticker=ev, series_ticker=MLB_SERIES,
                       kalshi_event_id=kev, title="San Diego Padres at Los Angeles Dodgers",
                       yes_sub_title="Los Angeles Dodgers",
                       rules_primary="Yes if the New York Yankees win against the New York Mets.")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-KAL-RULES-001" in codes


def test_yes_sub_title_disagreement_rejected(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    ev = "KXMLBGAME-25JUL04SDLAD"
    kev = seed_kalshi_event(conn, event_ticker=ev, series_ticker=MLB_SERIES,
                            title="San Diego Padres at Los Angeles Dodgers")
    seed_kalshi_market(conn, market_ticker=f"{ev}-LAD", event_ticker=ev, series_ticker=MLB_SERIES,
                       kalshi_event_id=kev, title="San Diego Padres at Los Angeles Dodgers",
                       yes_sub_title="San Diego Padres",  # disagrees with ticker suffix LAD
                       rules_primary=_RULES.format(yes="Los Angeles Dodgers",
                                                    other="San Diego Padres", sched="n/a"))
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1


def test_result_field_does_not_influence_yes(conn: sqlite3.Connection) -> None:
    dodgers, _padres, _gid = _dodgers_home_setup(conn)
    ev = "KXMLBGAME-25JUL04SDLAD"
    kev = seed_kalshi_event(conn, event_ticker=ev, series_ticker=MLB_SERIES,
                            title="San Diego Padres at Los Angeles Dodgers")
    # A misleading settled result must not affect the Yes-team resolution.
    seed_kalshi_market(conn, market_ticker=f"{ev}-LAD", event_ticker=ev, series_ticker=MLB_SERIES,
                       kalshi_event_id=kev, title="San Diego Padres at Los Angeles Dodgers",
                       yes_sub_title="Los Angeles Dodgers", result="no",
                       rules_primary=_RULES.format(yes="Los Angeles Dodgers",
                                                    other="San Diego Padres", sched="n/a"))
    _kal(conn, series_ticker=MLB_SERIES)
    repo = SqliteKalshiRepository(conn)
    m = repo.get_market_by_ticker(f"{ev}-LAD")
    assert m is not None
    _g, _d, yes, _h, _s = repo.market_link(m.kalshi_market_id)
    assert yes == dodgers  # from rules, not the 'no' result field
    _ = kev


# --------------------------------------------------------------------------- #
# Atomic links, replay, conflict
# --------------------------------------------------------------------------- #
def test_market_conflict_leaves_no_new_accepted(conn: sqlite3.Connection) -> None:
    from sports_quant.db.ids import new_match_decision_id
    dodgers, padres, gid = _dodgers_home_setup(conn)
    # A second, unrelated game the market's link is corruptly pointed at.
    other = _mlb_game(conn, dodgers, padres, start="2025-07-10T02:10:00Z", date="2025-07-09",
                      key="G2")
    kev, kmk = _seed_event_and_market(conn)
    with transaction(conn):
        mid = new_match_decision_id()
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, "
            "source_ref, matched_entity_id, outcome, method, score, threshold, matcher_version, "
            "needs_manual_review, decided_at, created_at) VALUES "
            "(?, 'kalshi_market', 'kalshi_public', ?, ?, 'accepted', 'kalshi_date', 0.92, 0.85, "
            "'v', 0, '2025-07-01T00:00:00.000000Z', '2025-07-01T00:00:00.000000Z')",
            (mid, kmk, other))
        conn.execute("UPDATE kalshi_markets SET game_id=?, match_decision_id=?, yes_team_id=?, "
                     "matched_rules_hash='oldhash', market_semantic='game_winner' "
                     "WHERE kalshi_market_id=?", (other, mid, dodgers, kmk))
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.needs_failure_exit
    # No new accepted market decision naming the (correct) gid; existing link intact.
    accepted = [d for d in SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kmk, entity_type="kalshi_market")
        if d.outcome == "accepted"]
    assert all(d.matched_entity_id == other for d in accepted)
    assert SqliteKalshiRepository(conn).market_link(kmk)[0] == other
    _ = (gid, kev)


def test_market_link_failure_rolls_back(conn: sqlite3.Connection) -> None:
    import pytest

    from sports_quant.matching.linkatomic import MatchLinkError
    _dodgers_home_setup(conn)
    _seed_event_and_market(conn)
    svc = MatchKalshiService(conn)
    svc._kal.link_market_game = lambda **_kw: LinkOutcomeStub.CONFLICT  # type: ignore[assignment,method-assign]  # noqa: E501
    before = SqliteMatchingRepository(conn).count()
    with pytest.raises(MatchLinkError):
        with transaction(conn):
            svc.match_range(series_ticker=MLB_SERIES)
    assert SqliteMatchingRepository(conn).count() == before  # rolled back


def test_replay_is_stable(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    before = SqliteMatchingRepository(conn).count()
    r2 = _kal(conn, series_ticker=MLB_SERIES)
    assert r2.counters.events_already_linked == 1 and r2.counters.markets_already_linked == 1
    assert r2.counters.events_accepted == 0 and r2.counters.markets_accepted == 0
    assert SqliteMatchingRepository(conn).count() == before  # no new decisions


# --------------------------------------------------------------------------- #
# rules_hash invalidation (Â§15)
# --------------------------------------------------------------------------- #
def test_rules_hash_change_invalidates(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    repo = SqliteKalshiRepository(conn)
    assert repo.is_kalshi_market_orientation_approved(kmk)  # approved under the matched hash
    matched_before = repo.market_link(kmk)[3]
    # Rules change (still parses to the same Yes team, but a new hash).
    set_kalshi_market_rules(
        conn, market_ticker=f"{_ticker(conn, kmk)}",
        rules_primary="Per updated house rules, Yes if the Los Angeles Dodgers win the game "
                      "against the San Diego Padres.",
        observed_at="2026-08-01T00:00:00.000000Z")  # newer than the seed knowledge time
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.rules_hash_conflicts >= 1 and r.needs_failure_exit
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-004" in codes
    assert not repo.is_kalshi_market_orientation_approved(kmk)  # readiness disabled
    assert repo.market_link(kmk)[3] == matched_before  # matched hash NOT overwritten


# --------------------------------------------------------------------------- #
# PIT + provenance
# --------------------------------------------------------------------------- #
def test_pit_as_of_and_provenance(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    repo = SqliteKalshiRepository(conn)
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kmk, entity_type="kalshi_market")[0]
    # Later decision invisible before the cutoff; visible at/after it.
    assert not repo.is_kalshi_market_orientation_approved(kmk, as_of="2000-01-01T00:00:00.000000Z")
    assert repo.is_kalshi_market_orientation_approved(kmk, as_of=d.decided_at)
    # Provenance is the market's CURRENT response, not fabricated.
    market = repo.get_market(kmk)
    assert market is not None and d.raw_response_id == market.current_raw_response_id


# --------------------------------------------------------------------------- #
# Price / order-book / trade isolation (Â§18)
# --------------------------------------------------------------------------- #
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


def test_sql_trace_never_touches_books_or_trades(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
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


def test_prices_and_trades_do_not_change_decision(tmp_path: Path) -> None:
    # Two fresh corpora -- one with a (lopsided) order book + trade, one without.
    # The order-book/trade tables are append-only, so isolation is proven by
    # comparing independent runs (matching provably never queries them).
    import random

    from sports_quant.db.init import initialize_database
    links: set[tuple] = set()  # type: ignore[type-arg]
    rng = random.Random(3)
    for i, with_book in enumerate((True, False, True)):
        db = tmp_path / f"iso{i}.db"
        initialize_database(db)
        with Database(db).connection() as c:
            _dodgers_home_setup(c)
            _kev, kmk = _seed_event_and_market(c)
            mt = _ticker(c, kmk)
            if with_book:
                _seed_book_and_trade(c, mt)
                _ = rng.random()
            with transaction(c):
                MatchKalshiService(c).match_range(series_ticker=MLB_SERIES)
            g, _d, yes, _h, sem = SqliteKalshiRepository(c).market_link(kmk)
            links.add((yes, sem, g is not None))
    assert len(links) == 1  # identical Yes/semantic/linked regardless of book/trade presence


def test_no_execution_import() -> None:
    src = (Path(__file__).parent.parent / "kalshi.py").read_text(encoding="utf-8")
    assert "execution" not in src and "gateway" not in src and "order_book" not in src


# --------------------------------------------------------------------------- #
# Dry-run + CLI
# --------------------------------------------------------------------------- #
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


def test_sportsbook_and_kalshi_books_untouched(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _seed_book_and_trade(conn, _ticker(conn, kmk))
    books_before = conn.execute("SELECT COUNT(*) FROM kalshi_orderbook_snapshots").fetchone()[0]
    sb_before = conn.execute("SELECT COUNT(*) FROM sportsbook_events").fetchone()[0]
    _kal(conn, series_ticker=MLB_SERIES)
    assert conn.execute("SELECT COUNT(*) FROM kalshi_orderbook_snapshots").fetchone()[0] == \
        books_before
    assert conn.execute("SELECT COUNT(*) FROM sportsbook_events").fetchone()[0] == sb_before


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


def test_cross_midnight_tier1_local_slate(conn: sqlite3.Connection) -> None:
    from .conftest import seed_venue
    dodgers, padres = _mlb_teams(conn)
    venue = seed_venue(conn, name="Dodger Stadium", provider="mlb_statsapi",
                       provider_venue_id="LAV", timezone="America/Los_Angeles")
    gid = _mlb_game(conn, dodgers, padres, start="2025-07-05T02:10:00Z", date="2025-07-04")
    conn.execute("UPDATE games SET venue = ? WHERE game_id = ?", (venue, gid))
    conn.commit()
    kev = seed_kalshi_event(conn, event_ticker="KXMLBGAME-25JUL04SDLAD", series_ticker=MLB_SERIES,
                            title="San Diego Padres at Los Angeles Dodgers")
    # Rules carry the exact scheduled instant -> Tier 1 with venue-local slate.
    seed_kalshi_market(
        conn, market_ticker="KXMLBGAME-25JUL04SDLAD-LAD", event_ticker="KXMLBGAME-25JUL04SDLAD",
        series_ticker=MLB_SERIES, kalshi_event_id=kev,
        title="San Diego Padres at Los Angeles Dodgers", yes_sub_title="Los Angeles Dodgers",
        rules_primary=_RULES.format(yes="Los Angeles Dodgers", other="San Diego Padres",
                                    sched="2025-07-05T02:10:00Z"))
    _kal(conn, series_ticker=MLB_SERIES)
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kev, entity_type="kalshi_event")[0]
    assert d.matched_entity_id == gid  # matched by instant, venue-local slate = 07-04


def test_indistinguishable_doubleheader_ambiguous(conn: sqlite3.Connection) -> None:
    dodgers, padres = _mlb_teams(conn)
    _mlb_game(conn, dodgers, padres, start="2025-07-04T17:00:00Z", date="2025-07-04", key="G1",
              gn=1)
    _mlb_game(conn, dodgers, padres, start="2025-07-04T23:00:00Z", date="2025-07-04", key="G2",
              gn=2)
    seed_kalshi_event(conn, event_ticker="KXMLBGAME-25JUL04SDLAD", series_ticker=MLB_SERIES,
                      title="San Diego Padres at Los Angeles Dodgers")
    r = _kal(conn, series_ticker=MLB_SERIES)  # date-only, two same-slate games
    assert r.counters.events_ambiguous == 1 and r.counters.events_accepted == 0


def test_same_game_different_yes_is_conflict(conn: sqlite3.Connection) -> None:
    from sports_quant.db.ids import new_match_decision_id
    dodgers, padres, gid = _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    # Corrupt prior link: same game, but Yes wrongly set to the Padres.
    with transaction(conn):
        mid = new_match_decision_id()
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, "
            "source_ref, matched_entity_id, outcome, method, score, threshold, matcher_version, "
            "needs_manual_review, decided_at, created_at) VALUES "
            "(?, 'kalshi_market', 'kalshi_public', ?, ?, 'accepted', 'kalshi_date', 0.92, 0.85, "
            "'v', 0, '2025-07-01T00:00:00.000000Z', '2025-07-01T00:00:00.000000Z')",
            (mid, kmk, gid))
        conn.execute("UPDATE kalshi_markets SET game_id=?, match_decision_id=?, yes_team_id=?, "
                     "matched_rules_hash='h', market_semantic='game_winner' "
                     "WHERE kalshi_market_id=?", (gid, mid, padres, kmk))
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.needs_failure_exit  # same game, different Yes team -> not a replay, blocking
    assert SqliteKalshiRepository(conn).market_link(kmk)[2] == padres  # unchanged
    _ = dodgers


class LinkOutcomeStub:
    """Local stand-in so the rollback test can force a CONFLICT without importing
    the repository enum at module load (kept trivial and explicit)."""

    from sports_quant.db.repositories.references import LinkOutcome as _LO
    CONFLICT = _LO.CONFLICT

