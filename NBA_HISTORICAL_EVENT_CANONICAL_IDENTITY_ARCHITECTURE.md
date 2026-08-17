# NBA Historical Odds-API Event ↔ Canonical Game Identity — Architecture

> ## ⚠ SUPERSEDED IN PART — read the independent review first
>
> `NBA_HISTORICAL_EVENT_CANONICAL_IDENTITY_ARCHITECTURE_INDEPENDENT_REVIEW.md`
> (2026-08-16) is **authoritative wherever it disagrees with this document**. It
> upheld all three verdicts below and the central judgements, and found four
> defects in the details:
>
> - **Specification #4 (§9) is impossible as written and is STRUCK.**
>   `reconstruction_corpus_versions.static_identity_map_digest` is definitionally
>   the TEAM map digest — composing a second map into it fails TEAM-A closed;
>   storing the team-only value leaves the event map unbound. Binding must happen
>   at **row** level via `record_static_crosswalk(attestation_map_digest=...)`,
>   with `provenance_policy_version` naming the map. Proven by test.
> - **The `S_final` conditions are insufficient.** Condition 5 verifies pregame
>   *availability*, not pregame *identifiability*, so `S_final` can still break a
>   tie no contemporaneous observer could have broken. Repairs S6–S9 add
>   monotone (reject-only) use, a deterministic procedure, a mandatory
>   counterfactual re-derivation test, and a separate counter.
> - **The proposed v20 uniqueness key destroys evidence.** It must include an
>   `observation_content_hash`, and the table additionally needs `league_id` and
>   the requested bucket. See the review §8a and §14.
> - **The completeness contract has a hiding channel.** One reconciliation cannot
>   distinguish "no market" from "request failed". Three separate
>   reconciliations are required — acquisition, identity, F1-R eligibility.
>
> Also corrected: the `ATTESTED_GENERATIONS` "hard stop" in §9 is a **code-only**
> guard — direct SQL can forge an accepted secondary-provider audit — and v19
> places **no format contract on `provider_id`**, so case-flipped and
> Unicode-lookalike event ids coexist as distinct keys.

**Verdicts.** Three separate questions, three separate answers:

> ### ARCHITECTURE ACCEPTED — SCHEMA CHANGE REQUIRED BEFORE IMPLEMENTATION
> The crosswalk *representation* fits schema v19 exactly. The **audit input does
> not**: there is no append-only typed table in which a historical market event
> observation can be preserved, so the G5 event-id audit that v19's own triggers
> demand has nothing to read. A migration is a prerequisite, not an optimization.
>
> ### ENTITLEMENT PROBE MAY PRECEDE IDENTITY IMPLEMENTATION
> The bounded probe needs no canonical identity and must not claim any. The
> previous report's ordering was unnecessarily strict and is corrected here.
>
> ### IDENTITY IMPLEMENTATION REQUIRED BEFORE TARGET-ANCHOR ACQUISITION
> Unchanged. No anchor may be acquired or certified until the chosen path is
> implemented and independently reviewed.

**Starting HEAD:** `156baeb` (`origin/main` = `156baeb`, working tree clean,
schema v19 / 19 migrations).
**Provider requests:** 0. **Credits spent:** 0. **Schema changes:** none.
**F1-R:** not executed. **Live probe:** not performed.

---

## 1. The exact blocker

`resolve_target_anchor()` needs a provider event id. `RefuseNameMatching` is the
fail-closed default and returns `IDENTITY_UNRESOLVED` before any snapshot is
requested. Nothing links an Odds API historical event to a canonical NBA game.

**This is not merely a matching problem, and calling it one understates it.**
The reviewed architecture requires a *provenance claim*, not a resolution: a
Lane-R input must be able to prove "*provider id P, in corpus version C, resolves
to canonical entity X under accepted identity audit A*" — the exact sentence
`game_bootstrap.py` already writes for official games (review decision
GAME-PROV-C). A matcher produces an opinion; Lane R requires an attested,
digest-bound, corpus-scoped, replayable statement.

### 1a. A gap in the reviewed architecture itself

`HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md` §6 admits a target only if *"the quote
event maps unambiguously to the official game (**via §5**)"*. But §5, the static
identity contract, opens:

> "A stable **official provider** ID may map to a canonical entity … iff all
> hold: 1. the provider is the league's **designated official** source for that
> entity type (`OFFICIAL_PROVIDER_BY_LEAGUE`); …"

