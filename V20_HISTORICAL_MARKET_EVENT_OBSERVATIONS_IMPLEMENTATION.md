# v20 — Historical Market Event Observations: Schema + Repository

> ## ⚠ SUPERSEDED IN PART — read the independent review
>
> `V20_HISTORICAL_MARKET_EVENT_OBSERVATIONS_INDEPENDENT_REVIEW.md` (2026-08-17)
> is **authoritative wherever it disagrees with this document**. Verdict:
> **ACCEPTED WITH REPAIRS**. It upheld the digest-compatibility argument, the
> content hash and `observed_at` exclusion, the deterministic id, and the
> strict-PIT classification — and reproduced **four defects**, repaired in
> migration **f021** (schema is now **v21 / 21 migrations / 53 tables**):
>
> - **D1** `REPLACE INTO` / `INSERT OR REPLACE` silently mutated append-only
>   rows. §8 of this document overstates the guarantee: f020 guarded only
>   `BEFORE UPDATE`/`BEFORE DELETE`, and SQLite REPLACE deletes without firing
>   DELETE triggers. **32 of 33 append-only tables share this; 5 are repaired,
>   28 remain as a documented pre-existing defect.**
> - **D2** A BLOB bypassed the event-id format CHECK (`GLOB` matches and
>   `length()` = 32 for a blob). §6's claim of two-layer enforcement held only
>   for TEXT. `typeof()` now required.
> - **D3** A forged `observation_content_hash` was stored and digest-bound;
>   nothing recomputed. A `verify_observation_content_hashes` verifier now must
>   pass before a corpus is digested, audited or curated.
> - **D4** `audited_source_tables` returned the linking set for any unknown
>   provider. Now fails closed against an empty `REGISTERED_LINKING_PROVIDERS`.
>
> Also recorded there: **L1**, an observation may still cite an unrelated valid
> same-provider 200 response (Stage-A parser must close it), and a **structural
> finding** that one corpus version cannot bind both an official and a linking
> audit.

**Starting HEAD:** `34a85a5` (`origin/main` = `34a85a5`, tree clean, schema v19 /
19 migrations). **Provider requests:** 0. **Credits spent:** 0.

Persistence foundation only. No identity resolution, no linking-provider
registry, no identity audit, no event→game map, no entitlement probe, no Stage-A
acquisition, no F1-R.

---

## 1. §0 first — the digest blast radius, adjudicated before the migration

### The measurement

Running the **real** `source_corpus_digest` over a real v19 database, before and
after simulating a naive global registration of a fourth audited table:

| | digest |
|---|---|
| v19, three audited tables | `ecfae814b90d3f6033abdd204669cbdb…` |
| v20, four audited tables | `cfe703aa3552c828ace0b28f9a7ca669…` |
| rows in the new table for `balldontlie` | **0** |

**The digest changes with zero relevant rows present.** `source_corpus_digest`
folds *one payload entry per audited table*, so a fourth table contributes a new
key (`{"rows": 0, "digest": sha256("")}`) to every corpus — pure breakage, no
information gained.

### What that would have broken (Q2)

`runner.py:185` recomputes the digest and writes it into the audit record.
`trg_xwk_audit_corpus_binding` then requires the audit's `source_corpus_digest`
to equal the corpus version's. So a naive registration means: every existing
accepted identity audit, every static crosswalk citing one, all TEAM-A
provenance, and every CI verifier that recomputes, begin failing **merely because
the software upgraded to v20** — against append-only rows that are historical
facts.

### The decisive structural fact (Q4)

**The digest is already provider-partitioned, and always has been.**

- `source_corpus_digest` calls `require_provider_league(provider, league_id)`,
  which **refuses** any provider absent from `PROVIDER_LEAGUES`. Verified:
  `the_odds_api:basketball_nba` raises `SourceCorpusError`.
- Every audited query filters `WHERE provider = ?`.

So an official-provider digest could never have contained a linking provider's
rows anyway. The market table would contribute a permanently empty set to every
official digest.

### Verdict — outcome B: a small, versioned compatibility mechanism

Option B *as written in the architecture* answers a different question. It says
which corpora need **new versions going forward**; it does not address old
corpora, which are collateral damage from a **global constant**. Both are needed.

Implemented: **the audited source set is selected by provider class.**

```python
def audited_source_tables(provider: str) -> tuple[str, ...]:
    if provider in PROVIDER_LEAGUES:      # official
        return AUDITED_SOURCE_TABLES      # the same three tables, unchanged
    return LINKING_SOURCE_TABLES          # the market observation table
```

