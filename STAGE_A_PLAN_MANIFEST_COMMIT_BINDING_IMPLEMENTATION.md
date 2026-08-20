# B2 — Stage-A Plan Manifest Commit / Content Binding

**Starting HEAD:** `e98363d` (= `origin/main`, tree clean, schema v22 / 22 migrations / 61 tables).
**Schema after this task:** **v22 / 22 migrations / 61 tables — UNCHANGED. No migration.**
**Provider requests:** 0. **Credits:** 0.

## Verdicts

| Question | Verdict |
|---|---|
| **B2** | **CLOSED** |
| Schema change | **none — v22 sufficient** |
| Content-digest contract | **exact committed blob BYTES** (was newline-normalized text) |
| `stage-a-manifest-v1` schema | **CLOSED** — unknown fields refused |
| §AF / B3 | **still open** |

---

> **SUPERSEDED IN PART by the independent review**
> (`STAGE_A_PLAN_MANIFEST_COMMIT_BINDING_INDEPENDENT_REVIEW.md`), which
> reproduced three further defects against `40846d0`:
>
> - **git replacement objects** (`refs/replace/*`, honoured by default) let one
>   `git replace` silently substitute a different committed manifest while the
>   verifier reported the requested commit. Repaired with
>   `GIT_NO_REPLACE_OBJECTS=1`; `GIT_NO_LAZY_FETCH=1` added alongside it.
> - the closed schema still **coerced types** on known fields, so a committed
>   `60.9` certified as `60` and a JSON `null` became the string `"None"`.
>   Repaired with exact JSON types and strict-JSON constants.
> - a **symlink tree entry** was read as the manifest, returning the link target.
>   Repaired by requiring a regular-file tree mode.
>
> The review is authoritative wherever the two differ.

## 1. The defect, reproduced

`stage_a_plans` stored `manifest_commit_sha`, `manifest_content_digest` and
`manifest_path`, but certification only ever proved

```
DB plan  <->  caller-supplied StageAManifest  <->  caller-supplied text
```

Three values the caller controls agreeing with one another is not source-control
provenance. Reproduced at `e98363d`: a plan carrying a fabricated 40-character
commit id, a content digest matching the caller's own text, matching targets and
buckets, and otherwise valid acquisition provenance — certified.

`test_db_and_caller_agreement_is_not_source_control_provenance` pins exactly that
sentence.

## 2. Two further defects found while closing it

### 2a. The content digest was not over committed bytes (§18)

`load_committed_text` ran git with `text=True`, which applies Python universal
newline translation. Measured on a blob committed with CRLF:

```
true blob bytes     : b'{"a": 1,\r\n "b": 2}'
load_committed_text : '{"a": 1,\n "b": 2}'
sha256(blob)        : 38e2a1b8...
sha256(loaded)      : a90e8a13...
```

So a digest advertised as fingerprinting the committed file did not, and two
different committed artefacts could share one digest. Repaired with
`load_committed_bytes`, which uses `git cat-file blob` with no text decoding;
`load_committed_text` is now a UTF-8 decode of those exact bytes and refuses
invalid UTF-8. `manifest_content_digest_bytes` is the load-bearing form.

### 2b. `stage-a-manifest-v1` was an open schema (§19)

Unknown top-level fields and unknown per-target fields were silently ignored.
Measured: adding `"future_magic"` left `plan_digest` unchanged while the content
digest changed — so the committed artefact asserted something the parser dropped,
and a future producer could believe such a field was load-bearing.

For a frozen scientific manifest that is wrong, so v1 is now a **closed schema**:
unknown fields at either level are refused. Safe to tighten because no real
Stage-A v1 manifest has ever been declared.

## 3. The load-bearing chain

`load_committed_stage_a_manifest(commit, path, repo_root)` performs, in order:

```
resolve a real COMMIT object       (blob/tree/tag/short/uppercase all refused)
  -> validate the repository path  (absolute, .., ':', backslash, control chars)
  -> git cat-file blob             (exact bytes; working tree never consulted)
  -> decode UTF-8                  (invalid UTF-8 refused)
  -> StageAManifest.loads          (duplicate keys, unknown fields, structure)
  -> manifest_content_digest_bytes (exact artefact identity)
  -> plan_digest                   (semantic plan identity)
```

