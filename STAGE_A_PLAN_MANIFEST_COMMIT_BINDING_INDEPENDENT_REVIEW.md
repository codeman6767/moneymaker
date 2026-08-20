# Independent Adversarial Review — B2 Plan Manifest Commit / Content Binding

**Reviewed artefact:** B2 at `40846d0` (schema v22 / 22 migrations / 61 tables, no migration in the commit).
**Starting HEAD:** `40846d0` = `origin/main`, tree clean. **Git 2.55.0.**
**Provider requests:** 0. **Credits:** 0.

## VERDICT: **ACCEPTED WITH REPAIRS**

Three defects were reproduced against `40846d0`, two of them **HIGH**. The
load-bearing chain B2 built is sound; two of its links could be bypassed by
mechanisms outside the implementer's model, and one repeated the exact defect
class B2 had just fixed one field-type deeper.

---

## D1 (HIGH, repaired) — git replacement objects defeat the whole binding

Git honours `refs/replace/*` **by default**. Reproduced end to end:

```
git replace -f C1 C2
asked for commit  : 5eb3adac8774   (M1, request_budget 5)
resolver reports  : 5eb3adac8774
budget returned   : 999            <- M2's manifest
```

The verifier reported it had resolved C1 while returning C2's committed content
and C2's derived digests. A verifier that can be told *"when you look up this
object, use a different one"* is not binding an object at all — and this is the
single claim B2 exists to make.

It needs no privileged access: one `git replace` in the local repository silently
redirects every subsequent verification. Nothing in the chain downstream can
notice, because every digest is recomputed from the substituted bytes and is
therefore internally consistent.

**Repair:** every load-bearing git read now runs with `GIT_NO_REPLACE_OBJECTS=1`.
Verified: the same attack now returns budget 5, and a repository with no replace
refs is unaffected.

## D2 (HIGH, repaired) — the closed schema still coerced types

B2 closed the schema against *unknown fields* but left `str(...)` / `int(...)`
conversions on every known field. All of these were **ACCEPTED** at `40846d0`:

| Committed JSON | Became | Effect |
|---|---|---|
| `decision_horizon_minutes: 60.9` | `60` | truncated |
| `decision_horizon_minutes: "60"` | `60` | string coerced |
| `decision_horizon_minutes: true` | `1` | `bool` is an `int` subclass |
| `request_budget: 5.7` | `5` | truncated |
| `league_id: 12345` | `"12345"` | number → text |
| `provider: true` | `"True"` | bool → text |
| `sport_key: null` | `"None"` | **JSON null became the literal string `None`** |
| target `canonical_game_id: null` | `"None"` | same, per target |

This is precisely the defect class B2 itself fixed: the committed artefact
asserts semantics the frozen parser silently rewrote, so the content digest and
the plan digest describe *different documents*. A manifest committing
`60.9` is certified as a plan that says `60`.

**Repair:** exact JSON types, no coercion — `bool` explicitly excluded from the
integer check, empty and outer-whitespace strings refused, buckets required to be
strings, integers bounded to the exactly-storable signed 64-bit range so SQLite
cannot round-trip a value differently from the artefact, and
`parse_constant` refusing `NaN` / `Infinity` / `-Infinity`, which Python's `json`
accepts by default.

## D3 (MEDIUM, repaired) — a symlink tree entry read as the manifest

`resolve_commit` checked the *revision* was a commit, but nothing checked the
*tree entry type*. A `120000` symlink entry is itself a blob holding a path
string, so:

```
load_committed_bytes(sym_sha, "plan.json") -> b'../outside/secret.json'
```

The provenance claim "this file was committed" is misleading for a symlink, and a
gitlink (`160000`) names another repository entirely.

**Repair:** the tree entry must be `100644` or `100755`. An executable regular
file is still accepted; symlinks and gitlinks are refused.

---

## Adjudications (no change required)

**Partial-clone lazy fetch (§4).** Git may fetch a missing object from a promisor
remote during an object command. Nothing was observed fetching here, but the
contract must be enforced rather than assumed, so `GIT_NO_LAZY_FETCH=1` is now
set alongside the replacement control. Verified that normal reads still work
under it.

