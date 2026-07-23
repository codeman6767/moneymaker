# Point-in-Time Data

Temporal semantics and leakage prevention for the historical corpus.

The single question this document answers:

> **What did we actually know, and when did we know it?**

Every historical dataset row must be reconstructable from facts that were
observable strictly before the row's decision time. A dataset that violates this
produces a model that appears excellent in backtest and loses money live. This
is the most expensive failure mode available to this project, so it is designed
against structurally rather than checked for afterwards.

Companion documents: `DATA_ARCHITECTURE.md` (schema), `ENTITY_MATCHING.md`
(matching), `DATA_FOUNDATION_PLAN.md` (phasing).

---

## 1. Bitemporal model

The corpus is **bitemporal**. Every observation carries two independent time
axes:

| Axis | Question | Columns |
| --- | --- | --- |
| **Valid time** | When was this true in the world? | `provider_timestamp` |
| **Transaction time** | When did *we* learn it? | `observed_at`, `ingested_at` |

Conflating the two is the root cause of most leakage. A provider can publish at
14:00 a report timestamped 09:00. The fact was *true* at 09:00 but was not
*knowable to us* until 14:00. A backtest making a 10:00 decision must not see
it. Only the transaction-time axis answers "could I have acted on this?", and
only the valid-time axis answers "when did this actually happen?".

---

## 2. The five timestamps

Every point-in-time row carries these. Definitions are exact and not
interchangeable.

### `provider_timestamp` — valid time, provider's clock

When the provider says the fact became true. The provider's own event time:
The Odds API's `last_update`, an injury report's publication time, a Kalshi
trade's execution time.

- **Nullable.** Many providers omit it. A NULL is recorded honestly and raises
  a data-quality note; it is never defaulted to `observed_at`, because doing so
  silently invents a provenance claim.
- **Not trusted for ordering across providers.** Provider clocks are unsynchronized
  and occasionally wrong.
- **Never used as the point-in-time cutoff.** See §3.

### `observed_at` — transaction time, our clock, the load-bearing one

When *we* received the response containing this fact. Taken from the owning
`raw_responses.received_at` (the column name in migration `b004`), so every
fact derived from one response shares one `observed_at`. In the Phase B
sportsbook path this is enforced structurally: the odds ingestor reads
`observed_at` from the stored raw response and stamps every derived price
snapshot with it. This is the only timestamp that answers "was this knowable to
us?".

- **Never NULL.** A fact with no observation time cannot be used safely and is
  rejected at write time.
- **Never back-dated.** Not to the provider's timestamp, not to the game start,
  not to anything. `observed_at` is when the bytes arrived.
- Corresponds to `retrieved_at` in the existing `intel.SourceMeta`, which
  already draws exactly this distinction against `published_at`.

### `ingested_at` — when we wrote it to the database

Normally within milliseconds of `observed_at`, but meaningfully different during
a **backfill**: a response captured on 2026-04-01 and parsed into new tables on
2026-07-01 has `observed_at = 2026-04-01`, `ingested_at = 2026-07-01`. Used for
operational questions ("what did last night's re-parse write?"), never for
feature cutoffs.

### `created_at` / `updated_at` — row lifecycle

Physical row bookkeeping. On append-only tables `created_at` equals
`ingested_at` and `updated_at` does not exist. On mutable current-state tables
(`games`, `teams`, `players`, `injuries`, `lineups`) `updated_at` records the
last mutation. **Neither is ever a feature input or a join key** — they describe
the database, not the world.

### `raw_response_id` / `raw_response_hash` — provenance

The link back to the exact bytes this row was parsed from. Both are stored
(see `DATA_ARCHITECTURE.md` §4.1). Any row that cannot name its source cannot
be audited, and an unauditable corpus is not a research asset.

### 2.1 Which timestamp training and backtesting use

> **`observed_at` is the point-in-time cutoff. Always. Without exception.**

Every as-of query, every training-set join, and every backtest replay filters on
`observed_at <= cutoff`.

