"""Provider-capability snapshot repository (append-only, historically auditable).

Records what the system believed a provider (at a tier) could supply, and when.
Snapshots are append-only and idempotent per observation, so an earlier
prediction cutoff can read the capability picture that held then; a later audit
never rewrites history. A capability is recorded with its typed state -- a
missing capability is stored, never fabricated as available.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional, Protocol

from streaming.event_envelope import canonical_json

from ..ids import new_provider_capability_id
from ..models import ProviderCapabilityRecord
from ..schema import CAPABILITY_STATES, utc_now_iso
from .base import Repository, RepositoryError


def capability_content_hash(
    *, provider: str, tier: Optional[str], capability: str, state: str, detail: Optional[str]
) -> str:
    """Identity of a capability observation (excludes ``observed_at``)."""

    payload = {
        "provider": provider,
        "tier": tier,
        "capability": capability,
        "state": state,
        "detail": detail,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class CapabilityRepositoryProtocol(Protocol):
    def record(
        self,
        *,
        provider: str,
        capability: str,
        state: str,
        observed_at: str,
        **fields: object,
    ) -> tuple[Optional[ProviderCapabilityRecord], bool]: ...


class SqliteCapabilityRepository(Repository):
    """Append-only capability-snapshot storage."""

    _COLUMNS = (
        "capability_id, provider, tier, capability, state, detail, observed_at, run_id, "
        "raw_response_id, content_hash, created_at"
    )

    def record(
        self,
        *,
        provider: str,
        capability: str,
        state: str,
        observed_at: str,
        tier: Optional[str] = None,
        detail: Optional[str] = None,
        run_id: Optional[str] = None,
        raw_response_id: Optional[str] = None,
    ) -> tuple[Optional[ProviderCapabilityRecord], bool]:
        """Append a capability snapshot. Returns ``(record, inserted)``.

        Idempotent on ``(provider, tier, capability, observed_at, content_hash)``
        via ``INSERT OR IGNORE``: the same observation at the same time writes one
        row. No historical snapshot is ever updated or deleted.
        """

        if state not in CAPABILITY_STATES:
            raise RepositoryError(
                f"invalid capability state {state!r}; expected one of {list(CAPABILITY_STATES)}"
            )
        if not provider.strip():
            raise RepositoryError("capability provider must be non-blank")
        content_hash = capability_content_hash(
            provider=provider, tier=tier, capability=capability, state=state, detail=detail
        )
        capability_id = new_provider_capability_id()
        now = utc_now_iso()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO provider_capabilities "
            "(capability_id, provider, tier, capability, state, detail, observed_at, run_id, "
            " raw_response_id, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                capability_id, provider, tier, capability, state, detail, observed_at, run_id,
                raw_response_id, content_hash, now,
            ),
        )
        if cursor.rowcount == 0:
            existing = self._fetch_one(
                f"SELECT {self._COLUMNS} FROM provider_capabilities "
                "WHERE provider = ? AND tier IS ? AND capability = ? AND observed_at = ? "
                "AND content_hash = ?",
                (provider, tier, capability, observed_at, content_hash),
            )
            return (None if existing is None else self._to_model(existing)), False
        inserted = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM provider_capabilities WHERE capability_id = ?",
            (capability_id,),
        )
        assert inserted is not None  # noqa: S101
        return self._to_model(inserted), True

    def state_as_of(
        self, provider: str, capability: str, as_of: str
    ) -> Optional[ProviderCapabilityRecord]:
        """The latest capability observation at or before ``as_of``."""

        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM provider_capabilities "
            "WHERE provider = ? AND capability = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, capability_id DESC LIMIT 1",
            (provider, capability, as_of),
        )
        return None if row is None else self._to_model(row)

    def list_for_provider(self, provider: str) -> list[ProviderCapabilityRecord]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM provider_capabilities WHERE provider = ? "
                "ORDER BY observed_at, capability, capability_id",
                (provider,),
            )
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM provider_capabilities")

    def _to_model(self, row: sqlite3.Row) -> ProviderCapabilityRecord:
        return ProviderCapabilityRecord(
            capability_id=str(row["capability_id"]),
            provider=str(row["provider"]),
            capability=str(row["capability"]),
            state=str(row["state"]),
            observed_at=str(row["observed_at"]),
            content_hash=str(row["content_hash"]),
            created_at=str(row["created_at"]),
            tier=self._opt_str(row, "tier"),
            detail=self._opt_str(row, "detail"),
            run_id=self._opt_str(row, "run_id"),
            raw_response_id=self._opt_str(row, "raw_response_id"),
        )
