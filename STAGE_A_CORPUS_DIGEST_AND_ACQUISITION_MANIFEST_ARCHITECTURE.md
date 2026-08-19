# Stage-A Corpus Digest & Acquisition Manifest — Structural Architecture

**Starting HEAD:** `aa571a6` (`origin/main` = `aa571a6`, tree clean, schema
v21 / 21 migrations / 53 tables — **unchanged by this task**).
**Provider requests:** 0. **Credits spent:** 0. **F1-R remains blocked.**

Architecture and offline analysis only. Nothing implemented.

---

> **RECONCILED AFTER INDEPENDENT REVIEW.** See
> `STAGE_A_CORPUS_DIGEST_AND_ACQUISITION_MANIFEST_ARCHITECTURE_INDEPENDENT_REVIEW.md`,
> which found one critical defect — lanes cannot be appended to a
> content-addressed corpus — plus thirteen further repairs. The verdict table and
> the marked sections below are reconciled to that review. **The review is
> authoritative wherever the two differ.** Sections not marked `RECONCILED`
> survived review unchanged.

## Verdicts (reconciled)

| # | Question | Verdict |
|---|---|---|
| 1 | Complete Stage-A response-set representation | **NEW BINDING REQUIRED**; the plan reuses the manifest/checkpoint **infrastructure** under a **new Stage-A manifest schema** (§4) |
| 2 | One corpus / two audits | **PER-EVIDENCE-LANE DIGEST BINDINGS**, and the corpus **commits to its lane set**; a new lane yields a **new corpus version by supersession** (§3) |
| 3 | Projection-policy binding | recorded **per acquisition**, required uniform across a lane's members by the verifier |
| 4 | `observed_at` ownership | **`observed_at` = the cited `raw_responses.received_at`** — correct, but **tamper-evident only** (§6) |
| 5 | Completeness certification | **attempt-level + terminal-bucket-level + projection**, all derived from keyed sets (§7) |
| 6 | Probe-response reuse | **ALLOWED ONLY** against a pre-committed probe registration row (§8) |
| 7 | Schema | **V22 REQUIRED, repaired shape** (§14) |
| 8 | Overall | **ACCEPTED WITH REPAIRS** — this reconciled document is the implementation contract |

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

> **RECONCILED.** Review finding D1: `market_evidence_digest` is **an input to
> the corpus's `semantic_digest`**, and `reconstruction_corpus_versions` carries
> `UNIQUE (semantic_digest)` with the comment *"The semantic digest IS the corpus
> identity."* So this column is **not** a decorative slot awaiting a binding — it
> is how a corpus commits to having market evidence. Leaving it NULL while a lane
> row asserts market evidence exists makes the corpus contradict itself, and two
> reconstructions differing only in market evidence collapse into **one**
> `corpus_version_id` (proven). This column is therefore **used, not superseded**:
> for v22 it holds the E0 lane binding's digest. See §3 and §14.

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

> **RECONCILED.** Review finding D7: the reuse is of **infrastructure**, not of
> the manifest format. `f1a-manifest-v1` cannot carry a Stage-A plan as-is —
> `_SUPPORTED_COST_POLICY_VERSIONS` is `{"mlb-cost-v1", "bdl-cost-v1"}` with no
> Odds policy, `_SUPPORTED_PLAN_VERSIONS` is `{"f1a-plan-v1"}`, and, decisively,
> `PilotManifest.body()` **hashes `scratch_db` and `checkpoint_path`** — local
> filesystem paths — into the manifest identity, so the same logical plan hashed
> from a different directory yields a different hash. Stage A therefore reuses the
> canonicalization, duplicate-key rejection, atomic write and checkpoint
> machinery under a **new Stage-A manifest schema and policy version whose hashed
> body contains no local path**. Existing F1A manifests must stay byte-identical
> and parseable; the new format is additive.

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
    source_corpus_digest   the lane's digest
    acquisition_set_digest over the sorted member acquisition ids (§4)

