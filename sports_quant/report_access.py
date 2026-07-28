"""Shared read-only corpus-access plumbing for the E2 reporting commands.

``data-status`` and ``data-quality`` are OFFLINE and genuinely read-only: they
open the corpus through the E1 ``read_only_connection`` (SQLite ``immutable=1`` --
never creating the database or any ``-wal``/``-shm``/journal sidecar), require the
expected schema version, and never mutate a row. A missing / unmigrated / corrupt
/ unsupported database exits ``3``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Callable as _Callable
from typing import Optional

from .db.engine import table_exists
from .pit.asof import read_only_connection

EXIT_OK = 0
EXIT_THRESHOLD = 1
EXIT_DATABASE_ERROR = 3
EXPECTED_SCHEMA_VERSION = 16

Printer = _Callable[[str], None]


def resolve_db_path(database_path: Optional[Path]) -> Path:
    """The explicit ``--db`` path, or the configured default (offline resolution)."""

    if database_path is not None:
        return Path(database_path)
    from .config import load_settings
    return load_settings().resolved_database_path()


def schema_version_or_none(conn: sqlite3.Connection) -> Optional[int]:
    """Highest applied migration version, or None when unmigrated."""

    if not table_exists(conn, "schema_versions"):
        return None
    row = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def with_readonly_corpus(
    database_path: Optional[Path], out: Printer, work: Callable[[sqlite3.Connection], int],
) -> int:
    """Open the corpus read-only at the expected schema version and run ``work``.

    Returns ``work(conn)`` on success, or ``EXIT_DATABASE_ERROR`` (3) when the
    database is missing, unmigrated, corrupt, or at an unsupported schema. Creates
    no file and no sidecar."""

    path = resolve_db_path(database_path)
    if not path.exists():
        out(f"[FAILED ] database not found at {path}; run 'python -m sports_quant db-init'")
        return EXIT_DATABASE_ERROR
    try:
        with read_only_connection(path) as conn:
            version = schema_version_or_none(conn)
            if version is None:
                out(f"[FAILED ] database at {path} is not migrated; "
                    "run 'python -m sports_quant db-init'")
                return EXIT_DATABASE_ERROR
            if version != EXPECTED_SCHEMA_VERSION:
                out(f"[FAILED ] database at {path} is schema v{version}, expected "
                    f"v{EXPECTED_SCHEMA_VERSION} (unsupported)")
                return EXIT_DATABASE_ERROR
            return work(conn)
    except sqlite3.DatabaseError as exc:
        out(f"[FAILED ] database at {path} is unreadable/corrupt: {exc}")
        return EXIT_DATABASE_ERROR
