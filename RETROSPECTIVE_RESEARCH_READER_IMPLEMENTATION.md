# `RetrospectiveResearchReader` — implementation report

> **IMPLEMENTED 2026-08-13. INDEPENDENTLY REVIEWED 2026-08-13 — ACCEPTED WITH
> REPAIRS, with a RETAINED DATA BLOCKER for F1-R.**
> `RETROSPECTIVE_RESEARCH_READER_INDEPENDENT_REVIEW.md` is **authoritative where
> it differs from this document**. Two defects were reproduced and repaired:
> **(R2, high)** a tampered `static_crosswalk_provenance.canonical_entity_id` was
> ADMITTED, and `static_identity()` returned the wrong canonical id — the reader
> never recomputed the crosswalk's own semantic digest; **(R1, moderate)** the
> admission API silently ignored the namespace's `entity_type`. The review also
> found that **no event-completion instant exists anywhere in the bounded
> corpora** (`game_status_history` is empty in both), so EVENT_DERIVED is
> data-blocked for F1-R. Statements below marked **SUPERSEDED** were true of
> `0496987` and are no longer true.
>
> **Original banner (as written at implementation time):**
> Implements the Lane-R reader contract in `HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md`
> §12, on the identity/provenance foundation cleared by
> `RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION_INDEPENDENT_REVIEW.md`.
> **Schema unchanged: v19, 19 migrations.** No migration added or edited;
> `f018`/`f019` untouched.
>
> **Strict forward PIT was not weakened.** `_feature_cutoff` is byte-identical
> (hash pinned in two separate test files) and `AsOfReader` gained nothing.
>
> **F1-R, historical odds/market anchoring, F2, production matching, feature
> engineering, model training, calibration, backtesting, recommendation output
> and UI remain UNAUTHORIZED and were not started.**

---

## 1. What the reader is

An **admission decision procedure**. It answers one question:

> may family *F* for target game *G* in corpus *C* be used at cutoff *T*, and on
> what recorded basis?

It returns the provenance needed to defend that answer. It does **not** compute
feature values, aggregate anything, or read a feature out of an evidence table —
that is feature engineering and is not authorized. The separation is also
principled: *whether a fact was knowable* is a provenance question, and mixing it
with *what the fact equals* stops the two being independently checkable.

### Files

| File | Role |
|---|---|
| `sports_quant/retrospective/reader.py` | The reader, its outcome vocabulary and result types |
| `sports_quant/retrospective/families.py` | The reviewed family taxonomy as code |
| `sports_quant/db/tests/test_retrospective_reader.py` | 41 tests |
| `sports_quant/pit/tests/test_strict_pit_unchanged_under_lane_r.py` | +3 isolation tests |

Nothing else changed. No existing production module was modified.

## 2. Strict separation (§A)

Lane selection is a **type**, not a flag.

* `RetrospectiveResearchReader` is unrelated to `AsOfReader` by inheritance in
  either direction.
* No `ignore_pit=`, `retrospective=`, `unsafe=`, or mode boolean exists anywhere
  — asserted over `dir(AsOfReader)` and its `__init__` parameters.
* `sports_quant/pit/asof.py` contains no executable reference to
  `retrospective`, `effective_at`, `reconstructed`, `availability_basis` or the
  reader's name.
* Importing the reader does not mutate `AsOfReader` (checked by comparing `dir()`
  before and after, and re-pinning the `_feature_cutoff` hash afterwards).
* The two readers do not even share a cutoff type: `AsOfReader` takes a parsed
  `Cutoff`, this one takes an ISO string it parses itself.

## 3. Fail-closed family admission (§B)

`families.py` encodes the reviewed per-family taxonomy. It is consulted **before
any database access**, so a FORWARD_ONLY family is refused structurally, not
filtered.

| Class | Families | Returnable? |
|---|---|---|
| `STATIC_IDENTITY` | `static_identity` | yes, timeless |
| `EVENT_DERIVED` | `prior_results`, `team_rolling_stats`, `rest_schedule_density`, `pitcher_rolling_stats`*, `batter_rolling_stats`*, `bullpen_prior_usage`*, `player_rolling_stats`†, `advanced_rolling_stats`†, `plays_derived_stats`† | yes, completion + rule lag |
| `VERSIONED_SNAPSHOT` | `target_schedule_anchor`, `sportsbook_moneyline`, `kalshi_market`, `weather_forecast`* | yes, provider stamp |
| `LABEL_ONLY` | `final_result` | **only** via `admit_label()` |
| `FORWARD_ONLY` | `lineups`, `injuries`, `rosters`, `probable_pitchers`* | **never** |

