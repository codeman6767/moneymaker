# NBA March 2026 lineup-continuation execution — independent review

Independent, entirely offline review of the single live BALLDONTLIE lineup
continuation executed on 2026-08-06. Every reported value was reconstructed from
preserved local evidence rather than accepted from the execution's own summary.

**Verdict: ACCEPTED.** 40 of 40 games are merge-eligible. One reporting overclaim
and one reporting gap were proved and repaired; neither affects the validity of
the recovered evidence.

Reviewed at `HEAD c841a21`. No provider request was made. No merge was performed.

---

## 1. Zero-network review boundary

23 process-level guards were installed **before** importing the CLI, provider or
continuation modules: DNS (`getaddrinfo`, `gethostbyname`, `gethostbyname_ex`),
`socket.create_connection`, non-loopback `socket.connect`/`connect_ex`, sync and
async httpx transports and `Client.send`/`AsyncClient.send`, `httpx.get`/`request`,
`requests.request`/`Session.send`, `urllib.request.urlopen`/`OpenerDirector.open`,
`build_readonly_client`, `BalldontlieClient.__init__`, `MlbStatsApiClient.__init__`,
`config.load_settings`, `f1a._default_client_factory`, and `time.sleep`/`asyncio.sleep`
on the pacing and retry paths.

14 adversarial probes, all blocked:

```
DNS getaddrinfo(api.balldontlie.io)     socket.create_connection(...:443)
raw socket connect 1.1.1.1:443          httpx.get(https://api.balldontlie.io/v1/games)
httpx.Client.send                       httpx.AsyncClient.send
urllib.request.urlopen                  requests.get
BalldontlieClient(...)                  MlbStatsApiClient(...)
config.load_settings()                  f1a._default_client_factory('nba')
http_policy.build_readonly_client()     time.sleep(2.0) pacing
                                        -> 14/14 blocked
```

`cli.load_settings is config.load_settings` is `True`, proving the CLI bound the
guarded loader. Guard trips during evidence analysis: **0**. The NBA API key was
never loaded — the one review process that exercised the production `--execute`
path used an injected stand-in `settings` object behind an `httpx.MockTransport`,
and a control run with `settings=None` tripped the `config.load_settings` guard,
confirming the real loader is genuinely reachable only through that seam.

## 2. Process and authorization reconstruction

| Property | Reconstructed value |
|---|---|
| Command | `python -m sports_quant nba-lineup-continuation --manifest pilots/f1/nba_lineups_2026_03_continuation.manifest.json --source-db data/f1_nba_2026_03_scratch.db --recovery-db data/f1_nba_lineups_2026_03_recovery.db --checkpoint data/f1_nba_lineups_2026_03_recovery.ckpt --execute --json` |
| Launcher PID → child PID | 25908 → 41556 |
| Start / finish (UTC) | 07:35:39.458 → 07:35:55.023 |
| Wall duration | 15.565 s |
| Exit code | 0 |
| stderr | 0 bytes |
| `--resume` | absent from argv |
| Authorization | `MONEYMAKER_F1B_AUTHORIZED=1`, child environment only |
| Global authorization | absent (`authorization_in_parent_shell: false`) |
| Ungated-ingest variable | absent (`ungated_env_present: false`) |
| Processes remaining | none |

Independent corroboration, not taken from the wrapper's summary JSON: the
checkpoint records `usage_provenance.process_count = 1` with
`process_count_known: true` and exactly one process record; the database holds
40 ingestion runs all stamped with the same command; the earliest
`raw_responses.requested_at` (07:35:43.549) and latest `received_at`
(07:35:54.859) both fall strictly inside the launcher's start/finish window. A
single audit had run earlier against a separate artifact set; no second audit and
no second continuation appear anywhere in the evidence.

The API key was never printed, hashed or persisted. A scan of every text and blob
column of all 47 tables for `authorization`, `x-api-key`, `api_key`, `apikey`,
`bearer`, `token`, `secret` and `goat` returned **0 matches**. Stored response
headers are `content-type`, `date`, `etag` only.

