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

Each call also returns a :class:`RawExchange` -- the sanitized status code,
headers, timestamps and body bytes of the HTTP exchange -- so the ingestion
lane can preserve the response in the corpus without a second HTTP client and
without ever handling the key itself.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from ..redaction import sanitize_url
from .cache import ResponseCache
from .raw_exchange import RawExchange, build_exchange

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


class OddsApiHTTPError(httpx.HTTPStatusError):
    """An Odds API HTTP failure that still carries its sanitized exchange.

    Subclasses :class:`httpx.HTTPStatusError` so existing handlers keep
    working unchanged, while letting an ingestor preserve the bytes of a failed
    response instead of discarding the only evidence of what went wrong.
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        exchange: RawExchange,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.exchange = exchange


class OddsApiResult(BaseModel):
    """Odds for one sport: raw payload preserved alongside normalized events."""

    model_config = ConfigDict(extra="ignore")
    sport_key: str
    raw: list[dict[str, Any]]
    events: list[NormalizedEvent]
    credits: CreditHeaders
    exchange: Optional[RawExchange] = None
    from_cache: bool = False


class SportsResult(BaseModel):
    """The available-sports list: raw payload preserved alongside parsed sports."""

    model_config = ConfigDict(extra="ignore")
    raw: list[dict[str, Any]]
    sports: list[Sport]
    credits: CreditHeaders
    exchange: Optional[RawExchange] = None
    from_cache: bool = False


class HistoricalAccessError(RuntimeError):
    """The account cannot read historical snapshots. NEVER retried.

    The Odds API returns a documented plan-entitlement failure for historical
    endpoints on a free usage plan. Retrying it burns wall-clock and obscures
    the real answer, which is that a human must change a subscription.

    Carries the sanitized ``exchange`` so the refusal is preservable evidence.
    An entitlement refusal is the single most important thing a capability probe
    can learn, and discarding its exchange would mean the one authorized request
    left nothing behind. The API key is already redacted by ``build_exchange``.
    """

    def __init__(self, message: str, *, exchange: Optional[RawExchange] = None,
                 status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.exchange = exchange
        self.status_code = status_code


class HistoricalEvent(BaseModel):
    """One event exactly as it appeared at a historical snapshot instant.

    ``commence_time`` is the CONTEMPORANEOUS value from that snapshot -- the only
    start time the reviewed anchoring algorithm may use. It is not the
    retrospectively known final start.
    """

    model_config = ConfigDict(extra="ignore")
    id: str
    sport_key: str
    commence_time: str
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    sport_title: Optional[str] = None


class HistoricalSnapshot(BaseModel):
    """The provider historical wrapper: which snapshot actually answered.

    The requested ``date`` is NOT the snapshot instant. The provider returns the
    closest snapshot at or before the request, and ``timestamp`` is that instant.
    Everything downstream anchors on ``timestamp``; ``requested_date`` is only
    what was asked for, kept separate so the two can never be confused.
    """

    model_config = ConfigDict(extra="ignore")
    timestamp: str
    previous_timestamp: Optional[str] = None
    next_timestamp: Optional[str] = None
    events: list[HistoricalEvent] = Field(default_factory=list)
    requested_date: str = ""
    raw: list[dict[str, Any]] = Field(default_factory=list)
    credits: CreditHeaders = Field(default_factory=CreditHeaders)
    exchange: Optional[RawExchange] = None


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

    def _build_exchange(
        self,
        *,
        path: str,
        params: dict[str, Any],
        response: httpx.Response,
        requested_at: datetime,
        elapsed_ns: int,
    ) -> RawExchange:
        """Capture one exchange in already-sanitized form, redacting the API key."""

        return build_exchange(
            path=path,
            params=params,
            response=response,
            requested_at=requested_at,
            elapsed_ns=elapsed_ns,
            secrets=[self._api_key.get_secret_value()],
        )

    async def _get_json(
        self, path: str, params: dict[str, Any]
    ) -> tuple[Any, CreditHeaders, RawExchange]:
        request_params = {"apiKey": self._api_key.get_secret_value(), **params}
        requested_at = datetime.now(timezone.utc)
        started_ns = time.monotonic_ns()
        response = await self._client.get(path, params=request_params)
        # Built before raise_for_status so an error response keeps its bytes:
        # a 4xx/5xx body is the only evidence of why a run failed.
        exchange = self._build_exchange(
            path=path,
            params=request_params,
            response=response,
            requested_at=requested_at,
            elapsed_ns=time.monotonic_ns() - started_ns,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Never let the raw URL (which carries apiKey) reach a log/traceback.
            raise OddsApiHTTPError(
                f"Odds API request failed ({exc.response.status_code}) for "
                f"{sanitize_url(str(exc.request.url))}",
                request=exc.request,
                response=exc.response,
                exchange=exchange,
            ) from None
        return response.json(), CreditHeaders.from_headers(response.headers), exchange

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

        raw, credits, exchange = await self._get_json("/v4/sports", params)
        result = SportsResult(
            raw=raw,
            sports=[Sport.model_validate(item) for item in raw],
            credits=credits,
            exchange=exchange,
        )
        self._cache.set(cache_key, result)
        return result

    def _odds_params(
        self,
        *,
        regions: Optional[str],
        markets: Optional[str],
        odds_format: Optional[str],
        bookmakers: Optional[str | Sequence[str]],
        commence_time_from: Optional[str],
        commence_time_to: Optional[str],
    ) -> dict[str, Any]:
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
        return params

    async def fetch_odds_raw(
        self,
        sport_key: str,
        *,
        regions: Optional[str] = None,
        markets: Optional[str] = None,
        odds_format: Optional[str] = None,
        bookmakers: Optional[str | Sequence[str]] = None,
        commence_time_from: Optional[str] = None,
        commence_time_to: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], CreditHeaders, RawExchange]:
        """Fetch odds and return the raw payload without normalizing it.

        The ingestion lane needs the exact bytes *and* the exchange metadata
        before any parsing, so a single malformed record cannot lose the whole
        response (normalization is applied per-event, defensively, downstream).
        This is not a cache path: capture-forward ingestion must observe the
        live response every run.
        """

        path = f"/v4/sports/{sport_key}/odds"
        params = self._odds_params(
            regions=regions,
            markets=markets,
            odds_format=odds_format,
            bookmakers=bookmakers,
            commence_time_from=commence_time_from,
            commence_time_to=commence_time_to,
        )
        raw, credits, exchange = await self._get_json(path, params)
        return list(raw), credits, exchange

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

        params = self._odds_params(
            regions=regions,
            markets=markets,
            odds_format=odds_format,
            bookmakers=bookmakers,
            commence_time_from=commence_time_from,
            commence_time_to=commence_time_to,
        )

        path = f"/v4/sports/{sport_key}/odds"
        cache_key = self._cache_key(path, params)
        cached = self._cache.get(cache_key)
        if isinstance(cached, OddsApiResult):
            return cached.model_copy(update={"from_cache": True})

        raw, credits, exchange = await self._get_json(path, params)
        result = OddsApiResult(
            sport_key=sport_key,
            raw=raw,
            events=[normalize_event(item) for item in raw],
            credits=credits,
            exchange=exchange,
        )
        self._cache.set(cache_key, result)
        return result


    async def get_historical_events(
        self, *, sport_key: str, date: str,
    ) -> HistoricalSnapshot:
        """GET /v4/historical/sports/{sport}/events -- E0 anchor evidence.

        Returns the events that HAD ODDS AVAILABLE at the requested historical
        instant. Under the reviewed economic-evidence grades that is **E0**
        ("market existed, no price"), which the architecture permits for
        **target anchoring only** and forbids for any EV claim. This method
        therefore fetches no prices, by design.

        Costs 1 credit per request (no charge when no events are found), against
        10 x markets x regions for the historical ODDS endpoint. That is a
        consequence of choosing the narrower evidence, not the reason for it.

        Separate from ``get_odds`` deliberately: no existing method may silently
        begin returning historical data, and nothing here falls back to the
        10-credit endpoint.
        """

        if not sport_key or not sport_key.strip():
            raise ValueError("sport_key is required for a historical request")
        if not date or not date.strip():
            raise ValueError("a historical request requires an explicit date")

        path = f"/v4/historical/sports/{sport_key}/events"
        params: dict[str, Any] = {"date": date, "dateFormat": "iso"}
        try:
            raw, credits, exchange = await self._get_json(path, params)
        except OddsApiHTTPError as exc:
            body = ""
            try:
                body = exc.response.text or ""
            except Exception:      # pragma: no cover - defensive
                body = ""
            if _is_historical_entitlement_failure(exc.response.status_code, body):
                raise HistoricalAccessError(
                    "The Odds API refused historical access for this plan "
                    "(HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN). That is a "
                    "subscription question, not a transient error, so it is "
                    "never retried.",
                    exchange=exc.exchange,
                    status_code=exc.response.status_code,
                ) from None
            raise

        return _parse_historical_snapshot(
            raw, requested_date=date, credits=credits, exchange=exchange)

    async def get_mlb_odds(self, **kwargs: Any) -> OddsApiResult:
        return await self.get_odds(MLB_SPORT_KEY, **kwargs)

    async def get_nba_odds(self, **kwargs: Any) -> OddsApiResult:
        return await self.get_odds(NBA_SPORT_KEY, **kwargs)


# --------------------------------------------------------------------------- #
# Historical snapshot parsing
# --------------------------------------------------------------------------- #
#: Substrings the provider uses for the historical plan-entitlement refusal.
_HISTORICAL_ENTITLEMENT_MARKERS = (
    "historical_unavailable_on_free_usage_plan",
    "historical data is not available on the free",
)


def _is_historical_entitlement_failure(status: int, body: str) -> bool:
    """Recognize the documented historical plan refusal, whatever its status."""

    lowered = (body or "").lower()
    if any(marker in lowered for marker in _HISTORICAL_ENTITLEMENT_MARKERS):
        return True
    return status == 401 and "historical" in lowered


def _parse_historical_snapshot(
    raw: Any, *, requested_date: str, credits: CreditHeaders,
    exchange: Optional[RawExchange],
) -> HistoricalSnapshot:
    """Parse the historical wrapper, failing closed on anything malformed.

    A snapshot with no ``timestamp`` carries no availability evidence, so it is
    refused rather than defaulted to the requested date. Substituting what was
    asked for in place of what the provider answered is exactly the clock
    overloading this lane forbids.
    """

    if not isinstance(raw, dict):
        raise ValueError(
            f"historical response is {type(raw).__name__}, expected the "
            "timestamp/previous_timestamp/next_timestamp/data wrapper")
    timestamp = raw.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError(
            "historical response carries no snapshot timestamp; the requested "
            "date is NOT a substitute for the instant the provider answered")
    data = raw.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ValueError(
            f"historical response data is {type(data).__name__}, expected a list")

    events: list[HistoricalEvent] = []
    kept: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("a historical event entry is not an object")
        kept.append(item)
        events.append(HistoricalEvent.model_validate(item))

    def _opt(key: str) -> Optional[str]:
        value = raw.get(key)
        return value if isinstance(value, str) and value.strip() else None

    return HistoricalSnapshot(
        timestamp=timestamp,
        previous_timestamp=_opt("previous_timestamp"),
        next_timestamp=_opt("next_timestamp"),
        events=events,
        requested_date=requested_date,
        raw=kept,
        credits=credits,
        exchange=exchange,
    )
