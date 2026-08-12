# Independent review — retrospective identity-audit engine (`b1a207d`)

Offline correctness, scientific-validity, identity, provenance, determinism and
integration review of the production corpus-scoped G5 identity-audit engine.

**Verdict: ACCEPTED WITH REPAIRS — AND A RETAINED BLOCKER.**

Mapping to the required categories, separated as §26 asks:

| Component | Status |
|---|---|
| **Audit engine** | **ACCEPTED WITH REPAIRS** — ten defects proven and repaired; audit policy bumped `g5-identity-audit-v1` → **`v2`** |
| **Player crosswalks** | **ACCEPTED**, under the explicitly-stated Option-A identity basis (§3) |
| **Team crosswalks** | **BLOCKED** — Option A ruled out on evidence; B/C/D need a separate authorized decision. *(Architecture since decided 2026-08-12 — see the note at the end of this document. Still blocked in code.)* |
| **Game crosswalks** | **BLOCKED**, transitively on teams *(same note)* |
| **Reader readiness** | **NOT READY** — the team/game canonical question must be resolved first |

Ten defects were proven with failing reproducers before repair. Two of them —
a game id reused across a doubleheader, and a namespace "verified" by any
arbitrary string — were **fail-open holes in the G5 contract itself**. One more,
the CLI accepting the same file as source and output, wrote provenance into the
corpus being audited.

No `RetrospectiveResearchReader`, no market anchoring, no F1-R, no F2, no
production matching, no model training, **no provider API request**, no mutation
of protected evidence, no schema change.

---

## 1. Boundary

`HEAD == origin/main == b1a207d` at start, clean tree, CI #94 green, schema v19,
19 migrations. 23 process guards installed before importing any
retrospective/provider-facing module; **8/8 adversarial probes blocked** (DNS,
raw socket, urllib, httpx, requests, both provider client constructors,
`config.load_settings`). **0 network trips.**

Protected evidence: **42/42 artefacts byte-identical** before and after,
including after re-running both real one-month audits. Source `.db` and `-wal`
mtimes unmoved. The `-shm` mtimes still read 2026-08-11 16:40 — from the previous
phase's pre-hardening `mode=ro` runs, disclosed in its report; they did **not**
move during this review, which is the behaviour the `immutable=1` open was
introduced to guarantee.

## 2. Defects proven and repaired

Each was reproduced first, and each has a permanent reproducer in
`test_retrospective_identity_audit_review.py`.

### R1 — same-matchup reuse on another date read as clean *(material)*

The identity signature was `(season, home, away)`. A provider game id carrying
**Yankees-vs-Red-Sox on June 1 AND on June 15** shares that triple exactly, so
v1 reported `GAME_ID_LAWFUL_MUTATION` and **ACCEPTED**.

Adding the date to the identity key is wrong — a postponement legitimately moves
it. The repair distinguishes the two using the provider's own continuity
evidence: a date change with **no** `reschedule_info` and **no** observed
moved-status is now a WARNING classified `insufficient_evidence`
(`GAME_ID_DATE_MOVED_WITHOUT_CONTINUITY`). It is deliberately not called a
collision, because the corpus genuinely cannot tell; it is no longer called
lawful either.

### R2 — doubleheader reuse read as clean *(material)*

One provider id carrying **both halves of a split doubleheader** — same date,
game numbers 1 and 2 — was ACCEPTED. Two events on one day under one id is
*provable* from the game numbers, and is now a BLOCKING
`GAME_ID_TWO_EVENTS_SAME_DAY` collision. A legitimate doubleheader (two distinct
ids) and a makeup game on a later date both remain clean.

### R3 — `reschedule_info` never reached the rules

It was bound into the source digest but absent from `GameObservation`, so the
strongest available continuity signal was invisible to the audit. Now exposed and
used by R1's rule.

### R4 — the namespace was "verified" by nothing *(material)*

`verified` was `generation != "unverified"`. A caller typing `banana`, `V1` or
`v99` got a **verified** namespace and an **ACCEPTED** audit authorizing
crosswalks — a fail-OPEN in precisely the contract G5 closed fail-CLOSED. Repaired
with `ATTESTED_GENERATIONS`: only generations this build actually ingests count;
anything else is unverified whatever string is supplied.

