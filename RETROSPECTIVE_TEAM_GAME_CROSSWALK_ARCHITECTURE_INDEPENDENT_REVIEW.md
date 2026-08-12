# Independent review — TEAM-A team/game crosswalk architecture (`7de77ab`)

Design/correctness review of `RETROSPECTIVE_TEAM_GAME_CROSSWALK_ARCHITECTURE.md`.

**Verdict: ACCEPTED WITH REPAIRS.**

The **TEAM-A choice is correct and stands** — the alternatives were rejected on
measurement, not preference, and I re-verified those measurements. But **six
design claims were proven false**, two of them load-bearing, and the schema
verdict is **V19 SUFFICIENT WITH ADDITIONAL CODE INVARIANTS** rather than the
plain "no schema change" the design asserted.

Nothing was implemented: no attestation map, no team crosswalks, no game
crosswalks, no reader, no migration, no F1-R, no F2, no production matching, no
model training, **no provider API request**, no mutation of protected evidence.

---

## 1. Boundary

`HEAD == origin/main == 7de77ab`, clean tree, CI #96 green, schema v19, 19
migrations, audit policy `g5-identity-audit-v2`, crosswalk support = `{player}`,
no attestation map present.

23 guards installed before importing any provider-facing module; **7/7 adversarial
probes blocked** (DNS, raw socket, urllib, httpx, both provider constructors,
`config.load_settings`). **0 network trips.**

**Protected evidence: 42/42 artefacts byte-identical**, source `.db` and `-wal`
mtimes unmoved.

## 2. Findings

Each was reproduced against `7de77ab` before any repair, and each has a permanent
reproducer in `test_team_attestation_architecture_review.py`.

### RV1 — the map digest does **not** bind the crosswalk *(load-bearing)*

The design's §12 stated that the source-controlled mapping digest lives in
`reconstruction_corpus_versions.static_identity_map_digest`, implying the binding
is provenance-enforced. **Proven false.** With a corpus declaring the digest of a
committed map M that says `147 → tm_mlb_hou`, a caller recorded a static crosswalk
`147 → tm_mlb_nyy` and **v19 accepted it**. Neither the stored row nor its
`semantic_digest` references M's contents at all.

The database cannot enforce this: the map is an **external artifact**, and SQLite
can only enforce relationships among rows it holds. That is a principled
difference from the G5 bindings (audit/corpus, entity type, league), which are all
row-to-row and *are* DB-enforced — so the inconsistency with precedent is
explicable rather than arbitrary, but it must be stated, not papered over.

**Required repairs, all at v19 (§32 verdict):**

1. the generator **recomputes the map and requires exact entry membership** before
   writing any crosswalk;
2. the **map digest participates in the crosswalk's `semantic_digest`**, so a
   crosswalk is cryptographically bound to the map version it came from — a digest
   *input* change, not a schema change;
3. a **verifier re-derives the map from committed source and checks every crosswalk
   row** in the corpus, run in CI.

Until all three exist, "the map digest binds the crosswalk" is false. This is
weaker than DB enforcement and is recorded as such.

### RV2 — the uniqueness rule contradicted the many→one rule *(load-bearing)*

§7 rule 4 required "exactly one canonical franchise, **and no other provider id in
that namespace claims it**", while §8 required "many official provider ids → one
canonical franchise is valid and must be supported". These cannot both hold.

**The schema settles it:** two provider ids mapping to one franchise is **accepted**;
one provider id mapping to two franchises is **refused**
(`ProvenanceConflictError`, backed by the f018 UNIQUE key). So the correct
invariant is **T1 — provider-key functional uniqueness** — and canonical-target
**injectivity is not required**.

The prior architecture test asserted `len(set(attested.values())) == 30`, promoting
a property of two one-month 2026 corpora to a global rule that would reject a
legitimate provider-id transition. **Repaired**: T1 is now the invariant, and the
30↔30 shape is recorded as an observation with an explicit comment telling a future
maintainer which assertion to relax.

### RV3 — the enforced game key carries neither league nor generation *(material)*

The design's game identity includes `namespace_generation`, but the enforced
constraint is `UNIQUE (official_provider, official_game_key)` — **global**, with no
league and no generation, and the provider strings (`mlb_statsapi`, `balldontlie`)
encode neither.

