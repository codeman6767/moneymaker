# v22 — Stage-A Plan / Acquisition / Evidence-Lane Provenance

**Starting HEAD:** `de7a48a` (= `origin/main`, tree clean, schema v21 / 21 migrations / 53 tables).
**Schema after this task:** **v22 / 22 migrations / 61 tables.**
**Provider requests:** 0. **Credits spent:** 0. **Historical quota unchanged: 19,998.**

Implements `STAGE_A_CORPUS_DIGEST_AND_ACQUISITION_MANIFEST_ARCHITECTURE.md` as
reconciled at `de7a48a`, with its independent review authoritative wherever the
two disagree.

---

> **SUPERSEDED IN PART.** The independent review
> (`V22_STAGE_A_PROVENANCE_INDEPENDENT_REVIEW.md`) reproduced nine defects against
> `46f1725`, two critical: the certification gate did **not** compose the accepted
> projection/body verifier, and an **uncertified** acquisition could be enriched
> into a superseding corpus. Six are repaired; three are retained blockers. In
> particular:
>
> - §13's "projection completeness" claim was **not** implemented at `46f1725`;
>   it is now, by calling the accepted verifier rather than a local copy.
> - The lane-backed crosswalk described in §12 is **structurally impossible**:
>   f019's trigger still fires, so f022's new trigger is unreachable today.
> - Probe registration is **not** content-bound, so `REUSED_PROBE_RESPONSE`
>   remains generalizable until that is closed.
>
> The review is authoritative wherever the two differ.

## 1. The central correction this is built around

`reconstruction_corpus_versions` is **content-addressed**. f018 declares

```sql
-- The semantic digest IS the corpus identity.
CONSTRAINT rcv_semantic_digest_unique UNIQUE (semantic_digest)
```

and `record_corpus_version` computes that digest over a fixed field set that
**already includes `market_evidence_digest`**. So `corpus_version_id` means a
fixed, complete, enumerated evidence set — and evidence lanes may **not** be
appended to an existing corpus.

The implemented E0 flow is therefore:

```
official corpus C1  +  certified E0 lane digest D  ->  NEW corpus C2
    C2.market_evidence_digest = D
    C2.semantic_digest        commits to that market evidence
    C2 supersedes C1;  C1 is never touched
```

`enrich_corpus_with_market_lane` performs exactly this and refuses if the
enriched corpus collapses back onto its parent. Because f018's supersession rule
states *"Supersession APPENDS. The superseded row is never touched"*, every audit
and crosswalk bound to C1 keeps its exact meaning forever.

**`market_evidence_digest` is USED, not deprecated** — the reversal the review
required. A corpus with no market lane keeps NULL, which correctly means "no
market evidence".

## 2. Migration f022 — new objects

| Object | Purpose |
|---|---|
| `stage_a_plans` | plan identity: `plan_digest` UNIQUE, manifest commit SHA + content digest, official parent provenance, policy versions |
| `stage_a_plan_targets` | the DB-provable target population and target→bucket mapping |
| `stage_a_planned_buckets` | the closed set of authorized request buckets |
| `stage_a_acquisitions` | ONE execution of a plan (`plan_id` deliberately **not** unique) |
| `stage_a_request_attempts` | the honest per-attempt outcome ledger |
| `stage_a_probe_registrations` | makes probe reuse machine-verifiable |
| `corpus_evidence_lane_bindings` | per-lane digest, policies, `acquisition_set_digest` |
| `corpus_evidence_lane_acquisitions` | lane ↔ many acquisitions |
| `identity_audit_records.lane_binding_id` | added nullable FK (ADD COLUMN, no row rewrite) |

`f018`–`f021` are **byte-identical** and were not edited. The only change to an
existing table is one `ALTER TABLE ... ADD COLUMN`, which SQLite performs without
rewriting rows and which defaults to NULL.

