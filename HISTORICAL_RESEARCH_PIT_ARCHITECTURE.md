# Historical research point-in-time architecture (design only)

**Status (2026-08-12) — the single authoritative status line for this document.**
Implemented and independently reviewed: the provenance foundation at schema
**v19** (`f018` + `f019`). Implemented and reviewed with repairs: the
**corpus-scoped G5 identity-audit engine** (audit policy `g5-identity-audit-v2`,
`RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_INDEPENDENT_REVIEW.md`), including **player**
static crosswalks. Still **not implemented**: `RetrospectiveResearchReader`,
historical odds/market anchoring, and **team/game** crosswalks — whose
architecture is now **decided (TEAM-A) but not yet independently reviewed**, so
they remain blocked in code. **F1-R, F2, production matching and model training
remain unauthorized.** Everything else in this document is design only.

> Older status blocks below are **historical snapshots** kept for provenance.
> Where any of them disagrees with the line above, the line above is current.

Replacement contract for historical model research after the blocker established
in `F1_HISTORICAL_PIT_FEASIBILITY_REVIEW.md` (`dc090af`). It extends
`RECONSTRUCTED_CORPUS_PROVENANCE.md`, which already anticipated this problem and
defined the three provenance classes; that document's `strict_forward_pit` /
`reconstructed_research` / `label_only_retrospective` map onto Lane L / Lane R /
LABEL_ONLY here and remain authoritative for the separation guarantees.

**Verdict: ARCHITECTURE READY FOR INDEPENDENT REVIEW**, with four enumerated open
gates (§13) the F1-R pilot must close.

