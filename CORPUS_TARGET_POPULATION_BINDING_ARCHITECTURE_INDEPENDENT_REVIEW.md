# Corpus Target-Population Binding — Independent Adversarial Architecture Review

**Starting HEAD:** `c6cbdd1` (= `origin/main`, tree clean, schema v22 / 22 migrations / 61 tables).
**Schema after this task:** **v22 / 22 / 61 — UNCHANGED. No migration. No v23 code.**
**Provider requests:** 0. **Credits:** 0. Zero-network guards: 31 patch points armed, 11/11 probes blocked.

## VERDICT: **ACCEPTED WITH REPAIRS — READY AFTER DOC RECONCILIATION**

The scientific core is right and survives attack. Four load-bearing mechanisms do
not: the closure rule is **not implementable**, run provenance sits **outside the
content address**, membership is **not projectable** from raw evidence the way the
document claims, and the bound run set is **caller-selected and unfalsifiable**.
All four have concrete repairs, three of them proved executable here.

| # | Question | Verdict |
|---|---|---|
| 1 | Target-population scientific definition | **ACCEPTED** |
| 2 | Official listing raw responses as authoritative source | **ACCEPTED WITH ADDITIONAL COMPLETENESS PROOF** |
| 3 | Bound run-set completeness | **ACQUISITION/MANIFEST BINDING REQUIRED** |
| 4 | Raw listing → canonical target derivation | **NEW VERIFIER/POLICY REQUIRED** |
| 5 | Representation | **REPAIRED V23 SHAPE REQUIRED** |
| 6 | `target_set_digest` membership-only semantics | **REPAIR REQUIRED** |
| 7 | Content-addressed corpus / run provenance | **REPAIR REQUIRED** |
| 8 | Construction/closure mechanism | **REPAIRED CONSTRUCTION STATE/ORDER REQUIRED** |
| 9 | Legacy corpus behaviour | **REPAIR REQUIRED** |
| 10 | Target-bound corpus relationship | **SIBLING / DISTINCT POLICY** |
| 11 | Schema | **V23 REQUIRED WITH REPAIRED SHAPE** |
| 12 | Historical NBA March source dependency | **EXISTING ARTIFACT NEEDS ADDITIONAL COMPONENTS** |
| 13 | §AF readiness after the *repaired* implementation | **YES, SUBJECT ONLY TO DATA** |
| 14 | Overall | **ACCEPTED WITH REPAIRS — READY AFTER DOC RECONCILIATION** |

---

## 1. The original blocker, re-reproduced (not quoted)

Re-derived on fresh v22 databases built by the real migration runner:

| | Result |
|---|---|
| **A** `target_set_digest` accepts arbitrary caller text | stored `'identity-audit-no-targets'` |
| **B** relation enumerating corpus → canonical games | **NONE** (no table has both `corpus_version_id` and `REFERENCES games`) |
| **C** same corpus identity in two DBs with different games populations | identical `semantic_digest` `c558c772f2bf1b1d…` |
| **D** scope query over the same corpus identity | **1 vs 6** members |
| **E** `stage_a_plan_targets` | `plan_id`-keyed, no `corpus_version_id` |
| **F** `reconstructed_input_provenance` | keyed `provider_game_id` + `feature_family` |

**A target-bound architecture is genuinely required.** The blocker is real and
the document's diagnosis of it is correct.

## 2. First primary attack — nothing proves the run set is complete

**The attack succeeds.** Enumerating every table in v22 whose name suggests an
acquisition or manifest returns only `stage_a_acquisitions` and
`corpus_evidence_lane_acquisitions` — both introduced by v22 **for Stage A**.
There is **no persisted object for the official BALLDONTLIE acquisition** from
which "the complete required run set" is derivable. The pilot manifest and the
resume checkpoint are **files on disk**, not rows.

So binding `run_id`s proves only *"these targets came from these runs."* A caller
who binds R1+R2 and omits R3 produces membership that is internally perfect: the
digest is correct for the smaller set and re-derivation from the bound runs
matches exactly. **Denominator shrinkage simply moves one layer earlier**, which
is the precise failure the whole target-binding exercise exists to prevent.

