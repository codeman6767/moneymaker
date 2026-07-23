"""Provider audit: evidence-backed, multi-probe capability verification.

Before any large backfill (D2/D3), ``provider-audit`` runs one minimal approved
GET **per capability group** and records what each probe actually verified. It
draws a hard line between:

* **declared** capabilities -- what documentation/config *expects* the provider
  (at the selected tier) to support. Persisted as ``is_observed = 0`` metadata,
  never as an endpoint observation.
* **observed** capabilities -- what an exact probe *verified* at a specific time,
  carrying the probe name, sanitized endpoint, HTTP status, error classification,
  and the raw-response id that is the evidence. Persisted as ``is_observed = 1``.

A successful ``/teams`` response therefore marks **only** teams (its group)
observed -- never injuries, stats, box scores, plays, or lineups. Capabilities
with no probe stay declared-only / ``unknown_until_audited``. A tier restriction
affects only its own group; a 401 marks nothing supported. Nothing is fabricated.

D1 exercises this against mocked transports only; no live call is made.
``--dry-run`` runs the probes in memory and persists absolutely nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.repositories.capabilities import SqliteCapabilityRepository
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from ..db.schema import to_iso
from ..providers.base_provider import ProviderError, ProviderResponse
from ..providers.capabilities import (
    PROVIDER_BALLDONTLIE,
    PROVIDER_MLB_STATSAPI,
    PROVIDER_NWS,
    PROVIDER_OPEN_METEO,
    CapabilityDeclaration,
    CapabilityState,
    ProviderCapability,
    ProviderErrorKind,
)
from ..providers.raw_exchange import RawExchange
from .runner import sanitize_error

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "provider-audit"

#: Providers this D1 audit supports.
SUPPORTED_AUDIT_PROVIDERS = (
    PROVIDER_MLB_STATSAPI,
    PROVIDER_BALLDONTLIE,
    PROVIDER_NWS,
    PROVIDER_OPEN_METEO,
)


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
@dataclass
class CapabilityProbe:
    """One minimal approved GET that provides evidence for a capability group.

    ``fetch`` returns a :class:`ProviderResponse` (raw exchange included) on
    success or raises :class:`ProviderError`. ``capabilities`` is the exact set
    this probe verifies -- and nothing else. ``endpoint`` is the sanitized
    endpoint/family recorded on each observation.
    """

    name: str
    endpoint: str
    capabilities: tuple[ProviderCapability, ...]
    fetch: Callable[[], Awaitable[ProviderResponse]]


@dataclass
class CapabilityObservation:
    """One capability + the evidence-backed conclusion the audit drew for it.

    ``is_observed`` is True only when a probe actually verified it; declared-only
    rows keep ``observed_state``/``probe_name``/``endpoint`` at ``None``.
    """

    capability: str
    state: str  # effective belief recorded (observed_state if observed, else declared)
    declared_state: Optional[str]
    observed_state: Optional[str]
    is_observed: bool
    probe_name: Optional[str] = None
    endpoint: Optional[str] = None
    http_status: Optional[int] = None
    error_kind: Optional[str] = None
    detail: Optional[str] = None
    raw_response_id: Optional[str] = None  # filled at persist time


@dataclass
class ProviderAuditResult:
    """Sanitized outcome of one provider audit, safe to print/JSON."""

    provider: str
    tier: Optional[str]
    dry_run: bool
    status: str
    run_id: Optional[str] = None
    requests_made: int = 0
    authenticated: Optional[bool] = None
    tier_restricted: bool = False
    observations: list[CapabilityObservation] = field(default_factory=list)
    capabilities_recorded: int = 0
    observed_count: int = 0
    declared_only_count: int = 0
    issues_recorded: int = 0
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass
class _ProbeResult:
    """Internal: outcome of running one probe, with its raw exchange (if any)."""

    probe: CapabilityProbe
    exchange: Optional[RawExchange]
    http_status: Optional[int]
    error_kind: Optional[ProviderErrorKind]
    observed_state: Optional[CapabilityState]  # None when the probe couldn't verify
    detail: Optional[str]
    auth_failed: bool = False


async def _run_probe(probe: CapabilityProbe) -> _ProbeResult:
    """Run one probe and classify its outcome into an observed state.

    * 2xx success -> the group is observed ``supported``.
    * TIER_RESTRICTED -> observed ``paid_tier_required`` (only this group).
    * FORBIDDEN -> observed ``unavailable`` (a permission gate, not a tier one).
    * AUTHENTICATION / INVALID_KEY -> auth failure; nothing marked supported.
    * anything else (rate limit / server / network / parser / not found /
      unexpected) -> could NOT verify: observed_state stays ``None`` (recorded as
      a failure, never a false observation).
    """

    try:
        response = await probe.fetch()
    except ProviderError as exc:
        kind = exc.kind
        if kind is ProviderErrorKind.TIER_RESTRICTED:
            return _ProbeResult(probe, exc.exchange, exc.status_code, kind,
                                CapabilityState.PAID_TIER_REQUIRED, "tier restriction observed")
        if kind is ProviderErrorKind.FORBIDDEN:
            return _ProbeResult(probe, exc.exchange, exc.status_code, kind,
                                CapabilityState.UNAVAILABLE, "forbidden (no tier evidence)")
        if kind in (ProviderErrorKind.AUTHENTICATION, ProviderErrorKind.INVALID_KEY):
            return _ProbeResult(probe, exc.exchange, exc.status_code, kind, None,
                                "authentication failure", auth_failed=True)
        # Inconclusive: recorded as a failure, never a supported observation.
        return _ProbeResult(probe, exc.exchange, exc.status_code, kind, None, kind.value)
    except Exception as exc:  # noqa: BLE001 - classify, never leak
        _t, msg = sanitize_error(exc)
        return _ProbeResult(probe, None, None, ProviderErrorKind.UNEXPECTED, None, msg)

    return _ProbeResult(
        probe, response.exchange, response.exchange.http_status, None,
        CapabilityState.SUPPORTED, None,
    )


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
async def audit_provider(
    *,
    database: Database,
    provider: str,
    probes: list[CapabilityProbe],
    declaration: CapabilityDeclaration,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> ProviderAuditResult:
    """Audit a provider by running each capability-group probe independently."""

    if provider not in SUPPORTED_AUDIT_PROVIDERS:
        raise ValueError(
            f"unsupported audit provider {provider!r}; expected one of "
            f"{list(SUPPORTED_AUDIT_PROVIDERS)}"
        )

    result = ProviderAuditResult(
        provider=provider, tier=declaration.tier, dry_run=dry_run, status="succeeded"
    )

    probe_results: list[_ProbeResult] = []
    probed_caps: set[ProviderCapability] = set()
    auth_failed = False
    for probe in probes:
        pr = await _run_probe(probe)
        probe_results.append(pr)
        result.requests_made += 1
        if pr.observed_state is CapabilityState.PAID_TIER_REQUIRED:
            result.tier_restricted = True
        if pr.auth_failed:
            auth_failed = True
            # A shared key that fails auth on one endpoint fails everywhere;
            # stop probing rather than hammer the provider with a bad key.
            break
        probed_caps.update(probe.capabilities)

    # authenticated: True unless we saw an auth failure; None if no probe ran.
    if auth_failed:
        result.authenticated = False
        result.status = "failed"
    elif probes:
        result.authenticated = True

    # -- Build observations ---------------------------------------------------
    observations: list[CapabilityObservation] = []
    # 1) Observed capabilities from each probe that verified something.
    for pr in probe_results:
        for cap in pr.probe.capabilities:
            declared = declaration.state(cap).value
            if pr.observed_state is None:
                # Probe attempted but could not verify: record the failure as a
                # declared-only row carrying the error evidence (never supported).
                observations.append(
                    CapabilityObservation(
                        capability=cap.value,
                        state=declared,
                        declared_state=declared,
                        observed_state=None,
                        is_observed=False,
                        probe_name=pr.probe.name,
                        endpoint=pr.probe.endpoint,
                        http_status=pr.http_status,
                        error_kind=(pr.error_kind.value if pr.error_kind else None),
                        detail=pr.detail,
                    )
                )
                continue
            # A verified observation. If the declaration is more specific than a
            # bare "supported" (best_effort / history_limited), keep that nuance.
            observed = _reconcile_observed(pr.observed_state, declaration.state(cap))
            observations.append(
                CapabilityObservation(
                    capability=cap.value,
                    state=observed.value,
                    declared_state=declared,
                    observed_state=observed.value,
                    is_observed=True,
                    probe_name=pr.probe.name,
                    endpoint=pr.probe.endpoint,
                    http_status=pr.http_status,
                    error_kind=(pr.error_kind.value if pr.error_kind else None),
                    detail=pr.detail,
                )
            )
    # 2) Declared-only capabilities (never probed): honest metadata, is_observed=0.
    for cap, state in sorted(declaration.states.items(), key=lambda kv: kv[0].value):
        if cap in probed_caps:
            continue
        observations.append(
            CapabilityObservation(
                capability=cap.value,
                state=state.value,
                declared_state=state.value,
                observed_state=None,
                is_observed=False,
            )
        )

    result.observations = observations
    result.observed_count = sum(1 for o in observations if o.is_observed)
    result.declared_only_count = sum(1 for o in observations if not o.is_observed)

    if dry_run:
        return result

    _persist(database, provider, declaration, probe_results, observations, result, tool_version)
    return result


def _reconcile_observed(
    observed: CapabilityState, declared: CapabilityState
) -> CapabilityState:
    """Keep a declaration's nuance when a probe merely proves accessibility.

    A 2xx proves the endpoint is reachable (``SUPPORTED``); if the declaration is
    the more specific ``BEST_EFFORT`` or ``PROVIDER_HISTORY_LIMITED``, preserve
    that (accessibility does not upgrade a known-partial capability to full).
    """

    if observed is CapabilityState.SUPPORTED and declared in (
        CapabilityState.BEST_EFFORT,
        CapabilityState.PROVIDER_HISTORY_LIMITED,
    ):
        return declared
    return observed


_GAP_STATES = frozenset(
    {
        CapabilityState.PAID_TIER_REQUIRED.value,
        CapabilityState.UNAVAILABLE.value,
        CapabilityState.UNKNOWN_UNTIL_AUDITED.value,
    }
)


def _persist(
    database: Database,
    provider: str,
    declaration: CapabilityDeclaration,
    probe_results: list[_ProbeResult],
    observations: list[CapabilityObservation],
    result: ProviderAuditResult,
    tool_version: str,
) -> None:
    import time

    started = time.monotonic_ns()
    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=_COMMAND,
                provider=provider,
                operation="audit",
                args_json=canonical_json({"provider": provider, "tier": declaration.tier}),
                started_monotonic_ns=started,
                tool_version=tool_version,
            )
        result.run_id = run.run_id
        raw_repo = SqliteRawResponseRepository(conn)

        # Store each probe's raw response ONCE; map endpoint -> (raw_id, received_at).
        # A raw response is attached to a capability ONLY if that probe actually
        # provided evidence for it.
        raw_by_endpoint: dict[str, tuple[str, str]] = {}
        for pr in probe_results:
            if pr.exchange is None or pr.probe.endpoint in raw_by_endpoint:
                continue
            content_hash = response_content_hash(
                provider=provider,
                endpoint=pr.exchange.endpoint,
                request_params=pr.exchange.request_params,
                body=pr.exchange.body,
            )
            with transaction(conn):
                raw = raw_repo.store(
                    run_id=run.run_id,
                    provider=provider,
                    endpoint=pr.exchange.endpoint,
                    request_params_json=canonical_json(pr.exchange.request_params),
                    http_status=pr.exchange.http_status,
                    response_headers_json=canonical_json(pr.exchange.response_headers),
                    requested_at=to_iso(pr.exchange.requested_at),
                    received_at=to_iso(pr.exchange.received_at),
                    elapsed_ns=pr.exchange.elapsed_ns,
                    body=pr.exchange.body,
                    content_hash=content_hash,
                    content_type=pr.exchange.content_type,
                )
            raw_by_endpoint[pr.probe.endpoint] = (raw.raw_response_id, raw.received_at)

        observed_at = to_iso(_now())
        caps = SqliteCapabilityRepository(conn)
        dq = SqliteDataQualityRepository(conn)
        with transaction(conn):
            for obs in observations:
                raw_id: Optional[str] = None
                verified_at: Optional[str] = None
                if obs.is_observed and obs.endpoint in raw_by_endpoint:
                    raw_id, verified_at = raw_by_endpoint[obs.endpoint]
                obs.raw_response_id = raw_id
                _snap, inserted = caps.record(
                    provider=provider,
                    tier=declaration.tier,
                    capability=obs.capability,
                    state=obs.state,
                    observed_at=observed_at,
                    detail=obs.detail,
                    run_id=run.run_id,
                    raw_response_id=raw_id if obs.is_observed else None,
                    declared_state=obs.declared_state,
                    observed_state=obs.observed_state,
                    is_observed=obs.is_observed,
                    probe_name=obs.probe_name,
                    endpoint=obs.endpoint,
                    http_status=obs.http_status,
                    error_kind=obs.error_kind,
                    verified_at=verified_at,
                )
                if inserted:
                    result.capabilities_recorded += 1
                # A DQ note for a genuine gap (observed or declared).
                if obs.state in _GAP_STATES or obs.error_kind not in (None,):
                    dq.record(
                        severity="note",
                        rule_code="DQ-CAP-001",
                        entity_type="provider",
                        description=(
                            f"{provider} capability {obs.capability!r}: state={obs.state}"
                            + (f", error={obs.error_kind}" if obs.error_kind else "")
                            + f" (tier={declaration.tier}, observed={obs.is_observed})"
                        ),
                        provider=provider,
                        run_id=run.run_id,
                        raw_response_id=raw_id,
                    )
                    result.issues_recorded += 1

        with transaction(conn):
            runs.complete(
                run.run_id,
                status="failed" if result.failed else "succeeded",
                duration_ns=time.monotonic_ns() - started,
                requests_made=result.requests_made,
                records_received=len(observations),
                records_normalized=len(observations),
                records_inserted=result.capabilities_recorded,
            )


def _now():  # small indirection so tests need not patch datetime
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Probe-set builders (documented endpoints only; unverified endpoints omitted)
# --------------------------------------------------------------------------- #
def declaration_for(provider: str, *, balldontlie_tier: "BalldontlieTier") -> CapabilityDeclaration:
    """The static capability declaration for a provider (BALLDONTLIE by tier)."""

    from ..providers.capabilities import (
        MLB_STATSAPI_DECLARATION,
        NWS_DECLARATION,
        OPEN_METEO_DECLARATION,
        balldontlie_declaration,
    )

    if provider == PROVIDER_MLB_STATSAPI:
        return MLB_STATSAPI_DECLARATION
    if provider == PROVIDER_BALLDONTLIE:
        return balldontlie_declaration(balldontlie_tier)
    if provider == PROVIDER_NWS:
        return NWS_DECLARATION
    if provider == PROVIDER_OPEN_METEO:
        return OPEN_METEO_DECLARATION
    raise ValueError(f"no declaration for provider {provider!r}")


_C = ProviderCapability


def build_balldontlie_probes(client) -> list[CapabilityProbe]:
    """Independent probes for the documented BALLDONTLIE GOAT endpoint families.

    Each group hits one documented endpoint. Capabilities without a documented
    endpoint (plays, lineups, confirmed pregame starters, substitutions) are
    **not** probed here -- they remain declared-only / unknown until a live audit
    confirms an endpoint, honouring "do not guess endpoint names". Lineup access
    and pregame-starter confirmation are represented separately.
    """

    return [
        CapabilityProbe("teams", "/v1/teams", (_C.TEAMS,), lambda: client.fetch_teams()),
        CapabilityProbe("players", "/v1/players", (_C.PLAYERS,), lambda: client.fetch_players()),
        CapabilityProbe(
            "games", "/v1/games", (_C.GAMES, _C.SCHEDULES, _C.GAME_RESULTS),
            lambda: client.fetch_games(),
        ),
        CapabilityProbe(
            "player_stats", "/v1/stats", (_C.PLAYER_STATISTICS,),
            lambda: client.fetch_stats(),
        ),
        CapabilityProbe(
            "box_scores", "/v1/box_scores", (_C.TEAM_STATISTICS,),
            lambda: client.fetch_box_scores(),
        ),
        CapabilityProbe(
            "injuries", "/v1/player_injuries", (_C.INJURIES,),
            lambda: client.fetch_player_injuries(),
        ),
    ]


def build_mlb_statsapi_probes(client) -> list[CapabilityProbe]:
    """Independent probes for the MLB StatsAPI endpoint families D1 verifies."""

    return [
        CapabilityProbe("teams", "/teams", (_C.TEAMS,), lambda: client.fetch_teams()),
        CapabilityProbe(
            "schedule", "/schedule", (_C.SCHEDULES, _C.GAMES),
            lambda: client.fetch_schedule(),
        ),
        CapabilityProbe("venues", "/venues", (_C.VENUES,), lambda: client.fetch_venues()),
    ]


def build_nws_probes(client) -> list[CapabilityProbe]:
    """A single NWS point probe (US forecast availability)."""

    return [
        CapabilityProbe(
            "point", "/points/{lat},{lon}", (_C.LIVE_AVAILABILITY,),
            lambda: client.fetch_point(40.7128, -74.0060),
        ),
    ]


def build_open_meteo_probes(client) -> list[CapabilityProbe]:
    """A single Open-Meteo forecast probe. Historical-forecast reconstruction is
    a separate documented endpoint and is NOT implied by a current forecast."""

    return [
        CapabilityProbe(
            "forecast", "/v1/forecast", (_C.LIVE_AVAILABILITY,),
            lambda: client.fetch_forecast(40.7128, -74.0060),
        ),
    ]


# Late import to avoid a cycle at module import time.
from ..providers.capabilities import BalldontlieTier  # noqa: E402
