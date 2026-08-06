# F1 NBA March-2026 month execution — independent review

Offline correctness, coverage, normalization and evidence review of the completed
BALLDONTLIE (GOAT) NBA `2026-03-01..2026-03-31` season-month execution.

**Zero provider requests were made by this review.** The month execution was not
re-run or resumed, MLB was not executed, the three separately documented
canonical-matching defects were not repaired, and F2 was not begun.

> **Verdict: the execution is ACCEPTED for six of the seven families it fetched;
> `lineups` is NOT accepted** — 40 of 239 games hold provably partial lineups
> because a provider pagination cursor was silently discarded. Labels are **0/239**
> under the real point-in-time contract, for two independent reasons. **Five**
> production defects were confirmed from preserved evidence and repaired offline;
> recovering the truncated lineups requires a separately authorized targeted live
> repair, which was **not** executed.
>
> Two of the reported claims did not survive reconstruction: `nba_team_statistics`
> is **478 rows, not zero**, and `pages_fetched = 3` is a *listing-page* count
> beside 1,437 provider responses. Every other reported figure reconciled exactly.

> **UPDATE (2026-08-05) — the offline results repair this review specified has
> since been applied.** `nba_game_results` is now **239/239** in the March working
> database, populated by replaying the preserved `/v1/games/{id}` bodies through
> the production normalizer with each response's own observation time and
> provenance. **No provider request was made**, the executed manifest and
> checkpoint were not changed, and a frozen pre-repair database preserves the
> original execution state locally. See `F1_NBA_2026_03_RESULTS_REPAIR.md`.
>
> Everything else in this review still stands as written and describes the corpus
> **as the month execution left it**: the `lineups` family is still unaccepted for
> 40 games, and **usable point-in-time labels are still 0/239** because canonical
> matching has not run. That repair has since been **independently reviewed and
> accepted** — `NBA_RESULTS_REPAIR_INDEPENDENT_REVIEW.md`.

> **UPDATE (2026-08-06) — the targeted live lineup recovery this review specified
> has since been executed and independently reviewed.** One authorized execution
> recovered all 40 truncated games: 40/40 provider cursor chains terminated
> normally, 40 requests, zero first-page requests, zero failures, and the March
> corpus was not modified. The execution review **accepted** the evidence and found
> **40/40 games merge-eligible** —
> `NBA_LINEUP_CONTINUATION_EXECUTION_REVIEW.md`.
>
> This review's diagnosis is confirmed by the recovered evidence: the 40 games are
> exactly those whose page one held 25 rows — a full page at `per_page=25` — which
> is why the discarded cursor mattered for them and for none of the other 199.
>
> The `lineups` family nevertheless **remains formally unaccepted here**: the
> recovered rows live only in a separate recovery database and **no merge into the
> March corpus has been performed or authorized**. Point-in-time labels remain
> **0/239**.

---

## 1. Review boundary and zero-network proof

A process-level sentinel installed **23 guards**: DNS (`getaddrinfo`,
`gethostbyname`, `gethostbyname_ex`, `create_connection`), non-loopback
`socket.connect` / `connect_ex`, `httpx.get` / `httpx.request`, sync and async
httpx transports, `httpx.Client.send` / `AsyncClient.send`, `requests.request` /
`Session.send`, `urllib.request.urlopen` / `OpenerDirector.open`, the project's
`build_readonly_client`, both provider client constructors, `config.load_settings`,
`f1a._default_client_factory`, and `time.sleep` / `asyncio.sleep` (any positive
duration). **Fourteen adversarial probes were run and all fourteen failed closed**
— every one raised the sentinel, none completed and none was refused by an
unrelated error.

No provider audit was run. No missing results or team statistics were fetched. No
live ingestion of any kind occurred.

Two guards were relaxed, each narrowly and only for the §13 reconstruction:

- `httpx.AsyncClient.send` — restored so an `httpx.MockTransport` serving only
  preserved bodies can answer. `AsyncHTTPTransport.handle_async_request`, DNS and
  the socket guards **stayed installed**, so a request for a body we did not
  preserve could not fall through to a real socket; the mock fails closed (599).
- `BalldontlieClient.__init__` — so the real production client is exercised rather
  than a stand-in.

Everywhere else — including both completed no-op resumes and the results replay —
all 23 guards stayed installed and the sentinel was never tripped. Pacing used an
injected recording no-op (`RequestGate(sleep=...)`); **no real sleep was taken**.

## 2. Evidence protection

Byte fingerprints (SHA-256) and SQLite integrity/count summaries were recorded for
41 artifacts before any work: the development corpus, both skeleton and both rich
F1B databases/checkpoints, the F1B audit scratch database, all four matching copies
and both matching reports, the MLB June execution database/checkpoint/logs and the
repaired June database/checkpoint, both F1 month manifests, all four F1B manifests,
the five committed review/pilot reports, and the original NBA month
database/checkpoint/logs.

The original NBA evidence is:

| | |
|---|---|
| `data/f1_nba_2026_03_scratch.db` | 354,844,672 B · `421c9f9a4566265e…` · regular file · not a symlink · git-ignored (`.gitignore:38`) |
| `data/f1_nba_2026_03.ckpt` | 48,469 B · `c17a375daa89e3f0…` · regular file · not a symlink · git-ignored |
| `data/f1_nba_2026_03_execution.json` | 3,787 B · `32d7ac293a33e4b9…` |
| `data/f1_nba_2026_03_execution_meta.json` | 883 B · `ce08e9a733ed8ab5…` |
| `data/f1_nba_2026_03_execution.stderr.log` | 0 B (empty — no stderr was produced) |

Review copies were made with the SQLite online-backup API from a `mode=ro` source
handle — **the original was never opened writable**:
`data/f1_nba_2026_03_review_a.db` and `_review_b.db`, both `integrity_check = ok`,
schema v17, 1,437 raw responses, distinct inodes from the original and from each
other, git-ignored. Two byte-identical checkpoint copies were made for the no-op
resume tests. The original database SHA-256 was recomputed after copying and after
every subsequent step and is **unchanged throughout**.

