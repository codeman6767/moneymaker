# The Odds API — Historical-Events Entitlement Re-Probe (post-upgrade)

> ## VERDICT: HISTORICAL ENTITLEMENT CONFIRMED
>
> **HTTP 200.** A valid historical snapshot wrapper with 11 NBA events, and the
> real response matched the offline-implemented contract on every checked point.
> The provider charged **exactly 1 credit** (`x-requests-last: 1`), the cost the
> planner predicted.
>
> Entitlement is no longer the binding constraint.

**Starting HEAD:** `e647b40` (`origin/main` = `e647b40`, tree clean).
**Schema:** v21 / 21 migrations / 53 tables — unchanged by this task.

## The two probes, side by side

| | Probe 1 (free plan) | Probe 2 (post-upgrade) |
|---|---|---|
| Bucket | `2026-03-01T17:00:00Z` | **same** `2026-03-01T17:00:00Z` |
| HTTP | 401 | **200** |
| Result | `HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN` | valid snapshot |
| `x-requests-last` | 0 | **1** |
| `x-requests-remaining` | 500 | **19,998** |
| Verdict | NOT AVAILABLE | **CONFIRMED** |

Probe 1's report is preserved unchanged at
`ODDS_API_HISTORICAL_ENTITLEMENT_PROBE.md`. Reusing the identical bucket is what
makes this a clean single-variable comparison: **only the plan changed.**

---

## 1. Authorization and guards

| | |
|---|---|
| Authorized requests | 1 |
| Requests that reached the provider | **1** |
| Retries | **0** |
| Other providers contacted | **none** |
| Blocked host attempts | 0 |

`RequestBudget(max_requests=1, max_credits=1)`, charged **before** the request;
a second charge was proven refused (`BudgetExceeded`) before any transport.

**Guards were validated offline first**, as required, because probe 1's harness
had a bytes-vs-`str` DNS bug. A 12-point offline suite (mock transport, zero
network) proved: non-authorized hosts blocked in both `bytes` and `str` form; the
authorized host recognised from `bytes`; a wrong path blocked on attempt 1; a
second attempt blocked; budget refuses the second charge; and the bucket is the
mandated one. **All 12 passed before the live call**, and the live run recorded
exactly one attempt to the one authorized endpoint.

## 2. The one request

```
GET /v4/historical/sports/basketball_nba/events
    date=2026-03-01T17:00:00Z
    dateFormat=iso
```

No `eventIds`, `regions`, `markets`, `oddsFormat` or any other filter. No
`/sports`, no current odds, no historical odds/prices, no BALLDONTLIE, Kalshi or
MLB StatsAPI.

## 3. Response contract — matched on every checked point

### Wrapper

| Field | Value | Check |
|---|---|---|
| `timestamp` | `2026-03-01T16:55:37Z` | parses ✓ |
| `timestamp <= requested` | 263 s (4.4 min) earlier | **✓** |
| `previous_timestamp` | `2026-03-01T16:50:37Z` | present, handled ✓ |
| `next_timestamp` | `2026-03-01T17:00:38Z` | present, handled ✓ |
| `data` | list | **✓** |
| `requested_date` echoed distinctly | `2026-03-01T17:00:00Z` | **✓** |

### Event population (structure only — no identity inferred)

| Measure | Count |
|---|---|
| Total events returned | **11** |
| Exact lowercase 32-hex ids | **11** |
| Malformed ids | **0** |
| `sport_key == basketball_nba` | **11** |
| Other `sport_key` | **0** |
| Well-formed `commence_time` | **11** |
| Missing/malformed `commence_time` | **0** |
| Both `home_team` and `away_team` present | **11** |
| Commence times strictly after the snapshot instant | **11** |

Commence range `2026-03-01T18:10:00Z … 2026-03-02T02:40:00Z`.

**No team name was mapped to a canonical team. No canonical game identity was
inferred. No final schedule or result evidence was consulted. Repair-4 target
anchoring was not executed.**

### A real contract observation worth recording

The provider's snapshot instants are **not aligned to exact five-minute
wall-clock boundaries**: `16:50:37`, `16:55:37`, `17:00:38` — five-minute
spacing at roughly a `:37`-second phase offset.

This is **not a defect**, and the implementation already handles it correctly,
but it is worth stating plainly because it validates a design decision that
previously rested on reasoning alone:

- We request an exact grid instant; the provider answers with *its* nearest
  snapshot at or before that instant. The two clocks genuinely differ, which is
  precisely why the architecture insisted on separate `requested_at_bucket` and
  `provider_snapshot_timestamp` columns and refused to let one stand in for the
  other. The real response confirms that was necessary, not pedantic.
- Repair-4 convergence compares a floored `cutoff` against a floored `target` —
  both grid instants — so the off-grid snapshot timestamp cannot destabilise it.
  The snapshot instant is used only for the already-commenced check, which is
  exactly where it belongs.
- Receiving a snapshot up to one interval *earlier* than requested is the
  conservative direction: less information, never more.

## 4. Credit accounting