`run_id` alone is therefore the wrong binding level. **Verdict 3: the corpus must
bind a precommitted acquisition identity** (`manifest_hash` + `plan_version`),
from which the required run set is derived, with the bound `run_id`s recorded as
the *resolution* of that manifest rather than as the primary claim. Selection
after seeing results must be structurally impossible.

## 3. Contingent discovery, and what the acquisition machinery actually guarantees

Reading `f1a.py`, `checkpoint.py` and `nba_ingestor.py` rather than the summary:

- The NBA skeleton family is `games`; the skeleton unit issues the listing calls
  and **freezes the selected set** into `Checkpoint.stage_game_ids`, described in
  the source as *"the frozen canonical selected game set … resume uses this exact
  set rather than re-deriving from a possibly-changed schedule."*
- The checkpoint binds `manifest_hash`, `plan_version`, `provider`, `league`,
  `date_range`, `families` and `scratch_fingerprint`; `verify_resume` refuses a
  resume on any mismatch.
- One contingent produces **one skeleton unit but many raw responses** (one per
  page), then one unit per selected game. A run is not a contingent.

**This corrects the architecture document.** Its finding G concludes that "no
precommitted artefact enumerates the original 239." That is true of the
*manifest* and remains the right reason to reject retroactive attestation — but
the document **overlooked the checkpoint**, which does freeze an enumerated game
set bound to a manifest hash and a database fingerprint. The checkpoint is
written *during* acquisition, so it is not precommitted evidence of the intended
population; it is, however, preserved evidence of the **selected** set, and it
belongs on the artefact checklist (§12 below).

**A caution that matters more than it looks.** `_select_nba_games` applies
`max_games`, and the listing loop can stop at `max_pages`/`max_records`, reporting
`games_truncated` and a `truncations` string. Those are **runtime report fields,
not persisted rows**. So `stage_game_ids` is the *selected* set, which is not
necessarily the *complete official* set. It may seed a target population; it may
never certify one.

## 4. Run binding does identify the right raw responses

This one the document gets right, and better than it claims. Verified against the
real DDL:

- `raw_responses.run_id` → `ingestion_runs(run_id)` is a **direct NOT NULL FK**.
- `raw_responses` stores `endpoint` **separately** from `request_params_json`, and
  a CHECK forbids a query string in `endpoint`. Exact endpoint equality
  (`/v1/games`) is therefore available — no substring matching needed.
- `game_schedule_snapshots` binds **both** `run_id` and `raw_response_id`, so the
  listing→observation projection already exists as preserved rows.

**Admission contract:** `provider = 'balldontlie'` AND `endpoint = '/v1/games'`
(exact) AND `http_status = 200` AND `run_id ∈ bound run set`. Stats, box scores,
plays, lineups and single-game fetches are excluded by exact endpoint equality.
One caveat: **`game_schedule_snapshots.run_id` is NULLABLE**, so a projection row
may be unattributed to a run; the verifier must derive from `raw_responses` and
use snapshots only as a cross-check, never as the primary set.

## 5. Pagination completeness IS derivable — with one honest residual

BALLDONTLIE paginates on an integer `meta.next_cursor`, and `cursor` is a
non-sensitive parameter that survives sanitization into `request_params_json`. So
the cursor chain is reconstructible from preserved evidence alone. Demonstrated:

| Scenario | Result |
|---|---|
| complete 3-page chain | verifies clean |
| truncated tail (`max_pages`) | **detected** — "chain breaks: page for cursor 50 is MISSING" |
| missing middle page | **detected** — break *and* unreachable orphan tail |
| duplicate page for one cursor | **detected** |
| non-listing endpoint present | correctly excluded |
| final page whose body lost `meta` | **NOT detectable** |

**Retained limitation, stated plainly:** a response body with no `meta` is
indistinguishable from a genuine last page. This is not a provenance forgery risk
(`body_hash`/`content_hash` cover mutation) — it is a provider-side truncation
risk, and the acquisition itself would have stopped at the same point. The
mitigation is that the acquisition manifest must commit an **expected coverage
assertion** (the complete date-window query protocol) so a short chain is
falsifiable against something other than itself.

**Owner:** an acquisition-completeness verifier, upstream of membership
derivation. Membership derivation must refuse to run on an unverified chain.

