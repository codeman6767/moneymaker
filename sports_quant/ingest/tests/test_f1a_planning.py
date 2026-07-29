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
    # skeleton schedule(1) + per-game single-invocation fan-out over 10 games:
    # game_schedule(10) + linescore(10) + box(10) + rosters(2*10) = 50; +1 = 51
    assert plan.semantic_requests_max() == 51
    assert plan.required_request_cap() == 51 * 4


def test_nba_skeleton_executable_when_bounded_credits_na() -> None:
    # BALLDONTLIE is request-RATE limited, not credit metered -> credits N/A. An
    # UNBOUNDED skeleton (no --max-pages) is still non-executable because the games
    # list fan-out is unbounded; a fully bounded skeleton IS executable, and NO
    # credit figures are ever fabricated.
    unbounded = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                           families=("games",), stage="skeleton", bounds=Bounds())
    assert unbounded.executable() is False
    assert unbounded.credits_applicable is False
    assert unbounded.credits_max() is None

    bounded = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                         families=("games",), stage="skeleton",
                         bounds=Bounds(max_pages=5, max_retries=3))
    assert bounded.semantic_requests_max() == 5           # requests ARE boundable
    assert bounded.credits_applicable is False            # credits NOT applicable
    assert bounded.credits_min() is None                  # ... and never fabricated
    assert bounded.credits_max() is None
    assert bounded.executable() is True                   # bounded requests -> executable
    assert bounded.unresolved_bounds() == ()


def test_nba_rich_requests_boundable_executable_credits_na() -> None:
    plan = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                      families=("box", "stats", "advanced", "plays", "lineups"), stage="rich",
                      bounds=Bounds(max_games=8, max_pages=3, max_retries=3))
    # games pages(3) + per-game single-invocation over 8 games:
    # game(8) + box(8) + stats(24) + advanced(24) + plays(24) + lineups(8) = 96; +3 = 99
    assert plan.semantic_requests_max() == 99
    assert plan.credits_applicable is False               # request-rate limited, not credits
    assert plan.credits_max() is None                     # never fabricated
    assert plan.executable() is True                      # fully bounded -> executable
    assert plan.unresolved_bounds() == ()


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
