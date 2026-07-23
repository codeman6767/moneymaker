"""Shared GET-only provider-client foundation for the Phase D data providers.

Every Phase D client (MLB StatsAPI, BALLDONTLIE, NWS, Open-Meteo) is a thin,
read-only wrapper over the shared policy-wrapped transport. This base captures
the behaviour they have in common so it is implemented once:

* GET only, through :class:`~sports_quant.http_policy.ReadOnlyPolicyTransport`;
* a sanitized :class:`~sports_quant.providers.raw_exchange.RawExchange` for every
  response (bytes preserved before parsing);
* bounded timeouts and a bounded, ``Retry-After``-aware exponential backoff for
  429 / retryable 5xx;
* rejection of unexpected content types and oversized bodies;
* **no automatic redirect following** -- a 3xx is surfaced, never chased to an
  unapproved host (and the policy transport would re-check it if it were);
* no network activity at import time; a mocked transport is injectable for tests.

Nothing here loads or echoes a credential; a subclass that needs a key passes it
to :meth:`_get` as a ``secret`` so the body is stripped and the URL/params are
sanitized by :func:`build_exchange`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import httpx

from ..http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from ..redaction import sanitize_url
from .capabilities import ProviderErrorKind, classify_http_status
from .raw_exchange import RawExchange, build_exchange_from_parts

#: Content types a data provider may return. A response outside this set is
#: rejected rather than parsed (defends against an HTML error/redirect page).
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"application/json", "application/geo+json", "application/ld+json", "text/json"}
)

#: Hard cap on a stored body (bytes). A larger response is refused, not stored.
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024  # 8 MiB

#: HTTP statuses worth retrying (transient). 429 is handled with Retry-After.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class ProviderError(RuntimeError):
    """A sanitized provider failure carrying its classification and exchange.

    ``kind`` distinguishes authentication from a subscription-tier restriction
    (see :mod:`~sports_quant.providers.capabilities`), so a caller can let
    unrelated supported capabilities continue and never mislabels a tier gate as
    an invalid key. ``message`` is already sanitized; ``exchange`` is present
    when a well-formed HTTP response (e.g. a 4xx/5xx body) was received.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: ProviderErrorKind,
        status_code: Optional[int] = None,
        exchange: Optional[RawExchange] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.exchange = exchange


@dataclass(frozen=True)
class ProviderResponse:
    """The parsed JSON payload plus its sanitized raw exchange."""

    data: Any
    exchange: RawExchange


