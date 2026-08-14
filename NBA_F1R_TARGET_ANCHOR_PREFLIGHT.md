# NBA F1-R target-anchor preflight / final execution gate

Determines whether the repository and already-preserved evidence contain
everything needed to produce a defensible NBA Lane-R target anchor.

**Starting HEAD: `6da803f`** (= `origin/main`), clean tree, schema v19,
19 migrations, `f018`/`f019` untouched.

> ## VERDICT: F1-R BLOCKED — HISTORICAL MARKET ANCHOR REQUIRED
>
> Target-anchor coverage from preserved evidence is **0 of 239 games (0.0 %)**.
>
> The reviewed contract requires every target's `T_cut` to derive from a
> **historical market snapshot's contemporaneous `commence_time`**. The NBA
> 2026-03 corpus contains **no market evidence of any kind**, and the Odds API
> client implements **no historical endpoint**. Both the evidence and the
> capability are missing.
>
> This is not a coverage shortfall to be worked around. It is the exact
> circularity the architecture's Repair 4 was written to prevent.

**No provider request was made. No credits were spent. No code was changed.**

---

## 1. The authoritative target-anchor contract

`HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` Repair 4 and
`..._INDEPENDENT_REVIEW.md` §6 are authoritative and supersede the original
design, which was **circular** — it anchored on `scheduled_start − 60 min` where
`scheduled_start` is the *retrospectively known final* start.

### Executable contract

| Step | Rule |
|---|---|
| 1 | Take the retrospective official start `S_final` as a **search hint only** |
| 2 | Query the historical snapshot at `S_final − 60 min`, floored to the provider's snapshot grid |
| 3 | Read the **contemporaneous `commence_time`** for that event **from the snapshot** |
| 4 | `T_cut := commence_time_snapshot − 60 min`. If that differs from step 2 by more than one snapshot interval, **re-query** and repeat, **bounded to 3 iterations** |
| 5 | Accept only if, in the final snapshot: the event is **present**, `commence_time` is **in the future relative to the snapshot timestamp**, and the market is **active** |
| 6 | **Reject** if: no market exists at `T_cut`; the event had already commenced at snapshot time; iteration does not converge; or the snapshot's `commence_time` is absent |

Handling: postponed/cancelled → rejected by step 5. Doubleheaders →
disambiguated by official game id **plus** contemporaneous `commence_time`.
Start-time changes / delayed tips → resolved by iteration. Missing market →
training-eligible only, never backtest-eligible.

**The decisive sentence:** *"`commence_time` from the snapshot is the
availability evidence. The retrospective final start is **never** the anchor."*

Corroborated by the per-family table, which classifies **Target schedule
(anchor)** as `VERSIONED_HISTORICAL`, availability *"odds snapshot `<= T_cut`"* —
i.e. a provider-stamped market snapshot, for both leagues.

## 2. Inventory of preserved candidate evidence

Every database in `data/` was scanned for market rows. **Only one contains any.**

| Candidate | What it actually is | Classification |
|---|---|---|
| `corpus.db` sportsbook price snapshots (1 978 rows) | Observed `2026-07-23T00:54 → 01:24` — a ~30-minute **development** capture | `CURRENT_ONLY_MARKET_DATA` |
| `corpus.db` sportsbook events (14) | **All `baseball_mlb`**; commence times 2026-07-22/23; **0 linked to a canonical game** | `NOT_APPLICABLE` + `UNLINKED_TO_CANONICAL_GAME` |
| `corpus.db` Kalshi events/markets (5 / 13) | Created 2026-07-23 — current-state dev capture | `CURRENT_ONLY_MARKET_DATA` |
| BALLDONTLIE `/v1/games` `datetime` | Scheduled tip, **collected 2026-08-04** for March games | `SEARCH_HINT_ONLY` + `RETROSPECTIVELY_OBSERVED_NOT_PIT` |
| `game_schedule_snapshots` (NBA) | August-observed schedule rows | `RETROSPECTIVELY_OBSERVED_NOT_PIT` |
| Canonical `games.scheduled_start` | Derived by the retrospective TEAM-A bootstrap | `RETROSPECTIVELY_OBSERVED_NOT_PIT` |
| `/v1/plays` final wallclock | Completion evidence for a **prior** event | `NOT_APPLICABLE` as a target anchor |

