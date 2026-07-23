# Phase D — Provider Decisions

Provider evaluation and selection for the official-data + matching phase of the
read-only MLB/NBA betting **recommendation** engine.

> **Status: Provider selection and implementation design complete; implementation
> not started.** No account was created, no subscription purchased, and no live
> provider call was made while writing this document. Every capability claim
> below must be re-verified against the provider's live documentation at
> implementation time (see *Verification obligations*, §7).

Companion: `PHASE_D_IMPLEMENTATION_PLAN.md` (schema, migrations, CLI, staging).

---

## 0. Constraints carried forward (unchanged)

Phase D is **additive** and inherits every rule in `CLAUDE.md` and
`READ_ONLY_ARCHITECTURE.md`:

- **GET-only, read-only.** Every provider request is forced through
  `sports_quant.http_policy`. Phase D extends the host/path allow-list; it never
  relaxes the method rule (`GET` only) or touches an order/account surface.
- **No scraping as a default.** HTML scraping is not a selected mechanism for any
  category. One provider (the official NBA injury report) is a **PDF the league
  publishes**; parsing an official publication is treated distinctly from
  scraping a third-party site, and its risk is labelled (§3, NBA injuries).
- **Undocumented ≠ automatically acceptable.** MLB StatsAPI and stats.nba.com are
  undocumented public JSON endpoints served by the leagues. They are widely used
  but their terms are ambiguous for analytics. Where selected, the risk is
  **labelled explicitly**, never assumed away, and a licensed paid alternative is
  named for any deployment beyond personal analytics.
- **No credential in code, tests, docs, stored URLs, logs, raw-response metadata,
  or CI.** Keys load only from `.env` as `SecretStr`, exactly like `ODDS_API_KEY`.
- The Odds API key supplies **sportsbook pricing only** — never game statistics.
  The Kalshi public API supplies **prediction-market data only** — never official
  sports statistics. Phase D adds genuinely new providers for official data.

---

## 1. Data categories and what the corpus already has

| Category | Needed for | Already in corpus? |
| --- | --- | --- |
| MLB/NBA schedules & game status | canonical games, PIT status timeline | **Partially** — `games` + `game_status_history` exist (canonical, append-only) but are unpopulated; Phase D fills them from an official provider |
| MLB/NBA game results & scores | labels, evaluation | No |
| Inning (MLB) / quarter (NBA) lines | in-game state features | No |
| Team & player box statistics | features | No |
| MLB probable / confirmed starting pitchers | largest pregame MLB signal | No (the `intel/` lane models the *concept* in memory; Phase D persists it) |
| MLB starting lineups | features | No |
| NBA injuries / availability | largest pregame NBA signal | No |
| NBA starters / lineups | features | No (honest limitation — see §3) |
| Rosters / transactions | player matching, eligibility | No |
| Venues (coords, roof, timezone) | weather join, doubleheader/date resolution | No (`games.venue` is free text only) |
| Weather (forecast + actual) | outdoor-MLB features | No |
| Official↔sportsbook↔Kalshi matching | joins across lanes | Designed in `ENTITY_MATCHING.md`; tables not built |
| Provider player/team/game id crosswalks | stable identity | No |

**Reused canonical entities (never duplicated):** `leagues`, `seasons`, `teams`,
`team_aliases`, `players`, `player_aliases`, `games`, `game_status_history`.
There is **no second canonical-game table**; official provider ids attach to the
existing `games.official_provider` / `games.official_game_key` columns (already
present, `UNIQUE`, nullable) and to a new `provider_game_references` crosswalk.

---

## 2. Providers evaluated (dossiers)

Each dossier records the fields the task requires. "Terms" reflects a good-faith
reading of publicly stated terms; it is **not legal advice** and must be
re-confirmed at implementation time.

### 2.1 MLB — MLB Stats API (`statsapi.mlb.com`)  ·  candidate: PRIMARY