`provider_timestamp` is used for exactly two things:

1. **Measuring provider lag**: `observed_at − provider_timestamp`. This is a
   modeled quantity in its own right and is already treated as such by
   `evaluation/` (`MarketEvent.provider_lag_ns`) and `backtest/latency_model.py`.
2. **Ordering facts within a single provider's stream**, where its clock is at
   least self-consistent.

It is never a cutoff. A worked example of why:

| Fact | provider_timestamp | observed_at |
| --- | --- | --- |
| "Judge scratched from lineup" | 2026-07-22T17:00:00Z | 2026-07-22T18:45:00Z |

A model making an 18:00 decision that filters on `provider_timestamp <= 18:00`
sees the scratch. In reality nobody outside the clubhouse knew until 18:45. The
backtest would credit the model with a 105-minute head start it never had, and
that edge would evaporate live. Filtering on `observed_at <= 18:00` correctly
hides it.

---

## 3. As-of query pattern

The canonical shape, implemented once in `sports_quant/pit/asof.py` and reused
everywhere:

```sql
-- Latest observation of each outcome's price, as known at :as_of
SELECT s.*
FROM sportsbook_price_snapshots s
JOIN (
    SELECT sb_outcome_id, MAX(observed_at) AS max_observed
    FROM sportsbook_price_snapshots
    WHERE observed_at <= :as_of
    GROUP BY sb_outcome_id
) latest
  ON  s.sb_outcome_id = latest.sb_outcome_id
  AND s.observed_at   = latest.max_observed
WHERE s.observed_at <= :as_of;
```

Three properties make this safe:

- The `<= :as_of` predicate appears in **both** the inner aggregate and the
  outer filter. Omitting it from the inner query is the classic bug: the
  aggregate picks a future maximum, the outer filter then finds nothing, and
  the row silently vanishes — which looks like missing data, not like leakage,
  and so goes uninvestigated.
- Ties on `observed_at` break deterministically by `snapshot_id` (ULIDs are
  creation-ordered), so a rebuild yields identical datasets.
- No `updated_at`, `created_at`, or `provider_timestamp` appears anywhere.

**API-level enforcement.** `sports_quant/pit/asof.py` exposes no function that
returns snapshot rows without a mandatory `as_of` parameter. There is no
"get latest" convenience overload, because that function would be the one every
future caller reaches for by accident. Not offering it is cheaper than
policing it.

---

## 4. Leakage prevention

Each subsection states the hazard, the structural defence, and the test. Rule
codes are stable and greppable; they appear in `data_quality_issues.rule_code`
and in test names.

### Implementation status

Phase A landed the temporal foundations these rules rest on. What exists today:

| Mechanism | Status |
| --- | --- |
| ISO-8601 UTC `TEXT` timestamps, lexicographically sortable | ✅ `db/schema.py`, enforced by `CHECK` constraints on every timestamp column |
| Naive datetimes rejected at write time | ✅ `schema.to_iso()` raises rather than assuming UTC |
| `game_status_history` with `provider_timestamp` / `observed_at` / `ingested_at` | ✅ migration `a002_games` |
| Append-only triggers (DQ-PIT-008) | ✅ on `game_status_history` |
| As-of accessor filtering on `observed_at` | ✅ `GameRepository.status_as_of()` |
| Deterministic tie-break by ULID | ✅ monotonic ULIDs, `ORDER BY observed_at DESC, status_id DESC` |
| `games.original_start` never updated | ✅ enforced by trigger (`a003`), not convention |
| Stale backfill cannot regress current state | ✅ `a003` patch — see below |
| Transition-aware status deduplication | ✅ `a003` — a repeated state is a real transition, not a duplicate |
| Sportsbook price snapshots append-only with `observed_at` / `provider_timestamp` | ✅ **Phase B** migration `b005_sportsbook`, `raw_responses.received_at` supplies `observed_at` |
| As-of price accessor filtering on `observed_at` (DQ-PIT-005/006 shape) | ✅ **Phase B** `SportsbookRepository.price_as_of()` / `latest_price()` / `prices_in_range()` |
| Transition-aware idempotent re-ingestion + preserved backfill (DQ-PIT-008) | ✅ **Phase B** `b006`: `UNIQUE (sb_outcome_id, observed_at, content_hash)` + immediate-predecessor comparison; append-only triggers |
| Current event/market metadata never regressed by a stale backfill | ✅ **Phase B** integrity repair — event/market upserts refresh only on a strictly-newer `observed_at` (see below) |
| Full `pit/asof.py`, `pit/dataset.py`, adversarial leak fixtures | ◻ Phase E |

