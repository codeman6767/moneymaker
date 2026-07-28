"""Phase E1 cutoff + as-of accessor tests (tasks §4, §5, §7, §12)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sports_quant.pit import AsOfReader, Cutoff, latest_as_of, read_only_connection
from sports_quant.pit.registry import ForbiddenJoinError

from .conftest import (
    CUTOFF,
    T1,
    T2,
    Ctx,
    link_sb_event,
    seed_dq,
    seed_injury,
    seed_kalshi_linked,
    seed_lineup,
    seed_player_stat,
    seed_price,
    seed_result,
    seed_sb_outcome_ctx,
    seed_status,
    seed_team_stat,
    seed_weather,
)


def _reader(conn: sqlite3.Connection, at: str = CUTOFF) -> AsOfReader:
    return AsOfReader(conn, Cutoff.parse(at))


# --------------------------------------------------------------------------- #
# §4 cutoff type
# --------------------------------------------------------------------------- #
def test_cutoff_requires_and_canonicalizes_utc() -> None:
    c = Cutoff.parse("2026-07-12T00:00:00.000000Z")
    assert c.iso == "2026-07-12T00:00:00.000000Z"
    # An explicit offset is accepted and normalized to the canonical UTC form.
    assert Cutoff.parse("2026-07-12T02:00:00+02:00").iso == "2026-07-12T00:00:00.000000Z"
    assert Cutoff.from_datetime(datetime(2026, 7, 12, tzinfo=timezone.utc)).iso == c.iso


def test_cutoff_rejects_naive_and_invalid() -> None:
    for bad in ("2026-07-12T00:00:00", "2026-07-12", "not-a-date", ""):
        with pytest.raises(ValueError):
            Cutoff.parse(bad)
    with pytest.raises(ValueError):
        Cutoff.from_datetime(datetime(2026, 7, 12))  # naive


def test_cutoff_equality_is_deterministic() -> None:
    assert Cutoff.parse("2026-07-12T00:00:00.000000Z") == Cutoff.parse("2026-07-12T02:00:00+02:00")
    assert Cutoff.parse("2026-07-12T00:00:00.000000Z") != Cutoff.parse("2026-07-12T00:00:01.000000Z")


# --------------------------------------------------------------------------- #
# §5 canonical latest-as-of
# --------------------------------------------------------------------------- #
def test_latest_observation_before_cutoff(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=3)
    obs = _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and obs.get("runs") == 3 and obs.observed_at == T1


def test_future_observation_excluded(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=3)
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T2,
                   runs=9)  # after cutoff
    obs = _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and obs.get("runs") == 3  # T2 (runs=9) is invisible


def test_equal_timestamp_deterministic_tie(conn: sqlite3.Connection, ctx: Ctx) -> None:
    # Two observations at the SAME observed_at; the stable id (ULID) breaks the tie
    # deterministically -> the later-inserted (higher id) wins, repeatably.
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=3)
    seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                   runs=7)
    obs = _reader(conn).team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and obs.get("runs") == 7


def test_latest_as_of_fails_closed_on_non_asof_table(conn: sqlite3.Connection) -> None:
    with pytest.raises(ForbiddenJoinError):
        latest_as_of(conn, table="sportsbook_events", cutoff=Cutoff.parse(CUTOFF),
                     anchor_where="sb_event_id = ?", anchor_params=("x",))


# --------------------------------------------------------------------------- #
# §12 read-only connection
# --------------------------------------------------------------------------- #
def test_read_only_connection_blocks_writes(db_path: Path) -> None:
    with read_only_connection(db_path) as ro:
        assert ro.execute("SELECT COUNT(*) FROM teams").fetchone()[0] >= 0
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO leagues (league_id, code, name, created_at, updated_at) "
                       "VALUES ('x','x','x','t','t')")


# --------------------------------------------------------------------------- #
# §7 game state / results
# --------------------------------------------------------------------------- #
def test_game_status_as_of(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_status(conn, game_id=ctx.game_id, status="scheduled", observed_at=T1)
    seed_status(conn, game_id=ctx.game_id, status="final", observed_at=T2)  # after cutoff
    rec = _reader(conn).game_status(ctx.game_id)
    assert rec is not None and rec.status == "scheduled"  # 'final' is post-cutoff


def test_result_excluded_pregame_then_available_as_label(conn: sqlite3.Connection,
                                                         ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    assert _reader(conn, CUTOFF).game_result(ctx.game_ref_id) is None  # pregame: invisible
    later = _reader(conn, T2).game_result(ctx.game_ref_id)  # label horizon: visible
    assert later is not None and later.get("winning_side") == "home"


def test_midseason_stats_exclude_future(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_player_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                     player_id=ctx.player_id, observed_at=T1, hits=1)
    seed_player_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                     player_id=ctx.player_id, observed_at=T2, hits=4)
    obs = _reader(conn).player_game_statistics(ctx.game_ref_id, ctx.player_id)
    assert obs is not None and '"hits": 1' in str(obs.get("batting_stats"))


def test_lineup_publication_boundary(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_lineup(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T1,
                is_confirmed=False)
    seed_lineup(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id, observed_at=T2,
                is_confirmed=True)  # confirmed after cutoff
    obs = _reader(conn).lineup(ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and int(obs.get("is_confirmed")) == 0  # only the projected one


def test_injury_published_before_observed_after_is_invisible(conn: sqlite3.Connection,
                                                             ctx: Ctx) -> None:
    # published_at BEFORE cutoff but observed_at AFTER cutoff -> still invisible.
    seed_injury(conn, player_ref_id=ctx.player_ref_id, team_id=ctx.home_team_id,
                player_id=ctx.player_id, game_ref_id=ctx.game_ref_id, observed_at=T2,
                published_at=T1, status="out")
    assert _reader(conn).injury(ctx.player_ref_id) is None


# --------------------------------------------------------------------------- #
# §7 sportsbook / kalshi
# --------------------------------------------------------------------------- #
def test_sportsbook_readiness_as_of(conn: sqlite3.Connection, ctx: Ctx) -> None:
    outcome_id = seed_sb_outcome_ctx(conn)
    seed_price(conn, sb_outcome_id=outcome_id, price_american=-120, observed_at=T1)
    # Link this event to the game with a decision decided in the future relative to
    # an early cutoff.
    link_sb_event(conn, sb_event_id=_sb_event_id(conn), game_id=ctx.game_id, orientation="direct")
    early = AsOfReader(conn, Cutoff.parse("2000-01-01T00:00:00.000000Z"))
    assert early.sportsbook_event_game(_sb_event_id(conn)) is None  # decision is in the future
    now_reader = AsOfReader(conn, Cutoff.parse("2999-01-01T00:00:00.000000Z"))
    link = now_reader.sportsbook_event_game(_sb_event_id(conn))
    assert link is not None and link.game_id == ctx.game_id
    # Price as-of is available at T1.
    assert now_reader.sportsbook_price(outcome_id) is not None


def test_sportsbook_neutral_swapped_excluded(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_sb_outcome_ctx(conn)
    link_sb_event(conn, sb_event_id=_sb_event_id(conn), game_id=ctx.game_id, orientation="swapped",
                  needs_review=True)
    reader = AsOfReader(conn, Cutoff.parse("2999-01-01T00:00:00.000000Z"))
    assert reader.sportsbook_event_game(_sb_event_id(conn)) is None  # never approved


def test_kalshi_orientation_as_of(conn: sqlite3.Connection, ctx: Ctx) -> None:
    kmk = seed_kalshi_linked(conn, game_id=ctx.game_id, yes_team_id=ctx.home_team_id)
    reader = AsOfReader(conn, Cutoff.parse("2999-01-01T00:00:00.000000Z"))
    link = reader.kalshi_market_orientation(kmk)
    assert link is not None and link.game_id == ctx.game_id
    assert link.as_dict()["yes_team_id"] == ctx.home_team_id


def test_kalshi_later_rules_conflict_no_backward_leak(conn: sqlite3.Connection, ctx: Ctx) -> None:
    kmk = seed_kalshi_linked(conn, game_id=ctx.game_id, yes_team_id=ctx.home_team_id)
    # The accepted decision is stamped ~now; choose cutoffs after it so this test
    # isolates the DQ timeline. A blocking rules conflict detected AFTER the earlier
    # cutoff must not block it, but must block once active.
    dq_detected = "2030-01-01T00:00:00.000000Z"
    seed_dq(conn, rule_code="DQ-MATCH-004", entity_type="kalshi_market", entity_id=kmk,
            severity="blocking", detected_at=dq_detected, provider="kalshi_public")
    before = AsOfReader(conn, Cutoff.parse("2029-01-01T00:00:00.000000Z"))
    after = AsOfReader(conn, Cutoff.parse("2030-06-01T00:00:00.000000Z"))
    assert before.kalshi_market_orientation(kmk) is not None   # conflict not yet detected
    assert after.kalshi_market_orientation(kmk) is None        # conflict active


# --------------------------------------------------------------------------- #
# §7 match decisions
# --------------------------------------------------------------------------- #
def test_future_match_decision_hidden(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_sb_outcome_ctx(conn)
    ev = _sb_event_id(conn)
    link_sb_event(conn, sb_event_id=ev, game_id=ctx.game_id)  # decided ~now
    early = AsOfReader(conn, Cutoff.parse("2000-01-01T00:00:00.000000Z"))
    assert early.accepted_decision(source_provider="the_odds_api", source_ref=ev,
                                   entity_type="sportsbook_event") is None
    later = AsOfReader(conn, Cutoff.parse("2999-01-01T00:00:00.000000Z"))
    assert later.accepted_decision(source_provider="the_odds_api", source_ref=ev,
                                   entity_type="sportsbook_event") is not None


def test_later_manual_review_hidden(conn: sqlite3.Connection, ctx: Ctx) -> None:
    from sports_quant.db.engine import transaction
    from sports_quant.db.repositories.matching import SqliteMatchingRepository
    seed_sb_outcome_ctx(conn)
    ev = _sb_event_id(conn)
    dec_id = link_sb_event(conn, sb_event_id=ev, game_id=ctx.game_id)
    with transaction(conn):
        SqliteMatchingRepository(conn).mark_reviewed(dec_id, reviewed_by="alice")
    reader = AsOfReader(conn, Cutoff.parse("2999-01-01T00:00:00.000000Z"))
    view = reader.accepted_decision(source_provider="the_odds_api", source_ref=ev,
                                    entity_type="sportsbook_event")
    assert view is not None
    # The review reviewed_at is ~now (< the 2999 cutoff), so it IS visible here...
    assert view.review_completed_by_cutoff and view.reviewed_by == "alice"
    # ...but an EARLIER cutoff (before the review) must not show it.
    early = AsOfReader(conn, Cutoff.parse("2000-01-01T00:00:00.000000Z"))
    early_view = early.decisions(source_provider="the_odds_api", source_ref=ev,
                                 entity_type="sportsbook_event")
    # (the decision itself is future to 2000, so nothing is returned)
    assert early_view == []


# --------------------------------------------------------------------------- #
# §7 weather
# --------------------------------------------------------------------------- #
def test_weather_current_forecast_accepted(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="current_forecast", observed_at=T1, pit_eligible=True)
    obs = _reader(conn).weather_pregame_forecast(ctx.game_ref_id, forecast_mode="point")
    assert obs is not None and obs.get("weather_kind") == "current_forecast"


@pytest.mark.parametrize("kind,pit", [
    ("station_observation", True),
    ("reanalysis", True),
    ("historical_forecast", None),  # pit_eligible unknown
])
def test_weather_unsafe_kinds_rejected(conn: sqlite3.Connection, ctx: Ctx, kind: str,
                                       pit: object) -> None:
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id, weather_kind=kind,
                 observed_at=T1, pit_eligible=pit)  # type: ignore[arg-type]
    assert _reader(conn).weather_pregame_forecast(ctx.game_ref_id, forecast_mode="point") is None


def test_weather_forecast_after_cutoff_rejected(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="current_forecast", observed_at=T2, pit_eligible=True)  # future
    assert _reader(conn).weather_pregame_forecast(ctx.game_ref_id, forecast_mode="point") is None


def test_data_quality_active_at_cutoff(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_dq(conn, rule_code="DQ-MATCH-003", entity_type="game", entity_id=ctx.game_id,
            severity="blocking", detected_at=T1, resolved_at=T2)
    # Active at CUTOFF (detected T1, resolved T2>cutoff); inactive well after T2.
    assert _reader(conn, CUTOFF).active_data_quality(entity_id=ctx.game_id)
    assert _reader(conn, "2999-01-01T00:00:00.000000Z").active_data_quality(
        entity_id=ctx.game_id) == []


def _sb_event_id(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT sb_event_id FROM sportsbook_events LIMIT 1").fetchone()[0])
