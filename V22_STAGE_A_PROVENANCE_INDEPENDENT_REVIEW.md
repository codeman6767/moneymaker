# Independent Adversarial Review — v22 Stage-A Provenance

**Reviewed artefact:** v22 at `46f1725` (schema v22 / 22 migrations / 61 tables).
**Baseline verified:** HEAD = `origin/main` = `46f1725`, tree clean, f018–f021
byte-identical to their pre-v22 form (`git diff de7a48a HEAD` empty for all four).
**Provider requests:** 0. **Credits:** 0. **Guards:** 31 armed, 11/11 blocked.

## VERDICT: **ACCEPTED WITH REPAIRS — plus three retained blockers**

Nine defects were reproduced against `46f1725`, two of them **critical**. Six are
repaired here with regression tests; three are recorded as retained blockers that
belong to the tasks that will actually depend on them.

The headline finding: **the v22 gate did not compose the independently accepted
projection/body verifier.** It imported two constants from
`historical_events_projection` and reimplemented a weaker check, so the L1
selective-materialization defect — the whole reason that verifier was accepted —
was reintroduced one layer up.

---

## Defects reproduced and repaired

### D1 (CRITICAL) — `certify_stage_a` did not verify bodies at all

`stage_a_provenance.py` imported only `HISTORICAL_EVENTS_ENDPOINTS` and
`STAGE_A_ALLOWED_REQUEST_PARAMS`. `_observation_failures` checked `observed_at`
equality, orphans, "a full snapshot has ≥1 observation", and that observations
only exist on projecting outcomes. **None of that opens the preserved body.**

Reproduced at `46f1725`, all certifying with **zero failures**:

| Attack | Result before repair |
|---|---|
| Body lists 2 events, only 1 observation stored | **CERTIFIED** |
| Body lists A, observations claim A + invented B | **CERTIFIED** |
| Stored team label does not match the body | **CERTIFIED** |
| Forged `observation_content_hash` | **CERTIFIED** |
| Malformed wrapper labelled `success_full_snapshot` | **CERTIFIED** |

The first row is the L1 threat verbatim: half the provider's evidence vanishes
and the acquisition still certifies.

**Repair:** the gate now *calls* `verify_historical_event_projections` per
projecting response and `verify_observation_content_hashes` for the acquisition's
rows, reporting missing, unexpected and hash-mismatched observations separately.
All five attacks now fail, each for its own correct reason.

### D2 (CRITICAL) — an uncertified acquisition could be enriched into C2

`enrich_corpus_with_market_lane` computed digests and minted a superseding corpus
without ever invoking the gate. Reproduced: an acquisition missing 1 of 2 planned
buckets returned `certified=False`, and enrichment **succeeded anyway**, creating
a C2 whose `market_evidence_digest` commits to incomplete evidence.

"Certification is derived, never stored" is only true if the consumer that
creates load-bearing provenance actually invokes it. A derived verdict nobody is
required to call is not a trust gate.

**Repair:** enrichment now requires a manifest per member and refuses unless
every member certifies.

### D3 (HIGH) — `registered_at` was a caller parameter

The reconciled architecture deleted `declared_at` because it was backdatable;
`register_acquisition` then exposed `registered_at` as an optional argument.
Reproduced: acquire a response in August, register the acquisition dated January,
record it as an **ordinary success** — accepted.

**Repair:** the parameter is removed; the clock comes from `utc_now_iso()`.
Backdating now requires direct SQL, which is the stated tamper-evidence boundary
rather than a supported API feature.

### D4 (HIGH) — outcome labels were unverified caller claims

A body containing a real market event could be recorded `success_empty_data` and
certify, erasing the event from the evidence set by relabelling the ledger.

**Repair:** `_outcome_claim_failures` derives full-vs-empty from the projected
body. A genuinely empty snapshot still certifies (verified).

### D5 (MEDIUM-HIGH) — budgets and policy versions were decorative

`request_budget`/`credit_budget` were stored and never compared. Reproduced: a
manifest declaring 10/10 with an acquisition storing 1/0 certified cleanly.

**Repair:** certification compares both budgets, both policy versions, and the
plan's provider, namespace, sport key, league, cost policy and official parent
digests against the manifest.

### D6 (MEDIUM) — enrichment was not atomic

Reproduced: a provider mismatch failing at the membership step left an **orphan
C2** — a content-addressed corpus committing to market evidence with no lane
provenance to reconstruct it from, which is worse than no corpus because the
commitment is unfalsifiable.

**Repair:** the three writes run inside a `SAVEPOINT` and roll back together.
`record_plan` gained the same treatment, since the architecture requires targets
and buckets to be "closed together" — which needs atomicity, not sequential
writes.

### D7 (LOW/MEDIUM) — argument handling

Duplicate `acquisition_ids` were silently de-duplicated (masking an upstream bug),
and an empty list produced a misleading `mixed projection policies []` error
rather than the intended guard. Both now raise explicit domain refusals.

---

## Retained blockers (NOT repaired — they belong to later tasks)

### B1 (HIGH) — probe registration is not content-bound; D9 is **not** closed

`stage_a_probe_registrations` stores a commit SHA and a path, but nothing
resolves the commit, loads the report, or proves the report names that response.
Reproduced: an arbitrary 2020 response, registered *after* inspection with an
all-zero SHA and a nonexistent path, certifies as `REUSED_PROBE_RESPONSE`.

The architecture required this exception to be machine-verifiable and impossible
to generalize. It is currently generalizable to any pre-plan response whose body
projects. **This must be closed before the real probe is registered** — pinned by
`test_retained_probe_registration_is_not_content_bound`, which is written to fail
loudly once the binding lands.

