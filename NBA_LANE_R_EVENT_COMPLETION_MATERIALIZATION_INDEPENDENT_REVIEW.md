# NBA Lane-R event-completion materialization — independent review

Reviews the implementation shipped at **`30a8746`** against the preserved
evidence and the reviewed architecture. Every claim in
`NBA_LANE_R_EVENT_COMPLETION_MATERIALIZATION_IMPLEMENTATION.md` was treated as a
hypothesis. The harness
(`sports_quant/db/tests/test_nba_completion_review.py`) is independent of the
implementation's fixtures.

> **VERDICT: ACCEPTED WITH REPAIRS.**
>
> Four defects were reproduced against `30a8746` and repaired. The most
> consequential was a **false rejection**: the shipped period-monotonicity gate
> discarded a genuine game whose terminal evidence was corroborated three
> independent ways. A false rejection silently shrinks a research corpus and is
> as damaging to a defensible result as a false admission.
>
> **Real coverage corrected from 236/239 to 237/239 (99.2 %).**
>
> **Schema stays v19** — 19 migrations, `f018`/`f019` untouched (last modified at
> `2824c3a`), no migration added, **no new availability rule**.
>
> **Strict-forward PIT unweakened.** `_feature_cutoff` byte-identical.

Where this document differs from the implementation report, **this document is
authoritative**.

---

## 1. The policy itself — upheld

The adopted policy is that the final recorded play's `wallclock` is a
**lower-bound completion proxy, not an official-final timestamp**, with the
existing `prior_event_completion_conservative_v1` (+6 h) rule providing
conservatism.

Independently confirmed:

* The final play cannot occur after the game ended, so the value is a sound
  lower bound.
* +6 h is applied to the derived bound and nowhere else; the derived instant is
  never substituted for another timestamp.
* The existing rule applies to this evidence class; **no new rule was needed or
  added** (`AVAILABILITY_RULES` still holds exactly the two reviewed rules).
* Downstream consumers can distinguish this from a DIRECT observation: the
  evidence object carries `classification = defensible_derived_lower_bound` and
  the policy version, and a test forbids over-claiming phrases in the policy
  text.

Overtime is handled correctly (9 real OT games accepted; a synthetic 6th-period
game derives its terminal instant).

## 2. Defects reproduced and repaired

### R1 — period monotonicity caused a FALSE REJECTION *(moderate)*

The shipped derivation required `period` to be non-decreasing along `order`.
Nothing in the preserved evidence supports that as a provider guarantee.

I falsified it on real data. Real game **`18447743`** was rejected purely for
mid-sequence period disorder, yet its terminal play is corroborated **three
independent ways**:

| Corroboration | Result |
|---|---|
| Carries the `End Game` marker at maximum `order` (514/514) | ✓ |
| Holds the maximum `wallclock` in the payload | ✓ |
| Its score `(138, 118)` equals the payload maximum **and** the official game-object score | ✓ |

I also disproved the implementation's stated hypothesis that the disorder is a
pagination-assembly artefact: with `per_page=100`, the regressions occur **within**
100-play chunks, not at chunk boundaries.

**Repair.** The period gate is removed, replaced by the check below.

### R2 — no terminal-score corroboration *(moderate, admitted bad evidence)*

With the period gate gone, what actually separates a truncated feed from a merely
disordered one? The shipped code had **no such check**: a payload whose terminal
play carried `End Game`, maximum `order` and maximum `wallclock` but a score
**lower than plays it supposedly follows** was accepted.

That is the real 18447741/18447742 shape:

| Game | Terminal play score | Payload maximum | Official |
|---|---|---|---|
| `18447741` | (103, 80) | **(121, 110)** | (121, 110) |
| `18447742` | (101, 74) | **(122, 92)** | (122, 92) |
| `18447743` | (138, 118) | (138, 118) | (138, 118) |

**Repair.** The terminal play's score must equal the payload's maximum score. A
score never decreases, so a terminal play below the maximum proves the recorded
sequence is not the whole game.

Deliberately compared **within the payload**, not against `/v1/games`: real game
**`18447470`**'s play feed disagrees with the game object by 3 points. That is a
scoring-feed discrepancy that says nothing about *when* the game ended, and
gating on it would have rejected a 237th sound game for an unrelated reason.

Across the population the marker check and the score check agree exactly
(237 each); the period check was the sole outlier at 236.

### R3 — a boolean `order` was accepted *(minor)*

`isinstance(True, int)` is `True` in Python, so `order: true` passed validation
and would sort as `1`, silently reordering the sequence. **Repair:** booleans are
refused explicitly.

### R4 — nothing re-derived a stored completion instant *(moderate)*

`availability_source` is a **free-text locator by architecture** — §11 asks for
"a pointer to the documenting evidence" and f018 calls the column "a stable
citation key". So it is correctly *not* digest-bound the way
`availability_rule_digest` is (which `verify_rule_digest` checks). **The
implementation's use of it is architecturally correct; the report's word "bound"
overstates it.**

