# Data Foundation Plan

Master plan for the real historical data foundation of the read-only MLB/NBA
betting **recommendation** engine.

This is an architecture and planning document. **No implementation has been
started.** Phase A begins only on explicit instruction.

Companion documents:

- `DATA_ARCHITECTURE.md` — engine choice, canonical IDs, full schema, raw-response contract
- `POINT_IN_TIME_DATA.md` — timestamp semantics, leakage prevention
- `ENTITY_MATCHING.md` — normalization, aliases, game/market matching

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
- **Idempotency:** `UNIQUE (sb_outcome_id, content_hash)` + `INSERT OR IGNORE`.
  Re-running with unchanged prices writes zero snapshot rows. Raw responses are
  deduplicated on `content_hash`.
- **Failure:** the raw response is persisted **before** parsing, so a parse
  failure never loses the bytes — it marks the run `partial`, records a
  `data_quality_issues` row, and exits `1`. The corpus keeps the response for a
  later re-parse.

### `ingest-kalshi`

```
python -m sports_quant ingest-kalshi [--series TICKER] [--league {mlb,nba}]
                                     [--with-orderbooks] [--with-trades]
                                     [--max-markets N] [--db PATH] [--dry-run]
```

Fetches Kalshi **public** events, markets, and optionally order books and public
trade prints via the existing `KalshiClient`.

- **Output:** run id, events/markets seen, order-book and trade snapshots
  written, duplicates skipped, unmatched markets.
- **Exit `0`:** success, including "exchange closed" (a legitimate skip).
- **Exit `1`:** a genuine fetch or write failure.
- **Idempotency:** as above, on `content_hash`.
- **Safety:** only the public GET surface. `--max-markets` bounds order-book
  fan-out; when it truncates, the count is **logged explicitly** rather than
  silently capped, so a partial sweep is never mistaken for a complete one.

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

### Phase A — Database engine, migrations, core entities

**Depends on:** nothing.

**Create:** `sports_quant/db/{__init__,engine,ids,schema,hashing}.py`;
`sports_quant/db/migrations/{a001_core_entities,a002_games}.sql`;
`sports_quant/db/repositories/{__init__,base,leagues,seasons,teams,players,games}.py`;
`sports_quant/db/seeds/{mlb_teams,nba_teams}.py`;
`sports_quant/db/tests/{test_engine,test_migrations,test_ids,test_repositories,test_isolation}.py`.

**Modify:** `sports_quant/config.py` (add `DATABASE_PATH`), `.env.example`,
`sports_quant/cli.py` (register `db-init`), `pyproject.toml` (package list).

**Tables:** `schema_versions`, `leagues`, `seasons`, `teams`, `team_aliases`,
`players`, `player_aliases`, `games`, `game_status_history`.

**Repositories:** `LeagueRepository`, `SeasonRepository`, `TeamRepository`,
`PlayerRepository`, `GameRepository` — each a `Protocol` with `Sqlite*` and
`InMemory*` implementations, per `tracking/base.py`.

**Tests:** migrations apply in order and are idempotent; checksum mismatch
raises; `PRAGMA foreign_keys` is ON; ULIDs are prefixed, sortable, unique;
append-only triggers reject UPDATE/DELETE; CRUD round-trips; **isolation test —
no module in `probability/`, `state/`, `evaluation/`, or `gateway/` imports
`sports_quant.db`**.

**CLI:** `db-init`.

**Done when:** `db-init` on an empty directory produces a migrated, seeded
database; re-running is a no-op; all three gates green.

---

### Phase B — Raw responses, ingestion runs, sportsbook odds

**Depends on:** A.

**Create:** `sports_quant/db/migrations/{b001_raw_responses,b002_sportsbook}.sql`;
`sports_quant/db/repositories/{raw_responses,ingestion_runs,sportsbook}.py`;
`sports_quant/ingest/{__init__,runner,odds_ingestor}.py`;
`sports_quant/ingest/tests/{test_runner,test_odds_ingestor,test_no_secrets}.py`.

**Modify:** `sports_quant/cli.py` (`ingest-odds`); `intel/base.py` (adopt shared
`canonical_json`).

**Tables:** `ingestion_runs`, `raw_responses`, `sportsbook_events`,
`sportsbook_markets`, `sportsbook_outcomes`, `sportsbook_price_snapshots`.

**Repositories:** `RawResponseRepository`, `IngestionRunRepository`,
`SportsbookRepository`.

**Tests:** raw response persisted before parsing; every normalized row resolves
to a raw response; re-ingestion is a no-op; append-only enforced;
**whole-database secret sweep** (every TEXT column of every table scanned for a
sentinel key); headers stored via allow-list; `http_method` CHECK rejects
non-GET; run lifecycle records `partial` on parse failure. All against mocked
transports — no live calls in the suite.

**CLI:** `ingest-odds --sport {mlb,nba}`.

**Done when:** an `ingest-odds` run against a mocked transport produces a fully
traceable corpus slice, twice, with identical row counts.

---

### Phase C — Kalshi public events, markets, order books, trades

**Depends on:** B.

**Create:** `sports_quant/db/migrations/c001_kalshi.sql`;
`sports_quant/db/repositories/kalshi.py`;
`sports_quant/ingest/kalshi_ingestor.py`;
`sports_quant/ingest/tests/test_kalshi_ingestor.py`.

**Modify:** `sports_quant/cli.py` (`ingest-kalshi`).

**Tables:** `kalshi_events`, `kalshi_markets`, `kalshi_orderbook_snapshots`,
`kalshi_trade_snapshots`.

**Repositories:** `KalshiRepository`.

**Tests:** derived asks equal `100 − opposing best bid` and match
`KalshiOrderBook`'s existing derivation; full ladders preserved; empty book
yields NULL asks; price CHECKs reject out-of-range cents; `rules_hash` changes
detected; `--max-markets` truncation is reported; **no account-scoped column
exists in the Kalshi schema** (asserted against `PRAGMA table_info`);
idempotent re-ingestion.

**CLI:** `ingest-kalshi`.

**Done when:** a mocked Kalshi sweep persists events, markets, books, and public
trades with derived asks matching the provider client exactly.

---

### Phase D — Official providers and canonical matching

**Depends on:** C. The largest phase; the only one adding a network host.

**Create:**
`sports_quant/providers/{mlb_official,nba_official}.py` (new clients, same
policy-wrapped pattern);
`sports_quant/matching/{__init__,normalize,teams,players,games,markets}.py`;
`sports_quant/db/migrations/d001_matching.sql`;
`sports_quant/db/repositories/matching.py`;
`sports_quant/matching/tests/{test_normalize,test_teams,test_players,test_games,test_markets,test_determinism}.py`.

**Modify:** `sports_quant/http_policy.py` (allow-list official hosts, GET-only);
`intel/player_matching.py` (back with `player_aliases`, API unchanged);
`sports_quant/cli.py` (`data-quality --review`).

**Tables:** `entity_match_decisions`; populates `games`, `game_status_history`;
sets `game_id` / `match_decision_id` on sportsbook and Kalshi rows.

**Repositories:** `MatchDecisionRepository`.

**Tests:** normalization golden file; determinism under 100 shuffled candidate
orderings; ambiguity refusal (two Jalen Williamses, bare `NY`, same-time
doubleheader); every §4.3 hard case; Kalshi title/rules disagreement rejected;
every matcher call writes exactly one decision row; new hosts remain GET-only
and account paths stay blocked.

**CLI:** `data-quality --review`.

**Done when:** a fixture slate matches end-to-end with every decision recorded,
and every hard case behaves as specified in `ENTITY_MATCHING.md` §4.3.

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
