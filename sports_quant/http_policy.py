"""Central hard read-only networking policy.

Every outbound HTTP request in the read-only lane is forced through this policy
by wrapping the real transport in :class:`ReadOnlyPolicyTransport`. The policy
is *default-deny*:

* Only the ``GET`` method is permitted. ``POST``/``PUT``/``PATCH``/``DELETE``
  (and anything else) are rejected before the request can leave the process.
* Only explicitly approved hosts are reachable.
* On each approved host, only an allow-list of public-data paths is reachable.
  For Kalshi that is::

      /events            /markets                    /markets/trades
      /series            /markets/{ticker}           /exchange/status
                         /markets/{ticker}/orderbook

  Account, portfolio, balance, order, fill and position paths are rejected
  explicitly (and, being outside the allow-list, would be rejected anyway).

Because enforcement lives in the transport, it applies uniformly to real
network calls *and* to mocked transports used in tests -- there is no code path
that reaches an exchange without passing the policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx

# The only HTTP method the read-only lane may ever use.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET"})

# Path segments that must never be reached on Kalshi even via GET: these are the
# authenticated, account-scoped surfaces (positions, balances, orders, fills).
FORBIDDEN_KALSHI_SEGMENTS: frozenset[str] = frozenset(
    {"portfolio", "account", "accounts", "balance", "orders", "order", "fills", "positions"}
)


class ReadOnlyPolicyError(RuntimeError):
    """Raised when a request violates the hard read-only networking policy."""


@dataclass(frozen=True)
class HostRule:
    """Allow-list for a single host.

    ``base_path`` (e.g. ``/trade-api/v2``) is stripped before matching so the
    patterns can be written against the public API surface rather than the
    versioned prefix. ``forbidden_segments`` gives pointed rejection messages
    for known-sensitive paths.
    """

    host: str
    allowed_patterns: tuple[re.Pattern[str], ...]
    base_path: str = ""
    forbidden_segments: frozenset[str] = frozenset()

    def suffix(self, path: str) -> str:
        if self.base_path and path.startswith(self.base_path):
            path = path[len(self.base_path):]
        return path or "/"

    def matches(self, path: str) -> bool:
        suffix = self.suffix(path)
        return any(pattern.fullmatch(suffix) for pattern in self.allowed_patterns)


def kalshi_host_rule(base_url: str) -> HostRule:
    """Build the Kalshi public-data allow-list from its base REST URL."""

    parts = httpx.URL(base_url)
    return HostRule(
        host=parts.host,
        base_path=parts.path.rstrip("/"),
        forbidden_segments=FORBIDDEN_KALSHI_SEGMENTS,
        allowed_patterns=(
            # list events + get a single event
            re.compile(r"/events(/[^/]+)?"),
            # list markets
            re.compile(r"/markets"),
            # public trades feed
            re.compile(r"/markets/trades"),
            # get a single market
            re.compile(r"/markets/[^/]+"),
            # a market's order book
            re.compile(r"/markets/[^/]+/orderbook"),
            # list series + get a single series
            re.compile(r"/series(/[^/]+)?"),
            # exchange status
            re.compile(r"/exchange/status"),
        ),
    )


def odds_api_host_rule(host: str = "api.the-odds-api.com") -> HostRule:
    """Build The Odds API allow-list (sports list + per-sport odds)."""

    return HostRule(
        host=host,
        allowed_patterns=(
            re.compile(r"/v4/sports/?"),
            re.compile(r"/v4/sports/[^/]+/odds/?"),
        ),
    )


class ReadOnlyHTTPPolicy:
    """Validates ``(method, url)`` pairs against the read-only allow-list."""

    def __init__(self, rules: Iterable[HostRule]) -> None:
        self._rules: dict[str, HostRule] = {rule.host: rule for rule in rules}

    @classmethod
    def for_kalshi(cls, base_url: str) -> "ReadOnlyHTTPPolicy":
        return cls([kalshi_host_rule(base_url)])

    @classmethod
    def for_odds_api(cls, host: str = "api.the-odds-api.com") -> "ReadOnlyHTTPPolicy":
        return cls([odds_api_host_rule(host)])

    def enforce(self, method: str, url: httpx.URL | str) -> None:
        """Raise :class:`ReadOnlyPolicyError` unless the request is permitted."""

        if isinstance(url, str):
            url = httpx.URL(url)

        method_upper = method.upper()
        if method_upper not in ALLOWED_METHODS:
            raise ReadOnlyPolicyError(
                f"method {method_upper} is blocked by the read-only policy "
                f"(only {sorted(ALLOWED_METHODS)} permitted)"
            )

        host = url.host
        rule = self._rules.get(host)
        if rule is None:
            raise ReadOnlyPolicyError(
                f"host {host!r} is not on the approved read-only allow-list"
            )

        path = url.path
        segments = {seg for seg in path.split("/") if seg}
        forbidden = segments & rule.forbidden_segments
        if forbidden:
            raise ReadOnlyPolicyError(
                f"path {path!r} touches account/portfolio/order surface "
                f"({sorted(forbidden)}); blocked in read-only mode"
            )

        if not rule.matches(path):
            raise ReadOnlyPolicyError(
                f"path {path!r} is not on the approved read-only allow-list for {host!r}"
            )


class ReadOnlyPolicyTransport(httpx.AsyncBaseTransport):
    """An ``httpx`` transport wrapper that enforces the policy on every request.

    Wrapping an inner transport (a real ``AsyncHTTPTransport`` in production, a
    ``MockTransport`` in tests) guarantees no request is dispatched without
    first clearing :meth:`ReadOnlyHTTPPolicy.enforce`.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, policy: ReadOnlyHTTPPolicy) -> None:
        self._inner = inner
        self._policy = policy

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._policy.enforce(request.method, request.url)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_readonly_client(
    *,
    base_url: str,
    policy: ReadOnlyHTTPPolicy,
    timeout: float = 15.0,
    headers: Optional[dict[str, str]] = None,
    inner_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` whose every request clears the policy."""

    inner = inner_transport if inner_transport is not None else httpx.AsyncHTTPTransport()
    return httpx.AsyncClient(
        base_url=base_url,
        transport=ReadOnlyPolicyTransport(inner, policy),
        timeout=timeout,
        headers=headers or {},
    )
