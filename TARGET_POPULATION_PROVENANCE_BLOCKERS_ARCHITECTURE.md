# Target-Population Provenance Blockers — Architecture Adjudication (RB-1 … RB-5)

**Starting HEAD:** `af58671` (= `origin/main`, tree clean, schema v23 / 23 migrations / 64 tables).
**Schema after this task:** **v23 / 23 / 64 — UNCHANGED. No migration. Nothing implemented.**
**Provider requests:** 0. **Credits:** 0. Zero-network guards: 31 armed, 11/11 probes blocked.

## VERDICT: **ACCEPTED WITH REPAIRS/CONDITIONS**

All five blockers have decidable resolutions. Two empirical results drive them:

- **239/239 provider games are ELIGIBLE** under the proposed exact identity
  admission policy — zero unresolved, ambiguous, malformed or conflicting.
- The manifest is **byte-immutable across all 64 commits containing it** and its
  commit is a **cryptographic ancestor** of the commit reporting the acquisition
  results — clock-independent, but still short of proving the execution was
  launched *because of* it.

The cheap historical path survives, **but only as an explicitly weaker class**.

> **RECONCILED BY INDEPENDENT REVIEW.** See
> `TARGET_POPULATION_PROVENANCE_BLOCKERS_ARCHITECTURE_INDEPENDENT_REVIEW.md`,
> authoritative wherever the two disagree. Four changes below are load-bearing:
>
> * **RR-1 (SEVERE) — verdict 2/RB-5 is NOT closed as written.** Deterministic
>   plan/unit digests carry no chronology (a digest computed after seeing results
>   is byte-identical), and timestamp-ordering triggers pass trivially when the
>   caller supplies the timestamps. The ledger closes RB-5 only if the
>   **executor owns provider contact** and stamps `registered_at`/`requested_at`
>   from the repository clock. Tamper-evidence over an audited path, **not**
>   cryptographic time.
> * **RR-2 (HIGH) — identity admission must bind TEAM-A.** `identity-admission-v1`
>   must commit the TEAM-A **map digest** and **namespace generation**, or a later
>   map change silently re-means every attestation.
> * **RR-3 — no plan seal; derive the root set.** Root units are fully derivable
>   from manifest semantics, so the verifier recomputes the expected root set and
>   compares, rather than trusting stored root rows (which can be appended).
> * **Verdict 17 REVERSED.** "Safe as a legacy-attested class, claim strength is
>   the user's call" is withdrawn. Legacy-attested March is **NOT ADMISSIBLE** for
>   any denominator- or distribution-sensitive use — §AF, F1-R, E0, training,
>   calibration, backtesting, EV. The undetectable deletion is a **structured
>   tail**, not random noise, because pagination tracks date order. §AF/E0/F1-R
>   require **PROSPECTIVE_LEDGER_COMPLETE**, and the verifier must return a
>   certification **class**, not a boolean.
>
> Smaller repairs RR-4 … RR-9 (failure-outcome evidence, retry acceptance rule,
> artefact-vs-semantic plan digest, provider-id typing, sanitized request params,
> deterministic accepted-evidence selection, games-family scoping) are itemized in
> the review.

| # | Question | Verdict |
|---|---|---|
| 1 | Scientific corpus portability | **PORTABLE ACROSS FAITHFUL REBUILDS** |
| 2 | Acquisition provenance | **NEW PROSPECTIVE LEDGER REQUIRED** |
| 3 | Historical March acquisition class | **LEGACY-ATTESTED ONLY** |
| 4 | Required-unit accounting | precommitted root units + derived successors; every unit carries an outcome |
| 5 | Pagination model | **ROOT UNIT precommitted, successor pages derived from the prior response's `next_cursor`** |
| 6 | Retry/attempt model | append-only attempts; exactly one accepted terminal per unit; failures never erased |
| 7 | Stable target identity | `(league_id, provider, provider_game_id)` |
| 8 | Local `game_id` role | **LOCAL SURROGATE ONLY** |
| 9 | First NULL→`game_id` admission gate | exact provider-team-id resolution through TEAM-A + immutable attestation |
| 10 | TEAM-A role | exact id→canonical resolution, no fallback; map unchanged |
| 11 | Target-membership digest | `target-set-v2` over sorted stable target keys |
| 12 | Source evidence digest | `target-source-scope-v2` over content, not row ids |
| 13 | Derivation digest | `target-derivation-v2` over manifest + unit semantics + accepted evidence |
| 14 | Manifest precommit strength | **LEVEL 2+** — immutable bytes, hash-matched, DAG-ancestor; **not Level 3** |
| 15 | RB-6 policy persistence | all five policy versions persisted on the seal |
| 16 | Schema | **V24 REQUIRED** |
| 17 | Real March cheap path | ~~SAFE ONLY AS LEGACY-ATTESTED CLASS~~ → **NOT ADMISSIBLE for denominator-sensitive use; FRESH PROSPECTIVE ACQUISITION REQUIRED** (reversed by review) |
| 18 | 239 identity preflight | **239 ELIGIBLE / 0 / 0 / 0 / 0** |
| 19 | §AF readiness | **YES, SUBJECT TO DATA** — and gated on **PROSPECTIVE_LEDGER_COMPLETE** (review) |
| 20 | Overall | **ACCEPTED WITH REPAIRS/CONDITIONS** |