## 3. Execution binding

Re-derived from the committed manifest and the protected source corpus, then
compared against the executed checkpoint and database. All thirteen agree exactly.

| Bound value | Reported | Independently reproduced |
|---|---|---|
| Manifest hash | `a8979cd1…fbac36` | matches (file SHA-256 == `manifest_hash()`) |
| Plan hash | `3c0ec01c…f96dfef7` | matches |
| Source fingerprint | `b5b475a4…428a6d8170` | matches |
| Target count | 40 | 40 |
| Target digest | `03d3df93…d927f737` | matches |
| Selected games | 239 | 239 |
| Already complete | 199 | 199 |
| Max continuation pages | 8 | 8 |
| Semantic maximum | 320 | 320 |
| Retry-inclusive cap | 640 | 640 |
| Configured rate | 60/min | 60/min |
| Tier maximum | 600/min | 600/min |
| Maximum retries | 1 | 1 |

The 40 executed provider game IDs equal the independently derived target set
exactly: no missing target, no extra target, no duplicate. Re-deriving under
three different target orderings produced identical sets, so the derivation is
not traversal-order dependent.

## 4. Request accounting

All accounting identities close.

```
reserved_attempts 40 = transport_starts 40 = responses_received 40
                     = parse_successes 40 = successful_responses 40
attempted_requests 40 = reserved 40 + retry_attempts 0
failed_responses 0   http_429s 0   blocked_requests 0
throttle_events 0    throttle_wait 0.000 s   budget_exhausted None
skipped_on_resume 0  prior_requests 0   prior_transport_starts 0
40 <= semantic max 320 <= retry-inclusive cap 640
```

Cross-layer reconciliation, each computed from its own source: checkpoint
`usage` totals = the single process record in `usage_provenance` = 40; database
`SUM(ingestion_runs.requests_made)` = 40; `COUNT(raw_responses)` = 40;
`report.continuation_requests` = 40. Exactly one recorded logical process.

**Endpoint discipline.** All 40 stored request paths audited: provider
`balldontlie` only, endpoint `/v1/lineups` only, method `GET` only, HTTP 200 only,
`per_page=25` on every request, exactly one provider game ID per request, and a
non-null integer cursor on **every** request — **0 first-page requests**. The 40
requested cursors and 40 game IDs match the report set exactly, one request per
game. No other endpoint, provider or family appears in the database.

## 5. Cursor-chain reconstruction (40 targets)

Each chain was rebuilt from the protected page-one response in the March corpus
and the preserved continuation response in the recovery database.

Invariants, all satisfied:

- requested cursor == page-one `meta.next_cursor`: **40/40**
- HTTP 200: **40/40**
- exactly one continuation page requested per target: **40/40**
- returned `next_cursor` null for every completed chain: **40/40**
- repeated or cyclic cursors: **0**
- page-limit (8) truncations: **0**
- wrong-game rows: **0**; malformed bodies: **0**
- targets marked complete without a persisted continuation response: **0**

`meta` on all 40 continuation responses contains `prev_cursor` and `per_page` and
**omits `next_cursor` entirely** — BALLDONTLIE's end-of-pagination convention.
`_next_cursor_of` maps that absence to `None`, so completion rests on the
provider ending the chain, not on a merely successful HTTP status.

