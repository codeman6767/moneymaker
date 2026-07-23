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
every one of these holds. Settings are loaded from the repository-root `.env`,
which is the **only** environment file the application reads. `.env` is
git-ignored; `.env.example` is the only committed template. (`.env.txt` was
previously read as a fallback — that support has been removed and the file
deleted, because it had leaked a real API key into git history.)

| Variable                   | Required value                                    |
| -------------------------- | ------------------------------------------------- |
| `READ_ONLY_MODE`           | `true`                                            |
| `ORDER_SUBMISSION_ENABLED` | `false`                                           |
| `PAPER_TRADING`            | `false`                                           |
| `LIVE_TRADING`             | `false`                                           |
| `MANUAL_LIVE_ARMING`       | `false`                                           |
| `KALSHI_ENVIRONMENT`       | `production`                                      |
| `KALSHI_PUBLIC_REST_URL`   | `https://external-api.kalshi.com/trade-api/v2`    |

`KALSHI_PUBLIC_REST_URL` is pinned to that exact value: arbitrary Kalshi hosts
and demo hosts are rejected at startup, before any network I/O, in addition to
being rejected by the transport policy below.

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

## Public Kalshi ingestion (Phase C)

```
python -m sports_quant ingest-kalshi --status open --limit 5
                                     [--include-orderbooks] [--include-trades]
```

`ingest-kalshi` persists Kalshi's **public** events, markets, order-book
snapshots (with full ladder levels), and public trade prints into the historical
corpus. It goes through the same GET-only, unauthenticated, policy-wrapped
transport as `providers-check`: **no Kalshi credential, private key, or request
signing is used or loaded**, and account/portfolio/order/fill paths remain
blocked. `--limit` (default 20) bounds the sweep, including per-market
order-book/trade fan-out, so the default never requests every book on the
exchange; a truncated sweep is reported explicitly.

The public trade feed is stored in `kalshi_public_trades` — anonymous
market-wide prints. These are **not** account fills: the engine has no positions
and never contacts a private fill endpoint. Order-book snapshots and trades are
append-only historical records; a book returning to an earlier state is
preserved (transition-aware), and re-ingesting the same trade is idempotent.

## Providers check

```
python -m sports_quant providers-check
```

A safe, GET-only smoke test. It confirms the Odds API key is present (without
displaying it), calls the Odds API sports endpoint, fetches MLB/NBA odds **only
when those sports are active**, calls the Kalshi exchange-status endpoint,
retrieves five open Kalshi markets, and prints sanitized record counts and
API-credit headers. It never places or simulates an order.

Every check is classified and printed as one of:

| Status    | Meaning                                                      | Affects exit code |
| --------- | ------------------------------------------------------------ | ----------------- |
| `OK`      | The provider responded successfully.                          | no                |
| `SKIPPED` | Not active: league out of season, or provider not configured. | no                |
| `FAILED`  | Something genuinely active failed.                            | **yes**           |

The process exits `1` if any check is `FAILED`, `0` otherwise, and `2` if the
read-only startup invariants are violated. An out-of-season league is a
successful skip, never a failure.

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
