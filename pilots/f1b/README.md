# F1B pilot manifests — skeleton (executed & reviewed) + rich (prepared only)

Canonical, secret-free, deterministic pilot manifests produced offline by
`--plan --manifest-out` (zero network, zero database writes).

| Stage | Status |
|---|---|
| **Skeleton** (`*_skeleton.manifest.json`) | **executed live and independently reviewed** |
| **Rich** (`*_rich.manifest.json`) | **prepared and validated offline — NOT executed** |

**Both have since been executed as live skeleton pilots and independently
reviewed** (see `F1B_SKELETON_PILOT_REPORT.md`): MLB on 2026-07-29 (1 request,
30 games received → 2 selected) and NBA on 2026-07-29 (1 request, 8 games
received → 2 selected). Each completed-resume verification made **zero**
additional provider requests. Schema remains v16. The manifests below are
unchanged (byte-identical) by that execution and by the review.

Executing a manifest requires the separate, reviewed authorization boundary
(`MONEYMAKER_F1B_AUTHORIZED=1`), which is **off** by default and is set only for
the single authorized process.

The **F1B rich-data pilot has NOT been executed and remains unauthorized.** Its two
manifests are now prepared and validated offline (see *Rich manifests* below), but
**each future execution requires separate explicit user authorization** and an
approved per-run request budget. Nothing here authorizes it. MLB and NBA must be
executed and reviewed **separately**.

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

## Rich manifests (PREPARED, validated offline, NOT executed)

| File | League | Provider | Range | Families (skeleton family auto-added) | `max_games` | `max_pages` | `max_records` | `max_retries` | Semantic max | **Request cap** | Rate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `mlb_rich.manifest.json` | MLB | MLB StatsAPI (keyless) | 2026-07-20..2026-07-21 | `schedule` + `results`, `box`, `inning`, `rosters` | 1 | — | — | 1 | 6 | **12** | n/a |
| `nba_rich.manifest.json` | NBA | BALLDONTLIE GOAT | 2026-01-05 | `games` + `box`, `stats`, `advanced`, `plays`, `lineups`, `quarters` | 1 | 1 | 100 | 1 | 7 | **14** | 60/min ≤ 600/min |

| | `manifest_hash` | `plan_hash` | Scratch database | Checkpoint |
|---|---|---|---|---|
| MLB rich | `f56b5c5da53d86c9…` | `73e887229ce20b8c…` | `data/f1b_mlb_rich_scratch.db` | `data/f1b_mlb_rich.ckpt` |
| NBA rich | `9de5d312b99c3e85…` | `1c896ae16a13c10b…` | `data/f1b_nba_rich_scratch.db` | `data/f1b_nba_rich.ckpt` |

**Caps are planner-derived, never hand-written**: `cap = semantic_max × (1 + max_retries)`
= 6 × 2 = **12** (MLB) and 7 × 2 = **14** (NBA). The planner models every request the
ingestor makes:

* **MLB (6)** — range schedule discovery, per-game schedule re-fetch, **one** linescore
  shared by `results`+`inning`, one box score, and up to **two** team/date rosters.
* **NBA (7)** — one games listing page, per-game `game` re-fetch, **one** box score
  shared by `box`+`quarters`, and one page each of `stats`, `advanced`, `plays`, `lineups`.

A zero-network differential (real orchestration + real client request construction over
mocked transports) confirms the executor attempts **exactly** the semantic maximum and
never exceeds it: MLB 6/6, NBA 7/7, both far inside their retry-inclusive caps.
`max_games=1` provably prevents any second-game request and `max_pages=1` any
second page. Rich families are discovery-plus-selected-game only — **no** injuries,
odds, weather, or Kalshi families.

These retrospective pilots will **not** create a strict historical point-in-time
corpus: both windows are completed past dates, so every observation carries the
**current receipt time**. They measure capability, coverage, request fan-out,
correction behaviour, and persistence only.

---

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
