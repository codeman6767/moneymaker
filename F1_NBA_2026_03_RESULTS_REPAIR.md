# F1 NBA March-2026 — offline `results` repair

Population of the `nba_game_results` family for the executed BALLDONTLIE (GOAT)
NBA `2026-03-01..2026-03-31` month corpus, by replaying **preserved raw
responses** through the production normalizer.

**Zero provider requests were made.** No lineup recovery, no canonical matching,
no checkpoint modification, and the combined F1 review was not begun.

> **Result: repaired.** `nba_game_results` went 0 → **239**, every row score- and
> provenance-consistent, the executed checkpoint byte-identical, and the frozen
> pre-repair database preserved. **Usable point-in-time labels remain 0/239** —
> the results gap is closed, the canonical-matching gap is not.

---

## 1. Authorization boundary

This task applies only the **offline** repair that
`F1_NBA_2026_03_EXECUTION_REVIEW.md` §5 proved feasible (its conclusion **B**).
Explicitly out of scope and not performed: the targeted **live** lineup
continuation run for the 40 partially-fetched games, canonical matching and
canonical-ID propagation, any modification of the executed checkpoint, and the
combined F1 coverage/depth review.

This repair is **not** marked independently reviewed. That is a separate step.

## 2. Zero-network proof

A process-level sentinel installed **23 guards** — DNS (`getaddrinfo`,
`gethostbyname`, `gethostbyname_ex`, `create_connection`), non-loopback
`socket.connect`/`connect_ex`, `httpx.get`/`request`, sync and async httpx
transports, `httpx.Client.send`/`AsyncClient.send`, `requests.request`/
`Session.send`, `urllib.request.urlopen`/`OpenerDirector.open`, the project's
`build_readonly_client`, both provider client constructors,
`sports_quant.config.load_settings`, `f1a._default_client_factory`, and
`time.sleep`/`asyncio.sleep` on any positive duration. **Fourteen adversarial
probes all failed closed.**

The sentinel was installed **before** `sports_quant.cli` was imported, so the CLI
bound the guarded `load_settings`: any attempt to read configuration — and
therefore any attempt to reach the NBA API key — would have raised. **The
sentinel was never tripped**, in the dry-run, the apply, the second apply, the
third dry-run, or any verification pass.

No provider audit was run. No client was constructed. No credential was loaded.
Inputs were exactly: the NBA March SQLite database, its own preserved
`raw_responses` rows, the committed manifest, and the review report.

The command is structurally incapable of reaching a provider: it accepts no URL,
no key and no network option, and argparse refuses `--base-url`, `--api-key`,
`--url` and `--timeout`. It has **no settings fallback** for `--db` — resolving a
default database path would mean loading configuration, so the path is always
explicit.

## 3. Pre-repair state

| | |
|---|---|
| Working database | `data/f1_nba_2026_03_scratch.db` |
| SHA-256 | `421c9f9a4566265e8493f053190b3c46c9f52813593ac9d7a6e30d16bf47a1a8` |
| Size | 354,844,672 B · WAL 0 B (fully checkpointed) · SHM 32,768 B |
| `integrity_check` | ok · schema **v17** · 17 migration rows · journal `wal` |
| `nba_game_results` | **0** |
| `provider_game_references` | 239 |
| Preserved `/v1/games/{id}` responses | 239 |
| `data_quality_issues` | 480 |

Every one of the 239 preserved single-game responses was verified to carry the
full set of fields the replay needs — nothing was assumed:

```
normalizes           239/239      received_at          239/239
provider_game_id     239/239      raw_response_id      239/239
home_team_id         239/239      body_hash            239/239
away_team_id         239/239      content_hash         239/239
home_score           239/239      run_id               239/239
away_score           239/239      status = final       239/239
provider_date        239/239      winner decisive      239/239
provider_datetime    239/239      in the selected set  239/239

duplicate game responses  0     games missing a response      0
responses not selected    0     tied finals                   0
```

**No response required a provider lookup**: the final score, both team
identities, the status and the period are all present in bodies already stored.

## 4. Frozen evidence copy

Created **before** any write, with the SQLite **online-backup API** from a
`mode=ro` source handle — not a raw filesystem copy, which can omit committed
content still resident in the WAL and so freeze a state that never existed:

