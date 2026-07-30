# F1B NBA Rich-Data Pilot Report (redacted, independently reviewed)

**Scope:** the completed **NBA rich-data** F1B pilot, its zero-request completed
resume, the lineup payload-shape defect it exposed, and the parser repair.

| | |
|---|---|
| Pilot executed at | `97dba1e` (manifests prepared) → run recorded 2026-07-30 |
| Lineup repair commit | **`9386b54`** — *Fix NBA lineup payload parsing* |
| Reviewed commit | **`9386b54`** (both CI jobs passed) |
| Schema | **v16** throughout |

Every figure below was re-derived from the preserved database, checkpoint and
committed manifest under a zero-network sentinel. Prior reports were treated as
unverified input. This report contains no secret, key, authentication header,
response body, or database content; those artifacts stay git-ignored under `data/`.

---

## 1. Manifest

| | |
|---|---|
| File | `pilots/f1b/nba_rich.manifest.json` |
| `manifest_hash` | `9de5d312b99c3e854833505da29f803e0d9cd22f575c35a7f24213200065afa6` |
| `plan_hash` | `1c896ae16a13c10bf5ab15c100e193675c9af06bc396b38ade28e798cdc391a9` |
| Provider / league / stage | `balldontlie` / `nba` / **rich** |
| Date | `2026-01-05` → parses to the inclusive pair `(2026-01-05, 2026-01-05)` |
| Families | `games` (auto) + `box`, `quarters`, `stats`, `advanced`, `plays`, `lineups` |
| Bounds | `max_games=1`, `max_pages=1`, `max_records=100`, `max_retries=1` |
| Rate | configured **60/min** ≤ verified provider maximum **600/min** |
| Semantic maximum / **request cap** | **7** / **14** |
| Credits | **not applicable** |

Canonical JSON verified (bytes == `canonical_json(parsed)`, re-canonicalisation
reproduces the file), duplicate JSON keys rejected, versions
`f1a-manifest-v1`/`f1a-plan-v1`/`bdl-cost-v1`, no secret-shaped field at any depth,
exact scratch/checkpoint paths, no unresolved bounds, and **rebuilding the plan from
the manifest's own fields reproduced the committed `plan_hash` exactly**. All four
committed pilot manifests are byte-identical.

---

## 2. Requests and endpoints — CONFIRMED

**7 semantic requests of a cap of 14**, all HTTP 200, all GET, seven unique content
hashes, every endpoint stored as a sanitized path:

| # | Endpoint | Family | Bytes |
|---|---|---|---|
| 1 | `/v1/games` (listing) | `games` | 6 905 |
| 2 | `/v1/games/18447316` (selected-game re-fetch) | `game` | 864 |
| 3 | `/v1/box_scores` (**shared** by `box` + `quarters`) | `box_scores` | 124 783 |
| 4 | `/v1/stats` (traditional) | `stats` | 41 816 |
| 5 | `/nba/v1/stats/advanced` | `advanced_stats` | 34 243 |
| 6 | `/v1/plays` | `plays` | 219 001 |
| 7 | `/v1/lineups` | `lineups` | 11 211 |

Every family is **gate-known** and **planner-authorized** (each observed count ≤ its
planner slot of 1); the observed total equals the planner's semantic maximum exactly.

**Accounting:** attempts 7, transport starts 7, successful responses 7, parse
successes 7, failures 0, retries 0, blocked 0, **pages fetched 1**, HTTP 429s **0**,
throttle events **0**, throttle wait **0.000 s**, `rate_policy_active=true` with
`rate_limited=false`, credits not applicable.

**Request totals agree across all four sources — all equal 7:** checkpoint cumulative
attempts, `raw_responses` rows, `sum(ingestion_runs.requests_made)` (1 + 6), and total
transport starts (`prior_transport_starts + transport_starts`).

---

## 3. Authentication and tier honesty

* **Authentication succeeded** (`authentication_status=succeeded`).
* **`tier_verified = false`**, `tier_status = configured_not_verified:goat`,
  `tier_evidence_source = none`.
* Tier was **not** inferred from a successful games request. `/v1/games` is available
  below GOAT, so a 200 proves authentication only. The 18 `provider_capabilities` rows
  the run wrote are **declared** states (`is_observed = 0` for all 18) — declarations,
  not observations, and therefore not tier evidence.
* GOAT reachability evidence comes solely from the separately reviewed bounded
  capability audit of 2026-07-28, not from this pilot.

---

## 4. Game selection — CONFIRMED

| | |
|---|---|
| Games received | **8** (counted from the persisted listing body) |
| Games selected | **1** |
| Excluded by `max_games=1` | **7** |
| Selected provider game | **`18447316`** |

The selected game equals the canonical `(date_local, game_id)` first game,
independently recomputed. Honest caveat: for this response the provider order happens
to coincide with canonical order, so **this payload alone does not discriminate**
ordering; the MLB rich pilot does (provider first `824410` vs canonical/selected
`822788`). No rich request or persisted row exists for any of the other 7 games.
`selection_truncated=true` while `budget_exhausted` is null and `blocked_requests=0`.

