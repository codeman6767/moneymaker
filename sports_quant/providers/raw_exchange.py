"""Shared, sanitized record of one HTTP exchange.

Both provider adapters (The Odds API and Kalshi public) must hand the ingestion
lane the exact bytes of a response *before* it is normalized, together with the
status code, timing, and the request that produced it. That capture is the same
shape for every provider, so it lives here once rather than being re-implemented
per adapter.

The model is constructed already-safe, so no downstream caller has to remember
to redact:

* ``endpoint`` is the request **path** only -- never a full URL, never a query
  string, so a query-string secret (the Odds API ``?apiKey=``) cannot travel
  with it.
* ``request_params`` passes :func:`sanitize_params`, which masks by parameter
  *name*.
* ``response_headers`` passes the :func:`sanitize_headers` allow-list, so an
  ``Authorization``/``Set-Cookie`` header can never be captured.
* ``body`` has any explicitly supplied secret stripped as a final guard.

Kalshi's public surface is unauthenticated, so it supplies no secrets; the same
sanitization runs anyway (a no-op there) rather than branching per provider.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import httpx
from pydantic import BaseModel, ConfigDict

from ..redaction import redact_secrets, sanitize_headers, sanitize_params


class RawExchange(BaseModel):
    """A sanitized, storable record of one HTTP exchange."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str
    request_params: dict[str, str]
    http_status: int
    response_headers: dict[str, str]
    content_type: Optional[str] = None
    #: Wall-clock moment the request was issued.
    requested_at: datetime
    #: Wall-clock moment the response arrived. This is the corpus's
    #: ``observed_at`` for every fact derived from this exchange.
    received_at: datetime
    #: Monotonic elapsed time; never derived from the two wall-clocks above,
    #: which can step.
    elapsed_ns: int
    body: str


def build_exchange(
    *,
    path: str,
    params: Mapping[str, Any],
    response: httpx.Response,
    requested_at: datetime,
    elapsed_ns: int,
    secrets: Iterable[str] = (),
) -> RawExchange:
    """Capture one exchange in already-sanitized form (see :class:`RawExchange`)."""

    return RawExchange(
        endpoint=path,
        request_params={k: str(v) for k, v in sanitize_params(params).items()},
        http_status=response.status_code,
        response_headers=sanitize_headers(response.headers),
        content_type=response.headers.get("content-type"),
        requested_at=requested_at,
        received_at=datetime.now(timezone.utc),
        elapsed_ns=elapsed_ns,
        body=redact_secrets(response.text, list(secrets)),
    )


def build_exchange_from_parts(
    *,
    path: str,
    params: Mapping[str, Any],
    status_code: int,
    headers: Mapping[str, str],
    body: str,
    requested_at: datetime,
    elapsed_ns: int,
    secrets: Iterable[str] = (),
) -> RawExchange:
    """Capture an exchange from already-read parts (streaming path).

    Used when the body is read chunk-by-chunk with a size guard rather than
    buffered by ``httpx`` -- the sanitization is identical to :func:`build_exchange`.
    """

    return RawExchange(
        endpoint=path,
        request_params={k: str(v) for k, v in sanitize_params(params).items()},
        http_status=status_code,
        response_headers=sanitize_headers(headers),
        content_type=headers.get("content-type"),
        requested_at=requested_at,
        received_at=datetime.now(timezone.utc),
        elapsed_ns=elapsed_ns,
        body=redact_secrets(body, list(secrets)),
    )