## 3. Execution timeline — exactly one logical process

| | |
|---|---|
| PID | 27284 |
| Process creation | `2026-08-04T22:12:10.2072140Z` |
| Started | `2026-08-04T22:12:10.205794Z` |
| Finished | `2026-08-04T22:36:00.221289Z` |
| Wall elapsed | 1,430.016 s (23.8 min) |
| Native exit code | **0** |
| Authorization | `MONEYMAKER_F1B_AUTHORIZED=1`, child process only; `ungated_env_present: false` |
| Command | `python -m sports_quant ingest-nba --pilot --manifest pilots/f1/nba_coverage_2026_03.manifest.json --scratch-db … --checkpoint … --json` |

**One logical process, confirmed four independent ways.** The checkpoint's
`usage_provenance` records `accounting_version=f1-usage-provenance-v1`,
`process_count=1`, `process_count_known=true`, `legacy_migrated=false`, and exactly
one process entry (`f34f8f99ef01a22d637e5197`) whose counters equal the logical-run
totals exactly. The database holds 240 `ingestion_runs`, all `command=ingest-nba`,
all `provider=balldontlie`, all `status=succeeded`, all `tool_version=sports_quant
0.1.0`, all `error_type` NULL. Stored response timestamps run from
`22:12:11.003355Z` to `22:35:53.636257Z` — one contiguous 1,422.6 s band inside the
process lifetime, with no gap longer than 44.2 s (the longest is a rate-limiter
minute-window wait, not a second launch). `incomplete_identities`,
`failed_identities`, `blocked_identities` and `recovered_identities` are all empty.

The Windows launcher/interpreter pair is not a duplicate execution: only one PID
appears in the execution metadata, only one `process_id` in v2 provenance, and a
second executing process would have produced a second contiguous request band and
additional `ingestion_runs` — neither exists. **No resume ran** (`process_count=1`,
`skipped_on_resume=0`, `performed_new_work=true`). **No second audit ran** (no
`/v1/teams`, `/v1/players` or `/v1/player_injuries` response is stored — the audit
probe endpoints are entirely absent). **No other provider process ran** (every one
of the 1,437 responses is `provider=balldontlie`; no MLB, odds, Kalshi or weather
row exists anywhere in the database).

**The API key never appeared in output or persisted metadata.** Stored response
headers are exactly `content-type`, `date`, `etag` on all 1,437 rows — no
`authorization`, `set-cookie` or key-bearing header. Stored request parameters are
exactly `start_date`, `end_date`, `per_page`, `cursor`, `date`, `game_ids[]`,
`game_id` — no credential. A whole-corpus sweep of `raw_responses`
(endpoint/params/headers/content-type), `ingestion_runs` (args/error) and
`data_quality_issues` (description/detail) for `authorization`, `x-api-key`,
`api_key`, `apikey`, `cookie`, `set-cookie`, `bearer`, `token`, `secret` returned
**zero hits**. The stderr log is empty.

### Request and pacing accounting — closed under the BALLDONTLIE contract

| Counter | Reported | Reconstructed from evidence |
|---|---|---|
| Reserved attempts | 1,437 | 1,437 |
| Transport starts | 1,437 | 1,437 |
| Responses received | 1,437 | **1,437 rows in `raw_responses`** |
| Parse successes | 1,437 | 1,437 |
| Successful terminal | 1,437 | **1,437 rows with `http_status=200`** |
| Failed terminal | 0 | 0 (no non-200 row exists) |
| Retry attempts | 0 | 0 |
| HTTP 429 | 0 | 0 |
| Blocked requests | 0 | 0 |
| Listing pages | 3 | **3 `/v1/games` responses** |
| `SUM(ingestion_runs.requests_made)` | — | **1,437** |

The accounting closes exactly: `1,437 = 3 listing pages + 239 games × 6 per-game
requests`, and the six per-game requests are visible one-for-one in the stored
endpoints (239 × `/v1/games/{id}`, `/v1/box_scores`, `/v1/stats`,
`/nba/v1/stats/advanced`, `/v1/plays`, `/v1/lineups`). Every response attaches to a
valid run (0 orphans) and every run produced at least one response (0 empty runs).

Pacing reconciles. Total transport time (`SUM(elapsed_ns)`) is 248.7 s
(p50 0.146 s, p95 0.329 s, max 2.589 s). Measured idle between one response's
receipt and the next request is **1,173.8 s**, of which the run recorded
**716.2 s** as throttle wait across **305 throttle events**; the residual 457.6 s
is normalization and persistence between units (240 committed runs writing 114,738
plays and 91,187 identity snapshots). Observed idle therefore exceeds recorded
throttle wait, which is the only direction consistent with honest accounting. The
18 waits of ≥ 20 s (553.2 s total) are the 60-requests-per-minute window
refilling — consistent with 1,437 requests over 24 minute-windows.

`rate_policy_basis="verified_tier_max"` is correctly distinguished from
`tier_verified=false` / `tier_status="configured_not_verified:goat"`: the basis
names the provider's *published* per-tier ceiling (600/min), while our own
subscribed tier remains unverified because no tier-gated probe was run in this
process.

## 4. Game discovery and selection

Three `/v1/games` listing responses, cursor-paginated correctly:

| page | params | rows | `meta` |
|---|---|---|---|
| 1 | `start_date=2026-03-01, end_date=2026-03-31, per_page=100` | 100 | `next_cursor=18447784` |
| 2 | `+ cursor=18447784` | 100 | `next_cursor=18447884` |
| 3 | `+ cursor=18447884` | 39 | *(no `next_cursor` — terminated correctly)* |

**Complete accounting identity:**

```
raw rows returned      239
rows with valid id     239   (0 malformed, 0 rejected)
unique valid ids       239   (0 duplicates)
selected               239   (provider_game_references)
excluded                 0
selected-not-received    0
239 received = 239 valid = 239 unique = 239 selected + 0 excluded   ✓
```

`selection_truncated=false`, `games_excluded_by_max_games=0`,
`games_deduplicated=0`; `max_games=400` was never approached.

