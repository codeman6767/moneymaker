"""Phase D5B2 independent-review regression gaps (§17).

Isolated temporary corpora only; no provider client, no network. Complements
``test_phase_d5b2_kalshi`` with the split-collision, DST, venue-knowledge-time,
NBA-ambiguity, rules-timeline, and provenance cases the independent review
requires. Strings labelled SYNTHETIC are unit edge cases, never a provider claim.
"""

from __future__ import annotations

import sqlite3

from sports_quant.db.repositories.kalshi import SqliteKalshiRepository
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.matching import kalshi_parse as kp
from sports_quant.matching.kalshi import _local_clock_to_utc

from . import kalshi_fixtures as fx
from .conftest import seed_kalshi_event, seed_kalshi_market, set_kalshi_market_rules
from .test_phase_d5a_matching import _create_canonical
from .test_phase_d5b2_kalshi import (
    _MLB_EVENT,
    _MLB_RULES,
    KALSHI,
    MLB_SERIES,
    NBA_SERIES,
    _dodgers_home_setup,
    _kal,
    _mlb_teams,
    _seed_event_and_market,
    _team,
    _ticker,
)


# --------------------------------------------------------------------------- #
# §3 team-code split: prefix collision + randomized alias order (§17.4/5)
# --------------------------------------------------------------------------- #
def test_prefix_collision_split_is_ambiguous() -> None:
    # 'NY' is a prefix of 'NYK'; 'NYKBOS' splits both NY+KBOS and NYK+BOS ->
    # a prefix code can never silently win; two valid splits reject.
    codes = frozenset({"NY", "NYK", "BOS", "KBOS"})
    assert kp.split_team_codes("NYKBOS", codes) is None


def test_zero_valid_splits_reject() -> None:
    assert kp.split_team_codes("AZPIT", frozenset({"AZ", "BOS"})) is None  # no PIT


def test_split_invariant_under_alias_order() -> None:
    # The split is a pure function of the code SET, independent of insertion order.
    base = ["SD", "LAD", "NYY", "BOS", "AZ", "PIT"]
    import random

    results = set()
    for seed in range(25):
        shuffled = base[:]
        random.Random(seed).shuffle(shuffled)
        results.add(kp.split_team_codes("SDLAD", frozenset(shuffled)))
    assert results == {("SD", "LAD")}  # one stable answer regardless of order


# --------------------------------------------------------------------------- #
# §4 MLB venue-local clock: DST gap / fold / association-after-cutoff (§17.7/8/10)
# --------------------------------------------------------------------------- #
def test_dst_gap_local_time_rejected() -> None:
    # 2026-03-08 02:30 America/New_York does not exist (spring-forward gap).
    utc, err = _local_clock_to_utc("2026-03-08", "02:30", "America/New_York", None)
    assert utc is None and err is not None


def test_dst_fold_local_time_rejected() -> None:
    # 2026-11-01 01:30 America/New_York is ambiguous (fall-back fold).
    utc, err = _local_clock_to_utc("2026-11-01", "01:30", "America/New_York", None)
    assert utc is None and err is not None


def test_unambiguous_local_time_ok() -> None:
    utc, err = _local_clock_to_utc("2026-11-01", "13:30", "America/New_York", "EST")
    assert err is None and utc is not None and utc.strftime("%H:%MZ") == "18:30Z"


def test_venue_association_learned_after_cutoff_no_exact_tier(conn: sqlite3.Connection) -> None:
    # Venue entity known early, but the game<->venue association (its accepted game
    # decision) is decided AFTER the Kalshi cutoff -> no strongest exact-time tier;
    # conservative date-only Tier 2.
    dodgers, padres = _mlb_teams(conn)
    from .conftest import seed_venue
    venue = seed_venue(conn, name="Dodger Stadium", provider="mlb_statsapi",
                       provider_venue_id="LAV", timezone="America/Los_Angeles",
                       observed_at="2025-01-01T00:00:00.000000Z")  # known early
    gid = _create_canonical(
        conn, league_code="MLB", home_team_id=dodgers, away_team_id=padres,
        scheduled_start="2025-07-05T02:10:00Z", game_date_local="2025-07-04",
        official_provider="mlb_statsapi", official_game_key="G1",
        decided_at="2027-01-01T00:00:00.000000Z")  # association learned AFTER cutoff (T0)
    conn.execute("UPDATE games SET venue=? WHERE game_id=?", (venue, gid))
    conn.commit()
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    d = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=KALSHI, source_ref=kmk, entity_type="kalshi_market")
    assert any(x.method == "kalshi_date" and x.matched_entity_id == gid for x in d)
    assert not any(x.method == "kalshi_ticker_time" for x in d)


