# RB-1 … RB-5 Adjudication — Independent Adversarial Architecture Review

**Starting HEAD:** `390ea66` (= `origin/main`, tree clean, schema v23 / 23 migrations / 64 tables; `390ea66` is documentation only; no f024 exists).
**Schema after this review:** **v23 / 23 / 64 — UNCHANGED. Nothing implemented.**
**Provider requests:** 0. **Credits:** 0. Guards: 31 armed, 11/11 probes blocked.

## VERDICT: **ACCEPTED WITH REPAIRS**

The two structural choices — a prospective acquisition ledger, and portable
identity built on a stable provider key — are correct and survive attack. Three
load-bearing repairs are required, and one adjudication is **reversed**.

**The reversal is the important part.** The adjudication left "may legacy-attested
March feed the denominator?" to a claim-strength decision by the user. That is not
a preference question. **It is NO**, and this review takes the decision.

| # | Adjudication | Verdict |
|---|---|---|
| 1 | Chain A ledger model | **ACCEPTED** |
| 2 | Plan-before-contact enforceable | **REPAIR REQUIRED** (RR-1, SEVERE) |
| 3 | Root-unit-before-contact | **REPAIR REQUIRED** (RR-1) |
| 4 | Successor-before-own-contact | **ACCEPTED with RR-1** |
| 5 | Fetch-then-declare attack | **NOT CLOSED as written** (RR-1) |
| 6 | Failed-unit accounting | **ACCEPTED with RR-4** |
| 7 | Retry / accepted-evidence selection | **REPAIR REQUIRED** (RR-5) |
| 8 | Plan finalization / root closure | **NO SEAL NEEDED — derive roots instead** (RR-3) |
| 9 | Manifest artefact vs semantic plan digest | **REPAIR REQUIRED** (RR-6) |
| 10 | Historical Level 2+ strength | **ACCEPTED, wording tightened** |
| 11 | Legacy vs prospective class semantics | **ACCEPTED** |
| 12 | Legacy admissibility for denominator-sensitive research | **NO — REVERSED** |
| 13 | §AF minimum class | **PROSPECTIVE_LEDGER_COMPLETE** |
| 14 | E0 / F1-R minimum class | **PROSPECTIVE_LEDGER_COMPLETE** |
| 15 | Target stable-key contract | **ACCEPTED with RR-7** (typing) |
| 16 | Local surrogate contract | **ACCEPTED, semantics sharpened** |
| 17 | First identity attestation gate | **ACCEPTED, restated** |
| 18 | TEAM-A version/digest binding | **REPAIR REQUIRED** (RR-2, HIGH) |
| 19 | Schedule/time consistency role | **ACCEPTED with a minimal guard** |
| 20 | `target-set-v2` portability | **ACCEPTED** (replay-proved) |
| 21 | `target-source-scope-v2` portability | **ACCEPTED with RR-8** |
| 22 | `target-derivation-v2` portability | **ACCEPTED** (replay-proved) |
| 23 | Accepted-evidence digest selection | **REPAIR REQUIRED** (RR-9) |
| 24 | Policy hierarchy / persistence | **ACCEPTED with RR-2** |
| 25 | v24 schema minimality | **ACCEPTED — four objects** |
| 26 | Plan seal required? | **NO** (see 8) |
| 27 | `ingestion_runs` as execution | **ACCEPTED** |
| 28 | Fresh prospective historical acquisition | **VALID for Lane-R target enumeration** |
| 29 | Real March cheap path | **NOT ADMISSIBLE for denominator-sensitive uses** |
| 30 | Next boundary | see §10 |

---

## 1. RR-1 (SEVERE) — the ledger does not close RB-5 as written

The adjudication's threat table answers *"plan created after responses"* with
*"plan digest is over the manifest; attempts FK to units FK to plan."* That is not
a chronology argument. Reproduced: a plan digest computed **after** seeing results
is byte-identical to one computed before — `247b3120…` both ways. Deterministic
identity carries no time.

**What is actually enforceable.** A timestamp-ordering trigger works. Built and
verified on a real v23 database:

| Attack | Result |
|---|---|
| unit registered before its plan | refused — *"unit registered before its plan"* |
| attempt requested before its unit | refused — *"attempt requested before its unit was registered"* |
| successor unit registered before its parent response arrived | refused |

**But it is only as good as who stamps the clock.** Also reproduced: an attacker
who fetches at 12:00 and then declares a plan "at" 09:00 passes **every** ordering
trigger, because the triggers compare two caller-supplied values.

**RR-1.** The ledger closes RB-5 only if the **executor owns provider contact**:

```
execute_acquisition_unit(unit_id)
    assert plan + unit exist and are registered      -- repository clock
    write attempt row, requested_at := repository clock
    contact provider                                  -- the ONLY contact path
    persist raw bytes + hashes before any parsing
    derive outcome; derive successor obligation if next_cursor present
```

`registered_at` and `requested_at` must never be caller parameters. A raw response
that no attempt row claims, or an attempt whose `requested_at` precedes its unit,
is inadmissible. This must be stated in the architecture, not left to the
implementer — it is exactly the "fetch-then-declare" class B1 and B2 already
closed twice.

**Honest limit.** This is tamper-evidence over an audited API path, not
cryptographic time. Direct SQL with fabricated timestamps remains outside the
boundary, and the architecture must say so rather than implying proof.

## 2. RR-2 (HIGH) — TEAM-A is not bound into identity admission

Identity admission resolves provider team ids through TEAM-A, but the adjudication
binds nothing about *which* TEAM-A. The map has a digest
(`ae21c26b…`), a policy version (`g5-team-attestation-v1`) and a format version
(`team-a-map-v1`). If the map changes, replay resolves the same provider team ids
to different canonical teams and every prior attestation silently means something
else — a portability hole in the very layer the adjudication chose to make
portable.

**RR-2.** `identity-admission-v1` must bind the TEAM-A **map digest** and
**namespace generation**, and the attestation row must persist them. Verified
positively: `attested_canonical_team` is a pure id lookup — the docstring is
explicit that no name, alias, abbreviation or nickname is consulted, and the code
touches only `league_id`, `provider`, `generation` and the id. The adjudication's
claim that no name matching is needed **holds**.

## 3. RR-3 — no plan seal; derive the root set instead

Reproduced: a new root unit can be appended to an existing plan, which would
reopen denominator expansion. The adjudication lists no plan seal.

A seal is nonetheless **not** the right repair. For the target-listing
acquisition the root unit set is **fully derivable** from manifest semantics —
provider, endpoint, league, date window, page size. So the verifier must
**recompute the expected root set from the manifest and compare**, rather than
trusting stored root rows. A stored root outside the derived set fails; a derived
root with no row fails. That is strictly stronger than a seal and adds no object.

Successor units are not derivable from the manifest — they are causally derived
from accepted responses, and the chain's explicit `next_cursor: null` terminus is
the closure. The adjudication's own §3 wording is right; only the "persisted roots
are the authority" implication needs correcting.

## 4. RR-4 … RR-9 — the smaller repairs

- **RR-4 (failed units).** "Outcomes are derived" is only true where evidence
  exists. `TRANSPORT_FAILURE` has no raw response, so an attempt row with a status
  string is a *claim*, not a derivation. Minimum immutable evidence: attempt
  identity, `attempt_started_at` (repository clock), exception class name and a
  sanitized message, plus a frozen classification policy. Say "recorded under a
  frozen classification policy", not "derived".
- **RR-5 (retry).** Pin the rule now: **the first policy-valid success terminates
  the unit; later attempts are recorded but can never be accepted.** No curator
  picks among two differing successes after seeing them; two differing successes
  for one unit is a conflict that fails closed.
- **RR-6 (plan digest).** Separate the two claims, as B2 taught. Persist
  `manifest_content_sha256` (artefact identity, whitespace-sensitive) **and**
  `plan_digest` (canonical acquisition semantics). Reformatting the manifest must
  not create a new scientific acquisition plan.