- **Every March day is represented.** 31 distinct dates, `2026-03-01 .. 2026-03-31`,
  no missing day and no date outside the range; 3–12 games per day.
- **All 239 belong to the requested range** under the provider date contract: the
  stored `game_date_local` is anchored on the provider's own `date` field (never
  recomputed from the UTC `datetime`, which would move every evening tipoff to the
  next day), and `MIN/MAX(game_date_local)` is exactly `2026-03-01`/`2026-03-31`.
- **No playoff or play-in game is present.** All 239 carry `postseason: false` and
  `season: 2025`; the NBA play-in begins after March.
- **No payload-order dependence.** Stored order is the canonical id-sorted order,
  not the provider's payload order (the two differ), and the stored set equals the
  payload set regardless of order.
- **No duplicate provider game reference**: 239 rows in
  `provider_game_references`, 239 distinct `provider_game_id`.
- All 239 report `status: "Final"`; all 239 normalize to `mapped_status='final'`.

## 5. The missing-`results` question

The manifest declares `["advanced","box","games","lineups","plays","quarters","stats"]`.
`results` is absent. The five questions, answered separately:

**1. Provider final-score availability — YES, complete.** All 239 listing rows and
all 239 single-game responses carry both `home_team_score` and
`visitor_team_score`, and all 239 report `status: "Final"`.

**2. Typed result-row availability — NONE.** `nba_game_results` holds **0 rows**;
`game_result_snapshots` (the MLB-only table) holds 0. `game_schedule_snapshots` has
**no score column at all** — final scores are preserved only inside raw response
bodies and, incidentally, in `nba_team_statistics.points`.

**3. Reconstructable quarter totals — EXACT for all 239.** Every game has
two-sided quarter lines; the per-side sums equal the provider's final score for
**239/239 games**, including all 9 overtime games. Zero mismatches, zero asymmetric
periods, zero games missing quarter data.

**4. Correction-history availability — NONE.** `nba_game_results` is where
`SqliteNbaResultRepository` maintains `is_correction` against the immediate
temporal predecessor. With zero rows there is no correction history, and
`CORRECTION_TIMESTAMPS` is declared `unsupported` at GOAT regardless.

**5. Current historical-dataset label eligibility — ZERO.**
`sports_quant/pit/asof.py:305` shows `AsOfReader.official_result` reads
**`nba_game_results` and nothing else**; it is the sole label source
`build_historical_dataset` consumes. Running the real builder against the review
copy returns **0 label rows**.

### Root cause — a planner vocabulary defect, not an operator choice