This is not a new concept bolted on — it makes an existing partition explicit.
Consequences:

- **Every existing corpus digest is byte-identical under v20** (Q3: Option B
  coexists honestly). Nothing is edited, no digest is fabricated, no verification
  is disabled, and this is *not* implicitly Option A — no corpus declares
  anything, and no corpus's meaning changes.
- Old corpora keep their historical source-set semantics exactly.
- **No linking provider is registered at v20**, so the linking branch remains
  unreachable through `source_corpus_digest`, which still refuses any provider
  outside `PROVIDER_LEAGUES`. The mechanism authorizes nothing; it makes a later
  registration a reviewed one-line change instead of a silent global break.

This also resolves the §K boundary: registering the table for storage does not
authorize any acceptance policy, because storage registration and provider
authorization are now separate.

**Proof in test:** `test_an_official_corpus_digest_is_unchanged_by_market_rows`
writes two market observations and asserts the `balldontlie` digest is unchanged.

## 2. Migration f020

`sports_quant/db/migrations/f020_historical_market_event_observations.sql`.
Additive only: one table, three indexes, four triggers. **No existing table,
trigger, index or row is touched.** f018 and f019 are byte-identical (verified by
hash across all 19 pre-existing migration files).

### Table `historical_market_event_observations`

| Column | Type | Null | Notes |
|---|---|---|---|
| `observation_id` | TEXT PK | no | deterministic, `hme_` prefix |
| `league_id` | TEXT | no | FK `leagues` |
| `provider` | TEXT | no | qualified secondary namespace; grants no authority |
| `namespace_generation` | TEXT | no | |
| `sport_key` | TEXT | no | provider's own value |
| `provider_event_id` | TEXT | no | `GLOB` 32×`[0-9a-f]` + `length = 32` |
| `requested_at_bucket` | TEXT | no | what we asked for |
| `provider_snapshot_timestamp` | TEXT | no | what the provider answered with |
| `commence_time` | TEXT | **yes** | NULL is real evidence |
| `home_team_raw` | TEXT | no | verbatim |
| `away_team_raw` | TEXT | no | verbatim |
| `observation_content_hash` | TEXT | no | portable identity |
| `raw_response_id` | TEXT | no | FK `raw_responses` |
| `observed_at` | TEXT | no | our clock |
| `created_at` | TEXT | no | our clock |

**Absent by design:** `canonical_game_id`, `game_id`, `match_decision_id`,
`orientation`, any price/odds column, any event-status column. Stage A is
identity-free *by construction* — there is nowhere to record an identity claim,
and the provider's events payload has no status field, so one would be invented
evidence. Asserted by test.