### R5 — an empty namespace was "clean"

An audit over zero observations returned ACCEPTED with `distinct_ids = 0`. An
audit of nothing found no contradiction, which is true and useless. Now refused.

### R6 — game scope relied on an unstated invariant

`game_schedule_snapshots` has **no `league_id` column**, so the game audit and the
game half of the digest were scoped by provider alone. Sound only while a provider
is league-exclusive — an invariant, not a fact of the schema. Now declared in
`PROVIDER_LEAGUES` and enforced; an undeclared provider is refused.

### R7 — detection power was never recorded *(material to the claim)*

The engine reported "zero collisions" with no statement of what it was *able* to
detect. Every audit now carries a `NAMESPACE_DETECTION_POWER` finding recording
ids audited, ids observed more than once, and ids carrying discriminating
evidence. See §4 for why this changes the reading of the real results completely.

### R8 — the dry run was not faithful

Crosswalk prediction ran against `players(player_id TEXT PRIMARY KEY, league_id
TEXT)` — no NOT NULLs, no CHECKs — so a dry run could promise a crosswalk the real
output schema would refuse. It now runs against a genuinely migrated v19 database.

### R9 — a test that asserted nothing

`assert cli_main([*argv]) == 0 or True` passes for every exit status. Repaired,
and paired with a case that must exit non-zero so the assertion has teeth.

### R10 — the CLI let the source be the output *(material)*

`--source-db X --output-db X --apply` **wrote provenance into the corpus being
audited**. The schema check happened to stop it for a v17 corpus, but that is
incidental: a v19 source was written to happily. Now refused by resolved path and
by `(st_dev, st_ino)`, so a hardlink or symlink alias cannot slip through either.

## 3. §3 / §4 / §8 — evidence strength: policy chosen and stated

**Policy A is adopted, with Option C's mechanics for honesty.**

*Option A* — the exact official provider id **is** the static-identity evidence;
the audit checks corpus-scoped *compatibility* and does **not** independently
verify non-reuse. This is not a new concession: it is what
`G5_PROVIDER_ID_STABILITY_REVIEW.md` already closed on, including its explicit
acknowledgement that two distinct persons sharing one id with every observed
attribute agreeing are indistinguishable from the evidence. Overturning it would
reopen an accepted verdict this review is not authorized to reopen.

*Option C mechanics* — because acceptance is that weak a claim, the audit now
records its own detection power (R7), so no reader can mistake ACCEPTED for
"verified non-reuse".

**What is therefore undetectable, stated plainly:**

| Class | Detectable? | Why |
|---|---|---|
| person id in two leagues | yes | league is identity-defining |
| person id with two supplied birth dates | yes | decisive secondary evidence |
| **person id reused within one league, no DOB** | **NO** | only names differ, and a name may never merge or split ids |
| team id in two leagues | yes | league is identity-defining |
| **team id reused within one league** | **NO** | only labels differ, and G5 forbids labels as identity |
| game id for a different matchup | yes | season + both team ids |
| game id across a doubleheader | yes (new) | distinct game numbers on one date |
| **game id reused, same matchup, no continuity evidence** | **NO** — now flagged | a postponement is indistinguishable from reuse |

`John Smith → Michael Jones` under one id with no DOB is still ACCEPTED with a
warning, and that is the honest consequence of Option A. It is not silent: the
audit carries `PLAYER_ID_NAME_CHANGED`, `PLAYER_ID_NO_SECONDARY_EVIDENCE` and the
detection-power record.

**§4 — name-variance classes.** Spelling correction, preferred-name change, legal
change, suffix add/remove, middle-name addition, one-character typo and a wholly
unrelated name all currently share one WARNING class. They **cannot** be
separated on this evidence without a similarity threshold, and a threshold on
names is exactly the fuzzy matching G5 forbids as identity evidence. Left as one
class **deliberately**, and the limitation is now recorded rather than implied.

## 4. §19 — the real one-month results, correctly qualified

