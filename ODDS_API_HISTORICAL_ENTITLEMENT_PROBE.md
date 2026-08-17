# The Odds API — Historical-Events Entitlement / Capability Probe

> ## VERDICT: HISTORICAL ENTITLEMENT NOT AVAILABLE
>
> The configured account **cannot** read `/v4/historical`. The provider returned
> **HTTP 401** with `error_code: HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN`.
> **No credits were consumed** — the provider reported `x-requests-last: 0`.
>
> Entitlement is no longer UNKNOWN. It is **absent**, and the answer cost
> nothing.

**Starting HEAD:** `d932889` (`origin/main` = `d932889`, tree clean).
**Schema:** v21 / 21 migrations / 53 tables — unchanged by this task.

| | |
|---|---|
| Authorized requests | **1** |
| Requests that reached the provider | **1** |
| Planned credits | **1** |
| **Credits the provider actually charged** | **0** (`x-requests-last: 0`) |
| Retries | **0** |
| Other providers contacted | **none** |

---

## 1. The probe bucket, chosen before any contact

**`2026-03-01T17:00:00Z`**

Selected deterministically as the **earliest bucket in the recomputed first-pass
plan** — not chosen by knowing anything about its provider contents, which would
have been impossible anyway.

The plan was recomputed offline from the preserved BALLDONTLIE `/v1/games` search
hints and reproduced the established figures exactly: **239 games → 160 distinct
`T−60` buckets**. One game shares the chosen bucket. The instant was written to
disk before the network guard was even armed.

Choosing a real first-pass bucket matters for one reason only: the architecture
permits a probe payload to be re-materialized later **only if** its requested
bucket also belongs to the declared Stage-A plan (§8).

## 2. Budget, armed before the network

`RequestBudget(max_requests=1, max_credits=1)`, charged **before** the request
under the 1-credit historical-events planning rule.

| Check | Result |
|---|---|
| After the charge | `requests_used=1, credits_used=1, requests_remaining=0` |
| A second charge | **refused** (`BudgetExceeded`) — proven before the network call |

Two further guards were installed, independent of the budget:

- **DNS allow-list** — any host other than `api.the-odds-api.com` raises.
- **Transport counter** — the *second* transport attempt aborts unconditionally,
  and any path other than the one authorized endpoint aborts.

Recorded: **1 transport attempt**, to
`api.the-odds-api.com/v4/historical/sports/basketball_nba/events`. Zero blocked
host attempts.

## 3. A false start that reached nothing, reported honestly

The first execution of the harness **never contacted the provider**, because of a
bug in my own guard: `httpx`/`anyio` pass the hostname to `getaddrinfo` as
**bytes**, and the guard compared it to a `str`. Every host therefore mismatched,
including the authorized one, so DNS resolution was refused and no TCP
connection, TLS handshake or HTTP request occurred.

Verified independently, offline, before doing anything else:
`getaddrinfo` receives `b'api.the-odds-api.com'`.

So that run consumed **no request and no credit**, and the single authorization
was still intact when the repaired harness ran. The guard failed *closed*, which
is the right direction for a bug of this kind to fail, but it was still a bug and
it is recorded rather than glossed over. The harness now also writes its result
defensively, because the first run additionally crashed while serializing its own
report — a serialization error must never destroy the evidence from a
once-only request.

## 4. The one authorized request

```
GET /v4/historical/sports/basketball_nba/events
    date=2026-03-01T17:00:00Z
    dateFormat=iso
```

No `eventIds` filter. No historical odds/prices endpoint. No current-odds
endpoint. No `/sports` preliminary call. No BALLDONTLIE, MLB or Kalshi request.

| | |
|---|---|
| HTTP status | **401** |
| `error_code` | `HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN` |
| Provider message | *"Historical odds are only available on paid usage plans."* |
| Elapsed | 236 ms |
| Retries | **0** — one attempt means one attempt |

## 5. Entitlement verdict — §E.2

### **HISTORICAL ENTITLEMENT NOT AVAILABLE**

This is the documented plan-entitlement refusal, not an auth failure, not a
transient failure and not a contract mismatch. The request was correctly
authenticated — an invalid key returns a different error — and the account simply
has no historical access.

**One thing the architecture left open is now answered.** It was not certain
whether the cheap 1-credit historical **events** endpoint might be reachable on a
free plan even though the 10-credit historical **odds** endpoint is not. It is
not: the refusal covers the whole `/v4/historical` family. The `E0`-only,
1-credit strategy does not sidestep the paid-plan requirement.

## 6. Contract check: the offline implementation matched reality

Only the failure branch could be exercised, and it matched exactly:

| Implemented behaviour | Real response |
|---|---|
| `_is_historical_entitlement_failure` recognises the documented refusal | **matched** — classified from the `error_code` substring |
| Raised as a terminal `HistoricalAccessError`, never retried | **held** — one attempt, no retry |
| Not disguised as a transient error | **held** |
| Sanitized exchange preserved | **held** (see §7) |

