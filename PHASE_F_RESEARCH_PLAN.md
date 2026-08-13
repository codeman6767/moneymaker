# Phase F — Research & Recommendation Plan (authoritative)

> ## ⛔ ARCHITECTURAL BLOCKER (2026-08-10) — read before authorizing anything
>
> `F1_HISTORICAL_PIT_FEASIBILITY_REVIEW.md` proves, from both real corpora, that
> **retrospective API backfill cannot produce historical pregame rows** under the
> present contract:
>
> * **239/239 NBA and 400/400 MLB games** had their earliest schedule observation
>   AFTER the scheduled start, so `_feature_cutoff` fails closed for every game.
> * Bounded matching on backup copies succeeded completely (15/15 NBA, 24/24 MLB
>   canonical games with accepted decisions) and `build_historical_dataset` still
>   returned **0 rows** — **every** exclusion was the schedule gate, not identity.
> * `decided_at` is matcher wall-clock time, so a March game matched today is
>   invisible at any March cutoff — a second, independent failure.
>
> Consequently:
>
> * **Do NOT run production F1 matching as an acceptance run.** It cannot move the
>   dataset off zero.
> * **Do NOT begin F2 backfill or modeling** on this basis.
> * The **≥99% PIT identity/label gate is unattainable from retrospective backfill**
>   and must be rescoped before it can be used as an acceptance criterion.
>
> The provider-depth work is **not** invalidated: ingestion, normalization,
> pagination, correction handling and matching mechanics are all validated. What
> retrospective data cannot supply is pregame features. Resolving the architecture
> (see the review's options A–D) is the prerequisite for any further F1/F2 step.
>
> **REPLACEMENT ARCHITECTURE PROPOSED (2026-08-10, design only, NOT authorized):**
> `HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` — verdict **ARCHITECTURE READY FOR
> INDEPENDENT REVIEW**. It replaces the blocked F1/F2 assumptions with:
>
> * a **four-clock model** (`ingested_at` / fact time / `effective_at` / decision
>   cutoff); `observed_at` and `decided_at` keep their meanings and are never backdated;
> * **Lane R** (retrospective core: static identity, event-derived features,
>   market-anchored targets, settled labels) and **Lane L** (forward-only mutable
>   pregame state), physically separated per `RECONSTRUCTED_CORPUS_PROVENANCE.md`;
> * **market-snapshot target anchoring** replacing the local-acquisition schedule
>   gate for Lane R only (The Odds API historical, verified: archive from 2020-06-06,
>   5-10 minute snapshots, `date` returns the closest snapshot at or before the
>   requested instant);
> * **three separate eligibility gates** — training, calibration, economic backtest —
>   that never collapse.
>
> **Revised F1/F2.** F1 keeps its proven value (provider depth, pagination,
> normalization, corrections, matching mechanics, request budgets) and is no longer
> asked to prove historical feature availability it never captured. A new bounded
> **F1-R reconstruction pilot** must pass first. **F2 is redefined** as building the
> Lane-R core corpus (minimum three seasons, target five), not backfilling every rich
> family: lineups, injuries, rosters and probable pitchers are **forward-only** and
> are excluded from retrospective features.
>
> **Still not authorized:** implementation, production F1 matching, F1-R execution,
> F2 backfill and modeling. Four open gates remain (architecture doc §13), the largest
> being that correction risk is bounded by assumption rather than measurement —
> neither corpus contains any correction history.
>
> **REVIEWED 2026-08-10 — ACCEPTED WITH REPAIRS**
> (`HISTORICAL_RESEARCH_PIT_ARCHITECTURE_INDEPENDENT_REVIEW.md`, authoritative where
> it differs). Lane R now splits into **core** (immutable facts, proven) and
> **extended** (correction-sensitive detail, bounded by assumption and flagged);
> static identity is decided per entity type; target anchoring uses the snapshot's
> contemporaneous `commence_time`; weather uses a conservative 24-hour-lead run and
> is excluded from the 5-season core; and retrospective economic *simulation* is
> permitted as research while *profitability claims* still require forward evidence.
> ~~G5 (provider-id stability) is the only gate blocking implementation.~~
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
> **No gate now blocks implementation**, but this authorizes nothing: implementation,
> F1-R, F2, production matching and model training each require separate authorization.
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

**Status:** F0 planning complete **and independently reviewed** (see §R). **F1A
(request/credit safety controls) is complete and independently reviewed.** **The
F1B *skeleton* pilots for MLB and NBA have now EXECUTED successfully and have been
independently reviewed — the skeleton stage is complete** (see
`F1B_SKELETON_PILOT_REPORT.md` and the box below). **Both F1 season-month pilots
have since executed and been independently reviewed** (see the F1 box below). No
feature engineering, model training, calibration, simulation, EV evaluation,
backtesting, or recommendation output has started. The F1B pilots ran at schema
**v16**; the schema is now **v17** and both F1 month manifests declare v17.

**The F1B *rich-data* pilots are COMPLETE for both leagues: MLB and NBA each executed
successfully and each passed its own independent review — so F1B capability
verification is complete.**

> **NBA rich pilot executed + independently reviewed (2026-07-30).** See
> `F1B_NBA_RICH_PILOT_REPORT.md`. Manifest `9de5d312b99c3e85…`
> (plan `1c896ae16a13c10b…`), `2026-01-05`, families `games` + `box`, `quarters`,
> `stats`, `advanced`, `plays`, `lineups`; `max_games=1`, `max_pages=1`,
> `max_records=100`, `max_retries=1`; 60/min ≤ 600/min; schema **v16**.
>
> - **7 requests of cap 14** (exactly the planner's semantic maximum): games listing,
>   selected-game re-fetch, **one** box score shared by `box`+`quarters`, traditional
>   stats, advanced stats, plays, lineups. All HTTP 200; 0 failures, 0 retries,
>   0 blocked, **1** listing page, **0** × 429, **0 s** throttle.
> - **8 games received → 1 selected (`18447316`) → 7 excluded** by `max_games=1`.
> - **389 → 960 rows (+571)**: 7 raw responses, 1 schedule snapshot, 2 team stats,
>   **60** player stats (35 traditional + **25 advanced**), **8** quarter lines,
>   **431** plays, 1 game / 2 team / 35 player references, 18 declared capability rows,
>   2 ingestion runs, 4 nonblocking capability-honesty notes. Grade **A**, score 1.00.
> - **Authentication succeeded; tier NOT verified** (`configured_not_verified:goat`,
>   `tier_evidence_source=none`). `/v1/games` is available below GOAT, so a 200 proves
>   authentication only; the 18 capability rows are **declared** (`is_observed=0`).
>   GOAT reachability evidence remains the separate 2026-07-28 capability audit.
> - **Completed resume made zero requests**, zero pages, zero mutation, and constructed
>   **no provider client** and performed **no authentication**, while preserving
>   provenance. Since the checkpoint-provenance repair a completed resume with nothing
>   outstanding is a **true no-op**: the checkpoint file is left byte-identical and the
>   logical-run totals (successes, terminal failures, retries, pacing, families) are
>   preserved rather than overwritten with the resuming process's zeros.
>
> **Lineup defect and repair.** The live `/v1/lineups` response returned HTTP 200 with
> 25 real flat rows (2 teams, 25 players, 10 starters) but the then-current parser
> expected a nested per-team `players` array, so it normalized to **zero** lineups
> silently. **The original NBA rich database is deliberately unchanged**
> (`lineup_snapshots=0`, `lineup_players=0`) as historical evidence of what the
> pre-repair code persisted. The parser was **repaired at `9386b54`**; replaying the
> exact preserved response through the repaired path yields **2 lineup snapshots,
> 25 players and 10 starters**, is idempotent, and is identical across shuffled input
> seeds. The independent review additionally found and repaired a non-total
> `_id_sort_key` ordering key (`"1"` vs `"01"` collided, making ordinals depend on
> provider row order).
>
> **Scope limits.** These retrospective pilots created **no strict historical
> point-in-time corpus** — every observation carries current receipt time, which is not
> historical pregame availability. **Canonical entity matching has not run**
> (`canonical_games=0`; provider references intentionally unresolved).

**F1B capability verification is complete. Phase F1 as a whole is NOT complete**, and
nothing below is authorized by this milestone:

- Canonical entity matching **mechanics** are proven offline on one game per
  league (schema v17). The bounded season-month coverage/depth pilots are now
  **prepared** (`pilots/f1/`) but have **not** been executed; broader
  coverage/depth measurement has **not** run.
- **F2 backfill is not authorized.**
- Historical corpus acceptance gates have **not** passed.
- Historical sportsbook-odds licensing is **unresolved**.
- Feature engineering, modeling, calibration, simulation, EV, recommendations, staking
  and execution have **not** started and may **not** begin.

> **F1 canonical matching mechanics pilot ran, returned 0%, and has now been
> unblocked (2026-07-31, schema v17).** The one-game matching pilot resolved
> nothing — 0 of 1 game, 0 of 2 teams, 0 of 52 (MLB) / 35 (NBA) players, both
> leagues. **The refusal was correct**: structured official identity did not
> exist. `game_schedule_snapshots` and `provider_team_references` stored provider
> ids and no names, so `TeamResolver` was handed the numeric id `'141'`; `players`
> and `player_aliases` were empty, so every player had an empty candidate pool.
> The provider-written names were in `raw_responses` all along.
>
> Migration `e017_provider_identity` (schema **v17**) adds append-only
> `provider_team_identity_snapshots` and `provider_player_identity_snapshots`;
> ingestion now lands the structured names it already receives, and matching reads
> them. Teams still resolve to the **seeded** canonical teams and are never
> invented. A canonical **player** may now be bootstrapped, but only by the
> league's designated official provider (`mlb_statsapi`, `balldontlie`) from a
> stable provider id plus a structured identity observation, under an explicit
> `official_provider_bootstrap` method at 1.00. Unknown and nonofficial names
> still never create anything. Full design: `ENTITY_MATCHING.md` §3.4.
>
> **Exact offline replay result** (preserved F1B rich corpora, zero provider
> requests, fresh temporary v16→v17 copies, originals untouched):
>
> | | MLB | NBA |
> | --- | --- | --- |
> | team identity observations | 34 | 42 |
> | player identity observations | 180 | 365 |
> | identities rejected | 0 | 0 |
> | provider teams linked | 2 / 2 | 2 / 2 |
> | canonical game | 1 created | 1 created (after the scheduled-start repair) |
> | provider players linked | 52 / 52 | 35 / 35 |
> | canonical players bootstrapped | 52 | 35 |
>
> Determinism: two independent replays per league varying raw-response order,
> endpoint-family order and player-reference traversal order produced **identical
> semantic serializations and identical logical hashes**. Idempotency: a second
> full pass inserted zero identity rows and created zero duplicate player, alias,
> link, game or DQ row.
>
> **NBA scheduled-start blocker — REPAIRED (2026-07-31).** The identity bootstrap
> was completed in schema v17; NBA matching then exposed a *separate* normalization
> defect: `nba_ingestor._normalize_game` set `scheduled_start` only for a
> *scheduled* game, so a finished game stored `NULL` despite the payload carrying
> `datetime`. BALLDONTLIE `datetime` is now the scheduled-start source
> **independent of game status**, with a documented precedence, strict
> timezone-aware validation, and sanitized DQ reporting for a supplied-but-unusable
> value. The game matcher's requirement for a scheduled start was **not** weakened.
>
> Replaying the exact preserved NBA responses into a clean schema-v17 corpus now
> resolves the one-game NBA official game: scheduled start
> `2026-01-06T00:00:00.000000Z`, teams 2/2, **1 canonical game created**
> (`official_key_exact`, 1.00), provider game reference linked, orientation home
> `tm_nba_det` / away `tm_nba_nyk`, season `sn_nba_2025_regular`, `game_date_local`
> `2026-01-05`, players 35/35. MLB is unaffected: game `822788` still resolves,
> `scheduled_start` unchanged at `2026-07-20T23:07:00Z`, all 52 player references
> still link. All original pilot databases, checkpoints, prior matching copies and
> reports remain byte-identical.
>
> **Checkpoint provenance loss — REPAIRED (2026-08-03).** The independent review of
> the live MLB June-2026 month execution found that a *completed* `--resume` rewrote
> the checkpoint with the resuming process's empty report, zeroing
> `successful_responses` (1999 → 0), `failed_responses` (2 → 0), `retry_attempts`
> (7 → 0), `throttle_wait_seconds` (~3407.9 → 0), `pages_fetched` (401 → 0) and
> `families_completed`. One harmless-looking resume destroyed the only durable record
> that the logical run had contained terminal failures and retries.
>
> The checkpoint is now `f1a-checkpoint-v2`: `usage` holds the **logical-run totals**
> and `usage_provenance.processes` is the append-only per-process history they derive
> from, with `logical_total = prior_total + current_process_value` for additive
> counters and declared non-additive rules (union / OR / high-water mark / precedence
> / identity-must-agree) for everything else. A completed resume with nothing
> outstanding is a **true no-op** — no provider client, no write, and the checkpoint
> file left byte-identical. Verified against copies of the real June artifacts: all
> twelve logical totals preserved exactly, every accounting invariant closing, and the
> **original June checkpoint still byte-identical and still v1 on disk**. Its
> incorrect historical completion decision was deliberately **not** retroactively
> repaired, and the two missing June roster responses remain explicitly missing.
>
> **Checkpoint provenance repair — INDEPENDENTLY REVIEWED AND ACCEPTED
> (2026-08-04).** The review reproduced the original evidence loss against the
> parent implementation extracted verbatim from `a281085`, proved a completed
> zero-work resume is a repeatable byte-identical no-op against copies of the real
> June artifacts (all twelve logical totals preserved), and validated the accounting
> model field by field. It found and repaired **seven** defects in the repair itself:
> no type/range validation of untrusted checkpoint values, legacy v1 accounting never
> validated, only additive totals checked against history, unchecked unit-set
> contradictions, no per-invocation process identifier, unreported recovered
> identities, and — most consequentially — `incomplete_identities` never being written
> on a unit failure, which had left the whole recovery mechanism inert in production.
> See `CHECKPOINT_RESUME_PROVENANCE_REVIEW.md`.
>
> **NBA remains unauthorized.** The next boundary is a fresh bounded BALLDONTLIE
> provider audit immediately before any NBA month execution, with the NBA manifest
> re-verified against its recorded hash. Nothing in this review authorizes it.
>
> **This still does not make F1 complete.** One game per league establishes
> matching *mechanics* only. It establishes **no** month coverage, **no** season
> coverage, and the 99% identity acceptance gate in §3.2 has **not** been
> approached, let alone passed. The bounded season-month coverage/depth pilot
> remains required and **F2 remains unauthorized.**
>
> **This does NOT make F1 complete.** One game per league establishes matching
> *mechanics* only. It establishes **no** month coverage, **no** season coverage,
> and does **not** approach the 99% acceptance gate in §3.2. The bounded
> season-month coverage/depth pilot is still required, and **F2 remains
> unauthorized.**

> **F1 season-month coverage/depth pilots BOTH EXECUTED AND INDEPENDENTLY
> REVIEWED (2026-08-05).** One-game mechanics were completed for both leagues
> first: capability verification (skeleton + rich), the schema v17 official
> identity bootstrap, and the NBA scheduled-start normalization repair. The two
> bounded season-month pilots this plan requires were then specified under
> `pilots/f1/` — with a full request-cap derivation, coverage-report contract and
> execution protocol in `pilots/f1/README.md` — and have since run:
>
> * **MLB June 2026** — executed, reviewed in `F1_MLB_2026_06_EXECUTION_REVIEW.md`.
> * **NBA March 2026** — executed, reviewed in `F1_NBA_2026_03_EXECUTION_REVIEW.md`.
>   One process, exit 0, 1,437 requests with zero failures/retries/429s, 239/239
>   games selected with a closed accounting identity, 240/240 units complete.
>   Six of seven families accepted; **`lineups` is not accepted** (40/239 games
>   partial after a provider pagination cursor was discarded), and **`results` was
>   never fetchable** because the planner's NBA family vocabulary omitted it.
> * **NBA March 2026 offline `results` repair — APPLIED (2026-08-05) and
>   INDEPENDENTLY REVIEWED AND ACCEPTED (2026-08-06)**, reported in
>   `F1_NBA_2026_03_RESULTS_REPAIR.md` and
>   `NBA_RESULTS_REPAIR_INDEPENDENT_REVIEW.md`. The 239 result observations were
>   populated offline from preserved live responses through the production
>   normalizer: **no provider request occurred**, the executed manifest and
>   checkpoint were not changed, and a frozen pre-repair database preserves the
>   original execution state locally. NBA typed result coverage is now
>   **239/239**. The review rebuilt the repair from that frozen evidence and
>   reproduced the committed rows and provenance record field-for-field, proved
>   atomic rollback at four injection points and safe concurrency, and hardened
>   five latent input-validation gaps that had not affected the applied data.
>
> **Usable point-in-time labels remain 0/239.** The results gap is closed; the
> remaining blocker is that no canonical `games` exist because matching has not
> run. The dataset builder must not be weakened to accept provider-only ids.
>
> **NBA lineup-continuation recovery EXECUTED and REVIEWED (2026-08-06); merge
> outstanding.** Forty of 239 games held partial lineups. `fetch_lineups` now
> takes a cursor, the planner expresses a bounded continuation shape, and
> `pilots/f1/nba_lineups_2026_03_continuation.manifest.json` is committed
> (manifest `a8979cd1…`, plan `3c0ec01c…`, bound to source manifest `901cb9de…`,
> source plan `e29ef60c…`, source database fingerprint `b5b475a4…`, target digest
> `03d3df93…`). Maximum live scope is **320 semantic requests / 640 attempts** at
> 60/min. No cursor value is committed — cursors are re-derived from the
> protected corpus at execution time and the run refuses if the digest has moved.
> The recovery writes only to new artifacts; the executed March manifest,
> checkpoint and database are untouched.
>
> **Independently reviewed and ACCEPTED (2026-08-07)** —
> `NBA_LINEUP_CONTINUATION_PREPARATION_REVIEW.md`. The review reproduced the
> target derivation, digest, manifest identity and source fingerprint
> independently, and repaired five defects, two of them blockers: the executor
> **persisted nothing**, and `--execute` was **not wired**. The production path is
> now fully wired and was exercised end to end over all 40 targets through a mock
> transport only — 90 continuation requests, zero first pages, all evidence
> durable, completed resume byte-identical.
>
> **EXECUTED LIVE (2026-08-06) and independently reviewed — ACCEPTED.** The fresh
> BALLDONTLIE audit passed, then exactly one authorized execution ran: one logical
> process, exit 0, 40/40 cursor chains terminated normally at the provider, 40
> continuation requests, zero first-page requests, zero retries, zero 429s, zero
> failures, checkpoint state `completed`, all 42 protected artifacts byte-identical.
> See `NBA_LINEUP_CONTINUATION_EXECUTION_REVIEW.md`.
>
> The review reconstructed every chain, the full request accounting, the pacing
> windows, the persistence and the checkpoint from preserved evidence, and
> replayed all 40 responses through the production path into three fresh v17
> databases that are semantically identical to the live one and idempotent on a
> second run. Row accounting closes exactly: 32 raw rows = 32 normalized = 32
> unique = 32 persisted, with zero rejection, zero overlap and zero silent loss.
> **40/40 games are merge-eligible.**
>
> The recovered volume is small for a structural reason, not a defect: all 40
> targets had exactly 25 rows on page one (a full page at `per_page=25`), which is
> why the provider issued a cursor for exactly these games and for none of the 199
> with 17–24 rows. 19 second pages were therefore legitimately empty. Merged
> lineups are 25–29 players with exactly two teams and exactly 10 starters each,
> contiguous with the accepted population.
>
> Two reporting defects were repaired: an unverified tier ceiling was rendered as
> a verified provider maximum, and empty continuation pages were absent from the
> report. Pacing enforcement was already correct — the limiter is built from the
> configured 60/min, never the tier maximum, and the maximum count in any rolling
> 60-second window was 40.
>
> **[HISTORICAL SNAPSHOT — SUPERSEDED]** *(read as of 2026-08-08; the merge has
> since been independently reviewed and ACCEPTED WITH LIMITATION —
> `NBA_LINEUP_MERGE_INDEPENDENT_REVIEW.md`.)*
> **OFFLINE MERGE APPLIED (2026-08-08) to a protected copy.**
> `F1_NBA_2026_03_LINEUP_MERGE.md`. The reviewed continuation evidence was merged
> into `data/f1_nba_2026_03_lineups_merged.db`; the original corpus, checkpoint
> and recovery evidence are byte-identical and no provider request was made.
>
> `lineup_snapshots` and `lineup_players` are hard append-only (BEFORE
> UPDATE/DELETE -> RAISE(ABORT)), so a page-one snapshot cannot gain members in
> place. The schema models observation-time revisions instead, so the merge
> appends one revision per affected `(game, team)` carrying page one plus its
> additions: **22 snapshots, 294 player rows, delivering the 32 recovered
> observations** (478 -> 500 snapshots, 5,125 -> 5,419 player rows). The naive
> "+32 rows" identity is unachievable under this schema and was replaced by a
> derived and asserted one.
>
> Merged distribution reproduces the review exactly (19@25, 13@26, 6@27, 1@28,
> 1@29); all 239 games show exactly two team snapshots and ten starters; the 199
> untouched games are unchanged; `is_confirmed` stays false. Idempotency,
> five-point atomic rollback, and determinism across three fresh destinations all
> hold. **PIT labels remain 0/239 in both the source and the merged copy** — the
> lineup merge alone cannot bypass canonical matching.
>
> **INDEPENDENTLY REVIEWED (2026-08-08) — ACCEPTED WITH LIMITATION.**
> `NBA_LINEUP_MERGE_INDEPENDENT_REVIEW.md`. The review rebuilt the 22/294/32
> identity from raw SQL without the production planner, reproduced the merged copy
> three times, and confirmed idempotency, eight-point atomic rollback, safe
> concurrency and path protection. One blocker-class defect was repaired: the
> revision base was selected by observation order, so any earlier observation at an
> affected anchor would silently displace the real page-one members. It now binds
> to page-one PROVENANCE; the merged database was unaffected and is byte-identical.
>
> **The NBA lineup family is accepted for this F1 month PROVIDER-DEPTH slice.**
>
> **MATCHER DEFECTS REPAIRED (2026-08-08), NOT YET REVIEWED.** The three known
> canonical-matching blockers are repaired offline -- official same-name identities
> are now order-independent, accepted exact-provider team replays no longer grow
> decision history, and accepted canonical mappings resolve downstream through the
> provider reference and its own backing accepted decision, knowledge-time gated on
> `decided_at`. No schema change: the repository stays at v17, and no observation
> table was backfilled because all of them are append-only.
> See `F1_CANONICAL_MATCHING_REPAIRS.md`. **These repairs are not independently
> reviewed, production matching over the F1 corpora has NOT run, and identity
> coverage has NOT been measured.**
>
> **Historical PIT limitation — do not conflate the two.** The entire March corpus
> was backfilled in August 2026 (page one 2026-08-04, continuation 2026-08-06) for
> games played in March. At every March pregame cutoff `latest_as_of` returns
> NOTHING for lineups in both the source and the merged copy. Historical pregame
> lineup availability for this month is **zero before and after the merge**; the
> merge adds retrospective provider depth only and introduces no leakage. These
> lineups must not be used as historical pregame features.
>
> **The combined F1 coverage/depth review has NOT begun. F1 remains incomplete and
> F2 remains unauthorized.** Canonical matching remains blocked by three separately
> documented defects (same-name official-player order dependence, non-idempotent
> team match decisions, and missing canonical-ID propagation into the observation
> tables); none was repaired by either month review.
>
> | | MLB | NBA |
> | --- | --- | --- |
> | manifest | `pilots/f1/mlb_coverage_2026_06.manifest.json` | `pilots/f1/nba_coverage_2026_03.manifest.json` |
> | range | `2026-06-01..2026-06-30` | `2026-03-01..2026-03-31` |
> | families | `schedule`, `results`, `box`, `inning`, `rosters` | `games`, `box`, `quarters`, `stats`, `advanced`, `plays`, `lineups` |
> | bounds | `max_games=600`, `max_retries=1` | `max_games=400`, `max_pages=8`, `max_records=1000`, `max_retries=1`, rate 60/min (tier max 600) |
> | semantic maximum | 3001 | 10808 |
> | hard request cap | **6002** | **21616** |
> | scratch / checkpoint | `data\f1_mlb_2026_06_scratch.db` / `data\f1_mlb_2026_06.ckpt` | `data\f1_nba_2026_03_scratch.db` / `data\f1_nba_2026_03.ckpt` |
> | declared schema | v17 | v17 |
>
> Both months are complete past regular-season calendar months with no
> postseason admixture: MLB June sits mid-season and avoids the July All-Star
> break; NBA March is entirely pre-play-in. Each cap is the planner's own
> `semantic_requests_max() x (1 + max_retries)` -- not a chosen number -- and
> every transport attempt, retry and pagination page reserves against it.
>
> A month-scale differential test drives the REAL `run_pilot_cli` orchestration
> and the REAL provider clients over `httpx.MockTransport` and asserts that the
> executor's attempt count can never exceed the manifest cap, that shared
> requests (MLB linescore for `results`+`inning`, NBA box for `box`+`quarters`)
> are not duplicated, that `max_games` / `max_pages` / `max_records` truncation is
> always reported rather than silent, that identity observations are recorded, and
> that a completed resume makes zero further requests.
>
> **Live season-month execution has NOT occurred.** Committing these manifests
> does not authorize it. Each live run requires explicit user authorization, the
> process-scoped `MONEYMAKER_F1B_AUTHORIZED=1` boundary, and a **fresh provider
> audit immediately beforehand**. The protocol is strictly sequential: audit ->
> MLB month -> independent MLB review -> NBA month -> independent NBA review ->
> combined F1 coverage/depth review -> only then decide whether F1 passes.
>
> **F1 remains incomplete and F2 remains unauthorized.** The 99% identity
> acceptance gates in section 3.2 have **not** passed and have not been
> approached: one season-month is a provider-depth pilot, not corpus acceptance.

> **MLB request pacing added before authorizing execution (2026-07-31).** A fresh
> MLB StatsAPI audit passed (5 canonical probes, 5 requests, no active failure, no
> persistence), and reviewing the manifest immediately afterwards exposed a
> separate safety gap: the MLB month manifest recorded
> `rate_per_min=null` / `configured_rate_per_min=null` /
> `provider_rate_limit_per_min=null`, and the MLB branch of `_make_gate` attached
> no rate policy at all. The 6,002 aggregate cap bounded total attempts but
> nothing bounded the RATE, so a 3,001-request month would have run as fast as
> responses returned. **Live execution was withheld and pacing added first.**
>
> MLB now carries a project-owned, versioned courtesy policy `mlb-pacing-v1`:
> **30 requests/minute, burst 1**, a derived **2.0 s** minimum interval between
> transport starts, on a monotonic clock. **No official provider maximum is
> claimed** — MLB StatsAPI publishes none this repository can verify, so
> `provider_rate_limit_per_min` stays null and the policy records
> `basis=project_courtesy_cap`. NBA's reviewed `bdl-rate-v1` verified-tier
> behaviour is unchanged (60/min configured, 600/min GOAT ceiling, sliding window).
>
> The MLB month manifest was regenerated: rate is part of plan identity, so the
> plan hash and manifest hash both changed, while the date range, families,
> `max_games=600`, `max_retries=1`, scratch/checkpoint paths, declared schema v17,
> **semantic maximum 3,001** and **hard cap 6,002** are all unchanged. The NBA
> month manifest and all four F1B manifests are byte-identical.
>
> **A NEW fresh MLB audit is required before execution.** The earlier audit passed
> but is no longer the immediately preceding audit for the manifest that will
> actually run.
>
> **[HISTORICAL SNAPSHOT — SUPERSEDED]** The MLB June-2026 month pilot **has since
> executed and been independently reviewed** (`F1_MLB_2026_06_EXECUTION_REVIEW.md`;
> the 400-game corpus is `data/f1_mlb_2026_06_scratch.db`). The paragraph above
> describes the pre-execution state and is retained as a record, not as current
> status or as authorization for a further run.
>
> **Current status:** F1 remains incomplete and F2 remains unauthorized — now for
> the architectural reason established in `F1_HISTORICAL_PIT_FEASIBILITY_REVIEW.md`
> and addressed by `HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` (reviewed: ACCEPTED
> WITH REPAIRS). Implementation, F1-R, F2, production matching and model training
> all remain unauthorized.

**Next planned phase boundary (not started):** the remaining F1 work — canonical
entity matching over the pilot corpora plus coverage/depth measurement — which must be
specified and reviewed before any F2 backfill authorization is considered.

> **MLB rich pilot executed + independently reviewed (2026-07-29).** See
> `F1B_MLB_RICH_PILOT_REPORT.md`. Manifest `f56b5c5da53d86c9…`
> (plan `73e887229ce20b8c…`), `2026-07-20..2026-07-21`, families `schedule` + `results`,
> `box`, `inning`, `rosters`, `max_games=1`, `max_retries=1`, schema **v16**.
>
> - **6 requests of cap 12** (exactly the planner's semantic maximum): range
>   `/schedule`, selected-game `/schedule`, `/game/822788/boxscore`,
>   `/game/822788/linescore`, and **two** team/date rosters. All HTTP 200, 0 failures,
>   0 retries, 0 blocked, 2 listing pages, 0 × 429, 0 s throttle.
> - **30 games received → 1 selected (`822788`) → 29 excluded** by `max_games=1`.
>   Selection is canonical, not provider order: the provider's first game was `824410`
>   while the canonical `(officialDate, gamePk)` first — and the game actually selected
>   — was `822788`.
> - **389 → 551 rows (+162)**: 6 raw responses, 1 schedule snapshot, 1 game + 2 team +
>   52 player references, 1 result, **18** inning lines, 2 team-stat and 25 player-stat
>   rows, **52** roster snapshots, 2 ingestion runs, **0** data-quality issues.
> - `results` and `inning` derived from **one shared linescore** response; inning rows
>   sum exactly to the persisted result on runs, hits and errors.
> - **Completed resume made zero requests**, zero pages, zero database mutation, and
>   built **no provider client**, while preserving the first run's provenance
>   (`prior_transport_starts=6`, `prior_pages_fetched=2`).
> - `data-quality` **grade A**, score 1.00, 0 blocking findings. **No secret was
>   persisted.** The **development corpus and all four skeleton artifacts were
>   unchanged**, and no NBA rich artifact exists.
> - The independent review found **no reporting defect** and added 7 regression tests
>   (`test_f1b_mlb_rich_review.py`), including explicit rich-stage protection of the
>   probable-pitcher inline-hydration policy (`probable_pitcher_snapshots=0` is
>   expected, costs no request, and hides no planner unit).
>
> **Matching and canonical corpus construction have not run** (`canonical_games=0`;
> provider references intentionally unresolved), and this retrospective pilot did **not**
> create a strict historical PIT corpus — every observation carries current receipt time.

> **F1B rich-data manifests prepared (2026-07-29, offline, zero provider requests).**
> `pilots/f1b/mlb_rich.manifest.json` (`f56b5c5da53d86c9…`, plan `73e887229ce20b8c…`)
> and `pilots/f1b/nba_rich.manifest.json` (`9de5d312b99c3e85…`, plan `1c896ae16a13c10b…`),
> both schema **v16**, canonical, deterministic, secret-free, and executable.
>
> - **MLB rich** — `2026-07-20..2026-07-21`, families `schedule` (auto) + `results`,
>   `box`, `inning`, `rosters`; `max_games=1`, `max_retries=1`; semantic max **6**;
>   **request cap 12**; credits n/a; scratch `data/f1b_mlb_rich_scratch.db`;
>   checkpoint `data/f1b_mlb_rich.ckpt`.
> - **NBA rich** — `2026-01-05`, families `games` (auto) + `box`, `stats`, `advanced`,
>   `plays`, `lineups`, `quarters`; `max_games=1`, `max_pages=1`, `max_records=100`,
>   `max_retries=1`; configured rate **60/min** ≤ verified **600/min**; semantic max
>   **7**; **request cap 14**; credits n/a; scratch `data/f1b_nba_rich_scratch.db`;
>   checkpoint `data/f1b_nba_rich.ckpt`.
>
> **Caps are planner-derived, not hand-written**: `cap = semantic_max × (1 + max_retries)`.
> A zero-network **differential** (real `run_pilot_cli` + real client request
> construction over mocked transports) shows the executor attempts **exactly** the
> semantic maximum and never exceeds it — MLB **6/6**, NBA **7/7** — with `results`+`inning`
> sharing one linescore, `box`+`quarters` sharing one box score, `max_games=1`
> preventing any second-game request, and `max_pages=1` preventing any second page.
>
> **Planner/executor mismatch found and repaired.** The NBA player-statistics group was
> named `player-stats` by the CLI/ingestor but `stats` by the planner/manifest. Untranslated
> this was silent in **both** directions: `--include player-stats` was dropped from the
> family set (collapsing a rich plan to skeleton), and a manifest family `stats` reached
> the ingestor as an unrecognised include, so player statistics were never fetched or
> persisted even though the plan had reserved a request for them. A single translation
> point now maps the two vocabularies. Also repaired: rich selection accounting was
> double-counted by per-game units (2 discovered games reported as 3 received), and
> `--max-retries` is now expressible on the CLI so the retry policy — part of the
> manifest identity — is reproducible.
>
> These retrospective pilots will **not** create a strict historical point-in-time
> corpus: both windows are completed past dates, so every observation carries the
> **current receipt time** and can never be treated as historical live-replay features.
> They measure capability, coverage, request fan-out, correction behaviour, and
> persistence only. No features, models, simulations, EV, recommendations, staking, or
> execution have started. **The rich stage is neither complete nor authorized.**

> **F1B skeleton pilot execution + independent review (2026-07-29).** Both pilots
> ran under the `MONEYMAKER_F1B_AUTHORIZED=1` boundary (process-scoped only, never
> persisted), each governed by its committed manifest:
> - **MLB** (`fa28695b…`, cap 4) — **1 request**, 1 listing page, **30 games
>   received → 2 selected (28 excluded by `max_games`)**, 1 raw response, 2 schedule
>   snapshots, 2 provider game refs, 4 provider team refs, 0 DQ findings.
> - **NBA** (`6fe6dc37…`, cap 8, 60/min configured ≤ 600/min GOAT max) — **1
>   request**, 1 listing page, **8 games received → 2 selected (6 excluded)**, 1 raw
>   response, 2 schedule snapshots, 2 provider game refs, 4 provider team refs, 9
>   declared capability rows, 2 non-blocking capability-honesty DQ notes.
>
> Selected games are the deterministic canonical first two (MLB by
> `(officialDate, gamePk)`, NBA by `(date_local, game_id)`), **not** provider
> response order — independently reconfirmed from the persisted response bodies.
> Both **completed resumes made zero additional provider requests**, zero database
> mutation, and left row counts and content hashes byte-identical. **No secret
> leakage occurred** (no key, `Authorization`, `x-api-key`, or secret-bearing URL in
> any database, checkpoint, or output; the only `authorization`/`?apiKey=` matches
> are schema DDL comments documenting that such values are never stored). **The
> development corpus was unchanged** (byte-identical SHA-256, every row predating
> the pilots), and the MLB artifacts were unchanged by the NBA execution.
>
> The review also repaired five **reporting** defects (the runs were correct; their
> reports were not): `rate_limited` no longer means "a rate policy exists"
> (`rate_policy_active` + `throttle_events` added); `max_games` selection accounting
> is now reported (`games_received` / `games_selected` /
> `games_excluded_by_max_games` / `selection_truncated`) strictly separately from
> budget truncation; `pages_fetched` now counts unique **successful** listing pages
> (page 0 included, failed transports excluded, retries counted once); authentication
> and tier are reported honestly (`authentication_status`, `tier_status`,
> `tier_verified`, `tier_evidence_source`); and a completed resume no longer erases
> the first run's transport provenance (`prior_transport_starts` /
> `prior_pages_fetched`).
>
> **Tier honesty:** a 200 from `/v1/games` proves **authentication only** — that
> endpoint is available below GOAT, so the skeleton run alone can never verify the
> tier. The 9 capability rows the skeleton wrote are **declared** states
> (`is_observed=0`), not observations. GOAT reachability evidence comes solely from
> the separate bounded capability audit of 2026-07-28 (`tier_restricted=false`, rich
> endpoints 200) recorded in its own git-ignored audit database.

> **F1B prerequisite discharge (2026-07-28).** Bounded, GET-only provider audits
> ran at the configured NBA tier against a git-ignored scratch DB (no secret ever
> printed):
> - **MLB StatsAPI** — `succeeded`, 5 GET requests, 0 active failures, keyless
>   (auth n/a), no tier restriction, capabilities observed.
> - **BALLDONTLIE (GOAT)** — `succeeded`, 9 GET requests, **authenticated=true**,
>   **tier_restricted=false**, all 9 probes HTTP 200 including the GOAT rich
>   endpoints (advanced_stats, plays, lineups, box_scores, player_stats). No HTTP
>   429s observed.
>
> Both audits were re-run independently on a **second, freshly initialised** scratch
> DB (schema v16, explicitly initialised, `data/` is git-ignored) and reproduced the
> same result: 14 GET requests total (5 MLB + 9 BALLDONTLIE), **every** request HTTP
> 200, 0 active failures, no 429s, BALLDONTLIE `authenticated=true` / `tier=goat` /
> `tier_restricted=false`. The only issues recorded were 2 `note`-severity
> `DQ-CAP-001` entries for the declared-unavailable `confirmed_pregame_starters`
> capability (expected, not a blocker). Scratch DB rows went 389 → 446 (+14
> `raw_responses`, +39 `provider_capabilities`, +2 `ingestion_runs`, +2 notes) and the
> development corpus (`data/corpus.db`) was **byte-identical** (same SHA-256) before
> and after, confirming database isolation. The NBA key was verified absent from the
> audit JSON, the scratch DB bytes, and all persisted headers/params; no
> `Authorization`/`x-api-key` header was persisted.
>
> **Quota-model correction.** BALLDONTLIE is metered by a per-minute **request-rate**
> limit per tier (Free 5, ALL-STAR 60, GOAT 600 req/min), **not** a monetary/credit
> balance. The codebase now models BALLDONTLIE credits as **not applicable** (never
> fabricated) and attaches a versioned request-rate policy (`RequestRatePolicy`,
> `bdl-rate-v1`) whose configured rate defaults to **100/min** (conservatively below
> the GOAT max) and can never exceed the verified tier maximum. The hard **aggregate
> request cap** still bounds total calls for the whole logical run; retries and
> pagination each consume a request; an unrecognised endpoint family fails closed
> (`UNKNOWN_ENDPOINT`); resumes do not reset the aggregate budget. The four concepts
> — aggregate request budget, requests attempted, provider tier rate limit,
> configured safe rate — plus throttle wait and observed 429s are reported distinctly.
> Re-verified against the official BALLDONTLIE documentation on 2026-07-28: the tier
> table is stated purely as "Requests / Min" (Free 5, ALL-STAR 60, GOAT 600) with a
> flat monthly subscription price; the documentation contains **no** credit balance
> and **no** per-request/endpoint-weighted credit cost. No user-facing surface may
> describe this rate limit as a billing credit: the `--credit-cap` CLI help now states
> credits are not applicable to any current provider (a stale string had still called
> a credit cap "required for a live NBA pilot"), covered by a regression test.
>
> **F1A audit-path defect (fixed).** The F1A ungated-transport guard blocked the
> bounded provider-audit (the audit built self-owned clients without the sanctioned
> `require_gate=False` opt-out; mocked-client tests never exercised the real-network
> path). Fixed in `_make_audit_probes` with a regression test.
>
> **Skeleton manifests generated (not executed).** `pilots/f1b/mlb_skeleton.manifest.json`
> and `pilots/f1b/nba_skeleton.manifest.json` — small completed date ranges,
> discovery-only families, explicit git-ignored scratch DB + checkpoint, conservative
> request caps, NBA rate 60/min. Deterministic (byte-identical on regeneration);
> credits n/a. See `pilots/f1b/README.md`.

> **F1A independent-review resolution (B1 + B2).** The two review blockers are
> resolved. **B1 — request-addressable resumability:** the pilot now checkpoints at
> per-**game** semantic-unit granularity (skeleton unit, then one atomic single-game
> ingest per selected game — reusing the audited ingestors' `game_pk`/`game_id`
> mode, whose committed transaction is the exact durable persistence boundary). A
> completed unit is skipped with ZERO transport on resume; an interrupted/failed
> unit stays incomplete and is retried idempotently (content-hash append-only); a
> completed resume performs zero transport; the selected game set is frozen in the
> checkpoint (`stage_game_ids`) so a later schedule change cannot alter it; and the
> manifest request/credit caps apply to the whole LOGICAL run across resumed
> processes (`RequestGate.seed_prior`; prior vs current usage reported separately;
> an uncertain interrupted request is counted conservatively). A non-budget failure
> (or `KeyboardInterrupt`) records a failed, resumable checkpoint (never "complete"),
> preserving durable units and the original error classification. **B2 — `max_games`
> + planner fidelity:** `ingest_mlb`/`ingest_nba` now validate and enforce
> `max_games` (reject negative/bool/oversized; zero = skeleton-only) using a
> deterministic canonical ordering (date + provider id, deduped), applied to every
> rich family, with honest truncation and a frozen `ordered_game_ids`; the planner
> models the real per-game-invocation fan-out (single-game schedule/`game` re-fetch,
> NBA `quarters`→`box_scores`) so a run's actual attempts never exceed the plan's
> conservative maximum before the runtime gate blocks them. All prior F1A hardening
> (legacy-bypass quarantine, ungated-transport refusal, credit fail-closed,
> manifest-governed execution, WAL-aware content-digest scratch identity, hardened
> checkpoints, accurate `network_occurred`) is preserved.
>
> **F1A implementation summary — offline.** A shared typed request/credit control
> layer gates the single transport chokepoint
> (`sports_quant/providers/base_provider.py:_get`): every attempt (initial call,
> each retry, each page) must reserve budget first, so a zero request budget makes
> zero transport calls and a run halts *before* exceeding a request or credit cap
> (`sports_quant/request_control.py`). Typed endpoint policies
> (`sports_quant/ingest/cost_policies.py`) mark BALLDONTLIE credits **not applicable**
> (it is request-**rate** limited per tier, not credit metered — see the F1B box
> above; a versioned `RequestRatePolicy` bounds the configured per-minute rate below
> the tier max, and an unrecognised endpoint family fails closed) and mark MLB
> StatsAPI credits *not applicable* (keyless). A genuine zero-network
> `--plan`/`--manifest-out`; deterministic,
> secret-free plans and pilot manifests with stable hashes (`planning.py`,
> `manifest.py`); manifest-governed `--pilot` (tamper/version/dup-key/schema/
> provider/policy-drift fail closed); a versioned external checkpoint with atomic
> temp+replace, precise persist-commit boundary, and verified resume
> (`checkpoint.py`, `pilot.py`); scratch-database isolation via a WAL-aware whole-DB
> content digest (`scratch_db.py`); and CLI wiring on `ingest-mlb`/`ingest-nba` with
> a distinct budget-exhaustion exit code (`4`) and run-failure exit code (`5`).
> Reconstructed-corpus provenance is **design-only** in
> `RECONSTRUCTED_CORPUS_PROVENANCE.md`; the strict E1/E2 builder is unchanged. **No
> live request, ingestion, backfill, feature, model, simulation, recommendation, or
> execution work occurred.**

**Baseline commit:** `631377a` (Phase E complete and independently reviewed; CI #54
green); F0 delivered at `06e8c55` and reviewed here. This document is the
authoritative roadmap for turning the completed point-in-time (PIT) data foundation
into a rigorously validated, **pregame** MLB/NBA game-winner recommendation model.

Companion: `PHASE_F_FEATURE_CONTRACT.md` (feature registry + manifest contract).

---

## R. F0 independent-review outcome (authoritative)

An independent offline review of F0 (at `06e8c55`) audited the point-in-time
semantics of the proposed historical pilot and the F1 request/credit controls. It
found **two blockers** that invalidate the original F1→F2 "download a historical
month and build the corpus" design for strict-PIT *feature* rows. This section is
authoritative and supersedes the earlier §3/§10 F1/F2 text where they conflict; the
superseded parts below are annotated.

> **Second review pass (at `44fcaf3`).** A follow-up independent offline review
> re-traced the `observed_at` code path and re-audited the request/credit controls at
> HEAD `44fcaf3` (no source changed since E2 — the ingest lane is docs-only across
> `06e8c55` and `44fcaf3`) and **re-confirmed both blockers and every resolution
> below verbatim**: `raw_exchange.py:75,105` still stamp
> `received_at=datetime.now(timezone.utc)` on both the buffered and streaming HTTP
> capture paths, and the E2 cutoff guard still excludes retrospectively-observed
> schedules. This pass additionally added the optional **MLB-only scope analysis
> (§13)**. No new code, migration, live request, or ingestion; schema remains v16;
> the live pilot remains **unauthorized**.

### R.1 Knowledge-time finding — retrospective backfill cannot produce strict-PIT feature rows (CONFIRMED)

`observed_at` is the wall-clock time this system *received the provider bytes*, and
it is never backdated. Exact code path:

- `sports_quant/providers/raw_exchange.py:75,105` — `received_at =
  datetime.now(timezone.utc)` at HTTP receipt (the **only** source of the timestamp;
  no provider/game field is ever substituted).
- `sports_quant/ingest/mlb_ingestor.py:1207` (and the NBA/odds/kalshi/weather/venues
  equivalents) — `raw_repo.store(..., received_at=to_iso(exchange.received_at))`,
  which returns that receipt time as the raw tuple's third element.
- every observation write unpacks that value into `observed_at=` (e.g.
  `mlb_ingestor.py:896` `sched_observed`, `:1006/1047/1095/1166` `observed`;
  `nba_ingestor.py:1244/1346/…` `observed`; `odds_ingestor.py:360`
  `observed_at = raw_response.received_at`; `kalshi_ingestor.py:528`; the injuries
  path even uses `to_iso(_now())` directly, `nba_ingestor.py:1607`).

Consequence, proven against the E2 builder:

- A historical game's schedule downloaded **today** gets `observed_at = today`. The
  feature-cutoff guard `_feature_cutoff` (`sports_quant/pit/dataset.py:261-262`)
  returns `None` whenever the earliest schedule `observed_at` is **after** the
  `scheduled_start`. For any past game, `today > scheduled_start` → **the row is
  excluded**. `build_historical_dataset` therefore yields **0 feature-ready rows**
  from an ordinary retrospective backfill, for both leagues.
- Prior-game rolling features are equally unavailable: their observations also carry
  `observed_at = today`, which is after a later game's historical cutoff.
- **Labels are recoverable** (a completed game's final result is unambiguous and is
  trivially known by dataset-build time), **but a recoverable label does not make
  the associated feature state point-in-time valid.**
- Provider timestamps, game dates, publication/update times **cannot** be
  substituted for `observed_at` without redefining the project's transaction-time
  semantics; backdating `observed_at` would violate the E1/E2 guarantees and the
  E2 visibility guard above. The code correctly fails closed — this is a **plan
  deficiency in F0's corpus-acquisition design, not a code defect.**

### R.2 Request/credit-control finding — no hard caps; live pilot unsafe (CONFIRMED)

Audit of the F1-capable commands found:

- `provider-audit` is inherently bounded (≤5 MLB, ≤9 BALLDONTLIE requests) — safe.
- `ingest-mlb`: schedule is one range call; **box, results/inning (per-game), and
  rosters (per team-date)** multiply — a rich MLB month ≈ **1,350–1,800 requests**.
- `ingest-nba`: games (paginated), box (per-date), and **player-stats, advanced,
  plays, lineups (per-game, paginated)** multiply — a rich NBA month ≈ **2,800
  requests nominal**, and up to **~17,500+** (plays capped at 50 pages/game ×
  ~350 games) before ×4 retry multiplication.
- **ABSENT:** any per-run request-count cap; any provider-credit budget; any
  halt-before-budget; BALLDONTLIE credit-header reading (credit accounting exists
  only for the out-of-scope Odds API).
- **`--dry-run` still performs every network GET** (it only skips DB persistence) —
  it is **not** a safe cost pre-flight.
- Pagination has **per-call** bounds only (`DEFAULT_MAX_PAGES=50`,
  `DEFAULT_MAX_RECORDS=10_000`, `nba_ingestor.py:104-105`), hardcoded and not
  CLI-exposed; there is **no per-run aggregate ceiling**.
- Runs are **idempotent** (content-hash append-only, no duplicate rows) but **not
  resumable** (a re-run re-issues every network call).

Because hard request/credit caps and a budget-halt are absent and dry-run is not
network-free, **the live pilot must not run yet.**

### R.3 Decision: split F1 into F1A (controls) and F1B (capability pilot)

- **F1A — request/credit safety + reconstructed-corpus provenance (offline build +
  independent review).** No live requests.
- **F1B — controlled live *capability* pilot**, only after F1A passes. F1B is a
  **capability/coverage/credit test**, explicitly **not** a strict-PIT data build
  (per R.1 it cannot be).

The original §10 "F1 pilot" and §3 "F2 build the accepted corpus (feature rows)"
are **superseded** by R.3 + R.4; see the revised §10.

### R.4 Corpus strategy (selected): staged hybrid (Option E)

Retrospective backfill cannot yield strict-PIT feature rows (R.1), no first-party
historical archive exists, and historical sportsbook odds are not obtainable
as-built (Gate G1). The rigorous, feasible, fastest-to-evidence path is a **staged
hybrid**:

1. **Forward-collected strict-PIT corpus (system of record for all live-replay and
   profitability claims).** Begin capturing pregame snapshots now (schedule, market
   quotes, lineups/injuries/probables/weather at the T−60 cutoff). `observed_at` is
   honest receipt time; rows are strict-PIT by construction. This is the only corpus
   permitted to support live-replay, calibration-for-deployment, and any economic
   claim. Maturity: a usable single-season sample accrues across one MLB (~Apr–Sep)
   or NBA (~Oct–Apr) season; **multiple seasons** are required before out-of-sample
   / profitability claims.
2. **Reconstructed-research corpus (explicitly NOT strict-PIT).** A separate,
   clearly-labeled corpus built from retrospective data under **conservative,
   provider-documented source-availability rules** (e.g. a prior game's final result
   is treated as available the morning after that game; opening lines per posting
   norms). It may drive **early baseline/feature/calibration-methodology research
   only** — never live-replay or profitability claims — and must carry an explicit
   provenance + reliability classification, be validated separately with sensitivity
   analysis, and never be silently mixed with the forward corpus. Reconstruction is
   defensible **only** for features that are pure functions of *prior completed
   games* with an unambiguous availability rule (ratings, opponent-adjusted form,
   rest/travel/schedule, venue/home, and market-implied *if* a PIT-timestamped odds
   source exists); fast-changing same-day families (lineups, injuries, probables,
   weather) are **excluded** from reconstruction unless a defensible availability
   rule is documented.

Permitted conclusions: the **reconstructed** corpus may establish relative feature
value, baseline model structure, calibration methodology, and approximate effect
sizes (with sensitivity analysis). It may **not** support strict out-of-sample
performance, deployment calibration, live-replay, or any profitability claim — those
require the **forward** corpus at multi-season maturity.

### R.5 Schema implication (no migration now)

The forward corpus needs **no** schema change — it is strict-PIT by construction on
schema v16. The **reconstructed** corpus eventually needs a provenance/availability
concept the schema lacks today (an explicit availability-time and a
provenance/reliability classification, kept in separate columns/tables so it can
**never** be confused with `observed_at`). This is a **future, separately-reviewed
migration**; it is **not** designed or implemented in this review, and schema
remains **v16**.

### R.6 Label semantics (see §3, revised)

Retrospective labels are usable but must never be read as evidence of retrospective
feature availability. Full policy in the revised §3.

### R.7 Prerequisite classification (F0 gates G1–G5)

- **Already satisfied:** MLB StatsAPI keyless public access + NBA GOAT endpoint
  *access* were probe-verified (2026-07-24) and **re-confirmed by a bounded GET-only
  `provider-audit` on 2026-07-28** — MLB `succeeded` (5 GET, keyless); BALLDONTLIE
  GOAT `succeeded` (9 GET, `authenticated=true`, `tier_restricted=false`, GOAT rich
  endpoints 200). The read-only/GET-only/execution-quarantine invariants hold; the
  audit persisted only to a git-ignored scratch DB and printed no secret.
- **G2 (active GOAT subscription): satisfied** — confirmed by the 2026-07-28 audit
  above. Historical *depth/coverage* (G3) is still measurable only by the F1B pilot.
- **Quota model:** BALLDONTLIE is request-**rate** limited per tier (not credit
  metered); credits are *not applicable*. A versioned request-rate policy
  (`bdl-rate-v1`, configured 100/min default ≤ GOAT 600/min) replaces the earlier
  (incorrect) "authoritative per-endpoint credit-cost" prerequisite.
- **User decision still required before *executing* F1B:** approve a per-run request
  **budget** and set the separate authorization boundary
  (`MONEYMAKER_F1B_AUTHORIZED=1`). Reviewed skeleton manifests are generated at
  `pilots/f1b/` but **not executed**.
- **User decision required before F2 / large backfill:** licensing/retention (G4 —
  MLB StatsAPI commercial terms; Open-Meteo CC-BY non-commercial).
- **Purchase/subscription that may be required:** The Odds API **historical** plan
  (G1) or an alternative PIT-timestamped historical odds source; otherwise sportsbook
  EV is forward-only.
- **Unverified historical products:** MLB StatsAPI and BALLDONTLIE historical
  *depth/coverage* (G3); any commercial PIT historical dataset (Option C) — each
  requires an audited sample before acceptance, never accepted on advertisement.

GOAT access is now **confirmed** by the 2026-07-28 bounded `provider-audit`
(`authenticated=true`, `tier_restricted=false`); the audit neither exposed nor logged
the key.

---

## 0. Product boundary (unchanged, restated as a constraint)

- **Read-only recommendation engine.** No bet placement, cancellation, account,
  portfolio, order submission, or execution. The execution surface
  (`evaluation/{evaluator,decision,portfolio}.py`, `gateway/`) stays dormant and
  quarantined; nothing in Phase F wires it in.
- **MLB and NBA only.** Separate, league-specific models unless the audit in F4
  produces out-of-sample evidence for a shared component.
- **Initial market: moneyline / game-winner win probability** (home-win
  probability). Spreads, totals, props, and in-game modeling are **separate later
  expansions**, never silently mixed into the first model.
- **Sportsbook and Kalshi prices are for comparison, pricing, and EV only**, under
  the explicit PIT policies in §5–§6. **Closing-line data is evaluation-only** and
  never a feature.
- The old synthetic probability implementation is **not** production-ready merely
  because its tests pass (see §9 disposition).

---

## 1. Current state (audit summary, commit 631377a)

### 1.1 Corpus readiness — the corpus is empty today

- No historical corpus is committed (`data/` is git-ignored). No `ingest-mlb` /
  `ingest-nba` run has ever executed. The `games`, `game_schedule_snapshots`,
  result, stat, roster, probable, lineup, injury, and weather tables hold **0
  rows**. Canonical matching has never run (`entity_match_decisions`,
  `provider_game_references` = 0).
- Therefore `sports_quant.pit.dataset.build_historical_dataset` yields **0 rows for
  both MLB and NBA today** — every one of its four preconditions (canonical game
  with official identity; accepted game↔provider reference at cutoff;
  historically-visible schedule snapshot; correction-aware final result observed
  strictly after cutoff) is currently unmet.
- The only *real* data ever ingested is **current** (not historical) sportsbook
  (The Odds API) and Kalshi public data, held locally in a git-ignored dev DB and
  unlinked to any canonical game.

### 1.2 Provider capability (verified vs declared) — the binding constraints

| Provider | Sport | Historical depth | Classification |
|---|---|---|---|
| MLB StatsAPI | MLB | date-ranged schedule/box/results; "decades" claimed | **declared** (access probe-verified; depth not) |
| BALLDONTLIE (GOAT tier) | NBA | date-ranged games/box/plays/lineups | **declared / commercial-needs-purchase** (paid GOAT key required; depth "provider-history-limited until audited") |
| hoopR | NBA | deep PBP/stint history | **documented-only, not implemented** (needs offline R toolchain) |
| The Odds API | MLB+NBA | **current odds only in code** (`/v4/historical` not implemented) | **current-data-only** — see Gate G1 |
| Kalshi public REST | both | current events/markets/orderbook/trades | **current-data-only** |
| Open-Meteo | MLB weather | ERA5 archive to **1940 hourly**; PIT historical-forecast implemented | **declared** (only concrete dated depth in the repo) |
| NWS | MLB weather | observations; archive inconvenient | **declared (best-effort)** |

### 1.3 Existing research code — see §9 for the full disposition table

The three research packages (`probability/`, `backtest/`, `evaluation/`) do **not**
import `sports_quant`; the only cross-lane edge is
`sports_quant/pit/dataset.py → probability.datasets.GameStateDataset` (lazy), which
already produces an honest zero-column `X` and all-NaN `true_prob`. The single
training path (`probability/pipeline.train_and_build → residual_model.train_champion`)
is hardwired to the **synthetic** dataset builders, consumes synthetic `true_prob`,
and has **no CLI**. It is not production-ready.

---

## 2. Pre-implementation gates (unresolved from the repository)

These are decisions the repository cannot settle on its own. Each is a hard gate
**before** the subphase that depends on it. Documenting a gate is not a reason to
delay committing this plan; it is a reason not to start the dependent subphase.

- **G1 — Historical sportsbook odds are not obtainable as-built.** The Odds API
  client implements only the current-odds endpoint. Historical pregame/closing
  odds require either The Odds API historical plan (paid; endpoint unimplemented)
  or an alternative historical odds source. **Options:** (a) purchase + implement
  the Odds API historical endpoint; (b) source historical closing/pregame odds
  elsewhere under license; (c) run the model on Kalshi executable prices only for
  EV and treat sportsbook EV as forward-only (collect current odds going forward,
  evaluate later). **Evidence needed:** provider plan terms + a controlled audit of
  historical odds coverage/latency. **Consequence if unresolved:** §6 sportsbook EV
  can only be evaluated *forward* (collect-now, evaluate-later); the predictive
  model (§4–§5) and Kalshi-based EV are unaffected. **Owner: user** (subscription).
- **G2 — NBA data requires a paid BALLDONTLIE GOAT subscription.** Free/All-Star
  tiers cannot supply box/plays/lineups. **Evidence needed:** an active GOAT key and
  a passing `provider-audit` at GOAT. **Consequence:** no NBA corpus without it.
  **Owner: user.**
- **G3 — Provider historical *depth* is unverified for MLB StatsAPI and
  BALLDONTLIE.** Only endpoint *access* was probe-verified. **Evidence needed:** the
  F1 pilot audit (bounded, date-ranged) measuring real returned coverage per season.
  **Consequence:** required-season targets in §3 are provisional until F1 passes.
- **G4 — Licensing / retention.** MLB StatsAPI terms are "ambiguous-to-restrictive"
  for commercial/betting redistribution; Open-Meteo free tier is non-commercial
  CC-BY. **Evidence needed:** a documented licensing decision per provider before a
  large backfill. **Owner: user.** **Consequence:** may constrain which provider is
  the system of record.
- **G5 — Confirmed pregame lineups may be unavailable at the chosen horizon.** NBA
  confirmed starters typically post ~30 min pre-tip; the recommended T−60 horizon
  (§5) may see only projected lineups. **Consequence:** lineup features are
  availability-gated with a missingness indicator in the first model (deferred to a
  later horizon A/B), not a blocker.

---

## 3. Subphase F1–F2 — Corpus acquisition and acceptance

> **Revised by §R.** There are now **two** corpora (R.4): the **forward-collected
> strict-PIT** corpus (system of record for all live-replay/economic claims) and the
> **reconstructed-research** corpus (early baseline/feature research only, never
> strict-PIT). The season targets (§3.1) and acceptance gates (§3.2) apply to
> whichever corpus is being accepted, with the reconstructed corpus additionally
> carrying a provenance/reliability classification and sensitivity analysis, and
> being barred from live-replay/profitability claims. "Backfill" (§3.3) refers to
> building the **reconstructed** corpus and to forward-capture batches — never to
> conjuring strict-PIT feature rows from retrospective downloads (impossible, R.1).

The model is **not** declared viable until real-corpus gates pass. No profitability
or accuracy claim may precede a **forward** corpus that clears the acceptance gates
below at multi-season maturity.

### 3.1 Required seasons and minimum usable samples (provisional pending G3)

- **MLB:** target **5 full regular seasons**, minimum **3**, plus **≥1 held-out
  season** never seen in training/validation. ~2,430 games/season → target
  ~12,000 labeled games, floor ~7,000.
- **NBA:** target **5 full regular seasons**, minimum **3**, plus **≥1 held-out
  season**. ~1,230 games/season → target ~6,000 labeled games, floor ~3,600.
- Regular season only for the first model; playoffs held out for a separate
  robustness slice (different base rates).

### 3.2 Acceptance gates (all must pass before modeling, per league)

- **Label coverage** ≥ **99%** of in-scope games have a provable final home/away
  label observed strictly after the cutoff (ties/postponements excluded, not
  fabricated).
- **Identity/matching coverage** ≥ **99%** canonical games have an accepted
  game↔provider reference valid at the cutoff.
- **Market coverage** (for EV eligibility, not for labeling): ≥ **90%** of in-scope
  games have at least one PIT-valid game-winner quote (Kalshi executable and/or
  sportsbook no-vig) at the cutoff; games without a quote are still labeled but are
  excluded from §6 EV.
- **Feature-family missingness:** a family is admitted to the initial model only if
  its missingness ≤ **20%**; otherwise it is represented by a missingness indicator
  only (per `PHASE_F_FEATURE_CONTRACT.md`).
- **Data-quality grade** ≥ **B** and **zero open blocking DQ issues** (`data-quality`
  reports `corpus_valid = true`) for the league corpus.
- **PIT determinism:** a fresh-rebuild and a randomized-insertion-order rebuild of
  the accepted corpus produce byte-identical dataset serializations.

### 3.3 Backfill order, credit control, and run discipline

> **Corrected by §R.2.** The controls below are **requirements on F1A**, not current
> behavior. Today the ingest path has **no** per-run request cap, **no** credit
> budget, **no** budget-halt, and `--dry-run` still makes every network GET; runs are
> idempotent but **not** resumable. These must be built and reviewed in **F1A**
> before any live batch.

- **F1A controls before any live run.** No live requests until F1A ships: request
  estimation, a hard per-run request/credit cap, a budget-halt that stops *before*
  exceeding a user-defined budget, a true no-network dry-run (cost preview without
  GETs), credit/usage reporting (requests + remaining credits + truncation + failed
  families), resumable checkpointing, and safe scratch-DB handling.
- Order (reconstructed corpus / forward batches): **oldest → newest, provider by
  provider, one league at a time**, official data (games/schedule/results) first,
  then stats/rosters/probables/lineups/injuries/weather, then market data.
- **Bounded runs:** explicit date range + record cap; a truncated sweep is reported
  (NBA already reports truncation; MLB truncation reporting is an F1A gap to close).
- **Idempotency** is already present (content-hash append-only, no duplicate rows);
  **resumability** is an F1A requirement (a re-run currently re-issues every call).
- A fresh **`provider-audit` must pass immediately before** each live stage.
- **Independent correctness review after F1A and after each acquisition stage**
  (coverage, leakage, determinism, DQ grade, credit accounting) before proceeding.

### 3.4 Label semantics (four times; policy per corpus)

Distinguish four timestamps: **(t0)** the real-world outcome time; **(t1)** the time
the provider recorded/corrected the result; **(t2)** the time this system received it
(`observed_at`); **(t3)** dataset-build time. Labels stay physically isolated from
feature state (E2), and a recoverable label **never** implies feature availability.

- **Strict forward-collected replay:** label = the final result with `observed_at`
  (t2) **strictly after** the T−60 feature cutoff and invisible at the cutoff
  (current E2 rule). This is the only label class admissible for live-replay/economic
  claims.
- **Retrospective reconstructed research:** label = the final result known by t3
  (unambiguous once the game is complete). Availability is trivially satisfied for
  the *label*; it says nothing about *feature* availability. Marked provenance =
  reconstructed; barred from live-replay claims.
- **Corrected results:** append-only, correction-aware; use the latest correction as
  of the policy time (forward: as-of the settlement horizon; reconstructed: the final
  corrected value by t3), flagged `is_correction`.
- **Results later overturned/amended:** recorded as a further append-only correction;
  a closed forward evaluation is **not** silently rewritten by a later amendment — the
  evaluation label is fixed as of a defined settlement horizon, and the amendment is
  retained with provenance for audit.
- **Abandoned / postponed / suspended / tied:** no home/away winner → **excluded**
  from moneyline labels (never fabricated). Postponement → the rescheduled game is a
  distinct cutoff; suspended-then-completed → label from completion; ties (MLB rare;
  effectively none in NBA) → excluded.

---

## 4. Pregame decision-time policy

A decision horizon must be **reproducible operationally and historically** — the
same rule that fires live must be replayable from the corpus. "The latest
information before the game" is rejected as ambiguous.

### 4.1 Recommended initial horizon (smallest trustworthy set: one)

**`pregame_t_minus_60`: exactly 60 minutes before the PIT-visible scheduled start.**

- **UTC cutoff calculation:** `cutoff = scheduled_start_utc − 60 min`, where
  `scheduled_start_utc` is taken from the **earliest schedule snapshot actually
  visible at that cutoff** (the E2 policy). A schedule first observed at/after its
  own start cannot set its cutoff → the game is excluded.
- **Schedule change:** if the visible scheduled start changes before the cutoff,
  the latest pre-cutoff schedule observation defines the start; changes observed
  after the cutoff never move it.
- **Postponement:** if the game is postponed and re-scheduled, the cutoff is
  recomputed from the earliest visible snapshot of the *new* start; if the
  postponement is not visible by the cutoff, the row fails closed (excluded).
- **Quote freshness:** a market quote qualifies only if `observed_at ≤ cutoff` and
  `cutoff − observed_at ≤ 15 min` (staleness bound); stale quotes are treated as
  missing.
- **Lineup/injury availability:** use the latest snapshot with `observed_at ≤
  cutoff`; if none, the feature takes its missing form + indicator (G5).
- **Weather (MLB):** latest `current_forecast` with `pit_eligible=1` and
  `observed_at ≤ cutoff`; else missing.
- **Multiple quotes at the same cutoff:** select deterministically — for sportsbook,
  the **best available no-vig price across books present at the cutoff** (best-book
  without hindsight, §6); for Kalshi, the executable price derived from the
  order-book snapshot with the latest `observed_at ≤ cutoff`. Equal-`observed_at`
  content conflicts fail closed via `AsOfAmbiguityError`.
- **Missing/ambiguous required information:** the model **abstains** (no
  recommendation) rather than guessing; the row is still usable for label-only
  corpus metrics.

### 4.2 Deferred horizons (later A/B, not in first model)

`pregame_t_minus_30` / `t_minus_20` (captures NBA confirmed lineups) and
`pregame_t_minus_24h` (early-market) are specified for later comparison. The first
model ships **one** horizon to keep the first result interpretable.

---

## 5. Feature architecture, baselines, models, and validation

### 5.1 Features

Feature families, per-family PIT rules, the versioned **feature registry**, and the
**feature-manifest contract** are specified in `PHASE_F_FEATURE_CONTRACT.md`.
Summary of the initial-model set: team-strength rating, opponent-adjusted rolling
form, MLB starting-pitcher state, rest/travel/schedule-density, venue/home
advantage, NBA pace/efficiency, market-implied probability (as **benchmark**; as a
model input only behind an ablation gate), and paired missingness indicators. No
feature is included merely because a field exists.

### 5.2 Baselines before models (required order)

Per league, in order, each evaluated with the §5.4 protocol:

1. **Base-rate** (constant league home-win rate).
2. **Home-field/home-court** baseline.
3. **Elo/rating** baseline.
4. **Market no-vig implied-probability** baseline (the bar to beat).
5. **Regularized logistic regression** on the registry features.
6. **Gradient-boosted trees** — only after 1–5 are established.
7. **Ensemble / residual-over-market** — only if out-of-sample evidence supports it.

Neural nets / LLMs / complex ensembles are **not** assumed superior. AI/ML is used
where it demonstrably beats the market+rating baselines out of sample; deterministic
statistical logic (Elo, no-vig, shrinkage) remains preferred where it is competitive.

### 5.3 Reusing existing code

`residual_model.train_champion` (logistic + bootstrap, val-log-loss selection) and
`probability/inference.py` are reusable once fed a **real, populated** feature matrix
from the F3 registry (they currently only see the synthetic in-game X). `pipeline.py`
gains a real-data entry point that reads `build_historical_dataset` + the F3 manifest
instead of the synthetic builders. See §9.

### 5.4 Validation design (leakage-safe)

- **Chronological only** — no random row shuffle; no cross-time leakage.
- **Rolling-origin / expanding-window** evaluation; **season-based holdouts**; the
  final held-out season is never used for tuning.
- **Purge/embargo** around each split boundary where overlapping rolling windows
  create dependence.
- **Separate MLB and NBA** evaluation end to end.
- **Slices:** by season, month, team, decision horizon, data-availability tier, and
  market price range.
- **Retraining rules:** documented expanding-window retrain cadence; every retrain
  reproducible from the corpus + manifest hash; correction/rebuild reproducibility
  asserted.
- **Required probability metrics:** log loss, Brier score, calibration intercept &
  slope, reliability curves, expected calibration error (with documented binning),
  sharpness / predicted-probability distribution, and uncertainty/confidence
  intervals. **Accuracy / win-rate alone is insufficient** and never reported alone.

---

## 6. Market and economic evaluation

Predictions are compared to prices **only** under PIT policy; no settled outcome,
final price, or closing price is ever a feature.

- **Sportsbook:** de-vig h2h to a no-vig implied probability; use the best book
  present **at the cutoff** (best-book without hindsight — never chosen using later
  information); record quote `observed_at`, staleness, spread, and fees.
- **Kalshi:** executable price derived correctly from the **public order book**
  (executable Yes ask = 100 − best No bid, and symmetrically), including fees and
  the ladder walk for size; an empty book → no executable price.
- **Availability:** a game contributes to EV only if a PIT-valid quote exists at the
  cutoff (market-availability bias reported explicitly).
- **Closing-line value (CLV)** is computed **evaluation-only** (never fed back into
  the decision), reusing `backtest/`'s existing CLV path.
- **Bet-selection thresholds** (edge cutoffs) are chosen **only on
  training/validation** periods, never on the held-out season.
- **Required economic metrics, each with uncertainty:** number of eligible
  opportunities; number of recommendations; average estimated edge; realized return
  under **explicitly defined** simulated fill/fee assumptions (reuse `backtest/`
  fill + fee models); drawdown; profit factor where meaningful; **bootstrap
  confidence intervals**; probability of loss; performance by edge bucket; and
  comparison to **market-only** and **no-bet** baselines.
- **No profitability claim** may be made from a small sample, an uncalibrated model,
  or a backtest lacking realistic quote availability.

---

## 7. Calibration and uncertainty

- **Out-of-fold** calibration only; calibration is **never** fitted on the final
  holdout.
- **Method selection** (Platt / isotonic / beta) chosen by OOF log-loss/ECE, per
  **league** and potentially per **horizon**.
- **Minimum calibration sample** documented; below it, the model **abstains**.
- **Recalibration schedule** (expanding-window cadence) and **distribution-shift
  monitoring** (feature and score drift) defined.
- **Per-prediction uncertainty** (bootstrap ensemble spread + OOD flag from
  `probability/uncertainty.OODDetector`).
- **Fail-closed policy:** insufficient data support, OOD features, stale/absent
  market, or below-minimum calibration sample → **no recommendation**.

---

## 8. Recommendation gate (contract only — not implemented)

A future recommendation must carry: league + game identity; decision timestamp
(UTC cutoff); model version; feature-manifest version/hash; data-quality state;
predicted probability; market implied probability; estimated edge; uncertainty
interval; price + provider timestamp; quote age; calibration status; and explicit
reasons for abstaining. The engine **prefers abstention over unsupported
confidence** and emits an explicit "no recommendation" with reasons.

**Staking/bankroll sizing is out of scope for F0 and deferred** until predictive
validity (§5,§7) and economic backtesting (§6) independently pass. When eventually
planned, fractional Kelly must be **capped** and based on an **uncertainty-adjusted**
edge — not part of this plan.

---

## 9. Existing-code disposition

| Component | Disposition | Basis |
|---|---|---|
| `probability/datasets.py` (synthetic builders) | **synthetic/test-only** | fabricates in-game states + synthetic `true_prob` |
| `probability/reference.py` (generative truth) | **synthetic/test-only** | the synthetic-truth source itself |
| `probability/features.py` (`FeatureSpec`, vectorizers) | **reusable-after-repair** | float32 layout/OOD/prior-as-feature-0 reusable; in-game body replaced by registry pregame vectorizers |
| `probability/pipeline.py` (`train_and_build`) | **reusable-after-repair** | only training path, but hardwired to synthetic + consumes synthetic `true_prob`; needs a real-data entry point |
| `probability/surfaces.py` | **reusable-after-repair** | in-game score/phase grid; empirical table logic reusable for calibration |
| `probability/residual_model.py` | **production-reusable-unchanged** | logistic + bootstrap champion; needs a populated X (not the E2 zero-column) |
| `probability/inference.py` | **production-reusable-unchanged** | model-agnostic serving; ONNX backend dormant/optional |
| `probability/pregame_prior.py` | **production-reusable-unchanged** | the one genuinely pregame module; feature 0 |
| `probability/uncertainty.py` (`OODDetector`) | **production-reusable-unchanged** | generic OOD on any X |
| `probability/calibration.py` | **production-reusable-unchanged** | Brier/ECE/reliability; evaluation utility |
| `probability/onnx_export.py` | **production-reusable-unchanged (dormant)** | lazy ONNX export; not on any live path |
| `backtest/*` (backtester, fill_model, latency_model, book_timeline, data_quality, events, metrics) | **evaluation-only** | latency/fill simulation harness; CLV uses closing price for the metric only, never fed back; simulated fills never touch a venue |
| `backtest/backtester.EdgeStrategy` | **evaluation-only (quarantined intent)** | emits `StrategyDecision` order *intents*; simulated fills only, no venue wiring |
| `evaluation/pricing.py` (`FeeModel`, `walk_ladder`, `quote_side`) | **production-reusable-unchanged** | pure executable-price/fee/EV math; no execution |
| `evaluation/latency_trace.py` | **production-reusable-unchanged** | monotonic latency trace |
| `evaluation/decision.py`, `evaluator.py`, `portfolio.py` | **quarantined** | in-game order/submit/bankroll surface; must stay out of the research app |
| `gateway/` | **quarantined** | execution gateway, `EXECUTION_QUARANTINED=True` (source-level) |
| `tracking/` (frame-level adapters) | **quarantined / optional-deferred** | optional per CLAUDE.md; not a dependency of the first model |
| `intel/` (lineup/injury/probable/news adapters, `material_change`) | **reusable-after-repair (deferred)** | must route through PIT accessors before any feature use; audited in a later subphase, not in the first model |

None of these are deleted or rewritten during F0.

---

## 10. Phase F subphases and review gates

For each subphase: **G**oal, **S**cope, **F**iles, **I/O**, **T**ests, **D**ata,
**Live**, **P**rereqs, **Gate**, **Review**, **Prohibited**, **`/clear`**.

### F1A — Request/credit controls + reconstructed-corpus provenance (OFFLINE)
- **G:** make live ingestion budget-safe and define the reconstructed-corpus
  provenance model — **before** any live request. Closes the R.2 blocker.
- **S:** implement (with tests) a hard per-run request/credit cap, a budget-halt that
  stops *before* exceeding a user-defined budget, a **true no-network dry-run**
  (request estimate without GETs), credit/usage reporting (requests, remaining
  credits, truncation, failed families) for MLB+NBA, resumable checkpointing, safe
  scratch-DB handling, and a pilot manifest. Specify (not implement) the
  reconstructed-corpus provenance/availability classification (R.5).
- **F:** ingestor/CLI request-control code + tests; a reconstructed-corpus design note.
- **I/O:** in = existing ingest code; out = budget-safe ingest path + estimator.
- **T:** cap halts before budget; dry-run issues **zero** network calls; resume after
  interrupt re-issues no already-fetched call; idempotent rerun; usage/credit report
  fields present. All offline (mocked transports).
- **D:** none (offline). **Live:** **no.** **P:** none. **Gate:** all controls tested
  + independently reviewed. **Review:** independent. **Prohibited:** any live request,
  ingestion, features, models. **`/clear`:** yes before F1A. *(No schema migration.)*

### F1B — Controlled live capability pilot (NOT a strict-PIT build)
- **G:** verify real provider coverage, credit cost, and matching on a tiny live
  slice; resolve G3. This is a **capability test only** — per R.1 it cannot and does
  not produce strict-PIT feature rows.
- **S:** budget-capped, date-ranged skeleton-then-rich ingest of one active-season
  month per league (§5 pilot spec) into a **separate scratch DB**; run matching;
  `data-status`/`data-quality`; idempotent + interrupted-recovery checks.
- **F:** pilot report only. **I/O:** out = coverage/credit report + scratch DB.
- **T:** the §5 pilot checks (row counts, rejections, resumability, no production-DB
  modification).
- **D:** one active-season month/league. **Live:** **yes — bounded, credit-capped,
  first allowed live requests**, only after F1A + a fresh `provider-audit` pass.
- **P:** **F1A passed**; G2 (NBA GOAT key). **Gate:** measured coverage/credit within
  budget; controls behaved. **Review:** independent. **Prohibited:** large backfill,
  treating pilot data as strict-PIT, features, models. **`/clear`:** yes.

### F1C — Begin forward strict-PIT collection (parallel, ongoing)
- **G:** start the forward strict-PIT corpus (R.4 system of record) accruing now.
- **S:** scheduled T−60 pregame captures (schedule, market quotes, available
  lineups/injuries/probables/weather) with honest receipt `observed_at`.
- **F:** a bounded scheduled-capture runner (reuses F1A controls). **I/O:** out =
  growing strict-PIT corpus. **T:** each capture is strict-PIT, bounded, idempotent.
- **D:** live current slates. **Live:** **yes, bounded/credit-capped.** **P:** F1A.
  **Gate:** captures validate as strict-PIT. **Review:** independent. **Prohibited:**
  claiming multi-season sufficiency early. **`/clear`:** yes.

### F2 — Reconstructed-research corpus (explicitly NOT strict-PIT)
- **G:** build the clearly-labeled reconstructed corpus for early baseline/feature
  research (R.4), under conservative provider-documented availability rules.
- **S:** date-ranged retrospective ingest → reconstructed rows carrying a
  provenance/reliability classification (per the F1A design; **eventual** schema
  change, separately reviewed — **not** in this subphase); run matching; produce a
  `data-quality` grade **for the reconstructed corpus**.
- **F:** reconstruction builder + tests (no strict-PIT `build_historical_dataset`
  change). **I/O:** out = reconstructed corpus + provenance + DQ report.
- **T:** §3.2 gates; determinism; **explicit non-PIT labeling**; never mixed with the
  forward corpus; sensitivity analysis harness.
- **D:** target seasons (§3.1). **Live:** **yes, bounded + credit-capped** (F1A path).
- **P:** F1A/F1B passed; G1/G4 licensing decision. **Gate:** §3.2 pass on the
  reconstructed corpus, provenance classified, sensitivity plan defined. **Review:**
  independent. **Prohibited:** representing it as strict-PIT; profitability claims;
  features/models. **`/clear`:** yes.

> **Corpus scope for F3–F9 (per §R.4).** F3–F6 baseline/feature/calibration/EV
> **research** run on the **reconstructed** corpus (early evidence only, non-PIT,
> with sensitivity analysis). F7 realistic backtesting and F9 shadow evaluation, and
> any deployment-calibration or profitability claim, require the **forward strict-PIT**
> corpus at multi-season maturity (F1C). Results from the two corpora are reported
> separately and never conflated.

### F3 — Feature specification & implementation
- **G:** implement the feature registry + manifest per `PHASE_F_FEATURE_CONTRACT.md`.
- **S:** registry, pregame vectorizers over `build_historical_dataset` rows, manifest emit.
- **F:** new `probability/feature_registry.py` (+ manifest), repaired `probability/features.py`; new tests.
- **I/O:** in = accepted corpus + E1 accessors; out = versioned feature matrix + manifest.
- **T:** the six leakage/determinism tests in the feature contract §5.
- **D:** F2 corpus. **Live:** **no.** **P:** F2 accepted. **Gate:** all §5 tests pass; manifest byte-stable. **Review:** independent (leakage focus). **Prohibited:** training/EV. **`/clear`:** yes.

### F4 — Baseline models
- **G:** train baselines 1–6 (§5.2) per league.
- **S:** real-data entry point in `pipeline.py`; fit champion; §5.4 protocol.
- **F:** repaired `pipeline.py`, reused `residual_model.py`; new training tests. **I/O:** out = model artifacts + manifest + metrics.
- **T:** chronological-split integrity, no-leakage, reproducibility from manifest hash.
- **D:** F3 features. **Live:** **no.** **P:** F3. **Gate:** logistic/GBT beat base-rate & home-field out of sample on §5 probability metrics; documented vs the market no-vig baseline. **Review:** independent. **Prohibited:** EV/recommendation, calibration on holdout. **`/clear`:** yes.

### F5 — Calibration & uncertainty
- **G:** OOF calibration + uncertainty per §7.
- **S:** method selection per league/horizon; OOD + bootstrap uncertainty; fail-closed thresholds.
- **F:** reuse `calibration.py`, `uncertainty.py`; new calibration-artifact + tests. **I/O:** out = calibrator artifact + calibration report.
- **T:** calibration never fitted on holdout; min-sample abstention; reliability/ECE.
- **D:** F4 OOF predictions. **Live:** **no.** **P:** F4. **Gate:** calibration slope≈1/intercept≈0 OOF; documented ECE. **Review:** independent. **`/clear`:** yes.

### F6 — Market / EV evaluation
- **G:** compare calibrated model to prices per §6.
- **S:** no-vig + Kalshi executable pricing at the cutoff; EV with bootstrap CIs; ablation of market-as-input.
- **F:** reuse `evaluation/pricing.py`, `backtest/` fill+fee; new EV report + tests. **I/O:** out = EV report by edge bucket + baselines.
- **T:** best-book-without-hindsight, staleness, availability-bias reporting, threshold-on-train-only.
- **D:** F5 model + F2 market data (subject to **G1** for sportsbook history). **Live:** **no.** **P:** F5; G1 for sportsbook EV (Kalshi EV unaffected). **Gate:** positive edge vs market-only & no-bet baselines with CIs, on validation only. **Review:** independent. **Prohibited:** profitability claims from small/uncalibrated samples; staking. **`/clear`:** yes.

### F7 — Realistic historical backtesting
- **G:** full historical replay with realistic quote availability, fees, latency.
- **S:** reuse `backtest/` harness end to end on the held-out season; CLV eval-only.
- **F:** backtest config/report; new tests. **I/O:** out = held-out backtest report.
- **T:** `backtest/data_quality` execution-valid gate; no closing-price feedback.
- **D:** held-out season. **Live:** **no.** **P:** F6. **Gate:** held-out economic metrics with CIs consistent with F6; no leakage. **Review:** independent. **`/clear`:** yes.

### F8 — Recommendation-only integration
- **G:** implement the §8 recommendation contract (read-only), abstention-first.
- **S:** a recommendation object + a read-only CLI/report; **no** staking, order, or portfolio path.
- **F:** new recommendation module + CLI route; tests. **I/O:** out = recommendation records (or explicit no-recommendation).
- **T:** every field present; abstains on missing/ambiguous/stale/OOD; execution surface not importable.
- **D:** F5–F7 artifacts. **Live:** current-quote reads only, bounded. **P:** F7. **Gate:** contract complete; fail-closed proven. **Review:** independent. **Prohibited:** staking/execution/portfolio. **`/clear`:** yes.

### F9 — Independent end-to-end review & shadow evaluation
- **G:** full-lane correctness review + forward shadow run (no bets).
- **S:** shadow evaluation on live pregame slates recording recommendation vs
  outcome/market, no action taken.
- **F:** shadow report only. **I/O:** out = shadow evaluation report.
- **T:** end-to-end reproducibility; PIT/leakage re-audit.
- **D:** live pregame slates (read-only). **Live:** current reads only. **P:** F8. **Gate:** shadow metrics consistent with backtest; independent sign-off. **Review:** independent (end-to-end). **`/clear`:** yes.

Boundaries may change if a later audit proves a better sequence; any change must be
justified against this baseline.

---

## 11. F0 deliverables & guarantees

- Created: `PHASE_F_RESEARCH_PLAN.md` (this file), `PHASE_F_FEATURE_CONTRACT.md`.
- Phase E behavior unchanged; **no migration**; schema remains **v16**.
- No feature, model, backfill, command, or recommendation output implemented.
- No live provider request or persisted ingestion occurred during F0 **or during
  either F0 independent-review pass** (findings re-confirmed at `44fcaf3`; §13 MLB-only
  scope option added).

---

## 12. F1B live capability-pilot specification (authorized only AFTER F1A)

**Not authorized yet. Do not execute.** This runs only after F1A ships the request/
credit controls and passes independent review, and a fresh `provider-audit` passes.
It is a **capability/coverage/credit** test — per §R.1 it does **not** create
strict-PIT data.

- **MLB date range:** a **current in-season** month (MLB regular season runs ~Apr–Sep;
  today 2026-07-28 is in-season, so a recent completed 30-day window is valid and
  minimizes credit while covering real games).
- **NBA date range:** a **past regular-season** month (NBA runs ~Oct–Apr; it is
  off-season now, so pick a completed in-season month, e.g. a month of the most recent
  regular season). Rationale: box/plays/lineups only exist for played regular-season
  games; a capability test needs real games, and NBA has none in July.
- **Why active-season ranges:** out-of-season windows return empty slates and cannot
  test per-game family coverage or credit cost.
- **Skeleton stage (first):** schedule/games only — MLB ≈ **1 request**, NBA ≈ **~4**.
  Verifies canonical-game creation + matching with near-zero credit.
- **Rich stage (second):** add box/results/inning + rosters (MLB) and box/player-stats/
  advanced/plays/lineups (NBA).
- **Expected games:** MLB ~400–450/month; NBA ~300–450/month.
- **Estimated requests:** MLB skeleton ~1, rich ~**1,350–1,800**; NBA skeleton ~4,
  rich ~**2,800 nominal**, worst-case **~17,500+** (plays 50 pages/game) — hence the
  hard cap is mandatory.
- **Estimated BALLDONTLIE credit usage:** derive from the F1A estimator per request;
  **stop at the user budget**.
- **Explicit hard-stop limits:** a per-run request cap and a credit cap (F1A);
  the run **halts before** exceeding either; a truncated sweep is reported.
- **Separate scratch DB:** a dedicated `--db` path (e.g. `data/pilot_scratch.db`),
  never the production/dev corpus.
- **Init & pre-run checks:** `db-init` the scratch DB; record pre-run table
  row-counts + a schema-version check (must be v16) and a file hash.
- **Provider audit immediately before each stage.**
- **Dry-run before persistence:** F1A's **true no-network** dry-run to preview the
  request/credit estimate; only then the live stage.
- **Post-run:** row counts, rejection/failed-family summaries, credit consumed +
  remaining, request count, truncations.
- **Matching sequence:** run canonical + reference matching on the scratch DB.
- **`data-status` then `data-quality`** on the scratch DB; record grade + open DQ.
- **Idempotent rerun:** re-run one stage; assert no duplicate observations.
- **Interrupted-run recovery:** kill mid-run; assert resume re-issues no already-
  fetched call and leaves no partial corruption.
- **Isolation proof:** confirm the production/existing user DB is untouched (hash/
  row-count unchanged).
- **Retention policy:** scratch DB and its raw responses are retained only for the
  review, then deleted (or explicitly archived with provenance); never promoted to a
  strict-PIT corpus.
- **Stop immediately if:** any cap is hit, `provider-audit` fails, unexpected auth/
  tier errors occur, rejections exceed a threshold, or the scratch-DB isolation check
  fails.

---

## 13. Optional future scope: MLB-only (explicit option, NOT a decision)

The project scope remains **MLB and NBA** (§0); the repository records no MLB-only
decision, so this section is an **explicit scope option**, not a change. It exists so
the consequences are understood if NBA is later deferred (e.g. to avoid the paid GOAT
subscription until MLB proves the approach).

### 13.1 BALLDONTLIE prerequisites that disappear
- **Gate G2 (paid BALLDONTLIE GOAT subscription + `NBA_DATA_API_KEY` /
  `NBA_DATA_TIER=goat`)** is no longer needed — this is the only strictly *paid,
  user-action* data prerequisite in the MVP, so MLB-only removes the sole mandatory
  purchase for corpus data.
- The **NBA half of Gate G3** (verifying BALLDONTLIE historical depth/coverage) drops;
  only MLB StatsAPI historical depth needs the F1B capability check.
- NBA-specific licensing questions (BALLDONTLIE terms) drop; MLB StatsAPI licensing
  (G4) still applies.

### 13.2 Code and documentation that remain harmlessly dormant
No code is removed. The following stays in the tree, compiled, typed, and tested, but
is simply never invoked under an MLB-only run: `sports_quant/ingest/nba_ingestor.py`,
`sports_quant/providers/balldontlie.py`, `sports_quant/ingest/hoopr_import.py`, the
NBA repositories (`db/repositories/nba.py`: results/team-stats/player-stats/quarter-
lines/plays/injuries), the NBA-specific schema tables (migrations `d012`/`d013`, which
stay present and **empty** — schema still v16, no migration to remove them), NBA
matching (Kalshi `KXNBAGAME` series), and the NBA branches of the probability feature/
dataset scaffolding. Ingestion code only issues requests when its CLI command is run,
so dormant NBA code incurs **zero** runtime, request, or credit cost.

### 13.3 Can MLB progress independently?
**Yes, fully.** The MLB lane depends only on MLB StatsAPI (keyless public), The Odds
API (MLB odds), Kalshi (`KXMLBGAME`), and weather (Open-Meteo/NWS) — none of which
touch BALLDONTLIE. `build_historical_dataset(conn, league="mlb")` and every downstream
subphase are already league-parameterized, so the MLB corpus (forward strict-PIT +
reconstructed research), features, baselines, calibration, EV, and backtest can
complete end-to-end with no NBA data present.

### 13.4 Effect of canceling NBA data access
Canceling the GOAT subscription affects **only future NBA ingestion** (a BALLDONTLIE
call would return `403` → handled as `TIER_RESTRICTED` / capability-unavailable, not a
crash). It does **not** touch stored code, the schema, or any already-persisted data;
NBA tables simply remain empty. No code deletion or migration is warranted — NBA can be
resumed later by restoring access, with no repository change.

### 13.5 Phase F gates that remain necessary for MLB-only
- **G1 (historical odds):** still applies — MLB sportsbook EV needs PIT-timestamped
  historical odds, which the current Odds API path cannot supply; else MLB sportsbook
  EV is forward-only (Kalshi `KXMLBGAME` EV unaffected).
- **PIT provenance (§R.1/R.4):** applies identically — retrospective MLB backfill is
  **not** strict-PIT; the forward + reconstructed corpus split is unchanged.
- **Licensing (G4):** MLB StatsAPI commercial/redistribution terms still need a
  decision before a large backfill.
- **Weather:** MLB is the weather-relevant league (outdoor venues); Open-Meteo
  (CC-BY non-commercial) / NWS licensing and PIT-eligibility gates remain. (NBA is
  indoor, so dropping NBA removes nothing here.)
- **Request controls (F1A):** still required — a rich MLB month is ~1,350–1,800
  requests with no per-run cap today. MLB StatsAPI is keyless, so the *credit-budget*
  portion is not needed for MLB, but the hard **request** cap, budget-halt, true
  no-network dry-run, resumability, and usage reporting are still mandatory before any
  live MLB pilot.
- **G5 (lineup/probable availability at T−60):** partially applies — MLB probable
  pitchers and lineups are the relevant availability-gated families.

### 13.6 Net
MLB-only is a clean, reversible sequencing option that removes the only paid data
prerequisite (GOAT) and the NBA capability/licensing checks, while leaving all NBA
code/schema dormant and intact. It does **not** reduce the knowledge-time, PIT-
provenance, historical-odds, licensing, weather, or request-control gates for MLB
itself. Adopt it only if the repository is updated to record the decision.
