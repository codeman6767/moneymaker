# Independent Adversarial Review — v20 Historical Market Event Observations

**Object under test:** v20 as shipped at `c56c4dc` (implementation `16b8f46`).
Treated as a set of hypotheses, not as authority.

**Starting HEAD:** `c56c4dc` (`origin/main` = `c56c4dc`, tree clean, schema v20 /
20 migrations / 53 tables). **Provider requests:** 0. **Credits spent:** 0.

---

## Verdict

> ## ACCEPTED WITH REPAIRS
>
> The v20 design is sound and its central claims survive: the digest
> compatibility argument is correct, the content hash is collision-resistant and
> independently reproducible, the deterministic id is portable, and the
> strict-PIT classification holds. **Four defects were reproduced against
> `c56c4dc`**, two of them serious, and all four are repaired here.
>
> One **retained limitation** is recorded rather than repaired, and one
> **repository-wide pre-existing defect** was discovered and is documented for a
> dedicated task.

| # | Defect | Severity | Status |
|---|---|---|---|
| D1 | `REPLACE` silently mutated append-only rows | **High** | repaired (f021) |
| D2 | A BLOB bypassed the exact event-id format contract | **High** | repaired (f021) |
| D3 | A forged `observation_content_hash` was stored and digest-bound | Medium | repaired (verifier) |
| D4 | `audited_source_tables` defaulted instead of refusing | Medium | repaired (fail-closed) |
| L1 | An observation may cite an unrelated valid response | — | **retained**, documented |
| P1 | 28 further append-only tables share the D1 bypass | **High** | pre-existing, documented |

**Ending state:** schema **v21 / 21 migrations / 53 tables** (f021 is
triggers-only). f018, f019 and **f020 are byte-identical** — the repair appends.

---

## 1. Method

Every claim about the database was attacked with **direct SQL**, not through the
repository, because a guarantee that holds only when the caller cooperates is not
a guarantee. Every re-derivation of a hash or digest was built from the
documented rule rather than by calling the production helper twice.

54 new tests in `sports_quant/db/tests/test_v20_independent_review.py`. Each test
in a DEFECT section fails at `c56c4dc` and passes after the repairs.

Zero network throughout: 31 guards armed before any provider-facing import,
11/11 adversarial probes blocked.

---

## 2. D1 — `REPLACE` silently mutated append-only rows  **[High]**

### Reproduction at `c56c4dc`

```sql
-- row already stored with home_team_raw = 'H'
REPLACE INTO historical_market_event_observations (...) VALUES ('hme_real', ... 'X' ...);
-- ACCEPTED.  SELECT home_team_raw -> 'X'
```

`INSERT OR REPLACE` behaved identically. The v20 suite tested only `UPDATE` and
`DELETE`, so this passed unnoticed.

### Cause

f020's guards are `BEFORE UPDATE` and `BEFORE DELETE`. SQLite's REPLACE conflict
resolution performs an **implicit DELETE that does not fire DELETE triggers**
unless `PRAGMA recursive_triggers` is ON. A pragma is per-connection, so the
guarantee cannot live there — an attacker simply does not set it.

`UPSERT ... DO UPDATE` *was* correctly refused (it fires the UPDATE trigger), which
is why the hole is easy to miss.

### Repair

`BEFORE INSERT` guards, which fire **before** conflict resolution and therefore
hold on any connection. Verified: the REPLACE now aborts and `home_team_raw` is
unchanged.

