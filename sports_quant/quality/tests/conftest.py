"""Quality-test fixtures: reuse the pit test corpus fixtures and seeders."""

from __future__ import annotations

# Re-export the pit fixtures (db_path/conn/ctx) so pytest registers them here, and
# the plain seeder functions used by the quality tests.
from sports_quant.pit.tests.conftest import (  # noqa: F401
    SCHED_START,
    T1,
    T2,
    Ctx,
    conn,
    ctx,
    db_path,
    seed_dq,
    seed_nba_ctx,
    seed_nba_result,
    seed_result,
    seed_status,
    seed_weather,
)