---

## 1. The two trust chains, kept apart

**Chain A — acquisition completeness** (RB-1, RB-2, RB-5): *were these the right
requests, all of them?* **Chain B — target identity** (RB-3, RB-4): *is this
provider game that canonical game, portably?* They compose only at the end: a
target-bound corpus needs a certified Chain A **and** a certified Chain B, and
neither substitutes for the other.

## 2. Chain A — the acquisition ledger (RB-1, RB-2)

`ingestion_runs` cannot serve. It has no manifest binding, no notion of a
*required* unit, and no way to represent a unit that was never attempted. Its
`args_json` for the historical March listing run is `{"includes":[],"tier":"goat"}`.

**Four objects, each answering "what stays undetectable without it?"**

| Object | Identity | Undetectable without it |
|---|---|---|
| `acquisition_plans` | **deterministic** — digest over manifest sha256 + plan policy + provider + league + window + families | which declaration an execution was answering; RB-1's "unrelated same-window acquisition" |
| `acquisition_units` | **deterministic** — digest over the semantic unit definition | a required request that was never made; RB-2 |
| `acquisition_attempts` | surrogate row id, append-only | a failure erased by a later success; retry history |
| `acquisition_executions` | surrogate (reuse `ingestion_runs`) | which process did the work — **local provenance, never in a digest** |

**Plan and unit ids are deterministic; execution and attempt ids stay local.**
That is the §8 distinction between semantic identity and row identity, and it is
what makes Chain A portable.

### Why this closes RB-1

`required_listing_runs` becomes `required_units(plan_id)` — a lookup of rows
precommitted **before contact**, not a query over whatever evidence a database
now holds. Two independent acquisitions of the same window are two different
plans (different manifest bytes → different plan digest) or two executions of one
plan; either way each corpus binds its own plan and an unrelated later
acquisition cannot enter its required set. A date window stops being an identity.

### Why this closes RB-2

Completeness is evaluated over **units**, not surviving successes. Every unit must
reach exactly one accepted terminal attempt. Outcomes are **derived** where
possible — `NOT_ATTEMPTED` (no attempt row), `TRANSPORT_FAILURE`,
`HTTP_FAILURE`, `AUTH_FAILURE`, `QUOTA_FAILURE`, `MALFORMED_RESPONSE`,
`SUCCESS_WITH_NEXT_CURSOR`, `SUCCESS_TERMINAL` — rather than trusted from a
caller-written status column. A unit with no successful attempt makes the
acquisition incomplete; it can no longer vanish.

## 3. Pagination — the part that cannot be fully precommitted

Cursors are discovered, so requiring them in advance is impossible. The model:

```
ROOT UNIT      precommitted before contact:
               (provider, endpoint, league, start_date, end_date, per_page, family)
SUCCESSOR UNIT derived deterministically: every accepted response carrying
               next_cursor = C REQUIRES exactly one successor unit for cursor C
TERMINATION    an accepted response with an explicit "next_cursor": null
```

Completeness = the root unit exists **and** the derived chain closes on an
explicit null terminus. This is exactly the cursor-chain closure v23 already
implements (hardened by RV-2 to require `meta` on every page); the ledger adds
the precommitted **root** the chain hangs from, which is what was missing.

