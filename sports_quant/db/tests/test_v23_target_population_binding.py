"""v23 corpus target-population binding: schema, digests, projection, verifier.

Every test builds its own disposable database and synthetic evidence. No
provider is contacted, no historical artefact is read, and no real target-bound
corpus is instantiated.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.repositories.raw_responses import response_content_hash
from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.schema import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    utc_now_iso,
)
from sports_quant.retrospective.listing_projection import (
    LISTING_PROJECTION_POLICY_V1,
    ListingProjectionError,
    admitted_listing_responses,
    project_targets,
    verify_cursor_chain,
)
from sports_quant.retrospective.provenance import G1Variant, ProvenanceClass
from sports_quant.retrospective.target_binding import (
    TARGET_BINDING_POLICY_V1,
    TARGET_DERIVATION_POLICY_V1,
    TARGET_SET_POLICY_V1,
    TargetBindingError,
    derivation_digest,
    members_digest,
    target_binding_digest,
)
from sports_quant.retrospective.target_population import (
    ACQUISITION_COMPLETENESS_POLICY_V1,
    TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
    AcquisitionBinding,
    TargetPopulationError,
    load_acquisition_binding,
    required_listing_runs,
    scoped_source_digest,
    seal_target_population,
    verified_target_members,
    verify_corpus_target_population,
)

NOW = utc_now_iso()
MANIFEST_HASH_PLACEHOLDER = "0" * 64


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _db(tmp_path: Path, name: str = "v23.db") -> sqlite3.Connection:
    path = tmp_path / name
    Database(path).migrate()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _seed_league(conn: sqlite3.Connection) -> None:
    for league, code, sport, season in (("lg_nba", "NBA", "basketball", "sn_nba"),
                                        ("lg_mlb", "MLB", "baseball", "sn_mlb")):
        conn.execute(
            "INSERT INTO leagues (league_id, code, name, sport, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", (league, code, code, sport, NOW, NOW))
        conn.execute(
            "INSERT INTO seasons (season_id, league_id, year, label, phase, "
            "start_date, end_date, created_at, updated_at) VALUES "
            "(?, ?, 2025, '2025-26', 'regular', '2025-10-01', '2026-06-30', ?, ?)",
            (season, league, NOW, NOW))


def _make_game(conn: sqlite3.Connection, index: int, *, league: str = "lg_nba",
               season: str = "sn_nba", start: str = "2026-03-04T23:10:00Z") -> str:
    prefix = league[-3:]
    home, away = f"tm_{prefix}_{index}a", f"tm_{prefix}_{index}b"
    for team in (home, away):
        conn.execute(
            "INSERT INTO teams (team_id, league_id, canonical_name, city, nickname,"
            " abbreviation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (team, league, f"Team {team}", f"City {team}", f"Nick {team}",
             team[-6:], NOW, NOW))
    game_id = f"gm_{prefix}_{index}"
    conn.execute(
        "INSERT INTO games (game_id, league_id, season_id, home_team_id, away_team_id,"
        " scheduled_start, original_start, game_date_local, game_number, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'final', ?, ?)",
        (game_id, league, season, home, away, start, start, start[:10], index + 1,
         NOW, NOW))
    return game_id


def _make_run(conn: sqlite3.Connection, run_id: str) -> str:
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, sport, operation,"
        " args_json, status, requested_at, started_at, completed_at,"
        " started_monotonic_ns, tool_version, created_at) VALUES "
        "(?, 'ingest_nba', 'balldontlie', 'nba', 'get_games', '{}', 'succeeded',"
        " ?, ?, ?, 0, 'test', ?)", (run_id, NOW, NOW, NOW, NOW))
    return run_id


def _make_response(conn: sqlite3.Connection, response_id: str, run_id: str, *,
                   cursor: object = None, next_cursor: object = None,
                   provider_games: tuple[str, ...] = (),
                   endpoint: str = "/v1/games", provider: str = "balldontlie",
                   status: int = 200, start_date: str = "2026-03-01",
                   end_date: str = "2026-03-31", body: object = None) -> str:
    params: dict[str, object] = {"per_page": "100", "start_date": start_date,
                                 "end_date": end_date}
    if cursor is not None:
        params["cursor"] = cursor
    if body is None:
        payload: dict[str, object] = {"data": [{"id": g} for g in provider_games]}
        payload["meta"] = ({"next_cursor": next_cursor} if next_cursor is not None
                           else {"next_cursor": None})
        text = json.dumps(payload)
    else:
        text = body if isinstance(body, str) else json.dumps(body)
    # Hashes are built with the PRODUCTION helpers: the verifier now recomputes
    # both (review defect RV-1), so a fixture that invents its own scheme would
    # be testing the fixture rather than the code.
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint,"
        " request_params_json, http_status, response_headers_json, requested_at,"
        " received_at, elapsed_ns, body, body_bytes, body_hash, content_hash,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, 1, ?, ?, ?, ?, ?)",
        (response_id, run_id, provider, endpoint, json.dumps(params, sort_keys=True),
         status, NOW, NOW, text, len(text),
         hashlib.sha256(text.encode()).hexdigest(),
         response_content_hash(provider=provider, endpoint=endpoint,
                               request_params=params, body=text), NOW))
    return response_id


def _make_reference(conn: sqlite3.Connection, provider_game_id: str,
                    game_id: object, response_id: str) -> None:
    conn.execute(
        "INSERT INTO provider_game_references (reference_id, provider,"
        " provider_game_id, game_id, first_raw_response_id, current_raw_response_id,"
        " current_raw_response_hash, first_observed_at, last_observed_at,"
        " created_at, updated_at) VALUES (?, 'balldontlie', ?, ?, ?, ?, 'h', ?, ?, ?, ?)",
        (f"pgr_{provider_game_id}", provider_game_id, game_id, response_id,
         response_id, NOW, NOW, NOW, NOW))


def _write_manifest(tmp_path: Path, *, name: str = "m.json",
                    max_pages: int = 8, max_records: int = 1000,
                    max_games: int = 400, plan_version: str = "f1a-plan-v1",
                    date_range: str = "2026-03-01..2026-03-31") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({
        "manifest_format_version": "f1a-manifest-v1",
        "plan_version": plan_version,
        "provider": "balldontlie",
        "league": "nba",
        "date_range": date_range,
        "families": ["games"],
        "bounds": {"max_pages": max_pages, "max_records": max_records,
                   "max_games": max_games},
    }, indent=2), encoding="utf-8")
    return path


def _write_checkpoint(tmp_path: Path, manifest: Path, *, name: str = "c.ckpt",
                      plan_version: str = "f1a-plan-v1",
                      date_range: str = "2026-03-01..2026-03-31",
                      stage_game_ids: tuple[str, ...] = ()) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({
        "checkpoint_format_version": "f1a-checkpoint-v2",
        "manifest_hash": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "plan_version": plan_version,
        "provider": "balldontlie",
        "league": "nba",
        "date_range": date_range,
        "families": ["games"],
        "scratch_db": "x.db",
        "scratch_fingerprint": "f" * 64,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "stage_game_ids": list(stage_game_ids),
    }), encoding="utf-8")
    return path


def _build_bound_corpus(conn: sqlite3.Connection, tmp_path: Path, *,
                        n_games: int = 3, pages: tuple[int, ...] = (2, 1),
                        **manifest_kw: object) -> tuple[str, Path, Path]:
    """A complete, correctly sealed target-bound corpus over synthetic evidence."""

    _seed_league(conn)
    games = [_make_game(conn, i) for i in range(n_games)]
    provider_ids = [f"1844{i:04d}" for i in range(n_games)]
    run = _make_run(conn, "run_listing_1")

    # Lay out the provider games across pages, chaining cursors.
    manifest = _write_manifest(tmp_path, **manifest_kw)  # type: ignore[arg-type]
    checkpoint = _write_checkpoint(tmp_path, manifest,
                                   stage_game_ids=tuple(provider_ids))
    idx, cursor, responses = 0, None, []
    for page_no, size in enumerate(pages):
        chunk = provider_ids[idx:idx + size]
        idx += size
        is_last = page_no == len(pages) - 1
        nxt = None if is_last else f"cur{page_no}"
        rid = _make_response(conn, f"raw_p{page_no}", run, cursor=cursor,
                             next_cursor=nxt, provider_games=tuple(chunk))
        responses.append(rid)
        cursor = nxt
    for pid, gid, rid in zip(provider_ids, games, [responses[0]] * len(games),
                             strict=True):
        _make_reference(conn, pid, gid, rid)
    conn.commit()

    binding = load_acquisition_binding(manifest, checkpoint_path=checkpoint)
    rows = admitted_listing_responses(conn, run_ids=[run])
    source = scoped_source_digest(rows)
    md = members_digest(league_id="lg_nba", members=games)
    dd = derivation_digest(acquisition_manifest_hash=binding.manifest_hash,
                           plan_version=binding.plan_version, run_ids=[run])
    bd = target_binding_digest(league_id="lg_nba", members_digest_value=md,
                               derivation_digest_value=dd)

    conn.execute("SAVEPOINT build")
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="cut", cutoff_policy_version="v1",
        source_corpus_digest=source, target_set_digest=bd,
        g1_variant=G1Variant.G1_B_CORE)
    seal_target_population(conn, corpus_version_id=corpus.corpus_version_id,
                           members=games, run_ids=[run], binding=binding)
    conn.execute("RELEASE build")
    conn.commit()
    return corpus.corpus_version_id, manifest, checkpoint


# --------------------------------------------------------------------------- #
# 1. Schema
# --------------------------------------------------------------------------- #
def test_fresh_database_reaches_v23_with_three_new_tables(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM schema_versions").fetchone()[0] == 23
    assert CURRENT_SCHEMA_VERSION == 23
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert len(tables) == 64
    assert {"reconstruction_corpus_targets", "reconstruction_corpus_target_runs",
            "reconstruction_corpus_target_seals"} <= tables
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_supported_versions_still_include_every_legacy_version() -> None:
    # Orphaning a preserved pilot artefact would be a destructive migration.
    assert {16, 17, 18, 19, 20, 21, 22, 23} == set(SUPPORTED_SCHEMA_VERSIONS)


def test_migration_is_idempotent_and_upgrade_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "up.db"
    Database(path).migrate()
    Database(path).migrate()  # second call must be a no-op
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM schema_versions").fetchone()[0] == 23


def test_legacy_corpus_digest_is_untouched_by_v23(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    legacy = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version="p1", cutoff_policy_id="cut",
        cutoff_policy_version="v1", source_corpus_digest="SRC",
        target_set_digest="identity-audit-no-targets",
        g1_variant=G1Variant.G1_B_CORE)
    conn.commit()
    again = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version="p1", cutoff_policy_id="cut",
        cutoff_policy_version="v1", source_corpus_digest="SRC",
        target_set_digest="identity-audit-no-targets",
        g1_variant=G1Variant.G1_B_CORE)
    assert again.semantic_digest == legacy.semantic_digest


# --------------------------------------------------------------------------- #
# 2. Frozen digest policies
# --------------------------------------------------------------------------- #
def test_ordering_never_changes_membership_identity() -> None:
    a = members_digest(league_id="lg_nba", members=["gm_c", "gm_a", "gm_b"])
    b = members_digest(league_id="lg_nba", members=["gm_a", "gm_b", "gm_c"])
    assert a == b


def test_membership_change_changes_identity() -> None:
    base = ["gm_a", "gm_b", "gm_c"]
    assert (members_digest(league_id="lg_nba", members=base)
            != members_digest(league_id="lg_nba", members=["gm_a", "gm_b", "gm_z"]))


def test_duplicate_member_is_refused_not_absorbed() -> None:
    with pytest.raises(TargetBindingError, match="duplicate"):
        members_digest(league_id="lg_nba", members=["gm_a", "gm_a"])


def test_duplicate_run_is_refused() -> None:
    with pytest.raises(TargetBindingError, match="duplicate"):
        derivation_digest(acquisition_manifest_hash="a" * 64,
                          plan_version="v1", run_ids=["run_1", "run_1"])


def test_digest_refuses_type_coercion() -> None:
    # `bool` is an int subclass and `int` is not a game id: B2's coercion defect.
    for bad in (1, True, None, 3.0):
        with pytest.raises(TargetBindingError):
            members_digest(league_id="lg_nba", members=["gm_a", bad])


def test_digest_refuses_untrimmed_or_empty_identifiers() -> None:
    for bad in (" gm_a", "gm_a ", ""):
        with pytest.raises(TargetBindingError):
            members_digest(league_id="lg_nba", members=[bad])


def test_unknown_policy_version_refuses() -> None:
    with pytest.raises(TargetBindingError, match="unknown target-set policy"):
        members_digest(league_id="lg_nba", members=["gm_a"],
                       policy_version="target-set-v2")
    with pytest.raises(TargetBindingError, match="unknown target-derivation"):
        derivation_digest(acquisition_manifest_hash="a" * 64, plan_version="v1",
                          run_ids=["r"], policy_version="x")
    with pytest.raises(TargetBindingError, match="unknown target-binding"):
        target_binding_digest(league_id="lg_nba", members_digest_value="a" * 64,
                              derivation_digest_value="b" * 64, policy_version="x")


def test_derivation_requires_a_hex_manifest_hash_and_a_run() -> None:
    with pytest.raises(TargetBindingError, match="sha256 hex"):
        derivation_digest(acquisition_manifest_hash="not-a-hash",
                          plan_version="v1", run_ids=["r"])
    with pytest.raises(TargetBindingError, match="at least one bound"):
        derivation_digest(acquisition_manifest_hash="a" * 64,
                          plan_version="v1", run_ids=[])


def test_same_members_different_runs_yield_different_corpus_identity() -> None:
    """The review's proved content-addressing violation, now closed."""

    md = members_digest(league_id="lg_nba", members=["gm_a", "gm_b"])
    d1 = derivation_digest(acquisition_manifest_hash="a" * 64, plan_version="v1",
                           run_ids=["run_1"])
    d2 = derivation_digest(acquisition_manifest_hash="a" * 64, plan_version="v1",
                           run_ids=["run_2"])
    assert d1 != d2
    assert (target_binding_digest(league_id="lg_nba", members_digest_value=md,
                                  derivation_digest_value=d1)
            != target_binding_digest(league_id="lg_nba", members_digest_value=md,
                                     derivation_digest_value=d2))