| game | p1 next_cursor | requested | HTTP | c.rows | next | stop | p1 | +new | merged | teams |
|---|---|---|---|---|---|---|---|---|---|---|
| 18447686 | 5615604 | 5615604 | 200 | 2 | null | exhausted | 25 | 2 | 27 | 13/14 |
| 18447691 | 5737288 | 5737288 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447696 | 5857191 | 5857191 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 11/14 |
| 18447698 | 5942559 | 5942559 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 12/14 |
| 18447705 | 6110760 | 6110760 | 200 | 2 | null | exhausted | 25 | 2 | 27 | 13/14 |
| 18447706 | 6121177 | 6121177 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 13/13 |
| 18447715 | 6413532 | 6413532 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 13/13 |
| 18447716 | 6443059 | 6443059 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 11/14 |
| 18447719 | 6551216 | 6551216 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447721 | 6572391 | 6572391 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447722 | 6578272 | 6578272 | 200 | 2 | null | exhausted | 25 | 2 | 27 | 12/15 |
| 18447729 | 6820018 | 6820018 | 200 | 4 | null | exhausted | 25 | 4 | 29 | 14/15 |
| 18447745 | 7261507 | 7261507 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447747 | 7292053 | 7292053 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447749 | 7428823 | 7428823 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 12/14 |
| 18447764 | 7791647 | 7791647 | 200 | 2 | null | exhausted | 25 | 2 | 27 | 13/14 |
| 18447768 | 7930783 | 7930783 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 11/14 |
| 18447770 | 7951633 | 7951633 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 13/13 |
| 18447783 | 8322843 | 8322843 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447788 | 8535013 | 8535013 | 200 | 3 | null | exhausted | 25 | 3 | 28 | 14/14 |
| 18447809 | 9155847 | 9155847 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 13/13 |
| 18447818 | 9395856 | 9395856 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 12/14 |
| 18447820 | 9419083 | 9419083 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447826 | 9645128 | 9645128 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 13/13 |
| 18447850 | 10267199 | 10267199 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 11/14 |
| 18447853 | 10328596 | 10328596 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447854 | 10347560 | 10347560 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447858 | 10451987 | 10451987 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 11/14 |
| 18447859 | 10458854 | 10458854 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 12/14 |
| 18447871 | 10841306 | 10841306 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 12/14 |
| 18447880 | 11021389 | 11021389 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447886 | 11268464 | 11268464 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 12/14 |
| 18447889 | 11308814 | 11308814 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 11/14 |
| 18447898 | 11541587 | 11541587 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447903 | 11741936 | 11741936 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 13/13 |
| 18447904 | 11746006 | 11746006 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |
| 18447908 | 11903327 | 11903327 | 200 | 2 | null | exhausted | 25 | 2 | 27 | 12/15 |
| 18447918 | 12191674 | 12191674 | 200 | 2 | null | exhausted | 25 | 2 | 27 | 13/14 |
| 18447920 | 12206834 | 12206834 | 200 | 1 | null | exhausted | 25 | 1 | 26 | 12/14 |
| 18447921 | 12291833 | 12291833 | 200 | 0 | null | exhausted | 25 | 0 | 25 | 12/13 |

## 6. The low continuation yield — resolved as correct

This was the review's primary question. The yield is **correct provider
pagination, not silent loss**, and the decisive evidence is the page-one row
count.

**Every one of the 40 targets had exactly 25 rows on page one** — a
completely full page at `per_page=25`. That is precisely why BALLDONTLIE emitted
a `next_cursor` for these 40 and for no other game. The 199 games that were
already cursor-complete have 17–24 rows (max 24), so none of them filled a page
and none received a cursor. The target set is exactly "games whose lineup count
is ≥ 25", which is a property of the corpus, not of the recovery.

It follows that a game with exactly 25 lineup entries must return an **empty**
second page: the provider advertises a cursor whenever the page is full, even
when nothing remains behind it. All 19 empty pages belong to games whose merged
total is exactly 25.

Row accounting across all 40 continuation pages:

| Quantity | Value |
|---|---|
| Raw `data` rows | 32 |
| Rows with valid game identity | 32 |
| Rows with valid team identity | 32 |
| Rows with valid player identity | 32 |
| Rows rejected during parsing | 0 |
| Rows lost during normalization | 0 |
| Rows deduplicated within a page | 0 |
| Rows overlapping page one | 0 |
| Rows contradicting page one | 0 |
| Rows persisted to `lineup_players` | 32 |
| DQ findings produced | 0 issue-level (40 note-level) |

