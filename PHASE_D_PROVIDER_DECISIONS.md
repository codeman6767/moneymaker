# Phase D — Provider Decisions

Provider evaluation and selection for the official-data + matching phase of the
read-only MLB/NBA betting **recommendation** engine.

> **Status: Provider selection and implementation design complete; implementation
> not started.** No account was created, no subscription purchased, and no live
> provider call was made while writing this document.
>
> **Documentation-review date: 2026-07-23.** Every capability table below carries
> this date. Because this planning pass made **no live provider call**, the tables
> reflect the providers' stated structure as understood on that date (and, for
> BALLDONTLIE, the tier structure supplied by the project owner). Each table
> **must be re-verified against the provider's live documentation** as the first
> gate of D1 and again by the `provider-audit` command before any backfill
> (§9, `PHASE_D_IMPLEMENTATION_PLAN.md` §10).

Companion: `PHASE_D_IMPLEMENTATION_PLAN.md` (schema, migrations, CLI, staging,
capability system, provider audit).

---

## 0. Constraints carried forward (unchanged)

Phase D is **additive** and inherits every rule in `CLAUDE.md` and
`READ_ONLY_ARCHITECTURE.md`:

- **GET-only, read-only.** Every provider request is forced through
  `sports_quant.http_policy`. Phase D extends the host/path allow-list; it never
  relaxes the method rule (`GET` only) or touches an order/account surface.
- **No scraping as a default.** HTML scraping is not a selected mechanism for any
  category. The official NBA injury report (a league-published PDF) is treated as
  an **optional independent cross-check**, not the primary feed, and its risk is
  labelled.
- **Undocumented ≠ automatically acceptable.** MLB StatsAPI is an undocumented
  public JSON endpoint served by MLB. It is selected as the MLB primary with its
  risk **labelled explicitly**; a licensed paid alternative is named for any
  deployment beyond personal analytics. `stats.nba.com` is **not** in the selected
  stack (demoted to last-resort, §2.6).
- **No credential in code, tests, docs, stored URLs, logs, raw-response metadata,
  or CI.** Keys load only from `.env` as `SecretStr`, exactly like `ODDS_API_KEY`.
- The Odds API key supplies **sportsbook pricing only** — never game statistics.
  The Kalshi public API supplies **prediction-market data only** — never official
  sports statistics. Both are already implemented (Phases B/C) and are **not
  duplicated** by Phase D.
- **Offline research supplements never become live runtime dependencies.**
  pybaseball/Statcast/Baseball Savant/FanGraphs (MLB) and hoopR (NBA) are
  offline-only, imported across a typed export boundary (Parquet); the live
  Python recommendation app never requires R, and never requires those packages
  at startup.

---

## 1. Data categories and what the corpus already has

| Category | Needed for | Already in corpus? |
| --- | --- | --- |
| MLB/NBA schedules & game status | canonical games, PIT status timeline | **Partially** — `games` + `game_status_history` exist (canonical, append-only) but unpopulated; Phase D fills them |
| MLB/NBA game results & scores | labels, evaluation | No |
| MLB inning lines / NBA quarter lines | in-game state features | No |
| Team & player box statistics | features | No |
| MLB probable / confirmed starting pitchers | largest pregame MLB signal | No (the `intel/` lane models the *concept* in memory) |
| MLB starting lineups | features | No |
| NBA injuries / availability | largest pregame NBA signal | No |
| NBA starters / lineups | features | No (**declared unavailable unless a provider observation truly supplies confirmed starters before the cutoff** — §3) |
| NBA play-by-play / substitutions / lineup stints | in-game + research features | No (live via GOAT where available; deep history via hoopR offline) |
| Rosters / transactions | player matching, eligibility | No |
| Venues (coords, roof, timezone) | weather join, venue-aware local date | No (`games.venue` is free text only) |
| Weather (forecast + actual) | outdoor-MLB features | No |
| Official↔sportsbook↔Kalshi matching | joins across lanes | Designed in `ENTITY_MATCHING.md`; tables not built |
| Provider player/team/game id crosswalks | stable identity | No |