Returned as `CommittedStageAManifest`. No caller-supplied value participates.

**Two digests, deliberately distinct.** `manifest_content_digest` proves *this
exact artefact was committed* and is formatting-sensitive.  `plan_digest` proves
*this is the scientific plan* and is formatting-insensitive. A pretty-printed
copy of the same plan therefore has a different content digest and the same plan
digest — pinned by
`test_noncanonical_but_semantically_identical_text_still_binds`.

## 4. Trusted declaration API

`record_committed_plan(conn, manifest_commit_sha, manifest_path, repo_root)` is
the audited path. The caller supplies only a pointer; the function resolves,
loads, derives both digests, and persists the plan with its bucket and target
membership atomically. Git resolution happens **before** any scientific row is
written, so an unprovable artefact never reaches the database.

The old `record_plan` is renamed **`_record_plan_unverified`** and is private by
name, because it accepts the manifest, its digest and its commit id as three
independent caller claims — the exact shape B2 removes. It survives for synthetic
fixtures, and rows it writes are still refused by certification.

## 5. Certification and enrichment

`certify_stage_a(conn, acquisition_id, repo_root)` no longer takes a manifest or
manifest text. It loads the artefact named by the **plan row**, then compares
every manifest-derived persisted column and the full target/bucket membership
against it. If the artefact cannot be proven, it returns immediately — evaluating
later checks against an unproven document would be meaningless.

`enrich_corpus_with_market_lane` loses `manifests` and `manifest_texts` entirely.
"Skip the source-control binding" is no longer a representable mode on the only
call that creates downstream provenance.

## 6. What B2 does NOT prove

- **Scientific correctness.** A manifest mapping every target to an absurd bucket
  binds perfectly. `test_a_scientifically_wrong_manifest_still_binds` asserts
  this deliberately: B2 answers *"did we certify the exact committed artefact?"*,
  and **§AF** — still open — answers *"does that artefact follow
  official-hint → T−60 → 5-minute-floor?"*
- **Chronology.** Git commit timestamps are attacker-settable and a local history
  can be rewritten. The plan-before-network boundary remains the combination of
  committed-artefact binding, acquisition registration, the
  `requested_at >= registered_at` rule, and the append-only ledger.

## 7. Operational dependency

Verification **fails closed** when the named commit is absent locally: no fetch,
no working-tree fallback, no acceptance of the stored digest alone. A shallow
production checkout therefore cannot certify a plan until history is available.

All B2 mechanism tests build self-contained temporary repositories, so the suite
runs at any checkout depth — the lesson from B1's CI failure applied up front
rather than after a red build.

## 8. Schema — v22 sufficient

No migration. The existing columns already store the pointer and both digests;
B2 makes them real by verification. A stored copy of the committed text would
duplicate an immutable git object and add a forgeable field rather than a proof.

## 9. Tests

35 new B2 tests, all failing at `e98363d`: fabricated commit, DB/caller agreement,
atomic declaration, constraint-triggered rollback, working-tree independence in
both directions, commit/path swap, forged stored digests, DB-membership
divergence, byte-exact digest, CRLF vs LF distinctness, invalid UTF-8, closed
schema at both levels, duplicate keys, noncanonical text, 11 unsafe path shapes,
cross-repo-root portability, the §AF boundary, and the API shape.

The pinned blocker `test_retained_manifest_commit_sha_is_never_resolved` is
renamed `test_manifest_commit_sha_is_now_resolved` and asserts the refusal; its
sibling for enrichment is retargeted from "requires caller text" to "cannot
resolve the committed manifest", which is the stronger property. B1's 36 tests
remain green.

## 10. Status

| | |
|---|---|
| **B2** | **CLOSED** |
| **§AF** | **OPEN** |
| **B3** | **OPEN** |
| Real Stage-A plan declared | **NO** |
| Real probe registered | **NO** |
| Stage A run | **NO** |
| Linking provider / G5 / crosswalk / F1-R / E1 / P1 | **NO** |

**B2 is ready for independent adversarial review.**
