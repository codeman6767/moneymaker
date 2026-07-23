# Phase D — Implementation Plan

Concrete, staged implementation design for official MLB/NBA data, weather, and
canonical matching.

> **Status: D1 provider infrastructure complete (incl. the `d010` audit-integrity
> repair); D2–D5 not started.** D1 built the typed provider-capability system, the
> four provider clients (MLB StatsAPI, BALLDONTLIE, NWS, Open-Meteo) over a shared
> GET-only base, the `http_policy` allow-lists, the pinned/validated config,
> migrations `d009_provider_infra` and `d010_provider_audit_integrity` (schema
> v10), the references/venues/matching/data-quality/capabilities repositories, and
> the evidence-backed multi-probe `provider-audit` + `ingest-venues` CLI commands —
> all tested against mocked transports (no live provider call was made). The audit
> separates declared from externally observed capabilities (§10). D2–D5
> (MLB/NBA/weather ingestion + canonical matching) remain unbuilt. This document
> is the build contract; providers are chosen in `PHASE_D_PROVIDER_DECISIONS.md`
> (doc-review date 2026-07-23).

Companion documents: `PHASE_D_PROVIDER_DECISIONS.md`, `DATA_ARCHITECTURE.md`,
`POINT_IN_TIME_DATA.md`, `ENTITY_MATCHING.md`, `DATA_FOUNDATION_PLAN.md`.

---

## 1. Existing components — reuse / extend / untouched / quarantine

**Reuse unchanged** (no duplication permitted):

- `sports_quant/http_policy.py` — GET-only, host+path allow-list. Phase D adds
  `for_mlb_statsapi()`, `for_balldontlie()`, `for_nws()`, `for_open_meteo()` host
  rules. **The method rule (`GET` only) is never relaxed.** `stats.nba.com` is
  **not** added (not selected).
- `sports_quant/redaction.py` — `sanitize_url/params/headers`, `STORABLE_RESPONSE_HEADERS`.
- `sports_quant/providers/raw_exchange.py` — `RawExchange` + `build_exchange`
  (the one sanitized capture used by every provider).
- `sports_quant/db/repositories/{raw_responses,ingestion_runs}.py` — raw-response
  preservation and run tracking. **No second audit system.**
- `sports_quant/db/schema.py` — `to_iso`, timestamp CHECK shape, provider
  constants, `APPEND_ONLY_TABLES` registry (extended, not replaced).
- `sports_quant/db/normalize.py` — the single name normaliser (write + read).
- `sports_quant/db/engine.py` — connections, transactions, migration runner.
- `streaming.event_envelope.canonical_json` — the one content hasher.
- `sports_quant/db/ids.py` — ULID + deterministic id construction (add prefixes).
- The Odds API (`providers/odds_api.py`) + Kalshi (`providers/kalshi.py`) clients
  and their Phase B/C ingestion — **reused as-is, never duplicated.** They already
  supply sportsbook prices and Kalshi markets/books/trades.
- `probability/datasets.py::GameStateDataset` — the Phase E output contract; Phase D
  supplies the real rows/labels. **Untouched by D.**

**Extend (additive, test-covered):**

- `sports_quant/config.py` — add Phase D settings (`NBA_DATA_API_KEY`, optional
  `WEATHER_API_KEY`/`SPORTRADAR_*`) and **pinned base URLs** (`MLB_STATS_API_BASE_URL`,
  `NWS_BASE_URL`, `OPEN_METEO_BASE_URL`). Read-only invariants unchanged.
- `sports_quant/cli.py` — register the Phase D sub-commands (incl. `provider-audit`).
- `sports_quant/db/migrations/` — new immutable migrations `d009`…`d013`
  (`d009`/`d010` built; `d011`–`d013` planned).
- `sports_quant/db/repositories/` — new typed repositories.
- `sports_quant/db/models.py`, `ids.py`, `schema.py` — new row models / prefixes / constants.
- `intel/player_matching.py` — **extend, not replace**: back the in-memory
  directory with the new `provider_player_references` + `player_aliases`, keeping
  the `MATCHED / AMBIGUOUS / UNMATCHED` contract.
- `intel/base.py` vocabulary (`PlayerStatus`, `SourceType`, `ChangeType`,
  `SourceMeta`) — reused when persisting injury/lineup snapshots.

**Leave untouched:** `evaluation/`, `state/`, `streaming/` (beyond `canonical_json`),
`tracking/` (frame-level, optional per `CLAUDE.md`), `backtest/`, `probability/`
internals, the Odds/Kalshi provider clients, all Phase A–C migrations (immutable).

**Quarantine (unchanged):** `gateway/` stays quarantined and is never imported by
any Phase D code. An isolation test asserts no Phase D module imports `gateway`
and the live lanes never import `sports_quant.db`.

**Offline supplements are not runtime dependencies:** pybaseball/Statcast/FanGraphs
(MLB) and **hoopR** (NBA) are offline-only, imported across a typed **Parquet**
boundary. They are **not** in core `pyproject.toml`, **not** required at live
startup, and **never** live-called by the recommendation app. **R is never
required at runtime.** SportsDataIO Discovery Lab is an optional delayed
**comparison** source, never the live feed.

