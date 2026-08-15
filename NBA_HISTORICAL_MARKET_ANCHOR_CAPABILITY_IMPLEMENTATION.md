# NBA Historical Market Target-Anchor Capability — Zero-Network Implementation

**Status:** capability implemented and tested offline. **F1-R remains BLOCKED**,
on a *different* blocker than before.

**Commit baseline:** `80497cd` (HEAD == `origin/main`, clean tree, schema v19).
**Network activity this task:** none. **Credits spent:** 0. **Provider requests:** 0.
**Schema changes:** none — schema stays at v19, 19 migrations, 52 tables. No
migration 020, no edits to `f018`/`f019`.

---

## 1. §A adjudication — is the historical *events* endpoint sufficient evidence
for the reviewed "market is active" predicate, for TARGET ANCHOR CONSTRUCTION?

### VERDICT A — YES, HISTORICAL **EVENTS** IS SUFFICIENT FOR THE TARGET ANCHOR.

This was decided from the repository's own reviewed vocabulary, not from cost.

`..._INDEPENDENT_REVIEW.md` §10 defines the economic-evidence grades:

| Grade | Evidence | Permits | Prohibits |
|---|---|---|---|
| **E0** | market existed, no price | **target anchoring only** | any EV claim |
| **E1** | timestamped single price | no-vig baseline | — |

and §18 states that historical snapshots serve two separable purposes:
**(A) target anchors** and **(B) the no-vig baseline the model must beat.**

`GET /v4/historical/sports/{sport}/events` returns *the events that had odds
available at the requested timestamp*, with each event's **contemporaneous
`commence_time`**. That is precisely **E0**: it evidences that a market existed
at the instant, and it carries no price. The architecture already names E0 as
sufficient for purpose (A) and insufficient for anything economic.

So the events endpoint fully satisfies the anchoring requirement of Repair 4 —
which needs a provider-stamped contemporaneous `commence_time`, not a price —
and satisfies nothing else.

### What this verdict does **not** license

- **Economic backtesting still requires E1.** Nothing changes there. A target
  anchored from E0 evidence is *training-eligible only*, never backtest-eligible,
  exactly as the per-family table already says.
- **No EV, edge, CLV, or no-vig claim** may be derived from an events-only
  snapshot. There is no price in it to derive one from.
- It is **not** a general substitute for `/v4/historical/sports/{sport}/odds`.

### Cost is a consequence, not the reason

The events endpoint costs **1 credit** per request; the historical odds endpoint
costs **10 × markets × regions**. The verdict above was reached from the
evidence-grade definitions and would be unchanged if the prices were reversed.
Recording the number here so the ordering of reasoning is auditable, and because
§K depends on it.

**Architecture was not changed to save credits.** No reviewed requirement was
weakened, no grade was redefined, and E1 remains mandatory wherever it was
mandatory before.

---

## 2. What was built

### 2.1 `OddsApiClient.get_historical_events()` (§B, §H)

`sports_quant/providers/odds_api.py`

- New: `HistoricalAccessError`, `HistoricalEvent`, `HistoricalSnapshot`,
  `get_historical_events()`, `_is_historical_entitlement_failure()`,
  `_parse_historical_snapshot()`.
- **Existing current-odds behaviour is unchanged.** `get_sports`, `get_odds`,
  `get_nba_odds`, `get_mlb_odds`, `fetch_odds_raw` are untouched. No existing
  public method can return historical data; historical access requires the
  explicit new method and returns a distinct type.
- **No fallback to the 10-credit odds endpoint** exists anywhere.
- The API key continues to flow only through the existing `_get_json` path, so
  it is redacted by the existing `build_exchange(secrets=[...])` mechanism. It
  is never placed in a cache key, a report, a test, or a log.

### 2.2 Read-only HTTP policy allow-list

`sports_quant/http_policy.py` — `odds_api_host_rule` now admits
`/v4/historical/sports/{sport}/events`.

The policy correctly **blocked** the new path on first run; that failure was a
genuine finding, not a nuisance. Its priced sibling
`/v4/historical/sports/{sport}/odds` was deliberately **not** added: nothing
implemented reads it, and an allow-list entry with no caller behind it is an
unguarded door.

### 2.3 `sports_quant/retrospective/market_anchor.py` (§C–§G)

Pure, offline, deterministic. Every snapshot arrives through an injected
`SnapshotSource`, so the whole module is exercised without a socket.

**Snapshot timestamp semantics (§C).** The requested date and the provider's
snapshot instant are different clocks and are represented separately
(`requested_at`/`requested_date` versus `timestamp`). The provider answers with
the nearest snapshot at or before the request, so they routinely differ. A
response carrying **no** `timestamp` is **refused**, never defaulted to the
requested date — substituting what was asked for in place of what was answered
is the exact confusion this lane forbids.

