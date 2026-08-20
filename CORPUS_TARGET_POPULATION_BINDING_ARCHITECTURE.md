# Corpus Target-Population Binding — Architecture Adjudication

**Starting HEAD:** `b6683ac` (= `origin/main`, tree clean, schema v22 / 22 migrations / 61 tables — **unchanged by this task**).
**Provider requests:** 0. **Credits:** 0. Architecture only; nothing implemented.

## Verdicts

| # | Question | Verdict |
|---|---|---|
| 1 | Target population definition | the complete set of canonical official games the corpus **asserts as its research targets**, fixed at corpus creation, before any eligibility |
| 2 | Authoritative membership source | the **preserved official listing raw responses of the bound acquisition run(s)** — available in principle, **not instantiable locally** |
| 3 | Representation | **EXPLICIT MEMBERSHIP**, re-derivable from the bound runs |
| 4 | `target_set_digest` | derived: `sha256(canonical_json{policy, league, sorted member ids})`, policy `target-set-v1` |
| 5 | Legacy values | **LEGACY UNBOUND** — valid for uses not needing completeness; can never pass the verifier |
| 6 | Existing corpus treatment | **NEW target-bound corpus.** No retroactive attestation of C1 |
| 7 | Schema | **V23 REQUIRED** |
| 8 | §AF ready after implementation | **YES for the mechanism**, gated on supplying the authoritative corpus |
| 9 | Overall | **ARCHITECTURE ACCEPTED — READY FOR IMPLEMENTATION**, with a retained **source/data** dependency |

> **SUPERSEDED IN PART BY INDEPENDENT REVIEW.** See
> `CORPUS_TARGET_POPULATION_BINDING_ARCHITECTURE_INDEPENDENT_REVIEW.md`. The
> scientific core (verdicts 1, 2, 3, 5, 6) survived attack. Four mechanisms did
> not and are corrected in place below: the closure rule in §7 is **not
> implementable** and is replaced by construct-then-seal; run provenance sits
> **outside the content address**, so the §6 digest becomes a composite; §4's
> "projection" requires a mutable, nullable mapping and a new verifier; and the
> run set must bind a **precommitted acquisition manifest**, not caller-chosen
> run ids. The v23 shape in §7 grows from two objects to three plus a column.
> Each correction is marked **[REVIEW]**.

---

## 1. The blocker, independently reproduced

Not quoted from the §AF report — re-derived offline on fresh v22 databases:

| | Result |
|---|---|
| **A** corpus accepts arbitrary caller text as `target_set_digest` | stored `'identity-audit-no-targets'` — the runner's literal default |
| **B** relation enumerating corpus → canonical games | **NONE**. Tables referencing a corpus: `reconstruction_corpus_versions`, `static_crosswalk_provenance`, `reconstructed_input_provenance`, `corpus_evidence_lane_bindings` — none links to `games` |
| **C/D** two faithful copies, same corpus | **identical `semantic_digest`**, yet "all NBA March games" yields **1 vs 6** |
| **E** `stage_a_plan_targets` | keyed by `plan_id`, no corpus reference — it is the claim under verification |
| **F** `reconstructed_input_provenance` | keyed by `provider_game_id` + `feature_family`, 0 rows in a fresh corpus |
| **G** committed NBA March pilot manifest | commits `date_range 2026-03-01..2026-03-31` + 7 families + **0 fixed units, 7 contingents**; enumerates **no** canonical game id |

**C/D is the decisive one.** Corpus identity is content-addressed and identical
across the two copies, yet the scope query returns different sets. A scope query
is therefore a property of *the database*, not of *the corpus* — which is exactly
why "league=NBA and March 2026" cannot be the membership source.

**G is the second decisive one.** The pilot manifest is precommitted and hashed,
but it commits the **acquisition scope**; the games were discovered by a
contingent *during* acquisition. So no precommitted artefact enumerates the
original 239.

> **[REVIEW] G is incomplete.** The resume **checkpoint** does enumerate a frozen
> game set — `Checkpoint.stage_game_ids`, bound to `manifest_hash`,
> `plan_version` and `scratch_fingerprint`. It is written *during* acquisition, so
> it is not precommitted evidence of the intended population and does not license
> retroactive attestation; but it is preserved evidence of the **selected** set
> and belongs on the artefact checklist. Note also that the selected set is not
> automatically the complete official set: `max_games`, `max_pages` and
> `max_records` truncation is reported in runtime fields, not persisted rows.

## 2. What a target population is — and is not

> The complete set of canonical official games this reconstruction corpus asserts
> are its retrospective research targets.

Fixed **at corpus creation**, before any downstream verdict. Four distinct
concepts that must never be collapsed:

