"""Open-Meteo client (read-only, GET-only, no key on the free tier).

Three DISTINCT products on three pinned hosts, never conflated:

* **current forecast** -- ``api.open-meteo.com/v1/forecast``;
* **historical forecast** (the stitched "previous model runs" archive) --
  ``historical-forecast-api.open-meteo.com/v1/forecast``; the point-in-time
  advantage, but a stitched product is NOT a single issued model run;
* **reanalysis archive** (ERA5) -- ``archive-api.open-meteo.com/v1/archive``; an
  *observation-grade* reanalysis, never a pregame forecast.

Commercial use may require a paid plan: a licensing limitation recorded as a note,
not a technical capability. The reanalysis/current-conditions/forecast surfaces
are kept as separate typed methods so a caller cannot accidentally substitute one
for another. Base URLs pinned + validated in config.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ..config import DEFAULT_OPEN_METEO_BASE_URL
from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_OPEN_METEO

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Canonical hourly variables requested for a forecast/historical-forecast window.
DEFAULT_FORECAST_HOURLY = (
    "temperature_2m,apparent_temperature,dew_point_2m,relative_humidity_2m,"
    "wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation,"
    "precipitation_probability,weather_code"
)
#: Reanalysis has no forecast probability; request the observation-grade subset.
DEFAULT_ARCHIVE_HOURLY = (
    "temperature_2m,apparent_temperature,dew_point_2m,relative_humidity_2m,"
    "wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation,weather_code"
)


def _iso_date(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value.strip()):
        raise ValueError(f"{label} must be a YYYY-MM-DD date (got {value!r})")
    return value.strip()


class OpenMeteoClient(BaseProviderClient):
    """Async, read-only adapter for the pinned Open-Meteo surfaces.

    One instance is bound to one ``base_url`` (one host). The policy admits all
    three pinned Open-Meteo hosts, but a given instance can only reach its own
    ``base_url`` host, so calling the wrong surface method for the configured host
    is refused by the path allow-list rather than silently hitting the wrong
    product.
    """

    provider_name = PROVIDER_OPEN_METEO

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPEN_METEO_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> None:
        # The pinned config base URL carries the ``/v1`` path, but the request
        # methods use absolute ``/v1/...`` paths; keep only scheme+host as the
        # client base so httpx does not concatenate ``/v1`` twice. The host is what
        # the policy pins, so this is purely a URL-joining nicety.
        host_base = str(httpx.URL(base_url).copy_with(raw_path=b"", query=None, fragment=None))
        super().__init__(
            base_url=host_base,
            policy=ReadOnlyHTTPPolicy.for_open_meteo_all(),
            client=client,
            **kwargs,
        )

    @staticmethod
    def _common_params(
        latitude: float, longitude: float, hourly: str,
        start_date: Optional[object], end_date: Optional[object], timezone: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "hourly": hourly,
            # Canonical internal units, requested explicitly so no guessing occurs.
            "temperature_unit": "celsius",
            "wind_speed_unit": "ms",
            "precipitation_unit": "mm",
            "timezone": timezone,
        }
        if (start_date is None) ^ (end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if start_date is not None and end_date is not None:
            params["start_date"] = _iso_date(start_date, label="start_date")
            params["end_date"] = _iso_date(end_date, label="end_date")
        return params

    async def fetch_forecast(
        self,
        latitude: float,
        longitude: float,
        *,
        hourly: str = DEFAULT_FORECAST_HOURLY,
        start_date: Optional[object] = None,
        end_date: Optional[object] = None,
        timezone: str = "UTC",
    ) -> ProviderResponse:
        """GET /v1/forecast -- the CURRENT forecast (api.open-meteo.com)."""

        return await self._get(
            "/v1/forecast",
            params=self._common_params(latitude, longitude, hourly, start_date, end_date, timezone),
        )

    async def fetch_historical_forecast(
        self,
        latitude: float,
        longitude: float,
        *,
        start_date: object,
        end_date: object,
        hourly: str = DEFAULT_FORECAST_HOURLY,
        timezone: str = "UTC",
    ) -> ProviderResponse:
        """GET /v1/forecast on the HISTORICAL FORECAST host (stitched previous runs).

        A bounded date range is REQUIRED: this is the archived-forecast product, so
        an unbounded request is refused. The result is a stitched historical
        forecast, not a single issued model run -- the caller records that fact.
        """

        return await self._get(
            "/v1/forecast",
            params=self._common_params(latitude, longitude, hourly, start_date, end_date, timezone),
        )

    async def fetch_archive(
        self,
        latitude: float,
        longitude: float,
        *,
        start_date: object,
        end_date: object,
        hourly: str = DEFAULT_ARCHIVE_HOURLY,
        timezone: str = "UTC",
    ) -> ProviderResponse:
        """GET /v1/archive on the ARCHIVE host -- ERA5 REANALYSIS (observation-grade).

        A bounded date range is REQUIRED. Reanalysis is never a pregame forecast;
        the caller stores it as ``weather_kind = reanalysis``.
        """

        return await self._get(
            "/v1/archive",
            params=self._common_params(latitude, longitude, hourly, start_date, end_date, timezone),
        )
