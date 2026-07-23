"""Migration discovery, ordering, idempotency, and checksum enforcement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import (
    Database,
    MigrationChecksumError,
    MigrationError,
    discover_migrations,
)
from sports_quant.db.schema import PHASE_A_TABLES, PHASE_B_TABLES, SCHEMA_VERSION_TABLE

EXPECTED_MIGRATIONS = (
    (1, "a001_core_entities"),
    (2, "a002_games"),
    (3, "a003_integrity_guards"),
    (4, "b004_raw_responses"),
    (5, "b005_sportsbook"),
    (6, "b006_sportsbook_transition_dedup"),
)


def test_discovered_migrations_are_ordered_and_complete() -> None:
    found = [(m.version, m.name) for m in discover_migrations()]
    assert found == list(EXPECTED_MIGRATIONS)


def test_migration_versions_are_monotonic_without_gaps() -> None:
    versions = [m.version for m in discover_migrations()]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))


def test_migration_checksums_are_stable_across_reads() -> None:
    first = {m.name: m.checksum for m in discover_migrations()}
    second = {m.name: m.checksum for m in discover_migrations()}
    assert first == second


def test_first_migration_creates_every_phase_a_table(database: Database) -> None:
    result = database.migrate()
    assert result.schema_version == len(EXPECTED_MIGRATIONS)
    with database.connection() as conn:
        names = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    for table in PHASE_A_TABLES:
        assert table in names, f"{table} missing after migration"
    for table in PHASE_B_TABLES:
        assert table in names, f"{table} missing after migration"
    assert SCHEMA_VERSION_TABLE in names


def test_schema_version_table_records_each_migration(database: Database) -> None:
    database.migrate()
    with database.connection() as conn:
        applied = database.applied_migrations(conn)
    assert [(a.version, a.name) for a in applied] == list(EXPECTED_MIGRATIONS)
    for record in applied:
        assert len(record.checksum) == 64  # sha256 hex
        assert record.applied_at.endswith("Z")


def test_migrate_is_idempotent(database: Database) -> None:
    first = database.migrate()
    second = database.migrate()
    assert len(first.applied) == len(EXPECTED_MIGRATIONS)
    assert second.applied == ()
    assert second.was_current is True
    assert second.schema_version == first.schema_version


def test_only_pending_migrations_are_applied(database: Database, tmp_path: Path) -> None:
    """Apply migration 1 alone, then confirm a full run applies only the rest."""

    partial_dir = tmp_path / "partial"
    partial_dir.mkdir()
    all_migrations = discover_migrations()
    (partial_dir / f"{all_migrations[0].name}.sql").write_text(
        all_migrations[0].sql, encoding="utf-8"
    )

    Database(database.path, migrations_dir=partial_dir).migrate()
    with database.connection() as conn:
        assert database.schema_version(conn) == 1

    result = database.migrate()
    assert [m.name for m in result.applied] == [m.name for m in all_migrations[1:]]
    assert result.schema_version == len(all_migrations)


def test_schema_version_is_zero_on_an_empty_database(database: Database) -> None:
    with database.connection() as conn:
        assert database.schema_version(conn) == 0


def test_edited_migration_raises_a_checksum_error(tmp_path: Path) -> None:
    """A migration edited after being applied is a hard error, never a warning.

    It means the live schema no longer matches the file the corpus was built
    with -- continuing silently is how a corpus quietly rots.
    """

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    original = discover_migrations()[0]
    target = migrations_dir / f"{original.name}.sql"
    target.write_text(original.sql, encoding="utf-8")

    db_path = tmp_path / "corpus.db"
    Database(db_path, migrations_dir=migrations_dir).migrate()

    target.write_text(original.sql + "\n-- edited after being applied\n", encoding="utf-8")
    with pytest.raises(MigrationChecksumError, match="has changed since it was applied"):
        Database(db_path, migrations_dir=migrations_dir).migrate()


def test_missing_applied_migration_file_raises(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    original = discover_migrations()[0]
    target = migrations_dir / f"{original.name}.sql"
    target.write_text(original.sql, encoding="utf-8")

    db_path = tmp_path / "corpus.db"
    Database(db_path, migrations_dir=migrations_dir).migrate()
    target.unlink()

    with pytest.raises(MigrationError, match="recorded as applied but its file is missing"):
        Database(db_path, migrations_dir=migrations_dir).migrate()


def test_badly_named_migration_file_raises(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "not_numbered.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="does not match the required"):
        discover_migrations(migrations_dir)


def test_duplicate_migration_version_raises(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "a001_one.sql").write_text("SELECT 1;", encoding="utf-8")
    (migrations_dir / "b001_two.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate migration version"):
        discover_migrations(migrations_dir)


def test_failed_migration_rolls_back_and_leaves_version_intact(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    good = discover_migrations()[0]
    (migrations_dir / f"{good.name}.sql").write_text(good.sql, encoding="utf-8")
    (migrations_dir / "a002_broken.sql").write_text(
        "CREATE TABLE ok_before_failure (x TEXT);\nTHIS IS NOT SQL;\n", encoding="utf-8"
    )

    db_path = tmp_path / "corpus.db"
    database = Database(db_path, migrations_dir=migrations_dir)
    with pytest.raises(MigrationError, match="failed and was rolled back"):
        database.migrate()

    with database.connection() as conn:
        # Migration 1 committed; migration 2 rolled back entirely, including the
        # table its first statement created.
        assert database.schema_version(conn) == 1
        names = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "leagues" in names
    assert "ok_before_failure" not in names


def test_migration_003_applies_once_and_is_idempotent(database: Database) -> None:
    first = database.migrate()
    assert (3, "a003_integrity_guards") in [(m.version, m.name) for m in first.applied]

    second = database.migrate()
    third = database.migrate()
    assert second.applied == () and third.applied == ()
    assert second.schema_version == third.schema_version == len(EXPECTED_MIGRATIONS)

    with database.connection() as conn:
        applied = database.applied_migrations(conn)
    # Exactly one row for migration 3, however many times db-init runs.
    assert len([a for a in applied if a.version == 3]) == 1


def test_migration_003_installs_its_triggers(database: Database) -> None:
    database.migrate()
    with database.connection() as conn:
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
    expected = {
        "trg_games_league_consistency_insert",
        "trg_games_league_consistency_update",
        "trg_games_original_start_immutable",
        "trg_team_aliases_league_consistency_insert",
        "trg_team_aliases_league_consistency_update",
        "trg_player_aliases_league_consistency_insert",
        "trg_player_aliases_league_consistency_update",
        # Recreated after the game_status_history rebuild.
        "trg_game_status_history_no_update",
        "trg_game_status_history_no_delete",
    }
    assert expected <= triggers


def test_migration_003_rebuild_preserves_existing_history_rows(tmp_path: Path) -> None:
    """The rebuild copies data; it does not start the table over.

    Applies 001-002 alone, writes a history row, then applies 003 and confirms
    the row survived the table swap.
    """

    partial_dir = tmp_path / "partial"
    partial_dir.mkdir()
    all_migrations = discover_migrations()
    for migration in all_migrations[:2]:
        (partial_dir / f"{migration.name}.sql").write_text(migration.sql, encoding="utf-8")

    db_path = tmp_path / "corpus.db"
    Database(db_path, migrations_dir=partial_dir).migrate()

    ts = "2026-07-01T00:00:00.000000Z"
    start = "2026-07-04T23:05:00.000000Z"
    database = Database(db_path)
    with database.connection() as conn:
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at, updated_at) "
            "VALUES ('lg_mlb', 'MLB', 'Major League Baseball', 'baseball', ?, ?)", (ts, ts)
        )
        conn.execute(
            "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date, "
            "created_at, updated_at) VALUES "
            "('sn_mlb_2026_regular', 'lg_mlb', 2026, '2026', 'regular', '2026-03-26', ?, ?)",
            (ts, ts),
        )
        for team, name, city, nick in [("tm_mlb_nyy", "New York Yankees", "New York", "Yankees"),
                                       ("tm_mlb_bos", "Boston Red Sox", "Boston", "Red Sox")]:
            conn.execute(
                "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname, "
                "abbreviation, created_at, updated_at) VALUES (?, 'lg_mlb', ?, ?, ?, ?, ?, ?)",
                (team, name, city, nick, team[-3:].upper(), ts, ts),
            )
        conn.execute(
            "INSERT INTO games (game_id, league_id, season_id, home_team_id, away_team_id, "
            "scheduled_start, original_start, game_date_local, game_number, is_neutral_site, "
            "status, created_at, updated_at) VALUES "
            "('gm_survivor', 'lg_mlb', 'sn_mlb_2026_regular', 'tm_mlb_nyy', 'tm_mlb_bos', "
            "?, ?, '2026-07-04', 1, 0, 'scheduled', ?, ?)", (start, start, ts, ts),
        )
        conn.execute(
            "INSERT INTO game_status_history (status_id, game_id, status, scheduled_start, "
            "provider, observed_at, ingested_at, content_hash, created_at) VALUES "
            "('gst_survivor', 'gm_survivor', 'scheduled', ?, 'mlb', ?, ?, 'hash-abc', ?)",
            (start, ts, ts, ts),
        )

    # Now apply the rest: 003 (and b004, which both rebuild game_status_history).
    result = database.migrate()
    assert [m.version for m in result.applied] == [3, 4, 5, 6]

    with database.connection() as conn:
        rows = conn.execute(
            "SELECT status_id, game_id, status, content_hash FROM game_status_history"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status_id"] == "gst_survivor"
    assert rows[0]["content_hash"] == "hash-abc"


def _seed_price_chain(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Insert one ingestion run -> raw response -> event -> market -> outcome and
    three price snapshots. Returns the snapshot rows inserted, for comparison."""

    ts = "2026-07-22T18:00:00.000000Z"
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, status, "
        "requested_at, started_at, started_monotonic_ns, tool_version, created_at) VALUES "
        "('run_seed', 'ingest-odds', 'the_odds_api', 'get_odds', '{}', 'started', ?, ?, 0, "
        "'test', ?)",
        (ts, ts, ts),
    )
    conn.execute(
        "UPDATE ingestion_runs SET status = 'succeeded', completed_at = ?, duration_ns = 1 "
        "WHERE run_id = 'run_seed'",
        (ts,),
    )
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, requested_at, received_at, "
        "elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) VALUES "
        "('raw_seed', 'run_seed', 'the_odds_api', '/v4/sports/baseball_mlb/odds', '{}', 200, "
        "'{}', ?, ?, 1, '[]', 2, 'bh', 'ch-raw', ?)",
        (ts, ts, ts),
    )
    conn.execute(
        "INSERT INTO sportsbook_events (sb_event_id, provider, provider_event_id, sport_key, "
        "commence_time, home_team_raw, away_team_raw, raw_response_id, first_observed_at, "
        "last_observed_at, created_at, updated_at) VALUES "
        "('sbe_seed', 'the_odds_api', 'e1', 'baseball_mlb', ?, 'NYY', 'BOS', 'raw_seed', ?, ?, "
        "?, ?)",
        (ts, ts, ts, ts, ts),
    )
    conn.execute(
        "INSERT INTO sportsbook_markets (sb_market_id, sb_event_id, bookmaker_key, market_key, "
        "raw_response_id, first_observed_at, last_observed_at, created_at, updated_at) VALUES "
        "('sbm_seed', 'sbe_seed', 'dk', 'h2h', 'raw_seed', ?, ?, ?, ?)",
        (ts, ts, ts, ts),
    )
    conn.execute(
        "INSERT INTO sportsbook_outcomes (sb_outcome_id, sb_market_id, outcome_name, "
        "provider_outcome_name, outcome_role, point_key, created_at) VALUES "
        "('sbo_seed', 'sbm_seed', 'nyy', 'NYY', 'home', '', ?)",
        (ts,),
    )
    # Three snapshots with DISTINCT content hashes (the old b005 UNIQUE
    # (sb_outcome_id, content_hash) permits only distinct-content rows).
    rows: list[dict[str, object]] = []
    for sid, price, obs, ch in [
        ("sbp_1", -110, "2026-07-22T18:00:00.000000Z", "ch-a"),
        ("sbp_2", -120, "2026-07-22T19:00:00.000000Z", "ch-b"),
        ("sbp_3", -115, "2026-07-22T20:00:00.000000Z", "ch-c"),
    ]:
        conn.execute(
            "INSERT INTO sportsbook_price_snapshots (snapshot_id, sb_outcome_id, price_american, "
            "observed_at, ingested_at, raw_response_id, raw_response_hash, run_id, content_hash, "
            "created_at) VALUES (?, 'sbo_seed', ?, ?, ?, 'raw_seed', 'ch-raw', 'run_seed', ?, ?)",
            (sid, price, obs, ts, ch, ts),
        )
        rows.append(
            {"snapshot_id": sid, "price_american": price, "observed_at": obs, "content_hash": ch}
        )
    return rows


