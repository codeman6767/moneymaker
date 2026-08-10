# F1 canonical-matching repairs — independent review

Independent, entirely offline review of the three matcher repairs committed at
`fae7650`. Zero provider requests. Production matching was **not** run over the
real MLB June or NBA March corpora, and no original execution, recovery, merged,
checkpoint or evidence database was modified.

**Verdict: ACCEPTED WITH REPAIRS.** The three original defects are genuinely
fixed. This review found **three further defects** — one in each repaired area —
and fixed them reproducer-first.

---

## 1. Boundary and protected evidence

23 process-level guards installed before importing any matching, PIT or provider
module; **14/14 adversarial probes blocked**;
`cli.load_settings is config.load_settings` is `True`; **0 guard trips**. No API
key read, no provider client constructed, no audit, no real sleep. All fixtures
are synthetic databases; the only contact with real evidence was read-only
inspection and SQLite backup copies.

Byte-identical before and after:

```
39064fa219f4eb66f37e26f567a8f42d7df278236aa70e42264a600b07a5d135  NBA March corpus
c17a375daa89e3f0f8ace2e6e3dffd965f8428b178127cb9b7c44bde6471300b  NBA March checkpoint
e2fea1c06c43400323b0266aeb8ba34db28e9b6ead13504413eb93ed4de6e1db  lineup recovery db
8c4e83ee6cffb5c713de8bd0382d85b6486b2b68dd75821c7ad5ab38a4c689df  recovery checkpoint
223c1185a0c7c1afb35e04ae8e31e50de4f7e62e69073808eb08cf75cafad02a  merged copy
802a7d76e42d08dc60894329c490ca92d4f98e95197f2eaac967d3065af8b6f2  MLB June corpus
70bbc7c907cd6038eb57edd744111d7a187567fd948ff3d473a0192aaf91569e  MLB June checkpoint
```

## 2. Verdict on each original defect

### Defect 1 — official same-name determinism: **CORRECT**

Reproduced independently for **both** leagues with 2, 3 and 4 same-name stable
ids, under permuted reference, identity-observation and processing order. In
every case each id linked to its **own** canonical player, N ids gave N distinct
canonical players, and no two ids shared an identity. 30 further randomized
permutations agree. Replay adds nothing; dry-run writes nothing.

The "one official stable id is one person" premise was verified as an explicit
repository contract, not merely a comment:
`OFFICIAL_PROVIDER_BY_LEAGUE == {"lg_mlb": mlb_statsapi, "lg_nba": balldontlie}`
is asserted in production and is the same gate `_bootstrap_official_player` uses
to refuse every other provider. A nonofficial provider with the same collision
still creates nothing.

The `all`, not `any`, candidate-claimed rule was checked directly: with one
**unclaimed** canonical candidate present, the id resolves onto that existing
player rather than bootstrapping a duplicate — real ambiguity stays conservative.

### Defect 2 — accepted team replay growth: **CORRECT for the valid path, DEFECTIVE for the broken path**

A valid `exact_provider_id` replay writes nothing across six repeated runs: no new
team, link, candidate or accepted decision, no rewritten history, and
`decisions_replayed` increments. Confirmed for MLB and NBA.

**But the BROKEN path grew without bound** — see Review defect A.

### Defect 3 — decision-backed downstream resolution: **ARCHITECTURE CORRECT, VALIDATION INCOMPLETE**

The architecture is right and was re-verified independently: all 16 observation
tables remain append-only (guards present), no canonical convenience column was
backfilled, and no schema change was made (still **v17**). Resolution goes
observation → provider reference → its own backing accepted decision → canonical
identity.

**But the resolver did not bind the decision to its source, and the PIT gate
compared strings** — see Review defects B and C.

## 3. Defects found by this review, and their repairs

### A. A broken team link grew audit history on every rerun

**Reproduction.** One matchable game, matched, then `match_decision_id` cleared on
one reference. Rerunning the identical broken state four times:

```
after corruption      team_accepted=2  candidates=3  DQ-017=0
after broken replay 1 team_accepted=3  candidates=4  DQ-017=1
after broken replay 2 team_accepted=4  candidates=5  DQ-017=1
after broken replay 3 team_accepted=5  candidates=6  DQ-017=1
after broken replay 4 team_accepted=6  candidates=7  DQ-017=1
```

