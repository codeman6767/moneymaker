# Retrospective PIT research architecture — independent review

Independent architecture-correctness and scientific-validity review of
`HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` (`c30fcf9`). Design review only: nothing
implemented, no schema v18, no F1-R, no F2, no production matching, no training,
**no provider data/API request**. Official documentation was read (research, not a
data request). No protected corpus mutated, no timestamp backdated.

**Verdict: ACCEPTED WITH REPAIRS.** The Lane-R/Lane-L architecture is sound in
structure and genuinely solves the blocker rather than bypassing it. Six defects
were found — one of them a false scientific claim at the centre of the design —
and are repaired here in documentation.

---

## 1. Boundary

`HEAD == origin/main == c30fcf9`, clean tree, CI #88 green, **schema v17**.
Commit `c30fcf9` touched **0** files under `sports_quant/` — documentation only.
No `RetrospectiveResearchReader` or any other implementation exists. Production
matching, F1-R, F2 and modeling remain unauthorized.

**Protected artefacts: 7/7 byte-identical** before and after this review.

## 2. The blocker is real and the design addresses it

Re-verified independently: `observed_at` is acquisition time; `decided_at` is
matcher wall-clock; **239/239** NBA and **400/400** MLB target schedules were first
observed after game start; the strict builder correctly yields **0** retrospective
pregame rows.

The design **solves** rather than bypasses it: it does not relax
`_feature_cutoff` for the strict lane, does not backdate anything, and does not
redefine `observed_at`. It introduces a *separate* lane with a *different and
independently evidenced* availability basis. **The strict Lane-L behaviour must
remain exactly as-is, and the design preserves it.**

## 3. Repair 1 — "formally proven" is false (G1). **Highest priority.**

The design's governing Lane-R rule claims inputs are *"formally proven to contain
no information whose semantic availability is after `T_cut`"*. For
correction-sensitive statistics **this claim is false**, and the design's own
evidence shows it.

Worked example, unrebutted:

> Game A completes Jan 1. Target B has cutoff Jan 3. A's box score is corrected
> Jan 10. The August fetch returns the Jan-10 value. **Even with L = 24 h**, B is
> fed a number that did not exist on Jan 3.

A lag bounds *publication* latency; it does nothing about *retroactive revision*.
And with exactly one stored version per game (NBA 239/239, MLB 400/400), the
corpus cannot even detect that a revision occurred. Rarity is irrelevant — an
unmeasured, undetectable, unbounded-in-principle error is not a proof.

**Resolution: G1-B for the core, G1-A for the extension.**

* **Core Lane-R baseline (G1-B):** admit only facts that are effectively immutable
  or independently reconstructable — win/loss, final score, game date, home/away,
  rest/schedule density, venue. These carry the design's strong guarantee.
* **Extended Lane-R (G1-A):** correction-sensitive box-score detail may be used
  for exploratory/baseline research **only**, carrying a mandatory limitation, the
  conservative lag, and sensitivity analysis. Results using it may **never** be
  described as transaction-time-exact PIT.
* The two must be reported as **separate feature-set variants**, never silently
  merged into one "Lane R" number.

**Required wording change** (applied in the repair): replace *"formally proven"*
with *"proven for immutable facts; bounded by documented assumption and sensitivity
analysis for correction-sensitive fields"*. Do not call an assumption a proof.

### Correction-sensitivity matrix

| League | Field | Correction possible | Versioned in current source | Can change target feature | Reconstructable from lower-level immutable data | Lane-R core? |
|---|---|---|---|---|---|---|
| Both | win/loss | very rare (protest/forfeit) | no | yes but negligible | yes (final score) | **core** |
| Both | final score | rare | no | yes | partly (play data) | **core** |
| Both | game date / home-away / venue | ~never | n/a | no | yes | **core** |
| Both | rest / schedule density | ~never | n/a | no | yes (schedule) | **core** |
| MLB | runs | rare | no | yes | yes (play data) | core |
| MLB | innings pitched | occasional | no | yes | partly | extended |
| MLB | pitcher K / batter hits / walks | **common** (scorer decisions) | **no** | yes | partly | **extended** |
| MLB | earned runs | **common** (ER/UER rescoring) | **no** | yes | no | **extended** |
| MLB | pitch counts | occasional | no | yes | no | extended |
| MLB | derived rate stats | inherits worst input | no | yes | no | extended |
| NBA | points | rare | no | yes | yes (play data) | core |
| NBA | rebounds / assists / turnovers | **common** (scorer review) | **no** | yes | partly | **extended** |
| NBA | minutes | occasional | no | yes | partly | extended |
| NBA | advanced stats | inherits worst input | no | yes | no | **extended** |
| NBA | play-derived metrics | occasional | no | yes | n/a (is the base) | extended |
| NBA | lineup-derived metrics | n/a — lineups are Lane L | — | — | — | **excluded** |

The design placed pitcher/batter and player rolling stats in Lane R without
qualification. **That is corrected here: they are Extended Lane-R, not core.**

## 4. Repair 2 — static identity, decided per entity type

**ACCEPT WITH ENTITY-SPECIFIC RESTRICTIONS**, not one blanket rule.