class BaseProviderClient:
    """Async, read-only, GET-only base client for a single provider host."""

    #: Overridden by subclasses; recorded on rows and used for error classification.
    provider_name: str = "provider"

    def __init__(
        self,
        *,
        base_url: str,
        policy: ReadOnlyHTTPPolicy,
        client: Optional[httpx.AsyncClient] = None,
        default_headers: Optional[Mapping[str, str]] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep: Any = asyncio.sleep,
        redact_values: Iterable[str] = (),
    ) -> None:
        self._base_url = base_url
        # Secrets always stripped from every stored body (e.g. a header key that
        # a provider might echo). Never affects behaviour, only redaction.
        self._always_redact = [v for v in redact_values if v]
        self._owns_client = client is None
        # follow_redirects defaults to False in build_readonly_client, so a 3xx
        # is never chased to an unapproved host.
        self._client = client or build_readonly_client(
            base_url=base_url,
            policy=policy,
            timeout=timeout,
            headers=dict(default_headers or {}),
        )
        self._max_retries = max(0, max_retries)
        self._max_body_bytes = max_body_bytes
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep

    async def __aenter__(self) -> "BaseProviderClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- Core GET ------------------------------------------------------------
    async def _get(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        secret: Optional[str] = None,
        secret_param: Optional[str] = None,
    ) -> ProviderResponse:
        """Perform one GET, returning parsed JSON + a sanitized RawExchange.

        ``secret``/``secret_param`` inject an API key into the query (BALLDONTLIE
        uses a header instead, so it does not use these); the key is stripped
        from every stored field. Retries honour ``Retry-After`` and back off
        exponentially with jitterless, bounded delays.
        """

        request_params: dict[str, Any] = dict(params or {})
        secrets: list[str] = list(self._always_redact)
        if secret and secret_param:
            request_params[secret_param] = secret
            secrets.append(secret)

        attempt = 0
        while True:
            requested_at = datetime.now(timezone.utc)
            started_ns = time.monotonic_ns()
            request = self._client.build_request("GET", path, params=request_params)
            try:
                # stream=True so the body is read chunk-by-chunk and the size cap
                # is enforced BEFORE the whole response is buffered into memory.
                response = await self._client.send(request, stream=True)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    await self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise ProviderError(
                    f"{self.provider_name} request failed: "
                    f"{sanitize_url(str(exc), secrets)}",
                    kind=ProviderErrorKind.NETWORK,
                ) from None

            body, oversized = await self._read_bounded(response)
            elapsed_ns = time.monotonic_ns() - started_ns
            status_code = response.status_code

            if oversized:
                # No exchange, no body: an oversized response never reaches
                # raw-response storage. Only a sanitized failure record survives.
                raise ProviderError(
                    f"{self.provider_name} response exceeded the maximum body size "
                    f"(> {self._max_body_bytes} bytes) for {path}",
                    kind=ProviderErrorKind.UNEXPECTED,
                    status_code=status_code,
                )

            exchange = build_exchange_from_parts(
                path=path,
                params=request_params,
                status_code=status_code,
                headers=response.headers,
                body=body,
                requested_at=requested_at,
                elapsed_ns=elapsed_ns,
                secrets=secrets,
            )

            if status_code in _RETRYABLE_STATUSES and attempt < self._max_retries:
                await self._sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue

            if status_code >= 400:
                self._raise_for_status(status_code, exchange)

            self._check_content_type(response, exchange)
            return ProviderResponse(data=self._parse_json(body, exchange), exchange=exchange)

    async def _read_bounded(self, response: httpx.Response) -> tuple[str, bool]:
        """Read a streamed body, aborting once it exceeds the size cap.

        Returns ``(decoded_body, oversized)``. When ``oversized`` is True the
        body is discarded (never returned/stored). Counts actual bytes, so a
        misleading or missing ``Content-Length`` cannot smuggle an oversized
        payload past the cap.
        """

        buffer = bytearray()
        oversized = False
        try:
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > self._max_body_bytes:
                    oversized = True
                    break
        finally:
            await response.aclose()
        if oversized:
            return "", True
        return buffer.decode("utf-8", errors="replace"), False

    # -- Helpers -------------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_cap, self._backoff_base * (2**attempt))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """Delay before a retry, honouring ``Retry-After`` when present."""

        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(self._backoff_cap, float(retry_after))
            except ValueError:
                pass  # HTTP-date form: fall back to exponential backoff
        return self._backoff(attempt)

    def _raise_for_status(self, status_code: int, exchange: RawExchange) -> None:
        # exchange.body is already sanitized; use a short snippet for wording.
        snippet = exchange.body[:200]
        kind = classify_http_status(
            status_code, body_snippet=snippet, provider=self.provider_name
        )
        raise ProviderError(
            f"{self.provider_name} responded {status_code} "
            f"({kind.value}) for {exchange.endpoint}",
            kind=kind,
            status_code=status_code,
            exchange=exchange,
        )

    def _check_content_type(self, response: httpx.Response, exchange: RawExchange) -> None:
        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip()
        if content_type and content_type.lower() not in ALLOWED_CONTENT_TYPES:
            raise ProviderError(
                f"{self.provider_name} returned unexpected content-type "
                f"{content_type!r} for {exchange.endpoint}",
                kind=ProviderErrorKind.UNEXPECTED,
                status_code=exchange.http_status,
                exchange=exchange,
            )

    def _parse_json(self, body: str, exchange: RawExchange) -> Any:
        import json

        try:
            return json.loads(body)
        except (ValueError, TypeError):
            raise ProviderError(
                f"{self.provider_name} returned an unparseable JSON body for "
                f"{exchange.endpoint}",
                kind=ProviderErrorKind.PARSER,
                status_code=exchange.http_status,
                exchange=exchange,
            ) from None


def merge_secrets(*values: Optional[str]) -> Iterable[str]:
    """Non-empty secret values, for :func:`build_exchange`'s ``secrets``."""

    return [v for v in values if v]
