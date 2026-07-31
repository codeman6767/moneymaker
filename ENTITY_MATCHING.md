# Entity Matching

Deterministic, explainable resolution of provider-supplied names and events to
canonical entities.

Two rules govern everything below:

> **1. Matching is deterministic.** The same inputs and the same alias tables
> always produce the same decision. No randomness, no floating-point
> tie-breaks, no dictionary-iteration order, no wall-clock dependence.
>
> **2. An ambiguous match is never silently accepted.** Ambiguity produces an
> `AMBIGUOUS` decision with `needs_manual_review = 1`, never a guess.

Companion documents: `DATA_ARCHITECTURE.md` (schema), `POINT_IN_TIME_DATA.md`
(temporal rules), `DATA_FOUNDATION_PLAN.md` (phasing).

---

## 1. Why fuzzy matching is rejected

Edit-distance matching is not used anywhere in this design.

The obvious counter-argument is that fuzzy matching handles typos. It does — and
it also confidently matches "Jalen Williams" to the wrong Jalen Williams, "Los
Angeles Clippers" to the Lakers on a bad tokenization, and "NY" to either New
York team. In a betting corpus these are not cosmetic errors: a mismatched team
inverts the sign of a position's edge, and the failure is invisible because the
row still looks well-formed.

The chosen approach is **deterministic normalization plus explicit alias
tables**. Unknown names do not get guessed at; they get recorded as unresolved
and reviewed once, after which the alias table knows them forever. The cost is
some manual curation early. The benefit is that a match is either right or
loudly absent, and every decision is explainable by pointing at the exact alias
row that produced it.

`intel/player_matching.py` already implements precisely this philosophy
(`MatchStatus.MATCHED | AMBIGUOUS | UNMATCHED`, exact-id and
`(team, normalized_name)` indexes, genuine ambiguity reported rather than
resolved). This design **extends that module rather than replacing it.**

---

## 2. Normalization

### 2.1 The normalization pipeline

**Implemented in Phase A** as `sports_quant/db/normalize.py::normalize_name()`
— in `db/` rather than the planned `matching/` because Phase A needs it for
alias storage and lookup. Phase D's matcher imports this module rather than
defining a second normalizer.

One function, applied identically at alias-write time and at lookup time.
Steps, in fixed order:

1. Unicode NFKD decomposition, then strip combining marks
   (`Acuña` → `Acuna`, `Dončić` → `Doncic`, `Jokić` → `Jokic`).
2. Casefold to lowercase (`str.casefold()`, not `str.lower()` — correct for
   non-ASCII).
3. Replace `&` with ` and `.
4. Remove punctuation: `. ' ’ - – — , /` → removed or replaced with a space
   (`St. Louis` → `st louis`, `D'Angelo` → `dangelo`, `Shai
   Gilgeous-Alexander` → `shai gilgeous alexander`).
5. Collapse internal whitespace runs to a single space; strip ends.
6. Collapse a run of single-character tokens into one (`"N.Y."` → `n y` → `ny`,
   matching `"NY"`). A name composed *entirely* of single letters is an
   abbreviation by construction, so joining is safe; a name with any
   multi-letter token is left alone, so `"J R Smith"` does not become
   `jrsmith`.
7. Drop a trailing generational suffix into a separate return value
   (see §3.1).

Deterministic and pure: no locale dependence, no `set` iteration, no clock.
A golden-file test pins the output for a fixed input corpus, so a change to
normalization is impossible to make accidentally — the diff shows every affected
name.

`normalize_name()` returns `(normalized: str, suffix: str | None)`. Callers must
handle both; the suffix is never silently discarded.

### 2.2 What normalization deliberately does not do

- **No stemming, no phonetics, no soundex.** These map distinct names together.
- **No stopword removal.** Removing "the" or "of" merges distinct franchise
  names in edge cases.
- **No abbreviation expansion.** `NY` → `New York` is an *alias table* fact, not
  a transformation. Expanding it in code hides it from review and makes it
  untestable per-team.

---

## 3. Alias handling

### 3.1 Player-name variations and suffixes

Suffixes (`Jr.`, `Sr.`, `II`, `III`, `IV`, `V`) are stored in
`players.suffix`, separate from `full_name` (`DATA_ARCHITECTURE.md` §3.2), and
extracted separately by `normalize_name()`.

This is not cosmetic. Consider:

| Case | Correct behaviour |
| --- | --- |
| Provider writes "Ronald Acuna" for "Ronald Acuña Jr." | Match. Only one Acuña in MLB; suffix omission is a formatting variance. |
| Provider writes "Ken Griffey" in 1990 | Match Ken Griffey **Sr.** — he is the active player that season. |
| Provider writes "Ken Griffey" in 1995 | **AMBIGUOUS.** Both were active. Refuse. |
| Provider writes "Vladimir Guerrero Jr." | Match the son on the explicit suffix; never the father. |

The rule: a suffix present in the input is **binding** — it must match the
canonical suffix. A suffix absent from the input is **permissive** — it may
match a player with a suffix, but only if exactly one candidate survives the
season filter. Two survivors is `AMBIGUOUS`.

Alias types recorded in `player_aliases.alias_type`:

| Type | Example |
| --- | --- |
| `full` | `Shai Gilgeous-Alexander` |
| `short` | `S. Gilgeous-Alexander`, `SGA` |
| `nickname` | `Bobby Witt` for `Bobby Witt Jr.` |
| `accent_stripped` | `Luka Doncic` for `Luka Dončić` |
| `suffix_variant` | `Ronald Acuna` for `Ronald Acuña Jr.` |
| `provider` | whatever a specific provider writes |

### 3.2 Team aliases

`team_aliases.alias_type` covers every requested variation:

| Type | Examples for `tm_mlb_nyy` |
| --- | --- |
| `abbreviation` | `NYY`, `NY`*, `NYA` |
| `city` | `New York` |
| `nickname` | `Yankees`, `Bronx Bombers` |
| `full` | `New York Yankees` |
| `punctuation` | handled by normalization, not stored |
| `historical` | `New York Highlanders` (season-scoped) |
| `provider` | The Odds API's / Kalshi's exact strings |

\* `NY` is inherently ambiguous in both leagues (Yankees/Mets; Knicks/Nets). It
is stored against **both** teams with `is_ambiguous = 1`, so a bare `NY` can
never resolve on its own — it must be disambiguated by opponent, schedule, or
provider scope. Encoding the ambiguity as data is what makes the refusal
automatic instead of relying on someone remembering the edge case.

