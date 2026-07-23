"""Shared ingestion-run bookkeeping.

The counters and the error-sanitization helper here are provider-agnostic, so
the Kalshi ingestor (Phase C) reuses them rather than re-deriving the run
lifecycle. Nothing in this module touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..redaction import sanitize_url


@dataclass
class RunCounters:
    """Mutable tally accumulated while an ingestion run executes.

    Five record counters, not one, because "1000 received, 0 inserted" and
    "0 received" are different incidents and the run history must keep them
    distinguishable.
    """

    requests_made: int = 0
    records_received: int = 0
    records_normalized: int = 0
    records_inserted: int = 0
    records_deduplicated: int = 0
    records_rejected: int = 0


def sanitize_error(exc: BaseException, extra_secrets: Iterable[str] = ()) -> tuple[str, str]:
    """Return ``(error_type, sanitized_message)`` safe to store.

    The exception class name carries no secret. The message is passed through
    :func:`sanitize_url` -- which masks query-string secrets and any explicitly
    supplied secret value -- so an Odds API key that reached an exception (it
    should not; the adapter already sanitizes its own) still cannot be
    persisted.
    """

    return type(exc).__name__, sanitize_url(str(exc), extra_secrets)
