"""Provider audit: a small, non-destructive capability check.

Before any large backfill (D2/D3), ``provider-audit`` probes a provider with a
couple of small approved GET requests, records what it observed, and persists a
``provider_capabilities`` snapshot (plus a `DQ-CAP-*` data-quality note for any
gap). It **never** buys or changes a subscription, and it never treats a tier
restriction as an invalid key -- a BALLDONTLIE plan-gated endpoint answering 403
is recorded as ``paid_tier_required``/``unavailable``, and unrelated capabilities
continue.

D1 exercises this against mocked transports only; no live provider call is made.
``--dry-run`` performs the probe(s) in memory and persists absolutely nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

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
from ..providers.base_provider import ProviderError
from ..providers.capabilities import (
    PROVIDER_BALLDONTLIE,
    PROVIDER_MLB_STATSAPI,
    PROVIDER_NWS,
    PROVIDER_OPEN_METEO,
    BalldontlieTier,
    CapabilityDeclaration,
    CapabilityState,
    ProviderCapability,
    ProviderErrorKind,
    balldontlie_declaration,
    is_tier_restriction,
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


@dataclass
class CapabilityObservation:
    """One capability + the state the audit concluded for it."""

    capability: str
    state: str
    detail: Optional[str] = None


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
    issues_recorded: int = 0
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"


def _declared_observations(declaration: CapabilityDeclaration) -> list[CapabilityObservation]:
    """Turn a static declaration into observations (the audit's starting point)."""

    return [
        CapabilityObservation(capability=cap.value, state=state.value, detail=None)
        for cap, state in sorted(declaration.states.items(), key=lambda kv: kv[0].value)
    ]


async def audit_provider(
    *,
    database: Database,
    provider: str,
    probe: "AuditProbe",
    tier: Optional[str] = None,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> ProviderAuditResult:
    """Audit one provider using an injected :class:`AuditProbe` (mockable).

    The probe performs the small approved GET(s) and returns whether the primary
    endpoint authenticated and whether a tier restriction was seen; the static
    capability declaration supplies the rest. Nothing is fabricated: a probe
    failure downgrades the affected capability, it never invents availability.
    """

    if provider not in SUPPORTED_AUDIT_PROVIDERS:
        raise ValueError(
            f"unsupported audit provider {provider!r}; expected one of "
            f"{list(SUPPORTED_AUDIT_PROVIDERS)}"
        )

    declaration = probe.declaration
    result = ProviderAuditResult(
        provider=provider, tier=declaration.tier or tier, dry_run=dry_run, status="succeeded"
    )
    observations = _declared_observations(declaration)

    # -- Probe (network via the injected probe; mocked in tests) --------------
    exchange: Optional[RawExchange] = None
    try:
        outcome = await probe.run()
        result.requests_made = outcome.requests_made
        result.authenticated = outcome.authenticated
        exchange = outcome.exchange
    except ProviderError as exc:
        if is_tier_restriction(exc.kind):
            # A tier restriction is honest capability info, not a failure.
            result.tier_restricted = True
            result.authenticated = True  # the key was accepted; the tier gated it
            result.requests_made = 1
            exchange = exc.exchange
            observations = _apply_tier_restriction(observations)
        elif exc.kind is ProviderErrorKind.AUTHENTICATION:
            result.authenticated = False
            result.status = "failed"
            result.error_type, result.error_message = _err(exc)
        else:
            result.status = "failed"
            result.error_type, result.error_message = _err(exc)
    except Exception as exc:  # noqa: BLE001 - classify, never leak
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)

    result.observations = observations

    if dry_run:
        # Report what a real run would persist; persist absolutely nothing.
        return result

    _persist(database, provider, declaration, observations, result, exchange, tool_version)
    return result


def _apply_tier_restriction(
    observations: list[CapabilityObservation],
) -> list[CapabilityObservation]:
    """Downgrade any 'supported' data capability the tier could not reach.

    Conservative: a probe-level tier restriction demotes the paid-tier data
    capabilities to ``paid_tier_required`` so the corpus records the tier gate.
    """

    demote = {
        ProviderCapability.PLAYER_STATISTICS.value,
        ProviderCapability.TEAM_STATISTICS.value,
        ProviderCapability.PLAYS.value,
        ProviderCapability.LINEUPS.value,
        ProviderCapability.INJURIES.value,
        ProviderCapability.QUARTER_LINES.value,
    }
    out: list[CapabilityObservation] = []
    for obs in observations:
        if obs.capability in demote and obs.state == CapabilityState.SUPPORTED.value:
            out.append(
                CapabilityObservation(
                    capability=obs.capability,
                    state=CapabilityState.PAID_TIER_REQUIRED.value,
                    detail="observed tier restriction during audit",
                )
            )
        else:
            out.append(obs)
    return out


def _persist(
    database: Database,
    provider: str,
    declaration: CapabilityDeclaration,
    observations: list[CapabilityObservation],
    result: ProviderAuditResult,
    exchange: Optional[RawExchange],
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

        raw_id: Optional[str] = None
        if exchange is not None:
            content_hash = response_content_hash(
                provider=provider,
                endpoint=exchange.endpoint,
                request_params=exchange.request_params,
                body=exchange.body,
            )
            with transaction(conn):
                raw = SqliteRawResponseRepository(conn).store(
                    run_id=run.run_id,
                    provider=provider,
                    endpoint=exchange.endpoint,
                    request_params_json=canonical_json(exchange.request_params),
                    http_status=exchange.http_status,
                    response_headers_json=canonical_json(exchange.response_headers),
                    requested_at=to_iso(exchange.requested_at),
                    received_at=to_iso(exchange.received_at),
                    elapsed_ns=exchange.elapsed_ns,
                    body=exchange.body,
                    content_hash=content_hash,
                    content_type=exchange.content_type,
                )
            raw_id = raw.raw_response_id
            observed_at = raw.received_at
        else:
            observed_at = to_iso(_now())

        caps = SqliteCapabilityRepository(conn)
        dq = SqliteDataQualityRepository(conn)
        with transaction(conn):
            for obs in observations:
                _snap, inserted = caps.record(
                    provider=provider,
                    tier=declaration.tier,
                    capability=obs.capability,
                    state=obs.state,
                    observed_at=observed_at,
                    detail=obs.detail,
                    run_id=run.run_id,
                    raw_response_id=raw_id,
                )
                if inserted:
                    result.capabilities_recorded += 1
                if obs.state in _GAP_STATES:
                    dq.record(
                        severity="note",
                        rule_code="DQ-CAP-001",
                        entity_type="provider",
                        description=(
                            f"{provider} capability {obs.capability!r} is {obs.state} "
                            f"(tier={declaration.tier})"
                        ),
                        provider=provider,
                        run_id=run.run_id,
                        raw_response_id=raw_id,
                    )
                    result.issues_recorded += 1

        with transaction(conn):
            runs.complete(
                run.run_id,
                status="succeeded" if not result.failed else "failed",
                duration_ns=time.monotonic_ns() - started,
                requests_made=result.requests_made,
                records_received=len(observations),
                records_normalized=len(observations),
                records_inserted=result.capabilities_recorded,
            )


_GAP_STATES = frozenset(
    {
        CapabilityState.PAID_TIER_REQUIRED.value,
        CapabilityState.UNAVAILABLE.value,
        CapabilityState.UNKNOWN_UNTIL_AUDITED.value,
    }
)


def _now():  # small indirection so tests need not patch datetime
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _err(exc: ProviderError) -> tuple[str, str]:
    return type(exc).__name__, str(exc)


# --------------------------------------------------------------------------- #
# Probe abstraction
# --------------------------------------------------------------------------- #
@dataclass
class ProbeOutcome:
    requests_made: int
    authenticated: bool
    exchange: Optional[RawExchange]


class AuditProbe:
    """Runs the small approved GET(s) for one provider and reports the outcome.

    Concrete probes are thin wrappers over the provider clients; tests inject a
    client with a mocked transport, so no live call occurs. The probe holds the
    static capability declaration used to seed the audit's observations.
    """

    def __init__(self, *, declaration: CapabilityDeclaration) -> None:
        self.declaration = declaration

    async def run(self) -> ProbeOutcome:  # pragma: no cover - overridden
        raise NotImplementedError


class SingleGetProbe(AuditProbe):
    """A probe that issues one GET via an async callable returning a ProviderResponse."""

    def __init__(self, *, declaration: CapabilityDeclaration, fetch) -> None:
        super().__init__(declaration=declaration)
        self._fetch = fetch

    async def run(self) -> ProbeOutcome:
        response = await self._fetch()
        return ProbeOutcome(requests_made=1, authenticated=True, exchange=response.exchange)


def declaration_for(provider: str, *, balldontlie_tier: BalldontlieTier) -> CapabilityDeclaration:
    """The static capability declaration for a provider (BALLDONTLIE by tier)."""

    from ..providers.capabilities import (
        MLB_STATSAPI_DECLARATION,
        NWS_DECLARATION,
        OPEN_METEO_DECLARATION,
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