## 6. Second primary attack — run provenance is outside the content address

**The attack succeeds, and more sharply than the review anticipated.**

`record_corpus_version` computes `semantic_digest` over a fixed payload that
includes `target_set_digest` but has no place for run provenance. Two derivations
from **different run sets** that reach the **same member set** produce the same
`target_set_digest`, hence the same `semantic_digest`. Demonstrated: both calls
returned `rcv_01M0GK70SH2FHCZVCZACKFXTZT` and **one row** exists — because
`record_corpus_version` looks the digest up and **returns the existing row**, and
`UNIQUE (semantic_digest)` enforces it.

A `reconstruction_corpus_target_runs` child table would therefore attach **both**
run sets to **one** corpus id. One content-addressed identity would carry two
different derivation explanations — directly contradicting the column comment
*"The semantic digest IS the corpus identity."*

**Why the obvious repair is blocked.** Adding a `target_provenance_digest` field
to the `semantic_digest` payload changes the digest of **every** corpus, including
legacy ones: `semantic_digest({"a":1,"b":2})` and
`semantic_digest({"a":1,"b":2,"c":None})` differ. The payload shape is effectively
frozen; widening it would make every existing corpus row unrecomputable.

**Repair (verdicts 6 + 7).** Run provenance must enter identity through the one
semantic field that already exists. Keep membership as a separately named,
separately verifiable object, and make the stored column a composite:

```
members_digest    = sha256(canonical{policy:"target-set-v1",
                                     league_id, members:[sorted unique game ids]})
derivation_digest = sha256(canonical{policy:"target-derivation-v1",
                                     acquisition_manifest_hash, plan_version,
                                     run_ids:[sorted unique]})
target_set_digest = sha256(canonical{policy:"target-binding-v1",
                                     league_id, members_digest, derivation_digest})
```

This satisfies §8's preference for unmuddied semantics — membership still has its
own digest, under its own frozen policy name — while making derivation provenance
part of corpus identity **without touching the frozen payload**. Two corpora with
identical members and different derivation evidence now have different identities,
which is correct: they make different scientific claims.

## 7. Third primary attack — the closure rule is not implementable

**The attack succeeds.** The document specifies *"no member may be inserted for a
corpus that already exists, because the corpus id is a function of the
membership."* Implemented literally, with an ordinary FK to the corpus and a
`BEFORE INSERT` trigger raising when the corpus exists, the result is:

```
sqlite3.IntegrityError: membership is closed: corpus already exists
```

**Membership becomes uninsertable in every case.** The FK requires the parent to
exist; the trigger fires precisely because it does. SQLite triggers cannot
distinguish "this parent was created earlier in my savepoint" from "this parent is
old" — there is no such predicate. "Insert atomically then close" is prose, not a
mechanism.

### The repair, proved executable

A **construct-then-seal** design with an explicit finalization row. Eleven attacks
run against a real migrated v22 database:

| Attack | Result |
|---|---|
| construct 3 members + seal in one `SAVEPOINT` | **succeeds** |
| append a same-league non-member **after** seal | refused — *"target membership is sealed for this corpus"* |
| `DELETE` a member | refused (append-only) |
| `UPDATE` a member | refused (append-only) |
| `INSERT OR REPLACE` an existing member | refused (f021 content-aware `BEFORE INSERT`) |
| re-seal with a different count | refused (seal immutable) |
| seal whose `member_count` disagrees with membership | refused |
| member from the wrong league | refused (league trigger) |
| member pointing at a missing `games` row | refused (FK) |
| membership insert fails mid-construction | **corpus rolls back too** — no orphan identity |
| unsealed corpus | still open — **so the verifier must require a seal** |

The last line is the load-bearing consequence: an unsealed corpus is open by
construction, so **absence of a seal must be a hard verifier failure**, not a
warning. The seal also carries `target_set_policy_version`, which resolves §21 —
a verifier cannot infer a hashing policy from an opaque 64-hex digest, and no
"try every known policy until one matches" contract is acceptable.

Run bindings need the identical treatment: they are sealed at construction, so a
later binding cannot silently re-explain an existing corpus.

## 8. Membership is not projectable — `game_id` is a random ULID

**This is the finding that most changes the implementation.**

