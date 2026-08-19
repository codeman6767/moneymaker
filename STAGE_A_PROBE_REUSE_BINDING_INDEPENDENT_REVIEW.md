# Independent Adversarial Review — B1 Probe-Reuse Binding

**Reviewed artefact:** B1 at `65ba376`, CI repair at `ac36cc9`.
**Starting HEAD:** `ac36cc9` = `origin/main`, tree clean, schema v22 / 22 migrations / 61 tables.
**Provider requests:** 0. **Credits:** 0. Network surfaces blocked 4/4 throughout.

## VERDICT: **ACCEPTED WITH REPAIRS**

One **HIGH** defect was reproduced and repaired: the fingerprint policy accepted
the published event-id set as an alternative to the body hash, which is not a
fingerprint at all. Everything else B1 claimed held up under attack, and the
real-probe verdict is confirmed — more strongly than B1 itself stated.

---

## D1 (HIGH, repaired) — a published identifier is not a fingerprint

B1 correctly rejected binding on *descriptive* facts, then accepted a fingerprint
alternative with the same flaw one level subtler. The frozen policy required
"body SHA-256 **and/or** the exact event-id set". But **the report publishes the
event ids**, so they are a KNOWN value: anyone who reads the report can construct
a body containing them.

Reproduced against `ac36cc9` — a report committing ids only, and a preserved body
carrying those same ids with entirely fabricated team names and commence times:

```
real body sha  : 2dd00c94dc1d8c0293d63750
forged body sha: 882cf91090765541d264c948
RESULT: BOUND -> raw_forged
```

The identical forgery is refused when the report commits `body_sha256`, which is
the asymmetry that matters: publishing a hash grants no ability to produce a
matching body; publishing an identifier does.

This is materially worse than the descriptive-facts problem B1 fixed, because a
report author following the documented contract could legitimately omit the hash
and believe the binding was sound.

**Repair:** `body_sha256` is now **mandatory**. `event_ids` is retained as an
additional cross-check when supplied but can never substitute. Verified
order-independent (report and candidate both sort), and subset/superset id sets
are refused.

## Attacks that B1 withstood

| Attack | Result |
|---|---|
| All-zero / short / uppercase / malformed commit id | refused |
| Blob id, tree id presented as a commit | refused on object type |
| Nonexistent commit; nonexistent path | refused, fails closed, no fetch |
| Report loaded from working tree instead of commit | not possible — `git show <commit>:<path>` |
| `PROBE-BINDING` block inside a fenced code sample | refused as a **conflicting** declaration |
| Duplicate / contradictory field | refused rather than resolved |
| Non-200, foreign provider, foreign endpoint | refused |
| Two byte-identical candidates | refused as **AMBIGUOUS** |
| Zero candidates | refused |
| Registration naming R1 while the report proves R2 | refused |
| Response stored under a non-events endpoint | refused |
| Caller-owned `registered_at` | absent from the API |

The fenced-code result deserves a note: the parser does not distinguish a
normative block from an example, but it fails **closed** — two declarations of
one field is a contradiction, so an example that disagrees refuses the whole
report rather than being silently adopted. That is the safe direction, though a
future report format containing a legitimate example would be unusable until a
`stage-a-probe-v2` defines fencing semantics.

## §8 — projection/body composition

`reused_probe_response` is a member of `PROJECTING_OUTCOMES`, so the v22 gate's
body-verification loop covers reused probes: a response that binds by hash but
carries a malformed or non-historical body is still refused at certification. The
layering is correct — **binding proves WHICH response was committed; it does not
make the body semantically valid.**

## §9 — the real `d3984d0` probe: verdict B **confirmed**

Resolved the actual commit and loaded the report from it (10,297 bytes), not from
the working tree:

| | |
|---|---|
| `PROBE-BINDING` block | **absent** |
| `body_sha256` | **absent** |
| any 64-hex string | **none** |
| any 32-hex string | **none** |

Stronger than B1 stated: the report contains **no 32-hex value at all**, so not
even the (now-removed) event-id path was ever available. The report is
non-bindable under any correct policy.