- **RR-7 (provider id typing).** Reproduced: `"18447686"` and `18447686` yield
  different digests. The preserved payload stores **integers**. Freeze one
  normative representation — decimal string, no leading zeros, refuse anything
  else — rather than accepting both via coercion.
- **RR-8 (canonical request params).** Define what enters
  `target-source-scope-v2`: provider, endpoint, and the **sanitized** semantic
  params only. Never a key, never a receipt timestamp, never a header. Cursor
  values normalize under the same rule as RR-7.
- **RR-9 (accepted-evidence selection).** "Earliest page" depends on execution
  order. Use the **sorted set of all accepted responses' content hashes** for the
  unit set — deterministic and order-free. And per §25 of the brief: acceptance
  selects exactly one response per unit, so the digest never sees duplicates by
  construction; digest-level duplicate refusal is a belt-and-braces invariant, not
  the mechanism.
- **Families scope.** The plan must bind the **games-family projection** of the
  manifest, deterministically derived. Otherwise a lineups or plays failure would
  invalidate a target population it has nothing to do with.

## 5. The reversal — legacy-attested March may never feed the denominator

The adjudication ended §9 with *"a decision about claim strength, not cost"* and
handed it to the user. An independent review cannot do that, because the failure
is not a matter of preference.

**The undetectable failure is not random.** BALLDONTLIE paginates a date range in
ascending id order, which tracks date order. A pre-preservation deletion therefore
removes games from the **end** of the window — late March. That is systematically
correlated with time, and with everything time carries late in a season: playoff
positioning, rest patterns, tanking. So the residual risk is not "we might be
missing a few random games"; it is "we might be missing a structured tail."

Consequences: a completion rate over a truncated denominator is **inflated**; a
backtest over a truncated population is biased in a direction that flatters
recency; calibration and training inherit the same skew.

### Downstream permission matrix

| Use | Legacy-attested admissible? |
|---|---|
| Descriptive debugging, acquisition-code audit | **YES** |
| Identity-attestation implementation testing | **YES** |
| Feature-code / mechanism testing | **YES** |
| Comparing a future prospective retrieval against the preserved 239 | **YES — and valuable** |
| Exploratory statistics | **only if no artefact reaches a validated claim** |
| Model training | **NO** — distribution skew propagates |
| Calibration | **NO** |
| Backtesting / EV / profitability | **NO** |
| Hit-rate, coverage, completion-rate claims | **NO** |
| F1-R target denominator | **NO** |
| §AF real target enumeration | **NO** |
| Production validation | **NO** |

**Verdict 12: NO** — this is the brief's option A/B hybrid: legacy-attested is
usable, but only in a lane whose outputs can never be mistaken for
prospective-complete results, and never for anything denominator- or
distribution-sensitive.

**Verdicts 13/14:** §AF, E0 and F1-R all require **PROSPECTIVE_LEDGER_COMPLETE**.
`verify_corpus_target_population` must therefore return a **certification class**,
not a boolean, and callers must declare the minimum strength they need. A shared
`verified=True` for both classes would guarantee the confusion this whole exercise
exists to prevent.

### Why the fresh acquisition genuinely fixes it

A current retrieval of March 2026 under the new ledger proves *"our procedure
completely captured what the provider returns today for that window"* — not *"this
is what the API exposed during March 2026."* For **target enumeration** that
distinction favours the fresh retrieval: the official listing today reflects
applied corrections and resolved postponements, which is exactly the retrospective
official population Lane-R wants. So the fresh acquisition is not a consolation
prize; for this purpose it is the better evidence. **Verdict 28: valid.**

The preserved 239 keeps real value as an independent cross-check against that
retrieval — a disagreement would be highly informative.

## 6. What survived attack unchanged

- **Portability replay.** Two databases differing in every surrogate — plan row
  placement, run ids, attempt ids, raw-response ids, `game_id` ULIDs, paths —
  produce identical `target-set-v2`, `target-source-scope-v2` and
  `target-derivation-v2` digests. Verified. Ordering-invariant, membership-
  sensitive.
