# NBA March 2026 lineup merge — independent review

Independent, entirely offline review of the offline lineup-continuation merge
committed at `d7c19df`. Every reported number was derived from raw SQL over the
protected corpora rather than accepted from the merge's own output, and the
central reconstruction deliberately does **not** call the production merge planner.

**Verdict: ACCEPTED WITH LIMITATION.** The merge is reproducible,
provenance-complete, idempotent, atomic, deterministic and PIT-safe. One
blocker-class implementation defect was found and repaired; it did not affect the
merged database, which is byte-identical to the committed one. The limitation is
about **historical availability**, not about the merge.

No provider request was made. No matching was performed.

---

## 1. Boundary and protected evidence

23 process-level guards were installed before importing any merge, ingestion,
dataset or provider module: DNS (`getaddrinfo`, `gethostbyname`,
`gethostbyname_ex`), `socket.create_connection`, non-loopback
`socket.connect`/`connect_ex`, sync and async httpx transports and
`Client.send`/`AsyncClient.send`, `httpx.get`/`request`,
`requests.request`/`Session.send`, `urllib.request.urlopen`/`OpenerDirector.open`,
`build_readonly_client`, `BalldontlieClient.__init__`, `MlbStatsApiClient.__init__`,
`config.load_settings`, `f1a._default_client_factory`, and
`time.sleep`/`asyncio.sleep`.

**14/14 adversarial probes blocked**; `cli.load_settings is config.load_settings`
is `True`; **0 guard trips** across every review harness. No API key was read, no
provider client constructed, no audit run, no live execution, no real sleep.

All protected artefacts are regular files, none is a symlink, and source, recovery
and merged are three distinct filesystem objects. All three pass
`PRAGMA integrity_check` and are schema **v17** with **17** migration rows. Each
matched its previously accepted hash before review began:

```
39064fa219f4eb66f37e26f567a8f42d7df278236aa70e42264a600b07a5d135  March corpus
e2fea1c06c43400323b0266aeb8ba34db28e9b6ead13504413eb93ed4de6e1db  recovery db
8c4e83ee6cffb5c713de8bd0382d85b6486b2b68dd75821c7ad5ab38a4c689df  recovery ckpt
223c1185a0c7c1afb35e04ae8e31e50de4f7e62e69073808eb08cf75cafad02a  merged copy
```

## 2. Independent eligibility reconstruction (raw SQL only)

| Claim | Independently derived |
|---|---|
| Selected NBA games | **239** |
| Page-one responses | **239** |
| Continuation targets | **40** |
| Continuation responses | **40** |
| Target set == games whose page one advertised a cursor | **yes, exactly** |
| Starting cursors match page-one `next_cursor` | **40/40** |
| All chains terminate, all HTTP 200 | **yes** |
| Literal terminal `data=[]` responses | **19** |
| Non-empty continuation responses | **21** |
| Raw player rows | **32** |
| Valid normalized rows | **32** |
| Unique recovered observations | **32** |
| Rejected / lost / overlapping / contradictory | **0 / 0 / 0 / 0** |
| Affected `(game, team)` pairs | **22** |

The canonical recovered-row serialization is identical under five randomized
traversals.

## 3. The 22 / 294 / 32 identity, derived from first principles

The schema is append-only, so `source rows + 32` cannot apply. Verified directly
on a copy: `lineup_snapshots` and `lineup_players` each **refuse both UPDATE and
DELETE** (`RAISE(ABORT)`, "append-only"). A changed team lineup is therefore a new
observation-time snapshot; `append_transition` collapses a semantically unchanged
re-observation; `latest_as_of` picks the newest observation at or before a cutoff.

The exact arithmetic:

```
affected (game, team) pairs                          = 22
SUM of their page-one memberships                    = 262
SUM of their continuation additions                  =  32
NEW lineup_players rows = 262 + 32                   = 294
NEW lineup_snapshots    = one revision per pair      =  22
```

294 is not 32 because each revision must **restate** its team's page-one members —
the append-only tables give no way to add members to an existing snapshot. The 32
genuinely recovered `(game, team, player)` observations are the 32 additions
inside those 22 restatements.