Re-run read-only under policy v2. Counts reproduce exactly; the reading does not.

| | ids | observations | comparable ids | discriminating | collisions | verdict |
|---|---|---|---|---|---|---|
| MLB game | 400 | 400 | **0** | 0 | 0 | accepted |
| MLB team | 30 | 1,630 | 30 | 30 (league only) | 0 | accepted |
| MLB player | 1,053 | 47,830 | 1,044 | **0** | 0 | accepted |
| NBA game | 239 | 239 | **0** | 0 | 0 | accepted |
| NBA team | 30 | 6,474 | 30 | 30 (league only) | 0 | accepted |
| NBA player | 550 | 91,187 | 549 | **0** | 0 | accepted |

**The game result is vacuous.** Not one game id in either corpus was observed
more than once, so no contradiction *could* have surfaced: the audit compared
nothing. "Zero collisions" for games means "nothing was comparable", and the
previous report's unqualified phrasing was misleading.

**The player result is near-vacuous for reuse.** 1,044/1,053 and 549/550 ids were
comparable, but `birth_date` is populated for **0** of them, so the only
discriminating evidence was league — and every id was in the expected league by
construction.

**The team result is league-only.** 30/30 comparable, but same-league reuse is
undetectable by design.

**What the one-month audits actually establish:** no id was observed in two
leagues, no person carried conflicting birth dates, no game id carried two
matchups, and no doubleheader reuse occurred. That is all. It remains
**non-transferable to 3–5 seasons**, and it is now additionally **bounded by
detection power** — a distinct and more important caveat than the window length.

## 5. §9 — team/game crosswalk architecture

