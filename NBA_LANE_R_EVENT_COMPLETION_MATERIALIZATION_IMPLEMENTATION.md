# NBA Lane-R event-completion policy + materialization — implementation report

> **IMPLEMENTED 2026-08-13. INDEPENDENTLY REVIEWED 2026-08-14 — ACCEPTED WITH
> REPAIRS.** `NBA_LANE_R_EVENT_COMPLETION_MATERIALIZATION_INDEPENDENT_REVIEW.md`
> is **authoritative where it differs from this document**. Four defects were
> reproduced and repaired: **(R1)** the period-monotonicity gate caused a FALSE
> REJECTION of a genuine game whose terminal play was corroborated three ways —
> real coverage is **237/239, not 236/239**; **(R2)** no terminal-score
> corroboration existed, so a truncated feed could be admitted; **(R3)** a boolean
> `order` was accepted because `isinstance(True, int)` is True; **(R4)** nothing
> re-derived a stored `source_event_completed_at` from its cited evidence — a new
> `verify_completion_certifications()` detective control closes that. Statements
> below marked **SUPERSEDED** were true of `30a8746` and are no longer true.
>
> **Original banner (as written at implementation time):**
> Implements the NBA-only path established by
> `LANE_R_EVENT_COMPLETION_EVIDENCE_INVESTIGATION.md` (verdict: *existing
> evidence sufficient for NBA only*).
> **Schema unchanged: v19, 19 migrations.** No migration added or edited;
> `f018`/`f019` untouched. **No new availability rule.**
> **F1-R was NOT executed.** **MLB remains blocked** pending its own separately
> authorized endpoint-capability probe.

**Starting HEAD: `16e6475`** (= `origin/main`), clean tree.

---

## 1. The policy, exactly as adopted

Recorded in code as `NBA_COMPLETION_POLICY`, bound to
`NBA_COMPLETION_POLICY_VERSION = "nba-final-play-wallclock-v1"`:

> For NBA retrospective Lane-R evidence, the wallclock of the final recorded play
> in the preserved BALLDONTLIE `/v1/plays` payload is accepted as the source
> event completion evidence. It is a **lower-bound completion proxy, not an
> official-final timestamp**. The existing six-hour
> `prior_event_completion_conservative_v1` rule is what makes downstream feature
> availability conservative.

### Why a lower bound, not the official final

`wallclock` is the provider's own UTC instant for a play. A game cannot have
ended *before* its last recorded play, so the value bounds completion **from
below**. Whether official scorekeeping declared the game final at that same
instant is **not evidenced by anything preserved in the corpus**, and nothing in
this implementation claims it is.

A test asserts the policy string contains "lower-bound" and "not an
official-final timestamp" and does **not** contain over-claiming phrases
(`is the official`, `equals the official`, `direct completion`), so the strength
claim cannot quietly drift in a later edit.

### Why no new availability rule

The residual gap between last recorded play and official final is minutes.
`prior_event_completion_conservative_v1` already adds **six hours** before a
derived fact becomes knowable — two orders of magnitude larger. Adding a bespoke
margin would introduce an unreviewed policy where a reviewed one already
suffices. `AVAILABILITY_RULES` is asserted to still contain exactly the two
reviewed rules.

### Where the policy is bound, without new schema

The policy string is stored as the certification's **`availability_source`** —
the v19 field whose stated purpose is naming the evidence documenting an
availability claim. No column, table or migration was added to hold prose.

## 2. Code boundaries

| File | Role |
|---|---|
| `sports_quant/retrospective/nba_completion.py` | Policy constants, derivation, materialization (new) |
| `sports_quant/db/tests/test_nba_completion_evidence.py` | 39 adversarial tests (new) |
| `sports_quant/retrospective/__init__.py` | Lazy exports only |

No other production file changed. No reader, repository, schema, migration or
strict-PIT code was modified.

## 3. Fail-closed derivation

`derive_completion_evidence(raw)` refuses — never repairs — on:

* provider ≠ `balldontlie` (so MLB evidence is structurally rejected)
* endpoint ≠ `/v1/plays`
* non-200 response
* missing/unparseable request params, or no `game_id`
* malformed JSON, non-object payload, missing/empty `data`
* payload mixing game ids, or covering a game other than the one requested
* non-integer or duplicate play `order`
* missing `wallclock` on any play — **never inferred** from tip-off, order,
  `received_at`, `observed_at` or file metadata