`GameRepository.status_as_of()` is the first working instance of the §3
pattern, and its tests already cover the DQ-PIT-004 shape: a status observed at
T2 but back-dated by the provider to T0 is **not** returned by a query as of
T1.

#### The stale-backfill rule (`a003`)

Backfill is where bitemporality earns its keep, and where it is easiest to get
wrong. Before the a003 patch, `record_status()` copied the row it had just
written into `games.status` — so a late-arriving observation describing an
*earlier* moment overwrote a newer state. Replaying yesterday's feed would have
rewound the corpus's idea of the present.

The rule now:

> **History is ordered by `observed_at`; current state is the newest
> observation, not the most recently written one.**

After every insert, `games.status` and `games.scheduled_start` are recomputed
from `ORDER BY observed_at DESC, status_id DESC LIMIT 1`. An older observation
is preserved in history — it is a genuine point-in-time fact — but it does not
touch the present. Both halves happen in one transaction, so the history row
and the current-state row can never disagree.

This is the same ordering `status_as_of()` uses, which is deliberate: a query
`as_of` "now" and a read of `games.status` must agree, and they only do if both
sort the same way. `status_id` is a monotonic ULID, so observations sharing an
`observed_at` resolve identically on every rebuild — without that second key,
two rows with the same timestamp would order arbitrarily and a rebuilt corpus
could disagree with the original.

### DQ-PIT-001 — Final scores in pregame features

**Hazard.** `games.status` and any final-score column reflect *now*, not the
decision time. Joining `games` directly into a pregame row leaks the outcome.

**Defence.** Final scores live only in game-result rows carrying their own
`observed_at` (set to when the result was *published*, not when the game ended).
The dataset builder reads game state exclusively through
`game_status_history` as of the cutoff. `games.status` is documented as
present-state-only and is unreachable from `sports_quant/pit/`.

**Test.** Build a pregame dataset with a cutoff before first pitch; assert no
column correlates with the label above chance; assert the generated SQL text
contains no reference to `games.status`.

### DQ-PIT-002 — Postgame statistics in pregame rows

**Hazard.** Player/team season aggregates computed from all games in a season
include games that had not been played at the cutoff.

**Defence.** Aggregates are never precomputed and stored. They are computed
inside the as-of window from games whose *result observation* satisfies
`observed_at <= cutoff`. A stored season-aggregate table is explicitly rejected
by this design; it cannot be made point-in-time-safe without becoming a
snapshot table, at which point it is one.

**Test.** For a fixture season, compute a team's win total as of mid-season and
assert it equals the hand-counted value, not the season-final value.

### DQ-PIT-003 — Confirmed lineups before publication

**Hazard.** A lineup is *known* to the team hours before it is *published*.
`lineups.is_confirmed` reflects the present.

**Defence.** `lineups.confirmed_at` is `NOT NULL` whenever `is_confirmed = 1`
(schema `CHECK`). Point-in-time reads use `lineup_snapshots` filtered on
`observed_at <= cutoff` and treat confirmation as true only if a snapshot with
`is_confirmed = 1` was observed by the cutoff. The `lineups` table is not
readable from `sports_quant/pit/`.

**Test.** Insert a lineup confirmed at T+60m; query as of T; assert it reads as
unconfirmed and that its player list is either absent or flagged projected.

