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

Until all four pass, only `strict_forward_pit` (forward collection) may support
evaluation, and no profitability claim may be made from reconstructed data.