### B2 (HIGH) — the manifest commit SHA is never resolved

`manifest_commit_sha` is accepted as any non-empty text; a 40-character
fabrication is stored and never checked. Supplying `manifest_text` proves the
*content* digest, but nothing proves the text came from that commit, that the
commit exists, or that it is a commit object rather than a blob or tag.

Partially mitigated: `manifest_text` is now **mandatory** on the enrichment path,
so "skip the source-control binding" is no longer a supported mode of the only
call that creates downstream provenance. Full git-object resolution belongs to
the real-plan declaration task.

### B3 (HIGH) — a lane-backed crosswalk is **structurally impossible**

This was a primary review target and the finding is confirmed. f022 adds
`trg_xwk_lane_backed_audit_binding`, but adding a second BEFORE INSERT trigger
does not neutralise f019's `trg_xwk_audit_corpus_binding`, which independently
raises ABORT. Reproduced end to end with a genuine enriched corpus and a
lane-backed audit:

```
CROSSWALK REFUSED -> static crosswalk cites an identity audit taken over a
                     different source corpus
```

Because a lane-backed audit's digest is the **lane** digest while the corpus
carries the **official source** digest, f019 can never be satisfied. v22's new
trigger is therefore unreachable dead code today.

This blocks nothing yet — no linking provider is registered and no crosswalk
exists — but **v22 must not be described as supporting lane-backed crosswalks**.
The implementation document has been corrected. Repairing it requires changing
f019's composition without weakening the legacy official path, which is the
linking-provider task's job.

---

## Other findings (documented, not defects in v22's claimed contract)

- **Receipt ordering is second-granularity.** The f022 trigger compares
  `substr(...,1,19)`, so `received_at` may precede `requested_at` by up to 999ms
  within the same second. Harmless today; the documented claim is stronger than
  the check. Pinned.
- **Re-enrichment is not idempotent.** Calling enrichment twice with identical
  certified evidence raises a raw `UNIQUE constraint failed` on
  `corpus_evidence_lane_bindings` rather than returning the existing lane or
  giving a domain refusal.
- **Lane digest scope (§L).** `market_events_e0` covers only
  `historical_market_event_observations`, so two acquisitions differing only in
  whether a bucket **failed** or returned **empty** produce the same
  `lane_evidence_digest` and therefore the same C2 identity. The gate
  distinguishes them; the corpus identity does not. This is a defensible scope
  ("E0 = the typed market observation population") but it must be stated, because
  `market_evidence_digest` currently reads as though it covers the acquisition.
- **`acquisition_set_digest` uses surrogate ids (§M).** Two semantically identical
  reproductions yield different set digests. Acceptable because corpus identity
  depends on `lane_evidence_digest` (portable), not the set digest — verified.
- **§AF target→bucket algorithm.** Certification proves DB ⇄ manifest agreement,
  **not** that the manifest follows the reviewed T−60/flooring algorithm from
  preserved official hints. A consistent-but-wrong manifest passes. This belongs
  to the real-plan declaration task and must be resolved before the 160-bucket
  plan is declared.

## Four retargeted tests (§AH)

Reviewed before/after. All four preserve or strengthen the original invariant:

| Test | Original protection | After |
|---|---|---|
| audit-lane bypass | v21 accepted a forged linking audit with no lane | asserts refusal; still records that `namespace_verified` remains caller-asserted |
| backwards receipt clock | v21 accepted `received_at` < `requested_at` | asserts refusal **and** that an internally consistent fabrication is still admitted — the honest limit |
| `_policy_for` fail-open | pinned that Odds resolved to `mlb-cost-v1` | asserts explicit `odds-cost-v1` + refusal of unknowns |
| projection `observed_at` | proved the verifier does not own the clock | asserts the DB now owns it **and** the verifier's remit is unchanged |

No unrelated assertion was dropped to make v22 pass. The six other changed
fixtures were clock/label/body updates forced by the stronger gate.

## Malicious end-to-end chain (§AL)

forged plan → acquisition → attempt → response → partial observation →
certify → lane → C2 → lane-backed audit → crosswalk

**Before repair** the chain ran unimpeded to C2 and stopped only at the crosswalk
(by accident, via f019). **After repair** it fails at the first evidence step:
body projection. Independent guards, in order: projection completeness → content
hash → outcome derivation → budget/policy agreement → plan-before-network →
gate-on-enrichment → parent-corpus binding → lane digest recomputation → f019.

## Validation

ruff clean · mypy clean (358 files) · `git diff --check` clean · schema
**v22 / 22 migrations / 61 tables** · f018–f021 byte-identical · fresh init,
v18/v19/v20/v21 → v22 upgrades, FK and integrity checks all clean · protected
artefacts 85/85 identical · `REGISTERED_LINKING_PROVIDERS` empty ·
`ATTESTED_GENERATIONS` unchanged · **0 provider requests, 0 credits**.

Stage A not run · no real plan declared · no probe registered · no G5 · no
crosswalk · F1-R blocked · no E1 · P1 untouched.

## Next authorization boundary — derived from the trust chain

The prior order assumed probe registration could come first. The reviewed chain
says otherwise, because **B1 makes probe reuse a general bypass today**:

1. **Close B1** — bind probe report content, then register the real probe.
2. **Close B2** — resolve the manifest commit object, before any real plan.
3. **Resolve §AF** — independently recompute target→bucket from official hints.
4. **Declare the real Stage-A plan**, then acquire.
5. **Linking-provider registration + B3** (lane-backed crosswalk) — required only
   when a crosswalk is actually needed, i.e. after Stage-A certification.

Steps 1–3 are prerequisites for spending the first credit; step 5 is not.