**Reused canonical entities (never duplicated):** `leagues`, `seasons`, `teams`,
`team_aliases`, `players`, `player_aliases`, `games`, `game_status_history`.
There is **no second canonical-game table**; official provider ids attach to the
existing `games.official_provider` / `games.official_game_key` columns (already
present, `UNIQUE`, nullable) and to a new `provider_game_references` crosswalk.

---

## 2. Providers evaluated (dossiers) — *doc-review date 2026-07-23; re-verify at D1*

Every "Terms" line is a good-faith reading of publicly stated terms, **not legal
advice**, and must be re-confirmed at implementation time.

### 2.1 MLB — MLB Stats API (`statsapi.mlb.com`)  ·  candidate: **PRIMARY (live/current)**

| Field | Finding |
| --- | --- |
| Official name | MLB Stats API ("StatsAPI") |
| Docs source | No official public developer docs. De-facto reference: `github.com/toddrob99/MLB-StatsAPI` wiki. Base `https://statsapi.mlb.com/api/v1/`. |
| Auth / key | **None** (unauthenticated public JSON). |
| Free / paid | Free. |
| Historical depth | Very deep (schedules/box back decades; live feed for modern seasons). |
| Live data | Yes — near-real-time `game/{gamePk}/feed/live`, linescore, boxscore. |
| Rate limits | Not documented → conservative single-flight + backoff. |
| Pagination | Date-range params (`?startDate&endDate&sportId=1`); no cursoring. |
| Timestamps | `gameDate` (scheduled UTC), status codes, per-play `dateTime`. **No explicit correction timestamp** — corrections appear as changed payloads (we detect via content hash, stamp `observed_at`). |
| Correction behaviour | Live feed mutable during a game; final boxscore authoritative; late stat corrections rewrite the same `gamePk` payload → handled by append-only content-hash snapshots. |
| Lineups | Posted lineups once available (`boxscore` battingOrder / `feed/live`). Pregame projected lineups **not** provided. |
| Injuries | Limited (IL moves via `/transactions`); no clean injury feed. |
| Venue metadata | Strong — `/venues?hydrate=location,fieldInfo` → lat/lon, `roofType`, timezone. |
| Probable pitchers | Yes — `/schedule?hydrate=probablePitcher`. |
| Stable ids | `gamePk`, `teamId`, `personId` (MLBAM) — stable; excellent crosswalk anchors. |
| Terms / betting use | MLB.com ToU restrict commercial use/redistribution; no licensed programmatic access. **AMBIGUOUS-to-RESTRICTIVE; likely prohibited for commercial/betting redistribution. Risk: LABELLED (high for commercial).** Personal, non-redistributed analytics is a widely-practised gray area. |
| Reliability | High availability in practice; **undocumented → no SLA**, endpoints may change without notice. |
| Major missing | Pregame projected lineups; clean injuries; explicit correction timestamps. |

**Documented limitations (must remain in the plan):** undocumented API; no SLA;
no explicit correction timestamp; uncertain terms for livelihood/commercial
betting usage.

### 2.2 MLB — offline research supplements (deferred; not a D1 runtime dependency)

- **pybaseball / Baseball Savant (Statcast) / FanGraphs-derived features** —
  rich pitch-level and advanced metrics. **Offline-only research supplement**,
  imported across a typed Parquet boundary **later**; **not** a required D1/D2
  runtime dependency and **not** added to core `pyproject.toml`. Statcast is
  frame-level tracking → `CLAUDE.md` keeps it an optional adapter. Same
  undocumented/terms posture as StatsAPI → labelled.
- **Chadwick Bureau Register** (`github.com/chadwickbureau/register`) — open CSV
  crosswalk MLBAM ↔ Retrosheet ↔ BBRef ↔ FanGraphs player ids. **Use for MLB
  player matching** (id bridge). Open data, attribution.
- **Retrosheet** — historical play-by-play; non-commercial + attribution;
  commercial/betting restricted → labelled. Optional deep-history backfill only.

### 2.3 MLB — Sportradar MLB / Stats Perform  ·  candidate: **PROFESSIONAL upgrade**

