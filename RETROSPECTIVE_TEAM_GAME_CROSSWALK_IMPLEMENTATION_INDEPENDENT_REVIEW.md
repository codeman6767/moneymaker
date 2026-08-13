# TEAM-A team/game crosswalk implementation — independent review

Reviews the implementation committed at `982b73b` against the architecture
independently reviewed at `c0dfcd0`. Every claim in
`RETROSPECTIVE_TEAM_GAME_CROSSWALK_IMPLEMENTATION.md` was treated as unproven and
re-derived; the review harness
(`sports_quant/db/tests/test_team_a_implementation_review.py`) is independent of
the implementer's fixtures.

> **VERDICT: ACCEPTED WITH REPAIRS.**
> Seven defects were proven with failing reproducers written first, and all seven
> are repaired. Two were serious: a canonical game could be created with **no
> persisted G5 audit at all**, and the dry run predicted the opposite of what
> apply did for both new entity types. **Schema stays v19** — 19 migrations, no
> migration added or edited.

Where this document and the implementation report differ, **this document is
authoritative**. Superseded statements in that report are labelled there rather
than deleted.

---

## 1. Scope, isolation, and evidence integrity

| Control | Result |
|---|---|
| Zero-network sentinel | 23 process guards installed **before** any retrospective/provider import |
| Adversarial probes | **12/12 blocked** — DNS ×2, `create_connection`, raw socket, urllib, httpx ×2, requests, both provider clients, `config.load_settings`, `build_readonly_client` |
| Provider requests made | **0** (`zeronet.TRIPPED == []` across every real-evidence run) |
| Protected artefacts | **42/42 byte-identical**; every database `mtime_ns`, inode and WAL sidecar unchanged. **One `-shm` sidecar mtime moved** — see below |
| Source corpora | opened read-only through the accepted `immutable=1` path only |

No reader was implemented. No Odds API code, no market anchoring, no F1-R, no F2,
no production matching, no model training, no feature engineering.

### The one `-shm` movement, traced

`data/f1_nba_2026_03_scratch.db-shm` changed mtime during the review (size,
database bytes, database mtime and `-wal` all unchanged). Traced to a
**pre-existing** path, not to anything in TEAM-A or this review:

* `sports_quant/ingest/scratch_db.py::_ro_connect` opens with `mode=ro`;
* on a WAL-mode database that builds the shared-memory index, which moves `-shm`;
* the full test suite exercises it via
  `test_committed_recovery_manifest_regenerates_byte_identically`, which
  regenerates the committed recovery manifest from that corpus.

Confirmed by direct measurement: calling `classify_scratch_db` moves `-shm` while
leaving the database bytes identical, whereas `open_source_corpus` (`immutable=1`)
moves nothing. Every TEAM-A read goes through the immutable path.

The `mode=ro` choice there is deliberate — it exists so committed WAL content is
included in the digest — so it was **not** changed. What was wrong was the inline
comment claiming `mode=ro` "never writes a `-wal`/`-shm` sidecar"; that is
factually false and is now corrected in place, with a pointer to the immutable
reader for callers that must leave an evidence directory untouched.

## 2. Defects found and repaired

Each was reproduced first, against a real disposable v19 schema.

### D1 — dry-run/apply parity was broken for BOTH new entity types *(serious)*

`run_identity_audit` routed every dry run through `_dry_run_crosswalks`, which
called the generic provider-key `generate_crosswalks`. That module explicitly
returns `supported=False` for teams and games. So for identical accepted
evidence:

| | dry run (before) | apply |
|---|---|---|
| TEAM | `supported=False`, 0 written, no TEAM-A plan | 3 attested, 3 written |
| GAME | `supported=False`, 0 ready | 2 ready, 2 created |

A dry run existing to predict an apply predicted its opposite.

**Repair.** One `_execute` body now serves both modes: the dry run performs the
*identical* work against the *real* output database inside a transaction that is
always rolled back. This also fixes a second limitation — the old scratch
database always assumed an empty target, so it could never predict reuse. A dry
run against a prepared database now correctly reports `written=0, reused=3` and
`created=0, reused=1`. Verified to write nothing: table counts are byte-equal
before and after.

