# Data Architecture

Storage design for the historical data foundation of the read-only MLB/NBA
betting **recommendation** engine.

This document defines the database engine choice, the canonical identifier
scheme, every table, and the raw-response preservation contract. Point-in-time
semantics are specified in `POINT_IN_TIME_DATA.md`; matching is specified in
`ENTITY_MATCHING.md`; sequencing is in `DATA_FOUNDATION_PLAN.md`.

Nothing here places, cancels, or manages a bet. No authenticated Kalshi
endpoint is used. No model is trained.

---

## 1. Engine choice: stdlib `sqlite3`

**Decision: SQLite via the standard library `sqlite3` module. No ORM, no new
dependency.**

The repository was inspected for an existing database abstraction worth
reusing. What exists:

| Location | What it is | Verdict |
| --- | --- | --- |
| `streaming/deduplicator.py` → `SqliteDedupStore` | stdlib `sqlite3`, WAL mode, `INSERT OR IGNORE` | **Precedent to follow.** Same engine, same idiom. |
| `tracking/base.py` → `ManifestRepository` Protocol + `InMemoryManifestRepository` + `PostgresManifestRepository` | Repository-per-entity with a Protocol and a lazily-imported driver | **Pattern to follow.** Not a general abstraction to reuse. |
| `streaming/replay.py` → `RawEventStore` / `JsonlRawEventStore` | Append-only JSONL log for *live stream* envelopes | Different concern (live replay, not historical corpus). Leave untouched. |
| `sports_quant/providers/cache.py` → `ResponseCache` | In-memory TTL credit-saver | Complementary, not storage. Leave untouched. |

There is **no general database abstraction** in the repository, so one must be
built. SQLite is chosen because:

- The repo already depends on stdlib `sqlite3` and adds no dependency for it.
- `pyproject.toml` deliberately keeps runtime dependencies small; SQLAlchemy
  would be the single largest addition to the project for no present gain.
- A single-file database is trivially snapshot-able, which matters enormously
  for point-in-time work: an entire corpus state can be copied and diffed.
- The corpus is append-heavy, single-writer, read-mostly — SQLite's sweet spot.

**Migration path.** Every repository is defined as a `Protocol` first (following
`tracking/base.py`). Swapping SQLite for PostgreSQL later means adding
`Postgres*Repository` classes, not rewriting callers. The SQL avoids
SQLite-only syntax where a portable form exists.

### 1.1 Connection settings (mandatory)

```sql
PRAGMA journal_mode = WAL;        -- concurrent readers during ingestion
PRAGMA foreign_keys = ON;         -- OFF by default in SQLite; must be set per connection
PRAGMA synchronous = FULL;        -- a raw response on disk must survive a crash
PRAGMA busy_timeout = 5000;
```

`foreign_keys = ON` is not the SQLite default and is silently ignored if set
inside a transaction. It must be issued on every new connection, and a test must
assert it is on.

### 1.2 Hot-path isolation (hard rule)

`CLAUDE.md` forbids querying a database on the hot decision path.
`probability/tests/test_probability.py::test_no_pandas_or_database_imports_in_package`
already enforces this for `probability/` by scanning for the literal token
`sqlite3`.

**The new database package must be subject to the same rule.** Phase A adds an
equivalent test asserting that no module in `probability/`, `state/`,
`evaluation/`, or `gateway/` imports `sports_quant.db`. The database is a
research-lane and ingestion-lane component only.

### 1.3 Type conventions

SQLite has no native date, boolean, or JSON type. Fixed conventions:

| Logical type | Storage | Convention |
| --- | --- | --- |
| Timestamp | `TEXT` | ISO-8601 UTC, always `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`. Lexicographic order == chronological order. Enforced by `CHECK (ts LIKE '____-__-__T__:__:__%Z')`. |
| Monotonic duration | `INTEGER` | Nanoseconds. Never used as a wall-clock. |
| Date (no time) | `TEXT` | `YYYY-MM-DD`, local to the venue, for doubleheader/schedule keys only. |
| Boolean | `INTEGER` | `0`/`1` with `CHECK (col IN (0,1))`. |
| JSON | `TEXT` | Canonical JSON (sorted keys, tight separators) — see §4.2. |
| Money / price | `INTEGER` | Cents. Never float. Kalshi prices are integer cents in `[1,99]`. |
| Decimal odds | `REAL` | American odds stored as `INTEGER`, decimal as `REAL`; both preserved as given. |

---

## 2. Canonical identifiers

### 2.1 The core rule

> **A provider identifier is never a canonical identifier.**

Every canonical row owns an internal ID. Provider identity lives exclusively in
`provider_ids` / alias tables and in the `provider_*` columns of provider-scoped
tables. This is what allows two providers to disagree about a team's name, or
one provider to renumber its events, without corrupting the corpus.

Consequences enforced by schema:

- No canonical table has a column named `*_provider_id` as its primary key.
- Every provider-scoped table carries **both** `provider` (the source name) and
  the provider's own id, and links to canonical IDs only via an explicit,
  recorded match decision (see `ENTITY_MATCHING.md`).
- A canonical ID is never reused, even after a soft delete.

### 2.2 ID format

Canonical IDs are `TEXT`, prefixed, and human-inspectable — debugging a corpus
by eye is a real activity:

| Entity | Prefix | Form | Kind |
| --- | --- | --- | --- |
| League | `lg_` | `lg_mlb`, `lg_nba` | Deterministic |
| Season | `sn_` | `sn_mlb_2026_regular` | Deterministic |
| Team | `tm_` | `tm_mlb_nyy`, `tm_nba_bos` | Deterministic |
| Player | `pl_` | `pl_` + 26-char ULID | Surrogate |
| Game | `gm_` | `gm_` + 26-char ULID | Surrogate |
| Sportsbook event | `sbe_` | `sbe_` + 26-char ULID | Surrogate |
| Sportsbook market | `sbm_` | `sbm_` + 26-char ULID | Surrogate |
| Kalshi event | `kev_` | `kev_` + 26-char ULID | Surrogate |
| Kalshi market | `kmk_` | `kmk_` + 26-char ULID | Surrogate |
| Raw response | `raw_` | `raw_` + 26-char ULID | Surrogate |
| Ingestion run | `run_` | `run_` + 26-char ULID | Surrogate |
| Match decision | `mtc_` | `mtc_` + 26-char ULID | Surrogate |

