# G5 — provider ID stability gate review

Focused architecture/correctness review of gate **G5**, which was — at the time this
review began — the only gate blocking implementation of the retrospective PIT
architecture (`94801b6`). It is closed below.

Design/documentation only. No Lane-R reader, no schema v18, no F1-R, no F2, no
production matching, no model training, **no provider data/API request**. Official
documentation was read (research). Protected evidence read-only and unchanged.

**Verdict: G5 CLOSED — CORPUS-SCOPED FAIL-CLOSED CONTRACT.**

**Schema v18 (2026-08-11): provenance FOUNDATION implemented, not yet reviewed.**
`RETROSPECTIVE_PIT_SCHEMA_V18_IMPLEMENTATION.md`. Migration `f018` adds five
append-only tables (reconstruction corpus versions, identity audit records,
identity audit findings, static crosswalk provenance, reconstructed input
provenance) plus the `sports_quant.retrospective` domain vocabulary, a
code-defined digest-bound availability-rule registry, and narrow repositories.
`availability_confidence` is not stored (removed by review) and `effective_at` is
derived, never materialized. Strict PIT is unchanged: the v18 tables are
`unsupported` joins, `AsOfReader` has no retrospective mode, and `_feature_cutoff`
is byte-identical to its v17 source.

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

**Lane-R reader INDEPENDENTLY REVIEWED 2026-08-13 — ACCEPTED WITH REPAIRS,
RETAINED DATA BLOCKER for F1-R.**
`RETROSPECTIVE_RESEARCH_READER_INDEPENDENT_REVIEW.md`. Two defects reproduced and
repaired: **(high)** a tampered crosswalk canonical target was ADMITTED and
`static_identity()` returned the wrong canonical id, because the reader never
recomputed the crosswalk's own semantic digest — now checked in-band on every
identity read; **(moderate)** the admission API silently ignored the namespace
`entity_type` — now required to be GAME. Seventeen further falsification attempts
(corpus/namespace/family substitution, rule-digest and rule-id tampering,
malformed timestamps, hostile family names, label relabelling, live-reference
identity) all failed to break it. Strict-forward PIT unweakened; **schema stays
v19**, no migration. **RETAINED BLOCKER:** no event-completion instant exists in
either bounded corpus (`game_status_history` empty in both), so **EVENT_DERIVED
is data-blocked** and F1-R may not yet be authorized to produce it. The safe next
step is a read-only investigation of whether preserved `raw_responses` carry a
usable completion timestamp. F1-R, odds/market anchoring, F2, production
matching, feature engineering, model training, calibration, backtesting,
recommendation output and UI remain UNAUTHORIZED. G1/G2/G3/G4/G6 unchanged.





**This foundation has NOT been independently reviewed.** Still unimplemented:
`RetrospectiveResearchReader`, the identity-audit engine, and historical
odds/market anchoring. Still unauthorized: **F1-R**, **F2**, production matching,
and model training. G1/G2/G3/G4/G6 are unchanged.

The G5 verdict itself is unchanged; f018 stores the contract, it does not reopen or re-decide it.

**REVIEWED 2026-08-11 — ACCEPTED WITH REPAIRS; the foundation is now schema v19.**
`RETROSPECTIVE_PIT_SCHEMA_V18_INDEPENDENT_REVIEW.md`. Eight defects proven (each
reproduced through the repository API *and* direct SQL), two material to the G5
contract: a static crosswalk could cite an ACCEPTED audit taken over a **different
source corpus** (a one-month audit vouching for five seasons — the exact transfer
G5 §16 forbids), and an ACCEPTED audit could later acquire a **blocking
identity_collision** finding while crosswalks built from it survived. Also repaired:
eligible inputs needed no source-evidence pointer; `source_evidence_table` accepted
any string; timestamps were shape-only (month 99, Feb 30, lower-case `z`, offsets
all stored); cross-league supersession was repository-only; credential-shaped and
non-JSON values passed the finding screen; and the package was import-order
dependent. Repairs ship as migration **`f019`** — `f018` is preserved byte-for-byte
as applied evidence and was **not** edited. Strict PIT re-proved behaviourally:
`AsOfReader` and `_feature_cutoff` unchanged, Lane-R tables still `unsupported`
joins, late-observed lineups still invisible.

The original closure criterion was **unachievable and mis-scoped**. It is replaced
with a verifiable, corpus-scoped consistency contract that is scientifically
sufficient for Lane R.

---

## 1. Boundary

`HEAD == origin/main == 94801b6`, clean tree, CI #89 green, **schema v17**.
Commit `94801b6` touched **0** files under `sports_quant/`. No identity-audit code,
no retrospective reader, no market anchoring exists. **Protected artefacts: 7/7
byte-identical.**

