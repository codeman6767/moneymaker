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
from .db.schema import CURRENT_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS
from .pit.asof import read_only_connection

EXIT_OK = 0
EXIT_THRESHOLD = 1
EXIT_DATABASE_ERROR = 3
#: The version a fresh database reaches. Read commands accept any SUPPORTED
#: version: e017 is additive, so a preserved v16 corpus stays readable rather
#: than becoming unopenable the moment the current build moves to v17.
EXPECTED_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION

Printer = _Callable[[str], None]


def resolve_db_path(database_path: Optional[Path]) -> Path:
    """The explicit ``--db`` path, or the configured default (offline resolution)."""

    if database_path is not None:
        return Path(database_path)
    from .config import load_settings
    return load_settings().resolved_database_path()


def validate_since(since: Optional[str]) -> Optional[str]:
    """Validate a ``--since`` value as a real ``YYYY-MM-DD`` calendar date, or raise
    ``ValueError`` (so an invalid date fails clearly rather than silently producing
    misleading zero counts)."""

    if since is None:
        return None
    from datetime import date
    try:
        y, m, d = (int(p) for p in since.split("-"))
        date(y, m, d)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid --since {since!r}: expected a real YYYY-MM-DD date") from exc
    if len(since) != 10 or since[4] != "-" or since[7] != "-":
        raise ValueError(f"invalid --since {since!r}: expected YYYY-MM-DD")
    return since


def pending_review_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """CURRENTLY-pending manual reviews grouped by entity_type (RF7).

    A decision counts only when it is still flagged (``needs_manual_review = 1``)
    AND is the LATEST decision for its ``(entity_type, source_provider, source_ref)``
    source -- an older flagged decision that was later superseded by a newer
    decision (or whose review was completed) is not counted. Deterministically
    ordered by entity_type."""

    rows = conn.execute(
        "SELECT d.entity_type AS et, COUNT(*) AS c FROM entity_match_decisions d "
        "WHERE d.needs_manual_review = 1 AND NOT EXISTS ("
        "  SELECT 1 FROM entity_match_decisions d2 "
        "  WHERE d2.entity_type = d.entity_type AND d2.source_provider = d.source_provider "
        "  AND d2.source_ref = d.source_ref AND ("
        "    d2.decided_at > d.decided_at "
        "    OR (d2.decided_at = d.decided_at AND d2.match_id > d.match_id))) "
        "GROUP BY d.entity_type ORDER BY d.entity_type").fetchall()
    return {str(r["et"]): int(r["c"]) for r in rows}


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
    # A nonempty WAL sidecar means committed-but-uncheckpointed data. The read-only
    # snapshot opens with SQLite immutable=1, which IGNORES the WAL and would read a
    # STALE corpus -- so fail closed rather than silently reporting stale state. We
    # never checkpoint (that would write to the user's database from a read command).
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        out(f"[FAILED ] database at {path} has an uncheckpointed WAL "
            f"({wal.stat().st_size} bytes); a complete read-only snapshot cannot be taken. "
            "Checkpoint the database (or close all writers) and retry.")
        return EXIT_DATABASE_ERROR
    try:
        with read_only_connection(path) as conn:
            version = schema_version_or_none(conn)
            if version is None:
                out(f"[FAILED ] database at {path} is not migrated; "
                    "run 'python -m sports_quant db-init'")
                return EXIT_DATABASE_ERROR
            if version not in SUPPORTED_SCHEMA_VERSIONS:
                out(f"[FAILED ] database at {path} is schema v{version}; supported: "
                    f"v{sorted(SUPPORTED_SCHEMA_VERSIONS)} (unsupported)")
                return EXIT_DATABASE_ERROR
            return work(conn)
    except sqlite3.DatabaseError as exc:
        out(f"[FAILED ] database at {path} is unreadable/corrupt: {exc}")
        return EXIT_DATABASE_ERROR