**Option A is ruled out on evidence.** *(Naming caution: this §9 "Option A" is
the task's *deterministic official-provider seed mapping*. It is NOT the later
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE.md` **TEAM-A**, which is a
source-controlled curated attestation — a different mechanism that this
paragraph does not address.)* `TeamSeed` in
`sports_quant/db/seeds/mlb_teams.py` carries `abbreviation`, `city`, `nickname`
and alias strings, and **no official provider id at all**; every seeded alias has
`provider = ''`. The canonical team dimension was built from labels, so binding it
to official ids necessarily means name matching. The blocker is real.

Options B (provider-key canonical team identity, needs schema/dimension change),
C (a reconstruction-specific identity dimension, changes downstream semantics)
and D (a curated one-time seed crosswalk — which arguably converts static identity
into a *matching decision* and would need its own provenance and review) are all
live, and none is unambiguous. Per §9 no architecture change was implemented.
**Game crosswalks stay blocked behind teams.**

## 6. §10 / §11 — acceptance semantics

The schema is **namespace-atomic**: `collision_count = 0` is required for
ACCEPTED and f019 forbids contradictory exclusion findings under one, so any real
collision rejects the whole entity-type namespace. The engine follows this
exactly, and no workaround was introduced.

**Adjudication: namespace-atomic is the scientifically preferable contract here**,
because partial acceptance would require the audit to assert that the *surviving*
ids are unaffected by a demonstrated reuse — and a corpus that has proven it
recycles identifiers gives no basis for that assertion about the ids it has not
yet contradicted. Blast-radius data is still recorded on the rejected audit's
findings, so a future partial-acceptance decision keeps its evidence.

**`ACCEPTED` means "no contradiction detected at this policy's detection power".
It does not mean "verified stable identity".** That is now stated in the engine
docstring, the implementation report and the CLI output, and is measurable per
audit via the detection-power finding.

## 7. Mechanics re-proved

* **Source digest** — binds exactly the audited subset, order-independent, shared
  across all three entity types for one corpus, and changes on any audited value.
  R6 closed the league-scope gap.
* **Counts** — `collision_count` is **distinct collided ids**, not finding rows;
  documented consistently. Reconciliation refuses any summary disagreeing with its
  findings, including duplicate semantic findings.
* **Atomicity** — failure injected before the summary, mid-findings, after the
  last finding, and **during crosswalk generation**: every case rolled back to
  zero rows across all five tables. No partial accepted state.
* **Idempotency** — replay reuses the audit record and adds no duplicate finding,
  crosswalk or canonical person; a changed corpus or policy version yields a new
  audit.
* **Determinism** — 100 randomized permutations over a fixture containing a
  doubleheader reuse, an explained move, an unexplained move and singletons: one
  digest, one summary.
* **Canonical player id** — 96 bits of SHA-256 over the full namespace key;
  namespace-, league- and generation-separated; idempotent; a later name variation
  cannot create a second canonical person or remap an existing crosswalk (the id
  never depends on the name).
* **§23 descriptive metadata** — the bootstrapped `full_name` is chosen by earliest
  `observed_at`, which is *acquisition* time, not availability. It is descriptive
  only. Structurally, `players` is `immutable` in the PIT registry and the Lane-R
  tables are `unsupported` joins, so no strict-PIT dataset can reach the crosswalk
  at all; the boundary is documented and tested.
* **WAL** — `immutable=1`, with a non-empty WAL refused rather than read stale.

## 8. §24 — strict-PIT isolation

`_feature_cutoff` still hashes to its pinned v17 value; `AsOfReader` has no audit
or bypass surface; all five Lane-R tables remain `unsupported` joins; no feature,
model or PIT module imports the engine; `pit/` and `matching/` are untouched since
`b1a207d`. The identity audit is not a PIT bypass.

## 9. §25 — documentation contradictions repaired

`HISTORICAL_RESEARCH_PIT_ARCHITECTURE.md`'s authoritative status banner still read
"No reader, no identity-audit engine" directly above a block stating the engine
was implemented. Repaired: one dated authoritative status line, with older blocks
explicitly labelled historical snapshots. `RECONSTRUCTED_CORPUS_PROVENANCE.md`
rule 6 and both scope sentences in the v18 review are now dated to the commits
they describe. No historical statement was erased.

## 10. Validation

`git diff --check` clean · ruff clean · mypy clean over **325** files ·
**2688 passed, 3 skipped** · fresh v19 twice · v17→v19 · v18→v19 · wheel smoke
green · staged-artifact and secret audit clean · **0 network trips** ·
**42/42 protected artefacts byte-identical**.

## 11. Scope confirmation

No `RetrospectiveResearchReader`, no historical Odds API client, no market
anchoring, no `commence_time` iteration, no F1-R, no F2, no feature engineering,
no model training, no Lane-L collection, and **no schema change** (still v19, 19
migrations).

**F1-R, F2, production matching and model training remain unauthorized.**
**G1, G2, G3, G4 and G6 remain open exactly as previously scoped.** G5 remains
closed as the corpus-scoped fail-closed contract — R2 and R4 were fail-open
defects in its *implementation*, now repaired, not changes to its verdict.

**The reader must not begin** until the team/game canonical-crosswalk architecture
(§5) is separately decided and reviewed.

**Team/game crosswalk architecture DECIDED 2026-08-12 — awaiting independent
review.** `RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE.md`. Chosen **TEAM-A**:
a source-controlled static attestation binding official provider franchise ids to
the **existing** canonical seed, with **no schema change** (stays v19). The
distinction that unblocks it is *when* labels are read — a one-time, reviewed,
source-controlled attestation answers "which franchise does this provider
franchise id denote", whereas forbidden runtime matching asks "which team was this
row probably about". Alternatives rejected on measurement: TEAM-B would move 13
FK-bearing tables and every deterministic `tm_*` id; TEAM-C would create a second
franchise dimension and split Lane-R from Lane-L; TEAM-D2 fails because strict PIT
gates `entity_match_decisions` on wall-clock `decided_at`, recreating the original
blocker. Diagnostic: **60/60 franchises uniquely attested and corroborated by a
second attribute, 33/33 historical aliases correct, 639/639 games ready** — and
`games` already carries a UNIQUE `(official_provider, official_game_key)` index,
so game bootstrap needs no schema work either.

**Nothing was implemented.** Team and game crosswalks remain BLOCKED in code, the
reader remains unimplemented, and **F1-R, F2, production matching and model
training remain unauthorized.** The architecture decision itself still requires
independent review. G1/G2/G3/G4/G6 unchanged.
