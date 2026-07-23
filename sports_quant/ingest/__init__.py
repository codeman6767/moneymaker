"""Ingestion lane: read-only provider fetches persisted into the corpus.

Each ingestor consumes an existing provider client (never a second one),
preserves the raw response before normalizing it, writes derived rows through
the typed repositories, and records one :class:`~sports_quant.db.models.IngestionRun`
describing what happened. Every write is GET-derived and public-data only; no
order surface is imported here.
"""

from __future__ import annotations

from .kalshi_ingestor import KalshiIngestResult, ingest_kalshi
from .odds_ingestor import OddsIngestResult, ingest_odds
from .provider_audit import ProviderAuditResult, audit_provider
from .runner import RunCounters, sanitize_error
from .venues_ingestor import VenueIngestResult, ingest_venues

__all__ = [
    "KalshiIngestResult",
    "OddsIngestResult",
    "ProviderAuditResult",
    "RunCounters",
    "VenueIngestResult",
    "audit_provider",
    "ingest_kalshi",
    "ingest_odds",
    "ingest_venues",
    "sanitize_error",
]