The document says membership derives from raw listing responses "projected through
the existing official normalization," and justifies the key as *"the project's
canonical surrogate identity, deliberately stable across provider-id changes and
corrections (`db/ids.py`)."* The stability claim is true. The **projectability**
implication is false.

`new_game_id()` is `prefixed_id(GAME_PREFIX)` — a **ULID**. Two consecutive calls
gave `gm_01M0GK70ZWZN6Y86XQKM0HHWJD` and `…WJE`. `ids.py` says so itself: surrogate
IDs are used "wherever the natural key can change … games get postponed."

Consequences:

1. **No pure projection exists.** Raw evidence yields `provider_game_id`, never a
   `gm_` ULID. The hop must go through `provider_game_references`, whose
   `game_id` is **NULLABLE** (identity may be unresolved) and which carries
   `updated_at` (**mutable**, not append-only). So membership derivation depends
   on a mutable, incomplete mapping — reintroducing exactly the self-confirmation
   problem the architecture set out to eliminate.
2. **`target_set_digest` over ULIDs is database-instance-specific.** A byte-copy
   preserves it, so the portability property the document proved still holds. But
   a *rebuild* from identical raw evidence yields different ULIDs, a different
   digest and a different corpus identity. Content-addressing is therefore over
   *this database's* surrogates, not over the evidence.

**Verdict 4: a new deterministic listing-projection verifier and policy are
required.** It must, for the bound listing responses: parse the exact listing
wrapper, enumerate every provider game object, resolve each through
`provider_game_references` under an explicit `official-listing-projection-v1`
policy, and **fail closed** — never silently drop — when a provider game has no
resolved canonical game. §18's case is settled: a missing `games` row is a
**refusal**, never a dropped member.

`provider_game_id` collisions, duplicates across pages, corrections and
reschedules resolve through the existing `UNIQUE (provider, provider_game_id)`
reference semantics: one provider game is one reference is at most one canonical
target. Cancelled/postponed games **remain members** (§9).

## 9. Membership must not depend on downstream success — re-proved

The document's §2 table is correct and survives. Restated as an enforcement rule:
the derivation may read **only** bound listing raw responses and the reference
mapping. It may **never** read `nba_game_results`, `game_result_snapshots`,
`reconstructed_input_provenance`, `historical_market_event_observations`,
`identity_audit_records` or `static_crosswalk_provenance`. Malformed completion
evidence, a failed Stage-A projection, an unresolved identity, a missing Odds
event and an absent E1 price all leave a target **in** the population with a
status. Any verifier that reconstructs membership from accepted/eligible rows is
computing a numerator and calling it a denominator.

## 10. Digest contract attacks (§22)

`games.game_id` is **TEXT**, so the B2 integer/string coercion defect has no
analogue here — members serialize as JSON strings, unambiguously. Verified:
ordering does not change identity; membership change does; same count with a
different member does; league case is significant.

Two contract holes:

- **Duplicates.** `sorted()` retains a duplicate, so one membership set has **two
  valid digests**. Duplicates must be **refused**, not de-duplicated — the
  document says this, and the digest function must enforce it rather than rely on
  callers.
- **Empty set.** `sha256` over zero members is a perfectly well-formed digest
  (`41c437797585a051…`). A target-bound corpus with no targets would verify
  trivially. Non-emptiness belongs on the **seal** (`member_count > 0`, enforced
  by CHECK in the proved design), not in the digest policy — generic schema
  semantics stay honest while no real corpus can be vacuously certified.

## 11. Legacy rejection must be structural, not textual (§20)

Confirmed forgeable: a direct-SQL caller stored a perfectly well-formed
`target-set-v1`-shaped 64-hex digest with **no membership rows behind it**. A
`startswith("target-set-v1:")` test — or any pattern test — creates no authority.

**A corpus is target-bound if and only if** it has a seal, the seal names a known
policy version, membership rows exist and are non-empty, run/manifest bindings
exist and are sealed, and the recomputed composite digest equals the stored
column. Everything else is **LEGACY UNBOUND**: still valid for TEAM-A provenance,
existing audits and crosswalks, never able to back §AF or a real Stage-A plan.

## 12. Supersession — **SIBLING / DISTINCT POLICY**