**Every new append-only table uses the hardened f021 pattern from day one**:
BEFORE UPDATE + BEFORE DELETE guards *plus* a content-aware BEFORE INSERT guard.
The last is what actually stops `REPLACE`, because SQLite's REPLACE conflict
resolution performs an implicit DELETE that skips DELETE triggers unless
`PRAGMA recursive_triggers` is on — and a pragma is per-connection, so the
guarantee cannot live there. Guards compare CONTENT, not bare key existence, so a
legitimate idempotent `INSERT OR IGNORE` stays a no-op.

## 3. Plan identity is distinct from acquisition identity

A plan may be executed more than once: an aborted run restarted from scratch, an
independent reproduction months later, the same plan against a different
database. Deriving `acquisition_id` from `plan_digest` would make all of those
unrepresentable. So `stage_a_acquisitions.plan_id` is **not** unique, and the
acquisition id is surrogate.

## 4. The Stage-A manifest is a new, additive format

`f1a-manifest-v1` is untouched and keeps parsing exactly as before. A new
`stage-a-manifest-v1` exists because the reviewed format could not carry a
Stage-A plan:

* it has no Odds cost policy (`_SUPPORTED_COST_POLICY_VERSIONS` refuses one);
* **`PilotManifest.body()` hashes `scratch_db` and `checkpoint_path`** — local
  filesystem paths — so the same logical plan would hash differently per checkout;
* it cannot represent the target population, the target→bucket mapping, or the
  official parent-corpus provenance.

The new format reuses the reviewed **infrastructure**: canonical JSON, duplicate
key rejection, secret-free bodies, SHA-256 identity. Its hashed body contains
**no path, no wall clock, no random id and no secret**. `manifest_path` is stored
on the plan row as convenience provenance and is never hashed.

The plan digest binds the target→bucket **mapping**, not merely the bucket set,
because the pilot maps 239 targets onto 160 buckets — by pigeonhole many buckets
serve several targets, so dropping one leaves the sorted bucket set
byte-identical while a target silently disappears.

## 5. The `_policy_for` fail-open repair

The review proved that `planning._policy_for` was
`build_balldontlie_policy() if provider == "balldontlie" else build_mlb_policy()`
— every unknown provider silently inherited MLB's model. Measured before repair:

```
_policy_for('the_odds_api') -> mlb-cost-v1
```

MLB is keyless and reports credits as *not applicable*, so a credit-metered
provider would have been planned and budgeted as if its requests were free — and
the emitted `cost_policy_version` was a **real registered version**, so it passed
the manifest's supported-version check instead of being rejected.

Repaired with an explicit registry and `UnknownProviderError`:

| Provider | Version | Credits |
|---|---|---|
| `mlb_statsapi` | `mlb-cost-v1` (unchanged) | not applicable |
| `balldontlie` | `bdl-cost-v1` (unchanged) | not applicable |
| `the_odds_api` | **`odds-cost-v1`** | **applicable, 1 credit per historical events request** |

No normalization is applied: a case variant, padded string or misspelling is a
DIFFERENT identifier and is refused rather than repaired. Only
`historical_events` is priced; a historical *odds* or *scores* path classifies as
`unknown` and fails closed, because those are separately priced and unauthorized.

## 6. Attempt vs terminal-bucket reconciliation

The broken `planned_buckets == sum(attempt outcomes)` equation is **not**
reproduced: it cannot balance once any bucket is retried. Two separate
reconciliations replace it, both derived by the verifier:

* **attempt reconciliation** — every attempt row carries exactly one outcome and
  none is discarded, so a failure preceding a retry stays permanently visible;
* **terminal bucket reconciliation** — every planned bucket gets exactly one
  derived terminal classification.

**First-pass policy: retries are FORBIDDEN**, enforced by
`trg_sat_first_pass_forbids_retries`. `attempt_ordinal` is retained structurally
so a future explicitly-versioned retry policy needs no schema change.

