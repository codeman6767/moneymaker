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

> **Phase E1 status — independently reviewed and COMPLETE.** Phase E1 point-in-time
> access and leakage-guard foundations have passed an independent correctness
> review (`sports_quant/pit/`, schema v16, no new migration). Every feature-facing
> historical read requires an explicit UTC cutoff and uses transaction time,
> never provider time or mutable current state. A fail-closed table registry
> classifies every future dataset join as immutable, season-scoped,
> as-of-filtered, evaluation-only, forbidden-current-state or unsupported.
> Closing lines are structurally isolated in an evaluation-only module.
> Sportsbook/Kalshi canonical links and orientations are visible historically
> only through accepted decisions and DQ/review timelines valid at the cutoff.
> Pregame weather requires a current forecast observed by cutoff with
> `pit_eligible=1`. Adversarial fixtures prove **DQ-PIT-001 through DQ-PIT-011**.
>
> **Focused E1 repair (this pass).** Six correctness repairs: **(1)** both mutable
> `games` columns — `status` AND `scheduled_start` (plus `updated_at`) — are
> forbidden for direct feature reads; `AsOfReader.game_schedule_state` returns the
> status and scheduled start TOGETHER from one `game_status_history` observation as
> of the cutoff (never a historical status with today's start). **(2)**
> `sportsbook_markets` is a MIXED table with an explicit structural allowlist
> (`sb_market_id`, `sb_event_id`, `bookmaker_key`, `market_key`); the mutable
> title, provider update times, current raw-response provenance, and
> first/last-observed fields are forbidden and `SELECT *` is prohibited. **(3)**
> Equal-`observed_at` rows are resolved by `content_hash`: a genuine conflict raises
> a typed `AsOfAmbiguityError` (fail-closed) rather than picking a ULID/insertion
> winner; `Observation.row_id` is provenance only, not a semantic tie-break. **(4)**
> `read_only_connection` opens SQLite in TRUE read-only mode
> (`file:…?immutable=1`, `uri=True`): a missing database is not created, no
> `-wal`/`-shm`/journal sidecar is written, and every write/DDL fails. **(5)** Alias
> tables (`team_aliases`, `player_aliases`, `venue_aliases`) are `unsupported` for
> feature joins — resolver inputs, not predictors; a season window does not make
> them feature-safe, and late alias curation cannot rewrite an earlier row.
> **(6)** Feature-facing identity (`sportsbook_event_game`, `kalshi_*`,
> `matched_entity`) fails closed for an accepted-but-review-gated decision until a
> required audited review is validly completed by the cutoff; a review completed
> after the cutoff is invisible earlier. The registry is programmatically verified
> to cover every schema-v16 table exactly once.
>
> **Independent-review repairs (this pass).** Four defects were found and fixed
> with adversarial regressions: **(1)** the generic as-of WHERE surface used a
> leaky blocklist that still admitted `OR`, `LIKE`/`GLOB`, `COLLATE`, quoted
> identifiers, commas and `1=1`; it is now a positive **allowlist** grammar
> (AND-conjunction of `col = ?` / `col = <int>` / `col = '<literal>'` /
> `col IS [NOT] NULL`), everything else fails closed. **(2)** `AsOfReader.observation()`
> exposed every column (content_hash, ingested_at, created_at, run_id,
> raw-response ids, provider ids and provider timestamps); it now projects to a
> per-table **feature-column allowlist** that is a strict subset of the
> content-hashed columns, so audit/provenance never becomes a feature and the
> returned object is fully determined by the content hash (rebuild-stable). A
> table with no feature policy fails closed. **(3)** game status/scheduled-start
> as-of read through the content-hash fail-closed path, not the repository's
> `status_as_of` ULID tie-break, so two providers disagreeing at the same
> `observed_at` raise `AsOfAmbiguityError` instead of a generated-id winner.
> **(4)** sportsbook/Kalshi identity is gated on the accepted-decision + review +
> DQ timelines valid at the cutoff (future cross-event conflicts, later decisions,
> later reviews, and DQ resolved after the cutoff do not rewrite an earlier
> cutoff's usability).
>
> **Phase E1 has independently passed its correctness review and is complete.**
>
> **Phase E2 status — independently reviewed and COMPLETE.** The historical
> row layer and quality tooling (`sports_quant/pit/dataset.py`,
> `sports_quant/quality/`, `sports_quant/status.py`, `sports_quant/report_access.py`,
> and the `data-status` / `data-quality` CLI commands) have passed an independent
> correctness review, schema still **v16** (no new migration).
> `build_historical_dataset(conn, league=…)` emits real pregame rows from
> persisted games + append-only observations using ONLY the E1 accessors and
> registry: one row per proven game; the feature cutoff is the game's scheduled
> start **taken from the earliest schedule snapshot actually visible at the cutoff**
> (a schedule first observed at/after that start cannot set its own cutoff — the row
> is excluded, fail-closed on equal-time schedule conflicts); label = the final
> result observed STRICTLY AFTER the cutoff (correction-aware, excluded/fail-closed
> on equal-time conflicts, invisible-at-cutoff verified); and the game↔reference
> correspondence gated on the accepted `entity_type='game'` decision decided by the
> cutoff. `score_diff`/`phase` are cutoff-known (0 pregame) and the result never
> enters the state payload. **Label isolation is structural:** `feature_state()` /
> `serialize()` carry identity + cutoff + cutoff-known state ONLY, while the label,
> winner and its provenance live on a SEPARATE `label_record()` / `serialize_labels()`
> surface — a later result correction changes the label surface byte-for-byte and
> leaves the feature-state serialization unchanged. It converts to the existing
> `GameStateDataset` WITHOUT fabricating data — a zero-column feature matrix and an
> all-NaN (explicitly unavailable) `true_prob`, length-invariant-checked — preserving
> chronological splitting; row `timestamp` is microseconds so sub-second-distinct
> cutoffs never collide.
>
> **Independent-review repairs (this pass), each with an adversarial regression.**
> **(1)** the feature cutoff is now the earliest *visible-at-cutoff* schedule start,
> closing a future-schedule/cutoff-rewriting leak. **(2)** feature-state and label
> surfaces are physically separated (above). **(3)** equal-time conflict coverage is
> **registry-derived**: `conflict_scan_tables` enumerates every as-of-filtered,
> content-hashed append-only table with a `UNIQUE(...)` anchor, so a newly-added
> observation table cannot silently escape the `DQ-PIT-008` scan. **(4)** `data-status`
> / `data-quality` **fail closed on a committed-but-uncheckpointed WAL** (a non-empty
> `-wal` sidecar → exit `3`), never a silent stale `immutable=1` read. **(5)** an OPEN
> blocking/`--fail-on` `data_quality_issues` row now gates BOTH `corpus_valid` and the
> exit code — the command can never report the corpus valid while a blocking open
> issue exists (`execution_valid` still reflects only the newly-detected E2 rule
> findings). **(6)** pending manual-review counts count only the LATEST flagged
> decision per `(entity_type, source_provider, source_ref)`, so superseded or
> completed reviews are not over-counted. **(7)** `provider_runs` breaks a same
> `started_at` tie deterministically — differing statuses are reported as
> `ambiguous(...)` rather than a rebuild-dependent generated-`run_id` winner. **(8)**
> `--since` is validated as a real `YYYY-MM-DD` (invalid → usage error, never silent
> zeros); league/since scoping is applied only where honest, else a `not attributable`
> note.
>
> `data-status` and `data-quality` are OFFLINE, genuinely read-only
> (`immutable=1`, no sidecars), exit `3` on a missing/unmigrated/corrupt/stale-WAL db,
> and `data-quality` exits `1` at/above its `--fail-on` severity (E2 findings OR open
> issues). E2 corpus rules (report-only; never upserted into `data_quality_issues`)
> prove leakage/determinism defects: `DQ-PIT-001` result-leak (blocking), `DQ-PIT-008`
> equal-time conflict (blocking), `DQ-PIT-011` unknown weather eligibility (issue),
> `E2-LABEL-UNAVAILABLE` / `E2-IDENTITY-MISSING` (note). **No feature engineering,
> model training, live request, ingestion, backfill, recommendation or execution
> work was performed.**
>
> **Phase E2 has independently passed its correctness review and is complete;
> Phase E (E1 + E2) is complete. F0 (Phase F research planning) is complete — see
> `PHASE_F_RESEARCH_PLAN.md` and `PHASE_F_FEATURE_CONTRACT.md`. No Phase F
> implementation (corpus backfill, feature engineering, modeling, calibration,
> simulation, recommendations, backtesting, paper trading, execution) has been
> started; schema remains v16 and no live provider request or persisted ingestion
> occurred during F0.**

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
| Kalshi order books append-only, transition-aware, with as-of reads | ✅ **Phase C** `c007_kalshi`: `UNIQUE (market_ticker, observed_at, content_hash)` + immediate-predecessor comparison; `KalshiRepository.orderbook_as_of()` / `latest_orderbook()` |
| Kalshi public trades append-only + idempotent, with range reads | ✅ **Phase C** `UNIQUE (market_ticker, content_hash)`; `KalshiRepository.trades_in_range()` |
| Kalshi event/market current metadata never regressed by a stale backfill | ✅ **Phase C** — upserts refresh only on a strictly-newer `observed_at`, equal retains earlier (deterministic) |
| Kalshi current-metadata provenance is explicit and traceable | ✅ **Phase C** `c008` — `first_raw_response_id` (creating) vs `current_raw_response_id`/`current_raw_response_hash` (supplied the current values); current pointers move only on a strictly-newer observation |
| Official results/box/injuries/lineups/probables/weather as-of accessors | ◻ **Phase D (planned)** — every Phase D snapshot table carries `observed_at` (= `raw_responses.received_at`) as the sole cutoff; authoritative-time-per-category table in `PHASE_D_IMPLEMENTATION_PLAN.md` §6 |
| Match decisions bounded by `decided_at` (DQ-PIT-010) | ◧ **D5A built (mocked/offline)** — `entity_match_decisions.decided_at` is a real transaction-time boundary; `SqliteMatchingRepository.decisions_for_source(..., as_of=cutoff)` filters `decided_at ≤ cutoff`, so a match (or later manual review) decided after the cutoff is invisible to an earlier point-in-time read. **A current `provider_*_references.*_id` link is NOT by itself PIT-safe** — it reflects the newest accepted decision, so Phase E historical joins must consult the decision timeline (`decided_at ≤ cutoff`), not the current link column. The D5A home-venue local-date tier is likewise
knowledge-bounded: a prior game supplies its venue timezone only when its accepted
non-swapped game decision was `decided_at ≤ the target schedule observation
cutoff`, so a game/venue matched (or a review approved) after the cutoff cannot
influence an earlier game's local date. **D5B1 (sportsbook events) built:** an
event's `entity_match_decisions` row is bounded the same way; a current
`sportsbook_events.game_id`/`orientation` is not itself PIT-safe, and
`SqliteSportsbookRepository.is_orientation_approved(sb_event_id, as_of=cutoff)`
returns False for a decision not yet decided (or a neutral swapped match not yet
reviewed) as of the cutoff, so a pricing consumer never treats an unapproved or
future orientation as historical truth. That readiness check is now
**fail-closed**: beyond `direct` + accepted + not-review-gated + `decided_at ≤
cutoff`, it also requires the decision and the link to name the same game, no
other sportsbook event linked to that game under a different orientation, and no
blocking identity/orientation data-quality issue on the event — a `direct`
orientation string alone is never sufficient. **These extra gates are themselves
as-of correct** (independent review): a conflicting event counts only when its
supporting decision's `decided_at ≤ cutoff` (a later conflict cannot leak
backward), and a blocking DQ blocks a cutoff only when it was *active* then
(`detected_at ≤ cutoff AND (resolved_at IS NULL OR resolved_at > cutoff)`) — a DQ
detected after the cutoff does not block, and one active at the cutoff blocks even
if resolved later. Because d015 stores only the current immutable link (no
event-link history), the supported historical boundary is the decision and DQ
timelines, not a reconstruction of prior mutable event metadata. A neutral
**swapped** event is never approved by this check and has **no implemented review
workflow** that would flip it to approved, so it stays excluded from price-safe
use indefinitely. A current `sportsbook_events` row is mutable current-state, not
a historical event snapshot: the decision/DQ `raw_response_id` is the event's
immutable **first-observation** response, and schema v15 records no per-field
current-supplying observation, so it must not be read as the exact source of the
current commence/team metadata used for matching; Phase E must read from the
decision and DQ timelines and respect that limitation. **D5B2 (Kalshi events +
game-winner markets) built (schema v16):** event and market
`entity_match_decisions` rows are bounded by `decided_at` the same way; a current
`kalshi_events.game_id` / `kalshi_markets.game_id` link is not itself PIT-safe;
`SqliteKalshiRepository.is_kalshi_market_orientation_approved(kalshi_market_id,
as_of=cutoff)` is fail-closed and as-of correct. **Current** readiness (no
cutoff) requires an accepted, non-review-flagged market decision naming this
market and game, a Yes team participating in the game, today's `rules_hash` still
equal to the decision's `matched_rules_hash` (a current rules change invalidates
it immediately via a blocking `DQ-MATCH-004`), and no unresolved blocking DQ.
The MLB exact-time tier converts the ticker's venue-local clock through the
candidate venue's timezone, but that venue association is itself knowledge-time
gated (an accepted `game` decision + venue `first_observed_at ≤ cutoff`), so the
time conversion can never borrow future venue evidence. **Historical** readiness
(`as_of=cutoff`) must NOT read today's mutable
`rules_hash` or `needs_manual_review` (a later change would retroactively rewrite
an earlier answer); it requires only that the accepted decision existed by the
cutoff (`decided_at ≤ cutoff`, immutable accept/game/Yes) and that no blocking
identity/orientation/rules DQ was *active* at the cutoff (DQ `detected_at ≤
cutoff AND (resolved_at IS NULL OR resolved_at > cutoff)`) — so a rules change
detected after the cutoff cannot block it, one active at the cutoff does, and a
resolution after the cutoff still blocks that cutoff. The decision/DQ provenance uses the Kalshi row's
`current_raw_response_id` (the response that supplied the matched metadata), never
the immutable `first_raw_response_id`. Current mutable Kalshi metadata is not a
complete historical snapshot; c008 retains only current + first provenance, so
Phase E must read from the decision/DQ timelines and must not claim to reconstruct
prior rules text/hash observations beyond that boundary |
| Weather forecast-vs-actual kept distinct (leakage vector) | ◧ **D4 built (schema v14)** — `weather_snapshots.weather_kind` separates `current_forecast` / `station_observation` / `historical_forecast` / `reanalysis`; `observed_at` is never backdated to a model-run time; an explicit `pit_eligible` (1/0/NULL) is set (a station observation / reanalysis is never PIT-eligible; a stitched historical forecast whose availability is unproven is `pit_eligible=NULL` + a `DQ-WX-PIT-001` note). Phase E must gate pregame weather features on `weather_kind='current_forecast' AND observed_at ≤ cutoff AND pit_eligible=1` — never on the endpoint of origin, and never a reanalysis/observation row |
| Full `pit/asof.py` + safe-join registry + evaluation-only isolation + adversarial leak fixtures | ✅ **E1 complete — independently reviewed (schema v16, no new migration)** — `sports_quant/pit/` ships the strict `Cutoff` type, the canonical `latest_as_of` algorithm (content-hash fail-closed ties, positive-allowlist WHERE grammar), the fail-closed table registry (structural + feature-column policies, true read-only URI mode, review-gated identity), feature-facing as-of accessors that project only feature-safe columns, `evaluation_only.closing_line_for_evaluation`, and adversarial fixtures proving **DQ-PIT-001..011**. **E2 complete — independently reviewed:** `pit/dataset.py` historical row builder (visible-at-cutoff cutoff, feature-state/label split, microsecond timestamps) + `sports_quant/quality/` rules/report (registry-derived conflict scan) + `data-status`/`data-quality` commands (uncheckpointed-WAL fail-closed, open-issue validity/exit gate). Phase E complete; no later phase started. |

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

