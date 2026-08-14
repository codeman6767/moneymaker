# Lane-R event-completion evidence — investigation

Answers the retained blocker recorded by
`RETROSPECTIVE_RESEARCH_READER_INDEPENDENT_REVIEW.md` §6: do the **preserved raw
responses** contain a trustworthy, source-originating instant that can defensibly
populate `source_event_completed_at` for EVENT_DERIVED Lane-R evidence?

**Starting HEAD: `ef72437`** (= `origin/main`), clean tree, schema v19,
19 migrations.

> ## VERDICT: EXISTING EVIDENCE SUFFICIENT FOR NBA ONLY
>
> **NBA 2026-03 — a defensible derived bound exists.** Every one of the 239
> bounded games carries a source-provided per-play UTC instant
> (`plays[].wallclock`). It survived ten adversarial falsification checks with
> zero anomalies.
>
> **SUPERSEDED IN PART (2026-08-13):** those ten checks tested wallclock
> *presence, plausibility and provenance*. They did not test **terminal
> completeness or play-order integrity**. Applying the full fail-closed contract
> during implementation rejected **3 of the 239** payloads for period regression
> along play order, giving **236 usable (98.7 %)**. See
> `NBA_LANE_R_EVENT_COMPLETION_MATERIALIZATION_IMPLEMENTATION.md` §5. It is a **DEFENSIBLE DERIVED BOUND**, not a DIRECT completion
> field, and using it requires **one narrow documented policy decision**.
>
> **MLB 2026-06 — insufficient. New collection required.** No completion
> timestamp exists in any preserved payload, and **play-by-play was never
> collected at all**. The only candidates are display strings — a local
> 12-hour clock with **no timezone marker anywhere in the corpus**, plus a
> duration that explicitly excludes delays. Deriving completion from them would
> mean manufacturing it.
>
> **Schema v19 is sufficient** for the NBA path. No migration, no new
> availability rule.

This task was an investigation. **No production code was changed.**

---

## 1. Evidence inventory

Both corpora were opened read-only through the accepted `immutable=1` path.

### Endpoint families actually preserved

| League | Endpoint family | Payloads | Size |
|---|---|---|---|
| **MLB** | `/teams/{id}/roster` | 798 | 5.29 MB |
| | `/schedule` | 401 | 1.35 MB |
| | `/game/{id}/boxscore` | 400 | 67.57 MB |
| | `/game/{id}/linescore` | 400 | 1.30 MB |
| | **total** | **1 999** | |
| **NBA** | `/v1/plays` | 239 | 58.48 MB |
| | `/v1/box_scores` | 239 | 31.78 MB |
| | `/v1/stats` | 239 | 10.21 MB |
| | `/nba/v1/stats/advanced` | 239 | 8.60 MB |
| | `/v1/lineups` | 239 | 2.31 MB |
| | `/v1/games/{id}` | 239 | 0.21 MB |
| | `/v1/games` | 3 | 0.21 MB |
| | **total** | **1 437** | |

**MLB has no play-by-play endpoint at all.** Confirmed by enumerating every
distinct endpoint in the corpus, not by sampling. This closes the MLB
final-play avenue (§D) before it opens.

### Collection windows — the reason nothing here is contemporaneous

| Corpus | Games described | Payloads received |
|---|---|---|
| MLB 2026-06 | 2026-06-01 → 09-04 | **2026-07-31 → 08-03** |
| NBA 2026-03 | 2026-03-01 → 03-31 | **2026-08-04** |

Every observation postdates its games by **months**. This is what makes
`observed_at` / `received_at` useless as completion evidence, and it is also why
§E (sequential-snapshot bounding) is impossible: there is no non-final → final
transition to observe, because every snapshot was taken long after every game
finished.

## 2. Candidate fields discovered, and their semantics

Fields were harvested by walking every payload structure, then classified from
the payload itself and the ingestion adapters.

| League | Field | Example | Semantics | Class |
|---|---|---|---|---|
| MLB | `gameDate` | `2026-06-01T22:40:00Z` | **Scheduled start** | NOT USABLE |
| MLB | `officialDate` | `2026-06-01` | Official game **date** | NOT USABLE |
| MLB | `status.detailedState` / `statusCode` | `Final` / `F` | Status **label**, no instant | NOT USABLE |
| MLB | `info["First pitch"]` | `6:47 PM.` | Local 12-h clock, **no date, no TZ** | NOT USABLE alone |
| MLB | `info["T"]` | `2:51.` / `3:08 (0:45 delay).` | Elapsed game time, **excludes delays** | NOT USABLE alone |
| MLB | `/linescore` | — | **No timestamp fields at all** | — |
| NBA | `data.datetime` | `2026-03-01T18:00:00.000Z` | **Scheduled tip-off** | NOT USABLE |
| NBA | `data.date` | `2026-03-01` | Game **date** | NOT USABLE |
| NBA | `data.status` / `data.time` | `Final` / `Final` | Status label / display clock | NOT USABLE |
| NBA | **`plays[].wallclock`** | `2026-03-01T20:36:10.000Z` | **Source-provided UTC instant per play** | **CANDIDATE** |