---

## 2. Provider capability system (D1)

D1 defines **typed provider capabilities** rather than inferring them from a
provider's name or from mere key possession. This is the mechanism that keeps the
plan honest about the BALLDONTLIE tiers.

### 2.1 Capability catalogue

`ProviderCapability` (enum), one per data kind: `teams`, `players`, `games`,
`schedules`, `game_results`, `team_statistics`, `player_statistics`,
`inning_lines`, `quarter_lines`, `injuries`, `probable_pitchers`, `lineups`,
`confirmed_pregame_starters`, `plays`, `substitutions`, `correction_timestamps`,
`venues`, `historical_depth`, `live_availability`.

### 2.2 Capability states

`CapabilityState` (enum): `supported`, `unsupported`, `paid_tier_required`,
`best_effort`, `unavailable`, `unknown_until_audited`.

### 2.3 Declared, not inferred

Each provider ships a typed **capability declaration** (a table of
`{capability: state}`) plus its **selected tier** (for BALLDONTLIE: `free |
all_star | goat`). The declaration is the source of truth; an ingestor consults it
before requesting a capability and records the state on every affected row/DQ
entry. Examples (per `PHASE_D_PROVIDER_DECISIONS.md`, re-verified at D1):

- **MLB StatsAPI:** schedules/games/results/inning_lines/team+player_statistics/
  probable_pitchers/lineups(posted)/venues/rosters = `supported`;
  `confirmed_pregame_starters` = `unavailable`; `correction_timestamps` =
  `unsupported` (inferred via content hash → `best_effort`).
- **BALLDONTLIE @ GOAT:** teams/players/games/schedules/game_results/
  player_statistics/team_statistics/injuries/plays/quarter_lines(derivable) =
  `supported`; `lineups` = `best_effort` (*when available*);
  `confirmed_pregame_starters` = `unavailable`; `correction_timestamps` =
  `unsupported`. At **ALL-STAR** the box/plays/lineups capabilities become
  `paid_tier_required`; at **Free**, player_statistics/injuries/box/plays/lineups
  are all `paid_tier_required`.
- **NWS / Open-Meteo:** weather forecast/actual = `supported`; Open-Meteo adds the
  leakage-free historical-forecast = `supported`; NWS non-US = `unavailable`.

### 2.4 Tier-error semantics (mandatory)

**Key possession never implies GOAT access.** A provider tier/authorization error
(e.g. BALLDONTLIE `403`/quota-for-tier) is classified and reported as
**"capability unavailable for current subscription tier"** and written as a
`data_quality_issues` / capability record — **never** as an invalid key, a network
bug, or an application defect. The ingestion run finishes with an honest status
(the capability was unavailable, not "failed"), and other capabilities proceed.

---

## 3. Schema plan (migrations after v8)

New immutable migrations, one global sequence continuing from `c008` (v8):

| Version | Migration | Adds |
| --- | --- | --- |
| 009 | `d009_provider_infra` *(built)* | `provider_team_references`, `provider_player_references`, `provider_game_references`, `venues`, `venue_aliases`, `entity_match_decisions`, `match_candidates`, `data_quality_issues`, `provider_capabilities` |
| 010 | `d010_provider_audit_integrity` *(built)* | `provider_capabilities` evidence columns (`declared_state`/`observed_state`/`is_observed`/`probe_name`/`endpoint`/`http_status`/`error_kind`/`verified_at`) separating declared from observed; partial unique index on `venue_aliases (provider, provider_venue_id)`; `data_quality_issues` resolution-only-update + no-delete triggers |
| 011 | `d011_official_games_stats` *(planned)* | `game_schedule_snapshots`, `game_result_snapshots`, `team_game_statistics`, `player_game_statistics`, `mlb_inning_lines`, `roster_snapshots`, `probable_pitcher_snapshots`, `lineup_snapshots`, `lineup_players` |
| 012 | `d012_nba_specifics` *(planned)* | `nba_quarter_lines`, `injury_snapshots`, `play_snapshots` (GOAT plays / substitutions; sport-agnostic-ish) |
| 013 | `d013_weather` *(planned)* | `weather_snapshots` |

### 3.1 Universal columns (every time-sensitive table)

Following Phase B/C, **every** snapshot/observation row carries: `provider`,
`provider_timestamp` (nullable), `published_at` (nullable), `observed_at`
(NN — the PIT cutoff, `= raw_responses.received_at`), `ingested_at`, `run_id`
(→ `ingestion_runs`), `raw_response_id` (→ `raw_responses`), `raw_response_hash`,
`content_hash`, `created_at`. Mutable current-state tables (`venues`, the
references, `entity_match_decisions` review columns) use the **c008 first/current
provenance** (`first_raw_response_id` immutable + `current_raw_response_id` /
`current_raw_response_hash`). Append-only observation tables go in
`schema.APPEND_ONLY_TABLES` with `BEFORE UPDATE/DELETE` triggers.