corpus_evidence_lane_acquisitions          -- append-only join
    lane_binding_id, acquisition_id
```

An identity audit binds to **its lane's row**, not to the corpus's single
`source_corpus_digest`.

> **RECONCILED.** Three repairs from the review:
>
> - **D5.** The original shape carried one `acquisition_manifest_id` while §12
>   allowed several manifests per lane — a contradiction. Replaced by the join
>   table plus `acquisition_set_digest`, so a lane that omits a member and a lane
>   whose digest covers a non-member **both** fail recomputation.
> - **D11.** `projection_policy_version` moves to the **acquisition**; the
>   verifier requires it uniform across a lane's members and refuses a mixed lane.
> - **D1 (critical).** Lane rows are **not** appended to an existing corpus. The
>   corpus commits to its lane set: adding the E0 lane sets
>   `market_evidence_digest` to that lane's digest, which changes
>   `semantic_digest` and therefore yields a **new corpus version that supersedes
>   the old one**. f018 already guarantees *"Supersession APPENDS. The superseded
>   row is never touched"*, so the old corpus and every audit and crosswalk bound
>   to it stay valid forever. Point 8 below ("no corpus row rewritten") still
>   holds — nothing is updated; a new row is inserted.

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
   changing no existing lane's digest and invalidating no accepted audit.
   *(**RECONCILED**, D1/X: "invalidating no accepted audit" is true under
   supersession for **every** option, including the composite ones this section
   rejects — the old corpus row is never touched. The rejection of Options 1 and 3
   therefore rests only on their coarser granularity, not on blast radius. E1 will
   additionally need a generic **lane-set digest** on the corpus, because a single
   E0-shaped column cannot commit to two market lanes; that is a v23 extension,
   not a v22 blocker.)*
7. **Append-only** — same trigger pattern as f018/f021.
8. **No corpus row rewritten** — lanes are inserted, never an `UPDATE`.
9. **Triggers fail closed** — see §10.
10. **Runtime can name its authorization** — the crosswalk cites the audit, the
    audit cites the lane, the lane cites the corpus.
11. **Composition is explicit** — a corpus with two lanes says so in rows.
12. **Deterministic replay** — every value is content-derived.

> **RECONCILED — this paragraph is REVERSED.** The original text read
> *"`market_evidence_digest` is superseded, not used. It should remain NULL."*
> Review finding D1 proves the opposite: the column is an input to
> `semantic_digest`, which is the corpus identity under
> `UNIQUE (semantic_digest)`. Leaving it NULL is precisely what lets two distinct
> reconstructions collapse into one `corpus_version_id`.
>
> **Disposition:** `market_evidence_digest` is **used**. For v22 it holds the E0
> lane binding's digest, so the corpus commits to its market lane. It is neither
> deleted nor repurposed — it is used for exactly what f018 documented it as: the
> market snapshot evidence set. Corpora with no market lane keep NULL, which
> correctly means "no market evidence".

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

> **RECONCILED.** The shape below is repaired per review findings D6, D13 and D9:
> plan identity is split from acquisition identity, target membership becomes DB
> rows, and probe eligibility gets a persisted object.

```
stage_a_plans                              -- WHAT was declared
    plan_id                   PK
    plan_digest               UNIQUE, over the rows below + policy versions
    manifest_commit_sha, manifest_content_digest, manifest_path
    manifest_format_version, plan_policy_version
    league_id, provider, namespace_generation, sport_key
    official_source_corpus_digest, official_target_set_digest   -- D14
    append-only

stage_a_plan_targets                       -- D13: the DB proves the population
    plan_id, canonical_game_id, requested_at_bucket
    append-only; closed with the bucket set

stage_a_planned_buckets
    plan_id, requested_at_bucket            UNIQUE together
    append-only; the CLOSED set of authorized buckets

