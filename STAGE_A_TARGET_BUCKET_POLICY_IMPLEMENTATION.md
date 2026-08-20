# §AF — Stage-A Target → T−60 → First-Pass Bucket Policy

**Starting HEAD:** `e667f4a` (= `origin/main`, tree clean, schema v22 / 22 migrations / 61 tables).
**Schema after this task:** **v22 / 22 / 61 — UNCHANGED. No migration.**
**Provider requests:** 0. **Credits:** 0.

## VERDICT: **RETAINED BLOCKER**

The bucket **algorithm** is specified, implemented and independently tested. The
**target population** it must be applied to is **not derivable from the parent
corpus at v22**, so §AF cannot close. §1 of the authorization directs me to stop
and document a design conflict rather than pick a convenient interpretation, and
that is what this is.

| Component | State |
|---|---|
| Reviewed policy reconstructed from repository evidence | **done** |
| T−60 / 5-minute-floor algorithm | **implemented, 42 tests** |
| Mapping comparison (keyed sets, pigeonhole-safe) | **implemented** |
| Target population from parent corpus | **BLOCKED — not derivable** |
| Composition into `certify_stage_a` / enrichment | **not done** (would be vacuous) |
| Real-corpus dry run | **NOT AVAILABLE LOCALLY** |

---

## 1. The reviewed policy, reconstructed from evidence

From `NBA_F1R_TARGET_ANCHOR_PREFLIGHT.md` §1, which states the executable Repair-4
contract and supersedes the original circular design:

| Step | Rule |
|---|---|
| 1 | Take the retrospective official start `S_final` as a **search hint only** |
| 2 | Query the historical snapshot at `S_final − 60 min`, floored to the grid |
| 3 | Read the contemporaneous `commence_time` **from the snapshot** |
| 4 | `T_cut := commence_time_snapshot − 60 min`, bounded to 3 iterations |

**Two instants, and conflating them is the circularity Repair 4 exists to
prevent:**

- the **first-pass REQUEST bucket** (step 2) is computable before any provider
  contact and is what a Stage-A plan declares — **this is what §AF verifies**;
- **`T_cut`** (step 4) derives from the provider's contemporaneous
  `commence_time` and cannot be known at plan time at all.

**Leakage analysis (§24).** Using the retrospectively-known final start as the
hint is explicitly permitted and reviewed, because it decides only *when to
look*, never *what is true*. The preflight's decisive sentence: *"`commence_time`
from the snapshot is the availability evidence. The retrospective final start is
**never** the anchor."* Nothing in §AF touches `AsOfReader` or `_feature_cutoff`.

**Provider grid.** The provider's snapshot instants were measured off the wall
clock at roughly `:37`. That is an *answer*, not a request. The request grid is
exact 5-minute UTC and `provider_snapshot_timestamp` never participates —
asserted by a test that every derived bucket lands on a 300-second boundary.

## 2. The algorithm, implemented

```
parse S_final under a strict canonical-UTC contract
  -> subtract exactly 60 minutes          (SUBTRACT, minutes, not seconds)
  -> floor the RESULT down to 300 seconds (FLOOR, never round or ceil)
  -> canonical UTC string
```

Order of operations is pinned deliberately even though the two orderings agree
while 60 stays divisible by 5 — the policy must say what it means, not merely
reproduce today's output.

Hand-computed test literals cover: on-boundary, +1µs, −1µs before the next
boundary, hour/date/month/year rollover, and a leap day. Strict parsing refuses
naive values, `+00:00` offsets, lowercase `z`, space separators, hour 24,
impossible calendar dates and non-canonical fractional widths.

Unresolved (no hint) and ambiguous (two conflicting hints, no reviewed
resolution) both fail closed. Duplicate *identical* evidence resolves to one
record. Derivation is order-independent.

Comparison uses **exact keyed set equality in both directions**, because the
architecture's pigeonhole finding means a dropped target can leave the bucket set
byte-identical when another target shares its bucket — pinned by a test.

## 3. Why §AF cannot close — the blocker, precisely

§AF requires the target population to come from the **parent corpus**, never the
manifest, so an omitted target is visible. Three independent checks show that is
not possible at v22:

