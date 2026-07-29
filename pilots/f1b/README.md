# F1B skeleton pilot manifests (generated — NOT YET EXECUTED)

These are canonical, secret-free, deterministic **skeleton** pilot manifests
produced offline by `--plan --manifest-out` (zero network, zero database writes).
They are reviewed plan documents only. **No live pilot has run against them.**

The live F1B pilot remains **NOT authorized and unexecuted**. Executing a manifest
requires the separate, reviewed authorization boundary
(`MONEYMAKER_F1B_AUTHORIZED=1`), which is **off** and is not set by this task.

## Manifests

| File | League | Provider | Range | Stage | Families | Executable | Request cap | Rate |
|------|--------|----------|-------|-------|----------|------------|-------------|------|
| `mlb_skeleton.manifest.json` | MLB | MLB StatsAPI (keyless) | 2026-07-20..2026-07-21 (completed) | skeleton | `schedule` | yes | 4 | n/a (not rate-metered) |
| `nba_skeleton.manifest.json` | NBA | BALLDONTLIE GOAT | 2026-01-05 (completed regular season) | skeleton | `games` | yes | 8 | 60/min (configured) ≤ 600/min (GOAT tier max) |

Both are **skeleton** stage — schedule/game **discovery only**. No rich families
(box/stats/advanced/plays/lineups/rosters/inning). Credits are **not applicable**
for either provider (MLB is keyless; BALLDONTLIE is request-**rate** limited, not
credit metered) and are never fabricated.

## How to execute (only after separate F1B authorization)

1. `db-init --db <scratch_db>` (schema v16; the recorded scratch DB is git-ignored).
2. Review the manifest and its `manifest_hash` / `plan_hash`.
3. With authorization in force, run the guarded pilot:
   `ingest-<league> --pilot --manifest <file> --scratch-db <scratch_db> --checkpoint <ckpt>`.

Execution is manifest-governed: any policy/planner drift, tamper, non-canonical
encoding, schema mismatch, or a request cap below the plan's conservative maximum
fails closed before any client or database work.