> **REVIEWED 2026-08-10 — ACCEPTED WITH REPAIRS.**
> `HISTORICAL_RESEARCH_PIT_ARCHITECTURE_INDEPENDENT_REVIEW.md` is **authoritative
> where it differs from this document**. Six repairs:
> 1. **"Formally proven" was false** for correction-sensitive fields. Lane R splits
>    into **core** (immutable facts — proven) and **extended** (correction-sensitive
>    box-score detail — bounded by assumption + sensitivity, flagged, never called
>    transaction-time-exact PIT). Pitcher/batter/player/advanced rolling stats are
>    **extended**, not core.
> 2. **Static identity is per entity type.** Game and team ids accepted (id equality
>    only, never name); player **person** accepted but **team affiliation is NOT
>    static** and moves to Lane L.
> 3. **Training population defined**, with cancellation/postponement bias declared.
> 4. **Anchoring was circular.** `T_cut` now derives from the snapshot's
>    **contemporaneous `commence_time`**, with bounded iteration and explicit
>    rejection; the retrospective final start is only a search hint.
> 5. **Weather is weaker than assumed.** Previous Runs is **day-granular** and
>    publication delay is **undocumented**; use the conservative `_previous_day1`
>    (24 h lead) rule. Weather is **excluded from the 5-season core**.
> 6. **Provenance conflict resolved:** retrospective economic *simulation* is
>    permitted as research evidence; *profitability claims* still require
>    strict-forward/live evidence.
>
> Also: credit budget **recomputed ~38% lower** from real schedules; evidence grades
> **E0–E3** added; **`availability_confidence` removed** (eligibility is binary);
> Odds API **Terms verified** (silent on storage/research; commercial use permitted
> where the data is not the product). New gates **G5** (provider-id stability —
> then the only gate blocking implementation — see the correction below) and **G6**
> (terms review before launch).
>
> **G5 (2026-08-10): CLOSED — corpus-scoped fail-closed contract.**
> `G5_PROVIDER_ID_STABILITY_REVIEW.md`. The original criterion (provider
> documentation guaranteeing global permanent non-reuse) is **unattainable**
> (BALLDONTLIE is silent on uniqueness/permanence/reuse; MLB StatsAPI documentation
> is login-gated) and **mis-scoped** — Lane R never dereferences an ID outside its own
> corpus. Replaced by a nine-point contract keyed on
> `(league, provider, entity_type, provider_id)`: every observation of an ID within
> the reconstruction corpus must be compatible with one canonical entity; any
> incompatibility **fails closed** on a severity ladder; display name, team
> affiliation and outcome data are never identity evidence; the manifest binds the
> identity-audit digest and API namespace generation. The audit ran clean on real
> evidence (MLB 400 games / 30 teams / 1,053 players; NBA 239 / 30 / 550; **zero
> collisions**, with 1,044 and 549 player ids observed more than once). **One month
> does not prove 3–5 season stability** — the same audit must be re-run over the full
> F2 source window, and collision-free status is a **corpus property, version-bound**;
> later evidence yields a new corpus version and never rewrites the old one.
>
> **No gate now blocks implementation.**
>
> **Schema v18 (2026-08-11): provenance FOUNDATION implemented, not yet reviewed.**
> `RETROSPECTIVE_PIT_SCHEMA_V18_IMPLEMENTATION.md`. Migration `f018` adds five
> append-only tables (reconstruction corpus versions, identity audit records,
> identity audit findings, static crosswalk provenance, reconstructed input
> provenance) plus the `sports_quant.retrospective` domain vocabulary, a
> code-defined digest-bound availability-rule registry, and narrow repositories.
> `availability_confidence` is not stored (removed by review) and `effective_at` is
> derived, never materialized. Strict PIT is unchanged: the v18 tables are
> `unsupported` joins, `AsOfReader` has no retrospective mode, and `_feature_cutoff`
> is byte-identical to its v17 source.
>
> **Identity-audit engine implemented 2026-08-12 — NOT independently reviewed.**
> `RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_IMPLEMENTATION.md`. The production
> corpus-scoped G5 audit now runs against real evidence and independently
> reproduces the reviewed one-month counts (MLB 400 games / 30 teams / 1,053
> persons; NBA 239 / 30 / 550; **zero collisions**), read-only and offline. Player
> static crosswalks are generated (1,053 and 550); **team and game crosswalks are
> BLOCKED** — canonical `teams` is pre-seeded from names under UNIQUE constraints,
> so a provider-keyed franchise cannot be bootstrapped and reusing a seed would
> require name matching, which G5 forbids as identity evidence. Reported as a
> blocker rather than forced. Also recorded: `birth_date` is absent for **every**
> person in both corpora, so person-collision detection had no secondary evidence.
> One month is still **not** evidence of 3–5 season stability.
>
> Still unimplemented: `RetrospectiveResearchReader`, historical odds/market
> anchoring, team/game crosswalks. Still unauthorized: **F1-R**, **F2**, production
> matching, model training. G1/G2/G3/G4/G6 unchanged.
>
> **Identity-audit engine REVIEWED 2026-08-12 — ACCEPTED WITH REPAIRS AND A
> RETAINED BLOCKER.** `RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_INDEPENDENT_REVIEW.md`.
> Ten defects proven and repaired; audit policy bumped to `g5-identity-audit-v2`.
> Two were fail-open holes in the G5 contract itself (a game id reused across a
> doubleheader read as clean; any generation string but `unverified` counted as
> VERIFIED), and one let the CLI write provenance into the corpus being audited.
> Detection power is now recorded on every audit, which changes how the one-month
> result must be read: **no game id in either corpus was observed more than once**,
> so the game audit compared nothing, and `birth_date` is absent for every person,
> so within-league person reuse is undetectable. `ACCEPTED` means "no contradiction
> detected at this policy's detection power" — **not** "verified stable identity".
> Player crosswalks accepted; **team and game crosswalks remain BLOCKED** (Option A
> ruled out on evidence: the canonical team seed carries no official provider id).
> **The reader must not begin** until that architecture is separately decided.
>
> **Team/game crosswalk architecture DECIDED 2026-08-12 — awaiting independent
> review.** `RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE.md`. Chosen **TEAM-A**:
> a source-controlled static attestation binding official provider franchise ids to
> the **existing** canonical seed, with **no schema change** (stays v19). The
> distinction that unblocks it is *when* labels are read — a one-time, reviewed,
> source-controlled attestation answers "which franchise does this provider
> franchise id denote", whereas forbidden runtime matching asks "which team was this
> row probably about". Alternatives rejected on measurement: TEAM-B would move 13
> FK-bearing tables and every deterministic `tm_*` id; TEAM-C would create a second
> franchise dimension and split Lane-R from Lane-L; TEAM-D2 fails because strict PIT
> gates `entity_match_decisions` on wall-clock `decided_at`, recreating the original
> blocker. Diagnostic: **60/60 franchises uniquely attested and corroborated by a
> second attribute, 33/33 historical aliases correct, 639/639 games ready** — and
> `games` already carries a UNIQUE `(official_provider, official_game_key)` index,
> so game bootstrap needs no schema work either.
>
> **Nothing was implemented.** Team and game crosswalks remain BLOCKED in code, the
> reader remains unimplemented, and **F1-R, F2, production matching and model
> training remain unauthorized.** The architecture decision itself still requires
> independent review. G1/G2/G3/G4/G6 unchanged.
>
> **TEAM-A architecture REVIEWED 2026-08-12 — ACCEPTED WITH REPAIRS.**
> `RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE_INDEPENDENT_REVIEW.md`. The TEAM-A
> choice stands, but six design claims were proven false. Two are load-bearing:
> **the corpus map digest does NOT bind the crosswalk** (v19 accepts a crosswalk
> contradicting the committed map — the database cannot enforce agreement with an
> external artifact), and **the curation uniqueness rule contradicted the many→one
> rule** (the correct invariant is provider-key functional uniqueness; canonical-
> target injectivity is NOT required, and a test that pinned the observed 30↔30
> shape as policy has been repaired). Also: the enforced game key carries **neither
> league nor generation** → resolved as GAME-NAMESPACE-B, namespace-qualified
> provider values (`balldontlie:nba:v1`), needing no migration; the crosswalk digest
> captures the conclusion, not the curation evidence; **no canonical-team seed digest
> exists**; and "independent attribute" overstated corroboration — TEAM-A curates
> *denotation* and does **not** prove provider-id permanence.
>
> Schema verdict: **V19 SUFFICIENT WITH ADDITIONAL CODE INVARIANTS** — no migration,
> but map-membership and seed-versioning enforcement must be added in code and CI.
>
> **TEAM-A implementation may be separately authorized. The reader remains BLOCKED**
> until that implementation is itself independently reviewed. F1-R, F2, production
> matching and model training remain unauthorized. G1/G2/G3/G4/G6 unchanged.
>
> **TEAM-A IMPLEMENTED 2026-08-12 — NOT independently reviewed.**
> `RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION.md`. The committed 60-entry
> attestation map, deterministic team static-crosswalk generation, official-provider
> canonical-game bootstrap, and the RV1/RV3/RV5 code+CI invariants are in place.
> **Schema stays v19** (19 migrations, no new migration, f018/f019 untouched).
> Reproduced read-only over both protected corpora with **0 provider requests** and
> byte-identical protected artefacts (MLB 2026-06: 30 teams, 400 games; NBA 2026-03:
> 30 teams, 239 games). RV1 map-membership enforcement is a **detective** control:
> CI proves a contradicting crosswalk is caught, but direct SQL can still write one,
> so it is weaker than the DB-enforced G5 bindings. **The reader remains BLOCKED**,
> and F1-R, F2, production matching, model training and feature engineering remain
> unauthorized. G1/G2/G3/G4/G6 unchanged.
>
> **TEAM-A implementation REVIEWED 2026-08-13 — ACCEPTED WITH REPAIRS.**
> `RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION_INDEPENDENT_REVIEW.md` is
> **authoritative where it differs from the implementation report**. Seven defects
> were proven and repaired. Two were serious: **a canonical game could be created
> with no persisted G5 audit at all** (the bootstrap trusted an in-memory object
> claiming ACCEPTED), and **dry-run predicted the opposite of apply** for both new
> entity types. Also: canonical games carried no corpus/audit provenance (now
> written as v19 game static crosswalks — **GAME-PROV-C, no v20**); no convergence
> with conventionally matched bare-provider games; an existing game with a
> contradictory season was silently reused; the verifier never recomputed the
> crosswalk semantic digest, so the "cryptographically bound to the map" claim was
> unverified; and live-reference conflicts were not decision-backed. Completeness
> semantics were split — the old check proved league-map materialization, not the
> reviewed referenced-id contract. Schema verdict: **V19 SUFFICIENT WITH
> ADDITIONAL REPAIRS**. Reproduced read-only over both corpora with **0 provider
> requests** (MLB 2026-06: 30 teams, 400 games, 400 game provenance rows; NBA
> 2026-03: 30 teams, 239 games, 239 rows), dry run matching apply on both.
> **The reader may now be separately authorized**; it was NOT started. F1-R, F2,
> production matching, model training and feature engineering remain unauthorized.
> G1/G2/G3/G4/G6 unchanged.
>
> **RetrospectiveResearchReader IMPLEMENTED 2026-08-13 — NOT independently
> reviewed.** `RETROSPECTIVE_RESEARCH_READER_IMPLEMENTATION.md`. The Lane-R reader
> (architecture §12) is a **distinct type**, not a flag: no `ignore_pit=`-style
> bypass exists on either reader, `_feature_cutoff` is byte-identical, and
> `AsOfReader` gained nothing. **Schema stays v19** — no migration. FORWARD_ONLY
> families (lineups, injuries, rosters, probable pitchers) are refused
> **structurally**, before any database access, at any cutoff, even with a valid
> certification present. Admission requires a **persisted** v19 certification for
> the exact corpus/namespace/target/family; `effective_at` is **derived on read**
> (STATIC_IDENTITY timeless, EVENT_DERIVED completion + digest-bound rule lag,
> VERSIONED_SNAPSHOT provider stamp) and gated `<= T_cut`. Three gates were found
> comparing a stored `str` to an enum with `is` and **failing open** — EXCLUDED
> certifications admitted, strict-forward corpora readable, extended evidence
> reported as core — all three repaired with regression tests. Real evidence,
> read-only, **0 provider requests**: exactly one family (`static_identity`) is
> admitted per corpus; `prior_results` was correctly **refused** because both
> corpora are collection-time-observed, which is the very leak Lane R exists to
> prevent. **F1-R, historical odds/market anchoring, F2, production matching,
> feature engineering, model training, calibration, backtesting, recommendation
> output and UI remain UNAUTHORIZED.** G1/G2/G3/G4/G6 unchanged.
>
> **Lane-R reader INDEPENDENTLY REVIEWED 2026-08-13 — ACCEPTED WITH REPAIRS,
> RETAINED DATA BLOCKER for F1-R.**
> `RETROSPECTIVE_RESEARCH_READER_INDEPENDENT_REVIEW.md`. Two defects reproduced and
> repaired: **(high)** a tampered crosswalk canonical target was ADMITTED and
> `static_identity()` returned the wrong canonical id, because the reader never
> recomputed the crosswalk's own semantic digest — now checked in-band on every
> identity read; **(moderate)** the admission API silently ignored the namespace
> `entity_type` — now required to be GAME. Seventeen further falsification attempts
> (corpus/namespace/family substitution, rule-digest and rule-id tampering,
> malformed timestamps, hostile family names, label relabelling, live-reference
> identity) all failed to break it. Strict-forward PIT unweakened; **schema stays
> v19**, no migration. **RETAINED BLOCKER:** no event-completion instant exists in
> either bounded corpus (`game_status_history` empty in both), so **EVENT_DERIVED
> is data-blocked** and F1-R may not yet be authorized to produce it. The safe next
> step is a read-only investigation of whether preserved `raw_responses` carry a
> usable completion timestamp. F1-R, odds/market anchoring, F2, production
> matching, feature engineering, model training, calibration, backtesting,
> recommendation output and UI remain UNAUTHORIZED. G1/G2/G3/G4/G6 unchanged.
>
> **This foundation has NOT been independently reviewed.** Still unimplemented:
> `RetrospectiveResearchReader`, the identity-audit engine, and historical
> odds/market anchoring. Still unauthorized: **F1-R**, **F2**, production matching,
> and model training. G1/G2/G3/G4/G6 are unchanged.
>
> **REVIEWED 2026-08-11 — ACCEPTED WITH REPAIRS; the foundation is now schema v19.**
> `RETROSPECTIVE_PIT_SCHEMA_V18_INDEPENDENT_REVIEW.md`. Eight defects proven (each
> reproduced through the repository API *and* direct SQL), two material to the G5
> contract: a static crosswalk could cite an ACCEPTED audit taken over a **different
> source corpus** (a one-month audit vouching for five seasons — the exact transfer
> G5 §16 forbids), and an ACCEPTED audit could later acquire a **blocking
> identity_collision** finding while crosswalks built from it survived. Also repaired:
> eligible inputs needed no source-evidence pointer; `source_evidence_table` accepted
> any string; timestamps were shape-only (month 99, Feb 30, lower-case `z`, offsets
> all stored); cross-league supersession was repository-only; credential-shaped and
> non-JSON values passed the finding screen; and the package was import-order
> dependent. Repairs ship as migration **`f019`** — `f018` is preserved byte-for-byte
> as applied evidence and was **not** edited. Strict PIT re-proved behaviourally:
> `AsOfReader` and `_feature_cutoff` unchanged, Lane-R tables still `unsupported`
> joins, late-observed lineups still invisible.

