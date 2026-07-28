# Phase F Feature Contract (proposed — not implemented)

Status: **proposal only.** No feature code, registry, or manifest exists yet.
This document is the design contract that Phase F feature work (subphase **F3**)
must satisfy. It is a companion to `PHASE_F_RESEARCH_PLAN.md`; read that first.

Schema remains **v16**. Nothing here is implemented during F0. Every rule below
is a *requirement on future code*, not a description of current behavior.

---

## 1. Why a contract before code

The completed point-in-time (PIT) foundation (Phase E) guarantees that a row's
feature *state* is computed only from observations visible at an explicit UTC
cutoff, and that label/closing-line information is physically separated from the
feature surface. A feature layer can quietly defeat that guarantee — by reading a
mutable "current" column, by using a lookback that straddles the cutoff, or by
letting a feature's value depend on rebuild-nondeterministic ids. This contract
exists so that every feature is forced through the E1 accessors and is auditable,
versioned, and reproducible **before** any modeling depends on it.

A feature is admitted to the initial model only if it can state, in the registry,
all of the fields in §3 and pass the leakage tests in §5. "The column exists" is
never sufficient justification.

---

## 2. Feature registry (proposed structure)

A single versioned, declarative registry (proposed: `probability/feature_registry.py`
plus a serialized `feature_manifest`), never free-form per-model feature code.

Each entry is immutable once published and addressed by a content hash. The
registry as a whole produces a **feature manifest** that is emitted alongside every
dataset build and every trained model, so a model can never be scored with a
feature set different from the one it was trained on.

### 2.1 Manifest contract (required fields)

The manifest is a deterministic, sorted, byte-stable document containing:

- `manifest_version` — semantic version of the feature set.
- `manifest_hash` — content hash over the ordered feature list + policies below.
- `league` — `mlb` or `nba` (manifests are league-specific; see F3).
- `cutoff_policy_id` — the decision-horizon policy id from the research plan
  (e.g. `pregame_t_minus_60`), so a manifest is bound to one horizon.
- `features` — an **ordered** list; order is part of the hash and defines the
  column order of `X`. Each element carries the §3 fields.
- `pit_layer_version` — the E1 accessor/registry version the manifest was built
  against, so an accessor change invalidates stale manifests.

### 2.2 Per-feature required fields (§3 summarized as schema)

```
name            # deterministic, stable, snake_case; never reused for a different meaning
dtype           # float32 | int32 | int8 | bool; fixed
unit            # explicit unit or "logit"/"probability"/"zscore"/"count"/"indicator"
source_tables   # exact SQL table(s) read, all asof_filtered/immutable/season_scoped
pit_accessor    # the sports_quant.pit accessor used (never a raw mutable-current read)
tx_time_rule    # exact transaction-time rule (which timestamp, strict/inclusive vs cutoff)
lookback        # window length or "since_season_start" or "career"; must end at/before cutoff
min_history     # minimum observations required or the feature emits its missing-value form
shrinkage       # prior/regularization/empirical-Bayes treatment toward a documented prior
missing_value   # explicit behavior: prior value + paired *_is_missing indicator (never silent 0)
correction_rule # how an append-only correction to a source row changes the feature (recompute)
league          # mlb | nba | both
leakage_risk    # none | low | medium | high, with the specific hazard named
initial_model   # include | defer, with a one-line reason
```

---

## 3. Feature-family catalog (proposed; evaluated, not implemented)

Legend for **initial_model**: `include` = candidate for the first baseline model;
`defer` = specified now, implemented in a later expansion. Every family routes
through a PIT accessor and pairs any missing value with an explicit
`*_is_missing` indicator; none may read a mutable "current" column directly.

