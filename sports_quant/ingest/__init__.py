"""Ingestion lane: read-only provider fetches persisted into the corpus.

Each ingestor consumes an existing provider client (never a second one),
preserves the raw response before normalizing it, writes derived rows through
the typed repositories, and records one :class:`~sports_quant.db.models.IngestionRun`
describing what happened. Every write is GET-derived and public-data only; no
order surface is imported here.
"""

from __future__ import annotations

from .odds_ingestor import OddsIngestResult, ingest_odds
from .runner import RunCounters, sanitize_error

__all__ = [
    "OddsIngestResult",
    "RunCounters",
    "ingest_odds",
    "sanitize_error",
]