| Entity | Verdict | Reasoning and restriction |
|---|---|---|
| **Game identity** (`gamePk`, BALLDONTLIE game id) | **ACCEPT** | The id is in the historical row; equality alone resolves it. A reschedule changes the *date*, not the id — so the id remains a timeless reference while the date must come from contemporaneous evidence (§6). Doubleheaders receive distinct ids. |
| **Team identity** | **ACCEPT, id-only** | Franchise relocation/rename changes display name, not the official id. Therefore **match on id only, never on name**. Any name-based fallback is prohibited in Lane R. |
| **Player identity** | **ACCEPT for the person; REJECT for affiliation** | id → canonical *person* is timeless. But **team affiliation, position and status are time-varying** and must never be taken from a later identity snapshot. The design did not draw this line; it is drawn here. |

Leakage argument: none of these references encodes the outcome. Knowing "`gamePk`
822728 is this game" is information-free with respect to who won. The relaxation
is safe **only** because it is restricted to id equality — the moment name,
roster, stat or output is consulted, it stops being timeless.

Provider-id reuse must be re-checked per provider before implementation
(**new gate G5**).

## 5. Repair 3 — training target population and selection bias

The design allows training eligibility without a market snapshot but never
defines the *population*. That is a real gap.

Retrospective selection sees **only games that actually completed**. Postponed,
cancelled and rescheduled games are invisible. This introduces:

* **cancellation/postponement bias** — weather-affected MLB games are
  systematically under-represented, which is exactly where weather features would
  matter;
* **schedule-change bias** — rescheduled games enter under their *final* date;
* **survivorship** is mild for major-league games but non-zero.

**Required definition (repair):**

* **Training population** — all games that (a) completed, (b) have a settled
  label, and (c) have Lane-R-eligible features. Cancellation bias is **declared as
  a known limitation**, not silently absorbed.
* **Backtest population** — the strict subset that additionally has a genuine
  contemporaneous market snapshot at `<= T_cut`.
* Every future experiment must **report both counts and the exclusion
  decomposition.** Evaluating only the market-matched subset without reporting the
  differential is prohibited.

Deployment shift: the live population includes games that may later be postponed;
the trained model never saw them. This must be stated in the model card.

## 6. Repair 4 — the anchoring algorithm was circular

The design says "snapshot at `T_cut = scheduled_start − 60 min`" but
`scheduled_start` is the **retrospectively known final** start. If a game was
moved, that instant is hindsight.

**Deterministic algorithm (repair):**

1. Take the retrospective official start `S_final` as a **search hint only**.
2. Query the historical snapshot at `S_final − 60 min`, floored to the provider's
   snapshot grid.
3. Read the **contemporaneous `commence_time`** for that event *from the snapshot*.
4. Set `T_cut := commence_time_snapshot − 60 min`. If that differs from step 2's
   instant by more than one snapshot interval, **re-query at the corrected
   instant** and repeat (bounded to 3 iterations).
5. Accept only if, in the final snapshot: the event is present, `commence_time` is
   **in the future relative to the snapshot timestamp**, and the market is active.
6. **Reject** the target if: no market exists at `T_cut`; the event had already
   commenced at snapshot time; iteration does not converge; or the snapshot's
   `commence_time` is absent.

Handling: **postponed/cancelled** — rejected by step 5 (no valid pre-commence
snapshot at the final date). **Doubleheaders** — disambiguated by official game id
plus contemporaneous `commence_time`. **Start-time changes / NBA delayed tips** —
resolved by iteration, since `commence_time` is contemporaneous. **Missing market**
— target is training-eligible only, never backtest-eligible.

`commence_time` from the snapshot is the availability evidence. The retrospective
final start is **never** the anchor.

## 7. The Odds API — independently verified

Documentation (accessed 2026-08-10):

* Endpoints `/v4/historical/sports/{sport}/odds`, `.../events/{eventId}/odds`,
  `.../events`.
* Archive from **2020-06-06**; **10-minute** snapshots before 2022-09-18,
  **5-minute** after.
* `date` returns **the closest snapshot equal to or earlier than** the requested
  timestamp — the required `<= T_cut` semantic. Confirmed.
* Cost **10× per region per market**; `h2h` available throughout.
* Pre-2022-09-18 captured **decimal only**; American odds derived (rounding risk).

### Terms and Conditions (accessed 2026-08-10) — the design did not check these

* **Redistribution:** *"Do not resell, repackage, or redistribute our data as a
  standalone data product."*
* **Commercial use permitted** in "websites, mobile apps, dashboards, analytical
  tools, and other user-facing applications … provided our data is not the primary
  product being sold or redistributed."
* **Storage/caching/retention: NOT ADDRESSED.**
* **Research / modelling / ML: NOT ADDRESSED.**
* **Attribution: NOT ADDRESSED.** **Historical data specifically: NOT ADDRESSED.**

**Conclusion.** Moneymaker's intended use — an analytical/recommendation
application where odds are an input, not the product — sits inside the permitted-use
clause. Local retention for research is **not prohibited, and also not expressly
permitted**; silence is not permission. Operating rule: retain only what the
research requires, never expose raw odds as a redistributable dataset or API, and
re-read the terms before any public launch. Recorded as **gate G6** (commercial
launch review), not a blocker for research.

### Independently recomputed credit budget — the design overstated it