| Field | Finding |
| --- | --- |
| Official name | MLB Stats API ("StatsAPI") |
| Docs source | No official public developer docs. De-facto reference: `github.com/toddrob99/MLB-StatsAPI` wiki; `github.com/jasonlttl/gameday-api-docs`. Endpoints under `https://statsapi.mlb.com/api/v1/`. |
| Auth / key | **None.** Unauthenticated public JSON. |
| Free / paid | Free. |
| Historical depth | Very deep (box scores and schedules back decades; live feed `feed/live` for modern seasons). |
| Live data | Yes — near-real-time `game/{gamePk}/feed/live`, linescore, boxscore. |
| Rate limits | Not documented; be conservative (single-threaded, low QPS, backoff). |
| Pagination | Mostly date-range params (`?startDate&endDate&sportId=1`); no cursoring. |
| Timestamps supplied | `gameDate` (scheduled UTC), status `codedGameState`/`detailedState`, `dateTime` per play; **no explicit per-record correction timestamp** — corrections appear as changed payloads. |
| Correction behaviour | Live feed is mutable during a game; final boxscore is authoritative. Late stat corrections rewrite the same `gamePk` payload — handled by our append-only snapshots comparing content hashes. |
| Lineup availability | Yes once posted — `boxscore` battingOrder; `feed/live` provides in-game. Pregame projected lineups **not** provided. |
| Injury availability | Limited (`/people/{id}` transactions, IL moves via `/transactions`); no clean injury-status feed. |
| Venue metadata | Yes — `/venues?hydrate=location,fieldInfo` gives lat/lon, `roofType`, timezone-ish city. Strong. |
| Probable pitchers | Yes — `/schedule?hydrate=probablePitcher`. |
| Stable ids | `gamePk` (game), `teamId`, `personId` (MLBAM) — all stable. Excellent for crosswalks. |
| Terms / betting use | MLB.com Terms of Use restrict commercial use / redistribution and do not license programmatic access. **AMBIGUOUS-to-RESTRICTIVE for analytics; likely prohibited for commercial/betting redistribution.** Personal, non-redistributed analytics is a widely-practised gray area. **Risk: LABELLED (high for any commercial use).** |
| Reliability | High availability in practice; undocumented so no SLA and endpoints can change without notice. |
| Major missing fields | Pregame projected lineups; clean injury feed; explicit correction timestamps. |

### 2.2 MLB — Sportradar MLB / Stats Perform  ·  candidate: PROFESSIONAL fallback

| Field | Finding |
| --- | --- |
| Official name | Sportradar MLB API (v7/v8); Stats Perform (Opta/SDAPI) |
| Docs source | `developer.sportradar.com` (documented, versioned). |
| Auth / key | API key. |
| Free / paid | **Paid** (limited trial). Betting use requires a specific licensed tier. |
| Historical depth | Deep via historical add-ons (paid). |
| Correction behaviour | Explicit change/correction feeds with timestamps — best-in-class PIT story. |
| Injuries / lineups | Included (injuries, expected/confirmed lineups) with stable ids. |
| Terms / betting use | **Explicitly licensable for betting** at the right tier. Cleanest licensing. |
| Reliability | Contractual SLA. |
| Downside | Cost; onboarding; overkill for a personal MVP. |

### 2.3 MLB — supporting open datasets (crosswalks / deep history)

- **Chadwick Bureau Register** (`github.com/chadwickbureau/register`) — open CSV
  crosswalk MLBAM ↔ Retrosheet ↔ BBRef ↔ FanGraphs player ids. **Use for player
  matching** (id bridge). Open data, attribution.
- **Retrosheet** — freely available historical play-by-play. License permits
  non-commercial use with attribution; **commercial/betting use restricted** →
  labelled. Optional deep-history backfill only.
- **Baseball Savant / Statcast** (`baseballsavant.mlb.com`) — MLB-served,
  undocumented, same terms posture as StatsAPI. **Not selected** for Phase D
  (Statcast is frame-level tracking → `CLAUDE.md` keeps that an optional adapter).

### 2.4 NBA — balldontlie (`api.balldontlie.io`)  ·  candidate: PRIMARY (documented)