### 3.2 New / notable tables

- `provider_capabilities` — persisted capability declarations
  `(provider, tier, capability) → state`, with `observed_at`/`run_id` provenance,
  so a corpus records which capabilities were available (and at which tier) when
  each row was ingested. Written by `provider-audit` and each ingestor.
- `provider_{team,player,game}_references` — `(provider, provider_id)` UNIQUE →
  canonical id (nullable until matched) + `match_decision_id`. Crosswalks; no
  second canonical-game table (`games.official_provider/official_game_key` is the
  anchor).
- `venues` / `venue_aliases` — canonical venue (`latitude`, `longitude`,
  `timezone`, `roof_type ∈ {open,retractable,dome,fixed,indoor}`, `is_outdoor`
  derived) + provider alias strings.
- `game_schedule_snapshots` / `game_result_snapshots` — append-only schedule +
  result observations; results carry `is_correction`.
- `team_game_statistics` / `player_game_statistics` — append-only box lines; typed
  key columns + canonical-JSON `extra`.
- `mlb_inning_lines` / `nba_quarter_lines` — append-only per-period lines.
- `roster_snapshots` — append-only membership.
- `probable_pitcher_snapshots` — append-only `status ∈ {probable,confirmed,scratched}`
  with `superseded_by`; **one table** covers "probable" and "confirmed starting
  pitcher" as states in one announcement timeline (documented deviation from the
  separate-item listing).
- `lineup_snapshots` + `lineup_players` — append-only lineup header + ordered
  players; `is_confirmed`, `confirmed_at`. For NBA, `confirmed_pregame_starters`
  rows exist **only** when a provider observation truly supplied confirmed
  starters before the cutoff; otherwise the capability is recorded `unavailable`.
- `injury_snapshots` — append-only `status` (reuse `intel.PlayerStatus`), `reason`,
  `published_at`, `is_correction`, `source_type` (reuse `intel.SourceType`).
  **Absence of an injury row is never "healthy"** — it is `unknown`, and a missing
  provider capability is a `data_quality_issues` record.
- `play_snapshots` — append-only GOAT plays / substitution events (NBA), or MLB
  play events; supports lineup-stint reconstruction where available.
- `weather_snapshots` — append-only `is_forecast` + `forecast_for`,
  temp/wind/precip/humidity, `is_dome`; forecast-vs-actual kept distinct.
- `entity_match_decisions` — decision log (`ENTITY_MATCHING.md` §7); append-only
  except review columns.
- `match_candidates` — **normalized child** of `entity_match_decisions` (one row
  per candidate + per-candidate score/tier), mirroring `kalshi_orderbook_levels`.
- `data_quality_issues` — `severity ∈ {blocking,issue,note}`, `rule_code`,
  `entity_type`, `entity_id`, `description`, `detected_at`, `resolved_at`; also
  records capability gaps (`DQ-CAP-*`) and UTC-fallback local-date notes
  (`DQ-TZ-*`).

**No second canonical-game table.** `games` remains the one canonical game.

---

## 4. Historical correction behaviour (append-only)

Append-only; **current state is derived, never overwritten** — dedup an
observation against its immediate temporal predecessor by content hash; recompute
current state from the newest observation by `(observed_at, id)`; older backfills
are stored but never regress current state (deterministic ULID tie-break).

| Event | Representation | Current-state selection |
| --- | --- | --- |
| Postponed / rescheduled / cancelled / suspended game | new `game_schedule_snapshots` + `game_status_history` status row; `games.scheduled_start` updated, `original_start` immutable; `official_game_key` stable across a move | newest status observation |
| MLB doubleheader | two `games` rows (`game_number` 1/2), each its own snapshots | per-game newest |
| Score / stat correction | new `game_result_snapshots` / `*_game_statistics` row (`is_correction`, changed hash); prior preserved | newest observation |
| Probable→confirmed→scratched pitcher | successive `probable_pitcher_snapshots` (`status`, `superseded_by`) | newest observation |
| Lineup change / NBA late scratch | new `lineup_snapshots`/`injury_snapshots` observation; prior preserved | newest by `observed_at` |
| Injury status change | new `injury_snapshots` (reuse `intel.PlayerStatus`); absence ≠ healthy | newest by `observed_at` |
| Weather forecast change | new `weather_snapshots` (`is_forecast=1`, `forecast_for` fixed, `observed_at` advances) | as-of `observed_at ≤ cutoff` (never the actual) |

**Older backfill never regresses current metadata** — proven the Phase C c008 way.

---

## 5. Canonical game & market matching

Implements `ENTITY_MATCHING.md` §4–§6; deterministic first; ambiguity never
silently accepted; **market price never used as evidence**.

### 5.1 Venue-aware local date (`game_date_local`) — resolution hierarchy

`game_date_local` is resolved by this hierarchy, **not** home-venue-only:

1. **Actual event venue timezone** — the timezone of the venue the game is
   actually played at (from `venues.timezone` for the resolved event venue,
   including neutral/temporary/relocated sites). Highest confidence.
2. **Official provider-supplied local game date / timezone**, when reliably
   supplied (e.g. StatsAPI game local date) — use it directly.
3. **Canonical home venue timezone** — fallback only when the actual event venue
   is unknown.
4. **UTC calendar date** — final fallback only. When used: **lower the match
   confidence**, write a `data_quality_issues` note (`DQ-TZ-001`), and **never**
   treat it as equivalent to an actual venue timezone.

### 5.2 Schedule key & tiers

Key: `(league_id, game_date_local, home_team_id, away_team_id, game_number)`,
each team resolved through team matching first (fail → `no_candidate`). Tiers per
`ENTITY_MATCHING.md` §4.2: `official_key` 1.00 → `schedule_key_exact` 0.95 (±90 min)
→ `schedule_key_window` 0.88 (±12 h) → `title_rules` 0.85 (Kalshi). Accept ≥ 0.85;
≥2 candidates in the winning tier → **AMBIGUOUS** (never fall through);
0 candidates → **no_candidate**; both `needs_manual_review=1`. A UTC-fallback local
date (§5.1 tier 4) caps the achievable tier below `schedule_key_exact`.
Deterministic tie-break by candidate id. Every attempt writes one
`entity_match_decisions` row + `match_candidates` children (incl. losers). PIT:
joins use only decisions with `decided_at ≤ cutoff` (DQ-PIT-010).

Chain: official game → canonical `games` (D5 populates `games` +
`official_provider/key`); then sportsbook_events → games and kalshi_events/markets
→ games (Kalshi adds title/rules cross-check + `rules_hash`-change detection).

### 5.3 Planned matching tests (venue-aware local date)

D5 tests must cover: (1) ordinary home game, (2) neutral-site NBA game,
(3) international MLB game, (4) temporary venue, (5) relocated game, (6) missing
event venue (falls to home venue, then UTC with lowered confidence + `DQ-TZ-001`),
(7) game crossing a UTC calendar boundary (7pm PT = next-day UTC), (8) doubleheader
at a temporary venue. Plus the existing determinism-under-shuffle, ambiguity-refusal,
and decision-completeness suites.

---

## 6. Player matching (extends `intel/player_matching.py`)

Keep the `MATCHED / AMBIGUOUS / UNMATCHED` contract and the deterministic
normaliser; **do not replace it.** Back the directory with
`provider_player_references` + `player_aliases`. Evidence order: provider player id
(exact → MATCHED) → `(team, normalized_full_name)` → `(league, normalized_full_name)`,
with suffix binding and active-season filtering; birth date only when legitimately
supplied and needed to break a genuine collision. Two players are **never** resolved
on name alone → AMBIGUOUS; an unknown player is UNMATCHED (never a silently-created
duplicate). Chadwick bridges MLBAM↔ids (MLB); BALLDONTLIE ids anchor NBA. Every
resolution writes an `entity_match_decisions` row.

---

## 7. Point-in-time & leakage rules (authoritative time per category)

`observed_at` (= `raw_responses.received_at`) is the **only** cutoff for every
as-of query and training join; `provider_timestamp`/`published_at` are for lag
measurement and within-provider ordering only.

| Hazard | Authoritative time | Defence | Rule |
| --- | --- | --- | --- |
| Final scores in pregame data | result `observed_at` | results read only from `game_result_snapshots` as-of; `games.status` unreachable from `pit/` | DQ-PIT-001 |
| Postgame stats in pregame rows | stat `observed_at` | computed inside the as-of window; never precomputed | DQ-PIT-002 |
| Confirmed lineups / starters before publication | lineup `observed_at` | `lineup_snapshots` as-of; NBA `confirmed_pregame_starters` present **only** if truly observed before the cutoff, else `unavailable` | DQ-PIT-003 |
| Probable-pitcher / injury / late scratch before observation | snapshot `observed_at` | `probable_pitcher_snapshots` / `injury_snapshots` as-of, never `published_at` | DQ-PIT-004 |
| Future weather forecast | forecast `observed_at`, subject `forecast_for` | only forecasts with `observed_at ≤ cutoff`; the *actual* is never a pregame feature | DQ-PIT-005w |
| Corrected stats before the correction was seen | correction `observed_at` | corrections are new appended rows; as-of hides later corrections | DQ-PIT-002 |
| Closing prices before the cutoff | price `observed_at` | Phase B snapshots as-of; closing line evaluation-only | DQ-PIT-005 |
| Future match decisions | `decided_at` | joins use only `decided_at ≤ cutoff` | DQ-PIT-010 |
| Cross-provider clock skew | `observed_at` (our single clock) | never order across providers by `provider_timestamp` | DQ-PIT-009 |

Adversarial fixtures (one planted leak per rule) are a **Phase E** gate.

---

## 8. Dry-run contract (consistent with Phases B/C)