**Zero sportsbook events commence in 2026-03. Zero market rows are linked to any
canonical game. The NBA corpus preserves no odds or Kalshi endpoint at all** —
its only endpoints are `/v1/plays`, `/v1/box_scores`, `/v1/stats`,
`/nba/v1/stats/advanced`, `/v1/lineups`, `/v1/games`, `/v1/games/{id}`.

A schedule downloaded in August for a March game is not a March pregame snapshot,
and none was upgraded on the grounds that it contains the correct game time.

## 3. Historical-market capability as built

### The Odds API

`OddsApiClient` exposes exactly: `get_sports`, `get_odds`, `get_nba_odds`,
`get_mlb_odds`, `fetch_odds_raw`, `aclose`.

Implemented paths: `/v4/sports` and `/v4/sports/{sport_key}/odds` — **current
odds only**.

| Requirement | Status |
|---|---|
| `/v4/historical/...` implemented | **NO** |
| Historical snapshot retrieval | **NO** |
| Historical snapshot timestamp persisted | **NO** (no such call exists) |
| Snapshot `commence_time` preservable | Not as historical evidence |
| Canonical game linking for a historical snapshot | **NO** |
| Credit estimation/cap for the historical endpoint | **NO** |
| No-network dry run for the historical endpoint | **NO** |

### Kalshi

The client covers current market/event state. **No historical target-anchor path
exists**, and none was invented from current market data.

### API-key entitlement

An Odds API key is configured. Its value was **not read and not printed**.
**Possession of a key proves nothing about plan entitlement to
`/v4/historical`.** It was **not** tested against the live API.

## 4. Coverage from preserved evidence (§D)

| Measure | Result |
|---|---|
| NBA 2026-03 target games considered | **239** |
| Market/odds endpoints preserved | **NONE** |
| Games with a valid historical-market anchor | **0** |
| **Coverage** | **0.0 %** |

Not one game can be anchored. There is no partial result to report and nothing to
call a bounded pilot.

## 5. Shortcut and leakage attacks (§E)

Every forbidden anchor path was tested against the actual implementation.
All are refused **structurally**, not by convention.

| Shortcut attempted | Result |
|---|---|
| Canonical `games.scheduled_start` as evidence | `games` is **not** in `SOURCE_EVIDENCE_TABLES` — canonical dimensions are excluded by design |
| `provider_game_references` / `provider_team_references` | **not** admissible evidence in either lane |
| Final-play wallclock as the **target** anchor | `target_schedule_anchor` admits **only** `versioned_snapshot`; `EVENT_DERIVED` is inadmissible → `basis_contradicts_family` |
| Target's own result/plays to infer its start | same refusal, plus the reader's `target_game_self_reference` gate |
| Label (`final_result`) as an anchor | `is_feature = False` |
| Forward-only families (lineups/injuries/rosters) | `forward_only` — unreturnable at any cutoff |
| **August-observed schedule snapshot as a March anchor** | **executed live:** admitted `effective_at = 2026-08-04T22:12:13Z` vs cutoff `2026-03-01T17:00:00Z` → **`not_yet_available`** |
| Rewriting August `observed_at` to March | `raw_responses` and provenance tables are append-only at the **database** level |
| Latest/current sportsbook quote or closing line | no NBA market rows exist; and a current quote carries no historical snapshot instant |
| Market row not linked to the canonical target | all 14 preserved events are `game_id` NULL |
| Fuzzy/nearest-match game identity | identity resolves only through the audited TEAM-A crosswalk |

The August-schedule case is the one a well-intentioned implementer would most
plausibly reach for, and the reader's cutoff gate defeats it on its own.

## 6. Reconciling the authorization wording (§F)

The two statements are **both true and not in conflict**, because they are about
different prerequisites:

* The NBA completion review said *"no **NBA completion-evidence** blocker
  remains"* and that a bounded NBA F1-R *"may be **separately** authorized"*. That
  cleared the **prior-event availability** prerequisite — the `source_event_completed_at`
  problem.
* The project simultaneously records historical odds/market anchoring as
  unimplemented and unauthorized. That is the **target-anchor** prerequisite, a
  different input to the same builder.

F1-R needs **both**. Completion evidence answers *"when did the prior game become
knowable?"*. The target anchor answers *"what instant are we deciding at?"*. The
completion review resolved the first and never spoke to the second; its own
authorization sentence was explicitly conditional on separate authorization.

**Adjudication: VERDICT B — the target anchor remains an unmet prerequisite.**
No wording is superseded, and nothing in the completion review should be read as
clearing it.

