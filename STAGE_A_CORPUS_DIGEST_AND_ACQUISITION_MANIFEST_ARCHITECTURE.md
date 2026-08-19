# Stage-A Corpus Digest & Acquisition Manifest — Structural Architecture

**Starting HEAD:** `aa571a6` (`origin/main` = `aa571a6`, tree clean, schema
v21 / 21 migrations / 53 tables — **unchanged by this task**).
**Provider requests:** 0. **Credits spent:** 0. **F1-R remains blocked.**

Architecture and offline analysis only. Nothing implemented.

---

## Verdicts

| # | Question | Verdict |
|---|---|---|
| 1 | Complete Stage-A response-set representation | **NEW BINDING REQUIRED** — but the *plan* reuses the existing reviewed manifest/checkpoint abstraction |
| 2 | One corpus / two audits | **PER-EVIDENCE-LANE DIGEST BINDINGS** (append-only child rows) |
| 3 | Projection-policy binding | persisted on the **evidence-lane digest binding**, declared in the manifest |
| 4 | `observed_at` ownership | **`observed_at` = the cited `raw_responses.received_at`**, enforced by DB trigger + parser + gate |
| 5 | Completeness certification | **three reconciliations, conjunction required** |
| 6 | Probe-response reuse | **ALLOWED**, explicitly classified `REUSED_PROBE_RESPONSE` |
| 7 | Schema | **V22 REQUIRED** |
| 8 | Overall | **ARCHITECTURE ACCEPTED — READY FOR IMPLEMENTATION** |

---

## 1. Two discoveries that changed the answer

Before adjudicating, two things in the repository materially narrowed the design
space. Both were found by inspection, and both mean **less** new machinery than
the prior reviews assumed.

### 1a. The corpus row already declares per-lane digests

`reconstruction_corpus_versions` (f018) carries **six** digest columns:

```
source_corpus_digest        target_set_digest        evidence_registry_digest
static_identity_map_digest  market_evidence_digest   semantic_digest
```

f018's own header names them the architecture's §19 reproducibility inputs —
*"source corpus fingerprint, static identity map, availability policy version,
cutoff policy, feature/evidence registry, target set, **market snapshot evidence
set**, and the G1 variant"*.

**`market_evidence_digest` already exists, is nullable, is plumbed through the
model and repository, and is written by nothing.** The schema anticipated a
separate market-evidence lane. The per-lane concept is therefore **already
reviewed and shipped** — what is missing is the *binding*, not the idea.

### 1b. A reviewed manifest + checkpoint abstraction already exists

`sports_quant/ingest/manifest.py` (`f1a-manifest-v1`), `planning.RequestPlan`,
and `checkpoint.py` (`f1a-checkpoint-v2`) already provide:

- a **versioned, deterministic, secret-free** plan document; equivalent inputs
  produce byte-identical canonical JSON and the same SHA-256;
- **no** API key, header, secret-bearing URL, random id or wall-clock in the
  hashed body;
- a **duplicate-key-rejecting** JSON loader (the same defence the projector
  review had to add);
- a checkpoint that records a request complete **only after** its raw response
  *and* the required normalized persistence commit in one transaction;
- append-only per-process usage provenance, so repeated resumes cannot multiply
  prior usage;
- atomic write (temp + `os.replace`), so a checkpoint never claims completion the
  database cannot back.

This is exactly the abort/partial/resume machinery §H asks for, already reviewed
and already exercised by the F1/F1B pilots. **Stage A must reuse it, not
reinvent it.**

## 2. Independent reproduction of the one-digest / two-audits problem (§E)

Constructed offline on a fresh v21 database, not cited from the review:

| Step | Result |
|---|---|
| Official (`balldontlie`) source digest | `b8ddb91b68fee1bf3037f83ad02de964…` |
| Insert a market observation, recompute official digest | **unchanged** — market evidence is invisible to it |
| Linking digest today | **refused** (`SourceCorpusError`, no linking provider registered) |
| Hypothetical linking digest (registry patched in memory only) | `da7a035a4a5e31361dd7f6680a0dccd0…` |
| **Digests differ** | **yes** |
| Corpus row: one `source_corpus_digest`, no `provider` column | confirmed |
| Official audit → crosswalk in that corpus | **ACCEPTED** |
| Linking audit → crosswalk in the same corpus | **REFUSED** by `trg_xwk_audit_corpus_binding` |