---

## 1. Problem statement

Measured, not assumed:

* 239/239 NBA March and 400/400 MLB June games had their first schedule
  observation **after** the game's scheduled start.
* `observed_at` everywhere is system acquisition time; `decided_at` is matcher
  wall-clock.
* Bounded matching succeeded completely (15/15 NBA, 24/24 MLB) and
  `build_historical_dataset` still returned **0 rows** — every exclusion the
  `_feature_cutoff` schedule gate, none from identity or labels.

The strict builder is correct. It refuses to pretend retrospectively downloaded
data was historically known. What it cannot do is support retrospective research,
because it demands that *Moneymaker itself* held the evidence before the cutoff.

The replacement must permit defensible retrospective research **without**
manufacturing availability, and **without** requiring that Moneymaker had been
running for years.

## 2. The four-clock model

One timestamp per meaning. Never overload.

| Clock | Question | Where it lives today | Change |
|---|---|---|---|
| **`ingested_at` / audit** | when did Moneymaker acquire or process this? | `observed_at`, `received_at`, `decided_at`, `created_at` | **unchanged, never backdated** |
| **fact / event time** | when did the event occur or the fact become true? | `scheduled_start`, `game_date_local`, result content | unchanged |
| **`effective_at` / availability** | earliest time a research process may defensibly use this | **does not exist** | new, Lane-R only, derived from documented rules |
| **decision cutoff `T_cut`** | the simulated betting decision instant (initially T−60) | dataset builder | unchanged concept, new derivation for Lane R |

