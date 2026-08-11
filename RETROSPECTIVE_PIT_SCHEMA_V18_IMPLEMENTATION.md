# Retrospective PIT provenance foundation — schema v18 (f018), repaired to v19 (f019)

Implementation of the schema and repository foundation authorized by the
independently reviewed retrospective PIT architecture at `f881916`.

**Reviewed 2026-08-11 — ACCEPTED WITH REPAIRS; the foundation is now schema v19.**
`RETROSPECTIVE_PIT_SCHEMA_V18_INDEPENDENT_REVIEW.md` is **authoritative where it
differs from this document**. Eight defects were proven and repaired via migration
`f019`; `f018` is preserved byte-for-byte. In particular, two claims made below were
too strong at v18 and are corrected by that review:

* a static crosswalk could cite an ACCEPTED audit taken over a **different source
  corpus**, so §4's "keyed on the exact source digest, so a clean one-month audit
  cannot be presented as covering a five-season window" held for the *audit lookup*
  but **not** for the crosswalk that consumed it;
* an ELIGIBLE reconstructed input needed **no source-evidence pointer at all**, and
  `source_evidence_table` accepted any string — so §4's "traceable without
  duplicating the evidence" was aspirational rather than enforced.

This document otherwise describes what was built and why; it is not an acceptance. The reader, the identity-audit engine, historical
market anchoring, F1-R, F2, production matching and model training all remain
unimplemented and unauthorized.

Offline throughout: no provider API request, no provider client construction, no
settings/API-key load, and no mutation of any protected F1 corpus.

---

## 1. What this phase is, and what it deliberately is not

`F1_HISTORICAL_PIT_FEASIBILITY_REVIEW.md` established the blocker: 239/239 NBA
and 400/400 MLB games in the preserved corpora were first observed **after** their
scheduled start, so a strict `observed_at <= cutoff` dataset yields zero rows.
That refusal is correct. The architecture's answer is not to weaken the cutoff but
to add a **second, clearly-labelled provenance lane** in which each input is
certified against a stated availability basis, and in which the corpus itself is a
versioned, digest-identified object.

This phase implements only the **storage contract** for that lane.

| Built | Deliberately absent |
|---|---|
| Migration `f018`, five append-only tables | `RetrospectiveResearchReader` |
| `sports_quant/retrospective` domain vocabulary | Identity-audit engine (corpus scanning) |
| Code-defined availability-rule registry | F1-R builder / executor |
| `SqliteRetrospectiveProvenanceRepository` | Historical Odds API client, market anchoring |
| Deterministic semantic digests | Any feature computation or feature store |
| 90 new tests | Lane-L forward collection |

The consumer is absent on purpose. Reviewing a storage contract by reading the
code that already depends on it is not an independent review of the contract.

## 2. Reconstructing the reviewed semantics

The original architecture draft (§11) proposed seven stored concepts. The
independent review changed two of them, and this implementation follows the
review, not the draft.

| Concept | Stored? | Where |
|---|---|---|
| `provenance_class` | yes | corpus versions, input provenance |
| `availability_basis` | yes | input provenance |
| `availability_rule_id` | yes | input provenance |
| `reconstruction_policy_version` | yes | corpus versions, input provenance |
| `source_event_completed_at` | yes, EVENT_DERIVED only | input provenance |
| `availability_source` | yes | input provenance |
| **`availability_confidence`** | **NO — removed by the review** | — |
| **`effective_at`** | **NO — derived, not stored** | — |

### Why `availability_confidence` is absent

Eligibility is binary. An ordinal confidence column is a dial, and a dial next to
a coverage number gets turned until the coverage looks acceptable. Confidence
belongs in the written evidence grade (E0–E3), where changing it requires
changing prose that a reviewer reads, not a threshold that a query silently
compares against.

### Why `effective_at` is absent

For EVENT_DERIVED, availability is
`source_event_completed_at + rule(availability_rule_id)`. Materializing that as a
column creates a second source of truth that goes stale the moment the rule
changes, and it is precisely the field a future bug would quietly backdate.
`derive_availability_instant()` computes it on read and re-verifies the rule
digest first, so a changed rule fails closed instead of returning a different
answer under the same name.

**One deliberate exception.** VERSIONED_SNAPSHOT stores `source_snapshot_at`,
because the **provider** published and stamped it. That is source evidence, not
our derivation, and two corpora built from different provider snapshots must not
share a digest.

### Per-basis obligations, enforced structurally