**Resolves the earlier contradiction.** For every external-provider ingestion
command, `--dry-run`:

- **may** perform the approved GET request(s);
- **may** parse and normalize in memory;
- **persists absolutely nothing** — **no** `ingestion_runs` row, **no**
  `raw_responses` row, **no** normalized row, **no** `data_quality_issues` row,
  **no** `provider_capabilities` row, **no** `entity_match_decisions` row;
- reports the counts a real run *would* have produced (including would-be
  rejections and capability gaps), then exits.

A **normal (non-dry) run** records every audit and normalized record: the
`ingestion_runs` row (with the c008 `records_updated` counter), raw responses,
normalized rows, capability records, and any `data_quality_issues`. **No CLI
description or test may claim that a command records an ingestion-run row while
`--dry-run` is active.** Pure-compute commands (`match-games`, `match-markets`)
also persist nothing under `--dry-run`.

---

## 9. CLI commands

All GET-only, read-only, sanitized; exit codes reuse the vocabulary (`0` success
incl. clean skip / zero-results / capability-unavailable; `1` genuine active
failure; `2` read-only startup violation; `3` db missing/unmigrated). `--dry-run`
obeys §8 (persists nothing). Rate-limit handling: conservative single-flight,
exponential backoff on 429/503, **respect the selected BALLDONTLIE tier QPS**;
a truncated sweep is reported explicitly, never silently capped. A provider
tier/authorization error → "capability unavailable for current subscription tier"
(§2.4), recorded, not treated as a bug.

| Command | Provider (tier) | Required | Optional | Notes |
| --- | --- | --- | --- | --- |
| `provider-audit --provider P` | the named provider | `--provider` | `--db --json` | small non-destructive audit (§10); **never** buys/changes a subscription |
| `ingest-mlb` | MLB StatsAPI (no key) | — | `--from --to`, `--game-pk`, `--include {results,box,inning,probables,lineups}`, `--db --dry-run` | day-paged; idempotent on content hash; zero games ≠ failure |
| `ingest-nba` | **BALLDONTLIE GOAT** (`NBA_DATA_API_KEY`) | — | `--from --to`, `--game-id`, `--include {results,box,player-stats,quarters,plays}`, `--db --dry-run` | cursor pagination; GOAT-tier QPS; tier error → capability-unavailable, not failure |
| `ingest-injuries --sport nba` | BALLDONTLIE GOAT (optional PDF cross-check) | `--sport` | `--date`, `--cross-check-pdf`, `--db --dry-run` | absence ≠ healthy; PDF is an optional independent cross-check |
| `ingest-lineups --sport mlb` | MLB StatsAPI | `--sport` | `--date`, `--game-pk`, `--db --dry-run` | posted lineups + probable/confirmed pitchers; NBA confirmed starters → unavailable path |
| `ingest-weather` | **NWS** primary, **Open-Meteo** secondary/historical-forecast | — | `--from --to`, `--game-pk`, `--forecast/--actual`, `--db --dry-run` | outdoor MLB only (gate by `venues.roof_type`); non-US → Open-Meteo |
| `ingest-venues` | MLB StatsAPI `/venues` | — | `--db --dry-run` | seeds `venues` + `venue_aliases` (coords/roof/tz) |
| `match-games` | none (compute) | — | `--league`, `--since`, `--db --dry-run` | official→canonical, sportsbook→canonical; venue-aware local date (§5.1) |
| `match-markets` | none (compute) | — | `--since`, `--db --dry-run` | Kalshi→canonical (title/rules); `rules_hash`-change detection |
| `matching-review` | none | — | `--entity-type`, `--reason`, `--json` | lists open `needs_manual_review` grouped by reason; read-only |

Offline supplements (**not live CLI network commands**): a separate offline
importer reads hoopR/pybaseball **Parquet** exports into the append-only tables
with provenance; it makes no live provider call and is not part of app startup.

---

## 10. Provider audit (before any large backfill)

Before D2 or D3 performs a large backfill, `provider-audit --provider P` runs a
small, non-destructive check and records `provider_capabilities` snapshots. It
**must not** make a purchase or change the subscription.

**Declared vs observed (enforced by `d010`).** The audit runs **one minimal
approved GET per capability group** and records only what a probe actually
verified as *externally observed* (`is_observed = 1`), carrying the probe name,
sanitized endpoint, HTTP status, error classification, verification timestamp,
and the `raw_response_id` that is the evidence. A static capability *declaration*
is **never** persisted as though an endpoint verified it: capabilities with no
probe stay declared-only (`is_observed = 0`, `observed_state` NULL). So a
successful `/teams` response marks only its own group observed — never injuries,
stats, box scores, plays, or lineups. One tier-restricted endpoint restricts only
its group; unrelated groups keep being probed. A `401` fails the run and records
**no** supported observation.

**Dependency-aware probing.** Some documented endpoints require a valid provider
id, so the audit resolves it from an earlier probe rather than hardcoding one:

* BALLDONTLIE `/v1/plays?game_id=…`, `/v1/lineups?game_ids[]=…`, and
  `/nba/v1/stats/advanced` each take a **game id** extracted from the sanitized
  `/v1/games` response.
* MLB **players** is verified via `/teams/{id}/roster` (a team id from the teams
  response) and then optionally `/people/{id}` (a person id from the roster) —
  never marked supported just because `/teams` returned 200.

When no suitable id is available the dependent capability is recorded
`unknown_until_audited` (skipped, no request issued, never supported, never an
auth failure); an id is never fabricated. A 2xx with an empty result verifies
*endpoint access* only, not historical coverage or payload completeness. Lineup
*endpoint access*, confirmed pregame starters, substitutions, and play-by-play
stay distinct: starters are never inferred from lineup access, and substitutions
are marked observed **only** when the returned play data actually contains
substitution events. Groups probed:

MLB StatsAPI — teams · schedules/games · venues · roster/person (players).
BALLDONTLIE (GOAT) — teams · players · games/schedules · player game statistics ·
box/team statistics · injuries · plays · lineups · advanced statistics
(`/nba/v1/stats/advanced`) — each a documented endpoint on the tightened
allow-list; the previously-listed `/v1/advanced_stats` was undocumented and was
removed.
NWS / Open-Meteo — one current-forecast probe each; a current forecast never
implies historical-forecast reconstruction.

Each observed/declared capability is one of the §2.2 states (e.g. a GOAT-only
endpoint answering a **plan-worded** 403 → `paid_tier_required`; a generic 403
with no plan evidence → `forbidden`/`unavailable`, never `paid_tier_required`).
The audit is the authoritative source for observed capability history; snapshots
are append-only, so an earlier belief is preserved and never overwritten. A tier
limitation it finds is a recorded capability state, never a failed run.

---

## 11. Credentials & config

Add to `sports_quant/config.py` (all `SecretStr`, `.env`-only, sanitized):
`nba_data_api_key`; optional `weather_api_key`, `sportradar_mlb_api_key`,
`sportradar_nba_api_key`; and **pinned base URLs** `mlb_stats_api_base_url`,
`nws_base_url`, `open_meteo_base_url` (defaults per `.env.example`; validated at
startup like `PRODUCTION_KALSHI_REST_URL`). Clarify in-repo: `NBA_DATA_API_KEY` is
a **BALLDONTLIE** key; **endpoint access depends on the account tier**; the Phase D
path **expects GOAT**; a key alone does not grant GOAT. MLB StatsAPI, NWS, Kalshi
public REST, and Open-Meteo (free) need **no** key. No real key ever enters docs,
source, or CI. Read-only startup invariants unchanged.

---

## 12. Implementation stages (D1–D5)

Each subphase is independently green under Ruff + mypy + pytest before the next.
Model column = recommended driver.

### D1 — Provider infrastructure  ·  model: **OpusPlan**  ·  ✅ COMPLETE

> **Built.** Capability system (`providers/capabilities.py` — typed
> `ProviderCapability` × `CapabilityState`, `BalldontlieTier`, per-provider
> declarations incl. `advanced_statistics`, evidence-based tier-error classifier
> with distinct authentication / invalid-key / tier-restricted / forbidden /
> rate-limited / not-found / network / server / invalid-payload / parser /
> unsupported / unexpected kinds), shared client base (`providers/base_provider.py`
> — GET-only, `RawExchange`, bounded timeouts/retries + `Retry-After`,
> content-type guard, **streamed** size guard: it rejects a declared
> `Content-Length` over the cap before reading and otherwise counts bytes and
> aborts mid-stream, so an oversized body never buffers or reaches storage; no
> redirect chasing), the four clients (`mlb_statsapi` incl. roster/person,
> `balldontlie` incl. plays/lineups/advanced-stats with id validation and bounded
> pages, `nws`, `open_meteo`), `http_policy` allow-lists + `for_*` (BALLDONTLIE
> tightened to explicit documented endpoints incl. `/v1/plays`, `/v1/lineups`,
> `/nba/v1/stats/advanced`; the undocumented `/v1/advanced_stats` removed; no path
> wildcard), pinned/validated config (exact host + normalized base path; rejects
> userinfo/port/query/fragment, duplicate slashes, dot segments, and deceptive
> prefixes), migrations `d009_provider_infra` (v9) and
> `d010_provider_audit_integrity` (v10), repositories (`references`, `venues`,
> `matching`, `data_quality`, `capabilities`), the **evidence-backed,
> dependency-aware** `provider-audit` + `ingest-venues` CLI, and full mocked
> tests. The audit separates declared from externally observed capabilities (§10):
> one GET per group, dependent probes resolve a game/team id from an earlier
> response (skipping honestly as `unknown_until_audited` when none is available),
> observed capabilities carry probe/endpoint/status/error/raw-response evidence,
> unprobed/unverified capabilities stay declared-only, and audit history is
> append-only. No historical backfill; no live call. Live-verification of provider
> docs/terms (decisions §7) is still owed before D2/D3 backfill.

