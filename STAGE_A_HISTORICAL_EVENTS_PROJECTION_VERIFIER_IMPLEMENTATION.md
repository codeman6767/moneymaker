# Stage-A Historical-Events Projection / Body Verifier

**Starting HEAD:** `d3984d0` (`origin/main` = `d3984d0`, tree clean).
**Schema:** v21 / 21 migrations / 53 tables — **unchanged**.
**Provider requests:** 0. **Credits spent:** 0.

Closes review finding **L1**. No acquisition, no identity, no registry change.

---

## 1. The exact threat

v21 lets a `historical_market_event_observations` row cite **any** same-provider
HTTP-200 `raw_responses` row. The database enforces provider equality and a 200
status, and it cannot go further without parsing the payload — so a row whose
cited response does not contain it is storable, readable, and folded into
`source_corpus_digest` exactly as if genuine.

Storage was never proof. The gap between *"a row exists"* and *"a row is
audit-grade"* had no bridge.

## 2. What was built

`sports_quant/retrospective/historical_events_projection.py`

**A deterministic projection, not a resemblance check.**

```
project_historical_events_response(raw_row) -> HistoricalEventProjection
```

A pure function of one preserved row: no clock, no network, no canonical entity.
It re-derives the **complete** typed observation set the response must yield, or
raises `ProjectionRejected` with an exact `RejectionCode`. It never returns a
partial result.

```
verify_historical_event_projections(conn, raw_response_id) -> VerificationReport
verify_historical_market_event_evidence(conn) -> list[VerificationReport]
```

### Two-way completeness

A row-level check alone leaves a completeness hole: a caller could materialize
the easy events from a snapshot and quietly omit a contradictory one, shrinking
the population a G5 event-id audit depends on. So the verifier asserts **both**
directions:

1. every stored observation citing X is derivable from X;
2. every event X carries is stored.

A missing row fails as loudly as an unexpected one.

### The composite gate

`verify_historical_market_event_evidence` = content-hash integrity **AND**
raw-response projection integrity **AND** projection completeness. Naming the
composition once is what makes it impossible to forget at a call site — evidence
is not "verified" when only one half passed.

With no explicit ids it scans **every** historical-events response in the
database, including ones carrying zero observations, so a response whose events
were never materialized cannot hide by having nothing to compare.

## 3. Raw-response admission — exact, never substring

| Requirement | Enforcement |
|---|---|
| Provider | exactly `the_odds_api` (`THE_ODDS_API_PROVIDER`) |
| Status | exactly 200 |
| Endpoint | **exact membership** in `HISTORICAL_EVENTS_ENDPOINTS` |
| `dateFormat` | exactly `iso` |
| `date` | present, a strict UTC instant |

Endpoint matching is exact-string, and that is deliberate:
`/v4/historical/sports/basketball_nba/events` and
`/v4/sports/basketball_nba/odds` share a lot of text, and a `startswith`/`in`
test is precisely how a current-odds payload would be admitted as historical
evidence. Refused and tested: current odds · historical **odds/prices** ·
another sport · `/v4/sports` · trailing slash · query string · case variant ·
extra path segment · missing leading slash.

`dateFormat` must be `iso` because under `unix` the provider's timestamps mean
something different, and projecting them as ISO instants would silently misread
the evidence.

The redacted `apiKey` placeholder does not participate in projection — proven by
a test that changes it and gets an identical projection.

### A contract detail worth recording

The observation's `provider` is `the_odds_api`, **not** the qualified
`the_odds_api:basketball_nba` the architecture §9a specifies for the *crosswalk*
key. Two reasons, and they agree: the f020 trigger requires the observation's
provider to equal the cited exchange's, and the observation table already has
separate `sport_key` and `namespace_generation` columns, so it does not need
sport folded into the provider string. The crosswalk table has neither column,
which is why qualification belongs there. Different tables, different
qualification needs.

## 4. Wrapper validation

Validated: valid JSON · top-level **object** (a bare list is the current-odds
shape and is refused) · `timestamp` present, strict UTC, **≤ requested bucket** ·
`data` present and a list.

`previous_timestamp` / `next_timestamp`: **absent and explicit null are both
permitted**; malformed is refused; ordering is enforced (`previous <
snapshot < next`, equality refused).

