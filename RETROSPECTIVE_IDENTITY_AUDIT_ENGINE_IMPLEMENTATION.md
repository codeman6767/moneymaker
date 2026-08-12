# Retrospective identity-audit engine (G5) — implementation

> Superseded in part by the independent review; see the banner below.

Production implementation of the corpus-scoped identity audit and static-crosswalk
construction required by `G5_PROVIDER_ID_STABILITY_REVIEW.md`, built on the
independently accepted schema-v19 provenance foundation at `2824c3a`.

**Reviewed 2026-08-12 — ACCEPTED WITH REPAIRS AND A RETAINED BLOCKER.**
`RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_INDEPENDENT_REVIEW.md` is **authoritative
where it differs from this document**. Ten defects were proven and repaired and
the audit policy moved to **`g5-identity-audit-v2`**, so three claims below are
corrected by that review:

* the game identity signature was **under-powered** — a game id reused for the
  same matchup on another date, and one reused across both halves of a
  doubleheader, both read as clean at v1;
* `verified` accepted **any** generation string but the literal `unverified`, so
  a typo produced a verified namespace and authorized crosswalks;
* the one-month "zero collisions" is reported below without a detection-power
  qualifier. **No game id in either corpus was observed more than once**, so the
  game audit compared nothing, and `birth_date` is absent for every person, so
  person reuse within a league was undetectable. See the review §4.

The `RetrospectiveResearchReader` is still
unimplemented, historical market anchoring is still unimplemented, and **F1-R,
F2, production matching and model training all remain unauthorized.**

Offline throughout: no provider API request, no provider client construction, no
settings/API-key load, no mutation of any protected F1 corpus.

---

## 1. The audit contract

> Within **this exact corpus**, is every observation of
> `(league, provider, namespace_generation, entity_type, provider_id)`
> compatible with one canonical entity?

A **closed-world** statement, and deliberately nothing more. Neither BALLDONTLIE
nor MLB StatsAPI documents global permanent non-reuse, and no amount of scanning
could establish it, so every audit record binds the exact `source_corpus_digest`
it read.

`audit_namespace()` takes one namespace, one source corpus and one policy version.
Nothing is inferred: the league, provider, API generation and entity type are all
explicit arguments, there is no "latest provider" and no namespace component is
ever derived from the shape of an id.

## 2. Source adapters (`retrospective/sources.py`)

The audit reads **typed append-only observations**, never raw JSON. Parsing
`raw_responses` bodies at audit time would couple the audit to every provider's
payload shape and make its conclusion unauditable — the same reason migration
e017 exists. `raw_responses` is not a primary audit path.

| Entity | Table | Real rows (MLB June / NBA March) |
|---|---|---|
| game | `game_schedule_snapshots` | 400 / 239 |
| team | `provider_team_identity_snapshots` | 1,630 / 6,474 |
| player | `provider_player_identity_snapshots` | 47,830 / 91,187 |

**The source is opened `immutable=1`.** `mode=ro` still builds the shared-memory
index, which moves the `-shm` sidecar's mtime beside protected evidence — a real
change to the evidence directory even when the database bytes are untouched. The
cost of `immutable` is that it cannot see WAL content, so a **non-empty WAL is
refused** rather than silently read as a stale view; the operator must checkpoint
into a protected copy and audit that.

## 3. Source-corpus fingerprint

`source_corpus_digest(conn, league_id, provider)` hashes exactly the audited
subset: the three tables above, restricted to the columns in `_DIGEST_COLUMNS`.
Excluded on purpose: surrogate ids, `created_at`/`ingested_at`/`run_id` (audit
bookkeeping, not evidence), and every column the compatibility rules are
forbidden to read.

Rows are reduced to canonical tuples and **sorted before hashing**, so SQLite
traversal order, insertion order and rowid assignment cannot affect it, while any
changed identity value does.

It is per **(corpus, league, provider)** and deliberately **not** per entity
type: all three entity-type audits of one corpus must agree on one digest, or a
reconstruction corpus version citing it could consume crosswalks from only one of
them.

Measured: MLB June `63c7a6a1…`, NBA March `5074428c…`.

## 4. Audit policy version

`AUDIT_POLICY_VERSION = "g5-identity-audit-v2"` (bumped by the independent
review) names the exact compatibility
rules below. It participates in the audit's semantic digest, and a run requesting
any other version is **refused rather than approximated** — an audit recorded
under a policy this build does not implement cannot be reproduced here.

## 5. Compatibility rules

### Game

**Identity-defining:** `(season, home_provider_team_id, away_provider_team_id)`.
A game's participants do not change; two observations that disagree mean the id
denotes two different events.