**Root cause.** The repair reported `DQ-MATCH-017` and then fell through to
`_record_decision`, which never deduplicates an `accepted` outcome. The DQ row
deduplicated; the accepted decision and its candidate did not. Every rerun over
one unchanged corruption appended another accepted team match for a link already
known to be broken — the same unbounded-growth class the original repair set out
to remove, relocated to the failure path.

**Repair.** On `BROKEN`, the blocking DQ is recorded and `_resolve_team` returns
without recording a decision. The contract is now: the blocking DQ is durable, the
corrupt reference is neither silently repaired nor treated as a valid replay, and
repeated identical corruption converges. A healthy sibling reference in the same
run still replays normally, so the failure is scoped to the corrupt reference.

### B. `resolve_canonical` accepted a decision belonging to another reference

**Reproduction.** Reference `2002` pointed at `2001`'s accepted decision, with the
canonical target agreeing. `resolve_canonical` returned a fully justified
`CanonicalLink` even though the decision's `source_ref` was `'2001'` — it had
never adjudicated `2002`.

**Root cause.** The resolver validated outcome, entity type and canonical target,
but not that the decision was *recorded for this provider reference*. A matching
canonical id only means both name the same entity, which can happen by
coincidence.

**Repair.** `resolve_canonical` now also requires
`decision.source_provider == provider` and `decision.source_ref == provider_entity_id`.
`_existing_team_link_state` received the same binding, closing the analogous hole
on the replay path (a decision belonging to the *other* team in the same game is
now reported rather than accepted as a valid replay).

### C. The point-in-time gate compared timestamps lexically

**Reproduction.** With `decided_at = '2026-08-08T23:55:49.342103Z'`:

| Cutoff | Was | Should be |
|---|---|---|
| `...49.342103+00:00` (same instant) | unresolved | resolved |
| `...49Z` (whole second, earlier) | **resolved** | unresolved |
| `2099-01-01T00:00:00+01:00` | resolved | resolved |
| naive `...49.342103` | unresolved | reject |
| **`'not-a-timestamp'`** | **RESOLVED** | reject |

**Root cause.** `decided_at > as_of` on raw strings. That is only correct for the
corpus canonical form. `'Z' > '.'`, so a whole-second cutoff sorts *after* a
sub-second decision on the same second; an equivalent instant written `+00:00`
sorts before the same `Z` value; and any malformed string beginning with a letter
sorts after every real timestamp, so **a malformed cutoff silently granted
access** — a fail-open in a point-in-time gate.

**Repair.** Comparison is now chronological on parsed instants: the stored value
through `from_iso` (storage format) and the caller's cutoff through
`Cutoff.parse`, which is the repository's single cutoff contract and the same one
`AsOfReader` uses. Naive and unparseable cutoffs now raise instead of being
guessed at. Boundary behaviour is unchanged and documented: cutoff `<` decision →
unresolved, `==` → resolved (inclusive), `>` → resolved. No decision was
backdated.

## 4. Provenance, PIT gating and integration

**Provenance.** A canonical link is now justified only by its own
`match_decision_id` with accepted outcome, correct `entity_type`, correct
`matched_entity_id` **and** correct `source_provider`/`source_ref`. Verified for
team, player and game kinds; a decision from another provider, another reference,
another entity type or another canonical target all resolve to `None`.

**Feature-path protection.** A static inventory found `resolve_canonical` /
`resolve_many` imported **only** by their own tests — no PIT, feature, label,
modeling or backtest module uses them. An architecture test now enforces that,
and a second test confirms all three `provider_*_references` tables remain
`ForbiddenJoinError` under `require_asof`, so the mutable current-state link
cannot be read as history.

**Who should use what:**

| Consumer | Path |
|---|---|
| Feature builders, label builders, historical dataset builder, any PIT read | **`AsOfReader`** — it also enforces the manual-review gate, which `resolution.py` does not |
| Matching-side joins and reporting that need current canonical identity | `resolution.py` |
| Anything reading `provider_*_references` directly for history | **forbidden** |

`resolution.py` was deliberately **not** wired into unrelated code to inflate
usage; it remains a matching/reporting-side utility.

