# Data Foundation Plan

Master plan for the real historical data foundation of the read-only MLB/NBA
betting **recommendation** engine.

## Status

**Phase A is complete**, including the a003 integrity patch. **Phase B is
complete** (migrations 001–006, schema v6), including the `b006` integrity
repair: raw-response storage, ingestion-run tracking, sportsbook
events/markets/outcomes, point-in-time price snapshots, the Odds API ingestion
service, and the `ingest-odds` CLI now exist, with team/sport validation,
stale-metadata protection, transition-aware price deduplication, and correct
failed-request counting, and the `b006` price-snapshot uniqueness rule
(`UNIQUE (sb_outcome_id, observed_at, content_hash)`).

**Phase C is complete** (migrations `c007_kalshi` + `c008_kalshi_metadata_integrity`,
schema v8): the public, GET-only, unauthenticated Kalshi surface is ingested into
`kalshi_events`, `kalshi_markets`, `kalshi_orderbook_snapshots`,
`kalshi_orderbook_levels`, and `kalshi_public_trades`, with derived executable
asks, transition-aware order-book history, trade idempotency, point-in-time
queries, and the `ingest-kalshi` CLI. The `c008` integrity repair adds explicit
first-vs-current metadata provenance, strict event/market/trade validation,
accurate insert/update/dedup counters (including a new `ingestion_runs.records_updated`),
and a single validation path shared by persisted and dry-run ingestion. No
Kalshi credential, private key, or signing is used anywhere; there is no
account/balance/position/fill/order column in the schema. All 894 tests pass
(1 skipped) under Ruff and mypy.

**Phase D provider selection and implementation design are complete; D1 provider
infrastructure and D2 MLB ingestion code are complete (schema v11, migration
`d011_official_games_stats`). No large historical backfill has been performed;
live MLB access still requires an approved provider audit and smoke test.** D2
added append-only, transition-aware official-MLB observation tables
(schedule/result/inning/team+player stats/roster/probable/lineup), the extended
MLB StatsAPI client (date-ranged schedule with probable/lineup hydration, box
score, line score), a typed status mapper, five typed repositories, and the
`ingest-mlb`/`ingest-lineups` CLI — all anchored on `provider_game_references`
(canonical resolution deferred to D5), tested against mocked StatsAPI fixtures.
The providers are chosen
(MLB StatsAPI; **BALLDONTLIE at the GOAT tier** for NBA — the free tier does
*not* supply box scores, player stats, injuries, plays, or lineups; NWS primary
+ Open-Meteo secondary/historical-forecast for weather; hoopR as an offline-only
NBA history supplement; Chadwick crosswalk; the official NBA injury report as an
optional cross-check; SportsDataIO Discovery Lab as an optional delayed
comparison; Sportradar/SportsDataIO commercial/Stats Perform as the professional
path). D1 has shipped the GET-only provider clients (MLB StatsAPI, BALLDONTLIE,
NWS, Open-Meteo), a typed provider-capability system, the `provider-audit` and
`ingest-venues` commands, and a corrected dry-run contract (dry-run persists
absolutely nothing) — see `PHASE_D_PROVIDER_DECISIONS.md` and
`PHASE_D_IMPLEMENTATION_PLAN.md`. A D1 **integrity repair** (migration
`d010_provider_audit_integrity`) then hardened the audit: a static capability
*declaration* is never persisted as an externally *observed* result; the audit
runs one minimal approved GET **per capability group** and records only what a
probe verified (with the probe, endpoint, HTTP status, error classification, and
raw-response evidence), leaving unprobed capabilities declared-only; provider
403s are only ever classified as tier-restricted with explicit plan/subscription
evidence (never a broad word alone); and the BALLDONTLIE allow-list is tightened
to explicit documented endpoints.

**D1 provider infrastructure and D2 MLB ingestion code are complete. D3–D5 have
not started. A live MLB provider audit and small smoke test remain required
before a large backfill.** D2 ingests the MLB StatsAPI schedule, box scores, line
scores, probable pitchers, posted lineups, and **date-aware rosters** into
append-only, transition-aware official-observation tables (migration `d011`,
schema v11), anchored on `provider_game_references` with provider player/team
references created from each supplying response (canonical resolution deferred to
D5). Two correctness distinctions are enforced: (1) **normal result progression
is not a correction** — a score/hit/error increase, an inning advance, or a
status-only transition (scheduled→pregame→in_progress→final with a
logically-consistent cumulative result) appends an ordinary observation, whereas
a **genuine correction** (a previously-final result changing, or a cumulative
run/hit/error total *decreasing*) sets `is_correction = 1` and increments
`corrections_appended`; (2) **date-aware roster ingestion** fetches each unique
`(provider_team, official_game_date)` pair once, stores that official date as
`roster_date`, and — when a game has no official date — records a data-quality
note instead of substituting today's roster. Every D1/D2 test uses mocked
transports; no live provider call was made for this work. Phase E remains
planning.

**Controlled live MLB StatsAPI check (2026-07-24).** The controlled live MLB
StatsAPI provider audit and bounded dry-run smoke test completed successfully on
July 24, 2026. The audit (`provider-audit --provider mlb_statsapi`) exited 0 with
status `succeeded`, made 5 GET-only keyless requests, and recorded 5 externally
observed capabilities (teams, schedules, games, venues, players — each with
probe/endpoint/HTTP-200/raw-response evidence) alongside 14 declared-only
capabilities (19 recorded), 0 active failures, and a single honest capability
note (`DQ-CAP-001`: `confirmed_pregame_starters` unavailable). Authentication was
correctly reported as not applicable for this keyless provider. The smoke test
covered the five completed MLB games from July 23, 2026 and exercised results,
box scores, inning lines, probable pitchers, posted lineups, and date-aware
rosters (`ingest-mlb --dry-run`, exit 0, `dry_run=true`, `run_id=null`, status
`succeeded`): 5 games received, 21 sequential GET-only requests, 10 roster
requests (one per distinct team/official-date pair), 0 corrections, 0
data-quality issues, 0 rejections, and 0 active failures. The dry run persisted
nothing — its isolated target database was never created and the corpus changed
only from the persisted audit run. No persisted MLB ingestion or historical
backfill has been performed. D3–D5 have not started.

| Phase | Scope | Status |
| --- | --- | --- |
| A | Database engine, migrations, core entities, `db-init` | ✅ Complete (schema v3) |
| B | Raw responses, ingestion runs, sportsbook odds | ✅ Complete (schema v6, incl. `b006` integrity repair) |
| C | Kalshi public events, markets, books, trades | ✅ Complete (schema v8, incl. `c008` integrity repair) |
| D | Official providers, weather, canonical matching | ◧ **D1 infra + D2 MLB ingestion code complete (schema v11, incl. `d010` audit-integrity repair and `d011` official MLB snapshots); D3–D5 not started** |
| E | Point-in-time builder, quality rules, leakage tests | ◻ Not started |