**Lawful mutation** (recorded as `legitimate_mutation`, never a collision):
scheduled start, status progression, venue, and a date change **accompanied by
provider continuity evidence** — a `reschedule_info` payload or an observed
postponed/suspended/delayed/rescheduled status. A postponement that moves a game
three days is not id reuse.

**Two additions from the independent review (policy v2):**

* **Same date, two game numbers → BLOCKING collision**
  (`GAME_ID_TWO_EVENTS_SAME_DAY`). One id carrying both halves of a split
  doubleheader is *provably* two events, and the matchup matching is exactly what
  hid it from the season/home/away triple.
* **Date change with no continuity evidence → WARNING**
  (`GAME_ID_DATE_MOVED_WITHOUT_CONTINUITY`, classified `insufficient_evidence`).
  Not a collision — a postponement genuinely moves a date — but not lawful
  mutation either, because the corpus cannot distinguish the two.

**Never read:** final score, winner, any result or box-score value, any match
decision.

### Team

**Identity-defining:** the league alone. A team id is *franchise* identity.

**Lawful:** rename, relocation, city change, abbreviation change, rebrand — all
detected as `name_variance` warnings only. Making the display name the key would
turn every rebrand into a collision.

A field that is **absent in one observation and supplied in another has not
changed.** MLB StatsAPI returns abbreviation/city/nickname from some endpoints
and not others; the first-pass rule flagged all 30 MLB franchises on exactly that
shape while `normalized_name` was identical throughout. Only conflicting
*supplied* values count.

### Player-person

**Identity-defining:** the league, and `birth_date` **only when the provider
genuinely supplied it on more than one observation**. Two different birth dates
under one id are two different people.

**Lawful:** team affiliation, position, active status, jersey, display-name
change. **Team affiliation is never person identity.**

**Name rules:** a name difference raises a warning and can never merge or split
ids; two different ids sharing a name remain two people. `suffix` is the one
field where `""` is a *real* value rather than "not supplied" — "Ken Griffey" is
not "Ken Griffey Jr.", which is why e017 stores it separately.

**Missing secondary evidence** reduces detection power and never justifies a
merge.

## 6. Finding codes

| Code | Trigger | Severity | Classification | Scope |
|---|---|---|---|---|
| `GAME_ID_TWO_DIFFERENT_EVENTS` | one game id, two identity triples | blocking | `identity_collision` | `entity` |
| `GAME_ID_LAWFUL_MUTATION` | reschedule/status/venue change | info | `legitimate_mutation` | `none` |
| `GAME_ID_INSUFFICIENT_EVIDENCE` | season or a participant never recorded | info | `insufficient_evidence` | `none` |
| `GAME_ID_TWO_EVENTS_SAME_DAY` *(v2)* | one date, two game numbers | blocking | `identity_collision` | `entity` |
| `GAME_ID_DATE_MOVED_WITHOUT_CONTINUITY` *(v2)* | date changed, no reschedule/moved-status evidence | warning | `insufficient_evidence` | `none` |
| `NAMESPACE_DETECTION_POWER` *(v2)* | recorded on every audit | info | `insufficient_evidence` | `none` |
| `TEAM_ID_TWO_LEAGUES` | one team id in two leagues | blocking | `identity_collision` | `dependent_games` |
| `TEAM_ID_LABEL_CHANGED` | conflicting supplied labels | warning | `name_variance` | `none` |
| `PLAYER_ID_TWO_LEAGUES` | one person id in two leagues | blocking | `identity_collision` | `entity` |
| `PLAYER_ID_TWO_BIRTH_DATES` | conflicting supplied birth dates | blocking | `identity_collision` | `entity` |
| `PLAYER_ID_NAME_CHANGED` | conflicting supplied name/suffix | warning | `name_variance` | `none` |
| `PLAYER_ID_NO_SECONDARY_EVIDENCE` | ids with no birth date (one namespace-level row) | info | `insufficient_evidence` | `none` |
| `NAMESPACE_GENERATION_UNVERIFIED` | generation not established | blocking | `namespace_unverified` | `league_namespace` |

Detail payloads are counts and field names only. No raw provider body, no
credential, no URL — the f019 sanitizer refuses those independently.

`PLAYER_ID_NO_SECONDARY_EVIDENCE` is recorded **once per namespace**, not once
per id: 1,053 identical rows would bury the finding that matters while asserting
nothing extra. Its reach is `none` — thin evidence excludes nothing, it bounds
what a clean result can be said to prove.

## 7. Complete-audit count reconciliation

The schema review deliberately left one completeness gap: the database can store
`collision_count = 5` on a rejected audit carrying no collision findings. Closing
it in SQL would need a deferred constraint SQLite lacks, or a mutable sealing
flag that would weaken the append-only guarantee. **The engine closes it
instead.**

