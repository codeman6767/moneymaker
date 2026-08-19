# Independent Adversarial Review — Stage-A Corpus Digest & Acquisition Manifest Architecture

**Reviewed artefact:** `STAGE_A_CORPUS_DIGEST_AND_ACQUISITION_MANIFEST_ARCHITECTURE.md` (`5e02b81`).
**Starting HEAD:** `5e02b81` = `origin/main`, tree clean, schema v21 / 21 migrations / 53 tables.
**Provider requests:** 0. **Credits:** 0. **Guards:** 31 armed, 11/11 probes blocked.
**Schema after this task:** still v21. No migration written. **F1-R remains blocked.**

---

## Verdicts

| # | Question | Verdict |
|---|---|---|
| 1 | Per-evidence-lane digest architecture | **ACCEPTED WITH REPAIRS** |
| 2 | Plan-before-network proof | **STRONGER BINDING REQUIRED** |
| 3 | Plan / acquisition identity | **PLAN AND ACQUISITION MUST BE DISTINCT** |
| 4 | Multiple acquisitions per lane | **ADDITIONAL BINDING REQUIRED** |
| 5 | Probe reuse | **ACCEPTED WITH STRONGER MACHINE-VERIFIABLE ELIGIBILITY** |
| 6 | `observed_at` = `received_at` | **REPAIR REQUIRED** (decision correct, justification overstated) |
| 7 | Projection-policy binding | **REPAIR REQUIRED** |
| 8 | Nullable audit lane FK | **STRONGER FAIL-CLOSED RULE REQUIRED** |
| 9 | Completeness reconciliations | **SET-LEVEL REPAIR REQUIRED** |
| 10 | E1 compatibility | **FUTURE EXTENSION NEEDED BUT NO V22 BLOCKER** |
| 11 | Schema | **V22 REQUIRED WITH REPAIRED SHAPE** |
| 12 | Overall | **ACCEPTED WITH REPAIRS — READY AFTER DOC RECONCILIATION** |

The architecture's **direction** survives. Its **central structural premise does not**:
the reviewed document assumed lanes could be appended to an existing corpus row
without changing it. That is incompatible with the shipped corpus design, and it
is the most serious defect found.

---

## D1 (CRITICAL) — The corpus is content-addressed, so lanes cannot be appended silently

f018 is explicit, in the schema itself:

```sql
-- The semantic digest IS the corpus identity. Two rows with the same digest
-- would be the same corpus recorded twice, which would make "which corpus
-- produced this experiment" ambiguous.
CONSTRAINT rcv_semantic_digest_unique UNIQUE (semantic_digest)
```

and `SqliteRetrospectiveProvenanceRepository.record_corpus_version`
(`db/repositories/retrospective.py:123-143`) computes that digest over a **fixed
enumerated field set that already includes `market_evidence_digest`**, then
**returns the existing row** when the digest matches.

So `corpus_version_id` today means interpretation **A** — a fixed, complete,
enumerated evidence set. The architecture chose interpretation **B**. Every
existing consumer assumes A.

**Two consequences, both reproduced offline.**

### D1a — Content-address collision merges distinct reconstructions

Two logically different reconstructions that differ *only* in their market
evidence both leave `market_evidence_digest = NULL` (because, under the reviewed
architecture, market evidence lives in a lane row and never reaches
`record_corpus_version`). Measured:

```
corpus A id       : rcv_01M0BW3XQEQ2B5VRW9SQE8RKAT
corpus B id       : rcv_01M0BW3XQEQ2B5VRW9SQE8RKAT
SAME ROW RETURNED : True
```

An official lane and a market lane belonging to **different logical corpora** can
therefore share one parent `corpus_version_id` — attack **B.1** — with no
forgery at all, using only the shipped repository API. A child FK to
`corpus_version_id` proves nothing about semantic compatibility, exactly as the
authorization warned.

Supplying the digest produces a distinct corpus (`True`), confirming the corpus
identity **already commits to market evidence**.

### D1b — Appending a lane makes the corpus contradict itself

Corpus `rcv_01M0…` stored `market_evidence_digest = None`. Its `semantic_digest`
therefore *commits to the claim* "this corpus has no market evidence." A v22 lane
row appended to that id asserts the opposite while the corpus digest still says
NULL. An older result citing only `corpus_version_id` silently changes meaning.

### The repair