| Field | Finding |
| --- | --- |
| Official name | Ball Don't Lie API |
| Docs source | `docs.balldontlie.io` (documented). |
| Auth / key | **API key required** (as of the 2024 relaunch). |
| Free / paid | **Freemium** — free tier with a key; higher rate limits + `injuries`/advanced on paid tiers. |
| Historical depth | Games back to 1946; player box scores historically more limited on lower tiers. |
| Live data | Limited/paid; not the strength. |
| Rate limits | Per-tier (free tier is low QPS); documented. |
| Pagination | Cursor/`per_page` documented. |
| Timestamps | Game `date`, `status`; no explicit correction timestamps on lower tiers. |
| Injuries / lineups | `injuries` endpoint on paid tiers; **no reliable pregame starters**. |
| Stable ids | balldontlie player/team/game ids (stable within the provider). |
| Terms / betting use | ToS permit analytics/personal use; betting not explicitly blessed → **LABELLED (low-moderate)**. Cleaner than stats.nba.com because it is documented and key-gated. |
| Reliability | Good for a hobby API; no SLA on free tier. |
| Major missing | Pregame starters; rich advanced box on free tier; live during-game. |

### 2.5 NBA — NBA Stats API (`stats.nba.com`)  ·  candidate: FALLBACK (rich, risky)

| Field | Finding |
| --- | --- |
| Official name | NBA Stats API |
| Docs source | Undocumented. De-facto: `github.com/swar/nba_api` wiki. |
| Auth / key | **None**, but requires specific request **headers** (`User-Agent`, `Referer: https://www.nba.com/`, `x-nba-stats-*`) or it returns 403. Frequently **blocks datacenter/CI IPs** and rate-limits aggressively. |
| Free / paid | Free. |
| Historical depth | Very deep (box scores, PBP, advanced). |
| Correction behaviour | Mutable payloads; no correction timestamps. |
| Injuries / lineups | No clean injury feed; starters appear only once the boxscore posts. |
| Terms / betting use | NBA.com ToS restrict; undocumented. **AMBIGUOUS-to-RESTRICTIVE; LABELLED (high for commercial).** Operational risk (403/IP blocks) is real. |
| Reliability | Flaky from servers/CI; better from residential IPs (which we will not special-case). |

### 2.6 NBA — official injury report (league PDF)  ·  candidate: injury SOURCE OF RECORD

| Field | Finding |
| --- | --- |
| Official name | NBA Official Injury Report (league publication, PDF) |
| Docs source | Published ~1h before tip and updated; hosted on official NBA infrastructure (`official.nba.com` / `ak-static.cms.nba.com`). No API. |
| Auth / key | None (public PDF). |
| Format | **PDF** — requires a parser; layout can change → fragile. Not HTML scraping of a third party, but parsing the league's own publication. |
| `published_at` | The report carries its own generated timestamp → strong PIT anchor. |
| Terms / betting use | It is a public official publication; still under NBA.com ToS → **LABELLED (moderate).** |
| Verdict | **Source of record for NBA injuries** in the MVP, *or* mark injuries unavailable (§3). Professional path uses Sportradar/Stats Perform injuries instead. |

### 2.7 NBA — Sportradar NBA / Stats Perform  ·  candidate: PROFESSIONAL fallback

Same posture as MLB (§2.2): documented, keyed, paid, betting-licensable, includes
injuries + confirmed lineups + correction timestamps + stable ids. The clean
professional answer for NBA availability/lineups.

### 2.8 Weather — Open-Meteo (`open-meteo.com`)  ·  candidate: PRIMARY

