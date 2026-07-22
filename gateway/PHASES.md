# Execution gateway — phase gating

Per `CLAUDE.md`: use Python for the first correct implementation; add a
Rust/Tokio service only after benchmarks show Python latency is material; never
claim sub-second unless measured provider-event → exchange-ack.

## Phase 1 — Python asyncio (IMPLEMENTED)

- Kalshi **demo** environment only, demo by default.
- WebSocket market data (`KalshiWsFeed`) and REST limit-order submission
  (`KalshiRestTransport`). Limit orders only.
- Safety envelope: explicit manual arming for live, local token-budget manager
  (built from limits queried at startup), unique client order ids + idempotency,
  timeout reconciliation, partial-fill processing, cancel/replace, market-pause
  handling, automatic disarm.
- Per-stage benchmarking (WS receipt → parse → order-book update → inference →
  decision → signing → network submission → exchange ack → fill notification),
  reporting p50/p90/p95/p99/max, sample counts, failures, reconnects and
  rate-limit events.

## Phase 2 — Rust/Tokio gateway (NOT IMPLEMENTED — gated)

Build **only if** the Phase 1 Python **p99 internal latency** is shown to
materially harm the tested strategy in the latency backtester (Module 7). The
trigger is data, not intuition: a documented backtest showing the strategy's
edge/fill-rate degrades at the measured Python internal-latency p99, beyond what
the strategy tolerates.

Until that evidence exists, this phase is intentionally absent.

## Phase 3 — Kalshi FIX (NOT IMPLEMENTED — gated)

Build **only after** account access and FIX documentation are verified:

1. a Kalshi account with FIX entitlement is confirmed;
2. the current Kalshi FIX specification is obtained and reviewed;
3. connectivity/credentials are validated against the demo FIX endpoint.

Until all three are verified, this phase is intentionally absent.

## Latency-claim policy

The system is described as "sub-second" **only** when the end-to-end
event-to-acknowledgement latency has a statistically meaningful sample
(`min_benchmark_samples`) and its p99 is below the configured threshold. See
`GatewayReport.claims_sub_second()` — it returns False (and the report makes no
sub-second claim) otherwise.