The refusal message is exactly right for what it was written to stop — *"cites an
identity audit taken over a different source corpus"* — and exactly wrong here,
because this is a **different lane of the same corpus**, not a different corpus.
The trigger cannot tell those apart, because nothing in the schema says which
lane an audit's digest belongs to.

## 3. Digest architecture — option-by-option

**Option 1, one composite digest over all evidence.** Rejected. Adding a lane
changes the corpus identity, so registering the Odds lane invalidates every
accepted TEAM-A audit, and adding E1 later invalidates the Odds one. This is the
blast-radius problem the v20 review already measured and rejected once.

**Option 3, a root digest over lane digests.** Rejected — Option 1 with
indirection. The root still moves when any lane is added, so accepted audits
citing the old root still break.

**Option 4, separate corpus versions per lane.** Rejected. Crosswalk resolution,
the retrospective reader and F1-R all key on one `corpus_version_id`; splitting
lanes means a linking crosswalk under `C_odds` resolves a canonical game whose
official provenance lives under `C_official`, with no persisted composition
proving they describe one reconstruction. It also proliferates with E1 and makes
accidental cross-corpus mixing easy rather than impossible.

**Option 5, an extra audit-input digest.** Rejected. `identity_audit_records.
source_corpus_digest` **already is** the audit-input digest. Adding a second
digest beside it creates two candidate truths and no rule for which governs.

**Option 2, per-evidence-lane digest bindings. CHOSEN.**

### The chosen shape

Append-only child rows of the corpus, one per evidence lane:

```
corpus_evidence_lane_bindings
    corpus_version_id      -> reconstruction_corpus_versions
    evidence_lane          'official_identity' | 'market_events_e0' | …
    provider, namespace_generation
    digest_policy_version  (which audited source set produced it)
    projection_policy_version   NULL except for projected lanes (§5)
    source_corpus_digest   the lane's digest
    acquisition_manifest_id     NULL except for acquired lanes (§4)
```

An identity audit binds to **its lane's row**, not to the corpus's single
`source_corpus_digest`.

**Why this satisfies the twelve requirements:**

1. **Old corpora stay verifiable** — the official lane's digest is byte-identical
   to today's `source_corpus_digest`; nothing recomputes differently.
2. **TEAM-A provenance stays valid** — untouched, and its map digest keeps its
   own existing column.
3. **Both audits bind one logical reconstruction** — one `corpus_version_id`,
   two lane rows.
4. **No cross-corpus reuse** — a lane row is a child of exactly one corpus, and
   the audit binds the lane row.
5. **Explicit, versioned source set** — `digest_policy_version` per lane.
6. **E1 has a defined behaviour** — a *new lane row* (`market_prices_e1`),
   changing no existing lane's digest and invalidating no accepted audit. This is
   the property Options 1 and 3 cannot provide, and it is why one more table beats
   reusing `market_evidence_digest` as a fixed column.
7. **Append-only** — same trigger pattern as f018/f021.
8. **No corpus row rewritten** — lanes are inserted, never an `UPDATE`.
9. **Triggers fail closed** — see §10.
10. **Runtime can name its authorization** — the crosswalk cites the audit, the
    audit cites the lane, the lane cites the corpus.
11. **Composition is explicit** — a corpus with two lanes says so in rows.
12. **Deterministic replay** — every value is content-derived.

**`market_evidence_digest` is superseded, not used.** It should remain NULL, with
the reason recorded: a fixed column per lane cannot express E0 and E1 separately
without either conflating them (blast radius) or adding a column per lane forever.
It stands as the precedent that the per-lane concept was always intended.

## 4. The Stage-A acquisition manifest (§A, §B, §C)

### Division of labour — file vs database

| Concern | Where | Why |
|---|---|---|
| The **plan** (buckets, budget, policy versions, target population) | **Committed manifest file**, `f1a-manifest-v1` lineage | Already reviewed; source-controlled; canonical-JSON hashed; secret-free; provably authored before the run because it is in git history |
| **Resume / partial-run state** | **Checkpoint file**, `f1a-checkpoint-v2` | Already reviewed; atomic; usage provenance; completion only after the DB transaction commits |
| **Membership** — which `raw_responses` rows belong to this acquisition | **New DB rows** | The corpus verifier runs *inside* the database and cannot read a file; this is the one thing neither existing artefact provides |