### DQ-PIT-004 — Injury information before observation

**Hazard.** Using `published_at` as the cutoff exposes reports we had not yet
fetched — the worked example in §2.1.

**Defence.** `injury_snapshots` is append-only and queried on `observed_at`.
`published_at` is stored for lag measurement only.

**Test.** Insert a snapshot with `published_at` well before `observed_at`; query
as of a time between them; assert it is not returned. Assert directly that
`pit.asof` emits no SQL filtering on `published_at`.

### DQ-PIT-005 — Closing odds before they existed

**Hazard.** Closing line value is the standard evaluation metric, and the
closing line is by definition the last price before start. Letting it reach a
pregame feature is catastrophic and easy to do accidentally.

**Defence.** Closing prices are retrievable only through an explicitly named
`closing_line_for_evaluation(game_id)` function, in a separate module from the
feature-facing API, documented as evaluation-only. The feature builder is
structurally unable to call it: a test asserts `sports_quant/pit/dataset.py`
does not import it.

**Test.** Grep-style assertion over `pit/dataset.py` imports, plus a runtime
assertion that no feature column's `observed_at` exceeds the row's cutoff.

### DQ-PIT-006 — Future sportsbook snapshots in historical predictions

**Hazard.** The inner-aggregate bug described in §3.

**Defence.** The single shared as-of builder, plus a `MAX(observed_at) <= cutoff`
assertion applied to every returned frame.

**Test.** Property test: for random cutoffs across a fixture corpus, assert
`max(observed_at) <= cutoff` over every returned row of every snapshot type.

### DQ-PIT-007 — Future records in training joins

**Hazard.** A join that is correct per-table can still leak: joining
point-in-time-correct odds to a `players` row whose `updated_at` is later
imports a future fact (a position change, a trade) through the dimension table.

**Defence.** Dimension attributes that can change (team membership, position)
are read from season-scoped or snapshot tables, never from the mutable current
row. `teams` carries `(first_season, last_season)`; roster membership is
season-scoped. The dataset builder emits its full join list, and every joined
table is either immutable, season-scoped, or as-of filtered.

**Test.** Enumerate the builder's joined tables; assert each is in the
immutable/season-scoped/as-of-filtered registry. A new join to an unregistered
mutable table fails the test — the failure is the point.

### DQ-PIT-008 — Overwritten historical snapshots

**Hazard.** Re-running an ingestion overwrites a stored snapshot, so the corpus
silently stops reflecting what was known then. This corrupts every historical
dataset built afterwards and is undetectable after the fact.

**Defence.** `BEFORE UPDATE` / `BEFORE DELETE` triggers that `RAISE(ABORT)` on
every snapshot table (`DATA_ARCHITECTURE.md` §5). Idempotency is achieved with a
`content_hash` uniqueness key + `INSERT OR IGNORE`, so re-ingesting identical
content is a no-op rather than a rewrite. Corrections append with
`is_correction = 1`.

**Transition-aware refinement (sportsbook prices, migration `b006`).** A purely
global `UNIQUE (sb_outcome_id, content_hash)` is too strong: a price that
reverts to an earlier value (`-110 → -120 → -110`) hashes its third observation
identically to its first, so a global key would drop a real transition — exactly
the `game_status_history` defect `a003` fixed. The price key therefore includes
`observed_at` (`UNIQUE (sb_outcome_id, observed_at, content_hash)`), and the
repository collapses an observation only when it equals its **immediate temporal
predecessor**. Consecutive unchanged re-polls collapse; a reversal appends;
exact replay and repeated backfill stay idempotent; no historical row is
mutated. See `DATA_ARCHITECTURE.md` §3.6.1.

