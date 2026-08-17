# NBA Historical Odds-API Event ↔ Canonical Game Identity — Architecture

> **Status.** Repaired 2026-08-17 to incorporate every finding of
> `NBA_HISTORICAL_EVENT_CANONICAL_IDENTITY_ARCHITECTURE_INDEPENDENT_REVIEW.md`
> (verdict: *ACCEPTED WITH REPAIRS*). **This document is now self-contained and
> implementation-ready** — an implementer needs only this file plus the v20 task
> instructions, and does not have to merge two documents mentally. The
> independent review is preserved unchanged as the authoritative record of *how*
> these repairs were established; where a reader finds any residual conflict, the
> review governs.
>
> Repairs applied: D1 (row-level map binding) · D2 (code-only audit guard) ·
> D3 (event-id format contract) · D4 (many-to-one counter) · S6–S9 (`S_final`) ·
> event-id audit semantics · R1–R4 (v20 shape) · digest blast radius ·
> three-way completeness · Stage-A boundary · probe constraint · threat model.

**Verdicts.** Three separate questions, three separate answers:

> ### ARCHITECTURE ACCEPTED — SCHEMA CHANGE REQUIRED BEFORE IMPLEMENTATION
> The crosswalk *representation* fits schema v19 exactly. The **audit input does
> not**: there is no append-only typed table in which a historical market event
> observation can be preserved, so the G5 event-id audit that v19's own triggers
> demand has nothing to read. A migration is a prerequisite, not an optimization.
>
> ### ENTITLEMENT PROBE MAY PRECEDE IDENTITY IMPLEMENTATION
> The bounded probe needs no canonical identity and must not claim any, subject to
> the re-materialization constraint in §13.
>
> ### IDENTITY IMPLEMENTATION REQUIRED BEFORE TARGET-ANCHOR ACQUISITION
> No anchor may be acquired or certified until the chosen path is implemented and
> independently reviewed.

**Provider requests:** 0. **Credits spent:** 0. **Schema:** v19, 19 migrations,
unchanged. **F1-R:** not executed. **Live probe:** not performed.

---

## 1. The exact blocker

`resolve_target_anchor()` needs a provider event id. `RefuseNameMatching` is the
fail-closed default and returns `IDENTITY_UNRESOLVED` before any snapshot is
requested. Nothing links an Odds API historical event to a canonical NBA game.