`results` is implemented end to end: it is in `nba_ingestor.VALID_INCLUDES`, mapped
in `_INCLUDE_CAPABILITY` to `GAME_RESULTS`, handled by `_persist_one_game`, backed
by `SqliteNbaResultRepository` and the `nba_game_results` table, and declared
`SUPPORTED` at GOAT. But `planning.NBA_RICH_FAMILIES` and `f1a._NBA_RICH` **omitted
the name** (MLB's equivalent sets include it). No NBA manifest could therefore ever
declare the family, so **every** NBA month run was structurally guaranteed to
produce zero label rows regardless of how complete its coverage was. Confirmed and
repaired — see §10.

### Conclusion: **B — the omission requires offline repair, and offline repair is valid**

Not A: no typed, correction-aware observation exists, so the downstream label
contract is not satisfied. Not C: NBA results need **no separately observed
`results`-family response**. Unlike MLB (whose results require their own linescore
call), an NBA result is normalized from the very `/v1/games` payload the plan
already fetches per game — all 239 of which are preserved.

**Proven on temporary copies.** Replaying the 239 preserved `/v1/games/{id}`
responses through the production normalizer (`_normalize_game`) and the production
repository (`SqliteNbaResultRepository.append`) into a backup copy produced:

```
inserted / unchanged / skipped        239 / 0 / 0
all final                             True
all scores present                    True
ties or null winner                   0
observed_at from preserved response receipt times   True   (no fabricated time)
raw_response_id is a preserved response id          True   (no fabricated provenance)
orphan raw references                 0
quarter-line sums agree               239   (disagree 0)
team-stat points agree                478 / 478
replay #2 inserted                    0     (idempotent)
sentinel trips                        NONE  (zero provider requests)
```

Observation time is the preserved `received_at` of the provider's own response and
provenance is that response's own `raw_response_id`/`body_hash` — **nothing is
fabricated**. **The original execution database was not modified**: its SHA-256
before and after the replay is identical (`421c9f9a4566265e…`).

Populating `nba_game_results` for the real March corpus is a separate, authorized
step; this review only proves it is achievable offline.

## 6. Final-label coverage

| Measure | Count |
|---|---|
| Games selected | 239 |
| Final-status games | 239 |
| Games with **typed result rows** | **0** |
| Games with complete home/away final scores in preserved evidence | 239 |
| Games whose quarter lines sum **exactly** to the final score | 239 |
| Games with missing or asymmetric periods | 0 |
| Games with overtime | 9 (period 5 only; no double OT in March) |
| Ties or invalid outcomes | 0 |
| **Label-eligible under the real PIT contract** | **0** |

- **Raw final-status coverage: 239/239 (100%).**
- **Score-available coverage: 239/239 (100%)** — in raw bodies, plus
  `nba_team_statistics.points` (478/478 agree with the provider final score).
- **Typed correction-aware result coverage: 0/239 (0%).**
- **Current dataset-label coverage: 0/239 (0%).**

239/239 final statuses is emphatically **not** 100% accepted labels. Two
*independent* blockers each force 0:

1. `nba_game_results` is empty (§5) — repairable offline.
2. **No canonical games exist.** `games`, `players`, `seasons` and `venues` are all
   empty, and 0 of 239 `provider_game_references` carry a canonical `game_id`,
   because matching has not run. `build_historical_dataset` reads canonical `games`
   first, so it returns 0 rows even *after* the results replay — verified:

```
ORIGINAL (no results)          canonical_games=0  nba_game_results=  0  -> PIT label rows = 0
AFTER offline results replay   canonical_games=0  nba_game_results=239  -> PIT label rows = 0
```

## 7. Team statistics

**The reported claim of zero `nba_team_statistics` is incorrect.** The database
holds **478 rows** — exactly 239 games × 2 teams, 239 home and 239 away, 30 distinct
provider teams, **zero NULL points**, and `points` equal to the provider's final
score for **478/478** rows.

The substantive concern behind the claim is nevertheless real, and it is about
*content*, not count. Exhaustive inspection of **all 31 distinct preserved
`/v1/box_scores` responses (239 game objects, 478 team blocks)** shows the team
block contains **exactly** these keys, on every single block:

```
id · abbreviation · city · conference · division · full_name · name · players
```

There is **no team aggregate statistic of any kind** — not under another key, not
under another shape, nowhere in the payload. Game-level fields are date, datetime,
status, period, time, season, postseason, the two team scores, the flat
`home_q1..q4`/`visitor_q1..q4` and `home_ot1..ot3`/`visitor_ot1..ot3` period
fields, `*_in_bonus` and `*_timeouts_remaining`. Team aggregates exist only
implicitly, inside the per-player rows.

`_parse_team_stats` (`_normalize_box_team_lines`) **was called and returned
correctly**: 478 rows, one per team block that carried an id, with the `players`
array deliberately excluded and the team identity block preserved as the JSON stat
line. It did not fail to recognise a supported shape — there is no such shape.

### Conclusion: **A — the row content is correct; the capability language was not**

The capability declaration `TEAM_STATISTICS: SUPPORTED` with the comment
"derivable from box scores" conflated **endpoint access** with **normalized team
aggregate support**, and the pre-execution audit inherited that conflation by
mapping `/v1/box_scores → TEAM_STATISTICS` with no qualification. What is actually
persisted is team **identity plus the team's final score**. Wording corrected in
three places (§10). No team aggregate was fabricated by summing player rows, and
the normalizer docstring now forbids it explicitly.

## 8. Per-family coverage

### Games
3 listing pages (cursor-followed to exhaustion) + 239 single-game requests.
**239 schedule observations**, 239 distinct games, 239/239 with a valid
`scheduled_start` (from the provider `datetime`, independent of status), 31
distinct `game_date_local`, all `mapped_status='final'`. Identity observations:
**6,474 team** and **91,187 player** snapshots; **30 team**, **239 game** and
**550 player** provider references. 0 rejected rows, 0 malformed entries.
Missingness: `venue_id` 239/239 NULL and `home_team_id`/`away_team_id` 239/239 NULL
— both expected, since venues are not in the manifest and canonical ids await
matching.

### Box and quarters
**239 `/v1/box_scores` requests returning 31 distinct responses.** Box scores are
fetched *by date*, one request per selected game, so a date with N games was
fetched N times — 208 of the 239 responses are byte-identical duplicates of one of
31 unique bodies (1,437 responses → 1,229 distinct content hashes; 239 − 31 = 208).
That is redundant provider load, not a correctness fault, and it is fully
attributed. 0 empty responses, 0 games missing box or quarter data, **no response
carried a cursor**.

**1,930 quarter rows** across 239 games: 478 rows each for periods 1–4 and 18 rows
for period 5 (the 9 overtime games). **Zero asymmetric periods** — every period has
exactly 2 rows. Sums close to the provider final score for all 239 games. Team-stat
outcome: 478 rows of identity + final score (§7).

### Traditional statistics
239 requests, 1 page each, `per_page=100`, no cursor on any response.
**8,526 rows** across **239 games** (0 missing), **549 unique players**, 30 teams.
0 empty responses, 0 duplicate `(game, player, group)` rows, 0 rejected.
Missingness: `position` and `is_starter` are NULL on all rows — the `/v1/stats`
payload carries neither; `points` is present on all 8,526.

### Advanced statistics
239 requests, 1 page each, no cursor. **6,289 rows** across **239 games** (0
missing), **507 unique players**, 30 teams. 0 empty, 0 duplicates, 0 rejected.
`points` is NULL on all 6,289 — the advanced payload has no `pts` field, which is
why 6,289 of the 14,815 combined player-stat rows have a NULL `points`. The 507 <
549 player gap is genuine provider missingness (advanced lines are not emitted for
every player who appears in a box line), not a normalization loss.

### Plays
239 requests, 1 page each. **114,738 rows** across 239 games; min 409, median 478,
max 568 per game. **114,738 distinct `play_identity`, zero duplicates within any
game.** `period`, `play_sequence` and `clock` are populated on 100% of rows, and
every one of the 239 games has strictly distinct sequence values, so ordering is
total and reproducible. `provider_play_id` is NULL on all rows because the payload
has no `id` field — the ingestor derives a deterministic game-scoped identity from
the provider's own `order`/`period` rather than guessing one. 0 empty responses, 0
truncation. **14,414 substitution events** (min 36, median 60, max 95 per game),
detected from `type`/`text`. `provider_player_id` is NULL on all 114,738 rows: the
payload carries a `participants` id array rather than a single player, and the
ingestor correctly refuses to guess which participant a play belongs to — but the
full array is preserved in `extra` on **114,738/114,738** rows, so substitution
player attribution remains recoverable later without a refetch.

### Lineups — **NOT ACCEPTED**
239 requests, one per game, `per_page=25`. **478 snapshots — exactly two per game
for all 239 games**, 5,125 lineup players, 0 duplicate players within a snapshot,
exactly **5 starters per snapshot (2,390 = 478 × 5)**, `player_count` ranging 8–15.
Both the flat (official) and nested payload shapes normalize; the March payload is
the flat shape. 0 empty responses.

**However: 40 of the 239 responses carried `meta.next_cursor` and the cursor was
silently discarded.** Those 40 games — and exactly those 40 — stored precisely 25
lineup players, the `per_page` ceiling, against a 21-player median across all games
(min 17, max 25). Their lineups are provably partial. The run recorded **no
truncation, no data-quality finding, and `families_truncated=[]`**. See §9 and §10.

Lineup evidence is **timing-unknown, and effectively retrospective**. The payload
carries no posting timestamp, and every request was issued months after the games
finished, so the `starter` flags are post-hoc box-score starters, not a pregame
posting. `is_confirmed` is `false` on all 478 snapshots and `confirmed_at` is NULL
on all 478 — correctly. This evidence **cannot prove confirmed pregame starters**:
there is no observation time earlier than the tipoff, no provider assertion that
the lineup was final before the game, and no correction history that would let a
pregame state be distinguished from a postgame one.

### Results — omitted from the manifest
The family was **not declared** and no `results` request was possible (nor needed —
§5). Result evidence that does exist through other authorized responses: final
scores in all 239 listing rows and all 239 single-game bodies; `mapped_status`
`final` on 239 schedule snapshots; quarter lines summing exactly to the final score
for 239/239; and `nba_team_statistics.points` matching the final score on 478/478
rows. Repair boundary: **offline only** — no new provider request, replay of
preserved bodies through the production normalizer with preserved observation time
and provenance (proven in §5).

## 9. Pagination and record limits

`pages_fetched = 3` is **correct but was mis-readable**. The counter covers
`LISTING_FAMILIES = {schedule, games}` only, by design
(`request_control.py:52-55`); a per-entity rich fetch is not a listing page. The run
made 1,437 provider requests across 7 endpoint families, so printing a bare
`pages … logical_total=3` beside `requests … logical_total=1437` invited exactly the
misreading the review brief anticipated. Repaired as a reporting change (§10); the
JSON key is unchanged so no checkpoint compatibility is affected.

Per-family verification:

| family | requests | rows | one page sufficed? | cursor advertised | cursor followed |
|---|---|---|---|---|---|
| games listing | 3 | 239 | no — 3 pages | 2 | **yes, both** |
| game single | 239 | 239 | yes | 0 | n/a |
| box_scores | 239 | 1,997 | yes (max 12 games/date) | 0 | n/a |
| stats | 239 | 8,526 | yes (max 39 rows) | 0 | n/a |
| advanced | 239 | 6,289 | yes (max 30 rows) | 0 | n/a |
| plays | 239 | 114,738 | yes (max 568 rows) | 0 | n/a |
| **lineups** | 239 | 5,125 | **no — 40 truncated at 25** | **40** | **NO** |

- **114,738 plays genuinely arrived one response per game.** `per_page=100` was
  sent but the provider returns the whole play list for a game regardless; max 568
  rows in a single response, no `next_cursor` on any of the 239.
- **`max_pages=8` and `max_records=1000` were never exercised and never bypassed.**
  The largest single response was 568 records (57% of `max_records`); no response
  reached 900. Zero truncation findings were raised by `_paginate`, correctly.
- **One family ignored a cursor.** `stats`, `advanced` and `plays` go through the
  bounded `_paginate` helper, which follows `meta.next_cursor`; `box_scores` does
  not paginate but *does* check for a cursor and records a truncation. `lineups`
  did neither. Under the review brief this is a **correctness blocker** for the
  lineups family, and it is treated as one: the lineups family is not accepted.

Pages and records are otherwise reported consistently at family and logical-run
level.

## 10. Data quality, capabilities, and confirmed defects

**All 480 DQ rows audited.** Every one is `DQ-CAP-NBA-001`, severity `note`,
entity type `capability`, all unresolved (correctly — they are standing capability
statements, not incidents). Distribution is **exactly 2 per run across all 240
runs**, matching the intended contract with no duplicate growth. The two notes are
`confirmed_pregame_starters` *unavailable* and `correction_timestamps`
*unsupported*.

No issue or blocking finding is hidden: there are **no** `DQ-NBA-STATUS-001`
(all statuses recognised), `DQ-NBA-SCHEDULE-001`/`-002` (all 239 `datetime` values
valid and consistent with `date`), `DQ-NBA-BOX-001` (all 239 box matches
unambiguous) or `DQ-NBA-LINEUP-001` (no silent lineup loss) rows — each absence is
independently corroborated by the corresponding table counts.

`provider_capabilities` holds 2,160 rows = 240 runs × 9 conditional capabilities:
`advanced_statistics` / `plays` / `quarter_lines` / `injuries` *supported*;
`lineups` / `substitutions` *best_effort*; `confirmed_pregame_starters`
*unavailable*; `correction_timestamps` *unsupported*; `historical_depth`
*provider_history_limited*. Confirmed pregame starters remain unavailable
(§8). Correction timestamps remain unsupported. Historical depth remains
unverified. **Substitution coverage is not inferred without observed events** —
14,414 substitution rows are all derived from real provider play rows. GOAT remains
**configured-not-verified** (`tier_verified=false`,
`tier_evidence_source="none"`): this process called no tier-gated probe capable of
establishing the subscribed tier, so the repository's tier-verification contract is
correctly not claimed as met.

**Grade vs acceptance.** The rule engine's grade is unchanged and was not lowered:
no rule fired, and none should have for the six accepted families. Execution
*acceptance* is a separate judgement, and it is where the lineups family fails. The
review brief's four candidate signals were assessed and three produced repairs:

### Confirmed defects and repairs

**D1 — `/v1/lineups` cursor silently discarded (correctness).** 40/239 games hold
partial lineups with no truncation and no durable finding. Repaired in
`nba_ingestor._fetch_all`: an advertised `next_cursor` now records a truncation, and
`_persist_lineups` writes a durable, sanitized **`DQ-NBA-LINEUP-002`** issue naming
the game (no cursor value, no body, no player names) so a later reader cannot
mistake 25 rows for a complete lineup. The cursor is deliberately **not** followed
here: the reviewed plan reserves exactly one lineups request per game, and following
it would exceed a bound this review has no authority to widen. Recovering the
missing rows needs a targeted live repair (§13).

**D2 — `results` unreachable from any NBA manifest (label blocker).** Added
`results` to `planning.NBA_RICH_FAMILIES` and `f1a._NBA_RICH`. Tests prove the
family now survives the include→family mapping, that declaring it adds **zero
contingents and zero request-cap** (NBA results are request-free), and that the
**already-executed March manifest is unaffected** — its plan hash still matches and
its manifest hash is still
`901cb9deaf3c5bf243f73ed60a820dd323933caea5dac7a45b69e01480f5ad3e` (which is also
the file's own SHA-256, and the hash the checkpoint is bound to). The existing
`test_month_manifests_regenerate_byte_identically` guard confirms both committed
manifests still regenerate byte-identically. **No manifest was broadened.**

**D3 — `TEAM_STATISTICS` capability wording overstated normalized support.**
Corrected in `providers/capabilities.py` (the declaration now states that this is
endpoint access, that the team block is identity plus a `players` array, that what
is persisted is identity + final score, and that aggregates must never be
synthesized from player rows), in the `nba_ingestor` module docstring, and in
`_normalize_box_team_lines`, whose docstring now records the measured contract
across all 478 team blocks. A regression test pins the normalizer's real output
shape.

**D4 — `pages_fetched` reads as a total page count.** The pilot report now prints
`listing_pages` with a documented `_PAGES_LABEL` constant explaining that the
counter covers `LISTING_FAMILIES` only.

**D5 — stored request parameters do not reconstruct the request that was sent
(evidence-fidelity).** Found by this review's own offline reconstruction, which
could not locate 717 of its 1,437 preserved responses. `raw_exchange.py` recorded
every parameter with `str(v)`, so a **repeated** query parameter was stored as the
stringified Python container:

```
sent   : /v1/stats?game_ids[]=18447686&per_page=100
stored : {"game_ids[]": "[18447686]", "per_page": "100"}
```

`"[18447686]"` does not round-trip to a URL. Every `/v1/stats`,
`/nba/v1/stats/advanced` and `/v1/lineups` response in the March corpus — 717 rows,
half the evidence — therefore carries provenance that does not identify its own
request. Since `response_content_hash` includes the params, the dedup identity was
also computed over a lossy encoding. Repaired: `RawExchange.request_params` is now
`dict[str, str | list[str]]` and a list/tuple is recorded as the list of element
strings it was actually sent as (`str`/`bytes` are explicitly not exploded).
Reproducer asserts the stored params replay to `httpx`'s own
`request.url.params.multi_items()`.

Note for future readers of the March corpus: those 717 rows predate the repair and
still hold the lossy form. Any replay tool must accept both encodings (this
review's did, once the defect was understood). The repair changes the content hash
of future list-parameter responses, so a fresh ingest will not dedupe against a
pre-repair row for those endpoints; both month databases are frozen, so nothing in
the existing corpus is affected.

Not repaired, reported only: an ingestor coverage truncation
(`NbaIngestResult.records_truncated` / `truncations`) does not propagate into the
pilot JSON or the checkpoint — `families_truncated` is driven solely by budget
exhaustion. The durable `DQ-NBA-LINEUP-002` row is the mitigation; wiring
coverage truncation through the pilot report is a separate change.

## 11. Database and provenance

| Check | Result |
|---|---|
| `PRAGMA integrity_check` | **ok** |
| Schema version | **17** (`MAX(schema_versions.version)`) |
| Migration records | **17** rows in `schema_versions` |
| WAL / SHM | `-wal` 0 B (fully checkpointed), `-shm` 32,768 B — no uncommitted state |
| Manifest binding | checkpoint `manifest_hash` == manifest file SHA-256 == `901cb9de…` |
| Plan binding | rebuilt plan hash == `manifest.computed_plan_hash()` |
| Checkpoint format | `f1a-checkpoint-v2`, `state=completed`, `schema_version=17` |
| Usage provenance | `f1-usage-provenance-v1`, `process_count=1`, known, not legacy-migrated |
| Completed identities | **240** (240 unique: 1 skeleton + 239 per-game) |
| `stage_game_ids` | 239 |
| Unresolved / recovered / failed / blocked | **0 / 0 / 0 / 0** |
| Scratch fingerprint | `d6c75901…` — content digest, matched on every resume |
| Raw responses | 1,437 rows, 1,437 distinct ids, 1,229 distinct content hashes (208 same-date box duplicates, fully attributed) |
| Ingestion runs | 240, `SUM(requests_made)` = 1,437 |
| Responses attached to a valid run | 1,437 / 1,437 (0 orphans) |
| Sanitized paths and headers | only `content-type`, `date`, `etag` stored; params `start_date`, `end_date`, `per_page`, `cursor`, `date`, `game_ids[]`, `game_id` (717 rows hold the lossy list encoding — D5) |
| Secrets | **0 hits** across all metadata columns |
| Injuries / MLB / sportsbook / Kalshi / weather rows | **0** (those tables are empty or absent) |
| Orphan references | 0 across all six observation tables (game-ref and raw-response) |
| Unauthorized date / family / provider | none — 31 March dates, 7 endpoints, `balldontlie` only |

Exact normalized row counts, read from the database rather than the execution JSON:

```
game_schedule_snapshots              239
nba_game_results                       0
nba_team_statistics                  478
nba_quarter_lines                  1,930
nba_player_statistics             14,815   (traditional 8,526 · advanced 6,289)
play_snapshots                   114,738
lineup_snapshots                     478
lineup_players                     5,125
provider_game_references             239
provider_team_references              30
provider_player_references           550
provider_team_identity_snapshots   6,474
provider_player_identity_snapshots 91,187
provider_capabilities              2,160
data_quality_issues                  480
ingestion_runs                       240
raw_responses                      1,437
```

(`leagues` 2, `teams` 60, `team_aliases` 311 are `db-init` seed data, not run output.)

## 12. Completed no-op resume, determinism and idempotency

**No-op resume — 4 runs (2 copies × 2 repeats), all identical:**

```
exit_code 0 · performed_new_work False · checkpoint_mutated False
database_mutated False · network_occurred False
initially_completed 240 · skipped_on_resume 240 · completed 0
unresolved 0 · recovered 0 · process_count 1
database SHA-256 byte-identical   True
checkpoint SHA-256 byte-identical True
logical totals preserved exactly: 1437/1437/1437/1437 · 0 failures · 0 retries
                                  3 listing pages · 305 throttles · 716.2137620006688 s
zero provider-client construction · zero authentication · zero DNS/socket/transport
zero pacing wait · sentinel trips: NONE
```

`BalldontlieClient.__init__` and `config.load_settings` stayed guarded throughout —
which is what makes "zero transport" provable rather than asserted: a resume that
tried to do work would have had to construct a client and would have tripped the
sentinel. The original database and checkpoint are byte-identical afterwards.

**Offline reconstruction and determinism.** Preserved raw responses (1,229 distinct
`(endpoint, params)` keys) were replayed through the real production client behind
an `httpx.MockTransport` into two fresh schema-v17 databases, varying raw-response
order, game order (reversed vs. seeded shuffle) and endpoint-family order, with
`results` additionally enabled. **All 1,437 preserved responses were served and
there were zero misses** — the mock fails closed, so nothing was invented and no
request could reach a socket. No missing family response was fabricated.

**Every normalized table reproduces the original execution exactly:**

| table | original | reconstructed |
|---|---|---|
| `game_schedule_snapshots` | 239 | 239 |
| `nba_team_statistics` | 478 | 478 |
| `nba_quarter_lines` | 1,930 | 1,930 |
| `nba_player_statistics` | 14,815 | 14,815 |
| `play_snapshots` | 114,738 | 114,738 |
| `lineup_snapshots` | 478 | 478 |
| `provider_team_identity_snapshots` | 6,474 | 6,474 |
| `provider_player_identity_snapshots` | 91,187 | 91,187 |
| `provider_game/team/player_references` | 239 / 30 / 550 | 239 / 30 / 550 |
| `nba_game_results` | 0 | **239** (results enabled) |
| `data_quality_issues` | 480 | **520** |

**Determinism: the two runs are semantically identical** across all 14 projections
— schedule observations, quarter lines, player statistics by group, plays *and
their ordering within each game*, lineups, identity snapshots, provider references,
DQ findings, and the replayed results — despite reversed vs. shuffled game order
and different endpoint-family order.

The 520 DQ rows are the 480 capability notes plus **exactly 40** new
`DQ-NBA-LINEUP-002` rows — an independent end-to-end confirmation that the lineup
cursor repair (§10 D1) fires on precisely the 40 games the raw evidence shows were
truncated, and on no others.

**Idempotency:** replaying a third time into the same database left every derived
observation table byte-stable. Only the three append-only *observation-time* logs
grew, by exactly one further set each — `data_quality_issues` 520 → 1,040,
`provider_team_identity_snapshots` 6,474 → 12,948,
`provider_player_identity_snapshots` 91,187 → 182,374. That is the documented
contract, not a defect: the identity uniqueness key is
`(provider, entity id, observed_at, content_hash)` with `observed_at` deliberately
excluded from the hash, and capability/DQ notes are recorded once per run, so a
replay at a later wall clock is a genuinely later observation.

The targeted results replay (§5) is idempotent in the stronger sense — it carries
the *preserved* observation time rather than a new one, so a second identical pass
inserted 0 rows.

## 13. Matching readiness — defects untouched

The three canonical-matching defects (same-name official-player order dependence,
non-idempotent team match decisions, missing canonical-ID propagation into
observation tables) were **not repaired** and production matching was **not run**,
so no misleading acceptance metric was produced.

Structured identity evidence present in the March corpus:

| | |
|---|---|
| Provider **team** references | 30 (all 30 NBA teams) |
| Provider **game** references | 239 (one per selected game, no duplicates) |
| Provider **player** references | 550 |
| Team identity snapshots | 6,474 |
| Player identity snapshots | 91,187 |
| References carrying a canonical id | **0 team · 0 game · 0 player** |
| Canonical `games` / `players` / `seasons` / `venues` rows | **0 / 0 / 0 / 0** |

**Are the identity observations sufficient for a later repaired matcher? Yes.**
Every provider entity is anchored by a stable provider id with an append-only
identity snapshot carrying full name, normalized name, abbreviation, city and
nickname (teams) or full/normalized name (players), each traceable to the exact raw
response that supplied it. Schedule snapshots carry `scheduled_start` for all 239
games — the field whose earlier loss blocked official NBA game matching — plus both
provider team ids. That is what an official-id bootstrap needs.

**Exact reason combined F1 matching cannot yet be accepted:** running matching today
would (a) resolve same-name players by traversal order, producing an arbitrary
accept/ambiguous split that differs between passes; (b) append a duplicate team
decision per pass, so the ledger is not idempotent and any "accepted" count is a
function of how many times matching ran; and (c) leave every observation table's
canonical id column NULL, so the coverage figure a combined review would quote
would describe `provider_*_references` alone and would not reflect anything a
downstream feature or label consumer could actually read. Any acceptance metric
computed before those three are fixed would be unreproducible and misleading.

## 14. Status

- **The NBA execution review is ACCEPTED for six of seven families** — games,
  box/quarters, traditional statistics, advanced statistics and plays are complete,
  internally consistent and fully attributed. **The `lineups` family is NOT
  accepted**: 40 of 239 games hold provably partial data because a provider cursor
  was discarded.
- **A targeted offline results repair is required** (§5, conclusion B) and has been
  proven feasible. **A targeted live repair is also required — for lineups only**
  (§15). Neither was executed.
- **Team-stat reporting and capability language were corrected** (§7, §10 D3). The
  reported "zero `nba_team_statistics`" was wrong — there are 478 rows — but the
  provider genuinely supplies no team aggregate line, so the capability wording,
  not the row count, was the defect.
- **Preserved evidence is now self-replayable** (§10 D5). Half the March corpus
  (717 responses) stored request parameters that could not reconstruct their own
  request; the capture was repaired, and any tool reading the pre-repair rows must
  accept both encodings.
- **Matching remains blocked** by the three separately documented defects, which
  this review did not touch.
- **Combined F1 review has not begun.**
- **F1 remains incomplete.**
- **F2 remains unauthorized.**

## 15. Required repairs, exact scope

### Offline — **DONE** (separately authorized, applied 2026-08-05)
Replay the 239 preserved `/v1/games/{id}` responses through
`SqliteNbaResultRepository.append`, carrying each response's stored `received_at`
as `observed_at`/`ingested_at` and its stored `raw_response_id`/`body_hash` as
provenance, into the March scratch database. Expected: 239 inserted, 0 corrections,
0 new raw responses, 0 provider requests. Proven in §5 on a copy — and since
**applied exactly as specified**: 239 inserted, 0 corrections, 0 new raw
responses, 0 provider requests, checkpoint byte-identical. See
`F1_NBA_2026_03_RESULTS_REPAIR.md`.

### Targeted live repair — lineups only (PREPARED OFFLINE 2026-08-06; NOT executed)

> Everything buildable without the provider now exists: `fetch_lineups` accepts a
> cursor, the planner expresses a bounded continuation contingent (40 targets ×
> ≤ 8 pages = **320** semantic requests, **640** attempts at `max_retries=1`), and
> `pilots/f1/nba_lineups_2026_03_continuation.manifest.json` is committed
> (manifest `a8979cd1…`, plan `3c0ec01c…`). It binds to this execution by source
> manifest hash `901cb9de…`, source plan hash `e29ef60c…`, source database
> fingerprint `b5b475a4…`, 239 selected games, 40 targets and target-set digest
> `03d3df93…`. Cursors are **not** committed; they are re-derived from the
> protected corpus at execution time and the run refuses if the digest has moved.
> The recovery writes only to `data\f1_nba_lineups_2026_03_recovery.db` and its
> own checkpoint.
>
> That preparation has since been **independently reviewed and ACCEPTED**
> (2026-08-07, `NBA_LINEUP_CONTINUATION_PREPARATION_REVIEW.md`), which repaired
> five defects — the executor persisted nothing, `--execute` was not wired,
> conflicts resolved by traversal order, cursor typing was inconsistent, and every
> exception was blamed on the provider — and exercised the now fully wired
> production path over all 40 targets through a mock transport.
>
> **The live run itself still requires a fresh provider audit and explicit
> authorization**, then an independent execution review and a separate offline
> merge.

| | |
|---|---|
| Scope | the **40** games whose `/v1/lineups` response advertised a `next_cursor` |
| Requests | continuation pages only — `GET /v1/lineups?game_ids[]=<id>&per_page=25&cursor=<preserved next_cursor>`, repeated while a cursor is returned |
| Bound | ≤ 8 pages per game (`max_pages`), so **≤ 320 requests**; first page already held |
| Manifest boundary | a **new** manifest is required. `planning.py` reserves `per_parent_max=1` for the `lineups` contingent, so paginated lineups changes the plan hash. The executed March manifest and its hash must not be edited. |
| Checkpoint boundary | a **new** checkpoint. The existing `f1_nba_2026_03.ckpt` is `completed` and bound to manifest `901cb9de…`; it must not be reopened or mutated. |
| Client change | `BalldontlieClient.fetch_lineups` needs a `cursor` parameter (it has none), and `_fetch_all` must route lineups through the bounded `_paginate` helper. |
| Preconditions | fresh provider audit on the day, clean tree, explicit user authorization |

Until that runs, the 40 affected games must be treated as partial lineups —
`DQ-NBA-LINEUP-002` now makes that durable for future runs, though the March rows
predate the repair and carry no such note. `DQ-NBA-LINEUP-002` remains the
**historical** signal that a first page was partial; the recovery run raises its
own `DQ-NBA-LINEUP-R00x` findings rather than rewriting it.

---

## Appendix — validation

Run entirely offline, with no real sleeps.

- **Zero-network proof:** 23 guards, 14/14 adversarial probes failed closed.
- **Original-artifact fingerprints:** every preserved artifact retains its starting
  SHA-256; the NBA database and checkpoint were re-verified after every step.
- **New tests:** `sports_quant/ingest/tests/test_f1_nba_month_review.py` — **12
  tests**, each a reproducer that failed before its repair, plus non-regression
  guards for the cursor-free lineup path, the untouched box-score cursor
  behaviour, scalar/string query parameters, and the executed March manifest hash.
  `test_f1b_reporting.py` updated for the `listing_pages` label (and asserts the
  old bare `pages` label is gone).
- **Suites:** NBA execution-review, result-family, quarter/final-score consistency,
  team-stat parser/capability, pagination and cursor, traditional/advanced stats,
  play ordering/truncation, lineup normalization/timing, DQ/capability, checkpoint
  no-op resume, reconstruction/determinism, redaction, CLI/reporting, and MLB
  non-regression — all green.

```
git diff --check                       clean
ruff check .                           All checks passed
mypy . --no-incremental                no issues (295 source files)
pytest -q                              2020 passed, 2 skipped, 0 failed (441 s)
schema init x2                         v17, 17 migration rows, 47 tables, integrity ok (both)
v16 -> v17 migration x2                v16 (0 identity tables) -> v17 (2), 17 rows,
                                       integrity ok, re-apply idempotent (both runs)
non-editable wheel smoke               22/22 checks passed
staged-file / secret audit             8 files, all source/docs/tests; no db, ckpt,
                                       raw response, log, wheel, env or graphify output;
                                       no secret-shaped literal
artifact fingerprints                  41/41 byte-identical to their starting SHA-256
```

The wheel smoke ran the **installed** package (resolved from the fresh venv's
`site-packages`, with the CWD outside the repository so the working tree was not
importable) and covered NBA evidence loading, the completed no-op resume,
quarter/final-score verification, the team-stat capability conclusion,
pagination/cursor handling, offline deterministic reconstruction, the zero-network
sentinel and schema v17.

No real sleeps were used anywhere: pacing was an injected recording no-op and the
`time.sleep`/`asyncio.sleep` guards refused any positive duration.