- **Stable key** `(league_id, provider, provider_game_id)`, scoped honestly to one
  official source namespace. It does not attempt cross-provider identity and does
  not touch B3.
- **`game_id` as local surrogate only.** The review's §20 point is well taken and
  sharpens the gate's purpose: since multiple ULIDs are equivalent local
  representations, the invalid case is **not** "a different random id" but
  "a stable key mapped to a row carrying different semantic facts". The gate
  should be stated that way.
- **Level 2+.** Independently re-verified: one distinct manifest blob across all
  64 commits containing it, `sha256` matching working tree and
  `checkpoint.manifest_hash`, and `2e5a082` a DAG ancestor of the result-report
  commits. Tightened wording: ancestry proves the manifest was not amended after
  results were reported. It does **not** prove the acquisition happened after the
  manifest commit — the acquisition could predate both.
- **239/239 ELIGIBLE**, independently re-run: 0 unresolved, 0 ambiguous, 0
  malformed, 0 conflicting; all 30 TEAM-A NBA attestations exercised; 0 descriptor
  collisions.
- **Four v24 objects, `ingestion_runs` as execution.** Attempts bind unit → run,
  which defines execution adequately. No fifth object justified.

## 7. Time and orientation

Schedule/start is **recorded consistency evidence at admission, not identity** —
a reschedule corrects a fact without creating a game. But teams alone are too
weak: mapping a stable key to a game with the right teams and a wildly different
date must fail. Minimal guard: the canonical game's start must equal the preserved
listing `datetime` **at admission time**, recorded in the attestation; later
corrections are governed by the existing corrections policy and do not invalidate
the attestation. Orientation is exact — home must be home, away must be away, and
`home == away` refuses.

## 8. Direct-SQL threat model, updated

| Attack | Control |
|---|---|
| plan/unit created after responses | executor-owned clock + ordering triggers (**RR-1**) — audited-path tamper-evidence, **not** cryptographic time |
| root unit appended later | root set derived from manifest, not trusted (**RR-3**) |
| successor fetched before its obligation exists | ordering trigger on parent `received_at` |
| response body altered before successor derivation | bytes + hashes persisted on receipt, before parsing |
| two differing successes for one unit | conflict, fails closed (**RR-5**) |
| TEAM-A map changed after attestation | map digest bound (**RR-2**) |
| provider id retyped | one normative representation (**RR-7**) |
| secrets entering the source digest | sanitized params only (**RR-8**) |
| legacy corpus presented as prospective-complete | certification **class**, not boolean |
| evidence discarded before preservation | **RETAINED** — and the reason legacy is barred from denominators |

## 9. Reconciliation

`TARGET_POPULATION_PROVENANCE_BLOCKERS_ARCHITECTURE.md` is updated in this same
commit for RR-1, RR-2, RR-3 and the reversal of verdict 17. Its other verdicts
stand. It remains the authoritative v24 implementation contract.

## 10. Exact next authorization boundary

> **V24 implementation** — acquisition ledger with **executor-owned contact and
> repository-stamped chronology**, derived root-set verification, TEAM-A-bound
> identity admission, the v2 digest policies, and a certification **class**
> returned by `verify_corpus_target_population`.

Then: independent v24 review → **fresh prospective March acquisition under the
ledger** (denominator-grade) → corpus construction → §AF closure.

The preserved March 239 is not on the critical path any more. It becomes the
cross-check against the fresh retrieval, which is a better use for it than a
denominator it cannot support.

## 11. Status

Schema **v23 / 23 / 64** unchanged; no migration; nothing implemented. No games
materialized, no provider references resolved, no corpus instantiated, no §AF run,
no provider contact. TEAM-A read-only and unmodified. B3 deferred. P1
unauthorized. No Stage-A plan. No probe. `REGISTERED_LINKING_PROVIDERS` empty.
`ATTESTED_GENERATIONS` unchanged. G5 NOT run. No crosswalk. **F1-R blocked.**
239 remains VERIFIED as a preserved observation; **160 / 161 remain invalid**.
