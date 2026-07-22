"""Connection policy, PRAGMAs, transactions, and the SQL statement splitter."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import (
    Database,
    foreign_keys_enabled,
    split_sql_statements,
    table_exists,
    transaction,
)
from sports_quant.db.init import DbInitResult


def test_connect_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "corpus.db"
    database = Database(nested)
    with database.connection() as conn:
        assert conn is not None
    assert nested.parent.is_dir()
    assert nested.exists()


def test_foreign_keys_are_enabled_on_every_connection(database: Database) -> None:
    # foreign_keys is OFF by default in SQLite and is scoped per connection,
    # so this has to hold for a *newly opened* one, not just the first.
    for _ in range(3):
        with database.connection() as conn:
            assert foreign_keys_enabled(conn) is True


def test_rows_are_accessible_by_column_name(database: Database) -> None:
    with database.connection() as conn:
        conn.execute("CREATE TABLE t (a TEXT, b TEXT)")
        conn.execute("INSERT INTO t VALUES ('x', 'y')")
        row = conn.execute("SELECT a, b FROM t").fetchone()
    assert row["a"] == "x"
    assert row["b"] == "y"


def test_connection_is_closed_even_when_the_body_raises(database: Database) -> None:
    captured: list[sqlite3.Connection] = []
    with pytest.raises(RuntimeError):
        with database.connection() as conn:
            captured.append(conn)
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


def test_each_call_returns_a_distinct_connection(database: Database) -> None:
    """No shared global connection: two units of work never alias each other."""

    with database.connection() as first, database.connection() as second:
        assert first is not second


def test_transaction_commits_on_success(database: Database) -> None:
    with database.connection() as conn:
        conn.execute("CREATE TABLE t (a TEXT)")
        with transaction(conn):
            conn.execute("INSERT INTO t VALUES ('kept')")
    with database.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_transaction_rolls_back_on_failure(database: Database) -> None:
    with database.connection() as conn:
        conn.execute("CREATE TABLE t (a TEXT)")
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES ('discarded')")
                raise RuntimeError("boom")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_transaction_rolls_back_a_multi_statement_write(database: Database) -> None:
    with database.connection() as conn:
        conn.execute("CREATE TABLE t (a TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO t VALUES ('first')")
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES ('second')")
                conn.execute("INSERT INTO t VALUES ('first')")  # duplicate PK
        rows = [r[0] for r in conn.execute("SELECT a FROM t ORDER BY a")]
    # The whole unit rolled back: 'second' is gone too.
    assert rows == ["first"]


def test_nested_transaction_joins_the_outer_one(database: Database) -> None:
    with database.connection() as conn:
        conn.execute("CREATE TABLE t (a TEXT)")
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES ('outer')")
                with transaction(conn):
                    conn.execute("INSERT INTO t VALUES ('inner')")
                raise RuntimeError("boom")
        # The inner block did not commit independently.
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_table_exists(conn: sqlite3.Connection) -> None:
    assert table_exists(conn, "leagues") is True
    assert table_exists(conn, "definitely_not_a_table") is False


# --------------------------------------------------------------------------- #
# Statement splitter
# --------------------------------------------------------------------------- #
def test_split_simple_statements() -> None:
    assert split_sql_statements("CREATE TABLE a (x TEXT); CREATE TABLE b (y TEXT);") == [
        "CREATE TABLE a (x TEXT)",
        "CREATE TABLE b (y TEXT)",
    ]


def test_split_ignores_semicolons_inside_string_literals() -> None:
    sql = "INSERT INTO t VALUES ('a;b'); SELECT 1;"
    assert split_sql_statements(sql) == ["INSERT INTO t VALUES ('a;b')", "SELECT 1"]


def test_split_handles_doubled_quotes_and_backslash_literals() -> None:
    # SQLite escapes a quote by doubling it, and does NOT treat backslash as an
    # escape -- so ESCAPE '\' is a complete, valid literal.
    sql = r"""SELECT 'it''s; fine'; SELECT x LIKE 'lg\_%' ESCAPE '\';"""
    statements = split_sql_statements(sql)
    assert statements == ["SELECT 'it''s; fine'", r"SELECT x LIKE 'lg\_%' ESCAPE '\'"]


def test_split_keeps_a_trigger_body_intact() -> None:
    sql = """
    CREATE TRIGGER trg_no_update
    BEFORE UPDATE ON t
    BEGIN
        SELECT RAISE(ABORT, 'append-only');
    END;
    CREATE INDEX idx ON t (a);
    """
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].count("SELECT RAISE") == 1
    assert statements[0].rstrip().endswith("END")
    assert statements[1].startswith("CREATE INDEX")


def test_split_drops_comment_only_fragments() -> None:
    sql = "-- a leading comment\nCREATE TABLE a (x TEXT);\n-- trailing comment\n"
    assert split_sql_statements(sql) == ["-- a leading comment\nCREATE TABLE a (x TEXT)"]


def test_split_ignores_semicolons_in_comments() -> None:
    sql = "CREATE TABLE a (x TEXT); -- note; with a semicolon\nCREATE TABLE b (y TEXT);"
    assert len(split_sql_statements(sql)) == 2


def test_initialized_database_is_a_temporary_file(
    initialized: DbInitResult, tmp_path: Path
) -> None:
    """Guards the whole suite: tests must never touch the real corpus."""

    assert initialized.database_path.is_relative_to(tmp_path)