`observed_at` and `decided_at` keep their current meanings exactly. Availability
lives only in a separate field, and is derived from a versioned documented rule —
never guessed, never written into `observed_at`.

## 3. Two research lanes

### Lane R — Retrospective Core

Built after the fact, admitting only inputs **formally proven to contain no
information whose semantic availability is after `T_cut`**.

The governing rule is *not* "Moneymaker downloaded it before `T_cut`". It is:

> Every input to the feature is a function of events completed before `T_cut`, or
> of an immutable identity, or of a source snapshot whose own timestamp is
> `<= T_cut`.

**Strength of that guarantee differs by field (review repair 1).** It is *proven*
for immutable facts (win/loss, final score, date, home/away, rest, venue) — **Lane-R
core**. For correction-sensitive box-score detail it is only *bounded by documented
assumption and sensitivity analysis*, because a post-cutoff correction is
undetectable in a single-version corpus — **Lane-R extended**. Extended results may
never be described as transaction-time-exact PIT, and the two must be reported as
separate feature-set variants.

Permits: predictive-model training, calibration methodology, relative feature
value, and — where a genuine market snapshot exists — economic backtest.

### Lane L — Live / Enhanced

Mutable near-game state that cannot be reconstructed without a genuine versioned
snapshot: injuries, lineups, scratches, probable/confirmed pitchers, bullpen
availability, late transactions, live weather forecast where no archive exists.

Lane L is **forward-collected only**. Its availability evidence is intrinsic —
the row was received before the cutoff by construction, exactly today's strict
contract. Lane-L availability is never backfilled from final state.

The lanes are physically separated per `RECONSTRUCTED_CORPUS_PROVENANCE.md` §3:
separate corpora, no silent union, manifest records the lane. **The existing
strict builder is not weakened** — Lane R gets its own builder.

## 4. Availability-evidence taxonomy

| Class | Definition | Retrospective? |
|---|---|---|
| **STATIC_IDENTITY** | immutable provider-id ↔ canonical-entity relation | **yes**, under §5 |
| **EVENT_DERIVED** | statistic computed solely from events completed before `T_cut` | **yes**, under §7 |
| **VERSIONED_HISTORICAL** | source supplies real timestamped snapshots/quotes | **yes**, timestamp is the evidence |
| **FORWARD_ONLY** | no trustworthy retrospective availability evidence | **no** — Lane L |
| **LABEL_ONLY** | known only after the event; permitted solely as target `y` | label only |

## 5. Static identity contract

A stable official provider ID may map to a canonical entity even though the
matcher ran later, **iff all hold**:

1. the provider is the league's **designated official** source for that entity
   type (`OFFICIAL_PROVIDER_BY_LEAGUE`);
2. the stable ID appears **directly in the historical source row** being used;
3. the crosswalk uses **no outcome- or future-sensitive information** — stable ID
   equality only, never name similarity, never a later roster/statistic;
4. no ambiguous provider-ID collision exists for that ID;
5. provenance records the **real** curation time honestly.

Justification: "MLB `gamePk` 822728 is this game" is a timeless proposition that
cannot encode the outcome. Admitting it retrospectively leaks nothing.

**In Lane R, static crosswalks are not time-gated** — a timeless fact has no
knowledge-time. In Lane L they remain gated exactly as today.

`decided_at` stays **audit time** and is not reused as effective identity time. A
static crosswalk needs a distinct class marker, not a backdated decision. This is
a genuine relaxation and must be independently reviewed on its own merits — it is
explicitly **not** justified by coverage, and by itself it does **not** unblock
F1 (the schedule gate is independent of identity).

## 6. Target-game anchoring

The Lane-R replacement for "Moneymaker saw the schedule before tip-off" is a
**genuine historical market snapshot**:

A game enters the Lane-R backtest set iff:

* a historical sportsbook/exchange market existed for the exact event;
* the **snapshot timestamp is `<= T_cut`**;
* the quote event maps unambiguously to the official game (via §5);
* the scheduled start is represented consistently between quote and schedule;
* the event had **not already started** at quote time.

This proves the matchup existed and was tradeable at the simulated cutoff — much
stronger evidence than local acquisition time, and it is the *market's* own
timestamp, not ours.

**Applies to Lane R only. The Lane-L schedule gate is unchanged.**

Where no market snapshot exists, the game may still be **training-eligible**
(§10) if its features are EVENT_DERIVED/STATIC_IDENTITY and the label is settled,
but it is **not** economic-backtest eligible. These gates never collapse.

## 7. Event-derived feature contract

The core route to a deep historical corpus without waiting years.

Rules:

1. Enumerate source games **strictly before** `T_cut` for the entity.
2. Each source game must be **final**.
3. Apply an explicit **availability lag** `L` after source-game completion before
   its statistics may be used (§8).