Both required identities close exactly:

```
raw rows 32 = rejected 0 + normalized candidates 32
normalized candidates 32 = unique persisted 32 + legitimate duplicates/overlaps 0
```

**What "32 rows" means.** All four candidate readings coincide at 32: 32 raw
provider rows; 32 normalized continuation rows; 32 unique players after
within-page deduplication; and 32 players remaining after comparison with page
one. They coincide because rejection, within-page duplication and page-one
overlap are each zero — cursor pagination is non-overlapping by construction. The
quantities are nevertheless distinct concepts and are reported separately above
rather than conflated.

**The 19 empty pages**, individually verified: all 19 returned a literal
`data: []` (not rows the parser rejected — rejection count is 0 for every one);
all 19 bodies are structurally valid with both `data` and `meta` present; all 19
terminated normally with `next_cursor` absent; and all 19 belong to games whose
page one held exactly 25 rows. They are consistent with provider pagination and
inconsistent with normalization loss.

**The 21 non-empty pages**: all 32 persisted players were verified individually
against the stored bodies — every one carries a valid team id and player id, and
every `(game, player)` pair is unique.

## 7. Page-one + continuation merged coverage

Merged offline in temporary databases; the March corpus was never modified.

- Exactly two teams after merge: **40/40**
- Player conflicts: **0**; contradictory overlaps: **0**; rejected rows: **0**
- Merged size distribution: 25 (×19), 26 (×13), 27 (×6), 28 (×1), 29 (×1)
- Team-count splits: 11/14, 12/13, 12/14, 12/15, 13/13, 13/14, 14/14, 14/15
- **Starters per merged game: exactly 10 in all 40** (five per side)
- Order-independence: re-merged under 200 randomized row and page orderings —
  identical result, conflicts and rejection count for all 40 targets

Comparison against the 199 already-complete games: merged sizes 17–24, two teams
in 199/199, and **exactly 10 starters in 199/199**. The recovered 25–29 range is
directly contiguous with the accepted 17–24 range, and the 10-starter invariant
holds across all 239 games. No confirmed-pregame-starter claim is made anywhere;
`is_confirmed` is 0 on every stored snapshot and observation timing is recorded
as observed. Statistical plausibility is not proof of completeness, but no
structural or provenance violation exists in any target.

## 8. Merge eligibility

Acceptance rule applied — a game is cursor-complete only when the source first
page is valid, every continuation page is preserved, the provider cursor chain
terminates normally, no page-limit or budget truncation occurred, no malformed or
wrong-game response exists, no silent normalization loss exists, the deterministic
merged lineup is internally consistent, and provenance is complete.

| Measure | Count |
|---|---|
| Cursor-complete games | 40 / 40 |
| Structurally valid merged games | 40 / 40 |
| Games with conflicts | 0 |
| Games with normalization loss | 0 |
| Games missing team/player identity | 0 |
| **Games eligible for offline merge** | **40** |
| Games still formally incomplete | 0 |

40/40 completed cursor chains were not assumed to imply 40/40 accepted lineups;
eligibility was computed independently against the rule above and happens to
reach the same number.

## 9. Provenance notes (DQ-NBA-LINEUP-R009)

All 40 audited: exactly one per target, severity `note`, stable rule code, each
naming its provider game and its page-one raw-response id **and** hash, each
recording the requested/returned cursor chain and stop reason, each pointing at a
raw response and ingestion run that exist. No duplicate growth (40 rows, 40
distinct games). No player names — all 29 distinct surnames appearing in the 32
continuation rows were checked against the note text and detail JSON, none
present. No bodies, API keys or authorization values. No note claims complete
pregame starters.

`findings=0` correctly means zero issue-level or blocking findings; the 40
note-level DQ rows are provenance records and are counted separately.

