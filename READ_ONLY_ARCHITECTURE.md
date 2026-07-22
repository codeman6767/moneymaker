# Read-Only Architecture

This project is a **strictly read-only MLB and NBA betting *recommendation*
engine**. It reads public data, produces recommendations, and does nothing
else. It must never place, cancel, or manage a bet.

## What the system does — and does not — do

- **The model only makes recommendations.** It consumes public market data and
  emits recommendations. There is no code path from a recommendation to an
  order on any venue.
- **The Odds API provides sportsbook prices.** Head-to-head, spreads, and
  totals for MLB and NBA are read from The Odds API over public GET endpoints.
- **Kalshi public REST provides real prediction-market data.** Events, markets,
  order books, trades, series, and exchange status are read from Kalshi's
  public REST API. This is real market data, not simulated.
- **No Kalshi authentication is required.** The engine uses only Kalshi's
  public-data surface. No API key, no private key, and no request signing are
  used or loaded. Account, portfolio, balance, order, and fill endpoints are
  never contacted.
- **No order-submission code is active.** The read-only application
  (`sports_quant`) contains no execution code and imports none on startup.
- **Existing gateway code is quarantined and excluded from application
  startup.** The earlier L0–L8 execution gateway is preserved for reference but
  disabled (see *Quarantine* below).
- **Historical simulated fills may remain only inside the backtesting
  package.** Any simulated-fill logic is confined to `backtest/`; it is research
  tooling and is never used to act on a live venue.

## Startup safety invariants

`sports_quant.config.load_settings()` refuses to start the application unless
every one of these holds (loaded from the repository-root `.env`, with
`.env.txt` accepted as a fallback for the current checkout):

| Variable                   | Required value |
| -------------------------- | -------------- |
| `READ_ONLY_MODE`           | `true`         |
| `ORDER_SUBMISSION_ENABLED` | `false`        |
| `PAPER_TRADING`            | `false`        |
| `LIVE_TRADING`             | `false`        |
| `MANUAL_LIVE_ARMING`       | `false`        |
| `KALSHI_ENVIRONMENT`       | `production`   |

If any invariant is violated, startup raises `ReadOnlyStartupError` and the
process exits without doing any network I/O.

The Odds API key (`ODDS_API_KEY`) is held as a `SecretStr` and is **never
printed or logged**. All outbound URLs and error messages are sanitized so the
key cannot leak (`sports_quant.redaction`).

## Hard read-only networking policy

Every outbound request is forced through `sports_quant.http_policy`, enforced at
the transport layer so real and mocked requests face identical checks. The
policy is *default-deny*:

- **Only `GET` is permitted.** `POST`, `PUT`, `PATCH`, and `DELETE` are rejected
  before a request can leave the process.
- **Only approved hosts are reachable** (`external-api.kalshi.com` for Kalshi,
  `api.the-odds-api.com` for The Odds API).
- **Only an allow-list of public-data paths is reachable.** For Kalshi:
  `/events`, `/markets`, `/markets/{ticker}`, `/markets/{ticker}/orderbook`,
  `/markets/trades`, `/series`, `/exchange/status`. Account, portfolio, balance,
  order, fill, and position paths are rejected explicitly.

## Order books

Kalshi publishes resting **bids** on two sides (`yes` and `no`) at integer cent
prices. Returned bids are never treated as asks. The executable asks are
*derived* from the opposing side's best bid:

```
executable Yes ask = 100 − best No bid
executable No ask  = 100 − best Yes bid
```

Every price/quantity level is preserved, and an empty book yields `None` for the
derived asks.

## Providers check

```
python -m sports_quant providers-check
```

A safe, GET-only smoke test. It confirms the Odds API key is present (without
displaying it), calls the Odds API sports endpoint, fetches MLB/NBA odds **only
when those sports are active** (reporting clearly when out of season), calls the
Kalshi exchange-status endpoint, retrieves five open Kalshi markets, and prints
sanitized record counts and API-credit headers. It never places or simulates an
order.

## Quarantine

The execution gateway (`gateway/`) is preserved but quarantined:

- `gateway.quarantine.EXECUTION_QUARANTINED` is `True` and is a **source-level**
  switch — there is no environment variable or runtime flag that flips it.
- The only code paths that could contact an exchange (the real Kalshi network
  transports `KalshiRestTransport.submit`/`.cancel` and `KalshiWsFeed.connect`)
  call `gateway.quarantine.ensure_execution_allowed()` first, which raises
  `ExecutionQuarantinedError` in read-only mode.
- The read-only application never imports the gateway on its startup path.

Re-enabling execution would require editing source, at which point every model,
paper-trading, and risk gate in `CLAUDE.md` still applies. Speed is never a
reason to enable live orders.