**No grid alignment is required of the provider snapshot.** The live probe
measured the real grid at roughly `:37` seconds past the minute
(`16:50:37`, `16:55:37`, `17:00:38`), so requiring a five-minute wall-clock
boundary would reject genuine evidence. The requested bucket and the provider
snapshot remain distinct concepts throughout.

### Instant normalization, and why it is not a liberty

The provider writes `2026-03-01T16:55:37Z`; v21's triggers require
`2026-03-01T16:55:37.000000Z`. Same instant, and the schema mandates one
canonical spelling precisely so TEXT comparison orders correctly. `canonical_instant`
performs that normalization and refuses anything that is not a real UTC instant —
naive values, offsets, lowercase `z`, hour 24, Feb 30, month 99.

Note the deliberate asymmetry: **timestamps are normalized, team labels never
are.** A label is an opaque string whose bytes are the evidence; an instant is a
quantity with one canonical rendering.

## 5. Event projection — lossless for v21 semantics

Every event yields exactly the v21 columns. `provider_event_id` must satisfy the
reviewed `^[0-9a-f]{32}$` contract with **no normalization into validity**;
`sport_key` must match the endpoint's sport; `home_team_raw`/`away_team_raw` are
preserved **byte-for-byte** — not lowercased, trimmed, Unicode-normalized,
aliased or fuzzy-matched (tested with leading/trailing spaces, mixed case, tabs,
and NFC vs NFD).

`league_id` and `namespace_generation` come from a **source-controlled constant**
keyed by the exact endpoint, not from an inference: the body carries no league,
and guessing one would be the derived identity this lane refuses.

Observations are ordered by their content-derived `observation_id`, so the
provider's ordering inside `data` cannot change the projection.

## 6. Adjudications

### 6a. Snapshot-level fail-closed

**Any event that cannot be losslessly represented rejects the WHOLE snapshot
from typed materialization.** The raw response is always preserved regardless.

The architecture requires every event in an acquired snapshot to be preserved,
because non-target events contribute the G5 audit's detection power. Skipping a
malformed event while materializing its neighbours would silently shrink that
population and produce a typed set that misrepresents the snapshot. No permissive
partial-materialization policy was invented to maximize coverage.

### 6b. Duplicate provider event ids — fail closed, including exact duplicates

Any repeated `provider_event_id` within one snapshot rejects it. The provider
contract does not authorize duplicates, so per §G the projector fails closed.

**Exact duplicates are refused too, not collapsed.** A snapshot that repeats an
event is a provider anomaly, and silently deduplicating it would hide exactly
the kind of id irregularity a G5 audit exists to notice. Tested: byte-identical ·
different home · different away · swapped home/away · different commence_time ·
null vs value · differing only in an ignored field (`sport_title`).

### 6c. `commence_time` — missing key, explicit null, malformed

| Case | Policy |
|---|---|
| Valid UTC instant | normalized and stored |
| **Explicit `null`** | stored as **NULL — real evidence** that the provider knew no start time |
| **Missing key** | **snapshot refused** |
| Malformed | snapshot refused |

Missing key and explicit null are **not collapsed**. An explicit null is the
provider saying *"no start time"*; an absent key is the provider not speaking the
shape this projector understands. Recording the second as the first would store a
claim the provider never made. No `commence_time` is ever manufactured from a
current schedule, final schedule, retrospective start, snapshot timestamp or
receipt clock.

### 6d. Empty success is valid evidence