| | |
|---|---|
| Path | `data/f1_nba_2026_03_pre_results_repair.db` (git-ignored) |
| SHA-256 | `ddc2a09188375a14f72d7f2c91fdc99312d45ebec322f7bfca848f7c38af9940` |
| Size | 354,844,672 B · WAL 0 B |
| `integrity_check` | ok · schema **v17** |
| `nba_game_results` | **0** — the pre-repair state exactly |
| Logical table counts | identical to the source |
| Identity | distinct inode from the source; regular file; not a symlink |

The source SHA-256 was recomputed immediately after the backup and was
**unchanged**. The frozen copy is byte-identical at the end of this task, and the
repair command refuses to run against it (`--forbid-path`, checked by
device+inode identity, not by path spelling).

## 5. Replay method

A narrow production command, `repair-nba-results-from-raw`
(`sports_quant/ingest/results_repair.py`), wired into the CLI. It is deliberately
**not** a general raw-response execution framework: it replays one family, for
one league, from one committed manifest.

It requires all of: explicit `--db`, explicit `--manifest`, `--provider
balldontlie`, `--league nba`, explicit `--date-range`, and an explicit
`--offline` acknowledgement. It supports `--dry-run`, `--json` and human output,
and repeatable `--forbid-path`.

Rows are produced by the **production** normalizer
`nba_ingestor._normalize_game` and written by the **production** repository
`SqliteNbaResultRepository.append`. There is no ad hoc INSERT into
`nba_game_results` anywhere in the module.

**It refuses**, before opening the database for writing: a different manifest, a
different date range, a different provider, a different league, a database not
bound to the manifest's plan (schema version, response provider, run
league/command, or any scheduled game outside the range), a database with
preexisting conflicting result observations, a symlinked or non-writable target,
a target aliasing a protected path, and any network-shaped option.

**Determinism.** Responses are grouped by provider game id and emitted in
canonical id order, so the outcome never depends on row or payload order. A
`semantic_result_hash` digests the normalized set.

**Ambiguity fails closed.** Two responses for one game that disagree are refused;
two that disagree **at the same observation time** are refused with a distinct
reason, because no correction rule can order them. Nothing is chosen by insertion
order.

## 6. Exact provenance contract

For every replayed row:

| field | source |
|---|---|
| `provider` | `balldontlie` |
| `provider_game_id` | the preserved response's own game id |
| `home_points` / `away_points` | the response's `home_team_score` / `visitor_team_score` |
| `winning_side` | derived from those two, refused if tied |
| `period` | the response's `period` |
| `mapped_status` | `final` (refused otherwise) |
| `observed_at` | **the preserved response's own `received_at`** |
| `ingested_at` | the same preserved instant |
| `raw_response_id` | the preserved response's own id |
| `raw_response_hash` | the preserved response's own `body_hash` |
| `run_id` | **the original ingestion run that actually fetched that response** |
| `is_correction` | decided by the repository's existing semantics |

Nothing is fabricated. Stamping the replay wall clock as `observed_at` would have
claimed the corpus learned each result months after it actually did, silently
breaking every point-in-time guarantee built on `observed_at`; the replay
therefore carries the original instant forward, and inventing a later
`ingested_at` was rejected for the same reason.

These rows were **not** fetched as a newly authorized live family, and they are
not presented as such — see the durable marker in §10.

## 7. Dry-run

```
repair-nba-results-from-raw  DRY-RUN  NBA 2026-03-01..2026-03-31  (offline)
  binding    manifest=901cb9deaf3c5bf2… plan=e29ef60cc1ecc613… schema=v17
  evidence   raw_responses=1437  single_game=239  selected_games=239
  normalize  candidates=239  valid=239  rejected=0
  results    before=0  inserted=0  unchanged=0  after=0  corrections=0
  isolation  database_mutated=False  checkpoint_mutated=False
             raw_responses_inserted=0  provenance_notes=0
  offline    network_occurred=False  provider_client_constructed=False
  digest     semantic_result_hash=eca7f38a5bd19954a51bb33791a2d614…
```