## 2. Primary-documentation findings

### BALLDONTLIE (docs.balldontlie.io, accessed 2026-08-10)

* Numeric `id` on every primary resource (games `15907925`, teams `1`, players `115`).
* Direct lookup: `GET /v1/games/<ID>`, `/v1/teams/<ID>`, `/v1/players/<ID>`;
  array filters `game_ids[]`, `team_ids[]`, `player_ids[]`.
* **Uniqueness / permanence / retirement / reuse: NOT ADDRESSED.** Zero statements.
* **ID-namespace versioning between v1 and v2: NOT ADDRESSED.**

### MLB StatsAPI (accessed 2026-08-10)

* The official documentation at `docs.statsapi.mlb.com` / `statsapi.mlb.com/docs`
  is **behind a login wall**; `statsapi.mlb.com/` returns **HTTP 406** to a
  documentation fetch.
* **No primary-source statement on identifier uniqueness, permanence, retirement
  or reuse is publicly obtainable.**
* Unofficial wrappers document `gamePk`, team `id` and person `id` as direct
  lookup keys, but per the task constraint **unofficial sources cannot close this
  gate** and are not relied on here.

### What is documented vs. assumed

| Claim | Status |
|---|---|
| IDs exist and are the direct lookup key | **documented** (BALLDONTLIE); conventional and observable for MLB |
| IDs are unique *within a response/namespace* | **conventional database/API behaviour**, not stated |
| IDs are never reused across all history | **NOT DOCUMENTED by either provider** |
| ID namespace is stable across API versions | **NOT DOCUMENTED** |

**Conclusion: "G5 CLOSED — PROVIDER GUARANTEE" is not attainable.** Neither
provider states permanence, and MLB's documentation is not even publicly readable.
Asserting permanent non-reuse would be exactly the fabrication this architecture
exists to prevent.

## 3. Global permanence is not the right requirement

The original G5 asked for *global permanent non-reuse across all history*. That is
**stronger than Lane R needs** and is unverifiable in principle.

Lane R resolves a provider ID **only when that ID appears inside the reconstruction
corpus**. A feature never references an ID outside its own source window. So the
question that actually matters is closed-world:

> Is every observation of `(league, provider, entity_type, provider_id)` **within
> this exact corpus** compatible with one canonical entity?

Reuse in 1974, or in 2035, cannot affect a 2021–2025 reconstruction, because no row
in that reconstruction references it. A closed-world property over the exact
evidence used is also the only form that is **verifiable and reproducible** —
which is what a scientific contract requires.

Per entity type, over the 3- and 5-season windows: this reframing is valid for
**game**, **team** and **player-person** identity alike, because all three are
resolved only from IDs appearing in corpus rows.

## 4. Replacement G5 contract — accepted, with additions

The proposed seven-point contract is **scientifically sufficient**, with two
additions the review requires (8 and 9):

An official provider ID may serve as a Lane-R static crosswalk only when:

1. provider and league are designated and fixed;
2. the exact provider ID appears **directly in the historical evidence row**;
3. resolution uses **namespace + entity type + provider ID**, never name alone;
4. **all observations of that ID within the reconstructed source corpus are
   compatible with one canonical entity**;
5. any incompatible reuse/collision **fails closed**;
6. later current-state affiliation, position, status or roster information can
   **never** justify the crosswalk;
7. the reconstruction manifest binds the exact provider namespace and the
   **identity-audit digest**;
8. **(added)** the audit is re-run over the **entire** source window of the
   reconstruction actually being accepted — a narrower window's pass never
   transfers;
9. **(added)** the API **version/namespace generation** is recorded, and any change
   fails closed pending explicit review.

## 5. Identity key

**`(league, provider, entity_type, provider_id)`** — all four components required.

This makes MLB team `147` and MLB person `147` different keys; BALLDONTLIE team `1`
and MLB team `1` different keys; and NBA player vs NBA team ids non-comparable.
`entity_type` is essential: without it, numerically small ids collide across
resource classes trivially. API generation is carried as namespace metadata
(point 9), so v1 and v2 ids are never silently equated.

## 6. Game-ID consistency contract

Compare, across every observation of a game ID:

**Identity-defining (a difference is an impossible collision → fail closed):**
season · sport/league · home official team id · away official team id.

**Legitimate mutation of the same event (a difference is NOT a collision):**
scheduled start · game date · status · venue · postponement/resumption ·
doubleheader game number · rescheduled date.

**Never used as identity evidence:** final score, outcome, any boxscore value.

Doubleheaders are distinguished by the provider assigning **distinct game ids**;
game number is metadata, not the key.

## 7. Team-ID consistency contract

The canonical `team` represents **franchise identity**.