| Basis | Requires | Forbids |
|---|---|---|
| `static_identity` | a static crosswalk | any timestamp — an identity needing an effective time would not be static |
| `event_derived` | `source_event_completed_at` + rule id + rule digest | `source_snapshot_at` |
| `versioned_snapshot` | `source_snapshot_at` | `source_event_completed_at` |
| `label_only_retrospective` | nothing | basis, rule, crosswalk, all timestamps |
| `strict_forward_pit` | — | **may not appear in this table at all** |

FORWARD_ONLY evidence cannot enter the retrospective path because there is no
forward basis to name, and `rip_not_forward_lane` refuses the class outright.

## 3. Existing-schema inventory, and what was reused

Migration `f018` follows the conventions already established across a001–e017:

* `<letter><NNN>_<name>.sql`, discovered and checksummed by `Database.migrations()`,
  immutable once applied.
* TEXT primary keys with a prefix CHECK (`rcv_`, `ida_`, `idf_`, `xwk_`, `rip_`).
* ISO timestamps matching `'____-__-__T__:__:__%Z'`, so lexicographic ordering is
  chronological ordering.
* `trg_<abbrev>_no_update` / `trg_<abbrev>_no_delete` abort triggers, as on
  `raw_responses`, `game_status_history` and the e017 identity tables.
* Existence triggers alongside FKs, because SQLite honours FKs only when
  `PRAGMA foreign_keys` is on.
* Repositories own all SQL; `Repository` base, frozen dataclasses in `db/models.py`,
  ULID factories in `db/ids.py`.
* Digests via `streaming.event_envelope.canonical_json`, as `observation_content_hash`
  already does.

**Nothing existing was duplicated or rewritten.** No prior migration was edited.
The e017 identity-observation tables were considered as a home for the audit
results and rejected: they record *what a provider called an entity at a moment*,
which is a different fact from *whether a namespace is internally consistent over
a corpus*. Overloading them would have made the G5 verdict look like an
observation.

## 4. The five tables

### `reconstruction_corpus_versions` (`rcv_`)

One reproducible retrospective reconstruction context. Digest inputs follow the
architecture's §19 rule: source corpus fingerprint · static identity map ·
availability policy version · cutoff policy · feature/evidence registry · target
set · market snapshot evidence set · G1 variant.

`evidence_registry_digest`, `static_identity_map_digest`, `market_evidence_digest`
and `code_version` are **nullable**, because only a later F1-R execution produces
them. They are left absent rather than back-filled with a placeholder: a
placeholder enters the semantic digest and makes two genuinely different corpora
look like the same one.

`semantic_digest` is UNIQUE and is the corpus's real identity.

### `identity_audit_records` (`ida_`)

The stored result of one corpus-scoped G5 audit for one
`(league, provider, namespace generation, entity type)` over one exact
`source_corpus_digest`.

The G5 fail-closed rule is a CHECK, not caller discipline:

```sql
CONSTRAINT ida_accepted_is_clean CHECK (
    verdict <> 'accepted' OR (collision_count = 0 AND namespace_verified = 1))
```

There is no way to record "accepted, but with three collisions we decided to
ignore". Because the audit is keyed on the exact source digest, a clean one-month
result cannot be presented as covering a five-season window — the lookup simply
does not match.

### `identity_audit_findings` (`idf_`)

One thing the audit observed, at the granularity the reviewed severity model
needs. `exclusion_scope` records the **observed blast radius**:

| Scope | Meaning |
|---|---|
| `entity` | this player/team/game is excluded |
| `dependent_games` | a team collision takes its games with it (team-only, enforced) |
| `league_namespace` | a namespace problem blocks the league |
| `corpus` | systemic; escalates the whole reconstruction |
| `none` | legitimate mutation, recorded so "we looked" is evidence |

**Policy is not stored.** How many entity exclusions should escalate to refusing
a corpus is a versioned decision that stays in code; freezing a threshold into an
evidence row would make old evidence carry a policy it never agreed to.

Three edges of the reviewed model are enforced rather than trusted: a collision is
always `blocking`; a legitimate mutation is always `info`/`none`; a
namespace-unverified finding carries no `provider_id` and reaches at least the
league.

`detail_json` is sanitized structured metadata with a 200-character per-string
bound. Raw provider bodies live in `raw_responses` and are never copied here.

### `static_crosswalk_provenance` (`xwk_`)

"Within corpus version X, provider key
`(league, provider, namespace_generation, entity_type, provider_id)` denotes
canonical entity Y, and audit Z cleared that namespace."

