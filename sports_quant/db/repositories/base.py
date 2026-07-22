"""Shared repository plumbing.

Every repository takes a ``sqlite3.Connection`` rather than a ``Database``. That
is what lets a caller compose several repository calls into one transaction:
seeding leagues, teams and aliases is a single atomic unit, not three
independently-committing writes that can leave a half-built corpus behind.

Repositories own all SQL. Nothing outside this package writes a query, so a
schema change has one blast radius (see DATA_FOUNDATION_PLAN.md §3).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional


class RepositoryError(RuntimeError):
    """Raised when a repository operation cannot be completed."""


class Repository:
    """Base class holding the connection and small row helpers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # -- Row helpers ---------------------------------------------------------
    @staticmethod
    def _opt_str(row: sqlite3.Row, column: str) -> Optional[str]:
        value = row[column]
        return None if value is None else str(value)

    @staticmethod
    def _opt_int(row: sqlite3.Row, column: str) -> Optional[int]:
        value = row[column]
        return None if value is None else int(value)

    @staticmethod
    def _bool(row: sqlite3.Row, column: str) -> bool:
        return bool(row[column])

    def _fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchone()

    def _fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def _count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return 0 if row is None else int(row[0])


def to_db_bool(value: bool) -> int:
    """SQLite has no boolean type; store 0/1 with a CHECK behind it."""

    return 1 if value else 0
