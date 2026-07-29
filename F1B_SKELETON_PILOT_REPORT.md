# F1B Skeleton Pilot Report (redacted, independently reviewed)

**Scope:** the two committed **skeleton** (discovery-only) F1B pilots for MLB and
NBA, their live execution, and the independent correctness review of the preserved
evidence. Reviewed at commit `3b8cfdd`; schema **v16** throughout.

This report is redacted by construction: it contains no secret, no API key, no
authentication header, no response body, and no database or checkpoint content.
Those artifacts remain git-ignored under `data/` for local review only.

---

## 1. Manifests

Both committed manifests were validated through the canonical validator
(`load_and_validate`) and were **byte-identical before and after** execution and
review. Each is exactly the canonical serialization of its own parsed content (the
canonical bytes *are* the integrity check), and duplicate JSON keys are rejected.

| | MLB | NBA |
|---|---|---|
| File | `pilots/f1b/mlb_skeleton.manifest.json` | `pilots/f1b/nba_skeleton.manifest.json` |
| `manifest_hash` | `fa28695b043eb38da3de13c1a49dd24adef022d83f40d870e495968351c4cf3b` | `6fe6dc37ec4d5868c7f456ba231d4b8c0f6edbda940fdba6f3f41acbb4b1f446` |
| `plan_hash` | `225e56f4bfbac628885ccf51650194d4642e4b74a20f3e10ed91048ca0498346` | `7d16acb78379ace65c07968f9a37191993ce9ecb26332d6613bd554c7063a8c9` |
| Provider / league | `mlb_statsapi` / `mlb` (keyless) | `balldontlie` / `nba` |
| Stage / families | `skeleton` / `["schedule"]` | `skeleton` / `["games"]` |
| Date range | `2026-07-20..2026-07-21` | `2026-01-05` (single day) |
| Bounds | `max_games=2`, `max_retries=3` | `max_games=2`, `max_pages=2`, `max_retries=3` |
| Request cap | **4** | **8** |
| Rate policy | n/a (not rate-metered) | configured **60/min** ≤ verified **600/min** (GOAT) |
| Credits | **not applicable** (`credit_cap` null) | **not applicable** (`credit_cap` null) |
| Versions | `f1a-manifest-v1` / `f1a-plan-v1` / `mlb-cost-v1` | `f1a-manifest-v1` / `f1a-plan-v1` / `bdl-cost-v1` |
| Schema | 16 | 16 |

Rebuilding each plan offline from the manifest's own fields reproduced the recorded
`plan_hash` exactly, and the NBA single-day range parses to the inclusive pair
`(2026-01-05, 2026-01-05)`. Neither manifest contains any secret-shaped field.

---

## 2. Execution results

One live GET per pilot. Both ran under the process-scoped
`MONEYMAKER_F1B_AUTHORIZED=1` boundary, which is off by default and was never
written to `.env`, a shell profile, a repository file, or system settings.

| Metric | MLB | NBA |
|---|---|---|
| Requests attempted / cap | **1 / 4** | **1 / 8** |
| Successful responses | 1 (HTTP 200) | 1 (HTTP 200) |
| Listing pages fetched | **1** | **1** |
| Failed responses · retries | 0 · 0 | 0 · 0 |
| Blocked requests | 0 | 0 |
| HTTP 429 | **0** | **0** |
| Throttle events · wait | 0 · **0.000 s** | 0 · **0.000 s** |
| Sanitized endpoint | `/schedule` | `/v1/games` |
| Exit code | 0 | 0 |
| Checkpoint state | `completed` | `completed` |

### Game selection (`max_games=2`)

| | Received | Selected | Excluded by `max_games` | `selection_truncated` |
|---|---|---|---|---|
| MLB | **30** | **2** | **28** | true |
| NBA | **8** | **2** | **6** | true |

Selection is deterministic and canonical, **not** provider response order:

* MLB orders by `(officialDate, gamePk)`. The provider returned `824410, 823522`
  first, yet the pilot selected `822788, 822874` — independently recomputed from the
  persisted response body. This is direct evidence the canonical ordering was applied.
* NBA orders by `(date_local, game_id)`, selecting `18447316, 18447317`. (For this
  response the provider order happened to coincide, so NBA alone does not
  discriminate ordering; MLB does.)

This is a **planned bound**, not a failure and not budget truncation: `truncated`,
`budget_exhausted`, `families_truncated`, and `blocked_requests` were all empty/false
for both runs.

### Persisted rows

| Table | MLB | NBA |
|---|---|---|
| `raw_responses` | 1 | 1 |
| `game_schedule_snapshots` | 2 | 2 |
| `provider_game_references` | 2 | 2 |
| `provider_team_references` | 4 | 4 |
| `provider_capabilities` | 0 | 9 (declared) |
| `ingestion_runs` | 1 (`succeeded`, `requests_made=1`) | 1 (`succeeded`, `requests_made=1`) |
| `data_quality_issues` | 0 | 2 (`note`) |
| **Total rows** | **399** | **410** |
| Tables present | 45 | 45 |