4. **Derive rolling statistics ourselves from per-game rows.** Never consume a
   season-to-date aggregate returned by an endpoint — such an aggregate may
   silently include the target game or later games. This is a hard prohibition.
5. Never include any target-game statistic.
6. The input set must be **formally bounded** and recorded in the manifest.

Eligible: rolling win/loss, run/point differential, offensive/defensive rates,
pace/possessions, opponent-adjusted metrics, Elo, rest days, days since previous
game, schedule density, home/away, prior-game boxscore and play-derived
aggregates, pitcher/batter and player rolling statistics.

## 8. Corrections and hindsight — the honest gap

**Measured:** in both retrospective corpora, every game has **exactly one** result
version (NBA 239 rows / 239 anchors; MLB 400 / 400; zero anchors with multiple
versions). A retrospective fetch captured only the **current, possibly already
corrected** value.

Therefore: **the value a prior game's statistics held at an earlier `T_cut` cannot
be reconstructed from this corpus.** If a prior game's record was corrected after
the target's cutoff, using today's corrected value is genuine hindsight leakage,
and the corpus contains no evidence to detect or undo it.

Policy (conservative, and explicitly a limitation rather than a solution):

* Apply an availability lag `L` after source-game completion (proposal: **L = 24 h**
  for MLB/NBA box scores) before a source game's statistics are usable, absorbing
  the common same-night stat-correction window.
* Run a **mandatory sensitivity analysis** across `L ∈ {0, 6 h, 24 h, 72 h}` and
  report how conclusions move. A result that only survives at `L = 0` is not a
  result.
* **Forward Lane-L collection must measure the real correction rate and latency
  distribution.** Until then the residual is bounded by assumption, not by
  evidence — this must be stated in every claim built on Lane R.
* Where a family's correction behaviour proves material and unmeasurable, exclude
  it from Lane R rather than model around it.

## 9. Source findings (official documentation; **no provider API request was made**)

### The Odds API — historical odds **(verified)**
* Endpoints: `/v4/historical/sports/{sport}/odds`,
  `/v4/historical/sports/{sport}/events/{eventId}/odds`,
  `/v4/historical/sports/{sport}/events`.
* Depth: **from 2020-06-06**. Snapshots **10-minute** intervals before
  2022-09-18, **5-minute** from then on.
* `date` returns **the closest snapshot at or earlier than** the requested
  timestamp — precisely the `<= T_cut` semantic §6 needs.
* Cost: historical is **10× normal**, `10 × markets × regions` per request.
* `h2h` (moneyline) available throughout the archive.
* Caveat: before 2022-09-18 only **decimal** odds were captured; American odds are
  derived and may carry rounding error.
* No stated retention/licensing restriction on the page reviewed.

**Estimated request/credit budget** (h2h only, one region, one snapshot per
distinct game-start-time per day; the snapshot endpoint returns all events at that
instant, so cost scales with distinct start times, not with games):

| Scope | Est. requests | Est. credits (×10) |
|---|---|---|
| One month proof (MLB + NBA) | ~710 | **~7,100** |
| One MLB season (~15 start-times/day × ~186 d) | ~2,790 | ~27,900 |
| One NBA season (~8 × ~170 d) | ~1,360 | ~13,600 |
| One combined season | ~4,150 | ~~41,500~~ → **25,780 (measured)** |
| Three seasons | ~12,450 | ~~124,500~~ → **77,340 (measured)** |
| Five seasons | ~20,750 | ~~207,500~~ → **128,900 (measured)** |

**Superseded by measurement.** The review recomputed these from the real schedules by flooring each game's `T-60` to the 5-minute grid and counting distinct buckets: NBA 5.0/day and MLB 9.3/day, giving 8,500 + 17,280 = **25,780 credits per combined season** and **4,480 for a one-month pilot**. The estimates above were ~38% too high.

Estimates, not quotes — derived from the documented 10× rule and typical schedule
density. The per-event endpoint would be far costlier and is not the plan. Five
seasons back from 2026 (2021–2025) sits entirely inside the 2020-06-06 archive.

### Kalshi — historical **(partially verified)**
* Dedicated historical tier:
  `/trade-api/v2/historical/markets/{ticker}/candlesticks`.
* `period_interval` ∈ **1 / 60 / 1440 minutes** — 1-minute granularity is ample
  for T−60 anchoring.
* Fields: `yes_bid`, `yes_ask`, `price {open, high, low, close}`, `volume`,
  `open_interest`.
* **Top-of-book and last-trade only — historical orderbook depth is NOT
  reconstructable.** Depth-dependent execution modelling is out of scope.
* **UNVERIFIED:** retention depth, exact timestamp semantics, rate limits,
  authentication for historical reads, and from what date single-game MLB/NBA
  markets exist. **Open gate G2.**

### Open-Meteo — historical weather **(verified, with a critical distinction)**
* **Historical Forecast API stitches the first hours of successive runs** into a
  continuous timeline. That is near-nowcast quality — it is **NOT** the forecast
  as it stood 60 minutes before a game, and using it as a pregame feature would be
  close to using observed weather. **Prohibited as a pregame feature.**
* A genuine archived pregame forecast requires:
  * **Previous Runs API** — fixed lead-time offsets (1–7 days), from **Jan 2024**
    (GFS from **Mar 2021**, JMA from 2018); or
  * **Single Runs API** — full horizon for a specific initialization via `run=`,
    ECMWF IFS HRES from **Mar 2024**, others from Apr 2026.
* Variables available: temperature, wind speed/direction/gusts, precipitation,
  visibility.
* Consequence: defensible pregame weather is available roughly **2021+ (GFS)** or
  **2024+ (ECMWF)** — shallower than the odds archive. Weather is therefore an
  **optional** Lane-R family with an explicit missingness flag on older seasons,
  never imputed.

