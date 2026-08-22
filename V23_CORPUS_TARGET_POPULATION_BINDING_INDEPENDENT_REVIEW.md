# V23 Corpus Target-Population Binding — Independent Adversarial Review

**Starting HEAD:** `c4fd935` (= `origin/main`, tree clean, schema v23 / 23 migrations / 64 tables; `f018`–`f022` byte-identical).
**Schema after this review:** **v23 / 23 / 64 — UNCHANGED. No migration.**
**Provider requests:** 0. **Credits:** 0. Zero-network guards: 31 armed, 11/11 probes blocked.

## VERDICT: **RETAINED BLOCKER**

Eight defects were reproduced and repaired. Five survive as **retained blockers**
because closing them requires architecture decisions this review is not
authorized to improvise — and two of those are **SEVERE**.

The single most important finding: **`required_listing_runs` is not
manifest-bound.** It is a query over current database contents, so a sealed
corpus that verified clean **stops verifying** when an unrelated later
acquisition lands in the same date window. Corpus validity is therefore not a
property of the corpus. That is the scope-predicate design the architecture
explicitly rejected, reappearing one layer down.

| # | Question | Verdict |
|---|---|---|
| 1 | v23 schema / seal | **ACCEPTED** |
| 2 | required run-set derivation | **RETAINED BLOCKER** (RB-1, SEVERE) |
| 3 | failed acquisition-unit accounting | **RETAINED BLOCKER** (RB-2, HIGH) |
| 4 | manifest precommitment | **NOT PROVABLE HISTORICALLY** (RB-5) |
| 5 | checkpoint role | **OPTIONAL NON-SEMANTIC CROSS-CHECK** (docs corrected) |
| 6 | manifest/checkpoint parser | **REPAIRED** (RV-3, RV-4, RV-5, RV-6, RV-7) |
| 7 | cursor-chain completeness | **REPAIRED** (RV-2) |
| 8 | raw-response integrity | **REPAIRED** (RV-1) |
| 9 | scoped source digest | **REPAIR REQUIRED** (RB-3) |
| 10 | derivation digest / run ids | **REPAIR REQUIRED** (RB-3) |
| 11 | random canonical game ids | **STABLE TARGET IDENTITY REQUIRED BEFORE MATERIALIZATION** (RB-3) |
| 12 | NULL→game_id first resolution | **RETAINED BLOCKER** (RB-4, SEVERE) |
| 13 | construct/seal atomicity | **REPAIRED** (RV-8) |
| 14 | policy-version persistence | **REPAIR REQUIRED** (RB-6, LOW) |
| 15 | §AF seam | **RETAINED BLOCKER** — inherits RB-1 and RB-4 |
| 16 | E0 seam | **ACCEPTED** |
| 17 | historical evidence status | see §5 — one claim promoted to VERIFIED |
| 18 | Real March next step | **SAFE ONLY AFTER ADDITIONAL PROVENANCE REPAIR** |
| 19 | Overall | **RETAINED BLOCKER** |

---

## 1. The v22 blocker and the v23 mechanism

Re-reproduced independently: at v22 no relation enumerates corpus → games, and
`target_set_digest` accepts arbitrary caller text. v23's sealed-member mechanism
genuinely closes target omission, post-seal addition, post-seal run binding,
legacy digests and unsealed corpora — all verified. **Verdict 1: ACCEPTED.**

Nothing trusts seal presence alone: the only non-test reader of the seal table is
the verifier, and `_require_target_bound_parent` always recomputes.

## 2. RB-1 (SEVERE) — the run set is a scope predicate, not a manifest binding

`required_listing_runs` selects every run holding a successful `/v1/games`
response whose params match the manifest's date window. Two reproductions:

**A sealed corpus is invalidated by unrelated later evidence.** A corpus verified
clean; a second, unrelated acquisition was then added for the same window; the
same corpus now fails with *"acquisition requires runs the corpus does not
bind"*. A content-addressed corpus whose validity depends on rows added
afterwards is not evidence about itself.

**Two legitimate acquisitions are indistinguishable.** Independent executions A
and B over one window both appear in `required_listing_runs`, so corpus A is
forced to bind B's runs.

**Why this is not repaired here.** Fixing it needs a persisted acquisition
identity binding runs to a manifest **at acquisition time**. No such object
exists: `ingestion_runs.args_json` for the historical March run is
`{"includes":[],"tier":"goat"}` — no manifest hash, no plan version, no unit
identity. It cannot be retrofitted without manufacturing provenance. The correct
owner is a new acquisition-ledger relation written when an acquisition runs, which
is an architecture decision.

## 3. RB-2 (HIGH) — failed and empty required units vanish

Required-run discovery considers only HTTP 200. A required listing unit that
returned 500, and an ingestion run that produced no response at all, both simply
leave the required set. **A target-discovery failure is therefore invisible**
rather than making completeness fail. The proof that "these are all the runs"
must range over required *units*, not surviving *successes* — the same missing
ledger as RB-1.

