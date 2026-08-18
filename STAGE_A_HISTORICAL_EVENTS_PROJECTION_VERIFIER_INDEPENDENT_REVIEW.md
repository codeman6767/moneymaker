# Independent Adversarial Review — Stage-A Projection / Body Verifier

**Object under test:** the verifier as shipped at `2805665`. Treated as a set of
hypotheses, not as authority.

**Starting HEAD:** `2805665` (`origin/main` = `2805665`, tree clean, schema
v21 / 21 migrations / 53 tables). **Provider requests:** 0. **Credits spent:** 0.

---

## Verdict

> ## ACCEPTED WITH REPAIRS
>
> The design is right and the two-way completeness idea is the correct answer to
> L1. But **four defects were reproduced against `2805665`**, one of them
> severe enough to defeat the module's central guarantee entirely, and all four
> are repaired here.
>
> The severe one is worth stating plainly: **completeness was evadable from
> outside the body.** A filtered request produced a perfectly self-consistent
> response whose complete projection passed two-way verification while the real
> snapshot population had been reduced by the *request*.

| # | Defect | Severity | Status |
|---|---|---|---|
| D1 | Absent / null `data` admitted as evidence of zero events | **High** | repaired |
| D2 | Population-reducing request parameters admitted | **Severe** | repaired |
| D3 | Duplicate JSON keys silently resolved last-value-wins | **High** | repaired |
| D4 | Corpus gate accepted caller-selected ids; orphaned rows unexamined | Medium | repaired |
| C1 | "byte-for-byte team label" overstates the typed guarantee | doc | corrected |

Upheld without change: exact endpoint admission · snapshot-level fail-closed ·
duplicate-event policy · missing-key-vs-null `commence_time` · timestamp
normalization and its precision ceiling · strict-PIT and authority isolation.

---

## 1. D2 — filtered requests defeated completeness  **[Severe]**

### Reproduction at `2805665`

Every one of these projected **successfully**, yielding a complete, internally
consistent one-event projection:

```
eventIds=<one id> · eventIds=[] · eventIds='' · commenceTimeFrom · commenceTimeTo
regions · markets · bookmakers · any unknown parameter
```

### Why it is severe

The module's whole claim is that a caller cannot materialize a convenient subset
of a snapshot. Two-way completeness enforces *body ↔ rows*. But a request of

```
GET /v4/historical/sports/basketball_nba/events?date=…&eventIds=<one event>
```

returns a body containing exactly that event. Its complete projection is that
event. Every row matches. **The verifier passes** — while the real snapshot
population was reduced before the body was ever written.

The guarantee was evadable one level up from where it was being enforced, which
is exactly the class of hole the two-way check was introduced to close.

### Repair

An explicit allow-list, enforced where the request is still visible:

```python
STAGE_A_ALLOWED_REQUEST_PARAMS = {"apiKey", "date", "dateFormat"}
```

`date` and `dateFormat` define the snapshot; `apiKey` is the redacted
placeholder and selects nothing. Everything else is refused as
`FILTERED_REQUEST` — the documented narrowing parameters because they demonstrably
reduce the population, and unknown parameters because they *cannot be proven not
to*. Fail-closed on unknown is the right default here precisely because the
provider may add a narrowing parameter later.

Tested end-to-end: a filtered request whose rows match perfectly is now rejected
by the gate.

## 2. D1 — absent / null `data` became "no market existed"  **[High]**

### Reproduction at `2805665`

```python
data = body.get("data")
if data is None:
    data = []
```

- `data` member **absent** → ACCEPTED, 0 observations
- `data: null` → ACCEPTED, 0 observations

### Why it matters

A 200 with `data: []` is the provider stating **no events existed at that
instant** — real evidence, and downstream it means a target has no anchor. A
missing or null member is the provider not returning the shape the projector
understands. Collapsing the second into the first lets **malformed output become
a market fact**, which is the same error class as reading a failed request as
evidence of absence.

The implementation's own documentation said *"`data` exists and is a list"*; the
code did not enforce it.

### Repair

`data` must be **present and a list**. Absent and explicit-null are both refused
as `DATA_MISSING`, with the distinction stated in the message. Only `[]` is
evidence of zero events.

## 3. D3 — duplicate JSON keys silently resolved  **[High]**

### Reproduction at `2805665`

Python's parser accepts duplicate object keys and keeps the last. Admitted:

| Tampered document | Result at `2805665` |
|---|---|
| `dateFormat` twice (`unix`, then `iso`) | **ACCEPTED** |
| wrapper `timestamp` twice | **ACCEPTED**, later value used |
| event `id` twice | **ACCEPTED**, later value used |
| event `home_team` twice | **ACCEPTED**, later value used |
| `data` twice (`[]`, then events) | **ACCEPTED** |

The duplicate-`date` case *appeared* to be refused, but **only by accident**: the
parser silently took the later date and the snapshot-ordering check happened to
fire. With a different pair of dates it would have passed. A guard that works by
coincidence is not a guard, and the review test now pins the correct rejection
code rather than the lucky one.

### Why it matters

Audit-grade proof claims an exact projection from preserved bytes. A document
with two readings, silently resolved one way, cannot support that claim — and a
tampered persisted row is exactly where this arises, since genuine provider
output never contains duplicates.

### Repair

`_load_json_strict` parses both `request_params_json` and `body` with an
`object_pairs_hook` that **rejects any duplicate key**, recursively. One document,
one reading, or refusal.

## 4. D4 — the corpus gate was subvertible and had a blind spot  **[Medium]**

Two related problems.

**Caller-selected ids.** `verify_historical_market_event_evidence(conn,
raw_response_ids=[…])` let a caller hand in only the convenient responses and
treat the passing reports as "the corpus is verified". The name promised a
corpus-level gate; the signature delivered a subset helper.

**Orphaned observations.** An observation citing a response the scan never
covers — a current-odds response, a non-200, a deleted row — was examined by
**no report at all**. Reproduced: a row citing a `/v4/sports/.../odds` response
sat in the database entirely unchecked.

### Repair

The API is split so the distinction cannot be lost:

- `verify_historical_market_event_evidence(conn)` — **no caller-selected ids**.
  Database-wide, and now returns a typed `EvidenceGateResult` that is `verified`
  only when every report passes **and** no orphaned observation exists.
- `verify_selected_responses_subset(conn, ids)` — explicitly named a subset, and
  documented as *not* corpus proof.

## 5. C1 — "byte-for-byte team label" overstates the typed guarantee

`"Boston Celtics"` and `"Boston Celtics"` are byte-distinct JSON that decode
to the identical Python string, and therefore project to the identical
observation and the identical id. Verified.

That behaviour is **correct** — the provider said the same thing both times — but
it means the typed layer preserves the **decoded Unicode string**, not the JSON
token bytes. The byte-level claim belongs to `raw_responses.body`, which does
retain the original bytes.

The implementation document has been corrected. NFC vs NFD remain genuinely
distinct strings and stay distinct, which is the property that actually matters.

## 6. Findings upheld

**Exact endpoint admission** — trailing slash, query string, case variants,
percent-encoding, extra segments, missing leading slash, historical *odds*,
current odds and other sports are all refused. Byte-exact membership is the right
contract, and a future client that persisted an equivalent-but-different path
should require a **new projection policy version**, not a silent widening of the
accepted set.

**Snapshot-level fail-closed** — upheld. One malformed event rejects the whole
typed snapshot. There is no reviewed lossless representation for a partially
materializable snapshot, so whole-snapshot refusal is the correct fail-closed
choice, and the raw response is preserved regardless.

*The tradeoff is real and must be reported, not hidden:* an anomalous event
unrelated to any target can remove a snapshot that also covered valid target
candidates. That belongs in **acquisition-completeness reporting** as its own
exclusion category — recorded here as a requirement for the acquisition task.

**Duplicate event ids** — upheld, including byte-identical duplicates. The v21
table cannot represent multiplicity (identical content yields one identity), so
the honest options are refuse-and-explain or silently lose the anomaly. Refusal
plus the preserved raw response and an explicit reason is sufficient evidence,
and inventing a second storage model for duplicate counts is not warranted.

**Missing-key vs explicit-null `commence_time`** — upheld. One live probe is not
sufficient evidence that the key is always present, so relaxing missing-key to
NULL would be inferring a provider guarantee from a single observation. Fail
closed, exactly as §G directs.

**Timestamp normalization** — semantic, not mutation: same instant, and v21
mandates one canonical spelling so TEXT ordering is correct. The >6-fractional-digit
refusal is right; widening to nanoseconds must be a **new policy version**, never
silent truncation. Leap seconds, padding, offsets and hour 24 all refused.

## 7. Policy-version binding (§D) — a recorded gap, not repaired here

`PROJECTION_POLICY_VERSION` is returned in an ephemeral `VerificationReport` and
`EvidenceGateResult`. It is **not persisted** on observations, on any
verification record, or in corpus provenance.

