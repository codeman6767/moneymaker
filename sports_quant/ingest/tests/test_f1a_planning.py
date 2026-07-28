"""F1A planner + manifest determinism tests (fully offline; no network/DB)."""

from __future__ import annotations

from sports_quant.ingest.manifest import build_manifest
from sports_quant.ingest.planning import Bounds, build_plan


def test_mlb_skeleton_is_executable_single_request() -> None:
    plan = build_plan(league="mlb", from_date="2026-07-01", to_date="2026-07-30",
                      families=("schedule",), stage="skeleton", bounds=Bounds(max_retries=3))
    assert plan.executable() is True
    assert plan.semantic_requests_min() == 1
    assert plan.semantic_requests_max() == 1
    assert plan.credits_applicable is False
    assert plan.credits_max() is None
    assert plan.required_request_cap() == 4  # 1 semantic * (1 + 3 retries)


def test_mlb_rich_unbounded_is_non_executable() -> None:
    plan = build_plan(league="mlb", from_date="2026-07-01", to_date="2026-07-30",
                      families=("box", "results", "inning", "rosters"), stage="rich",
                      bounds=Bounds())  # no max_games
    assert plan.executable() is False
    assert plan.semantic_requests_max() is None
    assert any("per_game" in b or "max-games" in b for b in plan.unresolved_bounds())


def test_mlb_rich_bounded_is_executable() -> None:
    plan = build_plan(league="mlb", from_date="2026-07-01", to_date="2026-07-30",
                      families=("box", "results", "inning", "rosters"), stage="rich",
                      bounds=Bounds(max_games=10, max_retries=3))
    assert plan.executable() is True
    # schedule(1) + linescore(10) + box(10) + rosters(2*10) = 41
    assert plan.semantic_requests_max() == 41
    assert plan.required_request_cap() == 41 * 4


def test_nba_skeleton_non_executable_unknown_credit_cost() -> None:
    # NBA request fan-out is boundable (max_pages), but the BALLDONTLIE per-request
    # credit cost is UNKNOWN (no authoritative source) -> credit-capped plan is
    # non-executable and fails closed, even fully bounded.
    unbounded = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                           families=("games",), stage="skeleton", bounds=Bounds())
    assert unbounded.executable() is False

    bounded = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                         families=("games",), stage="skeleton",
                         bounds=Bounds(max_pages=5, max_retries=3))
    assert bounded.semantic_requests_max() == 5           # requests ARE boundable
    assert bounded.credits_applicable is True
    assert bounded.credits_max() is None                  # ... but credits are unknown
    assert bounded.executable() is False
    assert any("unknown_credit_cost" in b for b in bounded.unresolved_bounds())


def test_nba_rich_requests_boundable_credits_unknown() -> None:
    plan = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                      families=("box", "stats", "advanced", "plays", "lineups"), stage="rich",
                      bounds=Bounds(max_games=8, max_pages=3, max_retries=3))
    # games(3) + box(1) + stats(24) + advanced(24) + plays(24) + lineups(8) = 84
    assert plan.semantic_requests_max() == 84
    assert plan.credits_max() is None                     # unknown credit cost
    assert plan.executable() is False
    assert any("unknown_credit_cost" in b for b in plan.unresolved_bounds())


def _nba_skeleton_plan():  # type: ignore[no-untyped-def]
    return build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                      families=("games",), stage="skeleton", bounds=Bounds(max_pages=5))


def test_manifest_is_deterministic_and_hashes_stable() -> None:
    m1 = build_manifest(_nba_skeleton_plan(), scratch_db="data/pilot.db",
                        checkpoint_path="data/pilot.ckpt")
    m2 = build_manifest(_nba_skeleton_plan(), scratch_db="data/pilot.db",
                        checkpoint_path="data/pilot.ckpt")
    assert m1.canonical() == m2.canonical()  # byte-identical
    assert m1.manifest_hash() == m2.manifest_hash()
    assert m1.expected_schema_version == 16


def test_manifest_hash_changes_with_inputs() -> None:
    base = build_manifest(build_plan(
        league="nba", from_date="2026-01-05", to_date="2026-01-05", families=("games",),
        stage="skeleton", bounds=Bounds(max_pages=5)))
    changed = build_manifest(build_plan(
        league="nba", from_date="2026-01-06", to_date="2026-01-06", families=("games",),
        stage="skeleton", bounds=Bounds(max_pages=5)))
    assert base.manifest_hash() != changed.manifest_hash()


def test_manifest_carries_no_secret_material() -> None:
    m = build_manifest(build_plan(
        league="nba", from_date="2026-01-05", to_date="2026-01-05",
        families=("games", "plays"), stage="rich",
        bounds=Bounds(max_games=5, max_pages=2)), scratch_db="data/pilot.db")
    blob = m.canonical().lower()
    for token in ("api_key", "authorization", "bearer", "apikey", "secret", "token="):
        assert token not in blob