Documented (`developer.sportradar.com`), API-keyed, **paid** (limited trial),
deep history via add-ons, explicit correction feeds with timestamps (best PIT
story), injuries + expected/confirmed lineups, stable ids, **betting-licensable**
at the right tier. The clean answer for any commercial deployment. Kept as a
documented adapter/extension point; **not implemented or purchased now.**

### 2.4 NBA — BALLDONTLIE  ·  candidate: **PRIMARY (live/current) at the GOAT tier**

> **Correction (2026-07-23):** an earlier draft implied a *free* BALLDONTLIE key
> supplies NBA player box statistics. That is **wrong**. BALLDONTLIE is tiered;
> the free tier does **not** include game player statistics, box scores,
> injuries, advanced stats, plays, or lineups. The Phase D NBA path therefore
> targets the **GOAT** tier explicitly. Tier structure below is per the project
> owner's specification and **must be re-verified against `docs.balldontlie.io`
> at D1.**

| Field | Finding (doc-review 2026-07-23; re-verify at D1) |
| --- | --- |
| Official name | Ball Don't Lie API (BALLDONTLIE) |
| Docs source | `docs.balldontlie.io`. |
| Auth / key | **API key required** (`NBA_DATA_API_KEY`). **Key possession does NOT imply GOAT access** — endpoint access depends on the account tier. |
| Tiers | **Free**, **ALL-STAR**, **GOAT** (see the tier table below). |
| Historical depth | Games back decades; richer per-game/box/plays availability increases with tier; treat historical depth of box/plays/lineups as **tier- and provider-history-limited until audited**. |
| Live data | Current/recent games + stats at GOAT; not a play-by-play-latency guarantee. |
| Rate limits | **Free 5 req/min · ALL-STAR 60 req/min · GOAT 600 req/min** (per owner spec; re-verify). |
| Pagination | Cursor / `per_page` documented. |
| Timestamps | Game `date`, `status`; **no guaranteed explicit correction timestamps** → best-effort, inferred via content hash. |
| Injuries | **GOAT** (and ALL-STAR per owner spec). Missing injury data is **never** interpreted as "healthy" (§3). |
| Lineups | **GOAT** — *lineups when available*. **Not a guarantee of pregame confirmed starters** (§3). |
| Stable ids | BALLDONTLIE player/team/game ids (stable within the provider) → anchor NBA crosswalks. |
| Terms / betting use | ToS permit analytics/personal use; betting not explicitly blessed → **LABELLED (low-moderate)**. Documented + key-gated (cleaner than `stats.nba.com`). |
| Reliability | Documented commercial-ish API with tiered SLAs; re-verify current SLA. |

**BALLDONTLIE tier capabilities (per owner spec 2026-07-23; re-verify at D1):**

| Capability | Free | ALL-STAR | GOAT |
| --- | --- | --- | --- |
| Teams | ✅ | ✅ | ✅ |
| Players | ✅ | ✅ (active players) | ✅ |
| Games / schedules | ✅ | ✅ | ✅ |
| Game **player statistics** | ❌ | ✅ | ✅ |
| Player **injuries** | ❌ | ✅ | ✅ |
| **Box scores** | ❌ | ❌ | ✅ |
| **Advanced statistics** | ❌ | ❌ | ✅ |
| Season averages | ❌ | ❌ | ✅ |
| **Plays** (play-by-play) | ❌ | ❌ | ✅ |
| **Lineups** (when available) | ❌ | ❌ | ✅ |
| Rate limit | 5 req/min | 60 req/min | 600 req/min |

**Selected NBA tier: GOAT.** The MVP NBA path assumes GOAT capabilities. A
`403`/tier error from BALLDONTLIE is reported as **"capability unavailable for
current subscription tier"**, never as an invalid key or an application bug
(`PHASE_D_IMPLEMENTATION_PLAN.md` §4).

### 2.5 NBA — hoopR  ·  candidate: **OFFLINE historical supplement (no live dependency)**