**Do empty pages need a distinct note?** No. `DQ-NBA-LINEUP-R005` already covers
the genuinely anomalous shape — an empty page that advertises a *further* cursor —
and correctly stayed silent here because all 19 empty pages were terminal. Adding
a finding for a terminal empty page would mark ordinary pagination as a defect.
The reporting gap was elsewhere and is repaired below: the *count* of empty pages
was invisible.

The low-yield pattern does not warrant an aggregate defect finding. It is fully
explained by the page-one-exactly-25 structure and reconciles to the row without
residual.

## 10. Pacing — conclusion A: compliant

The 40 transport-start timestamps were reconstructed from
`raw_responses.requested_at`:

| Measure | Value |
|---|---|
| Transport starts | 40 |
| First-to-last duration | 11.176 s |
| Max requests in any rolling 60 s | **40** |
| Max requests in any rolling 10 s | 36 |
| Max requests in any rolling 1 s | 6 |
| Configured policy | 60/min, `burst=BURST_WINDOW_ONLY(0)`, `min_interval=0.0 s` |
| Provider tier maximum | 600/min |

The committed 60/minute policy **intentionally permits an opening burst**.
`RequestRatePolicy.burst = BURST_WINDOW_ONLY` is documented as "the reviewed
BALLDONTLIE behaviour, preserved byte-for-byte", under which "the first
`configured_per_min` calls in a window are free"; `min_interval_seconds` returns
`0.0` for that setting. The sliding window therefore allowed all 40 requests
immediately, because no rolling 60-second window ever reached 60.

Critically, the gate enforced the **configured** rate, not the tier maximum. The
only `RateLimiter` construction site in the repository is
`sports_quant/request_control.py:544`, which passes `rate_policy.configured_per_min`
(60). The tier maximum is used solely as a validation ceiling in
`RequestRatePolicy.__post_init__` and is reported as `provider_rate_limit_per_min`;
it is never enforced. Conclusion B does not apply.

**Correction to earlier reporting.** The post-execution summary of the execution
task flagged an effective "≈212 requests/minute" as a possible departure from the
configured 60/minute. That was a short-run extrapolation of an 11-second burst and
is not a rate violation: the correct measure is the maximum count in a rolling
60-second window, which was 40 against a limit of 60. `throttle_events = 0` is the
**expected** outcome for a 40-request run starting with an empty window, not a
sign the policy was inactive. This review supersedes that observation.

**Terminology adjudication — one narrow defect, repaired.** `basis` and
`tier_verified` answer different questions and both contain the word "verified":
`verified_tier_max` says the ceiling is a provider-published per-tier maximum
rather than a number we chose, while `tier_verified` says whether this run
confirmed the account sits on that tier. Each field was individually honest.
But `_render_rate_line` printed `basis=verified_tier_max … provider_max=600/min`
with no qualification while the same usage record said
`tier_verified=false`, `tier_status=configured_not_verified:goat`,
`tier_evidence_source=none`. A published ceiling keyed to an unconfirmed tier is
still an assumption, so that single line presented a provider maximum as verified
when the repository's verification contract was not met. The rendering is
repaired; enforcement was already correct and the historical execution record is
untouched.

## 11. Persistence

Recovery database, read-only:

- `PRAGMA integrity_check` → `ok`; `foreign_key_check` → 0 rows
- Schema **v17**, exactly **17** migration records in `schema_versions`
- `journal_mode=wal`; WAL 0 bytes (clean close), SHM 32,768 bytes present —
  reported as found, not checkpointed away for hashing convenience

| Table | Count |
|---|---|
| `raw_responses` | 40 |
| `ingestion_runs` (all `succeeded`, Σ`requests_made` = 40) | 40 |
| `provider_game_references` | 40 |
| `provider_team_references` | 14 |
| `provider_player_references` | 29 |
| `provider_team_identity_snapshots` | 22 |
| `provider_player_identity_snapshots` | 32 |
| `lineup_snapshots` (21 games; one game has both teams) | 22 |
| `lineup_players` | 32 |
| `data_quality_issues` (all R009, `note`) | 40 |
| `entity_match_decisions` / `match_candidates` | 0 / 0 |
| `games` / `players` / `nba_game_results` | 0 / 0 / 0 |