`raw_response_id` rules are fail-closed CHECK constraints: outcomes carrying an
HTTP exchange (including 401 / 429 / 500) **require** the preserved response — a
provider failure with a body must never discard its raw evidence — and outcomes
where no transport completed **must** be NULL. No outcome leaves it optional.
**A request failure can never become "no market".**

## 7. Plan-before-network — the honest trust boundary

Enforced: plan and acquisition are registered before transport; plan membership
closes on the first attempt; an ordinary attempt's cited
`raw_responses.requested_at` must be **≥** `stage_a_acquisitions.registered_at`;
a pre-registration response is admissible **only** as `REUSED_PROBE_RESPONSE`.

**This is tamper-EVIDENCE, not cryptographic proof of temporal ordering.** An
operator with direct SQL write access can construct a self-consistent history.
Git commit dates are attacker-settable (`GIT_COMMITTER_DATE`), so the commit SHA
binds *history*, not a wall clock. `declared_at` was **deleted** from the design
because it was caller-supplied and backdatable; `registered_at` replaces it with
one enforceable job and is not part of plan identity.

## 8. Probe registration

`raw_responses` has no probe classification, so without a registration object
`REUSED_PROBE_RESPONSE` would be a general bypass wearing a specific name.
`stage_a_probe_registrations` binds the exact `raw_response_id`, the probe report
commit SHA and path, and the probe policy version.

A registration means **only** that this exact preserved response was an
independently documented capability probe that the gate may CONSIDER. It carries
**no identity semantics**: not audit acceptance, not event-id stability, not any
mapping to a canonical game, not provider trust for identity, not namespace
registration.

The gate additionally requires: exact historical events endpoint, unfiltered
request allow-list, HTTP 200, valid wrapper, projection verifier passes, bucket
is in the declared plan, original clocks preserved, and **at most one** eligible
reused probe per bucket — several candidates cause REFUSAL rather than selection.

**No probe was registered in this task.** The real successful entitlement probe
is untouched and is not consumed into production provenance.

## 9. `observed_at` and receipt-clock integrity

`observed_at` MUST equal the cited `raw_responses.received_at`, enforced at all
three layers the architecture named:

1. **Write path.** `SqliteMarketObservationRepository.record` previously accepted
   `observed_at` as a caller parameter. It now **derives** it from the cited
   response, refuses an observation citing a response that is not preserved, and
   refuses a caller-supplied value that disagrees. Before this change the DB
   trigger was the only enforcement, so a caller could still pass any value and
   receive a hard error instead of correct behaviour.
2. **Database.** `trg_hme_observed_at_equals_cited_receipt`, which stops direct
   SQL that bypasses the repository entirely.
3. **Gate.** Re-checked in `certify_stage_a`, which also covers rows written
   before the trigger existed.

Semantics: `observed_at` is when **we** possessed the evidence — never the
provider snapshot instant. A March snapshot received in August has an **August**
`observed_at`.

b004 constrained `requested_at` / `received_at` by shape only. Since v22 makes
that clock load-bearing, `trg_raw_responses_receipt_clock_integrity` adds
calendar validity and `received_at >= requested_at` as a **forward** trigger.
b004 is applied evidence and was not edited; existing preserved rows keep their
bytes, and the gate fails closed if such a row is proposed for certification.

The v20 decision that `observed_at` is excluded from the observation content hash
is **unchanged**.

## 10. Digest-policy freezing

A lane records `digest_policy_version`, but the live source-table selection
derives from mutable registries. `stage_a_policies.py` pins each frozen version
to an exact table and column set captured as data. Adding a linking provider, a
table, or E1 cannot redefine an existing version; a changed source contract
requires a NEW version. Paired CI invariants assert the live registry still
agrees with the frozen snapshot — and one of them **caught a real error during
implementation**, where the frozen official set had been written from memory
rather than from the registry.

## 11. Lane digests are never trusted at insert

