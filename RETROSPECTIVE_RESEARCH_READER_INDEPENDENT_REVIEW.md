# `RetrospectiveResearchReader` — independent review

Reviews the reader implemented at **`0496987`** ("Implement
RetrospectiveResearchReader") against the reviewed Lane-R architecture. Every
claim in `RETROSPECTIVE_RESEARCH_READER_IMPLEMENTATION.md` was treated as a
hypothesis and re-derived; the review harness
(`sports_quant/db/tests/test_retrospective_reader_review.py`) is independent of
the implementer's fixtures.

> **VERDICT: ACCEPTED WITH REPAIRS — with a RETAINED DATA BLOCKER for F1-R.**
>
> Two defects were reproduced on `0496987` and repaired; one is **high
> severity** and was an identity leak at admission time. The reader's structure
> is otherwise sound: 17 separate falsification attempts against the availability
> gate, rule binding, label isolation, lane separation and identity resolution
> all failed to break it.
>
> **Schema stays v19** — 19 migrations, no migration added or edited,
> `f018`/`f019` untouched (last modified at `2824c3a`).
>
> **Strict-forward PIT is unweakened.** `_feature_cutoff` byte-identical.

Where this document differs from the implementation report, **this document is
authoritative**.

---

## 1. Contract reconstructed independently

The reviewed Lane-R reader contract is `HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md`
§12: two readers sharing one leakage contract, differing only in admissible
evidence, with lane selection as a **type not a flag**, FORWARD_ONLY unreturnable,
and `provider_*_references` forbidden in both lanes.

### A vocabulary discrepancy, adjudicated

The architecture's per-family tables label market/weather/odds evidence
**`VERSIONED_HISTORICAL`** (9 occurrences); the code enum is
**`VERSIONED_SNAPSHOT`**. This is **not** a defect:

* §11 of the same document specifies the *schema* vocabulary as
  `static_identity | event_derived | versioned_snapshot`;
* `f018`'s CHECK constraint enforces `versioned_snapshot`, and that migration was
  independently reviewed and shipped at `2824c3a`.

`VERSIONED_HISTORICAL` is the prose evidence-type label; `versioned_snapshot` is
the reviewed persisted vocabulary. The implementation follows the binding one.
Recorded here so the divergence is not rediscovered as a defect later.

## 2. Defects reproduced and repaired

Both were reproduced on the unmodified `0496987` before any change.

### R2 — a tampered crosswalk target was ADMITTED *(HIGH — identity leak)*

The `STATIC_IDENTITY` path verified only that the cited crosswalk **existed** and
**belonged to this corpus**. It never re-derived the crosswalk's own
`semantic_digest`. So a single direct-SQL statement:

```sql
UPDATE static_crosswalk_provenance SET canonical_entity_id = 'tm_mlb_hou' ...
```

produced an **admitted** static identity pointing at the wrong franchise — and
`static_identity()` returned that wrong canonical id straight to the caller,
where it would flow into any downstream join.

Reproduced on **real evidence in both leagues** (`crosswalk_target_tampered:
!! ADMITTED !!` for MLB and NBA).

The out-of-band `team-attestation-verify` verifier does catch this — but a reader
whose entire purpose is admission cannot depend on someone remembering to run a
CLI afterwards. The architecture makes the reader the gate; the gate was open.

**Repair.** `_crosswalk_integrity_error()` re-derives the crosswalk's semantic
digest from its own stored contents (corpus, namespace, provider id, canonical
target, audit digest, policy version) and compares. Both payload forms are tried
— plain, and bound to the currently committed TEAM-A map — because the map digest
is optional for player crosswalks. A row matching neither has been altered since
it was written, or was built under a different map; both fail closed.

Applied in **both** paths: `static_identity()` raises, and the admission branch
returns the new `AdmissionOutcome.CROSSWALK_DIGEST_MISMATCH`.

After repair, on real evidence, both leagues: `rejected: crosswalk_digest_mismatch`.

### R1 — the admission API silently ignored `entity_type` *(MODERATE — misuse hazard)*

`reconstructed_input_provenance` has **no `entity_type` column**, and correctly
so: a certification is about a *feature family for a target game*. But
`admit_feature()` / `admit_label()` accepted a full `ProviderNamespace` — which
carries `entity_type` — and silently dropped that component. A caller passing a
TEAM or PLAYER namespace received game-scoped certifications with no complaint.

Not a leak: nothing uncertified was admitted. But an argument the type checker
reads and the runtime ignores is an invitation to misuse, and it violates the
"narrow, explicit, typed, difficult to misuse" requirement.

**Repair.** Admission now requires `entity_type is GAME` and raises otherwise.
`static_identity()` deliberately still accepts TEAM/PLAYER namespaces — there the
entity type **is** used, because the crosswalk lookup filters on it.

## 3. Falsification attempts that FAILED to break the reader

These are the load-bearing confirmations. Each was an active attempt to make the
reader accept something wrong.

| Attack | Result |
|---|---|
| Certification for another corpus / namespace / target game / family / generation | `no_certification` |
| Certification whose league disagrees with its corpus | refused by DB at write |
| Corpus flipped to `strict_forward_pit` by direct SQL | `LaneRAdmissionError` |
| Superseded corpus | refused at construction |
| `EXCLUDED` certification (direct SQL) | `certified_excluded` |
| Crosswalk corpus id tampered | `crosswalk_from_another_corpus` |
| Dangling crosswalk pointer | `missing_crosswalk` |
| Rule digest tampered | `RetrospectiveProvenanceError: has changed` |
| Unknown rule id | `UnknownAvailabilityRuleError` |
| Rule id swapped, stale digest kept | fails closed |
| Malformed persisted snapshot instant | `ValueError` — no lenient compare |
| Malformed cutoff (7 forms: no µs, offset, naive, space-separated, garbage, empty, out-of-range) | refused at construction |
| `VERSIONED_SNAPSHOT` boundary at −1µs / exact / +1µs | exact, inclusive |
| Label certification relabelled onto a feature family | `wrong_lane` |
| 12 hostile family names (case, whitespace, tab, newline, zero-width space, NBSP, Roman-numeral lookalike, empty) | refused |
| FORWARD_ONLY via the batch API | raises |
| Live `provider_team_references` link as identity | `no static crosswalk` |

**Blocking findings — verified as a non-defect.** The reader never consults
`identity_audit_findings`, which looked like a gap. It is not: `f019`'s
`trg_idf_accepted_audit_no_contradiction` makes a blocking/collision/non-`none`
exclusion-scope finding **impossible** on an ACCEPTED audit at the *database*
level. Proved at both layers, including with the repository bypassed.

**The fail-open enum class.** Scanned every `.field is <Enum>` site in production
code (13 found) and classified each against the model's declared type. All 13 are
on **in-memory** objects holding real enums (`ProviderNamespace`, `AuditPlan`,
`FeatureFamily`, `TableEntry`) — safe. Every persisted-string field the reader
touches goes through `_parse`, which fails closed. The three bugs the implementer
found were the whole population, not a sample.

## 4. Lane separation — realistic bypasses, not greps

* No callable on `AsOfReader` takes a lane-shaped parameter (checked by
  signature across all public members, not by source string).
* The two readers share **zero** public callable names, so one cannot be
  duck-typed for the other.
* Monkeypatching the Lane-R module does not alter `pit.asof`; no shared mutable
  state.
* Importing Lane R does **not** mutate `TABLE_REGISTRY`; all five Lane-R tables
  remain `UNSUPPORTED` joins.
* `_feature_cutoff` byte-identical (`5d55345b…`), pinned in two files.

## 5. §I — the same-database evidence constraint

The implementation flagged that `f018`'s evidence check runs
`SELECT 1 FROM <table> WHERE <id> = ?` on the **provenance connection**, so cited
evidence must live in the same database. I built the smallest offline proof to
decide whether this is an acceptable boundary or a design contradiction.

**Finding: it is an intentional, acceptable boundary — NOT a blocker.** A
reconstruction corpus is meant to be a self-contained artifact. Materializing an
evidence row into the reconstruction DB and citing it works today, and
`observed_at` is carried **verbatim** — no backdating, and the append-only
triggers prevent any. Proved by copying a real MLB result row into a fresh v19
database and confirming byte-preservation of `observed_at`.

**But a separate, real blocker was found underneath it.**

## 6. RETAINED BLOCKER — no event-completion instant exists

`prior_results` is refused on both corpora. The implementation attributed this to
`observed_at` being collection time. That is correct, and I confirmed it is
**worse than reported**:

| | MLB June 2026 | NBA March 2026 |
|---|---|---|
| Completion-like columns in the results table | **NONE** | **NONE** |
| `game_status_history` rows | **0** | **0** |
| `observed_at` range | 2026-07-31 → 08-03 | 2026-08-04 |
| Game dates described | 2026-06-01 → 09-04 | 2026-03-01 → 03-31 |

There is **no** field anywhere in either bounded corpus from which a genuine
`source_event_completed_at` could be derived. `game_status_history` — the one
table that could bound completion via a transition to `final`, and which is
already registered `asof_filtered` — is **empty in both**.

So the reader's refusal is correct and follows the architecture. But F1-R cannot
manufacture EVENT_DERIVED availability from this evidence at all.

**Safe documented paths for F1-R** (none require backdating `observed_at`, and
the schema forbids it):

1. **Populate `game_status_history`** from preserved `raw_responses`, if the
   stored payloads carry a status-transition timestamp — needs verification, not
   assumption.
2. **Forward collection** recording a completion instant as it happens.
3. **A new bounded re-collection** capturing a provider completion field.

Until one exists, **EVENT_DERIVED is data-blocked**. This is a *data* blocker,
not a schema or reader blocker.

## 7. Real-evidence results (read-only, 0 provider requests)

Corpora built from the protected evidence through the reviewed TEAM-A path; all
tampering performed on **copies**.

| | MLB | NBA |
|---|---|---|
| Team crosswalks / canonical games | 30 / 400 | 30 / 239 |
| `static_identity` admitted | yes | yes |
| Identity chain | `108 → tm_mlb_laa` | `1 → tm_nba_atl` |
| Backing audit | `accepted`, `team`, `lg_mlb`, digest matches corpus | `accepted`, `team`, `lg_nba`, digest matches corpus |
| FORWARD_ONLY families | 4/4 refused | 4/4 refused |
| All five tamper attacks | refused | refused |

## 8. Zero-network and protected evidence

23 guards armed **before** any provider-facing import; **15/15** adversarial
probes blocked (DNS ×2, `create_connection`, raw socket, urllib ×2, httpx ×3,
requests ×2, both provider constructors, `load_settings`,
`build_readonly_client`). `zeronet.TRIPPED == []` throughout.

Protected artefacts: **42/42 byte-identical**, inodes and WAL sidecars unchanged.
The NBA `-shm` sidecar mtime moves during a full-suite run; I re-isolated the
cause rather than restating it — the pre-existing `ingest/scratch_db._ro_connect`
(`mode=ro`) path used by the recovery-manifest test moves it, and **no reader
code or review test does**. Database bytes identical in every case. This is a
SQLite read-only side effect, not evidence mutation.

## 9. Schema

**v19 unchanged.** 19 migrations; `f018`/`f019` last modified at `2824c3a` and
untouched by this review. Fresh v19 init idempotent; v17→v19 and v18→v19 both
reach 52 tables. No implementation depends on a test-only schema state. **No
migration was required by either repair** — both are code-level.

## 10. Test-quality assessment (§K)

The implementer's 41+3 tests are mostly sound and do test refusal, not only
acceptance. Two structural weaknesses found:

1. **The static-identity tests never tampered with the crosswalk row.** They
   proved a *missing* and a *foreign* crosswalk fail closed, but not a *corrupted*
   one — which is exactly where R2 lived.
2. **The entity-type component of the namespace was never varied**, so R1 was
   invisible.

Both are now covered. The review adds **55 tests**, including the two defect
regressions and a structural guard on the digest recomputation.

## 11. Limitations that remain

1. **EVENT_DERIVED is data-blocked** (§6) — retained blocker for F1-R.
2. **`source_event_completed_at` is not bound to the cited evidence row.** The
   reader cannot cross-check it: the evidence tables carry `observed_at`
   (collection time), not a completion instant. Documented, with the reader
   reporting both values so the unverifiable link is visible rather than hidden.
   This becomes checkable only once §6 is resolved.
3. **Map-membership remains a detective control.** R2 closes digest
   *self-consistency* in-band; verifying a crosswalk against the committed TEAM-A
   map still requires the out-of-band verifier.
4. **`kalshi_market` depth (G2) and `weather_forecast` archive depth (G3)** are
   unclosed gates; both families are classified on availability grounds only.

## 12. Verdict and next authorization boundary

**ACCEPTED WITH REPAIRS.** The reader is a fail-closed, corpus-scoped, leakage-safe
admission decision procedure, and strict-forward PIT is unweakened.

**F1-R may NOT yet be authorized to produce EVENT_DERIVED evidence.** The blocker
is data, not design: no event-completion instant exists in the bounded corpora
(§6). The next authorized step should be a **narrowly scoped investigation** of
whether preserved `raw_responses` contain a usable completion timestamp — which
is a read-only question answerable offline, and which would determine whether
F1-R can proceed on existing evidence or requires new collection.

F1-R execution, historical odds and market anchoring, F2, production matching,
feature engineering, model training, calibration, backtesting, recommendation
output and UI all remain **UNAUTHORIZED**. Gates G1, G2, G3, G4, G6 unchanged.
