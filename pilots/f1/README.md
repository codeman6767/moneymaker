# F1 season-month coverage/depth pilots

Two bounded, reviewed manifests and the protocol for executing them **later**.

> **Nothing here authorizes execution.** Committing a manifest is not
> authorization. Each live run needs explicit user authorization plus the
> process-scoped `MONEYMAKER_F1B_AUTHORIZED=1` boundary, and a fresh provider
> audit immediately beforehand. Neither pilot has run.

## Status

| | |
| --- | --- |
| One-game skeleton + rich capability verification | complete (MLB, NBA) |
| Schema v17 official identity bootstrap | complete |
| NBA scheduled-start normalization repair | complete |
| Official team / game / player matching mechanics | proven offline, one game per league |
| **Season-month coverage/depth pilots** | **manifests prepared, NOT executed** |
| F1 | **incomplete** |
| F2 | **unauthorized** |
| 99% identity acceptance gate | **not approached, not passed** |

## The two slices

| | MLB | NBA |
| --- | --- | --- |
| manifest | `mlb_coverage_2026_06.manifest.json` | `nba_coverage_2026_03.manifest.json` |
| range | `2026-06-01..2026-06-30` | `2026-03-01..2026-03-31` |
| provider | MLB StatsAPI | BALLDONTLIE (GOAT) |
| season | 2026 | season-start year 2025 |
| families | `schedule`, `results`, `box`, `inning`, `rosters` | `games`, `box`, `quarters`, `stats`, `advanced`, `plays`, `lineups` |
| scratch db | `data\f1_mlb_2026_06_scratch.db` | `data\f1_nba_2026_03_scratch.db` |
| checkpoint | `data\f1_mlb_2026_06.ckpt` | `data\f1_nba_2026_03.ckpt` |
| schema | v17 | v17 |

### Why these months

Both are **complete past calendar months** relative to the repository's working
date (2026-07-31), so neither can grow after measurement — a month still in
progress would make coverage percentages move under the reviewer.

Both sit **inside the regular season and nowhere near a postseason boundary**:

* MLB's regular season spans late March to late September. June is mid-season,
  after the April ramp-up and before any September call-up distortion, and
  contains no postseason games — so a June slice is not an accidentally mixed
  regular/playoff sample. It also avoids the July All-Star break, which would
  depress the games-per-day rate and make a per-season extrapolation misleading.
* The NBA regular season spans October to mid-April, so March is entirely
  regular-season: the play-in and playoffs begin after it. March is also the
  densest ordinary NBA month, which is the right stress case for pagination and
  request fan-out.

Both are **recent** — the most recent completed regular-season month available
for each league — so they measure current provider behaviour rather than
historical archive behaviour. The one-game pilots that preceded them used
`2026-07-20..2026-07-21` (MLB) and `2026-01-05` (NBA), both of which these
manifests deliberately do not reuse.

Neither slice was chosen to be easy: both are full months at full density.

## What these pilots measure

Schedule/game coverage · final-label coverage · team/game/player identity
coverage · per-family endpoint reachability · nonempty vs empty family behaviour
· normalized row counts · missingness · pagination and record truncation ·
corrections and history behaviour · request fan-out · actual vs planned maximum
requests · rate limiting and retries · restartability and completed resume ·
matching determinism and idempotency · `data-status` / `data-quality` ·
estimated requests per season.

## What they do NOT establish

Strict historical point-in-time feature availability · historical sportsbook or
Kalshi quote coverage · profitability · model accuracy · F2 corpus acceptance ·
the final 99% gate across multiple seasons. **One season-month is a
provider-depth pilot, not corpus acceptance.**

## Request-cap derivation

Both caps come from `sports_quant.ingest.planning`, not from a chosen number.
`request_cap = semantic_requests_max() × (1 + max_retries)`, and **every**
transport attempt — each retry, each pagination page — reserves against it. The
gate is fail-closed: exhaustion stops the run.

### MLB — cap 6002

| term | count |
| --- | --- |
| month schedule discovery (one ranged call, no date partitioning) | 1 |
| per selected game: single-game schedule re-fetch | 600 |
| per selected game: linescore (**shared** by `results` + `inning`) | 600 |
| per selected game: box score | 600 |
| per selected game: up to 2 team-date rosters | 1200 |
| **semantic maximum** | **3001** |
| × (1 + `max_retries` = 1) | |
| **hard request cap** | **6002** |

`max_games = 600`. A June in which all 30 teams played every day is 15 × 30 =
450 games; adding doubleheaders leaves an ordinary June comfortably below 600.
Maximum game count representable: **600**.

### NBA — cap 21616