The preserved March root request carries
`start_date=2026-03-01, end_date=2026-03-31, per_page=100`, matching the
manifest's declared `date_range` exactly — so the root is checkable against the
declaration even historically.

## 4. Retry, and what enters scientific identity

Attempts are append-only and failures are never erased. Exactly one attempt per
unit may be **accepted**, chosen deterministically (the unique successful attempt;
more than one identical-content success is a no-op, more than one differing
success is a conflict that fails closed) — never by a caller picking the
convenient one afterwards.

**Failures make completeness fail but do not enter the corpus content address.**
The evidence digest commits the *accepted* evidence and the plan; the attempt
ledger preserves everything and is linked as execution provenance. So two clean
executions of one plan yielding byte-identical evidence produce the **same**
target-bound corpus — the right answer for a content-addressed scientific object
(§20) — while their differing retry histories remain fully visible.

## 5. Chain B — RB-3 portability

**Verdict 1: PORTABLE ACROSS FAITHFUL REBUILDS.** The architecture has called the
corpus content-addressed throughout; mixed semantics — some digest inputs
portable, others random — is the worst of the options and is what v23 shipped.

**Verdict 8: `games.game_id` is a LOCAL SURROGATE ONLY.** It stays the membership
table's foreign key. It never enters a scientific digest.

**Verdict 7: the stable target key is `(league_id, provider, provider_game_id)`.**
Option B (deterministic canonical game ids) would rewrite the project's identity
architecture, which `db/ids.py` deliberately made surrogate because a game's
natural key changes. Option A is the smallest safe change and every input is
already preserved official evidence.

The verifier must then prove the surrogate mapping is exactly one-to-one: a stable
key claimed by two local games, or one local game claimed by two stable keys
within one provider namespace, both fail closed. Two faithful rebuilds may hold
different `game_id`s and **must** still derive the same corpus identity — that is
the property being bought.

## 6. Chain B — RB-4, the first-resolution admission gate

Immutability after assignment is not correctness. The gate for
`(balldontlie, P, game_id=NULL) → G`:

1. `P` appears in an **accepted** listing response of the bound plan;
2. `home_team.id` and `visitor_team.id` resolve through **TEAM-A** exactly
   (`lg_nba`/`balldontlie`/`v1`), no aliases, no name normalization, no fallback;
3. home ≠ away;
4. the preserved `datetime` is well-formed;
5. `G` carries exactly those two canonical teams and that start;
6. an **immutable attestation** records the stable key, `G`, both canonical teams,
   the identity policy version and the **content hash of the exact response** the
   descriptor came from.

**On self-confirmation (§11).** If `G` is created from the same payload, comparing
them proves nothing about the outside world — and this design does not pretend
otherwise. What the attestation makes falsifiable is the **binding**: that exactly
one local surrogate stands for this stable key, that it was derived from named
preserved evidence, and that the derivation replays. Correctness of the descriptor
itself is inherited from the official provider plus TEAM-A, which is an existing
reviewed trust root, not a new one.

**Verdict 15: `status` is OBSERVATION STATE, not identity.** All 239 currently read
`Final`; that is a current-state fact and must not make the same game a different
identity. It is recorded as consistency evidence only.

**Verdict on time:** identity is the provider tuple alone. Teams and `datetime` are
**admission evidence**, not identity components — so a reschedule corrects a fact
without creating a new game, consistent with the existing corrections policy.

## 7. The 239 preflight (READ-ONLY — nothing assigned, nothing written)

| Outcome | Count |
|---|---|
| **ELIGIBLE** | **239** |
| UNRESOLVED_TEAM | 0 |
| AMBIGUOUS | 0 |
| MALFORMED | 0 |
| CONFLICTING | 0 |

All 30 TEAM-A NBA attestations are exercised and all 30 canonical team rows exist
locally. Zero descriptor collisions. All 239 are `status: Final`, none postponed.
142 games have `date` ≠ the UTC prefix of `datetime` — the expected venue-local
vs UTC divergence, not a defect, and precisely why `game_date_local` exists.

The gate is therefore **technically satisfiable** on this evidence. That is a
statement about sufficiency of information, not authorization to run it.

## 8. RB-5 — exactly what Git proves, and what it does not