**Deterministic vs surrogate — why the split.**

*Deterministic* IDs are derived from a natural key that genuinely never changes
(a league's identity; a franchise slot within a league; a season's year). These
are reproducible: rebuilding the corpus from raw responses yields identical IDs,
which makes corpus diffs meaningful.

A season identifier includes its **phase** (`sn_mlb_2026_regular`,
`sn_mlb_2026_postseason`) because a league runs preseason, regular and
postseason inside one year, and the `seasons` uniqueness key is
`(league_id, year, phase)`. A year-only identifier would collide across phases.

*Surrogate* IDs (ULIDs) are used wherever the natural key can change. Players
get married and change names; games get postponed to a different date;
sportsbook events get renumbered. Deriving an ID from a mutable natural key
means the ID changes when reality changes — silently orphaning every foreign
key. ULIDs are lexicographically sortable by creation time, which keeps index
locality without leaking a mutable fact into the identifier.

**Why `games` is surrogate, specifically.** The obvious key
`(league, date, home, away)` breaks on all four of: postponement (date changes),
doubleheaders (two games share it), neutral-site games (home is ambiguous), and
suspended-and-resumed games. `games.game_id` is therefore a ULID, and the
*official* provider key is stored beside it as a unique column:

```sql
official_provider  TEXT,   -- e.g. 'mlb_statsapi'
official_game_key  TEXT,   -- e.g. StatsAPI gamePk; NBA game id
UNIQUE (official_provider, official_game_key)
```

When an official provider is available (Phase D) this pair is the anchor of
truth and survives reschedules, because official providers keep their game key
stable across a postponement. Until Phase D, games are created from schedule
data with `official_game_key` NULL and matched by the rules in
`ENTITY_MATCHING.md`.

### 2.3 ULID generation

ULIDs are generated in-process from `time.time_ns()` + `secrets.token_bytes`.
No new dependency. A helper lives in `sports_quant/db/ids.py` and is the single
source of ID construction; a test asserts monotonicity within a millisecond and
correct prefixing.

---

## 3. Schema

Notation: `PK` primary key, `FK` foreign key, `NN` not null, `U` unique.
Timestamp column semantics are defined once in `POINT_IN_TIME_DATA.md` §2 and
not repeated per table.

### 3.1 Schema versioning

```sql
CREATE TABLE schema_versions (
    version        INTEGER PRIMARY KEY,          -- monotonic, no gaps
    name           TEXT    NOT NULL,             -- 'a001_core_entities'
    checksum       TEXT    NOT NULL,             -- sha256 of the migration SQL
    applied_at     TEXT    NOT NULL,
    applied_by     TEXT    NOT NULL,             -- tool version, e.g. 'sports_quant 0.1.0'
    execution_ms   INTEGER NOT NULL
);
```

Migrations are forward-only numbered SQL files under
`sports_quant/db/migrations/`. `db-init` applies every unapplied migration in
order inside a single transaction each. `checksum` makes an edited-after-apply
migration a hard startup error rather than a silent divergence — the most
common way a data corpus quietly rots.

There is deliberately no `down` migration. Rolling back a historical corpus is
a restore-from-snapshot operation, not a schema operation.

**Filenames** are `<phase-letter><3 digits>_<slug>.sql`. The digits are a
**single global sequence** and are the version; the phase letter is cosmetic.
Restarting the numbering per phase would make `a001` and `b001` both parse to
version 1 and collide. Applied so far:

| Version | Name | Contents |
| --- | --- | --- |
| 001 | `a001_core_entities` | leagues, seasons, teams, team_aliases, players, player_aliases |
| 002 | `a002_games` | games, game_status_history, append-only triggers |
| 003 | `a003_integrity_guards` | league-consistency triggers, `original_start` immutability, `game_status_history` rebuild (§3.4.1) |
| 004 | `b004_raw_responses` | ingestion_runs, raw_responses (append-only), `game_status_history` rebuilt to add the `raw_response_id` FK |
| 005 | `b005_sportsbook` | sportsbook_events, sportsbook_markets, sportsbook_outcomes, sportsbook_price_snapshots (append-only), identity-immutability triggers |

**Migrations are applied statement-by-statement, not via `executescript`.**
`sqlite3.Cursor.executescript` issues an implicit `COMMIT` before running,
which silently ends the surrounding transaction — a failure partway through
would leave earlier statements committed and the migration half-applied.
`sports_quant.db.engine.split_sql_statements` splits the file instead,
correctly handling SQL string literals (`''` escapes a quote; backslash is *not*
an escape, which matters for `ESCAPE '\'`), line and block comments, and
`CREATE TRIGGER` bodies whose `BEGIN … END;` block contains semicolons.

### 3.2 Reference entities

```sql
CREATE TABLE leagues (
    league_id      TEXT PRIMARY KEY,             -- 'lg_mlb'
    code           TEXT NOT NULL UNIQUE,         -- 'MLB' | 'NBA'
    name           TEXT NOT NULL,
    sport          TEXT NOT NULL,                -- 'baseball' | 'basketball'
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE seasons (
    season_id      TEXT PRIMARY KEY,             -- 'sn_mlb_2026'
    league_id      TEXT NOT NULL REFERENCES leagues(league_id),
    year           INTEGER NOT NULL,             -- NBA 2025-26 -> 2026 (end year)
    label          TEXT NOT NULL,                -- '2025-26'
    phase          TEXT NOT NULL,                -- 'preseason'|'regular'|'postseason'
    start_date     TEXT NOT NULL,
    end_date       TEXT,                         -- NULL while in progress
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (league_id, year, phase)
);

CREATE TABLE teams (
    team_id        TEXT PRIMARY KEY,             -- 'tm_mlb_nyy'
    league_id      TEXT NOT NULL REFERENCES leagues(league_id),
    canonical_name TEXT NOT NULL,                -- 'New York Yankees'
    city           TEXT NOT NULL,                -- 'New York'
    nickname       TEXT NOT NULL,                -- 'Yankees'
    abbreviation   TEXT NOT NULL,                -- 'NYY'
    first_season   INTEGER,                      -- franchise validity window
    last_season    INTEGER,                      -- NULL = active
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (league_id, abbreviation, first_season)
);

CREATE TABLE players (
    player_id      TEXT PRIMARY KEY,             -- 'pl_<ulid>'
    league_id      TEXT NOT NULL REFERENCES leagues(league_id),
    full_name      TEXT NOT NULL,
    first_name     TEXT,
    last_name      TEXT,
    suffix         TEXT,                         -- 'Jr.' | 'III' | NULL, stored separately
    birth_date     TEXT,                         -- strongest disambiguator available
    primary_position TEXT,
    debut_date     TEXT,
    final_game_date TEXT,                        -- NULL = active
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
```