Measured from the **real** schedules, flooring each game's `T−60` to the
provider's 5-minute grid and counting **distinct** snapshot instants (one request
covers all games sharing a bucket):

| | month (measured) | per season | 3 seasons | 5 seasons |
|---|---|---|---|---|
| NBA (5.0 buckets/day × 170 d) | 160 req / **1,600 cr** | 850 req / **8,500 cr** | 25,500 cr | 42,500 cr |
| MLB (9.3 buckets/day × 186 d) | 288 req / **2,880 cr** | 1,728 req / **17,280 cr** | 51,840 cr | 86,400 cr |
| **Combined** | **448 req / 4,480 cr** | **2,578 req / 25,780 cr** | **77,340 cr** | **128,900 cr** |

The architecture claimed ~41,500/season and ~208,000 for five. The measured
figures are **~38% lower**. The architecture is corrected. These remain estimates
of *credits*, not prices; no subscription was priced and nothing was purchased.

## 8. Repair 5 — weather is weaker than the design assumed

Open-Meteo documentation (accessed 2026-08-10):

* **Historical Forecast API stitches the first hours of successive runs** — a
  near-nowcast, **not** a pregame forecast. Correctly prohibited by the design.
* **Previous Runs API** uses **fixed day-granular lead offsets**
  (`_previous_day1` = 24 h before valid time … `_previous_day7`). It does **not**
  accept an arbitrary initialization timestamp. **The design implied a T−60
  forecast is retrievable; it is not.**
* **Single Runs API** takes `run=`, but ECMWF IFS HRES only from **Mar 2024**,
  others from **Apr 2026**.
* **Public availability delay is NOT DOCUMENTED.** Update *cadence* is given
  (global every 6 h) but not how long after initialization a run becomes
  downloadable. The design's rule — "select the latest run whose *public
  availability* ≤ `T_cut`" — is therefore **not deterministically satisfiable from
  documentation**.

**Repair.** Use `_previous_day1` (24-hour lead) as the conservative pregame
forecast. Because it is issued a full day before valid time, **no plausible
publication delay can make it unavailable by `T_cut`** — the undocumented delay is
sidestepped rather than guessed at. Cost: forecast freshness, which for
temperature/wind is acceptable.

**Coverage for 2021–2025 is thin**: most models begin Jan 2024; only **GFS 2 m
temperature** reaches Mar 2021. Weather is therefore **excluded from the core
5-season baseline** and admissible only as a comparable-coverage variant.

## 9. Kalshi — G2 remains open

Verified: dedicated historical tier
`/trade-api/v2/historical/markets/{ticker}/candlesticks`; `period_interval` ∈
1/60/1440 min; fields `yes_bid`, `yes_ask`, OHLC, `volume`, `open_interest`.

**Unverified:** retention depth, historical-tier cutoff mechanism, timestamp
semantics, authentication for historical reads, pagination, and — decisively —
**whether single-game MLB/NBA markets even existed across a 3–5 season window**.
Kalshi's sports listings are recent; assuming five seasons of single-game markets
is unsupported.

**Candlesticks are not orderbook depth.** They support a probability baseline and
a conservative top-of-book execution approximation only.

**G2 stays open and blocks any Kalshi-based EV or liquidity claim.**

## 10. Economic-evidence grades (new; the design lacked this)

| Grade | Evidence | Permits | Prohibits |
|---|---|---|---|
| **E0** | market existed, no price | target anchoring only | any EV claim |
| **E1** | timestamped single price (bookmaker h2h) | probability comparison, no-vig baseline, indicative EV | slippage, liquidity, fill claims |
| **E2** | timestamped bid/ask + volume/candles | E1 + conservative executable approximation at top-of-book | ladder-walking, depth-dependent sizing |
| **E3** | historical orderbook depth | E2 + slippage and liquidity modelling | — |

The Odds API gives **E1**. Kalshi candlesticks give **E2 at best** (pending G2).
**E3 is unavailable from any planned source.** No simulated-profitability claim may
assume depth we cannot evidence.

## 11. Repair 6 — the provenance-document contradiction is real

`RECONSTRUCTED_CORPUS_PROVENANCE.md` §4 states `reconstructed_research`
**prohibits** profitability claims. The new architecture permits an "economic
backtest" in Lane R. **These conflict**, and both are currently authoritative.

**Resolution (authoritative going forward):**

* A Lane-R **retrospective economic simulation is permitted as research
  evidence** — it may estimate and compare EV, rank strategies and inform go/no-go.
* A **profitability claim** — any assertion of realized or expected edge offered
  as a basis for staking real money — **requires strict-forward / live-shadow
  evidence.** Unchanged from the older document.
* Lane-R economic output must always carry its evidence grade (§10) and the G1
  variant used.

The older document's prohibition therefore stands, narrowed precisely: it
prohibits *profitability claims*, not *economic simulation as research*. Both
documents are updated to state this identically.

## 12. Vocabulary — one canonical set

The two documents used parallel names. Unified:

| Canonical (implementation) | Architecture doc | Provenance doc | Meaning |
|---|---|---|---|
| `strict_forward_pit` | **Lane L** | `strict_forward_pit` | received before cutoff by construction |
| `reconstructed_research` | **Lane R** | `reconstructed_research` | availability from documented evidence |
| `label_only_retrospective` | LABEL_ONLY | `label_only_retrospective` | settled outcome, target only |