Companion documents:

- `DATA_ARCHITECTURE.md` — engine choice, canonical IDs, full schema, raw-response contract
- `POINT_IN_TIME_DATA.md` — timestamp semantics, leakage prevention
- `ENTITY_MATCHING.md` — normalization, aliases, game/market matching
- `PHASE_D_PROVIDER_DECISIONS.md` — Phase D provider evaluation, selection, cost/coverage
- `PHASE_D_IMPLEMENTATION_PLAN.md` — Phase D schema, migrations, CLI, D1–D5 staging

---

## 1. Goal and non-goals

**Goal.** A durable, auditable, point-in-time-correct corpus of historical MLB
and NBA data — games, sportsbook prices, Kalshi public market data, injuries,
lineups, probable pitchers, weather — from which leakage-free research datasets
can be built and rebuilt reproducibly.

**Explicit non-goals for this work.** Not deferred-and-maybe; out of scope:

| Not in scope | Where it belongs |
| --- | --- |
| Model training | Research lane, after the corpus exists |
| Feature engineering | A later stage; Phase E delivers rows and cutoffs, not features |
| Monte Carlo simulation | Research lane |
| Order placement, cancellation, management | Nowhere — permanently quarantined |
| Authenticated Kalshi endpoints | Nowhere — public data only |
| Rewriting working provider clients | Nowhere — they are reused as-is |

**Safety rules carried forward unchanged.** Everything in `CLAUDE.md` and
`READ_ONLY_ARCHITECTURE.md` remains in force. The data foundation is additive:
GET-only, public-data-only, no account/portfolio/order/fill endpoints, no
credential in any stored field, execution still quarantined. The
`sports_quant.http_policy` transport remains the only network path, so new
ingestion code inherits the existing default-deny policy rather than
re-implementing it.

---

## 2. Repository areas inspected

| Area | Finding | Consequence |
| --- | --- | --- |
| `sports_quant/providers/` | Working `OddsApiClient` + `KalshiClient`, typed, cached, redacting, policy-wrapped | **Reuse unchanged.** Ingestors consume them. |
| `sports_quant/http_policy.py` | Default-deny GET-only transport, host + path allow-list | **Reuse.** Phase D extends the allow-list for official providers. |
| `sports_quant/redaction.py` | `sanitize_url`, `sanitize_params`, name-based masking | **Reuse.** The raw-response writer depends on it. |
| `sports_quant/config.py` | `.env`-only, `SecretStr`, pinned Kalshi URL, read-only invariants | **Extend** with a `DATABASE_PATH` setting. |
| `sports_quant/cli.py` | `argparse` sub-parser dispatch, `CheckStatus`/`CheckResult` classification | **Extend** with the six new commands, reusing the status vocabulary. |
| `streaming/event_envelope.py` | `canonical_json`, four-timestamp discipline, content hashing, correction linkage | **Reuse** `canonical_json`. Model the timestamp discipline on it. |
| `streaming/deduplicator.py` | `SqliteDedupStore` — stdlib `sqlite3`, WAL, `INSERT OR IGNORE` | **Precedent** for engine and idiom. |
| `streaming/replay.py` | `RawEventStore` / `JsonlRawEventStore` for *live* envelopes | **Leave untouched.** Different concern. |
| `tracking/base.py` | `Protocol` + `InMemory*` + `Postgres*` repository triple | **Pattern to follow** for every repository. |
| `intel/base.py`, `intel/history.py` | `SourceMeta(published_at, retrieved_at)`, immutable `StatusSnapshot`, append-only `StatusHistory` | **Reuse the model.** Injury/lineup tables mirror it. |
| `intel/player_matching.py` | `MATCHED`/`AMBIGUOUS`/`UNMATCHED`, exact-id + `(team, name)` indexes, refuses to guess | **Extend, do not replace.** |
| `backtest/data_quality.py` | A–F grading, `execution_valid`, `issues` vs `notes` | **Reuse the vocabulary** in `data-quality`. |
| `probability/datasets.py` | `GameStateDataset` + `chronological_split()`; docstring states synthetic builders are placeholders with a stable interface | **The Phase E output contract.** |
| `probability/tests/test_probability.py` | Asserts `sqlite3` never appears in `probability/` | **A constraint to honour and mirror.** |
| `evaluation/`, `gateway/`, `state/` | Live decision/execution lanes; gateway quarantined | **Leave untouched.** Must not import the DB. |

Three duplicate content-hash implementations were found
(`streaming/event_envelope.py`, `intel/base.py`, `evaluation/decision.py`).
Adding a fourth is explicitly rejected; see `DATA_ARCHITECTURE.md` §4.2.

---

## 3. Existing-code integration

### Reuse unchanged
`sports_quant/providers/` (both clients, `ResponseCache`), `http_policy.py`,
`redaction.py`, `streaming.event_envelope.canonical_json`,
`streaming.latency.monotonic_ns`, `probability.datasets.GameStateDataset`,
`backtest.data_quality` grading vocabulary.

### Modify (additively, small, test-covered)
- `sports_quant/config.py` — add `DATABASE_PATH` (default `./data/corpus.db`),
  plus `.env.example`. Read-only invariants unchanged.
- `sports_quant/cli.py` — register six new sub-commands.
- `sports_quant/http_policy.py` — Phase D only: add official-provider hosts to
  the allow-list. Still GET-only, still default-deny.
- `intel/player_matching.py` — Phase D: back the directory with
  `player_aliases`; keep the existing API and semantics.
- `intel/base.py` — Phase B: switch `_canonical` to the shared
  `canonical_json`, removing one of the three duplicate hashers.

### Quarantined — unchanged
`gateway/` stays quarantined. Nothing in the data foundation imports it. The
corpus stores **public** Kalshi trade prints, never fills, positions, or orders.

### Left untouched
`evaluation/`, `state/`, `streaming/` (other than reusing `canonical_json`),
`tracking/`, `probability/` internals, `backtest/` internals.

### Avoiding duplication — standing rules
1. One provider client per provider. Ingestors call the existing ones.
2. One content hasher. `canonical_json` from `streaming.event_envelope`.
3. One normalization function. `matching/normalize.py`, used at write and read.
4. One as-of query builder. `pit/asof.py`.
5. One redaction utility. `sports_quant/redaction.py`.
6. Provider response models stay in `providers/`; DB row models stay in `db/`.
   The ingestor maps between them, so a schema change never edits a client.

---

## 4. CLI design

All commands are read-only with respect to external systems: GET-only, public
endpoints, no order of any kind. They extend the existing `argparse` dispatch in
`sports_quant/cli.py` and reuse its `CheckStatus` (`OK` / `SKIPPED` / `FAILED`)
classification vocabulary.