### Sportradar / SportsDataIO — alternatives **(not evaluated in depth)**
Pricing and change-log/versioning guarantees are not publicly documented and
require sales contact. **Recommendation: do not engage now.** Only reconsider if a
core-model experiment demonstrates that a FORWARD_ONLY family is likely worth the
cost. Default is to minimize new paid providers.

## 10. Three eligibility gates — never collapsed

| Gate | Requires |
|---|---|
| **Training** | label settled; every feature STATIC_IDENTITY / EVENT_DERIVED / VERSIONED_HISTORICAL with `effective_at <= T_cut`; no target-game statistics |
| **Calibration / validation** | training requirements **plus** a fixed chronological split (no shuffling, no future folds) |
| **Economic backtest** | calibration requirements **plus** a genuine market snapshot at `<= T_cut` (§6), and prices that are not closing prices |

A game may be training-eligible but backtest-ineligible. That asymmetry is
expected and must be reported, not hidden by dropping rows.

## 11. Proposed schema semantics (design only — no migration authorized)

Minimum additions, in **separate** columns/tables so they can never be confused
with `observed_at`, extending `RECONSTRUCTED_CORPUS_PROVENANCE.md` §2:

* `provenance_class` — `strict_forward_pit | reconstructed_research | label_only_retrospective`
* `effective_at` — the derived availability instant (Lane R only)
* `availability_basis` — `static_identity | event_derived | versioned_snapshot`
* `availability_source` — pointer to the documenting evidence
* `availability_confidence` — ordinal, with documented criteria
* `source_event_completed_at` — for EVENT_DERIVED, the bounding completion instant
* `availability_rule_id` + `reconstruction_policy_version`

Do **not** add anything derivable reproducibly from these. Do **not** mutate
historical `observed_at`. Do **not** overload `decided_at`. Any migration is a
separate, independently reviewed change — a schema bump to v18 would be required
and must not be bundled with a builder.

## 12. Reader / API architecture

Two readers sharing one leakage contract, differing only in admissible evidence:

* `StrictForwardReader` — today's `AsOfReader`, unchanged. Evidence:
  `observed_at`/`decided_at` only.
* `RetrospectiveResearchReader` — admits `effective_at` from the taxonomy above.

Requirements:

* **No `ignore_pit=True`-style bypass anywhere.** Lane selection is a distinct
  type, not a flag, so an unsafe read is a different object rather than an
  argument.
* A Lane-R reader must be unable to return a FORWARD_ONLY family at all.
* Feature manifests record **lane, availability basis, cutoff, source version,
  correction policy and sensitivity results**.
* `provider_*_references` remain forbidden direct PIT inputs in both lanes.

## 13. Open gates

* **G1 — correction risk is bounded by assumption, not measurement.** No
  correction history exists in either corpus (§8). Forward collection must
  quantify it.
* **G2 — Kalshi historical retention/timestamps/market inception unverified.**
* **G3 — weather archive depth (2021 GFS / 2024 ECMWF) is shallower than the odds
  archive**; older seasons carry explicit weather missingness.
* **G4 — pre-2022-09-18 odds are decimal-derived**, with possible rounding error
  in American prices.

None blocks the core Lane-R design (identity + event-derived + odds anchoring +
labels). All must be closed or explicitly accepted by the F1-R pilot.

## 14. Revised F1 and F2

**F1 keeps its value, with its scope stated honestly.** The month pilots proved
provider depth, pagination, normalization, corrections, matching mechanics,
endpoint completeness and request budgeting. They were never capable of proving
historical feature availability and must not be asked to.

**F1-R (new, bounded, not executed here):** a retrospective-reconstruction pilot
over a short bounded period proving, end to end: Lane-R target anchoring from real
market snapshots; static identity; event-derived construction from prior games
only; archived-forecast availability; **zero future leakage**; reproducibility;
and the §8 sensitivity analysis.

**F2 redefined:** build the historical **Lane-R core corpus** — minimum three
seasons, target five — **only after F1-R passes**. F2 is no longer "backfill every
rich family"; families without retrospective availability evidence are out of
scope by construction.

**Forward Lane-L collection should begin as soon as the architecture is
implemented**, in parallel and non-blocking, to accumulate true pregame schedules,
injuries, lineups, probable pitchers, forecasts and market state — and to measure
the G1 correction rate.

## 15. Model roadmap

1. **Core model** — Lane-R features only. This is the required baseline and
   remains champion until beaten.
2. **Enhanced model** — Lane-L features added later, and only adopted if they show
   **incremental out-of-sample value**. More features are not assumed better.

## 16. Sequencing (phases, not dates)

1. architecture independently reviewed and accepted
2. F1-R bounded reconstruction pilot
3. Lane-R historical corpus (3→5 seasons)
4. feature engineering against the contract
5. baseline training
6. calibration
7. economic backtest (market-anchored subset only)
8. recommendation engine / UI
9. live shadow + enhanced-feature evaluation

**Historical core modeling does not require waiting years.** Steps 2–7 run on
retrospective Lane-R data available now. Forward collection runs in parallel and
gates only the *enhanced* model and profitability claims.

## 17. Prohibited shortcuts

* backdating `observed_at`, `decided_at` or review timestamps
* treating stitched/observed weather as a pregame forecast
* using endpoint season-to-date aggregates that may include the target game
* closing prices as features
* any future injury, lineup, roster or scratch information
* later canonical curation treated as mutable pregame evidence
* collapsing training and economic-backtest eligibility
* unioning Lane R and Lane L corpora silently
* imputing a missing family instead of flagging explicit missingness
* any `ignore_pit` escape hatch

## 18. Decision matrix

Earliest defensible availability is relative to the target game's `T_cut`.

### MLB