**Repair-4 resolver (§D).** `resolve_target_anchor()` implements the reviewed
contract step for step:

1. `S_final` (retrospective start) is a **search hint only**. It selects the
   first snapshot to look at and has no other effect. It is never compared
   against, never returned, and cannot become an anchor even when every
   iteration fails.
2. Query at `floor(S_final − 60 min)`.
3. Read the **contemporaneous** `commence_time` from the snapshot.
4. `T_cut := floor(commence_time_snapshot − 60 min)`; re-query and repeat,
   **bounded to 3 iterations**.
5. Accept only if the event is present, `commence_time` is in the future
   **relative to the snapshot timestamp**, and a snapshot exists.
6. Reject on: no snapshot, event absent, missing `commence_time`, already
   commenced, non-convergence, pre-archive hint, unresolved identity.

Every rejection is a distinct `AnchorOutcome`; none of them yields a `cutoff`.
Resolutions carry `policy_version = "historical-market-anchor-repair4-v1"` and
the full list of instants requested.

**Grid flooring (§E).** `floor_to_snapshot_grid()` floors **downward** in UTC —
always, including for pre-epoch instants — because rounding up would request a
snapshot *later* than the intended cutoff, the one direction that can leak
post-cutoff information. 5-minute grid from 2022-09-18, 10-minute before.
Naive datetimes are refused rather than assumed to be UTC. Sub-second precision
is discarded downward. Integer arithmetic throughout; no float epochs.

**Budget guard (§G).** `RequestBudget` caps **both** requests and credits,
because they are different risks: a request count bounds provider load, a credit
count bounds spend, and one request to a priced endpoint can cost ten or more
credits. `charge()` refuses **before** the request is issued and charges nothing
on refusal. Defaults are the authorized probe ceiling: **≤10 requests,
≤100 credits**. The resolver charges the budget ahead of every snapshot.

**Dry-run planner (§G, §K).** `plan_snapshot_requests()` computes the exact
instants a run would request, deduplicated by bucket, plus the bucket→games
mapping, with no I/O of any kind.

---

## 3. §F — RETAINED BLOCKER: exact historical-event ↔ canonical-game identity

**This is not implemented, by decision.**

An Odds API historical event carries `home_team`/`away_team` as provider display
**names**. It carries no identifier that joins to the audited TEAM-A crosswalk.
The repository's only existing bridge is `sports_quant/matching/sportsbook.py`,
which resolves sportsbook events to canonical games using provider-scoped team
**aliases** plus a `normalized_key` — i.e. name matching.

Name matching is not admissible Lane-R identity evidence, and no reviewed exact
path has been authorized. Wiring the production matcher in would introduce fuzzy
matching into the one lane whose entire value is exactness, and it would do so
below the level at which such a decision should be made.

So the blocker is recorded **in code**: `RefuseNameMatching` is the default
`IdentityResolution` and raises `IdentityUnresolved` with the reason above.
`resolve_target_anchor()` returns `IDENTITY_UNRESOLVED` **before requesting any
snapshot**, so an unauthorized run cannot spend a credit either.

Everything else in this module is complete and tested against an exact-identity
double. **What is missing is one architectural decision, not code.**

---

## 4. §K — Offline NBA 2026-03 request plan (independently recomputed)

Computed by running the real planner over the preserved BALLDONTLIE `/v1/games`
list payloads in `data/f1_nba_2026_03_scratch.db`, opened `immutable=1`.
The number was **not** taken from the preflight and not forced to match it.

| Quantity | Value |
|---|---|
| Games in the preserved list payloads | **239** (0 without `datetime`) |
| Distinct `T−60` 5-minute buckets (first pass) | **160** |
| Credits, first pass (1/request) | **160** |
| Distinct UTC bucket dates | 32 |
| Buckets per date | 5.00 |
| Largest bucket | 4 games |
| Bucket-size histogram | 1→106, 2→33, 3→17, 4→4 |
| First / last bucket | `2026-03-01T17:00Z` / `2026-04-01T02:00Z` |
| Worst case (all 3 iterations, no re-query sharing) | **≤ 638** requests / **≤ 638** credits |

Worst-case requests and credits are the same number here only because the events
endpoint is 1 credit per request. The two are tracked separately regardless,
because that identity does not hold for any other endpoint — which is exactly
why the guard caps both.

The independently recomputed bucket count, **160**, agrees exactly with the
preflight's independently computed 160. The date count differs (32 here versus
31 there) because this counts distinct UTC dates of the *buckets*, five of which
fall on `2026-04-01`; that is a definitional difference, not a discrepancy in
the plan.