Every count above was read from the database, not from the execution report.

**The 32 lineup players reconcile four ways**: `COUNT(lineup_players)` = 32;
`SUM(lineup_snapshots.player_count)` = 32; raw continuation rows = 32; distinct
`(game, player)` pairs = 32, with zero duplicates inside any game. Distinct
`provider_player_id` is **29**, not 32, because two players legitimately appear in
more than one March game (one in two games, one in three) — which is exactly why
`provider_player_references` holds 29 provider entities while
`provider_player_identity_snapshots` holds 32 per-observation records.

**No canonical identity was invented**: `lineup_snapshots.team_id`,
`lineup_players.player_id`, `provider_game_references.game_id` and
`match_decision_id` are NULL on every row, and the match tables are empty. No
orphan lineup snapshots, lineup players or provider game references.

Evidence was written by the production repositories — `SqliteRawResponseRepository`,
`SqliteProviderReferenceRepository`, `IdentityRecorder`, `SqliteDataQualityRepository`
and `SqliteIngestionRunRepository` — reached through the production
`LineupContinuationExecutor._persist_target`, not by ad hoc SQL. This was confirmed
by re-running the full production CLI path over the preserved responses (§13) and
obtaining a semantically identical database.

## 12. Checkpoint

`f1a-checkpoint-v2`, `state: completed`, `schema_version: 17`, bound to manifest
`a8979cd1…` and plan `3c0ec01c…`, `plan_version: f1a-plan-v1`,
`code_version: nba-lineup-continuation`, `families: ["lineups"]`,
`date_range: 2026-03-01..2026-03-31`, `request_cap: 640`.

40 completed identities over 40 distinct entity keys, all with
`endpoint_family: lineups_continuation`. Incomplete, blocked, failed and recovered
identity lists are all empty. `usage_provenance` is
`f1-usage-provenance-v1`, `legacy_migrated: false`, `process_count: 1`, one process
record whose totals equal the checkpoint-level usage exactly.

Completion is justified by **normal cursor termination**, not by HTTP success:
`ContinuationOutcome.complete` returns true only for `stop_reason == STOP_EXHAUSTED`,
and `STOP_EXHAUSTED` is set only when the provider returns no next cursor. All 40
reached it.

**Completed no-op resume**, run twice on transactionally safe copies (SQLite
online-backup API) under zero-network guards — 16/16 checks passed each time:
exit 0, zero client construction, zero requests, `performed_new_work: false`,
`checkpoint_mutated: false`, database and checkpoint byte-identical, semantic
content preserved, checkpoint still holding 40 completed identities in state
`completed`, and no extra process record appended (`process_count` stays 1).

One clarification worth recording: on a completed resume the top-level
`targets_completed` reads 0 and `usage.network_occurred` reads `true`. These are
not in conflict — the per-target counters describe *this process* (which correctly
did nothing) while `network_occurred` and `database_mutated` are logical-run
lifetime values loaded from the checkpoint. `performed_new_work: false`
disambiguates them. The originals were never resumed; only copies.

## 13. Reconstruction, determinism and idempotency

The 40 preserved continuation responses were replayed through the **production
CLI path** behind an `httpx.MockTransport` into three fresh schema-v17 databases,
varying target order (unshuffled, and two different seeds), raw-response order and
row order. 24/24 checks passed:

- each reconstruction completed 40/40 targets with exactly 40 replayed requests
- each database is schema v17 with 17 migration records
- each is **semantically identical to the live recovery database** across raw
  responses (cursor → body hash), provider references, identity observations,
  lineup snapshots (including content hashes), lineup-player rows, DQ records,
  ingestion runs and cursor-chain provenance
- running each reconstruction a second time is a byte-identical no-op issuing
  zero requests with `performed_new_work: false` — idempotent