### Global conventions

| Aspect | Rule |
| --- | --- |
| Exit `0` | Success, including successful skips |
| Exit `1` | Genuine failure of something active |
| Exit `2` | Read-only startup invariants violated (existing `ReadOnlyStartupError` path) |
| Exit `3` | Database missing, unmigrated, or schema-checksum mismatch |
| Global flags | `--db PATH`, `--json`, `--verbose`, `--dry-run` |
| Secrets | No command ever prints the Odds API key; presence only |
| Idempotency | Every command is safe to re-run; re-running never mutates history |

`--json` emits machine-readable output on stdout with human text on stderr, so
the commands compose in scripts without parsing prose.

### `db-init`

```
python -m sports_quant db-init [--db PATH] [--dry-run]
```

Creates the database if absent and applies every unapplied migration in order.
Seeds leagues, current teams, and their seed aliases.

- **Output:** current schema version, migrations applied (or "already current"),
  seeded row counts.
- **Idempotency:** re-running applies nothing and reports "already current".
  Seeds use `INSERT OR IGNORE`.
- **Failure:** each migration runs in its own transaction; a failure rolls that
  migration back, leaves `schema_versions` at the last good version, and exits
  `3`. A checksum mismatch on an already-applied migration is a hard error, not
  a warning — a silently edited migration means the schema no longer matches
  what the corpus was built with.
- **`--dry-run`:** lists pending migrations, writes nothing.

### `ingest-odds`

```
python -m sports_quant ingest-odds --sport {mlb,nba} [--markets h2h,spreads,totals]
                                   [--regions us] [--db PATH] [--dry-run]
```

Fetches current odds for one sport via the existing `OddsApiClient` and writes
one `raw_responses` row plus derived events/markets/outcomes/price snapshots.

- **Output:** run id, requests made, credits remaining (from the existing
  `CreditHeaders`), events seen, new price snapshots, duplicates skipped,
  unmatched events.
- **Exit `0`:** success, **including an out-of-season sport** — a `SKIPPED`
  result, consistent with the existing `providers-check` semantics.
- **Exit `1`:** the sport is active but the fetch or write failed.
- **Idempotency:** transition-aware — `UNIQUE (sb_outcome_id, observed_at,
  content_hash)` plus an immediate-temporal-predecessor comparison in the
  repository (migration `b006`; see `DATA_ARCHITECTURE.md` §3.6.1). Re-running
  with unchanged prices writes zero snapshot rows, an unchanged re-poll
  collapses, and a price that reverts to an earlier value is still preserved.
  Raw responses are kept per observation (not deduplicated), so every run can
  name the exact response it received.
- **Failure:** the raw response is persisted **before** parsing, so a parse
  failure never loses the bytes — it marks the run `partial`, records a
  `data_quality_issues` row, and exits `1`. The corpus keeps the response for a
  later re-parse.

### `ingest-kalshi` (implemented, Phase C)

```
python -m sports_quant ingest-kalshi [--status open] [--event-ticker T]
                                     [--market-ticker T] [--limit N]
                                     [--include-orderbooks] [--include-trades]
                                     [--max-pages N] [--db PATH] [--dry-run]
```

Fetches Kalshi **public** events, markets, and optionally order books and public
trade prints via the existing `KalshiClient`. Public, GET-only, unauthenticated
— no Kalshi credential, key, or signing is ever used or required.

- **Output:** run id, requests made, events/markets seen (and rejected),
  order-book snapshots new/unchanged and levels stored, public trades
  new/duplicate/rejected, sanitized rejection reasons.
- **Exit `0`:** success, **including zero matching markets** (reported as a
  clean zero, not a failure).
- **Exit `1`:** a genuine provider, parse, validation, or persistence failure.
- **Exit `3`:** database missing or unmigrated for Phase C.
- **Idempotency:** order books use transition-aware dedup
  (`UNIQUE (market_ticker, observed_at, content_hash)` + immediate-predecessor
  comparison); trades dedup on `(market_ticker, content_hash)`.
- **Safety — bounded fan-out:** `--limit` (default **20**) bounds events,
  markets, **and** the per-market order-book/trade fan-out, so the default never
  sweeps every book on the exchange. When the market list exceeds `--limit`, the
  truncation point is **reported explicitly** rather than silently capped, so a
  partial sweep is never mistaken for a complete one.
- **Dry-run:** performs the external GETs and normalization but persists
  **nothing at all** — not the run, not the raw response, not a single
  normalized row (see the dry-run note in the Phase C completion criteria).

### `data-status`

```
python -m sports_quant data-status [--league {mlb,nba}] [--since DATE] [--json]
```

Read-only corpus summary. Never touches the network.

- **Output:** schema version; per-table row counts; coverage windows
  (earliest/latest `observed_at` per snapshot type); last run per provider with
  status; unmatched-entity counts; open data-quality issues by severity.
- **Exit `0`:** report produced, whatever it says.
- **Exit `3`:** database missing or unmigrated.

`data-status` reports; it does not judge. Judging is `data-quality`'s job, and
keeping them separate means a status check never fails a pipeline.

### `data-quality`

```
python -m sports_quant data-quality [--league {mlb,nba}] [--rule CODE]
                                    [--review] [--fail-on {blocking,issue,note}]
                                    [--json]
```

Runs the data-quality and leakage rule set over the corpus. Never touches the
network.

- **Output:** an A–F grade and `execution_valid` flag reusing
  `backtest/data_quality.py`'s vocabulary, then findings grouped by severity
  with `rule_code`, counts, and examples. `--review` lists open manual-review
  items grouped by rejection reason, most-frequent first.
- **Exit `0`:** nothing at or above `--fail-on` (default `blocking`).
- **Exit `1`:** findings at or above the threshold.
- **Exit `3`:** database missing or unmigrated.
- **Idempotency:** pure read plus upserts into `data_quality_issues` keyed by
  `(rule_code, entity_id)`; re-running does not duplicate findings.

Default `--fail-on blocking` makes the command CI-usable: only genuinely
corpus-invalidating findings (`DQ-MATCH-003`, `DQ-MATCH-004`, `DQ-MATCH-006`,
leakage violations) fail the build, while notes accumulate visibly.

---

## 5. Implementation phases

Strictly ordered. Each phase's completion criteria are its exit gate; a phase
does not begin until its dependencies are green under Ruff, mypy, and pytest.

### Phase A — Database engine, migrations, core entities ✅ COMPLETE

**Depends on:** nothing.

**Created:** `sports_quant/db/{__init__,engine,ids,schema,normalize,models,init}.py`;
`sports_quant/db/migrations/{a001_core_entities,a002_games}.sql`;
`sports_quant/db/repositories/{__init__,base,leagues,teams,players,games}.py`;
`sports_quant/db/seeds/{__init__,loader,mlb_teams,nba_teams}.py`;
`sports_quant/db/tests/{conftest,test_engine,test_migrations,test_ids,test_normalize,test_repositories,test_seeds,test_isolation,test_db_init_cli}.py`.

