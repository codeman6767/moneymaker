# Retrospective TEAM-A team/game crosswalk — implementation report

> **IMPLEMENTED 2026-08-12. INDEPENDENTLY REVIEWED 2026-08-13 — ACCEPTED WITH
> REPAIRS.** `RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION_INDEPENDENT_REVIEW.md`
> is **authoritative where it differs from this document**. Seven defects were
> proven and repaired: dry-run/apply parity was broken for teams and games; a
> canonical game could be created with no persisted G5 audit; canonical games
> carried no corpus/audit provenance (now written as v19 game static crosswalks);
> no convergence with conventionally matched bare-provider games; an existing
> game with a contradictory season was silently reused; the verifier never
> recomputed the crosswalk semantic digest; and live-reference conflicts were not
> decision-backed. Statements below marked **SUPERSEDED** were true of `982b73b`
> and are no longer true.
>
> **Original banner (as written at implementation time):**
> This report describes what was built against the architecture reviewed at
> `c0dfcd0` (*ACCEPTED WITH REPAIRS*, schema verdict *V19 SUFFICIENT WITH
> ADDITIONAL CODE INVARIANTS*). It has not itself been reviewed. The
> retrospective research reader remains blocked; F1-R, F2, production matching
> and model training remain unauthorized.

**Schema unchanged: v19, 19 migrations, 52 tables.** No migration was added, no
migration was edited, and `f018`/`f019` are untouched.

---

## 1. What was authorized, and what was built

| Reviewed item | Status |
|---|---|
| Source-controlled TEAM-A franchise attestation map | `retrospective/attestations.py` — 60 entries |
| Deterministic team static-crosswalk generation | `retrospective/team_crosswalks.py` |
| Deterministic official-provider canonical-game bootstrap | `retrospective/game_bootstrap.py` |
| RV1 code + CI invariant (map digest actually binds) | `retrospective/verifier.py` + CLI + CI job |
| RV3 code invariant (game namespace carries league + generation) | `retrospective/namespaces.py` |
| RV5 code + CI invariant (canonical seed is versioned) | `canonical_team_seed_digest()` + CI |

Nothing outside that list was implemented. In particular there is no
`RetrospectiveResearchReader`, no historical Odds API fetching, no market
anchoring, and no feature engineering.

## 2. The committed map

`TEAM_ATTESTATIONS` is 60 rows of source-controlled curation — 30 MLB, 30 NBA —
each binding `(league_id, provider, namespace_generation, provider_team_id)` to a
canonical franchise that already exists in the seeded `teams` dimension.

| Constant | Value |
|---|---|
| `MAP_FORMAT_VERSION` | `team-a-map-v1` |
| `TEAM_ATTESTATION_POLICY_VERSION` | `g5-team-attestation-v1` |
| `GAME_BOOTSTRAP_POLICY_VERSION` | `g5-game-bootstrap-v1` |
| `canonical_team_seed_digest()` | `19d7e98d239a582c8968fbe819fb6926b60f8a568ef56a71e134e7e74d6a7fcc` |
| `attestation_map_digest()` | `ae21c26b7ca642755dbf6d2f5e4aeee1ae4c169889670f730ff40aa031d290d9` |

**Resolution is exact lookup only.** `attested_canonical_team` consults the key
and nothing else — no name, no alias, no abbreviation, no nearest match. A key
absent from the map returns `None`, which means UNRESOLVED and stops the write.
This is asserted structurally: a test strips docstrings from the resolver and
from `team_crosswalks` and fails if `normalize_name`, `alias_specs`,
`normalized_name`, `full_name`, `abbreviation` or `nickname` appears in
executable code.

**The map validates many→one but not one→many.** `_validate_t1` refuses a
provider key that denotes two franchises, and deliberately does *not* require
canonical-target injectivity: several provider ids legitimately denote one
franchise across generations, and forcing injectivity would rewrite sports
history to make ids convenient. `describe_map_shape()` reports
`distinct_canonical_targets` as an **observation, never a rule**.

## 3. The three review repairs, and what each is actually worth

### RV1 — the map digest must genuinely bind the crosswalk

The review proved the digest was recorded but not enforced. Three changes:

1. `record_static_crosswalk` accepts an optional `attestation_map_digest` and
   folds it into the row's semantic digest **only when present**, so existing
   player crosswalk digests are byte-identical.
2. `_require_corpus_provenance` refuses to write a team crosswalk unless the
   corpus carries `static_identity_map_digest`, that digest equals the digest
   recomputed from committed source, and `code_version` is present. Corpus rows
   are append-only, so neither can be backfilled later.
3. `retrospective/verifier.py` re-derives the map and checks every stored team
   crosswalk against it.

> **This is a detective control and is weaker than DB row-to-row enforcement.**
> Stated plainly rather than glossed: direct SQL can still write a contradicting
> row, because SQLite cannot read an external artifact. What is guaranteed is
> that such a row cannot survive a verification pass — and CI runs one.

Proven in both directions in CI (§6) and in tests: an honest crosswalk verifies
clean; a row with a **real** ACCEPTED audit, a **real** corpus and a **real**
canonical team that nonetheless contradicts the map is reported, and
`team-attestation-verify` exits non-zero.

> **SUPERSEDED (partially):** as written at `982b73b`, the verifier checked the
> corpus map digest, map membership, the canonical target and the policy — but
> **never recomputed the row's own `semantic_digest`**, so "the crosswalk is
> cryptographically bound to the map" was not actually verified: a tampered
> digest passed. The verifier now recomputes it, and distinguishes a tampered
> digest from a legitimate pre-TEAM-A non-map-backed one. See the independent
> review §D6.