- **Provider(s):** infrastructure for all selected providers; **required tier:**
  BALLDONTLIE **GOAT** declared (not yet exercised for backfill). **Optional:**
  SportsDataIO Discovery Lab client stub (comparison, off by default).
  **Offline:** none yet (hoopR/pybaseball importers are D2/D3).
- **Capabilities:** build the typed capability catalogue + states + per-provider
  declarations + selected-tier record; the tier-error → "capability unavailable
  for current subscription tier" classifier.
- **Unavailable-data behaviour:** a `provider_capabilities` record + optional
  `data_quality_issues` note; never fabricate.
- **Licensing risk:** confirm §7 verification obligations of the decisions doc
  before writing clients; pin base URLs.
- **Create:** `providers/{mlb_statsapi,balldontlie,nws,open_meteo}.py`
  (policy-wrapped GET, `RawExchange`), `providers/capabilities.py` (types),
  provider config, `http_policy` host rules + `for_*`, migration
  `d009_provider_infra`, repositories `db/repositories/{references,venues,matching,
  data_quality,capabilities}.py`, models/ids/schema constants, `provider-audit`
  command, test fixtures, isolation test.
- **Modify:** `http_policy.py`, `config.py`, `.env.example` (done), `cli.py`,
  `db/{models,ids,schema}.py`, `pyproject.toml`.
- **Migration:** `d009` (v9). **Tables:** references ×3, venues, venue_aliases,
  entity_match_decisions, match_candidates, data_quality_issues, provider_capabilities.
- **CLI:** `provider-audit`, `ingest-venues`.
- **Tests:** migration applies once + idempotent; new hosts GET-only, account/order
  paths still blocked; no key printed/stored; capability states typed & persisted;
  tier error → capability-unavailable (not invalid-key); venues seedable from a
  mocked StatsAPI fixture; gateway never imported; dry-run persists nothing.
- **Completion:** all above green; **no historical backfill yet**.
- **Expected blockers:** confirming allow-list host/path entries; re-verifying
  BALLDONTLIE tier boundaries; terms confirmation.

### D2 — MLB ingestion  ·  model: **Sonnet**

- **Provider:** MLB StatsAPI (no key). **Tier:** n/a. **Optional/offline:**
  pybaseball/Statcast/FanGraphs **deferred** (offline Parquet importer only, not a
  runtime dep). **Cross-check:** none.
- **Capabilities:** consult the StatsAPI declaration; `confirmed_pregame_starters`
  = `unavailable`; `correction_timestamps` = `best_effort`.
- **Create:** `ingest/mlb_ingestor.py`; repositories for schedule/result/stats/
  inning/roster/probable/lineup; mocked-StatsAPI fixtures + tests.
- **Modify:** `cli.py` (`ingest-mlb`, `ingest-lineups --sport mlb`).
- **Migration:** `d011` (v11). **Tables:** game_schedule/result snapshots,
  team/player_game_statistics, mlb_inning_lines, roster_snapshots,
  probable_pitcher_snapshots, lineup_snapshots, lineup_players.
- **Completion:** mocked date-range sweep persists canonical `games` + provenance,
  results, box, inning lines, probables, posted lineups; idempotent twice;
  append-only enforced; every row traces to a raw response; `--dry-run` persists
  nothing; capability records written; live smoke-test safe.
- **Expected blockers:** doubleheader/game-number; status-code mapping; stat coverage.

### D3 — NBA ingestion  ·  model: **Sonnet**

- **Provider:** **BALLDONTLIE GOAT** (`NBA_DATA_API_KEY`). **Required tier: GOAT.**
  **Offline supplement:** **hoopR** via a typed Parquet import boundary (historical
  PBP/possessions/substitutions/lineup-stints) — **not** a live dependency, **no**
  R at runtime. **Optional comparison:** SportsDataIO Discovery Lab (delayed;
  id/field/record comparison; off by default; never the live feed).
- **Capabilities (per GOAT):** teams/players/games/schedules/game_results/
  player_statistics/team_statistics/advanced_statistics/injuries/plays/quarter_lines
  = `supported`; `lineups` = `best_effort`; `confirmed_pregame_starters` =
  `unavailable`; `substitutions` = `best_effort` (from plays where present);
  `correction_timestamps` = `unsupported`. Advanced statistics are served by the
  documented `/nba/v1/stats/advanced` endpoint; play-by-play and lineups require a
  game id (`/v1/plays?game_id=…`, `/v1/lineups?game_ids[]=…`).
- **Required D3 outputs (must be produced):** provider teams, provider players,
  schedules, games, game-level results, **available** player statistics,
  **available** box scores, **available** injuries, provider ids, raw-response
  provenance.
- **Conditional D3 outputs (record state, never fabricate):** quarter lines,
  plays, lineups, confirmed pregame starters, substitutions, correction
  timestamps — each recorded as `available | unavailable | paid_tier_required |
  best_effort | provider_history_limited` in `provider_capabilities` +, when
  missing, a `data_quality_issues` (`DQ-CAP-*`) record. **NBA D3 must not require
  any conditional field unconditionally.**
