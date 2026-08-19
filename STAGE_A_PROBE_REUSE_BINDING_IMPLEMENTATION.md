# B1 — Machine-Verifiable Stage-A Probe-Reuse Binding

**Starting HEAD:** `8703239` (= `origin/main`, tree clean, schema v22 / 22 migrations / 61 tables).
**Schema after this task:** **v22 / 22 migrations / 61 tables — UNCHANGED. No migration.**
**Provider requests:** 0. **Credits:** 0. **Historical quota untouched: 19,998.**

## Verdicts

| Question | Verdict |
|---|---|
| **B1** | **CLOSED** |
| **Real `d3984d0` probe reuse** | **B — NOT SAFELY REUSABLE** |
| Schema change | **none required — v22 sufficient** |
| Caller-owned probe `registered_at` | **removed** |
| B2 / §AF / B3 | **still open** |

---

## 1. The threat, reproduced

The v22 review's chain ran end to end at `8703239`:

```
arbitrary 2020 raw response
+ all-zero commit SHA
+ nonexistent report path
-> stage_a_probe_registrations row
-> REUSED_PROBE_RESPONSE
-> Stage-A certification PASSED
```

Nothing resolved the commit, loaded the report, or tied the report to a response.
The registration row *was* the trust fact, which is exactly what it must not be.

## 2. Why the obvious design is wrong — and the measurement that proves it

The natural approach is to extract the facts the probe report already states and
require a candidate response to match them all. **That is insufficient**, and
this is the central finding of the task.

The committed `d3984d0` report precommits: requested bucket
`2026-03-01T17:00:00Z`, the exact endpoint, HTTP 200, snapshot `16:55:37Z`,
previous `16:50:37Z`, next `17:00:38Z`, 11 events, 11/11 lowercase 32-hex ids,
11/11 `basketball_nba`, a commence range, and 2,255 body bytes.

The real preserved response satisfies **8/8** of the machine-checkable ones. But
a synthetic body containing **zero real provider data** was constructed that also
satisfies **every one of them**, at exactly 2,255 bytes:

```
FORGED body bytes : 2255
FORGED sha256     : 1180fe7c4ca9dfc4f378d0712da69fad…
SATISFIES EVERY COMMITTED FACT : True
Uses any REAL provider event id: False
```

Every committed fact is reproducible **by construction**. Byte length is tunable
by padding one team name; timestamps and counts are free variables. The report
precommits a **specification**, not a preimage-resistant fingerprint. Binding on
it would admit fabricated evidence — the same failure class the review reported,
merely harder to notice.

## 3. The frozen policy: `stage-a-probe-v1`

A report is bindable only if it precommits at least one value a forger cannot
produce without the provider's actual answer:

- the exact **SHA-256 of the preserved response body**, and/or
- the exact **set of provider-assigned event ids** (opaque 32-hex values).

Plus, all required: provider (∈ `{the_odds_api}`), endpoint (∈ the single
historical-events path), requested bucket, HTTP status (must be 200).

Facts are read from an explicit machine-readable `PROBE-BINDING:` block, **not**
scraped from prose. A narrow frozen contract is deliberately preferred over a
general Markdown parser: a general parser must guess which of several candidate
values in prose is normative, and guessing is precisely how "HTTP 200 here,
HTTP 500 there" gets resolved conveniently. Two declarations of one field is a
**contradiction, refused** — never a choice.

Unknown policy version → refused. Changing any rule requires a new version
string; the version→semantics mapping is never mutated in place.

## 4. Git object resolution (probe reports only)

`resolve_commit` refuses: a non-hex/short/uppercase id (ambiguity is not
identity), a missing object, and **any object that is not a commit** — a blob or
tree id would otherwise "exist" and read as valid. An annotated tag is refused
rather than silently peeled, because peeling is a policy decision this policy
does not grant.

`load_committed_text` reads `<commit>:<path>` via `git show`. **The working tree
is never consulted**, so editing a report locally cannot change a historical
verification result. All git calls use `--no-optional-locks` and no network; if
the commit is absent locally, verification **fails closed**.

Verified against this repository's real history: `d3984d0…` resolves as a
`commit`; its report blob id is refused as a blob; its tree id is refused as a
tree.

The helper takes an injectable `repo_root` so tests point at a throwaway
repository rather than mutating module state. **It is deliberately not wired into
plan-manifest certification — B2 remains open.**

## 5. The unique-candidate rule

The caller does not nominate a response. The candidate set is every preserved
response satisfying the committed fingerprint, and it must contain exactly one:

- **0 → REFUSE** (the report names evidence this database does not hold)
- **>1 → REFUSE AS AMBIGUOUS** (choosing would be curator selection)

A registration naming a *different* response than its own report uniquely
identifies is refused: *"a registration may not nominate a different response
than its own report proves"*. This is what makes the exception ungeneralizable —
the question is not "is this *a* match" but "is this *the only* match".

## 6. Registration creates no eligibility

`register_probe_response` persists a **pointer**. Certification independently
re-resolves the commit, re-loads the committed report, re-parses the frozen
contract and re-derives the unique match, so a registration forged by direct SQL
stays unusable. The probe path also composes the already-accepted body/projection
verification through `certify_stage_a`, and it grants **no** identity semantics:
not audit acceptance, not event-id stability, not any canonical-game mapping, not
provider trust, not namespace registration.

