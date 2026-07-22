"""Event-driven market evaluator.

On each relevant event it runs the full pipeline -- verify sequence, verify
freshness, update features, infer, read the book, price, fee, slippage, reserves,
portfolio limits -- and returns BET / WATCH / SKIP with a complete latency trace
and the game-state and order-book hashes attached.

Orchestration guarantees:

* nonmaterial events are debounced (WATCH, no recompute);
* every evaluation bumps a per-market token, so a decision from an older event is
  *superseded* the moment a newer event is evaluated;
* submission revalidates the state version and re-prices immediately before
  placing the order, never sweeping beyond the approved limit, and only ever
  uses limit orders;
* risk-reducing cancels run first, even when the new order is blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from streaming.latency import LatencyRegistry, monotonic_ns

from .decision import (
    Action,
    Decision,
    LimitOrder,
    MarketEvent,
    MarketSnapshot,
    SubmissionResult,
    SubmitStatus,
    validate_trade,
)
from .latency_trace import LatencyTrace
from .portfolio import Portfolio
from .pricing import (
    FeeModel,
    adverse_reserve_cents,
    quote_side,
    uncertainty_reserve_cents,
    walk_ladder,
)


@dataclass
class EvaluationConfig:
    max_provider_lag_ns: int = 500_000_000       # 0.5 s
    max_state_age_ns: int = 2_000_000_000        # 2 s
    max_slip_ticks: int = 1                        # approved ceiling above best ask
    base_size: int = 20
    min_edge_cents: float = 2.0
    watch_margin_cents: float = 1.0               # within this below min_edge => WATCH
    uncertainty_reserve_coeff: float = 0.5
    adverse_reserve_coeff: float = 2.0
    fee_coeff: float = 0.07
    decision_budget_ns: int = 2_000_000           # 2 ms


class MarketEvaluator:
    def __init__(
        self,
        engine,
        portfolio: Portfolio,
        config: Optional[EvaluationConfig] = None,
        latency: Optional[LatencyRegistry] = None,
    ) -> None:
        self.engine = engine
        self.portfolio = portfolio
        self.config = config or EvaluationConfig()
        self.fee_model = FeeModel(coeff=self.config.fee_coeff)
        self.latency = latency or LatencyRegistry()
        self._token: Dict[str, int] = {}
        self._last_hashes: Dict[str, Tuple[str, str]] = {}
        self._current: Dict[str, Decision] = {}

    # -- Public API -----------------------------------------------------------
    def evaluate(self, event: MarketEvent, snapshot: MarketSnapshot, *, now_ns: Optional[int] = None) -> Decision:
        now = now_ns if now_ns is not None else monotonic_ns()
        token = self._bump_token(event.market)
        trace = LatencyTrace(event.event_id, provider_lag_ns=event.provider_lag_ns)
        decision = Decision(
            action=Action.SKIP,
            market=event.market,
            event_id=event.event_id,
            state_token=token,
            game_state_hash=snapshot.game_state_hash,
            order_book_hash=snapshot.order_book_hash,
            trace=trace,
        )

        # Debounce nonmaterial events (also coalesces rapid no-op updates).
        material = event.material
        prev = self._last_hashes.get(event.market)
        if material is None:
            material = prev is None or prev != (snapshot.game_state_hash, snapshot.order_book_hash)
        trace.mark("debounce")
        if not material:
            decision.action = Action.WATCH
            decision.reasons.append("debounced_nonmaterial")
            return self._finalize(decision, snapshot, trace)

        # (1) Verify state sequence.
        if not snapshot.game_sequence_ok or not snapshot.book.sequence_ok:
            return self._skip(decision, snapshot, trace, "sequence_gap")
        trace.mark("sequence")

        # Market pause / closed.
        if snapshot.market_status != "open":
            return self._skip(decision, snapshot, trace, f"market_{snapshot.market_status}")
        trace.mark("market_status")

        # (2) Verify state freshness + provider lag.
        if now - snapshot.game_updated_monotonic_ns > self.config.max_state_age_ns:
            return self._skip(decision, snapshot, trace, "stale_game_state")
        if now - snapshot.order_book_updated_monotonic_ns > self.config.max_state_age_ns:
            return self._skip(decision, snapshot, trace, "stale_order_book")
        if event.provider_lag_ns is not None and event.provider_lag_ns > self.config.max_provider_lag_ns:
            return self._skip(decision, snapshot, trace, "provider_lag_exceeded")
        trace.mark("freshness")

        # (3) Update incremental features (already vectorized by the caller) and
        # (4) run fast inference.
        pred = self.engine.predict_vector(snapshot.feature_vector)
        trace.mark("inference")
        decision.win_prob = pred.win_probability
        decision.uncertainty_std = pred.uncertainty_std
        decision.ood = bool(pred.ood_flag)

        # (5) Read local order book (already in snapshot).
        book = snapshot.book
        trace.mark("book_read")

        # Out-of-distribution: don't trade, keep watching.
        if pred.ood_flag:
            decision.action = Action.WATCH
            decision.reasons.append("out_of_distribution")
            return self._finalize(decision, snapshot, trace)

        # (9,10) Reserves.
        unc = uncertainty_reserve_cents(pred.uncertainty_std, self.config.uncertainty_reserve_coeff)
        adv = adverse_reserve_cents(event.provider_lag_ns, self.config.adverse_reserve_coeff)

        # (6,7,8) Executable price, fees, slippage per side.
        p = pred.win_probability
        yes_q = quote_side(
            side="yes", ladder=book.yes_ask_ladder, fair_prob=p, size=self.config.base_size,
            max_slip_ticks=self.config.max_slip_ticks, fee_model=self.fee_model,
            uncertainty_reserve_cents=unc, adverse_reserve_cents=adv,
        )
        no_q = quote_side(
            side="no", ladder=book.no_ask_ladder, fair_prob=1.0 - p, size=self.config.base_size,
            max_slip_ticks=self.config.max_slip_ticks, fee_model=self.fee_model,
            uncertainty_reserve_cents=unc, adverse_reserve_cents=adv,
        )
        trace.mark("pricing")

        tradeable = [q for q in (yes_q, no_q) if q.tradeable]
        if not tradeable:
            decision.action = Action.WATCH
            decision.reasons.append("no_liquidity")
            return self._finalize(decision, snapshot, trace)
        best = max(tradeable, key=lambda q: q.edge_cents)

        # (11) Portfolio limits.
        desired = min(self.config.base_size, best.filled)
        allowed = self.portfolio.allowed_size(event.market, desired)
        trace.mark("portfolio")

        decision.side = best.side
        decision.limit_price = best.limit_price
        decision.avg_fill_cents = best.avg_fill_cents
        decision.fair_value_cents = best.fair_value_cents
        decision.edge_cents = best.edge_cents
        decision.fee_per_contract_cents = best.fee_per_contract_cents
        decision.slippage_cents = best.slippage_cents
        decision.uncertainty_reserve_cents = unc
        decision.adverse_reserve_cents = adv

        # (12) Decide.
        if allowed <= 0:
            decision.action = Action.SKIP
            decision.reasons.append("risk_limit_reached")
        elif best.edge_cents >= self.config.min_edge_cents:
            decision.action = Action.BET
            decision.size = allowed
            decision.reasons.append("edge_clears_threshold")
        elif best.edge_cents >= self.config.min_edge_cents - self.config.watch_margin_cents:
            decision.action = Action.WATCH
            decision.reasons.append("thin_edge")
        else:
            decision.action = Action.SKIP
            decision.reasons.append("no_edge")

        return self._finalize(decision, snapshot, trace)

    def submit(self, decision: Decision, current: MarketSnapshot, *, now_ns: Optional[int] = None) -> SubmissionResult:
        # Prioritize risk-reducing cancels: run them first, always.
        executed = []
        for oid in decision.cancels:
            self.portfolio.cancel(oid)
            executed.append(oid)

        if decision.action is not Action.BET:
            return SubmissionResult(SubmitStatus.NO_OP, cancels_executed=executed, reason="no order")

        # A BET must carry complete, in-range trade parameters before anything
        # downstream touches a price, a ladder or an order. An incomplete
        # decision is rejected safely rather than coerced into a trade.
        trade, invalid_reason = validate_trade(decision)
        if trade is None:
            return SubmissionResult(SubmitStatus.REJECTED_INCOMPLETE, cancels_executed=executed,
                                    reason=invalid_reason)

        # Never act on an outdated state version.
        if decision.state_token != self._token.get(decision.market):
            return SubmissionResult(SubmitStatus.REJECTED_SUPERSEDED, cancels_executed=executed,
                                    reason="a newer event superseded this decision")
        if current.game_state_hash != decision.game_state_hash:
            return SubmissionResult(SubmitStatus.REJECTED_SUPERSEDED, cancels_executed=executed,
                                    reason="game-state version changed")

        # Revalidate sequence/freshness.
        if not current.game_sequence_ok or not current.book.sequence_ok:
            return SubmissionResult(SubmitStatus.REJECTED_SEQUENCE, cancels_executed=executed,
                                    reason="unresolved sequence gap")

        # Revalidate price immediately before submission; never sweep beyond the
        # approved limit.
        best_now = current.book.best_ask(trade.side)
        if best_now is None or best_now > trade.limit_price:
            return SubmissionResult(SubmitStatus.REJECTED_PRICE, cancels_executed=executed,
                                    reason="price moved beyond approved limit")
        filled, _avg = walk_ladder(current.book.ladder(trade.side), trade.size, trade.limit_price)
        if filled == 0:
            return SubmissionResult(SubmitStatus.REJECTED_PRICE, cancels_executed=executed,
                                    reason="no fill available within limit")

        order = LimitOrder(market=decision.market, side=trade.side,
                           limit_price=trade.limit_price, size=filled)
        self.portfolio.apply_fill(order)
        return SubmissionResult(SubmitStatus.SUBMITTED, order=order, cancels_executed=executed)

    def is_superseded(self, decision: Decision) -> bool:
        return decision.state_token != self._token.get(decision.market)

    # -- Internals ------------------------------------------------------------
    def _bump_token(self, market: str) -> int:
        token = self._token.get(market, 0) + 1
        self._token[market] = token
        return token

    def _skip(self, decision: Decision, snapshot: MarketSnapshot, trace: LatencyTrace, reason: str) -> Decision:
        decision.action = Action.SKIP
        decision.reasons.append(reason)
        # Attach risk-reducing cancels so they can be prioritized on submission.
        decision.cancels = self.portfolio.risk_reducing_cancels(decision.market)
        return self._finalize(decision, snapshot, trace)

    def _finalize(self, decision: Decision, snapshot: MarketSnapshot, trace: LatencyTrace) -> Decision:
        trace.finish()
        if trace.total_ns is not None:
            self.latency.record("decision_ns", trace.total_ns)
        self._last_hashes[decision.market] = (snapshot.game_state_hash, snapshot.order_book_hash)
        self._current[decision.market] = decision
        return decision

    # -- Reporting ------------------------------------------------------------
    def latency_snapshot(self):
        return self.latency.histogram("decision_ns").snapshot()

    def within_budget(self) -> bool:
        snap = self.latency_snapshot()
        return snap.p99_ns is None or snap.p99_ns <= self.config.decision_budget_ns
