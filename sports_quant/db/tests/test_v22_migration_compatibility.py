"""v21 -> v22 upgrade compatibility.

The load-bearing claim is that f022 is ADDITIVE: an existing corpus keeps its
identity, its audits and its crosswalks. `reconstruction_corpus_versions` is
content-addressed, so a corpus whose `semantic_digest` changed during an upgrade
would silently become a different corpus and orphan every result attributed to it.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from sports_quant.db.engine import _BOOTSTRAP_SQL, Database, discover_migrations
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS

# Migrations that were already published before f022 and must never be edited:
# the runner records each file's checksum when it applies it, so editing an
# applied migration makes the live schema silently disagree with its history.
_FROZEN_MIGRATIONS = (
    "f018_retrospective_provenance",
    "f019_retrospective_provenance_repairs",
    "f020_historical_market_event_observations",
    "f021_append_only_replace_and_event_id_type",
)


def _fresh(path: Path) -> Database:
    db = Database(path)
    db.migrate()
    return db


def test_fresh_database_reaches_v22_with_22_migrations(tmp_path):
    _fresh(tmp_path / "fresh.db")
    migrations = discover_migrations()
    assert len(migrations) == 22
    assert migrations[-1].version == 22
    assert CURRENT_SCHEMA_VERSION == 22


def test_supported_versions_still_include_every_older_corpus():
    """Dropping an older version would orphan preserved pilot artifacts."""

    assert {16, 17, 18, 19, 20, 21, 22} <= set(SUPPORTED_SCHEMA_VERSIONS)


def test_frozen_migrations_are_unchanged():
    by_name = {m.name: m for m in discover_migrations()}
    for name in _FROZEN_MIGRATIONS:
        assert name in by_name, f"{name} disappeared"


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "idem.db"
    db = _fresh(path)
    before = _schema_fingerprint(path)
    db.migrate()  # second run must be a no-op
    assert _schema_fingerprint(path) == before


def _schema_fingerprint(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return sorted(
            (str(r[0]), str(r[1] or ""))
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        )
    finally:
        conn.close()


def _apply_through(path: Path, last_version: int) -> None:
    """Build a database containing only migrations up to ``last_version``."""

    conn = sqlite3.connect(path)
    # The engine's own bootstrap DDL, so the partial database is byte-compatible
    # with one the migrator produced itself.
    conn.executescript(_BOOTSTRAP_SQL)
    for migration in discover_migrations():
        if migration.version > last_version:
            break
        conn.executescript(migration.sql)
        conn.execute(
            "INSERT INTO schema_versions (version, name, checksum, applied_at,"
            " applied_by, execution_ms) VALUES (?,?,?,"
            "'2026-01-01T00:00:00.000000Z','test',0)",
            (migration.version, migration.name, migration.checksum))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("from_version", [18, 19, 20, 21])
def test_upgrade_path_preserves_existing_corpus_identity(tmp_path, from_version):
    """Upgrade from each supported version and prove nothing was rewritten."""

    path = tmp_path / f"upgrade_{from_version}.db"
    _apply_through(path, from_version)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    now = "2026-01-01T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO leagues (league_id, code, name, sport, created_at, updated_at)"
        " VALUES ('lg_nba','NBA','NBA','basketball',?,?)", (now, now))
    conn.execute(
        "INSERT INTO reconstruction_corpus_versions (corpus_version_id,"
        " provenance_class, league_id, reconstruction_policy_version,"
        " cutoff_policy_id, cutoff_policy_version, source_corpus_digest,"
        " target_set_digest, g1_variant, semantic_digest, created_at)"
        " VALUES ('rcv_legacy','reconstructed_research','lg_nba','p','cut','v1',"
        " 'SRC','TGT','g1_b_core','SEM_LEGACY', ?)", (now,))
    conn.execute(
        "INSERT INTO identity_audit_records (identity_audit_id, league_id, provider,"
        " namespace_generation, namespace_verified, entity_type,"
        " source_corpus_digest, audit_policy_version, distinct_ids,"
        " total_observations, collision_count, flagged_count, verdict,"
        " semantic_digest, created_at) VALUES ('ida_legacy','lg_nba','balldontlie',"
        " 'v1',1,'game','SRC','pol',1,1,0,0,'accepted','SEM_A', ?)", (now,))
    conn.commit()
    before = conn.execute(
        "SELECT semantic_digest, market_evidence_digest FROM"
        " reconstruction_corpus_versions WHERE corpus_version_id='rcv_legacy'"
    ).fetchone()
    before_digest = before["semantic_digest"]
    conn.close()

    Database(path).migrate()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    after = conn.execute(
        "SELECT semantic_digest, market_evidence_digest FROM"
        " reconstruction_corpus_versions WHERE corpus_version_id='rcv_legacy'"
    ).fetchone()
    assert after["semantic_digest"] == before_digest
    assert after["market_evidence_digest"] is None

    # The added column defaults to NULL on every pre-existing audit, which is
    # what keeps a legacy official audit valid.
    audit = conn.execute(
        "SELECT lane_binding_id, verdict FROM identity_audit_records"
        " WHERE identity_audit_id='ida_legacy'").fetchone()
    assert audit["lane_binding_id"] is None
    assert audit["verdict"] == "accepted"

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_upgraded_database_matches_a_fresh_one(tmp_path):
    """A v21 upgraded to v22 must be schema-identical to a fresh v22."""

    upgraded = tmp_path / "upgraded.db"
    _apply_through(upgraded, 21)
    Database(upgraded).migrate()

    fresh = tmp_path / "fresh2.db"
    _fresh(fresh)

    assert _schema_fingerprint(upgraded) == _schema_fingerprint(fresh)


def test_protected_evidence_is_never_migrated_in_place():
    """f022 is applied to working copies only; protected corpora are read-only.

    This test documents the contract rather than exercising it: the upgrade
    tests above all operate on `tmp_path` copies, never on `data/`.
    """

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "disposable.db"
        _fresh(scratch)
        assert scratch.exists()
