# Reconstructed-Corpus Provenance (design only)

**Status: DESIGN ONLY.** Nothing here is implemented. No reconstructed rows, no
migration, and no change to the strict E1/E2 point-in-time (PIT) dataset builder
are produced by F1A. Schema remains **v17** (this line read v16 when written; the
repository has since migrated and no reconstructed row exists yet). This document is
extended by `HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md`, whose Lane R / Lane L /
LABEL_ONLY classes map onto the three provenance classes below. It specifies the
provenance model a *future*, separately-reviewed subphase (F2) must satisfy
before any reconstructed data exists. It exists so the distinction between strict
PIT data and reconstructed research data can never blur.

See `PHASE_F_RESEARCH_PLAN.md` §R for why ordinary retrospective backfill cannot
produce strict-PIT feature rows (every observation receives today's receipt
`observed_at`, which the E2 cutoff guard excludes).

---

## 1. Provenance classes

Every future row-producing corpus carries an explicit, immutable
**provenance class**. The three classes and what each asserts:

### 1.1 `strict_forward_pit`
- **Meaning:** captured live, going forward; `observed_at` is the honest receipt
  transaction time, never backdated. This is the E1/E2 contract as it exists now.
- **Availability evidence:** intrinsic — the row was received before the cutoff it
  is used at, by construction.
- **Permits:** live-replay evaluation, deployment calibration, market/EV backtests,
  and any profitability claim (at multi-season maturity).
- **Prohibits:** nothing beyond the existing E1/E2 guards.

### 1.2 `reconstructed_research`
- **Meaning:** derived retrospectively from historical provider data under a
  **conservative, documented source-availability assumption**. It is **not**
  transaction-time PIT and must never be represented as such.