| Level | Claim | Status |
|---|---|---|
| 1 | manifest bytes existed in history | **YES** — and **one distinct blob across all 64 commits containing it**: never amended |
| — | those bytes hash to what the seal/checkpoint record | **YES** — `sha256` of the committed blob, the working tree and `checkpoint.manifest_hash` all equal `901cb9de…` |
| 2 | a later committed artefact names that hash | **YES** — the execution review and results-repair reports |
| **2+** | the manifest commit is a **cryptographic ancestor** of the commit reporting acquisition results | **YES** — `2e5a082` is an ancestor of `f3f7fde`, `e09f546`, `56823a4`. Clock-independent: it rules out amending the manifest after results were reported |
| 3 | an execution row binds the run to that manifest | **NO** — the basis of RB-5 |

**Verdict 14: LEVEL 2+.** Stronger than the review credited, because ancestry is
DAG-cryptographic rather than a commit timestamp — and commit timestamps are
explicitly not trusted here. Still short of Level 3: ancestry cannot exclude a
manifest authored to match already-obtained results and committed before the
report. Only a ledger row written at acquisition time closes that.

## 9. Verdict 3 / 17 — the historical class

> **LEGACY-ATTESTED ONLY**, and the cheap path is ~~**SAFE ONLY AS A
> LEGACY-ATTESTED CLASS**~~ → **NOT ADMISSIBLE for denominator- or
> distribution-sensitive uses** (reversed by the independent review; see its §5
> and permission matrix).

What the historical evidence *can* support, and this is genuinely a lot: the
listing population is self-proving for page completeness (intact cursor chain,
explicit null terminus, `meta` on every page, all body hashes recompute), the root
request matches the manifest's declared window, the manifest bytes are immutable
and DAG-anterior, and identity admission is 239/239 satisfiable.

What it **cannot** support: proof that no evidence was discarded before
preservation. A trailing page deleted together with a rewritten `next_cursor` and
recomputed hashes leaves no trace. A prospective ledger closes this because the
root unit and its derived successors are precommitted, so a deletion leaves a
required unit unsatisfied; retrospective attestation cannot, because the expected
chain length is only ever known from the surviving evidence.

So two classes are needed — not for tidiness, but because they license different
claims:

- **`PROSPECTIVE_LEDGER_ACQUISITION`** — plan and root units persisted before
  contact, all unit outcomes present. Supports **target completeness** and
  therefore a denominator.
- **`LEGACY_ATTESTED_ACQUISITION`** — preserved evidence verified today against an
  immutable manifest. Supports descriptive reconstruction, audit, and research
  that does not depend on the denominator being provably complete.

**Verdict 25 (no fabrication):** the historical corpus may carry a
`RETROSPECTIVE_ATTESTATION` recording *"today we verified these preserved
artefacts satisfy X"*. It must never be written as ledger rows claiming to have
existed at acquisition time, and the type name must keep that distinction visible.

**A consequence worth stating plainly.** ~~If the eventual research claim is a
completion *rate*, a legacy-attested denominator must be reported as
attested-not-precommitted, and if that is unacceptable the answer is a fresh
prospective acquisition — a decision about claim strength, not cost.~~

**REVERSED.** The independent review took this decision rather than deferring it,
and it is NO. The undetectable deletion removes a **structured tail** (pagination
tracks date order), so the risk is systematic bias, not random loss. A fresh
prospective acquisition is required for any denominator- or
distribution-sensitive use. The preserved 239 becomes the cross-check against
that retrieval.

## 10. Digests — the portable contracts (all bumped to v2)

v1 semantics shipped in code and tests, so tightening them in place would leave
two meanings for one name. Explicit bumps:

```
target-set-v2         sha256{policy, league_id,
                             members: sorted [{provider, provider_game_id}]}
target-source-scope-v2 sha256{policy, provider, endpoint,
                             responses: sorted [{canonical_request_params,
                                                 body_content_hash}]}
target-derivation-v2  sha256{policy, acquisition_manifest_sha256, plan_policy,
                             plan_digest, unit_ids: sorted,
                             accepted_evidence_digest}
target-binding-v2     sha256{policy, league_id, members_digest,
                             derivation_digest}
```

No `raw_response_id`, no `run_id`, no `game_id`, no receipt wall-clocks. Source
identity means evidence **content**; two byte-identical responses from separate
executions are the same evidence. Duplicates refused, never de-duplicated —
carried forward from v1.