def test_migration_b006_rebuild_preserves_every_price_snapshot(tmp_path: Path) -> None:
    """The b006 rebuild copies every snapshot verbatim -- ids, provenance, and all.

    Applies 001-005, writes a full sportsbook chain with three price snapshots
    under the old UNIQUE (sb_outcome_id, content_hash) rule, then applies b006
    and confirms row count and row contents are unchanged.
    """

    partial_dir = tmp_path / "partial"
    partial_dir.mkdir()
    all_migrations = discover_migrations()
    assert all_migrations[-1].name == "b006_sportsbook_transition_dedup"
    for migration in all_migrations[:5]:  # 001..b005
        (partial_dir / f"{migration.name}.sql").write_text(migration.sql, encoding="utf-8")

    db_path = tmp_path / "corpus.db"
    Database(db_path, migrations_dir=partial_dir).migrate()

    database = Database(db_path)
    with database.connection() as conn:
        conn.execute("BEGIN")
        expected = _seed_price_chain(conn)
        conn.execute("COMMIT")
        # Sanity: the old UNIQUE is still active here (pre-b006).
        old_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'sportsbook_price_snapshots'"
        ).fetchone()[0]
        assert "UNIQUE (sb_outcome_id, content_hash)" in old_sql
        assert "observed_at, content_hash" not in old_sql

    # Apply b006.
    result = database.migrate()
    assert [m.version for m in result.applied] == [6]

    with database.connection() as conn:
        after = conn.execute(
            "SELECT snapshot_id, sb_outcome_id, price_american, observed_at, ingested_at, "
            "raw_response_id, raw_response_hash, run_id, content_hash "
            "FROM sportsbook_price_snapshots ORDER BY observed_at"
        ).fetchall()

    assert len(after) == len(expected) == 3
    for row, exp in zip(after, expected, strict=True):
        assert row["snapshot_id"] == exp["snapshot_id"]
        assert row["price_american"] == exp["price_american"]
        assert row["observed_at"] == exp["observed_at"]
        assert row["content_hash"] == exp["content_hash"]
        # Provenance and references preserved exactly.
        assert row["sb_outcome_id"] == "sbo_seed"
        assert row["raw_response_id"] == "raw_seed"
        assert row["raw_response_hash"] == "ch-raw"
        assert row["run_id"] == "run_seed"


