"""Execution-gateway configuration.

Demo by default (``CLAUDE.md``: "demo by default", "no live order without
explicit manual arming"). Live endpoints are defined for reference but are only
reachable when the environment is explicitly ``live`` *and* the arming
controller has been manually armed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Kalshi demo (paper) environment -- the default and the only target for Phase 1.
DEMO_REST_URL = "https://demo-api.kalshi.co/trade-api/v2"
DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"
# Live endpoints, for reference only; unreachable unless armed.
LIVE_REST_URL = "https://api.elections.kalshi.com/trade-api/v2"
LIVE_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


@dataclass
class GatewayConfig:
    environment: str = "demo"  # "demo" | "live"
    rest_base_url: str = DEMO_REST_URL
    ws_url: str = DEMO_WS_URL

    # Reconciliation / safety.
    order_timeout_ns: int = 2_000_000_000       # 2s to see an ack before reconciling
    max_consecutive_failures: int = 3           # auto-disarm threshold

    # Benchmark / claims.
    sub_second_threshold_ns: int = 1_000_000_000
    min_benchmark_samples: int = 100            # min sample for any latency claim

    @property
    def is_demo(self) -> bool:
        return self.environment == "demo"

    @property
    def is_live(self) -> bool:
        return self.environment == "live"

    @classmethod
    def demo(cls) -> "GatewayConfig":
        return cls(environment="demo", rest_base_url=DEMO_REST_URL, ws_url=DEMO_WS_URL)

    @classmethod
    def live(cls) -> "GatewayConfig":
        # Constructing a live config does NOT arm it; arming is a separate,
        # explicit, manual step.
        return cls(environment="live", rest_base_url=LIVE_REST_URL, ws_url=LIVE_WS_URL)