### Cost comparison, with the superseded estimate preserved

| Approach | Credits, first pass |
|---|---|
| **Historical ODDS** (superseded estimate) — 160 requests × 10 credits | **1,600** |
| **Historical EVENTS** (this implementation) — 160 requests × 1 credit | **160** |

The 1,600-credit figure is **retained, not deleted**. It remains the correct cost
of the E1 evidence that economic backtesting will still require. This task did
not make backtesting cheaper; it built the E0 anchor capability, which is a
different and narrower thing.

### 160 requests is not a bounded probe

The default guard is 10 requests / 100 credits. A full March pass is 160
requests / 160 credits and therefore **does not fit**, by design. Running one
requires its own explicit authorization with its own cap; the guard will refuse
otherwise, before spending anything.

---

## 5. Tests (§J)

`sports_quant/db/tests/test_historical_market_anchor.py` — **46 offline tests**,
all passing. No socket, no credit, no DB write.

| Group | Cases |
|---|---|
| Snapshot grid | 10 — on-boundary identity, ±1s, microseconds, legacy 10-min grid, the grid-change instant and one second before it, naive refusal, non-UTC conversion, grid-multiple invariant |
| Snapshot timestamp semantics | 7 — provider timestamp preferred over requested date, missing timestamp refused, null data, bare list, non-list data, non-object entry |
| Repair-4 resolution | 12 — converge on iteration 1 and 2, non-convergence bounded at 3, missing snapshot, absent event, missing `commence_time`, already commenced, pre-archive, identity refusal, determinism |
| Budget | 5 — request cap, independent credit cap, refusal costs nothing, negative caps, resolver charges per snapshot |
| Planning | 3 — bucket sharing, budget fit, priced-endpoint cost |
| Client | 9 — endpoint/parse, entitlement refusal, unrelated failures not disguised, key redaction, empty-argument refusal, `RawExchange` shape, absent quota headers, list-shaped response |

### Adversarial cases worth naming

- **Commencement is judged against the snapshot clock, not the request.** The
  provider may answer with a *later* snapshot than requested. A resolver
  comparing `commence_time` against the requested instant would accept a game
  that had already tipped. Explicitly tested.
- **Matching team names do not substitute for a matching id.** A snapshot holds
  exactly the right two teams under a different event id; the result is
  `EVENT_ABSENT`. Any name-based fallback would fail this test.
- **The retrospective hint never becomes the anchor.** With a hint half an hour
  earlier than the contemporaneous start, the circular rule would anchor at
  22:40; the correct rule follows the snapshot to 23:10. Asserted directly.
- **A refused budget charge costs nothing** — a breach cannot itself spend.

---

## 6. §I — Provenance readiness (no second storage system)

`HistoricalSnapshot.exchange` is the **same** `RawExchange` the existing
ingestors already persist through `SqliteRawResponseRepository`. No parallel
record type, no new store, no new table. Asserted by test: endpoint, status,
`received_at` and `elapsed_ns` are present and well-formed, and the API key
appears nowhere in the serialized exchange (only the redacted `apiKey`
parameter *name* survives, which is what keeps the request auditable).

**Nothing was materialized.** No historical snapshot row, no
`target_schedule_anchor` certification, no target population.

---

## 7. Scope — what was deliberately NOT done (§M)

Not done, and not partially done: real historical market snapshots; certified
`target_schedule_anchor` rows; the F1-R target population; enumeration of target
or prior feature rows; rolling statistics; feature values; historical price
fetching; economic backtesting; model training. No live probe was performed. No
Odds API subscription was purchased, inspected, or modified. The configured API
key was never read, printed, or copied.

---

## 8. Validation (§P)

| Check | Result |
|---|---|
| `git diff --check` | clean |
| `ruff check sports_quant` | all checks passed |
| `mypy sports_quant` | success, 250 source files |
| **Full `pytest`** | **3032 passed, 3 skipped** (651 s) |
| New module suite | 46 passed |
| Migration suite | 31 passed (includes v17→v19 and v18→v19) |
| Fresh init | 19 migrations applied, `max_version=19`, **52 tables** — unchanged |
| Zero-network sentinel | **30 guards armed, 8/8 probes blocked**, resolver and planner exercised under guards |
| Secret scan (touched files) | 0 secret-shaped hits; no API key, no key-shaped literal |
| Provider requests / credits | **0 / 0** |

### Protected-artifact integrity — one honest caveat

41 of 42 baselined artifacts are unchanged on every dimension. The single
deviation is the **`-shm` sidecar mtime** of `data/f1_nba_2026_03_scratch.db`.