There is **no name column, no team affiliation, no position, no roster state and
no outcome** — none is identity evidence under the reviewed contract, and the
tests assert their absence rather than merely not using them.

`curated_at` is **audit time**. It is not backdated and it is not a reused
`decided_at`: a matcher wall-clock is not a historical effective time.

Four triggers enforce §12 referential integrity:

* the cited audit must be `accepted`, for the identical namespace, with a matching
  digest;
* a `team` key must resolve to a `teams` row **in the same league**;
* a `player` key to a `players` row in the same league;
* a `game` key to a `games` row in the same league.

So an NBA player crosswalk cannot bind a team id, and an MLB provider key cannot
bind NBA canonical identity.

### `reconstructed_input_provenance` (`rip_`)

"For target game G in corpus version X, input family F is (or is not) admissible,
on basis B, under rule R at policy P, from this source evidence."

**No feature value is stored.** Certification metadata now; a feature store only
if and when one is separately justified.

Two triggers bind it: the certification must match its corpus version's league,
and a cited crosswalk must belong to the **same corpus version and namespace** —
so a clean crosswalk from one corpus cannot silently vouch for an input in
another.

## 5. Provider namespace contract

The identity key is `(league, provider, entity_type, provider_id)`, carried in
code as `ProviderNamespace` and in SQL as four explicit columns plus
`namespace_generation`.

* **Generation is never inferred from an id value.** Neither BALLDONTLIE nor MLB
  StatsAPI documents whether identifiers are stable across API versions, so v1 and
  v2 ids are never silently equated.
* **Unknown is representable.** `UNVERIFIED_GENERATION = "unverified"` with
  `namespace_verified = 0`, and an audit carrying it is refused an `accepted`
  verdict by CHECK.
* **`entity_type` is mandatory.** Without it, MLB team `147` and MLB person `147`
  are the same string.

## 6. Availability rule registry: code-defined, digest-bound

Three designs were available (§9). The narrowest that still guarantees
reproducibility wins:

* A **registry table** lets a row exist that no code implements, and lets two
  deployments disagree about what a parameter means. It stores a number and calls
  it a policy.
* A **bare identifier** reproduces nothing: a later edit silently reinterprets
  every accepted corpus.
* **Code-defined + bound implementation digest** — chosen. Rules live in one frozen
  mapping in `retrospective/rules.py`. Each digest covers its id, version, *named
  evaluation form*, and every parameter. A provenance row stores the rule id
  **and** that digest; `verify_rule_digest` re-checks on read and raises if the
  rule was edited.

The evaluation form is in the digest because hashing parameters alone would not
notice `completed + lag` becoming `completed - lag`.

Two rules ship: `prior_event_completion_conservative_v1` (6h, the stated
conservative assumption) and `prior_event_completion_immediate_v1` (0h, the
optimistic bound for the required sensitivity analysis). Neither lag is a
measurement; the publication delay of official box-score detail is undocumented,
and the architecture requires it be carried as an assumption.

## 7. Digest and versioning contract

`semantic_digest(payload)` is SHA-256 over `canonical_json`, which sorts keys.
Therefore:

* insensitive to dict ordering, insertion order, and SQLite row order (callers sort
  collections before hashing);
* sensitive to every semantic field — a changed policy version, source fingerprint,
  target set or G1 variant yields a different corpus;
* excludes surrogate ids and audit wall-clocks, so two identical audits run on
  different days share a digest (tested across two separate databases);
* **includes** provider-published snapshot instants, which are evidence.

**Supersession appends.** `supersedes_corpus_version_id` points at a row that must
already exist; combined with append-only insertion and a no-self-supersede CHECK,
the supersession graph is **acyclic by construction** — an edge can only point
backward in time, and no row is ever updated. The superseded corpus stays
byte-identical, so every experiment attributed to it remains attributable. Two
corpora may supersede the same parent; that is recorded, not resolved, because
silently picking one would be the "latest wins" shortcut this lane refuses.

## 8. Repository API discipline

* Explicit `record_*` / `certify_input`; no upsert anywhere.
* Deterministic lookup by exact id or exact key. **No latest-wins, no fuzzy match.**
* Not-found returns `None`; **ambiguous raises** `AmbiguousProvenanceError` rather
  than taking the first row SQLite returns.
* Idempotent replay by digest; a *different* claim under the same natural key
  raises `ProvenanceConflictError` — a contradiction, not an update.
* Immutable frozen dataclasses out; no raw SQL surface exposed upward.
* Strong enums throughout, clean under mypy.