| Concept | Meaning | May shrink the population? |
|---|---|---|
| **TARGET POPULATION** | which games the corpus is about | **never** |
| Target eligibility | did this target survive completion evidence | no — it gains a status |
| Feature availability | is a feature computable for it | no |
| Market-anchor eligibility | did a historical market exist at `T_cut` | no |

A game stays a target even when its completion evidence is malformed, its
identity is unresolved, Stage-A projection fails, no market matched, or modelling
later excludes it. **Shrinking the population downstream is selection bias that
hides failure** — a 100 % completion rate over a set redefined to the games that
succeeded is not a result. This is the same shape as the pigeonhole finding: the
denominator must be fixed before the numerator is measured.

## 3. Option analysis

**Option B — scope binding only. REJECTED.** Disproved by reproduction C/D: the
same corpus identity yields different sets in two faithful copies. A scope is
also silently mutable by late ingestion, corrections to `officialDate`, or an
extra unrelated game landing in the interval. This is precisely the workaround
§AF refused.

**Option D — committed external target manifest. REJECTED as the source.** B2's
lesson applies directly: Git binding proves *which list was committed*, never
*that the list is complete*. With no independent evidence to check the list
against, a target manifest only relocates the trust. It would also make §AF
depend on two Git artefacts and leave a copied database unable to verify without
repository history.

**Option A — explicit membership rows. CHOSEN**, with the acquisition binding
from Option C that makes membership *re-derivable* rather than merely asserted.

**Option C — scope + membership.** Adopted only in the narrow form where the
"scope" is the **bound acquisition run set**, not a date predicate. A free-text
scope beside materialised rows would create two truths that can disagree; the run
binding instead makes the rows independently recomputable.

## 4. Authoritative membership source

> The preserved **official listing raw responses** of the acquisition run(s) the
> corpus binds, projected through the existing official normalization.

Why this and not the `games` table: raw responses are append-only preserved
evidence that travels *with* the corpus. Adding unrelated rows to `games` cannot
change what the provider returned for the bound runs, so two faithful copies
re-derive the identical set — the property reproduction C/D showed a scope query
lacks.

The corpus must therefore bind the exact `run_id` set, so derivation is scoped to
*those* acquisitions rather than to every response the database happens to hold.

> **[REVIEW] Two corrections.**
> **(a) Run ids alone are unfalsifiable.** No v22 object derives "the complete
> required run set" for the official acquisition — the manifest and checkpoint are
> files. A caller binding R1+R2 while omitting R3 yields membership that is
> internally perfect, moving denominator shrinkage one layer earlier. The corpus
> must bind the **precommitted acquisition manifest hash and plan version**; the
> run ids are the *resolution* of that manifest, not the primary claim.
> **(b) This is not a pure projection.** `games.game_id` is a random **ULID**
> (`ids.py`), never derivable from a provider payload. Derivation must resolve
> `provider_game_id` through `provider_game_references`, whose `game_id` is
> NULLABLE and whose row is mutable. A deterministic
> `official-listing-projection-v1` verifier is required, and it must **fail
> closed** — never drop a member — when a provider game has no resolved canonical
> game. Page completeness comes from **cursor-chain closure** over the bound
> `/v1/games` responses, which is derivable from preserved evidence.

**Retained source dependency.** This source is sound in principle and **cannot be
instantiated now**: no database under `data/` contains a single NBA game row, so
the March corpus and its listing responses are absent. The minimum input needed
later is the **preserved NBA 2026-03 reconstruction database containing the
`balldontlie` listing raw responses and their `ingestion_runs`**. If that artefact
cannot be produced, the correct route is a **new officially bounded parent corpus
acquired under a fresh precommitted manifest** — not a retroactive attestation.

## 5. Existing corpora and legacy values

**No retroactive binding.** Corpus identity is content-addressed and
`target_set_digest` already feeds `semantic_digest`. Adding membership to C1 today
and claiming it was always C1's population would invent historical provenance —
and nothing precommitted enumerates the original 239, so the claim would be
unfalsifiable. C1 stays byte-identical.

**Legacy status: LEGACY UNBOUND.** Values such as `identity-audit-no-targets`,
`"t"` and `"TGT"` are **not** reinterpreted as memberships. Such corpora remain
valid for every historical use that never required target completeness — TEAM-A
provenance, existing audits and crosswalks are untouched — but they can never pass
the target-population verifier and can never back §AF or a real Stage-A plan.

**Chain.** The target-bound corpus is a **new corpus version**, and E0 enrichment
must build from it, never from a target-unbound parent:

```
official source corpus  ->  TARGET-BOUND corpus  ->  E0-enriched corpus
```