**`provenance_class` is the canonical field name; "Lane R"/"Lane L" are prose
shorthand only.** Two names for subtly different guarantees must not drift.

## 13. Four clocks — accepted, with `effective_at` narrowed

The four-clock separation is correct and each timestamp keeps one meaning.

**Refinement:** `effective_at` should be **derived, not stored per raw
observation**. It is a function of (evidence class, source evidence, policy
version) and storing it per row would create a mutable derived column that could
drift from its policy — the exact failure the architecture is trying to avoid.

* **STATIC_IDENTITY** — no `effective_at`; timeless by construction.
* **EVENT_DERIVED** — derived: `source_event_completed_at + L`. Store the
  immutable `source_event_completed_at`; derive the rest.
* **VERSIONED_HISTORICAL** — the **source timestamp is** `effective_at`; store it
  as received, since it is source evidence, not our inference.

**Authority rule:** where source evidence and reconstruction policy disagree,
**source evidence wins and the row is rejected if the policy cannot accommodate
it.** A policy may never override a timestamp the source actually published.

## 14. Reader and schema

**Reader — accepted.** Two distinct types, no boolean bypass, no implicit R→L
fallback. Strengthened: a Lane-R read must take `(evidence_class,
availability_rule_id, policy_version, cutoff)` as **required** arguments, and
feature construction should read **pre-certified reconstructed feature inputs**,
not generic rows — certification is the leakage checkpoint.

**Schema — trimmed.** Do not store all eight proposed fields.

Keep: `provenance_class`, `availability_basis`, `availability_rule_id`,
`reconstruction_policy_version`, `source_event_completed_at`,
`availability_source`. Derive `effective_at`. **Drop `availability_confidence`
entirely** (§15).

Placement: **append-only provenance tables keyed by reconstructed feature input**,
not columns on existing observation rows. Existing `observed_at`/`decided_at` are
untouched. A v18 migration would be a separate, independently reviewed change —
still **not authorized**.

## 15. `availability_confidence` — removed

Availability eligibility is **binary**: was it defensibly usable by the cutoff or
not? A graded confidence invites a "medium" row to slip in. Evidence *quality* is
a separate concern and belongs to the evidence grade (§10) and the G1 variant.
**Eligibility and quality must not share a field.**

## 16. Missingness

The design's "explicit missingness, never imputed" is right but incomplete: an
**absence indicator can itself leak era information** (e.g. "weather present" ⇒
2024+, which correlates with rule and roster regimes a model can exploit).

**Policy:** optional families are **excluded from the core 5-season baseline** and
evaluated only as **separate comparable-coverage variants**. Never a missingness
flag spanning a coverage regime change. Chronological validation alone does not
neutralise this.

## 17. Lane matrices (corrections to the design in bold)

### MLB

| Family | Lane | Evidence | Correction exposure | F1-R | 3-season F2 | 5-season F2 |
|---|---|---|---|---|---|---|
| Target anchor | R | VERSIONED (E1) | none | yes | yes | yes |
| Static identity (game/team) | R | STATIC | none | yes | yes | yes |
| Static identity (player person) | R | STATIC | none | yes | yes | yes |
| **Player team affiliation** | **L** | FORWARD_ONLY | — | no | no | no |
| Prior results (W/L, score) | R core | EVENT_DERIVED | low | yes | yes | yes |
| Rest / home-away / park / density | R core | EVENT_DERIVED | none | yes | yes | yes |
| Team rolling stats | R core (score-based) | EVENT_DERIVED | low | yes | yes | yes |
| **Pitcher rolling stats** | **R extended** | EVENT_DERIVED | **high (ER, K)** | yes, flagged | variant only | variant only |
| **Batter rolling stats** | **R extended** | EVENT_DERIVED | **high (H, BB)** | yes, flagged | variant only | variant only |
| Bullpen usage (prior games) | R extended | EVENT_DERIVED | medium | yes, flagged | variant only | variant only |
| Probable pitcher / lineup / roster | L | FORWARD_ONLY | — | no | no | no |
| **Weather** | R optional | VERSIONED (`_previous_day1`) | none | **only if ≥2024** | variant only | **no** |
| Sportsbook moneyline | R | VERSIONED (E1) | none | yes | yes | yes |
| Kalshi market | R | VERSIONED (E2?) | none | **blocked by G2** | pending G2 | pending G2 |
| Final result | — | LABEL_ONLY | — | label | label | label |

### NBA

| Family | Lane | Evidence | Correction exposure | F1-R | 3-season F2 | 5-season F2 |
|---|---|---|---|---|---|---|
| Target anchor | R | VERSIONED (E1) | none | yes | yes | yes |
| Static identity (game/team/person) | R | STATIC | none | yes | yes | yes |
| Prior results (W/L, score) | R core | EVENT_DERIVED | low | yes | yes | yes |
| Rest / density / home-away | R core | EVENT_DERIVED | none | yes | yes | yes |
| Team rolling stats | R core (score-based) | EVENT_DERIVED | low | yes | yes | yes |
| **Player rolling stats** | **R extended** | EVENT_DERIVED | **high (reb/ast/TO)** | yes, flagged | variant only | variant only |
| **Advanced rolling stats** | **R extended** | EVENT_DERIVED | **high (inherits)** | yes, flagged | variant only | variant only |
| Play-derived metrics | R extended | EVENT_DERIVED | medium | yes, flagged | variant only | variant only |
| Lineups / starters / injuries / roster | L | FORWARD_ONLY | — | **no** | no | no |
| Sportsbook moneyline | R | VERSIONED (E1) | none | yes | yes | yes |
| Kalshi market | R | VERSIONED (E2?) | none | blocked by G2 | pending G2 | pending G2 |
| Final result | — | LABEL_ONLY | — | label | label | label |

