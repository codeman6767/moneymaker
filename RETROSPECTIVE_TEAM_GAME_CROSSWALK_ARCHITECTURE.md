# Retrospective team/game crosswalk architecture (design only)

Resolves the blocker retained by
`RETROSPECTIVE_IDENTITY_AUDIT_ENGINE_INDEPENDENT_REVIEW.md` §5.

**Verdict: ARCHITECTURE READY FOR INDEPENDENT REVIEW.**
**Chosen: TEAM-A — source-controlled static attestation to the existing canonical
seed.** No schema change; **schema stays v19**.

Design only. No team crosswalks, no game crosswalks, no
`RetrospectiveResearchReader`, no schema v20, no F1-R, no F2, no production
matching, no model training, **no provider API request**, no mutation of
protected evidence. The architecture decision itself **still requires independent
review** before anything is implemented.

---

## 1. The blocker as stated, and what measurement changed

The review recorded: canonical teams are pre-seeded from names under
`UNIQUE (league_id, canonical_name)` and `UNIQUE (league_id, abbreviation)`; the
seeds carry no official provider id; so a provider-keyed franchise cannot be
bootstrapped and reusing a seed appeared to require name matching, which G5
forbids as historical identity evidence. Games inherit it through NOT NULL team
FKs.

Every one of those facts is confirmed. What the review did not separate — and
what this document turns on — is **when** the labels are consulted. That is §5,
and it is the whole decision.

## 2. What a canonical team currently means (§2)

**It is a franchise dimension with historical continuity encoded**, not a
season-team and not a current-brand row. Measured from the seeds:

| Canonical | Historical aliases carried |
|---|---|
| `tm_mlb_wsh` Washington Nationals | **Montreal Expos** |
| `tm_mlb_cle` Cleveland Guardians | Cleveland Indians |
| `tm_mlb_mia` Miami Marlins | Florida Marlins |
| `tm_mlb_ath` Athletics | Oakland Athletics, Oakland A's, A's |
| `tm_mlb_laa` Los Angeles Angels | Anaheim Angels, LA Angels |
| `tm_nba_okc` Oklahoma City Thunder | **Seattle SuperSonics** |
| `tm_nba_bkn` Brooklyn Nets | New Jersey Nets |
| `tm_nba_mem` Memphis Grizzlies | Vancouver Grizzlies |
| `tm_nba_was` Washington Wizards | Washington Bullets |
| `tm_nba_cha` Charlotte Hornets | Charlotte Bobcats |
| `tm_nba_nop` New Orleans Pelicans | New Orleans Hornets |