The whole conclusion is built in memory first, and `_reconcile()` refuses to
persist unless:

* `distinct_ids` == audited distinct provider ids
* `total_observations` == scanned observations
* `collision_count` == distinct ids carrying a blocking collision finding
* `flagged_count` == warning findings
* verdict agrees with the collision set and the namespace verification
* no duplicate semantic finding

Every count is **derived from the findings**, never asserted beside them.

## 8. Atomicity, determinism, idempotency

**Atomic.** Summary and every finding are written in one transaction; a failure
anywhere rolls the whole audit back, leaving no consumable partial audit, no
orphan findings and no crosswalk from a failed audit (tested with an injected
failure after the last finding).

**Deterministic.** The semantic digest binds namespace, source digest, policy
version, the canonical **provider-id set**, the reconciled counts, the verdict
and the canonical finding set — and excludes wall-clock, surrogate ids and
traversal order. Verified over **100 randomized insertion orders** of a fixture
containing a collision, a name variance and a thin-evidence id: one digest, one
summary.

**Idempotent.** A replay returns the same audit record, adds zero findings, zero
crosswalks and zero canonical entities. A changed corpus or a changed policy
version produces a new audit.

## 9. Crosswalk generation, and the canonical-entity blocker

Crosswalk eligibility is: exact official provider key + accepted audit **for this
exact source corpus** + correct namespace generation + exact canonical entity. No
name fallback, no roster, no outcome, no `decided_at` gate. `curated_at` remains
honest wall-clock audit time.

Canonical preparation was investigated per entity type, and the answer is a
measured property of this repository rather than a preference:

| Entity | Crosswalks | Why |
|---|---|---|
| **player** | **supported** | `players` is empty in a fresh output DB and has no uniqueness constraint beyond its PK. `canonical_player_id()` is a pure SHA-256 of the official key — deterministic, future-blind, no name involved. The provider-written name is stored as descriptive metadata (earliest `observed_at`, tie-broken by normalized name) and never participates in identity. |
| **team** | **BLOCKED** | `teams` is pre-seeded with 60 name-based franchises under `UNIQUE (league_id, canonical_name)` and `UNIQUE (league_id, abbreviation)`. Inserting a provider-keyed franchise with the provider-written name is **refused by those constraints** (verified empirically), and reusing a seeded row would mean deciding that the provider's "Houston Astros" denotes seed `tm_mlb_hou` — name matching, which §16 forbids as historical identity evidence. Mangling the canonical label to dodge the constraint would corrupt a canonical dimension to satisfy a foreign key. |
| **game** | **BLOCKED, transitively** | `games.home_team_id`/`away_team_id` are NOT NULL references to `teams`. |

Measured precondition: across both protected corpora, `provider_team_references`,
`provider_player_references` and `provider_game_references` have **zero** bound
canonical ids and **zero** match decisions, and canonical `players`/`games` are
empty. There is no existing deterministic official→canonical binding to reuse.

This is reported as a blocker rather than forced. The audit engine audits all
three entity types regardless; only crosswalk generation is limited.

**Honest limitation of the player crosswalk:** the canonical person is *defined*
as "the entity denoted by this official key", so the binding is provider-scoped
and somewhat tautological. Its value is the audit and corpus binding it carries,
not novelty in the target. Cross-provider unification of one person under two
providers remains a matching problem that G5 never claimed to solve.

## 10. Accepted vs partially excluded audits (§20 adjudication)

`identity_audit_records` requires `collision_count = 0` for an ACCEPTED verdict,
and f019 refuses any contradictory exclusion finding under an accepted audit.
Together these make **any real identity collision a REJECTED namespace audit**,
not "accepted except these ids".

The engine follows that contract exactly: `cleared_provider_ids` is empty unless
the verdict is ACCEPTED, so a rejected namespace clears nothing.

**The mismatch is documented rather than worked around.** G5's severity ladder
describes per-entity blast radius (a player collision excludes that player), which
reads as though a namespace could continue with that id removed. The schema does
not permit it, and no schema change was made: recording per-ID partial
continuation would require either a new verdict value or a mutable exclusion set,
both of which are independent-review decisions. `exclusion_scope` still records
the observed reach on the rejected audit's findings, so the evidence needed to
make that decision later is preserved. **This is flagged as an explicit
independent-review target.**

## 11. Real one-month audit results

Read-only, zero network, fresh temporary v19 output databases. Counts are
recomputed by the engine, not asserted from the prior review.

### MLB June 2026 — `data/f1_mlb_2026_06_scratch.db` (schema v17, never migrated)
source corpus digest `63c7a6a1daaee723c81ae17e…`