| # | Family | League | Source tables (via PIT accessor) | Tx-time rule | Lookback / min history | Shrinkage / prior | Missing behavior | Leakage risk | Initial |
|---|--------|--------|----------------------------------|--------------|------------------------|-------------------|------------------|--------------|---------|
| 1 | Team-strength rating (Elo/rating) | both | `game_result_snapshots` / `nba_game_results`, `game_schedule_snapshots` | prior games' final result `observed_at` **strictly < cutoff** | expanding, since first available season; min 20 games | rating regresses to league mean between seasons | new team → league-mean prior + `_is_missing` | medium (must use only pre-cutoff finals) | include |
| 2 | Opponent-adjusted rolling form | both | result + team-game stats | source `observed_at` **< cutoff** | rolling 20–40 games | shrink to team rating | short history → rating fallback | medium | include |
| 3 | MLB starting pitcher state | mlb | `probable_pitcher_snapshots`, `player_game_statistics` | probable snapshot visible **≤ cutoff**; pitcher stats **< cutoff** | season + trailing N starts | empirical-Bayes to league pitcher mean | no confirmed probable → projected + `_is_missing` | high (probable can change) | include |
| 4 | MLB bullpen state / availability | mlb | `player_game_statistics`, roster/usage | prior appearances **< cutoff** | trailing 7–14 days | shrink to team mean | thin usage → team prior | medium | defer (v2) |
| 5 | Rest / travel / schedule density / B2B | both | `game_schedule_snapshots`, `games` | schedules visible **≤ cutoff** | prior N days | none (deterministic) | unknown prior game → neutral + `_is_missing` | low | include |
| 6 | Player availability / injuries | both | `injury_snapshots` (NBA), transactions (MLB) | snapshot `observed_at` **≤ cutoff** | latest as-of cutoff | none | no feed → `_is_missing` | high (status changes fast) | defer (NBA v2; MLB deferred) |
| 7 | Confirmed vs projected lineup strength | both | `lineup_snapshots`, `lineup_players` | latest lineup **≤ cutoff**; `is_confirmed` flag | as-of cutoff | shrink player values to prior | not posted by cutoff → projected + `confirmed_is_missing` | high | defer (needs late horizon) |
| 8 | Venue / home advantage | both | `games`, `venues` | immutable/season-scoped | static / season | league home-edge prior | unknown venue → league prior | low | include |
| 9 | Weather / roof state (MLB) | mlb | `weather_snapshots` (`current_forecast`, `pit_eligible=1`), `venues` | forecast observed **≤ cutoff**, PIT-eligible only | as-of cutoff | none | not eligible/absent → `_is_missing` | medium (eligibility must be proven) | defer (v2) |
| 10 | NBA lineup / rotation strength | nba | `lineup_snapshots`, player stats | lineup **≤ cutoff**; stats **< cutoff** | season | shrink to team prior | projected + `_is_missing` | high | defer |
| 11 | Pace / efficiency (NBA) | nba | `nba_team_statistics`, `nba_player_statistics` | source **< cutoff** | rolling 10–20 games | shrink to league mean | short history → league mean | low | include |
| 12 | Market-implied probability (benchmark/input) | both | `sportsbook_price_snapshots` (no-vig h2h), `kalshi_orderbook_snapshots` | quote `observed_at` **≤ cutoff**, freshness-bounded | latest valid quote at cutoff | none | no fresh quote → excluded from EV; `market_is_missing` for model | high (must never read settled/closing) | include as **benchmark**; as model input only behind an ablation gate |
| 13 | Cross-market disagreement | both | sportsbook + kalshi implied probs | both quotes **≤ cutoff** | at cutoff | none | one side missing → `_is_missing` | high | defer |
| 14 | Data-missingness indicators | both | derived | n/a | n/a | n/a | the indicators themselves | none | include (paired with every optional family) |

Notes:
- Family **12** (market-implied) is the primary **benchmark** the model must beat.
  Using it as a *model input* is allowed only behind an explicit ablation gate
  (F6) proving the model adds information beyond the market; it must never read a
  settled outcome, final price, or closing price.
- Families marked `high` leakage risk get a dedicated adversarial leakage test
  (§5) that plants a post-cutoff change and asserts the feature does not move.

---

## 4. Determinism requirements

- Feature values must be a pure function of `(cutoff, PIT-visible observations,
  manifest_hash)` — never of insertion order, ULID tiebreaks, or wall-clock.
- A fresh-database rebuild and a randomized-insertion-order rebuild must produce a
  **byte-identical** feature matrix for the same manifest (mirrors the E2
  determinism tests).
- Any equal-`observed_at` content conflict in a source row must fail closed
  through the existing `AsOfAmbiguityError`, never resolve to a silent winner.

---

## 5. Required leakage / correctness tests (F3 gate)

1. **Post-cutoff invariance** — plant an observation (result, lineup change,
   injury, price) dated after the cutoff; assert every feature value is unchanged.
2. **Correction propagation** — append a correction to a *pre-cutoff* source row;
   assert the affected feature recomputes and the manifest hash is unchanged.
3. **Missingness honesty** — remove an optional source; assert the feature takes
   its documented missing form and the paired `_is_missing` indicator flips, with
   no silent zero.
4. **Determinism** — fresh-rebuild and randomized-order rebuild are byte-identical.
5. **No mutable-current read** — a static check that feature accessors never touch
   a `forbidden_columns` / mutable-current field (reuses the E1 registry).
6. **Manifest binding** — scoring a model with a manifest whose hash differs from
   the training manifest fails closed.

---

## 6. Relationship to existing code

- `probability/features.py` `FeatureSpec` (fixed-size float32 layout, OOD bounds,
  prior-as-feature-0) is **reusable scaffolding**; its current *in-game* state
  body is not used for the pregame lane and is replaced by registry-driven pregame
  vectorizers.
- `probability/pregame_prior.py` (expectations → prior logit) is directly reusable
  as feature 0.
- `probability/uncertainty.py` `OODDetector` consumes the manifest's OOD bounds.
- No feature may reintroduce the synthetic `reference.py` / `datasets.py`
  generators; those remain synthetic/test-only.
