# Checkpoint and resume provenance — independent review of `bd0903f`

Offline correctness and evidence audit of the repair committed at `bd0903f`
(*Preserve checkpoint evidence across resumes*). **Zero provider requests were
made.** NBA was not executed. The original MLB June artifacts were never executed,
resumed or written; all 47 protected artifacts are byte-identical before and after.

## Verdict

### Accepted

The checkpoint-provenance repair is independently validated. Its central claims
hold: a completed zero-work resume is a true byte-identical no-op, logical-run
totals are preserved across any number of processes, prior usage is never
multiplied, and the original June checkpoint remains the untouched historical v1
file.

The review did, however, find **seven defects** in the committed implementation —
all in the same area the repair introduced — plus one **pre-existing durability
weakness** in the checkpoint write path that predates `bd0903f`. All eight are
repaired here with regressions. Acceptance is of the repaired state at the commit
this review produces, not of `bd0903f` as it stood.

---

## 1. Review boundary and zero-network proof

A process-level sentinel installed 20 guards; **12 adversarial probes all failed
closed**: DNS resolution, a non-loopback socket connect, sync HTTP transport, async
HTTP transport, `requests`, `urllib`, MLB client construction, BALLDONTLIE client
construction, the project's read-only transport builder, settings/authentication
loading, and both the sync and async retry sleeps.

Two guards were narrowly relaxed for local file reads only, each documented at the
point of use: `f1a._default_client_factory` (building a closure touches nothing)
and `config.load_settings` (resolving a database path). `MlbStatsApiClient.__init__`
stayed armed throughout, and additional tripwires on client construction and
settings loading recorded **zero** events across every June-copy run — which is how
"no client, no authentication" is proven structurally rather than asserted.

All resume testing ran against synthetic fixtures or copies: databases duplicated
through the SQLite backup API from a read-only source handle, checkpoints through
plain file copies, everything git-ignored.

## 2. Protected evidence

47 artifacts fingerprinted: the development corpus, skeleton and rich databases and
checkpoints, matching copies and reports, the MLB June database, the original June
checkpoint, the June execution logs and meta, all six F1/F1B manifests, and the
committed review reports.

The original June checkpoint was confirmed to be:

- `f1a-checkpoint-v1` (`70bbc7c9…`), with **no** `usage_provenance` block;
- readable without mutation (hash unchanged across loads);
- loaded as `legacy_migrated` with `process_count_known = false`, which is honest —
  its per-process split is genuinely unknowable;
- still **semantically untrustworthy**: it records `state = "completed"` while its
  own usage holds `failed_responses = 2`, i.e. the completion claim conceals the two
  lost roster requests. That historical decision was *not* retroactively repaired.

## 3. Original defect reproduction

Rather than paraphrase the pre-repair behaviour, the parent implementation was
extracted verbatim from `a281085` (`git show a281085:sports_quant/ingest/pilot.py`
and `checkpoint.py`), re-pointed at the installed package, and run against a copy of
the real June checkpoint. It reports `CHECKPOINT_FORMAT_VERSION = f1a-checkpoint-v1`
and contains no no-work short-circuit, confirming it is the pre-repair code.

The parent's completed resume constructed **no** provider client, made **zero**
transport starts and mutated **no** database — and rewrote the checkpoint
(`70bbc7c9…` → `86871d21…`), destroying ten fields:

| field | before | after |
|---|---|---|
| `successful_responses` | 1999 | 0 |
| `failed_responses` | 2 | 0 |
| `retry_attempts` | 7 | 0 |
| `responses_received` | 1999 | 0 |
| `parse_successes` | 1999 | 0 |
| `pages_fetched` | 401 | 0 |
| `throttle_events` | 1999 | 0 |
| `throttle_wait_seconds` | 3407.889 | 0.0 |
| `network_occurred` | true | false |
| `families_completed` | `[game, skeleton]` | `[]` |

`http_429s` and `blocked_requests` were already 0 in the June run, so no loss is
observable there — they are equally exposed in principle. Running the *same* resume
under the current implementation preserved every one of those fields and left the
checkpoint byte-identical.

## 4. Accounting-model and field-rule audit