# --------------------------------------------------------------------------- #
# §5 NBA date-only ambiguity (§17.11)
# --------------------------------------------------------------------------- #
def test_nba_same_slate_doubleheader_ambiguous(conn: sqlite3.Connection) -> None:
    nyk = _team(conn, league="NBA", abbr="NYK", name="New York", code="NYK")
    sas = _team(conn, league="NBA", abbr="SAS", name="San Antonio", code="SAS")
    for key, gn in (("NG1", 1), ("NG2", 2)):
        _create_canonical(conn, league_code="NBA", home_team_id=sas, away_team_id=nyk,
                          scheduled_start="2026-06-14T00:00:00Z", game_date_local="2026-06-13",
                          official_provider="balldontlie", official_game_key=key, game_number=gn,
                          decided_at="2026-06-01T00:00:00.000000Z")
    seed_kalshi_event(conn, event_ticker="KXNBAGAME-26JUN13NYKSAS", series_ticker=NBA_SERIES,
                      title="New York at San Antonio")
    r = _kal(conn, series_ticker=NBA_SERIES)
    assert r.counters.events_ambiguous == 1 and r.counters.events_accepted == 0
    _ = (nyk, sas)


# --------------------------------------------------------------------------- #
# §7 rules parsing: malformed date + misleading extra text (§17.14/15)
# --------------------------------------------------------------------------- #
def test_rules_malformed_date_rejected() -> None:
    bad = ("If Arizona wins the Arizona vs Pittsburgh professional baseball game originally "
           "scheduled for Jul 32, 2026 at 6:40 PM EDT, then the market resolves to Yes.")
    assert isinstance(kp.parse_rules_yes_subject(bad), kp.ParseError)


def test_rules_misleading_extra_team_not_captured() -> None:
    # A trailing clause names a third team; the explicit template must not be
    # fooled -- Yes stays Arizona and the pair stays {Arizona, Pittsburgh}.
    text = ("If Arizona wins the Arizona vs Pittsburgh professional baseball game originally "
            "scheduled for Jul 27, 2026 at 6:40 PM EDT, then the market resolves to Yes. "
            "This has nothing to do with the New York Yankees.")
    r = kp.parse_rules_yes_subject(text)
    assert isinstance(r, kp.ParsedRules)
    assert r.yes_name == "Arizona" and set(r.names) == {"Arizona", "Pittsburgh"}


# --------------------------------------------------------------------------- #
# §10 corrupted replay pairing (§17.19)
# --------------------------------------------------------------------------- #
def test_corrupted_replay_pairing_is_blocking(conn: sqlite3.Connection) -> None:
    # The decisions table is append-only except its review columns (a DB trigger
    # blocks tampering with matched_entity_id). A review-gated supporting decision
    # is the reachable "corrupt pairing": the same game id is no longer a clean,
    # non-review-gated replay, so it must be blocking, never idempotent.
    _d, _p, gid = _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    repo = SqliteKalshiRepository(conn)
    dec_id = repo.market_link(kmk)[1]
    assert dec_id is not None
    SqliteMatchingRepository(conn).flag_for_review(dec_id)  # gate the supporting decision
    conn.commit()
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_already_linked == 0
    assert r.counters.markets_rejected >= 1 and r.needs_failure_exit
    assert repo.market_link(kmk)[0] == gid  # link never silently overwritten


# --------------------------------------------------------------------------- #
# §11 rules timeline: benign hash change PIT before/after; A->B->A; disagreeing
# change must also block (defect probe) (§17.20/21)
# --------------------------------------------------------------------------- #
def _link_then_change_rules(conn: sqlite3.Connection, new_rules: str):  # type: ignore[no-untyped-def]
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    set_kalshi_market_rules(conn, market_ticker=_ticker(conn, kmk), rules_primary=new_rules,
                            observed_at="2026-08-01T00:00:00.000000Z")
    r = _kal(conn, series_ticker=MLB_SERIES)
    return kmk, r