**Ambiguity is derived, not hand-marked.** After seeding, the loader runs
`mark_ambiguous_duplicates()`, which flags every alias whose normalized form
maps to more than one team in the league. In the shipped seed that flags 6 MLB
rows (`chicago`, `new york`, `los angeles` — two teams each) and 2 NBA rows
(`los angeles`). Deriving the flag is deterministic and self-correcting as
franchises move, where a hand-maintained list drifts.

This is also why `TeamSeed` carries `extra_cities`. The Clippers brand
themselves "LA", so with canonical cities alone `"Los Angeles"` would have
resolved cleanly — and wrongly — to the Lakers. Recording "Los Angeles" as an
additional Clippers city makes the genuine ambiguity visible to the derivation.

**Historical names are season-scopable — and the scoping is not yet curated.**
`valid_from_season` / `valid_to_season` bound each alias, and `teams` carries
`(first_season, last_season)`. `TeamAliasRepository.resolve()` accepts a
`season_year` argument and, when given one, excludes aliases whose window does
not contain it. Provider aliases are additionally scoped by `provider`, so one
provider's idiosyncratic spelling cannot pollute another's namespace.

> **One season-year contract.** The season integer is always the season's START
> year: MLB `2026` = the 2026 season; NBA `2025` = the 2025-26 season. The three
> helpers agree by construction — `season.season_year_for(date)` maps a date to
> that start year (NBA Jan–June → the previous year), `season.season_bounds`
> gives the membership window (NBA `[Y-07-01, (Y+1)-06-30]`), and
> `schema.season_label` renders it (`2026`, `2025-26`, `… postseason`). Sportsbook
> and official-game matching both pass this start-year integer to the resolver, so
> no call site uses NBA ending-year semantics. `season_bounds` is authoritative
> for membership; the placeholder `seasons.start_date` (calendar Jan 1) written by
> official-game matching is not.

> ⚠️ **The seeded aliases carry no real validity years.** Every seeded alias —
> including historical names such as "Cleveland Indians", "Washington Bullets"
> and "Oakland Athletics" — is stored with the unbounded sentinels
> `valid_from_season = 0`, `valid_to_season = 9999`, because verified validity
> dates are not present in repository-controlled data and **inventing them
> would be worse than leaving them open**. A wrong date silently excludes
> correct matches, and nothing surfaces the error.
>
> So resolving "Washington Bullets" with `season_year=2026` currently
> **matches** rather than returning `UNMATCHED`. The filtering mechanism works
> and is enforced for any alias that does carry a curated window; populating
> real windows for the seeded historical names is **Phase D curation work**.

Because "matched under a season filter" and "verified as valid that season" are
different claims, `AliasResolution` reports which one applies:

| Field | Meaning |
| --- | --- |
| `season_year` | The season the caller asked about, or `None` |
| `season_scoped` | Whether candidates were filtered by validity window at all |
| `season_validity_verified` | Whether **every** surviving candidate carries a curated (non-sentinel) window |

`season_validity_verified=False` means the match does not prove the alias was in
use that season. A caller that needs a real historical guarantee must check it
rather than assume the filter did the work — the API is built so that
assumption cannot be made silently.

### 3.3 Alias resolution order

Strictly ordered; the first tier that yields exactly one candidate wins.

| Tier | Method | Score | Notes |
| --- | --- | --- | --- |
| 1 | `exact_provider_id` | 1.00 | Provider's stable id already linked. Cheapest and strongest. |
| 2 | `exact_alias` | 0.99 | Raw string matches an alias verbatim, provider- and season-scoped. |
| 3 | `normalized_alias` | 0.95 | Normalized forms match, provider- and season-scoped. |
| 4 | `normalized_alias_unscoped` | 0.90 | Normalized match ignoring provider scope. |

Season scoping is applied as a *filter* before these tiers run, not as a tier of
its own: an alias outside its validity window is not a weaker candidate, it is
not a candidate. Implemented in Phase A; see the caveat in §3.2 about seeded
aliases still being unbounded.
| 5 | `structured_key` | 0.85 | Games only — schedule-key match (§4). |

If a tier yields **two or more** candidates: stop, emit `AMBIGUOUS`, record
every candidate, set `needs_manual_review = 1`. Do **not** fall through to a
weaker tier — a lower tier cannot resolve an ambiguity a stronger one could not,
and trying is how a wrong answer gets manufactured.

If every tier yields zero candidates: emit `no_candidate` with
`needs_manual_review = 1`. Unknown entities are a curation task, not an error.

Acceptance threshold is `0.85`, stored per decision in
`entity_match_decisions.threshold` so a future threshold change does not
retroactively reinterpret old decisions.

---

## 3.4 Structured provider identity and official bootstrap (F1, schema v17)

> **Status.** Implemented; migration `e017_provider_identity`. Proven by an
> offline replay of the preserved F1B rich corpora (§3.4.4). F1 itself is
> **not** complete — see `PHASE_F_RESEARCH_PLAN.md`.

### 3.4.1 Why the F1 one-game matching pilot returned 0%

The F1 canonical entity-matching pilot resolved **nothing**: 0 of 1 game, 0 of 2
teams and 0 of 52 (MLB) / 35 (NBA) players, in both leagues. **The refusal was
correct.** The matcher evaluated every reference, scored each 0.00 against the
0.85 threshold with full provenance, flagged all of them for manual review,
raised `DQ-MATCH-010`, and invented nothing. The cause was a missing bootstrap,
not a matcher defect:

* `game_schedule_snapshots` stores `home_provider_team_id` /
  `away_provider_team_id` and **no team name**; `provider_team_references` stores
  ids and provenance and no name either. `TeamResolver.resolve` falls back to the
  provider id as the match string when given no `raw_name`, so it was asked to
  find an alias equal to `'141'` — and none of the 311 seeded aliases is a
  provider-id alias.
* `provider_player_references` likewise stores ids only, and `players` /
  `player_aliases` start empty, so every provider player had an empty candidate
  pool.

The provider-written names existed all along — inside `raw_responses` bodies.

### 3.4.2 What v17 adds

Two append-only tables, written by ingestion and read by matching:

`provider_team_identity_snapshots` — provider, provider team id, league,
provider-written `full_name`, `normalized_name`, plus `abbreviation` / `city` /
`nickname` **only when genuinely supplied**, `observed_at`, raw-response id +
hash, `content_hash`, `created_at`.

`provider_player_identity_snapshots` — the same shape plus `suffix` (split by the
shared normalizer), and `first_name` / `last_name` / `birth_date` / `position` /
`provider_team_id` **only when genuinely supplied**. MLB StatsAPI sends
`fullName` and no parts, so MLB rows carry no first/last name: splitting a full
name is a guess, and a guess in a typed column is indistinguishable from a fact.

