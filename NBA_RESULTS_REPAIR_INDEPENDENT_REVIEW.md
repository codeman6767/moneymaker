# Independent review — offline NBA March-2026 results repair (`e09f546`)

Offline correctness, provenance and isolation audit of the results replay applied
to the executed BALLDONTLIE NBA `2026-03-01..2026-03-31` month corpus.

**Zero provider requests were made.** No lineup continuation, no canonical
matching, no combined F1 review, no F2 work.

> **Verdict: ACCEPTED.** The applied repair is correct, fully reproducible from
> the frozen pre-repair evidence, provenance-honest and perfectly isolated —
> copies rebuilt independently from that evidence reproduce the committed 239
> rows and the provenance record **exactly, field for field**.
>
> Five latent hardening defects were found by adversarial probing and repaired.
> **None of them affected the applied March data**, which was re-verified
> byte-consistent after the fixes; each was a way a *future* corpus could have
> been repaired wrongly or silently.

---

## 1. Review boundary and zero-network proof

A process-level sentinel installed **23 guards** — DNS (`getaddrinfo`,
`gethostbyname`, `gethostbyname_ex`, `create_connection`), non-loopback
`socket.connect`/`connect_ex`, `httpx.get`/`request`, sync and async httpx
transports, `httpx.Client.send`/`AsyncClient.send`, `requests.request`/
`Session.send`, `urllib.request.urlopen`/`OpenerDirector.open`,
`build_readonly_client`, both provider client constructors,
`config.load_settings`, `f1a._default_client_factory`, and `time.sleep` /
`asyncio.sleep` on any positive duration. **Fourteen adversarial probes all
failed closed.**

The guards were installed **before** `sports_quant.cli` and the production
modules were imported, and this was verified rather than assumed:
`cli.load_settings is config.load_settings` → `True`, i.e. the CLI bound the
**guarded** loader, so any attempt to read configuration (and therefore the NBA
API key) raises.

**The sentinel was never tripped** across the pre-repair reproduction, candidate
reconstruction, dry-runs, both independent applies, correction-semantics tests,
atomicity injection, concurrency, provenance audit and MLB checks. Two guards
were relaxed only to *build fixtures* behind an `httpx.MockTransport`
(`build_readonly_client`, `BalldontlieClient.__init__`), and the guards were
re-installed before the repair itself ran in every such case.

Structural confirmations from source inspection: the repair module imports **no
provider client and no settings**, the CLI handler **never calls
`load_settings`** and has **no default `--db` fallback**, and the module contains
**zero `INSERT INTO`** statements — results are written only through
`SqliteNbaResultRepository.append`.

## 2. Artifact protection

All **42** fingerprinted artifacts were unchanged at review start, and unchanged
again at the end. Every evidence path is a regular file; **no symlinks**. The
frozen and working databases are **distinct filesystem objects**
(`…:11821949022237643` vs `…:21392098230465272`, both `nlink=1`). The frozen copy
is git-ignored. The original database was never opened writable by this review.

Two independent review copies were taken **from the frozen pre-repair evidence**
with the SQLite backup API (`data/f1_nba_results_review_a.db`, `…_b.db`), both
`integrity_check = ok`, v17, **`nba_game_results = 0`**, distinct inodes,
git-ignored.

| artifact | SHA-256 | state |
|---|---|---|
| Working (repaired) database | `39064fa219f4eb66…` | unchanged by the review |
| Frozen pre-repair database | `ddc2a09188375a14…` | **byte-identical**, still 0 results |
| Executed checkpoint | `c17a375daa89e3f0…` | **byte-identical** |
| Execution JSON / metadata / stderr | `32d7ac29…` / `ce08e9a7…` / `e3b0c442…` | **byte-identical** |
| MLB June database / checkpoint | `802a7d76…` / `70bbc7c9…` | **byte-identical** |

## 3. Pre-repair state, reproduced from the frozen database

`integrity_check = ok`, schema **v17**, **`nba_game_results = 0`**,
`game_result_snapshots = 0`, **239** selected provider game references, **239**
preserved `/v1/games/{id}` responses, **239** final statuses, quarter lines for
all **239** games, and the PIT dataset returns **zero** NBA rows. No typed result
row existed before the repair.

## 4. Command contract

Required and enforced: explicit `--db`, explicit `--manifest`,
`--provider balldontlie`, `--league nba`, explicit `--date-range`, explicit
`--offline`.

Every refusal was exercised against the real working database and **left it and
the checkpoint byte-identical**:

| attempt | outcome |
|---|---|
| wrong provider / wrong league | refused (exit 2) |
| wrong date range | refused — manifest range mismatch |
| wrong manifest (MLB June) | refused — provider/league mismatch |
| database not bound to the March plan (MLB corpus) | refused — foreign provider in `raw_responses` |
| target aliasing the frozen database | refused |
| `--offline` withheld | refused |
| nonexistent database | refused |
| `--base-url`, `--api-key`, `--url`, `--timeout`, `--proxy` | refused by argparse (exit 2) |

