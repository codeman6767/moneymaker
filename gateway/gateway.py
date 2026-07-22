"""Phase 1 execution gateway (Python asyncio, Kalshi demo).

Consumes market-data events, runs a decision, and submits Kalshi REST limit
orders on the demo environment. It enforces every safety requirement: demo by
default, no live order without explicit arming, local token budget, unique
client order ids + idempotency, timeout reconciliation, partial-fill processing,
cancel/replace, market-pause handling and automatic disarm -- and benchmarks
every stage from WebSocket receipt to fill notification.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Optional

from evaluation.decision import OrderBookView
from streaming.latency import monotonic_ns

from .arming import ArmError, ArmingController
from .benchmark import GatewayReport, LatencyBenchmark, Stage
from .client_ids import ClientOrderIdFactory, IdempotencyRegistry
from .config import GatewayConfig
from .limits import KalshiLimits, LimitsProvider, StaticLimitsProvider
from .orders import (
    Fill,
    LimitOrderRequest,
    OrderAck,
    OrderIntent,
    OrderRecord,
    OrderState,
)
from .token_budget import TokenBudgetManager
from .transport import OrderTransport

# A strategy maps (book, market_status) -> an order intent or None.
Strategy = Callable[[OrderBookView, str], Optional[OrderIntent]]


class ExecutionGateway:
    def __init__(
        self,
        config: GatewayConfig,
        transport: OrderTransport,
        strategy: Strategy,
        *,
        arming: Optional[ArmingController] = None,
        limits_provider: Optional[LimitsProvider] = None,
        id_factory: Optional[ClientOrderIdFactory] = None,
        idempotency: Optional[IdempotencyRegistry] = None,
        benchmark: Optional[LatencyBenchmark] = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.strategy = strategy
        self.arming = arming or ArmingController(environment=config.environment)
        self.limits_provider = limits_provider or StaticLimitsProvider()
        self.id_factory = id_factory or ClientOrderIdFactory()
        self.idempotency = idempotency or IdempotencyRegistry()
        self.benchmark = benchmark or LatencyBenchmark(
            sub_second_threshold_ns=config.sub_second_threshold_ns,
            min_samples=config.min_benchmark_samples,
        )
        self.limits: Optional[KalshiLimits] = None
        self.token_budget: Optional[TokenBudgetManager] = None

        self._orders: Dict[str, OrderRecord] = {}
        self._open_by_market: Dict[str, List[str]] = {}
        self._books: Dict[str, OrderBookView] = {}
        self._paused: Dict[str, bool] = {}
        self._consecutive_failures = 0
        self._started = False

    # -- Startup --------------------------------------------------------------
    async def startup(self) -> None:
        """Query current Kalshi limits/costs and build the local token budget."""

        self.limits = await self.limits_provider.fetch()
        self.token_budget = TokenBudgetManager.from_limits(self.limits)
        self._started = True

    # -- Event pipeline (benchmarked) ----------------------------------------
    async def process_event(self, raw: dict) -> Optional[OrderRecord]:
        if not self._started:
            raise RuntimeError("gateway.startup() must be called before processing events")

        timer = self.benchmark.timer()  # WS receipt = baseline
        etype = raw.get("type")
        market = raw.get("market")
        timer.mark(Stage.PARSE)

        if etype == "status":
            self._handle_status(market, raw.get("status", "open"))
            return None

        if etype != "orderbook":
            return None

        # A book event without a usable market id cannot be keyed into state.
        # Skip it rather than indexing the book cache with None.
        if not isinstance(market, str) or not market:
            return None

        book = OrderBookView.make(
            yes_ask_ladder=tuple(map(tuple, raw.get("yes", ()))),
            no_ask_ladder=tuple(map(tuple, raw.get("no", ()))),
        )
        self._books[market] = book
        timer.mark(Stage.ORDER_BOOK_UPDATE)

        if self._paused.get(market):
            return None  # market paused: no new orders

        intent = self.strategy(book, "open")
        timer.mark(Stage.INFERENCE)
        timer.mark(Stage.DECISION)
        if intent is None:
            return None

        return await self._submit_intent(intent, timer)

    async def _submit_intent(self, intent: OrderIntent, timer) -> Optional[OrderRecord]:
        # Arming gate (raises for live-not-armed) -- treat as blocked, not error.
        try:
            self.arming.ensure_order_allowed()
        except ArmError:
            return None

        # Local token budget.
        assert self.token_budget is not None and self.limits is not None
        category, cost = self.limits.cost_of("create_order")
        if not self.token_budget.consume(category, cost):
            return None  # rate-limited locally

        coid = self.id_factory.new()
        req = LimitOrderRequest(coid, intent.market, intent.side, intent.price, intent.size)

        # Signing (measured).
        sign_t0 = monotonic_ns()
        self._sign(req)
        timer.record_stage(Stage.SIGNING, monotonic_ns() - sign_t0)

        record = OrderRecord(request=req, state=OrderState.NEW, submitted_monotonic_ns=monotonic_ns())
        self._orders[coid] = record
        try:
            submit_t0 = monotonic_ns()
            ack = await self.submit_limit_order(req)
            submit_dur = monotonic_ns() - submit_t0
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            # No acknowledgement: leave SUBMITTED for reconciliation, count the
            # failure and auto-disarm if we are failing repeatedly.
            record.state = OrderState.SUBMITTED
            self.benchmark.note_failure()
            self._register_failure()
            return record

        # Ack stages: exchange ack latency is transport-reported; the remainder
        # of the round-trip is network submission.
        ex_ack = ack.exchange_ack_latency_ns or 0
        timer.record_stage(Stage.EXCHANGE_ACK, ex_ack)
        timer.record_stage(Stage.NETWORK_SUBMISSION, max(0, submit_dur - ex_ack))
        timer.record_e2e()  # event receipt -> acknowledgement

        if ack.accepted:
            record.state = OrderState.ACKED
            record.exchange_order_id = ack.exchange_order_id
            self._open_by_market.setdefault(intent.market, []).append(coid)
            self._consecutive_failures = 0
        else:
            record.state = OrderState.REJECTED
            self.benchmark.note_failure()
            self._register_failure()
        return record

    async def submit_limit_order(self, req: LimitOrderRequest) -> OrderAck:
        """Idempotent transport submit. A repeated client order id returns the
        cached ack without re-hitting the exchange."""

        # Arming is enforced here too, so no submission path can bypass it.
        self.arming.ensure_order_allowed()
        cached = self.idempotency.get(req.client_order_id)
        if cached is not None:
            return cached  # idempotent: do not resubmit
        ack = await self.transport.submit(req)
        self.idempotency.record(req.client_order_id, ack)
        return ack

    # -- Fills, cancel/replace, reconciliation --------------------------------
    async def on_fill(self, fill: Fill) -> Optional[OrderRecord]:
        record = self._orders.get(fill.client_order_id)
        if record is None:
            return None
        t0 = monotonic_ns()
        record.apply_fill(fill)
        if record.is_terminal:
            self._remove_open(record.request.market, fill.client_order_id)
        self.benchmark.record(Stage.FILL_NOTIFICATION, monotonic_ns() - t0)
        return record

    async def cancel_replace(
        self, coid: str, *, new_price: Optional[int] = None, new_size: Optional[int] = None
    ) -> Optional[OrderRecord]:
        old = self._orders.get(coid)
        if old is None or old.is_terminal:
            return None
        # Cancel first (risk-reducing), then place the replacement.
        if old.exchange_order_id is not None:
            await self.transport.cancel(old.exchange_order_id)
        old.state = OrderState.CANCELED
        self._remove_open(old.request.market, coid)

        intent = OrderIntent(
            market=old.request.market,
            side=old.request.side,
            price=new_price if new_price is not None else old.request.price,
            size=new_size if new_size is not None else old.remaining,
        )
        return await self._submit_intent(intent, self.benchmark.timer())

    async def reconcile_timeouts(self) -> List[str]:
        """Resolve orders that were submitted but never acknowledged in time."""

        now = monotonic_ns()
        reconciled: List[str] = []
        for coid, record in self._orders.items():
            if record.state is not OrderState.SUBMITTED:
                continue
            if record.submitted_monotonic_ns is None:
                continue
            if now - record.submitted_monotonic_ns < self.config.order_timeout_ns:
                continue
            report = await self.transport.query(coid)
            if report is None:
                record.state = OrderState.TIMED_OUT
            else:
                record.state = report.state
                record.filled_size = report.filled_size
            reconciled.append(coid)
        return reconciled

    # -- Market pause / disarm ------------------------------------------------
    def _handle_status(self, market: Optional[str], status: str) -> None:
        if status == "open":
            if market is not None:
                self._paused[market] = False
            return
        # paused / suspended / closed: block new orders. Resting orders are
        # pulled via the explicit awaitable cancel_resting() (callers await it),
        # keeping this status handler synchronous and side-effect-light.
        if market is not None:
            self._paused[market] = True

    async def cancel_resting(self, market: str) -> int:
        """Cancel all resting orders for a market (used on pause)."""

        canceled = 0
        for coid in list(self._open_by_market.get(market, [])):
            record = self._orders.get(coid)
            if record and record.exchange_order_id is not None and not record.is_terminal:
                await self.transport.cancel(record.exchange_order_id)
                record.state = OrderState.CANCELED
                canceled += 1
        self._open_by_market[market] = []
        return canceled

    def _register_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.config.max_consecutive_failures:
            self.arming.disarm("consecutive_failures")

    def on_reconnect(self) -> None:
        self.benchmark.note_reconnect()

    # -- Helpers --------------------------------------------------------------
    def _remove_open(self, market: str, coid: str) -> None:
        lst = self._open_by_market.get(market)
        if lst and coid in lst:
            lst.remove(coid)

    def _sign(self, req: LimitOrderRequest) -> None:
        # Placeholder for request signing (measured for the signing stage). Real
        # RSA-PSS signing lives in the Kalshi REST transport.
        _ = f"{req.client_order_id}:{req.market}:{req.side}:{req.price}:{req.size}"

    # -- Reporting ------------------------------------------------------------
    def report(self) -> GatewayReport:
        if self.token_budget is not None:
            self.benchmark.set_rate_limit_events(self.token_budget.rate_limit_events)
        return self.benchmark.report()
