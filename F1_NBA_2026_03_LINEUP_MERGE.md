# F1 NBA March 2026 — offline lineup-continuation merge

Offline merge of the independently accepted NBA lineup-continuation recovery into
a **protected copy** of the March corpus. The original March corpus, its
checkpoint, the recovery database and the recovery checkpoint were never opened
writable and are byte-identical afterwards.

**No provider request was made. This merge is NOT yet independently reviewed.**

Applied at `HEAD a03b9a6`.

---

## 1. Authorization and boundary

23 process-level guards were installed before importing any merge, ingestion or
provider module: DNS (`getaddrinfo`, `gethostbyname`, `gethostbyname_ex`),
`socket.create_connection`, non-loopback `socket.connect`/`connect_ex`, sync and
async httpx transports and `Client.send`/`AsyncClient.send`, `httpx.get`/`request`,
`requests.request`/`Session.send`, `urllib.request.urlopen`/`OpenerDirector.open`,
`build_readonly_client`, `BalldontlieClient.__init__`, `MlbStatsApiClient.__init__`,
`config.load_settings`, `f1a._default_client_factory`, and `time.sleep`/`asyncio.sleep`.

**14/14 adversarial probes blocked**; `cli.load_settings is config.load_settings`
is `True`; **0 guard trips** across every merge, dry run, idempotency, atomicity,
determinism and PIT run. The NBA API key was never loaded, no provider client was
constructed and no provider audit was run. The merge command reads only evidence
already on disk and reports `network_occurred=false`.

| Input | Identity |
|---|---|
| Source March corpus | `data/f1_nba_2026_03_scratch.db`, content fingerprint `b5b475a4…428a6d8170` |
| Reviewed recovery | `data/f1_nba_lineups_2026_03_recovery.db`, content fingerprint `4168789b…d10d4494` |
| Continuation manifest | `a8979cd1feb8a72377c5581ec9a9ad8baaa89e0b8cff0fffe7da6bb52cfbac36` |
| Continuation plan | `3c0ec01ce7ca6c1a8c0cfd99eec665278023053bd7fcdf70b144c2f0e96dfef7` |
| Target count / digest | 40 / `03d3df93…d927f737` (re-derived with the production `derive_targets`) |
| Execution review | `NBA_LINEUP_CONTINUATION_EXECUTION_REVIEW.md` — **ACCEPTED**, 40/40 merge-eligible |
| Destination | `data/f1_nba_2026_03_lineups_merged.db` (Git-ignored, never committed) |

## 2. Merge-eligibility reconstructed independently

Every reported continuation fact was recomputed from the two corpora before any
write. All checks passed:

- 40 target games; 40 continuation responses; all HTTP 200
- every target has page-one evidence with **exactly 25 rows**
- requested cursor equals the page-one `next_cursor` in 40/40
- every chain terminates (no further `next_cursor`); no page-limit or budget truncation
- no wrong-game row; no malformed body

| Quantity | Required | Reconstructed |
|---|---|---|
| Literal `data=[]` terminal pages | 19 | **19** |
| Non-empty pages | 21 | **21** |
| Raw continuation rows | 32 | **32** |
| Normalized rows | 32 | **32** |
| Unique new rows | 32 | **32** |
| Rejected | 0 | **0** |
| Lost to normalization | 0 | **0** |
| Within-page duplicates | 0 | **0** |
| Overlapping page one | 0 | **0** |
| Contradicting page one | 0 | **0** |

## 3. Why the merge appends revisions — and the exact expected identity

This is the one place where the merge necessarily departs from the naive
arithmetic in the brief, so it is derived rather than assumed.

`lineup_snapshots` and `lineup_players` are **hard append-only**: both carry
`BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT)`. An existing
page-one snapshot therefore **cannot** gain members in place — a merge that tried
would be rejected by the database itself. The naive identity
"post-merge players = source + 32" presumes exactly that in-place extension and
is not achievable under this schema.

The schema instead models observation-time **revisions**:
`UNIQUE (game_ref_id, provider_team_id, observed_at, content_hash)`, an index on
`(game_ref_id, provider_team_id, observed_at)`, and `append_transition`, which
collapses an unchanged re-observation and appends a changed one. As-of selection
(`latest_as_of`) then takes the latest observation at or before the cutoff, so a
later revision unambiguously supersedes page one. §8's caveat — "unless the schema
intentionally models observation-time revisions and downstream selection
unambiguously chooses the correct observation" — is therefore satisfied, and no
design blocker exists.

So for each affected `(game, team)` the merge appends **one** revision snapshot
whose membership is page one plus that team's reviewed additions, observed at the
continuation's own receipt instant. The proven identity:

```
affected (game, team) pairs                     = 22
NEW lineup_snapshots  = affected pairs          = 22        478 -> 500
NEW lineup_players    = SUM(page_one_team + continuation_team) = 294   5125 -> 5419
distinct recovered (game, team, player) observations           = 32
```

The 32 recovered observations are still exactly 32. They arrive inside 22 revision
snapshots that necessarily **restate** the page-one members of those same teams,
which is why the row count grows by 294 rather than 32. Source counts were read
from the protected corpus as authoritative (478 / 5,125), never hard-coded.

## 4. Destination copy

Created from the source with SQLite's **online-backup API** (never a raw file
copy, so committed WAL content cannot be omitted).

- distinct filesystem object from both the source and the recovery; not a symlink
- `PRAGMA integrity_check` → `ok`; schema **v17** with **17** migration rows
- **all pre-merge logical table counts matched the source exactly**
- pre-merge destination SHA-256 `036c0d01339c8124f63bce2c6ed23a1ef31ee2dfa18c282259ec19961f7b6b17`, 355,012,608 bytes

## 5. Dry run

Run five times with connections reopened between runs so traversal order varies.

| Measure | Value |
|---|---|
| Targets / eligible | 40 / 40 |
| Conflicts / rejected rows | 0 / 0 |
| Recovered observations | 32 |
| Snapshots to append | 22 |
| Player rows to append | 294 |
| Raw responses to copy | 40 |
| Provenance rows | 40 |
| `network_occurred` | false |
| Semantic digest | `eb596818496bda977c214f57094b544b6e84436814ffa09d34d0cb819aa83bce` |

Digest and the full report body were **identical across all five runs**, and the
destination was byte-identical before and after: the dry run mutates nothing.

## 6. Applied merge

Applied exactly once, inside a single transaction, to
`data/f1_nba_2026_03_lineups_merged.db` — never to the source corpus or either
checkpoint.

| Counter | Applied |
|---|---|
| Targets processed | 40 |
| Snapshots appended | 22 |
| Snapshots unchanged | 0 |
| Player rows appended | 294 |
| Recovered observations | 32 |
| Raw responses copied | 40 |
| Ingestion runs copied | 40 |
| Provenance rows | 40 |
| Conflicts / rejected | 0 / 0 |
| `network_occurred` | false |

```
lineup_snapshots     478 -> 500   (+22)
lineup_players      5125 -> 5419  (+294)
raw_responses       1437 -> 1477  (+40)
ingestion_runs       240 -> 280   (+40)
data_quality_issues  481 -> 521   (+40)
```

The 40 continuation raw responses and their 40 recovery ingestion runs are copied
so the reviewed provenance chain stays traversable inside the merged copy and so
`lineup_snapshots.raw_response_id` (NOT NULL FK) and
`data_quality_issues.raw_response_id` resolve. Provider game *references* are
deliberately **not** copied — the destination already holds one per game under
`UNIQUE (provider, provider_game_id)`, and each revision anchors on the
destination's own reference.

Durable merge provenance is one `note`-severity `DQ-NBA-LINEUP-M001` row per
target (40), each carrying the merge contract version, source and recovery
fingerprints, manifest and plan hashes, target count and digest, page-one and
continuation evidence ids and hashes, the requested cursor, the added-player
count, the semantic digest, `network_occurred=false` and the code version. The
merge is recorded as a merge, **not** disguised as a provider ingestion run.

### Per-game merged distribution