August-fetched March lineups are **never** March features. Confirmed prohibited.

## 18. Market price: anchor, baseline, and *optional* feature

Design decision (was ambiguous): **A + B, with C as an explicitly flagged
variant.** Historical snapshots are (A) target anchors and (B) the no-vig baseline
the model must beat. Using no-vig probability **as a predictive feature** is
permitted **only** as a separately reported variant, because a market-derived
feature makes the model partly a market-follower and changes what "beating the
market" means. **Closing prices remain prohibited as features.** Any snapshot later
than `T_cut` is rejected by construction (§6 step 5).

## 19. Reproducibility digests

A Lane-R corpus version is the digest over: source corpus fingerprint · static
identity map · availability policy version · cutoff policy · feature registry ·
target set · market snapshot evidence set · **G1 variant**. A change in **any**
input changes the corpus version. Rebuilds must be byte-identical given identical
inputs.

## 20. Populations

Training / hyperparameter selection / calibration / final holdout must be
**chronologically disjoint**; economic backtest runs on the market-anchored subset
of the holdout; live shadow is Lane-L only. Every experiment reports population
sizes, overlaps and exclusions.

## 21. F1-R pilot design

Smallest falsifying pilot: **one month per league, in a window with the strongest
archive** — proposed **May 2025** (5-minute odds history since 2022; Previous-Runs
weather since Jan 2024; both leagues in regular season).

Must prove: retrospective target construction via contemporaneous `commence_time`;
static identity by entity type; core vs extended event-derived variants;
correction sensitivity; optional weather; deterministic rebuild; **zero same-game
and zero future-game leakage**; explicit exclusion decomposition; economic
eligibility by grade.

**Budget (measured, no requests made): ~448 historical requests ≈ 4,480 credits**
for both leagues for one month.

Falsification criteria: any leakage detection, non-deterministic rebuild, or
inability to decompose exclusions fails the pilot.

## 22. Lane-L forward collection — endorsed, with priority

Endorsed, and **G1 makes it urgent**: forward collection is the only way to
measure the correction rate that currently bounds Lane R by assumption.

