# F1 MLB June 2026 season-month execution review

Independent, offline correctness / coverage / evidence review of the single live
MLB StatsAPI month execution (PID 29056). **Zero provider requests were made by
this review.** No original database, checkpoint or execution log was modified: all
28 preserved artifacts were fingerprinted before and after and are byte-identical.

| | |
|---|---|
| Manifest | `pilots/f1/mlb_coverage_2026_06.manifest.json` |
| `manifest_hash` | `fcb3c4287a419555636142b5c5a5558997f3d2fe083846613838b51400f1433a` |
| `plan_hash` | `afe14d88807026f26ad1785f64cfa55057659b2c0a8d597045a8385a12c70068` |
| Scratch database | `data/f1_mlb_2026_06_scratch.db` — sha256 `802a7d76…`, schema v17, 152,354,816 bytes, `integrity_check = ok` |
| Checkpoint | `data/f1_mlb_2026_06.ckpt` — sha256 `70bbc7c9…`, 75,341 bytes, state `completed` |
| Execution report | `data/f1_mlb_2026_06_execution.json` — sha256 `b8b93610…` |
| stderr | 0 bytes (`e3b0c442…`, the empty-string digest) |

**Verdict: the run's data is trustworthy and the coverage claim is accurate, but
the `completed` checkpoint state is _not_ semantically trustworthy.** One unit was
recorded complete while two of its requests had failed terminally. Five confirmed
defects are repaired in this change; four more are confirmed and reported for
separate work.