13 MLB and 13 NBA seeds carry historical aliases. Relocation and rename are
treated as **the same franchise**, which is exactly the semantics G5's team rule
assumes ("a team id is franchise identity; rename, relocation and rebrand are
lawful").

**The Hornets/Pelicans case is already correct** and is the hardest one: the seed
keeps `tm_nba_cha` (Charlotte Hornets ← Bobcats) and `tm_nba_nop` (Pelicans ←
New Orleans Hornets) as **two** franchises, matching the league's own disposition
of the name and history in 2014. The seed does not need rewriting to make ids
convenient, and this architecture does not rewrite it.

## 3. Canonical team id determinism (§3)

`team_id(league_code, abbreviation) -> f"tm_{league}_{abbrev}"` — for example
`tm_mlb_nyy`. Therefore:

* **deterministic** across rebuilds, and **source-controlled**;
* **derived from the abbreviation**, so changing a seed's abbreviation would
  change the canonical id and orphan every FK pointing at it;
* 13 tables hold an FK to `teams`, and existing corpora already store these ids.

Consequence for this decision: **the canonical id is effectively immutable
infrastructure.** Any option that renames or re-keys it pays a very large
compatibility bill, which is measured in §6.

## 4. Provider team evidence (§4)

Read-only from the protected corpora, opened immutable. Per official team id the
corpora carry: provider team id, provider-written full name, normalized name,
abbreviation, city, nickname, league, and repeated observations.

| League | Official ids observed | Identity observations |
|---|---|---|
| MLB (`mlb_statsapi`) | **30** | 1,630 |
| NBA (`balldontlie`) | **30** | 6,474 |

Every id was observed many times, and abbreviation/city/nickname are supplied on
at least some observations for all 60.

## 5. Runtime matching vs curated attestation — the core distinction (§5, §7)

These are **different evidence classes**, and the difference is not convenience.

**Runtime fuzzy matching (forbidden).** At retrospective build time, for each
historical row, search names and pick the closest canonical seed. It is forbidden
because the answer is *computed from a label at the moment of use*: it can change
when the alias table changes, it is per-row, it is unreviewable in aggregate, and
it answers *"which team was this row probably about?"* — a probabilistic question
whose answer depends on when you asked it.

**Curated static attestation (proposed).** Once, offline, a human establishes and
commits a source-controlled constant: *official provider franchise id `147`
denotes canonical franchise `tm_mlb_hou`.* At build time the reader performs an
**exact dictionary lookup**. No name is consulted at read time — the mapping is
already a fact of the source tree.

The distinction is rigorous on five axes:

| | runtime matching | curated attestation |
|---|---|---|
| when labels are read | at every historical row | once, before commit |
| what is asked | "which team was this row about?" | "which franchise does this **franchise id** denote?" |
| reviewability | per-row, in aggregate never | a 60-line diff |
| stability | changes when aliases change | frozen; a change is a new digest |
| dependence on cutoff | none in principle, but recomputed each build | none — resolved before any corpus exists |

The second row is the substantive one. Attestation is a claim about a **provider's
franchise identifier**, an entity that exists independently of any historical row
and of any cutoff. It is precisely the kind of timeless fact G5's STATIC_IDENTITY
class was defined for. A per-row name match is a claim about a *row*.

**This is accepted as STATIC_IDENTITY attestation** — but only under the curation
evidence rules in §7, not merely because it is convenient.

## 6. Options considered, and why the others are rejected

### TEAM-B — provider-key canonical team redesign: **rejected, invasive**

Measured cost: **13 tables carry an FK to `teams`** (`games`,
`game_schedule_snapshots`, `kalshi_markets`, `lineup_snapshots`,
`injury_snapshots`, `provider_team_references`, `team_aliases`, and five
statistics tables). Every deterministic `tm_mlb_nyy` id in every stored corpus,
every matching path, and the live/strict lane would move at once, for a benefit
that TEAM-A obtains with no migration. Rejected as unnecessarily invasive.

### TEAM-C — reconstruction-specific team dimension: **rejected, duplicative**

Creates a second franchise concept. Lane-R features would join one team
dimension and Lane-L features another, so a model trained retrospectively and
served live would be reasoning over two different notions of "team" — the exact
class of silent inconsistency this project keeps refusing elsewhere. It also
duplicates `team_aliases` history and forces every downstream join to know which
dimension it is in. More correctness risk than it removes.

### TEAM-D2 — an ordinary accepted matching decision: **rejected on evidence**

`entity_match_decisions` is registered in the PIT safe-join registry as
`asof_filtered` with `observed_at = decided_at`, i.e. **strict PIT gates it on
`decided_at <= cutoff`**, and `decided_at` is matcher wall-clock. A binding
curated in 2026 would therefore be invisible at every 2021–2025 cutoff — which
**recreates the original historical-identity-time blocker exactly**. Confirmed by
reading the registry, not assumed.

A *distinct static-curation decision class* could dodge the gate, but it would
duplicate provenance concepts that `static_crosswalk_provenance` already models,
so it is rejected in favour of TEAM-A rather than pursued.

### TEAM-A — source-controlled attestation to the existing seed: **chosen**

Closes the blocker with **no schema change**, **no change to the canonical team
dimension**, no second identity concept, and no dependence on a wall-clock
decision time. It is the narrowest option that is also correct.

## 7. Curation evidence contract (§7, §8)

An attestation entry may be committed only when **all** of these hold. This is
the rule that stops manual editing from being fuzzy matching with extra steps.

1. The provider team id **appears in the audited source corpus** and its namespace
   carries an **ACCEPTED** corpus-scoped G5 identity audit for that exact corpus.
2. The provider-written **full name**, normalized by the shared normalizer,
   matches **exactly** one canonical seed alias. Exact normalized equality only —
   no similarity, no scoring, no ranking, no nearest-match.
3. **At least one second independent attribute** (abbreviation or nickname),
   likewise exact, resolves to the **same** canonical franchise.
4. The resolution is **unique**: exactly one canonical franchise, and no other
   provider id in that namespace claims it.
5. The entry is **committed to source control** and reviewed as a diff.
6. No entry may use a game outcome, a roster, a player, a statistic, a target
   game, or any cutoff-dependent fact.

The curation aid that proposes the map is a **one-time offline diagnostic**
(§9 below). Its output is reviewed by a human and frozen; it is never consulted at
retrospective build time. Re-running it is a *verification* step — it must
reproduce the committed map exactly — not a resolution step.

**Verification process for the implementation phase:** regenerate the map with the
diagnostic, diff it against the committed constant, require byte equality, and
require the two-attribute corroboration to hold for every entry. A disagreement
fails closed and blocks the build; it is never auto-resolved.

## 8. Provider-id multiplicity (§10)

**`many official provider ids → one canonical franchise` is valid and must be
supported.** A provider may issue a new team id after a franchise transition, or
carry one id through a rename. The architecture therefore requires:

* each provider id carries **its own** attestation entry and its own audited
  presence;
* **no provider id may point to two canonical franchises** (enforced by the
  existing `UNIQUE (corpus_version_id, league, provider, generation, entity_type,
  provider_id)` on `static_crosswalk_provenance`);
* the many→one direction is explicit in the map and is reviewable.

1:1 is **not** assumed globally. In the two corpora measured here it happens to be
1:1 (30↔30), but that is an observation about this evidence, not a rule.

## 9. Diagnostic result — the architecture survives falsification (§22, §23)

Run read-only, offline, in memory. Nothing was persisted.

### Team attestation

| | MLB | NBA |
|---|---|---|
| official team ids observed | 30 | 30 |
| canonical seeds | 30 | 30 |
| **uniquely attested** | **30** | **30** |
| ambiguous | 0 | 0 |
| unresolved | 0 | 0 |
| one canonical claimed by >1 provider id | 0 | 0 |
| canonical seeds left unmapped | 0 | 0 |
| **corroborated by ≥1 second independent attribute** | **30/30** | **30/30** |

**60/60 franchises are unambiguously curatable, every one corroborated.** No entry
required a judgement call, so §7's rules are satisfiable in practice and not just
in principle.

### Historical alias stress test (§23)

33 historical aliases from the seeds (Montreal Expos, Seattle SuperSonics,
Vancouver Grizzlies, Charlotte Bobcats, New Orleans Hornets, Cleveland Indians,
Florida Marlins, Washington Bullets, New Jersey Nets, …) tested by exact
normalized equality:

* **0 resolve to more than one franchise**;
* **0 resolve to the wrong or an extra franchise**.

A historical provider-written label therefore cannot create a second canonical
team, and the Hornets/Pelicans pair does not cross-contaminate.

## 10. Game bootstrap design (§17, §18, §19)

**The `games` table is already designed for this.** It carries
`official_provider` and `official_game_key` under

```sql
CREATE UNIQUE INDEX idx_games_official_key
    ON games (official_provider, official_game_key)
    WHERE official_provider IS NOT NULL
```

so a canonical game keyed on the official provider game id is a **native**
concept, not a bolt-on. No schema change.

**Identity** (may bootstrap a canonical game): league, official provider,
namespace generation, official provider game id, and the canonical home/away
teams resolved through their attestations.

**Descriptive / mutable** (never identity): scheduled start, `original_start`,
venue, status, reschedule information, and `game_number` except where §11 of the
audit policy uses it to *detect* two events on one day. A reschedule updates
descriptive fields and **cannot create a second canonical game**, because
`official_game_key` is unique per provider.

**No final score, winner or outcome participates in identity**, consistent with
the audit policy.

**Canonical game id.** Either a deterministic hash of the official key (as
`canonical_player_id` does) or a ULID with the official key carried in
`official_provider`/`official_game_key`. The unique index makes both convergent;
the deterministic hash is preferred for the same reason it was preferred for
persons — a rebuilt corpus reproduces every id, so a corpus diff is meaningful.

**Convergence and fail-closed behaviour (§19):**

| Situation | Required behaviour |
|---|---|
| no existing canonical game | bootstrap one |
| existing canonical game already carrying this official key | **reuse it**, write no duplicate |
| existing canonical game created by the conventional matcher for the same official key | **reuse it** — the unique index guarantees there is at most one |
| a *different* canonical game claims the official key | **fail closed**; never pick a winner |
| replay | idempotent, by the same unique index |
| a G5 collision on the game namespace | the whole entity-type namespace is rejected under the current namespace-atomic policy, so **nothing bootstraps** |

### Game readiness diagnostic (§24)

Because team attestation came back 100% unambiguous, the game diagnostic was run:

| | official game ids | both teams attested **and** required metadata present | blocked on a team | blocked on metadata |
|---|---|---|---|---|
| MLB June 2026 | 400 | **400** | 0 | 0 |
| NBA March 2026 | 239 | **239** | 0 | 0 |

**639/639 games are ready.** No blockers.

## 11. Multi-provider authority (§20, §21)

The rule already exists in the repository and does not need inventing:
`OFFICIAL_PROVIDER_BY_LEAGUE` in `sports_quant/matching/service.py`, documented as
*"Only these providers may bootstrap a canonical player … a sportsbook, Kalshi, an
offline import, a manually-supplied string or an unknown provider never can."*

Extended verbatim to teams and games:

* **Only the designated official provider may attest a canonical team or bootstrap
  a canonical game** — `mlb_statsapi` for MLB, `balldontlie` for NBA.
* **Secondary providers must match/link to the existing canonical entity** and may
  never create a competing one.
* This is already consistent with `PROVIDER_LEAGUES` in the retrospective source
  adapters, which the identity-audit review added; the two lists must be kept in
  agreement by test.

Without this rule, per-provider bootstrap would defeat canonicalization by minting
one canonical game per provider for a single real event.

## 12. Provenance contract (§15) — no schema change

Everything required is already modelled at v19.

| Required field | Where it lives at v19 |
|---|---|
| league, provider, namespace generation, entity type, provider id | `static_crosswalk_provenance` columns |
| canonical team id | `static_crosswalk_provenance.canonical_entity_id` |
| accepted G5 audit + its digest | `identity_audit_id`, `identity_audit_digest` (trigger-enforced ACCEPTED, same namespace, **same source corpus**) |
| attestation policy version | `provenance_policy_version` (e.g. `g5-team-attestation-v1`) |
| curation evidence digest | folded into `semantic_digest`, which already covers the full key + audit digest + policy |
| **source-controlled mapping digest** | **`reconstruction_corpus_versions.static_identity_map_digest`** — an existing nullable column named for exactly this and currently unused |
| curated/reviewed timestamp | `curated_at` (audit wall-clock; never backdated, never a reused `decided_at`) |
| corpus binding | `corpus_version_id` + the f019 cross-corpus trigger |

The f018 trigger `trg_xwk_team_target_valid` already requires a team crosswalk to
bind an existing `teams` row **in the same league**, which is precisely the
integrity TEAM-A needs.

**Schema impact: none. Stays v19, 19 migrations.** What is required is a
source-controlled attestation constant plus the existing v19 provenance — the
outcome §25 says to prefer.

## 13. Reader resolution contract (§16)

To resolve a historical provider team id, the future reader must have **all** of:

1. the exact provider key `(league, provider, namespace_generation, entity_type,
   provider_id)`;
2. an **ACCEPTED** identity audit for that namespace over **this corpus's exact
   `source_corpus_digest`**;
3. an accepted **static attestation** for that key, whose map digest matches the
   corpus version's `static_identity_map_digest`;
4. the canonical team row.

**No runtime name lookup. No fallback. No nearest match.** If any layer is
missing, the result is **unresolved** and the input is excluded — never guessed.

## 14. Strict PIT and Lane-L compatibility (§26, §27)

**Unchanged:** `AsOfReader`, `_feature_cutoff`, `observed_at`, `decided_at`, the
strict matching-decision gate and the strict dataset builder. The attestation path
is Lane-R-specific and reaches Lane L nowhere: the Lane-R tables remain
`unsupported` joins, and no feature reader may consult a fuzzy team matcher.

**On whether attestation is also safe as general infrastructure (§27):** it is
*compatible* with the live lane — it binds to the very same canonical franchises
the live path already uses, which is the main reason TEAM-A beats TEAM-C. But this
document proposes it as **retrospective-only** for now. Letting the live matcher
consult it as well is a separate, defensible follow-up; it must not be assumed
here, because the live path's failure modes (a genuinely new franchise, an
expansion team, a provider adding an id mid-season) have not been analysed.
Crucially, TEAM-A creates **no second canonical-team system**, so that follow-up
stays open rather than being foreclosed.