## 4. Revision model and the anchor — one defect found and repaired

**Defect (blocker class, repaired).** The merge chose each revision's base by
taking the **earliest** observation at the `(game, team)` anchor. "Earliest" is
only accidentally page one. Reproduced on a copy of the real corpus: injecting a
single earlier observation at an affected anchor made the plan rebase on it, and
the planned revision contained **2 members instead of the true 11 plus 2
additions** — the real page-one members silently dropped out. §6 of the review
brief designates exactly this a blocker.

**Repair.** The base is now identified by **provenance**: the destination snapshot
whose `raw_response_id` is the page-one response for that game. Two snapshots
claiming the same page-one response, or none at all, now fail closed rather than
guessing. Planned membership is therefore a pure function of the two source
corpora — independent of destination history, insertion order, prior merges,
existing revisions and run time.

**The merged database was not affected.** In the real corpus each `(game, team)`
has exactly one snapshot and it is the page-one one, so earliest and page-one
coincided. Three independent replays after the repair reproduce the live merged
copy semantically and carry the committed digest
`eb596818496bda977c214f57094b544b6e84436814ffa09d34d0cb819aa83bce`. No rebuild was
required and the merged copy is byte-identical to the committed one.

Plan independence is now pinned across all five required scenarios: fresh
destination, already-merged destination, destination carrying an unrelated later
revision, reordered snapshot insertion, and reordered continuation evidence.

**Snapshot counting, stated precisely** — these are three different quantities:

| Quantity | Value |
|---|---|
| Raw stored snapshot **rows** | **500** |
| Distinct `(game, team)` **anchors** / latest logical team lineups | **478** |
| Teams represented **per game** | **2** |

The database does *not* hold "exactly two snapshot rows per game"; it holds two
logical team lineups per game, carried by 500 rows because 22 anchors have a
second, later observation.

All 22 revisions were checked individually: correct destination game reference,
correct provider team, `observed_at` equal to the continuation receipt instant,
correct recovery `raw_response_id`, `is_confirmed = 0`, `team_id` NULL, and
membership exactly page one plus that team's additions in provenance order. Every
original page-one snapshot is still present with an unchanged content hash.

## 5. Point-in-time and leakage — the central finding

**The March corpus is a retrospective backfill.** The games were played in March
2026; page one was observed **2026-08-04** and the continuation **2026-08-06**.
Every revision uses the continuation's own observation time — never a March game
date, never the page-one time, never the merge execution time — and every revision
timestamp is present in the recovery evidence.

`latest_as_of` behaviour over the 22 revision-carrying anchors:

| Cutoff | Source | Merged |
|---|---|---|
| Each game's own scheduled start (pregame) | **nothing (22/22)** | **nothing (22/22)** |
| 2026-08-05 (after page one, before continuation) | page one | **page one (22/22), identical to source** |
| 2026-08-07 (after continuation) | page one | **revision (22/22)** |
| 2026-08-08 (merge execution time) | page one | **revision (22/22)** |

Worked example: at the game's pregame cutoff both databases return `None`; at
2026-08-05 both return the same 11-member page-one snapshot; at 2026-08-07 the
merged copy returns the 13-member revision.

**Conclusion, stated without conflation:**

- The merge improves **retrospective provider-depth completeness** — 40 previously
  truncated games are now complete in the protected copy.
- It does **not** and **cannot** improve **historical point-in-time pregame
  feature availability**. That availability was **zero before the merge and
  remains zero after**, because even page one was retrieved five months after the
  games. No August-retrieved evidence is exposed at any March cutoff, so the merge
  introduces **no leakage** — but it also confers no historical pregame benefit.

Downstream read paths confirm this: `AsOfReader.lineup` anchors on canonical
`team_id`, which is NULL on all 500 merged snapshots, so it returns nothing
pre-matching; the real PIT dataset builder reports **0/239** accepted labels for
**both** databases with 0 canonical games and 0 match decisions; and
`is_confirmed` is 0 on every snapshot, so no confirmed-pregame-starter claim
exists anywhere.

## 6. Provenance