Priority order — **MLB:** schedules, probable pitchers, lineups, weather forecast,
market quotes, rosters/transactions. **NBA:** schedules, injuries, lineups/starters,
market quotes, roster state. Plus **result re-polling** for correction measurement
(the highest-value addition, and absent from the design's list).

Must not block Lane-R research.

## 23. Claims framework

| Claim | Lane-R core | Lane-R extended | Lane L / live |
|---|---|---|---|
| Provider capability | yes | yes | yes |
| Statistical predictive performance | yes | yes, flagged | yes |
| Calibration methodology | yes | yes, flagged | yes |
| Retrospective economic simulation | yes (grade-bounded) | yes, flagged | yes |
| **Realistic profitability** | **no** | **no** | yes (mature) |
| Live shadow / deployment performance | no | no | yes |

## 24. Documentation repairs applied

* Architecture doc: "formally proven" → honest language; core/extended split;
  entity-specific identity; anchoring algorithm; corrected credit table; weather
  `_previous_day1` rule and coverage limits; Odds terms; evidence grades;
  `availability_confidence` dropped; profitability reconciliation.
* Provenance doc: profitability-vs-simulation distinction stated identically.
* `PHASE_F_RESEARCH_PLAN.md`: stale present-tense blocks relabelled as historical
  snapshots so no future agent can act on superseded authorization text.

## 25. Open gates

| Gate | Unresolved fact | Blocks impl. | Blocks F1-R | Blocks F2 | Blocks training | Blocks calibration | Blocks EV backtest | Closure evidence |
|---|---|---|---|---|---|---|---|---|
| **G1** correction history | true correction rate/latency unknown | no | no | **yes for extended** | no (core) | no (core) | no (core) | forward re-poll measurement |
| **G2** Kalshi depth/inception | retention, timestamps, whether sports markets existed 3–5 seasons back | no | no | pending | no | no | **yes for Kalshi** | official docs + inception evidence |
| **G3** weather archive depth | most models Jan 2024; GFS-temp Mar 2021 | no | **yes if pre-2024** | **yes for 5-season** | no | no | no | documented coverage |
| **G4** pre-2022 decimal-derived odds | rounding error magnitude | no | no | no | no | no | **quantify for pre-2022** | sensitivity on rounding |
| **G5** provider-id reuse (new) | whether any provider reuses/retires ids | **yes** | **yes** | yes | yes | yes | yes | provider id-stability documentation |
| **G6** Odds terms at launch (new) | storage/research unaddressed in terms | no | no | no | no | no | no | legal review before public launch |

~~**G5 is the only gate that blocks implementation**~~ — **G5 was CLOSED on
2026-08-10** (`G5_PROVIDER_ID_STABILITY_REVIEW.md`). The requirement for provider
documentation of global non-reuse proved unattainable and mis-scoped; it is
replaced by a corpus-scoped, fail-closed, version-bound identity-consistency
audit. **No gate now blocks implementation**; G1–G4 and G6 continue to bound what
an implementation may CLAIM, not whether it may be built.

## 26. Citations (accessed 2026-08-10, no API call)

* The Odds API v4 historical guide — `the-odds-api.com/liveapi/guides/v4/`
* The Odds API Terms — `the-odds-api.com/terms-and-conditions.html`
* Kalshi historical candlesticks — `docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks`
* Open-Meteo Historical Forecast — `open-meteo.com/en/docs/historical-forecast-api`
* Open-Meteo Previous Runs — `open-meteo.com/en/docs/previous-runs-api`

Kalshi retention/inception and Open-Meteo publication delay are **not** documented
at these sources; both remain open rather than assumed.

## 27. Validation

Design/documentation only; **zero source files changed**.

```
git diff --check                     clean
ruff check .                         All checks passed
mypy . --no-incremental              Success: no issues found in 310 source files
pytest -q                            2386 passed, 2 skipped, 0 failed (507 s)
schema                               v17 (no migration, no v18)
protected artefacts                  7/7 byte-identical
documentation consistency            two stale present-tense status blocks relabelled
                                     as historical snapshots; cross-references added
staged / forbidden-artifact audit    4 files, all documentation; no db, ckpt, raw
                                     response, log, wheel, env or graphify output
provider data/API requests           NONE (official documentation read only)
```

---

## Verdict

**ACCEPTED WITH REPAIRS.**

The architecture is structurally sound: the four clocks are correctly separated,
Lane R/Lane L genuinely solve the blocker without weakening the strict lane or
fabricating availability, and the eligibility gates are right in principle. Six
repairs were required:

1. **G1** — "formally proven" was false for correction-sensitive fields; split
   into **core (proven)** and **extended (bounded assumption, flagged)**.
2. **Static identity** — accepted *per entity type*; player **affiliation** is not
   static and moves to Lane L.
3. **Training population** — defined, with cancellation/postponement bias declared.
4. **Anchoring** — the algorithm was circular; now driven by contemporaneous
   `commence_time` with bounded iteration and explicit rejection rules.
5. **Weather** — Previous Runs is day-granular and publication delay is
   undocumented; conservative `_previous_day1` rule adopted, weather excluded from
   the 5-season core.
6. **Provenance conflict** — retrospective economic *simulation* is permitted as
   research; *profitability claims* still require strict-forward evidence.

Also: credit budget recomputed **~38% lower** from real schedules; evidence grades
E0–E3 added; `availability_confidence` removed; Odds terms verified (silent on
storage and research; commercial use permitted where data is not the product).

**G5 has since been closed** (2026-08-10, `G5_PROVIDER_ID_STABILITY_REVIEW.md`), so
no gate blocks implementation. A separately authorized phase may implement the
reviewed architecture, subject to its own independent review. Nothing here or
there authorizes F1-R, F2, production matching or training.

**Schema v18 (2026-08-11): provenance FOUNDATION implemented, not yet reviewed.**
`RETROSPECTIVE_PIT_SCHEMA_V18_IMPLEMENTATION.md`. Migration `f018` adds five
append-only tables (reconstruction corpus versions, identity audit records,
identity audit findings, static crosswalk provenance, reconstructed input
provenance) plus the `sports_quant.retrospective` domain vocabulary, a
code-defined digest-bound availability-rule registry, and narrow repositories.
`availability_confidence` is not stored (removed by review) and `effective_at` is
derived, never materialized. Strict PIT is unchanged: the v18 tables are
`unsupported` joins, `AsOfReader` has no retrospective mode, and `_feature_cutoff`
is byte-identical to its v17 source.

**Identity-audit engine implemented 2026-08-12 — NOT independently reviewed.**
`RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_IMPLEMENTATION.md`. The production
corpus-scoped G5 audit now runs against real evidence and independently
reproduces the reviewed one-month counts (MLB 400 games / 30 teams / 1,053
persons; NBA 239 / 30 / 550; **zero collisions**), read-only and offline. Player
static crosswalks are generated (1,053 and 550); **team and game crosswalks are
BLOCKED** — canonical `teams` is pre-seeded from names under UNIQUE constraints,
so a provider-keyed franchise cannot be bootstrapped and reusing a seed would
require name matching, which G5 forbids as identity evidence. Reported as a
blocker rather than forced. Also recorded: `birth_date` is absent for **every**
person in both corpora, so person-collision detection had no secondary evidence.
One month is still **not** evidence of 3–5 season stability.

Still unimplemented: `RetrospectiveResearchReader`, historical odds/market
anchoring, team/game crosswalks. Still unauthorized: **F1-R**, **F2**, production
matching, model training. G1/G2/G3/G4/G6 unchanged.

**Identity-audit engine REVIEWED 2026-08-12 — ACCEPTED WITH REPAIRS AND A
RETAINED BLOCKER.** `RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_INDEPENDENT_REVIEW.md`.
Ten defects proven and repaired; audit policy bumped to `g5-identity-audit-v2`.
Two were fail-open holes in the G5 contract itself (a game id reused across a
doubleheader read as clean; any generation string but `unverified` counted as
VERIFIED), and one let the CLI write provenance into the corpus being audited.
Detection power is now recorded on every audit, which changes how the one-month
result must be read: **no game id in either corpus was observed more than once**,
so the game audit compared nothing, and `birth_date` is absent for every person,
so within-league person reuse is undetectable. `ACCEPTED` means "no contradiction
detected at this policy's detection power" — **not** "verified stable identity".
Player crosswalks accepted; **team and game crosswalks remain BLOCKED** (Option A
ruled out on evidence: the canonical team seed carries no official provider id).
**The reader must not begin** until that architecture is separately decided.

**Team/game crosswalk architecture DECIDED 2026-08-12 — awaiting independent
review.** `RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE.md`. Chosen **TEAM-A**:
a source-controlled static attestation binding official provider franchise ids to
the **existing** canonical seed, with **no schema change** (stays v19). The
distinction that unblocks it is *when* labels are read — a one-time, reviewed,
source-controlled attestation answers "which franchise does this provider
franchise id denote", whereas forbidden runtime matching asks "which team was this
row probably about". Alternatives rejected on measurement: TEAM-B would move 13
FK-bearing tables and every deterministic `tm_*` id; TEAM-C would create a second
franchise dimension and split Lane-R from Lane-L; TEAM-D2 fails because strict PIT
gates `entity_match_decisions` on wall-clock `decided_at`, recreating the original
blocker. Diagnostic: **60/60 franchises uniquely attested and corroborated by a
second attribute, 33/33 historical aliases correct, 639/639 games ready** — and
`games` already carries a UNIQUE `(official_provider, official_game_key)` index,
so game bootstrap needs no schema work either.

**Nothing was implemented.** Team and game crosswalks remain BLOCKED in code, the
reader remains unimplemented, and **F1-R, F2, production matching and model
training remain unauthorized.** The architecture decision itself still requires
independent review. G1/G2/G3/G4/G6 unchanged.

**TEAM-A architecture REVIEWED 2026-08-12 — ACCEPTED WITH REPAIRS.**
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE_INDEPENDENT_REVIEW.md`. The TEAM-A
choice stands, but six design claims were proven false. Two are load-bearing:
**the corpus map digest does NOT bind the crosswalk** (v19 accepts a crosswalk
contradicting the committed map — the database cannot enforce agreement with an
external artifact), and **the curation uniqueness rule contradicted the many→one
rule** (the correct invariant is provider-key functional uniqueness; canonical-
target injectivity is NOT required, and a test that pinned the observed 30↔30
shape as policy has been repaired). Also: the enforced game key
`UNIQUE(official_provider, official_game_key)` carries **neither league nor
generation** → resolved as GAME-NAMESPACE-B, namespace-qualified provider values
(`balldontlie:nba:v1`), which needs no migration; the crosswalk digest captures the
conclusion, not the curation evidence; **no canonical-team seed digest exists**, so
a later seed edit would silently change an old corpus's meaning; and "independent
attribute" overstated corroboration — TEAM-A curates *denotation* and does **not**
prove provider-id permanence.

Schema verdict: **V19 SUFFICIENT WITH ADDITIONAL CODE INVARIANTS** — no migration,
but map-membership and seed-versioning enforcement must be added in code and CI,
and are weaker than the DB-enforced G5 bindings.

**TEAM-A implementation may be separately authorized. The reader remains BLOCKED**
until that implementation is itself independently reviewed. F1-R, F2, production
matching and model training remain unauthorized. G1/G2/G3/G4/G6 unchanged.

**TEAM-A IMPLEMENTED 2026-08-12 — NOT independently reviewed.**
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION.md`. The committed 60-entry
attestation map, deterministic team static-crosswalk generation, official-provider
canonical-game bootstrap, and the RV1/RV3/RV5 code+CI invariants are in place.
**Schema stays v19** (19 migrations, no new migration, f018/f019 untouched).
Reproduced read-only over both protected corpora with **0 provider requests** and
byte-identical protected artefacts (MLB 2026-06: 30 teams, 400 games; NBA 2026-03:
30 teams, 239 games). RV1 map-membership enforcement is a **detective** control:
CI proves a contradicting crosswalk is caught, but direct SQL can still write one,
so it is weaker than the DB-enforced G5 bindings. **The reader remains BLOCKED**,
and F1-R, F2, production matching, model training and feature engineering remain
unauthorized. G1/G2/G3/G4/G6 unchanged.

**TEAM-A implementation REVIEWED 2026-08-13 — ACCEPTED WITH REPAIRS.**
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION_INDEPENDENT_REVIEW.md` is
**authoritative where it differs from the implementation report**. Seven defects
were proven and repaired. Two were serious: **a canonical game could be created
with no persisted G5 audit at all** (the bootstrap trusted an in-memory object
claiming ACCEPTED), and **dry-run predicted the opposite of apply** for both new
entity types. Also: canonical games carried no corpus/audit provenance (now
written as v19 game static crosswalks — **GAME-PROV-C, no v20**); no convergence
with conventionally matched bare-provider games; an existing game with a
contradictory season was silently reused; the verifier never recomputed the
crosswalk semantic digest, so the "cryptographically bound to the map" claim was
unverified; and live-reference conflicts were not decision-backed. Completeness
semantics were split — the old check proved league-map materialization, not the
reviewed referenced-id contract. Schema verdict: **V19 SUFFICIENT WITH
ADDITIONAL REPAIRS**. Reproduced read-only over both corpora with **0 provider
requests** (MLB 2026-06: 30 teams, 400 games, 400 game provenance rows; NBA
2026-03: 30 teams, 239 games, 239 rows), dry run matching apply on both.
**The reader may now be separately authorized**; it was NOT started. F1-R, F2,
production matching, model training and feature engineering remain unauthorized.
G1/G2/G3/G4/G6 unchanged.

**RetrospectiveResearchReader IMPLEMENTED 2026-08-13 — NOT independently
reviewed.** `RETROSPECTIVE_RESEARCH_READER_IMPLEMENTATION.md`. The Lane-R reader
(architecture §12) is a **distinct type**, not a flag: no `ignore_pit=`-style
bypass exists on either reader, `_feature_cutoff` is byte-identical, and
`AsOfReader` gained nothing. **Schema stays v19** — no migration. FORWARD_ONLY
families (lineups, injuries, rosters, probable pitchers) are refused
**structurally**, before any database access, at any cutoff, even with a valid
certification present. Admission requires a **persisted** v19 certification for
the exact corpus/namespace/target/family; `effective_at` is **derived on read**
(STATIC_IDENTITY timeless, EVENT_DERIVED completion + digest-bound rule lag,
VERSIONED_SNAPSHOT provider stamp) and gated `<= T_cut`. Three gates were found
comparing a stored `str` to an enum with `is` and **failing open** — EXCLUDED
certifications admitted, strict-forward corpora readable, extended evidence
reported as core — all three repaired with regression tests. Real evidence,
read-only, **0 provider requests**: exactly one family (`static_identity`) is
admitted per corpus; `prior_results` was correctly **refused** because both
corpora are collection-time-observed, which is the very leak Lane R exists to
prevent. **F1-R, historical odds/market anchoring, F2, production matching,
feature engineering, model training, calibration, backtesting, recommendation
output and UI remain UNAUTHORIZED.** G1/G2/G3/G4/G6 unchanged.

**Lane-R reader INDEPENDENTLY REVIEWED 2026-08-13 — ACCEPTED WITH REPAIRS,
RETAINED DATA BLOCKER for F1-R.**
`RETROSPECTIVE_RESEARCH_READER_INDEPENDENT_REVIEW.md`. Two defects reproduced and
repaired: **(high)** a tampered crosswalk canonical target was ADMITTED and
`static_identity()` returned the wrong canonical id, because the reader never
recomputed the crosswalk's own semantic digest — now checked in-band on every
identity read; **(moderate)** the admission API silently ignored the namespace
`entity_type` — now required to be GAME. Seventeen further falsification attempts
(corpus/namespace/family substitution, rule-digest and rule-id tampering,
malformed timestamps, hostile family names, label relabelling, live-reference
identity) all failed to break it. Strict-forward PIT unweakened; **schema stays
v19**, no migration. **RETAINED BLOCKER:** no event-completion instant exists in
either bounded corpus (`game_status_history` empty in both), so **EVENT_DERIVED
is data-blocked** and F1-R may not yet be authorized to produce it. The safe next
step is a read-only investigation of whether preserved `raw_responses` carry a
usable completion timestamp. F1-R, odds/market anchoring, F2, production
matching, feature engineering, model training, calibration, backtesting,
recommendation output and UI remain UNAUTHORIZED. G1/G2/G3/G4/G6 unchanged.





**This foundation has NOT been independently reviewed.** Still unimplemented:
`RetrospectiveResearchReader`, the identity-audit engine, and historical
odds/market anchoring. Still unauthorized: **F1-R**, **F2**, production matching,
and model training. G1/G2/G3/G4/G6 are unchanged.

**REVIEWED 2026-08-11 — ACCEPTED WITH REPAIRS; the foundation is now schema v19.**
`RETROSPECTIVE_PIT_SCHEMA_V18_INDEPENDENT_REVIEW.md`. Eight defects proven (each
reproduced through the repository API *and* direct SQL), two material to the G5
contract: a static crosswalk could cite an ACCEPTED audit taken over a **different
source corpus** (a one-month audit vouching for five seasons — the exact transfer
G5 §16 forbids), and an ACCEPTED audit could later acquire a **blocking
identity_collision** finding while crosswalks built from it survived. Also repaired:
eligible inputs needed no source-evidence pointer; `source_evidence_table` accepted
any string; timestamps were shape-only (month 99, Feb 30, lower-case `z`, offsets
all stored); cross-league supersession was repository-only; credential-shaped and
non-JSON values passed the finding screen; and the package was import-order
dependent. Repairs ship as migration **`f019`** — `f018` is preserved byte-for-byte
as applied evidence and was **not** edited. Strict PIT re-proved behaviourally:
`AsOfReader` and `_feature_cutoff` unchanged, Lane-R tables still `unsupported`
joins, late-observed lineups still invisible.

Confirmations: no implementation occurred; **no provider data/API request** was
made; protected evidence **7/7 byte-identical**; schema **v17**; F1-R, F2,
production matching and model training all remain **unauthorized**.