stage_a_acquisitions                       -- ONE EXECUTION of a plan
    acquisition_id            PK, 'sga_' prefix
    plan_id                   FK  (NOT unique -- D6)
    acquisition_policy_version, projection_policy_version
    request_budget, credit_budget
    registered_at             registration instant; compared to requested_at
    append-only

stage_a_request_attempts
    acquisition_id, requested_at_bucket     -> planned_buckets (FK)
    attempt_ordinal
    outcome        enum, §7
    raw_response_id  see §7 for which outcomes require / forbid it
    append-only

stage_a_probe_registrations                -- D9
    raw_response_id, registered_at, probe_report_commit_sha
    append-only; committed BEFORE the plan
```

`declared_at` is **deleted** as a provenance field (D8): it was caller-supplied
and directly backdatable, so it proved nothing. `registered_at` replaces it with a
purpose: it is the instant every non-probe attempt's `requested_at` must exceed.

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

> **RECONCILED — the claim above is OVERSTATED (D8).** None of the four
> mechanisms establishes that the *raw response* was acquired after declaration.
> The closure trigger fires on the first **attempt** row, and an attempt may cite
> a response that already existed; the whole fetch-then-declare sequence survives.
> Point 3 is also wrong on its own terms: git commit dates are attacker-settable
> (`GIT_COMMITTER_DATE`), so git history is **not** trustworthy chronology.
>
> **Required additions:**
>
> 5. Plan and acquisition registration is persisted **before any transport**, and
>    a trigger requires every non-probe attempt's cited
>    `raw_responses.requested_at` to be **≥ `stage_a_acquisitions.registered_at`**.
> 6. The acquisition binds the manifest's **commit SHA and content digest**, and
>    the verifier re-hashes the file at that commit. The **content digest is the
>    semantic identity**; `manifest_path` is a pointer that may rot and must never
>    become identity (G).
> 7. A response predating registration is admissible **only** as
>    `REUSED_PROBE_RESPONSE` under §8's eligibility proof.
>
> **Stated trust boundary.** Even repaired, this is **tamper-evidence, not
> cryptographic proof of temporal ordering**. An operator with direct SQL write
> access to the evidence database can construct a self-consistent history. That is
> the reviewed boundary and this document claims nothing stronger.

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

> **RECONCILED — decision upheld, justification corrected (D10).** "A caller
> cannot invent it" is **false**. `raw_responses.received_at` is constrained only
> by *shape* — `CHECK (received_at LIKE '____-__-__T__:__:__%Z')` (b004:148) —
> with no ordering check against `requested_at` and no calendar validity, the same
> defect class f019's D5 repaired for the retrospective tables. A caller writing
> the raw response controls `received_at`, and `observed_at` inherits exactly that
> trust level.
>
> The decision still stands, for the accurate reason: it **removes a free
> parameter**, forcing one clock instead of two, and ties the observation to a row
> that other verifiers already check. Required repairs: add
> `received_at >= requested_at` and calendar validity before relying on it.
>
> **Semantics, explicit:** `observed_at` is when **we first possessed the
> preserved evidence** — never the historical provider snapshot instant. A March
> snapshot received in August has an **August** `observed_at`. A response
> transported between databases keeps its original `received_at`; re-receiving the
> same content in a new exchange is a new raw response with its own clock.

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

> **RECONCILED — this equation is BROKEN as written (D4).** It balances *planned
> buckets* against a *sum of outcome counts*, while
> `UNIQUE(acquisition_id, bucket, attempt_ordinal)` permits N attempts per bucket.
> Proven: 2 planned buckets with one retry produce 3 attempt rows, and
> `planned == sum` is **False**. Replace with two reconciliations:
>
> **(i) Attempt reconciliation** — every attempt row carries exactly one outcome;
> none is discarded. A failure that preceded a successful retry stays permanently
> visible.
>
> **(ii) Terminal bucket reconciliation** — every planned bucket maps to exactly
> one *derived* terminal classification, computed from its attempts by a stated
> rule (a bucket is terminal-success iff exactly one attempt succeeded).
>
> **First-pass policy: retries are FORBIDDEN** (one attempt per bucket).
> `attempt_ordinal` stays in the key so a future declared retry policy needs no
> schema change.
>
> **Outcome ↔ `raw_response_id` (M).** Categories where a response was preserved —
> `succeeded_full_snapshot`, `succeeded_empty_data`, `reused_probe_response`,
> `malformed_wrapper`, `projection_rejected_snapshot`, and any HTTP-status failure
> (401/429/500) — **REQUIRE** a `raw_response_id`: a provider failure carrying an
> HTTP response must never discard its raw evidence. Categories where no transport
> completed — transport timeout, DNS failure, `budget_blocked`, `quota_blocked`,
> `not_requested` — **MUST** have NULL. No category may leave it optional.
>
> **Raw-response membership (L).** One `raw_response_id` may appear **at most
> once** across all attempts of one acquisition (UNIQUE), so it cannot be counted
> for two buckets, nor as both a success and a reuse. The same probe response
> **may** legitimately be reused by two independent acquisitions of the same plan
> — that is honest shared provenance — so the constraint is per-acquisition, not
> global.

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

> **RECONCILED — counts are not sufficient (T, D3).** Equal counts can hide a
> double-counted response, a bucket with two attempts beside one with none, or a
> target dropped while the bucket count is unchanged. Certification must be
> derived from **keyed set equalities**, never from caller-supplied counters:
>
> - `{planned buckets}` **=** `{buckets with a terminal classification}`
> - `{plan targets}` **=** `{targets in the committed manifest}`, and every target
>   maps to exactly one planned bucket (no target unmapped, none mapped twice)
> - `{attempt raw_response_ids}` is **injective** within the acquisition
> - `{succeeded response ids}` **=** `{response ids with ≥1 observation}`
> - `{observation raw_response_ids}` **⊆** `{acquisition response ids}`
>   (orphans empty)
> - `{reused probe ids}` **⊆** `{registered probe ids}` and disjoint from
>   `{ordinary success ids}`
>
> **Lane acceptance additionally requires recomputation (D3).** A digest string is
> caller-supplied and forgeable at insert — a trigger cannot compute SHA-256 over
> evidence rows, and append-only only prevents *rewriting* a forged value. Before
> a lane may back an ACCEPTED audit, one deterministic verifier must recompute and
> compare: acquisition membership → acquisition completeness → projection
> completeness → observation-content integrity → lane evidence digest →
> `acquisition_set_digest` → policy versions. Any disagreement refuses the audit.

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

> **RECONCILED — the economic argument survives, the enforceability did not
> (D9).** Searched: `raw_responses` has **no** probe classification. The only
> `probe_name` in the schema is on `provider_capabilities` (d010), which concerns
> capability verification, not response membership. So nothing distinguishes the
> legitimate probe response from any other pre-manifest response, and
> `REUSED_PROBE_RESPONSE` as originally written is a **general bypass of
> plan-before-network wearing a specific name**.
>
> **Required eligibility proof**, every clause machine-checkable, before the gate
> accepts a pre-registration response:
>
> - an append-only **`stage_a_probe_registrations` row naming that exact
>   `raw_response_id`, committed before the plan** — this is the object whose
>   absence made the exception ungeneralizable in name only;
> - exact endpoint; unfiltered shape (`{apiKey, date, dateFormat}`); HTTP 200;
>   valid wrapper; passes the projection verifier;
> - its bucket is a member of the declared plan;
> - **at most one** reused probe per bucket, and the gate **refuses** when several
>   candidate pre-existing responses exist for one bucket — otherwise a curator
>   silently picks the convenient one.

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

> **RECONCILED (D13).** Binding the mapping into the *plan digest* alone is not
> enough, because §7c presents certification as a **database** property while the
> mapping would live only in a committed file. The database cannot then prove
> target completeness at all. Repair: `stage_a_plan_targets(plan_id,
> canonical_game_id, requested_at_bucket)` — append-only, closed with the bucket
> set — with `plan_digest` recomputed over those rows. The DB then proves the
> population itself.
>
> The **offline manifest verifier remains part of the certification gate** and is
> named as such: it confirms the committed manifest's target set equals the
> persisted rows. This document does **not** claim the database alone proves
> target completeness.

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

> **RECONCILED — four rows of this table were wrong or incomplete.**
>
> | Row | Correction |
> |---|---|
> | "Official digest from corpus A + Odds digest from corpus B" | A child FK **does not** prevent this: two reconstructions differing only in market evidence collapse into one `corpus_version_id` (D1a, proven). Prevented only once the corpus commits to its lane set (§3) **and** the plan binds the official corpus's `source_corpus_digest` + `target_set_digest`, refused on mismatch at lane attachment (D14). |
> | "A successful/failed response omitted" | The cited equation does not balance under retries (D4). Replaced by the keyed set equalities in §7c. |
> | "`observed_at` backdated" | The trigger forces equality with `received_at`, but `received_at` itself is only shape-checked (D10). Tamper-evident, not tamper-proof. |
> | "Audit cites the wrong lane" | True only if a lane is **required**. With a nullable lane FK a new linking audit sets NULL, forges the digest and is **ACCEPTED** (D2, proven). NULL must be permitted only for legacy official audits; non-official providers must cite a matching lane. |
>
> Two threats the table omitted: **digest forgery at insert** (D3 — no trigger
> recomputes SHA-256; only the §7c verifier closes it) and **`digest_policy_version`
> redefinition** (D12 — the version string records *which* policy ran but does not
> stop that version's meaning changing when a provider or table is later added; a
> CI invariant must pin each frozen version's resolved table/column set).

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

> **RECONCILED.** The "multiple manifests per lane" statement contradicted §3's
> single `acquisition_manifest_id` (D5). Resolved in §3's favour of *plural*: a
> `corpus_evidence_lane_acquisitions` join table plus an `acquisition_set_digest`,
> so omitting a member and covering a non-member both fail recomputation.
>
> **Parent-corpus binding (D14).** "Created after certification" left unstated
> *which* corpus. Stage A is planned from official corpus **C1** but the lane is
> attached to corpus **C2**; nothing here enforced C1 ≡ C2. The plan therefore
> records C1's `source_corpus_digest` and `target_set_digest`, and lane attachment
> is refused unless the parent corpus matches both. Combined with D1a's collision
> this was the most direct cross-lane mixing route.
>
> **Checkpoint vs database authority (K).** Stated explicitly, because two
> ledgers now exist: the **database is the durable scientific evidence ledger and
> is authoritative for certification**; the **checkpoint is operational resume
> state only**. A lost, stale or copied checkpoint may cause conservative re-work
> — never a different scientific verdict. Certification reads only DB rows. This
> matches the existing checkpoint contract, which already records completion only
> after the raw response *and* its normalized persistence commit in one
> transaction, so a checkpoint can lag the DB but cannot legitimately lead it.

## 13. E1 future compatibility (§M)

E1 historical **prices** arrive as a **new lane** (`market_prices_e1`) with its
own acquisition manifest, its own digest policy and its own audit. It changes no
existing lane's digest, so **every already-accepted audit remains valid** and no
corpus row is rewritten.

That is the single strongest argument for Option 2 over Options 1 and 3, both of
which would invalidate the Odds E0 audit the moment E1 landed — a redesign
forced immediately after shipping.

> **RECONCILED — this argument does not hold, and two claims are corrected.**
>
> Under f018's supersession rule the superseded corpus row *is never touched*, so
> an accepted E0 audit stays valid when E1 lands under **any** of the options,
> including the composite ones. The rejection of Options 1 and 3 therefore rests
> on **granularity** — a lane needs its own provider, namespace generation and
> policy versions, which one column cannot carry — not on blast radius.
>
> Two corrections to this section's wording:
>
> - E1 prices need **evidence certification, not an identity audit**.
>   `identity_audit_records` concerns identifier cleanliness and does not
>   meaningfully apply to a price series; E1 should **cite the E0 crosswalk** for
>   identity rather than re-auditing it.
> - Because the corpus commits to its lane set through a single E0-shaped column,
>   E1 will require a generic **lane-set digest** on the corpus. The lane *table*
>   is generic enough; the corpus's commitment is not. That is a **v23 extension,
>   not a v22 blocker** — but the claim that E1 slots in with no schema change is
>   withdrawn.

## 14. Schema verdict — **V22 REQUIRED**

v21 cannot express this. Conceptual additions only; **no migration is written
here**.

> **RECONCILED.** The repaired object set is below. Every new append-only table
> **must** use the hardened f021 content-aware BEFORE-INSERT pattern from day one,
> because the ordinary BEFORE UPDATE/DELETE pattern does not stop SQLite `REPLACE`
> (Y). **P1** — the 28 legacy tables — remains outstanding and is not authorized
> by this document.

| Object | Purpose | PK / Unique | Append-only | Must NOT contain |
|---|---|---|---|---|
| `stage_a_plans` | plan identity, independently re-executable | `plan_id`; UNIQUE `plan_digest` | yes | any secret |
| `stage_a_plan_targets` | the DB-provable target population | UNIQUE `(plan_id, canonical_game_id)` | yes, closed with buckets | — |
| `stage_a_planned_buckets` | the closed authorized bucket set | UNIQUE `(plan_id, requested_at_bucket)` | yes, **closed once any attempt exists** | canonical identity |
| `stage_a_acquisitions` | ONE execution of a plan | `acquisition_id`; FK → plan, **not** unique on it | yes | any canonical game id; any secret |
| `stage_a_request_attempts` | honest per-attempt outcome ledger | UNIQUE `(acquisition_id, bucket, attempt_ordinal)`; UNIQUE `(acquisition_id, raw_response_id)`; FK → planned bucket, FK → `raw_responses` | yes | canonical identity |
| `stage_a_probe_registrations` | makes probe reuse machine-verifiable | UNIQUE `raw_response_id` | yes | any secret |
| `corpus_evidence_lane_bindings` | per-lane digest + policy versions | UNIQUE `(corpus_version_id, evidence_lane)` | yes | a second corpus's data |
| `corpus_evidence_lane_acquisitions` | lane ↔ many acquisitions | UNIQUE `(lane_binding_id, acquisition_id)` | yes | — |
| `identity_audit_records` | **one added lane FK**, nullable only for legacy official audits | — | unchanged | — |

**Trigger changes:**

1. `trg_xwk_audit_corpus_binding` compares an audit against its **lane binding's**
   digest. A NULL lane keeps the existing comparison — but **only** when the
   audit's provider is an official provider; a non-official provider with a NULL
   lane is **refused** (D2, otherwise the whole mechanism is bypassable).
2. Lane attachment refuses unless the parent corpus's `source_corpus_digest` and
   `target_set_digest` equal the plan's recorded official values (D14).
3. Non-probe attempts require `raw_responses.requested_at >=
   stage_a_acquisitions.registered_at` (D8).

`reconstruction_corpus_versions.market_evidence_digest` is **used, not
superseded**: for v22 it carries the E0 lane binding's digest so the corpus
commits to its lane set (D1). Adding a lane therefore produces a **new corpus
version by supersession**; existing corpora, audits and crosswalks remain valid
untouched, so the v21→v22 upgrade is still additive.

**No certification-record table is added.** Certification is *derived* by the
verifier; a stored verdict would be exactly the caller-supplied claim D3 warns
against.

## 15. Implementation order (§P)

The informal order was wrong: it registered the linking provider before the
schema existed to bind its evidence.

1. **Independent adversarial review of this architecture.** *(**DONE** — findings
   D1–D14 are reconciled into this document, which is now the implementation
   contract.)*
2. **v22 schema + provenance implementation** (§14, repaired shape).
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