| Field | Finding |
| --- | --- |
| Official name | Open-Meteo |
| Docs source | `open-meteo.com/en/docs` (well documented). |
| Auth / key | **None** for the free tier. |
| Free / paid | Free for non-commercial; **commercial use requires a paid subscription** → LABELLED for betting-analytics. |
| Historical depth | Archive API (ERA5 reanalysis) back to 1940, hourly. |
| Forecast | Forecast API up to 16 days, hourly. |
| **Point-in-time forecast** | Historical-forecast / "previous model runs" API returns *what the forecast was* at a past time — a genuine leakage-free pregame-forecast source. **Major PIT advantage.** |
| Coordinates | lat/lon based (we supply venue coords). |
| Observation timestamps | Hourly ISO timestamps; UTC. |
| Roof handling | Provider-agnostic; we gate by our own `venues.roof_type`. |
| Terms / betting use | Free tier is non-commercial (CC-BY-4.0 attribution). Commercial → paid. **LABELLED (low for personal, moderate for commercial).** |
| Reliability | Good; no SLA on free tier. |

### 2.9 Weather — NWS / `api.weather.gov`  ·  candidate: FALLBACK (cleanest licensing)

| Field | Finding |
| --- | --- |
| Official name | US National Weather Service API |
| Docs source | `weather.gov/documentation/services-web-api` (documented). |
| Auth / key | **None** (requires a descriptive `User-Agent`). |
| Free / paid | Free; **US-government public-domain data — cleanest licensing of any provider here.** |
| Coverage | US only — covers all US MLB parks; **does not cover Toronto (Rogers Centre)** or international series. Open-Meteo covers those. |
| Historical | Observations available; less convenient long archive than Open-Meteo. |
| Verdict | Fallback / cross-check for US parks; primary if licensing cleanliness is prioritised over convenience. |

### 2.10 Rejected outright

| Provider / method | Reason |
| --- | --- |
| ESPN hidden API (`site.api.espn.com`) | Undocumented **and** ESPN ToS clearly restrict programmatic use/redistribution → not selected. |
| RotoWire / Lineups.com / Rotoworld HTML | Third-party HTML scraping → violates "no scraping as default"; ToS restrict. |
| Any provider requiring account auth for *data* we can't get otherwise | If a data endpoint needs authentication we do not have, report it **unavailable** rather than add credentials (`READ_ONLY_ARCHITECTURE` posture). |
| Twitter/X or social scraping for lineups/injuries | `CLAUDE.md` forbids unauthorized social scraping. The `intel/` lane already models *authorized* beat/social sources separately. |

---

## 3. Selection decision

Ranked by the required priorities: (1) point-in-time correctness, (2) historical
coverage, (3) licensing suitability, (4) data quality, (5) stable identifiers,
(6) correction timestamps, (7) reliability, (8) cost, (9) implementation
complexity. Fewest practical providers preferred.

| Category | **Primary (MVP)** | Fallback | Professional |
| --- | --- | --- | --- |
| MLB schedule/results/lines/box/probables/lineups/venues | **MLB StatsAPI** (no key) — *risk labelled* | balldontlie (MLB not covered — n/a) | Sportradar MLB / Stats Perform (paid, betting-licensed) |
| NBA schedule/results/box | **balldontlie** (free key, documented) | stats.nba.com (no key, header-gated, risky) | Sportradar NBA / Stats Perform |
| NBA quarter lines / PBP | stats.nba.com (fallback) | balldontlie (paid tier) | Sportradar NBA |
| **NBA injuries** | **Official NBA injury report PDF** (source of record) *or mark unavailable* | balldontlie `injuries` (paid) | Sportradar / Stats Perform |
| **NBA starters / lineups** | **No documented free pregame source → explicit unavailable path** | intel/ lane (authorized beat/projection) | Sportradar confirmed lineups |
| MLB probable/confirmed SP + lineups | **MLB StatsAPI** | — | Sportradar |
| Weather (forecast + actual) | **Open-Meteo** (no key; PIT-forecast API) | **NWS** (no key; US-only; cleanest licensing) | Open-Meteo commercial / paid met provider |
| Player id crosswalk | **Chadwick register** (MLB, open); balldontlie ids (NBA) | — | Provider-native ids |
| Venue coords / roof / tz | **MLB StatsAPI `/venues`** + curated seed | NWS point metadata | Sportradar venues |

### Categories with **no acceptable free/clean provider** → explicit unavailable path