Both refuse UPDATE and DELETE by trigger. Uniqueness is
`(provider, entity id, observed_at, content_hash)` — one row per *(observation
time, content)*. Keying on the content hash alone would deduplicate **states**
rather than **observations**, and the surviving row would keep whichever
`observed_at` was written first, making every later "latest identity as of T"
answer depend on raw-response processing order. Migration `a003` already fixed
exactly this mistake in `game_status_history`. Latest selection is
`ORDER BY observed_at DESC, content_hash DESC`; the hash is a **total** tie-break,
and an equal-time contradiction is additionally reported as `DQ-IDENTITY-002`
rather than resolved silently.

Extraction is centralized in `sports_quant/ingest/identity_extract.py`, one entry
point for every endpoint family (MLB: schedule, box score, line score, roster;
NBA: games, single game, box scores, stats, advanced stats, plays, lineups).
Dispatch is fail-closed: an unrecognised family yields nothing rather than a
best-effort scrape. A name is **never** inferred from an id; a nameless identity
object is reported (`DQ-IDENTITY-001`, severity `note`), never filled in. Plays
carry bare integer participants, so they contribute team identities only.

Identity observations are a matching **resolver input**, not a feature: both
tables are registered `unsupported` in the point-in-time registry.

### 3.4.3 Resolution changes

**Teams.** `MatchGamesService` now passes the latest structured team name to
`TeamResolver`, bounded by the schedule observation's own `observed_at`, so a name
the provider wrote *later* cannot retroactively decide an earlier point-in-time
match. Teams resolve against the **existing seeded aliases** (tier 4,
`normalized_alias_unscoped`, 0.90) and are **never invented**. With no identity
recorded the resolver still receives nothing and still returns the same honest
`no_candidate` — the refusal path is unchanged, not weakened. Ambiguity is still
refused. After the first accepted link, replay resolves through
`exact_provider_id`.

**Players.** The rule was "a canonical player is never created from a provider
name". It is now:

* an **unknown or nonofficial** provider name never creates a canonical player —
  not a sportsbook, not Kalshi, not an offline import, not a manually supplied
  string, not an unrecognised provider; and
* the league's **designated official** provider's stable player id, together with
  a structured identity observation carrying a nonempty provider-written name,
  **may** bootstrap the canonical player.

| Tier | Method | Score | Notes |
| --- | --- | --- | --- |
| — | `official_provider_bootstrap` | 1.00 | Designated official provider's stable id + structured identity. Last resort only. |

The designation is `matching.service.OFFICIAL_PROVIDER_BY_LEAGUE`
(`lg_mlb` → `mlb_statsapi`, `lg_nba` → `balldontlie`), derived from one map so it
cannot drift. The score is 1.00 because identity is anchored by that permanent
official id, **not** by a fuzzy name guess.

Order and guards, each a real refusal:

1. an existing exact provider link wins (`exact_provider_id`);
2. otherwise the structured name is tried through the ordinary alias/name tiers;
3. exactly one existing candidate — link to it, no creation;
4. two or more candidates — `AMBIGUOUS`; bootstrap is **not** reached;
5. a name landing on a canonical player that **another id from the same
   provider already owns** is a same-name collision, not a discovery, so it is
   refused and two distinct official ids stay two identities;
6. only with nothing resolved, and only for the designated official provider with
   a stable id, a nonempty name, a known league and an unlinked reference, is one
   canonical player created — together with one provider-scoped alias from the
   exact provider-written string, the provider link, and one accepted decision,
   **all four in a single transaction that rolls back as a unit**.

Never fabricated: first/last name, birth date, position, city, abbreviation,
nickname, `debut_date`, `final_game_date`. A career window is not observable from
one identity snapshot.

### 3.4.4 Offline replay result (preserved F1B corpora, one game per league)

Replayed from preserved raw responses only, zero provider requests, on fresh
temporary v16 to v17 copies. Originals untouched.

| | MLB | NBA |
| --- | --- | --- |
| team identity observations | 34 | 42 |
| player identity observations | 180 | 365 |
| identities rejected | 0 | 0 |
| provider teams linked | 2 / 2 | 2 / 2 |
| canonical teams created | 0 (60 seeded, untouched) | 0 |
| canonical game | **1 created** | **1 created** (after the §3.4.6 repair; 0 at bootstrap time) |
| provider players linked | 52 / 52 | 35 / 35 |
| canonical players bootstrapped | 52 | 35 |
| provider aliases created | 52 | 35 |
| invented career windows | 0 | 0 |

Teams resolved via `normalized_alias_unscoped` (0.90) and re-affirm via
`exact_provider_id` (1.00) on replay. All players resolved via
`official_provider_bootstrap` (1.00), exactly once each.

**Separate blocker — NBA game canonicalization — now REPAIRED.** At the time of
the identity bootstrap the NBA game refused with `no scheduled start to match a
game on`. That was never an identity gap (both teams resolved and all 35 players
bootstrapped); the cause was a distinct pre-existing ingestion defect —
`nba_ingestor._normalize_game` set `scheduled_start` only when the game's status
was *scheduled*, so a **finished** game stored `NULL` even though the
BALLDONTLIE payload carried `datetime: "2026-01-06T00:00:00.000Z"`.

That normalization boundary has since been repaired (§3.4.6). The game matcher's
requirement for a scheduled start was **not** weakened. Replaying the preserved
NBA responses into a clean schema-v17 corpus now yields, for the pilot game:

| | NBA |
| --- | --- |
| schedule scheduled_start | `2026-01-06T00:00:00.000000Z` |
| provider teams linked | 2 / 2 |
| canonical game | **1 created**, `official_key_exact` 1.00 accepted |
| provider game link | linked |
| orientation | home `tm_nba_det` (provider 9), away `tm_nba_nyk` (provider 20) |
| season | `sn_nba_2025_regular` |
| `game_date_local` | `2026-01-05` (provider date, unchanged) |
| provider players linked | 35 / 35 |

### 3.4.6 The scheduled-start contract (NBA)

`scheduled_start` is derived from the provider's `datetime` field and is
**independent of `mapped_status`**. Status and scheduled start are separate
facts; a final status does not un-schedule a game. Precedence:

1. a valid provider `datetime` — authoritative for every status (scheduled,
   in-progress, final, delayed, postponed, suspended);
2. a valid full ISO datetime in the legacy `status` field, **only** when
   `datetime` is absent;
3. otherwise `None`.

A value must be a nonempty string parsing as a complete ISO-8601 instant with an
explicit offset or `Z`, normalized to the repository's canonical UTC form. Refused
outright: a timezone-naive value (never assumed UTC — a wrong guess shifts the
venue-local date), a bare calendar date, display text such as `"7:00 pm ET"`,
receipt time, and anything inferred from play, box-score, result or betting data.
A present-but-unusable `datetime` does **not** fall through to the legacy field —
a broken authoritative value must surface, not be papered over — and raises a
sanitized `DQ-NBA-SCHEDULE-001` carrying only the provider game id and a generic
reason. A genuinely absent value is silent, not a finding.