No hedging: the target-bound corpus **does not supersede** the target-unbound
official corpus.

Supersession asserts a corrected restatement of the *same* claim. A target-bound
corpus makes a claim the old corpus never made, under a different
`reconstruction_policy_version`, and — given no authoritative March corpus exists
locally — may well have no ancestor at all. Recording supersession would
manufacture a lineage.

Consequences to enforce: C1 stays byte-identical; legacy audits and crosswalks
remain valid; "latest corpus" resolution must **not** silently switch consumers
onto the target-bound corpus; and the invariant *E0 enrichment may only descend
from a target-bound (sealed, verified) corpus* is enforced **at the E0 seam**, not
by recency.

`source_corpus_digest` (§13) must be **scoped to the bound acquisition's evidence**,
not inherited from a broader official corpus. Inheriting a broad digest would
overstate the source set and would let corpus A's members pair with corpus B's
run bindings; scoping it means the derivation digest and the source digest commit
to the same evidence, which is what §14 asks for.

## 13. Repaired minimal v23 shape

Not two objects — **three tables and one column**, each with a named undetectable
failure. Anything without one was removed.

| Object | Key | Undetectable without it |
|---|---|---|
| `reconstruction_corpus_targets` | PK `(corpus_version_id, game_id)`; FK both | a target omitted from the corpus |
| `reconstruction_corpus_target_runs` | PK `(corpus_version_id, run_id)`; FK both | membership asserted but not re-derivable |
| `reconstruction_corpus_target_seals` | PK `corpus_version_id` | membership or run bindings extended after creation; unknown digest policy; a vacuous empty corpus |
| `…_seals.acquisition_manifest_hash` + `plan_version` | — | a caller-selected run subset omitting a required run (§2) |

Rejected: a separate scope table (disproved by reproduction C/D), per-target hint
columns and a stored `S_final` (§14 below), and a separate provenance-digest
column (blocked by the frozen `semantic_digest` payload — folded into the
composite instead).

**Executable construction order**, in one savepoint:

```
verify acquisition completeness (manifest -> required runs -> cursor-chain closure)
project bound listing responses -> canonical members  (fail closed on any unresolved)
compute members_digest, derivation_digest, target_set_digest
SAVEPOINT build
  INSERT corpus row                       -- FK parent must exist first
  INSERT membership rows                  -- allowed: no seal yet
  INSERT run-binding rows                 -- allowed: no seal yet
  INSERT seal (policy, member_count, manifest_hash, plan_version)
     -- trigger asserts member_count == COUNT(membership)
RELEASE build
verify by recomputation
```

## 14. §AF seam and S_final

`S_final` must **not** be stored in target rows. It is hint evidence, a different
claim from membership, and duplicating it would create a second truth that can
disagree with the source evidence. The coherence §AF needs comes from both facts
descending from the same sealed acquisition: membership from the bound listing
responses, `S_final` from the official evidence under the same scoped
`source_corpus_digest`. Because the derivation digest commits the manifest hash
and run set, members from corpus A cannot be paired with `S_final` evidence from
corpus B without changing the corpus identity.

The seam is unchanged in shape and correct:

```
verify_corpus_target_population(parent)   <- requires seal; recomputes composite digest
  AND B2 committed-manifest verification
  AND §AF independent target -> bucket recomputation
  AND v22 acquisition / projection certification
  -> eligible for E0 enrichment
```

`derive_target_population` stops refusing only after `verify_corpus_target_population`
returns clean. No step may take the Stage-A manifest as the source of expected
membership.

## 15. Direct-SQL threat model (repaired design)

| Attack | Control |
|---|---|
| Forged `target_set_digest` | verifier recomputes the composite from membership + bindings |
| Membership added after creation | seal trigger (**proved**) |
| Membership removed / updated | append-only triggers (**proved**) |
| `REPLACE` / `INSERT OR REPLACE` | f021 content-aware `BEFORE INSERT` (**proved**) |
| Run binding appended later to re-explain a corpus | seal trigger; derivation digest is in corpus identity |
| Two run sets collapsing to one corpus id | composite digest separates them (**attack proved against the unrepaired design**) |
| Member from the wrong league | league trigger (**proved**) |
| Duplicate member | PK (**proved**) |
| Member game row missing | FK (**proved**) |
| Membership from another corpus | member set is an input to *this* corpus's identity |
| Caller-selected run subset (§2) | manifest hash + derived required-run set |
| Truncated / gapped listing | cursor-chain closure (**proved**) |
| Provider-truncated body losing `meta` | **RETAINED** — mitigated by manifest coverage assertion |
| Legacy unbound corpus presented as bound | seal required; pattern matching explicitly insufficient |
| Empty target set | `member_count > 0` CHECK on the seal |
| Extra unrelated games in the DB | membership derives from bound runs, not from `games` |
| Rebuild yields different ULIDs | **RETAINED** — content address is over this database's surrogates |

