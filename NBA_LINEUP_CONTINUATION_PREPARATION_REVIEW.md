# Independent review — NBA lineup-continuation preparation (`56823a4`)

Offline correctness, safety, determinism and **executability** audit of the
bounded March-2026 lineup-continuation recovery.

**Zero provider requests were made.** No provider audit, no continuation
execution, no lineup merge, no canonical matching, and neither the combined F1
review nor F2 was begun.

> **Verdict: ACCEPTED**, after repairing five confirmed defects — one of which
> meant the recovery would have produced **no durable evidence at all**, and
> another of which meant the prepared path **could not actually execute**.
>
> The derivation, manifest, planner, ordering and source-fingerprint work was
> correct as committed and is confirmed independently. The executor was not.

---

## 1. Review boundary and zero-network proof

23 process guards installed **before** the CLI and continuation modules were
imported — DNS, non-loopback sockets, sync and async HTTP transports, `requests`,
`urllib`, both provider client constructors, settings/authentication loading,
retry sleeps and the project transport entry points. **14/14 adversarial probes
failed closed**, and `cli.load_settings is config.load_settings` confirmed the
CLI bound the *guarded* loader rather than the real one.

Every provider interaction in this review used `httpx.MockTransport`. Where the
production client had to be exercised, only `httpx.AsyncClient.send`,
`BalldontlieClient.__init__` and `build_readonly_client` were restored — DNS,
sockets and the raw sync/async transports stayed guarded throughout, so nothing
could leave the process regardless. The sentinel was never tripped.

## 2. Evidence protection

42 artifacts fingerprinted; **all byte-identical at the end**. Every original path
is a regular file, none is a symlink, and the recovery paths
(`data/f1_nba_lineups_2026_03_recovery.db`, `…​.ckpt`, execution JSON) **did not
exist before this review and do not exist after it**. The March corpus was opened
`mode=ro` only. All test databases and checkpoints were temporary.

| artifact | SHA-256 |
|---|---|
| March working database | `39064fa219f4eb66…` |
| March checkpoint | `c17a375daa89e3f0…` |
| Frozen pre-results database | `ddc2a09188375a14…` |
| Month manifest | `901cb9deaf3c5bf2…` |
| MLB June database | `802a7d76e42d08dc…` |

## 3. Target derivation — independently recomputed

Recomputed with **raw SQL**, not the product helper, then cross-checked against
it:

```
selected games                 239
anchored first pages           239        missing first pages          0
already complete (null cursor) 199        duplicate/ambiguous pages    0
CONTINUATION TARGETS            40
page-one rows per target       {25}       teams per target          {2}
page-one players per target    {25}       starters per target      {10}
distinct starting cursors       40        all cursors int         True
```

The product derivation agrees exactly. **Twelve randomized traversals produced
one canonical serialization and one digest**, `03d3df938a13cc9b…`, matching the
committed manifest. No target is selected because of traversal order.

## 4. Cursor typing — **defect found and repaired**

The chain was inconsistent. `fetch_lineups`/`_validate_cursor` accepted **opaque
text**, while `next_cursor` and `_next_cursor_of` read **integers only**. A text
cursor could therefore be *sent* but never *read back*: the paginator would see
`None`, treat a live chain as finished and truncate silently — the exact defect
cursor support exists to fix, reintroduced one layer down.

Every preserved March cursor is an integer, and BALLDONTLIE's documented
`meta.next_cursor` is an integer, so the contract was **narrowed to integer-only**
across the code and the documentation rather than claiming support the read side
never had. `0` is a valid cursor (not a falsy absence); `bool`, `float`, `str`,
`bytes`, containers and blanks are refused before any request. Cursor identity is
stable through checkpoint and resume because the cursor is re-derived from the
protected corpus, not carried in the checkpoint.

## 5. Canonical ordering