The architecture's §13 argument — that composite digests "invalidate accepted
audits" — is **wrong**, and this is the error that produced the whole flawed
premise. f018 already solves this with supersession:

```sql
-- Supersession APPENDS. The superseded row is never touched.
```

Adding a lane must create a **new corpus version that supersedes** the old one.
The old row, its digest, and every audit and crosswalk bound to it **remain valid
forever, untouched**. Nothing is invalidated; the new corpus is simply a
different, richer reconstruction. The blast-radius fear was unfounded.

Concretely for v22 (E0 only): `market_evidence_digest` **is set to the E0 lane
binding's digest**. This reverses the architecture's §W disposition entirely —
the column is **not** superseded, it is exactly the right field, and leaving it
NULL is what breaks the design.

**Lane rows still earn their place**: they carry provider, namespace generation,
digest-policy version, projection-policy version and acquisition membership —
detail one column cannot express and that an audit must bind to.

---

## D2 — Nullable audit lane FK is a proven fail-open (§R)

The architecture proposes `identity_audit_records.lane_binding_id NULL` "so old
audits remain valid." Reproduced: a **new linking-provider audit**
(`provider='the_odds_api'`, `namespace_verified=1`, `verdict='accepted'`) that
cites **no lane** and forges `source_corpus_digest` to the corpus's official
digest is **ACCEPTED**, and its crosswalk inserts cleanly:

```
LINKING crosswalk ACCEPTED with lane_binding_id conceptually NULL
```

`trg_xwk_audit_corpus_binding` (f019:52-61) compares only
`identity_audit_records.source_corpus_digest IS NOT
reconstruction_corpus_versions.source_corpus_digest`. A NULL lane simply routes
around the entire new mechanism. **Nullable-for-legacy is nullable-for-everyone.**

**Required repair — smallest fail-closed contract:** NULL is permitted **only**
for audits whose provider is in the official-provider registry **and** whose
`audit_policy_version` predates the v22 policy era. Any audit whose provider is
not an official provider **must** cite a lane binding whose provider, league and
namespace generation match the audit's. Enforced by trigger, not by convention.

---

## D3 — Lane digests are forgeable at insert; only a verifier can stop it (§S)

The forged audit above declared a `source_corpus_digest` while **no market
evidence existed at all**. Nothing recomputed it. A SQLite trigger cannot compute
SHA-256 over evidence rows, so "append-only" does not address forgery — it only
prevents *rewriting* a forged value.

**Required certification sequence** before a lane may back an ACCEPTED audit, all
recomputed by one deterministic verifier and compared to the stored row:
acquisition membership → acquisition completeness → projection completeness →
observation-content integrity → lane evidence digest → policy versions. The
acceptance guard must refuse the audit when any recomputation disagrees. The
architecture named a gate but never named this recomputation as a precondition of
*lane acceptance*.

---

## D4 — Retries break the reconciliation equation (§J)

`UNIQUE(acquisition_id, bucket, attempt_ordinal)` explicitly permits N attempts
per bucket, but §7a balances **planned buckets** against a **sum of outcome
counts**. With one retry:

```
planned buckets : 2
attempt rows    : 3
planned == sum  : False
```

The equation cannot balance the moment any bucket is retried. Two reconciliations
are required, not one:

- **Attempt reconciliation** — every attempt row classified, none discarded, so a
  failure that preceded a successful retry stays permanently visible.
- **Terminal bucket reconciliation** — every planned bucket maps to exactly one
  *derived* terminal classification.

For the bounded first pass I recommend **retries forbidden by policy** (one
attempt per bucket) while keeping `attempt_ordinal` in the key so a future
declared retry policy needs no schema change. Both reconciliations must be
**derived by the verifier**, never read from caller-supplied counters.

---

## D5 — Single vs multiple acquisitions per lane is a genuine contradiction (§D)

The reviewed document states both:

- lane row carries **one** `acquisition_manifest_id` (§3);
- "**multiple acquisition manifests may feed one lane**" (§12).

These cannot both hold. **Adjudication: option 3 — a lane binding plus a child
membership table** `corpus_evidence_lane_acquisitions(lane_binding_id,
acquisition_id)`, append-only.

The lane digest must then be computed over the **union of member acquisitions**,
and the lane must additionally store an **acquisition-set digest** over the sorted
member acquisition ids. This closes both directions of the stated attack:

- *lane cites only A but digest covers A+B* → the recomputed acquisition-set
  digest over `{A}` disagrees with the stored value;