- **Availability evidence:** a provider/source-documented rule for when the
  information would have been publicly available (e.g. "a completed game's final
  result is available no earlier than T+3h after scheduled start"; "an opening
  line is available at the provider's documented posting time"). The **assumed
  availability time** is stored explicitly and separately from `observed_at`.
- **Permits:** early baseline/feature/calibration-methodology research, relative
  feature-value studies, approximate effect sizes — **with sensitivity analysis**.
- **Prohibits:** live-replay claims, deployment calibration, and any profitability
  claim. Reconstructed features are limited to families that are pure functions of
  *prior completed games* with a defensible availability rule (ratings,
  opponent-adjusted form, rest/travel/schedule, venue/home; market-implied only
  with a PIT-timestamped odds source). Fast-changing same-day families (lineups,
  injuries, probables, weather) are excluded unless a defensible rule is documented.

### 1.3 `label_only_retrospective`
- **Meaning:** a final outcome recovered retrospectively for use **as a label
  only**. A completed game's result is unambiguous by dataset-build time.
- **Availability evidence:** none required for the *label* (the game is over).
- **Permits:** supplying the training target `y` for either corpus.
- **Prohibits:** being read as evidence of *feature* availability. A label's
  retrospective recoverability never implies the associated feature state was
  knowable at the cutoff. Labels remain physically isolated from feature state
  (the existing E2 feature-state / label-surface split).

---

## 2. Required per-row provenance fields (future schema concept)

A future migration (NOT in F1A) would add, in **separate** columns/tables so they
can never be confused with `observed_at`:

- `provenance_class` — `strict_forward_pit | reconstructed_research | label_only_retrospective`.
- `assumed_available_at` — the conservative availability timestamp (reconstructed
  only); **distinct from** `observed_at`, which stays the true receipt time.
- `availability_rule_id` + `reconstruction_policy_version` — the exact documented
  rule and its version used to derive `assumed_available_at`.
- `reliability_class` — an ordinal reliability/quality classification
  (e.g. `high | medium | low`) with documented criteria.
- `source_evidence_ref` — a pointer to the provider documentation justifying the
  availability rule.

`observed_at` semantics are **unchanged**: it is always the receipt transaction
time and is never backdated. Reconstructed availability lives only in
`assumed_available_at`, never in `observed_at`.

---

## 3. Separation guarantees

- **Physical & logical separation.** Reconstructed rows live in a separate corpus
  (separate database file and/or a `provenance_class`-partitioned store) and are
  **never** silently unioned with `strict_forward_pit` rows. A dataset build must
  select a single provenance class (or an explicitly-labelled, separately-validated
  mix) and record it in the feature manifest (`corpus_provenance`, see
  `PHASE_F_FEATURE_CONTRACT.md`).
- **Builder isolation.** The strict E1/E2 builder
  (`sports_quant/pit/dataset.py`) is **unchanged** and must **never** accept an
  `assumed_available_at` in place of `observed_at`; it continues to use receipt
  transaction time only. A reconstructed corpus is built by a *separate* future
  builder, never by weakening the strict one.
- **Quality reporting.** Reconstructed corpora receive their own `data-quality`
  reporting and a mandatory **sensitivity analysis** (how conclusions move as the
  availability assumptions are tightened/loosened). Strict-PIT quality reporting is
  never conflated with reconstructed reporting.

---

## 4. Claims matrix

| Claim | strict_forward_pit | reconstructed_research | label_only |
|---|---|---|---|
| Provides training label `y` | yes | yes | yes |
| Feature availability at cutoff proven | yes | **no** (assumed) | n/a |
| Relative feature-value / methodology research | yes | yes (with sensitivity) | no |
| Out-of-sample / live-replay performance | yes | **no** | no |
| Deployment calibration | yes | **no** | no |
| Retrospective economic simulation | yes | **yes, grade-bounded** | no |
| Profitability claim | yes (mature) | **no** | no |

**Reconciliation (2026-08-10, authoritative).** A Lane-R / `reconstructed_research`
**retrospective economic simulation IS permitted as research evidence** — it may
estimate and compare EV, rank strategies and inform go/no-go, always carrying its
evidence grade (E0–E3) and G1 variant. What remains prohibited is a **profitability
claim**: any assertion of realized or expected edge offered as a basis for staking
real money. That still requires `strict_forward_pit` / live-shadow evidence. The
prohibition in this table is therefore narrowed to claims, not to simulation.

---

## 5. Independent-review requirements (future)

Before any reconstructed corpus is built or used:

1. The availability rules and `reconstruction_policy_version` are independently
   reviewed against provider documentation.
2. The separation guarantees (§3) are verified by tests (no silent mixing; builder
   isolation; manifest records provenance).
3. The sensitivity-analysis protocol is defined and reviewed.
4. Any schema change implementing §2 is a **separate, independently-reviewed
   migration** — not bundled with the reconstruction builder, and never during
   ingestion.
5. **(2026-08-10)** The static-identity crosswalk passes a **corpus-scoped
   identity-consistency audit** over the exact reconstruction window, keyed on
   `(league, provider, entity_type, provider_id)` and failing closed on any
   collision — see `G5_PROVIDER_ID_STABILITY_REVIEW.md`. Collision-free status is
   a **corpus property and is version-bound**: a narrower window's pass never
   transfers to a wider one, and later evidence produces a new corpus version
   rather than rewriting an old one.
6. **(2026-08-11 — historical snapshot, superseded by 7 and 8.)** The schema for
   §2 and §5 exists as migration `f018` (schema **v18**) -- see
   `RETROSPECTIVE_PIT_SCHEMA_V18_IMPLEMENTATION.md`. *At that commit* it was a
   provenance foundation only, not independently reviewed, with no reader, no
   identity-audit engine and no market anchoring. F1-R, F2, production matching
   and model training were, and remain, unauthorized.
7. **(2026-08-11)** That foundation was independently reviewed
   (`RETROSPECTIVE_PIT_SCHEMA_V18_INDEPENDENT_REVIEW.md`): **ACCEPTED WITH REPAIRS**,
   now at schema **v19** via migration `f019`. The two material findings both
   concerned this document's own guarantees -- a crosswalk could be cleared by an
   audit of a *different* source corpus, and an accepted audit could later be
   contradicted by a blocking collision finding. Both now fail closed at the
   database. An eligible reconstructed input must additionally cite preserved source
   evidence from a fixed allowlist; excluded rows remain exempt, because "not
   admissible" is often precisely a statement that the evidence does not exist.
8. **(2026-08-12)** The production G5 identity-audit engine is implemented and
   **has not been independently reviewed**
   (`RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_IMPLEMENTATION.md`). Static crosswalks
   are produced for **persons only**; team and game crosswalks are blocked on
   canonical-entity preparation, which cannot be done deterministically without
   name matching. F1-R, F2, production matching and model training remain
   unauthorized.
9. **(2026-08-12)** That engine was independently reviewed
   (`RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_INDEPENDENT_REVIEW.md`): **ACCEPTED WITH
   REPAIRS AND A RETAINED BLOCKER**, audit policy now `g5-identity-audit-v2`.
   `ACCEPTED` means "no contradiction detected at this policy's detection power",
   never "verified stable identity" -- and every audit now records that power.
   Team and game crosswalks remain blocked; the reader must not begin until the
   canonical-team architecture is separately decided.
10. **(2026-08-12)** That architecture is now decided — **TEAM-A**, a
    source-controlled static attestation to the existing canonical franchise seed,
    with no schema change (`RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE.md`).
    The attestation map digest binds into the already-existing
    `reconstruction_corpus_versions.static_identity_map_digest`, so a remap
    produces a new corpus version and can never silently reinterpret an old one.
    **Decision only — not implemented, and awaiting independent review.**
11. **(2026-08-12)** That architecture was independently reviewed: **ACCEPTED WITH
    REPAIRS**. The map digest does **not** bind the crosswalk at v19, so
    map-membership must be enforced in code and CI; canonical-target injectivity is
    not required (provider-key functional uniqueness is); the game official key
    needs namespace-qualified provider values; and a canonical-team seed digest is
    required so a seed edit cannot silently change an old corpus's meaning. Schema
    verdict: **V19 SUFFICIENT WITH ADDITIONAL CODE INVARIANTS**. TEAM-A
    implementation may be separately authorized; the reader remains blocked.
12. **(2026-08-12)** TEAM-A was **implemented**
    (`RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION.md`): the committed
    60-entry attestation map, deterministic team static crosswalks, the
    official-provider canonical-game bootstrap, and the RV1/RV3/RV5 code+CI
    invariants. **Schema stayed v19 — no migration was added or edited.** The map
    digest now genuinely participates in the crosswalk semantic digest, and a
    corpus must declare the matching map digest and a code version *before* a team
    crosswalk may be written. Map-membership enforcement is a **detective**
    control (code + CI), weaker than the DB-enforced G5 bindings: direct SQL can
    still write a contradicting row, but it cannot survive verification.
    Reproduced read-only over both protected corpora with **0 provider requests**.
    **Not independently reviewed; the reader remains blocked.**
13. **(2026-08-13)** That implementation was independently reviewed:
    **ACCEPTED WITH REPAIRS**
    (`RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION_INDEPENDENT_REVIEW.md`).
    Seven defects were proven with failing reproducers and repaired. Two were
    serious: a canonical game could be created with **no persisted G5 audit**,
    and the dry run predicted the opposite of apply for teams and games. Every
    Lane-R canonical game now carries a corpus-scoped, audit-backed **game static
    crosswalk** (GAME-PROV-C) using structures v19 already had, so a reader can
    prove *provider game G in corpus C resolves to canonical game X under audit
    A*. Bare-provider games written by the conventional matcher now **converge**
    instead of being duplicated. The verifier recomputes the crosswalk semantic
    digest, so the map binding is genuinely checked. Completeness now means
    **referenced-id coverage**, not league-map materialization. Schema verdict:
    **V19 SUFFICIENT WITH ADDITIONAL REPAIRS** — no migration. **The reader may
    now be separately authorized.**
14. **(2026-08-13)** The `RetrospectiveResearchReader` was **implemented**
    (`RETROSPECTIVE_RESEARCH_READER_IMPLEMENTATION.md`). Lane selection is a
    **distinct type, not a flag**; strict-forward PIT is byte-identical and
    `AsOfReader` gained nothing. FORWARD_ONLY families are refused structurally
    before any database access. Admission requires a persisted v19 certification
    for the exact corpus/namespace/target/family, and `effective_at` is derived
    on read — never stored — then gated `<= T_cut`. **Schema stayed v19.**
    Implementation found three gates comparing a stored `str` to an enum with
    `is`, all **failing open**; all three are repaired with regression tests.
    Real-evidence validation admitted exactly one family per corpus and
    correctly **refused** `prior_results`, because both bounded corpora are
    collection-time-observed rather than carrying a true event-completion
    instant. **Not independently reviewed.** F1-R, odds/market anchoring, F2,
    matching, feature engineering and model training remain unauthorized.
15. **(2026-08-13)** The reader was **independently reviewed**:
    **ACCEPTED WITH REPAIRS, with a RETAINED DATA BLOCKER**
    (`RETROSPECTIVE_RESEARCH_READER_INDEPENDENT_REVIEW.md`). Two defects were
    reproduced on `0496987` and repaired. The serious one: the reader verified
    only that a cited crosswalk existed and named this corpus, never recomputing
    the crosswalk's own semantic digest — so a direct-SQL edit to
    `canonical_entity_id` produced an **admitted** static identity pointing at
    the wrong franchise, and `static_identity()` returned that wrong id.
    Reproduced on real MLB and NBA evidence. The digest is now recomputed in-band
    on every identity read. The second: the admission API silently ignored the
    namespace `entity_type`, now required to be GAME. Seventeen further
    falsification attempts failed to break the reader. **Schema stays v19; no
    migration.** **RETAINED BLOCKER:** neither bounded corpus contains any
    event-completion instant — `game_status_history` is empty in both and no
    results table has a completion column — so **EVENT_DERIVED is data-blocked**
    and F1-R cannot yet produce it. The same-database evidence constraint was
    adjudicated as an intentional, acceptable boundary, not a contradiction.

Until all four pass, only `strict_forward_pit` (forward collection) may support
evaluation, and no profitability claim may be made from reconstructed data.
