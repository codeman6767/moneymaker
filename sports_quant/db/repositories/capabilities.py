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
from .base import Repository, RepositoryError, to_db_bool


def capability_content_hash(
    *,
    provider: str,
    tier: Optional[str],
    capability: str,
    state: str,
    detail: Optional[str],
    is_observed: bool = False,
    declared_state: Optional[str] = None,
    observed_state: Optional[str] = None,
    probe_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    http_status: Optional[int] = None,
    error_kind: Optional[str] = None,
) -> str:
    """Identity of a capability record (excludes ``observed_at``).

    Includes the evidence fields so a *declared-only* row and an *observed* row --
    or two observations differing only in probe/endpoint/status/error -- never
    collide under the ``(provider, tier, capability, observed_at, content_hash)``
    idempotency key.
    """

    payload = {
        "provider": provider,
        "tier": tier,
        "capability": capability,
        "state": state,
        "detail": detail,
        "is_observed": is_observed,
        "declared_state": declared_state,
        "observed_state": observed_state,
        "probe_name": probe_name,
        "endpoint": endpoint,
        "http_status": http_status,
        "error_kind": error_kind,
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
        "raw_response_id, content_hash, created_at, declared_state, observed_state, "
        "is_observed, probe_name, endpoint, http_status, error_kind, verified_at"
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
        declared_state: Optional[str] = None,
        observed_state: Optional[str] = None,
        is_observed: bool = False,
        probe_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        http_status: Optional[int] = None,
        error_kind: Optional[str] = None,
        verified_at: Optional[str] = None,
    ) -> tuple[Optional[ProviderCapabilityRecord], bool]:
        """Append a capability record. Returns ``(record, inserted)``.

        Idempotent on ``(provider, tier, capability, observed_at, content_hash)``
        via ``INSERT OR IGNORE``. ``is_observed`` must be ``True`` **only** when an
        exact probe verified the capability (with ``raw_response_id`` evidence);
        a declared-only row leaves ``observed_state`` NULL and ``is_observed`` 0.
        No historical record is ever updated or deleted.
        """

        if state not in CAPABILITY_STATES:
            raise RepositoryError(
                f"invalid capability state {state!r}; expected one of {list(CAPABILITY_STATES)}"
            )
        for label, value in (("declared_state", declared_state), ("observed_state", observed_state)):
            if value is not None and value not in CAPABILITY_STATES:
                raise RepositoryError(f"invalid {label} {value!r}")
        if not provider.strip():
            raise RepositoryError("capability provider must be non-blank")
        if is_observed and raw_response_id is None:
            raise RepositoryError(
                "an observed capability must carry a raw_response_id (evidence); "
                "declared-only rows must set is_observed=False"
            )
        if is_observed and observed_state is None:
            raise RepositoryError("an observed capability must set observed_state")
        content_hash = capability_content_hash(
            provider=provider, tier=tier, capability=capability, state=state, detail=detail,
            is_observed=is_observed, declared_state=declared_state, observed_state=observed_state,
            probe_name=probe_name, endpoint=endpoint, http_status=http_status, error_kind=error_kind,
        )
        capability_id = new_provider_capability_id()
        now = utc_now_iso()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO provider_capabilities "
            "(capability_id, provider, tier, capability, state, detail, observed_at, run_id, "
            " raw_response_id, content_hash, created_at, declared_state, observed_state, "
            " is_observed, probe_name, endpoint, http_status, error_kind, verified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                capability_id, provider, tier, capability, state, detail, observed_at, run_id,
                raw_response_id, content_hash, now, declared_state, observed_state,
                to_db_bool(is_observed), probe_name, endpoint, http_status, error_kind, verified_at,
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

    def observed_state_as_of(
        self, provider: str, capability: str, as_of: str
    ) -> Optional[ProviderCapabilityRecord]:
        """The latest **externally observed** record at or before ``as_of``.

        Filters to ``is_observed = 1`` so a declared-only belief never masquerades
        as a verified observation.
        """

        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM provider_capabilities "
            "WHERE provider = ? AND capability = ? AND is_observed = 1 AND observed_at <= ? "
            "ORDER BY observed_at DESC, capability_id DESC LIMIT 1",
            (provider, capability, as_of),
        )
        return None if row is None else self._to_model(row)

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
            declared_state=self._opt_str(row, "declared_state"),
            observed_state=self._opt_str(row, "observed_state"),
            is_observed=bool(row["is_observed"]),
            probe_name=self._opt_str(row, "probe_name"),
            endpoint=self._opt_str(row, "endpoint"),
            http_status=self._opt_int(row, "http_status"),
            error_kind=self._opt_str(row, "error_kind"),
            verified_at=self._opt_str(row, "verified_at"),
        )