No evidence was fabricated or refetched; the mock refuses any first-page request
and any cursor without preserved evidence.

## 14. Integrity and isolation

All **42 protected artifacts are byte-identical** to their pre-execution
fingerprints — unchanged 42, changed 0, new 0, removed 0 — covering the March
database and checkpoint, the March manifest and execution evidence, the frozen
pre-results database, MLB June evidence, skeleton/rich pilot evidence, matching
databases and reports, all F1/F1B manifests and the existing review reports. The
source March database still hashes
`39064fa219f4eb66f37e26f567a8f42d7df278236aa70e42264a600b07a5d135` and still holds
1,437 raw responses, 478 lineup snapshots, 5,125 lineup players and 239 results.

No original artifact is a symlink; all are regular files. The recovery database is
a distinct filesystem object that does not alias the source database or
checkpoint. Original evidence was opened read-only (`mode=ro`) throughout. All
recovery artifacts are Git-ignored (`.gitignore:38 data/`) and nothing is staged.

Recovery artifact fingerprints:

```
e2fea1c06c43400323b0266aeb8ba34db28e9b6ead13504413eb93ed4de6e1db  1372160  recovery.db
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855        0  recovery.db-wal
fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb    32768  recovery.db-shm
8c4e83ee6cffb5c713de8bd0382d85b6486b2b68dd75821c7ad5ab38a4c689df    11625  recovery.ckpt
f2aa0c8a9b834ca97ca73e426b69dca9f32cf59b55f39b4c13871bcdbe34e7fc    10234  recovery_execution.json
7d235d85c911115e4e9d81a5754e61da7920ab82029cf91a6a17087022d2c1ee     1025  recovery_execution_meta.json
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855        0  recovery_stderr.log
9427e4bdcfccff417cab76f9c988f098fe1347f277263d1c18906a325e1cb437      127  recovery_progress.log
```

## 15. Defects found and repaired

Two, both in reporting, both proved from preserved evidence, each with a failing
reproducer written before the fix. Neither invalidates any recovered evidence.

**D1 — a provider maximum was presented as verified when it was not.**
`_render_rate_line` printed `provider_max=600/min` under `basis=verified_tier_max`
with no caveat while the same record carried `tier_verified=false` and
`tier_status=configured_not_verified:goat`. Repaired in
`sports_quant/ingest/f1a.py`: when the basis is a tier maximum, a provider maximum
is claimed, and the tier is unverified, the line now appends
`(TIER NOT VERIFIED: ceiling assumes the configured tier)`. The MLB courtesy-cap
disclaimer and the verified-tier wording are both unchanged; no constant was
renamed and no historical record was rewritten.

**D2 — empty continuation pages were invisible in the report.** 19 of 40 pages
came back empty and nothing in the JSON or human report said so; distinguishing
legitimate terminal empty pages from silent normalization loss required
re-reading every stored body. Repaired in
`sports_quant/ingest/lineup_continuation.py` and `sports_quant/cli.py`:
`ContinuationReport` gained `empty_continuation_pages` and
`nonempty_continuation_pages` (page-level, not target-level), both surfaced in
`as_dict()`, in the human `rows:` line as `empty_pages=`/`nonempty_pages=`, and in
the CLI JSON.

**Not defects, examined and dismissed:** the low yield (correct pagination, §6);
R005 staying silent on terminal empty pages (correct — it targets the anomalous
shape, §9); `distinct provider_player_id = 29 ≠ 32` (two players legitimately
appear in multiple games, §11); `network_occurred: true` on a no-op resume
(logical-run lifetime value, §12).

## 16. Validation

All offline. No real sleeps were used: the pacing sleeps are guarded and the
`RateLimiter` was driven by an injected deterministic clock.