A 200 with `data: []` projects to an **empty** observation set, and verification
proves that **zero** observations cite it. That is evidence of zero events at
that snapshot — categorically different from a failed request (refused) and from
an unrequested bucket (no row at all, and not this verifier's concern). The
architecture's acquisition-completeness categories stay intact.

## 7. Body/request authority

Nothing caller-supplied is trusted as proof. The body and request are
authoritative; `observation_content_hash` and `observation_id` are **recomputed**
through the existing reviewed v21 machinery and compared, never accepted.

Detected and tested: changed home · changed away · changed commence_time ·
changed snapshot timestamp · changed requested bucket · changed sport ·
changed namespace generation · forged content hash · forged observation id ·
observation citing the wrong response · missing expected row · unexpected extra
row.

## 8. Mutation of the cited evidence

Tested against a scratch database with the append-only guards **deliberately
dropped**, because "that row is normally immutable" is not a test of the
verification logic. After materialization the verifier detects a mutated `body`,
`endpoint`, `http_status`, `provider` and `request_params_json`, and a
non-existent cited response.

## 9. Probe payload replay (§L) — real evidence, on a copy

The sanitized probe-2 payload was still available locally. It was **copied** to a
disposable destination; the protected corpus was untouched and no new provider
request was made.

| Step | Result |
|---|---|
| Projection of the real preserved body | **11 observations** — exactly the 11 events the probe recorded |
| Requested bucket | `2026-03-01T17:00:00.000000Z` |
| Snapshot timestamp | `2026-03-01T16:55:37.000000Z` |
| previous / next | `16:50:37.000000Z` / `17:00:38.000000Z` |
| league / generation | `lg_nba` / `v4` |
| Verify **before** materialization | correctly **rejected** — 11 expected, 0 stored |
| Verify **after** materializing all 11 | **VERIFIED**, 11/11, no failures |
| Tamper one stored team label | **REJECTED**, naming the column, stored value and body value |
| Canonical rows created | **0** |

The disposable copy is **not staged**, and no raw provider payload is committed.

## 10. Validation

| Check | Result |
|---|---|
| New adversarial suite | **136 passed, 1 skipped** |
| `git diff --check` | clean |
| `ruff check .` | all checks passed |
| `mypy .` | success |
| **Full `pytest`** | **3341 passed, 4 skipped** |
| Schema | **v21 / 21 migrations / 53 tables — unchanged** |
| Zero-network guards | 31 armed, 11/11 probes blocked |
| **Provider requests / credits** | **0 / 0** |
| Protected artefacts + 21 migrations | **85 / 85 unchanged** |

Expected values in tests are constructed from the spec independently wherever
possible, rather than calling the production projector to produce both sides.

## 11. Strict-PIT and authority — no regression

`AsOfReader` and `_feature_cutoff` untouched; `pit/asof.py` references neither
the new module nor the observation table; the table remains `_unsupported` in the
PIT registry and is not readable as a Lane-L or Lane-R feature source. No
canonical game created or modified, no runtime name matching, and The Odds API
gained no official-provider authority.

## 12. Scope — what was NOT done

`REGISTERED_LINKING_PROVIDERS` remains **empty** · `ATTESTED_GENERATIONS` and
`PROVIDER_LEAGUES` untouched · no `LINKING_NAMESPACES` · **no linking provider
registered** · **no identity audit** · **no crosswalk** · no event→canonical-game
map · **Stage A was NOT run** · no 160-request execution · no E1 prices · no MLB
or model work · **F1-R was NOT executed**.

### Retained structural blocker, deliberately untouched

`reconstruction_corpus_versions` carries one `source_corpus_digest` and no
provider, so a corpus cannot bind both an official and a linking audit. That
remains the **next architecture/adjudication task**, after this verifier is
independently reviewed. Nothing here presumes its outcome: the verifier operates
on raw responses and observations and needs no corpus digest at all.

## 13. Readiness

**Ready for independent adversarial review.** Not self-reviewed. A reviewer
should attack, at minimum: the exact-endpoint admission set; the
snapshot-level fail-closed and duplicate adjudications; the missing-key vs
explicit-null decision; whether the two-way completeness check can be evaded by
splitting evidence across responses; the instant-normalization asymmetry; and
whether the composite gate can be satisfied while one half is actually failing.

## 14. Exact next authorization boundary

1. **Independent adversarial review of this verifier.**
2. **The corpus-digest structural adjudication** (one digest, two audits).
3. Register the linking namespace with the disjointness tests the architecture
   requires.
4. **Stage-A first-pass acquisition** — 160 requests / ~160 credits, own
   authorization and cap, with this verifier gating materialization.
5. G5 event-id audit → curation with the mandatory S8 counterfactual → review.
6. Target-anchor acquisition → E1.

Independent of all the above: **P1**, the repository-wide `REPLACE` hardening for
the remaining 28 append-only tables.

**F1-R remains blocked.**