**Modified:** `sports_quant/config.py` (`DATABASE_PATH` +
`resolved_database_path()`), `.env.example`, `.gitignore` (ignore `data/`),
`sports_quant/cli.py` (`db-init`), `pyproject.toml` (packages + testpaths).

**Tables:** `schema_versions`, `leagues`, `seasons`, `teams`, `team_aliases`,
`players`, `player_aliases`, `games`, `game_status_history`.

**Repositories:** `LeagueRepository`, `SeasonRepository`, `TeamRepository`,
`TeamAliasRepository`, `PlayerRepository`, `PlayerAliasRepository`,
`GameRepository` — each a `Protocol` with a `Sqlite*` implementation.

**Tests:** 178 in `sports_quant/db/tests/`, all against temporary databases.

**CLI:** `db-init`.

**Delivered:** migrations 001–002 applied in order and idempotently; checksum
mismatch raises; `PRAGMA foreign_keys` verified ON per connection; ULIDs
prefixed, monotonic, unique; append-only triggers reject UPDATE/DELETE; CRUD
round-trips; 30 MLB + 30 NBA teams and 311 aliases seeded deterministically;
isolation enforced by source scan **and** by subprocess import check.

#### Deviations from the original Phase A sketch

Each was a correction found during implementation, not a shortcut:

| # | Change | Why |
| --- | --- | --- |
| 1 | `team_aliases` uniqueness is scoped to `team_id`, not `league_id` | The original sketch referenced a `league_id_denorm` column that was never defined, and league-scoping would have **rejected** legitimate shared aliases — "chicago" belongs to both the Cubs and the White Sox. That is ambiguity to record, not a write to refuse. A real `league_id` column was added for lookup. |
| 2 | `provider`, `valid_from_season`, `valid_to_season` are `NOT NULL` with sentinels (`''`, `0`, `9999`) | SQLite treats two `NULL`s as **distinct** inside a `UNIQUE` constraint, so nullable columns would let identical seed rows insert again on every `db-init`, silently defeating idempotency. |
| 3 | `game_status_history.raw_response_id` / `raw_response_hash` are nullable with no FK | `raw_responses` is a Phase B table. Phase A has no ingestion, so no row can reference one yet. Phase B adds the FK and tightens the hash to `NOT NULL`. |
| 4 | `season_id` includes the phase (`sn_mlb_2026_regular`) | A league runs preseason, regular and postseason inside one year, and the `seasons` uniqueness key covers all three. The original `sn_mlb_2026` example would have collided. |
| 5 | Migration numbers are a single global sequence; the phase letter is cosmetic | `a001` and `b001` would both parse to version 1 and collide. Phase B continues at `003`. |
| 6 | `InMemory*` repositories not built | They would have had no consumer in Phase A (tests use temporary SQLite files, per requirement), and an in-memory repository cannot reproduce SQLite's constraint semantics — an unverified reimplementation gives *false* test confidence. Deferred to Phase B, where ingestors give them a real consumer. |
| 7 | Normalization lives in `db/normalize.py`, not `matching/normalize.py` | Phase A needs it for alias storage and lookup. Phase D's matcher imports this module rather than defining a second normalizer. |
| 8 | Added `db/models.py` and `db/init.py`; no `db/hashing.py` | Typed row models need a shared home to avoid import cycles; `db/init.py` keeps SQL out of CLI code. A `hashing.py` shim was unnecessary — `canonical_json` is imported directly from `streaming.event_envelope`. |
| 9 | Added `split_sql_statements()` to the engine | `sqlite3.Cursor.executescript` issues an implicit `COMMIT`, which silently ended the migration transaction and would have left a half-applied migration committed. The splitter handles string literals, comments, and `CREATE TRIGGER` bodies. |
| 10 | `TeamSeed.extra_cities` added | The Clippers brand as "LA", so "Los Angeles" would have resolved unambiguously to the Lakers. Recording the extra city makes the genuine NBA ambiguity detectable. |

#### Integrity patch — migration `a003_integrity_guards`

Three defects were found in the Phase A schema and repaired additively (001 and
002 are immutable once applied):

**1. Foreign keys did not enforce league consistency.** An FK proves a
referenced row exists, not that it belongs to the same league — an MLB game
could reference an NBA season or an NBA team and the database accepted it. The
row looks well-formed, and every downstream join inherits the error. `a003`
adds `BEFORE INSERT` and column-scoped `BEFORE UPDATE` triggers for:
`games.league_id` vs. its season, home team and away team; `team_aliases.league_id`
vs. its team; `player_aliases.league_id` vs. its player. Plus
`games.original_start` immutability, since that column is the only record that a
game was ever moved.

**2. A stale backfill could regress current game state.** `record_status()`
copied the row it had just written into `games.status`, so a late-arriving
observation of an *earlier* moment overwrote a newer state. Current state is now
recomputed from the newest observation by `(observed_at, status_id)` after every
insert. See §5.1 below.

**3. Status deduplication was global rather than transition-aware.** See §5.2.

---

### Phase B — Raw responses, ingestion runs, sportsbook odds ✅ COMPLETE

**Depended on:** A (complete). Phase B additionally added the `raw_responses`
foreign key to `game_status_history` and consolidated `intel/base.py` onto the
shared `canonical_json`.

**Created:** `sports_quant/db/migrations/{b004_raw_responses,b005_sportsbook,b006_sportsbook_transition_dedup}.sql`;
`sports_quant/db/repositories/{raw_responses,ingestion_runs,sportsbook}.py`;
`sports_quant/ingest/{__init__,runner,odds_ingestor}.py`;
`sports_quant/ingest/tests/{__init__,conftest,test_runner,test_odds_ingestor,test_no_secrets}.py`;
`sports_quant/db/tests/{test_sportsbook_repositories,test_price_snapshot_schema}.py`;
`sports_quant/tests/test_ingest_cli.py`.

**Modified:** `sports_quant/cli.py` (`ingest-odds`); `sports_quant/providers/odds_api.py`
(sanitized `RawExchange` + `fetch_odds_raw`, no second client); `sports_quant/redaction.py`
(header allow-list); `sports_quant/db/{schema,ids,models}.py`; `intel/base.py`
(adopt shared `canonical_json`); `pyproject.toml`.

**Tables:** `ingestion_runs`, `raw_responses`, `sportsbook_events`,
`sportsbook_markets`, `sportsbook_outcomes`, `sportsbook_price_snapshots`.

**Repositories:** `SqliteRawResponseRepository`, `SqliteIngestionRunRepository`,
`SqliteSportsbookRepository` — each a `Protocol` plus a SQLite implementation.