Caller-owned `registered_at` is **removed**, matching the v22 treatment of
`register_acquisition`: a trusted API must not offer backdating as a feature.
This is not cryptographic chronology — it removes an avoidable fail-open.

## 7. Chronology semantics preserved

A reused probe legitimately **predates** its acquisition, so the
`requested_at >= registered_at` rule is deliberately **not** applied to it. The
two paths stay disjoint: a response failing probe binding does **not** fall back
to ordinary reuse; it is simply refused.

## 8. Real `d3984d0` verdict — **NOT SAFELY REUSABLE**

The report deliberately recorded *"Event population (structure only — no identity
inferred)"*. Confirmed by loading it from the commit:

| Precommitted? | |
|---|---|
| body SHA-256 | **no** |
| `raw_response_id` | **no** |
| any of the 11 provider event ids | **no** (0 / 11) |
| 2,255 body bytes, counts, timestamps | yes — but forgeable (§2) |

The preserved response still exists in an uncommitted scratch database
(`raw_01M0A1GA953DC83R…`, HTTP 200, 2,255 bytes, 11 events). Its hash could be
computed today — but a fingerprint computed *now* proves nothing about which
response was selected *then*, which is exactly the after-the-fact attestation the
authorization forbids. The report was never staged or committed alongside it.

**Consequence:** the March 1 `17:00:00Z` bucket must be acquired as an ordinary
Stage-A request, costing **one additional historical-events credit** (161 rather
than 160). That is the correct trade. Saving one credit is not worth accepting
evidence whose identity was never committed.

**The real probe was NOT registered in this task.** No row was inserted into
`stage_a_probe_registrations` in any project or protected database.

## 9. Schema adjudication — **v22 sufficient**

No migration. The existing registration already stores `raw_response_id`,
`probe_policy_version`, `probe_report_commit_sha` and `probe_report_path`, and a
verifier can resolve and bind those. A committed-report content digest column was
considered and rejected: it is fully derivable from the committed artefact, and
storing a caller-supplied copy would add a forgeable field rather than a proof.

## 10. Tests (33, all failing at `8703239`)

Git objects: all-zero SHA, malformed/short/uppercase ids, blob-as-commit,
tree-as-commit, missing path, commit-not-working-tree. Report contract: missing
block, conflicting duplicate fact, **no fingerprint**, non-200, foreign
provider/endpoint, malformed hash, duplicate event ids, unknown policy, frozen
version pin. Binding: zero matches, filtered request, wrong bucket, one-byte body
difference, fabricated body with different ids, **two identical candidates →
ambiguous**, unique match + caller cannot nominate another. Real probe: not
bindable, commits no fingerprint. API: no caller clock, no identity authority.

The previously pinned `test_retained_probe_registration_is_not_content_bound` is
**not deleted or weakened** — it is renamed to
`test_probe_registration_is_now_content_bound` and now asserts the refusal,
records what `8703239` did, and requires the failure to name
`"does not bind to committed evidence"`.

## 10a. A defect this task introduced, and how it was fixed

The first push (`65ba376`) **failed CI #125**. Six B1 tests resolved
`d3984d0` against this repository, and `actions/checkout@v4` defaults to a
**depth-1 clone**, so that object does not exist on the runner. The tests passed
locally only because a development clone has full history — an
environment-dependent test defect, introduced here.

It was reproduced deliberately rather than guessed at: a local
`git clone --depth 1` showed `commits in clone: 1` and
`d3984d0 … could not get object info`, then the same six failures.

Two changes, in that order of importance:

1. **The suite is now correct in any checkout.** Blob-as-commit,
   tree-as-commit and missing-path tests build their own throwaway git
   repository, so they exercise the MECHANISM unconditionally — and they are
   better tests for it, since they no longer depend on one specific historical
   commit. Only the four EVIDENCE tests about `d3984d0` itself are conditional,
   skipping with an explicit reason when the object is absent (a shallow clone or
   an exported tree genuinely cannot evaluate them). Verified in the shallow
   clone: **29 passed, 4 skipped, 0 failed.**
2. **CI now fetches full history** (`fetch-depth: 0`), so those four run rather
   than silently skipping. The verdict-B claim is load-bearing for the next
   task's credit budget, so it should be enforced by CI, not assumed.

The skip guard alone would have turned a red build green while quietly dropping
the claim; the CI change alone would have left the suite broken for anyone with a
shallow clone. Both were needed.

## 11. Non-regression

`REGISTERED_LINKING_PROVIDERS` empty · `ATTESTED_GENERATIONS` unchanged ·
`OFFICIAL_PROVIDER_BY_LEAGUE`, `QUALIFIED_PROVIDERS`, `AsOfReader`,
`_feature_cutoff` untouched · no canonical game created or mutated · no identity
audit · no crosswalk · Stage-A tables remain non-feature sources.

## 12. Status

| | |
|---|---|
| **B1** | **CLOSED** |
| **B2** (plan manifest commit resolution) | **OPEN** |
| **§AF** (target→bucket recomputation) | **OPEN** |
| **B3** (lane-backed crosswalk / f019 composition) | **OPEN** |
| Stage A run | NO |
| Real Stage-A plan declared | NO |
| Real probe registered | **NO** |
| Linking provider / G5 / crosswalk / F1-R / E1 / P1 | NO |

**B1 is ready for independent adversarial review.** Because the real probe is
non-reusable, the "register the real probe" step is **removed** from the plan
rather than merely deferred.