* unparseable `wallclock`
* **naive / unzoned `wallclock`** (three forms tested)
* `wallclock` decreasing along play order
* ~~`period` decreasing along play order~~ **(SUPERSEDED — removed by the independent review; replaced by terminal-score corroboration)**
* **the terminal play carrying a score below the payload maximum** (added by the review)
* no `End Game` play (truncated)
* more than one `End Game` play
* `End Game` not being the last play by order
* the terminal play not carrying the maximum instant

`find_completion_payload()` additionally refuses when a game has **more than one**
preserved `/v1/plays` payload, rather than resolving the conflict by recency.

### The period-regression check — SUPERSEDED

> This section argued that a global period-monotonicity gate was load-bearing.
> The independent review **disproved it**: the gate rejected real game
> `18447743`, whose terminal play carries the `End Game` marker at maximum
> order, holds the maximum wallclock, and matches both the payload maximum and
> the official score. The pagination hypothesis offered here was also disproved —
> the regressions occur *within* 100-play chunks, not at chunk boundaries.
>
> What actually separates a truncated feed from a merely disordered one is
> whether the terminal play carries the payload's maximum score. That check
> replaces this one, and real coverage is **237/239, not 236/239**. See the
> independent review §2.

## 4. Materialization — honest by construction

The v19 evidence check resolves `source_evidence_id` against the **same**
database holding the certification, so a reconstruction corpus must contain the
evidence it cites. `materialize_completion_evidence()` performs exactly that copy.

* Reads the protected corpus only through the accepted `immutable=1` path.
* Copies **all 17 columns verbatim**, including `raw_response_id`. The identifier
  is **preserved, not regenerated**, so the destination row is provably the same
  evidence rather than a look-alike, and `source_evidence_id` means the same
  thing in both databases. (`SqliteRawResponseRepository.store()` always mints a
  new id, which is why a dedicated copy operation exists — it writes to the same
  `raw_responses` table and model, so no second storage model is introduced.)
* **`requested_at`, `received_at` and `created_at` are preserved exactly.** The
  derived March instant is *never* written over the August receipt metadata —
  that substitution is precisely the backdating this lane exists to prevent, and
  a dedicated test asserts the two values differ.
* Reads the row back through the ordinary repository and verifies body and
  content hash survived byte-identically.
* **Idempotent**: an identical existing row is reused.
* **Conflict-safe**: same id, different content raises rather than overwriting.
* **Never writes to the source** — proved with a SQLite trace hook on the source
  connection, asserting no `INSERT/UPDATE/DELETE/DROP/ALTER`.

### Why `game_status_history` is not populated

Writing a synthetic `final` transition there would manufacture an observation
that was never made. The last-play wallclock is *derived evidence*, not a
status observation, and `game_status_history` is the reviewed home for real
status observations. Citing the `raw_responses` row directly keeps the claim
exactly as strong as the evidence. Tests assert **zero** rows are created in
that table in both source and destination.

## 5. Real NBA 2026-03 results (recomputed, not assumed)

Read-only source, disposable destination, **0 provider requests**.

| Measure | Result |
|---|---|
| `/v1/plays` payloads found | **239** |
| **Accepted** | **236** |
| **Rejected** | **3** |
| Rejection reason | all 3: period regression along play order |

> **SUPERSEDED:** the independent review proved the period gate rejected a
> genuine game (`18447743`). Corrected figures: **237 accepted, 2 rejected**
> (`18447741`, `18447742`), refused because `End Game` is not the last play by
> order. See the review §2 and §4.
| Overtime games accepted | 9 |
| Earliest derived instant | `2026-03-01T20:36:10.000000Z` |
| Latest derived instant | `2026-04-01T05:29:21.000000Z` |
| Distinct instants | 236 (no collisions) |
| Materialized into disposable DB | 236 created |
| Deterministic replay | 236 reused, **0 created** |
| Destination `game_status_history` rows | **0** |
| Destination certification rows | **0** (F1-R not executed) |
| Derivation deterministic on re-run | **yes** |

> **SUPERSEDED:** the 236/98.7 % figures in this section are superseded by
> **237/99.2 %**. The reasoning below about 239 counting presence rather than
> terminal completeness still stands.

