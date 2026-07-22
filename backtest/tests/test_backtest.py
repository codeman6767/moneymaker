"""Tests for event-replay and latency backtesting (Module 7)."""

from __future__ import annotations

import numpy as np
import pytest

from evaluation.decision import OrderBookView

from backtest import (
    BacktestConfig,
    EdgeStrategy,
    EventType,
    FillOutcome,
    LatencyModel,
    ReplayBacktester,
    ReplayEvent,
    break_even_latency,
    grade_dataset,
    resolve_fill,
)

MS = 1_000_000


def ev(eid, etype, market="KX1", t_ms=None, **kw):
    return ReplayEvent(
        event_id=eid, event_type=etype, market=market,
        event_time_ns=(None if t_ms is None else t_ms * MS), **kw,
    )


def scenario():
    """One market exercising every event type and all fill outcomes across
    the latency sweep (fill, partial-region, miss, suspension, stale-free)."""

    return [
        # Market data.
        ev("s0", EventType.OB_SNAPSHOT, t_ms=0, publish_time_ns=0,
           yes_ask_ladder=((60, 50),), no_ask_ladder=((50, 50),)),
        ev("d1", EventType.OB_DELTA, t_ms=100, yes_ask_ladder=((62, 40),)),
        ev("d2", EventType.OB_DELTA, t_ms=300, yes_ask_ladder=((64, 30),)),
        ev("d3", EventType.OB_DELTA, t_ms=600, yes_ask_ladder=((70, 50),)),
        ev("tr", EventType.TRADE, t_ms=120, trade_side="yes", trade_price=61),
        ev("st", EventType.MARKET_STATUS, t_ms=1000, market_status="paused"),
        # Non-decisioning triggers (no fair_prob) covering the remaining types.
        ev("sp", EventType.SPORTS, t_ms=1),
        ev("gc", EventType.GAME_CLOCK, t_ms=2),
        ev("inj", EventType.INJURY, t_ms=3),
        ev("lin", EventType.LINEUP, t_ms=4),
        ev("cor", EventType.CORRECTION, t_ms=5, is_correction=True),
        # The one decisioning trigger.
        ev("odds", EventType.ODDS, t_ms=10, payload={"fair_prob": 0.63}),
    ]


def run_scenario():
    bt = ReplayBacktester(
        scenario(),
        EdgeStrategy(min_edge_cents=1.0, base_size=20, max_slip_ticks=5),
        LatencyModel(),
        BacktestConfig(),
    )
    return bt.run()


# --------------------------------------------------------------------------- #
# Full report + latency curves
# --------------------------------------------------------------------------- #
def test_report_is_execution_valid_and_graded():
    report = run_scenario()
    assert report.data_quality.execution_valid is True
    assert report.data_quality.grade in {"A", "B", "C"}
    assert report.summary().startswith("DATA QUALITY:")
    assert report.n_orders == 1
    # All 10 event types include 6 decision-trigger events.
    assert report.n_decision_events == 6


def test_profit_and_break_even_by_latency():
    m = run_scenario().latency_metrics
    # Profitable at zero latency; unprofitable once the price moves in transit.
    assert m.profit_by_latency[0] > 0
    assert min(m.profit_by_latency) < 0
    # Break-even latency lands between the 50ms and 100ms grid points.
    assert m.break_even_latency_ns is not None
    assert 50 * MS < m.break_even_latency_ns < 100 * MS


def test_fill_rate_and_edge_decay():
    m = run_scenario().latency_metrics
    assert m.fill_rate_by_latency[0] == 1.0
    assert m.fill_rate_by_latency[-1] == 0.0   # suspended at the largest latency
    # Edge decays as latency grows.
    assert m.edge_decay[0] > m.edge_decay[-1]


def test_decision_count_and_clv():
    m = run_scenario().latency_metrics
    assert m.decision_count_by_latency[0] == 1
    assert m.decision_count_by_latency[-1] == 0   # not actionable once suspended
    # CLV at zero latency: closing trade 61 minus fill 60.
    assert m.clv_by_latency[0] == pytest.approx(1.0)


def test_latency_distributions_present():
    m = run_scenario().latency_metrics
    assert m.provider_lag_dist["mean"] is not None and m.provider_lag_dist["mean"] > 0
    assert m.internal_latency_dist["mean"] is not None and m.internal_latency_dist["mean"] > 0