1. **`reconstruction_corpus_versions` carries no target scope.** Its columns are
   `league_id`, policy versions and digests. There is no season, no date range,
   and no corpus→game membership table. The only corpus-to-games link in the
   whole schema is `stage_a_plan_targets` — which is *the plan's own claim*, and
   therefore exactly what must not be trusted as the source of truth.
2. **`target_set_digest` has no production derivation.** No code computes it from
   evidence; `retrospective/runner.py` defaults it to the literal string
   `identity-audit-no-targets`, and every existing test passes `"t"` or `"TGT"`.
   It is a free-text caller-supplied label, so comparing a plan against it proves
   nothing.
3. **`reconstructed_input_provenance` is not a target population.** It records
   per-feature-input eligibility keyed by `provider_game_id` and `feature_family`,
   and is written only by an F1-R run, which is blocked.

**Why I did not work around it.** Enumerating "every NBA game in some month" from
the `games` table would make the target set a property of whatever rows a given
database happens to hold rather than of the corpus. Two copies of the "same"
corpus could then derive different populations, and the §AF comparison would be
meaningless for portability — while *looking* like verification. That is an
invented scoping rule presented as evidence, which is the failure mode every
review in this sequence has been catching.

`derive_target_population` and `verify_stage_a_target_bucket_policy` therefore
**refuse**, with the reason in the exception, and tests pin the refusal.

## 4. Composition deliberately not done

§AF is not wired into `certify_stage_a` or `enrich_corpus_with_market_lane`.
Composing a verifier that always refuses would either break every existing
Stage-A test or force a "skip when unavailable" branch — and a skippable
scientific gate is worse than an absent one, because it reads as coverage. The
single named entry point exists so that wiring it in later is a one-line change
at a reviewed seam.

## 5. Real-corpus dry run (§21) — **NOT AVAILABLE LOCALLY**

Every database under `data/` was scanned: **none contains any NBA game row.**

Therefore:

- the authoritative NBA March parent corpus is **not present**;
- the historical **239 targets / 160 buckets** expectation is **NOT confirmed**
  by this task and must not be treated as verified;
- the future ordinary request count **cannot be honestly derived yet**. It is
  whatever the independent derivation yields once the authoritative corpus is
  supplied — not 160, not 161.

Real-plan declaration therefore depends on supplying the authoritative parent
corpus **and** closing the target-population blocker.

## 6. Schema

**v22 remains sufficient for the algorithm.** No migration was written. Whether
closing the blocker needs one is a design decision for the next task: the corpus
must gain a way to enumerate or scope its target population, which could be a
membership table, a scope binding, or a derived `target_set_digest` over an
enumerable set. §26 directs stopping before implementing such a migration, and
that boundary is respected here.

## 7. Non-regression

B1 (36 tests) and B2 (35 + 48 tests) unaffected; no Stage-A provenance behaviour
changed. `REGISTERED_LINKING_PROVIDERS` empty; `ATTESTED_GENERATIONS` unchanged;
no canonical game created; no crosswalk; no identity work — §AF decides *when* to
request, never *which* provider event is a game.

## 8. Status

| | |
|---|---|
| **§AF** | **RETAINED BLOCKER** — algorithm done, target population not derivable |
| **B3** | OPEN |
| Real Stage-A plan declared | **NO** |
| Stage A run | **NO** |
| Probe registered | **NO** |
| Linking provider / G5 / crosswalk / F1-R / E1 / P1 | **NO** |

> **BLOCKER ADJUDICATED.** See `CORPUS_TARGET_POPULATION_BINDING_ARCHITECTURE.md`:
> membership will come from the preserved official listing raw responses of
> bound acquisition runs, materialized as explicit corpus membership rows with a
> derived `target-set-v1` digest (**v23 required**). Legacy `target_set_digest`
> values stay LEGACY UNBOUND and can never back §AF. A retained **data**
> dependency remains: the authoritative NBA March corpus is not present locally.

## 9. Exact next authorization boundary

> **Corpus target-population binding** — give
> `reconstruction_corpus_versions` a reviewed, portable way to enumerate its
> official target set, and derive `target_set_digest` from it.

That is the blocker. Until it closes, §AF cannot verify anything, no real plan
can be declared, and no acquisition can run. The bucket algorithm shipped here is
ready for independent review on its own terms.
