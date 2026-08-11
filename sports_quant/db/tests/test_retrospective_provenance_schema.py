"""Migration f018: schema shape, constraints and append-only guarantees.

Task §23. These tests exercise the DATABASE, largely through raw SQL rather than
the repository, on purpose: the CHECK constraints and triggers are meant to hold
against any writer, including a future one that is not this Python code. A test
that only ever writes through the repository would prove the repository is
careful, not that the schema is safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS

F018_TABLES = (
    "reconstruction_corpus_versions",
    "identity_audit_records",
    "identity_audit_findings",
    "static_crosswalk_provenance",
    "reconstructed_input_provenance",
)

F018_INDEXES = (
    "idx_rcv_league", "idx_rcv_supersedes",
    "idx_ida_namespace", "idx_ida_verdict",
    "idx_idf_audit", "idx_idf_entity",
    "idx_xwk_lookup", "idx_xwk_canonical", "idx_xwk_audit",
    "idx_rip_corpus", "idx_rip_target", "idx_rip_crosswalk",
)

ISO = "2026-08-11T00:00:00.000000Z"


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "migrations"


def _upto(path: Path, version: int) -> None:
    """Build a database containing only migrations up to ``version``.

    Used to produce a genuine v17 database (rather than a v18 one with rows
    deleted) so the v17 -> v18 upgrade is exercised for real.
    """

    src = _migrations_dir()
    staged = path.parent / f"migrations_v{version}"
    staged.mkdir(exist_ok=True)
    for sql in sorted(src.glob("*.sql")):
        if int(sql.name[1:4]) <= version:
            (staged / sql.name).write_bytes(sql.read_bytes())
    initialize_database(path, migrations_dir=staged)


# --------------------------------------------------------------------------- #
# §23 initialization and migration
# --------------------------------------------------------------------------- #
def test_fresh_database_initializes_at_v18(db_path: Path) -> None:
    result = initialize_database(db_path)
    assert result.schema_version == 18 == CURRENT_SCHEMA_VERSION
    assert result.migrations_applied == 18


def test_repeated_initialization_is_a_noop(db_path: Path) -> None:
    initialize_database(db_path)
    again = initialize_database(db_path)
    assert again.schema_version == 18
    assert again.migrations_applied == 0
    assert again.was_already_current


def test_migration_history_has_exactly_18_entries(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT version, name FROM schema_versions ORDER BY version").fetchall()
    assert [int(r["version"]) for r in rows] == list(range(1, 19))
    assert rows[-1]["name"].startswith("f018")


def test_v17_database_migrates_to_v18_without_touching_existing_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    _upto(path, 17)
    with Database(path).connection() as conn:
        assert Database(path).schema_version(conn) == 17
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("leagues", "teams", "seasons", "team_aliases")
        }
        legacy_rows = conn.execute(
            "SELECT version, checksum FROM schema_versions ORDER BY version").fetchall()
        legacy = [(int(r["version"]), str(r["checksum"])) for r in legacy_rows]

    initialize_database(path)

    with Database(path).connection() as conn:
        assert Database(path).schema_version(conn) == 18
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in before
        }
        assert after == before, "an additive migration changed existing application data"
        # Every previously-applied migration keeps its recorded checksum: f018 did
        # not rewrite an earlier migration to make itself fit.
        upgraded_rows = conn.execute(
            "SELECT version, checksum FROM schema_versions "
            "WHERE version <= 17 ORDER BY version").fetchall()
        assert [(int(r["version"]), str(r["checksum"])) for r in upgraded_rows] == legacy
        assert all(_table_exists(conn, t) for t in F018_TABLES)


def test_v17_to_v18_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _upto(path, 17)
    first = initialize_database(path)
    second = initialize_database(path)
    assert first.migrations_applied == 1
    assert second.migrations_applied == 0
    assert second.schema_version == 18


def test_integrity_and_foreign_keys_are_clean(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_supported_versions_still_admit_preserved_pilot_manifests() -> None:
    # The F1/F1B pilot checkpoints record the hash of a manifest declaring the
    # version current when they ran. Dropping 16 or 17 would orphan them.
    assert {16, 17, 18} <= SUPPORTED_SCHEMA_VERSIONS


# --------------------------------------------------------------------------- #
# §23 structure
# --------------------------------------------------------------------------- #
def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


@pytest.mark.parametrize("table", F018_TABLES)
def test_new_table_exists(conn: sqlite3.Connection, table: str) -> None:
    assert _table_exists(conn, table)


@pytest.mark.parametrize("index", F018_INDEXES)
def test_new_index_exists(conn: sqlite3.Connection, index: str) -> None:
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name = ?", (index,)
    ).fetchone() is not None


def test_no_availability_confidence_or_effective_at_column(
    conn: sqlite3.Connection,
) -> None:
    """The two fields the independent review removed must not exist anywhere.

    ``availability_confidence`` invites a soft threshold to be tuned until
    coverage looks good; a materialized ``effective_at`` is a second source of
    truth that goes stale when the rule changes.
    """

    for table in F018_TABLES:
        columns = {
            str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
        }
        assert "availability_confidence" not in columns
        assert "effective_at" not in columns
        # And no bypass, under any of its usual spellings.
        assert not {c for c in columns if "ignore" in c or "override" in c or "bypass" in c}


def test_existing_append_only_triggers_survive(conn: sqlite3.Connection) -> None:
    for trigger in ("trg_pti_no_update", "trg_ppi_no_delete",
                    "trg_raw_responses_no_update", "trg_raw_responses_no_delete",
                    "trg_game_status_history_no_update",
                    "trg_games_original_start_immutable"):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name = ?", (trigger,)
        ).fetchone() is not None


# --------------------------------------------------------------------------- #
# §23 append-only enforcement
# --------------------------------------------------------------------------- #
def _seed_corpus(conn: sqlite3.Connection, nba_league_id: str, digest: str = "d1") -> str:
    conn.execute(
        "INSERT INTO reconstruction_corpus_versions "
        "(corpus_version_id, provenance_class, league_id, reconstruction_policy_version,"
        " cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
        " target_set_digest, g1_variant, semantic_digest, created_at) "
        "VALUES (?, 'reconstructed_research', ?, 'rp1', 'cp', '1', 'src', 'tgt', "
        "'g1_b_core', ?, ?)",
        (f"rcv_{digest}", nba_league_id, digest, ISO),
    )
    return f"rcv_{digest}"


def test_corpus_version_refuses_update_and_delete(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE reconstruction_corpus_versions SET target_set_digest = 'x' "
            "WHERE corpus_version_id = ?", (cid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
            (cid,))


@pytest.mark.parametrize("table", F018_TABLES)
def test_every_new_table_has_both_append_only_triggers(
    conn: sqlite3.Connection, table: str
) -> None:
    triggers = {
        str(r["name"]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name = ?",
            (table,))
    }
    assert any(t.endswith("_no_update") for t in triggers), table
    assert any(t.endswith("_no_delete") for t in triggers), table


@pytest.mark.parametrize("table", F018_TABLES)
def test_every_new_table_refuses_update_and_delete_with_a_row_present(
    conn: sqlite3.Connection, nba_league_id: str, table: str
) -> None:
    """Refusal proved against a POPULATED table, for all five.

    A BEFORE UPDATE / BEFORE DELETE trigger never fires on an empty table, so
    asserting refusal without first inserting a row proves nothing at all -- the
    statement simply matches zero rows and reports success. Every table here is
    seeded first.
    """

    _seed_full_chain(conn, nba_league_id)
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, (  # noqa: S608
        f"{table} was not seeded; the refusal assertions below would be vacuous")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET created_at = created_at")  # noqa: S608
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608


def _seed_full_chain(conn: sqlite3.Connection, league_id: str) -> None:
    """One row in each of the five tables, satisfying every FK and trigger."""

    cid = _seed_corpus(conn, league_id, digest="chain")
    audit = _insert_audit(conn, league_id, audit_id="ida_chain", digest="chaind")
    conn.execute(
        "INSERT INTO identity_audit_findings "
        "(finding_id, identity_audit_id, league_id, provider, namespace_generation, "
        " entity_type, provider_id, severity, finding_code, classification, "
        " exclusion_scope, detail_json, detail_digest, created_at) "
        "VALUES ('idf_chain', ?, ?, 'balldontlie', 'v1', 'team', '1', 'info', 'M1', "
        "'legitimate_mutation', 'none', '{}', 'dd', ?)",
        (audit, league_id, ISO),
    )
    team_id = str(conn.execute(
        "SELECT team_id FROM teams WHERE league_id = ? ORDER BY team_id LIMIT 1",
        (league_id,)).fetchone()[0])
    conn.execute(
        "INSERT INTO static_crosswalk_provenance "
        "(crosswalk_id, corpus_version_id, league_id, provider, namespace_generation, "
        " entity_type, provider_id, canonical_entity_id, identity_audit_id, "
        " identity_audit_digest, provenance_policy_version, semantic_digest, "
        " curated_at, created_at) "
        "VALUES ('xwk_chain', ?, ?, 'balldontlie', 'v1', 'team', '1', ?, ?, "
        "'chaind', 'pp1', 'xwkd', ?, ?)",
        (cid, league_id, team_id, audit, ISO, ISO),
    )
    _insert_input(conn, league_id, cid, input_provenance_id="rip_chain",
                  semantic_digest="ripchain")


def test_audit_record_refuses_update_and_delete(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    _insert_audit(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE identity_audit_records SET verdict = 'accepted'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM identity_audit_records")


def _insert_audit(
    conn: sqlite3.Connection,
    league_id: str,
    *,
    audit_id: str = "ida_1",
    entity_type: str = "team",
    verdict: str = "accepted",
    collisions: int = 0,
    verified: int = 1,
    digest: str = "auditdigest1",
    provider: str = "balldontlie",
    generation: str = "v1",
) -> str:
    conn.execute(
        "INSERT INTO identity_audit_records "
        "(identity_audit_id, league_id, provider, namespace_generation, "
        " namespace_verified, entity_type, source_corpus_digest, audit_policy_version, "
        " distinct_ids, total_observations, collision_count, flagged_count, verdict, "
        " semantic_digest, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'src', 'ap1', 30, 239, ?, 0, ?, ?, ?)",
        (audit_id, league_id, provider, generation, verified, entity_type,
         collisions, verdict, digest, ISO),
    )
    return audit_id


# --------------------------------------------------------------------------- #
# §23 provenance-class and availability-basis constraints
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value", ["strict_forward_pit", "reconstructed_research", "label_only_retrospective"])
def test_valid_provenance_class_accepted_on_corpus(
    conn: sqlite3.Connection, nba_league_id: str, value: str
) -> None:
    conn.execute(
        "INSERT INTO reconstruction_corpus_versions "
        "(corpus_version_id, provenance_class, league_id, reconstruction_policy_version,"
        " cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
        " target_set_digest, g1_variant, semantic_digest, created_at) "
        "VALUES (?, ?, ?, 'rp1', 'cp', '1', 'src', 'tgt', 'g1_b_core', ?, ?)",
        (f"rcv_{value}", value, nba_league_id, value, ISO),
    )


@pytest.mark.parametrize("value", ["forward_only", "", "RECONSTRUCTED_RESEARCH", "research"])
def test_invalid_provenance_class_refused(
    conn: sqlite3.Connection, nba_league_id: str, value: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reconstruction_corpus_versions "
            "(corpus_version_id, provenance_class, league_id, "
            " reconstruction_policy_version, cutoff_policy_id, cutoff_policy_version, "
            " source_corpus_digest, target_set_digest, g1_variant, semantic_digest, "
            " created_at) "
            "VALUES ('rcv_bad', ?, ?, 'rp1', 'cp', '1', 'src', 'tgt', 'g1_b_core', "
            "'d', ?)",
            (value, nba_league_id, ISO),
        )


@pytest.mark.parametrize("value", ["g1_b_core", "g1_a_extended"])
def test_valid_g1_variant_accepted(
    conn: sqlite3.Connection, nba_league_id: str, value: str
) -> None:
    conn.execute(
        "INSERT INTO reconstruction_corpus_versions "
        "(corpus_version_id, provenance_class, league_id, reconstruction_policy_version,"
        " cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
        " target_set_digest, g1_variant, semantic_digest, created_at) "
        "VALUES (?, 'reconstructed_research', ?, 'rp1', 'cp', '1', 'src', 'tgt', ?, ?, ?)",
        (f"rcv_{value}", nba_league_id, value, value, ISO),
    )


def test_invalid_g1_variant_refused(conn: sqlite3.Connection, nba_league_id: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reconstruction_corpus_versions "
            "(corpus_version_id, provenance_class, league_id, "
            " reconstruction_policy_version, cutoff_policy_id, cutoff_policy_version, "
            " source_corpus_digest, target_set_digest, g1_variant, semantic_digest, "
            " created_at) "
            "VALUES ('rcv_x', 'reconstructed_research', ?, 'rp1', 'cp', '1', 'src', "
            "'tgt', 'g1_merged', 'd', ?)",
            (nba_league_id, ISO),
        )


def _insert_input(
    conn: sqlite3.Connection, league_id: str, corpus_id: str, **overrides: object
) -> None:
    row: dict[str, object] = {
        "input_provenance_id": "rip_1",
        "corpus_version_id": corpus_id,
        "league_id": league_id,
        "provider": "balldontlie",
        "namespace_generation": "v1",
        "provider_game_id": "g1",
        "feature_family": "rest_days",
        "provenance_class": "reconstructed_research",
        "availability_basis": "event_derived",
        "availability_rule_id": "r1",
        "availability_rule_digest": "rd1",
        "availability_source": None,
        "reconstruction_policy_version": "rp1",
        "source_evidence_table": None,
        "source_evidence_id": None,
        "source_event_completed_at": ISO,
        "source_snapshot_at": None,
        "crosswalk_id": None,
        "eligibility": "eligible",
        "exclusion_code": None,
        "semantic_digest": "ripdigest1",
        "created_at": ISO,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join("?" * len(row))
    conn.execute(
        f"INSERT INTO reconstructed_input_provenance ({columns}) "  # noqa: S608
        f"VALUES ({placeholders})",
        tuple(row.values()),
    )


@pytest.mark.parametrize(
    "basis,extra",
    [
        ("event_derived", {}),
        ("versioned_snapshot", {"source_event_completed_at": None,
                                "source_snapshot_at": ISO,
                                "availability_rule_id": None,
                                "availability_rule_digest": None}),
    ],
)
def test_valid_availability_basis_accepted(
    conn: sqlite3.Connection, nba_league_id: str, basis: str, extra: dict[str, object]
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    _insert_input(conn, nba_league_id, cid, availability_basis=basis, **extra)


@pytest.mark.parametrize("basis", ["forward_only", "static", "", "EVENT_DERIVED"])
def test_invalid_availability_basis_refused(
    conn: sqlite3.Connection, nba_league_id: str, basis: str
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(conn, nba_league_id, cid, availability_basis=basis)


def test_forward_only_cannot_be_certified_as_a_reconstructed_input(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    """The database refuses it, not just the repository (task §10)."""

    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(
            conn, nba_league_id, cid, provenance_class="strict_forward_pit",
            availability_basis=None, availability_rule_id=None,
            availability_rule_digest=None, source_event_completed_at=None,
        )


def test_reconstructed_research_requires_a_basis(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(
            conn, nba_league_id, cid, availability_basis=None,
            availability_rule_id=None, availability_rule_digest=None,
            source_event_completed_at=None,
        )


def test_label_only_is_structurally_distinguishable_from_a_predictive_input(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    """§2: LABEL_ONLY must be distinguishable from a reconstructed input.

    It is, by construction: a label carries no basis, no rule and no crosswalk,
    and the database refuses one that tries to.
    """

    cid = _seed_corpus(conn, nba_league_id)
    _insert_input(
        conn, nba_league_id, cid, provenance_class="label_only_retrospective",
        availability_basis=None, availability_rule_id=None,
        availability_rule_digest=None, source_event_completed_at=None,
        feature_family="final_score", semantic_digest="label1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(
            conn, nba_league_id, cid, input_provenance_id="rip_2",
            provenance_class="label_only_retrospective",
            availability_basis="event_derived", semantic_digest="label2",
        )


def test_event_derived_requires_completion_and_rule(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(conn, nba_league_id, cid, source_event_completed_at=None)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(
            conn, nba_league_id, cid, availability_rule_id=None,
            availability_rule_digest=None,
        )


def test_versioned_snapshot_requires_the_provider_stamp(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(
            conn, nba_league_id, cid, availability_basis="versioned_snapshot",
            source_event_completed_at=None, source_snapshot_at=None,
            availability_rule_id=None, availability_rule_digest=None,
        )


def test_eligibility_and_exclusion_code_are_mutually_determined(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(conn, nba_league_id, cid, eligibility="excluded")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(conn, nba_league_id, cid, exclusion_code="X1")


def test_rule_id_and_digest_travel_together(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    cid = _seed_corpus(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_input(conn, nba_league_id, cid, availability_rule_digest=None)


# --------------------------------------------------------------------------- #
# §23 audit verdict fail-closed rules
# --------------------------------------------------------------------------- #
def test_accepted_audit_cannot_carry_collisions(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_audit(conn, nba_league_id, verdict="accepted", collisions=3)


def test_accepted_audit_cannot_have_an_unverified_namespace(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    """G5: an unknown API generation is never eligible for acceptance."""

    with pytest.raises(sqlite3.IntegrityError):
        _insert_audit(conn, nba_league_id, verdict="accepted", verified=0,
                      generation="unverified")


def test_collision_verdict_requires_a_collision(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_audit(conn, nba_league_id, verdict="rejected_collision", collisions=0)


def test_observation_and_collision_counts_are_coherent(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO identity_audit_records "
            "(identity_audit_id, league_id, provider, namespace_generation, "
            " namespace_verified, entity_type, source_corpus_digest, "
            " audit_policy_version, distinct_ids, total_observations, collision_count, "
            " flagged_count, verdict, semantic_digest, created_at) "
            "VALUES ('ida_x', ?, 'p', 'v1', 1, 'team', 'src', 'ap', 30, 5, 0, 0, "
            "'accepted', 'dx', ?)",
            (nba_league_id, ISO),
        )


# --------------------------------------------------------------------------- #
# §23 namespace / entity-type integrity
# --------------------------------------------------------------------------- #
def test_finding_namespace_must_match_its_audit(
    conn: sqlite3.Connection, nba_league_id: str, mlb_league_id: str
) -> None:
    audit = _insert_audit(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError, match="must match its audit"):
        conn.execute(
            "INSERT INTO identity_audit_findings "
            "(finding_id, identity_audit_id, league_id, provider, "
            " namespace_generation, entity_type, provider_id, severity, finding_code, "
            " classification, exclusion_scope, detail_json, detail_digest, created_at) "
            "VALUES ('idf_1', ?, ?, 'balldontlie', 'v1', 'team', '1', 'blocking', "
            "'C1', 'identity_collision', 'entity', '{}', 'dd', ?)",
            (audit, mlb_league_id, ISO),
        )


def test_collision_finding_must_be_blocking(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    audit = _insert_audit(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO identity_audit_findings "
            "(finding_id, identity_audit_id, league_id, provider, "
            " namespace_generation, entity_type, provider_id, severity, finding_code, "
            " classification, exclusion_scope, detail_json, detail_digest, created_at) "
            "VALUES ('idf_2', ?, ?, 'balldontlie', 'v1', 'team', '1', 'info', 'C1', "
            "'identity_collision', 'entity', '{}', 'dd', ?)",
            (audit, nba_league_id, ISO),
        )


def test_only_a_team_finding_can_take_dependent_games_down(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    """The reviewed severity model: a team collision excludes its games."""

    audit = _insert_audit(conn, nba_league_id, entity_type="player",
                          audit_id="ida_p", digest="dp")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO identity_audit_findings "
            "(finding_id, identity_audit_id, league_id, provider, "
            " namespace_generation, entity_type, provider_id, severity, finding_code, "
            " classification, exclusion_scope, detail_json, detail_digest, created_at) "
            "VALUES ('idf_3', ?, ?, 'balldontlie', 'v1', 'player', '9', 'blocking', "
            "'C2', 'identity_collision', 'dependent_games', '{}', 'dd', ?)",
            (audit, nba_league_id, ISO),
        )


def test_namespace_finding_carries_no_provider_id_and_blocks_the_league(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    audit = _insert_audit(conn, nba_league_id, verdict="rejected_namespace_unverified",
                          verified=0, generation="unverified")
    conn.execute(
        "INSERT INTO identity_audit_findings "
        "(finding_id, identity_audit_id, league_id, provider, namespace_generation, "
        " entity_type, provider_id, severity, finding_code, classification, "
        " exclusion_scope, detail_json, detail_digest, created_at) "
        "VALUES ('idf_ok', ?, ?, 'balldontlie', 'unverified', 'team', NULL, 'blocking', "
        "'NS1', 'namespace_unverified', 'league_namespace', '{}', 'dd', ?)",
        (audit, nba_league_id, ISO),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO identity_audit_findings "
            "(finding_id, identity_audit_id, league_id, provider, "
            " namespace_generation, entity_type, provider_id, severity, finding_code, "
            " classification, exclusion_scope, detail_json, detail_digest, created_at) "
            "VALUES ('idf_bad', ?, ?, 'balldontlie', 'unverified', 'team', '7', "
            "'blocking', 'NS1', 'namespace_unverified', 'entity', '{}', 'dd2', ?)",
            (audit, nba_league_id, ISO),
        )


def test_legitimate_mutation_excludes_nothing(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    audit = _insert_audit(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO identity_audit_findings "
            "(finding_id, identity_audit_id, league_id, provider, "
            " namespace_generation, entity_type, provider_id, severity, finding_code, "
            " classification, exclusion_scope, detail_json, detail_digest, created_at) "
            "VALUES ('idf_4', ?, ?, 'balldontlie', 'v1', 'team', '1', 'blocking', 'M1', "
            "'legitimate_mutation', 'entity', '{}', 'dd', ?)",
            (audit, nba_league_id, ISO),
        )


# --------------------------------------------------------------------------- #
# §23 supersession / versioning
# --------------------------------------------------------------------------- #
def test_supersession_appends_and_leaves_the_old_corpus_untouched(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    old = _seed_corpus(conn, nba_league_id, digest="old")
    before = dict(conn.execute(
        "SELECT * FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (old,)).fetchone())
    conn.execute(
        "INSERT INTO reconstruction_corpus_versions "
        "(corpus_version_id, provenance_class, league_id, reconstruction_policy_version,"
        " cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
        " target_set_digest, g1_variant, semantic_digest, "
        " supersedes_corpus_version_id, created_at) "
        "VALUES ('rcv_new', 'reconstructed_research', ?, 'rp1', 'cp', '1', 'src2', "
        "'tgt', 'g1_b_core', 'new', ?, ?)",
        (nba_league_id, old, ISO),
    )
    after = dict(conn.execute(
        "SELECT * FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (old,)).fetchone())
    assert after == before, "supersession rewrote the superseded corpus"
    assert conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0] == 2


def test_corpus_cannot_supersede_itself(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reconstruction_corpus_versions "
            "(corpus_version_id, provenance_class, league_id, "
            " reconstruction_policy_version, cutoff_policy_id, cutoff_policy_version, "
            " source_corpus_digest, target_set_digest, g1_variant, semantic_digest, "
            " supersedes_corpus_version_id, created_at) "
            "VALUES ('rcv_self', 'reconstructed_research', ?, 'rp1', 'cp', '1', 'src', "
            "'tgt', 'g1_b_core', 'selfd', 'rcv_self', ?)",
            (nba_league_id, ISO),
        )


def test_supersession_target_must_already_exist(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    """Which is what makes the supersession graph acyclic by construction.

    An edge can only point at a row that already existed, and no row is ever
    updated, so a back-edge can never appear.
    """

    with pytest.raises(sqlite3.IntegrityError, match="does not exist"):
        conn.execute(
            "INSERT INTO reconstruction_corpus_versions "
            "(corpus_version_id, provenance_class, league_id, "
            " reconstruction_policy_version, cutoff_policy_id, cutoff_policy_version, "
            " source_corpus_digest, target_set_digest, g1_variant, semantic_digest, "
            " supersedes_corpus_version_id, created_at) "
            "VALUES ('rcv_orphan', 'reconstructed_research', ?, 'rp1', 'cp', '1', "
            "'src', 'tgt', 'g1_b_core', 'orph', 'rcv_never_existed', ?)",
            (nba_league_id, ISO),
        )


def test_semantic_digest_is_unique_per_corpus(
    conn: sqlite3.Connection, nba_league_id: str
) -> None:
    _seed_corpus(conn, nba_league_id, digest="dup")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reconstruction_corpus_versions "
            "(corpus_version_id, provenance_class, league_id, "
            " reconstruction_policy_version, cutoff_policy_id, cutoff_policy_version, "
            " source_corpus_digest, target_set_digest, g1_variant, semantic_digest, "
            " created_at) "
            "VALUES ('rcv_other', 'reconstructed_research', ?, 'rp1', 'cp', '1', "
            "'src', 'tgt', 'g1_b_core', 'dup', ?)",
            (nba_league_id, ISO),
        )
