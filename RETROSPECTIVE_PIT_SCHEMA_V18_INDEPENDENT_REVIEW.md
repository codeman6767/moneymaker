# Independent review — retrospective PIT provenance foundation (`31f78e2`, `4d4ae13`)

Correctness, integrity, migration, provenance and isolation review of the schema-v18
Lane-R provenance foundation.

**Verdict: ACCEPTED WITH REPAIRS.**

Eight defects were proven — two of them material to the G5 provenance contract —
and all eight are repaired. Five required database-level enforcement, delivered as
**migration `f019`**. **The reviewed foundation is now schema v19.** `f018` is
preserved byte-for-byte as historical migration evidence and was not edited.

Design/repair only — *describing what THIS review changed, as of `2824c3a`*. It
added no `RetrospectiveResearchReader`, no identity-audit engine, no market
anchoring, no F1-R, no F2, no production matching, no model training, **no
provider API request**, and no mutation of protected F1 evidence. (The
identity-audit engine was implemented later; see §10.)

---

## 1. Boundary

`HEAD == origin/main == 4d4ae13` at start, clean tree, CI #92 green, schema v18,
18 migrations. `4d4ae13` touched **only** `.github/workflows/ci.yml` (5 insertions,
4 deletions) — confirmed by diffstat.

**Zero network.** 23 process guards installed before importing any
retrospective/provider-facing module (DNS, non-loopback sockets, sync/async httpx
transports, `requests`, `urllib`, provider client constructors, settings/API-key
loading). **0 trips** across every probe, test run and wheel smoke.

**Protected evidence.** 42 artefacts fingerprinted before and after: **42/42
byte-identical**. The two preserved corpora keep their original mtimes. All **7**
committed pilot manifests are byte-identical to `4d4ae13`.

## 2. Independent reconstruction of f018

Read from a freshly built database, not from the implementation report:

| Table | Cols | CHECKs | UNIQUE | FKs | Indexes | Triggers |
|---|---|---|---|---|---|---|
| `reconstruction_corpus_versions` | 16 | 15 | 1 | 2 | 2 | 3 |
| `identity_audit_records` | 15 | 16 | 1 | 1 | 2 | 3 |
| `identity_audit_findings` | 14 | 17 | 1 | 2 | 2 | 3 |
| `static_crosswalk_provenance` | 14 | 11 | 2 | 3 | 3 | 6 |
| `reconstructed_input_provenance` | 22 | 27 | 2 | 3 | 3 | 4 |

All five carry `_no_update` and `_no_delete` abort triggers; append-only confirmed
against populated tables, not empty ones.

## 3. Defects proven, and repairs

Each was reproduced through the **repository API** and through **direct SQL** (with
foreign keys both ON and OFF) before any repair was written.

### D1 — cross-corpus audit reuse *(material)*

A static crosswalk could cite an ACCEPTED identity audit taken over a **different
source corpus**. Constructed corpus A over `month-A` with a clean audit over
`month-A`, then corpus B over `five-season-B`, then bound a crosswalk in B citing
A's audit:

| Path | v18 | v19 |
|---|---|---|
| repository API | **ACCEPTED** | refused |
| direct SQL, `foreign_keys=ON` | **ACCEPTED** | refused |
| direct SQL, `foreign_keys=OFF` | **ACCEPTED** | refused |

This is exactly the transfer `G5_PROVIDER_ID_STABILITY_REVIEW.md` §16 forbids: a
clean one-month audit vouching for a five-season reconstruction. f018 checked
league, provider, generation, entity type and audit digest — but never that the
audit's `source_corpus_digest` equalled the corpus version's, although both are
recorded and the check is one join away.

**Repair:** `trg_xwk_audit_corpus_binding` (f019) plus an explicit repository check
that also refuses an unknown corpus version.

### D2 — an accepted audit could acquire contradictory findings *(material)*

An ACCEPTED audit (`collision_count = 0`) could later be handed a BLOCKING
`identity_collision` finding. The audit then simultaneously asserted "accepted,
zero collisions" and "contains a blocking collision" — and the crosswalk already
built from it **survived unchallenged**. A `namespace_unverified` finding was
likewise accepted under a verified-namespace audit.

