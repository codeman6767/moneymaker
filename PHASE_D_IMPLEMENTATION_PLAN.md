# Phase D — Implementation Plan

Concrete, staged implementation design for official MLB/NBA data, weather, and
canonical matching.

> **Status: Provider selection and implementation design complete; implementation
> not started.** No Phase D provider client, migration, or repository has been
> written. This document is the build contract; providers are chosen in
> `PHASE_D_PROVIDER_DECISIONS.md`.

Companion documents: `PHASE_D_PROVIDER_DECISIONS.md`, `DATA_ARCHITECTURE.md`,
`POINT_IN_TIME_DATA.md`, `ENTITY_MATCHING.md`, `DATA_FOUNDATION_PLAN.md`.

---

## 1. Existing components — reuse / extend / untouched / quarantine

**Reuse unchanged** (no duplication permitted):

- `sports_quant/http_policy.py` — GET-only, host+path allow-list. Phase D adds
  `for_mlb_statsapi()`, `for_balldontlie()`, `for_nba_stats()`, `for_open_meteo()`,
  `for_nws()` host rules. **The method rule (`GET` only) is never relaxed.**
- `sports_quant/redaction.py` — `sanitize_url/params/headers`, `STORABLE_RESPONSE_HEADERS`.
- `sports_quant/providers/raw_exchange.py` — `RawExchange` + `build_exchange` (the
  one sanitized capture used by every provider).
- `sports_quant/db/repositories/{raw_responses,ingestion_runs}.py` — raw-response
  preservation and run tracking. **No second audit system.**
- `sports_quant/db/schema.py` — `to_iso`, timestamp CHECK shape, provider
  constants, `APPEND_ONLY_TABLES` registry (extended, not replaced).
- `sports_quant/db/normalize.py` — the single name normaliser (write + read).
- `sports_quant/db/engine.py` — connections, transactions, migration runner,
  `split_sql_statements`.
- `streaming.event_envelope.canonical_json` — the one content hasher.
- `sports_quant/db/ids.py` — ULID + deterministic id construction (add prefixes).
- `probability/datasets.py::GameStateDataset` — the Phase E output contract; Phase D
  supplies the real rows/labels it will consume. **Untouched by D.**

**Extend (additive, test-covered):**

- `sports_quant/config.py` — add the Phase D settings (`NBA_DATA_API_KEY` etc.,
  pinned base URLs). Read-only invariants unchanged.
- `sports_quant/cli.py` — register the Phase D sub-commands.
- `sports_quant/db/migrations/` — new immutable migrations `d009`…`d012`.
- `sports_quant/db/repositories/` — new typed repositories.
- `sports_quant/db/models.py`, `ids.py`, `schema.py` — new row models / prefixes / constants.
- `intel/player_matching.py` — **extend, not replace**: back the in-memory
  directory with the new `provider_player_references` + `player_aliases`, keeping
  the `MATCHED / AMBIGUOUS / UNMATCHED` contract.
- `intel/base.py` models (`PlayerStatus`, `SourceType`, `ChangeType`,
  `SourceMeta`) — reuse the vocabulary when persisting injury/lineup snapshots.

**Leave untouched:** `evaluation/`, `state/`, `streaming/` (beyond reusing
`canonical_json`), `tracking/` (frame-level, optional per `CLAUDE.md`),
`backtest/`, `probability/` internals, `sports_quant/providers/{odds_api,kalshi}.py`
(reused as-is), all Phase A–C migrations (immutable).

**Quarantine (unchanged):** `gateway/` stays quarantined and is never imported by
any Phase D code. An isolation test (mirroring `test_isolation.py`) will assert no
Phase D module imports `gateway` and that the live lanes never import
`sports_quant.db`.

---

## 2. Schema plan (migrations after v8)

New immutable migrations, one global sequence continuing from `c008` (v8):

