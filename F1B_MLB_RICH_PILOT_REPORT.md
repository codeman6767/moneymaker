# F1B MLB Rich-Data Pilot Report (redacted, independently reviewed)

**Scope:** the completed **MLB rich-data** F1B pilot and its independent correctness
review. Reviewed at commit `97dba1e`; schema **v16** throughout.

Every figure below was re-derived from the preserved database, checkpoint, and
committed manifest under a zero-network sentinel. The execution report was treated as
unverified input, and each of its claims is marked **CONFIRMED** or otherwise.

This report is redacted by construction: no secret, API key, authentication header,
response body, or database/checkpoint content is included. Those artifacts remain
git-ignored under `data/` for local review only.

---

## 1. Manifest

| | |
|---|---|
| File | `pilots/f1b/mlb_rich.manifest.json` |
| `manifest_hash` | `f56b5c5da53d86c94fe082127579eb75e30d9a59dc6ead65c76e92ef90d9767e` |
| `plan_hash` | `73e887229ce20b8c0e2413de156847abea0d047f0f426a100a7c26f09e10380e` |
| Provider / league / stage | `mlb_statsapi` / `mlb` / **rich** |
| Date range | `2026-07-20..2026-07-21` (completed, retrospective) |
| Families | `schedule` (auto) + `results`, `box`, `inning`, `rosters` |
| Bounds | `max_games=1`, `max_retries=1` |
| Semantic maximum | **6** |
| **Request cap** | **12** (= 6 × (1 + `max_retries`)) |
| Credits | **not applicable** (keyless provider) |
| Schema | **16** |

Validated through the canonical validator: canonical bytes are exactly
`canonical_json(parsed)`, re-canonicalisation reproduces the file, duplicate JSON keys
are rejected, versions are `f1a-manifest-v1` / `f1a-plan-v1` / `mlb-cost-v1`, no
secret-shaped field exists at any depth, and **rebuilding the plan from the manifest's
own fields reproduced the committed `plan_hash` exactly**. The `manifest_hash` equals
the file's SHA-256. The two skeleton manifests and the NBA rich manifest are
byte-identical.

---

## 2. Requests and endpoints — CONFIRMED

**6 semantic requests of a cap of 12**, all HTTP 200, all GET, six unique content
hashes, every endpoint stored as a sanitized path (no query string, no URL):

| Endpoint | Family | Bytes |
|---|---|---|
| `/schedule` (range discovery) | `schedule` | 44 901 |
| `/schedule` (selected game re-fetch) | `schedule` | 1 849 |
| `/game/822788/boxscore` | `game_boxscore` | 165 584 |
| `/game/822788/linescore` | `game_linescore` | 3 314 |
| `/teams/141/roster` | `teams` | 6 691 |
| `/teams/139/roster` | `teams` | 6 681 |

Every called family is **gate-known**, so the fail-closed guard would have blocked an
unmodelled call. Each request was reconstructed and shown to fit its planner
allowance, and the observed total equals the planner's semantic maximum exactly:

| Observed family | Count | Planner slot(s) | Authorized |
|---|---|---|---|
| `schedule` | 2 | fixed `schedule` + contingent `game_schedule` | 2 |
| `game_boxscore` | 1 | `game_boxscore` | 1 |
| `game_linescore` | 1 | `game_linescore` | 1 |
| `teams` | 2 | `roster` (per team/date, max 2) | 2 |
| **total** | **6** | | **6** |

Two labels differ between layers by design and are budgeted 1:1, so totals match: the
per-game schedule re-fetch classifies as `schedule` (same path) while the planner
budgets it as `game_schedule`; rosters classify as `teams` while the planner budgets
them as `roster`.

**Accounting (CONFIRMED):** attempts 6, transport starts 6, successful responses 6,
responses received 6, parse successes 6, failures 0, retries 0, blocked 0, listing
pages 2, HTTP 429s 0, throttle events 0, throttle wait 0.000 s. Credits, authentication
and tier all **not applicable** (keyless MLB); `tier_verified=false`; rate policy
inactive. The database independently corroborates the transport work: 6 raw responses
with HTTP 200, `ingestion_runs.requests_made` summing to 6, and 2 `/schedule` documents.

---

## 3. Game selection — CONFIRMED

| | |
|---|---|
| Games received | **30** (counted from the persisted range-schedule body) |
| Games selected | **1** |
| Games excluded by `max_games=1` | **29** |
| Selected provider game | **`822788`** |

The selection is **deterministic and canonical, not provider-response order** — proven
independently: the provider response's first game was **`824410`**, whereas the
canonical `(officialDate, gamePk)` first game is **`822788`**, which is exactly what
was selected and checkpointed. No rich request or persisted row exists for any of the
other 29 games.

`selection_truncated=true` while `budget_exhausted` is null and `blocked_requests=0`:
a bounded selection is reported strictly apart from budget truncation.

---

## 4. Persisted rows — CONFIRMED

