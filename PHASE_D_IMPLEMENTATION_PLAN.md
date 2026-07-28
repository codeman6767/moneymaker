# Phase D — Implementation Plan

Concrete, staged implementation design for official MLB/NBA data, weather, and
canonical matching.

> **D4 weather ingestion code is complete at schema version 14 against mocked NWS
> and Open-Meteo contracts (migration `d014_weather`). NWS is primary for supported
> US outdoor MLB venues; Open-Meteo covers non-US venues and the explicitly
> selected historical products. Forecasts, station observations, historical
> forecasts, and reanalysis are stored as DISTINCT data kinds; indoor/fixed-roof
> games are skipped without network requests; retractable-roof games are
> conditionally applicable unless roof-open status is known; historical rows are
> not automatically point-in-time-safe. The controlled live NWS/Open-Meteo
> current-forecast audits and a bounded zero-persistence dry run have passed; no
> persisted weather ingestion or weather backfill has been performed. D5 is complete at schema v16 (D5A canonical + official-game, D5B1
> sportsbook, and D5B2 Kalshi matching, each independently reviewed); Phase E has
> not started.**
>
> **Status: Phase D2 MLB ingestion code complete and its controlled live gate
> passed on July 24, 2026. D3 NBA ingestion code is complete and correctness-
> repaired against mocked BALLDONTLIE GOAT contracts and offline hoopR fixtures
> (schema v13, migrations `d012_nba_specifics` + `d013_nba_typed_repairs`), and
> its controlled live BALLDONTLIE GOAT capability audit + bounded dry-run smoke
> tests passed on July 24, 2026 (audit `run_01KYB91H2DHG0SKJDMAV2SN88M`: exit 0,
> authenticated, 9 GET-only probes, 11 observed capabilities; one completed-game
> `ingest-nba --dry-run` and one current `ingest-injuries --dry-run`, both
> persisting nothing and never creating their isolated database). A supplemental
> modern-game dry run (2026-06-13) additionally live-row-verified advanced
> statistics and plays and confirmed an honest empty lineup response, but exposed a
> quarter-line contract defect: BALLDONTLIE `/v1/box_scores` supplies per-period
> scores as flat `home_qN`/`visitor_qN` + `home_otN`/`visitor_otN` fields, not a
> nested `periods` array. The parser was repaired to the flat-key contract and
> **live-verified on 2026-07-24** by a repeated bounded 2026-06-13 dry run:
> `quarter_observations` normalized to 8 (regulation periods 1–4, both sides) where
> it was 0 before the repair, with 0 rejections and 0 data-quality notes; that game
> was a regulation game (no overtime), so null OT fields correctly produced no rows
> (overtime→period-5 mapping and explicit-zero preservation remain covered by the
> offline unit tests). No persisted NBA ingestion or historical NBA backfill has
> been performed. D4 weather ingestion code is implemented at schema v14 against
> mocked NWS/Open-Meteo contracts, including a focused correctness + point-in-time
> repair. The controlled live D4 current-forecast gate then completed successfully:
> the NWS and Open-Meteo current-forecast provider audits each succeeded with
> keyless, GET-only requests (one GET apiece, authentication not applicable, zero
> active failures, `live_availability` observed and historical depth left
> declared-only). A single bounded `ingest-weather --forecast --dry-run` used two
> isolated synthetic official-MLB fixtures at real outdoor venues: a US fixture
> (Wrigley Field) routed through NWS (2 GETs, point + hourly forecast) and a non-US
> fixture (London Stadium) routed through Open-Meteo (1 bounded current-forecast
> GET), normalizing 10 forecast observations with zero rejections, zero DQ notes,
> zero fallbacks and zero rows persisted; the scratch database was byte-for-byte
> unchanged and was removed after verification. NWS station observations, Open-Meteo
> historical forecasts and Open-Meteo reanalysis remain mocked/offline verified, not
> live-ingestor verified. No persisted weather ingestion or weather backfill has been
> performed. D5A deterministic canonical + official-game matching is complete
> (mocked/offline); D5B1 sportsbook + D5B2 Kalshi market matching complete
> (schema v16, `d016_kalshi_matching`; mocked/offline plus a bounded public-contract
> audit and parser smoke). Phase D is complete; Phase E1 (point-in-time foundation) is complete and Phase E2 has not started.** D1
> (schema v10) built the
> typed provider-capability system, the four provider clients over a shared
> GET-only base, the tightened `http_policy` allow-lists, and the evidence-backed
> dependency-aware `provider-audit` + `ingest-venues` CLI. **D2 (migration
> `d011_official_games_stats`, schema v11)** adds the append-only, transition-aware
> official-MLB observation tables (schedule / result / inning lines / team +
> player statistics / roster / probable-pitcher / lineup + lineup players), the
> extended MLB StatsAPI client (date-ranged schedule with `probablePitcher`/
> `lineups` hydration, box score, line score — GET-only, id/date validated), a
> typed MLB status mapper, five typed repositories, and the `ingest-mlb` +
> `ingest-lineups` CLI commands. Official game identity is anchored on
> `provider_game_references` (one row per `gamePk`); canonical `games`/team/player
> resolution is deferred to D5, so snapshots carry provider ids with NULLABLE
> canonical ids. Missing values stay NULL (never zero); contradictions become
> `data_quality_issues`. All tested against mocked, realistic StatsAPI fixtures
> (no live provider call was made). D3 (NBA ingestion) code + its mocked/offline
> correctness repair are now complete at schema v13 (see the D3 build note below);
> D4 (weather ingestion) code is complete at schema v14 (see the D4 status note);
> D5 (canonical matching) is complete at schema v16 — D5A + D5B1 + D5B2, each
> independently reviewed (see the D5 build/status notes below). This document is
> the build contract; providers are chosen in `PHASE_D_PROVIDER_DECISIONS.md`
> (doc-review date 2026-07-23).