| Version | Migration | Adds |
| --- | --- | --- |
| 009 | `d009_provider_infra` | `provider_team_references`, `provider_player_references`, `provider_game_references`, `venues`, `venue_aliases`, `entity_match_decisions`, `match_candidates`, `data_quality_issues` |
| 010 | `d010_official_games_stats` | `game_schedule_snapshots`, `game_result_snapshots`, `team_game_statistics`, `player_game_statistics`, `mlb_inning_lines`, `roster_snapshots`, `probable_pitcher_snapshots`, `lineup_snapshots`, `lineup_players` |
| 011 | `d011_nba_specifics` | `nba_quarter_lines`, `injury_snapshots` |
| 012 | `d012_weather` | `weather_snapshots` |

Grouping follows the D2–D5 stages (§9). `venues`/`match`/`references`/`quality`
land first (D1) because every later stage references them.

### 2.1 Universal columns (every time-sensitive table)

Following the established Phase B/C pattern, **every** snapshot/observation row
carries:

```
provider              TEXT NOT NULL        -- e.g. 'mlb_statsapi','balldontlie','open_meteo'
provider_timestamp    TEXT                 -- provider's own event/update time (nullable)
published_at          TEXT                 -- when the SOURCE published it (nullable; e.g. injury PDF stamp)
observed_at           TEXT NOT NULL        -- when WE received the bytes (= raw_responses.received_at) -- the PIT cutoff
ingested_at           TEXT NOT NULL        -- when WE wrote the row
run_id                TEXT NOT NULL REFERENCES ingestion_runs(run_id)
raw_response_id       TEXT NOT NULL REFERENCES raw_responses(raw_response_id)   -- provenance (id)
raw_response_hash     TEXT NOT NULL        -- provenance (hash) -- survives export/merge
content_hash          TEXT NOT NULL        -- dedup identity of the observation content
created_at            TEXT NOT NULL
```

Mutable current-state tables (`venues`, canonical references, `entity_match_decisions`
review columns) additionally use the **c008 first/current provenance pattern**:
`first_raw_response_id` (immutable) + `current_raw_response_id` /
`current_raw_response_hash` (move only on a strictly-newer observation).

Append-only observation tables go in `schema.APPEND_ONLY_TABLES` and get
`BEFORE UPDATE/DELETE` triggers, exactly like `sportsbook_price_snapshots` and the
Kalshi snapshot tables.

### 2.2 Table sketches (authoritative DDL written at implementation time)

- **`provider_game_references`** — `(provider, provider_game_id)` UNIQUE →
  `game_id` (nullable until matched) + `match_decision_id`; the crosswalk from a
  provider's game id to the canonical `games.game_id`. `games.official_provider` /
  `games.official_game_key` still hold the single authoritative anchor.
- **`provider_team_references`** / **`provider_player_references`** — analogous
  crosswalks to `teams.team_id` / `players.player_id`, each with `match_decision_id`.
- **`venues`** — canonical venue: `venue_id` (deterministic), `name`, `city`,
  `latitude`, `longitude`, `timezone`, `roof_type` (`open|retractable|dome|fixed|indoor`),
  `is_outdoor` derived. Mutable current-state (c008 provenance).
- **`venue_aliases`** — provider venue strings → `venue_id` (mirrors `team_aliases`).
- **`game_schedule_snapshots`** — append-only provider observation of a game's
  schedule attributes (scheduled start, venue, doubleheader number/type, neutral
  site, status detail). **Drives** `game_status_history` (the existing canonical
  append-only status timeline) via `GameRepository.record_status`; the snapshot
  row carries the richer provider payload and its own provenance.
- **`game_result_snapshots`** — append-only home/away score, `status`, `is_final`,
  `is_correction`; the result label source. Sport-agnostic.
- **`team_game_statistics`** / **`player_game_statistics`** — append-only box
  lines per (game, team|player, provider, observed_at); wide but typed key stat
  columns + a canonical-JSON `extra` for the long tail. Sport-agnostic.