---

## 5. Persisted rows — CONFIRMED

`PRAGMA integrity_check = ok`, schema **v16**, 45 tables, **empty WAL** (all reviewed
content committed and visible read-only).

| Table | Rows |
|---|---|
| `raw_responses` | 7 |
| `game_schedule_snapshots` | 1 |
| `nba_team_statistics` | 2 |
| `nba_player_statistics` | **60** (35 `traditional` + **25 `advanced`**) |
| `nba_quarter_lines` | **8** |
| `play_snapshots` | **431** |
| `provider_game_references` | 1 |
| `provider_team_references` | 2 |
| `provider_player_references` | 35 |
| `provider_capabilities` | 18 (declared) |
| `ingestion_runs` | 2 (`succeeded`, 1 + 6 requests) |
| `data_quality_issues` | 4 (`note`) |
| baseline reference rows | 389 |
| **TOTAL** | **960** |

**389 baseline → 960 = 571 rows added.** Zero duplicates across all nine append-only
tables. **Absent as required:** no MLB, sportsbook, Kalshi, weather, roster or injury
rows. `nba_game_results = 0` is expected — `results` was not an authorized family.

---

## 6. Cross-family verification — CONFIRMED

* Every rich observation references **only** game `18447316` (team stats, player
  stats, quarter lines, plays, schedule snapshot).
* Team statistics cover exactly the **two** provider team references (`9`, `20`).
* Traditional stats trace to `/v1/stats` (35) and advanced stats to
  `/nba/v1/stats/advanced` (25); every player-stat entity has a provider reference
  (35/35, none missing).
* **`box` and `quarters` shared one response:** the single `/v1/box_scores` content
  hash is the `raw_response_hash` of all 8 quarter rows *and* both team-stat rows, and
  only one `box_scores` request was made — no duplicate request.
* Plays are deterministically ordered: `play_sequence` is strictly increasing with
  **431 unique** sequence values and **431 unique** `play_identity` values across
  periods 1–4.
* Every raw response belongs to a valid ingestion run.
* No unsupported family was fabricated.
* **Retrospective receipt time is not pregame availability:** every observation carries
  a 2026-07-30 `observed_at`, months after the 2026-01-05 game date.

---

## 7. The original lineup defect

`GET /v1/lineups` returned **HTTP 200 with 25 real rows** — a flat list, one row per
player: 2 provider teams, 25 distinct players, **10** with `starter=true`, each row
carrying individual `player` and `team` objects plus `position`, and **0 of 25**
carrying the nested `players` list the pre-repair parser required.

The defect was reproduced by extracting `_parse_lineups` **verbatim from parent commit
`4a0e0a7`** and running it against the preserved body: **0 lineup groups, 0 children**.
One request of budget was spent, the body parsed successfully, and the family was
silently dropped with no data-quality finding.

### Why the original database is unchanged

The original NBA rich database therefore still shows **`lineup_snapshots = 0` and
`lineup_players = 0`**. That is **preserved historical evidence of what the pre-repair
code actually persisted**, and it was deliberately **not** retrofitted, replayed into,
or "corrected". The repaired parser's expected result is demonstrated instead in
throwaway schema-v16 databases (§8). Mutating the evidence to make status output look
cleaner would destroy the audit trail.

---

## 8. Exact-response offline replay of the repaired parser

The exact preserved `/v1/lineups` body was replayed through the **repaired production
normalization and persistence path** into throwaway schema-v16 databases outside any
committed path, under a full network sentinel:

| | |
|---|---|
| Lineup snapshots | **2** |
| Lineup players | **25** |
| `is_starter = true` children | **10** |
| Game | `18447316` |
| Teams | exactly **`9`** and **`20`** (12 + 13 children) |
| Provenance | every snapshot's `raw_response_hash` == the preserved lineup response hash |
| Provider references | a valid player reference for all **25**, none invented; both teams are valid team references |
| Positions | preserved where supplied (`F`/`G`/`C`), never fabricated |
| Parent `is_confirmed` | **false** on both snapshots — no pregame-confirmation claim |
| Other families | unchanged; only lineup/reference/provenance tables grew |

**Idempotency:** a second replay produced no duplicate snapshots (2), no duplicate
children (25), no changed observation without changed content
(`lineup_observations = 0`), identical table counts and identical append-only content
hashes.

**Deterministic ordering:** shuffling the preserved rows across seeds
`0, 1, 7, 42, 99, 1234, 20260730` produced an **identical normalized serialization**,
and persisting shuffled inputs (seeds `3, 11, 2024`) produced **identical persistence
identities** — same snapshot content hashes, same ordinals, same children.

The original NBA rich database and checkpoint remained **byte-identical** throughout.

---

## 9. Additional defect found and repaired by this review