Database and checkpoint SHA-256 **unchanged** by the dry-run. Sentinel trips:
none. Recorded under git-ignored
`data/f1_nba_2026_03_results_repair_dryrun.json`.

The dry-run produced exactly 239 unambiguous valid rows, so the apply proceeded.

## 8. Applied replay

Applied **once**, to `data/f1_nba_2026_03_scratch.db`:

```
results inserted            239
results unchanged             0
corrections appended          0
raw responses inserted        0
provenance notes written      1
results before / after      0 / 239
semantic_result_hash        eca7f38a5bd19954a51bb33791a2d61438a181d7f44f30e1f3f245cfc95994d3
                            (identical to the dry-run digest)
network_occurred            False
provider_client_constructed False
checkpoint_mutated          False
sentinel trips              none
started / finished          2026-08-05T21:51:51.056808Z / 21:51:51.128583Z
```

**Exactly two tables changed**, measured against the frozen pre-repair copy:

```
nba_game_results        0 -> 239
data_quality_issues   480 -> 481   (the single offline-replay provenance note)
```

Schedule observations, quarter lines, team statistics, player statistics, plays,
lineups, identity observations, provider references, capabilities, ingestion runs
and raw responses are **all unchanged**. No canonical game id was invented.

Recorded under git-ignored `data/f1_nba_2026_03_results_repair_applied.json`.

## 9. Score, quarter and team-point consistency

Verified independently of the repair, straight from the preserved bodies:

| check | result |
|---|---|
| Result scores == preserved provider final | **239/239** |
| Quarter-line sums == preserved provider final | **239/239** |
| Team-statistics points == result points | **478/478** |
| Home/away orientation vs the schedule's provider team ids | **239/239** |
| Winner correct | **239/239** |
| Provenance preserved exactly | **239/239** |
| Tied or invalid outcomes | **0** |
| Asymmetric or missing quarter sets | **0** |
| `result.period` == max persisted quarter period | **239/239** |

* **Regulation-only games: 230.**
* **Overtime games: 9.** Maximum period **5** (regulation 4 + 1 OT); no double
  overtime occurred in March. Quarter rows per game are 8 for regulation and 10
  for overtime games — overtime is included once, never double-counted.
* **Correction history:** 0 rows flagged `is_correction`, 0 games with more than
  one result observation, 239 distinct `observed_at` values, every one of which
  is the `received_at` of the response the row cites. All `mapped_status` values
  are `final`.

Quarter lines were used as an **independent consistency check only**; the
authoritative value is the provider's own final score in the preserved response,
and the repository's own semantics remain the sole correction authority.

## 10. Idempotency

The **second** identical apply:

```
results inserted             0        provenance notes written   0
results unchanged          239        already_complete        True
corrections appended         0        database_mutated       False
raw responses inserted       0        checkpoint changed     False
semantic_result_hash    unchanged     sentinel trips          none
database SHA-256        39064fa219f4eb66…  (byte-identical to after the first apply)
```

A **third** dry-run reports `already_complete=True`, `results_before ==
results_after == 239`, `rejected=0`, and the same semantic hash.

## 11. Checkpoint and accounting isolation

The executed month checkpoint is **byte-identical**:
`c17a375daa89e3f0f8ace2e6e3dffd965f8428b178127cb9b7c44bde6471300b`, before and
after every dry-run, apply and verification pass.

Because this is an offline data repair, the historical execution's accounting was
left completely alone. No provider request counter was incremented, no
provider-process entry was appended to the checkpoint's v2 usage provenance
(`process_count` remains 1), no new network work is claimed, no completed-unit
identity changed, and the executed manifest hash is unchanged. The command never
opens the checkpoint at all.

**Offline-repair provenance** is recorded in two places, neither of which is a
fictitious provider ingestion process. No `ingestion_runs` row was created.

*In the database* — one `data_quality_issues` row, severity `note`, rule
`DQ-NBA-RESULT-REPLAY-001`, entity type `repair`, entity id
`2026-03-01..2026-03-31`, whose `detail_json` is:

```json
{"command": "repair-nba-results-from-raw",
 "contract_version": "nba-results-offline-replay-v1",
 "tool_version": "sports_quant 0.1.0",
 "manifest_hash": "901cb9deaf3c5bf243f73ed60a820dd323933caea5dac7a45b69e01480f5ad3e",
 "plan_hash": "e29ef60cc1ecc613d014b700aa6fbe147f83b70e5a37fd59067041d0f3092c97",
 "source_single_game_responses": 239,
 "results_inserted": 239,
 "semantic_result_hash": "eca7f38a5bd19954a51bb33791a2d61438a181d7f44f30e1f3f245cfc95994d3",
 "network_occurred": false}
```

*In this report and the git-ignored run records* — additionally the start and
finish timestamps (`2026-08-05T21:51:51.056808Z` → `21:51:51.128583Z`), the
zero-network proof, and the pre- and post-repair database hashes (a database
cannot contain its own post-write hash).

## 12. Original-artifact and isolation audit

| artifact | outcome |
|---|---|
| Frozen pre-repair database | **byte-identical** `ddc2a09188375a14…` |
| Executed checkpoint | **byte-identical** `c17a375daa89e3f0…` |
| Execution JSON | **byte-identical** `32d7ac293a33e4b9…` |
| Execution metadata | **byte-identical** `ce08e9a733ed8ab5…` |
| Sanitized stderr log | **byte-identical** `e3b0c44298fc1c14…` (0 B) |
| MLB June evidence, F1B skeleton/rich evidence, matching copies and reports, both F1 month manifests, all four F1B manifests, all committed review reports, development corpus | **all byte-identical** |
| **Total** | **40 of 41 fingerprinted artifacts unchanged; the only change is the intended working database** |

**Repaired working database**

| | |
|---|---|
| SHA-256 | `39064fa219f4eb66f37e26f567a8f42d7df278236aa70e42264a600b07a5d135` |
| Size | 355,012,608 B |
| WAL / SHM | **0 B** / 32,768 B — no uncheckpointed committed data |
| `integrity_check` | **ok** |
| Schema | **v17**, 17 migration rows |
| `nba_game_results` | **239** |
| `data_quality_issues` | 481 |

A secret sweep over `raw_responses` (endpoint, params, headers),
`data_quality_issues` (description, detail) and `nba_game_results` found **no**
authorization, API-key, cookie, bearer, token or secret-shaped material. No
database, checkpoint, report, raw response, execution log or local repair JSON is
staged; every new `data/` artifact is git-ignored.

## 13. Remaining blockers

**Labels — still 0/239.** Measured with the real dataset builder against the
repaired database:

```
selected games                     239
final-status games                 239
typed result observations          239   <- repaired
score-consistent results           239
canonical games                      0
references carrying a canonical id   0
historical-dataset PIT labels        0   <- unchanged
```

The results-family gap is **repaired**. The canonical-matching gap is **not
repaired**. `build_historical_dataset` reads canonical `games` and requires a
game↔reference correspondence at the cutoff; matching has not run, so no
canonical game exists and the label count is still zero. **239/239 typed provider
results does not mean 239/239 usable point-in-time labels.**

The dataset builder must continue returning zero accepted NBA labels until
canonical game matching and canonical-ID propagation are fixed. **It must not be
weakened to accept provider-only ids** — a regression test now asserts the label
count stays zero after this repair precisely so that shortcut cannot be taken
quietly.

**Lineups — still 40 games short.** 40 of 239 `/v1/lineups` responses advertised
a `next_cursor` that the single bounded per-game request discarded. Recovery
needs a separately authorized **targeted live** continuation run (≤ 8 pages per
game, ≤ 320 requests, a `cursor` parameter on `fetch_lineups`, a **new** manifest
because the `lineups` contingent is `per_parent_max=1`, and a **new**
checkpoint). Not performed here.

**Matching — three defects still open** and untouched by this task: same-name
official-player order dependence, non-idempotent team match decisions, and
missing canonical-ID propagation into the observation tables.

## 14. Acceptance verdict

The offline results repair is **applied and self-consistent**: 239/239 typed
result observations, every score agreeing with three independent sources, exact
provenance preservation, byte-identical checkpoint and frozen evidence,
idempotent on repeat, and zero provider contact.

**This repair has not been independently reviewed.** That is a separate,
explicitly authorized step. Combined F1 review has not begun. **F1 remains
incomplete and F2 remains unauthorized.**