- *lane cites A+B but digest computed only from A* → the recomputed lane evidence
  digest over `{A,B}` disagrees.

Omission becomes detectable in both directions, which single-`acquisition_manifest_id`
cannot do.

---

## D6 — Plan identity and acquisition identity are conflated (§I)

`UNIQUE(plan_digest)` with `acquisition_id` derived from `plan_digest` means **one
plan may only ever have one acquisition**. That is wrong for: an aborted run
restarted from scratch, an independent later reproduction, the same plan against a
different database, and a reproducibility re-run.

**Verdict: plan and acquisition must be distinct objects.** `stage_a_plans` holds
plan identity (`plan_digest` UNIQUE, policy versions, declared bucket and target
membership). `stage_a_acquisitions` references a plan and is **not** unique on it,
carrying its own run provenance. Resume reuses the same acquisition; independent
reproduction creates a new acquisition of the same plan. This distinction is
scientifically necessary — without it, "we reproduced the plan independently" is
unrepresentable.

---

## D7 — `f1a-manifest-v1` cannot carry Stage A; the claimed reuse is not literal (§H)

Inspection of `ingest/manifest.py` shows three blocking facts:

1. `_SUPPORTED_COST_POLICY_VERSIONS = frozenset({"mlb-cost-v1", "bdl-cost-v1"})` —
   **no Odds API cost policy**. `parse` refuses anything else (line 322).

   Worse, this **fails open rather than closed**. `planning._policy_for` is a
   binary fallback — `balldontlie`, else MLB — so an unknown provider is silently
   costed as MLB. Measured:

   ```
   _policy_for('the_odds_api') -> mlb-cost-v1   (identical to the MLB policy)
   ```

   A Stage-A plan built through the existing planner would therefore be costed
   under **MLB's** model and emit `cost_policy_version = "mlb-cost-v1"`, which
   *passes* the supported-version check. The manifest would be accepted while
   silently mis-costing every Odds request — a budget/credit-accounting hazard
   strictly worse than an outright refusal. Any Stage-A planner must add an
   explicit Odds cost policy **and** make `_policy_for` fail closed on unknown
   providers.
2. `_SUPPORTED_PLAN_VERSIONS = frozenset({"f1a-plan-v1"})`, and
   `EXPECTED_SCHEMA_VERSION = 16`.
3. **`body()` hashes `scratch_db` and `checkpoint_path`** — *local filesystem
   paths* — into the manifest identity. The same logical plan executed from a
   different directory yields a different `manifest_hash`.

Point 3 is disqualifying on its own for a plan meant to be an independently
reproducible scientific declaration: local paths must never be semantic identity.

**Correct statement:** Stage A reuses the **canonicalization, duplicate-key
rejection, atomic-write and checkpoint infrastructure**, under a **new Stage-A
manifest schema and policy version** whose hashed body excludes local paths.
Existing F1A manifests must remain byte-identical and parseable — so the new
format is additive, never a mutation of `f1a-manifest-v1`.

---

## D8 — Plan-before-network is weaker than claimed (§E, §F, §G)

The proposed controls do **not** establish that the raw response was acquired
after plan declaration. The bucket-closure trigger fires on the first *attempt*
row, and an attempt can point at a raw response that already existed. The full
fetch-then-declare sequence in the authorization survives every proposed control.

Specific weaknesses:

- **`declared_at` is caller-supplied** and directly backdatable by SQL. It is
  **not** provenance proof. It must be stated as descriptive only, and must not
  participate in acquisition identity (it would also break replay determinism).
- **`plan_manifest_path` must not be semantic identity.** Content digest is the
  identity; the path is a convenience pointer that may rot.
- **Git timestamps are not trustworthy chronology** — commit dates are
  attacker-settable via `GIT_COMMITTER_DATE`. The architecture's claim that git
  history proves plan precedence is **overstated**.

**Required minimum binding:**

1. Persist plan/acquisition registration **before** any transport, and require
   every non-probe attempt's cited `raw_responses.requested_at` to be **≥ the
   acquisition's registration instant**, enforced by trigger.
2. Bind the manifest's **commit SHA plus its content digest**, verified by
   re-hashing the file at that commit.
3. Permit a response predating registration **only** under
   `REUSED_PROBE_RESPONSE`, with D9's eligibility proof.