**Attributes / autocrlf / filters (§5).** Confirmed the helper returns the blob
git **stores**. Worth stating precisely, because a test written the obvious way
fails: committing CRLF bytes under `*.json text eol=lf` stores an **LF** blob —
normalization happens on the way *in*. The correct property is therefore "the
helper returns exactly the blob the tree entry points at", cross-checked by
resolving the blob id independently via `ls-tree` rather than comparing
`cat-file` to itself. A later `core.autocrlf` flip cannot move it.

**Commit reachability (§7).** An unreachable orphan commit binds successfully.
**This is the correct contract**: refs are mutable — a branch can be force-pushed
or deleted — so requiring reachability would make verification depend on
something *less* stable than the object id it binds. The honest operational
requirement, now documented and pinned by a test: **the commit object must be
retained and locally available for future certification**, which a `gc` of an
unreferenced object would break.

**SHA-1 vs SHA-256 (§8).** Git commit identity is SHA-1, but the manifest is
independently bound by a SHA-256 content digest and a SHA-256 semantic plan
digest, both recomputed from the blob and compared against the plan row. A SHA-1
collision alone is therefore insufficient.

**`repo_root` boundary (§17).** With replacement disabled, git object identity
makes an arbitrary `repo_root` harmless: the same commit id can only carry the
same tree and blobs. Verified across two independent repositories producing
identical digests. Before D1 this was **not** true — that is what made
replacement objects severe rather than theoretical.

**`_record_plan_unverified` (§14) and direct SQL (§15).** Both confirmed
unusable: a row written by the private helper, and a fully fabricated plan row
with matching children inserted by SQL, are each refused by certification because
the gate re-resolves the artefact the row names. The safety claim is correctly
"the gate is unavoidable", not "the function is unreachable".

**Embedded `plan_digest` (§11).** Left OPTIONAL. The verifier always recomputes
it and refuses a mismatch, so its presence is redundancy rather than contract.
Documented so nobody assumes otherwise.

**§AF separation (§26).** Explicitly preserved and pinned: a manifest mapping
every target to `2029-12-31T23:55:00Z` binds perfectly and certifies its
source-control provenance. If a future change makes B2 reject that, §AF's
contract has been silently absorbed.

**Schema (§21).** v22 remains sufficient. No migration.

**B1 non-regression (§28).** All 36 B1 tests green. B1 shares the git helper and
therefore *inherits* the replacement and lazy-fetch hardening — a genuine
improvement to probe binding, since the same substitution attack applied to probe
reports.

**v1 freezing precondition (§22).** Verified: `stage_a_plans` is empty and no
`pilots/stage_a` artefact exists, so tightening `stage-a-manifest-v1` remains
safe. Pinned by a test. **Once the first real v1 plan is declared, semantic
changes to v1 are forbidden and require `stage-a-manifest-v2`.**

## Malicious chain — first guard for each

| Attack | First guard |
|---|---|
| Fabricated commit | `resolve_commit` — object does not exist |
| **Replacement-ref substitution** | **`GIT_NO_REPLACE_OBJECTS`** (was: none) |
| Arbitrary other repo | git object identity (only safe once D1 is fixed) |
| Working-tree / index swap | `cat-file blob` never reads the checkout |
| Content-digest swap | `plan_row_disagreements` |
| Semantic-plan-digest swap | `plan_row_disagreements` |
| **Type-coercion manifest** | **exact-type parse** (was: silently coerced) |
| **Symlink manifest path** | **tree-entry mode check** (was: returned link target) |
| Direct-SQL plan | certification re-resolves the artefact |
| `_record_plan_unverified` | same |

## Trust boundary — unchanged and not overstated

B2 proves *this database plan is bound to the exact bytes and semantics of a real
local git commit object*. It does **not** prove a truthful commit timestamp,
presence on any remote, branch reachability, immutability against local history
rewriting, or the scientific correctness of the plan.

## Validation

ruff clean · mypy clean (363 files) · `git diff --check` clean · schema
**v22 / 22 / 61**, no migration · protected artefacts 85/85 identical ·
**0 provider requests, 0 credits** · no network, including no git fetch.

§AF **OPEN** · B3 **OPEN** · no real plan declared · no probe registered ·
Stage A not run · F1-R blocked.

## Next authorization boundary

> **§AF — independent target → T−60 → first-pass bucket recomputation.**

B2 is closed. §AF is now the last gate before the real plan may be declared and
the 161-credit acquisition run.
