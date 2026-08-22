"""v23 seams: §AF target derivation and the E0 enrichment gate.

Synthetic evidence only. No real corpus, no Stage-A plan, no provider contact.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.schema import utc_now_iso
from sports_quant.db.tests.test_v23_target_population_binding import (
    _build_bound_corpus,
    _db,
    _seed_league,
    _write_manifest,
)
from sports_quant.retrospective.provenance import G1Variant, ProvenanceClass
from sports_quant.retrospective.stage_a_provenance import (
    StageAProvenanceError,
    _require_target_bound_parent,
)
from sports_quant.retrospective.stage_a_target_bucket import (
    TargetPopulationUnavailable,
    derive_target_population,
    verify_stage_a_target_bucket_policy,
)

NOW = utc_now_iso()


def _legacy_corpus(conn: sqlite3.Connection) -> str:
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_nba",
        reconstruction_policy_version="p1", cutoff_policy_id="c",
        cutoff_policy_version="v", source_corpus_digest="s",
        target_set_digest="identity-audit-no-targets",
        g1_variant=G1Variant.G1_B_CORE)
    conn.commit()
    return corpus.corpus_version_id


# --------------------------------------------------------------------------- #
# §AF
# --------------------------------------------------------------------------- #
def test_af_derives_the_population_from_a_verified_target_bound_corpus(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    hints = derive_target_population(
        conn, parent_corpus_id=corpus_id, manifest_path=manifest,
        checkpoint_path=checkpoint)
    assert len(hints) == 3
    assert all(h.official_start == "2026-03-04T23:10:00Z" for h in hints)
    # S_final is the SEARCH HINT, and it is not stored on the target rows.
    columns = {r[1] for r in conn.execute(
        "PRAGMA table_info(reconstruction_corpus_targets)")}
    assert columns == {"corpus_version_id", "game_id", "created_at"}


def test_af_refuses_a_legacy_target_unbound_corpus(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    legacy = _legacy_corpus(conn)
    with pytest.raises(TargetPopulationUnavailable, match="TARGET-UNBOUND"):
        derive_target_population(conn, parent_corpus_id=legacy,
                                 manifest_path=manifest)


def test_af_refuses_without_a_precommitted_manifest(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    with pytest.raises(TargetPopulationUnavailable, match="acquisition manifest"):
        derive_target_population(conn, parent_corpus_id=corpus_id)


def test_af_refuses_a_tampered_corpus(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    conn.execute("DROP TRIGGER trg_raw_responses_no_delete")
    conn.execute("DELETE FROM raw_responses WHERE raw_response_id = 'raw_p1'")
    conn.commit()
    with pytest.raises(TargetPopulationUnavailable):
        derive_target_population(conn, parent_corpus_id=corpus_id,
                                 manifest_path=manifest,
                                 checkpoint_path=checkpoint)


def test_af_has_no_skip_when_unavailable_branch(tmp_path: Path) -> None:
    """Every unusable parent raises. A skippable gate would read as coverage."""

    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    for corpus_id in (_legacy_corpus(conn), "rcv_does_not_exist"):
        with pytest.raises(TargetPopulationUnavailable):
            derive_target_population(conn, parent_corpus_id=corpus_id,
                                     manifest_path=manifest)


def test_af_verifier_compares_a_declared_plan_against_recomputation(
        tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    members = [r[0] for r in conn.execute(
        "SELECT game_id FROM reconstruction_corpus_targets WHERE "
        "corpus_version_id = ? ORDER BY game_id", (corpus_id,))]
    # 2026-03-04T23:10:00Z - 60min = 22:10:00 -> floors to 22:10:00.
    bucket = "2026-03-04T22:10:00.000000Z"
    conn.execute(
        "INSERT INTO stage_a_plans (plan_id, plan_digest, manifest_commit_sha, "
        " manifest_content_digest, manifest_path, manifest_format_version, "
        " plan_policy_version, league_id, provider, namespace_generation, sport_key, "
        " official_source_corpus_digest, official_target_set_digest, "
        " decision_horizon_minutes, bucket_floor_seconds, acquisition_policy_version, "
        " projection_policy_version, cost_policy_version, created_at) VALUES "
        "('sap_plan1', 'pd', 'c', 'mcd', 'p.json', 'stage-a-manifest-v1', "
        " 'stage-a-plan-v1', 'lg_nba', 'the_odds_api', 'gen_a', 'basketball_nba', "
        " 'src', 'tsd', 60, 300, 'acq-v1', 'proj-v1', 'cost-v1', ?)", (NOW,))
    conn.execute("INSERT INTO stage_a_planned_buckets VALUES (?, ?, ?)",
                 ("sap_plan1", bucket, NOW))
    for game_id in members:
        conn.execute(
            "INSERT INTO stage_a_plan_targets VALUES (?, ?, ?, ?)",
            ("sap_plan1", game_id, bucket, NOW))
    conn.commit()

    failures = verify_stage_a_target_bucket_policy(
        conn, plan_id="sap_plan1", parent_corpus_id=corpus_id,
        manifest_path=manifest, checkpoint_path=checkpoint)
    assert failures == (), failures


def test_af_detects_a_target_dropped_from_the_plan(tmp_path: Path) -> None:
    """The pigeonhole case: dropping a co-bucketed target leaves the bucket
    SET byte-identical, so only keyed comparison catches it."""

    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    members = [r[0] for r in conn.execute(
        "SELECT game_id FROM reconstruction_corpus_targets WHERE "
        "corpus_version_id = ? ORDER BY game_id", (corpus_id,))]
    bucket = "2026-03-04T22:10:00.000000Z"
    conn.execute(
        "INSERT INTO stage_a_plans (plan_id, plan_digest, manifest_commit_sha, "
        " manifest_content_digest, manifest_path, manifest_format_version, "
        " plan_policy_version, league_id, provider, namespace_generation, sport_key, "
        " official_source_corpus_digest, official_target_set_digest, "
        " decision_horizon_minutes, bucket_floor_seconds, acquisition_policy_version, "
        " projection_policy_version, cost_policy_version, created_at) VALUES "
        "('sap_plan1', 'pd', 'c', 'mcd', 'p.json', 'stage-a-manifest-v1', "
        " 'stage-a-plan-v1', 'lg_nba', 'the_odds_api', 'gen_a', 'basketball_nba', "
        " 'src', 'tsd', 60, 300, 'acq-v1', 'proj-v1', 'cost-v1', ?)", (NOW,))
    conn.execute("INSERT INTO stage_a_planned_buckets VALUES (?, ?, ?)",
                 ("sap_plan1", bucket, NOW))
    for game_id in members[:-1]:  # one target silently dropped
        conn.execute("INSERT INTO stage_a_plan_targets VALUES (?, ?, ?, ?)",
                     ("sap_plan1", game_id, bucket, NOW))
    conn.commit()

    failures = verify_stage_a_target_bucket_policy(
        conn, plan_id="sap_plan1", parent_corpus_id=corpus_id,
        manifest_path=manifest, checkpoint_path=checkpoint)
    assert failures, "a dropped target must be caught even with an identical bucket set"


# --------------------------------------------------------------------------- #
# E0 gate
# --------------------------------------------------------------------------- #
def test_e0_gate_refuses_a_parent_without_a_manifest(tmp_path: Path) -> None:
    """The gate itself, tested directly.

    `enrich_corpus_with_market_lane` runs this LAST, after the v22 admission
    checks, so that a specific v22 defect still reports its own reason instead
    of being masked by a generic "not target-bound". Reaching it through the
    full enrichment path would therefore require a complete certified
    acquisition, which tests a different thing.
    """

    conn = _db(tmp_path)
    corpus_id, _, _ = _build_bound_corpus(conn, tmp_path)
    with pytest.raises(StageAProvenanceError, match="target_manifest_path"):
        _require_target_bound_parent(conn, corpus_id, manifest_path=None,
                                     checkpoint_path=None)


def test_e0_gate_refuses_a_legacy_target_unbound_parent(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _seed_league(conn)
    manifest = _write_manifest(tmp_path)
    legacy = _legacy_corpus(conn)
    with pytest.raises(StageAProvenanceError, match="not a verified target-bound"):
        _require_target_bound_parent(conn, legacy, manifest_path=manifest,
                                     checkpoint_path=None)


def test_e0_gate_accepts_a_verified_target_bound_parent(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    corpus_id, manifest, checkpoint = _build_bound_corpus(conn, tmp_path)
    _require_target_bound_parent(conn, corpus_id, manifest_path=manifest,
                                 checkpoint_path=checkpoint)  # must not raise


def test_e0_gate_does_not_depend_on_recency(tmp_path: Path) -> None:
    """A NEWER legacy corpus must not become eligible just by being latest."""

    conn = _db(tmp_path)
    _build_bound_corpus(conn, tmp_path)
    manifest = _write_manifest(tmp_path, name="m2.json")
    newer = _legacy_corpus(conn)
    latest = conn.execute(
        "SELECT corpus_version_id FROM reconstruction_corpus_versions "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0]
    assert latest == newer, "fixture must make the legacy corpus the newest"
    with pytest.raises(StageAProvenanceError, match="not a verified target-bound"):
        _require_target_bound_parent(conn, newer, manifest_path=manifest,
                                     checkpoint_path=None)


def test_enrichment_actually_invokes_the_gate() -> None:
    """A gate no consumer calls is not a gate.

    The success path through `enrich_corpus_with_market_lane` with a target-bound
    parent is exercised in `test_v22_stage_a_provenance.py`; this pins that the
    call site exists and that no bypass parameter was added.
    """

    import inspect

    from sports_quant.retrospective import stage_a_provenance

    source = inspect.getsource(stage_a_provenance.enrich_corpus_with_market_lane)
    assert "_require_target_bound_parent(" in source
    for bypass in ("skip_target", "allow_unbound", "require_target_bound=False"):
        assert bypass not in inspect.getsource(stage_a_provenance)


# --------------------------------------------------------------------------- #
# Strict PIT / leakage
# --------------------------------------------------------------------------- #
def test_target_binding_touches_no_pit_reader_surface() -> None:
    import inspect

    from sports_quant.retrospective import (
        listing_projection,
        target_binding,
        target_population,
    )
    for module in (target_binding, listing_projection, target_population):
        source = inspect.getsource(module)
        for forbidden in ("AsOfReader", "_feature_cutoff", "sportsbook_price_snapshots",
                          "historical_market_event_observations"):
            assert forbidden not in source, f"{module.__name__} touches {forbidden}"


def test_membership_derivation_reads_no_outcome_or_market_table() -> None:
    """Membership must never be reconstructed from downstream success."""

    import inspect

    from sports_quant.retrospective import listing_projection, target_population
    for module in (listing_projection, target_population):
        source = inspect.getsource(module)
        for table in ("nba_game_results", "game_result_snapshots",
                      "reconstructed_input_provenance", "identity_audit_records",
                      "static_crosswalk_provenance", "stage_a_plan_targets"):
            assert table not in source, f"{module.__name__} reads {table}"