A trigger cannot compute SHA-256 over evidence rows, and append-only only
prevents *rewriting* a forged value. `verify_lane_binding` recomputes both the
`lane_evidence_digest` and the `acquisition_set_digest` from actual membership,
so **both** omission directions fail: a lane citing `{A}` whose evidence digest
covers A+B, and a lane citing `{A,B}` whose digest covers only A.

Lane attachment additionally requires the parent corpus's `source_corpus_digest`
and `target_set_digest` to equal the plan's recorded official values — closing
the C1→C2 cross-lane mixing attack — and requires the parent corpus to commit to
the lane digest in `market_evidence_digest`.

## 12. Audit lane binding

`lane_binding_id` is NULL-able **only** for the legacy official path.
`trg_ida_lane_required_for_non_official_providers` refuses a NULL lane for any
provider outside `('balldontlie', 'mlb_statsapi')`, closing the bypass the review
reproduced at v21 (a linking audit with `namespace_verified=1`, a forged official
digest and no lane was ACCEPTED). The official provider set is spelled in SQL
rather than read from a Python registry, so widening it requires a reviewable
migration.

When a lane IS cited it must match the audit's provider, namespace generation and
league, and the audit's digest must equal the lane's.

## 13. Certification is derived, never stored

There is deliberately no `certified` column: a stored verdict would be exactly
the caller-supplied claim this design avoids. `certify_stage_a` recomputes
manifest integrity, target population and mapping, bucket set, attempt and
terminal reconciliation, probe eligibility, response integrity, projection
completeness, observation `observed_at` ownership and orphan detection.

Counts are returned for **reporting only**. Every decision rests on keyed set
equalities, because equal counts can hide a double-counted response, a bucket
with two attempts beside one with none, or a target dropped while the count is
unchanged.

**The database is the evidence ledger and is authoritative for certification;
the checkpoint is operational resume state only.** A stale, missing or copied
checkpoint may cause conservative re-work, never a different scientific verdict.

## 14. Non-regression

`REGISTERED_LINKING_PROVIDERS` remains **empty**; `ATTESTED_GENERATIONS` is
unchanged; The Odds API holds no official provider authority; no canonical game
was created or mutated; no runtime alias/name matching was introduced; Stage-A
provenance tables are not feature sources and `historical_market_event_observations`
remains unavailable as a Lane-L feature.

## 15. E1 / v23 boundary

Not solved here, by design. E1 prices will need evidence certification rather
than an identity audit, and a generic **lane-set commitment** on the corpus,
because v22's `market_evidence_digest` is E0-shaped. The lane *table* is generic
enough to carry E1 without being thrown away. No price table, E1 manifest, lane
set root or v23 work exists in this change.

## 16. Limitations

- Plan-before-network is tamper-evidence only (§7).
- The gate's probe-eligibility check reads request params from
  `raw_responses.request_params_json`; a response preserved without them cannot
  be certified for reuse (fails closed).
- Terminal-bucket reconciliation currently assumes the first-pass no-retry
  policy; a retry-capable policy will need its own derived terminal rule.
- `verify_lane_binding` digests the lane over its member acquisitions' responses;
  it does not yet re-run the full projection verifier per response (the gate does
  that separately).

## 17. Status

| Item | State |
|---|---|
| Stage A executed | **NO** |
| Real 160-bucket plan declared | **NO** |
| Linking provider registered | **NO** |
| `ATTESTED_GENERATIONS` widened | **NO** |
| G5 run | **NO** |
| Real identity audit / crosswalk created | **NO** |
| F1-R executed | **NO** |
| E1 fetched | **NO** |
| P1 (28 legacy tables) performed | **NO** |
| Provider requests / credits | **0 / 0** |

**v22 was reviewed; see `V22_STAGE_A_PROVENANCE_INDEPENDENT_REVIEW.md` for the
verdict (ACCEPTED WITH REPAIRS) and the three retained blockers that gate the
real probe registration and plan declaration.**