`_canonical_id_key` is `(len(id), id)`, which is **injective over strings** and
therefore a true total order. Verified: `"9" < "10"`, `"1" ≠ "01"`, equal-length
ids order numerically, very large ids order correctly, non-numeric ids are
handled, and eight randomized input orders produce one sorted output. **No key
collision between distinct ids**, so no target, checkpoint identity or digest can
depend on Python's stable-sort tie-breaking.

## 6. Manifest and planner

Independently validated: canonical JSON that round-trips byte-identically, a
**duplicate JSON key is rejected** (`ManifestError`), provider/league/stage/family
correct, family exactly `("lineups",)`, schema v17, `max_retries=1`, rate 60/min
against a 600/min tier max, recovery paths new, **no cursor value and no
secret-shaped field committed**.

Bindings verified against their true sources: `source_manifest_hash` equals the
month manifest file digest, `source_plan_hash` equals the month plan-body digest,
`source_selected_games` is 239, `target_count` is 40, `max_continuation_pages`
is 8.

Caps derived independently: **40 × 8 = 320** semantic requests and
**320 × (1 + 1) = 640** attempts — matching `estimated_requests_max` and
`request_cap`. Every semantic input moves the plan and manifest identity, and the
month plan hash is **unchanged** at `e29ef60cc1ecc613…` because the `recovery`
key is omitted from ordinary plan bodies. Regeneration is byte-identical.

## 7. Source-database fingerprint

The bound value is the scratch classifier's **logical content digest** (schema
plus every row), computed over a read-only, WAL-aware connection — not the file's
bytes. Verified:

- the committed `b5b475a45f1076f7…` **reproduces from the current corpus**;
- it is stable across repeated read-only opens and does not modify the file;
- it differs from the file-byte hash and from the frozen pre-results database
  (`d6c75901d952b301…`), so a different corpus cannot pass;
- a logical backup copy reproduces it exactly (so harmless re-packing is fine);
- inserting a single row moves it.

## 8. Request path

First-page requests are byte-for-byte unchanged (`game_ids[]` + `per_page`, no
`cursor`). A continuation sends the cursor **exactly once** as a scalar; repeated
`game_ids[]` serialize canonically; stored provenance replays to
`request.url.params.multi_items()`; page one and a continuation hash differently,
so two pages of one game cannot collapse into a single stored response. The key
stays in the `Authorization` header and never enters the URL or stored metadata.
Only `/v1/lineups` is reachable, and the other endpoints are unaffected.

## 9. Page validation and exception classification — **defect found and repaired**

Payload/`data`/game-id/`meta` handling and all stop conditions behave correctly.
But the executor caught bare `Exception` around the fetch, so **a `TypeError`, a
`KeyError` or a `sqlite3.OperationalError` in our own code was reported as
`DQ-NBA-LINEUP-R006` "provider terminal failure"** — blaming the provider for our
bug and hiding it behind a resumable state.

Repaired: only `ProviderError` and `httpx.HTTPError` are provider terminal
failures. Everything else propagates as itself. The client is still closed
exactly once on every path, including when an error escapes.

A related distinction is now explicit: `BudgetExhausted` is the runner's
controlled truncation, not a provider failure — the target is recorded incomplete
with `stop_reason="budget_exhausted"` and the exception re-raised so the pilot
runner truncates normally.

## 10. Persistence — **blocker found and repaired**

**The executor persisted nothing.** After a successful continuation the recovery
database still contained zero raw responses, zero lineup snapshots, zero lineup
players, zero references, zero identity observations, zero DQ rows and zero runs.
The entire run existed only as in-memory `ContinuationOutcome` objects; the
module never referenced a single repository. A live recovery would have spent 320
provider requests and produced **no durable evidence**.

Repaired using the production repositories — no ad hoc SQL. Each target now
writes, in one connection:

| evidence | where |
|---|---|
| Raw continuation responses | `raw_responses` (one row per page, including the pages of a chain that ended badly) |
| **Requested** cursor | `raw_responses.request_params_json` |
| **Returned** cursor | the stored body's `meta.next_cursor` |
| Page ordinal, whole chain, stop reason | `DQ-NBA-LINEUP-R009` `detail_json`, with the page-one raw id/hash it extends |
| Provider game/team/player references | `provider_*_references` |
| Team and player identity observations | `provider_*_identity_snapshots` (via `IdentityRecorder`) |
| Lineup snapshots and players | `lineup_snapshots` / `lineup_players` |
| Findings | `data_quality_issues` |
| Recovery run | `ingestion_runs` (`command=nba-lineup-continuation`, `operation=lineup_continuation`) |

Schema stays **v17** — no migration was needed, because page ordinal and the full
cursor chain are recoverable from the stored request params and bodies, and the
chain note records them explicitly for a reader.

## 11. Merge contract with page one

Defined and tested now; **the merge itself was not performed**. Page one stays in
the March corpus and is never refetched. `(team, player)` deduplication spans page
one and every continuation page; identical overlaps collapse; a continuation
alone is demonstrably **not** a lineup (25 + 4 rows merge to 29, while the
continuation alone yields 4). A player appearing for two teams produces two
distinct identities rather than a silent overwrite. A merged lineup yields exactly
two team snapshots, and `is_confirmed` is `0` on every snapshot — a continuation
never claims confirmed pregame starters. Starter and position remain provider
observations.

## 12. Conflict handling — **defect found and repaired**

The committed code kept "the first" contradictory observation, where *first* meant
whichever page was folded first. Demonstrated directly: the same evidence in
opposite order stored `{"position": "G", "starter": True}` one way and
`{"position": "F", "starter": False}` the other. The conflict was flagged either
way, but the **retained value was chosen by arrival**.

Repaired with a provenance ordering: every row is sorted by
`(page_ordinal, provider row id, team, player)` before folding, so "first" means
the earliest page and lowest provider row id — a property of the evidence.
Verified order-independent across page order, row order within a page, and eight
randomized multi-page shuffles, with the conflict still reported.

## 13. Data-quality findings

Nine stable codes: `R001` repeated cursor, `R002` page limit, `R003` wrong game,
`R004` malformed page, `R005` empty page with cursor, `R006` terminal failure,
`R007` silent normalization loss, `R008` conflicting player, `R009` chain
provenance. Severity is `issue` for an incomplete target and `note` for a complete
one. Messages carry game and page identity, are sanitized (no body, no player
name, no header, no credential), and attach to the anchor raw response and the
recovery run. **`DQ-NBA-LINEUP-002` — the historical signal that the original
March page was partial — is untouched**; recovery findings are a separate family.
An incomplete target is visible both in the checkpoint (never yielded, so never
marked complete) and in the report.

## 14. Checkpoint and resume

One checkpoint unit per target game. Verified across four scenarios: an
interrupted run leaves the stalled target incomplete and resumable; a resume
re-walks **only** what was unfinished (targets that already completed are skipped
with zero transport and zero refetch); a completed no-op resume performs zero
requests, constructs no client, and leaves both database and checkpoint
**byte-identical**; and v2 provenance totals close. Budget is carried across
processes by the runner, so no process receives a fresh 640-attempt allowance.

## 15. Request accounting

From the full mocked run: attempts = transport starts = responses = successes =
**90**, retries 0, blocked 0, 429s 0, `pages_fetched` 0 (a continuation is not a
listing page — correctly distinguished from response count), all within the 320
semantic and 640 attempt caps. **No first-page request occurred and only
`/v1/lineups` was contacted.** Pacing is an injected recording no-op in tests, so
no real sleep is taken and normal pacing is never reported as rate limiting.

## 16. CLI executability — **blocker found and repaired**

The committed `--execute` branch performed its authorization and path checks and
then returned a **hard-coded refusal**: no client, gate, recovery database or
checkpoint runner existed. The preparation could not honestly be called
executable.