Two candidates were nearly missed by name-based search and were found only by
dumping raw key sets: NBA `wallclock` (matches none of the obvious
completion-like words) and the MLB `info` block (`First pitch`, `T` are *labels
inside a list*, not keys). Name-driven scanning alone would have produced a false
negative for NBA.

## 3. MLB — why the candidates fail

The only arithmetic that could yield completion is
`First pitch + T`. It fails on four independent grounds:

1. **No timezone.** `First pitch` is a bare local 12-hour string. Scanning the
   whole corpus found **zero** timezone markers (`EDT/EST/CDT/.../UTC`) anywhere
   in the boxscore `info` blocks. Resolving it would require inventing a
   venue→timezone policy and applying historical DST rules.
2. **No date.** `6:47 PM.` carries no date; it must be joined to `officialDate`,
   which is itself a local-calendar concept.
3. **`T` excludes delays.** 22 of 395 values carry a *separate* delay annotation
   (`3:08 (0:45 delay).`), proving `T` is elapsed playing time, not wall-clock
   span. Adding it to first pitch would understate completion for exactly the
   games where the gap matters most.
4. **Coverage gap.** 395/400 boxscores carry both fields; 5 carry neither.

Each layer is an assumption. Stacked, they manufacture a completion instant
rather than evidencing one — precisely what this task forbids. **Classification:
NOT USABLE.**

## 4. NBA — `plays[].wallclock`, and ten attempts to falsify it

Population scan over all 239 games (not a sample):

| Measure | Result |
|---|---|
| Games with plays carrying `wallclock` | **239 / 239 (100 %)** |
| Plays missing `wallclock` | **0** |
| Games with non-monotonic `wallclock` in play order | **0** |
| Duplicate `/v1/plays` payloads (conflict risk) | **0** |
| First-play span (last − first `wallclock`) | median **136.9 min**, range 93.3 – 172.7 |

### Adversarial falsification (§I)

| # | Attack | Result |
|---|---|---|
| 1 | Is it just the scheduled start? | **0/239** equal. First play is +10.0 to +73.2 min after scheduled (median +11.1 — normal tip-off delay); last play +129.6 to +225.5 min |
| 2 | Constant / identical across unrelated games? | **0 collisions** across 239 distinct values |
| 3 | Is it really collection time? | **0/239** fall in August; all fall in March |
| 4 | Does it postdate the game by weeks/months? | Offset from game date: **20 games same UTC day, 219 next UTC day** — exactly right for US evening tip-offs crossing midnight UTC |
| 5 | Postponed games | 0 present |
| 6 | Non-final games | 0 present |
| 7 | Overtime games | 9 games, median span **159.5 min** vs 136.9 overall — longer, as physics requires |
| 8 | Implausible durations | **0** outside 60–240 min |
| 9 | Units / naive vs UTC | Explicit `…T20:36:10.000Z`; no epoch-seconds/ms ambiguity |
| 10 | Duplicate payloads disagreeing | 0 duplicates exist |

Check 7 is the strongest single signal: the field's internal structure tracks a
real physical property (overtime games take longer) that a collection artifact or
a constant could not reproduce.

### What it is, and what it is not

`wallclock` on the **final** play (period 4/OT, clock `0.0`) is the source's own
instant for the last recorded game event. It is:

* **not** an explicit official-completion field — no such field exists;
* a **lower bound** on completion: the game cannot have ended before its last play;
* within a very short, unquantified interval of official final.

**Classification: DEFENSIBLE DERIVED BOUND (category 2), not DIRECT.**

**The residual gap is already absorbed.** The existing reviewed rule
`prior_event_completion_conservative_v1` adds **6 hours** to
`source_event_completed_at`. The last-play-to-official-final interval is minutes.
So **no new availability rule and no new safety margin need to be invented** —
which is what would otherwise have made this a design blocker under §D.

