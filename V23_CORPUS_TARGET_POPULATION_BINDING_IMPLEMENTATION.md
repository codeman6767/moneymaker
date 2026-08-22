# V23 — Corpus Target-Population Binding Implementation

**Starting HEAD:** `fd9ce6e` (= `origin/main`, tree clean, schema v22 / 22 migrations / 61 tables).
**Schema after this task:** **v23 / 23 migrations / 64 tables.**
**Provider requests:** 0. **Credits:** 0.

Implements the architecture as reconciled at `fd9ce6e` and its independent
review, which is authoritative wherever the two disagree.

> **SUPERSEDED IN PART.** See
> `V23_CORPUS_TARGET_POPULATION_BINDING_INDEPENDENT_REVIEW.md`, which is
> authoritative wherever the two disagree. It reproduced eight defects (repaired)
> and five retained blockers, two SEVERE. Corrections to claims made below:
>
> * **§5 run completeness** — "the required run set is derived from evidence, not
>   accepted from the caller" is true but insufficient: it is derived from a DATE
>   WINDOW over current database contents, so a sealed corpus is invalidated by
>   an unrelated later acquisition, and two acquisitions in one window are
>   indistinguishable. **RB-1, SEVERE.**
> * **§7 cursor chain** — the meta-less-terminus limit was described as mitigated
>   by the manifest cap proof. It was not: the caps are far from binding. Now
>   **repaired** — `meta` is required on every page.
> * **§9 mutable references** — "resolved better than predicted" was wrong.
>   Immutability after assignment is not correctness; the FIRST NULL → value
>   assignment is unchecked, and a wrong resolution projects cleanly and freezes.
>   **RB-4, SEVERE.**
> * **§10 scoped source digest** — it fingerprints stored `content_hash` values
>   and local `raw_response_id`s, not evidence. Integrity is now recomputed
>   (RV-1), but portability is **RB-3**.
> * **§6 checkpoint** — called "required historical evidence" here; it is an
>   **optional non-semantic cross-check**.
> * **§1 / §18** — canonical materialization is **not** the safe next step. See
>   the review's verdict 18.

---

## 1. Historical artefact preflight (read-only) — the headline result

The independent review predicted the historical March artefacts might be
unrecoverable. **They are present.** A read-only inventory (`mode=ro` URI, no
write, no import, no mutation) found:

| Artefact | Status |
|---|---|
| `data/f1_nba_2026_03_scratch.db` (355 MB) | **present** |
| `/v1/games` listing pages, with cursors in `request_params_json` | **present — 3 pages** |
| `ingestion_runs` | **present — 240 runs** |
| `provider_game_references` | **present — 239 rows** |
| `pilots/f1/nba_coverage_2026_03.manifest.json` | **present** |
| `data/f1_nba_2026_03.ckpt` | **present** |
| schema version | 17 (migratable on a COPY) |
| **canonical `games` rows** | **ABSENT — 0 rows** |
| **resolved `provider_game_references.game_id`** | **ABSENT — 239 of 239 are NULL** |

**`HISTORICAL_MARCH_TARGET_BINDING_ARTEFACTS = INCOMPLETE`** — missing only
canonical game materialization.

**`HISTORICAL_MARCH_CORPUS` is NOT permanently unusable for target binding.** The
review's "permanently unusable" condition was a missing manifest or checkpoint,
because completeness would then be unfalsifiable. Both exist, and they agree:

```
sha256(manifest bytes)     = 901cb9deaf3c5bf243f73ed60a820dd323933caea5dac7a45b69e01480f5ad3e
checkpoint.manifest_hash   = 901cb9deaf3c5bf243f73ed60a820dd323933caea5dac7a45b69e01480f5ad3e
```

The preserved cursor chain is **complete and naturally terminated**:

| page | request cursor | games | `meta.next_cursor` |
|---|---|---|---|
| 1 | *(none)* | 100 | `18447784` |
| 2 | `18447784` | 100 | `18447884` |
| 3 | `18447884` | **39** | **`null`** |
| | | **239** | genuine terminus |

And no cap bound it: 3 pages against `max_pages: 8`, 239 records against
`max_records: 1000`, 239 games against `max_games: 400`.

**What this does NOT license.** 239 is corroborated three ways (listing total,
`stage_game_ids` length, reference count) but is **still UNVERIFIED as a target
population**, because every one of the 239 provider references has a NULL
canonical `game_id`. Under `official-listing-projection-v1` that is 239 refusals,
not 239 members. Canonical materialization on a COPY is a separately authorized
prerequisite; it was not performed here.