`game_date_local` remains the provider's `date`. A `date`/`datetime` pair more
than one calendar day apart cannot be a timezone rollover (every venue offset lies
within UTC−12..+14), so both values are preserved as supplied and
`DQ-NBA-SCHEDULE-002` is raised rather than either being corrected.

### 3.4.5a F1 status: what one game does and does not establish

The one-game replays in 3.4.4 establish matching **mechanics** for both leagues:
teams resolve to seeded canonical teams, official games canonicalize, and players
bootstrap from the designated official provider. They establish **no** coverage
figure. Identity coverage across an ordinary month -- and therefore any judgement
about the 99% acceptance gate -- requires the bounded season-month pilots, whose
manifests, request-cap derivation, coverage-report contract and execution protocol
live in `pilots/f1/README.md`. Those pilots are **prepared and not executed**; F1
is **incomplete** and F2 is **unauthorized**.

### 3.4.5 Decision-history idempotency

The pilot also showed that rerunning unresolved references appended a
byte-equivalent refusal every time, doubling the audit log with no new
information. `record_unresolved_decision` now skips a semantically identical
replay. The rule is deliberately narrow:

* it applies only when `outcome != 'accepted'` — an accepted decision justifies a
  canonical link and is never deduplicated;
* only the **latest** prior decision for that source reference is compared, so a
  genuine A to B to A sequence still records all three attempts;
* the comparison covers every semantic field, the full ordered candidate set and
  `raw_response_id`, so a changed identity observation, matcher version, alias
  set, candidate set, score, reason or outcome always appends;
* `run_id` is excluded — it is run bookkeeping, and including it would defeat the
  rule entirely;
* nothing is ever deleted or rewritten.

Three counters make the outcome visible rather than implicit:
`decisions_recorded`, `decisions_replayed`, `decisions_changed`.

**Known residual, not hidden.** Re-running `match-games` over an already-matched
game still appends two *accepted* `exact_provider_id` team re-affirmations per
run (measured: +2 on the second pass and +2 on the third, in both leagues). They
create no canonical entity, no link and no duplicate alias. Deduplicating
accepted decisions touches link provenance and is deliberately left out of this
change; the counters above report the growth rather than concealing it.

---

## 4. Game matching

> **Phase D status (D5A — built, mocked/offline).** Official-game matching is
> **implemented** in `sports_quant/matching/` (D5A); its build note is
> `PHASE_D_IMPLEMENTATION_PLAN.md` §D5. D5A anchors on the official provider (MLB
> StatsAPI `gamePk`, balldontlie game id) via the existing
> `games.official_provider`/`official_game_key` columns and the
> `provider_game_references` crosswalk (no second canonical-game table), resolving
> each official provider game to the canonical `games` row by the official key and
> the schedule key below. **Matching sportsbook events and Kalshi events/markets
> (both ingested with `game_id` NULL) to the canonical game is D5B and is NOT yet
> built.** `game_date_local` is resolved
> by a **venue-aware timezone hierarchy** (`PHASE_D_IMPLEMENTATION_PLAN.md` §5.1):
> (1) the **actual event venue** timezone (neutral/temporary/relocated sites
> included), (2) an official provider-supplied local date/timezone when reliable,
> (3) the **canonical home venue** timezone as a fallback, and (4) the **UTC
> calendar date** only as a last resort — which **lowers the match confidence** and
> writes a `DQ-TZ-001` data-quality note rather than being treated as equivalent to
> a real venue timezone. It is *not* home-venue-only.
>
> **`match_candidates` is a normalized table, not a JSON blob.** Where §7 below
> describes `candidates_json`, Phase D instead stores one `match_candidates` row
> per candidate considered (with its per-candidate score and tier), a child of
> `entity_match_decisions` — mirroring the `kalshi_orderbook_levels` precedent of
> normalized rows over an opaque blob. The intent (every candidate, including the
> losers, is recorded) is unchanged.

The hardest problem here: reconciling an official game, a sportsbook event, and
a Kalshi market that all describe the same contest in different vocabularies.

### 4.1 The schedule key

The structured comparison key:

```
(league_id, game_date_local, home_team_id, away_team_id, game_number)
```

Each component is resolved through team matching first. If either team fails to
resolve, game matching **stops immediately** — a game match built on an
unresolved team is worthless, and continuing would produce a confident-looking
decision resting on a guess.

### 4.2 Matching tiers for games

| Tier | Method | Score | Condition |
| --- | --- | --- | --- |
| 1 | `official_key` | 1.00 | Provider exposes the official game id (Phase D). |
| 2 | `schedule_key_exact` | 0.95 | Both teams resolved, same local date, start within ±90 min. |
| 3 | `schedule_key_window` | 0.88 | Both teams resolved, start within ±12 h (catches date-boundary and postponement drift). |
| 4 | `title_rules` | 0.85 | Kalshi only — parsed title/rules (§6). |

The ±90-minute tolerance in tier 2 accommodates ordinary start-time drift
(TV windows, rain delays announced pre-start). The ±12-hour window in tier 3
exists to catch the case where a game listed as "Tuesday 7pm ET" is a Wednesday
00:00 UTC event — a pure timezone artifact that would otherwise look like a
different game.

### 4.3 The hard cases

**Neutral-site games.** `games.is_neutral_site = 1`. Providers disagree about
which team is "home" at a neutral site (MLB London Series, NBA Paris Games,
Mexico City). When `is_neutral_site = 1`, the matcher additionally attempts the
**team-swapped** schedule key. If the swapped key matches, the match is accepted
with `method = 'schedule_key_swapped'`, score `0.85`, and
`needs_manual_review = 1` — accepted so ingestion proceeds, flagged because the
home/away orientation determines the sign of every price and must be confirmed
by a human once.

**Postponed games.** The game keeps its `game_id` and `official_game_key`;
`games.scheduled_start` is updated and a `game_status_history` row is appended
with `status = 'postponed'`. `original_start` never changes. Sportsbook events
for the postponed game usually vanish and reappear with a new provider id — this
is why `sportsbook_events` carries its own surrogate `sbe_` id and links to
`games` by an explicit match decision rather than by identity.

**Rescheduled games.** The new date changes `game_date_local`, so tier 2 no
longer matches the old row. Tier 1 (official key) still does. Without an
official provider, a reschedule appears as `no_candidate` and lands in manual
review — correct behaviour, since automatically merging a game played on a
different date is exactly the kind of confident-and-wrong join this design is
built to avoid.