> **Live D2 gate (2026-07-24).** The controlled live MLB StatsAPI provider audit
> and bounded dry-run smoke test completed successfully on July 24, 2026. The
> audit exited 0 (`succeeded`) with 5 GET-only keyless requests, 5 observed
> capabilities (teams / schedules / games / venues / players, each with
> probe/endpoint/HTTP-200/raw-response evidence), 14 declared-only capabilities,
> 0 active failures, and one honest `DQ-CAP-001` note; authentication was
> correctly not applicable for this keyless provider. The smoke test covered the
> five completed MLB games from July 23, 2026 and exercised results, box scores,
> inning lines, probable pitchers, posted lineups, and date-aware rosters:
> `--dry-run` exited 0 with `run_id=null`, 5 games received, 21 sequential
> GET-only requests, 10 roster requests (one per distinct team/official-date
> pair), and 0 corrections / DQ issues / rejections / active failures. The dry
> run persisted nothing — its isolated target database was never created and the
> corpus changed only from the persisted audit. No persisted MLB ingestion or
> historical backfill has been performed.

> **D3 NBA build + correctness repair (mocked/offline only).** Migration
> `d012_nba_specifics` (schema v12) adds three NBA-specific append-only,
> transition-aware observation tables — `nba_quarter_lines`, `injury_snapshots`,
> `play_snapshots`. The forward-only repair migration `d013_nba_typed_repairs`
> (schema v13) then makes NBA storage sport-correct: NBA game results, team
> statistics, and player statistics live in dedicated `nba_game_results`
> (home/away **points** + **period**), `nba_team_statistics`, and
> `nba_player_statistics` (`stat_group IN ('traditional','advanced')`) tables —
> **no NBA row is ever stored as baseball `home_runs`/`innings_played` or
> role `batting`/`pitching`**, and the baseball-named d011 tables are MLB-only
> again. d013 also adds `injury_snapshots.return_estimate`, preserving the
> provider's exact return-estimate text (e.g. `"Nov 17"`) with a parsed ISO
> `return_date` only for an unambiguous full date (no fabricated year). Identity
> still anchors on `provider_game_references` (no second canonical game system);
> the corrected D2 correction semantics apply to NBA points/winner unchanged.
> Box scores are associated with a schedule game by the deterministic
> `(official date, provider home-team id, provider visitor-team id)` key (a
> genuine provider game id is honoured when present); a no-match/ambiguous match
> is rejected with a `DQ-NBA-BOX-001` note rather than guessed. Quarter lines are
> derived from the detailed box-score response (not the bare `/v1/games` listing,
> which has no per-quarter field). The BALLDONTLIE client gained documented
> date-range / game-id / cursor request shapes; a typed `nba_ingestor`
> (`ingest-nba` + `ingest-injuries`) with safe cursor pagination, raw-first
> persistence, capability recording, and a bounded zero-persistence dry-run; and a
> typed offline `hoopr_import` Parquet boundary (no R, no network, explicit
> supported schema, file-level SHA-256 provenance, idempotent re-import,
> `import-hoopr` CLI). `pyarrow` is an OPTIONAL (`tracking` extra) dependency: the
> hoopR tests skip cleanly when it is absent, so the standard `.[dev]` CI job
> collects and runs the full non-hoopR D3 suite. **D3's controlled BALLDONTLIE GOAT
> live capability audit, injury dry run, modern-game verification, and live
> verification of the repaired flat-key regulation-quarter parser all passed; no
> persisted NBA ingestion or historical backfill has been performed. D4 weather
> ingestion code is complete at schema v14 and its controlled live NWS/Open-Meteo
> current-forecast gate has passed (station observations, historical forecasts, and
> reanalysis remain mocked/offline verified); D5A deterministic canonical + official-game matching is complete (mocked/offline); D5B1 sportsbook + D5B2 Kalshi market matching complete (schema v16; mocked/offline plus a bounded public-contract audit). Phase D is complete; Phase E1 (point-in-time foundation) is complete and Phase E2 has not started.**

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
  (`d009`–`d011` built; `d012`–`d013` planned).
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
| 011 | `d011_official_games_stats` *(built, D2)* | `game_schedule_snapshots`, `game_result_snapshots`, `mlb_inning_lines`, `team_game_statistics`, `player_game_statistics`, `roster_snapshots`, `probable_pitcher_snapshots`, `lineup_snapshots`, `lineup_players` — all append-only, transition-aware, anchored on `provider_game_references`/`provider_team_references` (no second canonical game/team/player table) |
| 012 | `d012_nba_specifics` *(built, D3)* | `nba_quarter_lines`, `injury_snapshots`, `play_snapshots` — append-only, transition-aware; anchored on `provider_game_references` (no second canonical game system). GOAT plays / substitutions best-effort; also the offline hoopR play boundary |
| 013 | `d013_nba_typed_repairs` *(built, D3 repair)* | `nba_game_results` (home/away **points** + **period**), `nba_team_statistics`, `nba_player_statistics` (`stat_group IN ('traditional','advanced')`) — sport-correct NBA storage replacing the earlier baseball-named d011 reuse; plus `injury_snapshots.return_estimate` (exact provider text). MLB tables untouched |
| 014 | `d014_weather` *(built, D4)* | `weather_snapshots` — append-only, transition-aware weather observations anchored on `provider_game_references` + the existing `venues`; `weather_kind ∈ {current_forecast, station_observation, historical_forecast, reanalysis}`, `applicability ∈ {applicable, conditional_roof_unknown}`, honest `pit_eligible` (1/0/NULL), canonical units (degC / m·s⁻¹ / mm / % / degrees) |

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
- `weather_snapshots` — append-only observations with a `weather_kind`
  discriminator (`current_forecast` / `station_observation` / `historical_forecast`
  / `reanalysis`), canonical-unit temp/apparent/dew/humidity/wind/gust/direction/
  precip fields, `valid_time` / `forecast_target_time` / `model_reference_time` /
  `provider_available_at` / `lead_time_seconds`, and an honest `pit_eligible`
  (1/0/NULL); forecast, observation, and reanalysis are kept strictly distinct.
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
| Weather forecast change | new `weather_snapshots` (`weather_kind=current_forecast`, `valid_time` fixed, `observed_at` advances) | as-of `observed_at ≤ cutoff` **and** `pit_eligible=1` (never a station observation / reanalysis / unproven historical forecast) |

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

