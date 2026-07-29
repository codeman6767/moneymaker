"""F1A shared request- and credit-budget control layer (offline).

Every real provider transport attempt (initial call, pagination call, and each
retry) must pass through a single :class:`RequestGate` *reservation* before the
network is touched. The gate is injected into
:class:`~sports_quant.providers.base_provider.BaseProviderClient` at its one GET
chokepoint, so an endpoint helper cannot reach a transport outside the budget
gate. When a request cannot fit inside the remaining request **or** credit budget
the gate raises :class:`BudgetExhausted` *before* the transport is invoked; the
run stops in a controlled, truncated, resumable state.

This module performs **no** network or database I/O. It is pure accounting:

* :class:`RequestUnit`      -- the secret-free semantic identity of one request.
* :class:`EndpointCostPolicy` -- typed endpoint-family -> conservative credit cost.
* :class:`RequestBudget`    -- a hard maximum request count.
* :class:`CreditBudget`     -- a hard maximum credit count (or "not applicable").
* :class:`UsageReport`      -- deterministic usage/truncation accounting.
* :class:`RequestGate`      -- concurrency-safe reserve/commit of both budgets.
* :class:`BudgetExhausted`  -- raised before a transport that would exceed a cap.

Credit values are kept strictly separate: *estimated* (planner), *reserved*
(conservative maximum taken before a call), *reported-consumed* (provider), and
*provider-reported-remaining* (provider). A remaining figure is never invented.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional


class LimitType(str, Enum):
    """Which budget a :class:`BudgetExhausted` refers to."""

    REQUEST = "request"
    CREDIT = "credit"
    #: A single request whose conservative credit cost is unknown while a credit
    #: cap is in force -- the plan/run is not executable (fail closed).
    UNKNOWN_CREDIT_COST = "unknown_credit_cost"
    #: A request whose path classifies to an endpoint family the provider's policy
    #: does not recognise -- fail closed rather than issue an unmodelled call.
    UNKNOWN_ENDPOINT = "unknown_endpoint"


@dataclass(frozen=True)
class RequestUnit:
    """The secret-free semantic identity of one provider request.

    Identity is a pure function of the fields below; it never contains an API
    key, an authorization header, a secret-bearing URL, a random id, or a
    wall-clock time. Two logically-identical requests share an identity string,
    which is what makes plans, manifests, and checkpoints reproducible.
    """

    provider: str
    league: str
    endpoint_family: str
    #: A date, an inclusive date range ("YYYY-MM-DD..YYYY-MM-DD"), or "".
    date_key: str = ""
    #: A stable provider/game/team entity key when known ("" otherwise).
    entity_key: str = ""
    #: 1-based page number for a paginated family (0 = not paginated).
    page: int = 0
    #: Normalized, non-secret query parameters (sorted at serialization).
    params: tuple[tuple[str, str], ...] = ()

    def identity(self) -> str:
        """A canonical, stable identity string (no secrets, no wall-clock)."""

        payload = {
            "provider": self.provider,
            "league": self.league,
            "endpoint_family": self.endpoint_family,
            "date_key": self.date_key,
            "entity_key": self.entity_key,
            "page": self.page,
            "params": [list(p) for p in sorted(self.params)],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class EndpointCostPolicy:
    """Typed endpoint-family -> conservative credit cost for one provider.

    ``credit_applicable`` is False for a keyless/unmetered provider (MLB
    StatsAPI): requests are still hard-capped, but credits are reported *not
    applicable* and never fabricated. When credits are applicable, an endpoint
    family absent from ``costs`` has an **unknown** cost: any credit-capped plan
    or run that reaches it is non-executable / fails closed (never assumed 1).
    ``version`` is recorded in plans, manifests, and checkpoints.
    """

    def __init__(
        self,
        *,
        provider: str,
        version: str,
        credit_applicable: bool,
        costs: Mapping[str, int],
        classifier: Optional[Callable[[str], str]] = None,
        known_families: Optional[frozenset[str]] = None,
    ) -> None:
        self.provider = provider
        self.version = version
        self.credit_applicable = credit_applicable
        self._costs = dict(costs)
        self._classifier = classifier
        self._known_families = known_families

    def classify(self, path: str) -> str:
        """Map a raw request path to its endpoint family ("unknown" if none)."""

        if self._classifier is None:
            return "unknown"
        return self._classifier(path)

    def is_known_family(self, endpoint_family: str) -> bool:
        """True when the family is recognised. When ``known_families`` is declared,
        a family outside it (e.g. the ``unknown`` fallback) fails closed at the gate
        even for a request-rate-limited provider with no credit cost."""

        if self._known_families is None:
            return True
        return endpoint_family in self._known_families

    def cost_for(self, endpoint_family: str) -> Optional[int]:
        """Conservative credit cost, or ``None`` when unknown/not applicable.

        ``None`` when credits are not applicable (keyless provider) OR when the
        family is not in the policy (unknown -> fail closed under a credit cap).
        """

        if not self.credit_applicable:
            return None
        return self._costs.get(endpoint_family)

    def known(self, endpoint_family: str) -> bool:
        """True when the family's credit cost is explicitly known (or N/A)."""

        return (not self.credit_applicable) or (endpoint_family in self._costs)


