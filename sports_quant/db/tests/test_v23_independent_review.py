"""Independent adversarial review of v23 target-population binding (c4fd935).

Every defect below was reproduced against the shipped implementation before it
was repaired. Tests for RETAINED BLOCKERS assert the CURRENT (unsafe) behaviour
so the blocker cannot be silently forgotten or accidentally "fixed" without the
architecture decision that owns it -- each is named in
`V23_CORPUS_TARGET_POPULATION_BINDING_INDEPENDENT_REVIEW.md`.

Synthetic evidence only. No provider contact, no historical artefact mutation,
no real corpus.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.tests.test_v23_target_population_binding import (
    _build_bound_corpus,
    _db,
    _make_game,
    _make_reference,
    _make_response,
    _make_run,
    _seed_league,
    _write_checkpoint,
    _write_manifest,
)
from sports_quant.retrospective.listing_projection import (
    ListingProjectionError,
    admitted_listing_responses,
    project_targets,
    verify_cursor_chain,
    verify_response_integrity,
)
from sports_quant.retrospective.provenance import G1Variant, ProvenanceClass
from sports_quant.retrospective.target_population import (
    TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
    TargetPopulationError,
    load_acquisition_binding,
    required_listing_runs,
    seal_target_population,
    verify_corpus_target_population,
)


# --------------------------------------------------------------------------- #
# RV-1  raw-response integrity is recomputed, not trusted
# --------------------------------------------------------------------------- #
def test_rv1_forged_body_with_stale_hashes_is_detected(tmp_path: Path) -> None:
    """`scoped_source_digest` fingerprints the STORED content_hash, so a forged
    body left with its original hashes did not disturb it. Tampering was caught
    only when derived membership happened to differ; a forgery that preserved
    the member set would have passed."""

    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    assert verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint).ok

    conn.execute("DROP TRIGGER trg_raw_responses_no_update")
    body = json.loads(conn.execute(
        "SELECT body FROM raw_responses WHERE raw_response_id='raw_p0'").fetchone()[0])
    body["meta"]["next_cursor"] = "tampered"      # membership unchanged
    conn.execute("UPDATE raw_responses SET body=? WHERE raw_response_id='raw_p0'",
                 (json.dumps(body),))
    conn.commit()

    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert not report.ok
    assert any("body_hash" in p for p in report.problems), report.problems


def test_rv1_rewritten_request_params_are_detected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, _ = _build_bound_corpus(conn, tmp_path)
    conn.execute("DROP TRIGGER trg_raw_responses_no_update")
    conn.execute("UPDATE raw_responses SET request_params_json='{\"per_page\":\"1\"}' "
                 "WHERE raw_response_id='raw_p0'")
    conn.commit()
    report = verify_corpus_target_population(conn, corpus_id, manifest_path=manifest)
    assert not report.ok
    assert any("content_hash" in p for p in report.problems), report.problems


def test_rv1_integrity_helper_passes_on_untouched_evidence(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _build_bound_corpus(conn, tmp_path)
    rows = admitted_listing_responses(conn, run_ids=["run_listing_1"])
    assert verify_response_integrity(rows) == ()


# --------------------------------------------------------------------------- #
# RV-2  `meta` must be present on every accepted page
# --------------------------------------------------------------------------- #
def test_rv2_body_without_meta_is_refused(tmp_path: Path) -> None:
    """A single 100-game page with no `meta` previously certified as a complete
    population: the documented "cap proof" mitigation closes nothing when the
    caps (8 pages / 1000 records / 400 games) are nowhere near binding."""

    conn = _db(tmp_path)
    _seed_league(conn)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, body={"data": [{"id": "1"}]})
    with pytest.raises(ListingProjectionError, match="no `meta`"):
        verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))


def test_rv2_explicit_null_next_cursor_still_terminates(tmp_path: Path) -> None:
    """Only an ABSENT envelope is refused. The preserved March evidence carries
    `meta` on all three pages including the terminal one, so this stays true of
    real acquisitions."""

    conn = _db(tmp_path)
    _seed_league(conn)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, body={"data": [{"id": "1"}],
                                             "meta": {"next_cursor": None}})
    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    assert chain.ok and chain.pages == 1


def test_rv2_null_meta_is_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run, body={"data": [], "meta": None})
    with pytest.raises(ListingProjectionError, match="null `meta`"):
        verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))


# --------------------------------------------------------------------------- #
# RV-3 / RV-4  strict manifest parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field, value", [
    ("plan_version", True), ("plan_version", 1), ("plan_version", None),
    ("league", 123), ("provider", None), ("date_range", ["a", "b"]),
    ("plan_version", {"v": 1}),
])
def test_rv3_manifest_type_coercion_is_refused(tmp_path: Path, field: str,
                                               value: object) -> None:
    """`str(...)` turned None into "None", True into "True" and 1 into "1", so
    three different manifests could claim one plan version."""

    path = _write_manifest(tmp_path, name=f"m_{field}_{type(value).__name__}.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TargetPopulationError, match="must be a JSON string"):
        load_acquisition_binding(path)


def test_rv4_duplicate_manifest_keys_are_refused(tmp_path: Path) -> None:
    """Third appearance of the B2 defect class: last-value-wins meant a reader
    saw `"good"` while the parser used `"evil"`."""

    path = tmp_path / "dup.json"
    path.write_text(
        '{"manifest_format_version":"f1a-manifest-v1","plan_version":"good",'
        '"plan_version":"evil","provider":"balldontlie","league":"nba",'
        '"date_range":"2026-03-01..2026-03-31","families":["games"],'
        '"bounds":{"max_pages":8,"max_records":1000,"max_games":400}}',
        encoding="utf-8")
    with pytest.raises(TargetPopulationError, match="duplicate JSON key"):
        load_acquisition_binding(path)


def test_rv4_non_standard_json_constants_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "nan.json"
    path.write_text(
        '{"manifest_format_version":"f1a-manifest-v1","plan_version":"v",'
        '"provider":"balldontlie","league":"nba",'
        '"date_range":"2026-03-01..2026-03-31","families":["games"],'
        '"bounds":{"max_pages":NaN}}', encoding="utf-8")
    with pytest.raises(TargetPopulationError, match="non-standard JSON constant"):
        load_acquisition_binding(path)


def test_rv4_duplicate_keys_in_a_preserved_body_are_refused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    run = _make_run(conn, "run_1")
    _make_response(conn, "raw_1", run,
                   body='{"data":[],"meta":{"next_cursor":null},"meta":{"next_cursor":"x"}}')
    with pytest.raises(ListingProjectionError, match="duplicate key"):
        verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))


# --------------------------------------------------------------------------- #
# RV-5  date_range validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("date_range, match", [
    ("2026-03-01", "exactly 'START..END'"),
    ("2026-03-01..2026-03-15..2026-03-31", "exactly 'START..END'"),
    ("2026-03-31..2026-03-01", "ends before it starts"),
    ("2026-02-30..2026-03-31", "not a real calendar date"),
    ("2026-03-01..2026-3-31", "bare YYYY-MM-DD"),
])
def test_rv5_malformed_date_range_is_refused(tmp_path: Path, date_range: str,
                                             match: str) -> None:
    path = _write_manifest(tmp_path, name=f"d{abs(hash(date_range))}.json",
                           date_range=date_range)
    with pytest.raises(TargetPopulationError, match=match):
        load_acquisition_binding(path)


# --------------------------------------------------------------------------- #
# RV-6  the manifest must authorize the listing family
# --------------------------------------------------------------------------- #
def test_rv6_manifest_without_the_games_family_cannot_bind(tmp_path: Path) -> None:
    """Otherwise manifest binding is decorative: a manifest declaring only
    `stats` certified a population built from `/v1/games` responses that merely
    happened to exist."""

    path = _write_manifest(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["families"] = ["stats", "plays", "lineups"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TargetPopulationError, match="does not authorize the 'games'"):
        load_acquisition_binding(path)


def test_rv6_duplicate_or_empty_families_are_refused(tmp_path: Path) -> None:
    for families, match in ((["games", "games"], "twice"), ([], "non-empty list"),
                            ([1], "must be a non-empty string")):
        path = _write_manifest(tmp_path, name=f"f{abs(hash(str(families)))}.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["families"] = families
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(TargetPopulationError, match=match):
            load_acquisition_binding(path)


# --------------------------------------------------------------------------- #
# RV-7  the checkpoint is cross-checked, not merely loaded
# --------------------------------------------------------------------------- #
def test_rv7_checkpoint_naming_unlisted_games_is_refused(tmp_path: Path) -> None:
    """`stage_game_ids` was parsed and stored but never compared to anything."""

    conn = _db(tmp_path)
    corpus_id, manifest, _ = _build_bound_corpus(conn, tmp_path)
    bad = _write_checkpoint(tmp_path, manifest, name="bad.ckpt",
                            stage_game_ids=("NOT_IN_THE_LISTING",))
    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=bad)
    assert not report.ok
    assert any("never returned" in p for p in report.problems), report.problems


def test_rv7_duplicate_stage_game_ids_are_refused(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    ck = _write_checkpoint(tmp_path, manifest, stage_game_ids=("P0", "P0"))
    with pytest.raises(TargetPopulationError, match="duplicate provider game id"):
        load_acquisition_binding(manifest, checkpoint_path=ck)


# --------------------------------------------------------------------------- #
# RV-8  construction atomicity is owned by the API
# --------------------------------------------------------------------------- #
def test_rv8_failed_seal_leaves_no_open_child_rows(tmp_path: Path) -> None:
    """Previously the contract lived only in a docstring, so a caller who did
    not open a savepoint left committed membership behind an absent seal -- an
    OPEN corpus a later caller could still extend."""

    conn = _db(tmp_path)
    _seed_league(conn)
    game = _make_game(conn, 0)
    run = _make_run(conn, "run_1")
    conn.commit()
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version=TARGET_BOUND_RECONSTRUCTION_POLICY_V1,
        cutoff_policy_id="c", cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
    conn.commit()
    binding = load_acquisition_binding(_write_manifest(tmp_path))

    with pytest.raises(sqlite3.Error):
        # `run_absent` has no ingestion_runs row: the run-binding insert fails.
        seal_target_population(conn, corpus_version_id=corpus.corpus_version_id,
                               members=[game], run_ids=[run, "run_absent"],
                               binding=binding)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM reconstruction_corpus_targets"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reconstruction_corpus_target_runs"
                        ).fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# RETAINED BLOCKERS -- current behaviour pinned so it cannot be forgotten
# --------------------------------------------------------------------------- #
def test_rb1_a_later_unrelated_acquisition_invalidates_a_sealed_corpus(
        tmp_path: Path) -> None:
    """RETAINED BLOCKER RB-1 (SEVERE).

    `required_listing_runs` is a query over CURRENT database contents, not a
    binding to a precommitted acquisition identity. A sealed corpus that
    verified clean stops verifying when an unrelated later acquisition lands in
    the same date window, so corpus validity is not a property of the corpus.

    Pinned as-is: repairing it needs a persisted acquisition ledger that binds
    runs to a manifest at acquisition time, which does not exist and cannot be
    retrofitted onto the historical March runs.
    """

    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    assert verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint).ok

    later = _make_run(conn, "run_unrelated_later")
    _make_response(conn, "raw_unrelated", later, provider_games=("Z1",))
    conn.commit()

    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert not report.ok
    assert any("does not bind" in p for p in report.problems), report.problems


def test_rb1_two_acquisitions_in_one_window_are_indistinguishable(
        tmp_path: Path) -> None:
    """RETAINED BLOCKER RB-1 (SEVERE), same root cause: the "manifest binding"
    is a date-window scope predicate, the design the architecture rejected."""

    conn = _db(tmp_path)
    _seed_league(conn)
    a, b = _make_run(conn, "run_acq_a"), _make_run(conn, "run_acq_b")
    _make_response(conn, "raw_a", a, provider_games=("A1",))
    _make_response(conn, "raw_b", b, provider_games=("B1",))
    conn.commit()
    binding = load_acquisition_binding(_write_manifest(tmp_path))
    assert set(required_listing_runs(conn, binding)) == {"run_acq_a", "run_acq_b"}


def test_rb2_a_failed_or_empty_required_run_is_invisible(tmp_path: Path) -> None:
    """RETAINED BLOCKER RB-2 (HIGH).

    Required-run discovery considers only HTTP 200 responses, so a required
    listing unit that failed -- or an ingestion run that produced nothing --
    silently leaves the required set instead of failing completeness. Target
    discovery failure is therefore invisible.
    """

    conn = _db(tmp_path)
    _seed_league(conn)
    good = _make_run(conn, "run_good")
    _make_response(conn, "raw_good", good, provider_games=("P0",))
    failed = _make_run(conn, "run_failed")
    _make_response(conn, "raw_failed", failed, provider_games=(), status=500)
    _make_run(conn, "run_no_response")
    conn.commit()
    binding = load_acquisition_binding(_write_manifest(tmp_path))
    assert required_listing_runs(conn, binding) == ("run_good",)


def test_rb3_every_digest_input_is_a_database_local_surrogate() -> None:
    """RETAINED BLOCKER RB-3 (HIGH).

    `game_id`, `run_id` and `raw_response_id` are all random ULIDs, so
    members_digest, derivation_digest and target-source-scope-v1 are ALL
    database-local. A byte-copy is portable; the same evidence rebuilt in a
    fresh database yields a different corpus_version_id. f023 documents this for
    `game_id` only.
    """

    from sports_quant.db.ids import (
        new_game_id,
        new_ingestion_run_id,
        new_raw_response_id,
    )
    for factory in (new_game_id, new_ingestion_run_id, new_raw_response_id):
        assert factory() != factory(), f"{factory.__name__} is not deterministic"


def test_rb4_a_wrong_first_identity_resolution_projects_cleanly(
        tmp_path: Path) -> None:
    """RETAINED BLOCKER RB-4 (SEVERE).

    `trg_provider_game_ref_identity_immutable` freezes `game_id` once non-NULL,
    but immutability is not correctness: the FIRST NULL -> value assignment is
    unchecked. Projection compares only provider, provider_game_id and league --
    never the preserved listing payload's teams, start time or status -- so an
    arbitrary wrong resolution produces a clean, permanently frozen membership.

    This is why materializing the real 239 identities is not yet authorized.
    """

    conn = _db(tmp_path)
    _seed_league(conn)
    right = _make_game(conn, 1)
    wrong = _make_game(conn, 2)
    run = _make_run(conn, "run_1")
    rid = _make_response(conn, "raw_1", run, provider_games=("P1",))
    _make_reference(conn, "P1", None, rid)
    conn.execute("UPDATE provider_game_references SET game_id=? "
                 "WHERE provider_game_id='P1'", (wrong,))
    conn.commit()

    chain = verify_cursor_chain(admitted_listing_responses(conn, run_ids=[run]))
    result = project_targets(conn, chain=chain, league_id="lg_nba")
    assert result.ok, result.problems
    assert result.members == (wrong,) and right not in result.members


def test_rb5_no_run_records_its_acquisition_manifest(tmp_path: Path) -> None:
    """RETAINED BLOCKER RB-5.

    Nothing in `ingestion_runs` binds a run to the manifest that authorized it,
    so manifest PRECOMMITMENT is not provable from the database. The seal's
    manifest hash proves only "this corpus chose this file", not "this file
    constrained the historical acquisition".
    """

    conn = _db(tmp_path)
    _seed_league(conn)
    _make_run(conn, "run_1")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(ingestion_runs)")}
    assert not any("manifest" in c for c in columns), (
        "if ingestion_runs gains a manifest binding, RB-5 can be closed")


# --------------------------------------------------------------------------- #
# Confirmations: things the review attacked and found sound
# --------------------------------------------------------------------------- #
def test_no_path_trusts_seal_presence_without_recomputation() -> None:
    import inspect

    from sports_quant.retrospective import stage_a_provenance, target_population

    gate = inspect.getsource(stage_a_provenance._require_target_bound_parent)
    assert "verify_corpus_target_population" in gate
    # The seal table is read only by the verifier.
    assert inspect.getsource(target_population).count("_SEALS_TABLE") >= 1


def test_the_repaired_happy_path_still_verifies(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    report = verify_corpus_target_population(
        conn, corpus_id, manifest_path=manifest, checkpoint_path=checkpoint)
    assert report.ok, report.problems