**Dependency-aware probing (game id *and* game date).** Some documented
endpoints require a valid provider id or date, so the audit resolves both from an
earlier probe rather than hardcoding one:

* BALLDONTLIE `/v1/plays?game_id=…`, `/v1/lineups?game_ids[]=…`, and
  `/nba/v1/stats/advanced?game_ids[]=…` each take a **game id** extracted from the
  sanitized `/v1/games` response. Advanced stats uses the **documented array
  parameters** `game_ids[]` / `seasons[]` (never the undocumented singular
  `game_id`/`season`), with positive-int ids, valid seasons, non-empty and
  size-bounded lists, and bounded pagination.
* BALLDONTLIE `/v1/box_scores?date=YYYY-MM-DD` requires a **date**; the audit
  extracts and validates the game date from the same games response. `date` is
  required and strictly validated (`YYYY-MM-DD`) in the client before any request.
* MLB **players** is verified via `/teams/{id}/roster` (a team id from the teams
  response) and then optionally `/people/{id}` (a person id from the roster) —
  never marked supported just because `/teams` returned 200.

When no suitable id or date is available the dependent capability is recorded
`unknown_until_audited` (skipped, no request issued, never supported, never an
auth failure); an id or date is never fabricated. A 2xx with an empty result
verifies *endpoint access* only, not historical coverage or payload completeness.
Lineup *endpoint access*, confirmed pregame starters, substitutions, and
play-by-play stay distinct: starters are never inferred from lineup access, and
substitutions are marked observed **only** when the returned play data actually
contains substitution events (read from the documented event type / `text` /
`description` fields). Groups probed:

MLB StatsAPI — teams · schedules/games · venues · roster/person (players).
BALLDONTLIE (GOAT) — teams · players · games/schedules · player game statistics ·
box/team statistics (date-dependent) · injuries · plays · lineups · advanced
statistics (`/nba/v1/stats/advanced`) — each a documented endpoint on the
tightened allow-list; the previously-listed `/v1/advanced_stats` was undocumented
and was removed.
NWS / Open-Meteo — one current-forecast probe each; a current forecast never
implies historical-forecast reconstruction.

**Truthful overall status + exit codes.** The audit result is computed from real
probe outcomes, never assumed: `succeeded` (completed with no active operational
failure — it may include honestly unsupported, tier-restricted, or skipped
capabilities), `partially_failed` (a useful probe succeeded but another hit an
active failure), or `failed` (authentication failed, or active failures prevented
any trustworthy verification). Active operational failures are network failure
after retries, upstream 5xx after retries, exhausted rate limits, invalid/parser
payloads, and unexpected errors — an honest tier restriction, generic forbidden,
or dependency skip is **not** one. `provider-audit` exits 0 for a completed honest
audit, 1 for any active failure (`failed` or `partially_failed`), 2 for a
read-only startup violation, and 3 for a missing/unmigrated database.

**Authentication evidence.** For the keyed provider (BALLDONTLIE), `authenticated`
is `True` only with evidence (a successful 2xx, or a plan-worded tier 403 that
proves the key was recognized), `False` for a verified invalid-key/auth response,
and `None` (unknown) when only network/5xx/rate-limit/malformed/generic-forbidden
occurred. Keyless providers (MLB StatsAPI, NWS, Open-Meteo) report authentication
as **not applicable** (`None`) — the audit never claims a keyless provider was
authenticated. A tier restriction is a recorded capability state, never a failed
run; the tier-restriction classifier requires **explicit** plan/subscription/
upgrade phrasing or a documented error code, so a broad unrelated use of a word
like “plan” is not enough. Each declared capability receives exactly **one**
outcome per audit run (a 401 that halts probing yields one authentication outcome
for the attempted capability and honest unprobed rows for the rest — never a
duplicate or a supported fallback). Snapshots are append-only, so an earlier
belief is preserved across runs. Automated tests drive all of this through
**strict contract-level mocks** that 4xx a request violating the documented shape
(wrong path, singular instead of array params, missing/invalid date), so a test
cannot pass by sending the wrong request.

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
> `balldontlie` incl. plays/lineups/advanced-stats and date-required box scores,
> with id/season/date validation, `game_ids[]`/`seasons[]` array params, and
> bounded pages/lists, `nws`, `open_meteo`), `http_policy` allow-lists + `for_*`
> (BALLDONTLIE tightened to explicit documented endpoints incl. `/v1/plays`,
> `/v1/lineups`, `/nba/v1/stats/advanced`; the undocumented `/v1/advanced_stats`
> removed; no path wildcard), pinned/validated config (exact host + normalized base
> path; rejects userinfo/port/query/fragment, duplicate slashes, dot segments, and
> deceptive prefixes), migrations `d009_provider_infra` (v9) and
> `d010_provider_audit_integrity` (v10), repositories (`references`, `venues`,
> `matching`, `data_quality`, `capabilities`), the **evidence-backed,
> dependency-aware** `provider-audit` + `ingest-venues` CLI, and full mocked,
> contract-enforcing tests. The audit separates declared from externally observed
> capabilities (§10): one GET per group, dependent probes resolve a game id / game
> date / team id from an earlier response (skipping honestly as
> `unknown_until_audited` when none is available), observed capabilities carry
> probe/endpoint/status/error/raw-response evidence, each capability gets exactly
> one outcome per run, overall status is a truthful
> `succeeded`/`partially_failed`/`failed`, authentication is evidence-based (N/A for
> keyless providers), and the CLI exits non-zero on any active failure. No
> historical backfill; no live call. Live-verification of provider docs/terms
> (decisions §7) is still owed before D2/D3 backfill.

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

### D2 — MLB ingestion  ·  model: **Sonnet**  ·  ✅ CODE COMPLETE