`suffix` is a first-class column, not part of `full_name`. "Ronald Acuña Jr."
and "Ronald Acuña" are the same player; "Ken Griffey Jr." and "Ken Griffey" are
not. Separating the suffix is what makes that distinguishable rather than a
heuristic. See `ENTITY_MATCHING.md` §3.

Teams carry a `(first_season, last_season)` validity window so a historical
name resolves to the franchise that actually bore it in that season.

### 3.3 Aliases

**Implemented in migration `a001_core_entities`.** The authoritative DDL is that
file; the sketch below matches it.

```sql
CREATE TABLE team_aliases (
    alias_id       TEXT PRIMARY KEY,
    team_id        TEXT NOT NULL REFERENCES teams(team_id),
    league_id      TEXT NOT NULL REFERENCES leagues(league_id),
    alias          TEXT NOT NULL,                -- as written by the source
    normalized     TEXT NOT NULL,                -- normalization output (ENTITY_MATCHING §2.1)
    alias_type     TEXT NOT NULL,                -- 'abbreviation'|'city'|'nickname'
                                                 -- |'full'|'historical'|'provider'
    provider          TEXT NOT NULL DEFAULT '',     -- '' = not provider-scoped
    valid_from_season INTEGER NOT NULL DEFAULT 0,   -- 0    = unbounded start
    valid_to_season   INTEGER NOT NULL DEFAULT 9999,-- 9999 = still valid
    is_ambiguous   INTEGER NOT NULL DEFAULT 0 CHECK (is_ambiguous IN (0,1)),
    source         TEXT NOT NULL,                -- 'seed'|'manual'|'provider_observed'
    created_at     TEXT NOT NULL,
    UNIQUE (team_id, normalized, alias_type, provider, valid_from_season)
);

CREATE TABLE player_aliases (
    alias_id       TEXT PRIMARY KEY,
    player_id      TEXT NOT NULL REFERENCES players(player_id),
    league_id      TEXT NOT NULL REFERENCES leagues(league_id),
    alias          TEXT NOT NULL,
    normalized     TEXT NOT NULL,
    suffix         TEXT NOT NULL DEFAULT '',     -- 'jr'/'iii'/...; '' = none
    alias_type     TEXT NOT NULL,                -- 'full'|'short'|'nickname'
                                                 -- |'accent_stripped'|'suffix_variant'|'provider'
    provider       TEXT NOT NULL DEFAULT '',
    is_ambiguous   INTEGER NOT NULL DEFAULT 0 CHECK (is_ambiguous IN (0,1)),
    source         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (player_id, normalized, suffix, alias_type, provider)
);

CREATE INDEX idx_team_aliases_lookup   ON team_aliases   (league_id, normalized);
CREATE INDEX idx_player_aliases_lookup ON player_aliases (league_id, normalized);
```

**Uniqueness is scoped to the entity, not the league.** Two teams in one league
legitimately share an alias — "chicago" belongs to both the Cubs and the White
Sox, "los angeles" to both the Lakers and the Clippers. A league-scoped
constraint would *reject the second team's alias at write time*, which is
exactly backwards: shared aliases are ambiguity to record and refuse at match
time, not writes to forbid. The seed loader derives `is_ambiguous` from the data
after loading (`mark_ambiguous_duplicates`), so ambiguity is computed rather
than hand-maintained.

**`provider`, `valid_from_season` and `valid_to_season` are `NOT NULL` with
sentinels** (`''`, `0`, `9999`) rather than nullable. SQLite treats two `NULL`s
as *distinct* inside a `UNIQUE` constraint, so nullable columns would let an
identical seed row insert again on every `db-init` — silently defeating
idempotency. Sentinels keep the uniqueness check total.

**`player_aliases.suffix` participates in the key** so "Ken Griffey Jr." and
"Ken Griffey Sr." are two aliases of two players rather than a collision.

`is_ambiguous` is the mechanism that stops a silent bad match. When two players
in a league normalize identically (there are two Jalen Williamses; the
`intel/tests/test_intel.py` fixture already encodes exactly this case), both
alias rows are flagged, and the matcher is **required** to refuse a
name-only match and emit an `AMBIGUOUS` decision. Ambiguity is data, not an
exception path.

### 3.4 Games and status history

```sql
CREATE TABLE games (
    game_id            TEXT PRIMARY KEY,         -- 'gm_<ulid>'
    league_id          TEXT NOT NULL REFERENCES leagues(league_id),
    season_id          TEXT NOT NULL REFERENCES seasons(season_id),
    home_team_id       TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id       TEXT NOT NULL REFERENCES teams(team_id),
    scheduled_start    TEXT NOT NULL,            -- current scheduled UTC start
    original_start     TEXT NOT NULL,            -- first scheduled start, never updated
    game_date_local    TEXT NOT NULL,            -- venue-local date; doubleheader key
    game_number        INTEGER NOT NULL DEFAULT 1,  -- 1 or 2 within a doubleheader
    doubleheader_type  TEXT,                     -- NULL|'traditional'|'split'
    venue              TEXT,
    is_neutral_site    INTEGER NOT NULL DEFAULT 0 CHECK (is_neutral_site IN (0,1)),
    status             TEXT NOT NULL,            -- current status (see history below)
    official_provider  TEXT,
    official_game_key  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CHECK (home_team_id <> away_team_id),
    UNIQUE (official_provider, official_game_key)
);

CREATE UNIQUE INDEX idx_games_natural
    ON games (league_id, game_date_local, home_team_id, away_team_id, game_number);

CREATE TABLE game_status_history (
    status_id        TEXT PRIMARY KEY,
    game_id          TEXT NOT NULL REFERENCES games(game_id),
    status           TEXT NOT NULL,              -- 'scheduled'|'pregame'|'in_progress'
                                                 -- |'final'|'postponed'|'suspended'
                                                 -- |'cancelled'|'rescheduled'|'delayed'
    scheduled_start  TEXT NOT NULL,              -- start as believed AT THIS OBSERVATION
    detail           TEXT,                       -- 'rain'|'Game 2 of doubleheader'|...
    provider         TEXT NOT NULL,
    provider_timestamp TEXT,
    observed_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    -- Nullable with no FK in Phase A: `raw_responses` arrives in Phase B, and
    -- Phase A has no ingestion, so nothing can reference one yet. Phase B adds
    -- the foreign key and tightens raw_response_hash to NOT NULL.
    raw_response_id  TEXT,
    raw_response_hash TEXT,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (game_id, provider, content_hash)
);

CREATE INDEX idx_game_status_asof ON game_status_history (game_id, observed_at);
```