| term | count |
| --- | --- |
| games listing pages (`max_pages`) | 8 |
| per selected game: single-game fetch | 400 |
| per selected game: box score (**shared** with `quarters`) | 400 |
| per selected game: `stats` pages | 3200 |
| per selected game: `advanced` pages | 3200 |
| per selected game: `plays` pages | 3200 |
| per selected game: `lineups` | 400 |
| **semantic maximum** | **10808** |
| × (1 + `max_retries` = 1) | |
| **hard request cap** | **21616** |

`max_games = 400` (an NBA March is ~200–280 games). `max_pages = 8` at 100
records/page: the binding case is `plays`, where a normal game has ~430–480 and a
multi-overtime game can approach 600 — 8 pages carries 800 with margin, and the
month games listing needs only ~3 pages. `max_records = 1000` sits above the
worst single-call volume so `max_pages` is the binding bound.

The planner is conservative in the safe direction: a family that finishes in one
page consumes one request rather than `max_pages`, so a month whose `stats` and
`advanced` each fit on one page spends 2 rather than 16 per game. Actual usage
will be far below the cap; the cap is a ceiling, not a forecast.

**Measured, not assumed — box scores are one request per selected game.**
`_fetch_all` fetches box scores per distinct *date*, but the pilot executor drives
**one game per checkpointed unit**, so each unit sees a single date and issues its
own box request. Two games on the same night therefore each fetch that night's box
response. This is inside the plan (which models box as 1 per game, never per date)
and is not a budget risk, but it is a bounded redundancy of roughly
`(games - dates)` requests per month — for a 250-game March over 31 dates, about
220 redundant requests out of ~2500. It is recorded here rather than presented as
a saving, and reducing it would mean caching responses across pilot units, which
is a change to the pilot loop and out of scope for manifest preparation.

## Request pacing

Both pilots are paced, and the two policies are **different kinds of claim**.

| | MLB | NBA |
| --- | --- | --- |
| policy version | `mlb-pacing-v1` | `bdl-rate-v1` |
| basis | `project_courtesy_cap` | `verified_tier_max` |
| configured rate | **30 / minute** | 60 / minute |
| burst | **1** (no opening burst) | sliding window |
| minimum interval between starts | **2.0 s** (derived from the rate) | none |
| provider-published maximum | **unknown / null** | 600 / minute (GOAT tier) |
| authentication | not applicable | applies |
| tier | not applicable | `goat` |
| credits | not applicable | not applicable |

**The MLB rate is ours, not the provider's.** MLB StatsAPI is keyless, unmetered
and publishes no rate ceiling this repository can verify, so
`provider_rate_limit_per_min` stays **null** and the policy records
`basis=project_courtesy_cap`. Inventing a number to fill that field would turn our
own safety setting into a false provider claim. Reporting says so in words: the
human line prints `basis=project_courtesy_cap`, the parenthetical
`(PROJECT COURTESY CAP, not a provider limit)`, and `provider_max=unknown` rather
than `None/min`.

### Why it was added before authorizing execution

The first MLB month manifest was correctly **aggregate-capped** at 6,002 attempts,
but the MLB branch of `_make_gate` attached no rate policy at all: only a request
budget, a credits-not-applicable policy and the endpoint-cost classifier. The cap
bounded *how many* requests a month could make and nothing bounded *how fast*, so
a 3,001-request month would have gone as quickly as sequential responses returned.
Live execution was withheld and the pacing policy added first.

### Shape

`burst=1` means the first request goes immediately and every later transport start
is separated by at least two monotonic seconds. The interval is **derived** from
the configured rate (60 s / 30 per min = 2.0 s) so the stated rate and the stated
interval can never disagree. The clock is monotonic, so a system wall-clock change
cannot compress the spacing, and concurrent callers serialize onto distinct future
slots rather than all reading the same instant.

At 30/minute a full 3,001-request MLB month paces to roughly **100 minutes** of
client-side wait, on top of provider response time.

`rate_limited` remains reserved for an **actual** event: a request that really
waited, or a provider 429. A policy merely being attached sets
`rate_policy_active`, never `rate_limited`. Courtesy pacing accumulates in
`throttle_events` / `throttle_wait_seconds`; a provider 429 is counted separately
in `http_429s`, and its `Retry-After` backoff is a distinct sleep that does not
inflate the courtesy-wait total.

### Retries and 429s

Every retry re-enters the same chokepoint — `reserve()` then `rate_acquire()` then
`mark_transport()` — so a retry is both budgeted and paced. `max_retries` stays at
the committed **1**; this change added no retries. A numeric `Retry-After` is
honoured, bounded by the backoff cap; a malformed (HTTP-date) value falls back to
bounded exponential backoff. No header value is ever exposed in output.

### Resume

