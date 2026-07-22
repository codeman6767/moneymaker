"""SQLite engine: connections, transactions, and the migration runner.

Connection policy
-----------------
There is **no shared global connection**. :class:`Database` is a factory, and
every unit of work opens its own connection through a context manager that
closes it deterministically. A single connection passed around an application
is unsafe across threads and turns an unrelated failure into a poisoned
transaction for everyone else.

Repositories accept a ``sqlite3.Connection`` rather than a :class:`Database`,
so several repository calls compose into **one** transaction when the caller
wants that -- which is what makes a multi-step write atomic.

Migration policy
----------------
Migrations are forward-only numbered SQL files. Each is applied inside its own
transaction, and its SHA-256 is recorded. An already-applied migration whose
file has since changed is a hard error, never a warning: it means the live
schema no longer matches the definition the corpus was built with, and silently
continuing is how a data corpus quietly rots.

There is deliberately no ``down`` migration. Rolling back a historical corpus is
a restore-from-snapshot operation, not a schema operation.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator, Optional, Sequence

from .schema import SCHEMA_VERSION_TABLE, utc_now_iso

MIGRATIONS_DIR: Final = Path(__file__).resolve().parent / "migrations"

# `a001_core_entities.sql` -> version 1, name 'a001_core_entities'. The leading
# letter marks the phase and is cosmetic; the digits are a single global
# sequence, so Phase B continues at a003/b003 rather than restarting at 001 and
# colliding with a001.
_MIGRATION_FILENAME = re.compile(r"^(?P<phase>[a-z])(?P<number>\d{3})_(?P<slug>[a-z0-9_]+)\.sql$")

_BOOTSTRAP_SQL: Final = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} (
    version      INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    checksum     TEXT    NOT NULL,
    applied_at   TEXT    NOT NULL,
    applied_by   TEXT    NOT NULL,
    execution_ms INTEGER NOT NULL
)
"""


def split_sql_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements.

    ``sqlite3.Cursor.executescript`` cannot be used for migrations: it issues an
    implicit COMMIT before running, which silently ends the surrounding
    transaction and would leave a half-applied migration committed. Splitting
    lets each statement run inside one explicit transaction that really does
    roll back as a unit.

    The splitter understands SQL string literals (``''`` is an escaped quote;
    backslash is *not* an escape in SQLite, which matters for
    ``ESCAPE '\\'``), line and block comments, and ``CREATE TRIGGER`` bodies --
    whose ``BEGIN ... END;`` block contains semicolons that must not split it.
    """

    statements: list[str] = []
    buf: list[str] = []
    index = 0
    length = len(sql)
    in_string = False
    in_line_comment = False
    in_block_comment = False

    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""

        if in_line_comment:
            buf.append(char)
            if char == "\n":
                in_line_comment = False
            index += 1
            continue

        if in_block_comment:
            buf.append(char)
            if char == "*" and following == "/":
                buf.append(following)
                index += 2
                in_block_comment = False
                continue
            index += 1
            continue

        if in_string:
            buf.append(char)
            if char == "'":
                if following == "'":  # doubled quote inside a literal
                    buf.append(following)
                    index += 2
                    continue
                in_string = False
            index += 1
            continue

        if char == "-" and following == "-":
            in_line_comment = True
            buf.append(char)
            index += 1
            continue

        if char == "/" and following == "*":
            in_block_comment = True
            buf.append(char)
            index += 1
            continue

        if char == "'":
            in_string = True
            buf.append(char)
            index += 1
            continue

        if char == ";" and _is_statement_end(buf):
            statement = "".join(buf).strip()
            if _strip_comments(statement):
                statements.append(statement)
            buf = []
            index += 1
            continue

        buf.append(char)
        index += 1

    trailing = "".join(buf).strip()
    if _strip_comments(trailing):
        statements.append(trailing)
    return statements


def _strip_comments(statement: str) -> str:
    """Statement text with comment-only lines removed, for emptiness checks."""

    lines = []
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            lines.append(stripped)
    return " ".join(lines).strip()


def _is_statement_end(buf: list[str]) -> bool:
    """Whether a ``;`` at this point terminates the statement.

    Only a ``CREATE TRIGGER`` carries an inner ``BEGIN ... END;`` block, so its
    semicolons terminate the statement only when they follow ``END``.
    """

    body = _strip_comments("".join(buf))
    if not body.upper().startswith("CREATE TRIGGER"):
        return True
    return body.rstrip().upper().endswith("END")


class DatabaseError(RuntimeError):
    """Base class for database-layer failures."""


class MigrationError(DatabaseError):
    """Raised when migrations cannot be discovered, ordered, or applied."""


class MigrationChecksumError(MigrationError):
    """Raised when an applied migration's file has changed on disk."""