def test_historical_readiness_before_and_after_conflict(conn: sqlite3.Connection) -> None:
    benign = _MLB_RULES.replace("then the market resolves to Yes.",
                                "then the market resolves to Yes (rev 2).")
    kmk, r = _link_then_change_rules(conn, benign)
    assert r.counters.rules_hash_conflicts >= 1 and r.needs_failure_exit
    repo = SqliteKalshiRepository(conn)
    mrepo = SqliteMatchingRepository(conn)
    d0 = mrepo.decisions_for_source(source_provider=KALSHI, source_ref=kmk,
                                    entity_type="kalshi_market")[0].decided_at
    dq_t = conn.execute(
        "SELECT detected_at FROM data_quality_issues WHERE rule_code='DQ-MATCH-004' "
        "AND entity_id=? ORDER BY detected_at LIMIT 1", (kmk,)).fetchone()[0]
    assert d0 < dq_t
    assert repo.is_kalshi_market_orientation_approved(kmk, as_of=d0)       # before detection
    assert not repo.is_kalshi_market_orientation_approved(kmk, as_of=dq_t)  # after detection
    assert not repo.is_kalshi_market_orientation_approved(kmk)              # current, fail-closed


def test_rules_history_a_b_a_does_not_erase_conflict(conn: sqlite3.Connection) -> None:
    _dodgers_home_setup(conn)
    _kev, kmk = _seed_event_and_market(conn)
    _kal(conn, series_ticker=MLB_SERIES)
    repo = SqliteKalshiRepository(conn)
    tkr = _ticker(conn, kmk)
    b = _MLB_RULES.replace("then the market resolves to Yes.", "then the market resolves to Yes (B).")
    set_kalshi_market_rules(conn, market_ticker=tkr, rules_primary=b,
                            observed_at="2026-08-01T00:00:00.000000Z")
    _kal(conn, series_ticker=MLB_SERIES)
    assert not repo.is_kalshi_market_orientation_approved(kmk)
    # Rules revert to the exact original A.
    set_kalshi_market_rules(conn, market_ticker=tkr, rules_primary=_MLB_RULES,
                            observed_at="2026-08-02T00:00:00.000000Z")
    _kal(conn, series_ticker=MLB_SERIES)
    # The intermediate conflict is not erased: the decision stays flagged and the
    # blocking DQ-MATCH-004 remains, so readiness is still closed.
    assert not repo.is_kalshi_market_orientation_approved(kmk)
    dec_id = repo.market_link(kmk)[1]
    assert dec_id is not None
    flagged = SqliteMatchingRepository(conn).get(dec_id)
    assert flagged is not None and flagged.needs_manual_review


def test_disagreeing_rules_change_blocks_historical_readiness(conn: sqlite3.Connection) -> None:
    # A linked market whose rules change to a DISAGREEING form (Yes flips to the
    # other team) must invalidate orientation as strongly as a benign hash change:
    # a blocking DQ-MATCH-004 that historical readiness honours. Otherwise a stale
    # orientation stays wrongly approved as-of a post-change cutoff.
    disagreeing = ("If San Diego Padres wins the San Diego Padres vs Los Angeles Dodgers "
                   "professional baseball game originally scheduled for Jul 4, 2025 at "
                   "7:10 PM PDT, then the market resolves to Yes.")
    kmk, r = _link_then_change_rules(conn, disagreeing)
    repo = SqliteKalshiRepository(conn)
    assert r.counters.rules_hash_conflicts >= 1 and r.needs_failure_exit
    assert not repo.is_kalshi_market_orientation_approved(kmk)  # current
    assert not repo.is_kalshi_market_orientation_approved(
        kmk, as_of="2030-01-01T00:00:00.000000Z")  # historical, after the change


