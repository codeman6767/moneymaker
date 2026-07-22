"""``db-init``: create the database, apply migrations, seed canonical data.

Kept separate from the CLI so the operation is testable without going through
``argparse``, and so no SQL leaks into command-line code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .engine import Database, Migration, transaction
from .seeds.loader import SeedResult, seed_all


@dataclass(frozen=True)
class DbInitResult:
    """Outcome of one ``db-init`` run."""

    database_path: Path
    created_database: bool
    schema_version: int
    applied: tuple[Migration, ...]
    seeds: SeedResult

    @property
    def migrations_applied(self) -> int:
        return len(self.applied)

    @property
    def was_already_current(self) -> bool:
        """True when nothing needed applying and no seed row was new."""

        return not self.applied and self.seeds.teams_created == 0


def initialize_database(
    database_path: Path | str,
    *,
    tool_version: str = "sports_quant",
    migrations_dir: Optional[Path] = None,
) -> DbInitResult:
    """Create, migrate and seed the corpus database.

    Safe to run repeatedly: migrations are applied only when pending and
    seeding is idempotent, so a second run reports zero new rows and destroys
    nothing.

    Migrations commit one at a time, so a failure leaves the schema at the last
    good version. Seeding then runs in a single separate transaction, so a seed
    failure rolls the seed back without undoing a schema that is already
    correct.
    """

    path = Path(database_path)
    if migrations_dir is None:
        db = Database(path, tool_version=tool_version)
    else:
        db = Database(path, tool_version=tool_version, migrations_dir=migrations_dir)

    created = not path.exists()
    db.ensure_parent_dir()

    with db.connection() as conn:
        migration_result = db.migrate(conn)
        with transaction(conn):
            seeds = seed_all(conn)
        schema_version = db.schema_version(conn)

    return DbInitResult(
        database_path=path,
        created_database=created,
        schema_version=schema_version,
        applied=migration_result.applied,
        seeds=seeds,
    )


__all__ = ["DbInitResult", "initialize_database"]