@dataclass(frozen=True)
class Migration:
    """One forward-only migration file."""

    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AppliedMigration:
    """A row of the schema-version table."""

    version: int
    name: str
    checksum: str
    applied_at: str


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of a :meth:`Database.migrate` call."""

    applied: tuple[Migration, ...]
    schema_version: int

    @property
    def was_current(self) -> bool:
        return not self.applied


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """Load and order every migration file in ``directory``.

    Ordering is by the numeric component, not by filename sort, so the sequence
    stays correct regardless of the phase letter.
    """

    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")

    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename {path.name!r} does not match the required "
                "pattern <phase-letter><3 digits>_<slug>.sql (e.g. a001_core_entities.sql)"
            )
        migrations.append(
            Migration(
                version=int(match.group("number")),
                name=path.stem,
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )

    migrations.sort(key=lambda m: m.version)

    seen: dict[int, str] = {}
    for migration in migrations:
        if migration.version in seen:
            raise MigrationError(
                f"duplicate migration version {migration.version}: "
                f"{seen[migration.version]!r} and {migration.name!r}"
            )
        seen[migration.version] = migration.name

    return tuple(migrations)


class Database:
    """A SQLite database file plus its connection and migration policy."""

    def __init__(
        self,
        path: Path | str,
        *,
        migrations_dir: Path = MIGRATIONS_DIR,
        tool_version: str = "sports_quant",
        timeout_seconds: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self._migrations_dir = migrations_dir
        self._tool_version = tool_version
        self._timeout = timeout_seconds

    # -- Connections ---------------------------------------------------------
    def ensure_parent_dir(self) -> None:
        """Create the containing directory. The corpus lives outside source."""

        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Open a new, fully configured connection. The caller must close it.

        Prefer :meth:`connection` or :meth:`transaction`, which close it for you.
        """

        self.ensure_parent_dir()
        conn = sqlite3.connect(
            str(self.path),
            timeout=self._timeout,
            # isolation_level=None disables the driver's implicit transaction
            # handling so BEGIN/COMMIT/ROLLBACK are explicit and visible.
            isolation_level=None,
        )
        # Rows behave like mappings: repositories read columns by name, so a
        # column reordering in a migration cannot silently shift a field.
        conn.row_factory = sqlite3.Row
        configure_connection(conn, timeout_seconds=self._timeout)
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """A connection closed deterministically, even on failure."""

        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A connection wrapped in one explicit transaction.

        Commits on clean exit, rolls back on any exception, and always closes.
        """

        conn = self.connect()
        try:
            with transaction(conn):
                yield conn
        finally:
            conn.close()

    # -- Migrations ----------------------------------------------------------
    def migrations(self) -> tuple[Migration, ...]:
        return discover_migrations(self._migrations_dir)

    def applied_migrations(self, conn: sqlite3.Connection) -> tuple[AppliedMigration, ...]:
        """Rows of the schema-version table, oldest first."""

        if not table_exists(conn, SCHEMA_VERSION_TABLE):
            return ()
        rows = conn.execute(
            f"SELECT version, name, checksum, applied_at FROM {SCHEMA_VERSION_TABLE} "
            "ORDER BY version"
        ).fetchall()
        return tuple(
            AppliedMigration(
                version=int(r["version"]),
                name=str(r["name"]),
                checksum=str(r["checksum"]),
                applied_at=str(r["applied_at"]),
            )
            for r in rows
        )

    def schema_version(self, conn: sqlite3.Connection) -> int:
        """Highest applied migration version, or 0 on an empty database."""

        applied = self.applied_migrations(conn)
        return applied[-1].version if applied else 0

    def pending_migrations(self, conn: sqlite3.Connection) -> tuple[Migration, ...]:
        """Migrations not yet applied, verifying the ones that are."""

        applied = {m.version: m for m in self.applied_migrations(conn)}
        available = self.migrations()
        self._verify_checksums(available, applied)
        return tuple(m for m in available if m.version not in applied)

    @staticmethod
    def _verify_checksums(
        available: Sequence[Migration], applied: dict[int, AppliedMigration]
    ) -> None:
        by_version = {m.version: m for m in available}
        for version, record in sorted(applied.items()):
            migration = by_version.get(version)
            if migration is None:
                raise MigrationError(
                    f"migration {version} ({record.name!r}) is recorded as applied but its "
                    "file is missing; the database was built by a different revision"
                )
            if migration.checksum != record.checksum:
                raise MigrationChecksumError(
                    f"migration {version} ({record.name!r}) has changed since it was applied. "
                    "The live schema no longer matches this file. Migrations are immutable "
                    "once applied -- add a new migration instead of editing this one."
                )

    def migrate(self, conn: Optional[sqlite3.Connection] = None) -> MigrationResult:
        """Apply every pending migration in order.

        Each migration runs in its own transaction, so a failure rolls back only
        that migration and leaves the schema version at the last good one.
        """

        if conn is not None:
            return self._migrate(conn)
        with self.connection() as owned:
            return self._migrate(owned)

    def _migrate(self, conn: sqlite3.Connection) -> MigrationResult:
        conn.execute(_BOOTSTRAP_SQL)
        pending = self.pending_migrations(conn)

        applied: list[Migration] = []
        for migration in pending:
            started = _now_ms()
            try:
                with transaction(conn):
                    for statement in split_sql_statements(migration.sql):
                        conn.execute(statement)
                    conn.execute(
                        f"INSERT INTO {SCHEMA_VERSION_TABLE} "
                        "(version, name, checksum, applied_at, applied_by, execution_ms) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            utc_now_iso(),
                            self._tool_version,
                            max(0, _now_ms() - started),
                        ),
                    )
            except sqlite3.Error as exc:
                raise MigrationError(
                    f"migration {migration.version} ({migration.name!r}) failed and was "
                    f"rolled back: {exc}"
                ) from exc
            applied.append(migration)

        return MigrationResult(applied=tuple(applied), schema_version=self.schema_version(conn))


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #
def configure_connection(conn: sqlite3.Connection, *, timeout_seconds: float = 5.0) -> None:
    """Apply the mandatory PRAGMAs to a connection.

    ``foreign_keys`` is **off** by default in SQLite and is scoped to the
    connection, so it must be set every time one is opened. Without it the
    foreign keys in the schema are documentation rather than enforcement.
    """

    # executescript() would commit any open transaction; issue these singly.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one explicit transaction on an existing connection.

    Nests safely: if a transaction is already open, the block joins it rather
    than opening a second one (SQLite has no nested transactions), leaving the
    outermost caller to commit or roll back.
    """

    if conn.in_transaction:
        yield conn
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def foreign_keys_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    return bool(row[0]) if row is not None else False


def _now_ms() -> int:
    import time

    return time.monotonic_ns() // 1_000_000
