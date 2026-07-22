"""Typed asynchronous adapter for The Odds API (sportsbook prices).

Read-only. Wraps three public GET endpoints:

* ``GET /v4/sports``
* ``GET /v4/sports/baseball_mlb/odds``
* ``GET /v4/sports/basketball_nba/odds``

It preserves the raw JSON before normalizing, captures the API-credit response
headers (``x-requests-remaining`` / ``x-requests-used`` / ``x-requests-last``),
caches identical requests so development does not waste credits, and never
prints or logs the API key (the key is a query-string secret; all outbound URLs
and any error messages are sanitized).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from ..redaction import sanitize_url
from .cache import ResponseCache

DEFAULT_BASE_URL = "https://api.the-odds-api.com"
DEFAULT_REGIONS = "us"
DEFAULT_MARKETS = "h2h,spreads,totals"
DEFAULT_ODDS_FORMAT = "american"

MLB_SPORT_KEY = "baseball_mlb"
NBA_SPORT_KEY = "basketball_nba"


# --------------------------------------------------------------------------- #
# Normalized models
# --------------------------------------------------------------------------- #
class NormalizedOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    price: float
    point: Optional[float] = None


class NormalizedMarket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    last_update: Optional[datetime] = None
    outcomes: list[NormalizedOutcome] = Field(default_factory=list)


class NormalizedBookmaker(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    title: Optional[str] = None
    # Bookmaker update time (required normalized field).
    last_update: Optional[datetime] = None
    markets: list[NormalizedMarket] = Field(default_factory=list)


class NormalizedEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider_event_id: str
    sport_key: str
    commence_time: Optional[datetime] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    bookmakers: list[NormalizedBookmaker] = Field(default_factory=list)


class Sport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    group: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    active: bool = False
    has_outcomes: bool = False


class CreditHeaders(BaseModel):
    """The Odds API credit-accounting headers (captured when present)."""

    requests_remaining: Optional[str] = None
    requests_used: Optional[str] = None
    requests_last: Optional[str] = None

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> "CreditHeaders":
        return cls(
            requests_remaining=headers.get("x-requests-remaining"),
            requests_used=headers.get("x-requests-used"),
            requests_last=headers.get("x-requests-last"),
        )


class OddsApiResult(BaseModel):
    """Odds for one sport: raw payload preserved alongside normalized events."""

    model_config = ConfigDict(extra="ignore")
    sport_key: str
    raw: list[dict[str, Any]]
    events: list[NormalizedEvent]
    credits: CreditHeaders
    from_cache: bool = False


class SportsResult(BaseModel):
    """The available-sports list: raw payload preserved alongside parsed sports."""

    model_config = ConfigDict(extra="ignore")
    raw: list[dict[str, Any]]
    sports: list[Sport]
    credits: CreditHeaders
    from_cache: bool = False


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def normalize_event(raw: dict[str, Any]) -> NormalizedEvent:
    """Normalize one raw Odds API event object into the typed model."""

    bookmakers: list[NormalizedBookmaker] = []
    for bm in raw.get("bookmakers", []) or []:
        markets: list[NormalizedMarket] = []
        for mk in bm.get("markets", []) or []:
            outcomes = [
                NormalizedOutcome(
                    name=oc.get("name", ""),
                    price=oc.get("price"),
                    point=oc.get("point"),
                )
                for oc in mk.get("outcomes", []) or []
            ]
            markets.append(
                NormalizedMarket(
                    key=mk.get("key", ""),
                    last_update=mk.get("last_update"),
                    outcomes=outcomes,
                )
            )
        bookmakers.append(
            NormalizedBookmaker(
                key=bm.get("key", ""),
                title=bm.get("title"),
                last_update=bm.get("last_update"),
                markets=markets,
            )
        )
    return NormalizedEvent(
        provider_event_id=raw.get("id", ""),
        sport_key=raw.get("sport_key", ""),
        commence_time=raw.get("commence_time"),
        home_team=raw.get("home_team"),
        away_team=raw.get("away_team"),
        bookmakers=bookmakers,
    )


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class OddsApiClient:
    """Async, read-only adapter for The Odds API."""

    def __init__(
        self,
        api_key: SecretStr | str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        cache: Optional[ResponseCache] = None,
        default_regions: str = DEFAULT_REGIONS,
        default_markets: str = DEFAULT_MARKETS,
        default_odds_format: str = DEFAULT_ODDS_FORMAT,
    ) -> None:
        self._api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self._base_url = base_url
        self._owns_client = client is None
        self._client = client or build_readonly_client(
            base_url=base_url,
            policy=ReadOnlyHTTPPolicy.for_odds_api(httpx.URL(base_url).host),
        )
        self._cache = cache if cache is not None else ResponseCache()
        self.default_regions = default_regions
        self.default_markets = default_markets
        self.default_odds_format = default_odds_format

    async def __aenter__(self) -> "OddsApiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- Internal request helper ---------------------------------------------
    @staticmethod
    def _cache_key(path: str, params: dict[str, Any]) -> str:
        # The API key is deliberately excluded from the key so no secret is
        # ever stored in the cache map.
        safe = sorted((k, str(v)) for k, v in params.items() if k != "apiKey")
        return f"GET {path}?{safe}"

    async def _get_json(self, path: str, params: dict[str, Any]) -> tuple[Any, CreditHeaders]:
        request_params = {"apiKey": self._api_key.get_secret_value(), **params}
        try:
            response = await self._client.get(path, params=request_params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Never let the raw URL (which carries apiKey) reach a log/traceback.
            raise httpx.HTTPStatusError(
                f"Odds API request failed ({exc.response.status_code}) for "
                f"{sanitize_url(str(exc.request.url))}",
                request=exc.request,
                response=exc.response,
            ) from None
        return response.json(), CreditHeaders.from_headers(response.headers)

    # -- Public endpoints -----------------------------------------------------
    async def get_sports(self, *, all_sports: bool = False) -> SportsResult:
        """GET /v4/sports -- the list of available sports."""

        params: dict[str, Any] = {}
        if all_sports:
            params["all"] = "true"
        cache_key = self._cache_key("/v4/sports", params)
        cached = self._cache.get(cache_key)
        if isinstance(cached, SportsResult):
            return cached.model_copy(update={"from_cache": True})

        raw, credits = await self._get_json("/v4/sports", params)
        result = SportsResult(
            raw=raw,
            sports=[Sport.model_validate(item) for item in raw],
            credits=credits,
        )
        self._cache.set(cache_key, result)
        return result

    async def get_odds(
        self,
        sport_key: str,
        *,
        regions: Optional[str] = None,
        markets: Optional[str] = None,
        odds_format: Optional[str] = None,
        bookmakers: Optional[str | Sequence[str]] = None,
        commence_time_from: Optional[str] = None,
        commence_time_to: Optional[str] = None,
    ) -> OddsApiResult:
        """GET /v4/sports/{sport_key}/odds -- odds for one sport."""

        params: dict[str, Any] = {
            "regions": regions or self.default_regions,
            "markets": markets or self.default_markets,
            "oddsFormat": odds_format or self.default_odds_format,
        }
        if bookmakers:
            params["bookmakers"] = (
                bookmakers if isinstance(bookmakers, str) else ",".join(bookmakers)
            )
        if commence_time_from:
            params["commenceTimeFrom"] = commence_time_from
        if commence_time_to:
            params["commenceTimeTo"] = commence_time_to

        path = f"/v4/sports/{sport_key}/odds"
        cache_key = self._cache_key(path, params)
        cached = self._cache.get(cache_key)
        if isinstance(cached, OddsApiResult):
            return cached.model_copy(update={"from_cache": True})

        raw, credits = await self._get_json(path, params)
        result = OddsApiResult(
            sport_key=sport_key,
            raw=raw,
            events=[normalize_event(item) for item in raw],
            credits=credits,
        )
        self._cache.set(cache_key, result)
        return result

    async def get_mlb_odds(self, **kwargs: Any) -> OddsApiResult:
        return await self.get_odds(MLB_SPORT_KEY, **kwargs)

    async def get_nba_odds(self, **kwargs: Any) -> OddsApiResult:
        return await self.get_odds(NBA_SPORT_KEY, **kwargs)
