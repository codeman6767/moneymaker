# Independent Adversarial Review — NBA Historical Event ↔ Canonical Game Identity

**Object under test:** `NBA_HISTORICAL_EVENT_CANONICAL_IDENTITY_ARCHITECTURE.md`
at `dea1b96`. Treated as a hypothesis, not as authority.

**Starting HEAD:** `dea1b96` (`origin/main` = `dea1b96`, tree clean, schema v19 /
19 migrations). **Provider requests:** 0. **Credits spent:** 0.

---

## Verdicts

| # | Question | Verdict |
|---|---|---|
| 1 | Secondary-provider LINK clause | **ACCEPTED WITH REQUIRED REPAIRS** (4 defects) |
| 2 | `S_final` in offline curation | **ACCEPTED WITH STRONGER CONSTRAINTS** — one real hole found |
| 3 | Schema | **V20 REQUIRED** — but the proposed shape is **wrong in 4 respects** |
| 4 | Entitlement probe | **MAY PRECEDE V20**, with one re-materialization constraint |
| 5 | Overall architecture | **ACCEPTED WITH REPAIRS** |

The architecture's central judgements survive. Its *details* do not: one of its
19 numbered specifications is **impossible as written**, its proposed uniqueness
key **destroys the evidence the audit exists to find**, its completeness contract
has a **hiding channel**, and its account of what stops a secondary provider today
**overstates a code-only guard**.

---

## 1. Adversarial method

28 executable tests in
`sports_quant/db/tests/test_historical_event_identity_review.py`, written to
falsify rather than confirm. Each builds a real v19 database from the real
migrations, seeds canonical NBA and MLB games, and then attacks with **direct
SQL** — bypassing every Python guard — because a claim that survives only when
the caller cooperates is not a defence.

Zero network throughout: 31 guards armed before any provider-facing import,
11/11 adversarial probes blocked. Nothing under `data/` was opened.

---

## 2. Defect D1 — **the flagged hidden blocker is real**

> **Architecture specification #4 is impossible as written and must be struck.**

It requires that a crosswalk's map digest *"matches
`reconstruction_corpus_versions.static_identity_map_digest`"*. That field is
already claimed.

`attestation_map_digest()` takes **no arguments** and digests only the TEAM map
(`"kind": "team_attestation_map"`). `team_crosswalks._require_corpus_provenance`
demands **exact equality** with it:

```python
expected = attestation_map_digest()
if corpus.static_identity_map_digest != expected:
    raise AttestationError(...)
```

So the single field is *definitionally* the team map digest, and there are only
two options, both proven by test:

| Option | Result | Test |
|---|---|---|
| Compose team ⊕ odds into the one field | **TEAM-A crosswalk generation fails closed** | `test_a_composed_two_map_digest_breaks_team_a` |
| Store the team-only digest | **the Odds map is entirely unbound at corpus level** | `test_team_only_digest_leaves_a_second_map_unbound` |

And it cannot be fixed after the fact — `reconstruction_corpus_versions` is
append-only (`test_the_corpus_row_cannot_be_amended_later`).

**REPAIR D1.** Bind the Odds-event map at **row level**, not corpus level. The
mechanism already exists: `record_static_crosswalk(..., attestation_map_digest=)`
is an optional parameter that participates in the row's `semantic_digest`
(verified, `test_row_level_map_binding_does_exist_as_a_mitigation`).

Therefore:

- `static_identity_map_digest` **remains team-only**. It is not extended,
  composed, or reinterpreted. No schema change for this.
- Every Odds-event crosswalk row passes the **event-map digest** at write time,
  making a row built under map *M* cryptographically distinct from one under *M'*.
- `provenance_policy_version` must **name the map** (e.g. `odds-event-link-v1`)
  so a verifier knows which committed map to re-derive. Without this the row
  carries a digest nobody can recompute.
- The CI verifier re-derives the committed event map from source and checks every
  row, exactly as the crosswalk review already requires for TEAM-A.

This is strictly better than what the architecture proposed: it binds *per row*
rather than per corpus, so two maps coexist with no collision and no overwrite.

