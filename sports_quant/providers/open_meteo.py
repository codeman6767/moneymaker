"""Open-Meteo client (read-only, GET-only, no key on the free tier).

Global forecasts, a reanalysis archive, and -- the point-in-time advantage -- a
historical-forecast ("previous model runs") surface. Commercial use may require a
paid plan: a licensing limitation recorded as a note, not a technical capability.
D1 needs only the infrastructure a ``provider-audit`` exercises; forecast/actual
ingestion is D4. Base URL pinned in config.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import DEFAULT_OPEN_METEO_BASE_URL
from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_OPEN_METEO

_HOST = "api.open-meteo.com"


class OpenMeteoClient(BaseProviderClient):
    """Async, read-only adapter for api.open-meteo.com."""

    provider_name = PROVIDER_OPEN_METEO

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPEN_METEO_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            policy=ReadOnlyHTTPPolicy.for_open_meteo(_HOST),
            client=client,
            **kwargs,
        )

    async def fetch_forecast(
        self, latitude: float, longitude: float, *, hourly: str = "temperature_2m"
    ) -> ProviderResponse:
        """GET /v1/forecast -- current forecast for a coord (used by the audit)."""

        return await self._get(
            "/v1/forecast",
            params={
                "latitude": float(latitude),
                "longitude": float(longitude),
                "hourly": hourly,
            },
        )