### DQ-PIT-011 — Weather reanalysis / observation / unproven historical forecast as a pregame feature

**Hazard.** A weather row's *endpoint of origin* does not make it point-in-time
safe. An ERA5 **reanalysis** row and a **station observation** describe what
actually happened — using either as a "pregame forecast" leaks the outcome. A
**stitched historical forecast** retrieved today is not a single issued model run,
and its availability before a given cutoff cannot be assumed. Backdating
`observed_at` to a model-run time would silently make any of these look
prediction-eligible.

**Defence.** `weather_snapshots.weather_kind` keeps the four kinds distinct, and
`observed_at` always records when *this project* received the response (never a
provider model-run time). Each row carries an explicit `pit_eligible`:
`station_observation` and `reanalysis` are always `0`; a `current_forecast` is `1`
only when it was received at or before first pitch; a `historical_forecast` is
`NULL` (unknown) with a `DQ-WX-PIT-001` note because availability is unproven.
Phase E pregame features must select `weather_kind='current_forecast' AND
observed_at <= cutoff AND pit_eligible=1` — never a reanalysis/observation row, and
never a `pit_eligible IS NOT 1` forecast.

**Test.** Ingest a reanalysis row and a historical-forecast row; assert
`pit_eligible` is `0` and `NULL` respectively, and that a pregame-feature query
filtered on `weather_kind='current_forecast' AND pit_eligible=1` returns neither.

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