Now fully wired, and exercised end to end **only through `MockTransport`**:
manifest validation → source binding (fingerprint + digest + counts) →
authorization → explicit recovery paths and alias/symlink refusal → key-presence
check → recovery-database preparation → request gate → production client →
`run_pilot` with the continuation executor → persistence → JSON and human
reporting.

Every bound is checked **before a client exists**, proven by injecting a factory
that raises if called: missing recovery paths, a target aliasing the source
corpus, and a missing key all refuse without constructing anything.

## 17. Authentication and settings isolation

Offline validation loads **no settings and no key** — verified by making
`config.load_settings`, `cli.load_settings` and `BalldontlieClient.__init__` all
explosive and running the default path to a clean exit. The live branch loads only
the normal read-only settings, checks the key by **presence only** (never printed,
hashed or logged — asserted against every column of the recovery database and all
output), and fails **before the recovery database is created**. Tier is `goat`.
Tests inject settings and client factories, so no real credential is ever read.

## 18. Recovery-database contract

The CLI creates a **brand-new schema-v17 recovery database offline, before any
client exists**. An existing database that already holds continuation evidence is
refused unless `--resume` is passed with a checkpoint bound to the same manifest;
`--resume` against a database that does not exist is refused; a schema mismatch is
refused. The March source is never migrated or altered. `integrity_check` passes.
No checkpoint is written before validation succeeds.

## 19. Determinism and concurrency

Two runs over shuffled target orders persist **identical lineup semantics**.
Cursor chains, page identities, findings and completed/incomplete sets are
order-independent. Execution is intentionally sequential — one target at a time
through `run_pilot` — and the sole-writer protections plus per-unit checkpointing
mean a second concurrent writer cannot interleave partial evidence.

## 20. Full mocked production-path simulation

All 40 real targets, through the production CLI, manifest validation, source
binding, recovery database, checkpoint, gate, production client behind
`MockTransport`, executor, persistence and reporting:

```
targets completed            40/40        continuation requests        90
first-page requests           0           endpoints contacted     {/v1/lineups}
attempts / starts / responses / successes   90 / 90 / 90 / 90
retries 0 · blocked 0 · 429s 0 · listing pages 0      caps: 320 / 640 respected
raw_responses 90 · ingestion_runs 40 · lineup_snapshots 45 · lineup_players 89
provider_game_references 40 · team refs 2 · player refs 65
identity snapshots  team 90 / player 90       chain-provenance notes 40
recovery database: schema v17, integrity_check ok
completed resume: 0 requests, database and checkpoint byte-identical
interrupted run: 5 targets completed, 1 incomplete; resume refetched none of the 5
protected March database and checkpoint unchanged; no artifact written to data/
```

## 21. Defects found and repaired

| # | Defect | Severity |
|---|---|---|
| **D1** | The executor **persisted nothing** — a live recovery would have spent 320 requests and produced no durable evidence | **blocker** |
| **D2** | `--execute` was **not wired**; it could only return a hard-coded refusal | **blocker** |
| **D3** | Contradictory player rows resolved by **traversal order** | correctness |
| **D4** | Cursor typing inconsistent: text accepted on write, unreadable on read → silent truncation | correctness |
| **D5** | Every exception classified as a **provider** terminal failure, hiding our own bugs | correctness |

All five are fixed, each with a reproducer that failed first. Re-verified after
the repairs: derivation, digest, manifest identity and every ordinary plan hash
are unchanged.

## 22. Status

**ACCEPTED** — the continuation implementation and manifest are independently
validated and the production execution path is **fully wired but not executed**.

- **No provider audit occurred.**
- **No continuation request occurred.**
- **Recovery artifacts from a live run do not exist.**
- **Forty March games remain partial.**
- **PIT labels remain 0/239.**
- **Three matching defects remain open.**
- **F1 remains incomplete.**
- **F2 remains unauthorized.**

The next authorized step is a fresh BALLDONTLIE provider audit, then the
explicitly authorized live continuation run — no code change should be needed
between this review and that run.
