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

## 4. Game matching

> **Phase D status.** Official-game matching is **planned, not built** — its
> implementation contract is `PHASE_D_IMPLEMENTATION_PLAN.md` §4. Phase D adds the
> official-provider anchor (MLB StatsAPI `gamePk`, balldontlie game id) via the
> existing `games.official_provider`/`official_game_key` columns and a new
> `provider_game_references` crosswalk (no second canonical-game table), then
> matches sportsbook events (Phase B, already ingested with `game_id` NULL) and
> Kalshi events/markets (Phase C, already ingested with `game_id` NULL) to the
> canonical `games` row by the schedule key below. `game_date_local` is resolved
> in the **home venue's timezone** from the new `venues` table.
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

> **Phase B status.** Ingestion is live but matching is **not** — Phase B
> deliberately performs no fuzzy game matching. `sportsbook_events` stores the
> provider's `home_team_raw` / `away_team_raw` verbatim with `game_id` left
> NULL, and `league_id` is set from the static `sport_key` map only. The
> structural parts below that need no name resolution *are* implemented:
> `market_key` is stored as the provider enum, and `outcome_role`
> (`home`/`away`/`over`/`under`/`draw`) is derived by comparing the normalized
> outcome name against the resolved-from-raw team names — an unmatched name is
> stored with `outcome_role = 'unknown'`, never dropped. Steps 1–5 below (team
> resolution, venue-local date, the match tiers, and writing a decision row)
> are Phase D, when `entity_match_decisions` exists.

Inputs from The Odds API (already normalized by the existing
`sports_quant/providers/odds_api.py`): `id`, `sport_key`, `commence_time`,
`home_team`, `away_team`.

Procedure:

1. `sport_key` → `league_id` (static map: `baseball_mlb` → `lg_mlb`,
   `basketball_nba` → `lg_nba`).
2. `home_team` / `away_team` → `team_id` via §3, scoped
   `provider = 'the_odds_api'`. Unresolved team ⇒ stop, `no_candidate`.
3. `commence_time` → `game_date_local` in the **home venue's** timezone (venue
   timezone, not UTC and not the runner's local zone — a 7pm PT game is
   03:00 UTC the following day, and using UTC would place it on the wrong
   slate).
4. Apply game tiers §4.2.
5. Persist the decision; on acceptance set `sportsbook_events.game_id` and
   `match_decision_id`.

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

> **Phase C status.** Kalshi public events, markets, order books, and trades are
> now **ingested** (migration `c007_kalshi`), but matching is **not** performed —
> Phase C deliberately does no fuzzy game matching and infers no sports meaning
> from market text. `kalshi_events`/`kalshi_markets` store the provider's
> `event_ticker` / `market_ticker` as the stable identity with `game_id` left
> NULL, and `rules_hash` is stored but not yet acted upon. The procedure below —
> series filtering, ticker parsing, title/rules cross-check, and writing an
> `entity_match_decisions` row — is **Phase D**, when that table exists.

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
5. **Date resolution** from `close_time` in venue-local terms, then game tiers
   §4.2.

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