This is why Verdict 1 is "new binding required" rather than "new manifest
required". The plan already has a reviewed home.

### Minimal DB objects

```
stage_a_acquisitions
    acquisition_id            PK, 'sga_' prefix, deterministic from the plan digest
    league_id, provider, namespace_generation, sport_key
    plan_digest               the committed manifest's SHA-256
    plan_manifest_path        the committed file, for inspection
    acquisition_policy_version, projection_policy_version
    request_budget, credit_budget
    declared_at
    append-only

stage_a_planned_buckets
    acquisition_id, requested_at_bucket        UNIQUE together
    append-only; the CLOSED set of authorized buckets

stage_a_request_attempts
    acquisition_id, requested_at_bucket        -> planned_buckets (FK)
    attempt_ordinal
    outcome        enum, §7
    raw_response_id  NULL unless a response was preserved
    append-only
```

**What each object buys — the §R test.** Without `stage_a_planned_buckets`, a
raw response acquired outside the plan is undetectable. Without
`stage_a_request_attempts`, a *failed* request is undetectable and silently
becomes "no market". Without `stage_a_acquisitions`, there is no object the
corpus verifier can scope to, and the gate stays too broad or too narrow. Each
answers a concrete undetectable failure; nothing else is proposed.

### Plan-before-network, proven persistently (§B)

Four mechanisms, none relying on application call order:

1. `stage_a_request_attempts.requested_at_bucket` is an **FK to
   `stage_a_planned_buckets`** — a response for an undeclared bucket cannot be
   recorded at all.
2. `stage_a_planned_buckets` is **append-only and closed by a trigger**: no
   insert is permitted once any attempt row exists for that acquisition. A bucket
   cannot be back-filled around a convenient response.
3. `plan_digest` is the hash of a **committed file**; git history is
   independently datable evidence that the plan preceded the run.
4. The **checkpoint** records the manifest hash it is executing, and refuses to
   resume against a different manifest — the existing reviewed behaviour.

Together these mean the fetch-then-declare attack requires editing committed
history *and* defeating two append-only triggers.

## 5. Projection-policy binding (§ Problem 3)

**Persisted on the evidence-lane binding row** as `projection_policy_version`,
and **declared in the manifest** before acquisition.

The lane row is the right owner because it is precisely the statement *"this
lane's evidence set was certified under these policies"*. The audit is about
**identifiers**, not projection; the manifest alone is pre-network and cannot
record what actually certified the result.

**Validation, not free text:** the implementing task must verify the named
version exists in the projector's registry of known policy versions and refuse
an unknown string — the same fail-closed shape as `ATTESTED_GENERATIONS`. A code
hash is *not* proposed: the repository has no convention for it, and it would
change on every unrelated refactor.

Re-verification under *current* policy may still fail old evidence — that is the
intended detective behaviour. What the binding adds is that the **historical
certification remains inspectable**: an accepted corpus records which rules
certified it, so changed code can never silently reinterpret it.

## 6. `observed_at` ownership (§ Problem 4)

### Reconstructed semantics

| Clock | Meaning |
|---|---|
| `raw_responses.requested_at` | when we issued the request |
| `raw_responses.received_at` | when the response arrived |
| `provider_snapshot_timestamp` | the instant the **provider** answered with |
| `requested_at_bucket` | the historical instant we **asked for** |
| `observations.observed_at` | *(unowned today)* |
| `observations.created_at` | DB record clock |

`POINT_IN_TIME_DATA.md` §2.1 is unambiguous: **"`observed_at` is the point-in-time
cutoff. Always. Without exception."** Every other typed observation table uses it
as the transaction time at which *we* learned the fact.

### Decision

> **`observed_at` MUST equal the cited `raw_responses.received_at`.**

Chosen over "materialization time" for one decisive reason: **it is
independently verifiable.** Materialization time is unfalsifiable — any value a
caller writes is as plausible as any other, which is exactly the hole the review
found. Receipt time is a fact already preserved on the row the observation
cites, so a verifier can recompute it and a caller cannot invent it.