**Not exercised, and therefore still unverified:** the success-path wrapper
(`timestamp`, `previous_timestamp`, `next_timestamp`, `data`), event shape,
event-id format in real data, and the snapshot-timestamp-vs-requested-bucket
relationship. Those remain offline-only claims. Event count, provider snapshot
timestamp and id-format statistics are all **N/A** — no snapshot was returned.

## 7. Preservation and secret redaction

The exchange was preserved through the **existing** `raw_responses` repository
path into an **isolated probe database** in the scratchpad. It is not under
`data/`, is not a protected corpus, and **is not staged or committed**.

| Proof | Result |
|---|---|
| API key absent from the endpoint | **yes** |
| API key absent from stored request params | **yes** — stored as `apiKey: ***REDACTED***` |
| API key absent from the body | **yes** |
| API key absent from response headers | **yes** |
| API key absent from the whole persisted row | **yes** |
| API key absent from this report and the result file | **yes** |
| Endpoint contains no query string | **yes** |
| No `Authorization`/`api-key` header persisted | **yes** |

The parameter *name* survives, which is what keeps the request auditable; only
the secret value is replaced.

**Nothing else was written.** In the probe database: `raw_responses` = 1, and
`historical_market_event_observations`, `identity_audit_records`,
`static_crosswalk_provenance`, `reconstruction_corpus_versions` and `games` are
all **0**.

## 8. Quota and cost

| Header | Value |
|---|---|
| `x-requests-remaining` | **500** |
| `x-requests-used` | **0** |
| `x-requests-last` | **0** |

**`x-requests-last = 0`, so the refused request cost nothing.** It is not `> 1`,
so there is **no cost-contract mismatch** and no reason to stop on that ground.
The account's quota is untouched: 500 remaining, 0 used.

Planned cost was 1 credit; actual was 0. The local planner over-estimated a
*refused* request, which is the safe direction and is not a defect — the 1-credit
rule describes a served response.

No second request was made to clarify billing.

## 9. Stage-A re-materialization status

### **PROBE-ONLY — NOT ELIGIBLE FOR STAGE-A INPUT**

The bucket *is* in the recomputed first-pass plan, so on that criterion it would
qualify. But the response is a **401 entitlement refusal containing no snapshot
and no events**. There is nothing to project into a typed observation, and the
v21 schema correctly refuses to store an observation citing a non-200 response.

This is not a limitation to work around. A failed request must never read as
evidence about a market, which is precisely why that check exists.

## 10. Scope — what was NOT done

No identity claim of any kind was made. Specifically: Stage A was **not** run ·
no historical observation rows were created · no G5 audit was run · no crosswalk
was created · no event was mapped to a canonical game ·
`REGISTERED_LINKING_PROVIDERS`, `ATTESTED_GENERATIONS` and `LINKING_NAMESPACES`
were untouched · `resolve_target_anchor` was not called with real evidence · no
Repair-4 iteration · no second bucket · the 160-request March plan was **not**
executed · **F1-R was not executed** · no E1 prices were fetched · the P1
repository-wide REPLACE repair was **not** broadened into.

## 11. Code change and validation

One narrowly necessary production change, made **before** the request and tested
offline first: `HistoricalAccessError` now carries the sanitized `exchange` and
`status_code`. Without it an entitlement refusal — the most likely and most
informative outcome — would have discarded its own evidence, leaving the single
authorized request with nothing preserved. One offline test covers it, including
that the preserved refusal is still redacted.

| Check | Result |
|---|---|
| `ruff check .` | all checks passed |
| `mypy .` | success, 348 source files |
| Focused regression (odds API + anchor + v20 review suites) | **105 passed** |
| Schema | **v21 / 21 migrations / 53 tables**, unchanged |
| Protected artefacts + all 21 migrations | **85 / 85 unchanged** on sha256, size, mtime, `-wal`, `-shm` |
| Generated DBs staged | none |
| Raw payload committed | none (only the quoted error message) |

## 12. Exact next authorization boundary

Entitlement is the binding constraint now, and it is a **human/commercial
decision, not an engineering one**.

**Nothing further in the historical-market lane can proceed until the account has
a paid usage plan with historical access.** Specifically blocked: Stage-A
discovery acquisition, the G5 event-id audit, curation, the full 160-request
March pass, and E1 price evidence.

Work that remains available and needs no entitlement:

1. **P1 — repository hardening.** Add BEFORE INSERT existence guards to the
   remaining 28 append-only tables carrying the pre-existing REPLACE bypass. Own
   task, evidence and fix pattern already established.
2. **The structural finding from the v20 review** — a reconstruction corpus
   carries one `source_corpus_digest` and cannot bind both an official and a
   linking audit. Adjudication only; needs no network.
3. **The Stage-A parser/body verifier contract (L1)** — design only.

After a plan upgrade, the order is unchanged: re-probe entitlement (one request),
then Stage-A first-pass acquisition, then audit and curation with their reviews.

**F1-R remains blocked.**
