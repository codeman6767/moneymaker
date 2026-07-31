"""Regenerate the two canonical F1 season-month coverage manifests, offline.

Run from the repository root:

    python pilots/f1/generate_manifests.py

This is the single source of truth for the committed manifests' semantic inputs.
It makes no network request, touches no database, and writes only the two manifest
files. `test_f1_month_manifests.py` asserts that regeneration is byte-identical,
so a drift between this script and the committed bytes fails CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sports_quant.ingest.f1a import emit_plan  # noqa: E402

HERE = Path(__file__).resolve().parent

#: Both pilots record e017 provider identity observations during ingestion, so
#: they genuinely require schema v17 and declare it (the F1B manifests stay at 16).
SCHEMA_VERSION = 17

#: Semantic inputs, one dict per pilot. Every value here is part of the manifest
#: hash: change any one and the hash changes, which is the contract §6 requires.
MLB_MONTH: dict[str, object] = {
    "league": "mlb",
    "from_date": "2026-06-01",
    "to_date": "2026-06-30",
    # `schedule` is the planner's fixed unit and is added to families automatically.
    "includes": ("results", "box", "inning", "rosters"),
    # A June with every team playing every day is 15 games/day * 30 = 450; add
    # doubleheaders and an ordinary month still lands far below 600. Finite and
    # fail-closed, but high enough that a normal June is never truncated.
    "max_games": 600,
    # One retry per semantic request. Every attempt is counted against the cap.
    "max_retries": 1,
    # PROJECT COURTESY pacing, not a provider limit: MLB StatsAPI is keyless
    # and publishes no ceiling we can verify. 30/min with burst 1 (see
    # cost_policies.build_mlb_rate_policy, mlb-pacing-v1) means one request
    # immediately and then one every two seconds. The rate is part of the plan
    # identity, so changing it changes both the plan and manifest hashes; it
    # does NOT change request counts, so the semantic maximum stays 3001 and
    # the retry-inclusive cap stays 6002.
    "rate_per_min": 30,
    "scratch_db": "data\\f1_mlb_2026_06_scratch.db",
    "checkpoint": "data\\f1_mlb_2026_06.ckpt",
    "manifest_out": HERE / "mlb_coverage_2026_06.manifest.json",
}

NBA_MONTH: dict[str, object] = {
    "league": "nba",
    "from_date": "2026-03-01",
    "to_date": "2026-03-31",
    "includes": ("box", "quarters", "stats", "advanced", "plays", "lineups"),
    # An NBA March is ~200-280 games; 400 leaves real headroom without being
    # unbounded.
    "max_games": 400,
    # Every paginated family fetches 100 records per page. The binding case is
    # plays: a normal game has ~430-480 and a multi-overtime game can approach
    # 600, so 8 pages (800 records) keeps a full game's plays intact with margin.
    # The month games listing needs only ~3 pages at 100/page.
    "max_pages": 8,
    # Per-`_paginate`-call record ceiling. Above the worst single-call volume
    # (plays ~600, games listing ~280) so `max_pages` is the binding bound and
    # neither silently drops a record.
    "max_records": 1000,
    "max_retries": 1,
    # Configured well below the GOAT tier maximum of 600/min.
    "rate_per_min": 60,
    "scratch_db": "data\\f1_nba_2026_03_scratch.db",
    "checkpoint": "data\\f1_nba_2026_03.ckpt",
    "manifest_out": HERE / "nba_coverage_2026_03.manifest.json",
}

PILOTS = (MLB_MONTH, NBA_MONTH)


def generate(*, out_dir: Path | None = None, emit: object = None) -> list[Path]:
    """Write both manifests; return the paths written."""

    written: list[Path] = []
    for spec in PILOTS:
        kwargs = dict(spec)
        target = Path(kwargs.pop("manifest_out"))  # type: ignore[arg-type]
        if out_dir is not None:
            target = out_dir / target.name
        rc = emit_plan(  # type: ignore[misc]
            expected_schema_version=SCHEMA_VERSION,
            manifest_out=target,
            out=lambda _s: None,
            **kwargs,  # type: ignore[arg-type]
        )
        if rc != 0:
            raise SystemExit(f"planning failed for {target.name} (exit {rc})")
        written.append(target)
    return written


if __name__ == "__main__":
    for path in generate():
        print(f"wrote {path}")
