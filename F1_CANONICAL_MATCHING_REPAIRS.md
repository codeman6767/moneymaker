# F1 canonical-matching repairs

Offline repair of the three known matcher defects that blocked F1 matching
acceptance. Zero provider requests. Production matching was **not** run over the
F1 MLB June or NBA March corpora, and no execution, recovery, merged, checkpoint
or evidence database was modified.

**These repairs are not independently reviewed.** Identity coverage has not been
measured.

---

## 1. Zero-network boundary

23 process-level guards were installed before importing any matching or
provider-facing module: DNS (`getaddrinfo`, `gethostbyname`, `gethostbyname_ex`),
`socket.create_connection`, non-loopback `socket.connect`/`connect_ex`, sync and
async httpx transports and `Client.send`/`AsyncClient.send`, `httpx.get`/`request`,
`requests.request`/`Session.send`, `urllib.request.urlopen`/`OpenerDirector.open`,
`build_readonly_client`, `BalldontlieClient.__init__`, `MlbStatsApiClient.__init__`,
`config.load_settings`, `f1a._default_client_factory`, and
`time.sleep`/`asyncio.sleep`.

**14/14 adversarial probes blocked**; `cli.load_settings is config.load_settings`
is `True`. No API key was read, no provider client constructed, no audit run, no
real sleep. All fixtures are synthetic databases built by `initialize_database`.

Protected artefacts, byte-identical before and after:

```
39064fa219f4eb66f37e26f567a8f42d7df278236aa70e42264a600b07a5d135  NBA March corpus
c17a375daa89e3f0f8ace2e6e3dffd965f8428b178127cb9b7c44bde6471300b  NBA March checkpoint
e2fea1c06c43400323b0266aeb8ba34db28e9b6ead13504413eb93ed4de6e1db  lineup recovery db
223c1185a0c7c1afb35e04ae8e31e50de4f7e62e69073808eb08cf75cafad02a  merged copy
802a7d76e42d08dc60894329c490ca92d4f98e95197f2eaac967d3065af8b6f2  MLB June corpus
70bbc7c907cd6038eb57edd744111d7a187567fd948ff3d473a0192aaf91569e  MLB June checkpoint
```

## 2. Defect 1 — official same-name matching depended on traversal order

**Reproduction.** Two `mlb_statsapi` player references, different stable ids
(`1001`, `1002`), same provider-written name ("Will Smith"), both unlinked. After
`match-players`, only **one** id was linked:

```
linked_ids == ['1001']   expected ['1001', '1002']
```

Reversing insertion order reversed which id survived, so the final canonical set
was a function of processing order, not of evidence.

**Root cause.** The first id found no name candidate, bootstrapped a canonical
player and wrote a provider-scoped alias from the name. The second id then
resolved *by that alias* onto the first id's canonical player.
`_claimed_by_another_provider_id` correctly detected that another id of the same
provider already owned it and `_collision` turned the match into an `AMBIGUOUS`
refusal — but the bootstrap gate was `if res.status != AMBIGUOUS`, so the second
id could no longer create its own identity and was left unresolved. The guard
prevented the *worse* bug (two ids collapsing into one person) but produced
"first one wins the name".

**Corrected contract.** For a league's **designated official provider**, one
stable id is one person *by construction*. So two official ids sharing a name are
two people, and each must get its own canonical identity. The bootstrap is now
also reachable when the refusal was a same-provider id collision:

* a direct collision (name matched a canonical player another id of the same
  provider owns);
* an `AMBIGUOUS` result where **every** candidate is already claimed by another id
  of the same provider — the three-or-more-same-name case. Deliberately *all*, not
  *any*: one unclaimed candidate means there is a real canonical player this id
  might legitimately be, and that stays ambiguous.

`_bootstrap_official_player` is unchanged and still refuses unless the provider is
the league's official source, so a nonofficial or unknown provider hitting the
same collision falls through to the ordinary ambiguous refusal and creates
nothing. No birth date, name part, position or career date is invented.

**Why the new rule is order-independent.** The outcome of each id now depends only
on (a) whether it is an official id and (b) whether any *unclaimed* canonical
candidate exists for it. Neither depends on which id ran first, so every
processing order converges on the same semantic state: N official ids sharing a
name become N distinct canonical players, each linked to its own.

**Result.** 25 randomized permutations of reference order, identity-observation
order and processing order over four same-name official ids: all four ids linked,
four distinct canonical players, zero shared identities, in every permutation.
Replay is idempotent (no new players, no new decisions). Dry-run persists nothing.

## 3. Defect 2 — accepted exact-provider team replays grew decision history

**Reproduction.** One matchable MLB game, matched once, then matched again with
identical inputs:

```
team_accepted: 2 -> 4      candidates: 3 -> 5
```

Exactly the +2-per-replay growth recorded as a known residual in
`ENTITY_MATCHING.md` 3.4.5.

**Root cause.** `_resolve_team` called `_record_decision` unconditionally,
including on the `exact_provider_id` path, and `_record_decision` never
deduplicates an `accepted` outcome (correctly — an accepted decision justifies a
link). But an `exact_provider_id` hit *means the reference already carries the
link*, so the run learned nothing and the re-affirmation carried no new fact.