Proven: `(balldontlie, "12345")` inserted under `lg_mlb` **refuses** the same pair
under `lg_nba`; a hypothetical v2 generation re-issuing numeric ids would collide
with v1 rows. BALLDONTLIE also serves more than one sport under one brand, so the
single string is a latent hazard rather than a hypothetical one.

**Resolution: GAME-NAMESPACE-B.** Make the stored `official_provider` value
**namespace-qualified** — `balldontlie:nba:v1`, `mlb_statsapi:mlb:v1` — so the
existing index enforces the intended key. `official_provider` is plain TEXT with
**no CHECK**, so this needs **no migration** (verified by inserting a qualified
value). The qualified form must be one source-controlled constant, never built by
string concatenation at a call site.

### RV4 — the crosswalk digest captures the conclusion, not the evidence

§12 claimed "curation evidence digest folded into `semantic_digest`". The digest
actually covers corpus id, namespace, provider id, canonical id, audit digest and
policy version — **the conclusion (which team), not the evidence (why)**. No
curation attribute appears. Corrected in the document; the implementation must
either fold in the map digest (RV1 repair 2) or carry a separate source-controlled
evidence manifest.

### RV5 — no canonical-team seed digest exists

Verified: nothing in the repository digests the seed. Since canonical ids are
**abbreviation-derived** and the seed also carries the historical aliases curation
relies on, a later seed edit — alias added or removed, abbreviation changed,
franchise-history reinterpreted — would silently change what an already-built
corpus's attestation *means*. `code_version` is nullable and optional and cannot
carry this.

**Requirement:** compute a deterministic canonical-team **seed semantic digest**
and bind it into the attestation map digest, so a seed edit changes the map digest
and therefore the corpus version. No new storage — it rides the existing
`static_identity_map_digest`.

### RV6 — "second independent attribute" overstates the evidence

Name, abbreviation and nickname arrive on the **same provider observation row**,
from the same provider, at one instant. They are **secondary corroborating
attributes**, not independent-source evidence. Corroboration lowers the risk of an
accidental single-label match; it proves nothing more.

### RV7 — the correlated-label case, stated plainly (§7)

If a provider reused a team id and copied its labels coherently — name
`Houston Astros`, abbreviation `HOU`, nickname `Astros` — every corroboration rule
passes. Therefore:

> **TEAM-A curates the *denotation* of a provider franchise id. It does not prove
> provider-id permanence or non-reuse.**

That is not a new weakness: the identity-audit review already recorded that
same-league team reuse is **undetectable** from label evidence. Attestation
inherits, and cannot exceed, the audit's detection power. The document now says so
where the corroboration rule is stated, rather than only in a limitations section.

### RV8 — historical franchise semantics: correct, but two are a different relation

I verified the seed's lineage. Expos→Nationals, Indians→Guardians,
Florida→Miami, the Angels sequence, the Athletics relocations, Vancouver→Memphis,
New Jersey→Brooklyn and Bullets→Wizards are all plain franchise continuation, and
the seed has them right.

Two are **league-recognized history reassignment**, a genuinely different relation:
the 1988–2002 Charlotte Hornets history was reassigned to `tm_nba_cha` in 2014 and
relinquished by `tm_nba_nop`; and Seattle→Oklahoma City left the SuperSonics name
and banners in Seattle by settlement while the franchise continued. The seed's
treatment is defensible for a *franchise dimension*, but the design described all
entries uniformly as continuation. Documented rather than flattened.

## 3. Claims re-verified and upheld

* **Canonical teams are a franchise dimension** with continuity encoded — 13 MLB
  and 13 NBA seeds carry historical aliases; Hornets/Pelicans correctly separate.
* **Canonical ids are deterministic and abbreviation-derived** (`tm_mlb_nyy`),
  which is precisely why TEAM-B is expensive.
* **TEAM-B cost is real:** 13 tables hold an FK to `teams`.
* **TEAM-D2 is genuinely blocked:** `entity_match_decisions` is registered
  `asof_filtered` with `observed_at = decided_at`, so strict PIT gates it on
  wall-clock — recreating the original blocker. Re-read, not assumed.
* **The §5 curation/matching distinction is sound.** Attestation asks about a
  provider *franchise identifier*, an entity independent of any row or cutoff;
  runtime matching asks about a *row*. TEAM-A does not merely relocate matching
  into a file, **provided** the runtime path is an exact dictionary lookup with no
  alias fallback (§16 — must be pinned by test at implementation).
