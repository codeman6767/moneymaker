"""§AF -- Stage-A target → T−60 → first-pass bucket policy.

Expected buckets below are HAND-COMPUTED literals, never produced by calling the
production helper and asserting it equals itself.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.retrospective.stage_a_target_bucket import (
    BUCKET_FLOOR_SECONDS,
    DECISION_HORIZON_MINUTES,
    STAGE_A_TARGET_BUCKET_POLICY_V1,
    SUPPORTED_LEAGUES,
    TargetBucketPolicyError,
    TargetHint,
    TargetPopulationUnavailable,
    compare_to_declared,
    derive_mapping,
    derive_requested_bucket,
    derive_target_population,
    parse_canonical_instant,
    verify_stage_a_target_bucket_policy,
)


# --------------------------------------------------------------------------- #
# The algorithm, against hand-checked values
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("official_start, decision, bucket", [
    # 18:10 tip - 60m = 17:10 -> already on the 5-minute grid.
    ("2026-03-01T18:10:00.000000Z", "2026-03-01T17:10:00.000000Z",
     "2026-03-01T17:10:00.000000Z"),
    # 18:12 - 60m = 17:12 -> floors DOWN to 17:10, not up to 17:15.
    ("2026-03-01T18:12:00.000000Z", "2026-03-01T17:12:00.000000Z",
     "2026-03-01T17:10:00.000000Z"),
    # One microsecond past a boundary still floors to that boundary.
    ("2026-03-01T18:10:00.000001Z", "2026-03-01T17:10:00.000001Z",
     "2026-03-01T17:10:00.000000Z"),
    # One microsecond before the next boundary must NOT round up.
    ("2026-03-01T18:14:59.999999Z", "2026-03-01T17:14:59.999999Z",
     "2026-03-01T17:10:00.000000Z"),
    # Hour rollover: 00:30 - 60m = 23:30 the previous day.
    ("2026-03-02T00:30:00.000000Z", "2026-03-01T23:30:00.000000Z",
     "2026-03-01T23:30:00.000000Z"),
    # Midnight / date rollover with flooring.
    ("2026-03-02T00:47:13.000000Z", "2026-03-01T23:47:13.000000Z",
     "2026-03-01T23:45:00.000000Z"),
    # Month rollover.
    ("2026-04-01T00:20:00.000000Z", "2026-03-31T23:20:00.000000Z",
     "2026-03-31T23:20:00.000000Z"),
    # Year rollover.
    ("2027-01-01T00:03:00.000000Z", "2026-12-31T23:03:00.000000Z",
     "2026-12-31T23:00:00.000000Z"),
    # Leap day.
    ("2028-02-29T01:07:00.000000Z", "2028-02-29T00:07:00.000000Z",
     "2028-02-29T00:05:00.000000Z"),
])
def test_hand_checked_buckets(official_start, decision, bucket):
    assert derive_requested_bucket(official_start) == (decision, bucket)


def test_the_horizon_is_subtracted_not_added():
    _, bucket = derive_requested_bucket("2026-03-01T18:10:00.000000Z")
    assert bucket < "2026-03-01T18:10:00.000000Z"
    assert bucket == "2026-03-01T17:10:00.000000Z"


def test_subtract_then_floor_is_the_pinned_order():
    """Pinned even where the two orderings agree.

    Flooring the HINT first then subtracting would give the same answer while
    60 stays divisible by 5, so the ordering must be asserted deliberately or a
    later horizon change would silently alter results.
    """

    # 18:12:30 -> floor-first would be 18:10 - 60m = 17:10; subtract-first is
    # 17:12:30 -> 17:10. Same here, which is exactly why the next case matters.
    assert derive_requested_bucket("2026-03-01T18:12:30.000000Z")[1] == \
        "2026-03-01T17:10:00.000000Z"
    # The decision instant retains full precision BEFORE flooring.
    decision, _ = derive_requested_bucket("2026-03-01T18:12:30.000000Z")
    assert decision == "2026-03-01T17:12:30.000000Z"


def test_the_frozen_policy_parameters_are_pinned():
    """A semantic change here requires a NEW policy version."""

    assert STAGE_A_TARGET_BUCKET_POLICY_V1 == "stage-a-plan-v1"
    assert DECISION_HORIZON_MINUTES == 60
    assert BUCKET_FLOOR_SECONDS == 300
    assert SUPPORTED_LEAGUES == frozenset({"lg_nba"})


# --------------------------------------------------------------------------- #
# Strict timestamp contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "2026-03-01T18:10:00",            # naive
    "2026-03-01T18:10:00+00:00",      # offset spelling
    "2026-03-01T18:10:00z",           # lowercase z
    "2026-03-01 18:10:00Z",           # space separator
    " 2026-03-01T18:10:00Z",          # leading whitespace
    "2026-02-30T18:10:00Z",           # impossible calendar date
    "2026-03-01T24:00:00Z",           # hour 24
    "2026-03-01T18:10:00.123Z",       # non-canonical fractional width
    "",
])
def test_non_canonical_hints_are_refused(bad):
    with pytest.raises(TargetBucketPolicyError):
        parse_canonical_instant(bad, what="hint")


def test_a_non_string_hint_is_refused():
    with pytest.raises(TargetBucketPolicyError, match="must be a string"):
        parse_canonical_instant(1772000000, what="hint")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Mapping derivation, ordering, pigeonhole
# --------------------------------------------------------------------------- #
def _hints():
    return [
        TargetHint("gm_a", "2026-03-01T18:10:00.000000Z"),   # -> 17:10
        TargetHint("gm_b", "2026-03-01T18:12:00.000000Z"),   # -> 17:10 (shared)
        TargetHint("gm_c", "2026-03-01T20:40:00.000000Z"),   # -> 19:40
    ]


def test_multiple_targets_may_share_one_bucket():
    d = derive_mapping("lg_nba", _hints())
    assert d.mapping() == {
        "gm_a": "2026-03-01T17:10:00.000000Z",
        "gm_b": "2026-03-01T17:10:00.000000Z",
        "gm_c": "2026-03-01T19:40:00.000000Z"}
    assert d.distinct_buckets() == (
        "2026-03-01T17:10:00.000000Z", "2026-03-01T19:40:00.000000Z")
    assert len(d.records) == 3 and len(d.distinct_buckets()) == 2


def test_dropping_a_co_bucketed_target_leaves_the_bucket_set_identical():
    """The exact failure the mapping commitment exists to catch."""

    full = derive_mapping("lg_nba", _hints())
    dropped = {k: v for k, v in full.mapping().items() if k != "gm_a"}
    assert set(full.distinct_buckets()) == set(dropped.values()), (
        "bucket set is unchanged, so bucket comparison alone cannot see this")

    failures = compare_to_declared(full, dropped, list(full.distinct_buckets()))
    assert failures and "target population differs" in failures[0]


def test_derivation_is_independent_of_input_order():
    forward = derive_mapping("lg_nba", _hints())
    reverse = derive_mapping("lg_nba", list(reversed(_hints())))
    assert forward.records == reverse.records
    assert forward.distinct_buckets() == reverse.distinct_buckets()


def test_a_missing_hint_makes_the_target_unresolved():
    d = derive_mapping("lg_nba", [TargetHint("gm_x", "")])
    assert d.unresolved == ("gm_x",)
    assert not d.complete


def test_conflicting_hints_for_one_target_are_ambiguous():
    d = derive_mapping("lg_nba", [
        TargetHint("gm_y", "2026-03-01T18:10:00.000000Z"),
        TargetHint("gm_y", "2026-03-01T19:10:00.000000Z")])
    assert d.ambiguous == ("gm_y",)
    assert not d.complete
    assert "gm_y" not in d.mapping()


def test_duplicate_identical_evidence_resolves_to_one_record():
    d = derive_mapping("lg_nba", [
        TargetHint("gm_z", "2026-03-01T18:10:00.000000Z"),
        TargetHint("gm_z", "2026-03-01T18:10:00.000000Z")])
    assert d.complete
    assert len(d.records) == 1


def test_an_unknown_policy_version_is_refused():
    with pytest.raises(TargetBucketPolicyError, match="unknown target-bucket"):
        derive_mapping("lg_nba", _hints(), policy_version="stage-a-plan-v99")


def test_an_unsupported_league_is_refused():
    with pytest.raises(TargetBucketPolicyError, match="no reviewed"):
        derive_mapping("lg_mlb", _hints())


# --------------------------------------------------------------------------- #
# Comparison against a declared plan
# --------------------------------------------------------------------------- #
def test_a_plan_one_bucket_wrong_fails():
    d = derive_mapping("lg_nba", _hints())
    declared = dict(d.mapping())
    declared["gm_c"] = "2026-03-01T19:45:00.000000Z"        # one grid step late
    failures = compare_to_declared(
        d, declared, sorted(set(declared.values())))
    assert failures and any("does not derive" in f for f in failures)


def test_a_plan_with_an_extra_target_fails():
    d = derive_mapping("lg_nba", _hints())
    declared = dict(d.mapping())
    declared["gm_ghost"] = "2026-03-01T17:10:00.000000Z"
    assert compare_to_declared(d, declared, list(d.distinct_buckets()))


def test_a_plan_with_an_extra_or_missing_bucket_fails():
    d = derive_mapping("lg_nba", _hints())
    extra = list(d.distinct_buckets()) + ["2026-03-01T21:00:00.000000Z"]
    assert compare_to_declared(d, d.mapping(), extra)
    missing = list(d.distinct_buckets())[:1]
    assert compare_to_declared(d, d.mapping(), missing)


def test_a_wholly_absurd_but_internally_consistent_plan_fails():
    """The exact case B2 accepts and §AF must reject."""

    d = derive_mapping("lg_nba", _hints())
    absurd = {g: "2029-12-31T23:55:00.000000Z" for g in d.mapping()}
    failures = compare_to_declared(d, absurd, ["2029-12-31T23:55:00.000000Z"])
    assert failures


def test_a_correct_plan_produces_no_failures():
    d = derive_mapping("lg_nba", _hints())
    assert compare_to_declared(d, d.mapping(), list(d.distinct_buckets())) == ()


def test_an_unresolved_target_blocks_a_complete_plan():
    d = derive_mapping("lg_nba", _hints() + [TargetHint("gm_none", "")])
    failures = compare_to_declared(d, d.mapping(), list(d.distinct_buckets()))
    assert any("no admitted official start" in f for f in failures)


# --------------------------------------------------------------------------- #
# The provider snapshot grid must never reach a bucket
# --------------------------------------------------------------------------- #
def test_the_provider_off_grid_snapshot_cannot_influence_a_bucket():
    """The provider answers at roughly `:37`; the request grid is exact.

    A bucket is derived only from the official hint, so no `:37` value can enter
    it. Asserted by construction: every derived bucket lands on a 300-second
    boundary with zero microseconds.
    """

    d = derive_mapping("lg_nba", _hints())
    for record in d.records:
        assert record.requested_at_bucket.endswith(":00.000000Z")
        minute = int(record.requested_at_bucket[14:16])
        assert minute % 5 == 0


# --------------------------------------------------------------------------- #
# RETAINED BLOCKER -- the target population is not derivable at v22
# --------------------------------------------------------------------------- #
def test_the_parent_corpus_binds_membership_but_still_carries_no_scope(
        conn: sqlite3.Connection):
    """v23 closed the blocker with MEMBERSHIP, deliberately not with a scope.

    Replaces the v22-era assertion that nothing binds a corpus to its targets.
    A scope predicate was rejected by the architecture's portability
    reproduction, so the corpus row must still carry no season or date range;
    the corpus→games link is now an explicit membership table rather than the
    plan's own claim.
    """

    columns = {r[1] for r in conn.execute(
        "PRAGMA table_info(reconstruction_corpus_versions)")}
    for absent in ("season_id", "start_date", "end_date", "target_scope"):
        assert absent not in columns
    linking = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND sql LIKE '%REFERENCES games%' AND sql LIKE '%corpus%'")}
    assert linking == {"reconstruction_corpus_targets"}


def test_target_set_digest_has_no_production_derivation():
    """It is a caller-supplied label, defaulted to a literal by the runner."""

    import inspect

    from sports_quant.retrospective import runner

    source = inspect.getsource(runner)
    assert 'target_set_digest: str = "identity-audit-no-targets"' in source


def test_target_population_derivation_refuses_without_a_manifest(
        conn: sqlite3.Connection):
    """v23 replacement: the refusal is now about PROVENANCE, not absence.

    Without the precommitted acquisition manifest the bound run set would be
    caller-selected and a required run could be omitted undetectably, so
    derivation still fails closed.
    """

    with pytest.raises(TargetPopulationUnavailable, match="acquisition manifest"):
        derive_target_population(conn, parent_corpus_id="rcv_anything")


def test_the_load_bearing_verifier_refuses_an_unknown_parent(
        conn: sqlite3.Connection):
    """§AF is composed at v23; a real verification result is asserted in
    `test_v23_af_and_e0_seams.py`. Here the seam must still fail closed on a
    parent that does not exist."""

    with pytest.raises(TargetPopulationUnavailable, match="does not exist"):
        verify_stage_a_target_bucket_policy(
            conn, plan_id="sap_x", parent_corpus_id="rcv_x")


def test_no_provider_authority_or_identity_work_is_performed():
    """§AF decides WHEN to request, never WHICH provider event is a game."""

    import inspect

    from sports_quant.retrospective import stage_a_target_bucket

    source = inspect.getsource(stage_a_target_bucket)
    for forbidden in ("provider_event_id", "home_team", "away_team",
                      "crosswalk", "ATTESTED_GENERATIONS", "httpx"):
        assert forbidden not in source