- **NBA confirmed pregame starters** — no documented free source before tip.
  Design: `lineup_snapshots` remain empty for NBA on the MVP; a
  `data_quality_issues` note records the gap; the feature builder treats NBA
  starters as *unavailable* (masked), never fabricated. Professional tier fills it.
- **NBA injuries without the PDF parser** — if the PDF parser is deferred, NBA
  injuries are **unavailable** and recorded as such; the model must degrade
  gracefully rather than assume "healthy".
- **Explicit correction timestamps for MLB/NBA (free tier)** — undocumented
  providers give none; corrections are inferred from changed content hashes and
  stamped with our `observed_at`. Professional feeds provide real ones.

**Nothing above fabricates a capability.** Where a provider cannot supply a field,
the field is nullable and a `data_quality_issues` row is written.

---

## 4. Credentials (placeholders only)

New `.env` variables Phase D may require. **Placeholders only — no real values are
read, displayed, or committed.** Each is a `SecretStr` in `sports_quant.config`
and is sanitized out of every stored URL/param/header/log exactly like
`ODDS_API_KEY`.

```
# --- Phase D (official data). Blank placeholders; fill locally in .env only. ---

# NBA schedule/results/box scores via balldontlie (https://www.balldontlie.io).
# REQUIRED for the NBA MVP path. Free tier issues a key.
NBA_DATA_API_KEY=

# MLB StatsAPI needs NO key (unauthenticated public JSON) -> no variable.
# NBA Stats API (stats.nba.com) needs NO key (headers only) -> no variable.
# Open-Meteo free tier needs NO key -> no variable.
# NWS (api.weather.gov) needs NO key (User-Agent only) -> no variable.

# Optional weather key ONLY if a keyed weather provider is chosen instead of
# Open-Meteo/NWS (e.g. Open-Meteo commercial). Leave blank for the no-key path.
WEATHER_API_KEY=

# Optional professional (paid, betting-licensed) providers. Leave blank for MVP.
SPORTRADAR_MLB_API_KEY=
SPORTRADAR_NBA_API_KEY=
```

**No-key providers (explicit):** MLB StatsAPI, stats.nba.com, Open-Meteo (free),
NWS, Chadwick register (static file), NBA injury report PDF. The **only** key the
MVP strictly needs is `NBA_DATA_API_KEY` (balldontlie). Base URLs for the
undocumented league endpoints will be **pinned** in `config.py` (like the Kalshi
URL) so an arbitrary host cannot be substituted.

---

## 5. Cost & coverage report

### 5.1 Implementable with **no paid subscription**

- MLB: schedules, game status, results, inning lines, team/player box, probable &
  confirmed starting pitchers, posted lineups, venues (coords/roof/tz),
  transactions/roster — **MLB StatsAPI (no key)**.
- NBA: schedules, results, team/player box — **balldontlie (free key)**; deeper
  box/quarter/PBP via **stats.nba.com (no key)** as a best-effort fallback.
- Weather: forecast + historical actuals + **leakage-free historical-forecast** —
  **Open-Meteo (no key)**; **NWS (no key)** cross-check for US parks.
- Player id crosswalk (MLB) — **Chadwick (open file)**.
- All matching (official↔sportsbook↔Kalshi, player, team) — pure compute on data
  already ingested; no provider cost.

### 5.2 Requires only a **free API key**

- NBA core data via **balldontlie** (`NBA_DATA_API_KEY`).

### 5.3 Likely requires a **paid plan**

- NBA injuries as a clean feed (balldontlie paid or Sportradar/Stats Perform).
- NBA confirmed pregame starters/lineups (Sportradar/Stats Perform).
- Explicit provider correction timestamps and contractual SLA/licensing suitable
  for **commercial/betting** deployment (Sportradar/Stats Perform, both sports).
- Commercial weather licensing (Open-Meteo commercial) if used beyond personal.

### 5.4 Missing paid data represented as **unavailable** (no model-blocking)