**Corrected contract — narrow semantic replay.** When a team resolves by
`exact_provider_id`, the existing link is classified before anything is recorded:

| State | Condition | Behaviour |
|---|---|---|
| `ABSENT` | no reference, or no canonical id | ordinary first match, recorded |
| `VALID_REPLAY` | reference points at the **same** canonical team **and** its `match_decision_id` is an accepted `team` decision whose `matched_entity_id` is that same team | **nothing written**, counted as `decisions_replayed` |
| `BROKEN` | linked but the provenance fails any part of that check | `DQ-MATCH-017` blocking issue, then the ordinary recording path — never silently repaired |

The rule is deliberately not a global accepted-decision dedupe. It applies only to
this already-linked exact-id path, so a genuinely new observation, changed
identity evidence, a different canonical target or a changed matcher version all
still append and stay auditable. Nothing is ever deleted or rewritten — history
before the repair is preserved verbatim.

**Result.** Second and third replays add zero decisions, zero candidates, zero
canonical entities and zero links; the decision table is byte-identical across
replays; `decisions_replayed` reports the replay instead of it being
misreported as a fresh accepted match. A link whose backing decision is removed
is reported via `DQ-MATCH-017` and is **not** repaired. `team_id` on a provider
reference is immutable once set (`trg_provider_team_ref_identity_immutable`), so a
link cannot be silently re-pointed at another team.

## 4. Defect 3 — accepted canonical mappings reachable downstream

### Schema v17 inventory

Every normalized observation table is **append-only** and carries provider
identifiers; the nullable canonical columns were left NULL by ingestion because
matching had not run.

| Table | Entity | Provider reference cols | Canonical col | Mutability | Prior propagation | Downstream consumer | Corrected behaviour |
|---|---|---|---|---|---|---|---|
| `provider_team_references` | team | `provider`,`provider_team_id` | `team_id`,`match_decision_id` | identity-immutable | authoritative link | matching, resolver | **source of truth** (unchanged) |
| `provider_player_references` | player | `provider`,`provider_player_id` | `player_id`,`match_decision_id` | identity-immutable | authoritative link | matching, resolver | **source of truth** (unchanged) |
| `provider_game_references` | game | `provider`,`provider_game_id` | `game_id`,`match_decision_id` | identity-immutable | authoritative link | matching, resolver | **source of truth** (unchanged) |
| `entity_match_decisions` | — | `source_provider`,`source_ref` | `matched_entity_id` | append-only | decision record | PIT `decisions_for_source` | justifies every link |
| `game_schedule_snapshots` | game | `game_ref_id`,`provider_game_id` | `venue_id` | append-only | none | `AsOfReader`, dataset cutoff | resolve via reference |
| `nba_game_results` | game | `game_ref_id`,`provider_game_id` | — | append-only | none | `AsOfReader.official_result`, labels | resolve via reference |
| `nba_quarter_lines` | game | `game_ref_id`,`provider_game_id` | — | append-only | none | features | resolve via reference |
| `nba_team_statistics` | team | `+ provider_team_id` | `team_id` | append-only | none | features | resolve via reference |
| `team_game_statistics` | team | `+ provider_team_id` | `team_id` | append-only | none | features | resolve via reference |
| `nba_player_statistics` | player | `+ provider_player_id` | `player_id`,`team_id` | append-only | none | features | resolve via reference |
| `player_game_statistics` | player | `+ provider_player_id` | `player_id`,`team_id` | append-only | none | features | resolve via reference |
| `lineup_snapshots` | team | `game_ref_id`,`provider_team_id` | `team_id` | append-only | none | `AsOfReader.lineup` | resolve via reference |
| `lineup_players` | player | `provider_player_id` | `player_id` | append-only | none | lineup features | resolve via reference |
| `roster_snapshots` | player | `team_ref_id`,`provider_player_id` | `player_id` | append-only | none | features | resolve via reference |
| `probable_pitcher_snapshots` | player | `game_ref_id`,`provider_player_id` | `player_id` | append-only | none | `AsOfReader.probable_starter` | resolve via reference |
| `injury_snapshots` | player | `player_ref_id`,`provider_player_id` | `player_id`,`team_id` | append-only | none | `AsOfReader.injury` | resolve via reference |
| `play_snapshots` | game | `game_ref_id`,`provider_game_id` | — | append-only | none | features | resolve via reference |
| `mlb_inning_lines` | game | `game_ref_id`,`provider_game_id` | — | append-only | none | features | resolve via reference |
| `weather_snapshots` | game | `game_ref_id`,`provider_game_id` | `venue_id` | append-only | none | features | resolve via reference |

**Conclusion: no table should be backfilled.** Every observation table carries
`BEFORE UPDATE`/`BEFORE DELETE` guards that `RAISE(ABORT)`; filling a convenience
column would require weakening an audit guard to buy a join shortcut, which the
task forbids and which would also destroy the point-in-time property (a *current*
canonical id in an observation row carries no knowledge time). **No schema change
was made — the repository stays at v17.**