| Header | Value |
|---|---|
| `x-requests-last` | **1** |
| `x-requests-used` | **2** |
| `x-requests-remaining` | **19,998** |

| | |
|---|---|
| Locally planned cost | **1 credit** |
| Provider-reported actual cost | **1 credit** |
| Cost contract | **MATCHED** — not 0, not > 1 |

The one-credit-per-historical-events-request rule the planner assumes is now
confirmed against the real provider, which matters directly for the 160-request
March plan: **160 buckets ≈ 160 credits**, against a reported quota of 20,000
(19,998 remaining + 2 used).

The second used request is not attributable from this probe and is not
speculated about here; only `x-requests-last` describes what *this* request cost.

## 5. Preservation and secret redaction

Preserved through the **existing** `raw_responses` repository path into an
**isolated probe database** in the scratchpad — not under `data/`, not a
protected corpus, **not staged or committed**. 2,255 body bytes preserved
verbatim.

| Proof | Result |
|---|---|
| API key absent from endpoint | **yes** |
| API key absent from stored request params | **yes** (`apiKey: ***REDACTED***`) |
| API key absent from body | **yes** |
| API key absent from response headers | **yes** |
| API key absent from the whole persisted row | **yes** |
| API key absent from the result file and this report | **yes** |
| Endpoint contains no query string | **yes** |
| No `Authorization`/`api-key` header persisted | **yes** |

## 6. No v21 observations were materialized

Deliberately, despite success. In the probe database:
`historical_market_event_observations`, `identity_audit_records`,
`static_crosswalk_provenance`, `reconstruction_corpus_versions`, `games`,
`provider_game_references` and `entity_match_decisions` are **all 0**.
`raw_responses` = 1.

Registries verified unchanged: `REGISTERED_LINKING_PROVIDERS` is **empty**;
`PROVIDER_LEAGUES`, `ATTESTED_GENERATIONS` and `OFFICIAL_PROVIDER_BY_LEAGUE`
contain only the two official providers.

## 7. Stage-A re-materialization eligibility

### **ELIGIBLE FOR LATER STAGE-A RE-MATERIALIZATION, SUBJECT TO STAGE-A PARSER/BODY VERIFICATION**

The requested bucket is the earliest member of the planned NBA March first-pass
set, the response is a successful snapshot, and the sanitized payload is
preserved. That satisfies the architecture's eligibility precondition.

**It is an eligibility statement only. The payload is NOT promoted, and it is NOT
yet audit-grade evidence.**

### L1 is preserved, not bypassed

The v20 independent review retained **L1**: a typed observation can currently
cite an unrelated same-provider HTTP-200 raw response, because the database
cannot check body contents without parsing the payload. A successful probe
response therefore does **not** become trustworthy merely by existing.

Before any observation from this payload may enter the research or audit trust
chain, the Stage-A task must prove the exact projection

> raw response → historical wrapper → event → typed observation

for each row, in addition to `verify_observation_content_hashes`. This probe
neither performs nor pre-empts that verification.

## 8. Scope — what was NOT done

No identity claim of any kind · Stage A **not** run · **no v21 observation rows
created** · no source corpus · no G5 audit · no crosswalk · no linking-provider
registration · no canonical game created or modified · no target anchoring · no
Repair-4 iteration · no second bucket · the 160-request March plan **not**
executed · no E1 historical odds/prices · **F1-R remains unexecuted**.

## 9. Validation

No production code change was required, and none was invented.

| Check | Result |
|---|---|
| Offline guard validation | **12 / 12 passed** before the live call |
| Provider transport attempts | **exactly 1** |
| `ruff check .` | all checks passed |
| `mypy .` | success, 348 source files |
| Focused regression (odds API + anchor + v20 review) | **105 passed** |
| Schema | **v21 / 21 migrations / 53 tables**, unchanged |
| Protected artefacts + all 21 migrations | **85 / 85 unchanged** on sha256, size, mtime, `-wal`, `-shm` |
| Generated DB staged | none |
| Raw payload committed | none |

## 10. Exact next authorization boundary

Entitlement is confirmed and the commercial blocker is cleared. The next task is
**not** Stage A, because two reviewed prerequisites still stand between a
successful probe and audit-grade evidence:

1. **The Stage-A parser/body verifier contract (L1).** Design and implement the
   proof that each typed observation is derivable from the exact cited payload,
   including endpoint family and wrapper timestamp. Without it, acquired rows are
   storage, not evidence. **This is the true next task.**
2. **The corpus-digest structural finding.** A reconstruction corpus carries one
   `source_corpus_digest` and no provider, so it cannot bind both an official and
   a linking audit. Must be adjudicated before Stage-B.

Then, in order: register the linking provider namespace (with the disjointness
tests the architecture requires) → **Stage-A first-pass acquisition** (160
requests / ~160 credits, own authorization and cap) → G5 event-id audit →
curation with the mandatory S8 counterfactual → independent review → target-anchor
acquisition → E1.

Also still open and independent of all the above: **P1**, the repository-wide
`REPLACE` hardening for the remaining 28 append-only tables.

**F1-R remains blocked.**