**MLB doubleheaders.** The single most error-prone case in baseball data.
`(league, date, home, away)` matches two games. Resolution order:

1. If the provider supplies a game number / `gamePk` suffix, use it (tier 1).
2. Otherwise use scheduled start times: the earlier event maps to
   `game_number = 1`, the later to `game_number = 2`, but **only if the two
   starts differ by at least 90 minutes**.
3. Otherwise — two same-day games with indistinguishable start times —
   `AMBIGUOUS`, both candidates recorded, manual review.

> **D5A implementation: batch-order-independent, grouped by the resolved slate.**
> The inferred number in rule 2 is the game's chronological rank computed from the
> **whole schedule corpus** (the latest observation of every provider game sharing
> the same provider and provider home/away team ids), ranked by
> `(scheduled_start, provider_game_id)` — never from whichever canonical sibling
> was created first, and never by sorting on the provider-game id. Siblings are
> grouped by the **resolved venue-local date**: each candidate's local date is
> derived through the same venue-aware hierarchy (actual venue tz → provider local
> date → knowledge-bounded home-venue tz → UTC), so a **missing provider local
> date does not collapse the game to a single-game slate** and a cross-midnight UTC
> start still lands on the correct local slate. When choosing each sibling's latest
> observation, ties on `observed_at` break on `schedule_id` (a creation-ordered
> ULID), never on SQLite row order. The result is identical regardless of insertion
> order, processing order, or which sibling is presented first (verified under 100
> randomized orders); a bounded run that processes only the later game still
> numbers it `2` because the earlier sibling is visible in the source schedule; a
> provider-supplied game number always overrides. Under rule 3, **both** games are
> ambiguous — neither is arbitrarily created as game 1.
>
> **Season intervals (`matching/season.py`).** Roster-team and career-window
> filtering use the providers' own season convention: **MLB** `season` is the
> calendar year (`[Y-01-01, Y-12-31]`); **BALLDONTLIE NBA** `season` is the start
> year of a two-year season (`2024` = the 2024-25 season, `[Y-07-01, (Y+1)-06-30]`).
> A single helper feeds both filters, so they never disagree about which season a
> `roster_date` belongs to; an undated roster is never season-proven evidence, and
> conflicting teams within the applicable season omit the team tier rather than
> choosing by row order.

Split doubleheaders (separate admissions, typically ~5 h apart) resolve cleanly
under rule 2. Traditional doubleheaders (second game ~30 min after the first
ends, start time often listed identically or as TBD) frequently hit rule 3, and
that is the correct outcome: guessing which of two games a price refers to is
how a corpus silently acquires mispriced rows.

**Suspended and resumed games.** Appended to `game_status_history` as
`suspended` then `in_progress`. The game retains one `game_id`; a resumption is
never a new game.

---

## 5. Sportsbook event matching

> **D5B1 status — built (mocked/offline).** Sportsbook-event matching is
> implemented in `sports_quant/matching/sportsbook.py`. Already-ingested The Odds
> API `sportsbook_events` resolve to canonical `games` using provider-scoped
> (`the_odds_api`) team aliases and deterministic schedule/time evidence only —
> **never** a price, implied probability, bookmaker count, final score, or settled
> outcome (the module does not import or read `sportsbook_price_snapshots`). Tiers:
> `schedule_key_exact` 0.95 (±90 min, direct), `schedule_key_window` 0.88 (±12 h,
> direct), `schedule_key_swapped` 0.85 (neutral-site only, review-gated,
> `DQ-MATCH-007`). A non-neutral reversed orientation is blocking (`DQ-MATCH-003`);
> the provider event id never gets an official-key tier. Migration `d015` adds
> `sportsbook_events.match_decision_id` (the exact accepted decision) and a typed
> `orientation` (`direct`/`swapped`) — a neutral swapped match is never
> orientation-approved pricing data (`is_orientation_approved()` requires
> `orientation = 'direct'`, so a swapped event is **always excluded**; see the
> review note below on the absence of an approval workflow). Existing
> `market_key`/`outcome_role` are validated against the
> accepted orientation; unknown/malformed outcomes are retained and surfaced via
> `DQ-SB-OUTCOME-001`, never dropped or rewritten. Kalshi event/game-winner
> market matching is now built (§6, D5B2).
>
> **D5B1 correctness repairs.** The season passed to the team resolver now uses
> the league-specific convention (`matching/season.py::season_year_for`): an NBA
> January–June date maps to the *previous* start year, so 2025-26 aliases resolve
> an April-2026 event; a naive/unparseable commence never produces a guessed
> season. Local-slate agreement is a **real candidate requirement**, not just a
> confidence hint: for each league/team/UTC-window candidate the event's
> candidate-relative local date is derived (candidate's actual event-venue tz when
> that venue evidence was known by `last_observed_at` → knowledge-time-valid home
> venue tz → UTC last resort) and must equal the candidate's `game_date_local`;
> contradictory candidates are excluded, an unresolvable timezone is surfaced
> (`DQ-TZ-001`) rather than forced to UTC, and only a genuine UTC fallback keeps a
> candidate (capped 0.88). A blocking orientation conflict now **prevents linking
> entirely** — no accepted decision, no `game_id`/`match_decision_id`/`orientation`,
> no outcome validation, exit 1 — instead of linking first and discovering the
> conflict after. `is_orientation_approved()` is fail-closed: it also requires
> decision↔link agreement, no other event linked to the game under a different
> orientation, and no unresolved blocking identity/orientation DQ on the event.
> Outcome roles are recomputed provider-side from the immutable names + market
> (never trusting the stored `outcome_role`); disagreements are surfaced scoped to
> the outcome and never approved or rewritten. DQ issues are scoped to the
> narrowest entity — event / `sportsbook_market` / `sportsbook_outcome` — and are
> idempotent per `(rule, entity, provider, description)`, so two distinct defects
> stay independently visible.
>
> **D5B1 independent-review repairs.** The accepted decision and its link are one
> atomic unit: before recording an accepted decision the matcher inspects this
> event's own current link — an exact idempotent replay (same game, same
> orientation, decision that is accepted, belongs to this event, and names this
> game) is recognized and records **no** new decision (counter
> `events_already_linked`); any other existing link (different game/orientation,
> or a corrupt decision) is a blocking rejection with no fresh accepted row; a
> `link_game` result other than a clean `LINKED` in the fresh path raises and
> rolls the whole attempt back (exit 1) rather than leaving an accepted decision
> unlinked. **UTC fallback (Policy A):** a UTC-only candidate is kept only when
> the UTC date equals the canonical `game_date_local` (reduced-confidence 0.88 +
> `DQ-TZ-001`); a cross-midnight game without timezone evidence is excluded, not
> matched on instant proximity. **Venue-association knowledge time:** the actual
> event-venue tier requires BOTH the venue entity (`venues.first_observed_at`) and
> the game's venue association (an accepted non-swapped `game` decision with
> `decided_at`) to have been known by the event's `last_observed_at`, so a venue
> learned/attached later cannot leak backward. **As-of readiness** evaluates DQ
> `detected_at`/`resolved_at` and any conflicting event's decision `decided_at`
> relative to the cutoff (a later detection/conflict does not block an earlier
> cutoff; an issue active at the cutoff blocks even if resolved later). **Outcome
> approval** is gated on the real `is_orientation_approved` check, not the
> orientation argument, and runs only after a verified link. **Unsupported market
> keys** cannot be ingested (the `sportsbook_markets.market_key` CHECK allows only
> `h2h`/`spreads`/`totals`); the matcher additionally never approves roles for any
> other key defensively. **Market shape** is judged per betting contract (totals
> grouped by point, spreads by `abs(point)`, h2h single), so alternate lines do
> not raise false duplicate-side findings. **Neutral swapped review:** there is
> **no implemented workflow** that promotes a swapped event to orientation-approved
> — `is_orientation_approved` requires `direct`, so swapped events remain excluded
> from price-safe use indefinitely; `mark_reviewed` records review bookkeeping only
> and does not grant orientation approval. **Provenance:** the decision/DQ
> `raw_response_id` is the event's immutable first-observation response; schema v15
> records no per-field current-supplying observation, so it is honest
> first-observation provenance, not a claim about which later re-poll supplied the
> current mutable commence/team metadata.