## 15. Security and auditability (§28)

The map is a source-controlled Python constant: diffable, deterministic, free of
secrets, with no provider raw bodies and no URLs. A change to any entry changes
the map digest, which changes `static_identity_map_digest`, which changes the
reconstruction corpus version's semantic digest — so a remap **produces a new
corpus version and can never silently reinterpret an old one**.

## 16. Remaining limitations

* The map is curated against **two one-month corpora**. Auditing a wider window may
  surface provider ids not present here (expansion, a new id after a franchise
  transition); each needs its own attested entry, and an unattested id must resolve
  **unresolved**, not guessed.
* Attestation inherits the audit's detection power. A namespace whose G5 audit is
  ACCEPTED only because nothing was comparable confers no extra confidence on the
  attestation, which is a claim about franchise denotation rather than about
  provider-side non-reuse.
* Whether the live/strict lane should also consult the attestation map is
  deliberately **left open** (§14).
* The Hornets/Pelicans and Athletics cases are correct in the seed today; a future
  seed edit that merged or split a franchise would change canonical ids and must be
  treated as a corpus-versioning event, not a routine edit.

## 17. Status

**ARCHITECTURE READY FOR INDEPENDENT REVIEW.** Nothing here is implemented: there
are no team crosswalks, no game crosswalks, no reader, and no schema change. The
identity-audit engine remains as reviewed at `922b24c`; **team and game crosswalk
generation remain BLOCKED in code** until this architecture is independently
reviewed and separately authorized.

**F1-R, F2, production matching and model training remain unauthorized.**
**G1, G2, G3, G4 and G6 remain open exactly as previously scoped.**