# --------------------------------------------------------------------------- #
# §2 real calendar-date validation (ticker + rules share ONE validator)
# --------------------------------------------------------------------------- #
def test_leap_day_accepted_only_in_leap_years() -> None:
    assert kp._parse_nl_date("Feb 29, 2024") == "2024-02-29"   # leap year
    assert kp._parse_nl_date("Feb 29, 2025") is None           # non-leap
    assert kp._parse_date_code("24FEB29") == "2024-02-29"      # ticker leap year
    assert kp._parse_date_code("25FEB29") is None              # ticker non-leap


def test_impossible_calendar_dates_rejected() -> None:
    for code in ("26APR31", "26JUN31", "26FEB30", "26JUL00"):  # Apr/Jun 31, Feb 30, day 0
        assert kp._parse_date_code(code) is None, code
    for nl in ("Apr 31, 2026", "Jun 31, 2026", "Feb 30, 2026", "Jul 0, 2026"):
        assert kp._parse_nl_date(nl) is None, nl


def test_valid_dates_and_month_names_accepted() -> None:
    assert kp._parse_date_code("26DEC31") == "2026-12-31"
    assert kp._parse_nl_date("December 31, 2026") == "2026-12-31"   # full month name
    assert kp._parse_nl_date("Dec 31, 2026") == "2026-12-31"       # abbreviation


def test_ticker_and_rules_date_validators_agree() -> None:
    # (ticker YYMONDD, natural-language date) must yield the SAME ISO or both None.
    cases = [
        ("24FEB29", "Feb 29, 2024", "2024-02-29"),
        ("25FEB29", "Feb 29, 2025", None),
        ("26APR31", "Apr 31, 2026", None),
        ("26DEC31", "Dec 31, 2026", "2026-12-31"),
        ("26JAN01", "Jan 1, 2026", "2026-01-01"),
        ("99DEC31", "Dec 31, 2099", "2099-12-31"),  # documented 2000-2099 window
    ]
    for code, nl, want in cases:
        assert kp._parse_date_code(code) == want, code
        assert kp._parse_nl_date(nl) == want, nl


# --------------------------------------------------------------------------- #
# §3 title AND subtitle validated independently (event has both pair fields)
# --------------------------------------------------------------------------- #
_T_TITLE = "San Diego Padres vs Los Angeles Dodgers"   # unordered, agrees w/ ticker
_T_ORDERED = "San Diego Padres at Los Angeles Dodgers"  # ordered away=SD home=LAD (ok)
_T_REVERSED = "Los Angeles Dodgers at San Diego Padres"  # reversed orientation
_T_ONE_TEAM = "San Diego Padres"                        # malformed (one team)
_T_UNRELATED = "New York Yankees vs Boston Red Sox"      # different teams


def _event_result(conn: sqlite3.Connection, *, title: str, sub_title):  # type: ignore[no-untyped-def]
    _dodgers_home_setup(conn)
    seed_kalshi_event(conn, event_ticker=_MLB_EVENT, series_ticker=MLB_SERIES,
                      title=title, sub_title=sub_title)
    return _kal(conn, series_ticker=MLB_SERIES)


def test_title_only_valid_accepts(conn: sqlite3.Connection) -> None:
    r = _event_result(conn, title=_T_TITLE, sub_title=None)
    assert r.counters.events_accepted == 1


def test_subtitle_only_valid_accepts(conn: sqlite3.Connection) -> None:
    # Title absent (empty) -> the pair sub-title alone proves the teams.
    r = _event_result(conn, title="", sub_title="SD vs LAD (Jul 4)")  # decorated codes
    assert r.counters.events_accepted == 1


def test_matching_title_and_subtitle_accepts(conn: sqlite3.Connection) -> None:
    r = _event_result(conn, title=_T_TITLE, sub_title="SD vs LAD (Jul 4)")
    assert r.counters.events_accepted == 1


def test_ordered_title_plus_equivalent_unordered_subtitle_accepts(
        conn: sqlite3.Connection) -> None:
    r = _event_result(conn, title=_T_ORDERED, sub_title=_T_TITLE)
    assert r.counters.events_accepted == 1


def test_valid_title_reversed_subtitle_rejected(conn: sqlite3.Connection) -> None:
    r = _event_result(conn, title=_T_TITLE, sub_title=_T_REVERSED)
    assert r.counters.events_accepted == 0 and r.counters.events_rejected >= 1