Inputs from The Odds API (already normalized by the existing
`sports_quant/providers/odds_api.py`): `id`, `sport_key`, `commence_time`,
`home_team`, `away_team`.

Procedure:

1. `sport_key` → `league_id` (static map: `baseball_mlb` → `lg_mlb`,
   `basketball_nba` → `lg_nba`).
2. `home_team` / `away_team` → `team_id` via §3, scoped
   `provider = 'the_odds_api'`, under the **league-specific season** for the
   commence date (`season_year_for`; NBA Jan–June → previous start year). A
   naive/unparseable commence stops the attempt (`no_candidate`) — no guessed
   season. Unresolved team ⇒ stop, `no_candidate`.
3. Generate candidates by league + team + bounded UTC window, then require each
   candidate to share the event's **resolved local slate**. The event's
   candidate-relative local date comes from the venue-aware timezone hierarchy
   (`PHASE_D_IMPLEMENTATION_PLAN.md` §5.1): the candidate's actual event venue tz
   (only when that venue evidence was known by `last_observed_at`) → canonical
   home venue tz → UTC date (last resort, which caps confidence at 0.88 and writes
   `DQ-TZ-001`). A candidate whose `game_date_local` contradicts the derived date
   is excluded; an unresolvable timezone is surfaced honestly, never forced to
   UTC. A 7pm PT game is 02:00 UTC the following day, so the slate is validated by
   venue, not by the UTC calendar date.
4. Apply game tiers §4.2 to the slate-consistent candidates.
5. Before recording an accepted decision, reject a blocking orientation conflict
   with an event already linked to the candidate game (no link, exit 1). Otherwise
   persist the decision; on acceptance set `sportsbook_events.game_id`,
   `match_decision_id`, and `orientation`, then revalidate outcome roles.

The Odds API's `home_team` field is authoritative for orientation except at
neutral sites, where §4.3 applies.

Markets and outcomes are matched structurally rather than by name:
`market_key` (`h2h` / `spreads` / `totals`) is a provider enum, and
`outcome_name` maps to `outcome_role` by comparing against the resolved team
names (`home` / `away`) or the literals `Over` / `Under`. An outcome name that
matches neither is recorded with `outcome_role = 'unknown'` and raises a
data-quality issue rather than being dropped — a silently dropped outcome is
missing data nobody notices.

---

## 6. Kalshi market matching