**Implemented in migration `a002_games`**, together with the append-only
triggers of §5. `games` additionally carries a partial unique index on
`(official_provider, official_game_key)` — partial so the many rows without an
official key do not collide on `NULL` — and a `CHECK` requiring the provider and
key to be present or absent together, since a key without its issuer is
meaningless.

### 3.4.1 Status-history uniqueness (corrected in `a003`)

`a002` shipped `UNIQUE (game_id, provider, content_hash)`, which deduplicates
**states** globally. `a003` rebuilds the table with:

```sql
CONSTRAINT game_status_history_unique
    UNIQUE (game_id, provider, observed_at, content_hash)
```

The reason: `content_hash` covers the reported state and deliberately excludes
`observed_at`, so a game going `delayed → in_progress → delayed` produced a
third observation that hashed identically to the first and was silently
discarded — losing a real transition and leaving `games.status` reading
`in_progress` while the game was delayed. Ordinary rain delays do this, and the
failure was worst when `provider_timestamp` was absent.

Including `observed_at` means the same state at a different time is storable,
while an exact duplicate observation is still rejected. Deduplication of
unchanged re-polls moved to the repository, which compares an observation
against its **immediate temporal predecessor** from the same provider rather
than against the whole history. The table therefore stores *state transitions
per (game, provider)*. Full analysis in `DATA_FOUNDATION_PLAN.md` §5.2.

SQLite cannot drop an inline `UNIQUE`, so `a003` rebuilds the table: create the
replacement, copy every row, drop the original, rename, then recreate the index
and the append-only triggers. Nothing references `game_status_history` by
foreign key, so no dependent constraint is disturbed. A test applies 001–002
alone, writes a history row, then applies 003 and asserts the row survived.

### 3.4.2 League-consistency guards (`a003`)

A foreign key proves a referenced row **exists**; it cannot prove the row
belongs to the same league. Without a further guard an MLB game can reference an
NBA season or an NBA team, and the database accepts it — the row is
well-formed, nothing surfaces the error, and every downstream join inherits it.

`a003` adds `BEFORE INSERT` and column-scoped `BEFORE UPDATE` triggers:

| Rule | Trigger |
| --- | --- |
| `games.league_id` = league of `games.season_id` | `trg_games_league_consistency_*` |
| `games.league_id` = league of `games.home_team_id` | `trg_games_league_consistency_*` |
| `games.league_id` = league of `games.away_team_id` | `trg_games_league_consistency_*` |
| `team_aliases.league_id` = league of its team | `trg_team_aliases_league_consistency_*` |
| `player_aliases.league_id` = league of its player | `trg_player_aliases_league_consistency_*` |
| `games.original_start` never changes | `trg_games_original_start_immutable` |

The UPDATE triggers are scoped with `BEFORE UPDATE OF <columns>` so an ordinary
status write does not pay for the subqueries. These are database rules, not
repository checks — anything holding a connection can write, so the enforcement
has to live where the data does.

`games.status` and `games.scheduled_start` are **mutable current-state
columns** — a deliberate exception to the append-only rule, and the *only*
place a game's present state lives. Every value they ever held is preserved in
`game_status_history`, which is append-only. A point-in-time query must read
`game_status_history` as of the cutoff and must never read `games.status`
(see `POINT_IN_TIME_DATA.md` §4). `games.original_start` is written once and
never updated, so "was this game moved?" is answerable without a scan.

### 3.5 Ingestion and raw responses

```sql
CREATE TABLE ingestion_runs (
    run_id           TEXT PRIMARY KEY,           -- 'run_<ulid>'
    command          TEXT NOT NULL,              -- 'ingest-odds'|'ingest-kalshi'|...
    provider         TEXT NOT NULL,
    args_json        TEXT NOT NULL,              -- SANITIZED invocation args
    status           TEXT NOT NULL,              -- 'running'|'succeeded'|'failed'|'partial'
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    started_monotonic_ns INTEGER NOT NULL,       -- durations use the monotonic clock
    duration_ns      INTEGER,
    requests_made    INTEGER NOT NULL DEFAULT 0,
    rows_written     INTEGER NOT NULL DEFAULT 0,
    rows_skipped_duplicate INTEGER NOT NULL DEFAULT 0,
    error_type       TEXT,                       -- exception class name only
    error_message    TEXT,                       -- SANITIZED
    tool_version     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE raw_responses (
    raw_response_id  TEXT PRIMARY KEY,           -- 'raw_<ulid>'
    run_id           TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    provider         TEXT NOT NULL,              -- 'the_odds_api'|'kalshi_public'|...
    endpoint         TEXT NOT NULL,              -- SANITIZED path, no query string
    request_params_json TEXT NOT NULL,           -- SANITIZED, canonical JSON
    http_method      TEXT NOT NULL DEFAULT 'GET' CHECK (http_method = 'GET'),
    http_status      INTEGER NOT NULL,
    response_headers_json TEXT NOT NULL,         -- SANITIZED allow-list, canonical JSON
    requested_at     TEXT NOT NULL,
    responded_at     TEXT NOT NULL,
    elapsed_ns       INTEGER NOT NULL,           -- monotonic
    body             TEXT NOT NULL,              -- verbatim response body
    body_hash        TEXT NOT NULL,              -- sha256 of body bytes
    content_hash     TEXT NOT NULL,              -- sha256 over (provider, endpoint,
                                                 -- params, body) — dedup key
    body_bytes       INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX idx_raw_responses_dedup    ON raw_responses (content_hash);
CREATE INDEX idx_raw_responses_provider ON raw_responses (provider, requested_at);
CREATE INDEX idx_raw_responses_run      ON raw_responses (run_id);
```