def test_different_members_same_runs_yield_different_identity() -> None:
    dd = derivation_digest(acquisition_manifest_hash="a" * 64, plan_version="v1",
                           run_ids=["run_1"])
    m1 = members_digest(league_id="lg_nba", members=["gm_a", "gm_b"])
    m2 = members_digest(league_id="lg_nba", members=["gm_a", "gm_c"])
    assert (target_binding_digest(league_id="lg_nba", members_digest_value=m1,
                                  derivation_digest_value=dd)
            != target_binding_digest(league_id="lg_nba", members_digest_value=m2,
                                     derivation_digest_value=dd))


def test_policy_names_are_frozen() -> None:
    # A rename is a semantic change and must break loudly, not drift.
    assert TARGET_SET_POLICY_V1 == "target-set-v1"
    assert TARGET_DERIVATION_POLICY_V1 == "target-derivation-v1"
    assert TARGET_BINDING_POLICY_V1 == "target-binding-v1"
    assert LISTING_PROJECTION_POLICY_V1 == "official-listing-projection-v1"
    assert ACQUISITION_COMPLETENESS_POLICY_V1 == "acquisition-completeness-v1"


def test_frozen_digest_vectors_pin_the_canonical_serialization() -> None:
    """Hand-computed vectors. A silent change to the canonical form trips here."""

    expected_members = hashlib.sha256(json.dumps(
        {"policy": "target-set-v1", "league_id": "lg_nba",
         "members": ["gm_a", "gm_b"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert members_digest(league_id="lg_nba", members=["gm_b", "gm_a"]) == expected_members


# --------------------------------------------------------------------------- #
# 3. Construct-then-seal and append-only hardening
# --------------------------------------------------------------------------- #
def test_construct_then_seal_succeeds_and_closes_membership(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    assert conn.execute(
        "SELECT member_count FROM reconstruction_corpus_target_seals "
        "WHERE corpus_version_id = ?", (corpus_id,)).fetchone()[0] == 3
    extra = _make_game(conn, 99)
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        conn.execute("INSERT INTO reconstruction_corpus_targets VALUES (?, ?, ?)",
                     (corpus_id, extra, NOW))


def test_run_bindings_are_closed_by_the_seal(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    _make_run(conn, "run_late")
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        conn.execute("INSERT INTO reconstruction_corpus_target_runs VALUES (?, ?, ?)",
                     (corpus_id, "run_late", NOW))


@pytest.mark.parametrize("statement", [
    "UPDATE reconstruction_corpus_targets SET game_id = 'gm_nba_9' "
    "WHERE corpus_version_id = ?",
    "DELETE FROM reconstruction_corpus_targets WHERE corpus_version_id = ?",
])
def test_membership_is_append_only(tmp_path: Path, statement: str) -> None:
    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement, (corpus_id,))


def test_membership_replace_is_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    existing = conn.execute(
        "SELECT game_id FROM reconstruction_corpus_targets WHERE corpus_version_id = ?",
        (corpus_id,)).fetchone()[0]
    for verb in ("REPLACE INTO", "INSERT OR REPLACE INTO"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"{verb} reconstruction_corpus_targets VALUES (?, ?, ?)",
                         (corpus_id, existing, NOW))


def test_seal_is_immutable(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    for statement in (
            "UPDATE reconstruction_corpus_target_seals SET member_count = 99 "
            "WHERE corpus_version_id = ?",
            "DELETE FROM reconstruction_corpus_target_seals WHERE corpus_version_id = ?"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(statement, (corpus_id,))


def test_seal_member_count_must_match_membership(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_1")
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
    conn.execute("INSERT INTO reconstruction_corpus_targets VALUES (?, ?, ?)",
                 (corpus.corpus_version_id, game, NOW))
    conn.execute("INSERT INTO reconstruction_corpus_target_runs VALUES (?, ?, ?)",
                 (corpus.corpus_version_id, run, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="member_count disagrees"):
        conn.execute(
            "INSERT INTO reconstruction_corpus_target_seals VALUES (?,?,?,?,?,?,?,?)",
            (corpus.corpus_version_id, TARGET_SET_POLICY_V1,
             LISTING_PROJECTION_POLICY_V1, ACQUISITION_COMPLETENESS_POLICY_V1,
             "a" * 64, "v1", 5, NOW))


def test_zero_member_seal_is_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    run = _make_run(conn, "run_1")
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
    conn.execute("INSERT INTO reconstruction_corpus_target_runs VALUES (?, ?, ?)",
                 (corpus.corpus_version_id, run, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reconstruction_corpus_target_seals VALUES (?,?,?,?,?,?,?,?)",
            (corpus.corpus_version_id, TARGET_SET_POLICY_V1,
             LISTING_PROJECTION_POLICY_V1, ACQUISITION_COMPLETENESS_POLICY_V1,
             "a" * 64, "v1", 0, NOW))


def test_seal_requires_at_least_one_run_binding(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    game = _make_game(conn, 0)
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
    conn.execute("INSERT INTO reconstruction_corpus_targets VALUES (?, ?, ?)",
                 (corpus.corpus_version_id, game, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="at least one bound"):
        conn.execute(
            "INSERT INTO reconstruction_corpus_target_seals VALUES (?,?,?,?,?,?,?,?)",
            (corpus.corpus_version_id, TARGET_SET_POLICY_V1,
             LISTING_PROJECTION_POLICY_V1, ACQUISITION_COMPLETENESS_POLICY_V1,
             "a" * 64, "v1", 1, NOW))


def test_member_from_the_wrong_league_is_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    mlb_game = _make_game(conn, 0, league="lg_mlb", season="sn_mlb")
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
    with pytest.raises(sqlite3.IntegrityError, match="league"):
        conn.execute("INSERT INTO reconstruction_corpus_targets VALUES (?, ?, ?)",
                     (corpus.corpus_version_id, mlb_game, NOW))


def test_member_pointing_at_a_missing_game_is_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO reconstruction_corpus_targets VALUES (?, ?, ?)",
                     (corpus.corpus_version_id, "gm_absent", NOW))


def test_failed_membership_insert_rolls_back_the_corpus(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    before = conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0]
    try:
        conn.execute("SAVEPOINT build")
        repo = SqliteRetrospectiveProvenanceRepository(conn)
        corpus = repo.record_corpus_version(
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            league_id="lg_nba",
            reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
            cutoff_policy_id="c", cutoff_policy_version="v",
            source_corpus_digest="s", target_set_digest="t",
            g1_variant=G1Variant.G1_B_CORE)
        conn.execute("INSERT INTO reconstruction_corpus_targets VALUES (?, ?, ?)",
                     (corpus.corpus_version_id, "gm_absent", NOW))
        conn.execute("RELEASE build")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO build")
        conn.execute("RELEASE build")
    after = conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0]
    assert before == after, "a failed membership insert must leave no orphan corpus"


def test_seal_helper_refuses_duplicate_input(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    binding = AcquisitionBinding(
        manifest_hash="a" * 64, plan_version="v1", provider="balldontlie",
        league="nba", date_range="2026-03-01..2026-03-31", families=("games",),
        start_date="2026-03-01", end_date="2026-03-31",
        max_pages=None, max_records=None, max_games=None)
    with pytest.raises(TargetPopulationError, match="duplicate member"):
        seal_target_population(conn, corpus_version_id="rcv_x",
                               members=["gm_a", "gm_a"], run_ids=["r"],
                               binding=binding)
    with pytest.raises(TargetPopulationError, match="at least one member"):
        seal_target_population(conn, corpus_version_id="rcv_x", members=[],
                               run_ids=["r"], binding=binding)


# --------------------------------------------------------------------------- #
# 4. Listing admission, cursor chain and projection
# --------------------------------------------------------------------------- #
def test_admission_excludes_every_non_listing_family(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_ok", run, provider_games=("1",))
    for endpoint in ("/v1/games/18447469", "/v1/stats", "/v1/box_scores",
                     "/v1/plays", "/v1/lineups", "/nba/v1/stats/advanced"):
        _make_response(conn, f"raw_{endpoint.replace('/', '_')}", run,
                       endpoint=endpoint, provider_games=("999",))
    _make_response(conn, "raw_other_provider", run, provider="mlb_statsapi",
                   provider_games=("998",))
    rows = admitted_listing_responses(conn, run_ids=[run])
    assert [r["raw_response_id"] for r in rows] == ["raw_ok"]


def test_failed_request_is_not_an_empty_listing(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_429", run, status=429, provider_games=())
    assert admitted_listing_responses(conn, run_ids=[run]) == []


def test_unrelated_run_evidence_is_not_admitted(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    bound = _make_run(conn, "run_bound")
    other = _make_run(conn, "run_other")
    _make_response(conn, "raw_bound", bound, provider_games=("1",))
    _make_response(conn, "raw_other", other, provider_games=("2",))
    rows = admitted_listing_responses(conn, run_ids=[bound])
    assert [r["raw_response_id"] for r in rows] == ["raw_bound"]


def test_complete_cursor_chain_verifies_and_unions_pages(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, next_cursor="c1", provider_games=("1", "2"))
    _make_response(conn, "raw_2", run, cursor="c1", next_cursor="c2",
                   provider_games=("3",))
    _make_response(conn, "raw_3", run, cursor="c2", provider_games=("4",))
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    assert chain.ok, chain.problems
    assert chain.pages == 3
    assert chain.provider_game_ids == ("1", "2", "3", "4")


def test_truncated_tail_is_detected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, next_cursor="c1", provider_games=("1",))
    _make_response(conn, "raw_2", run, cursor="c1", next_cursor="c2",
                   provider_games=("2",))  # claims more, none preserved
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    assert not chain.ok
    assert any("chain breaks" in p for p in chain.problems)


def test_missing_middle_page_is_detected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, next_cursor="c1", provider_games=("1",))
    _make_response(conn, "raw_3", run, cursor="c2", provider_games=("3",))
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    assert not chain.ok
    assert any("chain breaks" in p for p in chain.problems)
    assert any("not reachable" in p for p in chain.problems)


def test_duplicate_page_for_one_cursor_is_detected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, next_cursor="c1", provider_games=("1",))
    _make_response(conn, "raw_2", run, cursor="c1", provider_games=("2",))
    _make_response(conn, "raw_3", run, cursor="c1", provider_games=("3",))
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    assert not chain.ok
    assert any("duplicate listing page" in p for p in chain.problems)


def test_missing_first_page_is_detected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_2", run, cursor="c1", provider_games=("2",))
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    assert not chain.ok
    assert any("no first listing page" in p for p in chain.problems)


def test_malformed_body_refuses(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, body="{not json")
    with pytest.raises(ListingProjectionError, match="not valid JSON"):
        verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))


def test_listing_without_data_array_refuses(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, body={"meta": {"next_cursor": None}})
    with pytest.raises(ListingProjectionError, match="no `data` array"):
        verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))


def test_body_omitting_meta_is_refused_after_review_repair_rv2(
        tmp_path: Path) -> None:
    """Replaces the test that PINNED this as an accepted limitation.

    The independent review showed the documented mitigation (the manifest cap
    proof) closes nothing when the caps are far from binding, so a single
    100-game page with no `meta` certified as a complete population. Requiring
    the pagination envelope is safe against the preserved March evidence, whose
    three pages all carry `meta`.
    """

    conn = _db(tmp_path)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, body={"data": [{"id": "1"}]})  # no meta
    with pytest.raises(ListingProjectionError, match="no `meta`"):
        verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))


def test_projection_refuses_unresolved_and_never_drops(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("100", "200"))
    _make_reference(conn, "100", game, rid)          # resolved
    _make_reference(conn, "200", None, rid)          # NULL canonical identity
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    result = project_targets(conn, chain=chain, league_id="lg_nba")
    assert not result.ok
    assert any("UNRESOLVED" in p for p in result.problems)


def test_projection_refuses_a_provider_game_with_no_reference(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, provider_games=("777",))
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    result = project_targets(conn, chain=chain, league_id="lg_nba")
    assert any("no provider_game_references row" in p for p in result.problems)


def test_projection_refuses_a_wrong_league_resolution(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    mlb = _make_game(conn, 0, league="lg_mlb", season="sn_mlb")
    run = _make_run(conn, "run_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("100",))
    _make_reference(conn, "100", mlb, rid)
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    result = project_targets(conn, chain=chain, league_id="lg_nba")
    assert any("not 'lg_nba'" in p for p in result.problems)


def test_repeated_provider_game_across_pages_yields_one_target(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_1")
    rid = _make_response(conn, "raw_1", run, next_cursor="c1",
                         provider_games=("100",))
    _make_response(conn, "raw_2", run, cursor="c1", provider_games=("100",))
    _make_reference(conn, "100", game, rid)
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    result = project_targets(conn, chain=chain, league_id="lg_nba")
    assert result.ok, result.problems
    assert result.members == (game,)


def test_downstream_result_status_cannot_change_membership(tmp_path: Path) -> None:
    """A cancelled game returned by the listing stays a target."""

    conn = _db(tmp_path)
    _seed_league(conn)
    game = _make_game(conn, 0)
    conn.execute("UPDATE games SET status = 'cancelled' WHERE game_id = ?", (game,))
    run = _make_run(conn, "run_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("100",))
    _make_reference(conn, "100", game, rid)
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    result = project_targets(conn, chain=chain, league_id="lg_nba")
    assert result.members == (game,)


# --------------------------------------------------------------------------- #
# 5. Acquisition manifest / run completeness
# --------------------------------------------------------------------------- #
def test_manifest_hash_is_the_sha256_of_the_exact_file_bytes(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    binding = load_acquisition_binding(manifest)
    assert binding.manifest_hash == hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_checkpoint_disagreeing_with_the_manifest_refuses(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    checkpoint = _write_checkpoint(tmp_path, manifest, plan_version="other-plan")
    with pytest.raises(TargetPopulationError, match="contradicts"):
        load_acquisition_binding(manifest, checkpoint_path=checkpoint)


def test_checkpoint_with_a_foreign_manifest_hash_refuses(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    other = _write_manifest(tmp_path, name="other.json", max_pages=4)
    checkpoint = _write_checkpoint(tmp_path, other, name="c2.ckpt")
    with pytest.raises(TargetPopulationError, match="manifest_hash"):
        load_acquisition_binding(manifest, checkpoint_path=checkpoint)


def test_missing_manifest_refuses(tmp_path: Path) -> None:
    with pytest.raises(TargetPopulationError, match="no acquisition manifest"):
        load_acquisition_binding(tmp_path / "absent.json")


def test_required_run_set_is_derived_from_evidence_not_the_caller(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    manifest = _write_manifest(tmp_path)
    binding = load_acquisition_binding(manifest)
    for run_id in ("run_1", "run_2", "run_3"):
        _make_run(conn, run_id)
        _make_response(conn, f"raw_{run_id}", run_id, provider_games=("1",))
    _make_run(conn, "run_other_window")
    _make_response(conn, "raw_other", "run_other_window", provider_games=("9",),
                   start_date="2026-04-01", end_date="2026-04-30")
    assert required_listing_runs(conn, binding) == ("run_1", "run_2", "run_3")


# --------------------------------------------------------------------------- #
# 6. The verifier
# --------------------------------------------------------------------------- #
def test_a_correctly_sealed_corpus_verifies(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert report.ok, report.problems
    assert report.target_bound
    assert report.member_count == 3
    assert report.pages == 2


def test_legacy_unbound_corpus_is_refused_despite_a_plausible_digest(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    legacy = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version="p1", cutoff_policy_id="c",
        cutoff_policy_version="v", source_corpus_digest="s",
        # A perfectly well-formed 64-hex string with nothing behind it.
        target_set_digest="a" * 64, g1_variant=G1Variant.G1_B_CORE)
    conn.commit()
    report = verify_corpus_target_population(
        conn, legacy.corpus_version_id, manifest_path=manifest)
    assert not report.ok
    assert any("TARGET-UNBOUND" in p for p in report.problems)


def test_unsealed_corpus_with_members_and_runs_is_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_1")
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="b" * 64, g1_variant=G1Variant.G1_B_CORE)
    conn.execute("INSERT INTO reconstruction_corpus_targets VALUES (?, ?, ?)",
                 (corpus.corpus_version_id, game, NOW))
    conn.execute("INSERT INTO reconstruction_corpus_target_runs VALUES (?, ?, ?)",
                 (corpus.corpus_version_id, run, NOW))
    conn.commit()
    report = verify_corpus_target_population(
        conn, corpus.corpus_version_id, manifest_path=manifest)
    assert not report.ok
    assert any("no target seal" in p for p in report.problems)


def test_a_substituted_manifest_is_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, _, checkpoint = _build_bound_corpus(conn, tmp_path)
    forged = _write_manifest(tmp_path, name="forged.json", max_pages=99)
    report = verify_corpus_target_population(conn, corpus_id, manifest_path=forged)
    assert not report.ok
    assert any("seal commits" in p for p in report.problems)


def test_an_omitted_required_run_is_detected(tmp_path: Path) -> None:
    """The review's first primary attack: bind R1+R2, omit R3."""

    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    # A second listing run for the SAME manifest window now exists in evidence,
    # so the acquisition requires it, but the sealed corpus cannot bind it.
    _make_run(conn, "run_listing_2")
    _make_response(conn, "raw_extra", "run_listing_2", provider_games=("9999",))
    conn.commit()
    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert not report.ok
    assert any("requires runs the corpus does not bind" in p for p in report.problems)


def test_an_extra_unrelated_bound_run_is_detected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    binding = load_acquisition_binding(manifest)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_listing_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("100",))
    _make_reference(conn, "100", game, rid)
    unrelated = _make_run(conn, "run_unrelated")
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    conn.execute("SAVEPOINT b")
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="c" * 64, g1_variant=G1Variant.G1_B_CORE)
    seal_target_population(conn, corpus_version_id=corpus.corpus_version_id,
                           members=[game], run_ids=[run, unrelated], binding=binding)
    conn.execute("RELEASE b")
    conn.commit()
    report = verify_corpus_target_population(
        conn, corpus.corpus_version_id, manifest_path=manifest)
    assert any("does not require" in p for p in report.problems)


def test_a_forged_target_set_digest_is_detected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    binding = load_acquisition_binding(manifest)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_listing_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("100",))
    _make_reference(conn, "100", game, rid)
    rows = admitted_listing_responses(conn, run_ids=[run])
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    conn.execute("SAVEPOINT b")
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v",
        source_corpus_digest=scoped_source_digest(rows),
        target_set_digest="d" * 64,  # forged
        g1_variant=G1Variant.G1_B_CORE)
    seal_target_population(conn, corpus_version_id=corpus.corpus_version_id,
                           members=[game], run_ids=[run], binding=binding)
    conn.execute("RELEASE b")
    conn.commit()
    report = verify_corpus_target_population(
        conn, corpus.corpus_version_id, manifest_path=manifest)
    assert any("recomputed target-binding digest" in p for p in report.problems)


def test_a_resolved_provider_reference_cannot_be_remapped(tmp_path: Path) -> None:
    """The mutable-reference worry is already closed for RESOLVED references.

    `trg_provider_game_ref_identity_immutable` fires on
    `OLD.game_id IS NOT NULL AND NEW.game_id IS NOT OLD.game_id`, so once a
    provider game resolves to a canonical game the mapping is frozen. Target
    membership derived from it therefore cannot be silently rewritten.
    """

    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    other = _make_game(conn, 50)
    with pytest.raises(sqlite3.IntegrityError, match="identity columns are immutable"):
        conn.execute(
            "UPDATE provider_game_references SET game_id = ? WHERE provider_game_id = ?",
            (other, "18440000"))


def test_a_null_reference_may_later_resolve_but_cannot_be_sealed_meanwhile(
        tmp_path: Path) -> None:
    """The one mutation the trigger permits is NULL -> resolved, and it is safe.

    A corpus whose listing contains an unresolved provider game can never be
    sealed, because projection REFUSES rather than dropping it. So the permitted
    transition cannot retroactively change any sealed corpus's membership.
    """

    conn = _db(tmp_path)
    _seed_league(conn)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_listing_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("100",))
    _make_reference(conn, "100", None, rid)
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    assert not project_targets(conn, chain=chain, league_id="lg_nba").ok

    conn.execute("UPDATE provider_game_references SET game_id = ? "
                 "WHERE provider_game_id = ?", (game, "100"))  # permitted
    assert project_targets(conn, chain=chain, league_id="lg_nba").members == (game,)


def test_preserved_listing_evidence_is_append_only_at_the_database_level(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _build_bound_corpus(conn, tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM raw_responses WHERE raw_response_id = 'raw_p1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE raw_responses SET body = '{}' "
                     "WHERE raw_response_id = 'raw_p1'")


def test_tampering_with_bound_evidence_is_detected_even_with_triggers_dropped(
        tmp_path: Path) -> None:
    """An attacker with full SQL access can drop the guards; the verifier still fails."""

    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    assert verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint).ok
    conn.execute("DROP TRIGGER trg_raw_responses_no_delete")
    conn.execute("DELETE FROM raw_responses WHERE raw_response_id = 'raw_p1'")
    conn.commit()
    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert not report.ok, "a removed listing page must break verification"


def test_a_capped_listing_acquisition_cannot_be_sealed_as_complete(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    # max_pages=2 with a 2-page chain: the cap cannot be distinguished from a
    # natural terminus, so completeness is unprovable and sealing must refuse.
    corpus_id, manifest, checkpoint = _build_bound_corpus(
        conn, tmp_path, max_pages=2)
    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert not report.ok
    assert any("max_pages" in p for p in report.problems)


def test_unrelated_official_evidence_does_not_change_the_scoped_source_digest(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    before = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert before.ok, before.problems
    noise = _make_run(conn, "run_noise")
    _make_response(conn, "raw_noise", noise, endpoint="/v1/teams",
                   provider_games=("1",))
    conn.commit()
    after = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert after.ok, after.problems


def test_verified_target_members_raises_for_an_unverified_corpus(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    legacy = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version="p1", cutoff_policy_id="c",
        cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
    conn.commit()
    with pytest.raises(TargetPopulationError, match="not a verified target-bound"):
        verified_target_members(conn, legacy.corpus_version_id,
                                manifest_path=manifest)


# --------------------------------------------------------------------------- #
# 7. Sibling / distinct policy
# --------------------------------------------------------------------------- #
def test_target_bound_corpus_does_not_supersede_the_legacy_corpus(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    row = conn.execute(
        "SELECT supersedes_corpus_version_id, reconstruction_policy_version "
        "FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (corpus_id,)).fetchone()
    assert row[0] is None, "setting supersession would manufacture a lineage"
    assert row[1] == TARGET_BOUND_RECONSTRUCTION_POLICY_V1


def test_a_corpus_with_the_wrong_reconstruction_policy_is_refused(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    binding = load_acquisition_binding(manifest)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_listing_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("100",))
    _make_reference(conn, "100", game, rid)
    rows = admitted_listing_responses(conn, run_ids=[run])
    md = members_digest(league_id="lg_nba", members=[game])
    dd = derivation_digest(acquisition_manifest_hash=binding.manifest_hash,
                           plan_version=binding.plan_version, run_ids=[run])
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    conn.execute("SAVEPOINT b")
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version="some-other-policy",
        cutoff_policy_id="c", cutoff_policy_version="v",
        source_corpus_digest=scoped_source_digest(rows),
        target_set_digest=target_binding_digest(
            league_id="lg_nba", members_digest_value=md, derivation_digest_value=dd),
        g1_variant=G1Variant.G1_B_CORE)
    seal_target_population(conn, corpus_version_id=corpus.corpus_version_id,
                           members=[game], run_ids=[run], binding=binding)
    conn.execute("RELEASE b")
    conn.commit()
    report = verify_corpus_target_population(
        conn, corpus.corpus_version_id, manifest_path=manifest)
    assert any("reconstruction policy" in p for p in report.problems)