- **Unavailable-data behaviour:** missing injury data is `unknown`, **never**
  "healthy"; missing starters → `confirmed_pregame_starters = unavailable`;
  GOAT-thin history → `provider_history_limited`.
- **Create:** `ingest/nba_ingestor.py`; nba repositories; `ingest/hoopr_import.py`
  (offline Parquet importer); optional `providers/sportsdataio.py` (comparison
  stub); optional `providers/nba_injury_report.py` (PDF cross-check) **only if
  built**; mocked GOAT fixtures (+ small Parquet + optional fixture PDF) + tests.
- **Modify:** `cli.py` (`ingest-nba`, `ingest-injuries --sport nba`).
- **Migration:** `d012` (v12). **Tables:** nba_quarter_lines, injury_snapshots,
  play_snapshots (box/result/roster reuse d011).
- **Completion:** mocked GOAT sweep persists the **required** outputs; each
  **conditional** output is recorded with an explicit capability state; a tier
  error is reported as capability-unavailable (not failure); hoopR Parquet import
  path exercised offline; idempotent twice; append-only; `--dry-run` persists
  nothing.
- **Expected blockers:** GOAT tier verification + QPS; box/plays historical depth
  (`provider_history_limited`); no free pregame starters; PDF fragility (if built);
  hoopR export schema mapping.

### D4 — Weather  ·  model: **Sonnet**

- **Provider:** **NWS** primary (US, no key); **Open-Meteo** secondary + the
  leakage-free historical-forecast (no key). **No paid weather key at D1/D4.**
- **Capabilities:** forecast/actual `supported`; NWS non-US `unavailable` →
  Open-Meteo; commercial Open-Meteo `paid_tier_required` (documented, not used).
- **Create:** `ingest/weather_ingestor.py`; weather repository; mocked NWS/Open-Meteo
  fixtures + tests.
- **Modify:** `cli.py` (`ingest-weather`).
- **Migration:** `d013` (v13). **Tables:** weather_snapshots (venues from d009).
- **Completion:** forecast + actual persisted distinctly; outdoor-only gating by
  `venues.roof_type`; leakage-free historical-forecast; dome/indoor skipped;
  non-US → Open-Meteo; idempotent; append-only; `--dry-run` persists nothing.
- **Expected blockers:** venue coord/roof accuracy; NWS US-only (Toronto → Open-Meteo);
  Open-Meteo historical-forecast API shape.

### D5 — Canonical matching  ·  model: **OpusPlan**

- **Provider:** none (pure compute over ingested data).
- **Create:** `matching/{__init__,normalize,teams,players,games,markets,decisions,
  localdate}.py` (import `db/normalize.py` — one normaliser; `localdate.py`
  implements the §5.1 hierarchy), matching repository glue; tests.
- **Modify:** `cli.py` (`match-games`, `match-markets`, `matching-review`);
  `intel/player_matching.py` (back with references/aliases, API unchanged).
- **Migration:** none if `entity_match_decisions`/`match_candidates` landed in
  d009 (a small `d014` only if review columns need widening). **Populates**
  `games.official_*`, `provider_*_references`, sportsbook/Kalshi `game_id` +
  `match_decision_id`.
- **Tests:** the eight §5.3 venue-aware local-date scenarios; determinism under
  100 shuffles; ambiguity/no-candidate never silently accepted; every §4.3 hard
  case; price never used as evidence; decision-completeness; title/rules
  disagreement; UTC-fallback lowers confidence + writes `DQ-TZ-001`.
- **Completion:** a fixture slate matches end-to-end with every decision +
  candidate recorded; `matching-review` lists open items; `--dry-run` persists
  nothing.
- **Expected blockers:** venue-timezone edge cases; doubleheader ambiguity; Kalshi
  ticker/title/rules parsing; neutral-site orientation.

---

## 13. Verification gates (every subphase)

`ruff check .` clean; `mypy . --no-incremental` zero project-source errors;
`pytest -q` zero failures; migrations apply once + idempotent (second `db-init`
no-ops); **no live network call in the test suite** (mocked transports/fixtures);
**no credential** in any output/log/stored column (whole-DB sweep); **`--dry-run`
persists nothing** (asserted); `providers-check` still passes; **GET-only**;
execution quarantined; append-only history preserved; capability states recorded,
never inferred from key possession.

---

## 14. Open decisions carried from provider selection

See `PHASE_D_PROVIDER_DECISIONS.md` §8: BALLDONTLIE GOAT subscription (the NBA MVP
needs it), personal-vs-commercial intent, NBA injury cross-check, weather
licensing, and offline deep-history supplements. These are **user decisions**; the
plan supports the MVP (StatsAPI + GOAT + NWS/Open-Meteo) or the professional
(Sportradar/SportsDataIO/Stats Perform) path without rework, because every provider
sits behind an adapter with a typed capability declaration.