| Field | Finding (doc-review 2026-07-23) |
| --- | --- |
| Official name | hoopR (R package; `sportsdataverse`) |
| Role | **Offline** historical research + backfill only: play-by-play, possession sequences, substitution/rotation reconstruction, lineup-stint research. |
| Runtime | **Must NOT be a live dependency.** R is never required at live app startup. |
| Boundary | Export from hoopR → typed **Parquet** (or equivalent) → imported by an offline importer into append-only Phase D tables with full provenance. The import boundary is documented and versioned. |
| Terms | Aggregates public sources; deep-history/research use; label commercial/betting risk and attribute. |
| Verdict | Enriches NBA history where the live GOAT feed is thin; never gates the live app. |

### 2.6 NBA — `stats.nba.com`  ·  candidate: **DEMOTED (last-resort only, not selected)**

Undocumented; header-gated (`403`s / blocks datacenter & CI IPs); no correction
timestamps; NBA.com ToS restrict. **Not part of the selected stack.** Retained
only as a documented last-resort behind an adapter if GOAT is unavailable for a
category; its operational and terms risk is **high**. Prefer marking a capability
unavailable over depending on it.

### 2.7 NBA — official injury report (league PDF)  ·  candidate: **optional cross-check**

| Field | Finding |
| --- | --- |
| Official name | NBA Official Injury Report (league publication, PDF) |
| Role | **Optional independent cross-check** of BALLDONTLIE GOAT injuries — **not** the primary feed. |
| Auth / key | None (public PDF); parser required; layout can drift → fragile. |
| `published_at` | Report carries its own generated timestamp → strong PIT anchor for the cross-check. |
| Terms | Public official publication under NBA.com ToS → **LABELLED (moderate).** |
| Verdict | Reconciliation only; disagreements raise a `data_quality_issues` note. Missing injury data is never treated as "healthy". |

### 2.8 NBA — SportsDataIO Discovery Lab  ·  candidate: **optional free comparison**

| Field | Finding (doc-review 2026-07-23) |
| --- | --- |
| Official name | SportsDataIO (Discovery Lab free evaluation tier) |
| Role | **Optional free evaluation + secondary comparison source** — compare ids, fields, completed games, historical records against the primary provider. |
| Latency | **Delayed** — unsuitable as the primary same-day live feed. |
| Auth / key | Free evaluation key (not required by D1; not the live feed). |
| Terms | Free eval terms restrict production/redistribution; commercial requires the paid package. |
| Verdict | ID/field/record **cross-comparison** only; **not** implemented or purchased during D1; **not** a replacement for the live provider. Its commercial feeds are a documented professional upgrade (§2.10). |

### 2.9 Weather — NWS (`api.weather.gov`)  ·  candidate: **PRIMARY (US)**

| Field | Finding (doc-review 2026-07-23) |
| --- | --- |
| Official name | US National Weather Service API |
| Docs source | `weather.gov/documentation/services-web-api`. |
| Auth / key | **None** (descriptive `User-Agent` required). |
| Free / paid | Free; **US-government public-domain data — cleanest licensing here.** |
| Coverage | US only — covers all US MLB parks; **does not cover Toronto (Rogers Centre; retractable roof anyway)** or international series → Open-Meteo fills those. |
| Forecast / actual | Point forecasts + observations. |
| Historical | Observations available; long historical archive less convenient than Open-Meteo. |
| Verdict | **Primary weather for US outdoor MLB parks.** No paid key needed at D1. |

### 2.10 Weather — Open-Meteo (`open-meteo.com`)  ·  candidate: **SECONDARY + historical-forecast**

| Field | Finding (doc-review 2026-07-23) |
| --- | --- |
| Official name | Open-Meteo |
| Docs source | `open-meteo.com/en/docs`. |
| Auth / key | **None** for the free tier. |
| Free / paid | Free for non-commercial; **commercial use may require a paid plan later** → LABELLED. **No paid weather key at D1.** |
| Historical depth | Archive API (ERA5) back to 1940, hourly. |
| **Point-in-time forecast** | Historical-forecast / "previous model runs" API returns *what the forecast was* at a past time — a genuine leakage-free pregame-forecast source. **Major PIT advantage** → this is why Open-Meteo is kept even though NWS is primary. |
| Coverage | Global (covers Toronto + international series). |
| Terms | Free tier non-commercial (CC-BY-4.0 attribution); commercial → paid. **LABELLED (low personal, moderate commercial).** |
| Verdict | **Secondary + the leakage-free historical-forecast source**; non-US coverage fallback. |