## 16. Minimum future NBA March artefact checklist (§26)

The document's stated minimum (preserved DB with listing raw responses +
`ingestion_runs`) is **not sufficient**. Required:

1. `raw_responses` — every `/v1/games` page, with `request_params_json` (cursor
   preserved), full `body`, `body_hash`, `content_hash`
2. `ingestion_runs` — the runs those responses belong to
3. `provider_game_references` — the `provider_game_id` → `game_id` mapping
4. `games` — the canonical rows the members point at (FK targets)
5. **the acquisition manifest file** — `nba_coverage_2026_03.manifest.json`, whose
   hash the derivation digest commits
6. **the resume checkpoint file** — `manifest_hash`, `plan_version`,
   `stage_game_ids`, `scratch_fingerprint`, per-unit identities
7. schema version (migratable to v23)
8. **no secrets** — none are needed and none may be imported

Items 3, 5 and 6 are additions this review makes. If the checkpoint or manifest
cannot be produced, the derivation digest cannot be computed and the historical
March corpus is **permanently unusable for target binding** — not because the data
is bad, but because completeness would be unfalsifiable.

## 17. Fallback: a new bounded parent acquisition (§27)

Contingent discovery is acceptable — targets need not be enumerated in advance —
**provided the manifest precommits a complete listing-query protocol**: exact
endpoint and provider, exact date window and its decomposition, page size and
cursor-exhaustion rule, an explicit "no `max_pages`/`max_records`/`max_games` cap
applies to the listing family" assertion, and the projection policy version. Then
the resulting response population is verifiable against a precommitted rule rather
than against itself. Design and execution stay out of scope here.

## 18. Strict PIT / leakage

Unchanged and re-proved by inspection: no change to `AsOfReader` or
`_feature_cutoff`; `S_final` stays a search hint and is not stored on target rows;
no retrospective hint becomes Lane-L data; the population is never shrunk by
future outcomes (§9). Target binding is corpus provenance only.

## 19. 239 / 160 / 161 — all UNVERIFIED

- **239 targets** — prior observation only. `nba_ingestor.py` does reference "40 of
  239 games" for the March 2026 lineup truncation, which corroborates the number
  as a historical observation and **does not** verify it as a target population.
- **160 buckets** — prior observation only.
- **161 requests** — **invalid** as a current bound.

No request cap may be set before target binding, §AF derivation and independent
review are all complete.

## 20. Validation

Zero network (31 guards armed, 11/11 probes blocked); schema **v22 / 22 / 61**
unchanged; `git diff --check` clean; 108 protected artefacts unchanged except the
two documents in this commit; no secrets read or written; no database or raw
payload staged; no migration; no v23 code. Offline proofs are scratch-only.

## 21. Status

B3 deferred. P1 unauthorized. No real Stage-A plan. No probe registered. Stage A
NOT run. `REGISTERED_LINKING_PROVIDERS` empty. `ATTESTED_GENERATIONS` unchanged.
G5 NOT run. No crosswalk. **F1-R blocked.** No real NBA corpus supplied.

## 22. Exact next authorization boundary

> **V23 target-population implementation, in the repaired shape**: three tables
> (`reconstruction_corpus_targets`, `reconstruction_corpus_target_runs`,
> `reconstruction_corpus_target_seals` carrying the acquisition manifest binding
> and policy version), the composite `target-binding-v1` digest, the
> official-listing projection verifier with cursor-chain closure, and
> `verify_corpus_target_population`.

Not authorized here: writing migration f023, supplying a real NBA corpus, §AF
review, declaring the real Stage-A plan.