### RV3 — the game key must carry league and generation

`games` carries `UNIQUE (official_provider, official_game_key)` — global, with
no league or generation column. Rather than add a migration, the *provider value
itself* is namespace-qualified:

```
mlb_statsapi:mlb:v1     balldontlie:nba:v1
```

`qualified_provider()` fails closed on a wrong league, an unattested generation,
or an undeclared provider. The existing index therefore enforces league +
generation uniqueness with no schema change, which is why schema v20 was not
needed.

### RV5 — the canonical seed must be versioned

Canonical team ids are abbreviation-derived, and the seed also carries the
historical aliases the curation relied on, so a later seed edit would silently
change what an already-built corpus's attestation *means*.
`canonical_team_seed_digest()` covers, per franchise: league, canonical id,
abbreviation, canonical name, city, nickname, and the full alias set **with its
alias types** — a name moving from `historical` to `full` is a franchise-semantics
change even when the string is identical. It is folded into the map digest, so a
seed edit changes the corpus version rather than passing unnoticed.

## 4. Canonical game bootstrap

A canonical game is created only when the provider is the designated official
provider, an ACCEPTED G5 game audit exists for the corpus's **exact** source
digest, the generation is attested, **both** provider team ids have a TEAM-A
crosswalk *in this corpus version*, the source supplies the required non-outcome
metadata, and nothing conflicts with an existing canonical game.

`canonical_game_id` is a pure function of the qualified provider and the official
game key. **No score, winner or outcome participates in identity.** A reschedule
can never mint a second game.

> **SUPERSEDED:** this paragraph previously said a reschedule "updates
> description". It does not. Replay reuses the existing canonical game and writes
> no descriptive update -- canonical descriptive metadata is immutable after
> first bootstrap. That behaviour is correct; the prose was wrong. See the
> independent review section 10.

The plan separates two gaps that are easy to conflate:

* `missing_metadata` — the **source evidence** lacks a required field.
* `missing_output_season` — the **output database** has no `seasons` row for the
  season the evidence names. The bootstrap refuses to invent a canonical season.

That distinction was not cosmetic: an NBA run initially reported 0/239 games
ready, and the split showed the cause was an unprepared output season, not
missing evidence. The bootstrap had correctly refused to guess.

### Evidence strength — inherited, not upgraded

The G5 game audit over the one-month corpora observed **no game id more than
once**. A clean verdict there means *"no contradiction was detected"*, not
*"game ids are verified stable"*. This module inherits that limitation and does
not upgrade it.

## 5. Real-evidence reproduction (read-only, zero network)

Both protected F1 corpora were opened through the already-accepted
`immutable=1` path. **0 provider requests. 0 writes to any protected DB.** All
42 protected artefacts were fingerprinted before and after and are byte-identical
with unchanged source DB and WAL mtimes.

| Corpus | Teams: ids / attested / written / unresolved / conflicts | Games: ids / ready / created | Verifier |
|---|---|---|---|
| MLB June 2026 | 30 / 30 / 30 / 0 / 0 | 400 / 400 / 400 | `checked=30 ok=True` |
| NBA March 2026 | 30 / 30 / 30 / 0 / 0 | 239 / 239 / 239 | `checked=30 ok=True` |

Qualified providers used: `mlb_statsapi:mlb:v1`, `balldontlie:nba:v1`.

## 6. Enforcement in CI

The `wheel-smoke` job gained **TEAM-A attestation invariants (RV1/RV3/RV5,
offline, synthetic only)**, which runs against the installed wheel, requires no
protected corpus, and makes no provider request:

1. `team-attestation-verify --json` with no database must pass, and the map
   format, policy version, **both pinned digests** and the 60-entry shape must
   match. A silent edit to the map or the franchise seed fails CI, not just a
   corpus nobody rebuilds.
2. **RV5**: every canonical target the map names must exist in the freshly seeded
   `teams` dimension.
3. **RV1**: an honest crosswalk verifies clean, and a planted contradicting row —
   valid under every v19 constraint — is reported with `committed map says …` and
   exits non-zero.

## 7. Strict-PIT isolation, re-proved

Re-proved as durable tests, not asserted in prose:

* The strict-forward cutoff policy `_feature_cutoff` is **unchanged** (source
  hash pinned at `5d55345b6e2d8836df83428de82462df`). A deliberate change must
  update the pin and be reviewed as a PIT change.
* All **five** Lane-R tables (`identity_audit_records`,
  `identity_audit_findings`, `reconstruction_corpus_versions`,
  `static_crosswalk_provenance`, `reconstructed_input_provenance`) remain
  `unsupported` in the PIT registry.
* `AsOfReader` has no executable code mentioning a corpus version,
  retrospective mode, static crosswalk, attestation or identity audit.
* No TEAM-A module imports `pit.dataset`, `pit.asof`, `AsOfReader` or
  `_feature_cutoff`.
* An **August-fetched March lineup stays invisible at a March cutoff** — and the
  same reader *does* see it at an August cutoff, so the test cannot pass on a
  reader that simply returns nothing.

## 8. What is still blocked

* `RetrospectiveResearchReader` — **not implemented.** No public API produces
  historical model feature rows. **Since the independent review of 2026-08-13 the
  reader may be separately authorized**, because game identity is now
  corpus-scoped and audit-backed.
* Historical Odds API fetching and market anchoring — **not implemented.**
* F1-R, F2, production matching, model training, feature engineering — **not
  run, still unauthorized.**
* Architecture gates G1, G2, G3, G4, G6 — **not closed** by this work.
* This implementation — **not independently reviewed.**
