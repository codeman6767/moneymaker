"""Data-quality issue repository.

Records quality/capability gaps (``DQ-CAP-*``, ``DQ-TZ-001``, ...) with a
severity, rule code, and provenance. Mutable: a resolution can be recorded. A
missing capability is written here as an explicit issue rather than fabricated as
available -- the corpus never reinterprets absence as "healthy/confirmed/supported".
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Protocol

from ..ids import new_data_quality_id
from ..models import DataQualityIssue
from ..schema import DATA_QUALITY_SEVERITIES, utc_now_iso
from .base import Repository, RepositoryError


class DataQualityRepositoryProtocol(Protocol):
    def record(
        self,
        *,
        severity: str,
        rule_code: str,
        entity_type: str,
        description: str,
        **fields: object,
    ) -> DataQualityIssue: ...


class SqliteDataQualityRepository(Repository):
    """Data-quality issue storage."""

    _COLUMNS = (
        "issue_id, severity, rule_code, entity_type, entity_id, provider, description, "
        "detail_json, run_id, raw_response_id, detected_at, resolved_at, resolution_note, "
        "created_at"
    )

    def record(
        self,
        *,
        severity: str,
        rule_code: str,
        entity_type: str,
        description: str,
        entity_id: Optional[str] = None,
        provider: Optional[str] = None,
        detail_json: Optional[str] = None,
        run_id: Optional[str] = None,
        raw_response_id: Optional[str] = None,
    ) -> DataQualityIssue:
        """Record one data-quality issue."""

        if severity not in DATA_QUALITY_SEVERITIES:
            raise RepositoryError(
                f"invalid severity {severity!r}; expected one of {list(DATA_QUALITY_SEVERITIES)}"
            )
        if not rule_code.strip():
            raise RepositoryError("rule_code must be non-blank")
        issue_id = new_data_quality_id()
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO data_quality_issues "
            "(issue_id, severity, rule_code, entity_type, entity_id, provider, description, "
            " detail_json, run_id, raw_response_id, detected_at, resolved_at, resolution_note, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                issue_id, severity, rule_code, entity_type, entity_id, provider, description,
                detail_json, run_id, raw_response_id, now, now,
            ),
        )
        issue = self.get(issue_id)
        assert issue is not None  # noqa: S101
        return issue

    def resolve(self, issue_id: str, *, note: Optional[str] = None) -> DataQualityIssue:
        now = utc_now_iso()
        self._conn.execute(
            "UPDATE data_quality_issues SET resolved_at = ?, resolution_note = ? "
            "WHERE issue_id = ?",
            (now, note, issue_id),
        )
        issue = self.get(issue_id)
        if issue is None:
            raise RepositoryError(f"data-quality issue {issue_id!r} not found")
        return issue

    def get(self, issue_id: str) -> Optional[DataQualityIssue]:
        row = self._fetch_one(
            f"SELECT {self._COLUMNS} FROM data_quality_issues WHERE issue_id = ?", (issue_id,)
        )
        return None if row is None else self._to_model(row)

    def list_open(
        self, *, severity: Optional[str] = None, limit: int = 200
    ) -> list[DataQualityIssue]:
        if severity is None:
            rows = self._fetch_all(
                f"SELECT {self._COLUMNS} FROM data_quality_issues WHERE resolved_at IS NULL "
                "ORDER BY detected_at, issue_id LIMIT ?",
                (limit,),
            )
        else:
            rows = self._fetch_all(
                f"SELECT {self._COLUMNS} FROM data_quality_issues "
                "WHERE resolved_at IS NULL AND severity = ? "
                "ORDER BY detected_at, issue_id LIMIT ?",
                (severity, limit),
            )
        return [self._to_model(r) for r in rows]

    def _filtered(
        self,
        *,
        base_where: str,
        base_params: list[object],
        rule_code: Optional[str],
        severity: Optional[str],
        entity_type: Optional[str],
        provider: Optional[str],
        entity_id: Optional[str],
        limit: int,
    ) -> list[DataQualityIssue]:
        clauses = [base_where]
        params: list[object] = list(base_params)
        for column, value in (
            ("rule_code", rule_code), ("severity", severity), ("entity_type", entity_type),
            ("provider", provider), ("entity_id", entity_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " AND ".join(clauses)
        params.append(limit)
        rows = self._fetch_all(
            f"SELECT {self._COLUMNS} FROM data_quality_issues WHERE {where} "  # noqa: S608
            "ORDER BY detected_at, issue_id LIMIT ?",
            tuple(params),
        )
        return [self._to_model(r) for r in rows]

    def find_open(
        self,
        *,
        rule_code: Optional[str] = None,
        severity: Optional[str] = None,
        entity_type: Optional[str] = None,
        provider: Optional[str] = None,
        entity_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[DataQualityIssue]:
        """Currently-open issues, filterable by rule/severity/entity/provider/id.

        Deterministically ordered ``(detected_at, issue_id)``."""

        return self._filtered(
            base_where="resolved_at IS NULL", base_params=[], rule_code=rule_code,
            severity=severity, entity_type=entity_type, provider=provider, entity_id=entity_id,
            limit=limit)

    def list_active_at(
        self,
        *,
        as_of: str,
        rule_code: Optional[str] = None,
        severity: Optional[str] = None,
        entity_type: Optional[str] = None,
        provider: Optional[str] = None,
        entity_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[DataQualityIssue]:
        """Issues that were ACTIVE at a historical cutoff (task §11): detected by
        ``as_of`` and not yet resolved by ``as_of`` -- ``detected_at <= as_of AND
        (resolved_at IS NULL OR resolved_at > as_of)``. Uses only the recorded
        transaction-time timeline (never today's mutable resolution state applied
        retroactively), so a resolution recorded after the cutoff still counts the
        issue as active at that cutoff. Deterministically ordered."""

        return self._filtered(
            base_where="detected_at <= ? AND (resolved_at IS NULL OR resolved_at > ?)",
            base_params=[as_of, as_of], rule_code=rule_code, severity=severity,
            entity_type=entity_type, provider=provider, entity_id=entity_id, limit=limit)

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM data_quality_issues")

    def _to_model(self, row: sqlite3.Row) -> DataQualityIssue:
        return DataQualityIssue(
            issue_id=str(row["issue_id"]),
            severity=str(row["severity"]),
            rule_code=str(row["rule_code"]),
            entity_type=str(row["entity_type"]),
            description=str(row["description"]),
            detected_at=str(row["detected_at"]),
            created_at=str(row["created_at"]),
            entity_id=self._opt_str(row, "entity_id"),
            provider=self._opt_str(row, "provider"),
            detail_json=self._opt_str(row, "detail_json"),
            run_id=self._opt_str(row, "run_id"),
            raw_response_id=self._opt_str(row, "raw_response_id"),
            resolved_at=self._opt_str(row, "resolved_at"),
            resolution_note=self._opt_str(row, "resolution_note"),
        )