Path protection is genuinely identity-based, not spelling-based: a plain alias, a
parent-relative alias (`…/sub/../file.db`) and a **hard link** to a protected
database were all refused, as was a **read-only** target. All refusals occur
before any writable database access.

## 5. Candidate accounting and determinism

Re-derived independently from the frozen database with the production normalizer:

```
239 selected game IDs          0 missing selected games
239 candidate responses        0 extra non-selected games
239 unique game identities     0 duplicate ambiguous candidates
0 tied finals                  0 asymmetric or missing scores
0 invalid team IDs             0 missing observation timestamps
0 missing raw-response IDs or body hashes
```

**Determinism:** twelve randomized traversals of the raw-response set produced
**one** candidate serialization and **one** semantic digest
(`eca7f38a5bd19954…`), identical to the command's own. Equal-time conflicting
responses fail closed rather than being resolved by insertion order (§6 below).

## 6. Dry-run and independent apply

Dry-run against pre-repair copy A: 239 candidates, 239 valid, **0** rejected,
**0** inserts, **0** corrections, **0** raw-response additions, **0** DQ
additions, **0** checkpoint mutation, **0** table changes, copy **byte-identical**
afterwards, digest matching the independent reconstruction, zero network. A
repeated dry-run was stable.

Applied independently to copies **A** and **B**: each inserted **239** results, 0
corrections, 0 new raw responses, 1 provenance note.

**The decisive result:** comparing all seventeen result columns —
`provider_game_id, provider, home_points, away_points, period, winning_side,
mapped_status, result_detail, is_correction, observed_at, ingested_at, run_id,
raw_response_id, raw_response_hash, content_hash, provider_timestamp,
published_at` —

- copy A ≡ copy B, and
- **copies ≡ the committed working database, exactly.**

The provenance records are identical too, and there is exactly one per database.

## 7. Correction semantics

Verified through the production repository:

| behaviour | result |
|---|---|
| First valid observation | inserts exactly one row, `is_correction = 0` |
| Identical replay | inserts nothing |
| Later changed score | appends a correction; history intact, never overwritten |
| Earlier observation arriving late | appended in its own place; the newest observation stays latest |
| Equal-time conflicting scores | repair refuses the corpus |
| Final → final revision | append-only |
| Final → non-final regression | recorded as a correction (points went backwards) |
| Detail/wording-only change | appended as a new observation, **not** flagged a correction |
| Tied final / missing score / negative score | refused |
| Duplicate content at the same instant | idempotent, one row |

MLB semantics are untouched: the repair module never names
`game_result_snapshots`, `team_game_statistics`, `player_game_statistics` or
`roster_snapshots`, and is hard-wired to `nba`/`balldontlie`.

## 8. Score consistency — derived independently

For all 239 games, comparing the preserved body, the persisted result, the
quarter-line sums and the team-statistics points:

```
scores agree                                 239/239
quarter sums agree                           239/239
team points agree                            478/478
orientation agrees (vs schedule team ids)    239/239
winner agrees                                239/239
result.period == payload period == max qtr   239/239
ties 0 · missing/odd quarter sets 0
regulation games 230 · overtime games 9 · maximum period 5
overtime counted once (8 rows regulation, 10 rows OT)
```

## 9. Observation-time and PIT assessment

Every one of the 239 rows carries the source response's own `received_at` as both
`observed_at` and `ingested_at`, its own `raw_response_id` and `body_hash`, and
the run that actually fetched it (**239/239**). No `provider_timestamp`, no
`published_at` and no correction was fabricated.

The historical information boundary is preserved honestly, and this is
measurable: observation times span
`2026-08-04T22:12:12Z .. 2026-08-04T22:35:52Z` — inside the **original execution
window** — while `created_at` (row-write time) is `2026-08-05`. The corpus
therefore records that these facts were *observed* when the provider actually
answered and *written* when the repair ran; neither is disguised as the other.

**PIT labels remain 0** in all three states:

```
frozen (pre-repair)   results=0    canonical_games=0  labels=0
working (post-repair) results=239  canonical_games=0  labels=0
copy A (post-repair)  results=239  canonical_games=0  labels=0
```

Provider-only references did **not** become sufficient for dataset admission. A
regression test pins this so the builder cannot later be softened.

## 10. Provenance assessment

The single `DQ-NBA-RESULT-REPLAY-001` note (severity `note`, entity type
`repair`, `run_id` NULL) carries command, contract version, tool version,
manifest hash (equal to the manifest file digest), plan hash, source response
count (239), inserted count (239), the semantic digest, and
`network_occurred: false`. Exactly one exists; a reapply adds none. It contains
no secret-shaped material and **no absolute filesystem path**. It does not claim
a provider ingestion process.