**A prior claim I carried forward was too strong and is corrected.** The §AF
report said "every database under `data/` was scanned: none contains any NBA game
row." The `games`-row part is exactly right — 0 rows. But the conclusion drawn
from it, that the authoritative parent corpus and its listing responses were
absent, was wrong: the raw listing evidence, runs, references, manifest and
checkpoint are all present.

## 2. Schema

`f023_corpus_target_population_binding.sql`. Additive only; `f018`–`f022` are
untouched.

| Table | Key | Undetectable without it |
|---|---|---|
| `reconstruction_corpus_targets` | PK `(corpus_version_id, game_id)` | a target omitted from the corpus |
| `reconstruction_corpus_target_runs` | PK `(corpus_version_id, run_id)` | membership asserted but not re-derivable |
| `reconstruction_corpus_target_seals` | PK `corpus_version_id` | membership/bindings extended after creation; unknown digest policy; a vacuous empty corpus |

Seal fields, each with its own named failure: `target_set_policy_version`
(a verifier cannot infer a hashing policy from an opaque 64-hex digest),
`listing_projection_policy_version`, `acquisition_completeness_policy_version`,
`acquisition_manifest_hash` + `plan_version` (without them the bound run set is
caller-selected), `member_count` (> 0, asserted against actual membership).

Deliberately **not** stored: `S_final`, any per-target hint column, any scope
predicate.

## 3. Construct-then-seal

The architecture's original rule — *"no member may be inserted for a corpus that
already exists"* — was proved unimplementable, and this implementation does not
attempt it. Membership and run bindings are insertable **only while unsealed**;
the seal closes both permanently.

```
verify acquisition completeness (manifest -> required runs -> cursor-chain closure)
project bound listing responses -> canonical members   (fail closed on any unresolved)
compute members_digest, derivation_digest, target_set_digest, scoped source digest
SAVEPOINT build
  INSERT corpus row                -- FK parent must exist first
  INSERT membership rows           -- permitted: no seal yet
  INSERT run-binding rows          -- permitted: no seal yet
  INSERT seal                      -- trigger asserts member_count == COUNT(membership)
RELEASE build
verify by recomputation
```

An unsealed corpus stays open by construction, so **a missing seal is a hard
verifier failure**. There is no warning path and no skip branch.

## 4. Frozen digest policies

```
members_digest    = sha256(canonical{policy:"target-set-v1", league_id, members})
derivation_digest = sha256(canonical{policy:"target-derivation-v1",
                                     acquisition_manifest_hash, plan_version, run_ids})
target_set_digest = sha256(canonical{policy:"target-binding-v1",
                                     league_id, members_digest, derivation_digest})
```

Duplicates are **refused**, never de-duplicated: `sorted()` retains a duplicate,
so one membership set would otherwise have two valid digests. Type coercion is
refused (`bool` is an `int` subclass; `games.game_id` is TEXT, so the B2
integer/string defect has no analogue). Unknown policy refuses. An empty set
hashes validly but cannot be sealed.

**Why a composite rather than a new corpus column.** Adding a provenance field to
the `semantic_digest` payload would change the digest of **every** corpus,
including legacy rows — verified: `semantic_digest({"a":1,"b":2})` and
`semantic_digest({"a":1,"b":2,"c":None})` differ. The payload is effectively
frozen, so derivation provenance enters identity through the one semantic field
that already exists, while membership keeps its own separately verifiable digest.

## 5. Acquisition binding and run completeness

`required_listing_runs` derives the required set **from evidence**: every run
holding a successful listing response for the manifest's provider, endpoint and
exact date window. The bound set must equal it, both directions. Omitting a
required run fails; binding an extra one fails.

The manifest hash is the sha256 of the exact file **bytes**, matching what
`checkpoint.manifest_hash` records — re-serializing would introduce a
canonicalization the historical artefacts never used.

**Cap proof (§12).** Sealing requires strict inequality against every declared
cap. A run that used exactly `max_pages` pages cannot be distinguished from one
the cap stopped, so an unambiguous margin is demanded rather than assumed.

## 6. Checkpoint role

Cross-check, never authority. `manifest_hash`, `plan_version`, `league`,
`date_range` and `families` must agree with the manifest or loading **refuses**
rather than preferring one. `stage_game_ids` is read as **selected-set evidence**
and is never used to define membership — selection is a different claim from the
complete official population, and `max_games` truncation is reported only in
runtime fields, not persisted rows.

