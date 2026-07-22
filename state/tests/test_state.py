"""Tests for in-memory live state (Module 2)."""

from __future__ import annotations

from datetime import datetime, timezone

from streaming.event_envelope import EventEnvelope

from state import (
    ApplyStatus,
    DataQuality,
    LiveStateStore,
    MLBGameState,
    NBAGameState,
    OrderBookState,
)
from state.benchmark import benchmark_orderbook_deltas

BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def ob_event(event_type, sequence, **payload) -> EventEnvelope:
    return EventEnvelope.create(
        subject="kalshi.orderbook",
        provider="kalshi",
        event_type=event_type,
        event_time=BASE_TIME,
        sequence=sequence,
        payload=payload,
    )


# --------------------------------------------------------------------------- #
# MLB: complete half-inning event sequence
# --------------------------------------------------------------------------- #
def test_mlb_complete_sequence(mlb_events):
    meta, events = mlb_events
    store = LiveStateStore()
    for env in events:
        result = store.apply(env)
        assert result.applied, f"{env.event_type}#{env.sequence}: {result.status}"

    snap = store.snapshot(meta["game_id"])
    c = snap.content
    assert c["game_status"] == "live"
    assert c["inning"] == 1 and c["half"] == "top"
    assert c["outs"] == 2
    assert c["score"] == {"home": 0, "away": 1}
    assert c["batter"] == "a2" and c["pitcher"] == "p1"
    # 4 pitches to p1 (seq 5,6,7,10).
    assert c["pitch_count"] == 4
    assert c["total_pitches"] == 4
    assert c["bullpen_usage"]["p1"] == 4
    assert c["bases"]["1B"] == "a3"
    assert c["bases"]["2B"] is None
    assert c["batting_order"]["away"] == ("a1", "a2", "a3")
    assert snap.sequence == 13
    assert snap.data_quality == DataQuality.OK


def test_mlb_state_hash_is_deterministic(mlb_events):
    _, events = mlb_events

    def run():
        s = MLBGameState("mlb-123")
        for env in events:
            s.apply(env)
        return s.snapshot().state_hash

    assert run() == run()


def test_mlb_snapshot_is_immutable(mlb_events):
    _, events = mlb_events
    s = MLBGameState("mlb-123")
    for env in events:
        s.apply(env)
    snap = s.snapshot()
    # Applying more events must not mutate an already-taken snapshot.
    hash_before = snap.state_hash
    s.apply(
        EventEnvelope.create(
            subject="sports.mlb.events", provider="mlbprovider",
            event_type="out", event_time=BASE_TIME, sequence=14, payload={"outs": 1},
        )
    )
    assert snap.state_hash == hash_before
    assert s.snapshot().state_hash != hash_before


# --------------------------------------------------------------------------- #
# NBA: complete event sequence
# --------------------------------------------------------------------------- #
def test_nba_complete_sequence(nba_events):
    meta, events = nba_events
    store = LiveStateStore()
    for env in events:
        assert store.apply(env).applied

    snap = store.snapshot(meta["game_id"])
    c = snap.content
    assert c["game_status"] == "live"
    assert c["period"] == 1
    assert c["clock_seconds"] == 650.0
    assert c["score"] == {"home": 2, "away": 0}
    assert c["possession"] == "away"
    # team_foul (home) + player_foul (h1, home) => home 2.
    assert c["team_fouls"] == {"home": 2, "away": 0}
    assert c["timeouts"] == {"home": 7, "away": 6}
    assert len(c["players_on_court"]["home"]) == 5
    assert len(c["players_on_court"]["away"]) == 5
    assert c["player_fouls"]["h1"] == 1
    assert c["player_minutes"]["h1"] == 5.5
    assert snap.sequence == 12
    assert snap.data_quality == DataQuality.OK


# --------------------------------------------------------------------------- #
# Kalshi: snapshot + delta stream
# --------------------------------------------------------------------------- #
def test_kalshi_snapshot_plus_deltas(kalshi_events):
    meta, events = kalshi_events
    book = OrderBookState(meta["market"])
    for env in events:
        assert book.apply(env).applied

    snap = book.snapshot()
    c = snap.content
    assert c["levels"]["yes"] == {"41": 50, "42": 70}
    assert c["levels"]["no"] == {"54": 120, "55": 80}
    assert c["best_yes_bid"] == 42
    assert c["best_no_bid"] == 55
    # executable Yes ask = 100 - best No bid; No ask = 100 - best Yes bid.
    assert c["executable_yes_ask"] == 45
    assert c["executable_no_ask"] == 58
    assert snap.last_snapshot_sequence == 1
    assert snap.last_delta_sequence == 5
    assert snap.sequence == 5
    assert snap.data_quality == DataQuality.OK