### The investigation's 239 was a different number

`LANE_R_EVENT_COMPLETION_EVIDENCE_INVESTIGATION.md` reported 239 games carrying
`wallclock`. That counted **presence**, not terminal completeness. Applying the
full contract yields **236 usable (98.7 %)**. Nothing was tuned to reproduce 239.

The three rejected games are `18447741`, `18447742`, `18447743` — **contiguous
ids on one date (2026-03-08/09)**, which suggests a single collection-batch
defect rather than three independent provider quirks.

Two have `End Game` mid-sequence with 91 and 141 plays ordered after it. The
third, **`18447743`, would pass a terminal-marker-only check**: its `End Game`
*is* the last play at order 514/514. It is rejected because its period sequence
is scrambled (`[2,2,2,1,2,1,1]`). Ordering corruption that scrambled periods
could equally have scrambled wallclocks, so the payload is not defensible
evidence even though its final row looks correct. Ambiguity fails closed.

### Concept separation, proved on real rows

For representative real games, body and both hashes are identical across the
copy, while:

| | value |
|---|---|
| `received_at` (preserved collection time) | `2026-08-04T22:12:13.658496Z` |
| derived `source_event_completed_at` | `2026-03-01T20:36:10.000000Z` |

These are intentionally different concepts and remain so.

## 6. The v19 certification path, proved end to end

Proved on **disposable/scratch evidence only**, with no production contract
weakened:

```
preserved /v1/plays raw_response
  → materialized verbatim into the reconstruction DB
  → derived final-play wallclock
  → source_event_completed_at on an EVENT_DERIVED certification
  → prior_event_completion_conservative_v1 (+6h)
  → RetrospectiveResearchReader cutoff gate
```

* Admitted at exactly `completion + 6h` (`2026-03-02T02:36:10.000000Z`).
* Boundary exact: **−1 µs rejected, exact admitted, +1 µs admitted**.
* `source_evidence_table` remains `raw_responses`; `source_evidence_id` resolves
  to the exact destination row.
* A tampered `availability_rule_digest` still fails closed.
* `certify_input` and the reader were **not** weakened to make this work.

## 7. Isolation and integrity

* `_feature_cutoff` byte-identical (`5d55345b…`); `AsOfReader` gained nothing.
* The module references no `provider_*_references`, no `entity_match_decisions`,
  no `game_status_history` — asserted structurally.
* The module contains no `httpx`, `requests`, `socket`, `urllib` or provider
  client reference at all.
* 23 guards armed before provider-facing imports; **12/12** adversarial probes
  blocked; `zeronet.TRIPPED == []`. **0 provider requests.**
* Protected artefacts: **42/42 byte-identical**, with `mtime_ns`, inode and
  **both WAL and SHM sidecars unchanged**.
* The disposable reconstruction database lives in the scratch directory, outside
  the repository, and is not staged.

## 8. Limitations

1. **Coverage is 236/239 (98.7 %)**, not complete. The three excluded games must
   be reported as exclusions by any later pilot, not silently dropped.
2. **Prior-game coverage remains 228/239** at the corpus edge (11 first-date
   games have no in-corpus prior) — unchanged by this work, and one month gives
   thin rolling-window depth regardless.
3. **The bound's tightness is unquantified.** The interval between last recorded
   play and official final is not measured anywhere; the six-hour rule makes it
   immaterial for availability, but the value must not be described as the
   official final time.
4. **No per-target certifications exist.** Producing them is F1-R.
5. **MLB is untouched and still blocked.**

## 9. Scope statements

* **F1-R was NOT executed.** No target set, no prior-game enumeration, no feature
  values, no rolling statistics, no training rows, no target anchoring, no odds,
  no economic simulation, and no committed reconstructed research artefact. The
  disposable database contains **zero** certification rows.
* **MLB remains blocked** pending its separately authorized endpoint-capability
  probe. No MLB endpoint was probed, no MLB StatsAPI call made, and no completion
  was inferred from `First pitch + T`.
* Historical odds/market anchoring, F2, production matching, feature engineering,
  model training, calibration, backtesting, recommendation output and UI remain
  **UNAUTHORIZED**. Gates G1, G2, G3, G4, G6 unchanged.

## 10. Readiness

This implementation is **ready for independent review**. It has not been
independently reviewed, and that review should be a separate task.