---

## 3. Defect D2 — the "current hard stop" is code-only

The architecture says `ATTESTED_GENERATIONS` having no sportsbook entry is *"the
current hard stop, and it is fail-closed by design."* Half true, and the half it
gets wrong matters.

`ProviderNamespace.verified` is indeed `False` for the Odds API
(`test_an_odds_api_namespace_is_unverified_in_code`), and the honest path is
closed: an audit with `namespace_verified = 0` cannot be `accepted`
(`test_an_unverified_audit_still_cannot_be_accepted`).

But **the database has no idea which generations are attested**.
`ida_accepted_is_clean` only checks `namespace_verified = 1` — a *caller-asserted
integer*. A direct INSERT that simply asserts `1` mints an ACCEPTED audit for a
sportsbook namespace (`test_direct_sql_can_forge_an_accepted_secondary_provider_audit`),
and that forged audit is then sufficient to write a real event→game crosswalk
(`test_a_forged_audit_admits_a_secondary_provider_game_crosswalk`).

**REPAIR D2.** State the guard honestly as **code-only**, and add a CI verifier
assertion: no `identity_audit_records` row may name a provider/generation outside
the union of the attested and linking registries. The registries are the only
place this is enforced, so they must be checked, not assumed.

---

## 4. Defect D3 — `provider_id` has no format contract

v19 checks only `TRIM(provider_id) <> ''`. Five lookalike variants of a real
event id were each admitted **as a distinct key**, coexisting with the genuine
row and pointing at a *different* game
(`test_a_lookalike_event_id_is_admitted_as_a_distinct_key`, 5 parameterizations):

case-flipped (`BE25EB…`) · Cyrillic small letter IE (U+0435) · leading space
(U+0020) · zero-width space (U+200B) · fullwidth latin small e (U+FF45)

They do not collide, so nothing rejects them; they simply both exist, and one is
wrong. The unique constraint is a *string* constraint, and the architecture
relies on it for "one event id → one game".

**REPAIR D3.** The linking namespace must carry an explicit id format contract —
`^[0-9a-f]{32}$` for The Odds API — enforced at the repository boundary on both
the observation write and the crosswalk write. Ids are stored **byte-for-byte
with no normalization** (the architecture is right about that), which is exactly
why the *format* must be validated instead. The curation map must additionally be
scanned for keys that differ only by case, whitespace or confusable codepoints.

---

## 5. Defect D4 — many-to-one is invisible to the schema

Confirmed as designed in both directions:

- one event id → two games is **structurally impossible**
  (`test_one_event_id_cannot_bind_two_canonical_games`)
- two event ids → one game is **permitted**
  (`test_two_event_ids_may_bind_one_canonical_game`)

The asymmetry is correct — a reschedule can re-issue an id. But the schema
surfaces nothing: two rows sit there silently. The architecture says these must
be "counted and individually reviewed"; the test confirms **the database will not
help**, so the counter is the *only* control and must be mandatory rather than
advisory.

### Defences that held

| Attack | Result | Test |
|---|---|---|
| Link to a nonexistent game | refused | `..._link_to_a_nonexistent_game_is_refused` |
| Link an NBA namespace to an MLB game | refused | `..._cross_league_link_is_refused` |
| Audit over a different source corpus | refused | `..._different_source_corpus_is_refused` |
| Cite an accepted audit for another namespace | refused | `..._mismatched_namespace_is_refused` |
| UPDATE / DELETE a crosswalk | refused | `..._are_append_only` |
| Crosswalk mutating canonical game state | **byte-identical `games` row** | `..._cannot_touch_canonical_game_state` |

**Provider authority is genuinely unreachable through the crosswalk.** The link
writes no column of `games`, and `trg_xwk_game_target_valid` requires the game to
pre-exist, so the table is structurally incapable of bootstrapping. The
architecture's central claim here is **upheld**.

---

## 6. Question 1 verdict — LINK clause: **ACCEPTED WITH REQUIRED REPAIRS**