- **`mlb_inning_lines`** — append-only per (game, inning, half) R/H/E.
- **`nba_quarter_lines`** — append-only per (game, period, team) points.
- **`roster_snapshots`** — append-only (team, season, provider, observed_at)
  membership; feeds player matching / eligibility.
- **`probable_pitcher_snapshots`** — append-only (game, team, provider, observed_at)
  pitcher ref + `status` (`probable|confirmed|scratched`). **One table covers both
  "probable" and "confirmed starting pitcher"** as states in the announcement
  timeline (with an optional `superseded_by`), avoiding a second table for one
  concept — documented deviation from the task's separate-item listing.
- **`lineup_snapshots`** + **`lineup_players`** — append-only lineup header +
  ordered player rows (`slot`, `position`), `is_confirmed`, `confirmed_at`.
- **`injury_snapshots`** — append-only (player, provider, observed_at) `status`
  (reuse `intel.PlayerStatus`), `reason`, `published_at`, `is_correction`,
  `source_type` (reuse `intel.SourceType`).
- **`weather_snapshots`** — append-only (game|venue, provider, observed_at)
  `is_forecast` + `forecast_for` (NN when forecast), temp/wind/precip/humidity,
  `is_dome`. Forecast-vs-actual kept distinct (leakage vector — see §5).
- **`entity_match_decisions`** — the decision log from `ENTITY_MATCHING.md` §7:
  `entity_type`, `source_provider`, `source_ref`, `outcome`
  (`accepted|rejected|ambiguous|no_candidate|manual_override`), `method`, `score`,
  `threshold`, `rejection_reason`, `needs_manual_review`, `matcher_version`,
  `decided_at`. Append-only except the review columns (`reviewed_by/at`,
  `needs_manual_review`).
- **`match_candidates`** — **normalized child** of `entity_match_decisions`
  (one row per candidate considered, with per-candidate score + tier). Deviation
  from the earlier `candidates_json` blob sketch, mirroring the
  `kalshi_orderbook_levels` precedent (normalized rows over an opaque JSON blob);
  documented in `ENTITY_MATCHING.md`.
- **`data_quality_issues`** — `severity` (`blocking|issue|note`), `rule_code`,
  `entity_type`, `entity_id`, `description`, `detected_at`, `resolved_at`, plus
  the Phase D `DQ-*` codes (§5, §6).

**No second canonical-game table.** `games` remains the one canonical game;
everything above references it or a provider crosswalk to it.

---

## 3. Historical correction behaviour (append-only)

Every observation is append-only; **current state is derived, never overwritten.**
The rule reuses the Phase B/C mechanism: dedup an observation only against its
**immediate temporal predecessor** by content hash; recompute current state from
the **newest observation by `(observed_at, id)`**; an older backfill is stored but
never regresses current state (deterministic tie-break by monotonic ULID).

| Event | Representation | Current-state selection |
| --- | --- | --- |
| Postponed game | new `game_schedule_snapshots` + `game_status_history` row `status='postponed'`; `games.scheduled_start` updated, `original_start` immutable | newest status observation |
| Rescheduled game | new snapshot with new `game_date_local`/start; `official_game_key` stable across the move | newest observation; provider key anchors identity |
| Cancelled game | status `cancelled` snapshot | newest observation |
| Suspended MLB game | status `suspended` then `in_progress`/`final` on resumption; one `game_id` | newest observation |
| MLB doubleheader | two `games` rows (`game_number` 1/2); each its own snapshots | per-game newest |
| Score correction | new `game_result_snapshots` with `is_correction=1`; prior row preserved | newest result observation |
| Stat correction | new `team/player_game_statistics` row (changed content hash); prior preserved | newest observation |
| Probable→confirmed→scratched pitcher | successive `probable_pitcher_snapshots` (`status` transitions, `superseded_by`) | newest observation |
| Lineup change / NBA late scratch | new `lineup_snapshots`/`injury_snapshots` observation; prior preserved | newest by `observed_at` |
| Injury status change | new `injury_snapshots` (reuse `intel.PlayerStatus`) | newest by `observed_at` |
| Weather forecast change | new `weather_snapshots` (`is_forecast=1`, `forecast_for` fixed, `observed_at` advances) | as-of `observed_at ≤ cutoff` (never the actual) |

