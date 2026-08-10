# F1 historical point-in-time feasibility — independent review

Focused offline review of the historical point-in-time identity and backfill-time
contract, triggered by an apparent contradiction found after `3bcc01c`.

Zero provider requests. Production F1 matching was **not** run. No protected
corpus was mutated. No timestamp was backdated.

**Verdict: ARCHITECTURAL BLOCKER.** The hypothesis is confirmed empirically, and
it is worse than stated: the blocking gate fires *before* identity is ever
consulted. Retrospective API backfill cannot produce historical pregame rows under
the present contract, and no amount of matcher improvement changes that.

**Production F1 matching must not be run as an acceptance run until the
architecture is resolved.**

---

## 1. Boundary and evidence

23 process-level guards installed before importing matching, PIT or ingestion
modules; **14/14 adversarial probes blocked**;
`cli.load_settings is config.load_settings` is `True`; **0 guard trips**. No
provider audit, no real sleep. Only read-only inspection, synthetic fixtures and
SQLite backup copies were used.

All seven protected artefacts are **byte-identical** before and after, and both
protected corpora still contain **0 canonical games and 0 match decisions**:

```
39064fa219f4eb66f37e26f567a8f42d7df278236aa70e42264a600b07a5d135  NBA March corpus
c17a375daa89e3f0f8ace2e6e3dffd965f8428b178127cb9b7c44bde6471300b  NBA March checkpoint
e2fea1c06c43400323b0266aeb8ba34db28e9b6ead13504413eb93ed4de6e1db  lineup recovery db
8c4e83ee6cffb5c713de8bd0382d85b6486b2b68dd75821c7ad5ab38a4c689df  recovery checkpoint
223c1185a0c7c1afb35e04ae8e31e50de4f7e62e69073808eb08cf75cafad02a  merged copy
802a7d76e42d08dc60894329c490ca92d4f98e95197f2eaac967d3065af8b6f2  MLB June corpus
70bbc7c907cd6038eb57edd744111d7a187567fd948ff3d473a0192aaf91569e  MLB June checkpoint
```

## 2. Timestamp semantics, proven from code and data

| Column | Meaning, as implemented | Evidence |
|---|---|---|
| `raw_responses.received_at` | **system acquisition time** — when the HTTP response arrived | written from the exchange receipt |
| schedule `observed_at` | **system acquisition time** | equals the raw `received_at` of the response that produced it |
| result `observed_at` | **system acquisition time** | same ingestion path |
| lineup `observed_at` | **system acquisition time** | March games, observed 2026-08-04 / 2026-08-06 |
| identity snapshot `observed_at` | **system acquisition time** | same ingestion path |
| `entity_match_decisions.decided_at` | **matcher wall-clock execution time** | `record_decision` binds `now = utc_now_iso()` and writes it to both `decided_at` and `created_at` (`matching.py:118-131`) |
| `reviewed_at` | review completion time (processing) | set on review, NULL at insert |
| `created_at` | row insertion time | same `now` |
| provider `scheduled_start` / `game_date_local` | **provider event time** — when the game happens | descriptive of the event, not of knowability |

**Every `observed_at` in this repository is system acquisition time.** Nothing in
the ingestion path derives it from a provider publication or availability
timestamp. That is honest, and it is the root of the problem: the corpus records
*when we learned* a fact, and the provider gives no evidence of *when the fact was
publicly knowable*.

## 3. Real F1 month timing — the decisive measurement

Computed read-only over both protected corpora, every selected game:

| | games | earliest schedule obs **before** start | **equal** | **after** |
|---|---|---|---|---|
| **NBA March 2026** | 239 | 0 | 0 | **239 (100%)** |
| **MLB June 2026** | 400 | 0 | 0 | **400 (100%)** |

```
NBA  scheduled_start   2026-03-01T18:00Z .. 2026-04-01T03:00Z
     schedule observed 2026-08-04T22:12:11Z  (a ~4-5 month lag)
     result observed   2026-08-04T22:12:12Z .. 2026-08-04T22:35:52Z

MLB  scheduled_start   2026-06-01T22:40Z .. 2026-07-01T01:40Z
     schedule observed 2026-07-31T23:32:17Z  (a ~1-2 month lag)
     result observed   2026-07-31T23:32:22Z .. 2026-08-03T08:37:25Z
```