> **Built.** Migration `d011_official_games_stats` (schema v11) adds the nine
> append-only, transition-aware official-observation tables. `ingest/mlb_ingestor.py`
> reads the StatsAPI schedule (with `probablePitcher`/`lineups` hydration) and,
> per game, the box score and line score; it preserves each raw response once,
> writes schedule/result/inning/team-stat/player-stat/probable/lineup observations
> that each trace to the *exact* raw response that supplied them, and records
> capability gaps + contradictions as `data_quality_issues`. Official identity is
> the `provider_game_references` row (one per `gamePk`); canonical resolution is
> D5, so canonical ids stay NULLABLE. A typed `providers/mlb_status.py` maps
> provider status to a canonical status (unknown → explicit `unknown` + DQ). Five
> typed repositories (`official_games`, `game_statistics`, `rosters`, `probables`,
> `lineups`) share a transition-aware append helper. `ingest-mlb` /
> `ingest-lineups --sport mlb` CLI commands added. All mocked; no live call; no
> historical backfill; `--dry-run` persists absolutely nothing.
>
> **Integrity repair (code-only, no schema change).** Added real **roster
> ingestion** (`--include rosters`): each unique provider team's roster is fetched
> once per run (deduplicated across a doubleheader), preserved as its own raw
> response, and persisted as append-only roster observations with roster
> date/status/jersey/position. Every valid provider player id from **rosters, box
> scores, probables, and lineups** now creates/reuses a `provider_player_references`
> row with that exact response's provenance (never a canonical player, never a
> name match). A requested sub-fetch that genuinely fails (network/5xx/oversized/
> parser) is now a tracked **active failure** → status `partially_failed` and CLI
> **exit 1** (distinct from an honest data-quality rejection, which stays exit 0);
> `succeeded`/`partially_failed`/`failed` are the truthful statuses. **Corrections**
> are auto-detected: a changed result over a prior observation appends with
> `is_correction = 1` and increments `corrections_appended` (first observation and
> identical replays are not corrections). **Dry-run** now runs the full parse +
> validation in memory and reports truthful would-be counts (results/innings/team+
> player stats/rosters/references/DQ) while persisting absolutely nothing.
> **Inning reconciliation** compares each team's trustworthy inning-run sum to its
> total (`DQ-MLB-RECON-001` contradiction / `DQ-MLB-RECON-002` incomplete), kept
> separate from malformed-inning and negative-run checks. A **missing inning half**
> (e.g. the home team not batting in the bottom of the ninth) is never a fabricated
> all-NULL/zero row; an explicit zero half is stored as zero.

- **Provider:** MLB StatsAPI (no key, no SLA, no explicit correction timestamps).
  **Tier:** n/a. **Offline:** pybaseball/Statcast **deferred** (not integrated).
- **Capabilities:** consulted before optional groups; `confirmed_pregame_starters`
  = `unavailable` (never inferred from posted lineups); `correction_timestamps`
  = `best_effort` (changed-content detection; corrections are new observations).
- **Design deviation (documented):** D2 does **not** create a second canonical
  `games` row (which would need resolved teams + season). Snapshots anchor on
  `provider_game_references`; canonical `games` creation/linkage is D5.
- **Migration:** `d011` (v11). Tables: game_schedule/result snapshots,
  mlb_inning_lines, team/player_game_statistics, roster_snapshots,
  probable_pitcher_snapshots, lineup_snapshots, lineup_players. The integrity
  repair required **no schema change** (no `d012`); it populated existing tables.
- **Completion (met):** mocked date-range/game sweep persists schedule + results +
  box + inning lines + probables + posted lineups; idempotent twice; append-only
  enforced; every row traces to its raw response; missing≠zero; unknown status +
  contradictions flagged; `--dry-run` persists nothing; live smoke-test safe.

### D3 — NBA ingestion  ·  model: **Sonnet**

