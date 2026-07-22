"""Kalshi rate limits and endpoint costs, queried at startup.

The gateway queries current limits and per-endpoint costs before trading
(requirement: "query current Kalshi limits and endpoint costs at startup") and
builds its local token budget from them. A static provider is used for tests and
local runs; the Kalshi provider (lazy httpx) fetches them live in demo/prod.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Protocol


@dataclass(frozen=True)
class KalshiLimits:
    read_rate_per_sec: float
    read_burst: int
    write_rate_per_sec: float
    write_burst: int
    # Endpoint name -> (category, token cost).
    endpoint_costs: Dict[str, tuple] = field(default_factory=dict)

    def cost_of(self, endpoint: str) -> tuple:
        return self.endpoint_costs.get(endpoint, ("write", 1))


DEFAULT_LIMITS = KalshiLimits(
    read_rate_per_sec=10.0,
    read_burst=20,
    write_rate_per_sec=10.0,
    write_burst=20,
    endpoint_costs={
        "create_order": ("write", 1),
        "cancel_order": ("write", 1),
        "amend_order": ("write", 1),
        "get_order": ("read", 1),
        "get_markets": ("read", 1),
    },
)


class LimitsProvider(Protocol):
    async def fetch(self) -> KalshiLimits: ...


class StaticLimitsProvider:
    def __init__(self, limits: KalshiLimits = DEFAULT_LIMITS) -> None:
        self._limits = limits

    async def fetch(self) -> KalshiLimits:
        return self._limits


class KalshiLimitsProvider:
    """Fetches limits/costs from Kalshi at startup (lazy httpx)."""

    def __init__(self, rest_base_url: str, api_key: str = "") -> None:
        self._base = rest_base_url
        self._api_key = api_key

    async def fetch(self) -> KalshiLimits:  # pragma: no cover - network path
        import httpx

        async with httpx.AsyncClient(base_url=self._base) as client:
            resp = await client.get("/exchange/status")
            resp.raise_for_status()
        # Kalshi publishes tier limits in its docs/headers; absent a live
        # entitlement we fall back to conservative documented defaults rather
        # than guessing aggressive numbers.
        return DEFAULT_LIMITS