**Older backfill never regresses current metadata** — proven the same way as the
Phase C `c008` provenance/stale-backfill tests.

---

## 4. Canonical game & market matching

Implements `ENTITY_MATCHING.md` §4–§6; deterministic first, ambiguity never
silently accepted, **market price never used as evidence**.

**Schedule key:** `(league_id, game_date_local, home_team_id, away_team_id, game_number)`,
each team resolved through team matching first (fail → stop, `no_candidate`).
`game_date_local` uses the **home venue's timezone** (`venues.timezone`), not UTC.

**Tiers (per `ENTITY_MATCHING.md` §4.2), score / condition:**

| Tier | Method | Score | Condition |
| --- | --- | --- | --- |
| 1 | `official_key` | 1.00 | provider exposes the official game id (StatsAPI `gamePk`, balldontlie game id) |
| 2 | `schedule_key_exact` | 0.95 | both teams resolved, same local date, start within **±90 min** |
| 3 | `schedule_key_window` | 0.88 | both teams resolved, start within **±12 h** (date-boundary/postponement drift) |
| 4 | `title_rules` | 0.85 | Kalshi only — parsed title + `rules_primary` cross-check |

**Thresholds:** accept ≥ **0.85**; two or more candidates in the winning tier →
**AMBIGUOUS** (`needs_manual_review=1`, never fall through to a weaker tier); zero
candidates → **no_candidate** (`needs_manual_review=1`). Thresholds stored per
decision. Deterministic tie-break by candidate id when scores tie. Hard cases
(neutral site swap, postponement, reschedule, both doubleheader types, suspension)
per `ENTITY_MATCHING.md` §4.3. Every attempt writes exactly one
`entity_match_decisions` row plus its `match_candidates` children — including the
losers. Point-in-time: a dataset as of T may use only decisions with
`decided_at ≤ T` (DQ-PIT-010).

Chain: **official game → canonical `games`** (D5 populates `games` +
`official_provider/key`), then **sportsbook_events → games** and **kalshi_events/
markets → games** by the same schedule key, each recording a decision. Kalshi adds
the title/rules cross-check and `rules_hash`-change detection (deferred from Phase C).

---

## 5. Player matching (extends `intel/player_matching.py`)

Keep the `MATCHED / AMBIGUOUS / UNMATCHED` contract and the deterministic
normaliser; **do not replace it.** Back the directory with
`provider_player_references` + `player_aliases`.

Evidence, in order: **provider player id** (exact → MATCHED outright) →
`(team, normalized_full_name)` → `(league, normalized_full_name)`, with **suffix**
binding (a present suffix must match) and **active-season** filtering. Birth date
used **only** when legitimately supplied and needed to break a genuine collision
(e.g. two same-name players active the same season). Two players are **never**
resolved on name alone → AMBIGUOUS. An unknown player is UNMATCHED (a curation
task), **never** a silently-created duplicate canonical player. The Chadwick
register bridges MLBAM↔other ids for MLB; balldontlie ids anchor NBA. Every
resolution writes an `entity_match_decisions` row (`entity_type='player'`).

---

## 6. Point-in-time & leakage rules (authoritative time per category)

`observed_at` (= `raw_responses.received_at`) is the **only** cutoff for every
as-of query and training join; `provider_timestamp`/`published_at` are for lag
measurement and within-provider ordering only, **never** cutoffs.