It also satisfies every requirement: never backdated to March (the receipt clock
is *now*, whatever the snapshot instant), preserves that Stage A is being
collected today, deterministic and replay-safe, and handles probe re-use
honestly — a re-materialized probe observation carries the probe's **real**
receipt time, which is the truth.

**Enforcement at three layers**, because each catches what the others cannot:

- **DB trigger** — `observed_at` must equal the cited response's `received_at`.
  Stops direct SQL.
- **Stage-A parser** — sets it from the cited row rather than accepting a
  parameter. Stops caller error.
- **Corpus gate** — re-checks it. Catches a row written before the trigger
  existed.

## 7. Completeness composition (§ Problem 5, §H)

Three reconciliations. **Certification requires all three to balance**; any one
alone is a partial claim.

### 7a. Acquisition

```
planned_buckets = succeeded_full_snapshot
                + succeeded_empty_data            (data: [], valid evidence)
                + reused_probe_response
                + http_or_provider_failure
                + entitlement_or_auth_failure
                + quota_blocked
                + budget_blocked
                + malformed_wrapper
                + projection_rejected_snapshot
                + not_requested
```

`not_requested` **must be 0** for a run to be COMPLETE. `earlier_provider_snapshot_returned`
is recorded as an attribute of a succeeded attempt, not a separate outcome — the
provider answering at or before the request is normal, not an exception.

### 7b. Projection

```
succeeded_responses = verified_complete_projection
                    + verified_zero_event_response
                    + rejected_malformed_snapshot
                    + rejected_duplicate_event_id
                    + observation_mismatch
orphaned_observations  MUST be 0
```

### 7c. Certification gate

```
STAGE_A_CERTIFIED  ⟺  acquisition balances
                  AND  not_requested = 0
                  AND  projection balances
                  AND  orphaned_observations = 0
                  AND  every succeeded response verifies (content hash
                       AND body projection AND completeness)
```

**Two conflations the categories exist to prevent:** a *projection rejection*
must never be reported as "the provider returned no events", and a *request
failure* must never be reported as "no market existed". They are separate
categories in separate reconciliations precisely so neither can absorb the other.

### Abort / partial runs (§H)

An interrupted run is **not** COMPLETE and **cannot** back an accepted Stage-B
audit. Evidence already acquired is preserved; missing planned buckets stay
visible as `not_requested`.

State is **append-only attempt rows**, not a mutable `status` field: completion
is *derived* by reconciling attempts against planned buckets. A mutable
"status = complete" could be set by anyone; a derived verdict cannot be, and the
existing checkpoint already follows this pattern. Resume reuses prior successful
evidence without re-spending credits, and the checkpoint's usage provenance
prevents a resume from exceeding the original hard budget.

## 8. Probe re-materialization (§D) — **ALLOWED**, explicitly classified

Permitted only when **all** hold: the requested bucket is an exact member of the
predeclared plan; the request shape passes the unfiltered allow-list
(`{apiKey, date, dateFormat}` — the real probe does); the endpoint is the exact
historical-events endpoint; the response is HTTP 200 with a valid wrapper; it
passes the repaired projection verifier; and `requested_at` / `received_at` /
`observed_at` keep their **original real values**.

It is recorded as outcome **`REUSED_PROBE_RESPONSE`** and **must not** be counted
as a Stage-A provider request in the acquisition ledger.

**Why allowed rather than forbidden:** forbidding it would spend a credit to
obtain strictly *worse* provenance — a second response for a bucket that already
has valid preserved evidence, with the first left unexplained in the database.
The truth is "this bucket's evidence came from the capability probe", and the
architecture can simply say so. That is not optimizing away a credit; it is
declining to manufacture a misleading second request.

## 9. Plan digest and target binding (§I, §J)

### The plan digest must bind target→bucket membership, not just the buckets

**This is a real selection-bias attack, and the bucket set alone does not stop
it.** The pilot plans **160 buckets for 239 targets** (239 is confirmed in the
committed F1 execution reviews). Since 239 > 160, the pigeonhole principle makes
multi-target buckets **unavoidable**: at least 79 targets share a bucket with
another target, regardless of how the flooring policy is defined.