| Feature family | Lane | Evidence type | Earliest defensible availability | Retrospective usable? | New provider? | Notes |
|---|---|---|---|---|---|---|
| Target schedule (anchor) | R | VERSIONED_HISTORICAL | odds snapshot `<= T_cut` | **yes** | no | replaces local schedule gate; Odds API from 2020-06-06 |
| Static game/team/player identity | R | STATIC_IDENTITY | timeless | **yes** | no | stable `gamePk` / official ids only |
| Prior results | R | EVENT_DERIVED | source completion + L | **yes** | no | labels of prior games |
| Team rolling stats | R | EVENT_DERIVED | source completion + L | **yes** | no | self-derived, never season aggregates |
| Pitcher rolling stats | R | EVENT_DERIVED | source completion + L | **yes** | no | same |
| Batter rolling stats | R | EVENT_DERIVED | source completion + L | **yes** | no | same |
| Rest / schedule density | R | EVENT_DERIVED | from prior schedule | **yes** | no | pure calendar arithmetic |
| Rosters | L | FORWARD_ONLY | collection time | **no** | no | current state only |
| Probable pitchers | L | FORWARD_ONLY | collection time | **no** | maybe later | historical probables not reconstructable |
| Lineups | L | FORWARD_ONLY | collection time | **no** | maybe later | |
| Bullpen usage | R (partial) | EVENT_DERIVED | prior-game appearances + L | **partly** | no | prior usage only; same-day availability is Lane L |
| Weather forecast | R (optional) | VERSIONED_HISTORICAL | archived run `<= T_cut` | **partly** | no | GFS 2021+/ECMWF 2024+; stitched archive prohibited |
| Sportsbook moneyline | R | VERSIONED_HISTORICAL | snapshot `<= T_cut` | **yes** | no | 2020-06-06+, 5–10 min |
| Kalshi market | R | VERSIONED_HISTORICAL | candlestick `<= T_cut` | **likely** | no | 1-min candles; depth **not** reconstructable; G2 |
| Final result | — | LABEL_ONLY | after game | label only | no | never a feature |

### NBA

| Feature family | Lane | Evidence type | Earliest defensible availability | Retrospective usable? | New provider? | Notes |
|---|---|---|---|---|---|---|
| Target schedule (anchor) | R | VERSIONED_HISTORICAL | odds snapshot `<= T_cut` | **yes** | no | as MLB |
| Static game/team/player identity | R | STATIC_IDENTITY | timeless | **yes** | no | BALLDONTLIE stable ids |
| Prior results | R | EVENT_DERIVED | source completion + L | **yes** | no | |
| Team rolling stats | R | EVENT_DERIVED | source completion + L | **yes** | no | self-derived |
| Player rolling stats | R | EVENT_DERIVED | source completion + L | **yes** | no | |
| Advanced rolling stats | R | EVENT_DERIVED | source completion + L | **yes** | no | from prior games only |
| Plays-derived stats | R | EVENT_DERIVED | source completion + L | **yes** | no | 114,738 plays already validated |
| Rest / schedule density | R | EVENT_DERIVED | prior schedule | **yes** | no | back-to-backs explicit |
| Lineups / starters | **L** | FORWARD_ONLY | collection time | **no** | option B later | the merged March lineups are **August-observed**; never pregame features |
| Injuries | L | FORWARD_ONLY | collection time | **no** | option B later | |
| Rosters / active players | L | FORWARD_ONLY | collection time | **no** | no | |
| Sportsbook moneyline | R | VERSIONED_HISTORICAL | snapshot `<= T_cut` | **yes** | no | |
| Kalshi market | R | VERSIONED_HISTORICAL | candlestick `<= T_cut` | **likely** | no | G2 |
| Final result | — | LABEL_ONLY | after game | label only | no | |

**NBA rich pregame state resolves to (A) Lane-L forward-only**, with (B) a
versioned historical provider as a later option **only** if a core-model
experiment shows the family is likely worth the cost. The protected merged March
lineup corpus keeps its provider-depth value and is **never** a pregame feature.

## 19. Scientific standard — how this design satisfies it

| Requirement | Mechanism |
|---|---|
| No target outcome leakage | LABEL_ONLY isolation; label surface split preserved |
| No same-game final stats in features | EVENT_DERIVED §7 rule 5 |
| No closing prices as features | snapshot must be `<= T_cut`; closing prices prohibited (§17) |
| No future injury/lineup information | those families are FORWARD_ONLY |
| No postgame weather as forecast | stitched/observed archive prohibited; only Previous/Single Runs |
| No later curation as mutable evidence | static identity limited to immutable ids; everything else gated |
| No fabricated timestamps | `effective_at` derived from documented rules; `observed_at` untouched |
| Chronological splits | calibration gate §10 |
| Reproducible eligibility | manifest records lane, basis, cutoff, versions, policy |
| Explicit missingness | families flagged missing, never imputed |

## 20. Validation

Design/documentation only; **zero source files changed**.

```
git diff --check                     clean
ruff check .                         All checks passed
mypy . --no-incremental              Success: no issues found in 310 source files
pytest -q                            2386 passed, 2 skipped, 0 failed (494 s)
schema init x2 / v16->v17 x2         v17, 17 migration rows, integrity ok, idempotent
protected artefacts                  7/7 byte-identical
documentation consistency            no stale "not yet independently reviewed" claims;
                                     blocker preserved alongside the replacement
staged / forbidden-artifact audit    4 files, all documentation; no db, ckpt, raw
                                     response, log, wheel, env or graphify output
provider API requests                NONE (official documentation was read only)
```

---

## Verdict

**ARCHITECTURE READY FOR INDEPENDENT REVIEW.**

The Lane-R / Lane-L split solves the blocker without fabricating availability. The
core retrospective corpus — static identity, event-derived features, market-anchored
targets and settled labels — rests on verified provider evidence (Odds API historical
from 2020-06-06 with `<= T_cut` snapshot semantics) and on the repository's own
measured behaviour. Historical core modeling can therefore proceed **now**, without
waiting years, while forward Lane-L collection accumulates in parallel.

Four open gates (§13) remain and must be closed or explicitly accepted by the F1-R
pilot. The largest is **G1**: correction risk is currently bounded by a conservative
lag and sensitivity analysis rather than by measurement, because neither corpus
contains any correction history.