| game | page one | cont. raw | added | merged | team split | starters | conflicts | rejected |
|---|---|---|---|---|---|---|---|---|
| 18447686 | 25 | 2 | 2 | 27 | 13/14 | 10 | 0 | 0 |
| 18447691 | 25 | 0 | 0 | 25 | 13/12 | 10 | 0 | 0 |
| 18447696 | 25 | 0 | 0 | 25 | 14/11 | 10 | 0 | 0 |
| 18447698 | 25 | 1 | 1 | 26 | 14/12 | 10 | 0 | 0 |
| 18447705 | 25 | 2 | 2 | 27 | 13/14 | 10 | 0 | 0 |
| 18447706 | 25 | 1 | 1 | 26 | 13/13 | 10 | 0 | 0 |
| 18447715 | 25 | 1 | 1 | 26 | 13/13 | 10 | 0 | 0 |
| 18447716 | 25 | 0 | 0 | 25 | 11/14 | 10 | 0 | 0 |
| 18447719 | 25 | 0 | 0 | 25 | 12/13 | 10 | 0 | 0 |
| 18447721 | 25 | 0 | 0 | 25 | 13/12 | 10 | 0 | 0 |
| 18447722 | 25 | 2 | 2 | 27 | 12/15 | 10 | 0 | 0 |
| 18447729 | 25 | 4 | 4 | 29 | 14/15 | 10 | 0 | 0 |
| 18447745 | 25 | 0 | 0 | 25 | 12/13 | 10 | 0 | 0 |
| 18447747 | 25 | 0 | 0 | 25 | 12/13 | 10 | 0 | 0 |
| 18447749 | 25 | 1 | 1 | 26 | 12/14 | 10 | 0 | 0 |
| 18447764 | 25 | 2 | 2 | 27 | 13/14 | 10 | 0 | 0 |
| 18447768 | 25 | 0 | 0 | 25 | 11/14 | 10 | 0 | 0 |
| 18447770 | 25 | 1 | 1 | 26 | 13/13 | 10 | 0 | 0 |
| 18447783 | 25 | 0 | 0 | 25 | 13/12 | 10 | 0 | 0 |
| 18447788 | 25 | 3 | 3 | 28 | 14/14 | 10 | 0 | 0 |
| 18447809 | 25 | 1 | 1 | 26 | 13/13 | 10 | 0 | 0 |
| 18447818 | 25 | 1 | 1 | 26 | 12/14 | 10 | 0 | 0 |
| 18447820 | 25 | 0 | 0 | 25 | 13/12 | 10 | 0 | 0 |
| 18447826 | 25 | 1 | 1 | 26 | 13/13 | 10 | 0 | 0 |
| 18447850 | 25 | 0 | 0 | 25 | 11/14 | 10 | 0 | 0 |
| 18447853 | 25 | 0 | 0 | 25 | 13/12 | 10 | 0 | 0 |
| 18447854 | 25 | 0 | 0 | 25 | 12/13 | 10 | 0 | 0 |
| 18447858 | 25 | 0 | 0 | 25 | 14/11 | 10 | 0 | 0 |
| 18447859 | 25 | 1 | 1 | 26 | 12/14 | 10 | 0 | 0 |
| 18447871 | 25 | 1 | 1 | 26 | 14/12 | 10 | 0 | 0 |
| 18447880 | 25 | 0 | 0 | 25 | 12/13 | 10 | 0 | 0 |
| 18447886 | 25 | 1 | 1 | 26 | 12/14 | 10 | 0 | 0 |
| 18447889 | 25 | 0 | 0 | 25 | 11/14 | 10 | 0 | 0 |
| 18447898 | 25 | 0 | 0 | 25 | 12/13 | 10 | 0 | 0 |
| 18447903 | 25 | 1 | 1 | 26 | 13/13 | 10 | 0 | 0 |
| 18447904 | 25 | 0 | 0 | 25 | 12/13 | 10 | 0 | 0 |
| 18447908 | 25 | 2 | 2 | 27 | 12/15 | 10 | 0 | 0 |
| 18447918 | 25 | 2 | 2 | 27 | 13/14 | 10 | 0 | 0 |
| 18447920 | 25 | 1 | 1 | 26 | 12/14 | 10 | 0 | 0 |
| 18447921 | 25 | 0 | 0 | 25 | 13/12 | 10 | 0 | 0 |

**Distribution: 19 games at 25, 13 at 26, 6 at 27, 1 at 28, 1 at 29** — exactly the
distribution the independent execution review derived. Added observations total
**32**, and each game's additions equal its continuation raw-row count.

### Invariants

- exactly **two** logical team snapshots per game in **all 239** games
- exactly **ten** starter-marked players per game in **all 239** games
- no duplicate `(game, team, player)` membership anywhere
- no player on both teams of a game
- `is_confirmed` remains **false** on every snapshot — no confirmed-pregame-starter claim
- the **199** originally cursor-complete games are logically unchanged

## 7. Provenance

All 32 recovered observations are traceable end to end: merged membership →
recovery normalized observation → recovery raw-response id **and** hash → recovery
ingestion run → requested continuation cursor → source page-one response id and
hash → target game → accepted manifest/plan binding. Verified for all 32; zero
problems.

Page-one provenance is untouched: **80/80** original page-one snapshots for the 40
targets keep their original `observed_at` and `raw_response_id`, no original
raw-response body was rewritten, and the merged copy carries **260 distinct
observation times** — the two acquisition stages remain visibly distinct rather
than flattened into one.

## 8. Integrity

**Idempotency.** A second apply appended 0 snapshots and 0 player rows, reported
22 snapshots unchanged, copied 0 raw responses and 0 ingestion runs, wrote 0
provenance rows, left the semantic content and every table count identical, and
made no provider request. A third dry run reports all 40 targets merged with the
same digest.