### D2 — a canonical game could be created with no persisted audit *(serious)*

`write_game_bootstrap` took no `identity_audit_id`. It checked `plan.accepted` on
an in-memory dataclass. A `replace(plan)` object — never persisted, cleared by
nothing — minted canonical games. Team crosswalks were already held to the
persisted standard by schema triggers; games escaped because they cited no audit
at all.

**Repair — GAME-PROV-C (both options).** `write_game_bootstrap` now requires
`identity_audit_id` and validates, before any write, that it names a real
persisted **ACCEPTED** audit for the exact league, provider, generation,
`entity_type='game'`, **and** the same source corpus digest.

### D3 — canonical games carried no corpus- or audit-scoped provenance

A future reader must be able to prove *provider game G in corpus version C
resolves to canonical game X under accepted audit A*.
`games.official_provider`/`official_game_key` is **global**: it names no corpus,
no audit, and no policy version.

**Decision (§5): use v19's existing `static_crosswalk_provenance`.** It already
permits `entity_type='game'` (`xwk_entity_type` CHECK) and already validates the
target (`trg_xwk_game_target_valid`). **No v20 is required.** The bootstrap now
writes one game crosswalk per ready game, inside the caller's transaction, under
`g5-game-bootstrap-v1`.

### D4 — no convergence with conventionally matched games

The conventional matcher writes `official_provider='mlb_statsapi'`; TEAM-A writes
`'mlb_statsapi:mlb:v1'`. `UNIQUE (official_provider, official_game_key)` treats
those as unrelated pairs.

Observed behaviour before repair: TEAM-A attempted a **second canonical row for
the same real game** and was stopped only *incidentally*, by the unrelated
natural-key index on `(league, date, home, away, game_number)`. Had a reschedule
moved the local date, the duplicate would have been created.

**Repair (§7/§8).** `legacy_equivalent_providers()` states the equivalence
explicitly and narrowly: only the bare provider *of that exact qualified value*,
only for the league it is the designated official provider for. It is a **read**
equivalence for convergence — no historical row is rewritten, and new Lane-R
games are always written qualified. Two rows under equivalent providers naming
*different* games fail closed rather than picking a winner. Arbitrary provider
strings are never equated.

### D5 — an existing game with a contradictory season was silently reused

The replay check compared teams and the deterministic id, but not league or
season. A canonical row with the right key and right teams but the **wrong
season** was counted as a valid replay.

**Repair (§9).** League and season are now identity contradictions and block.
Descriptive fields (scheduled start, status, venue, reschedule metadata) remain
non-identity, as designed.

### D6 — the verifier never recomputed the crosswalk semantic digest

RV1 folded the attestation map digest into `semantic_digest`, and the report
claimed the crosswalk was "cryptographically bound to the map". Nothing
recomputed it. A row with correct corpus, correct accepted audit, correct
mapping, correct policy and a **tampered digest passed verification**. The
binding was decorative.

**Repair (§14/§15).** The verifier now independently recomputes the expected
digest from corpus version, full namespace, provider id, canonical target,
identity-audit digest, policy version and the map digest. It distinguishes two
failures: a **tampered** digest, and a row carrying the **pre-TEAM-A
non-map-backed** digest (internally consistent but not map-backed). Player
crosswalk digests are untouched and remain byte-identical.

### D7 — live-reference conflicts were not decision-backed

`_live_conflict` read `provider_team_references.team_id` directly. The
already-reviewed canonical matcher establishes a stronger contract
(`matching.service._existing_team_link_state`): a link is authoritative only when
its own `match_decision_id` names an accepted **team** decision that adjudicated
*this* provider and *this* provider team id and matched *that same* canonical
team.

Consequence: corrupt links were believed. The implementer's own test
`test_an_agreeing_live_mapping_is_not_a_conflict` seeded a reference with **no
decision at all** and asserted it counted as agreement.

**Repair (§11).** A `LiveLink` classifier mirrors the reviewed contract, and the
plan reports `broken_live_links` separately from `conflicts`. Behaviour matrix,
all ten cases tested:

| Live reference | Result |
|---|---|
| valid, agrees | attested |
| valid, disagrees | **conflict** (blocks) |
| no `match_decision_id` | **broken** (blocks) |
| decision id present, decision row missing | **broken** |
| decision rejected | **broken** |
| decision `entity_type='player'` | **broken** |
| decision matched a different team | **broken** |
| decision for another provider team id | **broken** |
| decision for another provider | **broken** |
| absent | attested (ordinary TEAM-A path) |

A broken link is never reported as an identity conflict: that would misattribute
matcher corruption to TEAM-A.

## 3. Claims re-derived and CONFIRMED

These needed no repair.

* **Committed map** — 60 entries, 30 MLB / 30 NBA, `team-a-map-v1`,
  `g5-team-attestation-v1`.
* **Exact lookup only** — unknown id, wrong league, wrong generation and
  arbitrary provider all resolve to `None`. No executable name/alias/abbreviation
  /nickname fallback (docstring-stripped source scan).
* **T1 many→one** — one provider key meaning two franchises is refused; two
  provider ids denoting one franchise is **permitted**. Canonical-target
  injectivity is correctly *not* required.
* **Seed digest** — reconstructed from first principles without calling any
  production helper; **matches exactly**
  (`19d7e98d239a582c8968fbe819fb6926b60f8a568ef56a71e134e7e74d6a7fcc`).
* **Map digest** — `ae21c26b…d290d9`; row-order and dict-order independent; moves
  on a map-entry change, a policy-version change, a format-version change, and a
  seed-digest change.
* **Canonical game id** — deterministic, binds league + qualified provider +
  official key, and excludes score, winner, status, start time and venue. The
  same numeric key coexists across two legitimate namespaces; identical
  namespace + key is unique.
* **RV3 qualified providers** — wrong league, wrong generation and arbitrary
  strings all fail closed.
* **Atomicity** — failures injected before the game insert, after it, and during
  provenance all roll back completely: **no orphan canonical game**, no partial
  corpus. A mid-crosswalk team failure leaves zero rows in all three provenance
  tables.
* **Convergence/idempotency** — scientifically identical re-runs land on the same
  corpus version and reuse; a changed `target_set_digest` creates a new corpus
  and does **not** inherit another corpus's crosswalks.
* **Ordering** — GAME before TEAM reports `unattested_team_ids` and creates
  nothing; TEAM then GAME succeeds; both replay as reuse.
* **Strict PIT** — `_feature_cutoff` source hash unchanged
  (`5d55345b6e2d8836df83428de82462df`); all five Lane-R tables still
  `unsupported`; no `pit/` or `quality/` module references TEAM-A.

## 4. Contracts corrected, not defects

### Completeness semantics (§17)

`require_complete` proved a **different property** than the reviewed contract. It
checked that every committed map entry for the league had a stored crosswalk.
The reviewed contract is that every official team id **the corpus actually
references** is covered.

They diverge in both directions: a legitimate two-team corpus was reported
incomplete, and a corpus referencing a historical id absent from the committed
map was reported **complete** — structurally unable to surface the selection
bias.

Split into two explicit checks:

* `--require-full-league-map` — the old property, honestly named.
* `--source-db` → `referenced_provider_team_ids()` — the reviewed contract,
  reading the source corpus read-only. A referenced id missing from the map is
  reported as *"NOT in the committed map at all — this is a selection-bias
  exclusion"*.

`VerificationReport.referenced_checked` is `0` when the reviewed contract was not
evaluated, so a report cannot imply a check it did not run.

### Code-version semantics (§16)

Adjudicated: **the map digest and seed digest are the semantic authority;
`code_version` is a locator, not a proof.** A non-empty string is not a truthful
revision, and the runner cannot verify one — an installed wheel outside a
repository has no git metadata, and inventing an identity there would be worse
than recording `unknown-revision`. The corpus requires the field to be present
(enforced) and records what it can; reproducibility rests on the digests.

### "A reschedule updates description" (§10)