\* MLB-only  † NBA-only. A league-specific family requested in the other league
is refused as `wrong_league_for_family`.

Asking for a FORWARD_ONLY family raises `ForwardOnlyFamilyError` from both
`admit_feature()` and `admit_label()`, at any cutoff, **even when a valid
certification exists for it**. An *unknown* family is refused too — a typo must
not silently inherit some other family's leakage rules.

`provider_*_references` are not in `SOURCE_EVIDENCE_TABLES`, so they cannot even
be cited as provenance; the certification is refused at write time. Verified for
all three reference tables.

## 4. Provenance-first eligibility (§C)

Every admitted input must have a **persisted** `reconstructed_input_provenance`
row for the exact corpus, namespace, target game and family. An in-memory claim
is never enough — there is no code path that accepts one.

Refusals, each individually named in `AdmissionOutcome`:

* corpus does not exist / is **superseded** / is strict-forward → construction fails
* no certification → `no_certification`
* certified `EXCLUDED` → `certified_excluded`
* wrong lane → `wrong_lane`
* basis contradicts the family's reviewed nature → `basis_contradicts_family`
* `STATIC_IDENTITY` with no crosswalk → `missing_crosswalk`
* crosswalk owned by another corpus → `crosswalk_from_another_corpus`

The cross-corpus case has **two independent layers**: the f018 trigger
`trg_rip_crosswalk_same_corpus` refuses the certification outright, and the
reader checks again. Both are tested — the second by dropping the trigger and
planting the row, because a reader that trusts its inputs is how a corpus
boundary leaks.

## 5. Availability semantics (§D)

`observed_at` / `received_at` / `decided_at` are untouched and unread by this
module. Lane-R availability is **derived, never stored**:

| Basis | `effective_at` | Gate |
|---|---|---|
| `STATIC_IDENTITY` | **`None`** | crosswalk provenance, **not** the clock |
| `EVENT_DERIVED` | `source_event_completed_at` + rule lag, via `rules.derive_availability_instant` | `effective_at <= T_cut` |
| `VERSIONED_SNAPSHOT` | `source_snapshot_at` (the provider's own stamp) | `effective_at <= T_cut` |

The rule is resolved through `verify_rule_digest`, so a corpus citing a rule this
build has edited fails closed rather than silently computing a different answer.

**Static identity is not wall-clock gated**, and that is deliberate: a timeless
fact has no availability instant, and inventing one would be the backdating this
design exists to prevent. It resolves at a 1901 cutoff — tested.

The cutoff is inclusive and enforced to the microsecond (`<=`), tested at
`-1µs / exact / +1µs`.

### Target-game self-reference

A prior-event feature may never derive from the target game's own evidence. The
temporal gate would normally catch it, but the architecture names this leak
explicitly, so it is **also** refused structurally: if the cited evidence row
carries `provider_game_id` equal to the target, the answer is
`target_game_self_reference` — even when the certification claims a completion
instant that would otherwise pass. Tested with exactly that lie.

For the four evidence tables with no `provider_game_id` (`raw_responses`,
`roster_snapshots`, `sportsbook_price_snapshots`, `lineup_players`) the temporal
gate is the only control, and the code says so rather than implying a check it
cannot make.

## 6. Core vs extended honesty (§E)

`AdmittedInput.correction_sensitive` is `True` whenever the corpus is
`G1_A_EXTENDED`, and it appears in `as_json()`. A caller therefore cannot
describe correction-sensitive extended evidence as transaction-time-exact by
accident. The sensitivity **experiment** is not implemented here — that is F1-R.

## 7. Defects found and fixed during implementation

Three gates were written comparing a stored value to an enum with `is`. The
provenance models hold `provenance_class`, `eligibility`, `availability_basis`
and `g1_variant` as **plain `str`**, so every one of those comparisons was
`False` forever — silently, with no error anywhere. **All three failed open:**

1. `eligibility is EligibilityVerdict.EXCLUDED` — an EXCLUDED certification
   would have been **admitted**.
2. `corpus.provenance_class is ProvenanceClass.STRICT_FORWARD_PIT` — a
   strict-forward corpus would have been **readable through the Lane-R reader**.
3. `g1_variant is G1Variant.G1_A_EXTENDED` — extended correction-sensitive
   evidence would have been reported as **core**.

All three now parse explicitly through `_parse`, which fails closed on any
unrecognized value. Three regression tests pin the behaviour, including a
structural one that greps the reader for the exact `.<field> is <Enum>` shape and
fails if it reappears. mypy found the first; the test suite found the second and
third.

## 8. Real-evidence validation (§J)

Read-only sources through the accepted `immutable=1` path, disposable v19
outputs, 23 network guards armed, **0 provider requests**.

| | MLB June 2026 | NBA March 2026 |
|---|---|---|
| Team crosswalks written | 30 | 30 |
| Canonical games | 400 | 239 |
| Schedule rows / games with results | 393 / 400 | 239 / 239 |
| **Admitted** | `static_identity` | `static_identity` |
| Static identity resolved | `108 → tm_mlb_laa` | `1 → tm_nba_atl` |
| FORWARD_ONLY families | 4/4 refused | 4/4 refused |
| `final_result` as a feature | refused | refused |
| Wrong-league families | 3 refused | 4 refused |
| Uncertified families | 10 × `no_certification` | 9 × `no_certification` |
| `prior_results` | **`not_yet_available`** | **`not_yet_available`** |

### The most important result: `prior_results` was refused

This is not a gap in the reader. Both bounded corpora were **collected in
July/August 2026**, long after the games they describe. Their `observed_at` is
therefore *collection* time, not event-completion time. Feeding it as
`source_event_completed_at` is deliberately the wrong thing to do, and the reader
refused it:

* MLB: result observed `2026-07-31T23:32Z`, cutoff `2026-07-01T01:40Z` → refused.
* NBA: result observed `2026-08-04T22:12Z`, cutoff `2026-04-01T03:00Z` → refused.

That is precisely the August-observed/March-cutoff leak Lane R exists to prevent,
caught on real evidence. **No family is claimed eligible merely because rows
exist**: of 19 families, exactly one is admitted per corpus, and it is the one
with genuine audited provenance behind it.

## 9. Limitations and blockers

> **SUPERSEDED (limitation 1):** the independent review determined this is an
> intentional, acceptable boundary rather than a blocker — a reconstruction
> corpus is meant to be self-contained, and materializing an evidence row carries
> `observed_at` verbatim with no backdating. The *real* blocker is limitation 2,
> which is more severe than stated here: there is no completion instant anywhere
> in the corpora, and `game_status_history` is empty in both. See the review §5–§6.

1. **Cross-database evidence citation.** The f018 evidence check runs
   `SELECT 1 FROM <table> WHERE <id> = ?` on the **provenance connection**, so a
   cited row must live in the same database as the provenance. Protected corpora
   are separate read-only files, so an EVENT_DERIVED certification cannot point
   at them directly. Materializing evidence into a reconstruction database is
   F1-R's job and is unauthorized. This bounds what real-evidence validation
   could demonstrate; it is a design boundary, not a reader defect.
2. **No true event-completion instant exists in the bounded corpora** (§8). Until
   forward collection or a builder supplies one, EVENT_DERIVED families are not
   admissible from this evidence.
3. **`kalshi_market` depth is not reconstructable** (gate G2) and
   **`weather_forecast` archive depth is shallow** (gate G3). Both are classified
   `VERSIONED_SNAPSHOT` on availability grounds only; neither gate is closed.
4. **The `admit_features()` batch call raises** on a FORWARD_ONLY family rather
   than reporting it as a rejection. Deliberate: asking for one is a programming
   error, and a raised error cannot be quietly ignored the way a list entry can.

## 10. Schema

**v19 unchanged.** Every requirement was expressible on existing structures:
certifications in `reconstructed_input_provenance`, identity in
`static_crosswalk_provenance`, the rule registry in code with a digest bound into
provenance. No migration was added; `f018` and `f019` were not edited. No v20.

## 11. Authorization boundary after this task

Implemented: the reader, the family taxonomy, their tests.

Still **unauthorized and not started**: `F1-R` execution, historical Odds API
fetching, market anchoring, `F2`, production matching, feature engineering, model
training, calibration, backtesting, recommendation output, UI.

Gates **G1, G2, G3, G4, G6 unchanged**. This reader has **not** been
independently reviewed.