## 9. Strict-PIT isolation

The whole justification for a second lane is that it does not weaken the first.
Tested three independent ways (`test_strict_pit_unchanged_under_v18.py`):

1. **Structural.** All five v18 tables are registered `unsupported` in the PIT
   safe-join registry, so a dataset builder naming one fails closed — including
   when declared alongside a legitimately joinable table.
2. **Behavioural.** `AsOfReader` has no retrospective mode, no boolean flag, and no
   bypass; a SQLite trace hook confirms it executes no statement mentioning a v18
   table. `_feature_cutoff` is pinned by SHA-256 of its source against its value at
   `f881916` and is **byte-identical**.
3. **The specific blocker.** A lineup observed after the cutoff is still invisible
   through the real repository path; an on-time one is still visible; and writing
   Lane-R provenance changes neither answer.

Full suite: **2525 passed, 2 skipped, 0 failed**.

## 10. Consequence worth flagging

`SUPPORTED_SCHEMA_VERSIONS` is now `{16, 17, 18}`, so every preserved F1/F1B
manifest and checkpoint stays valid and byte-identical. But two guards compare the
live schema to a manifest's declared version by **exact equality**
(`results_repair.py:255`, `cli.py:1120`) — correctly, since a manifest pins the
schema its run must occur against.

This means a manifest pinned at v17 can no longer create a *fresh* recovery
database on a v18 build, because a fresh database is now v18. It does **not**
affect any preserved evidence, all of which is already at v17 and still matches.
`pilots/f1/generate_lineup_continuation_manifest.py` was deliberately left
declaring 17: changing it would change the committed manifest's hash, which is
recorded in the preserved checkpoint. The affected test fixture overrides the
version locally, for the same reason it already overrides target counts.

## 10b. CI history (do not erase)

The first push of this foundation (`31f78e2`) **failed CI run #91**: the
wheel-smoke job pins the migration count and schema version inline in
`.github/workflows/ci.yml`, and the pre-commit scan for hard-coded `17`s covered
Python files only, so four assertions were missed. `4d4ae13` corrected them —
CI-only, no product change — and CI #92 was green on both jobs. The independent
review's repairs bumped the same assertions again to 19, verified locally against a
real built wheel and a clean non-editable install **before** pushing.

## 11. What remains unimplemented

| Item | Status |
|---|---|
| `RetrospectiveResearchReader` | not implemented — next phase, after independent review of this one |
| Identity-audit engine (corpus scan) | not implemented — this phase stores the result contract only |
| Historical Odds API client, snapshot download, market anchoring | not implemented |
| F1-R builder / executor | not implemented, **unauthorized** |
| F2 | not implemented, **unauthorized** |
| Production matching | **unauthorized** |
| Model training | **unauthorized** |
| Lane-L forward collection | not started |

Gates **G1** (extended EVENT_DERIVED features), **G2** (Kalshi EV/liquidity),
**G3** (pre-2024 weather), **G4** (pre-2022 rounding) and **G6** (terms review
before launch) remain open exactly as previously scoped. **G5** remains closed as
the corpus-scoped fail-closed contract; this migration implements its storage, not
a change to its verdict.

## 12. Files

**Added**

| File | Purpose |
|---|---|
| `sports_quant/db/migrations/f018_retrospective_provenance.sql` | the migration |
| `sports_quant/retrospective/__init__.py` | package surface |
| `sports_quant/retrospective/provenance.py` | enums, namespace key, digests |
| `sports_quant/retrospective/rules.py` | availability-rule registry |
| `sports_quant/db/repositories/retrospective.py` | repositories |
| `sports_quant/db/tests/test_retrospective_provenance_schema.py` | 74 tests |
| `sports_quant/db/tests/test_retrospective_provenance_repositories.py` | 49 tests |
| `sports_quant/pit/tests/test_strict_pit_unchanged_under_v18.py` | 16 tests |

**Modified**

| File | Change |
|---|---|
| `sports_quant/db/schema.py` | `CURRENT_SCHEMA_VERSION` 17→18; supported `{16,17,18}` |
| `sports_quant/db/ids.py` | five prefixes and factories |
| `sports_quant/db/models.py` | five frozen dataclasses |
| `sports_quant/db/repositories/__init__.py` | export the new repository |
| `sports_quant/pit/registry.py` | five tables classified `unsupported` |
| eight existing test modules | version-constant expectations follow `CURRENT_SCHEMA_VERSION` |