class BudgetExhausted(RuntimeError):
    """Raised *before* a transport that would exceed a request/credit cap.

    Carries everything the runner needs to report a controlled truncation and to
    decide resumability. Never carries a secret.
    """

    def __init__(
        self,
        *,
        limit_type: LimitType,
        cap: Optional[int],
        consumed: int,
        remaining: Optional[int],
        blocked_family: str,
        blocked_identity: str,
        resumable: bool,
    ) -> None:
        self.limit_type = limit_type
        self.cap = cap
        self.consumed = consumed
        self.remaining = remaining
        self.blocked_family = blocked_family
        self.blocked_identity = blocked_identity
        self.resumable = resumable
        super().__init__(
            f"budget exhausted ({limit_type.value}): cap={cap} consumed={consumed} "
            f"remaining={remaining} blocked_family={blocked_family}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit_type": self.limit_type.value,
            "cap": self.cap,
            "consumed": self.consumed,
            "remaining": self.remaining,
            "blocked_family": self.blocked_family,
            "blocked_identity": self.blocked_identity,
            "resumable": self.resumable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetExhausted":
        return cls(
            limit_type=LimitType(data["limit_type"]),
            cap=data.get("cap"),
            consumed=int(data.get("consumed", 0)),
            remaining=data.get("remaining"),
            blocked_family=str(data.get("blocked_family", "")),
            blocked_identity=str(data.get("blocked_identity", "")),
            resumable=bool(data.get("resumable", True)),
        )


@dataclass
class RequestBudget:
    """A hard maximum request count. ``max_requests=0`` permits no transport."""

    max_requests: int

    def __post_init__(self) -> None:
        if self.max_requests < 0:
            raise ValueError("max_requests must be >= 0")


@dataclass
class CreditBudget:
    """A hard maximum credit count, or an explicit "not applicable".

    ``applicable=False`` means the provider is unmetered (keyless); no credit
    ceiling is enforced and credits are reported N/A. When ``applicable=True``,
    ``max_credits`` must be set and is enforced conservatively.
    """

    applicable: bool
    max_credits: Optional[int] = None

    def __post_init__(self) -> None:
        if self.applicable and (self.max_credits is None or self.max_credits < 0):
            raise ValueError("an applicable credit budget requires max_credits >= 0")


@dataclass(frozen=True)
class RequestRatePolicy:
    """A versioned provider request-RATE contract (requests per minute).

    Some providers (BALLDONTLIE) meter by a per-minute REQUEST-RATE limit per
    subscription tier, NOT by a monetary/credit balance. This is distinct from the
    aggregate request budget (a hard total-call ceiling for the whole logical run):
    the rate policy bounds how FAST calls may be issued. ``configured_per_min`` is
    the safe operating rate the runtime honours and MUST be <= the provider's
    ``tier_max_per_min`` (a conservative default sits well below the tier maximum).
    """

    provider: str
    version: str
    tier: str
    tier_max_per_min: int
    configured_per_min: int

    def __post_init__(self) -> None:
        if self.tier_max_per_min <= 0 or self.configured_per_min <= 0:
            raise ValueError("rate limits must be positive")
        if self.configured_per_min > self.tier_max_per_min:
            raise ValueError(
                f"configured rate {self.configured_per_min}/min exceeds the verified "
                f"tier maximum {self.tier_max_per_min}/min for {self.provider} ({self.tier})")


class RateLimiter:
    """A client-side sliding-window rate limiter (requests per ``window`` seconds).

    Concurrency-safe. :meth:`acquire_wait` returns the seconds the caller must wait
    (0 when a slot is free now) before issuing a request to stay within
    ``per_min``, reserving the slot. The clock is injectable for deterministic
    tests; the async transport awaits the returned delay before sending.
    """

    def __init__(self, per_min: int, *, clock: Callable[[], float] = time.monotonic,
                 window: float = 60.0) -> None:
        if per_min <= 0:
            raise ValueError("per_min must be positive")
        self._per_min = per_min
        self._clock = clock
        self._window = window
        self._times: "deque[float]" = deque()
        self._lock = threading.Lock()

    def acquire_wait(self) -> float:
        with self._lock:
            now = self._clock()
            horizon = now - self._window
            while self._times and self._times[0] <= horizon:
                self._times.popleft()
            if len(self._times) < self._per_min:
                self._times.append(now)
                return 0.0
            # Wait until the oldest reservation leaves the window; reserve the slot
            # at that future instant so concurrent callers serialise correctly.
            wait = self._times[0] + self._window - now
            self._times.append(now + max(0.0, wait))
            return max(0.0, wait)


@dataclass
class UsageReport:
    """Deterministic usage/truncation accounting produced by a run or plan.

    Estimated / reserved / reported-consumed / provider-remaining credit figures
    are kept distinct and never conflated. Field order is fixed for stable JSON.
    """

    provider: str = ""
    league: str = ""
    planned_requests: int = 0
    estimated_requests_min: int = 0
    estimated_requests_max: int = 0
    # Logical-run carry-over from prior resumed processes (counted against the cap).
    prior_requests: int = 0
    prior_credits: int = 0
    # Staged accounting (each reserved attempt reaches exactly one terminal state):
    reserved_attempts: int = 0        # a budget slot was taken (may never send)
    attempted_requests: int = 0       # alias of reserved_attempts (back-compat)
    transport_starts: int = 0         # an actual transport send was attempted
    responses_received: int = 0       # a complete HTTP response body was received
    parse_successes: int = 0          # the body parsed as JSON
    successful_responses: int = 0     # a fully-successful ProviderResponse returned
    failed_responses: int = 0         # a terminal failure (network/status/parse/oversize)
    retry_attempts: int = 0
    pages_fetched: int = 0
    skipped_on_resume: int = 0
    blocked_requests: int = 0
    estimated_credits_min: Optional[int] = None
    estimated_credits_max: Optional[int] = None
    reserved_credits: int = 0
    reported_credits_consumed: Optional[int] = None
    provider_credits_remaining: Optional[int] = None
    credits_applicable: bool = False
    credit_header_status: str = "not_applicable"  # not_applicable|absent|present|inconsistent
    # Request-RATE accounting (distinct from the aggregate request budget and from
    # any credit balance): the provider's per-minute tier ceiling, the configured
    # safe operating rate, cumulative client-side throttle wait, and observed 429s.
    rate_limited: bool = False
    provider_rate_limit_per_min: Optional[int] = None
    configured_rate_per_min: Optional[int] = None
    throttle_wait_seconds: float = 0.0
    http_429s: int = 0
    families_completed: tuple[str, ...] = ()
    families_failed: tuple[str, ...] = ()
    families_truncated: tuple[str, ...] = ()
    budget_exhausted: Optional[dict[str, Any]] = None
    network_occurred: bool = False
    database_mutated: bool = False
    manifest_hash: str = ""
    checkpoint_state: str = "none"  # none|written|resumed|completed|truncated

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RequestGate:
    """Concurrency-safe reserve/commit of the request and credit budgets.

    A single lock makes every reservation atomic: the request slot **and** the
    conservative credit cost are both checked and taken together, or neither is
    (no partial reservation). :meth:`reserve` is called before *every* transport
    attempt, including each retry and each pagination page; on failure it raises
    :class:`BudgetExhausted` and takes nothing.
    """

    def __init__(
        self,
        *,
        request_budget: RequestBudget,
        credit_budget: CreditBudget,
        cost_policy: EndpointCostPolicy,
        usage: Optional[UsageReport] = None,
        resumable: bool = True,
        rate_policy: Optional[RequestRatePolicy] = None,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self._req = request_budget
        self._credit = credit_budget
        self._policy = cost_policy
        self._lock = threading.Lock()
        self._resumable = resumable
        self._rate_policy = rate_policy
        self._limiter = RateLimiter(rate_policy.configured_per_min) if rate_policy else None
        self.usage = usage or UsageReport(provider=cost_policy.provider)
        self.usage.credits_applicable = credit_budget.applicable
        if not credit_budget.applicable:
            self.usage.credit_header_status = "not_applicable"
        if rate_policy is not None:
            self.usage.rate_limited = True
            self.usage.provider_rate_limit_per_min = rate_policy.tier_max_per_min
            self.usage.configured_rate_per_min = rate_policy.configured_per_min

    @property
    def cost_policy(self) -> EndpointCostPolicy:
        return self._policy

    @property
    def rate_policy(self) -> Optional[RequestRatePolicy]:
        return self._rate_policy

    def rate_acquire(self) -> float:
        """Reserve a rate slot; return the seconds the caller must wait before the
        actual transport send to stay within the configured per-minute rate. The
        wait is accumulated in ``throttle_wait_seconds`` (0 when a slot is free)."""

        if self._limiter is None:
            return 0.0
        wait = self._limiter.acquire_wait()
        if wait > 0:
            with self._lock:
                self.usage.throttle_wait_seconds += wait
        return wait

    def record_429(self) -> None:
        """Record an observed HTTP 429 (rate-limit) response (handled via backoff)."""

        with self._lock:
            self.usage.http_429s += 1

    def seed_prior(self, *, prior_requests: int, prior_credits: int) -> None:
        """Pre-charge the gate with a previous process's usage on resume.

        The manifest's request/credit caps apply to the ENTIRE logical run across
        all resumed processes, so a resume must not get a fresh budget. Prior
        attempts/credits are counted against the cap here (an uncertain interrupted
        request is treated conservatively as consumed), and reported separately as
        ``prior_*`` so current-process usage stays distinguishable.
        """

        with self._lock:
            self.usage.prior_requests = max(0, int(prior_requests))
            self.usage.prior_credits = max(0, int(prior_credits))
            self.usage.attempted_requests = self.usage.prior_requests
            self.usage.reserved_attempts = self.usage.prior_requests
            self.usage.reserved_credits = self.usage.prior_credits

    # -- reservation ---------------------------------------------------------
    def reserve(self, unit: RequestUnit, *, is_retry: bool = False) -> None:
        """Atomically reserve one request (+ credit cost) before a transport.

        Raises :class:`BudgetExhausted` (taking nothing) when the request slot,
        the credit cost, or a required-but-unknown credit cost does not fit.
        """

        family = unit.endpoint_family
        with self._lock:
            # Unrecognised endpoint family -> fail closed (never issue an unmodelled
            # call), independent of whether the provider is credit-metered.
            if not self._policy.is_known_family(family):
                self.usage.blocked_requests += 1
                exc = BudgetExhausted(
                    limit_type=LimitType.UNKNOWN_ENDPOINT,
                    cap=self._req.max_requests, consumed=self.usage.attempted_requests,
                    remaining=max(0, self._req.max_requests - self.usage.attempted_requests),
                    blocked_family=family, blocked_identity=unit.identity(),
                    resumable=self._resumable)
                self.usage.budget_exhausted = exc.as_dict()
                raise exc
            # Unknown credit cost under an applicable credit cap -> fail closed.
            cost: Optional[int] = None
            if self._credit.applicable:
                cost = self._policy.cost_for(family)
                if cost is None:
                    self.usage.blocked_requests += 1
                    exc = BudgetExhausted(
                        limit_type=LimitType.UNKNOWN_CREDIT_COST,
                        cap=self._credit.max_credits,
                        consumed=self.usage.reserved_credits,
                        remaining=self._credit_remaining(),
                        blocked_family=family,
                        blocked_identity=unit.identity(),
                        resumable=self._resumable,
                    )
                    self.usage.budget_exhausted = exc.as_dict()
                    raise exc

            # Request slot.
            if self.usage.attempted_requests + 1 > self._req.max_requests:
                self.usage.blocked_requests += 1
                exc = BudgetExhausted(
                    limit_type=LimitType.REQUEST,
                    cap=self._req.max_requests,
                    consumed=self.usage.attempted_requests,
                    remaining=max(0, self._req.max_requests - self.usage.attempted_requests),
                    blocked_family=family,
                    blocked_identity=unit.identity(),
                    resumable=self._resumable,
                )
                self.usage.budget_exhausted = exc.as_dict()
                raise exc

            # Credit slot (conservative maximum reserved up front).
            if self._credit.applicable and cost is not None:
                assert self._credit.max_credits is not None
                if self.usage.reserved_credits + cost > self._credit.max_credits:
                    self.usage.blocked_requests += 1
                    exc = BudgetExhausted(
                        limit_type=LimitType.CREDIT,
                        cap=self._credit.max_credits,
                        consumed=self.usage.reserved_credits,
                        remaining=self._credit_remaining(),
                        blocked_family=family,
                        blocked_identity=unit.identity(),
                        resumable=self._resumable,
                    )
                    self.usage.budget_exhausted = exc.as_dict()
                    raise exc
                self.usage.reserved_credits += cost

            # Commit the RESERVATION only after both checks pass. A reservation is
            # not yet a transport call: `network_occurred`/`transport_starts` are
            # recorded by mark_transport() from the GET chokepoint when a send is
            # actually attempted, so a reserved-but-never-sent attempt never falsely
            # reports network activity.
            self.usage.reserved_attempts += 1
            self.usage.attempted_requests = self.usage.reserved_attempts
            if is_retry:
                self.usage.retry_attempts += 1

    def _credit_remaining(self) -> Optional[int]:
        if not self._credit.applicable or self._credit.max_credits is None:
            return None
        return max(0, self._credit.max_credits - self.usage.reserved_credits)

    # -- staged transport recording -----------------------------------------
    def mark_transport(self, *, page: bool = False) -> None:
        """A real transport send was attempted (distinct from a reservation)."""

        with self._lock:
            self.usage.network_occurred = True
            self.usage.transport_starts += 1
            if page:
                self.usage.pages_fetched += 1

    def record_response(self) -> None:
        """A complete HTTP response body was received (any status)."""

        with self._lock:
            self.usage.responses_received += 1

    def record_parse_success(self) -> None:
        with self._lock:
            self.usage.parse_successes += 1

    # -- outcome recording ---------------------------------------------------
    def record_success(self) -> None:
        with self._lock:
            self.usage.successful_responses += 1

    def record_failure(self) -> None:
        with self._lock:
            self.usage.failed_responses += 1

    def record_provider_credits(
        self, *, remaining: Optional[int], consumed: Optional[int]
    ) -> None:
        """Record trustworthy provider-reported credit headers, honestly.

        Never invents values. If credits are not applicable, or the headers are
        absent, that is recorded as-is. A remaining figure that contradicts our
        conservative reservation is flagged ``inconsistent`` rather than trusted.
        """

        with self._lock:
            if not self._credit.applicable:
                self.usage.credit_header_status = "not_applicable"
                return
            if remaining is None and consumed is None:
                self.usage.credit_header_status = "absent"
                return
            self.usage.provider_credits_remaining = remaining
            self.usage.reported_credits_consumed = consumed
            status = "present"
            if (
                remaining is not None
                and self._credit.max_credits is not None
                and remaining < 0
            ):
                status = "inconsistent"
            self.usage.credit_header_status = status