## 11. RB-6 — policy persistence

The seal gains explicit columns for **all five** policies: target-set,
target-derivation, target-binding, target-source-scope and identity-admission
(acquisition-completeness is already there). A verifier must read the semantics
from the row, never infer them from the current code, and never try each known
version until one matches.

## 12. Minimal v24 shape

**V24 REQUIRED.** Conceptually:

| Object | Purpose |
|---|---|
| `acquisition_plans` | deterministic plan identity + manifest binding |
| `acquisition_units` | deterministic required-unit identity, root precommitted |
| `acquisition_attempts` | append-only outcome ledger incl. failures |
| `official_game_identity_attestations` | stable key ↔ local surrogate, immutable, evidence-hashed |
| seal columns | the four missing policy versions (RB-6) |

Executions reuse `ingestion_runs`. No new scope table, no new corpus table. All
append-only under the hardened f021 pattern with construct-then-seal, which v23
already proved executable.

## 13. Direct-SQL threat model

| Attack | Control |
|---|---|
| forged plan digest | recomputed from manifest bytes + policy |
| plan created after responses | plan digest is over the manifest; attempts FK to units FK to plan |
| omitted required unit | completeness enumerates units, not successes |
| added unit | unit id is deterministic; a unit outside the derived chain fails |
| failed unit erased | attempts append-only; a unit with no accepted terminal fails |
| response attached to wrong unit | attempt binds `raw_response_id`; params must match the unit's semantics |
| unrelated same-window acquisition | different plan digest — **RB-1 closed** |
| wrong first canonical resolution | admission gate + immutable attestation — **RB-4 closed** |
| two attestations for one stable key | unique constraint |
| one local game for two stable keys | verifier fails closed |
| random surrogate substituted into a digest | no surrogate is a digest input — **RB-3 closed** |
| policy version omitted | seal columns NOT NULL — **RB-6 closed** |
| legacy corpus presented as portable | acquisition class is explicit and checked |
| **evidence discarded before preservation** | **RETAINED trust assumption for the legacy class only** |

## 14. Seams

**§AF** — unchanged in shape: certified acquisition completeness **and** certified
stable target identity **and** target-bound corpus verification → enumerate
targets → `S_final − 60` → 300-second floor. No expected membership from the
Stage-A manifest, ever. **Verdict 19: YES, subject to data/materialization** —
with the review's addition that the gate is **type-aware**: §AF, E0 and F1-R all
require **PROSPECTIVE_LEDGER_COMPLETE**, and the verifier returns a certification
class rather than a boolean.

**E0** — the accepted gate is preserved. The only change is that the parent's
member identity is now a stable key; the gate call site does not move.

## 15. Safe implementation order

1. ~~independent review of this adjudication~~ **done**
2. v24 ledger + stable-identity + attestation implementation, **with
   executor-owned contact (RR-1), TEAM-A binding (RR-2) and derived root-set
   verification (RR-3)**
3. independent v24 review
4. ~~historical March admissibility adjudication~~ **resolved: not admissible for
   denominator-sensitive use**
5. **fresh prospective March acquisition under the ledger** (denominator-grade)
6. corpus construction + independent review
7. §AF closure + independent review
8. real Stage-A plan, preflight, bounded acquisition

The preserved March 239 leaves the critical path and becomes the independent
cross-check against the fresh retrieval.

## 16. 239 / 160 / 161

**239 unique provider game ids in the preserved listing pages remains VERIFIED**
and is not demoted. It is **not** promoted to a canonical target population: that
needs RB-4 closure in code plus materialization, neither of which happened here.
**160 and 161 remain invalid as bounds.**

## 17. Status

Schema **v23 / 23 / 64 unchanged**; no migration; nothing implemented. No games
materialized, no provider references resolved, no corpus instantiated, no §AF run.
B3 deferred. P1 unauthorized. No Stage-A plan. No probe. Stage A NOT run.
`REGISTERED_LINKING_PROVIDERS` empty. `ATTESTED_GENERATIONS` unchanged. G5 NOT run.
No crosswalk. **F1-R blocked.** TEAM-A read only, unmodified.

## 18. Exact next authorization boundary

> **Independent adversarial review of this architecture adjudication**, then the
> v24 acquisition-ledger and stable-identity implementation.