The model is sound: `usage` holds logical-run totals, `usage_provenance.processes`
is the append-only per-process history they derive from, and
`logical_total = prior_total + current_process_value` for additive counters. A
process owns one entry and replaces it, and the gate's pre-charge is subtracted out
of that entry, so repeated resumes cannot multiply prior usage.

All 57 `UsageReport` fields carry exactly one rule, enforced by an exhaustiveness
test that fails if a field is added without declaring one. Each rule was checked
against the field's semantics:

| Rule | Fields | Assessment |
|---|---|---|
| Additive | `reserved_attempts`, `attempted_requests`, `transport_starts`, `responses_received`, `parse_successes`, `successful_responses`, `failed_responses`, `retry_attempts`, `pages_fetched`, `blocked_requests`, `http_429s`, `throttle_events`, `throttle_wait_seconds`, `reserved_credits` | Correct — each counts events in one process |
| Additive-optional | `reported_credits_consumed` | Correct — sums known values; unknown stays unknown |
| Latest | `provider_credits_remaining`, `skipped_on_resume`, `checkpoint_state` | Correct — a point-in-time balance is not a sum, and each process skips what *it* found done |
| High-water mark | `games_received`, `games_selected`, `games_excluded_by_max_games`, `games_deduplicated` | Correct — a frozen selected set; a process that never re-observes it (0) cannot erase it, one that does cannot double it |
| Logical OR | `network_occurred`, `database_mutated`, `rate_policy_active`, `rate_limited`, `selection_truncated`, `tier_verified` | Correct — something observed stays observed |
| Union | `families_completed`, `families_failed`, `families_truncated` | Correct — deterministic sorted set union |
| Evidence precedence | `authentication_status`, `tier_status`, `tier_evidence_source`, `credit_header_status` | Correct — ranked so evidence is never downgraded nor upgraded without an observation; `inconsistent` credit headers outrank clean ones |
| First evidence | `budget_exhausted` | Correct — a later `None` cannot erase an exhaustion |
| Stable configuration | provider, league, manifest hash, estimates, rate identity, credit applicability | Correct — must agree wherever both are known, else fail closed |
| Derived | `prior_requests`, `prior_credits`, `prior_transport_starts`, `prior_pages_fetched` | Correct — recomputed from history, never trusted from disk |

**No counter is treated as additive when it is not**, and the two traps were both
avoided in the original design: selection counts (which are re-observed) use a
high-water mark, and `skipped_on_resume` (meaningless to sum) uses latest.

What the rule table did **not** do was validate the *values* it combined — see
defect D1.

## 5. Accounting identities

Independent fixtures confirm exact closure: `logical = prior + current` for every
additive counter; `reserved ≥ transports ≥ terminal outcomes`;
`terminal = successful + failed`; `retries = transports − terminal` for the current
provider-client contract (applied only in a *settled* state, since a truncated
process may hold an attempt that never resolved — documented as an explicit
exception); counts non-negative; logical reserved attempts never exceeding the
manifest cap; credits never receiving a fresh budget on resume; a completed
checkpoint holding no unresolved unit; configuration identity constant across
entries; and a clean later process unable to erase an earlier failure.

**Unknown is not zero.** An absent counter is skipped rather than read as 0 —
except where the recorded values alone are already impossible (recorded outcomes
exceeding recorded transports is a contradiction whether or not the other outcome
field is present, because unknown can only add).

## 6. Process identity and PID reuse

Entries are ordered by process and replaced by identity. `bd0903f` distinguished
them **only by list position** and carried no identifier at all (defect D5).

The repaired implementation stamps a fresh random per-invocation `process_id` —
explicitly **not** a PID, since PIDs are reused; a test asserts 200 generated ids
are unique and that none equals the current PID. Duplicate identifiers and
malformed tokens are refused, and the migrated legacy aggregate carries the
non-random marker `legacy-v1-unsplit`, which says what it is (an unknown number of
earlier processes) rather than implying one identified run.

Verified: many writes within one process replace a single entry (5 units → 1 entry);
a distinct process adds exactly one; a crash before the first boundary records one
honest zero-work entry and fabricates nothing; repeated load/combine/write cycles
are byte-stable and order-stable; and process order is deterministic and does not
affect the totals.