**The documentation was wrong.** Replay reuses the existing canonical game and
writes no descriptive update. That is the correct behaviour — canonical
descriptive metadata stays immutable after first bootstrap, and historical typed
observations remain the only retrospective feature evidence — but the prose
claimed otherwise. Corrected rather than implementing mutable hindsight updates
to satisfy it.

### Evidence-strength wording (§23/§24)

Audited and found **accurate**. The game module states a clean audit means "no
contradiction detected", not "game ids verified stable", and does not upgrade the
one-month detection power. Nothing claims teams are "verified immutable",
"proven permanent" or "guaranteed stable"; TEAM-A curates *denotation* only.

### Minor, not repaired

`namespaces.qualified_provider_for` raises `SourceCorpusError` for a namespace
registration problem where `AttestationError` would read better. Both derive from
`RetrospectiveProvenanceError` and both fail closed, so this is cosmetic.

## 5. Real one-month reproduction (recomputed, not quoted)

Read-only sources, disposable v19 outputs, sentinel armed, **0 provider
requests**.

| | MLB June 2026 | NBA March 2026 |
|---|---|---|
| Team ids / attested / written | 30 / 30 / 30 | 30 / 30 / 30 |
| Unresolved / conflicts / broken links | 0 / 0 / 0 | 0 / 0 / 0 |
| Qualified provider | `mlb_statsapi:mlb:v1` | `balldontlie:nba:v1` |
| Game ids / ready / created | 400 / 400 / 400 | 239 / 239 / 239 |
| Game provenance rows | 400 | 239 |
| Replay | 0 created, 400 reused | 0 created, 239 reused |
| Dry run matched apply | **yes** (team and game) | **yes** (team and game) |
| Verifier | `checked=30 ok=True` | `checked=30 ok=True` |
| Referenced-id completeness | 30/30 covered | 30/30 covered |

Counts match the implementation report's team/game figures; the provenance rows
and dry-run parity are new. **One month remains NOT proof of 3–5 season
coverage.**

## 6. Schema verdict

**V19 SUFFICIENT WITH ADDITIONAL REPAIRS.** Every repair is expressible in code
plus existing v19 provenance structures. Game identity provenance uses the
`static_crosswalk_provenance` table, `entity_type='game'` CHECK and
`trg_xwk_game_target_valid` trigger that v19 already ships. `f018` and `f019` are
untouched. **No v20.**

## 7. Component verdicts

| Component | Verdict |
|---|---|
| Team attestation map | **ACCEPTED** — exact lookup, T1 correct, digests independently confirmed |
| Team crosswalks | **ACCEPTED WITH REPAIRS** — D7 (decision-backed live links) |
| Game bootstrap | **ACCEPTED WITH REPAIRS** — D2, D4, D5 |
| Game audit provenance | **REPAIRED** — GAME-PROV-C; was absent entirely |
| Conventional/live convergence | **REPAIRED** — explicit, narrow, fails closed on ambiguity |
| Verifier | **ACCEPTED WITH REPAIRS** — D6; the binding claim is now actually checked |
| Dry run | **REPAIRED** — D1; same body as apply, rolled back |
| Reader readiness | **READY TO BE SEPARATELY AUTHORIZED** |

## 8. Reader readiness

The blocker that motivated game provenance is closed: a reader can now answer
*provider game G in corpus C resolves to canonical game X under audit A* from
`static_crosswalk_provenance` alone, for teams **and** games, corpus-scoped and
audit-backed.

Standing limitations the reader must not paper over:

* Map-membership enforcement remains a **detective** control (code + CI). Direct
  SQL can still write a contradicting row; it cannot survive verification.
* Game-id stability is **not** proven — one-month corpora contained no repeated
  game id, so detection power was nil.
* TEAM-A attests **denotation**, not provider-id permanence.
* One month is not season coverage.

**The reader may be separately authorized.** It was not started here.

## 9. Status of other gates

G1, G2, G3, G4 and G6 are **unchanged** by this review. F1-R, F2, production
matching, model training and feature engineering remain **unauthorized**.