**The first form of this repair was wrong, and the suite caught it.** A bare
`WHEN EXISTS (same primary key)` guard broke `test_d010_integrity`: `INSERT OR
IGNORE` is a legitimate idempotent re-insert used across the codebase, and
`RAISE(ABORT)` is **not** suppressed by `OR IGNORE` — only `RAISE(IGNORE)` is —
so the guard turned a harmless no-op into a hard error. The guards now compare
**content** (each table's existing digest column) and fire only when a row with
that key exists *and* the incoming content differs. Behaviour:

| Case | Result |
|---|---|
| identical re-insert | trigger silent; caller's conflict mode applies, unchanged |
| REPLACE with changed content | **ABORT** — the defect is closed |
| `OR IGNORE` with changed content | **ABORT** — an attempted mutation in idempotent clothing must not be discarded silently |

Both branches are regression-tested.

Applied to the five tables the v20 provenance chain runs through:
`historical_market_event_observations`, `raw_responses`, `identity_audit_records`,
`static_crosswalk_provenance`, `reconstruction_corpus_versions`.

No production code anywhere uses `REPLACE INTO` or `INSERT OR REPLACE`
(verified), so these guards break no existing writer.

### P1 — the same bypass exists on 28 more tables *(pre-existing, not repaired here)*

Enumerated: **32 of 33** append-only tables had only UPDATE/DELETE guards
(`reconstructed_input_provenance` was the sole exception, for unrelated reasons).
The five above are repaired. The remaining 28 — including
`game_schedule_snapshots`, `provider_team_identity_snapshots`,
`sportsbook_price_snapshots`, `entity_match_decisions` and every typed
observation table — remain exposed.

This is a **pre-existing defect that predates v20** and is larger than this
review's scope; sweeping 28 tables into a review commit would be a substantial
unreviewed change. **Recommended as a dedicated task**, with the evidence and the
fix pattern both established here.

---

## 3. D2 — a BLOB bypassed the exact event-id format contract  **[High]**

### Reproduction at `c56c4dc`

Inserting the BLOB `b'be25eb82b82629d959c1e5ccb8dcc1e7'` was **ACCEPTED** and
stored with `typeof = 'blob'`.

### Cause

f020's CHECK is `GLOB '<32 hex classes>' AND length(...) = 32`. Measured against
that blob: **`GLOB` returns 1, `length()` returns 32, `typeof()` returns
`'blob'`.** SQLite coerces for GLOB comparison, counts blob bytes for `length()`,
and **exempts BLOBs from TEXT-affinity conversion**, so the value stays a blob.

This matters beyond tidiness. The reviewed contract is exact lowercase ASCII hex
stored byte-for-byte, and the resolver's guarantee is exact key equality. A blob
does **not** compare equal to the corresponding TEXT parameter, so such a row is
present in the table and **invisible to every lookup** — the worst of both.

The v20 suite tested ten hostile *strings*, all correctly refused, but no
non-TEXT storage class.

### Repair

`typeof()` is the only predicate that distinguishes them. f021 adds a
`BEFORE INSERT` trigger requiring `typeof = 'text'` for `provider_event_id` and
for every other column whose exact bytes are part of the observation's identity.
Verified refused, and tested per-column.

---

## 4. D3 — a forged content hash was stored and digest-bound  **[Medium]**

### Reproduction at `c56c4dc`

A direct INSERT with `observation_content_hash = 'not-a-real-hash'` was accepted;
the repository returned the row unchallenged; and
`_LINKING_DIGEST_COLUMNS` folds the **stored `observation_content_hash` column**
rather than a recomputation — so the fabricated row would be digest-bound exactly
as if genuine.

Nothing between "a row exists" and "a row is audit-grade" existed.

### Repair

`verify_observation_content_hashes(conn)` re-derives both the content hash and
the observation id from each row's own semantic columns and reports every
disagreement. Deterministic, offline, read-only. **It must pass before an
observation corpus is digested, audited or curated** — that obligation is now
stated in the implementation document.

This does not add canonical identity, and it does not weaken the digest; it makes
the digest's input checkable.

---

## 5. D4 — the audited-source classifier defaulted instead of refusing  **[Medium]**

### Reproduction at `c56c4dc`

```
audited_source_tables("banana")        -> ('historical_market_event_observations',)
audited_source_tables("")              -> ('historical_market_event_observations',)
audited_source_tables("BALLDONTLIE")   -> ('historical_market_event_observations',)
audited_source_tables(" balldontlie")  -> ('historical_market_event_observations',)
```

Anything absent from `PROVIDER_LEAGUES` silently resolved to the linking set.

This is **the same fail-OPEN shape G5 repair R4 closed** in
`ATTESTED_GENERATIONS`, where a caller typing `banana` obtained a *verified*
namespace. A defaulting classifier turns a typo into a silently different — and
silently **smaller** — audited subset. It was unreachable through
`source_corpus_digest` today, but `audited_source_tables` is a public exported
function and the identity task is precisely what will wire it up.

### Repair

An explicit, currently **empty** `REGISTERED_LINKING_PROVIDERS` frozenset.
Official → the three-table set; registered linking → the linking set; **anything
else raises `SourceCorpusError`**. Registering a linking provider is now the
reviewed decision the identity task must make, not a default.

---

## 6. Old-corpus digest compatibility — the implementation's argument **HOLDS**

Independently re-derived, answering the ten questions posed:

1. **How is the source set chosen?** By provider, via `audited_source_tables`.
2. **What persisted fact determines it?** *None* — and this is the honest answer.
   It is derived from **current software registries**, not from immutable corpus
   provenance.
3. **Derived from provenance or current software?** **Current software.**
4/5. **Could a registry change retroactively alter an old corpus's source set?**
   For official providers, only if `balldontlie`/`mlb_statsapi` were *removed*
   from `PROVIDER_LEAGUES` — which would break far more than digests and is
   guarded by an existing equality test against `OFFICIAL_PROVIDER_BY_LEAGUE`.
   After D4's repair, *adding* a provider cannot silently reclassify anything,
   because unknown providers no longer resolve at all.
6. **Is this Option A under another name?** No. Option A scoped the digest by a
   corpus's *declaration*; this scopes by provider, and no corpus declares
   anything. No corpus's meaning changes.
7. **Can a corpus hide market rows behind an official label?** The official
   digest genuinely ignores market rows — confirmed. But it cannot be exploited
   today: a linking-provider audit's digest would differ from an official-digest
   corpus's, and `trg_xwk_audit_corpus_binding` requires equality, so it fails
   closed. **See §11 for the structural consequence.**
8. **Can an old corpus inherit the new table?** No — official providers are
   matched by exact membership.
9. **Is the policy versioned?** `SOURCE_DIGEST_POLICY_VERSION` is unchanged,
   correctly: the official payload is byte-identical. The payload also *contains
   the table names as keys*, so an official and a linking digest can never
   collide or be mistaken for one another.
10. **Do identical old corpora still reproduce identical digests?** Yes.

**Proof:** an official-provider digest is byte-identical before and after two
market observations are written, and the digest is independently reconstructed
from the documented rule (policy · league · provider · one sorted, hashed entry
per audited table) and matches.

---

## 7. Content hash and `observed_at` — adjudicated independently

### The field set is correct

Independently reconstructed and matched byte-for-byte against a digest built from
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.

### Collision surface: clean

Ten adversarial pairs, all distinct: NULL vs string · a literal `"null"` label ·
delimiter shift (`a|b`/`c` vs `a`/`b|c`) · JSON quote injection · embedded
newline · embedded NUL · NFC vs NFD · `"1"` vs `"01"` · home/away swap ·
bucket/snapshot swap. JSON object keying is **structurally unambiguous**, so no
delimiter-injection collision is constructible. Stability across processes is
verified in a subprocess with `PYTHONHASHSEED=1`.

### `observed_at` — excluded, and that is right

**Adjudication: `observed_at` is local materialization provenance, not part of
the semantic market observation.** The observation asserts *what the provider
said at its snapshot instant*. Three provider-side clocks already carry that
(`requested_at_bucket`, `provider_snapshot_timestamp`, `commence_time`);
`observed_at` records only when *we* wrote it down.

Testing the alternative decides it: if `observed_at` were included, copying the
exact same evidence into a second reconstruction database would change its
portable identity purely because the local clock differed — which is precisely
the non-portability the design rejected `raw_response_id` for. The excluded case
has no matching harm: where local receipt timing genuinely matters it is
recoverable from `observed_at` and from the cited `raw_responses` row, neither of
which the hash removes.

`created_at`, `raw_response_id` and `observation_id` exclusions are likewise
correct (DB-local, DB-local, and circular respectively).

---

## 8. Deterministic `observation_id` — sound

`hme_ + sha256("historical_market_event_observation|<policy>|<content_hash>")[:24]`,
following the existing `canonical_game_id`/`canonical_player_id` convention. It
is a pure function of semantic content: replay-stable, portable, independent of
rowid, wall clock, insertion order and `raw_response_id`; distinct for
contradictory observations that must coexist.

**On duplication with the content hash:** they are not competing identities — the
id is a *derived shortening* of the hash. The one risk is that a caller could
supply an id and hash that disagree, which is exactly D3, now detected by the
verifier. The repository additionally refuses (never overwrites) if an id exists
with a different hash.

---

## 9. Timestamp enforcement — holds at both layers

Direct SQL refuses: missing microseconds · naive (no `Z`) · offset-bearing ·
lowercase `z` · Feb 30 · month 99 · hour 24 · non-timestamp text. The f019
technique — `GLOB` for byte-exact shape, `IFNULL`-guarded `strftime` round-trip
for impossible calendar instants — is correctly reused. Naive values are refused,
never converted. After f021, non-TEXT storage classes are refused too.

---

## 10. Raw-response binding — **L1, a retained limitation**

**Refused** (database-enforced, deterministic): a different provider's response;
a non-200 response. Both correct, and the non-200 check is load-bearing — it is
what stops a failed request reading as evidence of market absence.

**Still accepted** (reproduced): an observation citing a *same-provider, HTTP 200*
response whose body contains no such event, has different teams, a different
snapshot timestamp, or is a **current `/odds`** response rather than a historical
`/events` one.

**Adjudication: acceptable as a storage primitive, but only if the trust boundary
is named.** The database cannot check body contents without parsing the payload,
and putting a JSON parse in a trigger would make provider response shape a schema
concern. The implementation's boundary is right.

What was missing is the explicit statement of *which verifier* makes a row
trustworthy. Recorded now, and both obligations belong to the Stage-A task:

> Before any observation corpus may be digested, audited or curated, it must pass
> **(a)** `verify_observation_content_hashes` (D3, exists now), and **(b)** a
> Stage-A parser check that each observation is actually derivable from the body
> of the `raw_responses` row it cites, including the endpoint family and the
> wrapper snapshot timestamp.

**A fabricated observation must not become audit-grade merely by existing in the
table.** Today it cannot — see §12 — but that is because the chain is closed at
the front, not because the citation is verified.

---

## 11. Structural finding for the identity task — one corpus, one digest

`reconstruction_corpus_versions` records a **single** `source_corpus_digest` and
no provider. Under v19 that was unambiguous, because every provider's digest
covered the same table set. Under provider-scoped sets it is not: an official
audit and a linking audit over the same database produce **different** digests,
and `trg_xwk_audit_corpus_binding` requires the audit's digest to equal the
corpus's.

**Consequence:** a corpus cannot bind both a TEAM-A official audit *and* an Odds
event-id audit. It can bind one.

This is not a v20 defect — v20 correctly fails closed — but the identity task
**must** resolve it before Stage-B, and it is not addressed by anything currently
written. Options for that task: per-provider digest columns/rows; a composite
corpus digest; or separate corpus versions per provider lane. Not adjudicated
here.

---

## 12. Malicious trust chain — where it fails, exactly

Constructed: fabricate an observation by direct SQL → digest → audit → crosswalk.

**It fails at the first step.** `source_corpus_digest` refuses the Odds provider
(`SourceCorpusError`), because no linking provider is registered. So the
fabricated row cannot be digested, cannot bind an audit, and cannot back a
crosswalk. Verified: zero audit records, zero crosswalks, zero games.

**The exact guard is `REGISTERED_LINKING_PROVIDERS` being empty** — not the
raw-response checks and not the content hash. That is worth stating plainly,
because it means the guard disappears the moment the identity task registers a
provider, at which point §10(b) and §11 both become mandatory.

The previously reviewed **D2 direct-SQL weakness** (forging
`namespace_verified = 1`) still exists; v20 does not make it worse, and f021's
`trg_ida_no_replace` narrows the adjacent REPLACE path.

---

## 13. Strict-PIT classification — sufficient

`historical_market_event_observations` is registered `_unsupported`. Verified:
the registry covers every live table exactly (no missing, no extra); `AsOfReader`
and `_feature_cutoff` are materially unchanged; `pit/asof.py` contains no
reference to the table; and there is no reflection, wildcard registration or
schema-introspection fallback that could admit it. `classify()` on an unknown
table still raises `UnknownTableError`.

The registry's exact-coverage test is what forced the classification to be made
explicitly rather than by omission — it earned its keep.

---

## 14. Provider authority — no regression

`the_odds_api:basketball_nba` remains absent from
`OFFICIAL_PROVIDER_BY_LEAGUE`, `PROVIDER_LEAGUES`, `QUALIFIED_PROVIDERS` and
`ATTESTED_GENERATIONS`; **no `LINKING_NAMESPACES` exists**; writing an
observation creates no canonical game, no provider game reference, no audit and
no crosswalk, and mutates no canonical state.

---

## 15. Migration and CI review

**Migrations.** Fresh init reaches v21 / 21 migrations / **53 tables** (f021 adds
triggers only, proven by comparing table counts at v20 and v21). v17→v21,
v18→v21, v19→v21 and v20→v21 all succeed, preserve rows, and pass
`integrity_check` and `foreign_key_check`. Replay is idempotent. f018, f019 and
f020 are byte-identical.

**The 12 version pins the implementation changed.** Reviewed individually. None
was weakened to make a number agree: "every migration ships" and "the declared
version equals the discovered count" are *stronger* than the literals they
replaced, because they now fail if the two ever disagree. The one to watch was
`test_v19_schema_version_and_migration_count`, which could have lost its point;
it did not — it still asserts f018 and f019 keep applied positions 18 and 19, so
a later migration cannot renumber applied history.

**CI.** The implementation's move to derive the wheel's expected migrations from
the **source tree** is genuinely stronger, not merely dynamic: the two sides come
from different places (a built artifact vs. the checkout), so it catches a
packaging omission of *any* migration rather than only a count change. The
installed-package assertion is self-derived, but it is paired with a contiguity
check and with the source-tree comparison in the same job, so a broken
`CURRENT_SCHEMA_VERSION` cannot pass both. Adequate.

---

## 16. Test-quality audit of the 89 v20 tests

Mostly sound. The content-hash test correctly rebuilds the expected digest from
the spec rather than calling the helper. Direct-SQL bypasses were tested for the
event-id format and for UPDATE/DELETE.

Gaps found, all now covered by the 54 review tests:

- append-only tested only for `UPDATE`/`DELETE` — **`REPLACE` omitted** (D1);
- event-id format tested only for hostile *strings* — **no storage-class attack**
  (D2);
- `observation_content_hash` never recomputed from a stored row (D3);
- `audited_source_tables` tested only for the two official providers — **no
  unknown-provider case** (D4);
- raw-response relation tested only for the two refused cases, never for what is
  *accepted* (L1);
- digest compatibility tested on one provider only.

---

## 17. Validation

| Check | Result |
|---|---|
| `git diff --check` | clean |
| `ruff check .` | all checks passed |
| `mypy .` | success, 347 source files |
| **Full `pytest`** | **3204 passed, 3 skipped** |
| Independent-review suite | **54 passed** |
| Schema | **v21 / 21 migrations / 53 tables** |
| f018 / f019 / **f020** | byte-identical |
| Zero-network guards | **31 armed, 11/11 probes blocked** |
| Provider requests / credits | **0 / 0** |

**Protected evidence — isolated, not inherited (§R).** 83 of 84 baselined
artefacts (including all 20 pre-existing migration files) are unchanged on every
dimension. The single deviation is again the **`-shm` mtime** of
`data/f1_nba_2026_03_scratch.db`, with DB sha256, size, mtime, the `-wal` and the
`-shm` **size** all byte-identical.

Independently isolated rather than accepting the implementation's account: the
review's own 144 tests were run alone against a fresh mark and **did not move the
sidecar**. The movement occurs only during the full suite, from the pre-existing
`test_nba_lineup_continuation` test that fingerprints the March corpus. This is
metadata movement on a volatile shared-memory index, not evidence mutation.

**Reported as 83/84, not rounded to 84/84.**

---

## 18. Exact next authorization boundary

v20 is accepted with the above repairs and **no retained blocker for the schema
itself**. The next safe task is:

> ### The bounded entitlement probe.
> It needs no identity, no linking provider and no Stage-A plan; it claims
> nothing; and v19/v20 `raw_responses` preserves it honestly. Own cap
> (≤10 requests / ≤100 credits), subject to the architecture's §13
> re-materialization constraint.

Then, in order:

1. **Stage-A acquisition** — first-pass plan only, after the probe establishes
   entitlement.
2. **The linking-provider / G5 identity task**, which must additionally resolve:
   - §11 — one corpus version cannot bind both an official and a linking audit;
   - §10(b) — the Stage-A parser check binding an observation to its cited body;
   - registering `REGISTERED_LINKING_PROVIDERS` and `ATTESTED_GENERATIONS`, with
     the disjointness tests the architecture requires.
3. **P1** — a dedicated task adding BEFORE INSERT existence guards to the
   remaining 28 append-only tables.
4. Full March acquisition, then E1.

**F1-R remains blocked.**