**Concurrency was not a live hazard** even in `bd0903f`: `verify_resume` compares
the checkpoint's scratch fingerprint against the current database, so a concurrent
writer that persisted anything is rejected before writing. A `_assert_sole_writer`
guard was nevertheless added as defence in depth — the on-disk history must be the
prior history this process read plus at most its own entry — and it is exercised by
a test that stages a stale history and confirms the write is refused.

## 7. Gate pre-charge and double counting

Confirmed independently: prior reserved attempts reduce the remaining request
budget (8 of 10 pre-charged → 2 succeed, the third raises `BudgetExhausted`); prior
credits reduce the remaining credit budget and are excluded from the current
entry; the logical total includes the prior amount exactly once; four repeated
resumes leave the totals bit-identical; a blocked request is counted as blocked and
**not** as a transport start; and retry and pacing evidence is attributed to the
process that saw it (3 + 7 throttles → 10 logical; one process's single 429 stays
attributed to it).

A **five-process randomized state machine** over 30 seeds asserts, for every seed,
that all additive totals equal the per-process sum, that `prior + current` closes
exactly, that `prior_transport_starts` matches the earlier processes, that combining
is deterministic, and that every invariant including the retry identity holds.

## 8. Completed no-work resume

Against copies of the real June database and checkpoint, run **three times**:

- validates manifest, checkpoint and scratch database;
- constructs **no** provider client (tripwire recorded zero events);
- loads **no** settings or authentication (tripwire recorded zero events);
- makes zero DNS/socket/transport calls and uses zero pacing sleep;
- writes no database row and no checkpoint byte — database and checkpoint hashes
  identical after every attempt, all three digests equal;
- returns `new_work=false`, `checkpoint_mutated=false`, empty
  `current_process_usage`, and `skipped_on_resume = 401`;
- leaves the copy as `f1a-checkpoint-v1` with no `usage_provenance` block, because a
  no-op does not upgrade a legacy file.

Preserved logical totals, every attempt:

| | |
|---|---|
| Reserved attempts | 2008 |
| Transport starts | 2008 |
| Successful responses | 1999 |
| Terminal failed responses | 2 |
| Retry attempts | 7 |
| Pages fetched | 401 |
| Courtesy throttle events | 1999 |
| Courtesy throttle wait | 3407.8885556863097 s |
| Games received / selected | 402 / 400 |

The historical `completed` state is reported unchanged and is **not** re-judged.

## 9. Legacy v1 compatibility

Tested across full, sparse, zero-valued, unknown-field, secret-shaped,
wrong-type, negative, non-finite, duplicate-key, oversized, contradictory-state and
malformed-identity-set shapes.

Loading never mutates the v1 file (asserted on the byte hash, including after a
*failed* load). Present evidence is preserved; **missing evidence stays missing** —
a sparse file keeps only what it has, and `transport_starts`, `failed_responses`,
`retry_attempts`, `http_429s`, `pages_fetched` and `throttle_wait_seconds` are
absent from the entry rather than invented as 0. Unknown keys are dropped and never
reach a report or v2 output; a planted `sk-live-…` value appears nowhere in the
body, the logical usage, the process history or a rewritten file. A v1 file upgrades
to v2 only when genuine new work happens; `process_count_known` stays false, and the
legacy aggregate keeps its `legacy-v1-unsplit` marker across later resumes so no
process can pretend it was several known processes.

One clarification the review pinned: a **failed** legacy checkpoint whose units are
all complete legitimately *is* rewritten on resume — no provider work happens
(`performed_new_work=false`) but the state genuinely transitions to `completed`
(`checkpoint_mutated=true`). The byte-identity guarantee covers a checkpoint that is
already `completed`.

## 10. V2 corruption and tampering

All refused with deterministic, sanitized `CheckpointError`s raised **before** any
client construction, authentication load, writable database access or checkpoint
write: totals inconsistent with history (now for *every* rule kind, not only
additive — see D3), duplicate process identities, unsupported accounting version,
unsupported checkpoint version, incorrect process count, changed manifest hash,
changed rate-policy identity, changed provider/league, usage over the request cap,
credit use over cap, completed state with unresolved identities, one identity both
complete and failed/blocked/incomplete, a recovery identity never completed, a
recovery identity still unresolved, non-finite and negative values, booleans in
integer fields, strings and dicts in counters, non-object process entries, and
excessively deep nested provenance. Error messages are truncated and never echo a
whole hostile value (a 5000-character injection yields a message under 200 chars).