`_id_sort_key` — introduced by the `9386b54` repair — was **not a total order**.
`_provider_id` stringifies whatever JSON supplies, so `"1"` and `"01"` are both
reachable and distinct ids, yet both mapped to the key `(0, 1, "")`. Because `sorted`
is stable, two such players on the same team (same starter flag, no provider row id)
received ordinals that **depended on provider row order** — demonstrated empirically:
input order `["1", "01"]` yielded `[(1,'1'),(2,'01')]` while `["01", "1"]` yielded
`[(1,'01'),(2,'1')]`.

**Narrow repair:** the exact id string is now always carried in the key's final
component, making it total — any two distinct id strings produce distinct keys — while
preserving numeric ordering (`"9"` before `"10"`) and absent-ids-last. The real
provider ids are plain integers, so the **preserved payload's result is unchanged**
(still 2 groups / 25 children / 10 starters). Four adversarial regressions were added.

---

## 10. Completed-resume verification — PASSED

Run against protected **copies** under the network sentinel, an **exploding client
factory** and an **exploding authentication path**:

* **Zero provider-client constructions**, **zero authentication/settings loads**,
  zero transport calls, sentinel never tripped.
* Zero new requests (attempts 7, `prior_requests=7`), **zero new pages**, zero
  throttle activity, **no database mutation**.
* Checkpoint did not regress (still `completed`; report state `resumed_completed`).
* **No additional ingestion run** (still 2); no duplicates; identical table counts and
  identical append-only raw-response hashes; scratch digest unchanged.
* **Prior provenance correctly reports 7 requests and 1 page**
  (`prior_transport_starts=7`, `prior_pages_fetched=1`).
* Lineup tables remain 0 on the evidence copy, as they must.
* Originals byte-identical afterwards.

---

## 11. `data-status` / `data-quality` (original database, offline)

* **data-status** (`--league nba`): schema **v16**; `provider_runs.balldontlie = succeeded`;
  populated families as in §5; **`lineup_snapshots = 0` / `lineup_players = 0`**;
  unresolved references game 1 / team 2 / player 35; `pending_manual_review = 0`; open
  DQ **0 blocking / 0 issue / 4 note**.
* **data-quality** (`--league nba`): **grade A**, score **1.00**, `corpus_valid=true`,
  `execution_valid=true`, E2 findings 0/0/0, with 4 pre-existing `note` rows.

**Honest interpretation:**

* The lineup tables read zero because the **defect evidence was not rewritten**, not
  because the provider returned nothing. The repaired parser's coverage is established
  by the exact-response replay in §8, not by mutating this database.
* Unresolved game/team/player references are **expected**: canonical entity matching
  has not run.
* The 4 `DQ-CAP-NBA-001` capability-honesty notes are **nonblocking** and correct —
  they record that unavailable/unsupported capabilities produced no fabricated rows.
* Grade A here reflects schema/execution validity, **not** completeness of the lineup
  family. The historical silent lineup loss is recorded **in this report** as a known
  defect repaired by `9386b54`, rather than by inserting a new DQ row into the
  preserved database.
* **No strict historical point-in-time corpus exists.**

---

## 12. Isolation and secret audit — PASSED

* All thirteen protected artifacts **byte-identical**: development corpus, both
  skeleton databases and checkpoints, MLB rich database and checkpoint, NBA rich
  database and checkpoint, and all four committed manifests.
* **MLB was not executed:** the NBA database holds zero `mlb_statsapi` rows and the MLB
  rich database still holds its original 6 responses with a pre-review timestamp.
* No unrelated database changed. No path alias, symlink, directory, or cross-manifest
  substitution — every artifact is a regular file at a unique path, and the NBA rich,
  MLB rich and NBA skeleton checkpoints bind three different manifest hashes.
* Temporary replay artifacts were created only under the system temp directory and left
  nothing under `data/` or the repository root.
* **Secrets:** the configured key is absent from the database, checkpoint and every
  review output; all seven raw-response metadata rows are sanitized (path-only
  endpoints, allow-listed headers, no auth header, no key in params). The single
  `authorization` and `?apiKey=` matches occur **once each inside schema DDL comments**
  documenting that such values are never stored — column documentation, not data.

---

## 13. Statements of record

* This was **retrospective capability testing only**: one completed game from a past
  date, exercising capability, coverage, request fan-out and persistence.
* **Receipt time is not historical pregame availability.** Every observation carries a
  2026-07-30 `observed_at`, months after the 2026-01-05 game; these rows are
  retrospective and must never be treated as historical live-replay features.
* **Canonical matching has not run** (`canonical_games = 0`; provider references
  intentionally unresolved).
* **No strict historical point-in-time corpus was created.**
* **No features, models, calibration, EV, recommendations, staking or execution have
  started.**
* Schema remains **v16**; no migration was required by the lineup repair.
* The review itself made **zero live provider requests**, enforced by process-level
  guards on DNS, non-loopback sockets, sync/async httpx transports, `requests`,
  `urllib`, the project's read-only client builder, BALLDONTLIE/MLB client
  construction, authentication/settings loading, and retry sleeps — each proven to fail
  closed by an adversarial probe.