**Stated trust boundary (honest):** this yields *tamper-evidence*, not
cryptographic proof of temporal ordering. An operator with direct SQL write access
to the evidence database can construct a self-consistent history. That is the
reviewed boundary, and the architecture must say so rather than claim stronger
proof than exists.

---

## D9 — "Probe" is not a persisted type, so the exception is currently ungeneralizable-in-name-only (§N)

Searched: `raw_responses` has **no** probe classification. The only `probe_name`
in the schema is on `provider_capabilities` (d010), which is about capability
verification, not response membership.

Therefore the Stage-A gate has **no machine-verifiable way** to distinguish the
one legitimate probe response from any arbitrary pre-manifest response. As
written, `REUSED_PROBE_RESPONSE` is a **general bypass of plan-before-network**
wearing a specific name — precisely what the authorization forbade.

**Required eligibility proof**, all machine-checkable, before the gate may accept
a pre-registration response:

- an explicit append-only **probe registration row** naming that exact
  `raw_response_id`, committed before the Stage-A plan (this is the missing object);
- exact endpoint, unfiltered request shape (`{apiKey, date, dateFormat}`),
  HTTP 200, valid wrapper, passes the projection verifier;
- its bucket is a member of the declared plan;
- **at most one** reused probe response per bucket, and the gate refuses when
  several candidate pre-existing responses exist for one bucket — otherwise a
  curator silently selects the convenient one.

The architecture's economic argument for reuse (spending a credit buys *worse*
provenance) is **sound and survives**. Only its enforceability was missing.

---

## D10 — `observed_at = received_at` is the right choice, wrongly justified (§O)

The decision **survives** — it removes a free parameter by forcing one clock
instead of two, and is recomputable from the cited row. But the architecture's
claim that it "cannot be invented by a caller" is **false**: `raw_responses`
constrains `received_at` only by *shape*
(`CHECK (received_at LIKE '____-__-__T__:__:__%Z')`, b004:148). There is no
ordering check against `requested_at` and no calendar validity — the same class of
defect f019's D5 repaired for the retrospective tables.

So a caller writing the raw response controls `received_at`, and `observed_at`
inherits exactly that trust level. Required repairs: state the true trust boundary,
and add `received_at >= requested_at` plus calendar validity before relying on it.

Semantics confirmed correct and must be stated explicitly: **`observed_at` is when
*we* first possessed the preserved evidence — never the historical provider
snapshot instant.** A March snapshot received in August has `observed_at` in
August. This matches `POINT_IN_TIME_DATA.md` §2.1.

---

## D11 — Lane-level projection policy cannot describe a multi-acquisition union (§P)

Once D5 admits multiple acquisitions per lane, a single lane-level
`projection_policy_version` is not well-defined if members were projected under
different policies. **Repair:** `projection_policy_version` is recorded **per
acquisition**; the lane may carry it only as a derived field that the verifier
requires to be **uniform across all members**, refusing a mixed-policy lane. A
mixed lane must be split.

---

## D12 — Digest policy must not be redefinable under a fixed version string (§Q)

`sources.audited_source_tables` selects tables from **current software
registries**. Storing `digest_policy_version` records *which* policy was used but
does not prevent that version's meaning from changing when a provider or table is
added later. Required: a CI invariant pinning the resolved table/column set for
each frozen policy version, so an accepted lane stays re-verifiable under its
declared historical policy. Registration of E1 or a new provider must be unable to
retroactively redefine an existing version.

---

## D13 — Target completeness cannot be proved from the database alone (§U)

The architecture binds the target set and target→bucket mapping into the **plan
digest**, but proposes only `stage_a_planned_buckets` in the database. The
certification gate therefore cannot verify target completeness without reading the
committed file — while §7c presents certification as a database property.

**Repair:** add `stage_a_plan_targets(plan_id, canonical_game_id,
requested_at_bucket)`, append-only, closed with the bucket set, with the plan
digest recomputed over these rows. Then the DB proves the population itself. The
offline manifest verifier remains part of the gate and must be **named** as such —
the architecture must not imply the database alone proves target completeness when
the mapping lives only in a file.

The §9 pigeonhole argument for binding the mapping is **correct and survives**.

---

## D14 — The official→parent corpus binding is unspecified (§V)

Stage A is planned from official corpus **C1**; the lane is later attached to
corpus **C2**. Nothing proposed enforces C1 ≡ C2. Combined with D1a's collision,
this is the most direct cross-lane mixing route.