**On the split design** (database holds the durable semantic identity and counts;
report and local receipts hold the pre/post file hashes and run timestamps): this
is accepted. A database cannot contain its own post-write hash, so that half is
necessarily external, and the in-database half is sufficient on its own to
identify what was replayed, from which plan, to what count, with which digest.
No reproducibility or integrity defect was demonstrated, so it was not
redesigned.

## 11. Idempotency and atomicity

Idempotency: a second apply inserts zero results, adds no correction, no
provenance duplicate and no raw response, and leaves the database
**byte-identical**; a post-apply dry-run reports already complete.

**Atomicity under injected failure** — four points, each with a production seam
patched to raise:

| injection point | partial results | partial provenance | readable | retry completes |
|---|---|---|---|---|
| before transaction | 0 | 0 | ok | 5/5 |
| during result insertion (row 3 of 5) | 0 | 0 | ok | 5/5 |
| during provenance insertion | 0 | 0 | ok | 5/5 |
| immediately before commit | 0 | 0 | ok | 5/5 |

Every case rolled back completely, the database stayed readable with
`integrity_check = ok`, the pristine fixture was unchanged, and a retry produced
the full expected state.

**Concurrency:** four simultaneous repairs against one database produced
`inserted=5, 0, 0, 0` — exactly 5 results, 1 provenance note, `integrity_check =
ok`, and zero duplicate observations.

## 12. Isolation

Comparing the frozen pre-repair database with the repaired working database:

```
nba_game_results        0 -> 239
data_quality_issues   480 -> 481
```

**Exactly two tables changed.** The table set itself is unchanged. Sixteen
unrelated tables — raw responses, ingestion runs, schedule snapshots, quarter
lines, team and player statistics, plays, lineups, both identity-snapshot tables,
all three provider-reference tables, provider capabilities and canonical `games`
— were verified **content-identical**, not merely count-identical. Pre-existing
DQ rows are untouched; only the one note was added.

Checkpoint and accounting: checkpoint byte-identical; `reserved_attempts`,
`transport_starts`, `responses_received` still 1,437; `pages_fetched` 3;
`throttle_events` 305; `process_count` 1 with one process entry; 240 completed
identities; 239 `stage_game_ids`; state `completed`; manifest hash unchanged.
`ingestion_runs` is still **240** with `SUM(requests_made) = 1437` — **no
fictitious repair run**, and no run mentions the repair command. Every result row
cites a run that already existed.

## 13. Defects found and repaired

None affected the applied March data; all five are latent hardening gaps, each
now covered by a reproducer that failed before its fix.

**R1 — Non-integer provider scores were silently coerced.** `home_team_score =
110.7` was persisted as **110**, and `"110"` as `110`. The shared `_opt_int` is
deliberately permissive so live ingestion survives odd payloads; a *repair* whose
entire justification is "invent nothing" must not quietly round or reinterpret a
value the provider never sent. Added `_exact_score`, which re-reads both scores
strictly from the payload and accepts only a genuine `int` (a `bool` is not one).
**Deliberately scoped to the repair module** — `_opt_int` is unchanged, so live
NBA ingestion and MLB are untouched (asserted by test).

**R2 — Negative scores were accepted.** `-5` was persisted with the winner
computed from it. Now refused.

**R3 — Non-positive `period` was accepted.** `period = -3` and `0` were
persisted. Now refused; a genuinely absent period remains fine.

**R4 — A deeply nested body escaped as a raw `RecursionError`.** `_payload_of`
caught `JSONDecodeError`/`AttributeError`/`TypeError` but not the stack overflow
a 60,000-level body causes, so a corrupt preserved response crashed the command
with a traceback instead of failing closed. Now caught and refused.

**R5 — Selected games with no usable response passed silently.** Run against a
skeleton-only corpus (4 selected games, 0 preserved single-game responses) the
repair returned `results_inserted = 0`, `rejected = 0` and
`already_complete = True` — which an operator would reasonably read as "the
results are already in place". Added a coverage guard: a selected game with
neither a valid candidate nor an existing result row is now counted
(`games_without_response`) and **refuses the run**, in dry-run as well as apply.
A fully covered corpus, and a second pass where every game already has a result,
are unaffected.

**Re-verification after the fixes is the important part:** copies rebuilt from
frozen evidence under the hardened command still reproduce the committed 239 rows
and provenance record **exactly**, with the same semantic digest. The hardening is
additive; it changed nothing about the real corpus.

## 14. Status

**The offline results repair is independently validated — ACCEPTED.**

- The working database has **239/239 typed provider results**.
- **PIT labels remain 0/239.**
- **Forty lineup games remain partial.**
- **Three matching defects remain open.**
- **Combined F1 review has not begun.**
- **F1 remains incomplete.**
- **F2 remains unauthorized.**