* **Diagnostic reproduced:** MLB 30/30 and NBA 30/30 uniquely attested, all
  corroborated, 0 ambiguous, 0 unresolved, 0 conflicting; 33/33 historical aliases
  resolve correctly; 639/639 games have both teams attested and required metadata.
* **Multi-provider authority** already exists as `OFFICIAL_PROVIDER_BY_LEAGUE` and
  agrees with the Lane-R `PROVIDER_LEAGUES`; a test pins them together.
* **Strict PIT untouched:** `AsOfReader`, `_feature_cutoff`, `observed_at`,
  `decided_at`, the strict matcher gate and the dataset builder are all unchanged;
  Lane-R tables remain `unsupported` joins.

## 4. Answers to the remaining questions

**§17 wider window.** An unseen provider id must resolve **UNRESOLVED** — no guess,
no alias fallback. Adding it requires: an accepted G5 audit covering it in the
wider corpus, curation re-review of the *new* entry only (existing entries are
re-verified, not re-curated), a new map digest, and a new corpus version.

**§26 attestation update.** Prefer **git revision as the version axis**: the map
file is edited in place, its digest changes, and the corpus version that used the
old digest remains immutable and reproducible from that commit. Coexisting
versioned constants are rejected — they accumulate indefinitely and invite reading
the wrong one. This works only if `code_version` is recorded on the corpus (it is
nullable today; the implementation must populate it).

**§30 completeness.** A map must cover **every official team id referenced by any
eligible target or source event in the corpus** — not the whole world, and not
merely the ids that happen to resolve. Anything referenced and unattested is an
exclusion, never a silent omission.

**§31 exclusion reporting.** Unresolved teams/games must be reported with count,
identities, seasons and reason, and F1-R/F2 must quantify whether exclusions
correlate with era or franchise age. **Unresolved is not harmless missingness** —
it is potential selection bias and must be treated as such.

**§21/§28 live-lane conflict.** If Lane-R attests `P → team A` while live matching
previously accepted `P → team B`, the implementation must **fail closed and require
review**, never silently prefer either. Whether the live lane should consume the
map is deliberately left open; TEAM-A creates no second canonical-team system, so
that stays available.

**§23/§24 detection power.** Game and team bootstrap must claim only "no
contradiction detected at the audit's detection power". In the one-month corpora
**no game id repeated at all**, so the game audit compared nothing. Bootstrap must
not upgrade that.

**§25 policy versions.** Three distinct identifiers are required, because the
guarantees differ: player provider-id bootstrap, `g5-team-attestation-v1`, and a
canonical game-bootstrap policy. One generic string would let a material curation
change pass unversioned.

## 5. Schema verdict (§32)

**V19 SUFFICIENT WITH ADDITIONAL CODE INVARIANTS.**

No migration is required — `static_crosswalk_provenance`,
`static_identity_map_digest`, the f018/f019 triggers and the games official-key
index cover the structure, and RV3's repair is a value-level change to an
unconstrained TEXT column. But the design's plain "no schema change, therefore
sufficient" was too strong: map membership (RV1) and seed versioning (RV5) are
load-bearing and are **not** enforced today. They must be enforced in code, in CI,
and stated as weaker than the DB-enforced G5 bindings.

**v20 is not required and was not designed.**

## 6. Status and sequencing (§34)

| Question | Answer |
|---|---|
| Team architecture ready? | **Yes**, with the RV1–RV6 repairs applied |
| Game bootstrap ready? | **Yes, conditional on RV3** (namespace-qualified provider values) |
| May TEAM-A implementation be separately authorized? | **Yes** |
| May the reader begin? | **No** |

Sequencing is unchanged and must not be short-circuited:

1. implement the TEAM-A map plus team/game crosswalk support, including the RV1
   membership enforcement, the RV3 qualified namespace and the RV5 seed digest;
2. **independently review that implementation**;
3. only then implement `RetrospectiveResearchReader`.

## 7. Validation

`git diff --check` clean · ruff clean · mypy clean · full suite green · 38
architecture tests (23 existing, repaired; 15 new review reproducers) ·
**42/42 protected artefacts byte-identical** · 0 network trips · schema unchanged
at **v19**, 19 migrations · docs and tests only, **zero production code changed**.

**F1-R, F2, production matching and model training remain unauthorized.**
**G1, G2, G3, G4 and G6 remain open exactly as previously scoped.**