`http_method` carries `CHECK (http_method = 'GET')`. The read-only policy is
already enforced in `sports_quant/http_policy.py` at the transport layer; this
makes the storage layer independently incapable of recording a write verb, so a
future bug cannot quietly persist evidence of one.

**Implemented in migration `b004_raw_responses`** — the migration is
authoritative; deltas from the sketch above, each a correction found during
implementation:

- `ingestion_runs` statuses are `started | succeeded | partially_succeeded |
  failed` (the requirement's vocabulary), and the run carries `sport`,
  `operation`, `requested_at`, and **five** record counters —
  `records_received`, `records_normalized`, `records_inserted`,
  `records_deduplicated`, `records_rejected` — because "1000 received, 0
  inserted" and "0 received" are different incidents. A completion `CHECK`
  binds `completed_at` to any terminal status. It is deliberately **not**
  append-only: a run is opened `started` and closed with its counters.
- `raw_responses` is **not** deduplicated on `content_hash`. Two fetches
  returning identical bytes are two observations, each owned by its run;
  `content_hash` is indexed for traceability, and idempotency is enforced on
  the derived price snapshots instead. `endpoint` additionally carries
  `CHECK (endpoint NOT LIKE '%?%')`, so a query string (which would carry the
  key) cannot be stored even by accident.
- `b004` rebuilds `game_status_history` to add the nullable `raw_response_id`
  foreign key (see the note in §3.4 — it stays nullable because Phase A status
  rows have no owning response, and inventing one is worse than an honest NULL).

### 3.6 Sportsbook (The Odds API)

```sql
CREATE TABLE sportsbook_events (
    sb_event_id      TEXT PRIMARY KEY,           -- 'sbe_<ulid>'
    provider         TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,             -- The Odds API 'id'
    league_id        TEXT REFERENCES leagues(league_id),
    sport_key        TEXT NOT NULL,              -- 'baseball_mlb'|'basketball_nba'
    commence_time    TEXT NOT NULL,
    home_team_raw    TEXT NOT NULL,              -- exactly as the provider wrote it
    away_team_raw    TEXT NOT NULL,
    game_id          TEXT REFERENCES games(game_id),   -- NULL until matched
    match_decision_id TEXT REFERENCES entity_match_decisions(match_id),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (provider, provider_event_id)
);

CREATE TABLE sportsbook_markets (
    sb_market_id     TEXT PRIMARY KEY,           -- 'sbm_<ulid>'
    sb_event_id      TEXT NOT NULL REFERENCES sportsbook_events(sb_event_id),
    bookmaker_key    TEXT NOT NULL,              -- 'draftkings'
    bookmaker_title  TEXT,
    market_key       TEXT NOT NULL,              -- 'h2h'|'spreads'|'totals'
    created_at       TEXT NOT NULL,
    UNIQUE (sb_event_id, bookmaker_key, market_key)
);

CREATE TABLE sportsbook_outcomes (
    sb_outcome_id    TEXT PRIMARY KEY,
    sb_market_id     TEXT NOT NULL REFERENCES sportsbook_markets(sb_market_id),
    outcome_name     TEXT NOT NULL,              -- 'New York Yankees'|'Over'|'Under'
    outcome_role     TEXT NOT NULL,              -- 'home'|'away'|'over'|'under'|'draw'
    team_id          TEXT REFERENCES teams(team_id),   -- NULL for over/under
    created_at       TEXT NOT NULL,
    UNIQUE (sb_market_id, outcome_name)
);

CREATE TABLE sportsbook_price_snapshots (
    snapshot_id      TEXT PRIMARY KEY,
    sb_outcome_id    TEXT NOT NULL REFERENCES sportsbook_outcomes(sb_outcome_id),
    price_american   INTEGER,
    price_decimal    REAL,
    point            REAL,                       -- spread/total line; NULL for h2h
    bookmaker_last_update TEXT,                  -- provider's own 'last_update'
    market_last_update    TEXT,
    provider_timestamp TEXT,
    observed_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    raw_response_id  TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (sb_outcome_id, content_hash)
);

CREATE INDEX idx_sb_price_asof ON sportsbook_price_snapshots (sb_outcome_id, observed_at);
```

The `events → markets → outcomes → price_snapshots` split matters: an outcome's
*identity* (this bookmaker's home-team moneyline for this event) is stable while
its *price* changes constantly. Collapsing them would force re-storing the
identity on every poll and make "price history for this line" a string-matching
problem instead of an indexed scan.

`UNIQUE (sb_outcome_id, content_hash)` gives idempotent re-ingestion: polling
the same unchanged price twice writes one row, not two.

**Implemented in migration `b005_sportsbook`** — the migration is
authoritative; deltas from the sketch above:

- Every provider-scoped row carries a `raw_response_id NOT NULL` back to the
  response that created it, and each price snapshot additionally carries
  `raw_response_hash` and `run_id` (the two-link provenance contract, §4.1).
- The `match_decision_id` / `game_id` / `team_id` matching columns are **Phase
  D** and are not created yet: Phase B performs no matching, so `game_id`
  stays absent rather than nullable-and-unused. `league_id` *is* set, from the
  static `sport_key` map — a provider-enum lookup, not a name match.
- `sportsbook_outcomes` stores both `outcome_name` (normalized, part of the
  identity) and `provider_outcome_name` (verbatim), plus `point` and a NOT NULL
  `point_key` sentinel — the line is part of the identity, and a nullable point
  would let an h2h outcome insert twice. `outcome_role` includes `unknown`, so
  an unclassifiable outcome is recorded, never silently dropped.
- `sportsbook_price_snapshots.price_american` is `NOT NULL` and CHECK-bounded
  (`<= -100 OR >= 100`); `price_decimal` and `implied_probability` are exact
  arithmetic transforms stored for convenience. `implied_probability` is the
  **raw, vig-inclusive** number — no de-vigging happens in Phase B.
- Identity columns on events, markets and outcomes are frozen by
  `BEFORE UPDATE` triggers, so an upsert can refresh mutable current-state
  (commence time, `last_observed_at`, provider update times) without the
  identity drifting underneath its children.

### 3.7 Kalshi (public data only)

```sql
CREATE TABLE kalshi_events (
    kalshi_event_id  TEXT PRIMARY KEY,           -- 'kev_<ulid>'
    event_ticker     TEXT NOT NULL UNIQUE,       -- Kalshi's own ticker
    series_ticker    TEXT,
    title            TEXT,
    sub_title        TEXT,
    category         TEXT,
    league_id        TEXT REFERENCES leagues(league_id),
    game_id          TEXT REFERENCES games(game_id),
    match_decision_id TEXT REFERENCES entity_match_decisions(match_id),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE kalshi_markets (
    kalshi_market_id TEXT PRIMARY KEY,           -- 'kmk_<ulid>'
    kalshi_event_id  TEXT NOT NULL REFERENCES kalshi_events(kalshi_event_id),
    market_ticker    TEXT NOT NULL UNIQUE,
    title            TEXT,
    subtitle         TEXT,
    yes_sub_title    TEXT,
    market_type      TEXT,                       -- 'binary'
    rules_primary    TEXT,                       -- settlement rules text, verbatim
    rules_secondary  TEXT,
    rules_hash       TEXT,                       -- sha256; a rules change is material
    open_time        TEXT,
    close_time       TEXT,
    expiration_time  TEXT,
    settlement_side  TEXT,                       -- populated only after settlement
    game_id          TEXT REFERENCES games(game_id),
    match_decision_id TEXT REFERENCES entity_match_decisions(match_id),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE kalshi_orderbook_snapshots (
    snapshot_id      TEXT PRIMARY KEY,
    kalshi_market_id TEXT NOT NULL REFERENCES kalshi_markets(kalshi_market_id),
    yes_bids_json    TEXT NOT NULL,              -- [[price_cents, qty], ...] canonical JSON
    no_bids_json     TEXT NOT NULL,
    best_yes_bid     INTEGER,                    -- denormalized for fast scans
    best_no_bid      INTEGER,
    derived_yes_ask  INTEGER,                    -- 100 - best_no_bid
    derived_no_ask   INTEGER,                    -- 100 - best_yes_bid
    depth_levels     INTEGER NOT NULL,
    sequence         INTEGER,
    sequence_ok      INTEGER NOT NULL DEFAULT 1 CHECK (sequence_ok IN (0,1)),
    provider_timestamp TEXT,
    observed_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    raw_response_id  TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (kalshi_market_id, content_hash),
    CHECK (best_yes_bid IS NULL OR best_yes_bid BETWEEN 1 AND 99),
    CHECK (best_no_bid  IS NULL OR best_no_bid  BETWEEN 1 AND 99)
);

CREATE TABLE kalshi_trade_snapshots (
    trade_snapshot_id TEXT PRIMARY KEY,
    kalshi_market_id TEXT NOT NULL REFERENCES kalshi_markets(kalshi_market_id),
    provider_trade_id TEXT,
    taker_side       TEXT CHECK (taker_side IN ('yes','no')),
    yes_price        INTEGER CHECK (yes_price BETWEEN 1 AND 99),
    no_price         INTEGER CHECK (no_price  BETWEEN 1 AND 99),
    count            INTEGER NOT NULL,
    trade_time       TEXT,                       -- provider's trade timestamp
    provider_timestamp TEXT,
    observed_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    raw_response_id  TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (kalshi_market_id, content_hash)
);

CREATE INDEX idx_kalshi_book_asof  ON kalshi_orderbook_snapshots (kalshi_market_id, observed_at);
CREATE INDEX idx_kalshi_trade_asof ON kalshi_trade_snapshots (kalshi_market_id, observed_at);
```

**Derived asks are stored, never wire asks.** Kalshi publishes resting *bids* on
both sides; the executable Yes ask is `100 − best No bid`. `sports_quant/
providers/kalshi.py` already derives this correctly (`KalshiOrderBook.
executable_yes_ask`). The schema mirrors that derivation and keeps the full
ladders in `*_bids_json`, so no consumer is tempted to read a bid as an ask.

**No account-scoped columns exist anywhere.** There is no position, balance,
fill-ownership, order, or private-key column in this schema. `kalshi_trade_
snapshots` records the *public* trade print feed — anonymous market-wide prints,
not our fills, because we have none.

### 3.8 Injuries, lineups, probable pitchers, weather

These mirror the already-working `intel/` models (`SourceMeta` with
`published_at`/`retrieved_at`, immutable `StatusSnapshot` with a `content_hash`).
The current-state tables hold the resolved present view; the `*_snapshots`
tables are append-only observation logs.

```sql
CREATE TABLE injuries (
    injury_id        TEXT PRIMARY KEY,
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    team_id          TEXT REFERENCES teams(team_id),
    game_id          TEXT REFERENCES games(game_id),   -- NULL = not game-scoped
    current_status   TEXT NOT NULL,              -- mirrors intel.PlayerStatus
    body_part        TEXT,
    first_reported_at TEXT NOT NULL,
    resolved_at      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE injury_snapshots (
    snapshot_id      TEXT PRIMARY KEY,
    injury_id        TEXT REFERENCES injuries(injury_id),
    player_id        TEXT NOT NULL REFERENCES players(player_id),
    status           TEXT NOT NULL,
    reason           TEXT,
    expected_minutes REAL,
    minutes_restriction REAL,
    confidence       REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    source_id        TEXT NOT NULL,
    source_type      TEXT NOT NULL,              -- intel.SourceType
    is_official      INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0,1)),
    is_correction    INTEGER NOT NULL DEFAULT 0 CHECK (is_correction IN (0,1)),
    published_at     TEXT NOT NULL,              -- when the SOURCE published (= provider_timestamp)
    observed_at      TEXT NOT NULL,              -- when WE retrieved it
    ingested_at      TEXT NOT NULL,
    raw_response_id  TEXT REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash     TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL
);

CREATE TABLE lineups (
    lineup_id        TEXT PRIMARY KEY,
    game_id          TEXT NOT NULL REFERENCES games(game_id),
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    is_confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (is_confirmed IN (0,1)),
    confirmed_at     TEXT,                       -- NN whenever is_confirmed=1
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (game_id, team_id),
    CHECK (is_confirmed = 0 OR confirmed_at IS NOT NULL)
);

CREATE TABLE lineup_snapshots (
    snapshot_id      TEXT PRIMARY KEY,
    lineup_id        TEXT NOT NULL REFERENCES lineups(lineup_id),
    players_json     TEXT NOT NULL,              -- ordered [{player_id, slot, position}]
    is_confirmed     INTEGER NOT NULL CHECK (is_confirmed IN (0,1)),
    source_id        TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    raw_response_id  TEXT REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash     TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL
);

CREATE TABLE probable_pitchers (
    probable_id      TEXT PRIMARY KEY,
    game_id          TEXT NOT NULL REFERENCES games(game_id),
    team_id          TEXT NOT NULL REFERENCES teams(team_id),
    player_id        TEXT REFERENCES players(player_id),   -- NULL = announced TBD
    is_confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (is_confirmed IN (0,1)),
    superseded_by    TEXT REFERENCES probable_pitchers(probable_id),
    published_at     TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    raw_response_id  TEXT REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (game_id, team_id, content_hash)
);

CREATE TABLE weather_snapshots (
    snapshot_id      TEXT PRIMARY KEY,
    game_id          TEXT NOT NULL REFERENCES games(game_id),
    is_forecast      INTEGER NOT NULL CHECK (is_forecast IN (0,1)),
    forecast_for     TEXT,                       -- NN when is_forecast=1
    temperature_f    REAL,
    wind_speed_mph   REAL,
    wind_direction_deg REAL,
    precipitation_prob REAL,
    humidity_pct     REAL,
    conditions       TEXT,
    is_dome          INTEGER NOT NULL DEFAULT 0 CHECK (is_dome IN (0,1)),
    provider         TEXT NOT NULL,
    provider_timestamp TEXT,
    observed_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    raw_response_id  TEXT REFERENCES raw_responses(raw_response_id),
    raw_response_hash TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (game_id, provider, content_hash),
    CHECK (is_forecast = 0 OR forecast_for IS NOT NULL)
);
```

`probable_pitchers` is snapshot-shaped rather than mutable because a scratched
starter is one of the largest legitimate MLB price moves. Preserving the
announcement sequence (`superseded_by`) is exactly the signal, and overwriting
would destroy it.

`weather_snapshots.is_forecast` + `forecast_for` keep a *forecast made at
observed_at for forecast_for* distinct from an *observation of actual
conditions*. Conflating them is a leakage vector: the actual first-pitch weather
is not knowable pregame, but a forecast of it is.

### 3.9 Match decisions and data quality

```sql
CREATE TABLE entity_match_decisions (
    match_id         TEXT PRIMARY KEY,           -- 'mtc_<ulid>'
    entity_type      TEXT NOT NULL,              -- 'team'|'player'|'game'
                                                 -- |'sportsbook_event'|'kalshi_market'
    source_provider  TEXT NOT NULL,
    source_ref       TEXT NOT NULL,              -- provider id / raw text matched
    source_payload_json TEXT NOT NULL,           -- the inputs the matcher saw
    matched_entity_id TEXT,                      -- NULL when rejected/ambiguous
    outcome          TEXT NOT NULL,              -- 'accepted'|'rejected'|'ambiguous'
                                                 -- |'no_candidate'|'manual_override'
    method           TEXT NOT NULL,              -- 'exact_provider_id'|'exact_alias'
                                                 -- |'normalized_alias'|'schedule_key'
                                                 -- |'title_rules'|'manual'
    score            REAL NOT NULL CHECK (score BETWEEN 0.0 AND 1.0),
    threshold        REAL NOT NULL,
    candidates_json  TEXT NOT NULL,              -- ALL candidates + per-candidate scores
    rejection_reason TEXT,                       -- NN whenever outcome <> 'accepted'
    needs_manual_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_manual_review IN (0,1)),
    reviewed_by      TEXT,
    reviewed_at      TEXT,
    matcher_version  TEXT NOT NULL,              -- reproducibility of the decision
    raw_response_id  TEXT REFERENCES raw_responses(raw_response_id),
    decided_at       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    CHECK (outcome = 'accepted' OR rejection_reason IS NOT NULL),
    CHECK (outcome <> 'accepted' OR matched_entity_id IS NOT NULL)
);

CREATE INDEX idx_match_review ON entity_match_decisions (needs_manual_review, entity_type);

CREATE TABLE data_quality_issues (
    issue_id         TEXT PRIMARY KEY,
    run_id           TEXT REFERENCES ingestion_runs(run_id),
    severity         TEXT NOT NULL,              -- 'blocking'|'issue'|'note'
    rule_code        TEXT NOT NULL,              -- 'DQ-PIT-001', stable and greppable
    entity_type      TEXT NOT NULL,
    entity_id        TEXT,
    league_id        TEXT REFERENCES leagues(league_id),
    description      TEXT NOT NULL,
    detail_json      TEXT,
    detected_at      TEXT NOT NULL,
    resolved_at      TEXT,
    resolution_note  TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX idx_dq_open ON data_quality_issues (severity, resolved_at);
```

The two `CHECK` constraints on `entity_match_decisions` are the schema-level
expression of the rule *ambiguous matches are never silently accepted*: an
accepted decision must name an entity, and a non-accepted one must state why.
`candidates_json` stores every candidate considered — including the losers —
because "why did it pick that one?" is unanswerable from the winner alone.

`severity` deliberately reuses the vocabulary already in
`backtest/data_quality.py` (`issues` = execution-invalidating, `notes` =
non-fatal caveats), so the `data-quality` CLI and the backtester grade a corpus
on one scale rather than two.

---

## 4. Raw-response preservation

### 4.1 The contract

Every external response is written to `raw_responses` **before** any normalized
row derived from it. Every normalized row carries `raw_response_id` **and**
`raw_response_hash`. Two links, deliberately: the ID is the fast join; the hash
survives an export, a partial restore, or a corpus merge where IDs are
renumbered. A row whose `raw_response_hash` matches no `raw_responses.content_hash`
is a detectable corruption, and `data-quality` reports it as `DQ-RAW-001`.

Full traceability test (Phase B): for every normalized table, assert
`SELECT COUNT(*) FROM t LEFT JOIN raw_responses r ON t.raw_response_id =
r.raw_response_id WHERE r.raw_response_id IS NULL` equals zero.

### 4.2 Content hashing — reuse, do not reinvent

The repository already contains **three** content-hash implementations:

| Location | Function |
| --- | --- |
| `streaming/event_envelope.py` | `canonical_json()` + `compute_content_hash()` |
| `intel/base.py` | `_canonical()` + `content_hash()` |
| `evaluation/decision.py` | `_hash()` |

`state/base.py` already imports `canonical_json` from `streaming.event_envelope`,
and `streaming` is the established shared-foundation package (`evaluation`,
`gateway`, `probability`, `state` all import from it). **The database layer must
import `canonical_json` from `streaming.event_envelope` rather than add a
fourth implementation.** Phase B additionally consolidates `intel/base.py` onto
the same helper — a small, test-covered change that removes a real divergence
risk, since two canonicalizers that disagree produce two different hashes for
the same content and silently defeat deduplication.

### 4.3 Credential safety

No credential may appear in any stored column, ever. The Odds API key travels
as a `?apiKey=` query parameter, so this is a live hazard, not a theoretical
one.

Mechanisms, in layers:

1. **`endpoint` stores the path only** — never a full URL, never a query string.
2. **`request_params_json` is built from `sanitize_params()`**
   (`sports_quant/redaction.py`), which masks by parameter *name*
   (`apiKey`, `api_key`, `apikey`, `key`, `token`) so the key is redacted even
   where the value is unknown.
3. **`response_headers_json` uses an allow-list**, not a deny-list. Only
   `content-type`, `date`, `x-requests-remaining`, `x-requests-used`,
   `x-requests-last`, `cache-control`, `etag` are stored. A deny-list fails open
   when a provider adds a new header; an allow-list fails closed.
4. **`error_message` passes through `sanitize_url()`** before storage.
5. **Bodies are stored verbatim** — the public-data bodies of these two
   providers do not echo the API key. A Phase B test asserts this by scanning
   every stored body for the configured key.

**Enforcement test (Phase B, mandatory).** With a sentinel key configured, run
an ingestion against a mocked transport, then scan *every TEXT column of every
table* for the sentinel. Any hit fails the suite. This is a whole-database
sweep rather than a per-column assertion, so a newly added column is covered
the day it is created rather than the day someone remembers to test it.

---

## 5. Append-only enforcement

Snapshot tables are immutable. This is enforced in the database, not by
convention, because convention does not survive a future contributor:

```sql
CREATE TRIGGER trg_sb_price_snapshots_no_update
BEFORE UPDATE ON sportsbook_price_snapshots
BEGIN
    SELECT RAISE(ABORT, 'sportsbook_price_snapshots is append-only');
END;

CREATE TRIGGER trg_sb_price_snapshots_no_delete
BEFORE DELETE ON sportsbook_price_snapshots
BEGIN
    SELECT RAISE(ABORT, 'sportsbook_price_snapshots is append-only');
END;
```

Applied in Phase A to `game_status_history`, in **Phase B** to `raw_responses`
and `sportsbook_price_snapshots` (both now in
`sports_quant.db.schema.APPEND_ONLY_TABLES`), and in later phases to
`kalshi_orderbook_snapshots`,
`kalshi_trade_snapshots`, `injury_snapshots`, `lineup_snapshots`,
`probable_pitchers`, `weather_snapshots`, and `entity_match_decisions`.
`sports_quant.db.schema.APPEND_ONLY_TABLES` is the registry.

Corrections are appended, never applied in place: a corrected observation is a
new row with `is_correction = 1`, preserving both what was believed and what
replaced it. This mirrors the correction handling already in
`streaming/correction_handler.py` and `EventEnvelope.corrects_envelope_id`.

`entity_match_decisions` is append-only with one carve-out: `reviewed_by`,
`reviewed_at`, and `needs_manual_review` are updatable by the manual-review
workflow. The trigger permits an UPDATE only when every other column is
unchanged.

---

## 6. Layout

Built in Phase A (✅), planned for later phases (◻):

```
sports_quant/
  db/
✅  __init__.py          # public surface
✅  engine.py            # connections, PRAGMAs, transactions, migrations, SQL splitter
✅  ids.py               # ULID + deterministic canonical-ID construction
✅  schema.py            # timestamp format, enums, table registry
✅  normalize.py         # deterministic name normalization + alias resolution
✅  models.py            # typed row models
✅  init.py              # db-init orchestration (keeps SQL out of the CLI)
    migrations/
✅    a001_core_entities.sql
✅    a002_games.sql
✅    a003_integrity_guards.sql
✅    b004_raw_responses.sql
✅    b005_sportsbook.sql
◻     c006_kalshi.sql ...
    repositories/
✅    __init__.py  base.py
✅    leagues.py           # LeagueRepository + SeasonRepository
✅    teams.py             # TeamRepository + TeamAliasRepository
✅    players.py           # PlayerRepository + PlayerAliasRepository
✅    games.py             # GameRepository + status history
✅    raw_responses.py     # RawResponseRepository + content hashing
✅    ingestion_runs.py    # IngestionRunRepository
✅    sportsbook.py        # SportsbookRepository + as-of price queries
◻     kalshi.py  matching.py  data_quality.py
    seeds/
✅    __init__.py  loader.py  mlb_teams.py  nba_teams.py
✅ ingest/                 # __init__.py, runner.py, odds_ingestor.py
                          #   (kalshi_ingestor.py is Phase C)
◻ matching/               # teams.py, players.py, games.py, markets.py
                          #   (imports db/normalize.py -- one normalizer only)
◻ pit/                    # asof.py, dataset.py
```

`hashing.py` was not needed: `canonical_json` is imported directly from
`streaming.event_envelope`, keeping the count of content hashers at three
rather than four (§4.2).

`sports_quant/providers/` is untouched, exactly as planned.

`sports_quant/providers/` is **not** touched. The ingestors consume
`OddsApiClient` and `KalshiClient` exactly as they are today, so there is one
provider client per provider and the read-only transport policy remains the
only network path.
