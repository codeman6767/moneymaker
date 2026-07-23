"""MLB StatsAPI client (read-only, GET-only, unauthenticated).

D1 needs only the **venue** surface (to seed `venues`/`venue_aliases`) and the
infrastructure a ``provider-audit`` exercises. The schedule/game/box/roster
methods are added in D2. No key is used; the base URL is pinned in
`sports_quant.config` and enforced by the transport policy.

Undocumented API, no SLA, no explicit correction timestamps -- see
`PHASE_D_PROVIDER_DECISIONS.md` §2.1. Nothing here runs at import time.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import DEFAULT_MLB_STATS_API_BASE_URL
from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_MLB_STATSAPI

_HOST = "statsapi.mlb.com"


class MlbStatsApiClient(BaseProviderClient):
    """Async, read-only adapter for the MLB StatsAPI public surface."""

    provider_name = PROVIDER_MLB_STATSAPI

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_MLB_STATS_API_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(_HOST),
            client=client,
            **kwargs,
        )

    async def fetch_venues(self) -> ProviderResponse:
        """GET /venues -- venue directory with location/field info to seed venues.

        Returns the parsed payload plus the sanitized raw exchange; the caller
        preserves the raw response before normalizing.
        """

        return await self._get("/venues", params={"hydrate": "location,fieldInfo"})

    async def fetch_venue(self, venue_id: int) -> ProviderResponse:
        """GET /venues/{id} -- a single venue (used by the audit)."""

        return await self._get(
            f"/venues/{int(venue_id)}", params={"hydrate": "location,fieldInfo"}
        )