| Hazard | Authoritative time | Defence | Rule |
| --- | --- | --- | --- |
| Final scores in pregame data | result `observed_at` | results read only from `game_result_snapshots` as-of; `games.status` unreachable from `pit/` | DQ-PIT-001 |
| Postgame stats in pregame rows | stat `observed_at` | box stats never precomputed; computed inside the as-of window | DQ-PIT-002 |
| Confirmed lineups before publication | lineup `observed_at` | `lineup_snapshots` as-of; `is_confirmed` true only if a snapshot said so by the cutoff | DQ-PIT-003 |
| Probable-pitcher change before observation | snapshot `observed_at` | `probable_pitcher_snapshots` as-of, never `published_at` | DQ-PIT-004 |
| Injury status before publication | injury `observed_at` | `injury_snapshots` as-of; `published_at` for lag only | DQ-PIT-004 |
| NBA late scratch before observation | injury/lineup `observed_at` | as-of on `observed_at` | DQ-PIT-004 |
| Future weather forecast | forecast `observed_at`, subject `forecast_for` | only forecasts with `observed_at ≤ cutoff`; the *actual* is never a pregame feature | DQ-PIT-005w |
| Corrected stats before the correction was seen | correction `observed_at` | corrections are new appended rows; as-of hides later corrections | DQ-PIT-002 |
| Closing prices before the cutoff | price `observed_at` | Phase B `sportsbook_price_snapshots` as-of; closing line evaluation-only module | DQ-PIT-005 |
| Future match decisions | `entity_match_decisions.decided_at` | joins use only `decided_at ≤ cutoff` | DQ-PIT-010 |
| Cross-provider clock skew | `observed_at` (our single clock) | never order across providers by `provider_timestamp` | DQ-PIT-009 |

Adversarial fixtures (one planted leak per rule) are a **Phase E** gate; Phase D
supplies the schema, `observed_at` discipline, and as-of accessors they test.

---

## 7. CLI commands

All GET-only, read-only, sanitized, idempotent; exit codes reuse the existing
vocabulary (`0` success incl. clean skip/zero-results; `1` genuine active failure;
`2` read-only startup violation; `3` db missing/unmigrated). `--dry-run` performs
the external GET(s) + normalization and **persists nothing** (the Phase B/C
contract). Every command reports sanitized counts and records an `ingestion_runs`
row (with the c008 `records_updated` counter).

| Command | Provider | Required | Optional | Notes |
| --- | --- | --- | --- | --- |
| `ingest-mlb` | MLB StatsAPI | — | `--from --to` (date range), `--game-pk`, `--include {results,box,probables,lineups}`, `--db --dry-run` | date-range paging by day; idempotent on content hash; zero games ≠ failure |
| `ingest-nba` | balldontlie (fallback stats.nba.com) | — | `--from --to`, `--game-id`, `--include {results,box,quarters}`, `--db --dry-run` | cursor pagination; free-tier rate-limit backoff |
| `ingest-injuries --sport nba` | NBA injury PDF (or paid) | `--sport` | `--date`, `--db --dry-run` | parses the official report; `data_quality_issues` on parse failure; `--sport mlb` → StatsAPI transactions |
| `ingest-lineups --sport mlb` | MLB StatsAPI | `--sport` | `--date`, `--game-pk`, `--db --dry-run` | posted lineups + probable/confirmed pitchers; NBA → limited/unavailable path |
| `ingest-weather` | Open-Meteo (NWS fallback) | — | `--from --to`, `--game-pk`, `--forecast/--actual`, `--db --dry-run` | outdoor MLB only (gate by `venues.roof_type`); historical-forecast for leakage-free pregame |
| `ingest-venues` | MLB StatsAPI `/venues` | — | `--db --dry-run` | seeds `venues` + `venue_aliases` (coords/roof/tz) |
| `match-games` | none (compute) | — | `--league`, `--since`, `--db --dry-run` | official→canonical, sportsbook→canonical; writes decisions+candidates |
| `match-markets` | none (compute) | — | `--since`, `--db --dry-run` | Kalshi→canonical (title/rules); `rules_hash`-change detection |
| `matching-review` | none | — | `--entity-type`, `--reason`, `--json` | lists open `needs_manual_review` grouped by reason; read-only |