**Legitimate:** rename, relocation, abbreviation change, venue change, division/
conference change.
**Collision:** the same team id observed under a different **league/sport**, or
resolving to two distinct franchises within the corpus.

**Display name is never used for collision detection** — that is precisely the
signal that changes legitimately. This matches the review's earlier id-only rule.

## 8. Player-person consistency contract

Hardest class, and treated most conservatively.

**Legitimate:** team change, position change, jersey change, active-status change,
display/preferred-name change.

**Collision signature (detection only, never a matching key):** league/sport
mismatch is decisive; birth date and draft metadata are used **when present in the
historical evidence**, never required. Name variation is a **secondary signal
only** — it may raise a flag for review but must **never** override an exact stable
ID, and must never be used to merge two ids.

**Team affiliation is never identity evidence** (established in the architecture
review; reaffirmed here).

**Where secondary evidence is absent:** the exact official ID alone remains
acceptable **within the bounded namespace** provided points 1–9 hold. This is a
deliberate, stated limitation rather than a coverage optimisation: two genuinely
distinct persons sharing one provider id, with every observed attribute agreeing,
are indistinguishable from the evidence — no reconstruction could do better, and
the audit records that the limit was reached.

## 9. Corpus-wide identity audit (design)

Deterministic, pre-reconstruction, read-only. Emits per namespace: distinct game /
team / player ids · observations per id · allowed metadata transitions ·
incompatible observations · collisions · unresolved ids · provider
namespace/version · **audit digest**.

**Severity ladder — blast radius proportional to impact:**

| Finding | Blocks |
|---|---|
| One incompatible **player** id | that player id only; features depending on it are excluded and recorded |
| One incompatible **team** id | every game involving that franchise in the corpus (team identity is structural) |
| One incompatible **game** id | that game as a target **and** as a source event for rolling features |
| Namespace/version uncertainty | the **entire** league corpus, pending review |
| ≥1% of ids in a class incompatible | the **entire** league corpus — a systemic signal, not an outlier |

## 10. Cross-season behaviour

* **Same player over five seasons** — one id, many observations; team/position/name
  changes are legitimate transitions.
* **Retired / returning player** — absence is not a collision; re-appearance under
  the same id with compatible attributes is expected.
* **Traded player** — affiliation changes; identity does not.
* **Relocated/renamed franchise** — id-only matching absorbs it.
* **Postponed/rescheduled/replacement games** — date/status mutation, not collision.
* **Cancelled game** — the id simply never gains a result; excluded as a target.
* **Duplicate ids** — fail closed per the severity ladder.

Results must be **order-independent**: the audit aggregates into sets and sorted
structures, so traversal order cannot change the verdict or the digest.

## 11. New evidence after a corpus is built

* The old reconstruction stays **reproducible and immutable** — its provenance and
  digests are never rewritten.
* New evidence producing an incompatible observation yields a **new source-corpus
  version** with a **different identity-audit digest**.
* That new version **supersedes** the old for *future* research and must be
  declared when comparing results across versions.
* **Nothing silently mutates.** A superseded corpus is marked superseded, not
  deleted, and previously published results remain attributable to their exact
  corpus version.

## 12. Collision detection is not matching

The audit asks only *"is this exact official key internally consistent?"* — never
*"which entity does this name probably mean?"*. No fuzzy or name-based resolution
enters Lane R. Canonical mapping stays an exact, auditable crosswalk; the
separately reviewed matching system is unaffected and never influences historical
effective time.

## 13. Timing semantics — no new timestamp needed

* `decided_at` remains **honest audit time**: when Moneymaker curated the crosswalk.
  It is never backdated and never overloaded.
* Lane-R static identity is permitted **independently of `decided_at`** because a
  stable-ID reference is timeless — it carries no information about the game's
  outcome, so its curation date cannot leak anything.
* **No `effective_at` is required for STATIC_IDENTITY** (consistent with the
  architecture review, which already ruled that identity has no knowledge-time).

**Implementation therefore needs: an identity-audit record** (namespace, policy
version, counts, collisions, digest) **and a static-crosswalk provenance record**
binding a crosswalk to the audit that cleared it. **No new timestamp field.**

## 14. Adversarial cases the contract rejects

| Attack | Rejected by |
|---|---|
| Current team used to disambiguate an old player | rule 6 — affiliation is never identity evidence |
| Current name used after a historical name change | rule 3 — name alone never resolves; names are detection-only |
| Final status used to identify a rescheduled target | §6 — status/date are mutation, not identity; anchoring uses contemporaneous `commence_time` |
| Later franchise name as the only historical team key | §7 — display name never used |
| Final boxscore used to separate same-name players | §6/§8 — outcome data is never identity evidence |

The contract requires **none** of these to function.

