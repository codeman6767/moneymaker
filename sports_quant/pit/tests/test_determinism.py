"""Phase E1 determinism tests (task §10)."""

from __future__ import annotations

import random
import sqlite3

from sports_quant.pit import AsOfReader, Cutoff, deterministic_json, latest_as_of

from .conftest import CUTOFF, Ctx, seed_team_stat

# Distinct observed_at -> runs. The last value is post-cutoff (invisible).
_SERIES = {
    "2026-07-05T00:00:00.000000Z": 1,
    "2026-07-08T00:00:00.000000Z": 2,
    "2026-07-11T00:00:00.000000Z": 3,   # the expected as-of winner (max <= cutoff)
    "2026-07-20T00:00:00.000000Z": 9,   # future
}
_EXPECTED_RUNS = 3


def test_randomized_insertion_order_is_stable(conn: sqlite3.Connection, ctx: Ctx) -> None:
    """Over 100 shuffled insertion orders, the as-of selection is identical. Each
    run uses a distinct provider_team_id anchor so append-only rows do not collide
    across runs (they cannot be deleted), isolating the effect of insertion order."""

    cutoff = Cutoff.parse(CUTOFF)
    results: set[int] = set()
    for i in range(100):
        anchor = f"P{i}"
        items = list(_SERIES.items())
        random.Random(i).shuffle(items)
        for observed_at, runs in items:
            seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                           observed_at=observed_at, runs=runs, provider_team_id=anchor)
        row = latest_as_of(conn, table="team_game_statistics", cutoff=cutoff,
                           anchor_where="game_ref_id = ? AND provider_team_id = ?",
                           anchor_params=(ctx.game_ref_id, anchor))
        assert row is not None
        results.add(int(row["runs"]))
    assert results == {_EXPECTED_RUNS}  # one stable answer across all 100 orders


def test_inner_aggregate_future_row_trap(conn: sqlite3.Connection, ctx: Ctx) -> None:
    """The classic bug: MAX(observed_at) taken BEFORE filtering to the cutoff would
    return a future row (or NULL). The canonical algorithm filters first, so a
    future observation never yields a future value nor hides the correct prior."""

    for observed_at, runs in _SERIES.items():
        seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                       observed_at=observed_at, runs=runs)
    obs = AsOfReader(conn, Cutoff.parse(CUTOFF)).team_game_statistics(
        ctx.game_ref_id, ctx.home_team_id)
    assert obs is not None and obs.get("runs") == _EXPECTED_RUNS  # not 9 (future), not None


def test_serialized_output_is_byte_identical(conn: sqlite3.Connection, ctx: Ctx) -> None:
    for observed_at, runs in _SERIES.items():
        seed_team_stat(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                       observed_at=observed_at, runs=runs)
    reader = AsOfReader(conn, Cutoff.parse(CUTOFF))
    a = reader.team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    b = reader.team_game_statistics(ctx.game_ref_id, ctx.home_team_id)
    assert a is not None and b is not None
    # Key order in the frozen mapping never affects the serialization.
    assert deterministic_json(a) == deterministic_json(b)
    assert deterministic_json([a]) == deterministic_json([b])


def test_deterministic_json_key_order_independent() -> None:
    from types import MappingProxyType

    from sports_quant.pit.models import Observation
    o1 = Observation("t", "2026-07-05T00:00:00.000000Z", "id1",
                     MappingProxyType({"b": 2, "a": 1}))
    o2 = Observation("t", "2026-07-05T00:00:00.000000Z", "id1",
                     MappingProxyType({"a": 1, "b": 2}))
    assert deterministic_json(o1) == deterministic_json(o2)
