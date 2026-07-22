# Execution gateway — phase gating (LEGACY / QUARANTINED)

> **This entire package is legacy and quarantined. No order submission is
> active anywhere in this project.**
>
> The project is a strictly read-only MLB/NBA betting *recommendation* engine.
> It never places, cancels, or manages a bet. The code described below is
> preserved from the earlier L0–L8 build for reference only:
>
> - `gateway.quarantine.EXECUTION_QUARANTINED` is `True` — a **source-level**
>   switch with no environment variable or runtime flag that flips it.
> - Every code path that could contact an exchange
>   (`KalshiRestTransport.submit` / `.cancel`, `KalshiWsFeed.connect`) calls
>   `ensure_execution_allowed()` first, which raises `ExecutionQuarantinedError`.
> - The read-only application (`sports_quant`) never imports this package on its
>   startup path.
>
> Read every "IMPLEMENTED" below as "written, then disabled". See
> `READ_ONLY_ARCHITECTURE.md`.

Per `CLAUDE.md`: use Python for the first correct implementation; add a
Rust/Tokio service only after benchmarks show Python latency is material; never
claim sub-second unless measured provider-event → exchange-ack.

## Phase 1 — Python asyncio (WRITTEN, THEN QUARANTINED)

- Kalshi **demo** environment only, demo by default.
- WebSocket market data (`KalshiWsFeed`) and REST limit-order submission
  (`KalshiRestTransport`). Limit orders only. **Both refuse to run:** they raise
  `ExecutionQuarantinedError` before any network call.
- Safety envelope (all inert while quarantined): explicit manual arming for
  live, local token-budget manager (built from limits queried at startup),
  unique client order ids + idempotency, timeout reconciliation, partial-fill
  processing, cancel/replace, market-pause handling, automatic disarm.
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