**Repair:** the plan must bind C1's `source_corpus_digest` **and**
`target_set_digest`, persisted on `stage_a_plans`; lane attachment must refuse
unless the parent corpus's values equal them. This is a trigger-enforceable
equality, not a convention.

---

## §C answer — what `corpus_version_id` means

**Interpretation A**, unchanged: a fixed, complete, enumerated evidence set. Lanes
are *described* by child rows but the corpus **commits to its lane set** through
`market_evidence_digest` (E0) feeding `semantic_digest`. Adding a lane yields a
**new corpus version by supersession**. Downstream results may therefore continue
to cite `corpus_version_id` alone — its meaning is stable *because* the digest
enumerates the evidence. No root digest is introduced.

---

## §X — E1 compatibility

The lane **table** is generic enough for E1. Two corrections to the architecture's
wording:

- E1 prices need **evidence certification, not an identity audit**.
  `identity_audit_records` is about identifier cleanliness and does not
  meaningfully apply to price series; E1 should cite the E0 crosswalk for identity
  rather than re-auditing it.
- Because the corpus commits to its lane set through a *single* E0-shaped column,
  E1 will require a generic **lane-set digest** on the corpus. That is a v23
  extension, not a v22 blocker — but the architecture's claim that E1 slots in
  with *no* schema change is **not accurate** and is corrected.

---

## §Y — Append-only hardening

Every new v22 append-only table **must** use the hardened f021 content-aware
BEFORE-INSERT pattern from day one, because the ordinary BEFORE UPDATE/DELETE
pattern does not stop SQLite `REPLACE`. This is a requirement on new objects only;
**P1** (the 28 legacy tables) remains outstanding and is **not** authorized here.

---

## §Z — Minimal corrected conceptual schema

| Object | What becomes undetectable without it |
|---|---|
| `stage_a_plans` | a plan could not be reproduced or re-executed independently (D6) |
| `stage_a_plan_targets` | a target could vanish while the bucket set is unchanged (D13) |
| `stage_a_planned_buckets` | a response acquired outside the authorized set |
| `stage_a_acquisitions` | distinct executions of one plan would be indistinguishable (D6) |
| `stage_a_request_attempts` | a failed request would silently become "no market" |
| `stage_a_probe_registrations` | probe reuse could not be distinguished from fetch-then-declare (D9) |
| `corpus_evidence_lane_bindings` | two providers' evidence could not both bind one corpus |
| `corpus_evidence_lane_acquisitions` | acquisition omission from a lane union (D5) |
| `identity_audit_records.lane_binding_id` + fail-closed rule | a linking audit routing around lanes entirely (D2) |

**Dropped from the reviewed proposal:** `acquisition_manifest_id` as a lane column
(replaced by the join table); `UNIQUE(plan_digest)` on acquisitions;
`declared_at` as provenance evidence. **No** separate certification-record table
is added — certification is *derived* by the verifier, and a stored verdict would
be exactly the caller-supplied claim D3 warns against.

---

## §AA — Implementation order

1. this review;
2. **reconcile the architecture document** (done in this commit — see below);
3. v22 schema/provenance implementation;
4. independent v22 implementation review;
5. linking-provider registration (+ review);
6. commit the Stage-A plan **before** acquisition; register plan/acquisition;
7. Stage-A first pass under its own ~160-request/160-credit cap;
8. Stage-A certification;
9. G5 → curation + S8 → independent review;
10. target anchors → E1.

Because seven of eight original verdicts change, I **updated the architecture
document in this same commit** so that one authoritative implementation contract
exists before coding, as the authorization prefers. The architecture's verdict
table, §3, §4, §5, §6, §7, §8, §12, §13, §14 and §W disposition are reconciled to
this review; the review remains the authority wherever they differ.

---

## Validation

| Check | Result |
|---|---|
| Schema | **v21 / 21 migrations / 53 tables — unchanged** |
| Migration written | **none** |
| Provider requests / credits | **0 / 0** |
| Zero-network guards | 31 armed, **11/11 blocked** |
| `REGISTERED_LINKING_PROVIDERS` | **empty** — unchanged |
| `ATTESTED_GENERATIONS` | unchanged |
| Stage A / G5 / crosswalk / F1-R | **not run** |
| Protected artefacts | see task report |

## Exact next authorization boundary

> **v22 schema + provenance implementation**, built to the reconciled architecture
> document, followed by an independent implementation review.

**F1-R remains blocked.**