The preserved March evidence happens to contain no failed listing response and no
run without a response, so this does not currently mask anything real; the
mechanism is nonetheless unsound.

## 4. RB-3 — every digest input is a database-local random surrogate

`game_id`, `run_id` **and** `raw_response_id` are all random ULIDs. So
`members_digest`, `derivation_digest` and `target-source-scope-v1` are **all**
non-portable: identical evidence rebuilt in a fresh database yields a different
`corpus_version_id`. f023 documents this for `game_id` only; the other two are
undocumented, and the implementation report describes the source digest as though
it fingerprints evidence when it fingerprints local row ids.

This must be adjudicated **before** real sealing, because it decides what a
target-bound corpus *is*: a portable scientific object, or a byte-preserved
database artefact. Options: accept database-local identity explicitly; key the
digests on stable provider identity (`provider` + `provider_game_id`,
`content_hash`) while member rows still FK to canonical ids; or require
deterministic canonical ids.

## 5. RB-4 (SEVERE) — immutability is not correctness

`trg_provider_game_ref_identity_immutable` freezes `game_id` once non-NULL, and
the v23 report treated that as closing the mutable-reference concern. It does not:
**the first NULL → value assignment is unchecked.** Reproduced — a provider game
assigned to an arbitrary wrong NBA game projects **cleanly** and is then frozen
forever. Projection compares only provider, `provider_game_id` and league; it never
compares the preserved listing payload's teams, start time or status to the
canonical game.

This is decisive for the next step. The preserved March database has **239
references, all NULL**. Materializing them today means performing 239 unchecked
first assignments and then baking them into corpus identity.

## 6. RB-5 — manifest precommitment is not provable from the database

The seal's manifest hash proves *"this corpus chose this file"*, not *"this file
constrained the historical acquisition."* The checkpoint records the same hash but
is in-run evidence, and manifest plus checkpoint could be rewritten together. No
`ingestion_runs` column binds a run to a manifest. B2-style Git binding of the
manifest at a historical commit is the available route; the manifest **is**
committed in `pilots/f1/`, so this is closable, but it is not closed today.

## 7. Repairs made (each reproduced first, each with a regression test)

| id | Defect | Repair |
|---|---|---|
| **RV-1** | HIGH — `scoped_source_digest` fingerprints the STORED `content_hash`, so a forged body with stale hashes left it unchanged; tampering was caught only when membership happened to differ | `verify_response_integrity` recomputes `body_hash` and `content_hash` before any body is parsed |
| **RV-2** | HIGH — a body with no `meta` certified as a complete listing; the documented cap-proof mitigation closes nothing when caps are far from binding (1 page, 100 games, caps 8/1000/400) | `meta` required on **every** accepted page; explicit `"next_cursor": null` still terminates |
| **RV-3** | HIGH — `str(...)` coerced `None`→`"None"`, `True`→`"True"`, `1`→`"1"`, a list→its repr | `_exact_str`: real JSON strings only, `bool` rejected before `int` |
| **RV-4** | HIGH — duplicate JSON keys silently last-value-wins (third appearance of the B2 class) | `object_pairs_hook` refuses duplicates; `parse_constant` refuses `NaN`/`Infinity`; applied to manifests, checkpoints **and** preserved bodies |
| **RV-5** | MEDIUM — `date_range.split("..")` accepted a missing delimiter, a reversed range, `2026-02-30`, whitespace and a third segment | exact `YYYY-MM-DD..YYYY-MM-DD`, real calendar dates, start ≤ end; parsed once into the binding |
| **RV-6** | HIGH — `families` was parsed and stored but never consulted, so a manifest declaring only `stats` certified a `/v1/games`-derived population | the manifest must declare the `games` family; duplicates and empties refused |
| **RV-7** | MEDIUM — `stage_game_ids` and `scratch_fingerprint` were loaded and ignored | `stage_game_ids` must be a subset of what the bound listing returned; duplicates refused |
| **RV-8** | MEDIUM — construction atomicity lived only in a docstring; a caller without a savepoint left committed membership behind an absent seal | `seal_target_population` owns a nested `SAVEPOINT` and rolls back on any failure |

Two shipped v23 tests were **replaced, not deleted**: the one that pinned the
meta-less terminus as an accepted limitation, and the `AcquisitionBinding`
construction that predates validated `start_date`/`end_date`.

## 8. Verdict 5 — the checkpoint's role, stated once

**OPTIONAL NON-SEMANTIC CROSS-CHECK.** Verification succeeds identically with and
without it, so it cannot be load-bearing for acquisition completeness. RV-7 makes
it meaningful *when supplied* — it must not contradict the manifest and must not
name games the listing never returned — but its absence is not an error. The v23
implementation document called it "required historical evidence"; that wording is
corrected. The checkpoint is also **not hash-bound in the seal**, so a different
checkpoint satisfying the same few fields could be supplied later; consistent with
its non-semantic role, and stated rather than glossed.