# --------------------------------------------------------------------------- #
# Data-quality grading (execution-validity gate)
# --------------------------------------------------------------------------- #
def test_missing_orderbook_timestamps_not_execution_valid():
    events = [
        ev("s0", EventType.OB_SNAPSHOT, t_ms=None, yes_ask_ladder=((60, 10),)),  # no timestamp
        ev("odds", EventType.ODDS, t_ms=10, payload={"fair_prob": 0.7}),
    ]
    dq = grade_dataset(events)
    assert dq.execution_valid is False
    assert dq.grade in {"D", "F"}
    assert any("order-book" in i or "timestamp" in i for i in dq.issues)

    report = ReplayBacktester(events, EdgeStrategy()).run()
    assert report.data_quality.execution_valid is False
    assert "NOT EXECUTION-VALID" in report.summary()


def test_missing_event_timestamps_not_execution_valid():
    events = [
        ev("s0", EventType.OB_SNAPSHOT, t_ms=0, yes_ask_ladder=((60, 10),)),
        ev("odds", EventType.ODDS, t_ms=None, payload={"fair_prob": 0.7}),  # untimed
    ]
    dq = grade_dataset(events)
    assert dq.execution_valid is False


def test_no_orderbook_data_not_execution_valid():
    events = [ev("odds", EventType.ODDS, t_ms=10, payload={"fair_prob": 0.7})]
    dq = grade_dataset(events)
    assert dq.execution_valid is False
    assert dq.grade == "F"


def test_full_quality_dataset_grades_a():
    events = [
        ev("s0", EventType.OB_SNAPSHOT, t_ms=0, publish_time_ns=0,
           yes_ask_ladder=((60, 50),), no_ask_ladder=((50, 50),)),
        ev("d1", EventType.OB_DELTA, t_ms=100, publish_time_ns=100 * MS, yes_ask_ladder=((61, 40),)),
        ev("tr", EventType.TRADE, t_ms=120, trade_side="yes", trade_price=61),
        ev("st", EventType.MARKET_STATUS, t_ms=0, market_status="open"),
    ]
    dq = grade_dataset(events)
    assert dq.execution_valid is True
    assert dq.grade == "A"


# --------------------------------------------------------------------------- #
# Fill-model unit scenarios
# --------------------------------------------------------------------------- #
def test_resolve_fill_outcomes():
    v_full = OrderBookView.make(yes_ask_ladder=((60, 50),))
    v_thin = OrderBookView.make(yes_ask_ladder=((60, 5),))
    v_far = OrderBookView.make(yes_ask_ladder=((70, 50),))
    v_empty = OrderBookView.make(yes_ask_ladder=())

    assert resolve_fill(side="yes", limit_price=61, size=20, view=v_full,
                        status_at_arrival="open", is_stale=False).outcome is FillOutcome.FILLED
    assert resolve_fill(side="yes", limit_price=61, size=20, view=v_thin,
                        status_at_arrival="open", is_stale=False).outcome is FillOutcome.PARTIAL
    assert resolve_fill(side="yes", limit_price=61, size=20, view=v_far,
                        status_at_arrival="open", is_stale=False).outcome is FillOutcome.MISS
    assert resolve_fill(side="yes", limit_price=61, size=20, view=v_empty,
                        status_at_arrival="open", is_stale=False).outcome is FillOutcome.NO_FILL
    assert resolve_fill(side="yes", limit_price=61, size=20, view=v_full,
                        status_at_arrival="paused", is_stale=False).outcome is FillOutcome.SUSPENDED
    stale = resolve_fill(side="yes", limit_price=61, size=20, view=v_full,
                         status_at_arrival="open", is_stale=True)
    assert stale.outcome is FillOutcome.STALE and stale.cancelled is True


# --------------------------------------------------------------------------- #
# Latency model + break-even helper
# --------------------------------------------------------------------------- #
def test_latency_sample_components_sum():
    model = LatencyModel()
    rng = np.random.default_rng(0)
    s = model.sample(rng)
    assert s.total_ns == s.provider_lag_ns + s.internal_ns + s.exchange_ns
    assert s.provider_lag_ns > 0 and s.internal_ns > 0 and s.exchange_ns > 0


def test_break_even_interpolation():
    assert break_even_latency([0, 100, 200], [5.0, -5.0, -9.0]) == pytest.approx(50.0)
    assert break_even_latency([0, 100], [5.0, 5.0]) is None  # never unprofitable
    assert break_even_latency([0, 100], [-1.0, -2.0]) == 0.0  # already unprofitable