**Raw-response and ingestion-run mapping.** The 40 copied recovery raw responses
keep their original IDs — an identity mapping, not a remap — and none collides
with a March ID. All original March raw-response and run IDs survive, and no
original body was rewritten. Every copied response is byte-faithful in body hash,
content hash, request params, endpoint, provider and run linkage; each points at a
copied recovery run; and each retains its requested game ID and cursor. The 40
copied runs all carry
`(nba-lineup-continuation, balldontlie, lineup_continuation, succeeded)`, distinct
from the March month command `ingest-nba`, so no recovery response is attributed
to the original execution. `PRAGMA foreign_key_check` is clean.

**Provider identity completeness.** The 32 recovered observations span **29
distinct provider players** and **14 distinct provider teams**. All 29 players and
all 14 teams **already had references in the March corpus** — 29/29 and 14/14 — so
no identity evidence needed copying and **0 identities are unresolved**. Every
target game resolves to a destination provider game reference. No canonical entity
was created.

**Merge provenance (`DQ-NBA-LINEUP-M001`).** Exactly 40 rows, one per target, all
`note` severity, one stable sanitized message. Every row carries the merge
contract version, source and recovery fingerprints, manifest and plan hashes,
target count and digest, page-one response ID and hash, continuation response ID
and hash, requested cursor, added-player count, semantic digest,
`network_occurred=false` and the code version. Every row has `run_id = NULL`, so
M001 cannot be read as a provider ingestion having occurred during the merge. No
secret-shaped token appears in any row.

**On copying R009:** the recovery database holds 40 `DQ-NBA-LINEUP-R009` chain
notes; none was copied. M001 already records the page-one response, the
continuation response and the requested cursor for every target, and the copied
raw responses and runs carry the rest. No concrete downstream consumer needs R009
in the merged copy, so duplicating it would add rows without adding information.
**Not copying it is correct.**

## 7. Database reconciliation

Every table in the schema was compared. **Exactly five changed**, all as reported:

```
lineup_snapshots      478 ->  500   (+22)
lineup_players       5125 -> 5419   (+294)
raw_responses        1437 -> 1477   (+40)
ingestion_runs        240 ->  280   (+40)
data_quality_issues   481 ->  521   (+40)
```

No other table differs by a single row. Source and merged player/snapshot counts
independently confirm 5,125 → 5,419 and 478 → 500.

**The 199 untouched games** were compared semantically, not by count: snapshot
identity, observation timestamps, content hashes, raw-response linkage, provider
team, `is_confirmed`, player count, and full ordered membership including
positions, starter flags and canonical player IDs. **All 199 are identical.**

Unrelated tables verified identical: `nba_game_results` (239, typed results remain
**239/239**), schedule snapshots, quarter lines, team statistics, traditional and
advanced player statistics, plays, provider game/team/player references and
identity snapshots, canonical `games`/`teams`/`players`, match candidates and
match decisions. Every original DQ row is preserved and exactly 40 were added.

## 8. Robustness

**Independent replay.** Three fresh destinations were built from the protected
source via the backup API and merged under varied revision, outcome and player
orderings. All three produced 22 snapshots, 294 rows, 40 raw responses, 40 runs
and 40 M001 rows, are semantically identical to the live merged copy, and share
the committed digest.

**Non-circular check.** A review-only reconstruction that never calls the
production planner — page-one membership read directly by page-one
`raw_response_id`, plus continuation additions in provider row order — agrees with
production on **all 22 revisions** and independently yields **294** membership
rows.

**Idempotency.** Applies 2 and 3, an apply after reopening connections, and a
final dry run all report zero snapshots, zero player rows, zero copied responses,
zero copied runs and zero M001 rows, with 22 snapshots unchanged, the same digest,
stable counts and no observation stacking (snapshots stay at 500).

**Atomicity.** Failures injected at **eight** points — before the first target,
during raw-response copying, during run remapping, mid snapshot, mid membership,
between targets, before M001 provenance and immediately before commit — every one
propagated, rolled the destination back to exactly the pre-merge state
(478 / 5,125 / 1,437 / 240 / 481), and the retry converged to the accepted state.