### The chosen architecture: decision-backed resolution

`sports_quant/matching/resolution.py` resolves a provider id to a canonical
identity through the reference **and its own backing accepted decision**:

* the reference must exist and carry a canonical id;
* it must carry a `match_decision_id` — the exact decision that justified the
  link, never an unconstrained "latest decision for this source" lookup that a
  same-timestamp or later unrelated decision could win;
* that decision must exist, be `accepted`, have the matching `entity_type`, and
  have `matched_entity_id` equal to the canonical id the reference points at.

Anything else resolves to `None`: unmatched, ambiguous, rejected, missing
decision, decision for another entity. `resolve_many` is keyed by provider id, so
batch results never depend on input or SQLite row order. The module never writes.

### Point-in-time

`as_of` gates on the decision's `decided_at`. A decision decided after the cutoff
is invisible, so matching a reference today cannot make it resolvable at an
earlier cutoff. Cutoff `< decided_at` → unresolved; cutoff `== decided_at` →
resolved (documented inclusive bound, matching the existing
`decided_at <= cutoff` convention); cutoff `> decided_at` → resolved.

This mirrors — and does not replace — `AsOfReader.matched_entity`, which remains
the only **feature-facing** path because it additionally enforces the manual-review
gate. The PIT registry already marks all three `provider_*_references` tables
`_forbidden`, precisely so the mutable current-state link can never be read as
history; that policy is unchanged. Provider-only ids remain insufficient for
dataset admission: the historical dataset builder still returns **0 rows** for a
corpus with provider references but no canonical games.

## 5. Determinism, atomicity, concurrency, dry-run

* **Determinism** — 25 randomized permutations for defect 1; repeated replays for
  defect 2; order-independent batch resolution for defect 3. No test depends on
  implicit SQLite row order; every tie-break is explicit and evidence-based.
* **Atomicity** — unchanged and still exercised by the existing matching suite:
  bootstrap creates the player, alias, accepted decision and provider link inside
  one transaction, and `classify_link_attempt` still rolls back a non-`LINKED`
  outcome. The repairs add no new write path — defect 2 *removes* a write and
  defect 3 is read-only.
* **Concurrency** — provider-reference identity columns are immutable once set, so
  a second concurrent attempt cannot re-point a link; two official ids cannot come
  to own one canonical identity.
* **Dry-run** — `match-players` and `match-games` dry-runs still perform the same
  resolution and persist nothing: no canonical entity, alias, decision, candidate,
  link or propagation, and no network call. Verified explicitly for both.

## 6. Validation

All offline; no real sleeps.

```
git diff --check                       clean
ruff check .                           All checks passed
mypy . --no-incremental                Success: no issues found in 309 source files
pytest -q                              2329 passed, 2 skipped, 0 failed (506 s)
matching suite                         341 passed
schema init x2                         v17, 17 migration rows, 47 tables, integrity ok (both)
v16 -> v17 migration x2                v16 (0 identity tables) -> v17 (2), 17 rows,
                                       integrity ok, re-apply idempotent (both runs)
non-editable wheel smoke               24/24 checks passed
staged-file / secret audit             9 files, all source/docs/tests; no db, ckpt,
                                       raw response, log, wheel, env or graphify output
protected artefacts                    6/6 byte-identical before and after
```

`sports_quant/matching/tests/test_canonical_matching_repairs.py` adds **48 tests**:
same-name determinism (including 25 randomized permutations), nonofficial
conservatism, bootstrap replay idempotency and dry-run; accepted-team replay
counts, counter reporting, history preservation, broken-link fail-closed,
no-silent-repair, repeated-run determinism and match-games dry-run; and
resolution before/after an accepted mapping, unknown reference, bad kind, missing
backing decision, decision for another entity, ambiguous reference, the three PIT
cutoffs, read-only replay, order-independent batch resolution, provider-only
dataset admission and the append-only guard inventory.

`test_official_identity_bootstrap.py::test_exact_provider_id_replay_records_no_new_decision`
replaces an assertion that pinned the defect: it previously required the two
accepted re-affirmations to be appended.

The wheel smoke ran the **installed** package (fresh venv `site-packages`, CWD
outside the repository) and covered both matching CLIs and their `--dry-run`
flags, a three-way same-name official collision, replay idempotency, downstream
canonical resolution, all three PIT cutoffs, dry-run persistence, the zero-network
sentinel (14/14 blocked) and schema v17.

## 7. Limitations and status

* **The three known matcher defects have been repaired but are NOT independently
  reviewed.**
* **Production matching over the F1 month corpora has not run.** The real
  protected corpora still report their existing unmatched state, by design.
* **Identity coverage has not been measured.** No coverage figure is claimed.
* **NBA PIT labels remain 0/239** in the unchanged protected corpus.
* **Historical March NBA lineups remain unavailable at March pregame cutoffs** —
  the August-backfill limitation from the merge review is untouched and was not
  weakened to increase coverage.
* **The combined F1 review has not begun. F1 remains incomplete. F2 remains
  unauthorized.**
