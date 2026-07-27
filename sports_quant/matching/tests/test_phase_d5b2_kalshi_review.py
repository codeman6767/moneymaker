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

from .conftest import seed_kalshi_event, set_kalshi_market_rules
from .test_phase_d5a_matching import _create_canonical
from .test_phase_d5b2_kalshi import (
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