> **Status: code complete and correctness-repaired against mocked BALLDONTLIE
> GOAT contracts and offline hoopR fixtures (schema v13, migrations
> `d012_nba_specifics` + `d013_nba_typed_repairs`).** Created
> `ingest/nba_ingestor.py` (`ingest-nba`, `ingest-injuries`), `db/repositories/nba.py`
> (quarter / injury / play + NBA-typed result / team-stat / player-stat
> repositories), `ingest/hoopr_import.py` (`import-hoopr`), and
> `tests/test_phase_d3_nba.py` + `tests/test_phase_d3_hoopr.py`; extended the
> BALLDONTLIE client and `cli.py`. The correctness repair de-baseballed NBA
> storage (points/period + NBA stat groups), added deterministic box-score
> matching, preserved exact injury return estimates, and made `pyarrow` a clean
> optional dependency so the standard CI suite collects and runs without it. The
> optional PDF injury cross-check and the SportsDataIO comparison stub were
> deliberately **not** built (out of scope for this pass).
>
> **Controlled live gate (2026-07-24): PASSED.** The live BALLDONTLIE GOAT
> capability audit and bounded Phase D3 dry-run smoke tests completed successfully.
> The audit (`run_01KYB91H2DHG0SKJDMAV2SN88M`) exited 0 (`succeeded`, authenticated,
> tier `goat`), issued 9 GET-only probes (all HTTP 200, endpoints sanitized, no
> query string/secret stored), and recorded 11 observed capabilities — each with
> exact raw-response evidence — plus 9 declared-only; `confirmed_pregame_starters`
> and `substitutions` remained **not observed** (no over-inference from lineup/play
> access), and it wrote no NBA observation rows. The NBA smoke test was restricted
> to one completed provider game (id `874129`, official date 1946-11-01, NYK @ HUS,
> Final) selected from the games-probe evidence, and exercised the results, box
> score, and traditional player-statistics paths with live data (1 schedule, 1
> result, 2 team-stat, 22 player-stat observations, 0 corrections, box matched by
> its `(date, home, visitor)` key with 0 rejections). The advanced-statistics,
> quarter, plays, and best-effort lineup paths executed but returned **zero rows**
> because the only completed game in the games-probe evidence is the earliest game
> in provider history (consistent with `historical_depth = provider_history_limited`),
> which supplies no supported advanced/period/play/lineup structure — an honest
> zero, not a normalization failure (0 rejections, 0 data-quality notes, 0 active
> failures for that historical game). A separate current-injury dry run normalized
> 151 live injuries (each keeping its provider player identity, 0 rejections). Both
> dry runs persisted nothing (`run_id` null, 0 rows), did not create their isolated
> database, and left every corpus NBA table at 0.
>
> **Supplemental modern-game dry run (2026-06-13) + quarter-line repair.** A bounded
> one-day dry run on a completed 2026 NBA Finals game live-row-verified the
> previously-unproven paths: advanced statistics (30 observations, `stat_group =
> advanced`) and plays (536 observations) both normalized from live data, and
> lineups returned an honest empty `data: []` response (best-effort, not available).
> It also exposed a **quarter-line contract defect**: BALLDONTLIE `/v1/box_scores`
> supplies per-period scores as **flat** integer fields — `home_q1..home_q4` /
> `visitor_q1..visitor_q4` for regulation and `home_otN` / `visitor_otN` for
> overtime — **not** the nested `periods` array the parser originally assumed, so
> `quarter_observations` was 0 despite genuine non-null period data. `_parse_box_quarters`
> has been **repaired** to the flat-key contract (`qN → period N`, `otN → period
> 4+N`, `visitor → away`; explicit 0 preserved, null never coerced to 0, only
> periods supplied on both sides normalized, one-sided periods rejected with a
> `DQ-NBA-QTR-001` note, overtime discovered dynamically and ordered numerically
> with no fabricated gaps), with the dry-run counter sharing the same parser.
>
> **Quarter-line repair live-verified (2026-07-24).** A repeated bounded 2026-06-13
> dry run confirmed the fix on live data: `quarter_observations` normalized to **8**
> (regulation periods 1–4, both sides) where it was **0** before the repair, with 0
> rejections, 0 data-quality notes, 0 corrections, 0 rows persisted, and no database
> created; advanced statistics (30) and plays (536) were re-confirmed and lineups
> again returned an honest empty response. That specific game was a **regulation
> game (no overtime)**, so the parser correctly emitted no overtime rows for the
> null `*_otN` fields (no fabrication); the overtime→period-5 mapping and
> explicit-zero preservation remain covered by the committed offline unit tests
> (this live game exercised neither). Quarter parsing is now live-verified. **No
> persisted NBA ingestion or historical NBA backfill has been performed.**

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
- **Migration:** `d012` (v12): nba_quarter_lines, injury_snapshots, play_snapshots.
  **Repair migration `d013` (v13):** nba_game_results, nba_team_statistics,
  nba_player_statistics (sport-correct NBA storage) + `injury_snapshots.return_estimate`.
  Schedule + lineups reuse d011; the baseball-named d011 result/box tables are MLB-only.
- **Completion:** mocked GOAT sweep persists the **required** outputs; each
  **conditional** output is recorded with an explicit capability state; a tier
  error is reported as capability-unavailable (not failure); hoopR Parquet import
  path exercised offline; idempotent twice; append-only; `--dry-run` persists
  nothing.
- **Expected blockers:** GOAT tier verification + QPS; box/plays historical depth
  (`provider_history_limited`); no free pregame starters; PDF fragility (if built);
  hoopR export schema mapping.

### D4 — Weather  ·  model: **Sonnet**

> **Status: complete at schema v14 (migration `d014_weather`); code built against
> mocked NWS + Open-Meteo contracts and its controlled live current-forecast gate
> passed (see status below).** Created `ingest/weather_ingestor.py`
> (`ingest-weather --forecast|--actual|--historical-forecast`), `db/repositories/weather.py`
> (append-only transition-aware `SqliteWeatherRepository`), extended the NWS client
> (point → validated returned forecast/station URLs → station observations, with
> explicit unit normalization) and the Open-Meteo client (three pinned hosts:
> current forecast, Historical Forecast API, ERA5 archive), pinned the two new
> hosts in `config` + `http_policy`, and added `tests/test_phase_d4_weather.py`.
> **The controlled live NWS/Open-Meteo current-forecast audits (1 GET each,
> `succeeded`) and a bounded `--forecast` dry run (2 games, 3 GETs, 10 forecast
> observations, 0 rejections, 0 DQ notes, 0 active failures, 0 rows persisted;
> scratch database byte-for-byte unchanged and removed) have passed. NWS station
> observations, Open-Meteo historical forecasts, and reanalysis remain
> mocked/offline verified. No persisted weather ingestion or weather backfill has
> been performed. D5A deterministic canonical + official-game matching is complete (mocked/offline); D5B1 sportsbook + D5B2 Kalshi market matching complete (schema v16; mocked/offline plus a bounded public-contract audit). Phase D is complete; Phase E1 (point-in-time foundation) is complete and Phase E2 has not started.**

- **Provider:** **NWS** primary (US, no key); **Open-Meteo** secondary + the
  historical-forecast/archive (no key). **No paid weather key at D1/D4.** Open-Meteo
  commercial use is a licensing limitation (documented note), never claimed.
- **Capabilities:** forecast/actual `supported`; NWS non-US `unavailable` →
  Open-Meteo; commercial Open-Meteo `paid_tier_required` (documented, not used).
- **Create:** `ingest/weather_ingestor.py`; weather repository; mocked NWS/Open-Meteo
  fixtures + tests.
- **Modify:** `cli.py` (`ingest-weather`).
- **Migration:** `d014` (v14). **Tables:** weather_snapshots (venues from d009).
- **Completion (criteria met, verified by mocked/offline unit tests):** forecast + actual + historical-forecast +
  reanalysis persisted as distinct kinds; outdoor-only gating by `venues.roof_type`
  (dome/fixed/indoor skipped with no request; retractable conditional, never
  assumed open); honest NWS→Open-Meteo geographic fallback (a 5xx/parse failure is
  an active failure, not a fallback); missing venue metadata skipped, never
  guessed; canonical units with explicit conversion (unknown unit → NULL + note);
  historical rows carry `pit_eligible` = UNKNOWN with a `DQ-WX-PIT-001` note;
  request dedup for same venue/date/mode; idempotent; append-only; exact
  raw-response provenance; `--dry-run` creates no database and persists nothing.