Whether it *supersedes* the official corpus or is a sibling policy version is an
implementation detail the next task must settle; the invariant is that a
target-unbound corpus can never be the parent of an E0 lane.

> **[REVIEW] Settled: SIBLING / DISTINCT POLICY — it does not supersede.**
> Supersession asserts a corrected restatement of the *same* claim. A target-bound
> corpus makes a claim the old corpus never made, under a different
> `reconstruction_policy_version`, and may have no ancestor at all. Recording
> supersession would manufacture a lineage. Consequences: C1 stays byte-identical;
> legacy audits and crosswalks stay valid; "latest corpus" resolution must **not**
> silently switch consumers onto the target-bound corpus; and the E0 invariant is
> enforced **at the E0 seam**, never by recency. `source_corpus_digest` must be
> **scoped to the bound acquisition's evidence** rather than inherited from a
> broader official corpus, so the source and derivation digests commit to the same
> evidence.

## 6. `target_set_digest` — derived contract

```
target-set-v1 :=
    sha256(canonical_json({
        "policy":  "target-set-v1",
        "league_id": <league>,
        "members": <sorted unique canonical game ids>,
    }))
```

Membership only. `S_final` is deliberately **excluded**: membership and hint
evidence are different claims, and `source_corpus_digest` already commits the
source evidence. Ordering never changes identity; membership always does.
Duplicates are refused, not de-duplicated. Unknown policy version refuses. A
semantic change requires `target-set-v2`.

`games.game_id` is the membership key — the project's canonical surrogate
identity, deliberately stable across provider-id changes and corrections
(`db/ids.py`). Provider ids and team names are never used.

> **[REVIEW] The digest becomes a composite, and the key has a caveat.**
> Run provenance cannot be added to the `semantic_digest` payload: widening that
> payload changes the digest of **every** corpus, including legacy rows. But run
> provenance must be part of corpus identity, because otherwise two different run
> sets reaching the same member set collapse to **one** corpus row (proved), so a
> single content-addressed identity carries two derivation explanations. The
> repair keeps membership as its own named object inside a composite:
>
> ```
> members_digest    = sha256(canonical{policy:"target-set-v1",
>                                      league_id, members:[sorted unique game ids]})
> derivation_digest = sha256(canonical{policy:"target-derivation-v1",
>                                      acquisition_manifest_hash, plan_version,
>                                      run_ids:[sorted unique]})
> target_set_digest = sha256(canonical{policy:"target-binding-v1",
>                                      league_id, members_digest, derivation_digest})
> ```
>
> Duplicates must be **refused** by the digest function, not de-duplicated:
> `sorted()` retains a duplicate, so one membership set would otherwise have two
> valid digests. Non-emptiness is enforced on the seal, not in the digest.
> **Caveat on the key:** `game_id` is a random ULID, so the content address is
> over *this database's* surrogates. A byte-copy is portable; a rebuild from
> identical raw evidence is not.

## 7. Minimal schema — **V23 REQUIRED**

Two objects. Each answers "what failure is undetectable without this?"

| Object | Purpose | Key | Undetectable without it |
|---|---|---|---|
| `reconstruction_corpus_targets` | the exact member set | PK `(corpus_version_id, game_id)`; FK both | a target omitted from the corpus — invisible today |
| `reconstruction_corpus_target_runs` | the acquisition run(s) membership derives from | PK `(corpus_version_id, run_id)`; FK both | membership asserted but not re-derivable, so a forged member set cannot be caught |

No scope table, no per-target hint columns, no stored `S_final` — all fully
derivable from the bound runs and the existing source-evidence machinery.

**Ordering resolves the circularity** (`target_set_digest` feeds `semantic_digest`,
which yields `corpus_version_id`): derive members from the bound runs → compute
`target_set_digest` → compute `semantic_digest` and the corpus id → insert corpus,
run bindings and membership **in one savepoint** → verify by recomputation.

**Closure.** Membership is append-only under the hardened f021 pattern from day
one, *and* closed: no member may be inserted for a corpus that already exists,
because the corpus id is a function of the membership. A later insert is refused
by trigger, not convention.