**Not one game in either corpus has a pregame schedule observation.** This is
stated as measured fact, not inference.

## 4. `decided_at` is matcher execution time — confirmed live

Bounded matching on backup copies, run today:

```
NBA decided_at range: 2026-08-10T09:32:29.670228Z .. 2026-08-10T09:32:29.696558Z
MLB decided_at range: 2026-08-10T09:32:30.147415Z .. 2026-08-10T09:32:30.178230Z
```

Matching a March game today stamps the decision *today*. `AsOfReader.matched_entity`
requires `decided_at <= cutoff`, so at any March cutoff the correspondence is
invisible. Hypothesis points 1–4 are confirmed.

## 5. Dataset behaviour, with exclusions decomposed

Bounded matching plus the real `build_historical_dataset()` on backup copies:

| | games considered | canonical games | accepted game decisions | dataset rows |
|---|---|---|---|---|
| NBA March 1–2 | 15 | **15** | **15** | **0** |
| MLB June 1–2 | 24 | **24** | **24** | **0** |

Exclusion decomposition over the canonical games:

```
NBA:  15/15  schedule first observed AFTER scheduled start
MLB:  24/24  schedule first observed AFTER scheduled start
        e.g. 18447686  start=2026-03-01T18:00:00Z  first_obs=2026-08-04T22:12:11Z
        e.g. 822728    start=2026-06-02T22:45:00Z  first_obs=2026-07-31T23:32:17Z
```

**Matching succeeded completely and changed nothing.** Every exclusion is the
`_feature_cutoff` schedule gate, which fires *before* identity is consulted. Zero
exclusions were attributable to missing identity, missing labels or ambiguity.

This is the most important finding in this review: the identity gate the earlier
tasks worked so hard on is **not the binding constraint**. Even a flawless matcher
produces zero rows.

## 6. `_LABEL_HORIZON` — deliberate, not accidental

`build_historical_dataset` reads once at `_LABEL_HORIZON` (`9999-12-31`) to
discover the final result and the current accepted identity, then re-checks
identity at the feature cutoff. That two-stage pattern is **deliberate
protection**, not accidental double-gating:

* the far-future read answers "does a settled label exist at all?" — a question
  about the *label*, which is legitimately allowed to be known later;
* the cutoff read answers "was the identity correspondence knowable at feature
  time?" — a question about the *features*.

Collapsing them would let a correspondence established after the game decide a
pregame row. **The second gate must not be removed to gain rows.**

## 7. Three concepts that were conflated

| Concept | Question it answers | Present in the corpus? |
|---|---|---|
| **Fact-time** | when did the game happen / when was the score true? | yes — `scheduled_start`, result content |
| **Knowledge-time** | when did Moneymaker obtain the fact? | yes — every `observed_at`, `decided_at` |
| **Availability-time** | what could a bettor using a historical source have known at the cutoff? | **no — nothing in the corpus evidences this** |

`observed_at` is **knowledge-time**. A valid betting backtest needs
**availability-time**. Retrospective API backfill delivers fact-time *content*
stamped with knowledge-time, and carries no evidence of historical availability at
all. A provider's `date`/`datetime` field describes the event, not when a schedule
row was first publishable — using it as an availability stamp would be
manufacturing historical knowledge.

The code is therefore **correct and honest**: it refuses to invent availability.
The contradiction lives in the *plan*, which expects ≥99% PIT-valid
identity/labels out of retrospective backfill.

## 8. The formal example

Game at `T_game`; backfilled at `T_fetch > T_game`; matched at `T_match >= T_fetch`;
feature cutoff `T_cut < T_game`.

| Check | Result |
|---|---|
| schedule visible at `T_cut`? | **no** — first `observed_at = T_fetch > T_game > T_cut` |
| `_feature_cutoff` yields a cutoff? | **no** — fails closed before anything else |
| match decision visible at `T_cut`? | **no** — `decided_at = T_match > T_cut` (never reached) |
| result visible? | yes at the horizon, correctly invisible at `T_cut` |
| dataset row? | **no** |