- NBA pregame starters (masked feature; MVP model omits it).
- NBA injuries if the PDF parser is deferred (masked/`data_quality_issues`).
- MLB Statcast/tracking (out of Phase D scope; optional adapter per `CLAUDE.md`).

### 5.5 Missing data that **would prevent a serious model**

- **NBA injuries/availability** is the single largest NBA pregame signal. A
  serious NBA model needs *some* injury source — the official PDF (free, fragile)
  or a paid feed. Without either, NBA recommendations are materially weaker; this
  must be stated to the user, not hidden.
- **MLB probable/confirmed starting pitcher** is the largest MLB pregame signal —
  covered free by StatsAPI, so MLB is in good shape.

### 5.6 Recommended setups

- **Minimum viable (no paid, one free key):** MLB StatsAPI + balldontlie
  (`NBA_DATA_API_KEY`) + Open-Meteo + NWS + Chadwick + NBA injury-report PDF
  parser. NBA starters = unavailable. Licensing risk **labelled** for the
  undocumented league endpoints; suitable for **personal, non-redistributed
  analytics only**.
- **Professional (paid, clean licensing):** Sportradar (or Stats Perform) for
  **both** MLB and NBA (schedules, results, box, injuries, confirmed lineups,
  correction feeds, stable ids, betting licence) + Open-Meteo commercial (or a
  licensed met provider). Removes every "risk: labelled" flag and unlocks NBA
  starters/injuries cleanly.

---

## 6. Major licensing & reliability risks (summary)

| Risk | Severity | Mitigation |
| --- | --- | --- |
| MLB StatsAPI / stats.nba.com terms ambiguous-to-restrictive for analytics; likely prohibited for commercial/betting redistribution | **High for commercial** | Personal, non-redistributed use only on the MVP; professional tier (Sportradar/Stats Perform) for any deployment; risk labelled in-repo and surfaced to the user. |
| Undocumented endpoints can change without notice; stats.nba.com 403s/blocks CI IPs | Medium | Behind adapters (a swap, not a rewrite); mocked tests never hit live; ingestion is capture-forward and resumable; pin base URLs. |
| NBA injury PDF layout drift | Medium | Versioned parser; `data_quality_issues` on parse failure; raw PDF preserved for re-parse. |
| No free NBA pregame starters | Medium | Explicit unavailable path; feature masked, never fabricated. |
| No free explicit correction timestamps | Medium | Infer corrections from content-hash changes; stamp with `observed_at`; professional feed for real timestamps. |
| Open-Meteo free tier is non-commercial | Low–Medium | NWS (public domain) fallback for US parks; commercial tier if needed. |
| Chadwick/Retrosheet commercial-use restrictions | Low | Use Chadwick for id crosswalk only; treat Retrosheet as optional non-commercial deep-history backfill. |

---

## 7. Verification obligations (at implementation time, not now)

Because no live call was made, D1 must, before writing any client:

1. Re-read each selected provider's **current** docs and Terms of Use; re-confirm
   auth, rate limits, pagination, field availability, and the betting/analytics
   permission posture. Update this document if reality differs.
2. Confirm the exact host(s) and path prefixes to add to `http_policy` (GET-only,
   path allow-list), and pin base URLs in `config.py`.
3. Capture real (small, sanitized) response samples as **test fixtures** — never
   live calls in the automated suite.
4. If any provider's terms have moved to *clearly prohibit* this use, **switch to
   the professional/unavailable path** rather than proceed.

---

## 8. Unresolved decisions (for the user)

1. **Personal vs commercial intent.** If this will ever be commercial/redistributed,
   the undocumented league endpoints are not appropriate and a paid licensed
   provider is required from the start. Needs an explicit call.
2. **NBA injuries mechanism.** Official PDF parser (free, fragile) vs paid feed vs
   accept "unavailable" for the MVP.
3. **Weather licensing.** Open-Meteo free (non-commercial) vs NWS (public domain,
   US-only) vs paid.
4. **Deep MLB history.** Whether to backfill pre-StatsAPI seasons via Retrosheet
   (non-commercial) — optional, deferred.