Database: `PRAGMA integrity_check = ok`, schema **v16** (16 migration rows), 45 tables,
**empty WAL** so all reviewed content is committed and visible read-only.

| Table | Rows |
|---|---|
| `raw_responses` | 6 |
| `game_schedule_snapshots` | 1 |
| `provider_game_references` | 1 |
| `provider_team_references` | 2 |
| `game_result_snapshots` | 1 |
| `mlb_inning_lines` | **18** |
| `team_game_statistics` | 2 |
| `player_game_statistics` | 25 |
| `roster_snapshots` | **52** |
| `provider_player_references` | **52** |
| `ingestion_runs` | 2 |
| `data_quality_issues` | **0** |
| baseline reference rows (`leagues`, `schema_versions`, `team_aliases`, `teams`) | 389 |
| **TOTAL** | **551** |

**389 baseline → 551 final = 162 rows added — CONFIRMED.** Two ingestion runs (the
discovery unit and the per-game unit), both `succeeded`, with `requests_made` of 1 and
5. Every raw response belongs to one of those runs. **Zero duplicates** across all ten
append-only tables (checked on `content_hash` or provider id).

**Absent as required:** no NBA, sportsbook, Kalshi, weather, injury, plays, or lineup
rows — all verified zero.

---

## 5. Cross-family consistency — CONFIRMED (recalculated, not copied)

Persisted result for `822788`: away **7 runs / 13 hits / 0 errors**, home **1 run /
8 hits / 3 errors**, `innings_played=9`, `winning_side=away`, status `final`.

The 18 inning-line rows are 9 innings × 2 sides, and summing them reproduces the
result on **every** dimension:

| Side | Inning-sum runs | hits | errors | Result row |
|---|---|---|---|---|
| away | 7 | 13 | 0 | 7 / 13 / 0 ✔ |
| home | 1 | 8 | 3 | 1 / 8 / 3 ✔ |

`winning_side` agrees with the higher run total, and `innings_played` equals the count
of distinct innings.

**One shared linescore response backs both families:** the single `/game/822788/linescore`
response hash appears as the `raw_response_hash` of the result row **and** of all 18
inning rows, and of nothing else. The 2 team-stat rows trace to the single boxscore
response and agree exactly with the result totals (home `141`: 1/8/3, 33 AB; away
`139`: 7/13/0, 37 AB), matching the two provider team references.

**Rosters:** 52 snapshots = 26 per team across exactly the 2 referenced teams, with 52
distinct rostered players and 52 player references (a 1:1 match), tracing only to the
two roster responses. Roster fan-out was therefore exactly **two** team/date requests.

---

## 6. Checkpoint — CONFIRMED

`state=completed`, `manifest_hash` equal to the committed manifest, `plan_version`
`f1a-plan-v1`, `request_cap=12`, `credit_cap` null, `schema_version=16`, date range and
families matching the manifest, a 64-hex scratch fingerprint, `stage_game_ids=["822788"]`,
zero failed and zero incomplete identities, and **two durable identities** (`skeleton`
plus `game:822788`), each carrying the manifest date range. No secret-shaped content.

---

## 7. Completed-resume verification — PASSED

Run against **copies** (originals verified byte-identical afterwards) under the full
zero-network sentinel and a client factory that raises if ever called:

* **Zero provider-client constructions** — completed-work short-circuiting never built
  a client, so no authentication could occur.
* **Zero transport calls**, zero new request usage (attempts still 6, `prior_requests=6`),
  **zero new pages**, **zero database mutation**, `network_occurred=false`.
* Identical table counts **and** append-only content hashes; scratch database digest
  unchanged; no duplicate records; no additional ingestion run.
* Checkpoint remains `completed`; the report identifies the run as `resumed_completed`.
* **Provenance correctly reports the prior 6 requests and 2 pages**
  (`prior_transport_starts=6`, `prior_pages_fetched=2`), and selection accounting
  (30 / 1 / 29) survives the resume.

---

## 8. `data-status` / `data-quality`

* **data-status** (`--league mlb`): schema **16**; `provider_runs.mlb_statsapi = succeeded`;
  all six rich observation families populated; `pending_manual_review=0`; open DQ
  **0 blocking / 0 issue / 0 note**; `canonical_games=0` with unresolved references
  game 1 / team 2 / player 52.
* **data-quality** (`--league mlb`): **grade A**, score **1.00**, `corpus_valid=true`,
  `execution_valid=true`, 0 blocking / 0 issue / 0 note.

`canonical_games=0` and the unresolved references are **expected and correct**: entity
matching is a separate later step and was deliberately not run.

---

## 9. Isolation — PASSED

* **Development corpus unchanged** — byte-identical SHA-256, and every one of its rows
  predates the F1B pilots (latest raw response `2026-07-25T07:36:45Z`).
* **All four skeleton artifacts unchanged** — MLB and NBA skeleton databases and
  checkpoints byte-identical.