- **Correctness + PIT repair (verified by mocked/offline unit tests):** Open-Meteo is requested with
  `timeformat=unixtime` + `timezone=UTC`, so hourly timestamps are unambiguous UTC
  instants (no naive-local/DST/offset hazard); the request date range is the UTC
  union of member game windows, so a cross-midnight or previous-date window fetches
  every calendar date needed (each game still filtered to its own window, never
  cross-attached). Provider units are VALIDATED (Open-Meteo `hourly_units`; NWS
  `unitCode` incl. precipitation) — an unexpected unit leaves the canonical value
  NULL, preserves the exact value/unit in typed `extra`, and records a
  `DQ-WX-NORM-001` note (other valid fields kept). A wind RANGE (`"5 to 10 mph"`)
  is never collapsed to a scalar (NULL + `extra` + note). Each raw response keeps
  ITS OWN provider identity: any successfully-fetched NWS response stays `nws`,
  while an Open-Meteo fallback response is `open_meteo` and the derived rows
  reference it. Request counters count actual GETs (`nws_requests`/
  `open_meteo_requests`/`requests_made` identical in dry-run and persisted mode);
  `provider_fallbacks` counts events. Missing-metadata skips emit distinct DQ codes
  (`DQ-WX-VENUE/ROOF/COORD/TZ/SCHED-001`) with no provider request; an indoor skip
  is intentional, not a DQ. **Fallback classification is narrow:** ONLY a
  `/points/{lat},{lon}` 404 (the one response that genuinely proves the coordinate
  is outside NWS coverage) triggers the Open-Meteo geographic fallback. A 404 from a
  returned forecast URL, station list, or station observations, an off-host/
  disallowed returned URL (SSRF-blocked), and any 5xx/timeout/parser failure are all
  ACTIVE FAILURES — never silently reclassified as geographic unavailability (which
  would hide an NWS/endpoint defect).
- **Expected blockers:** venue coord/roof accuracy; NWS US-only (Toronto → Open-Meteo);
  Open-Meteo historical-forecast API shape.

### D5 — Canonical matching  ·  model: **OpusPlan**

