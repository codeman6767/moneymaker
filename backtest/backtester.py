"""Replay backtester: replay events, apply latency, simulate fills, report.

The strategy makes a decision at each trigger event using the book at event
time; the backtester then sweeps a grid of total pipeline latencies, and for
each latency re-resolves the fill against the book at the moment the order would
have reached the exchange. That isolates latency's effect on fills, prices,
profit and CLV.

Every report leads with the data-quality grade, and the strategy is never
labeled execution-valid when order-book or event timestamps are missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol

import numpy as np

from evaluation.decision import OrderBookView
from evaluation.pricing import FeeModel, quote_side

from .book_timeline import MarketTimeline, build_timelines
from .data_quality import DataQualityReport, grade_dataset
from .events import DECISION_TRIGGERS, ReplayEvent, as_timed_market_event
from .fill_model import clv_cents, expected_profit_cents, resolve_fill
from .latency_model import LatencyModel
from .metrics import LatencyMetrics, break_even_latency, build_distributions

DEFAULT_GRID_NS: tuple = (
    0, 25_000_000, 50_000_000, 100_000_000, 200_000_000,
    400_000_000, 800_000_000, 1_600_000_000,
)


@dataclass(frozen=True)
class StrategyDecision:
    side: str
    limit_price: int
    size: int
    fair_value_cents: float
    model_prob: float


class Strategy(Protocol):
    def decide(self, event: ReplayEvent, view: OrderBookView, status: str) -> Optional[StrategyDecision]: ...


@dataclass
class EdgeStrategy:
    """Reference strategy: take the side whose edge clears a threshold.

    Reads the model's fair probability from ``event.payload['fair_prob']`` (in
    production this comes from the live probability model reacting to the event).
    """

    min_edge_cents: float = 2.0
    base_size: int = 20
    max_slip_ticks: int = 1
    fee_coeff: float = 0.07

    def decide(self, event: ReplayEvent, view: OrderBookView, status: str) -> Optional[StrategyDecision]:
        if status != "open":
            return None
        p = event.payload.get("fair_prob")
        if p is None:
            return None
        fee_model = FeeModel(self.fee_coeff)
        yes_q = quote_side(side="yes", ladder=view.yes_ask_ladder, fair_prob=p, size=self.base_size,
                           max_slip_ticks=self.max_slip_ticks, fee_model=fee_model,
                           uncertainty_reserve_cents=0.0, adverse_reserve_cents=0.0)
        no_q = quote_side(side="no", ladder=view.no_ask_ladder, fair_prob=1.0 - p, size=self.base_size,
                          max_slip_ticks=self.max_slip_ticks, fee_model=fee_model,
                          uncertainty_reserve_cents=0.0, adverse_reserve_cents=0.0)
        tradeable = [q for q in (yes_q, no_q) if q.tradeable]
        if not tradeable:
            return None
        best = max(tradeable, key=lambda q: q.edge_cents)
        if best.edge_cents < self.min_edge_cents:
            return None
        limit_price = best.tradeable_limit_price()
        if limit_price is None:
            # A quote without an approved ceiling is not actionable.
            return None
        return StrategyDecision(
            side=best.side, limit_price=limit_price, size=self.base_size,
            fair_value_cents=best.fair_value_cents, model_prob=p,
        )


@dataclass
class DecisionPoint:
    market: str
    t0_ns: int
    side: str
    limit_price: int
    size: int
    fair_value_cents: float
    next_trigger_ns: Optional[int]
    closing_price: Optional[int]


@dataclass
class BacktestConfig:
    grid_ns: tuple = DEFAULT_GRID_NS
    seed: int = 0
    fee_coeff: float = 0.07


@dataclass
class BacktestReport:
    #: Prominent, first: the data-quality grade governs everything below.
    data_quality: DataQualityReport
    latency_metrics: LatencyMetrics
    n_decision_events: int
    n_orders: int

    def summary(self) -> str:
        lines = [self.data_quality.banner()]
        if not self.data_quality.execution_valid:
            lines.append("WARNING: results are NOT execution-valid and must not be "
                         "treated as achievable performance.")
        m = self.latency_metrics
        be = m.break_even_latency_ns
        lines.append(f"orders={self.n_orders} decisions={self.n_decision_events} "
                     f"break_even_latency_ns={be}")
        return "\n".join(lines)


class ReplayBacktester:
    def __init__(
        self,
        events: List[ReplayEvent],
        strategy: Strategy,
        latency_model: Optional[LatencyModel] = None,
        config: Optional[BacktestConfig] = None,
    ) -> None:
        self.events = events
        self.strategy = strategy
        self.latency_model = latency_model or LatencyModel()
        self.config = config or BacktestConfig()
        self.fee_model = FeeModel(self.config.fee_coeff)

    def run(self) -> BacktestReport:
        dq = grade_dataset(self.events)
        timelines = build_timelines(self.events)

        points = self._collect_decisions(timelines)

        # Provider-lag / internal-latency distributions from sampled stages.
        rng = np.random.default_rng(self.config.seed)
        provider_lags: List[float] = []
        internals: List[float] = []
        for _ in points:
            s = self.latency_model.sample(rng)
            provider_lags.append(float(s.provider_lag_ns))
            internals.append(float(s.internal_ns))
        provider_dist, internal_dist = build_distributions(provider_lags, internals)

        grid = list(self.config.grid_ns)
        profit_curve: List[float] = []
        edge_curve: List[float] = []
        fill_curve: List[float] = []
        clv_curve: List[float] = []
        count_curve: List[int] = []

        for L in grid:
            profits, edges, clvs = [], [], []
            fills = 0
            actionable = 0
            for pt in points:
                tl = timelines[pt.market]
                ack = pt.t0_ns + L
                is_stale = pt.next_trigger_ns is not None and pt.next_trigger_ns <= ack
                status = tl.status_at(ack)
                view = tl.view_at(ack)
                if not is_stale and status == "open":
                    actionable += 1
                fill = resolve_fill(
                    side=pt.side, limit_price=pt.limit_price, size=pt.size,
                    view=view, status_at_arrival=status, is_stale=is_stale,
                )
                profits.append(expected_profit_cents(pt.fair_value_cents, fill, self.fee_model))
                if fill.is_fill:
                    fills += 1
                    c = clv_cents(pt.closing_price, fill)
                    if c is not None:
                        clvs.append(c)
                # Available edge at arrival (drives edge decay).
                best = view.best_ask(pt.side) if view is not None else None
                if best is not None:
                    edges.append(pt.fair_value_cents - best)
            n = max(1, len(points))
            profit_curve.append(float(np.mean(profits)) if profits else 0.0)
            edge_curve.append(float(np.mean(edges)) if edges else 0.0)
            fill_curve.append(fills / n)
            clv_curve.append(float(np.mean(clvs)) if clvs else float("nan"))
            count_curve.append(actionable)

        metrics = LatencyMetrics(
            latencies_ns=grid,
            profit_by_latency=profit_curve,
            edge_decay=edge_curve,
            fill_rate_by_latency=fill_curve,
            clv_by_latency=clv_curve,
            decision_count_by_latency=count_curve,
            break_even_latency_ns=break_even_latency(grid, profit_curve),
            provider_lag_dist=provider_dist,
            internal_latency_dist=internal_dist,
        )
        n_decision_events = sum(
            1 for e in self.events if e.event_type in DECISION_TRIGGERS and e.event_time_ns is not None
        )
        return BacktestReport(
            data_quality=dq, latency_metrics=metrics,
            n_decision_events=n_decision_events, n_orders=len(points),
        )

    def _collect_decisions(self, timelines: Dict[str, MarketTimeline]) -> List[DecisionPoint]:
        # Only events carrying both a market id and a provider event time can be
        # placed on a timeline; the rest are counted as data-quality gaps.
        triggers = sorted(
            [timed for timed in
             (as_timed_market_event(e) for e in self.events if e.event_type in DECISION_TRIGGERS)
             if timed is not None],
            key=lambda timed: timed.event_time_ns,
        )
        # Next same-market trigger time, for staleness.
        next_by_market: Dict[str, List[int]] = {}
        for timed in triggers:
            next_by_market.setdefault(timed.market, []).append(timed.event_time_ns)

        points: List[DecisionPoint] = []
        for timed in triggers:
            tl = timelines.get(timed.market)
            if tl is None:
                continue
            view = tl.view_at(timed.event_time_ns)
            status = tl.status_at(timed.event_time_ns)
            if view is None:
                continue
            decision = self.strategy.decide(timed.event, view, status)
            if decision is None:
                continue
            times = next_by_market[timed.market]
            nxt = next((t for t in times if t > timed.event_time_ns), None)
            points.append(DecisionPoint(
                market=timed.market, t0_ns=timed.event_time_ns, side=decision.side,
                limit_price=decision.limit_price, size=decision.size,
                fair_value_cents=decision.fair_value_cents,
                next_trigger_ns=nxt, closing_price=tl.closing_price(decision.side),
            ))
        return points
