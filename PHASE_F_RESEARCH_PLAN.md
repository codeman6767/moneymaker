# Phase F — Research & Recommendation Plan (authoritative)

**Status:** F0 planning complete **and independently reviewed** (see §R). **F1A
(request/credit safety controls) is now implemented and locally validated; its
independent correctness review is still pending.** No corpus backfill, feature
engineering, model training, calibration, simulation, EV evaluation, backtesting,
or recommendation output has started. Schema remains **v16**. No live provider
request or persisted ingestion has occurred (F1A is offline; all provider behavior
is tested with mocked transports). **The live F1B pilot remains NOT authorized** —
it may run only after F1A passes independent review (and the external
prerequisites in §R.7 are met).

> **F1A implementation (this pass) — offline, review pending.** A shared typed
> request/credit control layer now gates the single transport chokepoint
> (`sports_quant/providers/base_provider.py:_get`): every attempt (initial call,
> each retry, each page) must reserve budget first, so a zero request budget makes
> zero transport calls and a run halts *before* exceeding a request or credit cap
> (`sports_quant/request_control.py`). Typed endpoint-cost policies
> (`sports_quant/ingest/cost_policies.py`) meter BALLDONTLIE credits (unknown
> endpoint → fail closed) and mark MLB StatsAPI credits *not applicable* (no
> fabricated balance). A genuine zero-network `--plan` mode + deterministic,
> secret-free request plans and pilot manifests with stable hashes
> (`planning.py`, `manifest.py`); a versioned external checkpoint with an atomic
> temp-file+replace write, a precise persist-commit consistency boundary, and
> verified resume (`checkpoint.py`, `pilot.py`); scratch-database isolation that
> classifies new/empty-v16/authorized-resumable/unsafe and never migrates or
> mutates (`scratch_db.py`); and CLI wiring on `ingest-mlb`/`ingest-nba`
> (`--plan`, `--pilot`, `--request-cap`, `--credit-cap`, `--max-games/pages/records`,
> `--scratch-db`, `--checkpoint`, `--resume`, `--manifest-out`) whose invalid
> combinations fail before any network or database work, with a distinct
> budget-exhaustion exit code (`4`). Reconstructed-corpus provenance is specified
> **design-only** in `RECONSTRUCTED_CORPUS_PROVENANCE.md` (no rows, no migration);
> the strict E1/E2 builder is unchanged. **No live request, ingestion, backfill,
> feature, model, simulation, recommendation, or execution work occurred.**

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
  *access* were probe-verified (2026-07-24); the read-only/GET-only/execution-
  quarantine invariants hold.
- **Claude can verify without exposing secrets:** a future `provider-audit` can
  confirm GOAT *access* and per-endpoint reachability (it reads no secret into
  output). Historical *depth* (G3) is measurable only by the F1B pilot.
- **User decision required before F1B:** confirm an active BALLDONTLIE **GOAT**
  subscription (G2); approve a per-run request/credit **budget**.
- **User decision required before F2 / large backfill:** licensing/retention (G4 —
  MLB StatsAPI commercial terms; Open-Meteo CC-BY non-commercial).
- **Purchase/subscription that may be required:** The Odds API **historical** plan
  (G1) or an alternative PIT-timestamped historical odds source; otherwise sportsbook
  EV is forward-only.
- **Unverified historical products:** MLB StatsAPI and BALLDONTLIE historical
  *depth/coverage* (G3); any commercial PIT historical dataset (Option C) — each
  requires an audited sample before acceptance, never accepted on advertisement.

The user currently expects GOAT access; this review neither exposes nor validates the
key — a future `provider-audit` confirms access safely.

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