The real gap is downstream: **nothing anywhere re-derived
`source_event_completed_at` from the evidence it cites.** A certification could
name this policy, cite real preserved evidence, and carry an instant that
evidence does not produce — and the reader would admit it, correctly, because the
reader decides availability rather than re-deriving evidence.

**Repair.** `verify_completion_certifications()` re-derives every stored instant
from its cited `raw_responses` row and reports mismatches. It is a **detective**
control, stated as such: direct SQL can still write a wrong instant; it cannot
survive verification. Detects a fabricated instant, evidence altered after
certification, and evidence that has since disappeared.

**A bug in my own first draft of that verifier is worth recording:** it initially
flagged *evidence game ≠ target game* as a problem. That is backwards — an
EVENT_DERIVED certification is for a **target** game and its evidence comes from
a **prior** one, so they must differ. The check is now inverted to catch the real
hazard (evidence from the target itself), which is also what the reader refuses
as `target_game_self_reference`.

## 3. Attacks that FAILED to break the implementation

| Attack | Result |
|---|---|
| Provider case/Unicode variants (`BALLDONTLIE`, zero-width space, padding) | refused |
| Endpoint variants (trailing slash, uppercase, query string, prefix) | refused |
| Non-200 status | refused |
| Malformed JSON, non-object payload, empty `data` | refused |
| Mixed or wrong game ids | refused |
| Duplicate / non-integer `order` | refused |
| Missing, malformed, or naive `wallclock` (3 naive forms) | refused |
| Wallclock regression | refused |
| Missing / duplicated `End Game`; `End Game` not terminal | refused |
| Terminal play not carrying the maximum instant | refused |
| Equal wallclocks on the terminal pair (tie ≠ regression) | accepted, correct |
| String vs integer `game_id` in payload | accepted, correct |
| Destination row with same id, unrelated content | **refused, not overwritten** |
| Evidence from two source corpora into one destination | coexists cleanly |
| All 17 columns compared at SQL level incl. NULL `content_type`, Unicode headers, param ordering | identical |
| Body compared as **bytes**, not parsed JSON | identical |
| Source connection traced for any write | none |

### `raw_response_id` preservation — adjudicated SAFE

The id is a ULID minted per capture, so it is **database-local, not a content
hash**. Preserving it across databases is nonetheless sound here because:

* conflict detection compares **all 17 columns**, so same-id/different-content is
  refused rather than overwritten (proved with an unrelated `/v1/box_scores` row
  under the same id);
* identical evidence is reused idempotently;
* evidence from multiple corpora coexists (ULIDs do not collide in practice, and
  a collision would be caught by the conflict check, not silently merged).

One consequence is documented rather than treated as a defect: two corpora that
independently captured the **same** provider response will materialize as **two
rows with identical bodies and different ids**. Each remains a faithful copy of
what its own corpus preserved, but a future reader must not assume one row per
distinct payload. A test pins this.

### Source immutability — upheld

`raw_responses` is append-only at the **database** level
(`trg_raw_responses_no_update` / `no_delete`), so casual mutation is impossible;
my tamper tests had to drop the triggers first. The source connection receives no
write, verified with a SQLite trace hook.

## 4. Real NBA 2026-03 coverage (recomputed after repairs)

| Measure | Shipped | **After review** |
|---|---|---|
| `/v1/plays` payloads | 239 | **239** |
| Accepted | 236 | **237 (99.2 %)** |
| Rejected | 3 | **2** |
| Rejected game ids | 41, 42, **43** | `18447741`, `18447742` |
| Rejection reason | period regression | `End Game` not last by order (91 and 141 plays follow it) |
| Overtime accepted | 9 | 9 |
| Distinct instants | 236 | **237** (no collisions) |
| Earliest / latest | — | `2026-03-01T20:36:10Z` / `2026-04-01T05:29:21Z` |
| Materialized / replay | 236 / reused | **237 created, 237 reused** (idempotent) |
| Destination certifications | 0 | **0** |
| Destination `game_status_history` | 0 | **0** |

**Prior-event availability:** 226 of the 237 accepted games fall on a date with an
earlier in-corpus date. The implementation reported 228/239; that used a
different denominator (all payloads, not accepted evidence). The 11 games on
2026-03-01 have no in-corpus prior. One month still gives thin rolling-window
depth.

**The two exclusions are genuine** and must be reported explicitly by any later
F1-R run, not silently dropped.

## 5. v19 certification path — re-proved

Preserved payload → materialized row → derived instant → EVENT_DERIVED
certification → `prior_event_completion_conservative_v1` → reader admission.
Admitted at exactly completion + 6 h; boundary exact to the microsecond
(−1 µs rejected, exact admitted, +1 µs admitted). A tampered rule digest still
fails closed. `certify_input` and the reader were **not** weakened.

## 6. Schema, isolation, integrity