## 7. Listing admission and cursor-chain closure

Admission is exact equality: `provider = 'balldontlie'`, `endpoint = '/v1/games'`,
`http_status = 200`, `run_id ∈ bound set`. `/v1/games/{id}`, `/v1/stats`,
`/v1/box_scores`, `/v1/plays`, `/v1/lineups` and `/nba/v1/stats/advanced` are
excluded because their endpoint is a different string — no substring rule a new
endpoint could slip past. A non-200 response is excluded entirely, so a failed
request never masquerades as an empty listing.

`verify_cursor_chain` detects a truncated tail, a missing middle page, a duplicate
page, an unreachable orphan page and a cursor cycle.

**Retained limitation, pinned by a test so nobody later claims otherwise:** a body
that omits `meta` terminates the chain and is indistinguishable from a genuine
last page. This is not cryptographically detectable — the acquisition itself would
have stopped there. The mitigation is the manifest's cap proof and coverage
assertion.

## 8. Official listing projection

`official-listing-projection-v1`. `games.game_id` is a random **ULID**, so there
is no pure projection: each provider game resolves through
`provider_game_references` under exact `(provider, provider_game_id)`. Every
failure **refuses** — missing reference, NULL `game_id`, missing `games` row,
wrong league, malformed id or object. Nothing is silently dropped, because a
dropped unresolved game is exactly how a population quietly shrinks.

Repeated evidence for one provider game (same id across pages or runs) collapses
**at the projection layer** to one member, so two semantically identical
acquisitions cannot produce two digests. Cancelled, postponed and rescheduled
games returned by the listing **remain members**.

## 9. Mutable `provider_game_references` — the review's §14, resolved better than predicted

The review flagged this table as mutable and demanded either detection or a
retained blocker. Neither is needed: an existing trigger already closes it.

```sql
trg_provider_game_ref_identity_immutable
  ... OR (OLD.game_id IS NOT NULL AND NEW.game_id IS NOT OLD.game_id);
```

Once a provider game resolves to a canonical game, **the mapping is frozen** — a
remap raises *"provider_game_references identity columns are immutable"*
(reproduced in tests). The single permitted mutation is NULL → resolved, i.e.
late identity resolution, and it cannot affect any sealed corpus: an unresolved
game makes projection refuse, so such a corpus could never have been sealed.

`raw_responses` is likewise fully append-only (no UPDATE, no DELETE). A test
drops those triggers to model an attacker with full SQL access and confirms the
verifier still fails on removed evidence.

**This corrects my own independent review**, which described the table as mutable
without noting that its identity columns are trigger-protected.

## 10. Scoped source digest

`target-source-scope-v1` over `(policy, provider, endpoint, sorted
[(raw_response_id, content_hash)])` for exactly the admitted responses. Unrelated
official evidence added elsewhere does not change it (tested); removing a bound
response changes it *and* breaks the chain. Source and derivation digests
therefore refer to the same bounded evidence population.

## 11. Sibling / distinct policy — no supersession

`supersedes_corpus_version_id` is left **NULL**, and a test asserts it. A
target-bound corpus makes a claim the old corpus never made; recording
supersession would manufacture a lineage. It declares
`reconstruction_policy_version = "target-bound-reconstruction-v1"`, so
target-boundness is visible in the corpus row and a legacy corpus cannot acquire
it retroactively.

The E0 gate does not consult recency: a test makes a **newer** legacy corpus the
latest row and confirms it is still refused.

## 12. Seams

**§AF.** `derive_target_population` no longer refuses. It requires
`verify_corpus_target_population` clean, reads the verified membership, and reads
`S_final` from `games.scheduled_start` — the CURRENT scheduled start, which f002
documents as *"updated on a postponement/reschedule"* and is therefore the
retrospective official start (`original_start` is the FIRST start and is not
`S_final`). A verified target with no hint **refuses** rather than being dropped.
`verify_stage_a_target_bucket_policy` now performs the real comparison, including
the pigeonhole case where a dropped co-bucketed target leaves the bucket set
byte-identical.

**E0.** `enrich_corpus_with_market_lane` calls the gate before doing anything, and
refuses without a manifest as well as on any verification failure.

## 13. Direct-SQL threat model — all reproduced in tests