**Uniqueness:** `(provider, namespace_generation, provider_event_id,
provider_snapshot_timestamp, observation_content_hash)` — the content hash is
part of the key so two content-distinct observations at one snapshot instant
**both survive**. That contradiction is what a G5-style audit exists to find;
deduplicating it destroys the evidence. Same shape as
`game_schedule_snapshots`' existing `UNIQUE (game_ref_id, observed_at,
content_hash)`.

**FK:** `raw_responses` is append-only, so the parent can never be deleted and
default RESTRICT is correct.

## 3. Deterministic `observation_id`

```
hme_ + sha256("historical_market_event_observation|<policy>|<content_hash>")[:24]
```

Follows the existing `prefix + sha256[:24]` convention (`canonical_game_id`,
`canonical_player_id`). It is a pure function of semantic content, so it replays
identically, differs for content-distinct observations that must coexist, and
depends on **no** rowid, wall clock or database-local id — a transported or
rebuilt corpus reproduces every id.

The repository refuses rather than overwrites if an id ever existed with a
different content hash. That cannot happen while the id derives from the hash;
it is there so a future change to the derivation cannot silently clobber
preserved evidence.

## 4. Content hash

Reuses the repository's existing `canonical_json` (sorted keys, tight
separators, `ensure_ascii=False`) — **no novel serialization**. Determinism,
insertion-order independence, JSON `null` as an unambiguous NULL encoding, and
UTF-8 bytes at the hash boundary all come from the existing convention.

**Participating:** `policy`, `league_id`, `provider`, `namespace_generation`,
`sport_key`, `provider_event_id`, `requested_at_bucket`,
`provider_snapshot_timestamp`, `commence_time` (including its NULL-ness),
`home_team_raw`, `away_team_raw`.

**Excluded:** `observation_id` (derived from the hash — circular),
`raw_response_id` (database-local), `created_at`.

### `observed_at` — adjudicated, not guessed

**Excluded.** The hash answers *"what did the provider say?"*; `observed_at` is
our materialization clock, not part of the provider's statement. Including it
would make one provider statement hash differently depending on when we happened
to write it down, breaking replay idempotence and allowing identical evidence to
be stored twice under two hashes. The architecture's §11b field list agrees, and
this implementation does not extend it. Proven by
`test_the_hash_excludes_our_clocks_and_db_local_ids`: the same observation
recorded under two different `observed_at` values yields one row, and the
original `observed_at` is **not** refreshed.

`OBSERVATION_CONTENT_POLICY_VERSION = "hme-observation-content-v1"` participates
in the hash, so an old hash can never be reinterpreted under new rules.

The hash is verified against an **independently constructed** digest built from
the spec in the test, not by calling the production helper twice.

## 5. Timestamp contract

Five clocks, kept apart and never conflated:

| Clock | Meaning |
|---|---|
| `requested_at_bucket` | the historical bucket **we requested** |
| `provider_snapshot_timestamp` | the instant the **provider answered** with |
| `commence_time` | contemporaneous event field in that snapshot; NULL is evidence |
| `observed_at` | our observation/materialization clock |
| `created_at` | DB record clock |

Neither of our clocks is ever backdated to a provider instant (asserted).
`requested_at_bucket` is never treated as the returned snapshot time (asserted
distinct).

Validation is **fail-closed at both layers** and uses the f019 D5 technique at
the DB: `GLOB` pins the byte-exact canonical spelling (including the upper-case
`Z`), `substr(...,12,2) <> '24'` keeps one spelling per instant, and an
`IFNULL`-guarded `strftime` round-trip rejects impossible calendar instants that
SQLite would otherwise normalize (2026-02-30 → 2026-03-02). A naive local
timestamp is **refused, never converted** — the offset it should have had is
unknowable, and guessing would shift a snapshot by hours while looking
well-formed.

## 6. Event-id format — reject, never repair

`^[0-9a-f]{32}$`, enforced in the domain type (`re.fullmatch`, so a trailing
newline cannot slip past `$`) **and** at the database (`GLOB` + explicit
`length = 32`, so direct SQL cannot bypass it).

Rejected, not normalized: uppercase hex · leading whitespace · trailing
whitespace · zero-width space · Cyrillic confusable (U+0435) · fullwidth Latin
confusable (U+FF45) · 31 chars · 33 chars · non-hex ASCII · trailing newline.
Each is tested twice — once through the domain type, once through direct SQL.

Trimming or case-folding a bad id into a good-looking one invents a *different*
identifier that would then coexist beside the real one, which is exactly the
defect the independent review reproduced against v19.

The read path validates too: `for_event` refuses a lookalike rather than
searching for it, so a confusable can never quietly return the real event's rows.

## 7. Raw-response binding — and its explicit boundary

**Enforced at the database**, deterministically and offline:

- the cited response must belong to the **same provider**;
- the cited response must be **HTTP 200**. A failed request is not evidence that
  a market did or did not exist — that conflation is precisely the hiding channel
  the architecture's §12.1 separates out.

**Deliberately NOT enforced here:** that the body contains this exact event id,
and that the wrapper timestamp equals `provider_snapshot_timestamp`. Both require
parsing the payload, which belongs to the **Stage-A acquisition parser**, not a
generic trigger. Putting a JSON parse in a trigger would make provider payload
shape a schema concern and couple the database to one response format.

The boundary stated plainly: an observation asserts *"this provider reported this
provider event in this historical snapshot"*. It does **not** assert *"this is
canonical game G"*.

## 8. Append-only enforcement

`trg_hme_no_update` and `trg_hme_no_delete` abort both operations. Tested through
**direct SQL**, not the repository API. FK enforcement stays on and is validated
(nonexistent league and nonexistent raw response both refused).

No trigger grants identity or canonical authority.

## 9. Repository API

`SqliteMarketObservationRepository` — narrow by design:

- `record(observation, raw_response_id, observed_at=None)` — idempotent on the
  deterministic id; returns `created=False` on replay and does not refresh
  `observed_at`
- `get(observation_id)`
- `for_namespace(provider, namespace_generation, league_id)` — the audit's input,
  totally ordered by content-derived keys
- `for_event(provider, namespace_generation, provider_event_id)` — exact key only
- `distinct_event_ids(...)` — the audit's `distinct_ids` population

**No** update, delete, upsert, merge, link or resolve method (asserted by test).
No canonical-game lookup, no name/alias lookup, no fuzzy matching, no sportsbook
matcher, no provider client.

## 10. Migration behaviour

| Check | Result |
|---|---|
| Fresh init | **20 migrations, schema v20, 53 tables** |
| Idempotent replay | applies nothing, version unchanged |
| v17 → v20 | succeeds, older rows preserved, applies exactly 18–20 |
| v18 → v20 | succeeds, applies exactly 19–20 |
| v19 → v20 | succeeds, applies exactly 20 |
| f018 / f019 and all 19 older files | **byte-identical** (hash-verified) |
| Protected DBs | not rebuilt, not opened for write |

`CURRENT_SCHEMA_VERSION` → 20; `SUPPORTED_SCHEMA_VERSIONS` gains 19 so v16–v19
corpora stay readable and their manifests stay valid.

## 11. Validation

| Check | Result |
|---|---|
| `git diff --check` | clean |
| `ruff check .` | all checks passed |
| `mypy .` | success, **347 source files** |
| **Full `pytest`** | **3149 passed, 3 skipped** |
| v20 suite | **89 passed** |
| Zero-network guards | **31 armed, 11/11 probes blocked** |
| Protected artefacts + 19 existing migrations | **82 / 83 unchanged** on every dimension; 1 volatile `-shm` mtime, characterized below |
| Provider requests / credits | **0 / 0** |
| Secrets / generated DBs / raw payloads staged | none |

### Protected evidence — the one deviation, with its cause located

82 of 83 baselined artefacts (including all 19 pre-existing migration files) are
unchanged on sha256, size, mtime and both sidecars. The single deviation is the
**`-shm` mtime** of `data/f1_nba_2026_03_scratch.db`:

| Dimension | Result |
|---|---|
| DB sha256 | **unchanged** |
| DB size / mtime | **unchanged** |
| `-wal` size / mtime | **unchanged** |
| `-shm` **size** | **unchanged** |
| `-shm` mtime | moved 2026-08-16T23:05:50 → 2026-08-17T08:04:17 |

**Cause, located in existing code rather than inferred:**
`sports_quant/ingest/tests/test_nba_lineup_continuation.py:876-880` —
`test_committed_recovery_manifest_regenerates_byte_identically` — is a
pre-existing test that reads the real protected March corpus to recompute a
`source_database_fingerprint`, skipping only when the corpus is absent. Reading a
SQLite database in a non-`immutable` mode builds the shared-memory index and moves
the `-shm` mtime. The bump falls inside the full-suite window.

**Nothing in v20 touches `data/`** — verified: no v20 source, migration or test
file references that directory (the only textual match is the test docstring
saying so). The `-shm` is a volatile shared-memory index; its mtime moving with
unchanged size, while the database and WAL are byte-identical, is not a mutation
of evidence. Recorded rather than rounded to 83/83.

## 11a. What the full suite caught, and how it was repaired

The first complete run failed **14 tests**. They fell into two groups, and only
one group was a nuisance.

### A genuine signal: the strict-PIT registry did not cover the new table

`test_registry_exactly_covers_schema_v16` and
`test_registry_still_exactly_covers_the_live_schema` failed with
`{'missing': {'historical_market_event_observations'}}`. The PIT registry
requires **every** live table to be explicitly classified, and an unclassified
table is precisely the hole through which Lane-R evidence could reach a
strict-forward dataset row.

Repaired by registering it as **`_unsupported`** in `pit/registry.py`, alongside
the other f018 Lane-R tables, with the reason recorded: retrospectively acquired
secondary-provider evidence, never a predictor and never a price; reaching it
from a forward row would import a snapshot fetched long after that row's cutoff;
and it carries no canonical game id, so it cannot be joined to a dataset row even
by accident.

This test earned its keep — it is the mechanism that forces the isolation
decision to be made explicitly rather than by omission.

### Version pins: 9 tests asserting `== 19`

Each was updated to its *real* invariant rather than bumped mechanically:

| Test | Repair |
|---|---|
| `test_wheel_includes_every_migration_through_f019` | renamed to `…_f020`; the invariant is "the wheel ships **every** migration" |
| `test_schema_version_is_current` (identity audit) | dropped the redundant `== 19`; `== CURRENT_SCHEMA_VERSION` *is* the invariant |
| `test_fresh_database_initializes_at_the_current_version` | same |
| `test_schema_is_at_the_current_version` (strict PIT) | same |
| `test_v19_schema_version_and_migration_count` | contiguity now bounded by `CURRENT_SCHEMA_VERSION`; f018/f019 asserted to keep **positions 18 and 19**, so a later migration cannot renumber applied history |
| `test_v18_database_migrates_to_v19` | renamed `…_to_the_current_version`; still guards that a v18 corpus survives with rows, `integrity_check` and `foreign_key_check` intact |
| `test_schema_is_unchanged_at_v19` ×2, `test_schema_unchanged_at_v19` | now assert `len(discover_migrations()) == CURRENT_SCHEMA_VERSION`, which still catches a migration slipped in by a TEAM-A change without pinning a number another task legitimately moves |

No failure was waived, and no assertion was deleted to make a number agree.

### CI #111: the workflow itself pinned 19 (four more)

The first push passed `Ruff, mypy, pytest` and **failed the wheel-install smoke
job**. The cause was not the wheel — f020 packages correctly — but four literals
inside `.github/workflows/ci.yml`:

| Location | Repair |
|---|---|
| wheel-contents check: `len(migs) == 19 and migs[-1] == "f019_…"` | now compares the wheel's migration set against the **source tree's**, which catches a migration that failed to package regardless of count and needs no future edit |
| installed-wheel check: `len(migs) == 19 and migs[-1].name == "f019_…"` | now asserts `len(migs) == CURRENT_SCHEMA_VERSION` **and** that versions are contiguous from 1 |
| `grep -q "Schema version: 19"` | now derives the expected version from the installed package |
| checkpoint-provenance step: `assert CURRENT_SCHEMA_VERSION == 19` | now asserts the declared version equals the discovered migration count **and** is in `SUPPORTED_SCHEMA_VERSIONS` — which is what actually keeps a preserved checkpoint's manifest valid across an additive migration |

Each was reproduced locally before repair: the wheel was built and the
contents check re-run (20 migrations, last `f020_…`), and the installed-package
assertions and `db-init` output (`Schema version: 20`) were verified. The YAML
parses and all seven inlined Python heredocs are syntactically valid.

These four were genuine failures introduced by this task. The repairs make the
workflow assert the *invariant* (every migration ships; the declared version and
the packaged sequence agree) rather than a number that moves with every additive
migration.

## 12. Non-regression

**Provider authority** — asserted by test: `the_odds_api:basketball_nba` is
absent from `OFFICIAL_PROVIDER_BY_LEAGUE`, `PROVIDER_LEAGUES`,
`ATTESTED_GENERATIONS` and `QUALIFIED_PROVIDERS`; the official set is still
exactly `{mlb_statsapi, balldontlie}`; **no `LINKING_NAMESPACES` was added**.

**No identity created** — writing an observation leaves `games`,
`provider_game_references`, `static_crosswalk_provenance` and
`identity_audit_records` counts unchanged.

**Strict PIT** — `AsOfReader` and `_feature_cutoff` are untouched; asserted that
`pit/asof.py` contains no reference to the new table, so it cannot become a
Lane-L feature source.

**TEAM-A** — the corpus `static_identity_map_digest` contract is untouched;
official-provider digests are byte-identical.

## 13. Limitations

- The table stores evidence; it proves nothing about identity. That is the point.
- Payload-level validation (body contains this event id; wrapper timestamp
  matches) is deferred to the Stage-A parser — §7.
- The linking-provider digest branch is implemented but **unreachable** until a
  linking namespace is authorized, so it is exercised only by unit test, not by
  an end-to-end audit.
- `source_corpus_digest` for a linking provider has never been computed over real
  evidence, because no such evidence exists yet.

## 14. Scope — what was NOT done

Stated explicitly: **the entitlement probe was NOT run · Stage-A acquisition was
NOT performed · no identity audit was run · no event→game map was created · no
linking-provider registry was added · no crosswalk was generated · no canonical
game was created or mutated · F1-R was NOT executed · no E1 price evidence was
fetched · no MLB, feature or model work.** Zero provider requests, zero credits.

## 15. Readiness

**v20 is ready for independent adversarial review.** It has not been
self-reviewed. The reviewer should attack, at minimum: the §0 digest
compatibility argument; the content-hash field set and the `observed_at`
exclusion; the deterministic-id derivation; the raw-response validation boundary
in §7; and whether the DB-level format check can be bypassed.

## 16. Exact next authorization boundary

1. **Independent adversarial review of v20** — next task.
2. Bounded entitlement probe — may proceed at v19/v20 in parallel, own cap
   (≤10 requests / ≤100 credits), claiming no identity, subject to the
   architecture's §13 re-materialization constraint.
3. Stage-A discovery acquisition — only after 1, first-pass plan only.
4. Stage-B audit + Stage-B2 curation, then their own review.
5. Full March target-anchor acquisition.
6. E1 economic evidence.

**F1-R remains blocked.**
