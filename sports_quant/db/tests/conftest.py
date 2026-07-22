"""Shared fixtures for the database tests.

Every fixture builds its database under pytest's ``tmp_path``, so a test run
can never touch the developer's real corpus at ``DATABASE_PATH``. Nothing here
makes a network call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import DbInitResult, initialize_database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A path for a fresh, temporary database file."""

    return tmp_path / "corpus.db"


@pytest.fixture
def database(db_path: Path) -> Database:
    """An unmigrated :class:`Database` pointing at a temporary file."""

    return Database(db_path)


@pytest.fixture
def initialized(db_path: Path) -> DbInitResult:
    """A fully migrated and seeded temporary database."""

    return initialize_database(db_path)


@pytest.fixture
def conn(db_path: Path, initialized: DbInitResult) -> Iterator[sqlite3.Connection]:
    """A configured connection to a migrated, seeded temporary database."""

    database = Database(db_path)
    with database.connection() as connection:
        yield connection


@pytest.fixture
def mlb_league_id(initialized: DbInitResult) -> str:
    return initialized.seeds.for_league("MLB").league_id


@pytest.fixture
def nba_league_id(initialized: DbInitResult) -> str:
    return initialized.seeds.for_league("NBA").league_id