**Tests delivered:** migration applies once and idempotently; API keys sanitized
from URLs and request parameters; authorization/`set-cookie` headers never
stored (allow-list); raw responses preserved and immutable; every normalized
row traces to a raw response (id **and** hash); MLB and NBA odds normalized;
h2h/spreads/totals all persist; repeated ingestion writes zero new snapshots; a
changed price appends a new snapshot; an older backfill is preserved;
latest-at-or-before returns the correct historical price; partial responses
handled safely; malformed data rejected and counted; run counters correct;
dry-run persists nothing; CLI exit codes (0/1/2/3) correct; **whole-database
secret sweep**; `http_method` CHECK and repository guard reject non-GET; run
lifecycle records `partially_succeeded` on partial data. All against mocked
transports — no live calls in the suite.

**CLI:** `ingest-odds --sport {mlb,nba}` with `--markets`, `--regions`,
`--bookmakers`, `--commence-from`, `--commence-to`, `--db`, `--dry-run`.

**Delivered:** an `ingest-odds` run against a mocked transport produces a fully
traceable corpus slice, twice, with idempotent snapshot counts. Live MLB and NBA
Odds API checks completed safely (MLB: 14 events → 590 price snapshots; NBA:
out-of-season → 0 events, reported as a clean zero not a failure). No API key
appears in any stored column or any output.

#### Deviations from the original Phase B sketch

| # | Change | Why |
| --- | --- | --- |
| 1 | Migrations are `b004`/`b005`, not `b001`/`b002` | Migration numbers are a single global sequence (§3.1); `b001` would parse to version 1 and collide with `a001`. Phase A ended at 003, so Phase B continues at 004. |
| 2 | `game_status_history.raw_response_id` gains its FK but stays **nullable**; `raw_response_hash` stays nullable | The sketch tightened both to `NOT NULL` here. Phase A's `record_status()` creates status rows from schedule data with no owning provider response, and the official-provider ingestion that would supply one is Phase D. A column made `NOT NULL` before it has a producer is filled with an invented value — worse than an honest NULL. The FK still rejects a *dangling* pointer. |
| 3 | `InMemory*` repositories were **not** built | They still have no consumer: the ingestor writes through the SQLite repositories against a temporary database in tests, exactly as Phase A does, and an in-memory repository cannot reproduce SQLite's constraint/trigger semantics — an unverified reimplementation gives false test confidence. Deferred until something actually needs one. |
| 4 | `raw_responses` is **not** deduplicated on `content_hash` | Two fetches returning identical bytes are two distinct observations, each owned by its own run; collapsing them would leave the second run unable to name the response it received. `content_hash` is indexed for traceability, but idempotency is enforced where it matters — on the derived price snapshots (transition-aware `UNIQUE (sb_outcome_id, observed_at, content_hash)`, migration `b006`). |
| 5 | Added `RawExchange` + `fetch_odds_raw()` to the existing adapter | The ingestor needs the raw bytes *and* the exchange metadata before parsing, and must survive one malformed record. `get_odds()` normalizes eagerly and can raise mid-parse, so a raw fetch that normalizes per-event downstream is the required feature — added additively to the one client, never a second one. |
| 6 | Outcome identity key is `(market, normalized_name, point_key)` with a NOT NULL `point_key` sentinel | The line is part of the identity ("Over 8.5" ≠ "Over 9.5"); a nullable point would let an h2h outcome insert twice, since SQLite treats two NULLs as distinct in a UNIQUE constraint. |
| 7 | `implied_probability` is stored (raw, vig-inclusive) | An exact arithmetic transform of the preserved American price, stored for convenience. **No de-vigging** — fair value is a later phase. The original price is preserved exactly regardless. |

#### Phase B integrity repair (migration `b006`)

Four correctness defects were found after the initial Phase B landing and
repaired before Phase C. Migrations `b004`/`b005` are immutable; the schema
change is the additive migration `b006`, the rest are repository/ingestor fixes.

| # | Root cause | Fix |
| --- | --- | --- |
| 1 | **Team names were defaulted to `''`.** `_ingest_event` stored `event.home_team or ""`, so a blank or missing team produced a well-formed-looking row no matcher could ever resolve. | `_validate_event` now rejects a missing/blank home team, a missing/blank away team, and two teams that normalize identically. The event is counted as rejected and the run continues; no empty string is ever stored. |
| 2 | **Response sport was not checked against the request.** A `basketball_nba` payload returned to the `baseball_mlb` endpoint would have been stored under the MLB league. | `_validate_event` takes the requested endpoint's `expected_sport_key` and rejects any event whose `sport_key` differs, so a mismatched payload is counted, not persisted. |
| 3 | **Stale backfill regressed current metadata.** The event/market upserts took `max(last_observed_at, observed_at)` but overwrote the metadata columns unconditionally, so an older backfill rewound the current commence time, team text, and provider update times. | The upserts now refresh mutable current-state **only when `observed_at` is strictly newer** than the stored `last_observed_at`; older-or-equal observations leave current metadata untouched. Equal timestamps retain the earlier-recorded value — deterministic under ordered replay. Backfilled *snapshots* are still preserved. |
| 4 | **A price reversal was silently dropped.** `UNIQUE (sb_outcome_id, content_hash)` excluded `observed_at`, so `-110 → -120 → -110` with missing provider timestamps discarded the third observation (identical hash to the first) — a lost transition. | Migration `b006` rebuilds the table with `UNIQUE (sb_outcome_id, observed_at, content_hash)`, and `append_price_snapshot` collapses an observation only when it matches its **immediate temporal predecessor**. Exact replay and repeated backfill stay idempotent; a reversal appends. See `DATA_ARCHITECTURE.md` §3.6.1. |
| 5 | **Failed HTTP requests counted as zero.** `requests_made` was set to 1 only after a *successful* fetch, so an `OddsApiHTTPError` run recorded `requests_made = 0`. | A completed 4xx/5xx round-trip now records `requests_made = 1` (and preserves the error body); a failure before any response arrives stays `0`. |

---

### Phase C — Kalshi public events, markets, order books, trades ✅ COMPLETE

**Depended on:** B (complete).

**Created:** `sports_quant/db/migrations/c007_kalshi.sql`;
`sports_quant/db/repositories/kalshi.py`;
`sports_quant/ingest/kalshi_ingestor.py`;
`sports_quant/providers/raw_exchange.py` (shared exchange capture, extracted
from the Odds API adapter so Kalshi reuses it — one capture, no duplication);
`sports_quant/ingest/tests/{test_kalshi_ingestor,test_kalshi_safety}.py`;
`sports_quant/db/tests/{test_kalshi_repositories,test_kalshi_schema}.py`;
`sports_quant/tests/test_kalshi_ingest_cli.py`.