**Verdict B stands: the real probe is NOT safely reusable.** The March 1
`17:00:00Z` bucket must be acquired as an ordinary request — **161 credits, not
160**. Independently reproduced B1's supporting demonstration: a synthetic body
with zero real provider data satisfies every descriptive fact the report commits,
at exactly 2,255 bytes.

## §1 / §10 — the CI repair

`ac36cc9` touched only `.github/workflows/ci.yml`, the implementation document and
the B1 test file; `git diff 65ba376 ac36cc9 -- sports_quant/retrospective/` is
**empty**, so no production semantics changed. It did not weaken the policy, the
unique-candidate rule, the git verification, or the real-probe verdict.

The original failure is confirmed as a depth-1 checkout making `d3984d0`
unavailable. The repair is correct in both required directions: mechanism tests
build their own throwaway repository and run anywhere; only the four evidence
tests about `d3984d0` are conditional. The `checks` job (which runs pytest)
declares `fetch-depth: 0`, so those four **execute** rather than skip. `wheel-smoke`
has no such setting and does not need it — it runs an install/CLI smoke, not these
tests.

**Residual risk, accepted:** the skip predicate is a genuine escape hatch. If the
object ever became unavailable in CI, the load-bearing verdict-B tests would skip
silently rather than fail. `fetch-depth: 0` is what prevents that today, and it is
a one-line change away from regressing. A future task could assert
"history must be present" in CI specifically; that is not worth a schema or policy
change now, but it is a real dependency and is recorded here.

## §11 — shallow production checkout

Production verification **fails closed** when the commit is unavailable: it never
fetches, never falls back to the working tree, and never accepts a registration
without proof. A shallow production checkout therefore refuses probe reuse until
history is available. That is the correct direction and is an operational
dependency worth documenting rather than a defect.

**Observation (not a defect):** the verifier shells out to `git`, and the
project's own zero-network harness blocks `subprocess` wholesale to prevent
`curl`-style escapes. Probe binding therefore cannot run under that harness as
armed. Git here is strictly local and performs no fetch; this review re-proved all
four network surfaces blocked while permitting local git.

## §13 / §14 — schema and policy freezing

**v22 remains sufficient.** Storing a committed-report content digest would
duplicate a value already derivable from an immutable git object, adding a
forgeable field rather than a proof. No migration.

`stage-a-probe-v1` semantics are pinned by tests. Note that this review **changed
v1's meaning** (hash mandatory) rather than minting v2 — defensible only because
no report has ever been registered under v1 and none can be, since the sole
historical report is non-bindable either way. Once any real probe is registered,
this door closes and a semantic change requires v2.

## §15 — malicious chain

forged old response → forged registration → `REUSED_PROBE_RESPONSE` →
certification → enrichment.

**First guard:** commit resolution (a fake SHA fails immediately). Then, in order:
report contract parsing → **mandatory hash** → unique-candidate → registration
cannot nominate a different response → projection/body verification →
observation-content hashes → the v22 enrichment gate. Direct-SQL registration does
not help: certification re-derives everything from the committed artefact.

## Non-regression

`REGISTERED_LINKING_PROVIDERS` empty · `ATTESTED_GENERATIONS` unchanged ·
`OFFICIAL_PROVIDER_BY_LEAGUE` / `QUALIFIED_PROVIDERS` / `AsOfReader` /
`_feature_cutoff` untouched · no canonical game, audit or crosswalk created ·
Stage-A tables remain non-feature sources.

## Retained blockers — all still open

**B2** (plan manifest commit resolution), **§AF** (target→bucket recomputation),
**B3** (lane-backed crosswalk). The git helper is reusable for B2 but is
deliberately not wired into plan certification.

## Next authorization boundary

> **B2 — Stage-A plan manifest commit/content resolution.**

B1 is closed. The next credit-spending step still requires B2 and §AF; B3 remains
deferred until a crosswalk is actually needed. **F1-R remains blocked.**