- The database file itself is **byte-identical** (SHA-256, size and mtime all
  unchanged), as is its `-wal`, and the `-shm` **size** is unchanged.
- `-shm` is SQLite's volatile shared-memory index. Moving its mtime is what a
  `mode=ro` open does; it is not a mutation of evidence.
- Every read this task performed used `immutable=1`, and that was verified
  experimentally **twice** — once on a different corpus and once on this exact
  file — to leave the `-shm` mtime untouched.
- The bump timestamps to the repository's own full test-suite run, not to any
  code introduced here.

Recording it rather than rounding 41/42 up to 42/42: no evidence changed, but
the baseline did not match exactly and saying otherwise would be false.

### Strict-PIT result

**No strict-PIT boundary was crossed, weakened, or bypassed.**

- No `ignore_pit`, `retrospective=True`, `unsafe=True`, or equivalent bypass flag
  was added anywhere. The strict-forward `AsOfReader` is untouched.
- The retrospectively known start `S_final` enters the resolver **only** as a
  search hint. It cannot reach an anchor by any code path: on every rejection
  `cutoff` is `None`, and on acceptance `cutoff` is derived solely from the
  snapshot's contemporaneous `commence_time`. This is asserted directly, with a
  case where the circular rule and the correct rule give different answers.
- Grid flooring is **always downward**, so a resolved cutoff is never later than
  intended — the only direction that could admit post-cutoff information.
- Step 5's "already commenced" test compares against the **snapshot's own
  timestamp**, not the requested instant, because the provider may answer with a
  different snapshot than the one asked for. Asserted adversarially.
- No feature value, rolling statistic, label, or training row was read or
  written, so there was no surface on which leakage could occur.

### Entitlement limitation (§H)

Whether the configured account can actually read `/v4/historical` is **UNKNOWN**
and was deliberately **not** tested — doing so requires a live request. Possession
of an Odds API key does not imply historical entitlement.

The code handles the documented refusal
(`HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN`) as a **terminal**
`HistoricalAccessError` that is **never retried**, because it is a subscription
question rather than a transient error. Unrelated failures are not disguised as
entitlement problems. Both behaviours are tested offline against a mock
transport. No subscription was purchased, inspected, or modified.

### F1-R was NOT executed

Stated explicitly: **F1-R was not started, not partially started, and not
prepared beyond this capability.** No target population was constructed, no
`target_schedule_anchor` row was certified, no real historical market snapshot
was created, no target or prior feature row was enumerated, no rolling statistic
or feature value was computed, no historical price was fetched, no economic
backtest was run, and no model was trained. The bounded live probe was **not**
performed.

## 9. Where F1-R stands now

**Before this task:** blocked because no historical-market anchor *capability*
existed and no preserved evidence could supply a contemporaneous
`commence_time`.

**After this task:** the capability exists, is tested, and is correct offline.
The remaining blocker has moved and narrowed to a single question:

> **How does an Odds API historical event link to a canonical game, exactly,
> without name matching?**

Answering that is an architectural decision. Until it is answered and reviewed,
`resolve_target_anchor()` refuses — costing nothing — and F1-R stays blocked.

---

## 10. Exact next authorization boundary

Nothing further should proceed without one of the following being authorized
explicitly and separately. They are listed in the order the blockers actually
bind; the first is the only one that unblocks anything.

**1. Exact historical-event ↔ canonical-game identity (BLOCKING, zero-network).**
Adjudicate how an Odds API historical event links to a canonical game without
name or alias matching. This is an architecture/adjudication task, not an
implementation one, and it needs no network and no credits. Until it lands,
every other item below is moot, because the resolver refuses before requesting a
snapshot.

**2. Independent adversarial review of this implementation.** Not performed here
and deliberately not self-reviewed. Reviewing this work in the same context that
produced it is not a review.

**3. A bounded live entitlement probe.** Only after (1) and (2). It must carry
its own explicit cap; the built-in guard defaults to **≤10 requests / ≤100
credits** and will refuse before spending. Its purpose is solely to establish
whether the account has historical access — currently UNKNOWN.

**4. A full NBA 2026-03 anchor pass.** **160 requests / 160 credits** first
pass, **≤638 / ≤638** worst case. This does **not** fit the probe guard, by
design, and needs its own authorization with its own cap.

**5. E1 economic evidence.** Separate again. Historical *odds* (with prices) at
**10 × markets × regions** per request — the retained ~1,600-credit first-pass
estimate. Required before any EV, edge, CLV, or no-vig claim. Nothing in this
task moved that requirement.

**F1-R itself remains blocked** and is not authorized by any of the above on its
own.