For any such bucket, dropping one of its targets leaves the sorted bucket set
**byte-identical** — a target silently disappears from the declared population
while the plan digest is unchanged. The attack therefore does not depend on the
exact bucket-size distribution; it follows from the counts alone.

*(An earlier in-session analysis put the distribution at 1→106, 2→33, 3→17, 4→4.
That figure is recorded here for context only and was **not** re-derived in this
task — the NBA March schedule is not present in any locally readable database,
so only the MLB corpora could be queried. Nothing in this section relies on it.)*

The plan digest must therefore bind:

- league, provider, namespace generation, sport key;
- the **official search-hint source digest** (which preserved rows produced the hints);
- the plan/algorithm policy version, decision horizon (`T−60`), flooring policy;
- the exact **sorted bucket set**;
- the exact **sorted target canonical game id set**;
- the **target → bucket mapping**.

### Binding target ids is correct (§J)

It is **necessary** — without it the §I attack succeeds. It **leaks nothing**:
the ids come from the already-preserved official corpus, and declaring "these are
the targets" is a population statement, not a claim about any Odds event. And it
does **not** drag official identity provenance into the market lane: Stage A
still assigns **no** Odds→canonical mapping, which remains Stage-B's job.

## 10. Direct-SQL threat model (§N)

| Attack | Defence |
|---|---|
| Manifest created after acquisition | Committed-file plan digest + append-only `stage_a_acquisitions` + git history |
| Planned bucket added after acquisition | Trigger: no bucket insert once any attempt exists |
| Planned bucket removed | Append-only; deletion refused |
| Raw response linked to undeclared bucket | FK from attempt to planned bucket |
| Probe response inserted as Stage A without classification | `outcome` enum; `REUSED_PROBE_RESPONSE` is a distinct value, and the ledger separates it from provider requests |
| Response from wrong endpoint | Projector's exact endpoint admission (already shipped) |
| Duplicate request membership | UNIQUE `(acquisition_id, bucket, attempt_ordinal)` |
| A successful response omitted from the manifest | Acquisition reconciliation: planned ≠ sum of outcomes |
| A failed request omitted | Same reconciliation |
| Caller verifies only easy ids | Corpus gate scopes to the acquisition, never to caller-supplied ids (already repaired) |
| Projection policy version forged | Registry validation; unknown version refused |
| `observed_at` backdated | Trigger equality against the cited `received_at` (§6) |
| Target omitted while bucket set unchanged | Plan digest binds target set **and** mapping (§9) |
| Official digest from corpus A + Odds digest from corpus B | Lane rows are children of one corpus; the audit binds the lane row |
| Audit cites the wrong lane | Trigger: the audit's provider/namespace must match the lane row's |
| Crosswalk cites the right audit but the wrong corpus | Existing corpus-binding trigger, retargeted to the lane row |
| Old TEAM-A crosswalk under v19 | Untouched: official lane digest is byte-identical |
| Provider registry changed after certification | Lane row records `digest_policy_version`; the CI verifier asserts no audit names a provider outside the registries (the v20 D2 repair) |

Every row above is a **DB constraint, a deterministic verifier, or a committed
artefact**. None is "CI will catch it" without a named input.

## 11. Snapshot de-duplication (§K) — preserve both

Two requested buckets can legitimately return the same provider snapshot.
**Preserve both raw responses and both observation sets.**

`requested_at_bucket` is part of the observation's content hash, so the same
snapshot returned for two buckets yields two distinct observations. **That is
correct.** The requested bucket is evidence of *what we asked*; the snapshot
timestamp is evidence of *what they returned*. Collapsing them would destroy
request provenance and break acquisition reconciliation, which counts per planned
bucket. The apparent redundancy is the honest record of two questions with one
answer.

## 12. Source corpus vs acquisition manifest (§L)

They are **not** the same object.

```
Stage-A acquisition manifest   what was planned / requested / received / projected
        ↓ (one lane's evidence)
Corpus evidence-lane binding   this lane's digest + policies, child of…
        ↓
Reconstruction corpus version  the logical research reconstruction
        ↓
Identity audit                 a certified claim over ONE lane's evidence set
        ↓
Static crosswalk               provider id → canonical entity, under that audit
```