**Cross-check.** Player and game exact-provider replay paths were audited for an
analogous growth defect. The player path already routes through
`classify_link_attempt` → `LinkAttempt.REPLAY` and returns before recording; the
game path creates at most one canonical game and is idempotent. No further
broadening was made, since no reproducer showed one.

## 5. Robustness

* **Determinism** — 30 randomized permutations for NBA plus the 25 already
  covering MLB, over reference, identity and processing order; parameterized 2/3/4
  same-name ids in both leagues. No result depends on implicit SQLite row order.
* **Atomicity/concurrency** — the review repairs add no write path: repair A
  *removes* a write, B and C are read-only checks. Provider-reference identity
  columns remain immutable once set, so no traversal or concurrency order can
  collapse two official ids into one identity.
* **Dry-run** — `match-games` and `match-players` dry-runs persist zero canonical
  entities, aliases, decisions, candidates, links and DQ rows, and are
  deterministic across repeats.
* **Bounded real-shape simulation** (SQLite backup copies only, one day per
  league — *not* an F1 coverage measurement):

  | | considered | canonical games | decisions | teams | DQ-MATCH | replay stable |
  |---|---|---|---|---|---|---|
  | NBA March | 11 | 11 | 33 | 60 (unchanged) | none | **yes** (22 replayed) |
  | MLB June | 9 | 9 | 36 | 60 (unchanged) | `DQ-MATCH-011` ×9 | **yes** (27 replayed) |

  The repaired matcher consumes the real month schema, the reference and identity
  shapes fit the repaired contracts, and the replay repair holds on real evidence.
  `DQ-MATCH-011` is the pre-existing "venue did not resolve to a canonical venue"
  gap, unrelated to these repairs. **No coverage figure is claimed from this.**

## 6. Validation

All offline; no real sleeps.

```
git diff --check                       clean
ruff check .                           All checks passed
mypy . --no-incremental                Success: no issues found in 310 source files
pytest -q                              2386 passed, 2 skipped, 0 failed (501 s)
matching + PIT suites                  500 passed
schema init x2                         v17, 17 migration rows, 47 tables, integrity ok
v16 -> v17 migration x2                idempotent on re-apply, both runs
non-editable wheel smoke               24/24 checks passed
protected artefacts                    7/7 byte-identical
staged-file / secret audit             6 files, all source/docs/tests; no db, ckpt,
                                       raw response, log, wheel, env or graphify output
```

`test_canonical_matching_repairs_review.py` adds **57 tests**: the three review
reproducers (written before their fixes), 2/3/4 same-name official ids
parameterized across **both** leagues, 30 randomized determinism permutations, the
unclaimed-candidate conservatism check, valid-replay stability over six runs,
resolver coverage of all three kinds, source-binding refusals for wrong reference
and wrong provider, the PIT boundary and malformed/naive cutoff refusals, and two
architecture tests pinning that no PIT/feature module imports the light resolver
and that `provider_*_references` stay `ForbiddenJoinError`.

The wheel smoke ran the **installed** package (fresh venv `site-packages`, CWD
outside the repository) covering both matching CLIs and their `--dry-run` flags, a
three-way same-name official collision, replay idempotency, downstream canonical
resolution, the three PIT cutoffs, dry-run persistence, the zero-network sentinel
(14/14) and schema v17. The review-specific additions (NBA collision, source
binding, timestamp edge cases, broken-link convergence) are covered by the unit
suite above, which CI also runs.

---

## Verdict

**ACCEPTED WITH REPAIRS.**

- The three original repairs are **independently reviewed**; three further defects
  were found and repaired reproducer-first.
- **Production F1 matching still has not run.**
- **Identity coverage still has not been measured.**
- **NBA PIT labels remain 0/239** in the unchanged protected corpus.
- **Historical March NBA lineups remain unavailable at March pregame cutoffs**;
  nothing here weakened a point-in-time rule to gain coverage.
- **Next step is NO LONGER a production matching run.** `F1_HISTORICAL_PIT_FEASIBILITY_REVIEW.md` (2026-08-10) proves the historical
  dataset yields 0 rows regardless of matching quality, because 100% of games in
  both corpora fail the pregame-schedule gate. The matcher repairs remain correct
  and useful; they simply are not the binding constraint. Resolve the PIT
  architecture before authorizing any acceptance run.
- **The combined F1 review has not begun. F1 remains incomplete. F2 remains
  unauthorized.**