A changed scratch-database fingerprint is rejected by the pre-existing
`verify_resume` before any write. Duplicated family names in a *derived total* are
normalized to a deterministic set rather than rejected — documented as intended
behaviour, since the loaded value is then correct.

## 11. Recovered-identity semantics

Only an identity previously failed, blocked or incomplete becomes recovered;
recovery removes it from every unresolved collection; it cannot remain
simultaneously failed or incomplete (fails closed); repeated successful resumes do
not duplicate it; a unit completed first time is never labelled recovered; and a
completed state plus any unresolved identity fails closed. Reports now distinguish
initially completed, still unresolved, and recovered on resume — see D6 and D7.

## 12. Multi-process partial-unit integration

Four processes driven through the **real** runner and ingestors against mocked
transports, for MLB and NBA:

1. some families persist, one required family fails terminally → the unit is **not**
   checkpointed complete (`state = failed`), persisted families survive, the lost
   family has zero rows;
2. a resume fails again → the second failure adds exactly once
   (`total = prior + current`), and no append-only observation is duplicated;
3. a resume succeeds → only the missing family is added, five other observation
   tables are unchanged, every earlier failure and retry is still in the totals, the
   current process reports zero failures, and the unit enters
   `recovered_identities` with three distinct process ids;
4. a completed no-work resume → zero requests, zero byte change, unchanged row
   counts, preserved totals, empty current-process usage.

## 13. Authentication, tier and rate evidence

Confirmed independently: a later unobserved process cannot upgrade authentication
from unknown to authenticated, nor promote a configured tier to verified; a verified
earlier tier and a `bounded_capability_audit` source survive a no-work process; an
explicit authentication failure stays visible; MLB `project_courtesy_cap` pacing
never becomes a claimed provider limit (`provider_rate_limit_per_min` stays null and
the report labels it "PROJECT COURTESY CAP, not a provider limit"); BALLDONTLIE
`configured_rate_per_min` and `provider_rate_limit_per_min` remain distinct
(300 vs 600); 429 evidence and `rate_limited` are not erased by a later clean
process; and courtesy throttle wait stays attributable per process.

## 14. Reporting

JSON and human output both distinguish current-process values, prior-process values
and logical-run totals for requests, successes, terminal failures, retries, pages,
throttling, 429s and blocked requests, plus `new_work`, `checkpoint_mutated`,
`process_count` and a legacy marker. The top-level `usage` key retains its name for
existing consumers and carries logical-run totals — never current-process zeros —
which is what makes the June no-op print `successes this_process=0 prior=1999
logical_total=1999` instead of a misleading clean zero. Unknown legacy values are
absent rather than zero. Unit-level provenance
(`initially_completed` / `recovered_on_resume` / `still_unresolved`) was missing and
is added here (D6/D7); structural assertions cover both MLB and NBA reports.

## 15. Atomicity and filesystem behaviour

Verified without weakening existing isolation: each write uses a unique temp name
(5 writes → 5 distinct names, all containing `.tmp-`); flush + fsync precede an
atomic `os.replace`; the directory fsync is best-effort; a simulated `os.replace`
failure leaves the previous checkpoint fully readable with its evidence intact and
**no** temp file behind; a symlinked target is refused for both read and write; an
oversized file is refused; and six threads writing the same path 20 times each
complete with a consistent result and no temp residue.

The concurrency test surfaced defect **D8**: under full-suite load on Windows,
`os.replace` intermittently failed with `PermissionError(13, 'Access is denied')`
even though the per-path lock had correctly serialized every writer of ours. The
cause is external — an unrelated handle (virus scanner, search indexer) on the file
just written — and the consequence was a *spurious* checkpoint-write failure, which
for a 401-write month run is a compounding risk of recording a failure that never
happened. Retrying is safe precisely because the replace is atomic (it either
happened or it did not), so the write now retries a transient `PermissionError` a
bounded five times with a short injectable backoff. A **persistent** failure still
raises, leaving the last valid checkpoint readable and no temp file behind, and the
tests inject a no-op sleep so nothing really waits.

---

## Defects found and repaired in this review