> **[REVIEW] This closure rule is NOT IMPLEMENTABLE and is replaced.** With an
> ordinary FK to the corpus, the parent must exist before a member can be
> inserted — at which point a literal "corpus already exists" trigger fires and
> membership becomes uninsertable in every case (reproduced:
> `IntegrityError: membership is closed: corpus already exists`). SQLite triggers
> cannot distinguish "this parent was created earlier in my savepoint" from "this
> parent is old".
>
> **Replacement: construct-then-seal**, proved executable against a real migrated
> v22 database under eleven attacks. A third table
> `reconstruction_corpus_target_seals` (PK `corpus_version_id`, carrying
> `target_set_policy_version`, `member_count > 0`, `acquisition_manifest_hash`,
> `plan_version`) is inserted last in the same savepoint. Membership and run
> bindings are insertable **only while unsealed**; a seal trigger asserts
> `member_count == COUNT(membership)`; the seal is immutable. Verified refusals:
> post-seal append, DELETE, UPDATE, `INSERT OR REPLACE`, re-seal, count
> disagreement, wrong league, missing `games` row. A failed membership insert
> rolls the corpus back too, leaving no orphan identity.
>
> **Consequence:** an unsealed corpus is open by construction, so the verifier
> must treat a missing seal as a hard failure. The seal is also where the policy
> version lives — a verifier cannot infer a hashing policy from an opaque 64-hex
> digest, and "try every known policy until one matches" is not a contract.
>
> **v23 is therefore three tables plus the manifest binding, not two.**

## 8. Direct-SQL threat model

| Attack | Control |
|---|---|
| Forged `target_set_digest` | verifier recomputes from membership |
| Membership added / removed after creation | append-only + closure trigger; digest recomputation |
| `REPLACE` / `INSERT OR REPLACE` / upsert | f021 content-aware BEFORE INSERT guard |
| Member from the wrong league | trigger: game league must equal corpus league |
| Duplicate member | PK |
| Member game row missing | FK |
| Membership copied from another corpus | member set is an input to *this* corpus's id; copying changes the recomputed digest |
| Corpus A members + corpus B source evidence | run bindings are children of one corpus; verifier re-derives from *those* runs |
| Legacy unbound corpus presented as bound | verifier refuses a non-`target-set-v1` digest |
| Extra games in the database | membership derives from bound runs, not from `games` |

## 9. §AF composition contract

```
verify_corpus_target_population(parent)      <- the new seam
  AND B2 committed-manifest verification
  AND §AF independent target -> bucket recomputation
  AND v22 acquisition / projection certification
  -> eligible for E0 enrichment
```

The exact seam: `stage_a_target_bucket.derive_target_population` stops refusing
and instead reads `reconstruction_corpus_targets` for the parent, after
`verify_corpus_target_population` has recomputed the digest. No step may take the
manifest as the source of expected membership.

## 10. Strict PIT / leakage

Unchanged: `AsOfReader` and `_feature_cutoff` untouched; `S_final` remains a
search hint and never becomes a feature; no retrospective hint becomes Lane-L
data; the population is never shrunk by future outcomes (§2).

## 11. 239 / 160 / 161 — all UNVERIFIED

- **239 targets** — prior observation only.
- **160 buckets** — prior observation only.
- **161 requests** — **invalid** as a current bound; it was derived from the
  now-rejected probe-reuse economics and an unverified bucket count.

No request cap may be set until target binding, §AF derivation and independent
review are all complete.

## 12. Safe implementation order

1. independent review of **this** architecture;
2. v23 target-population schema + provenance implementation;
3. independent implementation review;
4. **supply the authoritative NBA March corpus** (or acquire a new bounded parent
   under a fresh precommitted manifest);
5. instantiate the target-bound corpus and verify it;
6. close the §AF composition;
7. independent §AF review;
8. declare the real Stage-A manifest;
9. independent plan preflight;
10. bounded acquisition.

Step 4 is a **data** gate, not a code gate, and steps 8–10 cannot begin before it.

## 13. Status

Schema **v22 / 22 / 61 unchanged**; no migration written. B3 deferred. No real
plan, no probe registered, Stage A not run, linking provider unregistered,
`ATTESTED_GENERATIONS` unchanged, **F1-R blocked**.

## 14. Exact next authorization boundary

> ~~**Independent adversarial review of this architecture**, then the v23
> target-population implementation.~~ **Done — see
> `CORPUS_TARGET_POPULATION_BINDING_ARCHITECTURE_INDEPENDENT_REVIEW.md`.**
>
> **Next: V23 target-population implementation in the REPAIRED shape** — three
> tables (`reconstruction_corpus_targets`, `reconstruction_corpus_target_runs`,
> `reconstruction_corpus_target_seals` carrying the acquisition manifest binding
> and policy version), the composite `target-binding-v1` digest, the
> official-listing projection verifier with cursor-chain closure, and
> `verify_corpus_target_population`.

The artefact checklist in §4 is also extended by the review: beyond the preserved
database, target binding needs `provider_game_references`, the **acquisition
manifest file** and the **resume checkpoint file**. Without the last two the
derivation digest cannot be computed and the historical March corpus is
permanently unusable for target binding — completeness would be unfalsifiable.
