# Phase F — Research & Recommendation Plan (authoritative)

**Status:** F0 planning complete. **No** Phase F implementation, corpus backfill,
feature engineering, model training, calibration, simulation, EV evaluation,
backtesting, or recommendation output has started. Schema remains **v16**. No live
provider request or persisted ingestion occurred while producing this plan.

**Baseline commit:** `631377a` (Phase E complete and independently reviewed; CI #54
green). This document is the authoritative roadmap for turning the completed
point-in-time (PIT) data foundation into a rigorously validated, **pregame**
MLB/NBA game-winner recommendation model.

Companion: `PHASE_F_FEATURE_CONTRACT.md` (feature registry + manifest contract).

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

The model is **not** declared viable until real-corpus gates pass. No profitability
or accuracy claim may precede a corpus that clears the acceptance gates below.

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

- **F1 pilot before F2 backfill.** F1 ingests a **bounded** slice (one recent
  season-month per league) to measure real historical coverage (resolves G3),
  verify matching, and estimate per-season request-credit cost — *before* any large
  backfill.
- Backfill order: **oldest → newest, provider by provider, one league at a time**,
  official data (games/schedule/results) first, then stats/rosters/probables/
  lineups/injuries/weather, then market data.
- **Credit budget caps** per run (Odds API `CreditHeaders`, BALLDONTLIE tier
  limits); a run halts and reports when a budget cap is hit rather than exceeding it.
- **Restartability & idempotency:** every backfill run is resumable, idempotent
  (re-ingesting produces no duplicate observations), correction-aware (append-only),
  and **bounded** (explicit date range + record cap; a truncated sweep is reported).
- A fresh **`provider-audit` must pass immediately before** each backfill stage.
- **Independent correctness review after F1 and after F2** (coverage, leakage,
  determinism, DQ grade) before proceeding.

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

### F1 — Historical corpus pilot & capability verification
- **G:** measure real historical coverage/credit-cost; resolve G3.
- **S:** bounded date-ranged ingest of one recent season-month per league; run
  matching on it; measure coverage/latency/credit.
- **F:** none new in the research lane; uses existing ingestors/CLI. A pilot report
  doc only.
- **I/O:** in = provider APIs (bounded); out = pilot coverage report + populated dev DB slice.
- **T:** coverage/idempotency/restartability assertions on the pilot slice.
- **D:** one season-month, both leagues. **Live:** **yes, bounded** (first allowed live requests; provider-audit must pass first).
- **P:** G2 (NBA GOAT key), passing `provider-audit`. **Gate:** measured per-family coverage documented; determinism holds. **Review:** independent. **Prohibited:** large backfill, features, models. **`/clear`:** yes before F1.

### F2 — Controlled persisted backfill
- **G:** build the accepted MLB/NBA corpus.
- **S:** date-ranged backfill oldest→newest per §3.3; run matching; produce
  `data-quality` grade.
- **F:** none new (existing ingest/match CLI). **I/O:** out = persisted corpus + DQ report.
- **T:** §3.2 acceptance gates; PIT determinism; leakage scan (`data-quality`).
- **D:** target seasons (§3.1). **Live:** **yes, bounded + credit-capped.**
- **P:** F1 passed; G1/G4 licensing decision; credit budget. **Gate:** §3.2 all pass, `corpus_valid=true`. **Review:** independent. **Prohibited:** features/models. **`/clear`:** yes.

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
- No live provider request or persisted ingestion occurred during F0.