> **Follow-up #1 has since been repaired** — see
> [§14 Checkpoint provenance repair](#14-checkpoint-provenance-repair-follow-up-1).
> A completed resume can no longer erase an earlier process's evidence, and the
> original June checkpoint remains byte-identical and historically unchanged.

---

## 1. Review boundary

A process-level sentinel installed 20 guards (DNS, non-loopback sockets, sync and
async httpx transports, `httpx.Client/AsyncClient.send`, `requests`, `urllib`, the
project's `build_readonly_client`, both provider client constructors, settings/auth
loading, retry sleeps). Ten adversarial probes were run and **all ten failed
closed**. The settings/auth probe did not fail closed on the first attempt; guards
for `config.load_settings` and `f1a._default_client_factory` were added and
re-verified before any review work proceeded.

Two guards were deliberately and narrowly relaxed, each for a local file read, and
each documented at the point of use:

- `config.load_settings` — the matching runner and `db-init` resolve the default
  database path through it. No setting value is printed anywhere in this review.
- `f1a._default_client_factory` — the resume path builds a client *factory* (a
  closure) before it knows whether any unit remains. `MlbStatsApiClient.__init__`
  stayed guarded throughout, which is what makes "zero transport" provable: a
  resume that tried to do work would have had to construct a client and would have
  tripped the sentinel.

For the offline reconstruction (§9) the real client and `AsyncClient.send` were
restored behind an `httpx.MockTransport` serving only preserved bodies. The DNS,
socket and `AsyncHTTPTransport` guards stayed installed, so no request could leave
the process regardless.

Review copies were made with the SQLite backup API from a read-only source
handle: `_review_a.db`, `_review_b.db`, `_review_c.db` (all `integrity_check = ok`,
v17, 1,999 raw responses, git-ignored, not symlinks, not aliased to the original)
plus a byte-identical checkpoint copy.

## 2. Execution timeline (one process)

| | |
|---|---|
| PID | 29056 |
| Started | 2026-07-31T23:32:16.077521Z |
| Finished | 2026-08-03T08:37:32.623372Z |
| Wall elapsed | 205,523.0 s (57.1 h) |
| Exit code | 0 |
| `ingestion_runs` rows | 401, all `command=ingest-mlb`, all `provider=mlb_statsapi` |

The 57-hour wall time is **not** 57 hours of provider traffic. Gap analysis over
the 1,999 stored responses shows exactly one gap longer than five minutes:

```
55.98 h   2026-08-01T00:32:25.979091Z  ->  2026-08-03T08:31:01.449998Z
          last before : /game/824011/linescore
          first after : /schedule
```

Summing only the gaps ≤ 5 min gives **1.11 h of actual request activity** inside a
57.1 h window. The host was suspended; the process survived it.

The first ten inter-request deltas after the wake are
`[2.01, 1.99, 2.00, 2.00, 2.00, 2.00, 2.00, 2.00, 1.99, 2.00]` s — the
`mlb-pacing-v1` courtesy interval (30/min, burst 1 → 2.0 s) was respected
immediately on resumption with **no burst**. No HTTP 429 and no blocked request
occurred at any point in the run.

Effect of the suspension, stated only as far as the evidence supports: it did not
invalidate the run, did not corrupt the database (`integrity_check = ok`), and did
not cause a pacing violation. It **did** kill two in-flight requests, and that is
the origin of every coverage gap below. The audit that authorized this run was
performed immediately before it; the 56-hour suspension means the run's later half
executed well after that audit, which is a freshness caveat on the audit, not on
the data.

## 3. Request, retry and failure reconciliation

Five independent sources agree exactly: the execution JSON, the checkpoint's
`usage`, the database rows, the `ingestion_runs` totals, and 34 read-only progress
samples. Every field in the execution JSON matches the checkpoint field for field.

**Semantic requests planned and issued: 2,001**

| Family | Count | Derivation |
|---|---|---|
| `/schedule` | 401 | 1 discovery + 1 per selected game |
| `/game/{pk}/boxscore` | 400 | 1 per selected game |
| `/game/{pk}/linescore` | 400 | 1 per selected game |
| `/teams/{id}/roster` | 800 | 2 per selected game (home + away, at the game's date) |

**The accounting identity.** `retry_attempts` is a *subset* of attempts, not a
third terminal outcome, so the sets are never added together:

```
attempts (transport starts)      = 2008
terminal outcomes                = 1999 successful + 2 failed = 2001
attempts - terminal outcomes     = 7   == retry_attempts
```

Seven requests were retried once each (`max_retries = 1`); five of those succeeded
on the retry and two failed terminally. `reserved_attempts == transport_starts ==
2008`, within the semantic maximum 3,001 and far within the hard cap 6,002.
`throttle_events = 1999`, `throttle_wait_seconds = 3407.9` (56.8 min) — i.e. 9 of
2,008 attempts found a free pacing slot, which is consistent with a steady 2 s
interval.

Stored raw responses = 1,999 = `successful_responses`. Distinct
`(provider, endpoint, params, content_hash)` = 1,997; the two extra rows are the
same-content re-fetches of the 2026-06-24 doubleheader rosters (§6), not lost data.

### The two terminal failures

Both are roster requests, and both belong to the same unit:

| | Failure 1 | Failure 2 |
|---|---|---|
| Endpoint family | `roster` | `roster` |
| Identity | provider team `108`, date `2026-06-28` | provider team `133`, date `2026-06-28` |
| Attempts | 2 (initial + 1 retry) | 2 (initial + 1 retry) |
| Terminal error | request failed after retry; no response stored | same |
| Raw response stored | none | none |
| Normalized observations | absent — 0 `roster_snapshots` rows for that team-day | absent — 0 rows |
| Containing unit | game `824011` (2026-06-28, home 108 v away 133) | game `824011` |
| **Checkpointed as complete** | **yes** | **yes** |

Neither team is globally unrostered: team 108 and team 133 each have 668 roster
rows on other dates. Only that team-day is missing.

The narrative is exact. Game 824011's unit began at 2026-08-01T00:32:21Z and
stored `/schedule`, `/boxscore` and `/linescore` — its linescore at 00:32:25.979Z
is the **last request before the suspension**. The two roster requests were still
outstanding when the host slept. On wake, both failed, both were retried once, and
both failed terminally, so the unit returned `partially_failed`. Its box, linescore
and result data all persisted (1 result row, 18 inning lines, 2 team-stat rows,
28 player-stat rows); only the rosters are missing.

## 4. Partial-failure checkpoint semantics — CONFIRMED DEFECT (repaired)

`_IngestorExecutor._run_ingest` raised `_UnitFailed` only when
`result.status == "failed"`. The offline reconstruction reproduces the June run's
unit statuses as `{'succeeded': 400, 'partially_failed': 1}`, so game 824011's unit
was yielded and therefore written into `completed_identities`. The database
independently corroborates it: exactly one `ingestion_runs` row has status
`partially_succeeded` (`run_01KZ3BYVM5RCHRYRHZKEFTCENB`, 3 requests), and the unit
identity for game 824011 **is** present in the checkpoint's completed set.

Consequence: a `completed` checkpoint permanently concealed a real coverage gap.
No resume would ever retry that unit, because the unit was marked done.

**Is the original `completed` state semantically trustworthy? No.** The state is
*mechanically* correct — 401 of 401 units were yielded, and the run genuinely
exited 0 — but it asserts a completeness that does not hold: one unit is short two
of its five semantic requests. The database contents remain trustworthy; the
completeness *claim* does not.

Repaired in `sports_quant/ingest/f1a.py`: `partially_failed` now raises
`_UnitFailed` alongside `failed`, leaving the unit incomplete and resumable.

**Behavioural consequence, stated plainly:** under the repaired code the June run
would have stopped at game 824011 with a failed, resumable state after 371 units
rather than reporting a complete month. That is the intended trade — a visible stop
beats silent partial coverage — and completed units stay checkpointed, so a resume
continues from where it stopped rather than starting over.

The historical checkpoint was **not** edited. It still records exactly what the
live process wrote.

## 5. 402 received → 400 selected — CONFIRMED DEFECT (repaired)

The execution report gave no attribution for the gap: `games_received = 402`,
`games_selected = 400`, `games_excluded_by_max_games = 0`,
`selection_truncated = false`. Nothing accounted for the missing two.

Reconstructing the preserved discovery body (599,116 bytes) settles it: the payload
contains **402 game entries with only 400 distinct `gamePk`**. Two `gamePk` values
appear twice, and `_select_games` deduplicated them while counting nothing.

```
gamePk 824912  x2   officialDate=2026-06-16  Final(F)  gameDate=2026-06-16T23:15:00Z
                    officialDate=2026-06-16  Final(F)  gameDate=2026-06-17T18:00:00Z   [resumed]
gamePk 823613  x2   officialDate=2026-06-24  Postponed(DR)  gameDate=2026-06-22T23:10:00Z
                    officialDate=2026-06-24  Final(F)       gameDate=2026-06-24T17:10:00Z  dh=S
```

**Complete accounting identity for the discovery pass:**

```
games_received                      402
  - duplicate entries removed         2
  - entries with no gamePk            0
  - excluded by max_games (600)       0     (400 <= 600)
  = games_selected                  400
```

This is worse than a reporting gap. Both duplicated entries had **identical**
`(officialDate, gamePk)` sort keys, so the stable sort left the tie broken by
**provider payload order**, and dedup kept whichever copy arrived first. For
gamePk 823613 that was the superseded `Postponed` record — so the corpus records a
game that actually finished **10–3 in 9 innings** as `postponed`, and that outcome
would flip if the provider reordered its response. The function's own docstring
claimed it ordered "never by provider response order", which was exactly untrue in
the duplicate case dedup exists to handle.

Repaired in `sports_quant/ingest/mlb_ingestor.py`:

- Duplicate resolution is content-aware and totally ordered — most settled status
  wins, then latest `gameDate`, then canonical serialization. Payload order never
  decides.
- Deduplication is unconditional. It was previously skipped when `max_games` was
  `None`, which let one `gamePk` be ingested twice on an unbounded run.
- Removals are counted: `MlbIngestResult.games_deduplicated` and a new
  `games_deduplicated` usage field, surfaced in the pilot's `selection:` line, so
  `received = selected + excluded_by_max_games + deduplicated` always closes.
- An entry with **no** `gamePk` is deliberately kept for the normalizer, which
  already rejects it and records the data-quality rejection. An earlier draft of
  this repair dropped such entries and silenced that existing signal; the
  inherited test `test_malformed_data_rejection_is_not_active_failure` caught it.

The corpus was **not** rewritten. Game 823613's row still carries the status the
live run persisted; the repair changes future ingests only.

## 6. Membership, postponed dates and label-coverage denominators

All 400 selected games are `gameType = R`, season 2026, 400 distinct `gamePk`.
Local dates span 2026-06-01 … 2026-09-04 over 36 distinct dates. **No June date
has zero selected games.** Statuses: 393 `final`, 7 `postponed`.

Four membership categories:

| # | Category | Count | Games |
|---|---|---|---|
| 1 | Official date in June, played, correctly labelled `final` | 393 | — |
| 2 | Official date in June, **actually played**, mislabelled `postponed` | 1 | 823613 (real result 10–3, 9 innings) |
| 3 | Rescheduled **out** of June — official date now July–September | 6 | 823042 (07-23), 824664 (08-06), 824589 (08-20), 823539 (08-29), 824911 (08-31), 824424 (09-04) |
| 4 | Received at discovery but not selected (duplicate `gamePk` entries) | 2 | second entries for 824912 and 823613 |

Of category 3, one game (823042) has since been played and carries a full result
(10–6, 9 innings, 18 inning lines, 30 player-stat rows); the other five have no
depth at all, which is correct — their linescore payloads are 407 bytes with zero
innings, and their result rows are honestly `NULL` for every score field. **Nothing
was fabricated.**

Those six rows were **not** deleted or rewritten merely because their current
official date falls outside June. They were legitimately selected by a June-window
query and their `scheduled_start` still records the original June start time.

**Label-coverage denominators.** The denominator matters, and the obvious choice is
wrong:

| Denominator | Count | Use |
|---|---|---|
| Selected games | 400 | request/coverage accounting |
| Official date inside June 2026 | 394 | calendar-window questions |
| Labelled `final` by the persisted schedule | 393 | **understates reality by 2** |
| **Games with a complete result (score + innings)** | **395** | **correct denominator for result labels** |
| Games with no result available (not yet played) | 5 | genuinely unlabelable |

Result-label coverage is therefore **395/395 = 100 %** of playable games, not
393/400. A label builder that filters on `mapped_status = 'final'` would silently
drop two real completed games (823613, 823042).

Roster (team-day) coverage: expected 798 distinct `(team, date)` pairs from the
schedule, observed 796 → **99.75 %**, with both missing pairs on 2026-06-28 for
teams 108 and 133 — the two terminal failures, and nothing else. The exclusion is
reported, never used to inflate coverage.

## 7. Database and provenance audit

`integrity_check = ok`, schema v17. Every observation table has complete
provenance — zero NULLs in `run_id`, `raw_response_id`, `raw_response_hash`,
`content_hash`, `observed_at`, `ingested_at`, and **zero orphaned raw-response
references**:

| Table | Rows |
|---|---|
| `game_schedule_snapshots` | 400 |
| `game_result_snapshots` | 400 |
| `mlb_inning_lines` | 7,174 |
| `team_game_statistics` | 800 |
| `player_game_statistics` | 11,783 |
| `roster_snapshots` | 20,550 |
| `provider_team_identity_snapshots` | 1,630 |
| `provider_player_identity_snapshots` | 47,830 |
| `raw_responses` | 1,999 |
| `data_quality_issues` | 0 |

Roster sizes per team-day are plausible: min 23, median 26, max 27, across 30 teams
and 36 dates. Missingness is confined to one date: `{'2026-06-28': 2}`,
`{'108': 1, '133': 1}`.

Two provenance observations that are **not** defects but must not be mistaken for
latency evidence:

- `ingestion_runs.requests_made` sums to **1,999**, not 2,008. It counts stored
  (successful) responses, so the 7 superseded and 2 failed attempts are invisible
  in that column. Per-run it equals stored responses for all 401 runs.
- All 1,999 raw responses were **requested before** their own run row's
  `started_at`, and created after it. The run row is stamped when the persistence
  transaction opens, so `started_at` / `duration_ns` measure persistence, not the
  fetch window (72 ms for the unit whose fetches spanned 56 h). Per CLAUDE.md, this
  must not be reported as end-to-end latency.

## 8. Canonical matching (review copy A)

Nothing was pre-resolved: before matching, 0 of 30 team references, 0 of 400 game
references and 0 of 1,053 player references had canonical ids. Matching ran teams →
games → players through the product CLI:

| Entity | Method | Outcome | n | Score |
|---|---|---|---|---|
| team | `exact_provider_id` | accepted | 770 | 1.00 |
| team | `normalized_alias_unscoped` | accepted | 30 | 0.90 |
| game | `official_key_exact` | accepted | 400 | 1.00 |
| player | `official_provider_bootstrap` | accepted | 1,051 | 1.00 |
| player | `league_normalized_name` | **ambiguous** | 2 | 0.00 |
| venue | none | **no_candidate** | 31 | 0.00 |

Teams 30/30 and games 400/400 resolved. Players resolved 1,051/1,053.

The two unresolved players are **correct, deliberate refusals**, not failures:

```
mlb_statsapi:691777 -> ambiguous (name matches canonical player … which another
  mlb_statsapi player id already owns; two distinct official ids are two
  identities without stronger evidence)
mlb_statsapi:820862 -> ambiguous (same)
```

They are two genuine same-name collisions — two distinct players named **Max
Muncy** (571970, 691777) and two whose names differ only by an accent, **José
Fermín** / **José Fermin** (665877, 820862). Refusing to merge them is right.
**These two are not guessed at and are left explicitly unresolved.**

The 31 venue `no_candidate` decisions are expected: the `venues` table is empty
because this pilot never declared a venue family, and the schedule references 31
distinct venue ids. They produce 31 `DQ-MATCH-011` issues, all flagged for manual
review.

## 9. Determinism, idempotency and reconstruction

**Reversed traversal (copy B).** All 400 games and 1,053 players were re-driven
one at a time in descending id order. Teams, games, counts and linkage projections
are identical to copy A. Players are **not**: the accepted/ambiguous roles of both
collision pairs swap (copy A resolves 571970 and 665877; copy B resolves 691777 and
820862). Confirmed defect — reported below, not repaired here.

**Idempotency (copy B).** Repeating the full matching pass appended **800**
additional `entity_match_decisions` rows (2,284 → 3,084) with no change to any
canonical entity or linkage. Duplicates are exclusively `team` decisions (30 groups,
up to 55 repeats for team 109); game and venue decisions replay correctly.
Confirmed defect — reported below, not repaired here.

**Completed-resume proof (copy C, originals untouched).** Resuming the completed
checkpoint against copies exits 0, skips **401/401** units, and does
`transport_starts = 0`, `network_occurred = false`, `database_mutated = false`, with
the database byte-identical afterwards. `MlbStatsApiClient.__init__` remained
sentinel-guarded throughout, so zero transport is proven structurally: no client was
ever constructed. `reserved_attempts` reads 2,008 because the gate is pre-charged
with the prior process's usage so manifest caps span resumed processes — that is
the logical-run total, not new work.

The resume did, however, **rewrite the checkpoint** — see the reported defect below.

**Offline reconstruction into fresh v17 databases.** The 1,997 preserved bodies
were replayed through the real client behind a `MockTransport` into three fresh
`db-init` v17 databases, in ascending `gamePk`, descending, and a seeded shuffle.
The two failed rosters were served 503 rather than invented, so their families stay
explicitly incomplete. Ascending and descending are byte-identical and match the
original exactly on schedule (400), results (400), innings (7,174), team stats (800)
and player stats (11,783). The shuffle differs — in `roster_snapshots` only — which
exposed the fourth repaired defect.

## 10. Defects repaired in this change

| # | Defect | Location | Manifest in the June corpus? |
|---|---|---|---|
| 1 | A `partially_failed` unit was checkpointed as complete, hiding a coverage gap behind a `completed` state | `ingest/f1a.py` | **Yes** — game 824011 |
| 2 | Schedule dedup was payload-order dependent and uncounted, persisting a superseded record and leaving `received − selected` unattributed | `ingest/mlb_ingestor.py`, `request_control.py`, `ingest/f1a.py` | **Yes** — 823613 recorded `postponed` despite a 10–3 final; 2 removals unreported |
| 3 | A complete score under a non-final status raised no data-quality finding (`_result_issues` checked only the inverse) | `ingest/mlb_ingestor.py` — new `DQ-MLB-RESULT-003` | **Yes** — 823613 and 823042, with `data_quality_issues = 0` |
| 4 | Roster transition anchor omitted `roster_date` while `roster_date` was inside the content hash, so re-observing an earlier date after a later one read as a state change | `db/repositories/rosters.py` | Latent — 0 rows affected; the canonical `(officialDate, gamePk)` order happened to keep the doubleheader's two units adjacent. Reproduced by varying order (+48/+49 duplicate rows). |
| 5 | `data-status` reported a hard-coded `schema_version: 16`, misstating the v17 corpus it had just read | `status.py` | **Yes** — printed "schema v16" for this v17 database |

Fifteen regression tests in
`sports_quant/ingest/tests/test_f1_month_review_repairs.py` reproduce each defect
first and then pin the repaired behaviour, including that narrowing the roster
anchor does not hide a genuine same-date roster change.

## 11. Defects confirmed and reported, deliberately not repaired here

These are real and reproduced, but they sit in the matching and checkpoint
subsystems rather than the MLB ingestion lane this execution exercised. Bundling
them into a review commit would widen the blast radius well past the review's
scope; each deserves its own reviewed change.

1. **A completed resume overwrites the run's failure and retry evidence.**
   ~~Highest-priority follow-up~~ — **REPAIRED, see §14.** Resuming the completed
   checkpoint rewrote `usage`, zeroing `successful_responses` 1999→0,
   `failed_responses` 2→0, `retry_attempts` 7→0, `throttle_wait_seconds`
   3407.9→0.0, `pages_fetched` 401→0 and `families_completed` →`[]`. Requests were
   carried forward as `prior_requests` / `prior_transport_starts` /
   `prior_pages_fetched`, but there was **no** `prior_failed_responses` or
   `prior_retry_attempts`, so one harmless-looking `--resume` destroyed the only
   durable record that two requests failed and seven were retried. Completion facts
   (`state`, `completed_identities`, `stage_game_ids`) survived. This is the reason
   this review ran resume against a copy.
2. **Same-name player resolution is order-dependent.** Which member of a
   collision pair is accepted and which is left ambiguous depends purely on
   traversal order. The refusal to merge is correct; the arbitrariness is not. An
   official-id bootstrap should mint a distinct canonical player per distinct
   official id and never fall through to name matching for the official provider.
3. **Team match decisions are not idempotent.** Each matching pass appends one
   `team` decision per team *occurrence* rather than replaying an unchanged
   decision, growing the ledger without bound (55 rows for team 109 after three
   passes). Games and venues already replay correctly.
4. **Matching does not backfill canonical ids into the observation tables.** After
   fully successful matching, `game_schedule_snapshots.home_team_id` is 0/400,
   `roster_snapshots.player_id` 0/20,550, `player_game_statistics.player_id`
   0/11,783 and `team_game_statistics.team_id` 0/800. Canonical linkage lives only
   in `provider_*_references`. Any downstream consumer reading those columns
   directly — feature and label building in particular — would see nothing. This
   needs an explicit decision before F2, not a silent assumption.

## 12. Data-status and data-quality interpretation

```
data-status  schema v17  league=MLB
  canonical games: 400
  unresolved refs: game=0 team=0 player=2
  pending manual review: 33
  open DQ: blocking=0 issue=31 note=0

data-quality  B  [CORPUS-VALID]  score=0.86  league=MLB
  E2 findings: blocking=0 issue=0 note=7 (execution_valid=True)
    E2-LABEL-UNAVAILABLE: 7
  pre-existing open data_quality_issues: blocking=0 issue=31 note=0
```

Read correctly:

- **Grade B / score 0.86 is not a data defect.** The 7 `E2-LABEL-UNAVAILABLE`
  notes are the 7 `postponed` games, which is the label-availability signal working
  as intended. Five of those genuinely have no result yet.
- **`blocking = 0`** — nothing bars use of this corpus.
- **The 31 `issue` findings are all `DQ-MATCH-011` venue no-candidates**, caused by
  an empty `venues` table, not by anything the month run fetched.
- **`pending manual review: 33`** = 31 venues + the 2 legitimate player collisions.
- **`player = 2` unresolved references are the correct refusals**, not a gap to
  close by guessing.
- **`data_quality_issues = 0` before matching was itself the signal** that produced
  repaired defect #3: two games held complete 9-inning scores under a `postponed`
  status and nothing said so.

Note that the two roster failures produce **no** data-quality row at all. Coverage
missingness is currently visible only by comparing expected against observed
team-days, as this review did. Worth considering alongside follow-up #1.

## 14. Checkpoint provenance repair (follow-up #1)

Reported above as the highest-priority follow-up, and now repaired. The defect was
first reproduced offline against copies with 20 zero-network guards installed and
all ten adversarial probes failing closed: the completed resume constructed **zero**
provider clients, made zero transport starts and left the database byte-identical —
and destroyed **12** usage fields, with no `prior_*` counterpart existing for seven
of them (`failed_responses`, `retry_attempts`, `successful_responses`,
`throttle_events`, `throttle_wait_seconds`, `http_429s`, `blocked_requests`).

**Accounting model.** A *logical run* is one manifest executed by one or more
processes. The checkpoint format is now `f1a-checkpoint-v2`, where `usage` holds the
**logical-run totals** and `usage_provenance.processes` is the append-only
per-process history those totals derive from. For additive counters,
`logical_total = prior_total + current_process_value` exactly. Nothing is blindly
summed: `network_occurred` is a logical OR, family collections are deterministic
unions, selection counts take a high-water mark, a recorded budget exhaustion is
never overwritten by a later clean process, authentication and tier evidence follow
a precedence order that can never be upgraded without an observation, and plan
identity must agree across processes or the load fails closed. Every `UsageReport`
field carries exactly one declared rule in `sports_quant/usage_provenance.py`, and
a test asserts the table covers the dataclass exhaustively.

A process owns exactly **one** history entry and replaces it on each write, and the
gate's prior pre-charge is subtracted out of that entry, so repeated resumes can
never multiply prior usage and a third process cannot re-count the first one's
attempts.

**Completed no-work resume is now a true no-op.** It validates the manifest,
checkpoint and scratch database, proves offline that no unit remains, synthesizes
its result from the evidence already on disk, constructs no provider client, writes
no row, and does **not** rewrite the checkpoint. Verified against copies of the real
June artifacts:

| Logical total | Before resume | After resume |
|---|---|---|
| Reserved attempts | 2008 | 2008 |
| Transport starts | 2008 | 2008 |
| Successful responses | 1999 | 1999 |
| Terminal failed responses | 2 | 2 |
| Retry attempts | 7 | 7 |
| Pages fetched | 401 | 401 |
| HTTP 429s | 0 | 0 |
| Blocked requests | 0 | 0 |
| Courtesy throttle events | 1999 | 1999 |
| Courtesy throttle wait | 3407.889 s | 3407.889 s |
| Games received / selected | 402 / 400 | 402 / 400 |
| `families_completed` | `[game, skeleton]` | `[game, skeleton]` |

The checkpoint copy stayed byte-identical (`70bbc7c9…` → `70bbc7c9…`) across two
successive no-op resumes, and every accounting invariant closes on those totals.

**Backward compatibility.** The original June checkpoint is v1 and still loads
unmodified: its flat `usage` is adopted as a single legacy history entry so every
fact it holds survives, it is flagged `legacy_migrated` with
`process_count_known = false` because the per-process split is genuinely unknowable,
and a missing counter stays missing rather than becoming a misleading `0`. A v1 file
is upgraded to v2 only when a resume actually does work. **The original June
checkpoint remains byte-identical, still v1 on disk, with no `usage_provenance`
block written into it** — its historical completion decision was *not* retroactively
repaired, and it remains semantically untrustworthy for the reason in §4.

**Reporting.** A no-work resume now prints `new_work=False`,
`checkpoint_mutated=False` and, for every counter, `this_process=0` beside `prior=`
and `logical_total=`, so a clean row of zeros can never be read as a run that
fetched nothing. In JSON, `usage` is the logical total, with
`current_process_usage` and `prior_process_usage` as separate keys.

**Also repaired here:** a unit that an earlier process left blocked/failed/
incomplete and a later process completed now leaves the unresolved sets and is
recorded in `recovered_identities`, because a `completed` state holding an
unresolved unit is a contradiction — which a load now refuses.

Still true: the two missing June roster responses were **not** retrieved and remain
explicitly missing; no live request was made; the June data itself remains valid.

## 15. Status

- Live execution: **valid**. One process, one provider, correct pacing, no 429s,
  no blocked requests, within every declared bound, `integrity_check = ok`, full
  provenance, nothing fabricated.
- Coverage: **400/400 games**, 395/395 result labels on playable games, 796/798
  roster team-days. The single 2-request shortfall is identified, attributed and
  reported, not smoothed over.
- Checkpoint `completed` state: **not semantically trustworthy** for the reason in
  §4. The historical checkpoint is preserved exactly as written.
- The two terminal failures were **not** retrieved and the two missing responses
  were **not** fabricated. Their families remain explicitly incomplete.
- **NBA is not authorized by this review.** The live process exiting 0 does not
  authorize it. Follow-up #1 is now repaired (§14), but that repair itself remains
  unreviewed independently, so NBA stays unauthorized until it is reviewed or its
  validation boundary is explicitly accepted.
- **F1 remains incomplete, and F2 remains unauthorized.**

Validation: `ruff` clean; `mypy` clean (289 files); **1,865 passed, 1 skipped**;
`db-init` applies all 17 migrations to schema v17 and is idempotent on a second
run; all 28 preserved artifacts byte-identical.