An Odds API event id fails clause 1 by construction. **§6's "via §5" is a
dangling reference**: no reviewed document defines an identity class for a
sportsbook event id. `..._INDEPENDENT_REVIEW.md` §4's entity table covers Game,
Team and Player — there is no sportsbook-event row. G5 is likewise written for
official providers only (*"An **official** provider ID may serve as a Lane-R
static crosswalk only when:"*).

So the blocker is not an oversight in the implementation. It is an **unwritten
clause in the architecture**, and this document proposes it.

## 2. Current identity surfaces (inspected, not assumed)

| Surface | What it actually holds | Usable as Lane-R event identity? |
|---|---|---|
| `static_crosswalk_provenance` | `(corpus, league, provider, namespace_generation, entity_type, provider_id) → canonical_entity_id` + accepted-audit binding, append-only | **Yes, structurally** — see §9 |
| `identity_audit_records` | corpus-scoped G5 verdict; `entity_type IN ('game','team','player')` | Yes, but needs an input table it does not have |
| `sportsbook_events` | `UNIQUE(provider, provider_event_id)`, nullable `game_id`, `match_decision_id`, `orientation`. **Mutable current-state**, upserted, no snapshot instant | **No** |
| `entity_match_decisions` | supports `entity_type='sportsbook_event'`, `score`/`threshold`/`matcher_version` | **No** — and see §7, Option 5 |
| `provider_team_references` | `UNIQUE(provider, provider_team_id)` — needs a provider *team id* | **No** — Odds API events carry no team id |
| `games` | `official_provider`/`official_game_key` under a global unique index | Official provider only |
| `raw_responses` | append-only immutable payload store | Preservation only, not a typed audit path |

**Verified empirically (read-only, `immutable=1`):** the only preserved Odds API
events anywhere are **14 rows in `data/corpus.db`**, all `baseball_mlb`, all with
`game_id IS NULL`, `match_decision_id IS NULL`, `orientation IS NULL`. The
`entity_match_decisions` table is **completely empty — zero rows of any entity
type**. There is nothing to reuse.

### What an Odds API historical EVENTS row actually gives us

`id` (32 lowercase hex), `sport_key`, `sport_title`, `commence_time`,
`home_team`, `away_team` — plus the wrapper's snapshot `timestamp`. That is the
entire field set, confirmed against the preserved payload.

### What canonical NBA game identity requires

`game_id` (prefix `gm_`), `league_id`, `season_id`, `home_team_id`,
`away_team_id`, `official_provider` (`balldontlie:nba:v1`), `official_game_key`.

**Why there is no foreign key:** the two sides share *no identifier*. The Odds
API supplies an opaque event id and two display strings; the canonical side is
keyed on BALLDONTLIE ids. The only overlapping information is **team labels and
a time** — precisely the evidence Lane R refuses to treat as identity.

## 3. TEAM-A principles that TRANSFER

1. **Source-controlled curation followed by exact runtime lookup is legitimate.**
   TEAM-A established that the forbidden thing is *recomputing identity from
   labels at use time*, not *having once used labels offline under review*.
2. **The four-layer resolution contract** (crosswalk architecture §13): exact
   provider key · ACCEPTED audit over **this corpus's exact `source_corpus_digest`**
   · accepted attestation whose map digest matches the corpus version · the
   canonical row. *"No runtime name lookup. No fallback. No nearest match."*
3. **A per-row (per-event) crosswalk is not novel.** `write_game_bootstrap()`
   already writes one `static_crosswalk_provenance` row **per canonical game**.
   The objection "an event is not a timeless franchise" therefore cannot by
   itself disqualify the design — a *game* is not timeless either, and the
   reviewed architecture already crosswalks games.
4. **Identity versus description.** Identity is the id; scheduled start, venue,
   status and reschedule metadata are descriptive and mutable. A reschedule
   updates description and can never mint a second canonical entity.
5. **Namespace qualification (GAME-NAMESPACE-B).** A provider string must carry
   sport and API generation, from a single source-controlled constant, never
   concatenated at a call site.
6. **Honest evidence strength.** `game_bootstrap.py` already states that a clean
   G5 game verdict means *"no contradiction detected", not "game ids verified
   stable"*. The same restraint binds here.

## 4. TEAM-A principles that DO **NOT** transfer

1. **STATIC_IDENTITY does not transfer as written.** ARCH §5 clause 1 requires
   the *designated official provider*. The Odds API is secondary. The code enum
   `AvailabilityBasis.STATIC_IDENTITY` has a looser rationale (*"No timestamp is
   involved, which is precisely what makes it static"*) which an event→game link
   does satisfy — but the **reviewed §5 contract is the binding one**, and it
   excludes secondary providers. Reusing the term without amending §5 would be
   exactly the misuse this task was told to avoid.
2. **The evidence is a display label, not a provider entity id.** TEAM-A attests
   `provider_team_id`. An Odds API event exposes no team id at all. Any label
   involvement is therefore strictly weaker and must be confined to offline
   curation.
3. **Detection power differs in both directions.** The official *game* audit was
   near-vacuous (no game id observed twice). An Odds API event id, by contrast,
   appears in **many** snapshots before commencement, so a genuine collision
   audit is possible — better than the official game audit, and this should be
   stated rather than assumed either way.
4. **Bootstrap authority does not transfer at all.** See §6.

**Classification of the mapping.** It is **event-scoped identity provenance**
expressed as a static crosswalk under a *new, reviewed secondary-provider LINK
clause*. It is **not** a matching decision, **not** a reconstruction
certification, and **not** a new architectural evidence class — the existing
class fits once §5 is amended to distinguish *linking* from *bootstrapping*.

## 5. Provider authority — independently confirmed

`OFFICIAL_PROVIDER_BY_LEAGUE` = `{lg_mlb: mlb_statsapi, lg_nba: balldontlie}`,
derived in `matching/service.py` so the two halves cannot disagree, documented:
*"a sportsbook, Kalshi, an offline import, a manually-supplied string or an
unknown provider never can"* bootstrap. `MatchSportsbookService` holds no game
create path at all.

Confirmed for the chosen design:

- The Odds API may **never** bootstrap a canonical NBA game. Enforced three
  ways: the code registry; the absence of a create path; and, decisively,
  `trg_xwk_game_target_valid`, which refuses any `entity_type='game'` crosswalk
  whose `canonical_entity_id` is not an **already-existing** game in the same
  league. **The crosswalk table is structurally incapable of bootstrapping.**
- A historical event disagreeing with canonical home/away **fails closed** — it
  is a G5 collision, and the policy is namespace-atomic.
- Secondary-provider evidence may never overwrite canonical identity: the
  crosswalk writes no column of `games`.
- Market data may not adjudicate official-game identity. Prices are never read.

## 6. Option-by-option adjudication

### Option 1 — reuse the current sportsbook matcher → **REJECTED**

A correction to the previous report is owed here. The matcher is **not fuzzy**:
`ENTITY_MATCHING.md` states *"Edit-distance matching is not used anywhere in this
design"*, acceptance is structural (exactly one candidate in the first non-empty
tier, never a threshold comparison), it reads no score, price, result or status,
and it cannot create a game. Calling it "fuzzy matching" was imprecise.

It is nevertheless **disqualified for Lane R**, on four independent grounds:

1. **Not reproducible from committed source.** Team resolution reads
   `team_aliases` live, with no as-of bound and no alias-set hash. The documented
   remediation workflow *is* "add an alias row and re-run", so the function that
   produced a decision is not pinned to the state that produced it — only
   `matcher_version` is stored. Lane R requires a digest-bound claim.
2. **It consumes retrospective final schedule values.** Candidates come from
   `games.scheduled_start` within ±90 min, then ±12 h, and `_slate_filter`
   requires `games.game_date_local` equality. Those are the *final* values —
   the precise hindsight Repair 4 exists to eliminate.
3. **It reads mutable current-state.** `sportsbook_events.commence_time` and the
   raw team names are refreshed by re-polls; a later re-poll can move an event
   between the 90-minute and 12-hour tiers.
4. **Replay is not stable.** `_game_association_known` and `_candidate_venue_tz`
   query live `entity_match_decisions` and `venues`, so later-arriving rows can
   change an outcome on re-run.

Rejected. Lane R is not weakened because the matcher already exists.

### Option 2 — precommitted Odds-API team-label attestation → **REJECTED as a runtime mechanism**

Defensible in principle: a frozen, digest-bound `exact label → canonical team id`
map with exact lookup and fail-closed on unseen spelling is categorically
different from live alias resolution. But it is **strictly weaker than TEAM-A**
and must not be equated with it: it is a statement about a *provider display
label*, not a franchise id; labels rename; a label could in principle be reused;
and many-to-one is expected. It also fails to solve the actual problem, because
the runtime key we need is an **event**, not a team.

**Admitted only as offline curation evidence** (§8), never as a runtime lookup.
Were it ever promoted to runtime it would require: exact raw string (no
normalization), provider-scoped, league-scoped, generation-bounded,
source-controlled, digest-bound, independently reviewed, many-to-one permitted,
fail-closed on unseen spelling, and an explicit label-reuse audit. Not proposed.

### Option 3 — snapshot-local exact home/away resolution → **REJECTED**

Fetch a snapshot, convert the snapshot's home/away through an attestation, pick
the unique event whose canonical teams equal the target. The ordering objection
is *not* fatal — see §7, the "identity before fetch" rule is not a PIT invariant.

It is rejected for a different reason: it requires the **label→team map at
runtime**, which reintroduces label-derived identity into the hot path, and it
makes every anchor resolution depend on a second attestation layer that can
fail-open in ways the exact-id path cannot. It buys nothing Option 4 does not
already provide, at higher runtime risk.

### Option 4 — two-stage acquire, then curate an event-id attestation → **CHOSEN**

See §9.

### Option 5 — existing accepted match decisions as attestation input → **REJECTED (empirically)**

- `entity_match_decisions` contains **zero rows**, of any entity type, anywhere.
- The 14 preserved Odds API events are all `baseball_mlb`, none from March 2026,
  none linked to a canonical game.
- Decisions are scoped to exactly one `source_ref = sb_event_id`, whose identity
  is `(provider, provider_event_id)`. **An accepted decision is authoritative
  only for the exact provider event id it adjudicated** and may never be
  broadened to a different id.
- A *current* event id may not be assumed equal to a *historical* one: the
  historical endpoint is a different corpus and nothing establishes the
  correspondence.

Nothing to inherit. Fails closed.

### Option 6 — deterministic derivation → **REJECTED (empirically falsified)**

Odds API event ids in the preserved corpus are **32 lowercase hex characters**,
14/14, charset `[0-9a-f]` — an opaque digest.

Falsification attempt, run offline over all 14 preserved events: **45 candidate
formulations** (9 field combinations × 5 separators) × 3 hash algorithms
(MD5/SHA-1/SHA-256, first 32 hex) against every field the payload exposes
(`sport_key`, `commence_time`, `home_team`, `away_team`, date-only variants, both
orientations). **0 of 14 derived.**

Absence of a formula over 45 attempts does not prove none exists — it could hash
an internal row id or a salt we cannot see. But the burden lies with the claim,
the provider documents no derivation, and **inventing a hash and calling it a
provider id is forbidden**. Rejected; the ids must be observed.

### Option 7 — a new event-identity crosswalk → **PARTIALLY ADOPTED**

The *contract* is right and is what Option 4 uses. A **new table is not needed
for the crosswalk** — see §9. A new table **is** needed for the audit input —
see §10.

## 7. Cold-start analysis

The circularity is real and must not be hidden:

> `resolve_target_anchor()` requires `provider_event_id` **before** it fetches a
> snapshot. The event id exists **only inside** the snapshot it has not fetched.

**Is "identity before fetch" a security invariant?** No. It is not a
point-in-time invariant at all. Fetching the snapshot at `floor(S_final − 60 min)`
leaks nothing: the snapshot is itself contemporaneous evidence, and `S_final` is
already an authorized search hint. The rule's real value is **budget and scope
containment** — it guarantees no credit is spent without an established identity.
That protection is fully delivered by `RequestBudget`, which refuses before
spending, independently of the identity check.

**Conclusion: acquisition and resolution must be split.** The
`IdentityResolution` protocol is correct for *resolution* and stays; it is simply
**not applicable to acquisition**, and no acquisition path may be routed through
`resolve_target_anchor()`.

**How the very first event id is obtained**, without guessing, runtime matching,
budget escape or pretending an unknown id is known: by an explicitly authorized,
separately capped **Stage-A discovery acquisition** that fetches historical
EVENTS snapshots and **claims no canonical identity whatsoever**. It preserves
observations. It asserts nothing about which game any event is.

## 8. Evidence rules for curating a link

Given event `X` = (`event_id`, `home_team`, `away_team`, contemporaneous
`commence_time`, snapshot `timestamp`), establishing `X → canonical_game_id`:

| Evidence | Class |
|---|---|
| Exact raw provider team labels from the snapshot | **Curation evidence only** — offline, frozen, reviewed. Never runtime. Never identity. |
| Contemporaneous `commence_time` from the snapshot | **Curation corroboration**, and separately the *anchor* evidence at runtime. **Not part of the identity key.** |
| Snapshot `timestamp` | Curation scoping evidence; binds which snapshot generation the observation came from |
| Home/away orientation | **Curation corroboration; a mismatch BLOCKS.** Never silently swapped |
| Canonical team names / abbreviations from the source-controlled seed | Curation evidence only, and only via the digest-bound seed |
| Venue / venue-local date from the official corpus | Curation corroboration only |
| Retrospective final start `S_final` | **Curation evidence only, under the five conditions below** |
| `team_aliases` / provider-scoped aliases (live tables) | **FORBIDDEN** — mutable, unhashed, not reproducible from source |
| Final score | **FORBIDDEN** |
| Result / winner | **FORBIDDEN** |
| Final-play wallclock / completion instant | **FORBIDDEN** for identity (irrelevant: it is prior-game completion evidence) |
| Market prices, EV, implied probability | **FORBIDDEN** |

### May `S_final` participate in offline curation? **Yes — conditionally.**

The affirmative case is the architecture's own: *"Knowing 'gamePk 822728 is this
game' is information-free with respect to who won."* A link is a proposition
about identifiers; it encodes no outcome.

The genuine hazard is `POINT_IN_TIME_DATA.md` DQ-PIT-010: *"the mere existence of
the link encodes the future. A postponed game rematched two days later makes the
original row look resolvable when it was not."* Lane L answers this with
`decided_at <= cutoff`. Lane R cannot, because curation is retrospective by
construction.

`S_final` is therefore admitted **only if all five hold**:

1. the curated map is **source-controlled, frozen, digest-bound and independently
   reviewed** before use;
2. runtime **never re-derives it** — exact lookup or `UNRESOLVED`;
3. curation is **exhaustive over the declared target population**, decided
   *before* acquisition, never opportunistic (§11);
4. the **exclusion decomposition is reported** — the review's falsification
   criterion already makes *"inability to decompose exclusions"* a pilot failure;
5. the resolver still **independently verifies** presence and pre-commencement at
   `T_cut`, so a curation error can never manufacture an anchor.

Condition 5 is what contains DQ-PIT-010: a link to an event that did not exist at
`T_cut` yields `EVENT_ABSENT` and a counted rejection, not a usable target.

## 9. Chosen architecture — Option 4

**Stage A (discovery acquisition).** Bounded, separately authorized, hard-capped
by `RequestBudget`. Fetch historical EVENTS snapshots; preserve each into
`raw_responses` **and** into a typed append-only observation table (§10). Claims
no canonical identity. Not routed through `resolve_target_anchor()`.

**Stage B (audit).** Run the G5 identity audit over the acquired corpus for
`entity_type='game'` in the Odds API namespace. Namespace-atomic: any collision
rejects the whole namespace. Records `distinct_ids`, `total_observations`,
`collision_count`, `flagged_count`, detection power, and the exact
`source_corpus_digest`.

**Stage B2 (curation).** Offline, human, reviewed. Produce a source-controlled
map `event_id → canonical_game_id`, digest it, and write
`static_crosswalk_provenance` rows citing the ACCEPTED audit.

**Stage C (runtime).** `resolve_target_anchor()` performs an exact,
corpus-scoped crosswalk lookup. Exact hit or `IDENTITY_UNRESOLVED`. No nearest
match, no fuzzy fallback, no opportunistic alias resolution.

### The 19 required specifications

| # | Specification |
|---|---|
| 1 | **Identity key**: `(corpus_version_id, league_id, provider, namespace_generation, entity_type='game', provider_id)` where `provider = "the_odds_api:basketball_nba"` (one source-controlled constant, never concatenated at a call site), `namespace_generation = "v4"`, `provider_id` = the exact 32-hex event id, **byte-for-byte, never normalized** |
| 2 | **Representation**: `static_crosswalk_provenance`. `canonical_entity_id` = `games.game_id` |
| 3 | **Who may create it**: an offline curation tool run by a human over an already-acquired corpus, writing only from a committed map file. No runtime path may create a crosswalk |
| 4 | **Evidence required**: ACCEPTED G5 audit for this namespace over this corpus's exact `source_corpus_digest`; the canonical game already exists; the committed map contains the exact entry; the map digest matches `reconstruction_corpus_versions.static_identity_map_digest`; contemporaneous `commence_time` and home/away corroborate |
| 5 | **Evidence forbidden**: §8's forbidden rows — scores, results, prices, live alias tables — plus any runtime label lookup |
| 6 | **Scoping**: corpus-version-scoped and snapshot-generation-scoped. A narrower audit never transfers to a wider reconstruction (G5 §4.8), and `trg_xwk_audit_corpus_binding` already enforces the digest equality |
| 7 | **Version/digest binding**: the map digest must participate in the crosswalk's `semantic_digest`, plus a CI verifier that re-derives the map from committed source and checks every row — the three enforcement steps the crosswalk review already required, unchanged |
| 8 | **Runtime lookup**: exact key → canonical game, or `UNRESOLVED`. Nothing else |
| 9 | **Ambiguity**: refuse. Two candidate events for one target → unlinked with reason. One event id resolving to two games → **blocked**, already impossible via `xwk_key_unique` |
| 10 | **Reschedule**: identity is the event id alone. `commence_time` is descriptive; a time change never creates a new identity and is resolved by Repair-4 iteration |
| 11 | **Event-id change**: a rescheduled game acquiring a *new* event id yields two ids → one game. Permitted by the key's asymmetry, but **counted and individually reviewed**, never auto-merged |
| 12 | **Completeness**: §11's counters and invariants |
| 13 | **Replay/idempotence**: append-only table, deterministic map, digest-checked. Replay writes nothing new; a contradicting row is refused, not updated |
| 14 | **Strict-PIT isolation**: Lane-R only. `AsOfReader`, `_feature_cutoff` and the Lane-L matcher are untouched; the crosswalk is never consulted by a feature reader |
| 15 | **Schema v19**: **sufficient for the crosswalk, insufficient for the audit input** — §10 |
| 16 | **`IdentityResolution` must change**: the current signature `(canonical_game_id, sport_key)` is **not corpus-scoped**, but crosswalks are. It must carry the corpus version and the qualified namespace, or it can resolve against the wrong corpus |
| 17 | **Pre-acquisition discovery step**: **required** — §7 |
| 18 | **Entitlement probe may precede identity implementation**: **yes** — §12 |
| 19 | **Independent review before any full March acquisition**: the map, the audit result, the completeness decomposition, and the schema change — each independently |

### Three code registries block this today (code, not schema)

1. `ATTESTED_GENERATIONS` has no `the_odds_api` entry → `namespace_verified = 0`
   → the audit is forced to `rejected_namespace_unverified` → `CHECK
   ida_accepted_is_clean` and `trg_xwk_audit_accepted_and_matching` refuse every
   crosswalk row. **This is the current hard stop, and it is fail-closed by
   design.**
2. `PROVIDER_LEAGUES` is asserted **equal** to `OFFICIAL_PROVIDER_BY_LEAGUE` by
   test. Adding the Odds API there would assert it is official — wrong, and the
   test would rightly fail. A **separate linking-provider registry** is required.
3. `QUALIFIED_PROVIDERS` must remain official-only, because its value is written
   to `games.official_provider`, which the Odds API may never touch.

## 10. Schema-v19 adjudication

**The crosswalk table is adequate and is not being overloaded.** Its contract is
already *link*, not *bootstrap*: `trg_xwk_game_target_valid` requires the game to
pre-exist, and `write_game_bootstrap()` already writes per-game rows for exactly
this purpose. `entity_type='game'` is semantically correct — the canonical entity
*is* a game. Bootstrap authority lives in `games.official_provider` and the code
registries, not in this table, so a secondary-provider row cannot smuggle
authority it does not have.

**The audit input is missing, and this is the blocker.** The identity audit reads
only `AUDITED_SOURCE_TABLES` = `game_schedule_snapshots`,
`provider_team_identity_snapshots`, `provider_player_identity_snapshots`, and
*"`raw_responses` is **not** a primary audit path"*. Of the 52 tables at v19:

- `game_schedule_snapshots` requires `game_ref_id NOT NULL REFERENCES
  provider_game_references` and is documented as *official game identity
  observations*. Writing Odds API events there would misrepresent them as
  official identity evidence.
- `sportsbook_events` is **mutable current-state** — upserted, with
  `updated_at`/`last_observed_at` and **no snapshot-instant column**. PIT §4 says
  so explicitly: *"a current `sportsbook_events` row is mutable current-state,
  not a historical event snapshot"*.
- `sportsbook_price_snapshots` holds prices, not event-identity observations, and
  is E1 evidence we are deliberately not acquiring.

**Minimum schema addition (conceptual only — NOT implemented, no migration
written):** one append-only table of historical market **event** observations,
carrying at minimum the provider, namespace generation, `sport_key`, the exact
`provider_event_id`, the **provider snapshot timestamp**, the contemporaneous
`commence_time`, the raw home/away labels, and `raw_response_id` — with
append-only triggers and a uniqueness key over
`(provider, namespace_generation, provider_event_id, snapshot_timestamp)`. It
must then be added to `_DIGEST_COLUMNS`/`AUDITED_SOURCE_TABLES` so
`source_corpus_digest` covers it.

**Retained prerequisite: schema change required before implementation. No v20 in
this task.**

## 11. Completeness / selection-bias contract

The review already makes this a pass/fail gate: *"Every future experiment must
report both counts and the exclusion decomposition. Evaluating only the
market-matched subset without reporting the differential is prohibited"*, and
*"inability to decompose exclusions fails the pilot"*.

**The declared target population is fixed before acquisition**, from the bounded
official corpus. Curation is exhaustive over it. **239 is this pilot's value, not
a general invariant, and must never be hard-coded as one.**

Required counters, all reported, all reconciling:

```
N_targets_declared
  = N_linked
  + N_unlinked_no_event_observed
  + N_unlinked_ambiguous
  + N_unlinked_provider_disagreement
  + N_unlinked_other (each with an explicit reason)
```

Plus, reported separately and never netted away:

| Counter | Required behaviour |
|---|---|
| `N_snapshots_acquired`, `N_buckets_requested` | reported |
| `N_distinct_event_ids_observed` | reported |
| `N_events_observed_not_linked` | reported (expected non-zero: other games) |
| `N_games_with_multiple_event_ids` | **each individually reviewed** |
| `N_events_mapping_to_multiple_games` | **must be 0; any occurrence blocks** |
| `N_absent_at_T_cut_but_present_elsewhere` | reported as a counted exclusion |
| `N_postponed`, `N_rescheduled` | reported |
| audit `distinct_ids` / `total_observations` / `collision_count` / detection power | reported |

**No game and no event may silently disappear.** An unlinked target is an
explicit row with a reason, never an absence.

Empirical note for this pilot (read-only): all 239 NBA March games are `Final`,
**0 postponed**, and **0 same-two-teams-same-date collisions** — so the
doubleheader hazard does not bind for NBA March 2026. It binds for MLB, and the
rule stands regardless: disambiguation is official game id **plus**
contemporaneous `commence_time`, because id alone was a *proven fail-open defect*
already repaired once.

## 12. Entitlement-probe ordering — correcting the previous report

`NBA_HISTORICAL_MARKET_ANCHOR_CAPABILITY_IMPLEMENTATION.md` §10 ordered the
bounded probe *after* identity implementation and independent review. **That was
unnecessarily strict**, and it preserved an accidental implementation dependency
rather than a real constraint.

A capability/entitlement probe establishes: does this account get a 200 or the
documented plan refusal; what the wrapper shape is; what the provider snapshot
`timestamp` is; what the quota headers say. **None of that requires knowing which
canonical game any event is.** It does not go through `resolve_target_anchor()`,
so `RefuseNameMatching` never fires; `OddsApiClient.get_historical_events()` is
callable directly under `RequestBudget`.

The distinction to hold:

| | Needs canonical identity? | Needs the new table? |
|---|---|---|
| **Capability / entitlement probe** | **No** | No — `raw_responses` exists at v19 |
| **Stage-A discovery acquisition** | No | **Yes** — its output must be auditable |
| **Identity-resolved target-anchor acquisition** | **Yes** | Yes |

So the probe may run at v19 **provided** it claims no identity and its payload is
not later promoted to audit input without the typed table. The document §10
ordering is corrected accordingly.

## 13. Threat model

| Attack | Result |
|---|---|
| Unknown provider event id | **Rejects** — exact key miss → `UNRESOLVED` |
| Wrong sport | **Rejects** — `sport_key` is in the qualified provider constant |
| Wrong league | **Rejects** — `league_id` is in the key; `trg_xwk_game_target_valid` requires same-league |
| Wrong provider | **Rejects** — provider is in the key |
| Altered team labels | **Resolves** — labels are not the key; runtime never reads them |
| Historically renamed team | **Resolves** for identity; a label change is at most a curation-time `name_variance` flag, which *"may never override a stable id"* |
| Same canonical teams, two events | **Manual review** — counted as `N_games_with_multiple_event_ids` |
| One event id, conflicting teams across snapshots | **Rejects the whole namespace** — G5 collision, namespace-atomic |
| Event id reused | **Rejects** if the reuse is detectable in the acquired corpus; **undetectable** if the two uses are indistinguishable — a stated limitation, not a solved problem |
| Event id changes after postponement | **Manual review**; the old id fails at `T_cut` and is a counted exclusion |
| Swapped home/away | **Rejects** — a curation-time block; orientation is never silently swapped |
| Neutral site | **Manual review** — orientation corroboration cannot be assumed |
| Wrong season | **Rejects** — the canonical game carries `season_id`; corroboration fails |
| Wrong corpus | **Rejects** — `corpus_version_id` is in the key |
| Wrong snapshot generation | **Rejects** — `namespace_generation` is in the key |
| Missing attestation | **Rejects** — `UNRESOLVED`, fail closed |
| Stale attestation | **Rejects** — map digest ≠ corpus `static_identity_map_digest` |
| Tampered digest | **Rejects** — CI verifier re-derives the map from committed source |
| Superseded crosswalk | **Rejects** — supersession creates a new corpus version; the old key does not match |
| Direct SQL insertion | **Rejects** — the ACCEPTED-audit, corpus-binding and game-exists triggers all fire on INSERT |
| Partial map / only easy games attested | **Rejects at review** — §11 reconciliation must balance; this is the design's most important guard |
| Current alias-table changes | **Irrelevant** — alias tables are never read |
| Runtime normalizer changes | **Irrelevant** — `provider_id` is stored byte-for-byte, unnormalized |
| Future schedule information | **Rejects** — `commence_time` is not in the key; anchors come from the snapshot |
| Final score / outcome leakage | **Rejects** — never read at any stage |
| Price leakage | **Rejects** — the events endpoint returns no price; the odds endpoint is not on the allow-list |
| **Undetectable residual** | An event id genuinely reused for two indistinguishable events within one corpus. Bounded by detection power and **must be reported, never claimed away** |

## 14. Zero-network proof

| Check | Result |
|---|---|
| Guards armed before any provider-facing import | **31** |
| Adversarial probes blocked (socket, DNS ×3, TLS, httpx GET/POST, subprocess ×2, os.system) | **11 / 11** |
| Provider requests made | **0** |
| Credits spent | **0** |
| API key | never read, printed, or copied |
| Databases opened | read-only, `immutable=1` only |
| Schema | v19, 19 migrations — unchanged |
| Production behaviour | unchanged; this task is documentation-only |

## 15. Exact next authorization boundary

In the order the blockers bind:

1. **Independent adversarial review of this architecture.** Not self-reviewed
   here. Must specifically re-adjudicate: the §5 secondary-provider LINK
   amendment; the `S_final`-in-curation decision (§8); and the schema verdict.
2. **Schema change (v20): the historical market event observation table.** A
   separate bounded task — migration, triggers, `_DIGEST_COLUMNS` registration —
   then its own independent review.
3. **Bounded entitlement probe.** May proceed at v19 **in parallel with 1–2**,
   under its own explicit cap (≤10 requests / ≤100 credits), claiming no
   identity. Entitlement is currently **UNKNOWN**.
4. **Stage-A discovery acquisition.** Only after 2. Own cap, own authorization.
5. **Stage-B G5 event-id audit + Stage-B2 curation**, then independent review of
   the map and the completeness decomposition.
6. **Full NBA March target-anchor acquisition** — 160 requests / 160 credits
   first pass, ≤638 / ≤638 worst case.
7. **E1 economic evidence** — unchanged, ~1,600 credits, required before any EV
   claim.

**F1-R remains blocked** and is authorized by none of the above on its own.