**Contract chosen** (the narrowest coherent one, §4's option "prohibit
contradictory findings after an accepted audit"): an ACCEPTED audit is a *completed*
statement. It may carry flags — a `warning`/`name_variance` finding with
`exclusion_scope = none`, which is what `flagged_count` exists for — and benign
`legitimate_mutation` records. It may never receive a finding that is `blocking`,
classified `identity_collision`/`namespace_unverified`, or carries any exclusion
reach. Newly discovered trouble is recorded as a **new audit** over the corpus,
which yields a new digest and therefore a new corpus version downstream. Nothing is
mutated; no sealing flag was introduced, because a mutable finalization bit would
weaken exactly the append-only guarantee this lane exists to provide.

`flagged_count` must also already account for the audit's own warnings, so a flag
cannot be smuggled in under a summary claiming zero.

**Repair:** `trg_idf_accepted_audit_no_contradiction`, `trg_idf_flag_must_be_counted`
(f019) + repository `ProvenanceConflictError`.

**Accepted limitation, stated rather than engineered away:** a `rejected_collision`
audit may declare `collision_count = 5` while carrying zero finding rows. This is a
completeness gap, not a soundness gap — no crosswalk can cite a rejected audit, so
nothing can be built on it. Enforcing "every declared collision has a finding row"
would require either a deferred constraint SQLite does not have, or a mutable
sealing state. Recorded here as a known limitation for the audit-engine phase.

### D3 — eligible inputs needed no source evidence

An **ELIGIBLE** reconstructed input could be certified with no evidence pointer at
all — for `event_derived`, `versioned_snapshot` and `label_only` alike. A completion
timestamp is not proof that the source event exists, and a provider snapshot stamp
is evidence about *availability*, not a pointer to the data used.

**Minimum fail-closed rule adopted:**

| Basis | Requires |
|---|---|
| `static_identity` | the crosswalk (already required), which now transitively proves an accepted audit **over this corpus** |
| `event_derived` | a concrete `source_evidence_table` + `source_evidence_id` |
| `versioned_snapshot` | the same |
| `label_only_retrospective` | the same — a label must not become an untraceable assertion |

**EXCLUDED rows stay exempt**, deliberately: "not admissible" is frequently a
statement that the evidence does not exist, and demanding a pointer to absent
evidence would force a fabricated one.

**Repair:** `trg_rip_eligible_needs_evidence` (f019) + repository validation.

### D4 — evidence pointers were unvalidated strings

`source_evidence_table` accepted anything: `not_a_real_table`, `teams`,
`kalshi_markets`, `entity_match_decisions`, `'; DROP TABLE teams; --`, and
`reconstructed_input_provenance` citing itself. `source_evidence_id` was never
resolved.

**Repair (the narrowest reproducible design, §7's first option combined with a DB
allowlist):** `sports_quant/retrospective/evidence.py` defines the 19 permitted
tables — exactly the append-only observation tables plus `raw_responses`.
Deliberately excluded: mutable current-state, canonical dimensions, matcher/DQ
*conclusions*, and the provenance tables themselves.

* the **name** is enforced by `trg_rip_evidence_table_allowed` (f019), so raw SQL
  cannot bypass it;
* the **row's existence** is enforced by the repository, because SQLite cannot
  resolve a table name held in a column. This split is stated rather than papered
  over, and a test asserts the two copies of the list cannot drift.

### D5 — timestamps were shape-only

f018 used `LIKE '____-__-__T__:__:__%Z'`. `LIKE` is case-insensitive and checks no
calendar validity, so all of these stored successfully: month `99`, day `99`, hour
`77`, `2026-02-30`, `2026-02-29` (not a leap year), lower-case `z`, and
`+01:00Z`. These are TEXT columns compared **lexicographically**, so
`2026-99-01…` sorts after `2026-12-31…` and would mis-order against every
well-formed row.

**Repair:** f019 triggers combining a case-sensitive digit-exact `GLOB` with a
`strftime` round-trip. Two subtleties worth recording, because both are easy to get
wrong: SQLite *normalizes* `2026-02-30` to `2026-03-02` (so the repair requires the
round-trip to return the input **unchanged**), and a CHECK/WHERE evaluating to NULL
is **not** a failure in SQL (which is precisely how month 99 passed f018's test) —
hence the `IFNULL` wrapper. Verified against 20 cases, 20/20 correct.

### D6 — cross-league supersession was repository-only

Refused by `record_corpus_version`, accepted by raw SQL. **Repair:**
`trg_rcv_supersede_same_league`.

### D7 — credential-shaped and non-JSON values in finding detail

The 200-character blob bound did not catch short secrets: an API key is ~40
characters. `sk_live_…`, `Bearer eyJ…`, and `https://…?apiKey=SECRET123` all stored
cleanly. Separately, `float("nan")` produced the bare token `NaN`, which a strict
RFC-8259 parser rejects — so the stored detail, and the digest over it, would not be
readable outside Python.

**Repair:** a conservative marker/URL screen and rejection of non-finite floats. A
false positive is a sentence telling the caller to store a digest — never a silent
pass. Ordinary structured detail, digests and plain documentation URLs still pass.

### D8 — the package was import-order dependent

`import sports_quant.retrospective` as a process's **first** import raised
`ImportError` (a cycle through `db.schema` → `db/__init__` → repositories → back).
Every existing test masked it by importing `sports_quant.db` first via conftest.
Verified pre-existing at `4d4ae13`. **Repair:** the two `db.schema` imports in
`rules.py` are lazy. Pinned by a subprocess test, since in-process testing cannot
reproduce it.

## 4. What survived unchanged — no repair needed

* **Supersession acyclicity (§10).** Self-supersession, missing parent, and cycle
  creation via UPDATE were all refused, with foreign keys ON *and* OFF. Cycles are
  genuinely impossible: an edge can only point at a row that already exists, and no
  row is ever updated. Chains and forks both record correctly.
* **Basis-shape and verdict CHECKs (§19).** `rip_not_forward_lane`,
  `rip_static_shape`, `rip_event_shape` and `ida_accepted_is_clean` all held against
  direct SQL. The "structurally enforced" claim was true for these.
* **Rule registry (§11).** Stale digest refused, unknown rule refused, a changed lag
  changes the digest, a changed description does not — matching the documented
  contract exactly. A caller cannot supply a stale digest: the repository resolves it
  from the code registry rather than accepting it.
* **Digest determinism (§21).** A review-only canonical implementation, written
  independently of `semantic_digest`, produced **identical** digests, and both were
  stable across 200 randomized key orders while remaining sensitive to content.
* **Enum parity (§13).** All nine enum/CHECK pairs match exactly, and an
  out-of-domain value is refused. No drift existed — but there was no *test*, so one
  was added.
* **Strict-PIT isolation (§14).** Re-proved behaviourally, not by hash alone: the
  registry still exactly covers the live schema; all Lane-R tables are `unsupported`
  joins and are refused even when declared beside a joinable table; a SQLite trace
  hook confirms `AsOfReader` executes no statement naming one; `_feature_cutoff` is
  byte-identical to its v17 source; a late-observed lineup stays invisible at the
  cutoff, an on-time one stays visible, and writing Lane-R provenance changes
  neither answer.

## 5. §16 — v17 manifest compatibility: **(A) intentional and correct, with (C) a documentation duty**

Adjudicated, not assumed:

* `SUPPORTED_SCHEMA_VERSIONS` is now `{16, 17, 18, 19}`. Both preserved corpora
  (v17) remain readable; all 7 committed manifests remain valid and byte-identical.
* The exact-equality guards (`results_repair.py:255`, `cli.py:1120`) are **correct
  and must stay**: a manifest pins the schema its run must occur against, and that
  pin is what makes a preserved pilot reproducible.
* The consequence — a v17-pinned manifest cannot create a *fresh* database on a
  newer build — is the guard working, not a regression. It affects no preserved
  evidence.
* `pilots/f1/generate_lineup_continuation_manifest.py` correctly still declares 17;
  changing it would move a manifest hash recorded in a preserved checkpoint.

No compatibility repair is warranted. The documentation duty is discharged in §11 of
the implementation report and here.

## 6. Atomicity and concurrency (§17, §18)

Rollback verified around corpus + audit + findings + crosswalk + certification,
including the complete identity-audit workflow: **no accepted crosswalk survives a
failed audit transaction, and no half-written audit is consumable**. Duplicate
inserts converge on one row by digest; conflicting semantic claims fail closed;
competing supersessions are both recorded and neither silently wins.

## 7. Migration compatibility (§15)

Fresh → v19 (19 migrations, `integrity_check = ok`, `foreign_key_check` clean);
repeated init is a no-op; **v17 → v19** applies exactly 2 migrations with existing
data unchanged and every prior checksum preserved; **v18 → v19** applies exactly 1.
`f018` is byte-identical (`121619d4…`), pinned by test. Wheel packaging ships all 19.

## 8. Validation

`git diff --check` clean · ruff clean · mypy clean over **319** files ·
**2601 passed, 2 skipped** · non-editable wheel smoke **PASS** (19 migrations
packaged, fresh v19, real v17→v19 upgrade, all provenance types, D1/D2/D5 refusals
verified from the installed distribution, append-only refusal against populated
tables, 0 network trips).

## 9. Scope confirmation

*As of `2824c3a`, the commit this review covers:* no `RetrospectiveResearchReader`,
no identity-audit engine, no historical Odds API client, no market anchoring, no
F1-R executor, no F2, no model training, no Lane-L collection. (The identity-audit
engine landed afterwards — see §10.) `pit/asof.py`, `pit/dataset.py` and `matching/` are untouched since
`4d4ae13`.

**F1-R, F2, production matching and model training remain unauthorized.**
**G1, G2, G3, G4 and G6 remain open exactly as previously scoped.** G5 remains
closed as the corpus-scoped fail-closed contract — and D1 was a defect in its
*implementation*, not a change to its verdict.

---

## 10. Later status (not part of this review)

**Identity-audit engine implemented 2026-08-12 — NOT independently reviewed.**
`RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_IMPLEMENTATION.md`. The production
corpus-scoped G5 audit now runs against real evidence and independently
reproduces the reviewed one-month counts (MLB 400 games / 30 teams / 1,053
persons; NBA 239 / 30 / 550; **zero collisions**), read-only and offline. Player
static crosswalks are generated (1,053 and 550); **team and game crosswalks are
BLOCKED** — canonical `teams` is pre-seeded from names under UNIQUE constraints,
so a provider-keyed franchise cannot be bootstrapped and reusing a seed would
require name matching, which G5 forbids as identity evidence. Reported as a
blocker rather than forced. Also recorded: `birth_date` is absent for **every**
person in both corpora, so person-collision detection had no secondary evidence.
One month is still **not** evidence of 3–5 season stability.

Still unimplemented: `RetrospectiveResearchReader`, historical odds/market
anchoring, team/game crosswalks. Still unauthorized: **F1-R**, **F2**, production
matching, model training. G1/G2/G3/G4/G6 unchanged.

**Identity-audit engine REVIEWED 2026-08-12 — ACCEPTED WITH REPAIRS AND A
RETAINED BLOCKER.** `RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_INDEPENDENT_REVIEW.md`.
Ten defects proven and repaired; audit policy bumped to `g5-identity-audit-v2`.
Two were fail-open holes in the G5 contract itself (a game id reused across a
doubleheader read as clean; any generation string but `unverified` counted as
VERIFIED), and one let the CLI write provenance into the corpus being audited.
Detection power is now recorded on every audit, which changes how the one-month
result must be read: **no game id in either corpus was observed more than once**,
so the game audit compared nothing, and `birth_date` is absent for every person,
so within-league person reuse is undetectable. `ACCEPTED` means "no contradiction
detected at this policy's detection power" — **not** "verified stable identity".
Player crosswalks accepted; **team and game crosswalks remain BLOCKED** (Option A
ruled out on evidence: the canonical team seed carries no official provider id).
**The reader must not begin** until that architecture is separately decided.

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

**TEAM-A architecture REVIEWED 2026-08-12 — ACCEPTED WITH REPAIRS.**
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE_INDEPENDENT_REVIEW.md`. The TEAM-A
choice stands, but six design claims were proven false. Two are load-bearing:
**the corpus map digest does NOT bind the crosswalk** (v19 accepts a crosswalk
contradicting the committed map — the database cannot enforce agreement with an
external artifact), and **the curation uniqueness rule contradicted the many→one
rule** (the correct invariant is provider-key functional uniqueness; canonical-
target injectivity is NOT required, and a test that pinned the observed 30↔30
shape as policy has been repaired). Also: the enforced game key
`UNIQUE(official_provider, official_game_key)` carries **neither league nor
generation** → resolved as GAME-NAMESPACE-B, namespace-qualified provider values
(`balldontlie:nba:v1`), which needs no migration; the crosswalk digest captures the
conclusion, not the curation evidence; **no canonical-team seed digest exists**, so
a later seed edit would silently change an old corpus's meaning; and "independent
attribute" overstated corroboration — TEAM-A curates *denotation* and does **not**
prove provider-id permanence.

Schema verdict: **V19 SUFFICIENT WITH ADDITIONAL CODE INVARIANTS** — no migration,
but map-membership and seed-versioning enforcement must be added in code and CI,
and are weaker than the DB-enforced G5 bindings.

**TEAM-A implementation may be separately authorized. The reader remains BLOCKED**
until that implementation is itself independently reviewed. F1-R, F2, production
matching and model training remain unauthorized. G1/G2/G3/G4/G6 unchanged.

**TEAM-A IMPLEMENTED 2026-08-12 — NOT independently reviewed.**
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION.md`. The committed 60-entry
attestation map, deterministic team static-crosswalk generation, official-provider
canonical-game bootstrap, and the RV1/RV3/RV5 code+CI invariants are in place.
**Schema stays v19** (19 migrations, no new migration, f018/f019 untouched).
Reproduced read-only over both protected corpora with **0 provider requests** and
byte-identical protected artefacts (MLB 2026-06: 30 teams, 400 games; NBA 2026-03:
30 teams, 239 games). RV1 map-membership enforcement is a **detective** control:
CI proves a contradicting crosswalk is caught, but direct SQL can still write one,
so it is weaker than the DB-enforced G5 bindings. **The reader remains BLOCKED**,
and F1-R, F2, production matching, model training and feature engineering remain
unauthorized. G1/G2/G3/G4/G6 unchanged.

**TEAM-A implementation REVIEWED 2026-08-13 — ACCEPTED WITH REPAIRS.**
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION_INDEPENDENT_REVIEW.md` is
**authoritative where it differs from the implementation report**. Seven defects
were proven and repaired. Two were serious: **a canonical game could be created
with no persisted G5 audit at all** (the bootstrap trusted an in-memory object
claiming ACCEPTED), and **dry-run predicted the opposite of apply** for both new
entity types. Also: canonical games carried no corpus/audit provenance (now
written as v19 game static crosswalks — **GAME-PROV-C, no v20**); no convergence
with conventionally matched bare-provider games; an existing game with a
contradictory season was silently reused; the verifier never recomputed the
crosswalk semantic digest, so the "cryptographically bound to the map" claim was
unverified; and live-reference conflicts were not decision-backed. Completeness
semantics were split — the old check proved league-map materialization, not the
reviewed referenced-id contract. Schema verdict: **V19 SUFFICIENT WITH
ADDITIONAL REPAIRS**. Reproduced read-only over both corpora with **0 provider
requests** (MLB 2026-06: 30 teams, 400 games, 400 game provenance rows; NBA
2026-03: 30 teams, 239 games, 239 rows), dry run matching apply on both.
**The reader may now be separately authorized**; it was NOT started. F1-R, F2,
production matching, model training and feature engineering remain unauthorized.
G1/G2/G3/G4/G6 unchanged.

**RetrospectiveResearchReader IMPLEMENTED 2026-08-13 — NOT independently
reviewed.** `RETROSPECTIVE_RESEARCH_READER_IMPLEMENTATION.md`. The Lane-R reader
(architecture §12) is a **distinct type**, not a flag: no `ignore_pit=`-style
bypass exists on either reader, `_feature_cutoff` is byte-identical, and
`AsOfReader` gained nothing. **Schema stays v19** — no migration. FORWARD_ONLY
families (lineups, injuries, rosters, probable pitchers) are refused
**structurally**, before any database access, at any cutoff, even with a valid
certification present. Admission requires a **persisted** v19 certification for
the exact corpus/namespace/target/family; `effective_at` is **derived on read**
(STATIC_IDENTITY timeless, EVENT_DERIVED completion + digest-bound rule lag,
VERSIONED_SNAPSHOT provider stamp) and gated `<= T_cut`. Three gates were found
comparing a stored `str` to an enum with `is` and **failing open** — EXCLUDED
certifications admitted, strict-forward corpora readable, extended evidence
reported as core — all three repaired with regression tests. Real evidence,
read-only, **0 provider requests**: exactly one family (`static_identity`) is
admitted per corpus; `prior_results` was correctly **refused** because both
corpora are collection-time-observed, which is the very leak Lane R exists to
prevent. **F1-R, historical odds/market anchoring, F2, production matching,
feature engineering, model training, calibration, backtesting, recommendation
output and UI remain UNAUTHORIZED.** G1/G2/G3/G4/G6 unchanged.