What *is* required is one narrow, reviewable policy statement: *the wallclock of
the final recorded play is the completion evidence for an NBA game.* That is a
semantic decision, and it should be made explicitly rather than assumed inside an
implementation.

## 5. Coverage (§F)

| | MLB 2026-06 | NBA 2026-03 |
|---|---|---|
| Bounded games | 400 | 239 |
| **Direct** completion instants | **0** | **0** |
| **Defensible derived bounds** | **0** | **239 (100 %)** |
| No usable completion evidence | **400 (100 %)** | 0 |
| Earliest / latest derived completion | — | 2026-03-01 → 2026-04-01 (UTC) |
| Duplicates / conflicts | — | 0 |
| Malformed / missing candidates | 5 boxscores lack both fields | 0 |

### Prior-game coverage — the number that actually matters for F1-R

EVENT_DERIVED features need **prior** completed games, not target games.

* **228 / 239 NBA games (95.4 %)** fall on a date with at least one earlier
  in-corpus game date.
* **11 games on 2026-03-01** have **no in-corpus prior** — the corpus boundary,
  not an evidence defect.

This is genuinely **partial at the edge**, and a one-month window gives thin
rolling-window depth regardless. It is sufficient for a *bounded pilot* that
reports the 11 edge games as excluded; it is **not** sufficient to claim general
EVENT_DERIVED coverage, and it remains one month, not 3–5 seasons.

## 6. Provenance path if later authorized (§G — specification only)

| Question | Answer |
|---|---|
| Row cited | The preserved `raw_responses` row for the prior game's `/v1/plays` payload. `raw_responses` **is** already on the Lane-R evidence allowlist (`raw_response_id`) |
| Derivation | `max(plays[].wallclock)` for that game — deterministic, reproducible from the cited body |
| `source_event_completed_at` | Set to that derived instant on the certification |
| Destination | **Not** `game_status_history`. Writing a synthetic `final` transition would manufacture an observation that was never made. Citing the raw response directly keeps the claim honest |
| `observed_at` | **Untouched.** The raw response keeps its August `received_at`; append-only triggers prevent any rewrite |
| Availability rule | Existing `prior_event_completion_conservative_v1` (+6 h). No new rule |
| Corpus/source identity | Unchanged — the certification is corpus-scoped as today |
| Schema | **v19 sufficient.** `source_event_completed_at`, `source_evidence_table`, `source_evidence_id` all exist |
| New architectural decision | **One**: that the final play's wallclock is the completion evidence (§4) |

The same-database evidence constraint was already adjudicated as intentional; an
F1-R builder materializing the cited `raw_responses` row into the reconstruction
corpus satisfies it while carrying `observed_at` verbatim. **Not reopened.**

## 7. Zero-network and protected-evidence proof

* 23 guards armed **before** any provider-facing import; **12/12** adversarial
  probes blocked (DNS ×2, `create_connection`, raw socket, urllib, httpx ×2,
  requests, both provider constructors, `load_settings`,
  `build_readonly_client`). `zeronet.TRIPPED == []` in every scan.
* **0 provider requests.** No MLB StatsAPI, BALLDONTLIE, Odds API or Kalshi call.
* Protected artefacts: **42/42 byte-identical**, with `mtime_ns`, inode, and
  **both WAL and SHM sidecars unchanged**. No metadata movement to explain away —
  this investigation ran no full test suite, so the previously documented
  `mode=ro` sidecar effect never occurred.
* All analysis was read-only through `immutable=1`; nothing was copied or written.

## 8. What must be collected for MLB (§H — specified, not performed)

MLB requires **option A: a bounded historical re-collection from an endpoint that
carries per-event timestamps.** The existing MLB StatsAPI adapter already fetches
`/game/{id}/boxscore` and `/game/{id}/linescore`; a play-by-play family was simply
never part of the F1 collection plan. Whether MLB StatsAPI's play-level payloads
carry a wallclock-equivalent **is not established by anything preserved in this
repository**, and this task did not consult the live API.

So the honest statement is: *MLB needs a bounded, separately authorized probe to
determine whether an obtainable endpoint supplies event-level timestamps, before
any re-collection is planned.* No endpoint should be assumed usable, and no data
should be purchased or recommended to force progress.

Option B (forward collection of completion transitions) remains valid for both
leagues going forward and is the only path that yields DIRECT evidence.

## 9. Next authorization boundary

The reader is accepted; this investigation answers its retained blocker
**asymmetrically**, so the next step should be split:

1. **NBA — a narrow, separately authorized policy + materialization task**
   *before* F1-R: record the decision that the final play's wallclock is
   completion evidence, and materialize `source_event_completed_at` for the
   bounded NBA corpus under the path in §6. Schema v19; no new rule.
2. **MLB — a separate bounded endpoint-capability probe** to establish whether
   event-level timestamps are obtainable at all.

**F1-R remains UNAUTHORIZED**, and if later authorized would be **NBA-only and
bounded**, reporting the 11 first-date games as excluded. Historical
odds/market anchoring, F2, production matching, feature engineering, model
training, calibration, backtesting, recommendation output and UI all remain
UNAUTHORIZED. Gates G1, G2, G3, G4, G6 unchanged.

---

**NBA LANE-R EVENT-COMPLETION POLICY + MATERIALIZATION IMPLEMENTED 2026-08-13 —
NOT independently reviewed.**
`NBA_LANE_R_EVENT_COMPLETION_MATERIALIZATION_IMPLEMENTATION.md`. The versioned
policy `nba-final-play-wallclock-v1` records that the final recorded play's
wallclock in the preserved BALLDONTLIE `/v1/plays` payload is accepted as source
event completion evidence — a **lower-bound proxy, NOT an official-final
timestamp**; a test forbids the wording from drifting into an over-claim. It is
bound through the existing v19 `availability_source` field, so **no schema state
was added to hold prose**, and the existing
`prior_event_completion_conservative_v1` (+6 h) rule is reused — **no new
availability rule**. **Schema stays v19**, no migration, `f018`/`f019` untouched.
Real NBA 2026-03 result, recomputed rather than assumed: **236 of 239 payloads
accepted (98.7 %)**, 3 rejected for period regression along play order. The
investigation's 239 counted wallclock *presence*, not terminal completeness. The
three rejects are contiguous game ids on one date (a likely collection-batch
defect); one of them would have passed a terminal-marker-only check, and is
refused because its period sequence is scrambled. Materialization copies the
`raw_responses` row verbatim **including its identifier**, preserves
`requested_at`/`received_at`/`created_at` exactly (the derived March instant is
never written over the August receipt time), is idempotent, fails rather than
overwrites on conflict, never writes to the source, and **never synthesizes a
`game_status_history` row**. The full v19 path was proved end to end on
disposable evidence: admitted at exactly completion + 6 h, exact to the
microsecond. **F1-R was NOT executed** — zero certification rows were produced.
**MLB remains blocked** pending its own endpoint-capability probe. Odds/market
anchoring, F2, production matching, feature engineering, model training,
calibration, backtesting, recommendation output and UI remain UNAUTHORIZED.
G1/G2/G3/G4/G6 unchanged.

---

**NBA COMPLETION MATERIALIZATION INDEPENDENTLY REVIEWED 2026-08-14 — ACCEPTED
WITH REPAIRS.**
`NBA_LANE_R_EVENT_COMPLETION_MATERIALIZATION_INDEPENDENT_REVIEW.md`. Four defects
reproduced and repaired. The most consequential was a **false rejection**: the
period-monotonicity gate discarded real game `18447743`, whose terminal play was
corroborated three independent ways (End Game marker at max order, max wallclock,
score equal to both the payload maximum and the official score). The pagination
hypothesis was disproved — regressions occur *within* 100-play chunks. It is
replaced by **terminal-score corroboration**, which is what actually separates a
truncated feed from a merely disordered one, compared within the payload rather
than against `/v1/games` (real game `18447470` disagrees by 3 points, which says
nothing about when the game ended). Also repaired: a boolean `order` was accepted
because `isinstance(True, int)` is True; and **nothing re-derived a stored
`source_event_completed_at` from its cited evidence** — `availability_source` is a
free-text **locator** by architecture, correctly not digest-bound, so a new
`verify_completion_certifications()` detective control now re-derives every stored
instant. **Real coverage corrected to 237/239 (99.2 %)**, 2 genuine exclusions,
226/237 with an in-corpus prior date. `raw_response_id` preservation adjudicated
**safe** (all-17-column conflict detection; same-id/different-content refused).
Strict PIT unweakened, **schema stays v19**, no migration, no new availability
rule, **0 provider requests**, 42/42 protected artefacts byte-identical. **An
NBA-only bounded F1-R may now be separately authorized**, reporting the 2
exclusions and 11 no-prior games explicitly. MLB, odds/market anchoring, F2,
production matching, feature engineering, model training, calibration,
backtesting, recommendation output and UI remain UNAUTHORIZED. G1/G2/G3/G4/G6
unchanged.