| Entity | Distinct ids | Observations | Collisions | Flags | Verdict | Crosswalks |
|---|---|---|---|---|---|---|
| game | **400** | 400 | 0 | 0 | accepted | blocked |
| team | **30** | 1,630 | 0 | 0 | accepted | blocked |
| player | **1,053** | 47,830 | 0 | 0 | accepted | **1,053** |

### NBA March 2026 — `data/f1_nba_2026_03_lineups_merged.db` (schema v17)
source corpus digest `5074428c2e3755335c3fc0d3…`

| Entity | Distinct ids | Observations | Collisions | Flags | Verdict | Crosswalks |
|---|---|---|---|---|---|---|
| game | **239** | 239 | 0 | 0 | accepted | blocked |
| team | **30** | 6,474 | 0 | 0 | accepted | blocked |
| player | **550** | 91,187 | 0 | 0 | accepted | **550** |

The prior review's claimed counts (MLB 400/30/1,053; NBA 239/30/550, zero
collisions) are **independently reproduced**.

### Detection power (added by the independent review)

Every audit now records what it was **able** to detect. Over the real corpora:

| | ids | comparable (seen >1×) | discriminating |
|---|---|---|---|
| MLB game | 400 | **0** | 0 |
| MLB team | 30 | 30 | 30 (league only) |
| MLB player | 1,053 | 1,044 | **0** |
| NBA game | 239 | **0** | 0 |
| NBA team | 30 | 30 | 30 (league only) |
| NBA player | 550 | 549 | **0** |

**No game id in either corpus was observed more than once**, so the game audits
compared nothing at all: "zero collisions" there means "nothing was comparable".
`birth_date` is absent for every person, so within-league person reuse is
undetectable, and same-league team reuse is undetectable by design.

### Limitations of this evidence

* **One month per league. This is not evidence of 3–5 season identifier
  stability.** The same audit must be re-run over the full F2 source window
  before any multi-season reconstruction is accepted.
* **Person-collision detection had no secondary evidence at all.** `birth_date`
  is populated for **0 of 1,053** MLB and **0 of 550** NBA persons, so the clean
  player result rests on league consistency and the absence of name conflicts
  alone. This is recorded in every player audit as
  `PLAYER_ID_NO_SECONDARY_EVIDENCE` rather than left implicit.
* Both corpora are single-provider and single-season, so cross-generation and
  cross-provider reuse were never exercised against real data — only fixtures.

## 12. Dry run and CLI

`--apply` is required to write; without it the command performs the identical
audit — same scan, rules, counts, findings, digest, and even a crosswalk
prediction against a throwaway in-memory database — and persists **nothing**.
Dry run and apply are verified to agree on the semantic digest and the crosswalk
count.

```
sports-quant identity-audit-retrospective \
    --source-db DATA.db --output-db OUT.db \
    --league lg_mlb --provider mlb_statsapi \
    --namespace-generation v1 --entity-type all \
    [--expect-source-digest SHA256] [--crosswalks] [--apply] [--json]
```

There is **no provider-access argument**, and none could work: the engine imports
no provider client and reads no settings. Reports expose the source digest,
namespace, counts, verdict, findings by classification and scope, the semantic
digest, and `network_occurred=false`.

## 13. Strict-PIT isolation

`AsOfReader` and `_feature_cutoff` are unchanged (`_feature_cutoff` still hashes
to its pinned v17 value); `pit/asof.py`, `pit/dataset.py`, `pit/registry.py` and
`matching/` are untouched since `2824c3a`; all Lane-R tables remain `unsupported`
joins; and no feature, model or PIT module imports the audit engine. The identity
audit is not a PIT bypass.

## 14. Files

**Added:** `sports_quant/retrospective/sources.py`,
`sports_quant/retrospective/identity_audit.py`,
`sports_quant/retrospective/crosswalks.py`,
`sports_quant/retrospective/runner.py`,
`sports_quant/db/tests/test_retrospective_identity_audit.py` (57 tests),
this document.

**Modified:** `sports_quant/retrospective/__init__.py` (lazy engine surface —
the engine consumes `sports_quant.db` while the vocabulary is consumed *by* it,
so eager re-export reintroduced the D8 import cycle), `sports_quant/cli.py`
(`identity-audit-retrospective`).

## 15. What remains unimplemented

`RetrospectiveResearchReader`; historical Odds API fetching and market anchoring;
team and game crosswalk generation (blocked above); F1-R; F2; production
matching; model training; Lane-L collection.

**G1, G2, G3, G4 and G6 remain open exactly as previously scoped.** G5 remains
closed as the corpus-scoped fail-closed contract; this phase implements its
engine, and does not re-decide its verdict.