def test_append_only_trigger_blocks_update_and_delete(conn: sqlite3.Connection) -> None:
    """game_status_history is immutable: enforced by the database, not by habit."""

    league = conn.execute("SELECT league_id FROM leagues WHERE code = 'MLB'").fetchone()[0]
    conn.execute(
        "INSERT INTO seasons (season_id, league_id, year, label, phase, start_date, "
        "created_at, updated_at) VALUES "
        "('sn_mlb_2026_regular', ?, 2026, '2026', 'regular', '2026-03-26', "
        "'2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')",
        (league,),
    )
    conn.execute(
        "INSERT INTO games (game_id, league_id, season_id, home_team_id, away_team_id, "
        "scheduled_start, original_start, game_date_local, game_number, is_neutral_site, "
        "status, created_at, updated_at) VALUES "
        "('gm_test', ?, 'sn_mlb_2026_regular', 'tm_mlb_nyy', 'tm_mlb_bos', "
        "'2026-07-04T23:05:00.000000Z', '2026-07-04T23:05:00.000000Z', '2026-07-04', 1, 0, "
        "'scheduled', '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')",
        (league,),
    )
    conn.execute(
        "INSERT INTO game_status_history (status_id, game_id, status, scheduled_start, "
        "provider, observed_at, ingested_at, content_hash, created_at) VALUES "
        "('gst_1', 'gm_test', 'scheduled', '2026-07-04T23:05:00.000000Z', 'test', "
        "'2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 'hash1', "
        "'2026-07-01T00:00:00.000000Z')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE game_status_history SET status = 'final' WHERE status_id = 'gst_1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM game_status_history WHERE status_id = 'gst_1'")