* **v19, 19 migrations**, `f018`/`f019` untouched. No migration added. No new
  availability rule. Fresh init idempotent; v17→v19 and v18→v19 reach 52 tables.
* `_feature_cutoff` byte-identical (`5d55345b…`); `AsOfReader` has no
  completion/retrospective surface; all five Lane-R tables remain `UNSUPPORTED`
  joins.
* 23 guards armed before provider-facing imports, **15/15 probes blocked**,
  `zeronet.TRIPPED == []`, **0 provider requests**. BALLDONTLIE was **not**
  queried to clarify the rejected games — preserved evidence only.
* Protected artefacts: **42/42 byte-identical**, mtimes, inodes and **both WAL
  and SHM sidecars unchanged**.
* Disposable destination databases only; none staged.

## 7. Test-quality assessment

The implementation's 39 tests were sound on refusals it had thought of, but had
two structural blind spots:

1. **Fixtures carried no scores at all**, so the truncation signal that actually
   matters could not have been tested — and was not implemented.
2. **No test asked whether a rejection was correct.** Every case asserted
   refusal of bad input; none asked whether good input was being refused, which
   is exactly where R1 lived.

The review adds **31 tests**, including all four defect regressions.

## 8. Limitations

1. **Coverage is 237/239, not complete.** Two exclusions must be reported.
2. **Prior-event coverage is 226/237** at the corpus edge; one month remains thin.
3. **The bound's tightness is unquantified** — the interval between last recorded
   play and official final is measured nowhere. The +6 h rule makes it immaterial
   for availability, but the value is not the official final time.
4. **`availability_source` is a locator, not a binding.** Reproducibility rests on
   re-derivation via the new verifier, which is detective, not preventive.
5. **No per-target certifications exist.** Producing them is F1-R.
6. **MLB remains blocked** pending its own endpoint-capability probe.

## 9. Verdict and next authorization boundary

**ACCEPTED WITH REPAIRS.** No NBA blocker is retained. The policy is sound, the
derivation is fail-closed in both directions, materialization is honest and
idempotent, and the derived instant is now verifiable against its own evidence.

**An NBA-only, bounded F1-R may now be separately authorized**, subject to:

* it runs on the **237** accepted games and reports the **2** exclusions
  explicitly;
* it reports the **11** first-date games as having no in-corpus prior;
* it runs `verify_completion_certifications()` over what it produces;
* it makes no claim that the derived bound is an official-final timestamp;
* one month is not season coverage, and no profitability claim follows from it.

This review must not be the same task as that authorization. F1-R was **not**
executed here. MLB, historical odds/market anchoring, F2, production matching,
feature engineering, model training, calibration, backtesting, recommendation
output and UI remain **UNAUTHORIZED**. Gates G1, G2, G3, G4, G6 unchanged.

---

**NBA F1-R TARGET-ANCHOR PREFLIGHT 2026-08-14 — F1-R BLOCKED: HISTORICAL MARKET
ANCHOR REQUIRED.**
`NBA_F1R_TARGET_ANCHOR_PREFLIGHT.md`. Read-only, **0 provider requests, 0 credits
spent**. Target-anchor coverage from preserved evidence is **0 of 239 games
(0.0 %)**. The reviewed contract (architecture Repair 4) requires every target's
`T_cut` to derive from a **historical market snapshot's contemporaneous
`commence_time`**, with the retrospective start usable only as a search hint and
**never** as the anchor. Two independent gaps: the NBA 2026-03 corpus preserves
**no market endpoint at all**, and the Odds API client implements **only current
odds** (`/v4/sports`, `/v4/sports/{key}/odds`) with **no `/v4/historical/`**. The
only market rows anywhere in the project are 1 978 dev-capture price snapshots
from a 30-minute window on 2026-07-23, all `baseball_mlb`, **zero** commencing in
March 2026 and **zero** linked to a canonical game. Every anchor shortcut was
tested and refused structurally, including the most plausible one — an
August-observed schedule snapshot used for a March target, which the reader
rejects as `not_yet_available`. **Reconciliation:** the completion review cleared
the *prior-event availability* prerequisite and never spoke to the *target
anchor*; F1-R needs both, so no wording is superseded. Next steps, in order: a
historical Odds API implementation task (zero-network), a **user decision on plan
entitlement** (a configured key proves nothing; monetary price is **UNKNOWN** and
was not guessed), a bounded live probe capped at ≤10 requests / ≤100 credits, and
an independent review — only then a bounded NBA F1-R. First-pass anchoring cost
independently computed as **160 requests (5.16 buckets/day) ≈ 1 600 credits**,
corroborating the architecture's measured 5.0/day. Schema v19 unchanged, strict
PIT unchanged, 42/42 protected artefacts byte-identical. MLB probe, F2, feature
engineering, model training, calibration, EV/backtesting, recommendations and UI
remain UNAUTHORIZED. G1/G2/G3/G4/G6 unchanged.