**Modified:** `sports_quant/cli.py` (`ingest-kalshi`);
`sports_quant/providers/kalshi.py` (captured GET methods returning `RawExchange`
— the same GET requests through the same policy-wrapped transport, no second
client, no credential); `sports_quant/providers/odds_api.py` (uses the shared
`raw_exchange` module); `sports_quant/db/{schema,ids,models}.py`;
`sports_quant/db/repositories/__init__.py`; `sports_quant/ingest/__init__.py`.

**Tables:** `kalshi_events`, `kalshi_markets`, `kalshi_orderbook_snapshots`,
`kalshi_orderbook_levels`, `kalshi_public_trades`. (Order-book *levels* are a
separate table from the snapshot metadata, so the full ladder is preserved
row-per-level rather than as an opaque JSON blob.)

**Repositories:** `SqliteKalshiRepository` — a `Protocol` plus a SQLite
implementation, reusing the raw-response and ingestion-run repositories, the
transaction handling, timestamp normalization, and content hashing.

**Tests delivered:** migration applies once and idempotently; existing A/B
migrations still valid; only approved public GET paths reachable; POST/PUT/
PATCH/DELETE and account/portfolio/order/fill paths blocked (existing policy
tests, unweakened); **no authentication header sent** and **no private key
loaded** (behavioural + source scan); event/market normalization; pagination;
newer metadata becomes current and older backfills do not regress; Yes/No bids
preserved; derived Yes ask = `100 − best No bid` and vice versa; empty and
one-sided books; complete ladders preserved; identical consecutive books
deduplicate; changed books append; a book returning to a prior state is
preserved; older backfills preserved and exact replays idempotent;
latest-at-or-before returns the correct snapshot; equal timestamps tie-break
deterministically; public trades normalize and dedupe; legitimate repeated
trades remain representable; malformed records rejected/recorded; partial
ingestion finalized correctly; failed runs finalized as `failed`; dry-run
persists nothing; **no account-scoped column exists in the Kalshi schema**
(asserted against `PRAGMA table_info`); the gateway is never imported. All
against mocked transports — no live calls in the suite.

**CLI:** `ingest-kalshi --status {…} [--event-ticker] [--market-ticker]
[--limit] [--include-orderbooks] [--include-trades] [--max-pages] [--db]
[--dry-run]`.

**Delivered:** a mocked Kalshi sweep persists events, markets, order books (with
ladder levels), and public trades, with derived asks matching `KalshiOrderBook`
exactly, twice, idempotently. Live public ingestion completed safely (5 events,
8 markets, 3 empty order books; the public trades feed returned no trades for the
sampled open markets — preserved as an honest empty response, not fabricated).
No credential was required and every request was a GET.

#### Implementation decisions (Phase C)

| # | Decision | Why |
| --- | --- | --- |
| 1 | Migration is `c007_kalshi`, not `c001_kalshi` | Migration numbers are a single global sequence (§3.1); Phase B ended at 006, so Phase C is 007. |
| 2 | Order-book **levels** are their own table (`kalshi_orderbook_levels`), keyed `UNIQUE (snapshot_id, side, price)` | The requirement asks to preserve every level with side/price/quantity/ordering and to reject duplicate price levels with conflicting quantities. A per-level table makes both a schema fact rather than JSON-parsing logic. |
| 3 | Order-book history is **transition-aware** (`UNIQUE (market_ticker, observed_at, content_hash)` + immediate-predecessor comparison), mirroring the `b006` price fix | A book that returns to an earlier state (A→B→A) must be representable; a global content-hash key would drop the third state. |
| 4 | Trade identity is `(market_ticker, content_hash)`, where the hash uses the provider `trade_id` when present, else a documented field-based identity | No trade id is ever invented; exact replays collapse while genuinely different trades (different id/time/price/side) coexist. |
| 5 | `derived_yes_ask` / `derived_no_ask` are **stored**, computed `100 − opposing best bid` | A returned bid is never read as an ask; the derivation matches `KalshiOrderBook.executable_*_ask` exactly. |
| 6 | Provider = `kalshi_public` | Makes it explicit in the corpus that this is the unauthenticated public surface, distinct from any future authenticated feed (which is out of scope permanently). |
| 7 | `--limit` (default 20) bounds events, markets, **and** book/trade fan-out | Never sweeps every book on the exchange by default; truncation is reported, not silent. |
| 8 | Dry-run persists **nothing** (no audit rows either) | Simplest guarantee to reason about and test; a dry run is a pure read. Pinned by `test_dry_run_persists_nothing`. |
| 9 | `game_id` columns exist (nullable) but are **never set** in Phase C | Canonical game matching is Phase D; the column is created now so Phase D needs no table rebuild, but no fuzzy matching happens here. |

**Deferred to Phase D (documented):** matching Kalshi markets to canonical
MLB/NBA games (`game_id` / `match_decision_id`), `rules_hash`-change detection
across an accepted match, and series/ticker sports classification. Phase C stores
`rules_hash` but does not yet act on a change, because there is no accepted match
to invalidate until Phase D.

#### Phase C integrity repair (migration `c008`)

Five correctness issues were found after the initial Phase C landing and repaired
before Phase D. `c007` is immutable; the schema change is the additive migration
`c008_kalshi_metadata_integrity`, the rest are repository/ingestor fixes.

| # | Root cause | Fix |
| --- | --- | --- |
| 1 | **Current metadata was untraceable.** `kalshi_events`/`kalshi_markets` are mutable current-state, but their single `raw_response_id` was frozen by the identity trigger, so after an update it still pointed at the *creating* response, not the one that supplied the current values. | `c008` splits it into `first_raw_response_id` (immutable), `current_raw_response_id`, and `current_raw_response_hash`. The current pointers move only when a strictly-newer observation becomes current; a stale/equal backfill leaves them untouched. The ambiguous `raw_response_id` column is dropped. A query proves current metadata joins to its current raw response. |
| 2 | **Event metadata was loosely validated.** `bool(value)` was applied to `mutually_exclusive`, so the string `"false"` became `True`; a non-string status was accepted. | `validate_event` rejects a blank ticker, a supplied non-string/blank status, and a supplied `mutually_exclusive` that is not an actual Boolean. Rejected events are counted and their raw response preserved; valid events still process. |
| 3 | **Malformed timestamps were silently nulled.** A supplied-but-malformed `open_time`/`close_time`/… was collapsed to `None`, indistinguishable from a missing field. | `validate_market` distinguishes absent (→ `None`), supplied-and-valid (→ normalized), and supplied-but-malformed (→ reject). It also rejects a close before open and a settlement before close, and honours `expected_expiration_time` as a fallback. |
| 4 | **Anonymous trade identity was weak.** A trade without a provider id could be persisted on a field-identity that included no timestamp, and `observed_at` (our clock) risked leaking into identity. | `validate_trade` uses the provider `trade_id` when present; otherwise it requires a valid provider timestamp, at least one valid price, and a positive count, and derives identity from provider fields only. A trade with neither an id nor enough valid fields is rejected. `observed_at` is never part of identity. |
| 5 | **Updates were counted as inserts.** Every valid event/market incremented `records_inserted` even when the row already existed. | The event/market upserts return an explicit `UpsertOutcome` (`INSERTED` / `UPDATED` / `UNCHANGED`); the ingestor counts a refresh as `records_updated` (new column via `c008`), a no-op backfill as `records_deduplicated`, and only a genuinely new row as `records_inserted`. Every valid observation still counts as normalized. |

