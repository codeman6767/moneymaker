# F1B skeleton pilot manifests (EXECUTED and independently reviewed)

These are canonical, secret-free, deterministic **skeleton** pilot manifests
produced offline by `--plan --manifest-out` (zero network, zero database writes).

**Both have since been executed as live skeleton pilots and independently
reviewed** (see `F1B_SKELETON_PILOT_REPORT.md`): MLB on 2026-07-29 (1 request,
30 games received → 2 selected) and NBA on 2026-07-29 (1 request, 8 games
received → 2 selected). Each completed-resume verification made **zero**
additional provider requests. Schema remains v16. The manifests below are
unchanged (byte-identical) by that execution and by the review.

Executing a manifest requires the separate, reviewed authorization boundary
(`MONEYMAKER_F1B_AUTHORIZED=1`), which is **off** by default and is set only for
the single authorized process.

The **F1B rich-data pilot has NOT started and remains unauthorized**: it needs a
separately reviewed manifest (rich families) and an approved per-run request
budget. Nothing here authorizes it.

## Manifests

| File | League | Provider | Range | Stage | Families | Executable | Request cap | Rate |
|------|--------|----------|-------|-------|----------|------------|-------------|------|
| `mlb_skeleton.manifest.json` | MLB | MLB StatsAPI (keyless) | 2026-07-20..2026-07-21 (completed) | skeleton | `schedule` | yes | 4 | n/a (not rate-metered) |
| `nba_skeleton.manifest.json` | NBA | BALLDONTLIE GOAT | 2026-01-05 (completed regular season) | skeleton | `games` | yes | 8 | 60/min (configured) ≤ 600/min (GOAT tier max) |

Both are **skeleton** stage — schedule/game **discovery only**. No rich families
(box/stats/advanced/plays/lineups/rosters/inning). Credits are **not applicable**
for either provider (MLB is keyless; BALLDONTLIE is request-**rate** limited, not
credit metered) and are never fabricated.

## Executed results (independently reviewed)

| Manifest | `manifest_hash` | Requests | Pages | Games received → selected (excluded) | Resume |
|---|---|---|---|---|---|
| `mlb_skeleton.manifest.json` | `fa28695b…` | 1 of cap 4 | 1 | 30 → 2 (28) | 0 additional requests |
| `nba_skeleton.manifest.json` | `6fe6dc37…` | 1 of cap 8 | 1 | 8 → 2 (6) | 0 additional requests |

Selected games are the deterministic canonical first two — MLB by
`(officialDate, gamePk)`, NBA by `(date_local, game_id)` — never provider response
order. No HTTP 429 and no throttle wait occurred on either run.

## How to execute (requires separate F1B authorization)

1. `db-init --db <scratch_db>` (schema v16; the recorded scratch DB is git-ignored).
2. Review the manifest and its `manifest_hash` / `plan_hash`.
3. With authorization in force, run the guarded pilot:
   `ingest-<league> --pilot --manifest <file> --scratch-db <scratch_db> --checkpoint <ckpt>`.

A re-run against an already-populated scratch database is refused (`UNSAFE`:
"non-empty database not authorized by a matching resume checkpoint"); only an
explicit `--resume` is permitted, and it performs zero provider requests.

Execution is manifest-governed: any policy/planner drift, tamper, non-canonical
encoding, schema mismatch, or a request cap below the plan's conservative maximum
fails closed before any client or database work.