Forged `target_set_digest`; member added after seal; member removed; member
updated; `REPLACE`/`INSERT OR REPLACE`; run binding added after seal; seal
update/delete/replace; wrong `member_count`; zero-member seal; seal with no run
binding; wrong-league member; missing `games` row; omitted required run; extra
unrelated run; substituted manifest; wrong plan version; unknown policy version;
legacy corpus with a plausible 64-hex digest; unsealed corpus with members and
runs; incomplete listing chain; unresolved reference; NULL reference `game_id`;
remap attempt; evidence removed with triggers dropped; capped acquisition;
duplicate cursor; chain gap.

## 13a. Pre-existing tests whose subject v23 changed

24 existing tests failed on the first full run. None was waived; each was
repaired at the cause, and the repairs are listed because a reviewer should
check that none of them quietly weakened an invariant.

| Count | Tests | Repair |
|---|---|---|
| 2 | PIT table registry "exactly covers the live schema" | Registered the three new tables as `_unsupported` with reasons. This is a **completeness invariant**, and the right answer is a registration, not a relaxation: target membership is retrospective by construction and must never reach the strict reader as a predictor. |
| 8 | Migration count / `CURRENT_SCHEMA_VERSION == 22` / latest-migration filename | Extended to 23. `test_fresh_database_reaches_v22_with_22_migrations` was **renamed and retargeted**: its real invariant is that f022 keeps position 22 and the sequence stays contiguous, not that 22 is newest. |
| 3 | §AF "refuses" tests | **Replaced, as that file's own comment instructed** — the refusal is now about missing provenance, not missing capability, and a real verification result is asserted in the new seam suite. |
| 10 | v22 enrichment / lane tests | Given a **sealed target-bound parent**, since v23 legitimately refuses E0 from a free-text-digest corpus. `_seed` was made idempotent so the new helper and the acquisition fixture can both call it. |
| 1 | `test_wheel_excludes_tests_and_secrets` | The `"corpus" not in filename` heuristic tripped on the correctly-named f023 migration. Narrowed to what it actually guards — corpus **databases** and sidecars (`.db`, `.db-wal`, `.db-shm`, `.sqlite`, `.ckpt`, `data/corpus`) — rather than renaming an accurate migration. |

**One design decision came out of this.** The E0 gate initially ran first and
masked the more specific v22 refusals (uncertified acquisition, mismatched
official source corpus or target set) behind a generic "not target-bound"
message. It now runs **last** among the admission checks, so every earlier check
still fires for its own reason and the gate only decides final admission. That
ordering is asserted by the v22 suite and the gate itself is unit-tested
directly, plus a test pins that enrichment invokes it and that no bypass
parameter exists.

## 14. Migration compatibility

Fresh init reaches v23 / 23 migrations / 64 tables; `PRAGMA integrity_check` ok
and `foreign_key_check` empty. Migration is idempotent. `SUPPORTED_SCHEMA_VERSIONS`
is `{16…23}` — still a set, so preserved pilot artefacts (the March checkpoint
declares `schema_version: 17`) are not orphaned. A legacy corpus's
`semantic_digest` is unchanged and recomputes identically; such a corpus simply
verifies as **TARGET-UNBOUND**. No existing row is rewritten and no migration is
destructive.

## 15. Strict PIT / leakage

Tests assert by source inspection that the three new modules contain no reference
to `AsOfReader`, `_feature_cutoff`, `sportsbook_price_snapshots` or
`historical_market_event_observations`, and that membership derivation reads none
of `nba_game_results`, `game_result_snapshots`, `reconstructed_input_provenance`,
`identity_audit_records`, `static_crosswalk_provenance` or `stage_a_plan_targets`.
`S_final` remains a search hint and is not stored on target rows.

## 16. 239 / 160 / 161

**All three remain UNVERIFIED.** v23 working does not promote them. No real target
population has been instantiated, no real §AF derivation has been reviewed, and
no request cap may be set.

## 17. Status

B3 deferred. P1 unauthorized. No real Stage-A plan declared. No probe registered.
Stage A NOT run. `REGISTERED_LINKING_PROVIDERS` empty. `ATTESTED_GENERATIONS`
unchanged. G5 NOT run. No crosswalk. **F1-R blocked.** No real target-bound corpus
instantiated. No fresh acquisition started.

**v23 is ready for independent adversarial implementation review.**

## 18. Exact next authorization boundary

> **Fresh independent adversarial review of the v23 implementation**, before any
> real target-bound corpus is instantiated.

The likely task after that is **canonical game materialization on a COPY of the
preserved March scratch database**, since that is now the only missing input —
not a fresh acquisition.