### 2.11 Professional upgrade path (documented adapters; not implemented/purchased now)

Keep documented adapters / extension points for **Sportradar**, **SportsDataIO
commercial feeds**, and **Stats Perform** (both sports): documented, keyed, paid,
betting-licensable, with injuries, confirmed lineups, correction feeds, stable
ids, and contractual SLAs. Selecting one removes every "risk: labelled" flag and
unlocks NBA confirmed starters/injuries cleanly. **Not implemented or purchased in
Phase D.**

### 2.12 Rejected outright

| Provider / method | Reason |
| --- | --- |
| ESPN hidden API (`site.api.espn.com`) | Undocumented **and** ESPN ToS clearly restrict programmatic use/redistribution. |
| RotoWire / Lineups.com / Rotoworld HTML | Third-party HTML scraping → violates "no scraping as default"; ToS restrict. |
| Any provider requiring account auth for *data* we cannot otherwise get | Report the capability **unavailable** rather than add credentials. |
| Twitter/X or social scraping | `CLAUDE.md` forbids unauthorized social scraping; the `intel/` lane models *authorized* beat/social sources separately. |

---

## 3. Selection decision (final provider stack)

Ranked by: (1) point-in-time correctness, (2) historical coverage, (3) licensing
suitability, (4) data quality, (5) stable identifiers, (6) correction timestamps,
(7) reliability, (8) cost, (9) implementation complexity. Fewest practical
providers preferred.

| Category | **Primary (selected)** | Offline supplement | Cross-check / comparison | Professional |
| --- | --- | --- | --- | --- |
| MLB schedule/status/results/inning lines/box/rosters/probables/posted lineups/venues | **MLB StatsAPI** (no key) — *risk labelled* | pybaseball/Statcast/FanGraphs (offline, later) | — | Sportradar MLB / Stats Perform |
| MLB player id crosswalk | **Chadwick register** (open file) | — | — | provider-native ids |
| NBA teams/players/games/schedules/results/player stats/box/advanced/plays/injuries/lineups-when-available | **BALLDONTLIE — GOAT tier** (`NBA_DATA_API_KEY`) | **hoopR** (offline PBP/possessions/stints via Parquet) | **SportsDataIO Discovery Lab** (delayed; id/field/record comparison) | Sportradar NBA / SportsDataIO commercial / Stats Perform |
| NBA injuries | **BALLDONTLIE GOAT** (structured) | — | **Official NBA injury-report PDF** (optional) | Sportradar / Stats Perform |
| **NBA confirmed pregame starters** | **Declared UNAVAILABLE unless a provider observation truly supplies confirmed starters before the cutoff** (GOAT "lineups when available" does not guarantee this) | hoopR (historical stints, not pregame) | — | Sportradar confirmed lineups |
| MLB probable/confirmed SP + posted lineups | **MLB StatsAPI** | — | — | Sportradar |
| Weather (US outdoor MLB) | **NWS** (no key) | — | — | commercial met provider |
| Weather (non-US + leakage-free historical forecast) | **Open-Meteo** (no key) | — | NWS cross-check | Open-Meteo commercial |
| Venue coords / roof / timezone | **MLB StatsAPI `/venues`** + curated seed | — | NWS point metadata | Sportradar venues |

### Categories with **no acceptable primary** → explicit unavailable/capability path

- **NBA confirmed pregame starters** — not guaranteed by any selected provider
  before tip. **The system declares pregame starters `unavailable`** unless a real
  provider observation supplies confirmed starters *before the prediction cutoff*;
  the feature is masked, never fabricated; a `data_quality_issues` /
  capability record notes the gap.
- **Explicit provider correction timestamps (free/undocumented)** — not supplied;
  corrections inferred from content-hash changes, stamped with `observed_at`;
  professional feeds provide real ones.

**Nothing above fabricates a capability.** Where a provider cannot supply a field,
the field is nullable, a `data_quality_issues` row (or a capability record) is
written, and the model degrades gracefully.

---

## 4. Credentials (placeholders only)