**This is not merely a matching problem, and calling it one understates it.**
A Lane-R input must be able to prove "*provider id P, in corpus version C,
resolves to canonical entity X under accepted identity audit A*" — the exact
sentence `game_bootstrap.py` already writes for official games (review decision
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
sportsbook event id. G5 is likewise written for official providers only.

So the blocker is an **unwritten clause in the architecture**, and §5 below
supplies it.

## 2. Current identity surfaces (inspected, not assumed)

| Surface | What it actually holds | Usable as Lane-R event identity? |
|---|---|---|
| `static_crosswalk_provenance` | `(corpus, league, provider, namespace_generation, entity_type, provider_id) → canonical_entity_id` + accepted-audit binding, append-only | **Yes, structurally** — §9 |
| `identity_audit_records` | corpus-scoped G5 verdict; `entity_type IN ('game','team','player')` | Yes, but needs an input table it does not have |
| `sportsbook_events` | `UNIQUE(provider, provider_event_id)`, nullable `game_id`. **Mutable current-state**, upserted, no snapshot instant | **No** |
| `entity_match_decisions` | supports `entity_type='sportsbook_event'`, `score`/`threshold` | **No** — §6, Option 5 |
| `provider_team_references` | `UNIQUE(provider, provider_team_id)` — needs a provider *team id* | **No** — Odds events carry no team id |
| `games` | `official_provider`/`official_game_key` under a global unique index | Official provider only |
| `raw_responses` | append-only immutable payload store | Preservation only, not a typed audit path |

**Verified empirically (read-only, `immutable=1`):** the only preserved Odds API
events anywhere are **14 rows in `data/corpus.db`**, all `baseball_mlb`, all with
`game_id IS NULL`. `entity_match_decisions` is **completely empty**. Nothing to
reuse.

**What an Odds API historical EVENTS row gives:** `id` (32 lowercase hex),
`sport_key`, `sport_title`, `commence_time`, `home_team`, `away_team` — plus the
wrapper's snapshot `timestamp`. That is the entire field set.

**What canonical NBA game identity requires:** `game_id` (`gm_` prefix),
`league_id`, `season_id`, `home_team_id`, `away_team_id`, `official_provider`
(`balldontlie:nba:v1`), `official_game_key`.

**Why there is no foreign key:** the two sides share *no identifier*. The only
overlapping information is **team labels and a time** — precisely the evidence
Lane R refuses to treat as identity.

## 3. TEAM-A principles that TRANSFER

1. **Source-controlled curation followed by exact runtime lookup is legitimate.**
   The forbidden thing is *recomputing identity from labels at use time*, not
   *having once used labels offline under review*.
2. **The four-layer resolution contract**: exact provider key · ACCEPTED audit
   over this corpus's exact `source_corpus_digest` · accepted attestation bound
   to its committed map · the canonical row. *"No runtime name lookup. No
   fallback. No nearest match."*
3. **A per-row (per-event) crosswalk is not novel.** `write_game_bootstrap()`
   already writes one `static_crosswalk_provenance` row **per canonical game**.
4. **Identity versus description.** Identity is the id; scheduled start, venue,
   status and reschedule metadata are descriptive and mutable.
5. **Namespace qualification (GAME-NAMESPACE-B).** A provider string carries sport
   and API generation, from a single source-controlled constant.
6. **Honest evidence strength.** A clean G5 verdict means *"no contradiction
   detected"*, never *"ids verified stable"*.

## 4. TEAM-A principles that DO **NOT** transfer

1. **STATIC_IDENTITY does not transfer as written.** ARCH §5 clause 1 requires the
   designated official provider. Reusing the term without amending §5 would be a
   misuse.
2. **The evidence is a display label, not a provider entity id.** TEAM-A attests
   `provider_team_id`; an Odds event exposes no team id at all. Label involvement
   is strictly weaker and confined to offline curation.
3. **Detection power differs.** The official *game* audit was near-vacuous (no
   game id observed twice). An Odds event id appears in **many** snapshots before
   commencement, so a genuine collision audit is expected to be possible — a
   claim to be *measured*, not assumed.
4. **Bootstrap authority does not transfer at all.** §5.

**Classification.** **Event-scoped identity provenance** expressed as a static
crosswalk under the secondary-provider LINK clause below. Not a matching
decision, not a reconstruction certification, not a new evidence class.

## 5. Provider authority and the secondary-provider LINK clause

`OFFICIAL_PROVIDER_BY_LEAGUE` = `{lg_mlb: mlb_statsapi, lg_nba: balldontlie}`,
documented: *"a sportsbook, Kalshi, an offline import, a manually-supplied string
or an unknown provider never can"* bootstrap. **BALLDONTLIE remains the
designated official NBA provider.**

### The Odds API MAY

- supply historical market **event observations**;
- undergo an identity-stability audit over an acquired corpus;
- **link** an exact provider event id to an **already-existing** canonical NBA
  game via reviewed crosswalk provenance.

### The Odds API MAY NOT

- bootstrap a canonical game;
- set or alter `games.official_provider`;
- create an official provider game reference merely to make a schedule-snapshot
  write succeed;
- overwrite home/away, season, or any canonical schedule state;
- resolve a canonical disagreement using price or market-outcome evidence.

Enforced three ways: the code registries (§9a); the absence of any create path in
`MatchSportsbookService`; and, decisively, `trg_xwk_game_target_valid`, which
refuses any `entity_type='game'` crosswalk whose `canonical_entity_id` is not an
already-existing game in the same league. **The crosswalk table is structurally
incapable of bootstrapping** — proven by test, and a link leaves the `games` row
byte-identical.

A historical event that disagrees with canonical home/away **fails closed** — see
§8 for exactly which disagreements are identity collisions and which are
descriptive mutability.

## 6. Option-by-option adjudication

### Option 1 — reuse the current sportsbook matcher → **REJECTED**

The matcher is **not fuzzy**: *"Edit-distance matching is not used anywhere in
this design"*, acceptance is structural, and it reads no score, price, result or
status. It is nevertheless disqualified for Lane R on four grounds:

1. **Not reproducible from committed source.** Team resolution reads
   `team_aliases` live, with no as-of bound and no alias-set hash; the documented
   remediation workflow *is* "add an alias row and re-run".
2. **It consumes retrospective final schedule values** — `games.scheduled_start`
   ±90 min then ±12 h, plus `game_date_local` equality.
3. **It reads mutable current-state** `sportsbook_events` columns.
4. **Replay is not stable** — later-arriving decisions and venues change outcomes.

### Option 2 — precommitted team-label attestation → **REJECTED as runtime**

Strictly weaker than TEAM-A (a statement about a display label, not a franchise
id) and it does not solve the problem, because the runtime key needed is an
**event**. Admitted only as offline curation evidence (§7).

### Option 3 — snapshot-local exact home/away resolution → **REJECTED**

Requires the label→team map **at runtime**, reintroducing label-derived identity
into the hot path. Buys nothing Option 4 does not, at higher risk.

### Option 4 — two-stage acquire, then curate an event-id attestation → **CHOSEN**

§9.

### Option 5 — existing accepted match decisions → **REJECTED (empirically)**

`entity_match_decisions` has **zero rows**. Decisions are scoped to exactly one
`source_ref`; an accepted decision is authoritative only for the exact provider
event id it adjudicated. A current event id may not be assumed equal to a
historical one.

### Option 6 — deterministic derivation → **REJECTED (empirically falsified)**

Event ids are 32 lowercase hex, opaque. **45 candidate formulations × 3 hash
algorithms over all 14 preserved events: 0 derived.** Absence of a formula over
45 attempts is not proof none exists, but the burden lies with the claim and
inventing a hash and calling it a provider id is forbidden. **Ids must be
observed.**

### Option 7 — a new event-identity crosswalk → **PARTIALLY ADOPTED**

The contract is right and is what Option 4 uses. No new table is needed for the
**crosswalk**; one is needed for the **audit input** (§11).

## 7. Evidence rules for curating a link

Given event `X` = (`event_id`, `home_team`, `away_team`, contemporaneous
`commence_time`, snapshot `timestamp`), establishing `X → canonical_game_id`:

| Evidence | Class |
|---|---|
| Exact raw provider team labels from the snapshot | **Curation evidence only** — offline, frozen, reviewed. Never runtime. Never identity. |
| Contemporaneous `commence_time` from the snapshot | **Curation corroboration**, and separately the *anchor* evidence at runtime. **Not part of the identity key.** |
| Snapshot `timestamp` | Curation scoping evidence |
| Home/away orientation | **Curation corroboration; a mismatch BLOCKS.** Never silently swapped |
| Canonical team names from the digest-bound source-controlled seed | Curation evidence only |
| Venue / venue-local date from the official corpus | Curation corroboration only |
| Retrospective final start `S_final` | **Curation evidence only, under S1–S9 below** |
| `team_aliases` / provider-scoped aliases (live tables) | **FORBIDDEN** — mutable, unhashed, not reproducible from source |
| Final score · result / winner · final-play wallclock | **FORBIDDEN** |
| Market prices, EV, implied probability | **FORBIDDEN** |

### 7a. `S_final` — the reviewed contract (S1–S9)

A link is a proposition about identifiers and encodes no outcome. The genuine
hazard is `POINT_IN_TIME_DATA.md` DQ-PIT-010: *"the mere existence of the link
encodes the future."*

**Pregame availability is not pregame identifiability.** Runtime confirmation
that an event existed and was pre-commencement at `T_cut` proves it was
*available*. It does **not** prove a retrospective curator could have
*identified* it without future schedule knowledge. The original conditions
conflated the two; S6–S9 close the gap.

`S_final` is admitted **only if all nine hold**:

**S1.** The curated map is source-controlled, frozen, digest-bound and
independently reviewed before use.

**S2.** Runtime never re-derives it — exact lookup or `UNRESOLVED`.

**S3.** Curation is exhaustive over the declared target population, fixed
*before* acquisition, never opportunistic (§12).

**S4.** The exclusion decomposition is reported in full (§12).

**S5.** The resolver independently verifies presence and pre-commencement at
`T_cut`, so a curation error can never manufacture an anchor.

**S6 — MONOTONE / REJECT-ONLY USE.** `S_final` may **reject** a proposed link. It
may **never** create a candidate, choose between candidates, break a tie, rank
candidates, or promote one provider event over another. **If two or more
candidates survive contemporaneous evidence, the target is AMBIGUOUS and
excluded — even if `S_final` would make one look obvious.**

*Why this is load-bearing:* two same-team events at `T_cut` are both present and
both pre-commencement, so S5 catches nothing. Selection by `S_final` would be
invisible. Rejection can only shrink the population, and shrinkage is visible in
the decomposition.

**S7 — DETERMINISTIC CURATION PROCEDURE.** Curation must be executable, versioned,
deterministic and replayable. The procedure *proposes*; a human *approves or
rejects*. Human intuition may not silently generate mappings.

**S8 — COUNTERFACTUAL RE-DERIVATION (mandatory).** Before the map may be accepted,
run the procedure twice: once with `S_final` available in its reject-only role,
and once with `S_final` **completely withheld**. The proposed maps must be
**identical**. Any entry that appears, disappears or changes is evidence that
future schedule information selected identity; it is **dropped and counted**.

**S9 — SEPARATE COUNTER.** Links rejected specifically because of `S_final` are
counted separately as `unlinked_rejected_by_S_final`, never netted into ambiguity
or provider-disagreement counters.

## 8. Event-id audit semantics (secondary provider)

The official-provider game collision rule keys on `(season,
home_provider_team_id, away_provider_team_id)`. **An Odds event carries no
provider team ids, only labels**, so that rule must not be applied mechanically —
doing so would either be inapplicable or would make label spelling an identity
collision. The Odds event namespace uses these rules instead.

**Provider identifier stability** and **mutable event description/schedule state**
are different things, and only the first is identity.

| Observation | Class |
|---|---|
| Same event id under conflicting `sport_key` | **BLOCKING collision** |
| Same event id resolving to conflicting canonical team **pairs** | **BLOCKING collision** |
| Same event id with home/away **swapped** | **BLOCKING collision** |
| Same event id, `commence_time` changed across snapshots | **NOT a collision** — descriptive mutability; precisely what the Repair-4 target-anchor algorithm exists to handle |
| Event disappears and later reappears with consistent identity | **NOT a collision** |
| Label punctuation/spelling change still resolving to the same franchise | **NOT a collision** — `name_variance` flag only; a name *"may never override a stable id"* |
| Label changes to something **unresolvable** | **WARNING** — a detection-power limit; report, never merge |

**Namespace-atomic rejection is retained** for a true provider-id collision: a
corpus that has proven it recycles identifiers gives no basis for asserting the
un-contradicted ids are safe. But the audit **must report blast radius**, so a
reviewer can consciously re-scope the bounded corpus rather than have a namespace
partially accepted automatically.

## 9. Chosen architecture — Option 4

**Stage A (discovery acquisition).** Bounded, separately authorized, hard-capped.
Fetch historical EVENTS snapshots for the declared first-pass plan only; preserve
each into `raw_responses` **and** the typed append-only observation table (§11).
Claims no canonical identity. Never routed through `resolve_target_anchor()`.
Boundary in §14.

**Stage B (audit).** Run the identity audit over the acquired corpus for
`entity_type='game'` in the Odds namespace, under §8's rules.

**Stage B2 (curation).** Offline, deterministic, human-approved, reviewed. Produce
a source-controlled map `event_id → canonical_game_id`, digest it, and write
`static_crosswalk_provenance` rows citing the ACCEPTED audit.

**Stage C (runtime).** Exact, corpus-scoped crosswalk lookup. Exact hit or
`IDENTITY_UNRESOLVED`.

### 9a. Identity key and provenance scoping

**Logical provider identity** — the stable key that names the thing:

```
(league_id, provider, namespace_generation, entity_type='game', provider_event_id)
```

where `provider = "the_odds_api:basketball_nba"` (one source-controlled constant,
never concatenated at a call site) and `namespace_generation = "v4"`.

**Corpus scoping is provenance, not logical identity.** `corpus_version_id` is
part of the `static_crosswalk_provenance` row and of the resolution context — it
answers *"under which reconstruction was this claim made?"*, not *"which event is
this?"*. Both matter; conflating them is what makes cross-corpus reuse look safe.

### 9b. Event-id format contract (repair D3)

`provider_event_id` for this namespace must match exactly:

```
^[0-9a-f]{32}$
```

Semantics: **byte-for-byte storage · no normalization · no case folding · no
whitespace trimming into a valid value · no Unicode normalization · no confusable
substitution.** An invalid value is **rejected**, never repaired.

This is required because v19 checks only `TRIM(provider_id) <> ''`. The following
attacks were each **reproduced against a real v19 database** and each produced a
*distinct, silently coexisting key* pointing at a different game:

- case-flipped hex (`BE25EB…`)
- leading/trailing whitespace (U+0020)
- zero-width whitespace (U+200B)
- Cyrillic confusable (U+0435, `е`)
- fullwidth Latin confusable (U+FF45, `ｅ`)

The format contract applies to **both** future historical-event observation
writes and future secondary-provider crosswalk writes. Additionally, the
committed curation map must be **scanned for visually or normalization-equivalent
confusable keys** even though every valid id is ASCII lowercase hex — the scan
catches a corrupted entry that the format check alone would pass.

### 9c. The required specifications

| # | Specification |
|---|---|
| 1 | **Identity key**: §9a, with `provider_id` the exact 32-hex event id, byte-for-byte |
| 2 | **Representation**: `static_crosswalk_provenance`; `canonical_entity_id` = `games.game_id` |
| 3 | **Who may create it**: an offline curation tool run by a human over an already-acquired corpus, writing only from a committed map file. No runtime path may create a crosswalk |
| 4 | **Evidence required**: ACCEPTED audit for this namespace over this corpus's exact `source_corpus_digest`; the canonical game already exists; the committed map contains the exact entry; **the row carries the committed event-map digest** (§9d); contemporaneous `commence_time` and home/away corroborate |
| 5 | **Evidence forbidden**: §7's forbidden rows, plus any runtime label lookup |
| 6 | **Scoping**: corpus-version-scoped and generation-scoped. A narrower audit never transfers to a wider reconstruction, and `trg_xwk_audit_corpus_binding` enforces the source-digest equality |
| 7 | **Version/digest binding**: §9d — **row-level**, plus a CI verifier that re-derives the committed map from source and checks every row |
| 8 | **Runtime lookup**: exact key → canonical game, or `UNRESOLVED`. Nothing else. No fuzzy matching, no nearest match, no runtime alias lookup, no normalization fallback, no home/away string matching at use time |
| 9 | **Ambiguity**: refuse. Two candidate events for one target → unlinked with reason (§12). One event id resolving to two games → structurally impossible via `xwk_key_unique` |
| 10 | **Reschedule**: identity is the event id alone. `commence_time` is descriptive; a time change never creates a new identity and is handled by Repair-4 iteration |
| 11 | **Event-id change**: §9e |
| 12 | **Completeness**: §12's three reconciliations |
| 13 | **Replay/idempotence**: append-only, deterministic map, digest-checked. Replay writes nothing new; a contradicting row is refused, never updated |
| 14 | **Strict-PIT isolation**: Lane-R only. `AsOfReader`, `_feature_cutoff` and the Lane-L matcher are untouched; no feature reader consults the crosswalk or the observation table |
| 15 | **Schema v19**: sufficient for the crosswalk, **insufficient for the audit input** — §11 |
| 16 | **`IdentityResolution` must change**: the present signature `(canonical_game_id, sport_key)` is **not corpus-scoped**, but crosswalks are. It must carry the corpus version and the qualified namespace |
| 17 | **Pre-acquisition discovery step**: required — §10, §14 |
| 18 | **Entitlement probe may precede identity implementation and v20**: yes — §13 |
| 19 | **Independent review before any full March acquisition**: the schema change, the audit result, the map, and the completeness decomposition — each independently |

### 9d. Map-digest binding is at ROW level (repair D1)

**`reconstruction_corpus_versions.static_identity_map_digest` remains
definitionally the TEAM-A team-attestation map digest.** It is **not** extended,
**not** composed with a second map, and **not** reinterpreted. The Odds-event map
never equals, replaces or modifies it.

This is forced, and both alternatives were proven against a real v19 database:
`attestation_map_digest()` takes no arguments and digests only the team map, and
`team_crosswalks._require_corpus_provenance` demands **exact equality** with it.
Composing a second map into that field makes TEAM-A crosswalk generation fail
closed; storing the team-only value leaves the event map unbound; and the corpus
row is append-only, so it cannot be amended afterwards.

**The Odds-event map binds at row level instead**, through the existing contract:

```python
record_static_crosswalk(..., attestation_map_digest=<committed event-map digest>)
```

which participates in the row's `semantic_digest`. Therefore:

- each Odds-event crosswalk row carries the **exact committed event-map digest**,
  making a row built under map *M* cryptographically distinct from one under *M'*;
- `provenance_policy_version` **identifies the event-map policy/version** (e.g.
  `odds-event-link-v1`) so a verifier knows which committed map to re-derive —
  without this the row carries a digest nobody can recompute;
- a verifier independently re-derives the committed Odds-event map from source
  and checks **every** row, in CI;
- **two independently versioned maps therefore coexist** without either claiming
  the single corpus-level digest field.

This binds per row rather than per corpus, which is strictly stronger than the
corpus-level scheme it replaces.

### 9e. Many event ids → one canonical game (repair D4)

The asymmetry is deliberate and is preserved:

- **one provider event id → two canonical games: structurally forbidden**
  (`xwk_key_unique`, proven);
- **multiple provider event ids → one canonical game: permitted** — a reschedule
  can legitimately re-issue an id.

**The database surfaces nothing in the permitted direction.** Two rows simply
coexist. The counter is therefore the *only* control, and it is mandatory, not
advisory. Every canonical game linked to more than one Odds event id must be
**counted · listed · individually reviewed · given an explicit reason** (e.g.
reschedule / provider re-issue) · **included in completeness reporting** (§12).

### 9f. Provider authority registries

The registries stay separated, and none of the following may be conflated:

| Registry | Contents | May grant bootstrap authority? |
|---|---|---|
| `OFFICIAL_PROVIDER_BY_LEAGUE` | official only — unchanged | yes (official only) |
| `PROVIDER_LEAGUES` | official only — unchanged | yes (official only) |
| `QUALIFIED_PROVIDERS` | official only — unchanged; its value is written to `games.official_provider`, which the Odds API may never touch | yes (official only) |
| **`LINKING_NAMESPACES`** *(new, not implemented here)* | secondary identity-**linking** namespaces only | **never** |

`ATTESTED_GENERATIONS` gains the Odds linking generation **only when the identity
implementation is separately authorized**. Adding it must **not** widen
official-provider or bootstrap authority, and tests must prove that
`LINKING_NAMESPACES` is **disjoint** from all three official registries.

### 9g. The audit guard is CODE-ONLY (repair D2)

The earlier claim that `ATTESTED_GENERATIONS` is a *database* hard stop is
**incorrect** and is withdrawn. What is actually true:

- the honest Python path fails closed — `ProviderNamespace.verified` is `False`
  for the Odds API, and an audit with `namespace_verified = 0` cannot be accepted;
- **but the database only ever sees `namespace_verified` as a caller-asserted
  `1`/`0`.** It holds no list of attested generations;
- **direct SQL can therefore forge a `namespace_verified = 1` ACCEPTED
  secondary-provider audit**, which then admits a real crosswalk. Both steps were
  reproduced against a real v19 database.

The generation restriction is **code-only**. Required invariant, enforced by a
CI verifier:

> **No `identity_audit_records` row may name a provider/generation outside the
> union of the authorized official attested namespaces and the authorized
> `LINKING_NAMESPACES`.**

The registries are the only place this is enforced, so they must be *checked*,
never assumed.

## 10. Cold start

> `resolve_target_anchor()` requires `provider_event_id` **before** it fetches a
> snapshot. The event id exists **only inside** the snapshot it has not fetched.

**"Identity before fetch" is not a point-in-time invariant.** Fetching the
snapshot at `floor(S_final − 60 min)` leaks nothing: the snapshot is itself
contemporaneous evidence, and `S_final` is already an authorized search hint. The
rule's real value is budget and scope containment, and that is delivered by
`RequestBudget`, which refuses before spending.

**Acquisition and resolution must therefore be split.** `IdentityResolution` is
correct for *resolution* and stays; it is simply **not applicable to
acquisition**, and no acquisition path may be routed through
`resolve_target_anchor()`. The very first event id is obtained by the explicitly
authorized, separately capped **Stage-A discovery acquisition** (§14), which
asserts nothing about which game any event is.

## 11. Schema — v20 contract

**The crosswalk table is adequate and is not overloaded.** Its contract is
already *link*, not *bootstrap*, and `write_game_bootstrap()` already writes
per-game rows for exactly this purpose.

**The audit input is missing.** The audit reads only `AUDITED_SOURCE_TABLES` =
`game_schedule_snapshots`, `provider_team_identity_snapshots`,
`provider_player_identity_snapshots`, and *"`raw_responses` is not a primary audit
path"*. Each alternative was falsified against the real schema:

- **`game_schedule_snapshots`** — `game_ref_id` is NOT NULL referencing
  `provider_game_references`; the INSERT fails. Writing an Odds event there
  requires minting an official game reference for a sportsbook, the exact
  authority §5 forbids.
- **`sportsbook_events`** — mutable in place (an `UPDATE` of `commence_time` and
  the raw labels succeeds with no trigger) and it has **no snapshot-instant
  column**.
- **`raw_responses`** — genuinely append-only, but untyped. An unpersisted parse
  would make **payload-parsing code part of identity evidence**, and completeness
  reconciliation would degrade to a full JSON scan.
- **A committed out-of-SQL artifact** — right for the curated **map**, wrong for
  the observations: a transcription is not evidence, since nothing binds it to
  the raw responses except a claim.

### 11a. Table: `historical_market_event_observations`

| Column | Type | Null | Note |
|---|---|---|---|
| `observation_id` | TEXT PRIMARY KEY | no | deterministic, `hme_` prefix |
| `league_id` | TEXT | no | FK `leagues` |
| `provider` | TEXT | no | qualified linking namespace |
| `namespace_generation` | TEXT | no | |
| `sport_key` | TEXT | no | the provider's own value |
| `provider_event_id` | TEXT | no | exact lowercase 32-hex (§9b) |
| `requested_at_bucket` | TEXT | no | the historical bucket **requested** |
| `provider_snapshot_timestamp` | TEXT | no | the instant the provider **answered** |
| `commence_time` | TEXT | **yes** | contemporaneous source value; **NULL is valid evidence** |
| `home_team_raw` | TEXT | no | verbatim |
| `away_team_raw` | TEXT | no | verbatim |
| `observation_content_hash` | TEXT | no | §11b |
| `raw_response_id` | TEXT | no | FK `raw_responses`, RESTRICT |
| `observed_at` | TEXT | no | our clock, existing project semantics |
| `created_at` | TEXT | no | |

**Uniqueness:**

```
(provider, namespace_generation, provider_event_id,
 provider_snapshot_timestamp, observation_content_hash)
```

**The content hash MUST be part of uniqueness.** Two content-distinct
observations under the same event id and the same provider snapshot timestamp
must **both survive**, because that contradiction is exactly what the audit
exists to detect. Deduplicating it silently would destroy the evidence. This
mirrors `game_schedule_snapshots`' existing
`UNIQUE (game_ref_id, observed_at, content_hash)`.

**No `canonical_game_id` column may exist in this table.** Stage A is
identity-free *by construction*, and the cleanest enforcement is that there is
nowhere to record identity.

**Deliberately excluded:** an event-status column — the Odds events payload
exposes exactly `id`, `sport_key`, `sport_title`, `commence_time`, `home_team`,
`away_team`, `bookmakers`, so a status field would be invented evidence.

**Absence semantics need no second table.** A row means "the provider reported
this event in this snapshot." Three states are already distinguishable because
`raw_responses` records request params and HTTP status: *bucket never requested*
(no raw response) · *requested and failed* (raw response, non-200) · *requested,
succeeded, event absent* (raw response 200, no observation row). A failed request
must never be readable as evidence of market absence.

`raw_responses` is append-only, so deletion is already impossible and default
RESTRICT on the FK is correct.

### 11b. Content-hash contract

- **`raw_response_id` is database-local** and does not survive transport between
  reconstruction databases.
- The portable audit/digest contract must therefore bind observation **content**,
  not the DB-local id. `source_corpus_digest` must not rely on `raw_response_id`
  as the portable semantic identity.
- `observation_content_hash` is computed deterministically from the **normalized
  observation tuple**, which participates: `provider`, `namespace_generation`,
  `sport_key`, `provider_event_id`, `requested_at_bucket`,
  `provider_snapshot_timestamp`, `commence_time` (including its NULL-ness),
  `home_team_raw`, `away_team_raw`, `league_id`.
- The exact canonical serialization is left to the v20 implementation task, which
  must make it **explicit, deterministic, versioned if it can ever change, and
  independently tested** (including over randomized insertion order).

### 11c. Digest blast radius — DECIDED, not deferred

Registering a fourth table in `AUDITED_SOURCE_TABLES` **changes
`source_corpus_digest` for every corpus, including those with no market data at
all** (proven). Existing audit and crosswalk bindings depend on that digest and
`trg_xwk_audit_corpus_binding` enforces equality, so registration **invalidates
every existing corpus**. Corpus rows are append-only and must not be edited.

The two alternatives:

- **Option A** — scope the market observation table into the source-corpus digest
  only for corpus versions that explicitly declare the market-evidence lane.
- **Option B** — accept that corpora using the expanded source set require **new
  corpus versions**, and rebuild the affected audit/crosswalk provenance.

> **DECISION: Option B is adopted** as the honest default under the current
> append-only design. No existing corpus row is edited; a corpus that uses the
> expanded source set is a **new** corpus version.
>
> Option A remains available to the v20 task **only if it first proves** the
> narrower scoped-digest architecture is sound, which would itself require
> independent review. Absent that proof, Option B stands.

**This must be settled before the migration runs, not discovered after it.** The
v20 task confirms or overturns this decision as its first step.

## 12. Completeness — three separate reconciliations

The declared target population is fixed **before** acquisition, from the bounded
official corpus. **239 is this pilot's value, not a general invariant, and must
never be hard-coded as one.**

A single reconciliation is insufficient: it cannot distinguish *"the provider had
no market"* from *"we never requested that bucket"* from *"the request failed"*,
so an acquisition failure would masquerade as a market fact. **Three independent
reconciliations are required, and no category from one phase may absorb a failure
belonging to another.**

### 12.1 Acquisition completeness

```
buckets_planned   = buckets_requested + buckets_not_requested

buckets_requested = succeeded
                  + failed
                  + quota_blocked
                  + malformed
                  + answered_with_earlier_snapshot
```

Categories are mutually exclusive. **A request failure must never be reported as
"provider had no market."**

### 12.2 Identity completeness

```
targets_declared = linked
                 + unlinked_no_event_in_any_acquired_snapshot
                 + unlinked_ambiguous_multiple_candidates
                 + unlinked_provider_disagreement
                 + unlinked_rejected_by_S_final          (S9)
                 + unlinked_other_with_reason
```

Reported separately, never netted away:

| Counter | Required behaviour |
|---|---|
| `targets_with_multiple_event_ids` | listed and **individually reviewed** (§9e) |
| `events_observed_not_linked` | reported (expected non-zero: other games) |
| `events_observed_outside_requested_bucket` | reported |
| `events_mapping_to_multiple_games` | **must be 0; any occurrence blocks** |
| audit `distinct_ids` / `total_observations` / `collision_count` / detection power / blast radius | reported |

### 12.3 F1-R eligibility (downstream of identity, never merged into it)

```
linked = anchored
       + no_market_at_T_cut
       + absent_at_final_Repair4_T_cut
       + already_commenced
       + missing_commence_time
       + no_convergence
       + no_completion_evidence
       + no_prior_game / first_date
       + postponed / cancelled / rescheduled
```

**No game and no event may silently disappear.** An unlinked target is an
explicit row with a reason, never an absence.

*Empirical note for this pilot:* all 239 NBA March games are `Final`, **0
postponed**, and **0 same-two-teams-same-date collisions**, so the doubleheader
hazard does not bind for NBA March 2026. It binds for MLB, and the rule stands:
disambiguation is official game id **plus** contemporaneous `commence_time`,
because id alone was a proven fail-open defect already repaired once.

## 13. Entitlement probe

A capability probe establishes entitlement, the wrapper shape, the provider
snapshot timestamp and the quota headers. **None of that requires canonical
identity**, and it does not go through `resolve_target_anchor()`.

| | Needs canonical identity? | Needs the v20 table? |
|---|---|---|
| **Capability / entitlement probe** | **No** | **No** — `raw_responses` suffices at v19 |
| **Stage-A discovery acquisition** | No | **Yes** — its output must be auditable |
| **Identity-resolved target-anchor acquisition** | **Yes** | Yes |

v19 `raw_responses` honestly preserves the request, the response, the HTTP
result, the provider wrapper, the provider snapshot timestamp inside the payload,
the quota headers, and our distinct `requested_at` / `received_at` clocks.

> **Re-materialization constraint.** A probe payload may later be projected into
> the v20 observation table **only if its historical requested bucket is also one
> of the declared Stage-A plan buckets.** Otherwise it is **not** audit-input
> evidence and must be **explicitly recorded as excluded** from Stage-A/audit
> input.
>
> A capability-probe bucket must never silently widen the research corpus, which
> would perturb `source_corpus_digest` and the declared population.

## 14. Stage-A acquisition boundary

The first-pass historical EVENTS request plan **is computed before any provider
contact**, from preserved official-corpus search hints only, is **fixed**, and is
**digest-bound before acquisition**. (Independently reproduced: 239 games → 160
distinct `T−60` buckets, purely from the preserved `/v1/games` datetimes.)

**Stage A MAY request:** only the exact buckets in that declared first-pass plan.

**Stage A MUST NOT:**

- perform Repair-4 iteration;
- request an undeclared bucket;
- request per-event historical endpoints;
- request the historical odds/prices endpoint;
- expand toward the ≤638 worst-case iterative plan.

Its request budget **must equal the declared plan size**, and any attempt to
exceed the plan must fail **before** the request is issued.

One bucket may legitimately serve multiple targets (106 buckets serve one game;
the largest serves four). **All events returned inside a requested snapshot must
be preserved**, because non-target events contribute the audit's detection power
— but **Stage A assigns no canonical identity to any of them**, which the absence
of a `canonical_game_id` column enforces structurally.

## 15. Threat model

| Attack | Result |
|---|---|
| Unknown provider event id | **Rejects** — exact key miss → `UNRESOLVED` |
| Malformed / non-32-hex id | **Rejects** — §9b format contract |
| Upper-case hex id | **Rejects** — §9b (at v19 alone it would have coexisted as a distinct key) |
| Leading/trailing or zero-width whitespace | **Rejects** — §9b |
| Cyrillic / fullwidth confusable | **Rejects** — §9b, plus the curation-map confusable scan |
| Wrong sport | **Rejects** — `sport_key` is in the qualified provider constant |
| Wrong league | **Rejects** — `league_id` in the key; `trg_xwk_game_target_valid` requires same-league |
| Wrong provider / wrong namespace generation | **Rejects** — both are in the key |
| Altered team labels | **Resolves** — labels are not the key; runtime never reads them |
| Historically renamed team | **Resolves**; a label change is at most a curation-time `name_variance` flag |
| Same id, `commence_time` changed only | **Resolves** — descriptive mutability, not a collision (§8) |
| Same id, conflicting home/away or teams | **Rejects the whole namespace** — collision, namespace-atomic, blast radius reported |
| Event id reused across seasons | **Rejects** if detectable in the acquired corpus; **undetectable** if the two uses are indistinguishable — a stated limitation |
| Event id changes after postponement | **Manual review** (§9e); the old id fails at `T_cut` and is a counted exclusion |
| One canonical game, two event ids | **Permitted, but counted, listed and individually reviewed** — the schema surfaces nothing |
| Two canonical games, one event id | **Structurally impossible** — `xwk_key_unique` |
| Neutral site | **Manual review** — orientation corroboration cannot be assumed |
| Wrong season | **Rejects** — canonical `season_id` corroboration fails |
| Wrong corpus / cross-corpus reuse | **Rejects** — `corpus_version_id` scopes the provenance row; `trg_xwk_audit_corpus_binding` enforces the source digest |
| Missing attestation | **Rejects** — `UNRESOLVED`, fail closed |
| **Stale map / stale attestation** | **Rejects** — the row's **event-map digest** (§9d) no longer matches the committed map re-derived by the verifier. *(It is NOT rejected by comparison against the corpus `static_identity_map_digest`; that field is team-only.)* |
| Tampered map after digest creation | **Rejects** — CI verifier re-derives the committed map from source and checks every row |
| Stale audit / wrong `source_corpus_digest` | **Rejects** — trigger-enforced |
| Superseded crosswalk | **Rejects** — supersession creates a new corpus version |
| **Direct SQL insertion** | **Partially rejects.** The ACCEPTED-audit, corpus-binding and game-exists triggers all fire. **But `namespace_verified` is caller-asserted, so direct SQL can forge an accepted secondary-provider audit and admit a crosswalk** — closed only by the §9g CI verifier invariant, not by the database |
| Partial map / only easy targets curated | **Rejects at review** — §12's three reconciliations must balance |
| **Missing request represented as market absence** | **Rejects** — §12.1 separates `buckets_not_requested` / `failed` / `quota_blocked` from market absence |
| Raw response changed after materialization | **Impossible** — `raw_responses` is append-only (proven); the content hash would detect it regardless |
| Current alias-table changes | **Irrelevant** — alias tables are never read |
| Runtime normalizer changes | **Irrelevant** — `provider_id` stored byte-for-byte |
| **`S_final` resolving an ambiguity impossible from contemporaneous evidence** | **Rejects** — S6 forbids tie-breaking outright, and S8's counterfactual re-derivation detects any entry `S_final` created |
| Future schedule information generally | **Rejects** — `commence_time` is not in the key; anchors come from the snapshot |
| Final score / result / price leakage into curation | **Rejects** — forbidden at every stage (§7) |
| F1-R consumer bypassing the crosswalk | **Rejects** — resolution is exact lookup or `UNRESOLVED`; there is no other path |
| Strict `AsOfReader` consuming the observation table | **Must reject** — the table is Lane-R only and is not an `AsOfReader` source (§9c.14) |
| **Undetectable residual** | An event id genuinely reused for two indistinguishable events within one corpus. Bounded by detection power; **must be reported, never claimed away** |

## 16. Residual limitations

- Genuine indistinguishable event-id reuse within one corpus is undetectable.
- A one-month audit proves nothing about multi-season stability.
- `ACCEPTED` means *"no contradiction detected at this policy's detection power"*,
  never *"verified stable"*.
- Entitlement remains **UNKNOWN**.
- The expected repeat-observation advantage over the official game audit is
  **expected, not yet measured**, and must be claimed only once observed.

## 17. v20 implementation boundary — next-task contract

### The v20 schema task MAY include

- the migration creating `historical_market_event_observations` (§11a);
- exact constraints, including the §9b format check and the §11a uniqueness key;
- append-only triggers;
- deterministic observation ids;
- the content hash and its canonical serialization (§11b);
- source-digest integration under the §11c decision (Option B unless a scoped
  alternative is proved first);
- audit-source registration as appropriate;
- fresh-init and v19→v20 upgrade producing identical schema;
- migration idempotence;
- schema tests and adversarial observation tests — at minimum: append-only
  enforcement; the content-distinct-same-instant case preserving **both** rows;
  the format check rejecting all five §9b lookalikes; absence-vs-failure
  distinguishability; digest determinism over randomized insertion order.

### The v20 schema task MUST NOT include

- live provider acquisition;
- the entitlement probe;
- Stage-A acquisition;
- the Odds linking-provider registry, except the minimum needed solely to
  validate observation writes;
- an accepted identity audit;
- the event→game map;
- crosswalk generation;
- resolver implementation;
- target-anchor execution;
- F1-R;
- E1 price acquisition;
- feature, model, calibration, backtest, recommendation or UI work.

**The schema task must have its own independent review before Stage A.**

## 18. Exact next authorization boundary

1. **Schema change (v20)** per §11 and §17 — own task, own independent review.
   Its first step is to confirm or overturn the §11c digest decision.
2. **Bounded entitlement probe** — may proceed at v19 **in parallel with 1**,
   under its own explicit cap (≤10 requests / ≤100 credits), claiming no
   identity, subject to the §13 re-materialization constraint.
3. **Stage-A discovery acquisition** — only after 1, first-pass plan only (§14).
4. **Stage-B audit** (§8) **+ Stage-B2 curation** (§7a, including the mandatory
   S8 counterfactual), then independent review of the map, the audit result and
   the completeness decomposition.
5. **Full NBA March target-anchor acquisition** — 160 requests / 160 credits
   first pass, ≤638 / ≤638 worst case.
6. **E1 economic evidence** — unchanged, ~1,600 credits, required before any EV
   claim.

**F1-R remains blocked** and is authorized by none of the above on its own.