Both databases passed `PRAGMA integrity_check`, are schema **v16** (16 migration rows),
and had an **empty WAL**, so all reviewed content is committed and visible read-only.
No append-only duplicates exist (raw responses unique by `content_hash`, snapshots
unique by `content_hash`, references unique by provider id).

**No rich-data table was populated in either database** — verified across
`nba_game_results`, `nba_player_statistics`, `nba_team_statistics`,
`nba_quarter_lines`, `play_snapshots`, `lineup_snapshots`, `lineup_players`,
`player_game_statistics`, `team_game_statistics`, `injury_snapshots`,
`roster_snapshots`, `mlb_inning_lines`, `game_result_snapshots`,
`probable_pitcher_snapshots`, `weather_snapshots`, `sportsbook_price_snapshots`,
`kalshi_orderbook_snapshots`, `kalshi_public_trades`, `players`, `venues`, `games`,
and `entity_match_decisions` (all zero).

---

## 3. Completed-resume verification

Each pilot was resumed explicitly against the same manifest, scratch database, and
checkpoint.

* **Zero additional transport calls** (`transport_starts=0`, `network_occurred=false`).
* **Zero additional authentication requests**, **zero additional request usage**
  (`prior_requests=1`, cumulative `attempted_requests=1`), **zero additional pages**,
  **zero additional throttle wait**.
* **Zero database mutation**; scratch database file SHA-256 unchanged.
* No duplicate raw responses, observations, or provider references; row counts and
  per-table content digests byte-identical; no second `ingestion_runs` row.
* Checkpoint remained `completed`; the report state is `resumed_completed`.