**Answer: C — impossible for retrospective API backfill under the present
contract.** Two independent gates each fail, and the schedule gate fails first.
Option B (genuine versioned availability timestamps) is the only route to A, and
it requires provider evidence this corpus does not contain.

## 9. Static canonical identity — analysed, and it does not rescue F1

Both positions were considered for stable official ids (`gamePk`, BALLDONTLIE game
id, official player/team ids):

* **As a time-gated observation.** A matcher run in August used an identity
  observation acquired in August; that observation was not historically knowable,
  so the decision should be gated.
* **As a static crosswalk.** The stable id is contained *inside the historical
  schedule response* and is immutable — "MLB gamePk 822728 is this game" is a
  timeless fact that cannot encode the outcome.

The second position is defensible for a narrow class: **immutable
provider-id → canonical-entity crosswalks whose supporting evidence is a stable
identifier, not a mutable observation.** Such a mapping carries no outcome
information, so admitting it retrospectively leaks nothing. Time-varying
observations (rosters, lineups, injuries, statistics, prices) must stay
knowledge-time gated.

**But this does not rescue F1**, and that must be said plainly: the measured
blocker is the *schedule* gate, which is independent of identity. Treating
identity as timeless would still yield **0 rows**. Any identity relaxation must
therefore be justified on its own merits, never as a coverage fix — and this
review does not authorize one.

## 10. `decided_at` carries two incompatible meanings

`decided_at` is currently used as both:

* **audit creation time** — when the matcher ran (what it is actually set to); and
* **knowledge-validity time** — the instant `AsOfReader` gates identity on.

Those coincide only when matching runs live alongside ingestion. For any
retrospective run they diverge completely. A correct architecture would need a
separate immutable *evidence-effective* time distinct from matcher execution time.
**No such field is added here** — that is an architecture decision requiring the
options below to be settled first, and adding it now would risk legitimising
backdating. Nothing was overwritten.

## 11. Architecture options

| | Leakage risk | Reproducible | F1 month can test it | F2 viable | Provider requirement | Change needed | Scientifically defensible |
|---|---|---|---|---|---|---|---|
| **A** — strict knowledge-time; retrospective data never becomes pregame features | none | yes | **no** (F1 has no pregame data) | only via forward shadow collection | none | none | **yes** |
| **B** — historically reconstructable availability | low **if** provider timestamps are trustworthy | yes | no — current evidence lacks them | yes, if such a provider exists | versioned publication/availability timestamps | ingestion + schema for availability time | yes, conditional on provider evidence |
| **C** — static identity treated as timeless, observations still gated | none for pure id crosswalks | yes | partly | no — does not fix the schedule gate | none | narrow identity-gating change | defensible in scope, **insufficient alone** |
| **D** — hybrid: A for features, C for identity, B where a provider genuinely supplies availability | low | yes | partly | yes | per-family | substantial | **most promising** |

**Recommendation (not authorization):** D, with A as the immediate operating rule.
Retrospective corpora keep their real value as provider-depth and
correction-behaviour evidence, and feature-valid data accumulates only by forward
collection or a provider with genuine availability timestamps.

## 12. Feature-family historical availability

| Family | Classification |
|---|---|
| Schedule | retrospective final state only — **no** historical "known by" timestamp |
| Results | retrospective final state only (label-legitimate; not a feature) |
| Team statistics | retrospective final only |
| Player statistics | retrospective final only |
| Advanced NBA statistics | retrospective final only |
| Rosters | current state only — no historical snapshot |
| Probable pitchers | current state only; historical probables not reconstructable |
| Lineups | **proven** retrospective (March games observed August) |
| Injuries | current state only |
| Weather | retrospective observation; forecast-as-of-pregame unavailable |
| Sportsbook prices | **genuine quote history exists** via historical-odds endpoints (paid) |
| Kalshi | **genuine trade/quote history** via API |

**`pregame_t_minus_60` cannot be reconstructed from the planned providers** for
team/player/roster/lineup/injury families. Only the market families
(sportsbook, Kalshi) plausibly support genuine historical pregame reconstruction.