**Rate-limit handling:** conservative single-flight, exponential backoff on 429/503,
respect any documented per-tier QPS; a truncated sweep is **reported explicitly**
(never silently capped), mirroring the Kalshi `--limit` behaviour. **Failure**
preserves the raw response before parsing, records a sanitized `ingestion_runs`
failure + `data_quality_issues`, and exits non-zero only when something active
failed.

---

## 8. Credentials & config

Add to `sports_quant/config.py` (all `SecretStr`, `.env`-only, sanitized
everywhere): `nba_data_api_key`, optional `weather_api_key`,
`sportradar_mlb_api_key`, `sportradar_nba_api_key`; and **pinned base URLs** for
the undocumented league endpoints (like `PRODUCTION_KALSHI_REST_URL`) so an
arbitrary host cannot be substituted. `.env.example` gains blank placeholders only
(done in this planning pass). No-key providers need no variable. Read-only startup
invariants are unchanged.

---

## 9. Implementation stages (D1–D5)

Each subphase is independently green under Ruff + mypy + pytest before the next
begins. Model column = recommended driver for that stage.

### D1 — Provider infrastructure  ·  model: **OpusPlan** (cross-cutting foundations)

- **Create:** `sports_quant/providers/{mlb_statsapi,balldontlie,nba_stats,open_meteo,nws}.py`
  (policy-wrapped GET clients returning `RawExchange`), provider config in
  `config.py`, `http_policy` host rules + `for_*` classmethods, migration
  `d009_provider_infra`, repositories
  `db/repositories/{references,venues,matching,data_quality}.py`, models/ids/schema
  constants, test fixtures (small sanitized samples), an isolation test.
- **Modify:** `http_policy.py`, `config.py`, `.env.example` (done), `cli.py`
  (`ingest-venues`), `db/{models,ids,schema}.py`, `pyproject.toml` (packages/testpaths;
  add a PDF parser dep **only** if the NBA-injury PDF path is built in D3).
- **Migration:** `d009` (v9). **Tables:** references ×3, venues, venue_aliases,
  entity_match_decisions, match_candidates, data_quality_issues.
- **Completion:** migration applies once + idempotent; new hosts GET-only and
  account/order paths still blocked; no key printed/stored; venues seedable from a
  mocked StatsAPI fixture; gateway never imported.
- **Expected blockers:** confirming exact host/path allow-list entries; stats.nba.com
  header requirements; verifying terms (§7 of decisions doc).

### D2 — MLB ingestion  ·  model: **Sonnet** (well-scoped, one provider)

- **Create:** `sports_quant/ingest/mlb_ingestor.py`; repositories for schedule/
  result/stats/inning/roster/probable/lineup; tests (mocked StatsAPI fixtures).
- **Modify:** `cli.py` (`ingest-mlb`, `ingest-lineups --sport mlb`).
- **Migration:** `d010` (v10). **Tables:** game_schedule/result snapshots,
  team/player_game_statistics, mlb_inning_lines, roster_snapshots,
  probable_pitcher_snapshots, lineup_snapshots, lineup_players. **Provider:** MLB StatsAPI.
- **Completion:** a mocked date-range sweep persists games (canonical `games` +
  provenance), results, box, inning lines, probables, lineups; idempotent twice;
  append-only enforced; every row traces to a raw response; live smoke-test safe.
- **Expected blockers:** doubleheader/game-number resolution; mapping StatsAPI
  status codes to canonical `game_status_history`; stat-field coverage.

### D3 — NBA ingestion  ·  model: **Sonnet** (mirrors D2)

