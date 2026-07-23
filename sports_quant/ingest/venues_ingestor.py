"""MLB venue ingestion (D1 scope only).

Seeds canonical ``venues`` + ``venue_aliases`` from the MLB StatsAPI ``/venues``
surface. This is the *only* ingestion D1 performs; schedules/results/stats/etc.
are D2+ and are not touched here.

Persist-before-parse: the raw response is stored before any venue row is
derived. ``--dry-run`` performs the GET + normalization and persists absolutely
nothing (no run, raw, venue, alias, or data-quality row). No credential is used
(StatsAPI is unauthenticated) and no live call is made in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.kalshi import UpsertOutcome
from ..db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from ..db.repositories.venues import SqliteVenueRepository, validate_venue_fields
from ..db.schema import to_iso
from ..providers.base_provider import ProviderError, ProviderResponse
from ..providers.capabilities import PROVIDER_MLB_STATSAPI
from ..providers.mlb_statsapi import MlbStatsApiClient
from .runner import sanitize_error

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "ingest-venues"

# StatsAPI roofType -> our approved vocabulary.
_ROOF_MAP = {
    "open": "open",
    "outdoor": "open",
    "retractable": "retractable",
    "dome": "dome",
    "domed": "dome",
    "fixed": "fixed",
    "indoor": "indoor",
    "closed": "fixed",
}


@dataclass
class VenueIngestResult:
    """Sanitized outcome of one venue ingest, safe to print/JSON."""

    dry_run: bool
    status: str
    run_id: Optional[str] = None
    requests_made: int = 0
    venues_seen: int = 0
    venues_inserted: int = 0
    venues_updated: int = 0
    venues_unchanged: int = 0
    venues_rejected: int = 0
    aliases_written: int = 0
    rejections: list[str] = field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def note(self, reason: str) -> None:
        if len(self.rejections) < 20:
            self.rejections.append(reason)


@dataclass(frozen=True)
class _NormVenue:
    name: str
    provider_venue_id: Optional[str]
    city: Optional[str]
    country: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    timezone: Optional[str]
    roof_type: Optional[str]


def normalize_venue(raw: dict[str, Any]) -> tuple[Optional[_NormVenue], Optional[str]]:
    """Validate + normalize one StatsAPI venue object.

    Rejects a blank name and clearly-invalid coordinates/roof; an absent field is
    ``None`` (never fabricated). Returns ``(normalized, None)`` or ``(None, reason)``.
    """

    name = str(raw.get("name") or "").strip()
    if not name:
        return None, "venue missing name"
    provider_venue_id = raw.get("id")
    location = raw.get("location") or {}
    coords = location.get("defaultCoordinates") or {}
    latitude = _opt_float(coords.get("latitude"))
    longitude = _opt_float(coords.get("longitude"))
    timezone_obj = location.get("timeZone") or {}
    timezone = _opt_str(timezone_obj.get("id"))
    field_info = raw.get("fieldInfo") or {}
    roof_raw = _opt_str(field_info.get("roofType"))
    roof_type = _ROOF_MAP.get(roof_raw.lower()) if roof_raw else None

    try:
        validate_venue_fields(
            latitude=latitude, longitude=longitude, timezone=timezone, roof_type=roof_type
        )
    except Exception as exc:  # noqa: BLE001 - a malformed record, not a crash
        return None, f"venue {name!r} invalid: {type(exc).__name__}"

    return _NormVenue(
        name=name,
        provider_venue_id=None if provider_venue_id is None else str(provider_venue_id),
        city=_opt_str(location.get("city")),
        country=_opt_str(location.get("country")),
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        roof_type=roof_type,
    ), None


async def ingest_venues(
    *,
    database: Database,
    client: MlbStatsApiClient,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> VenueIngestResult:
    """Seed venues from MLB StatsAPI. ``--dry-run`` persists nothing."""

    result = VenueIngestResult(dry_run=dry_run, status="succeeded")
    try:
        response = await client.fetch_venues()
    except ProviderError as exc:
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.error_type, result.error_message = sanitize_error(exc)
        return result

    result.requests_made = 1
    raw_venues = response.data.get("venues", []) if isinstance(response.data, dict) else []

    if dry_run:
        for raw in raw_venues:
            norm, reason = normalize_venue(raw)
            if norm is None:
                result.venues_rejected += 1
                result.note(reason or "invalid venue")
            else:
                result.venues_seen += 1
        if result.venues_rejected:
            result.status = "partially_succeeded"
        return result

    return await _persist(database, response, raw_venues, result, tool_version)


async def _persist(
    database: Database,
    response: ProviderResponse,
    raw_venues: list[dict[str, Any]],
    result: VenueIngestResult,
    tool_version: str,
) -> VenueIngestResult:
    import time

    started = time.monotonic_ns()
    exchange = response.exchange
    with database.connection() as conn:
        runs = SqliteIngestionRunRepository(conn)
        with transaction(conn):
            run = runs.start(
                command=_COMMAND,
                provider=PROVIDER_MLB_STATSAPI,
                operation="fetch_venues",
                args_json=canonical_json({}),
                started_monotonic_ns=started,
                tool_version=tool_version,
            )
        result.run_id = run.run_id

        content_hash = response_content_hash(
            provider=PROVIDER_MLB_STATSAPI,
            endpoint=exchange.endpoint,
            request_params=exchange.request_params,
            body=exchange.body,
        )
        with transaction(conn):
            raw = SqliteRawResponseRepository(conn).store(
                run_id=run.run_id,
                provider=PROVIDER_MLB_STATSAPI,
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
        observed_at = raw.received_at

        venues = SqliteVenueRepository(conn)
        with transaction(conn):
            for raw_venue in raw_venues:
                norm, reason = normalize_venue(raw_venue)
                if norm is None:
                    result.venues_rejected += 1
                    result.note(reason or "invalid venue")
                    continue
                venue, outcome = venues.upsert(
                    name=norm.name,
                    raw_response_id=raw.raw_response_id,
                    raw_response_hash=raw.content_hash,
                    observed_at=observed_at,
                    city=norm.city,
                    country=norm.country,
                    latitude=norm.latitude,
                    longitude=norm.longitude,
                    timezone=norm.timezone,
                    roof_type=norm.roof_type,
                )
                result.venues_seen += 1
                if outcome is UpsertOutcome.INSERTED:
                    result.venues_inserted += 1
                elif outcome is UpsertOutcome.UPDATED:
                    result.venues_updated += 1
                else:
                    result.venues_unchanged += 1
                # Alias: the provider's own name + provider id.
                venues.add_alias(
                    venue_id=venue.venue_id,
                    alias=norm.name,
                    provider=PROVIDER_MLB_STATSAPI,
                    provider_venue_id=norm.provider_venue_id,
                    source="provider_observed",
                )
                result.aliases_written += 1

        status = "partially_succeeded" if result.venues_rejected else "succeeded"
        with transaction(conn):
            runs.complete(
                run.run_id,
                status=status,
                duration_ns=time.monotonic_ns() - started,
                requests_made=1,
                records_received=result.venues_seen + result.venues_rejected,
                records_normalized=result.venues_seen,
                records_inserted=result.venues_inserted,
                records_updated=result.venues_updated,
                records_deduplicated=result.venues_unchanged,
                records_rejected=result.venues_rejected,
            )
        result.status = status
    return result


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