**Concurrency and path protection.** Two simultaneous merges against one
destination resulted in one apply and one no-op with no duplicated revisions,
membership or provenance, and the final state equals the live merged copy. A
read-only consumer alongside a merge was not corrupted. The command refuses a
destination equal to the source, the recovery database, a checkpoint, or a
**hard-link alias** of the source. Symlink refusal is implemented
(`dest_path.is_symlink()`) but could not be exercised on this host: creating a
symlink requires a privilege this account lacks (`WinError 1314`). That is an
environment limitation, reported rather than papered over.

## 9. Validation

All offline; no real sleeps.

```
git diff --check                       clean
ruff check .                           All checks passed
mypy . --no-incremental                Success: no issues found in 307 source files
pytest -q                              2281 passed, 2 skipped, 0 failed (497 s)
schema init x2                         v17, 17 migration rows, 47 tables, integrity ok (both)
v16 -> v17 migration x2                v16 (0 identity tables) -> v17 (2), 17 rows,
                                       integrity ok, re-apply idempotent (both runs)
non-editable wheel smoke               44/44 checks passed
staged-file / secret audit             8 files, all source/docs/tests; no db, ckpt,
                                       raw response, log, wheel, env or graphify output;
                                       no secret-shaped literal
evidence isolation                     13/13 protected artefacts byte-identical,
                                       including the merged copy
```

Review harnesses, all under the zero-network sentinel with **0 guard trips**:

| Harness | Result |
|---|---|
| Fingerprints, append-only contract, observation timing | 0 failures |
| As-of cutoffs, downstream read paths, PIT/leakage | 0 failures |
| Independent raw-SQL reconstruction (§4, 5, 8–15) | 0 failures |
| Replay ×3, idempotency, 8-point atomicity, concurrency, paths | 0 failures |
| Non-editable wheel smoke | 0 failures |

`sports_quant/ingest/tests/test_nba_lineup_merge_review.py` adds **9 tests**: the
anchor reproducer (written before the repair and confirmed failing), later
unrelated revision, already-applied destination, insertion-order independence,
fail-closed when no snapshot came from the page-one response, revision observation
time, pregame invisibility across three cutoffs, page-one queryability after the
merge, and the raw-rows-vs-logical-lineups distinction. The 32 merge tests, the
continuation suites, the PIT suite, the results-repair regressions and the MLB
pacing suite were run as non-regression coverage (302 passed together).

The wheel smoke ran the **installed** package (fresh venv `site-packages`, CWD
outside the repository) and covered merge CLI help, review evidence loading, the
independent reconstruction, a fresh protected-copy merge, run/raw-response
remapping, reapply no-op, as-of/PIT cutoff behaviour, atomic rollback, concurrent
serialization, path protection, protected-artefact immutability, the zero-network
sentinel (14/14 blocked) and schema v17.

---

## Verdict

**ACCEPTED WITH LIMITATION.**

- **Accepted game count: 40 of 40** target games merged; **239 of 239** games are
  complete in the protected merged copy.
- **Accepted recovered observation count: 32.**
- **The NBA lineup family is now accepted for this March F1 provider-depth
  slice** — retrospective completeness of provider lineup data for the month.
- **Historical PIT limitation (the reason this is not a plain ACCEPTED):** the
  entire March corpus was backfilled in August 2026, so **no lineup is visible at
  any March pregame cutoff, before or after this merge**. Historical pregame
  lineup availability for this month is **zero** and this merge does not change
  that. Lineups from this corpus must not be used as historical pregame features;
  they are retrospective provider depth only. No leakage is introduced, and
  nothing was backdated to manufacture availability.
- **Defect found and repaired:** the revision anchor now binds to page-one
  provenance instead of observation order. The merged database was unaffected and
  remains byte-identical at
  `223c1185a0c7c1afb35e04ae8e31e50de4f7e62e69073808eb08cf75cafad02a`.
- **Original March and recovery evidence remain unchanged.**
- **No provider request occurred.**
- **PIT labels remain 0/239.**
- **Three canonical-matching defects remain open.**
- **The combined F1 review has not begun. F1 remains incomplete. F2 remains
  unauthorized.**