## 7. Requirements if historical Odds API access is pursued (§G)

**No request was made.** Stated for a decision, not executed.

| Item | Value |
|---|---|
| Endpoint required | The Odds API `/v4/historical/sports/{sport_key}/odds` (snapshot form) |
| Date range for the bounded NBA pilot | 2026-03-01 → 2026-04-01 UTC (the preserved corpus window) |
| Snapshot cadence | Provider grid; 5-minute for the modern archive (10-minute before 2022-09-18). Archive begins **2020-06-06** |
| Fields required | snapshot timestamp, event id, **`commence_time`**, home/away, market status, and moneyline prices for the no-vig baseline |
| **First-pass calls (independently computed)** | **160** distinct `T−60` 5-minute buckets across 31 dates (**5.16/day**) — one request covers every game sharing a bucket |
| Worst case with the bounded 3-iteration rule | up to **480** requests |
| Credits (repository-recorded basis) | **≈1,600** for the first pass (10 credits/request); up to ~4,800 worst case |
| **Monetary price** | **UNKNOWN.** The repository records credits only and states plainly that *"no subscription was priced and nothing was purchased."* Not guessed here |
| Plan entitlement | **MUST be confirmed by the user.** A configured key proves nothing |
| Implementation before probe? | **YES** — the endpoint, snapshot persistence, canonical linking and credit capping do not exist |
| Hard cap for a first probe | **≤10 requests / ≤100 credits**, single date, dry-run first |
| Response evidence to persist | The full snapshot body in `raw_responses` with provider, endpoint, request params, `requested_at`/`received_at` and content hashes — the same contract the NBA completion evidence uses |
| Canonical linking | Snapshot event → canonical game via the audited TEAM-A crosswalk on official ids, **never** by name or nearest match |
| Review required before F1-R | An independent review of the historical-anchor implementation, as every prior phase received |

My independent bucket count (160, 5.16/day) corroborates the architecture's
measured 5.0/day from an entirely separate derivation.

## 8. Completion-evidence prerequisites (unchanged, §H)

Nothing here regresses accepted work. Any later F1-R must still use only the
**237** accepted payloads, report exclusions **`18447741`** and **`18447742`**,
report the **11** first-date games with no in-corpus prior, treat
`nba-final-play-wallclock-v1` as a **lower bound only**, apply
`prior_event_completion_conservative_v1` (+6 h), run
`verify_completion_certifications()` over its output, and never claim the
final-play wallclock is an official-final time.

**No defect was found in that machinery during this preflight.**

## 9. Proofs

* **Zero network.** 23 guards armed before provider-facing imports; **15/15**
  adversarial probes blocked (DNS ×2, `create_connection`, raw socket, urllib ×2,
  httpx ×3, requests ×2, both provider constructors, `load_settings`,
  `build_readonly_client`). **0 provider requests, 0 credits spent.** The Odds
  API, Kalshi, BALLDONTLIE and MLB StatsAPI were **not** contacted.
* **Protected evidence.** 42/42 artefacts byte-identical; `mtime_ns`, inode and
  **both WAL and SHM sidecars unchanged**.
* **Strict PIT.** `_feature_cutoff` byte-identical (`5d55345b…`). No production
  code was modified — the working tree held no code changes throughout.
* **Schema.** v19, 19 migrations, `f018`/`f019` untouched, no migration added.
* **Secrets.** The Odds API key's presence was checked; its value was never read,
  printed or transmitted.

## 10. Next authorization boundary

F1-R is **blocked**. The smallest safe next task is, in order:

1. **A historical Odds API endpoint architecture + implementation task**
   (design + code + tests, **still zero-network**): implement
   `/v4/historical/...` snapshot retrieval, snapshot-instant persistence,
   canonical linking, credit estimation and hard capping, and an offline dry run.
   No live call.
2. **A user decision** on plan entitlement, since it cannot be established
   without either provider-current information or a live call — and this task
   made neither.
3. **A bounded live capability probe** (≤10 requests / ≤100 credits, one date)
   only after 1 and 2.
4. **An independent review** of that implementation.
5. Only then, a bounded NBA F1-R.

Steps 1 and 3 are separate tasks; the implementation must precede any live probe.

**Unchanged:** MLB endpoint-capability probe, F2, feature engineering, rolling
statistics, model training, calibration, EV/backtesting, recommendations and UI
all remain **UNAUTHORIZED**. Gates G1, G2, G3, G4, G6 unchanged.