Nothing here is authorized for implementation. No provider request was made, no
protected corpus was touched, no timestamp was backdated, and production matching
and F2 backfill remain unauthorized.

---

**EVENT-COMPLETION EVIDENCE INVESTIGATED 2026-08-13 — EXISTING EVIDENCE
SUFFICIENT FOR NBA ONLY.**
`LANE_R_EVENT_COMPLETION_EVIDENCE_INVESTIGATION.md`. Read-only, zero provider
requests. **NBA 2026-03:** all 239 bounded games carry a source-provided per-play
UTC instant (`plays[].wallclock`) that survived ten adversarial checks with zero
anomalies — never equal to the scheduled tip, never a collection timestamp, no
duplicates, and overtime games show a longer median span (159.5 vs 136.9 min), an
internal-consistency signal a constant could not fake. It is a **DEFENSIBLE
DERIVED BOUND, not a DIRECT completion field**: it bounds completion from below,
and the existing conservative rule's 6-hour lag already absorbs the residual
last-play-to-final gap, so **no new availability rule is needed**. One narrow
policy decision is required — that the final play's wallclock is the completion
evidence. **MLB 2026-06: INSUFFICIENT.** Play-by-play was never collected at all,
and the only candidates are display strings (`First pitch` as a local 12-hour
clock with **zero timezone markers anywhere in the corpus**, plus `T`, which
explicitly excludes delays in 22 of 395 cases); deriving completion from them
would manufacture it. Sequential-snapshot bounding is impossible for both leagues
because every payload was received months after its games. **Schema v19 is
sufficient** for the NBA path; `raw_responses` is already an admissible evidence
table. Prior-game coverage is **228/239 (95.4 %)** — the 11 first-date games have
no in-corpus prior. **F1-R remains UNAUTHORIZED** and would be NBA-only and
bounded if later authorized; MLB needs a separate bounded endpoint-capability
probe. Odds/market anchoring, F2, production matching, feature engineering, model
training, calibration, backtesting, recommendation output and UI remain
UNAUTHORIZED. G1/G2/G3/G4/G6 unchanged.

---

**NBA LANE-R EVENT-COMPLETION POLICY + MATERIALIZATION IMPLEMENTED 2026-08-13 —
NOT independently reviewed.**
`NBA_LANE_R_EVENT_COMPLETION_MATERIALIZATION_IMPLEMENTATION.md`. The versioned
policy `nba-final-play-wallclock-v1` records that the final recorded play's
wallclock in the preserved BALLDONTLIE `/v1/plays` payload is accepted as source
event completion evidence — a **lower-bound proxy, NOT an official-final
timestamp**; a test forbids the wording from drifting into an over-claim. It is
bound through the existing v19 `availability_source` field, so **no schema state
was added to hold prose**, and the existing
`prior_event_completion_conservative_v1` (+6 h) rule is reused — **no new
availability rule**. **Schema stays v19**, no migration, `f018`/`f019` untouched.
Real NBA 2026-03 result, recomputed rather than assumed: **236 of 239 payloads
accepted (98.7 %)**, 3 rejected for period regression along play order. The
investigation's 239 counted wallclock *presence*, not terminal completeness. The
three rejects are contiguous game ids on one date (a likely collection-batch
defect); one of them would have passed a terminal-marker-only check, and is
refused because its period sequence is scrambled. Materialization copies the
`raw_responses` row verbatim **including its identifier**, preserves
`requested_at`/`received_at`/`created_at` exactly (the derived March instant is
never written over the August receipt time), is idempotent, fails rather than
overwrites on conflict, never writes to the source, and **never synthesizes a
`game_status_history` row**. The full v19 path was proved end to end on
disposable evidence: admitted at exactly completion + 6 h, exact to the
microsecond. **F1-R was NOT executed** — zero certification rows were produced.
**MLB remains blocked** pending its own endpoint-capability probe. Odds/market
anchoring, F2, production matching, feature engineering, model training,
calibration, backtesting, recommendation output and UI remain UNAUTHORIZED.
G1/G2/G3/G4/G6 unchanged.

---

**NBA COMPLETION MATERIALIZATION INDEPENDENTLY REVIEWED 2026-08-14 — ACCEPTED
WITH REPAIRS.**
`NBA_LANE_R_EVENT_COMPLETION_MATERIALIZATION_INDEPENDENT_REVIEW.md`. Four defects
reproduced and repaired. The most consequential was a **false rejection**: the
period-monotonicity gate discarded real game `18447743`, whose terminal play was
corroborated three independent ways (End Game marker at max order, max wallclock,
score equal to both the payload maximum and the official score). The pagination
hypothesis was disproved — regressions occur *within* 100-play chunks. It is
replaced by **terminal-score corroboration**, which is what actually separates a
truncated feed from a merely disordered one, compared within the payload rather
than against `/v1/games` (real game `18447470` disagrees by 3 points, which says
nothing about when the game ended). Also repaired: a boolean `order` was accepted
because `isinstance(True, int)` is True; and **nothing re-derived a stored
`source_event_completed_at` from its cited evidence** — `availability_source` is a
free-text **locator** by architecture, correctly not digest-bound, so a new
`verify_completion_certifications()` detective control now re-derives every stored
instant. **Real coverage corrected to 237/239 (99.2 %)**, 2 genuine exclusions,
226/237 with an in-corpus prior date. `raw_response_id` preservation adjudicated
**safe** (all-17-column conflict detection; same-id/different-content refused).
Strict PIT unweakened, **schema stays v19**, no migration, no new availability
rule, **0 provider requests**, 42/42 protected artefacts byte-identical. **An
NBA-only bounded F1-R may now be separately authorized**, reporting the 2
exclusions and 11 no-prior games explicitly. MLB, odds/market anchoring, F2,
production matching, feature engineering, model training, calibration,
backtesting, recommendation output and UI remain UNAUTHORIZED. G1/G2/G3/G4/G6
unchanged.

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
