"""US National Weather Service client (read-only, GET-only, no key).

Public-domain US weather. Requires a descriptive ``User-Agent`` (a courtesy, not
a credential). US-only: non-US venues are `unavailable` and handled by the
Open-Meteo path. D1 needs only the infrastructure a ``provider-audit`` exercises;
D4 adds the forecast/observation surface:

* ``/points/{lat},{lon}`` resolves a US coordinate to grid + station metadata and
  returns the (absolute) hourly-forecast and observation-station URLs;
* those returned URLs are **validated against the pinned host and approved path
  prefixes before being followed** (defence against an SSRF/redirect to an
  arbitrary host), then fetched as a relative path so provenance stays a clean
  path;
* observation stations for a gridpoint are discovered, then a station's
  observations are read over a bounded time interval.

A forecast is not an observation; the two are ingested as distinct weather kinds.
Base URL pinned in config.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import DEFAULT_NWS_BASE_URL
from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderError, ProviderResponse
from .capabilities import PROVIDER_NWS, ProviderErrorKind

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
        self._expected_host = httpx.URL(base_url).host

    async def fetch_point(self, latitude: float, longitude: float) -> ProviderResponse:
        """GET /points/{lat},{lon} -- resolves a coord to its gridpoint metadata."""

        return await self._get(f"/points/{float(latitude)},{float(longitude)}")

    def _validated_path(self, returned_url: object) -> str:
        """Validate a provider-returned URL and return its relative path.

        Rejects anything that is not an ``https`` URL on the pinned NWS host before
        any request is issued (fail closed); the transport policy independently
        re-checks the path allow-list. Returns the path (with no query string) so
        the follow-up request is a clean relative GET whose stored ``endpoint`` is a
        path, never a full URL.
        """

        if not isinstance(returned_url, str) or not returned_url.strip():
            raise ProviderError(
                "NWS returned no usable follow-up URL",
                kind=ProviderErrorKind.INVALID_PAYLOAD,
            )
        url = httpx.URL(returned_url.strip())
        if url.scheme != "https" or url.host != self._expected_host:
            raise ProviderError(
                f"refusing to follow a returned URL to an unapproved host "
                f"(expected https://{self._expected_host})",
                kind=ProviderErrorKind.UNSUPPORTED,
            )
        return url.path

    async def fetch_returned_url(self, returned_url: object) -> ProviderResponse:
        """GET a validated provider-returned URL (hourly forecast / station list)."""

        return await self._get(self._validated_path(returned_url))

    async def fetch_station_observations(
        self, station_id: str, *, start: str, end: str
    ) -> ProviderResponse:
        """GET /stations/{id}/observations?start=..&end=.. over a bounded interval.

        ``start``/``end`` are ISO-8601 instants bounding the request so it can never
        become an unbounded scan.
        """

        sid = str(station_id).strip()
        if not sid:
            raise ProviderError(
                "station id is required", kind=ProviderErrorKind.INVALID_PAYLOAD
            )
        return await self._get(
            f"/stations/{sid}/observations", params={"start": start, "end": end}
        )