A **completed** resume with nothing outstanding is a **true no-op**: zero
transport calls, zero pacing delay, zero throttle events, no provider client is
constructed, the database is not written, and the checkpoint file is left
**byte-identical**. An **interrupted** resume starts a fresh *process-local*
pacing window — the limiter holds no cross-process state — but it does **not** get
a fresh aggregate request budget: cumulative request usage carries forward and the
pacing policy applies before every new transport.

### Checkpoint usage provenance (`f1a-checkpoint-v2`)

A *logical run* is one manifest executed by one or more processes. Each process
produces its own usage report, and the checkpoint keeps them apart:

| Checkpoint field | Meaning |
|---|---|
| `usage` | **Logical-run totals** across every process of the run |
| `usage_provenance.processes` | Append-only per-process history, oldest first |
| `usage_provenance.legacy_migrated` | The history was adopted from a v1 file |
| `usage_provenance.process_count_known` | False for a migrated v1 history |

For additive counters the invariant is exactly
`logical_total = prior_total + current_process_value`. Set-like and evidence-like
fields are never summed: family collections take a deterministic union,
`network_occurred` is a logical OR, selection counts take a high-water mark,
authentication/tier evidence follows a precedence order that can never be upgraded
without an observation, a recorded budget exhaustion is never overwritten by a
later clean process, and plan/manifest identity must agree across processes or the
load fails closed. Every field of `UsageReport` carries exactly one declared rule
in `sports_quant/usage_provenance.py`, and a test asserts the table covers the
dataclass exhaustively.

A process owns exactly one history entry and *replaces* it on every checkpoint
write, so the many writes of one process — and any number of repeated resumes —
contribute their evidence once. The gate's prior pre-charge is subtracted out of a
process's own entry, so a third process can never re-count the first one's
attempts.

Why this exists: the June 2026 review proved that a completed `--resume` rewrote
the checkpoint with the resuming process's empty report, zeroing
`successful_responses` (1999 → 0), `failed_responses` (2 → 0), `retry_attempts`
(7 → 0), `throttle_wait_seconds` (~3407.9 → 0), `pages_fetched` (401 → 0) and
`families_completed`. One harmless-looking resume destroyed the only durable
record that the run had contained terminal failures and retries.

A v1 checkpoint (including the original June one) still loads: its flat `usage` is
adopted as a single legacy history entry so every fact it holds survives, it is
flagged `legacy_migrated`, and a missing counter stays **missing** rather than
becoming a misleading `0`. A v1 file is only upgraded to v2 when a resume actually
does work; reading it, or a no-op resume against it, leaves it untouched.

Reporting keeps the two views side by side. A no-work resume prints
`this_process=0` next to `prior=` and `logical_total=` for requests, successes,
terminal failures, retries, pages, throttling and 429s, plus
`new_work=False checkpoint_mutated=False`, so a clean row of zeros can never be
mistaken for a run that fetched nothing. In JSON, `usage` is the logical total and
`current_process_usage` / `prior_process_usage` are separate keys.

### Untrusted-input and unit-set validation

A checkpoint on disk is untrusted evidence. Every usage value is validated against
the `UsageReport` dataclass's own type annotations — the single source of truth, so
the validator cannot drift from the report — and a wrong type, a non-finite float, a
negative count or a boolean where a number is declared is **refused** rather than
coerced. Unknown keys are dropped and never reach a report or a rewritten file. An
**absent** counter stays unknown; it is never read as `0`, because a legacy file may
simply not carry a field and inventing zero would manufacture a contradiction the
evidence never claimed.

The stored logical totals must equal what the recorded history implies for **every**
rule, not only the additive ones, and the unit sets must not contradict each other:
no identity may be both completed and failed/blocked/incomplete, and
`recovered_identities` must be a subset of completed and disjoint from every
unresolved set. All of these fail closed with a sanitized error before any client is
constructed, any authentication is loaded, the database is opened writable, or the
checkpoint is written.

### Unit-level provenance

Each process entry carries a random per-invocation `process_id` — never the PID,
which the OS reuses. A migrated v1 aggregate instead carries the literal marker
`legacy-v1-unsplit`, which says exactly what it is: one entry standing for an unknown
number of earlier processes.

When a unit raises, the runner records the identity the executor reports as in
flight into `incomplete_identities` (read from the executor, never inferred). A later
process that completes that unit moves it into `recovered_identities`, so a unit
recovered after a failure stays distinguishable from one that completed first time.
Reports show `initially_completed`, `recovered_on_resume` and `still_unresolved`.

Independently reviewed and accepted in
`CHECKPOINT_RESUME_PROVENANCE_REVIEW.md`.

### Fail-closed conditions

