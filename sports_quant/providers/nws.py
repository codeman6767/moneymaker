"""US National Weather Service client (read-only, GET-only, no key).

Public-domain US weather. Requires a descriptive ``User-Agent`` (a courtesy, not
a credential). US-only: non-US venues are `unavailable` and handled by the
Open-Meteo path. D1 needs only the infrastructure a ``provider-audit`` exercises;
the forecast/observation ingestion is D4. Base URL pinned in config.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import DEFAULT_NWS_BASE_URL
from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_NWS

_HOST = "api.weather.gov"
#: A descriptive, contactable UA per NWS guidance. No secret; safe to send/store.
DEFAULT_NWS_USER_AGENT = "sports-quant/0.1 (read-only research; contact: local)"


class NwsClient(BaseProviderClient):
    """Async, read-only adapter for api.weather.gov."""

    provider_name = PROVIDER_NWS

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_NWS_BASE_URL,
        user_agent: str = DEFAULT_NWS_USER_AGENT,
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            policy=ReadOnlyHTTPPolicy.for_nws(_HOST),
            client=client,
            default_headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
            **kwargs,
        )

    async def fetch_point(self, latitude: float, longitude: float) -> ProviderResponse:
        """GET /points/{lat},{lon} -- resolves a coord to its gridpoint metadata."""

        return await self._get(f"/points/{float(latitude)},{float(longitude)}")