> **Status: D5 complete at schema v16 — D5A + D5B1 sportsbook + D5B2 Kalshi
> matching, each independently reviewed (mocked/offline plus a bounded public
> Kalshi contract audit and parser smoke); Phase E1 (point-in-time foundation) is complete and Phase E2 has not started.** D5A shipped as the
> `sports_quant/matching/` package: deterministic team / player / venue resolvers
> (exact-provider-id 1.00 / exact-alias 0.99 / normalized-scoped 0.95 / unscoped
> 0.90), a venue-aware `game_date_local` helper (actual-venue tz → provider local
> date → home-venue tz → UTC + `DQ-TZ-001`, capped confidence), and an
> official-game canonicalizer linking/creating the existing canonical `games` row
> (official-key 1.00 / schedule-key 0.95 ≤90 min / 0.88 ≤12 h / neutral-swapped
> 0.85, `needs_manual_review` + `DQ-MATCH-007`). Every attempt records one
> `entity_match_decisions` row + normalized `match_candidates` (losers kept);
> provider references link only after acceptance and never regress. Threshold
> 0.85; matcher version `d5a-1`. `match-games`, `match-players`, and
> `matching-review` CLI are local and network-free. Seasons (empty at ship) are
> ensured deterministically per (league, year, phase) since `games.season_id` is
> `NOT NULL`.
>
> **D5A completeness repair.** Player matching is now *operational*:
> `match-players` (+ `MatchPlayersService`) loads unresolved
> `provider_player_references`, resolves each through `PlayerResolver`, records one
> decision + candidates, and links `player_id` only on acceptance (dry-run persists
> nothing; a canonical player is never created from a name). The venue-aware
> local-date hierarchy now uses its **tier 3**: the canonical home team's ordinary
> venue tz, derived from its prior non-neutral home games only when unambiguous
> (never guessed, never replacing an actual/neutral venue; invalid tz refused, not
> silent UTC). Exact provider-id links (team + player) are **league-scope
> validated** — a wrong-league crosswalk is a blocking `DQ-MATCH-014`/`DQ-MATCH-015`
> conflict, not a 1.00 match, and is never auto-repaired. **Schema limitation:**
> provider player names are not stored structurally, so player resolution keys on
> the provider id via provider-scoped `player_aliases` + roster-derived team;
> league scope is provable from `players.league_id`.
>
> **D5A independent review hardening.** Player alias lookup is **provider-scoped**
> (only the resolving provider's own aliases and provider-neutral `''` aliases are
> candidates — a different provider's alias never cross-matches, before any team /
> season / suffix / birth-date step). Roster-derived team evidence is
> **season-valid**: with `--season YEAR` only rosters dated in that season count
> (a traded player's newer team cannot resolve an earlier reference; conflicting
> in-season teams omit the team tier rather than pick by row order; absence from a
> roster is never negative evidence). Doubleheader numbering is **batch-order
> independent** — the chronological rank is computed from the whole schedule corpus
> (verified under 100 shuffles), not from creation order. The home-venue timezone
> tier is **time-bounded**: only prior home games (`scheduled_start < target`)
> contribute, so a future game or later relocation cannot shift an earlier game's
> local date. Every decision now attaches the source schedule/reference
> `raw_response_id` (and the match `run_id`); provider references link to the
> **exact** decision from the same attempt (not a "latest decision" lookup). PIT
> reads use `decisions_for_source(..., as_of=cutoff)` — a current
> `provider_*_references.*_id` link is not by itself PIT-safe (Phase E must consult
> the decision timeline). **D5B1 sportsbook matching and D5B2 Kalshi
> event/supported game-winner market matching are complete and independently
> reviewed at schema version 16; Phase E1 (point-in-time foundation) is complete and Phase E2 has not started.**
>
> **D5A follow-up repair (season / slate / knowledge-time / link integrity).**
> Season validity is now **league-specific** (`matching/season.py`): MLB uses the
> calendar year, NBA uses the BALLDONTLIE start-year convention spanning two
> calendar years (`[Y-07-01, (Y+1)-06-30]`); the same helper drives roster-team and
> career-window filtering so they agree. Doubleheader grouping now uses the
> **resolved venue-local date** (venue tz → provider date → knowledge-bounded home
> tz → UTC) rather than requiring a provider local date, with a `schedule_id`
> tie-break on equal `observed_at`. The home-venue tier is bounded by **knowledge
> time as well as event time**: a prior game contributes only when it has an
> accepted, non-swapped game decision with `decided_at ≤ the target schedule
> observation cutoff` — so a game discovered in a later backfill, a later manual
> approval, or an unreviewed neutral-swapped match cannot shift an earlier game's
> local date (conservative UTC + `DQ-TZ-001` when nothing qualifies). The generic
> provider-reference link path **no longer swallows exceptions**: an absent
> optional crosswalk is an explicit checked skip, a conflicting link is blocking
> (`DQ-MATCH-016`), and a real repository/constraint failure propagates to command
> exit 1.
>
> **D5A atomic decision-and-link hardening (pre-D5B2).** The official-game and
> provider-player accept paths now share the D5B1 atomic invariant via
> `matching/linkatomic.py` (`classify_link_attempt` → CLEAN / REPLAY / CONFLICT;
> `MatchLinkError`). Before recording an accepted decision the matcher inspects
> the provider reference's CURRENT link: an exact idempotent replay records **no**
> new accepted decision (game replay → `canonical_entities_unchanged`, player →
> `already_linked`); an existing link to a different entity or a corrupt/mismatched
> supporting decision is a **blocking rejection** (`DQ-MATCH-003` game /
> `DQ-MATCH-016` player) that records no accepted decision; only a clean reference
> records the accepted decision and then applies + verifies the link, and a
> non-`LINKED` result raises `MatchLinkError` so the whole run rolls back (exit 1)
> rather than commit an accepted decision without its link. The official-game
> **create** path additionally pre-checks the provider reference *before* creating
> any season/game row, so a conflict never leaves an orphan canonical game or an
> inflated `canonical_games_created` counter. Team/venue resolution decisions
> remain input-resolution audit records (recorded per run); their crosswalk
> conflicts stay blocking (`DQ-MATCH-016`), and venues are not auto-linked via a
> crosswalk (no provider_venue_reference link path). The player entry point only
> processes `player_id IS NULL` references, so replay/conflict there are defensive.
>
> **Packaging / season contract (pre-D5B2).** `pyproject.toml` uses automatic
> package discovery (ships `sports_quant.matching`; excludes tests/venv/build/data)
> and declares `sports_quant.db` package-data `migrations/*.sql`; a CI
> `wheel-smoke` job installs the built wheel non-editable and runs `db-init` to
> schema v15 from outside the source tree. `schema.season_label` is corrected to
> the NBA START-year convention so it agrees with `season_year_for` /
> `season_bounds` (the one contract). Schema stays v15; no d016.
>
> **D5B1 — sportsbook-event matching (built, mocked/offline; migration d015,
> schema v15).** `sports_quant/matching/sportsbook.py` resolves already-ingested
> The Odds API `sportsbook_events` to canonical `games` via provider-scoped
> (`the_odds_api`) team aliases + venue-aware schedule/time evidence, with NO
> price/probability/bookmaker/score input (the module never imports
> `sportsbook_price_snapshots`). Tiers: `schedule_key_exact` 0.95 (±90 min),
> `schedule_key_window` 0.88 (±12 h), `schedule_key_swapped` 0.85 (neutral only,
> review-gated `DQ-MATCH-007`); non-neutral reversed → blocking `DQ-MATCH-003`; no
> official-key tier. `d015` adds `sportsbook_events.match_decision_id` + typed
> `orientation` (link columns set together, immutable once set). Existing
> h2h/spreads/totals outcomes are validated vs orientation; unknown/malformed →
> `DQ-SB-OUTCOME-001` (retained, never dropped). `SqliteSportsbookRepository`
> gains `link_game`, `list_events_for_matching`, `events_linked_to_game`,
> `event_link`, `is_orientation_approved` (PIT-aware). CLI: `match-games --source
> sportsbook`.
>
> **D5B2 — Kalshi event + game-winner market matching (built, mocked/offline;
> migration `d016_kalshi_matching`, schema v16).** `sports_quant/matching/kalshi.py`
> + pure versioned parsers in `matching/kalshi_parse.py` resolve public MLB/NBA
> Kalshi events and supported binary game-winner markets to canonical `games` via
> an exact series allowlist (`KXMLBGAME`/`KXNBAGAME`), **series-specific versioned
> ticker parsers** (MLB `kmlb-2`: `KXMLBGAME-{YYMONDD}{HHMM}{AWAY}{HOME}` with a
> venue-local `HHMM` clock; NBA `knba-1`: `KXNBAGAME-{YYMONDD}{AWAY}{HOME}`
> date-only) dispatched by exact series (team codes split against curated
> `kalshi_public` aliases), explicit title/`rules_primary` team + Yes-subject
> agreement, and venue-aware canonical schedule tiers (`kalshi_ticker_time` 0.97
> from the MLB ticker clock converted per candidate through the venue timezone +
> venue-local slate; `kalshi_date` 0.92 date-only; threshold 0.85). d016 adds
> `kalshi_events.match_decision_id`; `kalshi_markets.match_decision_id` +
> `yes_team_id` + `matched_rules_hash` + typed `market_semantic` (only
> `game_winner`), with link-integrity triggers (set-together; game/Yes-team/
> matched-hash immutable once set). The Yes team is verified transactionally to be
> a participant in the linked game. Only game-winner markets link; spreads,
> totals, props, period, team-total, and combo markets are retained and reported
> as unsupported semantics (never mislabeled as moneylines). A current
> `rules_hash` differing from the matched hash raises a blocking `DQ-MATCH-004`,
> flags the decision for review, and never rewrites the matched hash;
> `is_kalshi_market_orientation_approved(as_of=…)` is fail-closed and as-of
> correct. Event/market links are atomic via `matching/linkatomic.py`. Order
> books, trades, prices, `result`, and settlement are never evidence (SQL-trace
> tested). New DQ codes: `DQ-KAL-SERIES-001` (supported-series ticker malformed),
> `DQ-KAL-TITLE-001` (ticker/title disagreement), `DQ-KAL-RULES-001` (ticker/title/
> rules or Yes-subject disagreement), `DQ-KAL-YES-001` (Yes team not a
> participant); reuses `DQ-MATCH-003/004/006` and `DQ-TZ-001`. CLI: `match-markets`.
> **No authenticated Kalshi access or account/order surface. Phase E has not
> started.**
>
> **D5B2 repair (service atomicity / replay / historical readiness / title / No
> side / review-state).** Event and market accepts wrap the decision + link in one
> service-owned transaction (a direct persisted `KalshiMatchingService` call is
> atomic by itself; a link failure rolls back the decision + candidates + semantic
> fields; accepted/linked counters increment only after commit). `ALREADY_LINKED`
> replay verifies the full existing link (same game/Yes/hash/semantic, exact
> decision owned by this event/market, accepted, not review-gated) — a corrupt
> pairing is blocking, never idempotent. `is_kalshi_market_orientation_approved`
> separates current (today's hash + review flag, fail-closed) from historical
> (`as_of` uses only decision-existence + DQ `detected_at`/`resolved_at`, never
> today's mutable hash/flag). Ordered `A at B` titles are validated for
> away/home orientation (reversed rejected); `A vs B` stays unordered.
> `no_sub_title`, when present, must name a game participant (the current public
> Kalshi contract sets it equal to the Yes-subject team, per the live audit; an
> unrelated or unresolved team is rejected). Automated
> rules-hash invalidation uses `flag_for_review` (sets `needs_manual_review=1`,
> leaves `reviewed_by`/`reviewed_at` NULL) so it is never mistaken for a completed
> human review; `mark_reviewed` stays reserved for an audited reviewer. Schema
> remains v16; no d017.
>
> **D5B2 live public-contract repair (current MLB/NBA shapes).** A bounded GET-only
> unauthenticated public audit (3 requests) plus a controlled live parser smoke
> (2 requests; 5 of a 6-request budget, persisted nothing) replaced the earlier
> mocked single-ticker contract with the series-specific `kmlb-2` (MLB, ticker
> venue-local `HHMM`) and `knba-1` (NBA, date-only) parsers and the current rules
> wording `If {Yes} wins the [Game N: ]{A} (vs|at) {B} professional {sport} game
> originally scheduled for {Mon D, YYYY}[ at {H:MM AM/PM TZ}], then …` (no "the"
> before Yes; no "against the"). The MLB ticker clock is converted **per candidate**
> through the venue's `zoneinfo` timezone (knowledge-time gated), refusing on
> unknown zone, DST fold/gap, or a rules timezone abbreviation
> (`EDT`/`CDT`/`MDT`/`PDT`) that disagrees with the venue zone — never machine tz,
> UTC, or a fixed offset. The live smoke parsed **20/20** open MLB events under
> `kmlb-2`; NBA had no open events (offseason), so its live evidence rests on the
> audit + sanitized fixtures (`matching/tests/kalshi_fixtures.py`); parser
> versions are golden-pinned so a provider change breaks loudly. Full suite:
> 1315 passed / 1 skipped (Phase D). Schema remains v16; no d017. (Phase E1 later
> adds the `sports_quant/pit/` suite; the current full-suite total is 1358 passed
> / 1 skipped.)
>
> **D5B1 correctness repair (season / local-slate / conflict / outcome / DQ).**
> Team resolution now uses the league-specific season (`season_year_for`): an NBA
> Jan–June date maps to the previous start year, and a naive/unparseable commence
> produces no guessed season and no match. Local-slate agreement is enforced as a
> real candidate requirement — each league/team/UTC-window candidate must share
> the event's venue-derived local date (candidate actual-venue tz known by
> `last_observed_at` → home-venue tz → UTC last resort, capped 0.88); a
> contradictory candidate is excluded and an unresolvable timezone is surfaced
> (`DQ-TZ-001`), never forced to UTC. A blocking orientation conflict now prevents
> linking entirely (no accepted decision, no `game_id`/`match_decision_id`/
> `orientation`, no outcome validation, exit 1). `is_orientation_approved()` is
> fail-closed (also requires decision↔link agreement, no conflicting linked event,
> no unresolved blocking identity/orientation DQ on the event). Outcome roles are
> recomputed provider-side from immutable names + market and disagreements
> surfaced scoped to the outcome, never trusting or rewriting the stored role. DQ
> issues are scoped to event / `sportsbook_market` / `sportsbook_outcome` and
> idempotent per `(rule, entity, provider, description)`. No new migration (schema
> stays v15).
>
> **D5B1 independent-review repairs.** Accepted decision + link are one atomic
> transaction: an exact idempotent replay records no new decision
> (`events_already_linked`); an existing link to a different game/orientation or a
> corrupt supporting decision is a blocking rejection with no fresh accepted row;
> a non-`LINKED` result in the fresh path raises and rolls the attempt back (exit
> 1). UTC-fallback candidates are kept only when the UTC date equals the canonical
> local date (Policy A) — a cross-midnight game without timezone evidence is
> excluded. The actual event-venue tier requires the game's venue *association*
> (an accepted non-swapped `game` decision `decided_at <= last_observed_at`), not
> just `venues.first_observed_at`. `is_orientation_approved(as_of=…)` is
> temporally correct over DQ `detected_at`/`resolved_at` and conflicting-event
> decision times. Outcome approval uses the real readiness check after a verified
> link. Unsupported market keys are impossible to ingest (`sportsbook_markets`
> CHECK); market shape is validated per betting contract (alternate lines are not
> false duplicates). Neutral swapped events have **no implemented approval path**
> and stay excluded from price-safe use. Decision/DQ `raw_response_id` is honest
> first-observation provenance (schema v15 has no per-field current-response
> column). Schema stays v15. **D5B1 has completed its independent correctness
> review.**

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