Additionally, the **dry-run path now calls the same `validate_event` /
`validate_market` / `validate_trade` / `validate_orderbook` helpers** as
persisted ingestion, so it reports identical rejections while still persisting
nothing — there is one validation rule set, not two.

**Counter semantics (precise).** A strictly-newer observation of an existing
entity is an `UPDATED` (it advances `last_observed_at` and the current-provenance
pointers), counted in `records_updated`. An older-or-equal observation is
`UNCHANGED`, counted in `records_deduplicated`. A routine re-poll therefore
counts as an update, because it genuinely moves current provenance forward — it
is never miscounted as a new insert.

---

### Phase D — Official providers, weather, and canonical matching

> **Provider selection + implementation design complete; D1 provider
> infrastructure and D2 MLB ingestion code complete (schema v11), D3–D5 not
> started. No large historical backfill performed; live MLB access still requires
> an approved provider audit and smoke test.** The authoritative, up-to-date
> Phase D plan lives in two dedicated documents —
> `PHASE_D_PROVIDER_DECISIONS.md` (provider evaluation, selection, credentials,
> cost/coverage, licensing risk) and `PHASE_D_IMPLEMENTATION_PLAN.md` (schema,
> migrations `d009`–`d013`, correction behaviour, matching, PIT rules, CLI, and
> the D1–D5 staging). The sketch below is superseded by those documents.

**Depends on:** C. The largest phase; the only one adding network hosts.

**Selected providers:** MLB StatsAPI (no key, risk-labelled) for all MLB official
data + venues; **BALLDONTLIE at the GOAT tier** (`NBA_DATA_API_KEY`, paid) for NBA
teams/players/games/results/player-statistics/box-scores/advanced/injuries/plays/
lineups-when-available — the free tier supplies **none** of the statistics/box/
injuries/plays/lineups and is insufficient; **hoopR** as an **offline-only**
historical NBA supplement (PBP/possessions/substitutions/lineup-stints via a typed
Parquet import boundary, no R at runtime); **NWS** (no key, US) primary weather +
**Open-Meteo** (no key) secondary and leakage-free historical-forecast; the
official NBA injury-report PDF as an **optional cross-check** (not the primary
feed); SportsDataIO Discovery Lab as an **optional delayed comparison** source;
Chadwick register for the MLB player-id crosswalk. **Professional path:**
Sportradar / SportsDataIO commercial / Stats Perform (paid, betting-licensed).
`stats.nba.com` is **not** selected. The Odds API key supplies sportsbook pricing
only; the Kalshi public API supplies prediction-market data only — neither
supplies official statistics.

**New credential:** `NBA_DATA_API_KEY` is a **BALLDONTLIE** key; endpoint access
depends on the **account tier**, and the Phase D NBA path expects **GOAT** (a key
alone does not grant GOAT). Every other selected provider is key-less
(MLB StatsAPI, NWS, Open-Meteo). Optional paid keys (`WEATHER_API_KEY`,
`SPORTRADAR_*`) and pinned base URLs (`MLB_STATS_API_BASE_URL`, `NWS_BASE_URL`,
`OPEN_METEO_BASE_URL`) are in `.env.example` as blank/default placeholders.

**Provider capabilities are typed and declared, not inferred** from a provider
name or key possession; a tier limitation is reported as "capability unavailable
for current subscription tier", never an invalid key or bug. A `provider-audit`
command verifies auth/tier/capabilities before any large backfill.

**Migrations:** `d009_provider_infra` (v9 — references, venues, match
decisions/candidates, data-quality, provider_capabilities) and
`d010_provider_audit_integrity` (v10 — the D1 audit-integrity repair: evidence
columns on `provider_capabilities`, the partial unique index pinning a provider
venue id to one canonical venue, and `data_quality_issues` immutability), and
`d011_official_games_stats` (v11 — D2's nine append-only official MLB observation
tables) are applied. Planned next: `d012_nba_specifics` (v12 — quarter lines,
injuries, plays), `d013_weather` (v13). Migration numbers are a single global
forward-only sequence. No second canonical-game
table: official ids attach to the existing
`games.official_provider`/`official_game_key` and a `provider_game_references`
crosswalk.

**Staging:** D1 provider infrastructure → D2 MLB → D3 NBA → D4 weather → D5
matching. Per-stage files, tables, repositories, CLI, tests, completion criteria,
and expected blockers are enumerated in `PHASE_D_IMPLEMENTATION_PLAN.md` §9.

**CLI (planned):** `ingest-mlb`, `ingest-nba`, `ingest-injuries`,
`ingest-lineups`, `ingest-weather`, `ingest-venues`, `match-games`,
`match-markets`, `matching-review`.

**Done when:** all four ingestion lanes plus matching pass against mocked
transports with append-only history, full raw-response traceability, GET-only
networking, no credential in any stored column, and every `ENTITY_MATCHING.md`
§4.3 hard case behaving as specified.

---

### Phase E — Point-in-time builder, quality rules, leakage tests

**Depends on:** D.

**Create:** `sports_quant/pit/{__init__,asof,dataset,evaluation_only}.py`;
`sports_quant/quality/{__init__,rules,report}.py`;
`sports_quant/db/migrations/e001_data_quality.sql`;
`sports_quant/db/repositories/data_quality.py`;
`sports_quant/pit/tests/{test_asof,test_dataset,test_leakage,test_determinism}.py`;
`sports_quant/quality/tests/test_rules.py`.

**Modify:** `sports_quant/cli.py` (`data-status`, `data-quality`).

**Tables:** `data_quality_issues`; reads everything else.

**Repositories:** `DataQualityRepository`.

**Tests:** every `DQ-PIT-001`…`DQ-PIT-010` guard with an **adversarial fixture
that plants the specific leak**; as-of correctness under random cutoffs;
byte-identical rebuilds; joined-table registry enforcement;
`pit/dataset.py` does not import `evaluation_only`; emitted `GameStateDataset`
satisfies `chronological_split()`.

**CLI:** `data-status`, `data-quality`.

**Done when:** a fixture corpus yields a reproducible `GameStateDataset`, every
planted leak is caught, and `data-quality` grades the corpus using the existing
A–F vocabulary.