* **NBA rich artifacts do not exist** — neither `data/f1b_nba_rich_scratch.db` nor
  `data/f1b_nba_rich.ckpt`, confirming NBA rich had not executed **as of this review**.
  (Historical note: the NBA rich pilot executed later and was independently reviewed —
  see `F1B_NBA_RICH_PILOT_REPORT.md`. This finding is preserved as it stood at `4a0e0a7`.)
* **No cross-contamination** — the rich database holds zero BALLDONTLIE rows; the MLB
  skeleton database still holds exactly its original single raw response.
* **No aliasing** — all nine reviewed artifacts are regular files (no symlinks, no
  directories), each resolving to a unique path and unique inode.
* **No cross-manifest substitution** — the rich and skeleton checkpoints bind different
  manifest hashes and target different scratch databases; the rich checkpoint binds the
  rich manifest.
* No unrelated database was modified.

---

## 10. Secret audit — PASSED

The configured key is **absent** from the rich database, the checkpoint, and every
review output. All six persisted endpoints are sanitized paths; persisted response
headers are allow-listed only, with no authentication header stored; no request
parameter is secret-shaped.

The only matches for `authorization` and `?apiKey=` occur **once each inside the schema
DDL comments** — column documentation stating that such values are never stored — and
are fully accounted for there, not in any data row. No environment dump is present.
Nothing under `data/` is staged.

---

## 11. Probable-pitcher classification

Probable-pitcher IDs arrived **inline** through the hydrated schedule representation
(`hydrate=probablePitcher` on both `/schedule` requests) and are persisted only as the
`home_probable_pitcher_id` / `away_probable_pitcher_id` columns of the schedule
snapshot.

This is consistent with the previously reviewed policy:

* Inline schedule fields are **permitted** within the `schedule` family.
* **`probable_pitcher_snapshots = 0` is expected**, not a failure — no standalone
  probable-pitcher family is claimed as populated.
* **No standalone probable-pitcher endpoint was called** and **no planner unit is
  hidden**: the plan declares no probable-pitcher contingent, and the MLB cost policy
  has no such endpoint family at all. The hydration cost **zero** additional requests.

The rich stage was previously protected only implicitly; this review adds explicit
regression tests (see §13).

---

## 12. Provider limitations and expected-empty families

* `probable_pitcher_snapshots` — empty by policy (inline hydration), as above.
* `games`, `players`, `venues`, `entity_match_decisions` — empty because **canonical
  entity matching was not run**; the pilot deliberately stops at provider-scoped
  references (1 game, 2 team, 52 player references remain unresolved by design).
* No provider error, truncation, retry, or rate-limit condition was encountered, so no
  degraded-coverage path was exercised by this run.

---

## 13. Reporting defects found

**None.** Every claim in the execution report was independently **CONFIRMED**. In
particular the repaired reporting fields behaved correctly for a rich run: request and
page accounting, selection accounting (kept separate from budget truncation),
rate-policy reporting (inactive for a keyless provider), authentication/tier honesty
(both not applicable, tier never verified), and human/JSON output consistency.

The review also **positively validated the defect-E provenance repair** on a checkpoint
written by the repaired code: after the completed resume the checkpoint's transport
counters are resume-local (0) while `prior_transport_starts=6` and
`prior_pages_fetched=2` preserve the first run's evidence — logical-run totals of 6 and
2. (The earlier skeleton checkpoints predate that repair and honestly report zeros.)

One documented granularity limit, not a defect: only transport starts and pages are
carried across a resume, so the logical-run total of *successful responses* is
reconstructible from the database rather than from the checkpoint alone. The database
proves it unambiguously (6 raw responses, all HTTP 200).

**Tests added** — `sports_quant/ingest/tests/test_f1b_mlb_rich_review.py` (7 tests):
rich-stage probable-pitcher policy, planner declares no probable-pitcher unit, reviewed
request/page/selection accounting, reviewed endpoint-family set with bounded roster
fan-out and no second game, shared-linescore provenance with innings summing to the
result, completed-resume provenance, and deterministic secret-free reports.

---

## 14. Statements of record

* This was **retrospective capability testing only**: one completed game from a past
  date range, exercising capability, coverage, request fan-out, and persistence.
* **Current receipt time does not create strict historical point-in-time features.**
  Every observation carries a 2026-07-29 `observed_at`; these rows are retrospective and
  must never be treated as historical live-replay features. The E2 feature-cutoff guard
  continues to exclude them.
* **Matching and canonical corpus construction have not run** — `canonical_games=0`,
  and provider references remain deliberately unresolved.
* **NBA rich execution has not occurred.** Its manifest is prepared and validated but
  unexecuted and unauthorized; it requires separate explicit authorization.
* **No features, models, simulations, EV, recommendations, staking, or execution have
  started.**
* **Schema remains v16.** No migration was added.
* The review itself made **zero external requests**, enforced by process-level guards on
  DNS, non-loopback sockets, httpx transports (sync and async), `requests`, `urllib`, the
  project's read-only client builder, provider client construction, and retry sleeps.
* **F1B rich is NOT complete**: it requires the NBA rich pilot to execute and pass its
  own independent review.
