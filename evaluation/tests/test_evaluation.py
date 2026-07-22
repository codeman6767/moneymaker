"""Tests for event-driven market evaluation (Module 6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pytest

from evaluation import (
    MAX_PRICE_CENTS,
    MIN_PRICE_CENTS,
    VALID_SIDES,
    Action,
    EvaluationConfig,
    LimitOrder,
    MarketEvaluator,
    MarketEvent,
    MarketSnapshot,
    OrderBookView,
    Portfolio,
    PortfolioLimits,
    SubmitStatus,
    validate_trade,
    walk_ladder,
)

NOW = 1_000_000_000_000
T = datetime(2026, 7, 1, 19, 0, tzinfo=timezone.utc)


@dataclass
class StubPred:
    win_probability: float
    uncertainty_std: float = 0.01
    ood_flag: bool = False


class StubEngine:
    def __init__(self, prob: float, std: float = 0.01, ood: bool = False) -> None:
        self.prob, self.std, self.ood = prob, std, ood

    def predict_vector(self, x):
        return StubPred(self.prob, self.std, self.ood)


def make_event(market="KX1", seq=1, provider_lag_ns=1_000_000, material=None, eid="e1", etype="orderbook"):
    return MarketEvent(
        event_id=eid, market=market, event_type=etype, sequence=seq,
        event_time=T, received_monotonic_ns=NOW, provider_lag_ns=provider_lag_ns, material=material,
    )


def make_snapshot(
    market="KX1",
    gs_hash="gs1",
    market_status="open",
    game_seq_ok=True,
    book_seq_ok=True,
    game_age_ns=1_000,
    book_age_ns=1_000,
    yes=((70, 100),),
    no=((35, 100),),
):
    return MarketSnapshot(
        market=market,
        game_state_hash=gs_hash,
        market_status=market_status,
        game_sequence_ok=game_seq_ok,
        game_updated_monotonic_ns=NOW - game_age_ns,
        order_book_updated_monotonic_ns=NOW - book_age_ns,
        feature_vector=np.zeros(4, dtype=np.float32),
        book=OrderBookView.make(yes_ask_ladder=yes, no_ask_ladder=no, sequence_ok=book_seq_ok),
    )


def evaluator(prob=0.8, std=0.01, ood=False, portfolio=None, config=None):
    return MarketEvaluator(
        StubEngine(prob, std, ood),
        portfolio or Portfolio(),
        config or EvaluationConfig(),
    )


# --------------------------------------------------------------------------- #
# Happy path: BET, then a clean submission (limit order only)
# --------------------------------------------------------------------------- #
def test_bet_and_submit():
    ev = evaluator(prob=0.8)
    dec = ev.evaluate(make_event(), make_snapshot(), now_ns=NOW)
    assert dec.action is Action.BET
    assert dec.side == "yes"
    assert dec.limit_price == 71  # best ask 70 + 1 approved tick
    assert dec.size > 0
    # Hashes attached to the decision.
    assert dec.game_state_hash == "gs1"
    assert dec.order_book_hash == make_snapshot().order_book_hash
    # Complete latency trace recorded.
    assert dec.trace.total_ns is not None
    assert dec.trace.provider_lag_ns == 1_000_000
    assert any(name == "inference" for name, _ in dec.trace.stages)

    result = ev.submit(dec, make_snapshot(), now_ns=NOW)
    assert result.status is SubmitStatus.SUBMITTED
    assert isinstance(result.order, LimitOrder)
    assert result.order.order_type == "limit"
    assert result.order.limit_price == 71
    assert ev.portfolio.positions["KX1"] == result.order.size


# --------------------------------------------------------------------------- #
# 1. Price changes during inference -> revalidation rejects
# --------------------------------------------------------------------------- #
def test_price_changes_before_submission():
    ev = evaluator(prob=0.8)
    dec = ev.evaluate(make_event(), make_snapshot(), now_ns=NOW)
    assert dec.action is Action.BET
    # Book moved up beyond the approved limit before submission.
    moved = make_snapshot(yes=((85, 100),))
    result = ev.submit(dec, moved, now_ns=NOW)
    assert result.status is SubmitStatus.REJECTED_PRICE
    assert "KX1" not in ev.portfolio.positions  # nothing filled


def test_walk_ladder_never_sweeps_beyond_limit():
    ladder = ((70, 5), (72, 10), (80, 100))
    filled, avg = walk_ladder(ladder, size=50, limit_price=72)
    assert filled == 15  # only the 70 and 72 levels; never the 80 level
    assert avg == pytest.approx((70 * 5 + 72 * 10) / 15)


# --------------------------------------------------------------------------- #
# 2. Stale sports event / stale order book
# --------------------------------------------------------------------------- #
def test_stale_sports_event_skips():
    ev = evaluator()
    dec = ev.evaluate(make_event(), make_snapshot(game_age_ns=5_000_000_000), now_ns=NOW)
    assert dec.action is Action.SKIP
    assert "stale_game_state" in dec.reasons


def test_stale_order_book_skips():
    ev = evaluator()
    dec = ev.evaluate(make_event(), make_snapshot(book_age_ns=5_000_000_000), now_ns=NOW)
    assert dec.action is Action.SKIP
    assert "stale_order_book" in dec.reasons


def test_provider_lag_exceeded_skips():
    ev = evaluator()
    dec = ev.evaluate(make_event(provider_lag_ns=2_000_000_000), make_snapshot(), now_ns=NOW)
    assert dec.action is Action.SKIP
    assert "provider_lag_exceeded" in dec.reasons


# --------------------------------------------------------------------------- #
# 3. Sequence gap
# --------------------------------------------------------------------------- #
def test_sequence_gap_skips():
    ev = evaluator()
    dec = ev.evaluate(make_event(), make_snapshot(game_seq_ok=False), now_ns=NOW)
    assert dec.action is Action.SKIP
    assert "sequence_gap" in dec.reasons

    dec2 = ev.evaluate(make_event(eid="e2", material=True), make_snapshot(book_seq_ok=False), now_ns=NOW)
    assert dec2.action is Action.SKIP and "sequence_gap" in dec2.reasons


# --------------------------------------------------------------------------- #
# 4. Market pause -> SKIP, and risk-reducing cancels are prioritized
# --------------------------------------------------------------------------- #
def test_market_pause_skips_and_cancels_prioritized():
    portfolio = Portfolio()
    portfolio.register_open("o1", LimitOrder("KX1", "yes", 70, 10))
    ev = evaluator(portfolio=portfolio)
    dec = ev.evaluate(make_event(), make_snapshot(market_status="paused"), now_ns=NOW)
    assert dec.action is Action.SKIP
    assert "market_paused" in dec.reasons
    assert dec.cancels == ["o1"]
    # Submission runs the risk-reducing cancels first, even though it's a SKIP.
    result = ev.submit(dec, make_snapshot(market_status="paused"), now_ns=NOW)
    assert result.status is SubmitStatus.NO_OP
    assert result.cancels_executed == ["o1"]
    assert "o1" not in ev.portfolio.open_orders


# --------------------------------------------------------------------------- #
# 5. Lineup update during decision -> superseded (outdated state version)
# --------------------------------------------------------------------------- #
def test_lineup_update_during_decision_supersedes():
    ev = evaluator(prob=0.8)
    dec = ev.evaluate(make_event(eid="odds1"), make_snapshot(gs_hash="gs1"), now_ns=NOW)
    assert dec.action is Action.BET

    # A material lineup event arrives and is evaluated -> new state version.
    ev.evaluate(make_event(eid="lineup1", etype="injury", material=True), make_snapshot(gs_hash="gs2"), now_ns=NOW)
    assert ev.is_superseded(dec)

    result = ev.submit(dec, make_snapshot(gs_hash="gs2"), now_ns=NOW)
    assert result.status is SubmitStatus.REJECTED_SUPERSEDED


# --------------------------------------------------------------------------- #
# 6. Risk limit reached
# --------------------------------------------------------------------------- #
def test_risk_limit_reached_skips():
    portfolio = Portfolio(limits=PortfolioLimits(max_position_per_market=100))
    portfolio.positions["KX1"] = 100  # already at the per-market cap
    ev = evaluator(prob=0.8, portfolio=portfolio)
    dec = ev.evaluate(make_event(), make_snapshot(), now_ns=NOW)
    assert dec.action is Action.SKIP
    assert "risk_limit_reached" in dec.reasons


# --------------------------------------------------------------------------- #
# 7. Decision superseded by a newer event on the same market
# --------------------------------------------------------------------------- #
def test_decision_superseded_by_newer_event():
    ev = evaluator(prob=0.8)
    dec_a = ev.evaluate(make_event(eid="a"), make_snapshot(), now_ns=NOW)
    ev.evaluate(make_event(eid="b"), make_snapshot(), now_ns=NOW)
    result = ev.submit(dec_a, make_snapshot(), now_ns=NOW)
    assert result.status is SubmitStatus.REJECTED_SUPERSEDED


# --------------------------------------------------------------------------- #
# Debounce nonmaterial, OOD -> WATCH, no-edge -> SKIP
# --------------------------------------------------------------------------- #
def test_debounce_nonmaterial_event():
    ev = evaluator(prob=0.8)
    dec = ev.evaluate(make_event(material=False), make_snapshot(), now_ns=NOW)
    assert dec.action is Action.WATCH
    assert "debounced_nonmaterial" in dec.reasons


def test_out_of_distribution_watches():
    ev = evaluator(prob=0.8, ood=True)
    dec = ev.evaluate(make_event(), make_snapshot(), now_ns=NOW)
    assert dec.action is Action.WATCH
    assert "out_of_distribution" in dec.reasons


def test_no_edge_skips():
    ev = evaluator(prob=0.5)  # fair 50 vs asks at 70 -> negative edge both sides
    dec = ev.evaluate(make_event(), make_snapshot(yes=((70, 100),), no=((70, 100),)), now_ns=NOW)
    assert dec.action is Action.SKIP
    assert "no_edge" in dec.reasons


# --------------------------------------------------------------------------- #
# Latency budget
# --------------------------------------------------------------------------- #
def test_latency_trace_and_budget():
    ev = evaluator(prob=0.8)
    for i in range(500):
        ev.evaluate(make_event(eid=f"e{i}", material=True), make_snapshot(gs_hash=f"h{i}"), now_ns=NOW)
    snap = ev.latency_snapshot()
    assert snap.count >= 500
    assert snap.p50_ns is not None and snap.p95_ns is not None and snap.p99_ns is not None
    assert ev.within_budget()


# --------------------------------------------------------------------------- #
# Incomplete BET decisions are rejected, never coerced into a trade
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,value,expect_in_reason",
    [
        ("side", None, "side is not set"),
        ("side", "maybe", "invalid side"),
        ("limit_price", None, "limit_price is not set"),
        ("limit_price", 0, "outside the valid range"),
        ("limit_price", 100, "outside the valid range"),
        ("size", 0, "not greater than zero"),
        ("size", -5, "not greater than zero"),
    ],
)
def test_incomplete_bet_is_rejected(field, value, expect_in_reason):
    ev = evaluator(prob=0.8)
    dec = ev.evaluate(make_event(), make_snapshot(), now_ns=NOW)
    assert dec.action is Action.BET
    setattr(dec, field, value)

    result = ev.submit(dec, make_snapshot(), now_ns=NOW)

    assert result.status is SubmitStatus.REJECTED_INCOMPLETE
    assert result.order is None
    assert expect_in_reason in (result.reason or "")
    # Nothing was filled: an incomplete decision never reaches the portfolio.
    assert "KX1" not in ev.portfolio.positions


def test_validate_trade_accepts_a_complete_decision():
    ev = evaluator(prob=0.8)
    dec = ev.evaluate(make_event(), make_snapshot(), now_ns=NOW)
    trade, reason = validate_trade(dec)
    assert reason is None
    assert trade is not None
    assert trade.side in VALID_SIDES
    assert MIN_PRICE_CENTS <= trade.limit_price <= MAX_PRICE_CENTS
    assert trade.size > 0


def test_incomplete_bet_still_runs_risk_reducing_cancels():
    """A rejection must not strand risk-reducing cancels."""

    ev = evaluator(prob=0.8)
    dec = ev.evaluate(make_event(), make_snapshot(), now_ns=NOW)
    dec.limit_price = None
    dec.cancels = ["order-to-cancel"]

    result = ev.submit(dec, make_snapshot(), now_ns=NOW)

    assert result.status is SubmitStatus.REJECTED_INCOMPLETE
    assert result.cancels_executed == ["order-to-cancel"]