**Ordering:** the acquisition runs **first**; the corpus version and its lane
binding are created **after** Stage-A certification, because the lane digest
cannot be computed before the evidence exists. **Multiple acquisition manifests
may feed one lane** (e.g. a resumed run, or a second month), and the lane digest
covers their union.

## 13. E1 future compatibility (§M)

E1 historical **prices** arrive as a **new lane** (`market_prices_e1`) with its
own acquisition manifest, its own digest policy and its own audit. It changes no
existing lane's digest, so **every already-accepted audit remains valid** and no
corpus row is rewritten.

That is the single strongest argument for Option 2 over Options 1 and 3, both of
which would invalidate the Odds E0 audit the moment E1 landed — a redesign
forced immediately after shipping.

## 14. Schema verdict — **V22 REQUIRED**

v21 cannot express this. Conceptual additions only; **no migration is written
here**.

| Object | Purpose | PK / Unique | Append-only | Must NOT contain |
|---|---|---|---|---|
| `stage_a_acquisitions` | scope object the corpus gate verifies over | `acquisition_id`; UNIQUE `plan_digest` | yes | any canonical game id; any secret |
| `stage_a_planned_buckets` | the closed authorized bucket set | UNIQUE `(acquisition_id, requested_at_bucket)` | yes, **and closed once attempts exist** | canonical identity |
| `stage_a_request_attempts` | honest per-bucket outcome ledger | UNIQUE `(acquisition_id, bucket, attempt_ordinal)`; FK → planned bucket, FK → `raw_responses` | yes | canonical identity |
| `corpus_evidence_lane_bindings` | per-lane digest + policy versions | UNIQUE `(corpus_version_id, evidence_lane)` | yes | a second corpus's data |
| `identity_audit_records` | **one added nullable FK** to the lane binding | — | unchanged | — |

**Trigger changes:** `trg_xwk_audit_corpus_binding` must compare an audit against
its **lane binding's** digest rather than the corpus's single
`source_corpus_digest`. Old official audits carry a NULL lane reference and keep
the existing comparison, so **every existing corpus, audit and crosswalk remains
valid unchanged** — the v21→v22 upgrade is additive.

`reconstruction_corpus_versions.market_evidence_digest` stays **NULL and
superseded**, with the reason recorded in the migration header.

## 15. Implementation order (§P)

The informal order was wrong: it registered the linking provider before the
schema existed to bind its evidence.

1. **Independent adversarial review of this architecture.**
2. **v22 schema + provenance implementation** (§14).
3. **Independent review of that implementation.**
4. **Linking-provider registration** — `REGISTERED_LINKING_PROVIDERS`,
   `ATTESTED_GENERATIONS`, with the disjointness tests the identity architecture
   requires. It comes *here*, after the schema, because registering it earlier
   makes a linking digest computable with nowhere valid to bind it.
5. **Declare the Stage-A manifest** (committed file) and its acquisition rows.
6. **Stage-A first-pass acquisition** — 160 requests / ~160 credits, own cap.
7. **Stage-A certification** — the three reconciliations (§7).
8. **G5 event-id audit** → **curation** with the mandatory S8 counterfactual →
   independent review.
9. Target-anchor acquisition → E1.

Independent of all of it: **P1**, the `REPLACE` hardening for the remaining 28
append-only tables.

## 16. Validation

| Check | Result |
|---|---|
| Schema | **v21 / 21 migrations / 53 tables — unchanged** |
| Provider requests | **0** |
| Credits spent | **0** |
| Zero-network guards | 31 armed, **11/11 probes blocked** |
| Protected artefacts + 21 migrations | see the task report |
| Registries | `REGISTERED_LINKING_PROVIDERS` **empty**; `ATTESTED_GENERATIONS` unchanged |
| Stage A / G5 / crosswalk / F1-R | **not run** |
| Migration written | **none** |

## 17. Exact next authorization boundary

> **Independent adversarial review of this architecture**, then the **v22 schema
> and provenance implementation**.

A reviewer should attack, at minimum: whether per-lane bindings genuinely prevent
cross-lane evidence mixing; whether the plan-closure trigger really defeats
fetch-then-declare; whether `observed_at = received_at` is right for a
re-materialized probe; whether the three reconciliations can all balance while
evidence is still missing; and whether E1 truly slots in as a new lane without
disturbing an accepted E0 audit.

**F1-R remains blocked.**