```
git diff --check                       clean
ruff check .                           All checks passed
mypy . --no-incremental                Success: no issues found in 304 source files
pytest -q                              2240 passed, 2 skipped, 0 failed
schema init x2                         v17, 17 migration rows, 47 tables, integrity ok (both)
v16 -> v17 migration x2                v16 (0 identity tables) -> v17 (2), 17 rows,
                                       integrity ok, re-apply idempotent (both runs)
non-editable wheel smoke               40/40 checks passed
staged-file / secret audit             8 files, all source/docs/tests; no db, ckpt,
                                       raw response, log, wheel, env or graphify output;
                                       no secret-shaped literal
protected artifact fingerprints        42/42 byte-identical to the pre-execution baseline
recovery evidence fingerprints         unchanged by this review
```

Review harnesses, all under the zero-network sentinel with **0 guard trips**:

| Harness | Checks |
|---|---|
| Cursor-chain reconstruction (40 targets) | 0 issues |
| Raw/normalized/persisted reconciliation and merge | identities close; 40/40 eligible |
| No-op resume ×2 and reconstruction/determinism/idempotency | 40/40 passed |
| DQ, pacing windows, database persistence, isolation | 61/62 passed¹ |
| Non-editable wheel smoke | 40/40 passed |

¹ The single non-pass was an incorrect expectation in the review harness itself
(`distinct provider_player_id == 32`); the correct value is 29 because two players
appear in more than one March game. No product defect. See §11.

New test file `sports_quant/ingest/tests/test_nba_lineup_continuation_execution_review.py`
(14 tests): 4 reproducers for D1/D2 written before the repairs and confirmed
failing, plus 10 pinning the pagination, merge, identity and redaction semantics
the live run depended on. NBA month, continuation, F1B reporting, MLB pacing and
rate-policy suites were run as non-regression coverage.

The wheel smoke ran the **installed** package (resolved from a fresh venv's
`site-packages`, with the CWD outside the repository so the working tree was not
importable) and covered evidence loading, cursor-chain reconstruction,
empty-page classification, deterministic merge simulation, pacing-policy
adjudication, the completed no-op resume, the zero-network sentinel and schema
v17. It deliberately restores three seams so an `httpx.MockTransport` can be
installed — `AsyncClient.send`, `BalldontlieClient.__init__` and
`build_readonly_client` — and asserts that every other guarded seam, including
DNS, sockets, the raw transports, `requests`, `urllib`, settings loading and the
pacing sleeps, still fails closed with no undeclared escape.

---

## Verdict

**ACCEPTED.**

- **Merge-eligible games: 40 of 40.** No game is excluded and no limitation
  restricts the merge set.
- Request accounting closes at every layer; all 40 cursor chains genuinely
  terminated at the provider; raw and normalized continuation data reconcile
  exactly with zero silent loss; page-one + continuation merges are deterministic
  and structurally valid; persistence and checkpointing are correct; pacing is
  compliant with the committed policy; all 42 protected artifacts are unchanged.
- A pacing/reporting repair **was** required, but only to the rendering of tier
  verification (D1) — pacing *enforcement* was already correct, and the repair
  does not affect the validity of the preserved evidence.
- **An offline merge into the March corpus may be separately authorized.** It is
  not authorized by this review and did not occur here.

> **UPDATE (2026-08-08) — that merge has since been separately authorized and
> applied**, to a protected copy only: `data/f1_nba_2026_03_lineups_merged.db`.
> See `F1_NBA_2026_03_LINEUP_MERGE.md`. All 40 merge-eligible games were merged
> (22 revision snapshots, 294 player rows, 32 recovered observations), the
> original March corpus and this recovery evidence are byte-identical, and no
> provider request was made. **The merge is not yet independently reviewed and the
> NBA lineup family is not finally accepted.** PIT labels remain 0/239.

Standing status, unchanged by this review:

- **No merge occurred in this task**, and the recovery evidence has not been
  merged into the March corpus.
- **No provider request occurred** during this review.
- **PIT labels remain 0/239.**
- **Three canonical-matching defects remain open.**
- **The combined F1 review has not begun.**
- **F1 remains incomplete. F2 remains unauthorized.**