def test_orderbook_requires_snapshot_first():
    book = OrderBookState("KX1")
    # A delta before any snapshot cannot be trusted.
    result = book.apply(ob_event("delta", 1, market="KX1", side="yes", price=40, quantity=10))
    assert result.status is ApplyStatus.GAP_DETECTED
    assert result.needs_snapshot is True
    assert book.snapshot().data_quality & DataQuality.SEQUENCE_GAP


# --------------------------------------------------------------------------- #
# Sequence-gap detection + snapshot recovery
# --------------------------------------------------------------------------- #
def test_gap_detection_and_snapshot_recovery():
    book = OrderBookState("KX2")
    assert book.apply(ob_event("snapshot", 1, market="KX2", yes=[[40, 100]], no=[[55, 100]])).applied

    # Skip sequence 2 -> gap on 3.
    gap = book.apply(ob_event("delta", 3, market="KX2", side="yes", price=41, quantity=5))
    assert gap.status is ApplyStatus.GAP_DETECTED
    assert gap.needs_snapshot is True
    assert gap.missing_from == 2 and gap.missing_to == 2
    snap = book.snapshot()
    assert snap.awaiting_snapshot is True
    assert snap.data_quality & DataQuality.SEQUENCE_GAP
    # The gapped delta was NOT applied.
    assert snap.content["levels"]["yes"] == {"40": 100}

    # A fresh snapshot recovers the stream and clears the gap.
    rec = book.apply(ob_event("snapshot", 10, market="KX2", yes=[[42, 200]], no=[[58, 200]]))
    assert rec.status is ApplyStatus.SNAPSHOT_APPLIED
    snap2 = book.snapshot()
    assert snap2.awaiting_snapshot is False
    assert snap2.data_quality == DataQuality.OK
    assert snap2.content["levels"]["yes"] == {"42": 200}


def test_duplicate_sequence_is_ignored():
    book = OrderBookState("KX3")
    book.apply(ob_event("snapshot", 1, market="KX3", yes=[[40, 100]], no=[[55, 100]]))
    book.apply(ob_event("delta", 2, market="KX3", side="yes", price=41, quantity=5))
    dup = book.apply(ob_event("delta", 2, market="KX3", side="yes", price=41, quantity=999))
    assert dup.status is ApplyStatus.DUPLICATE
    # The duplicate did not overwrite the quantity.
    assert book.snapshot().content["levels"]["yes"]["41"] == 5


# --------------------------------------------------------------------------- #
# Correction handling
# --------------------------------------------------------------------------- #
def test_correction_restates_state():
    game = NBAGameState("nba-c")
    game.apply(
        EventEnvelope.create(
            subject="sports.nba.events", provider="nbaprovider",
            event_type="score", event_time=BASE_TIME, sequence=1,
            payload={"team": "home", "points": 3},
        )
    )
    assert game.snapshot().content["score"]["home"] == 3

    # A correction restates the absolute score (the 3 was actually a 2).
    corr = game.apply(
        EventEnvelope.create(
            subject="sports.nba.events", provider="nbaprovider",
            event_type="correction", event_time=BASE_TIME, sequence=1,
            payload={"score": {"home": 2}}, is_correction=True,
        )
    )
    assert corr.status is ApplyStatus.CORRECTION_APPLIED
    snap = game.snapshot()
    assert snap.content["score"]["home"] == 2
    assert snap.correction_count == 1


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #
def test_staleness_flag():
    book = OrderBookState("KX4")
    book.apply(ob_event("snapshot", 1, market="KX4", yes=[[40, 100]], no=[[55, 100]]))
    updated = book.last_update_monotonic_ns
    # Ask for a snapshot far in the future relative to a tiny max-age.
    snap = book.snapshot(now_monotonic_ns=updated + 10_000_000_000, staleness_max_age_ns=1_000_000)
    assert snap.data_quality & DataQuality.STALE
    # And not stale when within the window.
    fresh = book.snapshot(now_monotonic_ns=updated + 500_000, staleness_max_age_ns=1_000_000)
    assert not (fresh.data_quality & DataQuality.STALE)


# --------------------------------------------------------------------------- #
# Best-price recompute when the top level is removed
# --------------------------------------------------------------------------- #
def test_best_recompute_on_removal():
    book = OrderBookState("KX5")
    book.apply(ob_event("snapshot", 1, market="KX5", yes=[[40, 10], [45, 10], [48, 10]], no=[[55, 10]]))
    assert book.best_yes_bid == 48
    # Remove the current best -> must fall back to the next best (45).
    book.apply(ob_event("delta", 2, market="KX5", side="yes", price=48, quantity=0))
    assert book.best_yes_bid == 45
    assert book.snapshot().content["executable_no_ask"] == 55  # 100 - 45


# --------------------------------------------------------------------------- #
# Benchmark harness runs and reports percentiles
# --------------------------------------------------------------------------- #
def test_benchmark_runs_and_reports_percentiles():
    result = benchmark_orderbook_deltas(events=5_000)
    assert result.events == 5_000
    assert result.events_per_second > 0
    assert result.p50_ns <= result.p95_ns <= result.p99_ns <= result.max_ns