> **D5B2 status — built; mocked/offline plus a bounded public-contract audit and
> parser smoke.** Deterministic Kalshi event and
> supported **game-winner** market matching is implemented in
> `sports_quant/matching/kalshi.py` with pure, versioned parsers in
> `matching/kalshi_parse.py`. Already-ingested public MLB/NBA Kalshi events are
> matched through an **exact series allowlist** (`KXMLBGAME`/`KXNBAGAME`; no
> prefix guessing, no `category=Sports` catch-all), **series-specific versioned
> ticker parsers** (MLB `kmlb-2`: `KXMLBGAME-{YYMONDD}{HHMM}{AWAY}{HOME}` with a
> venue-local `HHMM` clock; NBA `knba-1`: `KXNBAGAME-{YYMONDD}{AWAY}{HOME}`,
> date-only) dispatched by exact series ticker, split against curated
> `kalshi_public` alias codes; market `{EVENT}-{SUBJECT}`), provider-scoped team
> aliases, explicit
> title/sub-title and `rules_primary` team/Yes agreement, and venue-aware
> canonical schedule evidence. Migration **d016** (schema v16) adds
> `kalshi_events.match_decision_id` and `kalshi_markets.match_decision_id` +
> `yes_team_id` + `matched_rules_hash` + typed `market_semantic` (`game_winner`),
> paired with `game_id` and non-regressing. **Only binary game-winner markets are
> linked automatically**; spreads, totals, player props, team totals, period, and
> multivariate markets are retained and reported as *unsupported semantics*, never
> mislabeled. Every accepted market stores its exact game, exact decision,
> canonical Yes team, semantic, and matched `rules_hash`; a later rules change
> invalidates readiness through a blocking `DQ-MATCH-004` and never silently
> retains approval (`is_kalshi_market_orientation_approved()` is fail-closed and
> as-of correct). Kalshi order books, trades, prices, `result`, and settlement are
> never game-match or Yes-team evidence (an SQL-trace test proves matching never
> queries the book/level/trade tables). `match-markets` is local and network-free;
> dry-run persists nothing. Event/market links are atomic via
> `matching/linkatomic.py`. **No authenticated Kalshi access, account/order
> surface, live request, or Phase E work is performed.**
>
> The Phase-C ingestion note still holds for the price/book/trade tables: those
> remain append-only public data, untouched by matching.
>
> **D5B2 repair (atomicity / replay / historical readiness / title / No-side /
> review-state).** A clean event or market accept records the decision and
> applies + verifies its link (for a market: `game_id`, decision, `yes_team_id`,
> `matched_rules_hash`, `market_semantic`) inside **one transaction owned by the
> service**, so a direct persisted `KalshiMatchingService` call is atomic on its
> own — a failed link rolls the decision and candidates back and accepted/linked
> counters increment only after commit. `ALREADY_LINKED` replay now requires the
> full existing link to be valid (same game/Yes/hash/semantic, the exact decision
> owned by this event/market, accepted, matching entity, and **not**
> review-gated); a matching game id alone or a corrupt pairing is blocking, never
> idempotent. Rules-hash readiness distinguishes **current** (compares today's
> `rules_hash` to `matched_rules_hash` and fails closed on a change or review
> flag) from **historical** (`as_of` reads rely only on the decision existing by
> the cutoff and the DQ `detected_at`/`resolved_at` timeline — never today's
> mutable hash or review flag — so a later rules change cannot retroactively
> rewrite an earlier answer). Title orientation is honoured: an `A at B` title
> must match the ticker's away/home (a reversed `at` is rejected + review-flagged);
> `A vs B` stays an unordered set match. When `no_sub_title` is present it must
> resolve to the **Yes-subject team** — the audited current public Kalshi contract
> sets `no_sub_title == yes_sub_title` for both KXMLBGAME and KXNBAGAME game-winner
> markets — so an opposing-team, unrelated, or unresolved value is rejected as a
> contract disagreement. When absent, both binary participants are already proven
> by the authoritative ticker + rules. An
> automated rules-hash invalidation flags the
> decision for review via `flag_for_review` (sets `needs_manual_review=1`, leaves
> `reviewed_by`/`reviewed_at` NULL) — it is never recorded as a completed human
> review; `mark_reviewed` remains reserved for an audited reviewer.
>
> **D5B2 live public-contract repair (current MLB/NBA shapes).** The mocked
> single-ticker contract was replaced with **series-specific versioned parsers**
> after a bounded, GET-only, unauthenticated public audit (3 requests) plus a
> controlled live parser smoke (2 requests; 5 of a 6-request budget, persists
> nothing). Verified current public shapes:
> * **MLB** `KXMLBGAME` (`kmlb-2`): `KXMLBGAME-{YYMONDD}{HHMM}{AWAY}{HOME}` — the
>   `HHMM` is a **venue-local wall clock**, not UTC; title `A vs B`; rules
>   `If {Yes} wins the {A} vs {B} professional baseball game originally scheduled
>   for {Mon D, YYYY} at {H:MM AM/PM TZ}, then …`.
> * **NBA** `KXNBAGAME` (`knba-1`): `KXNBAGAME-{YYMONDD}{AWAY}{HOME}` — date-only
>   (any time segment is rejected); title `[Game N: ]A at B`; rules date-only.
>
> The live smoke confirmed **20/20** open MLB events parse under `kmlb-2`
> (structural ticker, present clock, title, and rules template); NBA had **no**
> open events at smoke time (offseason), so its live evidence rests on the audit
> and sanitized fixtures (`matching/tests/kalshi_fixtures.py`). Parser versions
> are golden-pinned in tests so a future provider change breaks loudly instead of
> silently mis-parsing.
>
> **Time-safe MLB clock → UTC (task §4/§6).** The MLB `HHMM` is converted
> **per candidate** through that candidate's actual event-venue `zoneinfo`
> timezone (knowledge-time gated), never the machine tz, UTC, or a fixed offset.
> Conversion refuses (never guesses) on an unknown zone, a DST fold-ambiguous or
> non-existent local time, or a rules timezone abbreviation (`EDT`/`CDT`/`MDT`/
> `PDT`) that disagrees with the venue zone's abbreviation at that instant. The
> resulting UTC instant must fall within ±90 min of the canonical start on the
> same venue-local slate to earn the strongest tier (`kalshi_ticker_time`, 0.97);
> with no usable venue evidence or no ticker clock (NBA date-only) it falls back
> conservatively to a date-only slate (`kalshi_date`, 0.92). The ticker clock and
> a rules clock, when both present, must agree or the market is rejected.
>
> **D5B2 independent-review repair (uniform rules-hash invalidation).** For an
> already-linked market, a current `rules_hash` differing from the accepted
> decision's `matched_rules_hash` now raises the blocking `DQ-MATCH-004` and flags
> that decision for review **before** any parse-dependent rejection, so the
> invalidation is uniform whether the changed rules still parse, now *disagree*
> (e.g. the Yes team flips), or no longer parse at all. Previously only a benign,
> still-agreeing hash change reached the `DQ-MATCH-004` path (in `_accept_market`),
> while a disagreeing/unparseable change was rejected at `_resolve_yes_and_rules`
> with only a non-blocking `DQ-KAL-RULES-001` — which historical (`as_of`)
> readiness does not honour, leaving a stale orientation wrongly approved as-of a
> post-change cutoff. The matched hash and link are never rewritten;
> `flag_for_review` sets `needs_manual_review=1` and leaves `reviewed_by`/
> `reviewed_at` NULL.
>
> **D5B2 final parser-validation repair (calendar / title+sub-title / `no_sub_title`).**
> Three narrowly-scoped correctness fixes: **(1) Real calendar validation.** Both
> the ticker (`_parse_date_code`) and natural-language rules (`_parse_nl_date`)
> dates now share one `datetime.date` validator, so leap days are accepted only in
> leap years (Feb 29 2024 ok, 2025 rejected; ticker `24FEB29` ok, `25FEB29`
> rejected), and April 31 / June 31 / Feb 30 / day 0 / impossible months are
> rejected — replacing a hand-kept days-per-month table that hard-coded Feb=29 for
> every year. The two-digit ticker year keeps its documented 2000–2099 reading.
> **(2) Independent title *and* sub-title validation.** `_title_agrees` now checks
> **every supplied pair-identity field** (the event title *and* its pair sub-title,
> e.g. the audited `AZ vs PIT (Jul 27)` — a trailing decorative parenthetical is
> stripped) against the ticker; each ordered `A at B` must match away/home and each
> unordered `A vs B` must match the team set. A supplied field that is malformed or
> conflicts is reviewable — one valid field can no longer hide a conflicting or
> malformed second field — and the decision evidence records which fields were
> supplied and each kind. The market `subtitle`/`yes_sub_title`/`no_sub_title` are
> single-team Yes/No labels (audited), validated by the Yes-orientation logic, never
> as a pair. **(3) Exact `no_sub_title` convention.** The bounded audit found that
> both KXMLBGAME and KXNBAGAME game-winner markets set `no_sub_title` equal to the
> **Yes-subject** team (`no_sub_title == yes_sub_title`), so a supplied
> `no_sub_title` must resolve to the ticker Yes team; an opposing-team, unrelated,
> or unresolved value is a contract disagreement and is rejected (no "either
> participant" shortcut). Absent `no_sub_title` stays acceptable because the ticker
> + authoritative rules already prove both binary participants; `yes_sub_title`,
> `no_sub_title`, the market ticker suffix, and the rules Yes subject must all be
> mutually consistent.

Hardest of the three, because Kalshi identifies markets by ticker and prose
rather than by structured team fields.

Inputs: `event_ticker`, `series_ticker`, `title`, `sub_title`,
`yes_sub_title`, `rules_primary`, `close_time`.

Procedure:

1. **Series filter.** Only sports series for MLB/NBA are considered; everything
   else is skipped with `outcome = 'no_candidate'`,
   `rejection_reason = 'non-sports series'`. No review flag — this is the
   overwhelming majority of Kalshi's surface and flagging it would drown the
   review queue.
2. **Ticker parse.** Kalshi tickers are structured (league, date, team codes).
   Parsing is attempted with **explicit, versioned patterns per series**, never a
   generic regex. A ticker that does not match a known pattern is `no_candidate`
   with review, not a best-effort parse.
3. **Title/subtitle team extraction.** Extracted team strings resolve through
   §3 with `provider = 'kalshi'`.
4. **Rules cross-check.** `rules_primary` is parsed for the settlement subject.
   A market whose title suggests one game but whose rules name another is
   **rejected** — `rejection_reason = 'title/rules disagreement'`, review
   flagged. Rules text is authoritative because it is what actually settles.
5. **Date/time resolution.** The provider calendar date from the ticker fixes
   the venue-local slate; for MLB the ticker `HHMM` venue-local clock is converted
   to UTC through each candidate's venue timezone (see the D5B2 time-safety note
   above) for the exact-time tier. `close_time` is never treated as the scheduled
   start.