## 13. What F1 actually proves today

| Claim | Proven by current F1? |
|---|---|
| Provider historical depth | **yes** |
| Normalization correctness | **yes** |
| Result completeness | **yes** (239/239 NBA typed) |
| Pagination handling | **yes** (the lineup continuation) |
| Correction handling | **yes** |
| Canonical matching mechanics | **yes** (15/15 and 24/24 bounded) |
| Historical PIT feature availability | **no — disproven** |
| Historical label availability at a valid cutoff | **no** |
| Market coverage | not tested |
| Model-trainable corpus viability | **no** |

The provider-depth work is **not wasted**: it validated ingestion, normalization,
pagination, corrections and matching mechanics, all of which a forward-collecting
system needs. What it cannot do is supply pregame features.

## 14. Contradiction matrix

| Contract statement | Code | Real F1 evidence | Compatible? | Action |
|---|---|---|---|---|
| Decisions carry knowledge-validity time | `record_decision` sets `decided_at = utc_now_iso()` | today's clock on March games | **no** | needs an evidence-effective time; do not backdate |
| Identity visible only if `decided_at <= cutoff` | `AsOfReader.matched_entity` | invisible at every March/June cutoff | consistent in itself | keep; it is correct |
| Game↔provider correspondence at cutoff | `game_provider_reference` | same | consistent | keep |
| Cutoff needs a pregame schedule observation | `_feature_cutoff` | **639/639 games fail** | **no** with backfill | keep the gate; fix the data source |
| Result strictly after cutoff | dataset label rule | satisfiable | yes | keep |
| Phase F ≥99% PIT identity gate | dataset + AsOfReader | 0 rows achievable | **no** | gate unattainable from backfill |
| F1 = historical month pilot | pilots/f1 | measures depth, not PIT | **partly** | rescope F1 explicitly |
| F2 = historical backfill for modeling | PHASE_F plan | impossible as written | **no** | rescope or re-source F2 |
| `pregame_t_minus_60` | planned feature | unreconstructable except markets | **no** | defer / re-source |

## 15. Validation

Documentation-only change; **zero source files modified**.

```
git diff --check                       clean
ruff check .                           All checks passed
mypy . --no-incremental                Success: no issues found in 310 source files
pytest -q                              2386 passed, 2 skipped, 0 failed (506 s)
schema init x2 / v16->v17 x2           v17, 17 migration rows, integrity ok, idempotent
protected artefacts                    7/7 byte-identical; both corpora still
                                       0 canonical games, 0 match decisions
staged / forbidden-artifact audit      4 files, all documentation; no db, ckpt,
                                       raw response, log, wheel, env or graphify output
zero-network                           23 guards, 14/14 probes blocked, 0 trips
```

---

## Verdict

**ARCHITECTURAL BLOCKER.**

Retrospective API backfill cannot produce defensible historical pregame rows under
the present contract. The binding constraint is `_feature_cutoff`'s requirement of
a pregame schedule observation, which **100% of games in both real corpora fail**;
the identity gate fails independently and is never even reached. Confirmed live:
matching succeeds completely (15/15, 24/24) and the dataset still yields **0 rows**,
with every exclusion attributable to the schedule gate.

Per the review contract, **production F1 matching was not run and must not be run
as an acceptance run until the architecture is resolved.**

No code was changed. This is architecture-level ambiguity, not an implementation
bug contradicting a coherent written contract, so the review stops at the report.
No timestamp was backdated and no historical availability was fabricated.

Standing status:

- **Production F1 matching has not run.**
- **Identity coverage remains unmeasured** — and would not unblock the dataset.
- **NBA PIT labels remain 0/239**, now explained: the schedule gate, not matching.
- **Historical March NBA lineups remain unavailable at March pregame cutoffs.**
- **Protected corpora remain byte-identical** with 0 canonical games and 0 decisions.
- **The combined F1 review has not begun. F1 remains incomplete. F2 remains
  unauthorized — and F2 as currently specified is not achievable from retrospective
  backfill.**