New `.env` variables Phase D may require. **Placeholders only** — no real values
are read, displayed, or committed. Each key is a `SecretStr` in
`sports_quant.config`, sanitized out of every stored URL/param/header/log exactly
like `ODDS_API_KEY`. Base URLs for the operational providers are **pinned** in
`config.py` (like `PRODUCTION_KALSHI_REST_URL`) so an arbitrary host cannot be
substituted.

```
# Already in use (Phases A–C):
ODDS_API_KEY=
KALSHI_PUBLIC_REST_URL=https://external-api.kalshi.com/trade-api/v2

# Phase D (planned; blank placeholders only):
# BALLDONTLIE key. Endpoint access depends on the ACCOUNT TIER; the Phase D
# NBA path expects GOAT capabilities. A key alone does NOT grant GOAT.
NBA_DATA_API_KEY=

# Pinned base URLs for key-less operational providers:
MLB_STATS_API_BASE_URL=https://statsapi.mlb.com/api/v1
NWS_BASE_URL=https://api.weather.gov
OPEN_METEO_BASE_URL=https://api.open-meteo.com/v1

# Optional / professional -- leave blank for the no-paid MVP path:
WEATHER_API_KEY=
SPORTRADAR_MLB_API_KEY=
SPORTRADAR_NBA_API_KEY=
```

**Key-less providers (explicit):** MLB StatsAPI, NWS, Open-Meteo (free), Kalshi
public REST, Chadwick register (static file), the NBA injury-report PDF, and the
hoopR offline export (no live call). **The only key the MVP strictly needs is
`NBA_DATA_API_KEY` at the GOAT tier.** `stats.nba.com` is not selected.

---

## 5. Cost & coverage report

### 5.1 Implementable with **no paid subscription** (and only key-less providers)

- MLB: schedules, status, results, inning lines, team/player box, probable &
  confirmed starting pitchers, posted lineups, rosters, venues — **MLB StatsAPI**.
- Weather: US outdoor parks — **NWS**; non-US + **leakage-free historical
  forecast** — **Open-Meteo**.
- MLB player id crosswalk — **Chadwick** (open file).
- NBA historical PBP/possessions/stints — **hoopR** (offline import; no key).
- All matching (official↔sportsbook↔Kalshi, player, team) — pure compute.

### 5.2 Requires a **paid (GOAT) BALLDONTLIE tier**

- NBA game player statistics, box scores, advanced stats, plays, injuries, and
  lineups-when-available — **BALLDONTLIE GOAT** (`NBA_DATA_API_KEY`). This is the
  central NBA cost of the MVP. (ALL-STAR gives player stats + injuries but **no**
  box scores/plays/lineups; Free gives none of these.)

### 5.3 Free-with-a-key but **insufficient** for the NBA MVP

- BALLDONTLIE **Free** (teams/players/games only, 5 req/min) — **cannot** power the
  NBA feature set. Documented so no one mistakes a free key for a working NBA path.

### 5.4 Likely requires a **paid professional plan**

- NBA confirmed pregame starters/lineups guaranteed; explicit provider correction
  timestamps + SLA + betting-suitable licensing (Sportradar/SportsDataIO
  commercial/Stats Perform); commercial weather licensing (Open-Meteo commercial).

### 5.5 Missing data represented as **unavailable** (non-blocking)

- NBA confirmed pregame starters (masked; capability record).
- NBA plays/lineups where GOAT history is thin (best-effort / provider-history-limited).
- MLB Statcast/tracking + FanGraphs features (offline supplement, deferred).

### 5.6 Missing data that **would prevent a serious model**

- **NBA injuries/availability** — the largest NBA pregame signal — is covered by
  **GOAT** (paid). Without a GOAT (or professional) injury source, NBA
  recommendations are materially weaker; this is stated to the user, not hidden.
  Missing injury data is **never** read as "healthy".
- **MLB probable/confirmed starting pitcher** — largest MLB pregame signal —
  covered free by StatsAPI. MLB is in good shape.

### 5.7 Recommended setups