- **Create:** `sports_quant/ingest/nba_ingestor.py`; nba-specific repositories;
  optional `sports_quant/providers/nba_injury_report.py` (PDF parser) **only if**
  the injury path is built; tests (mocked balldontlie/stats.nba fixtures + a small
  fixture PDF).
- **Modify:** `cli.py` (`ingest-nba`, `ingest-injuries --sport nba`).
- **Migration:** `d011` (v11). **Tables:** nba_quarter_lines, injury_snapshots
  (box/result/roster reuse d010). **Provider:** balldontlie (fallback stats.nba.com);
  injuries: official PDF or paid or **unavailable path**.
- **Completion:** mocked sweep persists games/results/box/quarters; injuries either
  ingested from the fixture PDF or the unavailable path records `data_quality_issues`;
  NBA starters unavailable path exercised; idempotent; append-only.
- **Expected blockers:** balldontlie free-tier rate limits; stats.nba.com 403/header
  handling; PDF layout fragility; the honest "no free pregame starters" gap.

### D4 — Weather ingestion  ·  model: **Sonnet**

- **Create:** `sports_quant/ingest/weather_ingestor.py`; weather repository; tests
  (mocked Open-Meteo/NWS fixtures).
- **Modify:** `cli.py` (`ingest-weather`).
- **Migration:** `d012` (v12). **Tables:** weather_snapshots (venues from d009).
  **Provider:** Open-Meteo (NWS fallback).
- **Completion:** forecast + actual persisted distinctly; outdoor-only gating by
  `venues.roof_type`; leakage-free historical-forecast path; dome venues skipped;
  idempotent; append-only.
- **Expected blockers:** venue coord/roof accuracy; Open-Meteo historical-forecast
  API shape; NWS US-only coverage (Toronto).

### D5 — Canonical matching  ·  model: **OpusPlan** (subtle correctness)

- **Create:** `sports_quant/matching/{__init__,normalize,teams,players,games,markets,decisions}.py`
  (import `db/normalize.py` — one normaliser), matching repository glue; tests
  (determinism under shuffles, ambiguity refusal, every §4.3 hard case,
  decision-completeness, title/rules disagreement).
- **Modify:** `cli.py` (`match-games`, `match-markets`, `matching-review`);
  `intel/player_matching.py` (back with `player_aliases`/references, API unchanged).
- **Migration:** none required if `entity_match_decisions`/`match_candidates`
  landed in d009 (a small `d013` only if review columns need widening). **Populates**
  `games.official_*`, `provider_*_references`, sportsbook/Kalshi `game_id` +
  `match_decision_id`.
- **Completion:** a fixture slate matches end-to-end with every decision + candidate
  recorded; ambiguity/no-candidate never silently accepted; determinism under 100
  shuffles; price never used as evidence; `matching-review` lists open items.
- **Expected blockers:** venue-timezone-driven `game_date_local`; doubleheader
  ambiguity; Kalshi ticker/title/rules parsing; neutral-site orientation.

---

## 10. Verification gates (every subphase)

`ruff check .` clean; `mypy . --no-incremental` zero project-source errors (no
global ignores, no broad `type: ignore`); `pytest -q` zero failures; migrations
apply once + idempotent (second `db-init` no-ops); **no live network call in the
test suite** (mocked transports/fixtures only); **no credential** in any output/
log/stored column (whole-DB sweep like Phase B); `providers-check` still passes;
**GET-only**; execution remains quarantined; append-only history preserved.

---

## 11. Open decisions carried from provider selection

See `PHASE_D_PROVIDER_DECISIONS.md` §7–§8: personal-vs-commercial intent (governs
whether undocumented endpoints are acceptable at all), NBA-injury mechanism, weather
licensing, and optional Retrosheet deep-history backfill. These are **user
decisions**; the plan supports either the MVP (no-paid, risk-labelled) or the
professional (paid, clean-licence) path without rework, because every provider sits
behind an adapter.