**Explicitly not in Phase E:** feature engineering. Phase E delivers rows,
cutoffs, labels, and the safety proof. Populating `X` is a later stage.

---

## 5.1 The stale-backfill rule

> **Current game state always reflects the newest observation, never the most
> recently written one.**

`games.status` and `games.scheduled_start` are recomputed after every history
insert from:

```sql
SELECT status, scheduled_start FROM game_status_history
WHERE game_id = ?
ORDER BY observed_at DESC, status_id DESC
LIMIT 1
```

Consequences, all test-pinned:

| Situation | Behaviour |
| --- | --- |
| Newer observation arrives | Becomes current state |
| Older observation backfilled | Stored in history; current state unchanged |
| Observations arrive out of order | Current state converges to the newest regardless of arrival order |
| Two observations share `observed_at` | `status_id` (a monotonic ULID) breaks the tie, so the most recently recorded wins and a rebuild agrees |
| `original_start` | Never changed — now a database rule, not a convention |

The tie-break matters more than it looks. Without a deterministic second key,
two observations sharing a timestamp would resolve arbitrarily, and a rebuilt
corpus could disagree with the original about a game's current state.

## 5.2 Status deduplication: the decision

**Decision: the deduplication design was changed** (migration `a003` plus a
repository change), rather than documenting the old behaviour as intentional.

**The problem.** Migration `a002` declared
`UNIQUE (game_id, provider, content_hash)`, where `content_hash` covers
`(status, scheduled_start, detail, provider_timestamp)` and deliberately
excludes `observed_at`. That deduplicates *states* globally: a state can be
recorded once per game per provider, ever.

Baseball breaks that immediately. A rain delay that resumes and re-delays gives
`delayed -> in_progress -> delayed`. The third observation hashes identically to
the first, so `INSERT OR IGNORE` silently discarded it. Reproduced before the
fix:

```
record delayed      -> inserted=True
record in_progress  -> inserted=True
record delayed      -> inserted=False     # silently lost
history: ['delayed', 'in_progress']
current status: in_progress               # WRONG - the game is delayed
```

The corpus lost a real transition *and* the current state was left wrong. With a
missing `provider_timestamp` — common, and the case the requirement calls out —
nothing else distinguished the two observations. This is a correctness defect,
not a tuning preference, so redesign was warranted.

**The fix.** Two coordinated changes:

- **Database** (`a003`): uniqueness becomes
  `UNIQUE (game_id, provider, observed_at, content_hash)`. Adding `observed_at`
  means the same state at a *different* observation time is storable, while an
  exact duplicate observation is still rejected. SQLite cannot drop an inline
  `UNIQUE`, so the table is rebuilt — data is copied, and the append-only
  triggers are recreated.
- **Repository**: an observation is skipped only when it is unchanged from the
  one **immediately preceding it in time from the same provider**, not from the
  whole history.

**The resulting semantics**, which are what the corpus now promises:

> `game_status_history` stores **state transitions per (game, provider)**. An
> observation is appended when it differs from its temporal predecessor;
> unchanged re-polls collapse.

| Case | Result |
| --- | --- |
| Poll every 30s, nothing changed | One row |
| `delayed -> in_progress -> delayed` | Three rows |
| Exact observation replayed | No new row (idempotent) |
| Older observation backfilled that already exists | No new row (idempotent) |
| Two providers reporting the same state | Two rows — dedup is per-provider |

Comparing against the predecessor rather than the newest row is what makes
backfill idempotent: a replayed old observation is compared against its own
temporal neighbour, so it is recognised as unchanged even when the newest state
differs from it.

## 6. Unresolved design risks

Open questions requiring a decision or acceptance before or during the phase
noted. None blocks Phase A.

| # | Risk | Impact | Proposed resolution | Phase |
| --- | --- | --- | --- | --- |
| 1 | **The Odds API has no historical endpoint on the standard plan.** Odds history can only be accumulated forward from first ingestion. | No pre-existing odds history. A price-based model cannot be backtested until months of capture exist. | Accept and start capturing immediately; treat odds history as a growing asset. Evaluate the paid historical endpoint separately. **This is the single largest schedule risk in the project.** | B |
| 2 | **Kalshi order-book history is not retrievable.** The public API serves current state only. | Book history is capture-forward only, and polling frequency permanently bounds granularity. | Decide a polling cadence in Phase C and record it in `ingestion_runs`, so dataset builders know the true resolution rather than assuming continuity. | C |
| 3 | **Official provider terms.** MLB StatsAPI and NBA endpoints are undocumented/unofficial for programmatic use. | Legal and availability exposure. | Confirm acceptable use before Phase D. Keep both behind adapters so a paid licensed feed is a swap, not a rewrite. | D |
| 4 | **Weather provider unchosen.** | `weather_snapshots` has no producer. | Choose in Phase D. Schema is provider-agnostic; forecast-vs-actual is already modeled. | D |
| 5 | **Historical alias coverage.** Seeds cover current teams; older seasons need historical names. | Silent `UNMATCHED` for older data. | Season-scope aliases from the start; backfill as history is ingested. `DQ-MATCH-001/002` make gaps visible. | D |
| 6 | **SQLite single-writer.** Concurrent ingestions serialize. | Fine now; a ceiling later. | Accept. WAL + `busy_timeout`; repositories are `Protocol`s, so PostgreSQL is additive. | — |
| 7 | **Corpus growth.** Frequent order-book polling grows the file quickly. | Operational. | Measure in Phase C. Options: compress `body`, or move raw bodies to content-addressed files with hashes retained in-row. Do not optimize before measuring. | C |
| 8 | **Venue timezone table needed.** Local-date resolution requires per-venue timezones. | Doubleheader and date-boundary matching correctness. | Seed venue timezones in Phase D; treat a missing venue as a matching refusal, not a UTC fallback. | D |
| 9 | **Backfill semantics.** Re-parsing old raw responses into new tables gives `observed_at` ≪ `ingested_at`. | Confusing if conflated. | Already modeled (`POINT_IN_TIME_DATA.md` §2). Requires discipline, not schema change. | E |

---

## 7. Verification

Run after every phase, and after this documentation change:

```
.\venv\Scripts\python.exe -m ruff check .
.\venv\Scripts\python.exe -m mypy .
.\venv\Scripts\python.exe -m pytest -q
```

Standing gates for every phase:

- Ruff: clean.
- mypy: **zero project-source errors**, no global `ignore_missing_imports`, no
  broad `type: ignore`, no `Any` substitutions for meaningful types.
- pytest: zero failures; each phase adds its own tests.
- No live network calls in the test suite; providers are mocked.
- No credential in any output, log, or stored column.
- `providers-check` continues to pass, GET-only.
- Execution remains quarantined.