## 9. Verdict 14 — policy-version persistence (RB-6, LOW)

The seal stores `target_set_policy_version`, but the stored `target_set_digest` is
the **composite** `target-binding-v1`, and neither the binding policy, the
derivation policy nor `target-source-scope-v1` is persisted anywhere. A future
verifier facing a v2 composite would have to infer them from code — the
"try every known policy" contract the architecture rejected. Not repaired here:
adding seal columns is a schema change, and only one composite version exists, so
there is no ambiguity **yet**. It must be closed before `target-binding-v2`.

## 10. Verdict 16 — the E0 seam

**ACCEPTED.** The gate runs last among admission checks, and every preceding step
was audited: they are reads plus `certify_stage_a`, which creates no provenance.
The first write is inside the enrichment savepoint, after the gate. Ordering-for-
diagnostics does not leave partial state. Recency is not consulted, and no lookup
helper substitutes a target-bound sibling for legacy consumers.

## 11. Historical artefacts — independently rechecked, read-only

Re-derived from `data/f1_nba_2026_03_scratch.db` via a `mode=ro` connection; no
mutation, no import.

| Claim | Result |
|---|---|
| three `/v1/games` pages, 100 + 100 + 39 | **confirmed** |
| cursor chain `None → 18447784 → 18447884 → null` | **confirmed, genuine terminus** |
| `meta` present on every page including the terminal one | **confirmed** |
| stored `body_hash` matches recomputation on all three | **confirmed** |
| 239 provider game ids, all unique | **confirmed** |
| provider ids are JSON **integers** | confirmed (relevant to §14 of the brief) |
| references == listing ids exactly | **confirmed** |
| 239 references, 239 NULL `game_id`, 0 `games` rows | **confirmed** |
| 240 runs, 1 listing run, 0 runs without a response, 0 non-200 listing responses | **confirmed** |
| `manifest_hash` == `checkpoint.manifest_hash` (`901cb9de…`) | **confirmed** |
| caps: 3 pages/8, 239 records/1000, 239 games/400 | **none bound** |
| any run recording the manifest hash | **NONE** — the basis of RB-5 |

### Verdict 17 — what 239 means now, precisely

- **A. the preserved listing pages contain 239 unique provider game ids** —
  **VERIFIED.** Complete cursor chain, genuine null terminus, `meta` on every
  page, body hashes intact. Promoted from "prior observation".
- **B. those pages are the complete precommitted acquisition listing population**
  — **NOT VERIFIED** (RB-1, RB-2, RB-5).
- **C. 239 canonical targets** — **NOT VERIFIED.** Zero exist; 239 unchecked first
  resolutions stand between here and there (RB-4).
- **D. §AF bucket population** — **NOT VERIFIED.** 160 and 161 unchanged; **161
  remains invalid** as a bound.

## 12. Verdict 18 — real March materialization

> **SAFE ONLY AFTER ADDITIONAL PROVENANCE REPAIR.**

Not because the evidence is bad — claim A is now verified — but because
materializing 239 identities would perform 239 **unchecked** first resolutions
(RB-4) and freeze them into corpus identity built on **non-portable random
surrogates** (RB-3), inside a corpus whose validity is not stable against
unrelated later rows (RB-1).

Minimum before it is safe: RB-4 needs a canonical-identity admission gate that
checks each resolution against the preserved listing payload (teams, start, status
— exact, never fuzzy); RB-3 needs the portability decision; RB-1 needs the
acquisition-ledger decision.

The "cheap path" is still cheaper than a fresh acquisition, and it survives — but
it is not next.

## 13. Validation

`git diff --check` clean; schema **v23 / 23 / 64** unchanged; `integrity_check` ok,
`foreign_key_check` clean; no migration; no secrets; no staged database or payload;
`f018`–`f022` byte-identical; protected artefacts unchanged apart from the
documents in this commit. No real games materialized, no references resolved, no
target-bound corpus instantiated, no §AF run against real data.

## 14. Status

B3 deferred. P1 unauthorized. No real Stage-A plan. No probe registered. Stage A
NOT run. `REGISTERED_LINKING_PROVIDERS` empty. `ATTESTED_GENERATIONS` unchanged.
G5 NOT run. No crosswalk. **F1-R blocked.**

## 15. Exact next authorization boundary

> **Architecture adjudication of the five retained blockers** — RB-1 (acquisition
> identity / run-set binding), RB-2 (required-unit accounting), RB-3 (digest
> portability over random surrogates), RB-4 (canonical-identity admission gate),
> RB-5 (manifest precommitment via Git binding) — **before** any canonical
> materialization or corpus instantiation.

RB-1, RB-2 and RB-5 share one root cause and should be adjudicated together: there
is no persisted acquisition ledger. RB-4 and RB-3 together decide what a
target-bound corpus's members *are*, and both must be settled before the 239 are
materialized.