## 15. Empirical audit of existing evidence (read-only)

Running the proposed rules over the real corpora:

| | distinct games | game collisions | distinct teams | team collisions | distinct players | player collisions | ids with >1 observation |
|---|---|---|---|---|---|---|---|
| **MLB June 2026** | 400 | **0** | 30 | **0** | 1,053 | **0** | teams 30, players 1,044 |
| **NBA March 2026** | 239 | **0** | 30 | **0** | 550 | **0** | teams 30, players 549 |

Zero name-variance flags in either corpus. Digests were stable under
re-serialization, confirming order-independence.

The test genuinely exercised the repeated-observation path: **1,044 MLB and 549 NBA
player ids were observed more than once**, so consistency was checked, not merely
assumed from single observations.

> **SCOPE LIMIT — stated explicitly.** These are **one-month** corpora. A
> collision-free result here **does not prove 3- or 5-season stability** and is not
> presented as doing so. It demonstrates the rules are implementable, deterministic
> and clean on real evidence — nothing more.

## 16. Five-season closure rule

Before any multi-season Lane-R reconstruction is accepted, the identity audit is
**re-run over the entire source window** of that reconstruction. The guarantee the
project may then state is:

> *No provider-ID reuse or collision was observed anywhere in the exact
> reconstruction corpus, under the documented consistency rules, at the recorded
> audit-policy version and source-corpus fingerprint.*

**Never:** *"the provider guarantees IDs are never reused."*

**Is the narrower guarantee sufficient?** Yes, for Lane R, because Lane R
dereferences only IDs present in that corpus. The residual — two distinct entities
sharing one id with *every observed attribute agreeing* — is undetectable from the
evidence by any method, and is recorded as a bounded limitation rather than
resolved. No further external proof is required for the reconstruction to be valid
**as scoped**; a stronger, cross-era claim would require provider documentation
that does not exist.

## 17. Fail-closed behaviour

On any incompatibility: record a **blocking DQ finding**, exclude per the §9
severity ladder, and record the exclusion in the manifest.

Explicitly prohibited: name fallback · silent canonical merge · "take latest" ·
"first row wins" · majority vote · any heuristic tie-break. Missing supporting
attributes reduce *detection power* and must be reported; they never license a
merge.

## 18. Reproducibility

**Identity-audit digest** over: provider namespace (incl. API generation) ·
entity type · normalized provider id set · allowed/forbidden identity attributes ·
source-corpus fingerprint · audit-policy version · collision result.

Randomized traversal of identical evidence must yield an identical digest and
verdict. The reconstruction manifest binds this digest; a change in any input
changes the corpus version (§11).

---

## Verdict

**G5 CLOSED — CORPUS-SCOPED FAIL-CLOSED CONTRACT.**

The original criterion — provider documentation guaranteeing global non-reuse —
is **unattainable** (BALLDONTLIE is silent; MLB's documentation is login-gated) and
**mis-scoped** (Lane R never dereferences an ID outside its own corpus). It is
replaced by the nine-point corpus-scoped contract above, which is verifiable,
deterministic, fail-closed, version-bound, and demonstrated clean on real evidence.

This verdict was **not** chosen to unblock implementation: had the corpus-scoped
audit been unable to detect reuse, or had it required name or affiliation evidence
to function, the correct answer would have been *G5 REMAINS OPEN*. It detects
collisions using only namespace-scoped identity attributes, and requires none of
the future-informed signals in §14.

### Other gates — unchanged, none closed here

| Gate | Still blocks |
|---|---|
| **G1** correction history | **Extended Lane-R** features for F2; core is unaffected. Forward re-poll measurement still required. |
| **G2** Kalshi retention/inception | **All Kalshi-based EV and liquidity claims**; Kalshi is unusable in F1-R until closed. |
| **G3** weather archive depth | **Pre-2024 weather**; weather stays out of the 5-season core. |
| **G4** pre-2022 decimal-derived odds | Requires a **rounding sensitivity analysis** for pre-2022-09-18 backtests. |
| **G6** Odds API terms at launch | **Public commercial launch** only; not research. |

### Implementation authorization

With G5 closed, **no gate blocks implementation**. This task remains
documentation/design only and authorizes nothing. The statement this review
supports is:

> **The next separately authorized phase MAY implement the reviewed architecture** —
> identity audit, static-crosswalk provenance, the Lane-R reader and schema v18 —
> subject to its own independent review. G1–G4 and G6 continue to bound what that
> implementation may *claim*, not whether it may be *built*.

Confirmations: no implementation occurred; **no provider data/API request** was
made; protected evidence **7/7 byte-identical**; schema **v17**; **F1-R, F2,
production matching and model training remain unauthorized** unless a later task
explicitly changes that.