**Stale-metadata protection (Phase B integrity repair).** The mutable
current-state columns on `sportsbook_events` and `sportsbook_markets` (commence
time, team text, provider update times, `last_observed_at`) obey the same
stale-backfill rule as `games.status`: they are refreshed only from a strictly
**newer** `observed_at`, so a late-arriving observation of an *earlier* moment is
preserved through its snapshots but never rewinds the current metadata. Equal
`observed_at` retains the earlier-recorded value — deterministic under ordered
replay. Point-in-time reads use the append-only snapshots as of the cutoff, not
these current-state columns.

**Test.** Attempt `UPDATE` and `DELETE` on each snapshot table; assert both
raise. Re-run an identical ingestion twice; assert row counts are unchanged and
every `content_hash` still resolves to its original `raw_response_id`.

### DQ-PIT-009 — Cross-provider clock skew

**Hazard.** Ordering facts from two providers by `provider_timestamp` produces
an ordering that never existed, because their clocks disagree.

**Defence.** Cross-provider ordering always uses `observed_at`, which is our
single clock.

**Test.** Two providers reporting the same fact with inverted
`provider_timestamp`s; assert as-of ordering follows `observed_at`.

### DQ-PIT-010 — Match decisions made with future information

**Hazard.** Subtle and easy to miss. If a sportsbook event is matched to a
canonical game using information observed *after* the decision time, then the
mere existence of the link encodes the future. A postponed game rematched two
days later makes the original row look resolvable when it was not.

**Defence.** `entity_match_decisions.decided_at` is recorded, and
point-in-time joins may use only decisions with `decided_at <= cutoff`. A match
decided later is invisible to earlier datasets.

**Test.** Match a sportsbook event at T+1d; build a dataset as of T; assert the
event is unlinked in that dataset.

---

## 5. Leakage test suite

Lives at `sports_quant/pit/tests/test_leakage.py`, runs in the normal `pytest`
sweep, and is a Phase E completion gate. Its structure:

| Layer | What it proves |
| --- | --- |
| **Schema invariants** | Triggers fire; `CHECK`s hold; append-only tables reject UPDATE/DELETE. |
| **Query invariants** | Every as-of query returns only `observed_at <= cutoff`; ties break deterministically. |
| **Builder invariants** | Joined tables are all registered safe; no forbidden import reaches the feature path. |
| **Adversarial fixtures** | A hand-built corpus with a deliberately planted leak of each type, asserting the guard catches it. |

The adversarial fixtures matter most. A test asserting "no leakage found in
clean data" passes trivially and proves nothing. Each `DQ-PIT-*` fixture plants
one specific violation and asserts the specific guard rejects it — so the suite
fails loudly if a guard is ever removed.

**Determinism gate.** Building the same dataset twice from the same corpus at
the same cutoff must produce byte-identical output. This catches
nondeterministic tie-breaks and dict-ordering bugs that otherwise surface only
as unreproducible model results.

---

## 6. Interface with the existing research lane

`probability/datasets.py` defines `GameStateDataset` and states in its own
docstring that its synthetic builders are placeholders: *"In production these
builders are replaced by real historical game states with outcomes; the
interfaces stay the same."*

That contract is honoured exactly. `sports_quant/pit/dataset.py` emits a
`GameStateDataset` with identical field semantics:

| Field | Source under this design |
| --- | --- |
| `X` | Feature vectors — **built in a later stage, not here.** Phase E delivers the row set, cutoffs, and label; feature engineering is explicitly out of scope. |
| `y` | Home-win label from the game result, observed strictly after the cutoff. The label is the only permitted future value. |
| `timestamps` | The row's `observed_at` cutoff, monotonically increasing. |
| `score_diff`, `phase` | Read from game state as of the cutoff. |

`GameStateDataset.chronological_split()` already splits by time and never
shuffles across the boundary. Because `timestamps` carries `observed_at`, that
existing method becomes point-in-time-correct for free — no change to
`probability/` is required, and none is proposed.

The label deserves one explicit note. `y` is genuinely future information: it is
the training target and cannot be anything else. The discipline is that it
appears **only** as `y`, never as a feature, and the DQ-PIT-001 test exists
specifically to prove it has not leaked into `X`.