- **Minimum viable:** MLB StatsAPI (no key) + **BALLDONTLIE GOAT**
  (`NBA_DATA_API_KEY`, paid) + NWS + Open-Meteo (no keys) + Chadwick + hoopR
  (offline) + optional NBA injury-PDF cross-check + optional SportsDataIO
  comparison. NBA confirmed pregame starters = unavailable. Undocumented-endpoint
  and tier risks **labelled**; suitable for **personal, non-redistributed
  analytics**.
- **Professional (paid, clean licensing):** Sportradar (or Stats Perform, or
  SportsDataIO commercial) for **both** sports (schedules/results/box/injuries/
  confirmed lineups/correction feeds/stable ids/betting licence) + Open-Meteo
  commercial (or a licensed met provider). Removes every "risk: labelled" flag and
  unlocks NBA starters/injuries cleanly.

---

## 6. Major licensing & reliability risks (summary)

| Risk | Severity | Mitigation |
| --- | --- | --- |
| MLB StatsAPI terms ambiguous-to-restrictive; likely prohibited for commercial/betting redistribution; no SLA; no correction timestamps | **High for commercial** | Personal, non-redistributed MVP only; professional tier for any deployment; risk labelled; corrections inferred + stamped with `observed_at`. |
| **BALLDONTLIE tier confusion** — a key does not imply GOAT; free/ALL-STAR lack box/plays/lineups | Medium | Explicit typed capability declarations + selected-tier record; a tier error → "capability unavailable for current subscription tier", never invalid-key/bug; `provider-audit` verifies tier before backfill. |
| GOAT lineups ≠ guaranteed pregame confirmed starters | Medium | Starters declared `unavailable` unless truly observed before cutoff; feature masked; capability record. |
| hoopR requires R + is not a live feed | Low–Medium | Offline-only via a typed Parquet import boundary; R never required at live startup. |
| SportsDataIO Discovery Lab is delayed | Low | Used only for id/field/record comparison; never the live feed. |
| `stats.nba.com` (if ever used) 403s/blocks CI IPs; undocumented | High | Not selected; last-resort behind an adapter; prefer "unavailable". |
| Open-Meteo free tier non-commercial | Low–Medium | NWS (public domain) primary for US parks; commercial tier only if needed. |
| Undocumented endpoints change without notice | Medium | Behind adapters (swap not rewrite); mocked tests only; capture-forward + resumable; pinned base URLs. |
| Missing injury data misread as "healthy" | Medium | Explicitly forbidden; absence → `data_quality_issues` + masked feature. |

---

## 7. Verification obligations (at implementation time, not now)

Because no live call was made, **D1 must, before writing any client** (and the
`provider-audit` command re-checks before any backfill — §9,
`PHASE_D_IMPLEMENTATION_PLAN.md` §10):

1. Re-read each selected provider's **current** docs and Terms of Use; re-confirm
   auth, **BALLDONTLIE tier boundaries and rate limits**, pagination, field
   availability, and the betting/analytics permission posture. Update this
   document (and its doc-review date) if reality differs.
2. Confirm the exact host(s) and path prefixes for the `http_policy` GET-only
   allow-list; pin base URLs in `config.py`.
3. Capture small sanitized response samples as **test fixtures** — never live
   calls in the automated suite.
4. If any provider's terms have moved to *clearly prohibit* this use, **switch to
   the professional/unavailable path** rather than proceed.

---

## 8. Unresolved decisions (for the user)

1. **BALLDONTLIE GOAT subscription.** The NBA MVP requires GOAT (paid). Confirm
   the intent to subscribe, or accept a reduced NBA path (ALL-STAR: stats +
   injuries, no box/plays/lineups) or "NBA largely unavailable".
2. **Personal vs commercial intent.** Commercial/redistributed use rules out the
   undocumented MLB endpoint and free-tier weather; a paid licensed provider is
   required from the start.
3. **NBA injury cross-check.** Whether to build the official-PDF cross-check now
   or rely on GOAT injuries alone.
4. **Weather licensing.** NWS (US, public domain) + Open-Meteo free
   (non-commercial) vs a commercial plan.
5. **Deep history supplements.** hoopR (NBA) / Retrosheet + pybaseball (MLB)
   offline backfills — optional, deferred, offline-only.