A re-run *without* `--resume` against a populated scratch database is correctly
refused as `UNSAFE` ("non-empty database not authorized by a matching resume
checkpoint"), which prevents duplicate ingestion by design.

---

## 4. Data-quality summary

* **MLB** — 0 findings. `data-quality` grade **A**, score 1.00, `corpus_valid=true`,
  `execution_valid=true`.
* **NBA** — 2 `note`-severity `DQ-CAP-NBA-001` capability-honesty records stating that
  `confirmed_pregame_starters` (unavailable at tier) and `correction_timestamps`
  (unsupported at tier) produced **no fabricated rows**. Grade **A**, score 1.00,
  `corpus_valid=true`, `execution_valid=true`, 0 blocking, 0 issue.

Both runs left `canonical_games=0` with unresolved provider references (MLB and NBA
each: 2 game, 4 team). That is expected and correct for discovery-only skeleton
stage — entity matching is a later, separate step and was not performed.

---

## 5. Isolation

* **Development corpus (`data/corpus.db`) unchanged by both pilots** — byte-identical
  SHA-256, and every one of its rows predates the pilots (latest raw response
  `2026-07-25T07:36:45Z`, latest ingestion run `2026-07-25T07:36:46Z`, versus pilots
  on 2026-07-29). It holds no schedule snapshot from either pilot.
* **MLB artifacts unchanged by the NBA execution** — MLB database and checkpoint
  SHA-256 identical before and after.
* **Each scratch database is single-provider** — the MLB database contains zero
  BALLDONTLIE rows and the NBA database zero MLB rows.
* **Each checkpoint is bound to exactly one manifest** — matching `manifest_hash`,
  league, provider, and its manifest's `scratch_db`; the two hashes differ, so no
  cross-manifest substitution occurred.
* **No aliasing** — all five artifacts are regular files (no symlinks, no
  directories), each resolving to a unique path and a unique inode.
* No unrelated database was modified.

---

## 6. Secret and artifact audit — PASS

Scanned both scratch databases, both checkpoints, raw-response metadata,
ingestion-run records, and all captured JSON/human outputs for the configured key
value, `Authorization`, `x-api-key`, secret-bearing URLs/query parameters, and
environment dumps.

* The configured key is **absent** from every artifact.
* Persisted endpoints are **sanitized paths** (`/schedule`, `/v1/games`) with no query
  string and no URL.
* Persisted response headers are allow-listed only (MLB: `cache-control`,
  `content-type`, `date`; NBA: `content-type`, `date`, `etag`) — no authentication
  header is stored.
* The only matches for `authorization` and `?apiKey=` are **schema DDL comments**
  (one each, fully accounted for inside `sqlite_master.sql`) that document that such
  values are never stored. These are column documentation, not persisted secrets.
* Nothing under `data/`, no `.env`, key, database, checkpoint, report, temporary
  manifest, raw response, or Graphify output is staged.

---

## 7. Capability limitations (what the skeleton does and does not prove)

Authentication and tier are **separate claims** and are now reported separately:

* **Authentication — proven.** The NBA request returned HTTP 200 with the configured
  credential, so authentication succeeded. MLB StatsAPI is keyless, so authentication
  is **not applicable** there.
* **Tier — NOT proven by the skeleton.** `/v1/games` is available below GOAT, so a 200
  from it can never establish the subscribed tier. The 9 `provider_capabilities` rows
  the NBA skeleton wrote are **declared** states (`is_observed=0`) for the configured
  tier — declarations, not observations, and therefore not tier evidence.
* **Tier evidence source.** GOAT reachability was established only by the separate
  bounded capability audit of 2026-07-28 (`authenticated=true`,
  `tier_restricted=false`, GOAT-only rich endpoints returning 200), recorded in its
  own git-ignored audit database — not by these pilots.

Also unproven by the skeleton: historical depth/coverage, correction behaviour, and
any rich-family availability at scale.

---

## 8. MLB probable-pitcher hydration — classification

The MLB schedule request included `hydrate=probablePitcher`, and probable-pitcher IDs
appeared inline on the two schedule snapshot rows.

**Decision: allowed within the `schedule` family (classification 1).** Rationale:

* `hydrate` is a response-**shaping** parameter on the same `/schedule` endpoint, not
  a different endpoint or family. It consumed **no additional request** and no
  additional budget (1 request total).
* The hydrated values persist only as two inline columns on the schedule snapshot.
  **`probable_pitcher_snapshots` remained 0**, as did `players` and `roster_snapshots`
  — no rich-family observation was created.
* The reviewed contract bounds *endpoint families and request counts*; this stays
  inside both bounds.

**Future policy note (recorded, not retrofitted):** suppressing hydration would change
the real planned request shape and therefore the planner identity and manifest hash.
The committed manifests are deliberately **not** retrofitted. Any future strict-PIT
skeleton that must suppress hydration requires a **new cost/plan policy version**
(and regenerated, re-reviewed manifests). The selected policy is pinned by
`test_probable_pitcher_hydration_stays_within_the_schedule_family`.

---

## 9. Reporting defects found and repaired

The pilots executed correctly; their **reports** were misleading. Five defects were
found and repaired with the smallest principled changes. Runtime behaviour, request
counts, and all preserved evidence are unchanged.

| | Defect | Repair |
|---|---|---|
| A | `rate_limited=true` merely because a rate policy was attached (with 0 × 429 and 0 s wait) | `rate_policy_active` added for "a policy is enforcing"; `rate_limited` now true only on an actual wait, block, or provider 429; `throttle_events` added |
| B | `max_games` selection invisible — 28/6 games dropped while `truncated=false` | `games_received`, `games_selected`, `games_excluded_by_max_games`, `selection_truncated` added, kept strictly separate from `truncated` / `budget_exhausted` / `blocked_requests` |
| C | `pages_fetched=0` despite one successful listing page (page index 0 never counted) | pages now counted on **success**, keyed by page identity: page 0 counts, failed transports do not, a retried page counts once, distinct pagination pages count deterministically, resume adds zero |
| D | Authentication/tier unclear; risk of claiming GOAT from a games-only 200 | `authentication_succeeded`, `authentication_status`, `tier_status`, `tier_verified`, `tier_evidence_source` added; only a `bounded_capability_audit` may set `tier_verified`; MLB reports auth as not applicable |
| E | A completed resume rewrote the checkpoint's usage, zeroing the first run's transport counters (`network_occurred=false` for network-fetched data) | `prior_transport_starts` / `prior_pages_fetched` carried across resume, with `total_transport_starts` / `total_pages_fetched` for the logical run |

Defect E was found during this review (not previously reported). It caused no data
loss: durable proof of transport lives in the database (`raw_responses` HTTP 200 and
`ingestion_runs.requests_made=1`), and the cumulative request budget was always
carried correctly (`attempted_requests=1`, `prior_requests=1`).

**Documented limitation — legacy checkpoints.** The two preserved checkpoints were
written by the pre-repair code and therefore never stored the new fields
(`games_received`, `prior_transport_starts`, `prior_pages_fetched`, …). Resuming such
a checkpoint reports those fields as **zero rather than fabricating values**; the
repair applies to checkpoints written from now on. This was verified explicitly in
the non-editable wheel smoke (resume of the preserved NBA checkpoint) and the
forward-looking behaviour is pinned by
`test_completed_resume_adds_zero_requests_pages_selections_or_mutations`. The
preserved evidence is deliberately **not** rewritten.

The human-readable pilot output now prints `pages`, `selection`, `budget`, `rate`, and
`auth` as separate labelled lines so budget truncation can never be confused with
selection truncation.

---

## 10. Statements of record

* This work was **discovery-only**: schedule/games listing per league, one request
  each, no rich families requested or persisted.
* **No strict historical point-in-time corpus was created.** Both ranges are
  *completed past* dates, so every observation carries a present (2026-07-29) receipt
  `observed_at`.
* **Retrospective observations cannot be treated as historical live-replay features.**
  They are not strict-PIT; the E2 feature-cutoff guard continues to exclude schedules
  first observed after their scheduled start.
* **Rich-data ingestion has not started** and the F1B rich-data pilot **remains
  unauthorized**, pending a separately reviewed manifest and an approved per-run
  request budget.
* No feature engineering, model training, calibration, simulation, EV evaluation,
  backtesting, recommendation, staking, or execution work has started.
* **Schema remains v16.** No migration was added.
* The review itself made **zero external requests**, enforced by process-level guards
  on DNS, non-loopback sockets, httpx transports (sync and async), `requests`,
  `urllib`, the project's read-only client builder, provider client construction, and
  retry sleeps.