Adjudication: for this layer that is **acceptable**. The verifier is a *detective*
control, and re-verification under current policy is the intended behaviour — a
row that no longer projects under today's rules should fail today.

It becomes load-bearing at **Stage-B certification**, where an accepted audit
must record which projection policy certified its inputs; otherwise an old
corpus can be re-verified under changed semantics with no persisted proof of
which rules applied. Handed forward in §9 rather than broadening this task.

## 8. Boundaries located, not closed

**`observed_at` (§P).** Not derivable from the body, deliberately outside the
semantic hash (v20 review), and therefore **not constrained by this verifier** —
an arbitrary future `observed_at` still passes. That is not a defect of this
layer, but the clock invariant is owned by *no layer today*. The acquisition
layer must own it. Recorded with a test that states the boundary explicitly.

**Raw-response field coverage (§N).** The projection reads provider, endpoint,
status, request params and body. `http_method` and host are not read; the table
stores no host, and the endpoint plus provider identifier together imply it under
the repository's read-only client contract. Adding host/method to the proof would
require evidence the table does not carry, so the boundary is documented rather
than invented.

## 9. Requirements handed to the corpus-digest structural adjudication (§V)

This review deliberately does not solve that task. It must provide:

1. **What persisted object defines the complete Stage-A raw-response set.** The
   database-wide scan is *too broad* — it demands materialization of probe-only
   and scratch responses — while caller-selected ids are *too narrow*. A declared
   acquisition manifest is needed so the gate proves completeness over **exactly**
   the responses belonging to that acquisition.
2. **How probe-only responses are excluded** unless explicitly re-materialized
   under the declared plan.
3. **Where the projection policy version is persisted** so a certified corpus
   records which rules certified it (§7).
4. **Which layer owns `observed_at`** (§8).
5. **How acquisition completeness connects to projection completeness**, including
   the snapshot-rejection exclusion category from §6.

## 10. Real-evidence replay

The preserved probe payload was still available and was used **on a copy**; no
provider request was made and no protected corpus was touched.

| Check | Result |
|---|---|
| Real request params | `{apiKey: ***REDACTED***, date, dateFormat}` — passes the new allow-list |
| Projection under the **repaired** projector | **11 observations**, unchanged |
| Gate after materializing all 11 | **verified**, 0 orphans |

**The repairs do not break genuine provider evidence.** That mattered: an
allow-list that rejected the real request would have been a worse defect than the
one it fixed.

## 11. Test-quality audit (§S)

The original 136 tests were sound in substance but had a **structural blind
spot**: the body fixture built a dict and called `json.dumps`, which *cannot
express* an absent member, an explicit null in place of a member, or a duplicate
key. That is precisely why D1 and D3 survived 136 adversarial tests.

The review's 40 tests build raw JSON **text**, so those states are expressible.
Other gaps closed: no filtered-request case existed; no multi-response database;
no subset-verifier misuse case; no orphaned-observation case.

## 12. Validation

| Check | Result |
|---|---|
| Review suite | **40 passed** |
| Original suite (after repairs) | **136 passed, 1 skipped** |
| `git diff --check` | clean |
| `ruff check .` | all checks passed |
| `mypy .` | success, 351 source files |
| **Full `pytest`** | **3381 passed, 4 skipped** |
| Schema | **v21 / 21 migrations / 53 tables — unchanged** |
| Zero-network guards | 31 armed, 11/11 probes blocked |
| **Provider requests / credits** | **0 / 0** |
| Protected artefacts + 21 migrations | **85 / 85 unchanged** |

## 13. Strict-PIT and authority — no regression

`AsOfReader` and `_feature_cutoff` untouched; the observation table stays
`_unsupported`; the projector is not a feature reader; `REGISTERED_LINKING_PROVIDERS`
remains empty; `ATTESTED_GENERATIONS` and `PROVIDER_LEAGUES` unchanged; no
canonical game created or modified; no crosswalk; no runtime name matching.

## 14. Exact next authorization boundary

The verifier has **no retained blocker** after these repairs.

> ### The next safe task is the corpus-digest structural adjudication.
> It must deliver the five requirements in §9 — above all, the persisted object
> that defines the complete Stage-A raw-response set, without which the gate is
> either too broad or too narrow.

Then: register the linking namespace with disjointness tests → Stage-A first-pass
acquisition (160 requests / ~160 credits, own cap, gated by this verifier) → G5
audit → curation with the mandatory S8 counterfactual → independent review →
target anchors → E1.

Independent of all of it: **P1**, the `REPLACE` hardening for the remaining 28
append-only tables.

**F1-R remains blocked.**
