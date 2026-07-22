"""Read-only MLB/NBA betting *recommendation* engine (provider foundation).

This package is the read-only lane of the project. It reads public data only:

* **The Odds API** -- sportsbook prices (h2h / spreads / totals).
* **Kalshi public REST** -- real prediction-market events, markets, order
  books and trades. No Kalshi authentication is used and no private key is
  loaded.

The engine only ever *recommends*. It never places, cancels or manages a bet.
Every outbound request is forced through :mod:`sports_quant.http_policy`, which
permits GET requests to a small allow-list of public-data endpoints and rejects
everything else (all write verbs, all account/portfolio/order paths, and any
host that is not explicitly approved).

Order-execution code from the earlier L0-L8 build is preserved but quarantined
(see :mod:`gateway.quarantine` and ``READ_ONLY_ARCHITECTURE.md``); it is never
imported on the startup path of this package.
"""

from __future__ import annotations

from .config import (
    ReadOnlyStartupError,
    Settings,
    load_settings,
)
from .http_policy import (
    ReadOnlyHTTPPolicy,
    ReadOnlyPolicyError,
    ReadOnlyPolicyTransport,
    kalshi_host_rule,
    odds_api_host_rule,
)
from .providers.kalshi import KalshiClient, KalshiOrderBook
from .providers.odds_api import OddsApiClient, OddsApiResult, NormalizedEvent

__all__ = [
    "Settings",
    "load_settings",
    "ReadOnlyStartupError",
    "ReadOnlyHTTPPolicy",
    "ReadOnlyPolicyError",
    "ReadOnlyPolicyTransport",
    "kalshi_host_rule",
    "odds_api_host_rule",
    "OddsApiClient",
    "OddsApiResult",
    "NormalizedEvent",
    "KalshiClient",
    "KalshiOrderBook",
]
