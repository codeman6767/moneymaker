"""F1A scratch-database isolation guards (read-only classification).

Network-capable pilot execution must target an **explicit** scratch database and
must never silently touch a default/development/production corpus. This module
classifies a supplied path and refuses unsafe targets. It never creates,
migrates, deletes, truncates, resets, or overwrites a database; it only reads to
fingerprint and classify. A rejected or failed attempt leaves every database
byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from ..db.schema import CURRENT_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS

#: A fresh scratch database reaches this version; an existing one may be any
#: SUPPORTED version, because e017 only adds tables.
EXPECTED_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION

#: Tables whose non-emptiness means the DB already holds ingested/derived corpus
#: data (as opposed to the seeded teams/leagues/aliases a fresh db-init creates).
_CORPUS_TABLES: tuple[str, ...] = (
    "games", "game_schedule_snapshots", "game_result_snapshots", "game_status_history",
    "mlb_inning_lines", "team_game_statistics", "player_game_statistics", "roster_snapshots",
    "probable_pitcher_snapshots", "lineup_snapshots", "nba_quarter_lines", "injury_snapshots",
    "play_snapshots", "nba_game_results", "nba_team_statistics", "nba_player_statistics",
    "weather_snapshots", "raw_responses", "ingestion_runs",
    "entity_match_decisions", "provider_game_references",
)


class ScratchClass(str, Enum):
    NEW = "new_uninitialized"
    #: An empty scratch corpus at any SUPPORTED schema version. The member name
    #: and wire value are kept stable (checkpoints and reports carry the string);
    #: the classification's ``schema_version`` field states the actual version.
    EMPTY_V16 = "empty_scratch_v16"
    AUTHORIZED_RESUMABLE = "authorized_resumable"
    UNSAFE = "unsafe_or_unrelated"


class ScratchDbError(RuntimeError):
    """A scratch-database isolation violation (no DB was mutated)."""


@dataclass(frozen=True)
class ScratchClassification:
    path: str
    kind: ScratchClass
    schema_version: Optional[int]
    fingerprint: Optional[str]
    row_counts: dict[str, int]
    reason: str


def _ro_connect(path: Path) -> sqlite3.Connection:
    # `mode=ro` never creates the database and never writes database bytes, and
    # it is chosen deliberately over `immutable=1` so committed WAL content IS
    # included in the digest (see `classify_scratch_db`).
    #
    # It is NOT sidecar-free, though: on a WAL-mode database `mode=ro` builds the
    # shared-memory index, which moves the `-shm` sidecar's mtime. Measured, not
    # assumed. Callers that must leave a protected evidence directory completely
    # untouched want `retrospective.sources.open_source_corpus`, which uses
    # `immutable=1` and refuses a non-empty WAL rather than reading a stale view.
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _schema_version(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_versions").fetchone()
    except sqlite3.Error:
        return None
    return None if row is None or row["v"] is None else int(row["v"])


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    counts: dict[str, int] = {}
    for t in names:
        counts[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: S608
    return counts


def fingerprint_of(counts: dict[str, int], schema_version: Optional[int]) -> str:
    """Legacy row-count fingerprint (kept for callers). Weak: cannot detect a
    content change at unchanged row counts. Prefer :func:`content_digest`."""

    payload = {"schema_version": schema_version, "row_counts": counts}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _table_content_digest(conn: sqlite3.Connection, table: str) -> str:
    """An order-independent digest of a table's full content.

    Each row is hashed from its canonical column values; the row hashes are sorted
    and combined, so the digest depends only on the SET of rows (content), not on
    physical/scan order -- and it CHANGES when any row value changes even if the
    row count does not (updated rows, delete+replace, changed raw responses or
    normalized observations are all detected).
    """

    cur = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - table from sqlite_master
    cols = [d[0] for d in cur.description]
    row_hashes: list[str] = []
    for row in cur:
        payload = json.dumps([cols, list(row)], sort_keys=True, separators=(",", ":"),
                             default=lambda o: repr(o))
        row_hashes.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    row_hashes.sort()
    h = hashlib.sha256()
    h.update(str(len(row_hashes)).encode("utf-8"))
    for rh in row_hashes:
        h.update(rh.encode("utf-8"))
    return h.hexdigest()


def content_digest(conn: sqlite3.Connection, schema_version: Optional[int]) -> str:
    """A deterministic whole-database content identity (schema + every row).

    Includes the full ``sqlite_master`` schema (detects schema-object changes) and
    a per-table content digest for every table. Opened read-only via ``mode=ro``
    (never ``immutable``), so committed WAL content IS included and no sidecar is
    written. Two databases with identical row counts but different content get
    different digests; a byte-substituted or copied database is likewise detected.
    """

    schema_rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name").fetchall()
    schema_repr = json.dumps([[r["type"], r["name"], r["sql"]] for r in schema_rows],
                            sort_keys=True, separators=(",", ":"))
    tables = [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    per_table = {t: _table_content_digest(conn, t) for t in tables}
    payload = {
        "schema_version": schema_version,
        "schema_objects": hashlib.sha256(schema_repr.encode("utf-8")).hexdigest(),
        "tables": per_table,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _reject_unsafe_path(path: Path, *, forbidden: tuple[Path, ...]) -> None:
    if path.exists() and path.is_dir():
        raise ScratchDbError(f"scratch db path is a directory: {path}")
    if path.is_symlink():
        raise ScratchDbError(f"scratch db path is a symlink (isolation risk): {path}")
    resolved = path.resolve()
    for f in forbidden:
        try:
            if resolved == f.resolve():
                raise ScratchDbError(
                    f"scratch db path resolves to a protected database: {path}")
        except OSError:
            continue


def classify_scratch_db(
    database_path: Optional[Path],
    *,
    resume: bool = False,
    expected_fingerprint: Optional[str] = None,
    forbidden_paths: tuple[Path, ...] = (),
) -> ScratchClassification:
    """Classify a supplied scratch DB path without mutating anything.

    Raises :class:`ScratchDbError` for a missing explicit path, a directory,
    a symlink, or a path resolving to a protected database. Read-only otherwise.
    """

    if database_path is None:
        raise ScratchDbError("an explicit --scratch-db path is required for live execution")
    path = Path(database_path)
    _reject_unsafe_path(path, forbidden=forbidden_paths)

    if not path.exists():
        return ScratchClassification(
            path=str(path), kind=ScratchClass.NEW, schema_version=None,
            fingerprint=None, row_counts={},
            reason="path does not exist; must be explicitly initialized to schema v16")

    conn = _ro_connect(path)
    try:
        version = _schema_version(conn)
        counts = _row_counts(conn)
        # Strong identity: a whole-database content digest (schema + every row),
        # WAL-aware, computed while the read-only connection is open.
        fp = content_digest(conn, version)
    finally:
        conn.close()
    nonempty = any(counts.get(t, 0) > 0 for t in _CORPUS_TABLES)

    if version not in SUPPORTED_SCHEMA_VERSIONS:
        return ScratchClassification(
            path=str(path), kind=ScratchClass.UNSAFE, schema_version=version,
            fingerprint=fp, row_counts=counts,
            reason=f"schema version {version} not in supported {sorted(SUPPORTED_SCHEMA_VERSIONS)}")

    if not nonempty:
        return ScratchClassification(
            path=str(path), kind=ScratchClass.EMPTY_V16, schema_version=version,
            fingerprint=fp, row_counts=counts, reason=f"empty schema-v{version} scratch database")

    # Non-empty v16 DB: only acceptable when a resume authorizes THIS fingerprint.
    if resume and expected_fingerprint is not None and expected_fingerprint == fp:
        return ScratchClassification(
            path=str(path), kind=ScratchClass.AUTHORIZED_RESUMABLE, schema_version=version,
            fingerprint=fp, row_counts=counts,
            reason="non-empty v16 database authorized by matching resume checkpoint")

    return ScratchClassification(
        path=str(path), kind=ScratchClass.UNSAFE, schema_version=version,
        fingerprint=fp, row_counts=counts,
        reason="non-empty database not authorized by a matching resume checkpoint")


def require_usable(classification: ScratchClassification, *, resume: bool) -> None:
    """Raise unless the classification permits the intended (fresh vs resume) run."""

    if classification.kind is ScratchClass.UNSAFE:
        raise ScratchDbError(classification.reason)
    if resume and classification.kind is not ScratchClass.AUTHORIZED_RESUMABLE:
        raise ScratchDbError(
            f"resume requires an authorized resumable database; got {classification.kind.value}: "
            f"{classification.reason}")
    if not resume and classification.kind is ScratchClass.AUTHORIZED_RESUMABLE:
        raise ScratchDbError("database matches a resume checkpoint but --resume was not requested")
