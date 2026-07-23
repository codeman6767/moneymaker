"""Sportsbook repositories: identity upserts, idempotent + append-only prices,
and the point-in-time "latest price known at or before T" query.

These exercise the repositories directly with controlled ``observed_at`` values
so the as-of and backfill semantics can be pinned without depending on the wall
clock -- which an end-to-end ingest cannot.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.db.repositories.sportsbook import (
    SqliteSportsbookRepository,
    point_key,
    price_content_hash,
)

T0 = "2026-07-22T18:00:00.000000Z"
T1 = "2026-07-22T19:00:00.000000Z"
T2 = "2026-07-22T20:00:00.000000Z"


def _raw(conn: sqlite3.Connection, body: str = "[]"):
    """Create a run + raw response and return the raw-response model."""

    run = SqliteIngestionRunRepository(conn).start(
        command="ingest-odds",
        provider="the_odds_api",
        operation="get_odds",
        args_json="{}",
        started_monotonic_ns=0,
        tool_version="test",
        sport="mlb",
    )
    return SqliteRawResponseRepository(conn).store(
        run_id=run.run_id,
        provider="the_odds_api",
        endpoint="/v4/sports/baseball_mlb/odds",
        request_params_json="{}",
        http_status=200,
        response_headers_json="{}",
        requested_at=T0,
        received_at=T0,
        elapsed_ns=1,
        body=body,
        content_hash=response_content_hash(
            provider="the_odds_api",
            endpoint="/v4/sports/baseball_mlb/odds",
            request_params={},
            body=body,
        ),
    ), run.run_id


def _outcome(conn: sqlite3.Connection):
    """A single event -> market -> outcome scaffold; return (repo, outcome, raw, run)."""

    repo = SqliteSportsbookRepository(conn)
    raw, run_id = _raw(conn)
    event = repo.upsert_event(
        provider="the_odds_api",
        provider_event_id="evt-1",
        sport_key="baseball_mlb",
        commence_time=T2,
        home_team_raw="New York Yankees",
        away_team_raw="Boston Red Sox",
        raw_response_id=raw.raw_response_id,
        observed_at=T0,
        league_id="lg_mlb",
    )
    market = repo.upsert_market(
        sb_event_id=event.sb_event_id,
        bookmaker_key="draftkings",
        market_key="h2h",
        raw_response_id=raw.raw_response_id,
        observed_at=T0,
    )
    outcome = repo.upsert_outcome(
        sb_market_id=market.sb_market_id,
        outcome_name="new york yankees",
        provider_outcome_name="New York Yankees",
        outcome_role="home",
    )
    return repo, outcome, raw, run_id


def _append(repo, outcome, raw, run_id, *, price: int, observed_at: str):
    ch = price_content_hash(
        price_american=price,
        point=None,
        bookmaker_last_update=None,
        market_last_update=None,
        provider_timestamp=None,
    )
    return repo.append_price_snapshot(
        sb_outcome_id=outcome.sb_outcome_id,
        price_american=price,
        observed_at=observed_at,
        raw_response_id=raw.raw_response_id,
        raw_response_hash=raw.content_hash,
        run_id=run_id,
        content_hash=ch,
    )


def test_event_upsert_is_idempotent_on_provider_identity(conn: sqlite3.Connection) -> None:
    repo = SqliteSportsbookRepository(conn)
    raw, _ = _raw(conn)
    first = repo.upsert_event(
        provider="the_odds_api",
        provider_event_id="evt-1",
        sport_key="baseball_mlb",
        commence_time=T2,
        home_team_raw="New York Yankees",
        away_team_raw="Boston Red Sox",
        raw_response_id=raw.raw_response_id,
        observed_at=T0,
        league_id="lg_mlb",
    )
    second = repo.upsert_event(
        provider="the_odds_api",
        provider_event_id="evt-1",
        sport_key="baseball_mlb",
        commence_time=T2,
        home_team_raw="New York Yankees",
        away_team_raw="Boston Red Sox",
        raw_response_id=raw.raw_response_id,
        observed_at=T1,
        league_id="lg_mlb",
    )
    assert first.sb_event_id == second.sb_event_id
    assert repo.count_events() == 1
    # last_observed_at advanced; first_observed_at did not.
    assert second.first_observed_at == T0
    assert second.last_observed_at == T1


def test_outcome_identity_is_stable_across_price_changes(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    _append(repo, outcome, raw, run_id, price=-150, observed_at=T1)
    # A changed price is not a new identity.
    assert repo.count_outcomes() == 1
    assert repo.count_snapshots() == 2


def test_identical_observation_is_idempotent(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _snap, first = _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    _snap2, second = _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    assert first is True
    assert second is False
    assert repo.count_snapshots() == 1


def test_changed_price_appends_a_new_snapshot(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    _snap, inserted = _append(repo, outcome, raw, run_id, price=-150, observed_at=T1)
    assert inserted is True
    assert repo.count_snapshots() == 2


def test_older_backfill_is_preserved_and_does_not_replace(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    # Latest known first...
    _append(repo, outcome, raw, run_id, price=-150, observed_at=T2)
    # ...then a backfilled earlier observation arrives.
    _snap, inserted = _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    assert inserted is True
    assert repo.count_snapshots() == 2
    # The current latest is unchanged by the backfill.
    latest = repo.latest_price(outcome.sb_outcome_id)
    assert latest is not None and latest.price_american == -150


def test_latest_at_or_before_returns_the_correct_historical_price(
    conn: sqlite3.Connection,
) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    _append(repo, outcome, raw, run_id, price=-150, observed_at=T2)

    # Between the two observations, the as-of price is the earlier one.
    at_t1 = repo.price_as_of(outcome.sb_outcome_id, T1)
    assert at_t1 is not None and at_t1.price_american == -140
    # At/after T2, the later price.
    at_t2 = repo.price_as_of(outcome.sb_outcome_id, T2)
    assert at_t2 is not None and at_t2.price_american == -150
    # Before any observation, nothing is known.
    assert repo.price_as_of(outcome.sb_outcome_id, "2026-07-22T17:00:00.000000Z") is None


def test_prices_in_range_is_chronological(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    _append(repo, outcome, raw, run_id, price=-150, observed_at=T1)
    _append(repo, outcome, raw, run_id, price=-160, observed_at=T2)
    window = repo.prices_in_range(outcome.sb_outcome_id, start=T0, end=T1)
    assert [s.price_american for s in window] == [-140, -150]


def test_point_key_distinguishes_lines(conn: sqlite3.Connection) -> None:
    assert point_key(None) == ""
    assert point_key(8.5) != point_key(9.5)
    # Two totals lines are two identities under the same market.
    repo = SqliteSportsbookRepository(conn)
    raw, _ = _raw(conn)
    event = repo.upsert_event(
        provider="the_odds_api",
        provider_event_id="evt-2",
        sport_key="baseball_mlb",
        commence_time=T2,
        home_team_raw="A",
        away_team_raw="B",
        raw_response_id=raw.raw_response_id,
        observed_at=T0,
    )
    market = repo.upsert_market(
        sb_event_id=event.sb_event_id,
        bookmaker_key="dk",
        market_key="totals",
        raw_response_id=raw.raw_response_id,
        observed_at=T0,
    )
    o1 = repo.upsert_outcome(
        sb_market_id=market.sb_market_id,
        outcome_name="over",
        provider_outcome_name="Over",
        outcome_role="over",
        point=8.5,
    )
    o2 = repo.upsert_outcome(
        sb_market_id=market.sb_market_id,
        outcome_name="over",
        provider_outcome_name="Over",
        outcome_role="over",
        point=9.5,
    )
    assert o1.sb_outcome_id != o2.sb_outcome_id
    assert repo.count_outcomes() == 2


def test_malformed_american_price_is_rejected_by_the_schema(conn: sqlite3.Connection) -> None:
    """A magnitude-<100 American price cannot be stored.

    A plain INSERT (not the idempotent ``INSERT OR IGNORE`` the repository uses)
    surfaces the CHECK, proving the storage layer refuses malformed odds even if
    a caller's own validation were bypassed.
    """

    repo, outcome, raw, run_id = _outcome(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sportsbook_price_snapshots "
            "(snapshot_id, sb_outcome_id, price_american, observed_at, ingested_at, "
            " raw_response_id, raw_response_hash, run_id, content_hash, created_at) "
            "VALUES ('sbp_bad', ?, 50, ?, ?, ?, ?, ?, 'ch', ?)",
            (outcome.sb_outcome_id, T0, T0, raw.raw_response_id, raw.content_hash, run_id, T0),
        )


# --------------------------------------------------------------------------- #
# Stale-metadata protection (issue 3)
# --------------------------------------------------------------------------- #
def _event(repo, raw, *, provider_event_id, commence_time, home, away, observed_at):
    return repo.upsert_event(
        provider="the_odds_api",
        provider_event_id=provider_event_id,
        sport_key="baseball_mlb",
        commence_time=commence_time,
        home_team_raw=home,
        away_team_raw=away,
        raw_response_id=raw.raw_response_id,
        observed_at=observed_at,
        league_id="lg_mlb",
    )


def test_newer_observation_becomes_current_event_metadata(conn: sqlite3.Connection) -> None:
    repo = SqliteSportsbookRepository(conn)
    raw, _ = _raw(conn)
    _event(repo, raw, provider_event_id="e1", commence_time=T1,
           home="New York Yankees", away="Boston Red Sox", observed_at=T0)
    # A newer observation moves the game and re-labels the away team.
    updated = _event(repo, raw, provider_event_id="e1", commence_time=T2,
                     home="New York Yankees", away="Boston Red Sox (DH)", observed_at=T2)
    assert updated.commence_time == T2
    assert updated.away_team_raw == "Boston Red Sox (DH)"
    assert updated.last_observed_at == T2


def test_older_event_backfill_does_not_regress_current_metadata(conn: sqlite3.Connection) -> None:
    repo = SqliteSportsbookRepository(conn)
    raw, _ = _raw(conn)
    # Newest known first.
    _event(repo, raw, provider_event_id="e1", commence_time=T2,
           home="New York Yankees", away="Boston Red Sox", observed_at=T2)
    # An older backfill arrives with a stale commence time and team text.
    after = _event(repo, raw, provider_event_id="e1", commence_time=T0,
                   home="NY Yankees", away="Bosox", observed_at=T0)
    # Current metadata is unchanged -- the backfill did not regress it.
    assert after.commence_time == T2
    assert after.home_team_raw == "New York Yankees"
    assert after.away_team_raw == "Boston Red Sox"
    assert after.last_observed_at == T2


def test_older_market_backfill_does_not_regress_current_metadata(conn: sqlite3.Connection) -> None:
    repo = SqliteSportsbookRepository(conn)
    raw, _ = _raw(conn)
    ev = _event(repo, raw, provider_event_id="e1", commence_time=T2,
                home="A", away="B", observed_at=T0)
    repo.upsert_market(
        sb_event_id=ev.sb_event_id, bookmaker_key="dk", market_key="h2h",
        raw_response_id=raw.raw_response_id, observed_at=T2,
        bookmaker_title="DraftKings", bookmaker_last_update=T2, market_last_update=T2,
    )
    # Older backfill with stale provider update times.
    after = repo.upsert_market(
        sb_event_id=ev.sb_event_id, bookmaker_key="dk", market_key="h2h",
        raw_response_id=raw.raw_response_id, observed_at=T0,
        bookmaker_title="DK", bookmaker_last_update=T0, market_last_update=T0,
    )
    assert after.bookmaker_title == "DraftKings"
    assert after.bookmaker_last_update == T2
    assert after.market_last_update == T2
    assert after.last_observed_at == T2


def test_older_backfill_snapshots_are_still_preserved(conn: sqlite3.Connection) -> None:
    """Metadata does not regress, but the backfilled price is still stored."""

    repo, outcome, raw, run_id = _outcome(conn)
    _append(repo, outcome, raw, run_id, price=-150, observed_at=T2)
    _snap, inserted = _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)
    assert inserted is True
    assert repo.count_snapshots() == 2
    # As-of at T0 sees the backfilled price; current metadata unaffected.
    at_t0 = repo.price_as_of(outcome.sb_outcome_id, T0)
    assert at_t0 is not None and at_t0.price_american == -140


def test_equal_observed_at_event_metadata_is_deterministic(conn: sqlite3.Connection) -> None:
    """On an equal observed_at the first-recorded metadata is retained."""

    repo = SqliteSportsbookRepository(conn)
    raw, _ = _raw(conn)
    _event(repo, raw, provider_event_id="e1", commence_time=T1,
           home="New York Yankees", away="Boston Red Sox", observed_at=T1)
    # A second observation at the SAME observed_at with different metadata.
    after = _event(repo, raw, provider_event_id="e1", commence_time=T2,
                   home="New York Yankees", away="Different Text", observed_at=T1)
    # Deterministic tie-break: the earlier-recorded value wins, not the later.
    assert after.commence_time == T1
    assert after.away_team_raw == "Boston Red Sox"


# --------------------------------------------------------------------------- #
# Transition-aware price deduplication (issue 4)
# --------------------------------------------------------------------------- #
def test_unchanged_consecutive_repeat_collapses(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _snap, first = _append(repo, outcome, raw, run_id, price=-110, observed_at=T0)
    _snap2, second = _append(repo, outcome, raw, run_id, price=-110, observed_at=T1)
    assert first is True
    assert second is False  # unchanged from its predecessor
    assert repo.count_snapshots() == 1


def test_price_reversal_keeps_all_three_transitions(conn: sqlite3.Connection) -> None:
    """-110 -> -120 -> -110 with NO provider timestamps must keep all three."""

    repo, outcome, raw, run_id = _outcome(conn)
    assert _append(repo, outcome, raw, run_id, price=-110, observed_at=T0)[1] is True
    assert _append(repo, outcome, raw, run_id, price=-120, observed_at=T1)[1] is True
    assert _append(repo, outcome, raw, run_id, price=-110, observed_at=T2)[1] is True
    assert repo.count_snapshots() == 3
    prices = [s.price_american for s in repo.list_snapshots_for_outcome(outcome.sb_outcome_id)]
    assert prices == [-110, -120, -110]


def test_exact_replay_of_reversal_is_idempotent(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _append(repo, outcome, raw, run_id, price=-110, observed_at=T0)
    _append(repo, outcome, raw, run_id, price=-120, observed_at=T1)
    _append(repo, outcome, raw, run_id, price=-110, observed_at=T2)
    # Replaying every observation writes nothing new.
    assert _append(repo, outcome, raw, run_id, price=-110, observed_at=T0)[1] is False
    assert _append(repo, outcome, raw, run_id, price=-120, observed_at=T1)[1] is False
    assert _append(repo, outcome, raw, run_id, price=-110, observed_at=T2)[1] is False
    assert repo.count_snapshots() == 3


def test_repeated_backfill_is_idempotent(conn: sqlite3.Connection) -> None:
    repo, outcome, raw, run_id = _outcome(conn)
    _append(repo, outcome, raw, run_id, price=-150, observed_at=T2)
    # First backfill of a genuinely different earlier price appends.
    assert _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)[1] is True
    # Re-running the same backfill does not duplicate.
    assert _append(repo, outcome, raw, run_id, price=-140, observed_at=T0)[1] is False
    assert repo.count_snapshots() == 2


def test_equal_observed_at_price_tie_break_is_idempotent(conn: sqlite3.Connection) -> None:
    """Two observations at the same observed_at: same content collapses,
    different content coexists, and re-applying either is idempotent."""

    repo, outcome, raw, run_id = _outcome(conn)
    assert _append(repo, outcome, raw, run_id, price=-110, observed_at=T1)[1] is True
    # Different price at the SAME observed_at is a distinct content -> stored.
    assert _append(repo, outcome, raw, run_id, price=-120, observed_at=T1)[1] is True
    # Re-applying either exact observation is a no-op.
    assert _append(repo, outcome, raw, run_id, price=-110, observed_at=T1)[1] is False
    assert _append(repo, outcome, raw, run_id, price=-120, observed_at=T1)[1] is False
    assert repo.count_snapshots() == 2
    # The as-of answer at T1 is deterministic (ties break by snapshot_id).
    a = repo.price_as_of(outcome.sb_outcome_id, T1)
    b = repo.price_as_of(outcome.sb_outcome_id, T1)
    assert a is not None and b is not None and a.snapshot_id == b.snapshot_id