`static_crosswalk_provenance` does mean "provider key resolves to canonical
entity under audit A", and `entity_type='game'` does not collapse two concepts:
the canonical entity genuinely *is* a game, bootstrap authority lives elsewhere
(`games.official_provider` plus the code registries), and the reviewed
architecture already writes one crosswalk row **per game** for the official
provider. A secondary-provider row asserts strictly less than an official one.

Required repairs: **D1, D2, D3, D4.**

### 6a. Namespace atomicity — retained, but the collision rule must be rewritten

The architecture proposed applying G5 namespace-atomicity to the Odds namespace
without asking whether the official-provider *collision rule* transfers. It does
not. `GAME_ID_TWO_DIFFERENT_EVENTS` keys on `(season, home_provider_team_id,
away_provider_team_id)` — and an Odds event carries **no provider team ids**, only
display labels. Applied mechanically, either the rule is inapplicable or label
spelling becomes an identity collision. Both are wrong.

**Minimum rejection criteria for the Odds event namespace (this review's
definition, not the architecture's):**

| Observation | Class |
|---|---|
| Same event id under two `sport_key`s | **BLOCKING collision** |
| Same event id whose labels resolve to two different canonical team *pairs* | **BLOCKING collision** |
| Same event id with home/away **swapped** | **BLOCKING collision** (it changes the identity triple) |
| Same event id, `commence_time` changed | **NOT a collision** — descriptive mutability, the case Repair 4 exists for |
| Same event id, label spelling/punctuation changed but resolving to the same franchise | **NOT a collision** — `name_variance` flag only, and a name *"may never override a stable id"* |
| Same event id, label changed to something **unresolvable** | **WARNING** — a detection-power gap; report, never merge |
| Event disappears then reappears with consistent identity | **NOT a collision** |

On atomicity itself: **retain it.** G5's rationale — a corpus that has proven it
recycles identifiers gives no basis for asserting the un-contradicted ids are
safe — is a property of the *provider*, and applies to a sportsbook as much as to
an official source. The practical hazard the review brief raises is real (one
reused id rejects an otherwise useful March namespace), but partial acceptance
would require exactly the assertion G5 refuses. **Repair:** the audit must report
blast radius, so a human can re-scope the corpus window deliberately rather than
partial-accept silently.

---

## 7. Question 2 — `S_final`: **ACCEPTED WITH STRONGER CONSTRAINTS**

### 7a. Condition 5 does **not** contain the hazard

The architecture leans on condition 5 — the resolver independently verifies the
mapped event is present and pre-commencement at `T_cut`. Condition 5 establishes
**pregame availability**. It does **not** establish **pregame identifiability**,
and the architecture conflates the two.

**The falsifying case.** Two Odds events exist at `T_cut` with the same two
teams — a duplicate publication, a doubleheader, or a replacement event. A
curator holding `S_final` picks the one whose `commence_time` is nearest the
*final* start. A contemporaneous observer at `T_cut` could not have made that
choice. **Condition 5 catches nothing**: both candidates are present and
pre-commencement, so the resolver silently accepts the curated one.

That is `S_final` driving *selection*, not corroboration, and it is precisely the
distinction the brief asked for:

> **IDENTIFIER TRUTH** (event X eventually was game G) ≠ **PREGAME KNOWABILITY**
> (at T, X was identifiable as G).

The architecture's five conditions permit the first to substitute for the second
whenever contemporaneous evidence is ambiguous.

### 7b. The repair: `S_final` may subtract, never select

**S6 — MONOTONE USE.** `S_final` may **reject** a proposed link. It may **never
choose among candidates and never create one.** If two or more candidate events
survive contemporaneous evidence, the target is **excluded as ambiguous**,
whatever `S_final` says. Rejection-only use can only shrink the population, and
shrinkage is visible in the exclusion decomposition; selection is invisible.

**S7 — DETERMINISTIC PROCEDURE.** Curation must be an executable, versioned
procedure that *proposes*, with a human who *approves or rejects*. "Human
judgement" is unfalsifiable and cannot be replayed.

**S8 — COUNTERFACTUAL RE-DERIVATION TEST (the decisive control).** Re-run the
curation procedure with `S_final` **withheld**. The map must be **identical**.
Any entry that differs is an entry `S_final` created, and it must be dropped and
counted. This converts an argument into a check, and it is mandatory before any
map is accepted.

**S9 — SEPARATE COUNTER.** Links rejected *because of* `S_final` are counted and
reported separately, so a reviewer can see how much `S_final` removed.

With S6–S9 the architecture's original five conditions become sufficient. Without
S8 in particular, the claim that `S_final` is information-free is an assertion
rather than a result.

### 7c. Safer evidence, as the brief asks

Contemporaneous home/away labels, contemporaneous `commence_time`, snapshot
chronology and official-provider game identity are together sufficient to
propose a link for the **unambiguous** majority. `S_final` then reduces to (a) a
bucket search hint, already authorized, and (b) a human display aid. Under S6–S9
that is exactly what it is.

---

## 8. Question 3 — schema: **V20 REQUIRED**, proposed shape **repaired**

Each alternative was tested rather than argued.

| Option | Verdict | Evidence |
|---|---|---|
| **A** raw_responses + deterministic projection at audit time | **Rejected** | Preservation is genuine — `raw_responses` is append-only, UPDATE refused (`test_raw_responses_is_append_only_but_untyped`) — and `body_hash` would even give a portable digest. It fails on two other grounds: the audit's *findings* require parsed fields, so if the parse is never persisted, **a parser change silently changes audit results with nothing recording it**; and completeness reconciliation ("which snapshots contained event X") degrades to a full JSON scan, so the §11 counters become unverifiable. Making payload-parsing code part of identity evidence is a larger risk than one table. |
| **B** sportsbook_events | **Falsified by test** | Mutable in place — `UPDATE` of `commence_time` and `home_team_raw` succeeds with no trigger (`test_sportsbook_events_is_mutable`) — and it has **no snapshot-instant column**, only our clocks (`test_sportsbook_events_has_no_snapshot_instant_column`). Fixing both is a migration anyway, and would overload a Lane-L current-state table with Lane-R historical semantics that PIT §4 explicitly warns against. |
| **C** game_schedule_snapshots | **Falsified by test** | `game_ref_id` is NOT NULL referencing `provider_game_references`, so the INSERT fails (`test_game_schedule_snapshots_cannot_hold_an_odds_event`). Writing an Odds event there requires first minting an **official game reference for a sportsbook** — the exact authority a secondary provider must never hold. |
| **D** committed source artifact outside SQL | **Rejected for observations; ADOPTED for the map** | A committed transcription is not evidence: nothing binds it to the raw responses that produced it except a claim. But this *is* the right home for the curated event→game **map**, which is a human artifact and belongs in source control. The architecture already implies this; it is worth stating that D is right for one artifact and wrong for the other. |
| **E** new observation table | **Survives** | — |

### 8a. The proposed shape is wrong in four respects

The architecture proposed: provider · namespace_generation · sport_key ·
provider_event_id · provider_snapshot_timestamp · commence_time · raw home/away ·
raw_response_id · append-only · `UNIQUE(provider, namespace_generation,
provider_event_id, snapshot_timestamp)`.

**R1 — the uniqueness key destroys the evidence the audit exists to find.**
If the provider publishes byte-different content at the same snapshot timestamp,
that key silently drops the second observation — discarding exactly the
contradiction a collision audit looks for. The repository already solved this:
`game_schedule_snapshots` uses `UNIQUE (game_ref_id, observed_at, content_hash)`.
The key must be
`(provider, namespace_generation, provider_event_id, provider_snapshot_timestamp,
observation_content_hash)`.

**R2 — `league_id` is missing.** `source_corpus_digest` is computed per
`(corpus, league, provider)`, and `sources.py` already records
`game_schedule_snapshots` having no `league_id` as an awkwardness to work around.
Do not repeat it.

**R3 — the requested bucket is missing.** The provider answers with the nearest
snapshot at or before the request, so *requested* and *answered* differ
routinely. Without the requested instant it is impossible to prove the declared
request plan was followed, or to detect "the provider answered with a different
snapshot than we asked for" — both §11 proof obligations.

**R4 — `observation_content_hash` is missing.** `raw_response_id` is **DB-local**
and does not survive transport between reconstruction databases. A content hash
over the normalized observation tuple is portable and is what the digest should
bind.

### 8b. Fields deliberately **NOT** added

`event_status` — the Odds API events payload exposes exactly `id`, `sport_key`,
`sport_title`, `commence_time`, `home_team`, `away_team`, `bookmakers`. There is
no status field, so adding one would invent evidence.

`canonical_game_id` — **forbidden at acquisition by construction**. The table has
no such column. Stage A claims no identity, and the cleanest enforcement is that
there is nowhere to put one.

**Absence semantics — no extra table needed.** A row means "the provider reported
this event in this snapshot." Absence is *derived*, and three states are already
distinguishable because `raw_responses` records request params and HTTP status:
bucket never requested (no raw response) · requested and failed (raw response,
non-200) · requested, succeeded, event absent (raw response 200, no observation
row). This must be stated in the contract so absence is never conflated with a
failed request.

**FK on `raw_response_id`:** `raw_responses` is append-only (proven), so deletion
is already impossible; default RESTRICT is correct and needs no cascade.

---

## 9. `source_corpus_digest` — a migration-ordering obligation

`AUDITED_SOURCE_TABLES` is exactly the three official-provider snapshot tables
(`test_the_audited_source_tables_are_official_provider_evidence_only`).
Registering a fourth is **not digest-neutral**: the digest folds one entry per
audited table, so it changes for corpora **that contain no market data at all**
(`test_registering_a_new_audited_table_changes_every_corpus_digest`).

Every existing corpus's audit↔crosswalk binding is keyed on that digest, and
`trg_xwk_audit_corpus_binding` enforces equality. So registration **invalidates
every existing corpus**.

**REPAIR.** This is a stated, planned consequence, not a surprise to discover
during migration: the v20 task must either (a) scope the new table into the
digest only for corpora that declare a market-evidence lane, or (b) accept that
existing corpora must be rebuilt under a new corpus version, and say so. Option
(b) is the honest default given append-only corpus rows. Either way it must be
decided **before** the migration, not after.

Also: the digest should bind **content**, not `raw_response_id`, for the
portability reason in R4.

---

## 10. Completeness — the architecture's contract has a hiding channel

The proposed single reconciliation merges three different questions:

```
N_targets_declared = N_linked + N_unlinked_no_event_observed + N_unlinked_ambiguous
                   + N_unlinked_provider_disagreement + N_unlinked_other
```

`N_unlinked_no_event_observed` cannot distinguish *"the provider had no market"*
from *"we never requested that bucket"* from *"the request failed"* from *"quota
ran out"*. An acquisition failure therefore masquerades as evidence of absence —
which is a **selection channel disguised as a market fact**, and the single most
important thing this contract was supposed to prevent.

**REPAIR — three separate reconciliations, never netted:**

**(1) ACQUISITION**
```
buckets_planned = buckets_requested + buckets_not_requested
buckets_requested = succeeded + failed + quota_blocked + malformed
                  + answered_with_earlier_snapshot
```

**(2) IDENTITY**
```
targets_declared = linked
                 + unlinked_no_event_in_any_acquired_snapshot
                 + unlinked_ambiguous_multiple_candidates
                 + unlinked_provider_disagreement
                 + unlinked_rejected_by_S_final          (S9)
                 + unlinked_other_with_reason
```
plus, reported separately: `targets_with_multiple_event_ids`,
`events_observed_not_linked`, `events_observed_outside_requested_bucket`,
`events_mapping_to_multiple_games` (**must be 0**).

**(3) F1-R ELIGIBILITY** — downstream of identity, never merged into it:
```
linked = anchored
       + no_market_at_T_cut
       + absent_at_final_Repair4_T_cut
       + already_commenced
       + missing_commence_time
       + no_convergence
       + no_completion_evidence            (NBA Lane-R)
       + no_prior_game / first_date
       + postponed / cancelled / rescheduled
```

Identity completeness and F1-R eligibility are different populations. One set of
counters must never be able to absorb an exclusion belonging to another stage.

---

## 11. Cold start and the Stage-A boundary

The 160 first-pass buckets are derivable **before any provider contact** — they
were computed purely from the official corpus's preserved `/v1/games` datetimes,
independently reproduced last task. So the plan can and must be **fixed and
digest-bound before acquisition**.

**Stage A is permitted to request:** exactly the buckets in the declared,
digest-bound first-pass plan. Nothing else.

**Stage A is forbidden from:** Repair-4 iteration; any bucket not in the plan;
any per-event endpoint; the historical *odds* endpoint. Iteration can only be
driven by a contemporaneous `commence_time` read from a snapshot, which is Stage
C reasoning and requires identity to exist. Without this boundary, Stage A could
drift from 160 toward the 638 worst case with no authorization — the brief's
concern, and it is well founded.

Its budget cap must equal the plan size, so overrun is refused rather than
detected afterwards. One bucket legitimately serves many targets (106 buckets
serve 1 game, up to 4 for the largest), which is a property of the grid and needs
no special handling.

Unrequested events returned inside a requested snapshot **must be preserved** —
they are what gives the audit its detection power — and are structurally
prevented from implying target candidacy because the observation table has no
canonical id column.

---

## 12. Question 4 — entitlement probe: **MAY PRECEDE V20**

- **Is v19 `raw_responses` sufficient to preserve the probe honestly?** Yes.
  Proven append-only, and it records endpoint, request params, status, headers,
  body, `requested_at`, `received_at` and `elapsed_ns`. Our two clocks stay
  distinct from the provider's snapshot instant, which lives in the body.
- **Must the payload be discarded from later audit input?** No — re-materializing
  it into the v20 table without a new provider call is exactly what "preserve
  then project" means, provided the row records `raw_response_id`.
- **CONSTRAINT (this review's addition).** The probe's bucket is chosen for
  *capability*, not from the target plan. If its payload silently enters the
  audit corpus it widens the corpus with an unplanned observation, perturbing
  `source_corpus_digest` and the population. So: the probe payload may be
  re-materialized **only if its bucket is also a declared Stage-A plan bucket**;
  otherwise it must be **explicitly excluded and recorded as excluded**.

The architecture's ordering correction is upheld, and is now conditional.

---

## 13. Provider registries — minimum separation

- `OFFICIAL_PROVIDER_BY_LEAGUE`, `PROVIDER_LEAGUES`, `QUALIFIED_PROVIDERS`:
  **unchanged, official-only.** The existing equality test between the first two
  must remain.
- **New `LINKING_NAMESPACES`**: qualified linking namespace → league. Consulted
  only by the observation writer, the audit and the crosswalk writer. **Never** by
  any bootstrap path.
- `ATTESTED_GENERATIONS` **must** gain an Odds entry or no audit can ever be
  accepted. This is safe — `verified` feeds only the audit verdict, while
  bootstrap authority is separately gated — but "safe" must be **tested, not
  assumed**: a required test asserts that adding an entry widens neither
  `OFFICIAL_PROVIDER_BY_LEAGUE` nor `QUALIFIED_PROVIDERS`, and that
  `LINKING_NAMESPACES` is **disjoint** from both.

---

## 14. v20 implementation contract for the next task

**Table:** `historical_market_event_observations`

| Column | Type | Null | Note |
|---|---|---|---|
| `observation_id` | TEXT PK | no | prefix `hme_`, deterministic from the uniqueness tuple |
| `league_id` | TEXT | no | FK `leagues` — R2 |
| `provider` | TEXT | no | qualified linking namespace |
| `namespace_generation` | TEXT | no | |
| `sport_key` | TEXT | no | provider's own value |
| `provider_event_id` | TEXT | no | byte-for-byte; format CHECK per D3 |
| `requested_at_bucket` | TEXT | no | the instant we asked for — R3 |
| `provider_snapshot_timestamp` | TEXT | no | the instant the provider answered |
| `commence_time` | TEXT | yes | contemporaneous; NULL is a real observation |
| `home_team_raw` | TEXT | no | verbatim |
| `away_team_raw` | TEXT | no | verbatim |
| `observation_content_hash` | TEXT | no | over the normalized tuple — R4 |
| `raw_response_id` | TEXT | no | FK `raw_responses`, RESTRICT |
| `observed_at` | TEXT | no | our clock |
| `created_at` | TEXT | no | |

- **UNIQUE** `(provider, namespace_generation, provider_event_id,
  provider_snapshot_timestamp, observation_content_hash)` — **R1**.
- **No `canonical_game_id` column**, ever.
- ISO CHECKs on all four timestamps; id-prefix CHECK; non-empty CHECKs.
- **Append-only triggers** refusing UPDATE and DELETE.
- Register in `_DIGEST_COLUMNS` / `AUDITED_SOURCE_TABLES`, with the §9 corpus
  invalidation decided and documented **before** the migration runs.
- Repository API: `record_observation` (idempotent on the unique key) and
  read-only query helpers. **Forbidden writes:** any UPDATE; any canonical id;
  any write from a non-linking provider.
- Fresh init and v19→v20 upgrade must both produce identical schema; replay
  writes nothing new.
- Protected evidence untouched; existing corpora readable.
- **Tests required:** append-only enforcement; the R1 byte-different-same-instant
  case actually preserving both rows; format CHECK rejecting the five D3
  lookalikes; absence-vs-failure distinguishability; digest determinism over
  randomized insertion order; migration idempotence.

**Explicitly out of scope for the schema task:** no identity map, no live
acquisition, no registry changes, no resolver changes.

---

## 15. Residual limitations

- An event id genuinely reused for two indistinguishable events within one corpus
  is **undetectable**. Bounded by detection power; must be reported, never
  claimed away.
- A one-month audit proves nothing about multi-season stability (G5 §4.8).
- `ACCEPTED` will mean *"no contradiction detected at this policy's detection
  power"* — never *"verified stable"*.
- Entitlement remains **UNKNOWN**.
- The Odds API event-id namespace has one genuine advantage over the official
  game namespace, and it should be claimed only once measured: an event appears
  in **many** snapshots before commencement, so unlike the near-vacuous official
  game audit, real repeat observation is expected. Expected — not yet observed.

---

## 16. Zero-network proof and validation

| Check | Result |
|---|---|
| Guards armed before provider-facing imports | **31** |
| Adversarial probes blocked | **11 / 11** |
| Provider requests | **0** |
| Credits spent | **0** |
| API key | never read, printed or copied |
| New adversarial tests | **28 passed** |
| `ruff check sports_quant` | clean |
| `mypy sports_quant` | clean, 251 files |
| Schema | **v19, 19 migrations — unchanged** |
| Protected artefacts | **64 / 64 unchanged** |
| Strict PIT | untouched — no change to `AsOfReader`, `_feature_cutoff`, or any lane code |
| Production code changed | **none** — this review adds tests and documents only |

---

## 17. Exact next authorization boundary

1. **Apply the architecture repairs** (D1–D4, S6–S9, R1–R4, the three-way
   completeness split, the Stage-A boundary, the probe constraint) into
   `NBA_HISTORICAL_EVENT_CANONICAL_IDENTITY_ARCHITECTURE.md`. Documentation-only.
   *This review is authoritative where the two disagree.*
2. **v20 migration** per §14 — own task, own independent review.
3. **Bounded entitlement probe** — may proceed at v19 in parallel with 1–2, own
   cap (≤10 requests / ≤100 credits), claiming no identity, subject to §12.
4. **Stage-A discovery acquisition** — only after 2, first-pass plan only.
5. **G5 event-id audit** under the §6a rules, then **curation** with the §7b
   counterfactual test, then independent review of both.
6. **Full March target-anchor acquisition** — 160/160 first pass, ≤638/≤638 worst
   case.
7. **E1 economic evidence** — unchanged.

**F1-R remains blocked.**
