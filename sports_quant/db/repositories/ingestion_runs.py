"""Ingestion-run repository.

One row records the whole life of an ingest invocation: what was asked for,
what came back, and how it ended. It is the table that answers "why does the
corpus contain this, and when did we fetch it?" long after the fact.

Unlike the snapshot tables, an ingestion run is *not* append-only. It is opened
as ``started`` and later closed with its counters and a terminal status, which
is a mutation of the same row. What a run produced -- its raw responses and
price snapshots -- is immutable; the bookkeeping row that describes the run is
not. ``args_json`` and ``error_message`` are stored already-sanitized; this
repository never sanitizes on the caller's behalf, so a secret cannot enter by
a caller forgetting to.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Protocol

from ..ids import new_ingestion_run_id
from ..models import IngestionRun
from ..schema import INGESTION_RUN_STATUSES, utc_now_iso
from .base import Repository, RepositoryError


class IngestionRunRepositoryProtocol(Protocol):
    """Operations the ingestion lane needs from a run store."""

    def start(
        self,
        *,
        command: str,
        provider: str,
        operation: str,
        args_json: str,
        started_monotonic_ns: int,
        tool_version: str,
        sport: Optional[str] = None,
        requested_at: Optional[str] = None,
    ) -> IngestionRun: ...

    def complete(
        self,
        run_id: str,
        *,
        status: str,
        duration_ns: int,
        requests_made: int = 0,
        records_received: int = 0,
        records_normalized: int = 0,
        records_inserted: int = 0,
        records_updated: int = 0,
        records_deduplicated: int = 0,
        records_rejected: int = 0,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> IngestionRun: ...

    def get(self, run_id: str) -> Optional[IngestionRun]: ...

    def list_for_provider(self, provider: str, *, limit: int = 50) -> list[IngestionRun]: ...

    def count(self) -> int: ...


class SqliteIngestionRunRepository(Repository):
    """Ingestion-run storage."""

    _COLUMNS = (
        "run_id, command, provider, sport, operation, args_json, status, "
        "requested_at, started_at, completed_at, started_monotonic_ns, duration_ns, "
        "requests_made, records_received, records_normalized, records_inserted, "
        "records_updated, records_deduplicated, records_rejected, error_type, error_message, "
        "tool_version, created_at"
    )

    def start(
        self,
        *,
        command: str,
        provider: str,
        operation: str,
        args_json: str,
        started_monotonic_ns: int,
        tool_version: str,
        sport: Optional[str] = None,
        requested_at: Optional[str] = None,
    ) -> IngestionRun:
        """Open a run in the ``started`` state.

        ``requested_at`` records when the invocation was asked for; it defaults
        to now but may be passed in when the caller timed the request earlier.
        ``started_monotonic_ns`` is a monotonic reading, so the eventual
        duration never comes from subtracting two wall-clocks that can step.
        """

        run_id = new_ingestion_run_id()
        now = utc_now_iso()
        requested = requested_at or now
        self._conn.execute(
            "INSERT INTO ingestion_runs "
            "(run_id, command, provider, sport, operation, args_json, status, "
            " requested_at, started_at, completed_at, started_monotonic_ns, duration_ns, "
            " requests_made, records_received, records_normalized, records_inserted, "
            " records_deduplicated, records_rejected, error_type, error_message, "
            " tool_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?, NULL, ?, NULL, "
            " 0, 0, 0, 0, 0, 0, NULL, NULL, ?, ?)",
            (
                run_id,
                command,
                provider,
                sport,
                operation,
                args_json,
                requested,
                now,
                started_monotonic_ns,
                tool_version,
                now,
            ),
        )
        created = self.get(run_id)
        if created is None:  # pragma: no cover - unreachable after the insert
            raise RuntimeError(f"ingestion run {run_id!r} vanished immediately after insert")
        return created

    def complete(
        self,
        run_id: str,
        *,
        status: str,
        duration_ns: int,
        requests_made: int = 0,
        records_received: int = 0,
        records_normalized: int = 0,
        records_inserted: int = 0,
        records_updated: int = 0,
        records_deduplicated: int = 0,
        records_rejected: int = 0,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> IngestionRun:
        """Close a run with its terminal status and counters.

        ``status`` must be a terminal value (not ``started``); the schema's
        completion CHECK requires ``completed_at`` whenever it is.
        ``records_updated`` counts refreshes of existing mutable entities,
        distinct from ``records_inserted`` (new rows).
        """

        if status not in INGESTION_RUN_STATUSES or status == "started":
            raise RepositoryError(
                f"invalid terminal ingestion-run status {status!r}; expected one of "
                "'succeeded', 'partially_succeeded', 'failed'"
            )
        now = utc_now_iso()
        self._conn.execute(
            "UPDATE ingestion_runs SET "
            "status = ?, completed_at = ?, duration_ns = ?, requests_made = ?, "
            "records_received = ?, records_normalized = ?, records_inserted = ?, "
            "records_updated = ?, records_deduplicated = ?, records_rejected = ?, "
            "error_type = ?, error_message = ? "
            "WHERE run_id = ?",
            (
                status,
                now,
                duration_ns,
                requests_made,
                records_received,
                records_normalized,
                records_inserted,
                records_updated,
                records_deduplicated,
                records_rejected,
                error_type,
                error_message,
                run_id,
            ),
        )
        updated = self.get(run_id)
        if updated is None:
            raise RepositoryError(f"ingestion run {run_id!r} not found on completion")
        return updated

    def get(self, run_id: str) -> Optional[IngestionRun]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM ingestion_runs WHERE run_id = ?", (run_id,)
        )
        return None if row is None else self._to_model(row)

    def list_for_provider(self, provider: str, *, limit: int = 50) -> list[IngestionRun]:
        return [
            self._to_model(r)
            for r in self._fetch_all(
                f"SELECT {self._COLUMNS} FROM ingestion_runs WHERE provider = ? "
                "ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (provider, limit),
            )
        ]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM ingestion_runs")

    def _to_model(self, row: sqlite3.Row) -> IngestionRun:
        return IngestionRun(
            run_id=str(row["run_id"]),
            command=str(row["command"]),
            provider=str(row["provider"]),
            operation=str(row["operation"]),
            args_json=str(row["args_json"]),
            status=str(row["status"]),
            requested_at=str(row["requested_at"]),
            started_at=str(row["started_at"]),
            started_monotonic_ns=int(row["started_monotonic_ns"]),
            tool_version=str(row["tool_version"]),
            created_at=str(row["created_at"]),
            sport=self._opt_str(row, "sport"),
            completed_at=self._opt_str(row, "completed_at"),
            duration_ns=self._opt_int(row, "duration_ns"),
            requests_made=int(row["requests_made"]),
            records_received=int(row["records_received"]),
            records_normalized=int(row["records_normalized"]),
            records_inserted=int(row["records_inserted"]),
            records_updated=int(row["records_updated"]),
            records_deduplicated=int(row["records_deduplicated"]),
            records_rejected=int(row["records_rejected"]),
            error_type=self._opt_str(row, "error_type"),
            error_message=self._opt_str(row, "error_message"),
        )