Refused before any client is constructed: a nonpositive configured rate, a
nonpositive burst on a courtesy policy, an unsupported policy version, a courtesy
policy carrying a tier or a provider maximum, and a manifest whose declared
`configured_rate_per_min` disagrees with the runtime policy — because a declared
pacing bound that the runtime would ignore is worse than no bound at all.

> **The regenerated MLB manifest needs a NEW fresh provider audit.** The audit run
> immediately before this change passed, but it is no longer the immediately
> preceding audit for the manifest that will actually execute: the manifest hash
> and plan hash both changed. A fresh MLB StatsAPI audit is required again before
> MLB month execution is authorized.

## Truncation and record-loss guarantees

Nothing truncates silently. Verified in
`sports_quant/ingest/tests/test_f1_month_pilots.py` by driving the real
orchestration over `httpx.MockTransport`:

* **`max_games`** — selection reports `games_received`, `games_selected`,
  `games_excluded_by_max_games` and `selection_truncated` as four separate
  numbers. An ordinary synthetic month reports zero exclusions; a deliberately
  tiny bound reports the exclusion count and sets the flag.
* **`max_pages` / `max_records`** — `_paginate` emits an explicit truncation note
  and increments `records_truncated` when either bound stops a family, naming the
  bound that bit.
* **Box-score cursor** — a box response that still has a next cursor is reported
  as partial rather than treated as complete.
* **Shared requests** — one linescore backs `results` + `inning`, and one box
  response backs `box` + `quarters`. Neither family pair is re-fetched per
  consumer: the differential test asserts exactly one linescore and one box
  request per selected game, never two.
* **Budget** — `requests_used` equals the transport-attempt count exactly, so no
  page or retry gets a free request.

## Coverage-report contract

Each execution must emit a deterministic machine-readable JSON report and a
human-readable text report, per league, containing the following sections. A
figure that was not measured must be reported as unmeasured, never as zero.

### `requests`
`planned_semantic_maximum`, `hard_request_cap`, `attempts`, `successful`,
`failures`, `retries`, `pages`, `http_429`, `throttle_events`,
`throttle_wait_seconds`, `blocked`, `estimated_season_equivalent_requests`
(month attempts scaled by season-days ÷ month-days, labelled an estimate).

### `games_and_labels`
`scheduled_games_returned`, `games_selected`, `games_excluded_by_limits`,
`final_labels`, `nonfinal_excluded` broken out by
postponed / cancelled / suspended / scheduled, and `label_coverage_pct`
(final ÷ selected).

### `matching`
Provider `team` / `game` / `player` reference counts; `accepted`, `ambiguous`,
`unmatched`, `manual_review`; coverage percentage **by entity type** and **by
game**; and the method mix (`exact_provider_id`, `normalized_alias_unscoped`,
`official_provider_bootstrap`, …).

### `families`
One entry per authorized family: `endpoint_reached`, `response_nonempty`,
`normalized_rows`, `unique_games` / `unique_teams` / `unique_players`,
`missingness` (per-field null rates), `rejected_rows`, `dq_findings`,
`pagination_truncated`, `records_truncated`.

### `integrity`
`schema_version`, `manifest_hash`, `plan_hash`, `completed_resume_result`,
`determinism_result`, `data_quality_grade`, open `blocking` / `issue` / `note`
counts, `original_artifact_isolation` (every prior artifact byte-identical), and
a `secret_redaction_audit` result.

Every report must state plainly: **one season-month is a provider-depth pilot,
not final corpus acceptance.**

## Execution and review protocol

Strictly sequential. Each numbered step is a separate, explicitly authorized
task; none of them is authorized by this document.

1. **Fresh provider audit** immediately before each league's live execution —
   bounded, GET-only, on the day of the run. A stale audit does not carry over.
2. **MLB month execution only.**
3. **Independent MLB execution review.**
4. **NBA month execution only.**
5. **Independent NBA execution review.**
6. **Combined F1 coverage/depth review.**
7. **Only then** decide whether F1 passes and whether F2 may be *planned*.

Preconditions for any live step: clean tree, the committed manifest hash matches,
the scratch database is new or an authorized resumable checkpoint match, the
request cap is the manifest's, and the user has authorized that specific step.

## Regenerating the manifests

```
python pilots/f1/generate_manifests.py
```

`generate_manifests.py` holds every semantic input and is the single source of
truth. Regeneration is byte-identical and CI asserts it. Changing any semantic
input — date, family, `max_games` / `max_pages` / `max_records` / `max_retries`,
**rate**, scratch path, checkpoint path, declared schema version — changes the
manifest hash by construction. The MLB rate is part of plan identity, so adding
the 30/minute courtesy cap changed both the MLB plan hash and the MLB manifest
hash while leaving the semantic maximum at 3,001 and the hard cap at 6,002.