| # | Defect | Impact | Repair |
|---|---|---|---|
| D1 | Usage values from an untrusted checkpoint had **no type or range validation** | A string, dict, bool, negative or NaN counter loaded silently, then surfaced as a bare `TypeError` from inside the combiner, or corrupted a total (`True` counts as 1) | Every value is validated against the `UsageReport` dataclass's own annotations — one source of truth that cannot drift. Wrong type, non-finite and negative are refused with a sanitized error |
| D2 | A **legacy v1 checkpoint skipped accounting validation entirely** | Impossible evidence — outcomes exceeding transports, pages beyond successes, usage above the cap — loaded silently | Legacy files are validated for impossible values while still skipping the history-closure and retry identities they cannot satisfy |
| D3 | Only **additive** totals were compared against the recorded history | A tampered `families_completed`, `network_occurred`, selection count or auth/tier total went undetected | Every rule-covered field must equal what the history implies |
| D4 | **Unit-set contradictions were unchecked** | One identity could be both completed and failed/blocked/incomplete; `recovered_identities` could name a unit never completed or still unresolved | `_validate_identity_sets` fails closed on each, with counts and a truncated example rather than dumping identities |
| D5 | Process entries carried **no per-invocation identifier** | Nothing could correlate an entry with a run or detect a clobber; identity was list position alone | A random per-invocation `process_id` (never a PID), uniqueness enforced, plus a `_assert_sole_writer` guard |
| D6 | `PilotResult` exposed **no recovered-identity information** | A consumer could not tell a first-time completion from a recovery after failure | `recovered_identities`, `unresolved_identities`, `initially_completed` and counts in JSON, plus a `units:` line in the human report |
| D7 | **`incomplete_identities` was never written** by the pilot on a unit failure | The whole recovery mechanism was inert in production: nothing recorded which unit failed, so "still unresolved" was always empty and no completion could ever be recognised as a recovery. Earlier tests set the field by hand and masked this | The executor tracks the in-flight unit and the runner records it on failure — read from the executor, never inferred |
| D8 | `os.replace` **transient `PermissionError` on Windows** (pre-existing, not from `bd0903f`) | A spurious checkpoint-write failure under load, reporting a failure that never happened — compounding across a 401-write month run | Bounded 5-attempt retry with a short injectable backoff; a persistent failure still raises and leaves the last valid checkpoint intact |

D7 is the most consequential: the feature `bd0903f` added was untestable in
production without it. It was caught only by driving the real runner end to end
rather than a synthetic executor, which is why the four-process integration test
exists.

One regression was introduced and caught during this review: the first version of
D2 treated an **absent** legacy counter as zero, manufacturing a contradiction the
evidence never claimed. The sparse-legacy tests now pin absent-is-unknown.

## Validation

`git diff --check` clean · `ruff check .` clean · `mypy . --no-incremental` clean
(294 files) · `pytest -q` **2008 passed, 2 skipped** (one skip is symlink creation,
which needs privileges this host does not grant) · `db-init` twice at v17 · a
representative
v16→v17 migration applied twice (integrity ok, 17 ledger rows) · non-editable wheel
smoke covering legacy v1 loading, v2 tamper rejection, hostile value types,
absent-is-unknown, five-process accounting, partial-unit recovery bookkeeping, a
zero-network sentinel and schema v17 · staged-file and secret-artifact audit clean.

## Next boundary

NBA was **not** authorized or executed here. The exact next boundary is: a fresh,
bounded BALLDONTLIE provider audit immediately before any NBA month execution, with
the NBA season-month manifest re-verified against its recorded hash. Nothing in this
review authorizes it.

**F1 remains incomplete and F2 remains unauthorized.**

Three matching defects remain open and were deliberately **not** touched here:

1. **Official same-name player order dependence** — which member of a same-name
   collision pair resolves depends on traversal order.
2. **Non-idempotent team decisions** — each matching pass appends one `team`
   decision per occurrence, growing the ledger without bound.
3. **Missing canonical-ID propagation into observation tables** — after successful
   matching, `player_game_statistics.player_id` and friends remain NULL, so any
   consumer reading those columns sees nothing.

The original June data remains valid, its checkpoint remains the unchanged
historical v1 file whose completion claim is still semantically untrustworthy, and
the two missing June roster responses remain explicitly missing.
