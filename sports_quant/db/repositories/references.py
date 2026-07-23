"""Provider id crosswalk repositories (teams, players, games).

Each provider reference maps a ``(provider, provider_entity_id)`` identity to a
canonical id (nullable until matched, D5). Identity + ``first_raw_response_id``
are immutable; the ``current_*`` provenance moves only on a strictly-newer
observation, exactly like the c008 Kalshi metadata provenance. Linking a
reference to a canonical id is a separate, recorded operation (D5), not part of
ingestion.

One generic implementation backs three tables, keyed by ``kind``; SQL stays here.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Protocol

from ..ids import (
    new_provider_game_ref_id,
    new_provider_player_ref_id,
    new_provider_team_ref_id,
)
from ..models import ProviderReference
from ..schema import utc_now_iso
from .base import Repository, RepositoryError
from .kalshi import UpsertOutcome

# kind -> (table, provider-id column, canonical-fk column, id factory)
_TABLES: dict[str, tuple[str, str, str, object]] = {
    "team": (
        "provider_team_references",
        "provider_team_id",
        "team_id",
        new_provider_team_ref_id,
    ),
    "player": (
        "provider_player_references",
        "provider_player_id",
        "player_id",
        new_provider_player_ref_id,
    ),
    "game": (
        "provider_game_references",
        "provider_game_id",
        "game_id",
        new_provider_game_ref_id,
    ),
}


class ProviderReferenceRepositoryProtocol(Protocol):
    def upsert(
        self,
        *,
        kind: str,
        provider: str,
        provider_entity_id: str,
        raw_response_id: str,
        raw_response_hash: str,
        observed_at: str,
    ) -> tuple[ProviderReference, UpsertOutcome]: ...

    def get(self, kind: str, provider: str, provider_entity_id: str) -> Optional[ProviderReference]: ...


class SqliteProviderReferenceRepository(Repository):
    """Storage for the three provider crosswalk tables."""

    def _table(self, kind: str) -> tuple[str, str, str, object]:
        try:
            return _TABLES[kind]
        except KeyError:
            raise RepositoryError(
                f"unknown provider-reference kind {kind!r}; expected one of {sorted(_TABLES)}"
            ) from None

    def upsert(
        self,
        *,
        kind: str,
        provider: str,
        provider_entity_id: str,
        raw_response_id: str,
        raw_response_hash: str,
        observed_at: str,
    ) -> tuple[ProviderReference, UpsertOutcome]:
        """Insert a reference, or refresh its current provenance if newer.

        Returns ``(reference, outcome)``. A strictly-newer observation advances
        ``current_raw_response_id``/``hash`` and ``last_observed_at``; an
        older-or-equal one is a no-op (deterministic: equal retains earlier).
        Never sets the canonical id (that is a Phase D5 match decision).
        """

        table, pid_col, _canon_col, factory = self._table(kind)
        existing = self.get(kind, provider, provider_entity_id)
        now = utc_now_iso()
        if existing is None:
            reference_id = factory()  # type: ignore[operator]
            self._conn.execute(
                f"INSERT INTO {table} "
                f"(reference_id, provider, {pid_col}, first_raw_response_id, "
                " current_raw_response_id, current_raw_response_hash, first_observed_at, "
                " last_observed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reference_id, provider, provider_entity_id, raw_response_id,
                    raw_response_id, raw_response_hash, observed_at, observed_at, now, now,
                ),
            )
            fetched = self._fetch(kind, reference_id)
            assert fetched is not None  # noqa: S101
            return fetched, UpsertOutcome.INSERTED

        if observed_at > existing.last_observed_at:
            self._conn.execute(
                f"UPDATE {table} SET current_raw_response_id = ?, current_raw_response_hash = ?, "
                "last_observed_at = ?, updated_at = ? WHERE reference_id = ?",
                (raw_response_id, raw_response_hash, observed_at, now, existing.reference_id),
            )
            refreshed = self._fetch(kind, existing.reference_id)
            assert refreshed is not None  # noqa: S101
            return refreshed, UpsertOutcome.UPDATED

        return existing, UpsertOutcome.UNCHANGED

    def get(
        self, kind: str, provider: str, provider_entity_id: str
    ) -> Optional[ProviderReference]:
        table, pid_col, _canon_col, _factory = self._table(kind)
        row = self._fetch_one(
            f"SELECT * FROM {table} WHERE provider = ? AND {pid_col} = ?",
            (provider, provider_entity_id),
        )
        return None if row is None else self._to_model(kind, row)

    def count(self, kind: str) -> int:
        table, _pid, _canon, _factory = self._table(kind)
        return self._count(f"SELECT COUNT(*) FROM {table}")

    def _fetch(self, kind: str, reference_id: str) -> Optional[ProviderReference]:
        table, _pid, _canon, _factory = self._table(kind)
        row = self._fetch_one(
            f"SELECT * FROM {table} WHERE reference_id = ?", (reference_id,)
        )
        return None if row is None else self._to_model(kind, row)

    def _to_model(self, kind: str, row: sqlite3.Row) -> ProviderReference:
        _table, pid_col, canon_col, _factory = self._table(kind)
        return ProviderReference(
            reference_id=str(row["reference_id"]),
            kind=kind,
            provider=str(row["provider"]),
            provider_entity_id=str(row[pid_col]),
            first_raw_response_id=str(row["first_raw_response_id"]),
            current_raw_response_id=str(row["current_raw_response_id"]),
            current_raw_response_hash=str(row["current_raw_response_hash"]),
            first_observed_at=str(row["first_observed_at"]),
            last_observed_at=str(row["last_observed_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            canonical_id=self._opt_str(row, canon_col),
            match_decision_id=self._opt_str(row, "match_decision_id"),
        )