**`rules_hash` is load-bearing.** `kalshi_markets.rules_hash` is the SHA-256 of
the settlement rules. If a market's rules change after a match was accepted, the
match's premise has changed: the ingestor detects the hash change, appends a
new market observation, sets `needs_manual_review = 1` on the existing decision,
and raises `DQ-MATCH-004`. Silently keeping a match across a rules change is
how a market ends up mapped to a contest it no longer settles on.

**Orientation.** Kalshi binary markets resolve Yes/No against a specific
subject ("Will the Yankees win?"). The matcher records which canonical team the
Yes side refers to in the market's decision payload. Getting this backwards
inverts every derived probability, so it is recorded explicitly rather than
inferred at read time — and a market whose Yes subject cannot be determined is
rejected, not defaulted to home.

---

## 7. What is recorded for every decision

Every attempt — accepted, rejected, ambiguous, or no-candidate — writes exactly
one `entity_match_decisions` row. There is no code path that resolves an entity
without recording why.

| Requirement | Column |
| --- | --- |
| Candidates considered | `candidates_json` — **all** candidates with per-candidate scores and the tier that produced them |
| Matching method | `method` |
| Match score | `score`, with `threshold` alongside |
| Accepted or rejected | `outcome` |
| Rejection reason | `rejection_reason` (schema-required unless accepted) |
| Manual-review flag | `needs_manual_review`, `reviewed_by`, `reviewed_at` |

Plus `matcher_version`, so a decision made by an older matcher is identifiable
after the rules change, and `decided_at`, which bounds point-in-time visibility
(`POINT_IN_TIME_DATA.md`, DQ-PIT-010).

`candidates_json` stores the losers deliberately. When a match is wrong, the
question is always "what else was on the table, and why did this score higher?"
— unanswerable from the winner alone.

### 7.1 Review workflow

`data-quality --review` lists open items grouped by `entity_type` and
`rejection_reason`, most-frequent first, since one missing alias typically
explains dozens of failures. Resolution is normally *adding an alias row*, not
editing a decision: the alias is the durable fix, and re-running the matcher
then resolves every affected row identically and reproducibly.

A `manual_override` outcome exists for genuinely one-off cases and requires
`reviewed_by` to be set. It is the only way a human judgement enters the corpus,
and it is auditable.

---

## 8. Match-quality rules

Surfaced by `data-quality`:

| Code | Condition | Severity |
| --- | --- | --- |
| `DQ-MATCH-001` | Sportsbook event unmatched > 24 h after `commence_time` | issue |
| `DQ-MATCH-002` | Kalshi sports market unmatched at `close_time` | issue |
| `DQ-MATCH-003` | Two providers' events matched to the same game with conflicting orientation | blocking |
| `DQ-MATCH-004` | `rules_hash` changed after acceptance | blocking |
| `DQ-MATCH-005` | Ambiguous decisions pending review > 7 days | note |
| `DQ-MATCH-006` | Team alias resolved via `is_ambiguous` row | blocking |
| `DQ-MATCH-007` | Neutral-site swapped match unreviewed | issue |

`DQ-MATCH-003` and `DQ-MATCH-006` are blocking because both silently invert the
sign of a position. A corpus containing either is not fit for research, and the
`data-quality` command exits non-zero.

---

## 9. Testing

| Layer | Content | Status |
| --- | --- | --- |
| **Normalization golden file** | Fixed input corpus (accents, punctuation, suffixes, `&`, all-initial abbreviations) with pinned outputs. Any change shows as a reviewable diff. | ✅ `db/tests/test_normalize.py` |
| **Determinism** | Normalization is stable across repeated calls; resolution is order-independent under reversed candidate lists. | ✅ Phase A; extended to 100× shuffles in Phase D |
| **Ambiguity refusal** | Two Jalen Williamses, shared cities, generational collisions — each asserts `AMBIGUOUS`, never a match. | ✅ Phase A (unit and through the database) |
| **Suffix binding** | A present suffix is binding (`Guerrero Jr.` never resolves to the father); an absent one is permissive unless both generations exist. | ✅ Phase A |
| **Season scoping** | A curated alias resolves inside its window and is excluded outside it (boundaries inclusive); an unbounded alias reports `season_validity_verified=False`. | ✅ Phase A (`test_season_scoped_aliases.py`) |
| **League consistency** | An alias whose `league_id` disagrees with its team/player is rejected by the database on INSERT and UPDATE. | ✅ `a003` (`test_integrity_guards.py`) |
| **Hard cases** | One fixture per §4.3 case: neutral site, postponement, reschedule, both doubleheader types, suspension. | Postponement/reschedule/doubleheader ✅ Phase A; the rest ◻ Phase D |
| **Rules disagreement** | Kalshi market whose title and rules name different games ⇒ rejected. | ◻ Phase D |
| **Decision completeness** | Property test: every matcher invocation writes exactly one decision row, and accepted rows always name an entity. | ◻ Phase D (`entity_match_decisions` is a Phase D table) |

The determinism test is the one that earns its keep. Non-determinism from
iteration order is invisible in a single run and produces a corpus that cannot
be rebuilt identically — which undermines every downstream reproducibility
claim.