> A defect was found and fixed here before the final apply. The plan originally
> read the destination's **latest** snapshot as the revision base, so a replay
> folded the continuation rows into the base of the next revision and stacked
> them. The plan now anchors on the **earliest** (page-one) observation, making the
> planned membership a pure function of the two source corpora, so replays produce
> byte-identical content and `append_transition` collapses them. The destination
> was rebuilt from scratch after the fix; the merged copy described here was
> produced by a single clean apply.

**Atomicity.** Failures injected at five points — before the first target, mid
target, between target games, before the provenance insert, and immediately before
commit — on temporary copies. In every case the failure propagated, the
destination rolled back to exactly the pre-merge state (478 / 5,125 / 1,437 / 240 /
481 / 239), and the retry afterwards completed exactly with the same digest. No
destination was ever left reporting success with a partial merge.

**Determinism.** Three fresh destinations were created from the source and merged
while varying revision order, outcome order and player-row order. All three
appended 22 / 294, produced the same semantic digest
`eb596818…9aa83bce`, and are **semantically identical** to the applied merge in
snapshots, memberships and table counts.

**Dataset.** The real PIT historical dataset builder was run against both
databases:

| Database | Accepted labels | Canonical games | Match decisions |
|---|---|---|---|
| Source March corpus | **0/239** | 0 | 0 |
| Merged protected copy | **0/239** | 0 | 0 |

The lineup merge alone cannot bypass the canonical-game requirement, provider-only
ids never become acceptable labels, and the builder was not weakened.

**Isolation.** Every protected artefact is byte-identical after the merge: the
March database and checkpoint, the March execution JSON, the recovery database and
checkpoint, the recovery execution JSON/meta/stderr/progress logs, the
continuation manifest and the execution-review report.

```
merged destination sha256 : 223c1185a0c7c1afb35e04ae8e31e50de4f7e62e69073808eb08cf75cafad02a
merged destination size   : 355,270,656 bytes
…merged.db-wal            : present, 0 bytes (clean close)
…merged.db-shm            : present, 32,768 bytes
```

WAL/SHM state is reported as found; nothing was checkpointed merely to simplify
hashing.

## 9. Validation

All offline; no real sleeps.

```
git diff --check                       clean
ruff check .                           All checks passed
mypy . --no-incremental                Success: no issues found in 306 source files
pytest -q                              2272 passed, 2 skipped, 0 failed (492 s)
schema init x2                         v17, 17 migration rows, 47 tables, integrity ok (both)
v16 -> v17 migration x2                v16 (0 identity tables) -> v17 (2), 17 rows,
                                       integrity ok, re-apply idempotent (both runs)
non-editable wheel smoke               46/46 checks passed
staged-file / secret audit             8 files, all source/docs/tests; no db, ckpt,
                                       raw response, log, wheel, env or graphify output;
                                       no secret-shaped literal
```

Review harnesses, all under the zero-network sentinel with **0 guard trips**:

| Harness | Result |
|---|---|
| Fingerprints + eligibility + expected identity | 0 failures |
| Destination baseline + 5 randomized dry runs | 0 failures |
| Merged-game, unrelated-data and provenance validation | 0 failures |
| Idempotency, atomicity, determinism, PIT, isolation | 0 failures |
| Non-editable wheel smoke | 0 failures |

`sports_quant/ingest/tests/test_nba_lineup_merge.py` adds **32 tests** covering
source/recovery binding, destination-copy creation, dry-run zero mutation,
40-target eligibility, the row-addition identity, empty continuation pages, the
page-one + continuation union, no-overlap behaviour, duplicate idempotency,
contradictory-overlap refusal, opposing-team conflict, the two-team and
ten-starter invariants, observation-time provenance, recovery raw-response
traceability, no confirmed-starter upgrade, atomic rollback, digest determinism,
protected-path/symlink/alias refusals, original-database immutability, PIT
emptiness without canonical ids, and secret/redaction safety.

The wheel smoke ran the **installed** package (fresh venv `site-packages`, CWD
outside the repository) and covered the merge CLI help, dry run, protected-copy
creation, apply to a temporary copy, idempotent second apply, three conflict
refusals, atomic rollback, PIT emptiness, protected-artefact immutability, the
zero-network sentinel (14/14 probes blocked) and schema v17.

---

## Status

- The offline lineup merge was applied **to a protected copy only**.
- The **original March execution corpus remains unchanged**.
- The **recovery evidence remains unchanged**.
- Forty previously partial lineups are now represented completely in the protected
  merged copy under the reviewed provider cursor evidence.
- **This merge is NOT yet independently reviewed.**
- **The NBA lineup family must not be called finally accepted until an independent
  merge review is complete.**
- **PIT labels remain 0/239.**
- **Three canonical-matching defects remain open.**
- **The combined F1 review has not begun.**
- **F1 remains incomplete. F2 remains unauthorized.**