def test_reversed_title_valid_subtitle_rejected(conn: sqlite3.Connection) -> None:
    r = _event_result(conn, title=_T_REVERSED, sub_title=_T_TITLE)
    assert r.counters.events_accepted == 0 and r.counters.events_rejected >= 1


def test_valid_title_unrelated_subtitle_rejected(conn: sqlite3.Connection) -> None:
    r = _event_result(conn, title=_T_TITLE, sub_title=_T_UNRELATED)
    assert r.counters.events_accepted == 0 and r.counters.events_rejected >= 1


def test_two_ordered_fields_conflicting_orientation_rejected(conn: sqlite3.Connection) -> None:
    r = _event_result(conn, title=_T_ORDERED, sub_title=_T_REVERSED)
    assert r.counters.events_accepted == 0 and r.counters.events_rejected >= 1


def test_malformed_supplied_subtitle_is_reviewable_not_ignored(conn: sqlite3.Connection) -> None:
    # A supplied one-team sub-title is reviewable even though the title is valid.
    r = _event_result(conn, title=_T_TITLE, sub_title=_T_ONE_TEAM)
    assert r.counters.events_accepted == 0 and r.counters.events_rejected >= 1


# --------------------------------------------------------------------------- #
# §4/§6 audited no_sub_title convention: repeats the YES team (MLB + NBA)
# --------------------------------------------------------------------------- #
def test_mlb_no_sub_title_opposing_team_rejected(conn: sqlite3.Connection) -> None:
    # Yes team is the Dodgers (ticker suffix LAD); no_sub_title naming the OPPOSING
    # participant (Padres) violates the audited convention and must reject.
    _dodgers_home_setup(conn)
    _seed_event_and_market(conn, no_sub="San Diego Padres")
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1


def _seed_nba(conn: sqlite3.Connection, *, no_sub: str):  # type: ignore[no-untyped-def]
    nyk = _team(conn, league="NBA", abbr="NYK", name="New York", code="NYK")
    sas = _team(conn, league="NBA", abbr="SAS", name="San Antonio", code="SAS")
    _create_canonical(conn, league_code="NBA", home_team_id=sas, away_team_id=nyk,
                      scheduled_start="2026-06-14T00:00:00Z", game_date_local="2026-06-13",
                      official_provider="balldontlie", official_game_key="NG1",
                      decided_at="2026-06-01T00:00:00.000000Z")
    kev = seed_kalshi_event(conn, event_ticker=fx.NBA_NYK_SAS.event_ticker,
                            series_ticker=NBA_SERIES, title=fx.NBA_NYK_SAS.title)
    seed_kalshi_market(conn, market_ticker=fx.NBA_NYK_SAS.market_ticker,
                       event_ticker=fx.NBA_NYK_SAS.event_ticker, series_ticker=NBA_SERIES,
                       kalshi_event_id=kev, title=fx.NBA_NYK_SAS.title,
                       yes_sub_title=fx.NBA_NYK_SAS.yes_sub_title, no_sub_title=no_sub,
                       rules_primary=fx.NBA_NYK_SAS.rules_primary)
    return _kal(conn, series_ticker=NBA_SERIES)


def test_nba_no_sub_title_equals_yes_accepted(conn: sqlite3.Connection) -> None:
    r = _seed_nba(conn, no_sub="San Antonio")  # audited: == Yes-subject team
    assert r.counters.markets_accepted == 1


def test_nba_no_sub_title_opposing_team_rejected(conn: sqlite3.Connection) -> None:
    r = _seed_nba(conn, no_sub="New York")  # opposing participant -> reject
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1


def test_yes_sub_title_disagreeing_with_ticker_rejected(conn: sqlite3.Connection) -> None:
    # Mutual consistency: yes_sub_title must equal the ticker Yes subject.
    _dodgers_home_setup(conn)
    _seed_event_and_market(conn, yes_sub="San Diego Padres")  # ticker Yes = Dodgers
    r = _kal(conn, series_ticker=MLB_SERIES)
    assert r.counters.markets_accepted == 0 and r.counters.markets_rejected >= 1
