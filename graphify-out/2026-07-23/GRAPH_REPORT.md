# Graph Report - .  (2026-07-23)

## Corpus Check
- 232 files · ~190,751 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 3519 nodes · 9648 edges · 131 communities (124 shown, 7 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 592 edges (avg confidence: 0.55)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Sports Quant Odds Ingestor
- Sports Quant Sqlite Sportsbook Repository
- Sports Quant Test Phase D1
- Streaming Latency Registry
- Backtest Backtester
- Sports Quant Sqlite Kalshi Repository
- Evaluation Components
- Sports Quant Test Repositories
- Sports Quant Cli
- Sports Quant Db
- Sports Quant Ids
- Intel Base Adapter
- Sports Quant Database
- State Event Envelope
- Sports Quant Test Phase D2
- Sports Quant MLB Stats API
- Streaming Event Envelope
- Sports Quant Test Seeds
- Sports Quant MLB Ingestor
- Sports Quant Repositories
- Sports Quant Read Only Httppolicy
- Sports Quant Repository Error
- Sports Quant Test Kalshi Ingestor
- Sports Quant Models
- Streaming Test
- Sports Quant Test Phase D
- Gateway Components
- Sports Quant Sqlite Season Repository
- State Order Book
- Tracking Base
- Sports Quant Repositories Capabilities
- Sports Quant Sqlite Game Repository
- Sports Quant Test Migrations
- Sports Quant Test Phase D1
- Sports Quant Kalshi Ingestor
- Sports Quant Raw Exchange
- Intel Material Change Detector
- Sports Quant Odds API Client
- Sports Quant Sqlite Team Repository
- Sports Quant Sqlite Team Alias
- Gateway Latency Histogram
- Sports Quant Transaction
- Probability Inference Engine
- Sports Quant Utc Now Iso
- Sports Quant Settings
- Gateway Test
- Sports Quant Leagues
- Tracking Test
- Sports Quant Migrate
- Sports Quant Test Price Snapshot
- Tracking Components
- Intel Components
- Intel Test
- Evaluation Test
- Probability Components
- Sports Quant Test Integrity Guards
- Tracking Kinematics
- Gateway Kalshi Limits
- Gateway Kalshi Rest Transport
- Probability Pipeline
- Data Architecture Foundation Roadmap
- Sports Quant Odds API
- Sports Quant Test Db Cli
- Sports Quant Test Phase D
- Sports Quant Game Repository Protocol
- Backtest Test
- Probability Features
- Sports Quant Balldontlie
- Sports Quant Provider Error
- Sports Quant Kalshi Client
- Tracking Frame Manifest
- Gateway Execution
- Sports Quant Initialize Database
- Sports Quant Build Readonly Client
- Sports Quant Ingestion Runs
- Intel Report Registry
- Intel Player Ref
- Probability Reference
- Tracking Missing Dependency Error
- Probability Residual Win Prob Model
- Sports Quant Player
- Sports Quant Repository
- Sports Quant Read Only Policy
- State Mlbgame
- State Nbagame
- Sports Quant Run Ingest Odds
- Sports Quant Test Kalshi Ingest
- Sports Quant Sqlite Provider Reference
- Sports Quant Raw Response
- Sports Quant Run Ingest Venues
- Sports Quant Check Odds API
- Streaming NATS Event Bus
- Sports Quant Run Providers Check
- Sports Quant Schema
- Sports Quant Validate Trade
- Sports Quant Test Isolation
- Streaming Sqlite Dedup Store
- Streaming In Memory Dedup Store
- Codex Graphify Pipeline
- Backtest Latency Model
- Evaluation Portfolio
- Probability Train And Build
- Tracking Aggregations
- Gateway Arming Controller
- Sports Quant Validate Market
- Sports Quant Normalize Venue
- Sports Quant Test Kalshi
- Sports Quant Test Phase D
- Sports Quant Test D011 Official
- Sports Quant Test Kalshi Safety
- Phase D Implementation Plan Provider
- Streaming Latency Snapshot
- Sports Quant Provider Audit Result
- Sports Quant
- Tracking Level
- Tracking Error
- Evaluation Ladder
- Streaming Default Bucket Bounds Ns
- Codex Media Transcription Pipeline
- Pyproject Toml Sports Quant

## God Nodes (most connected - your core abstractions)
1. `Database` - 198 edges
2. `EventEnvelope` - 120 edges
3. `Repository` - 73 edges
4. `utc_now_iso()` - 62 edges
5. `initialize_database()` - 55 edges
6. `SqliteGameRepository` - 53 edges
7. `SqliteTeamAliasRepository` - 52 edges
8. `RepositoryError` - 50 edges
9. `audit_provider()` - 45 edges
10. `MlbStatsApiClient` - 45 edges

## Surprising Connections (you probably didn't know these)
- `Optional Frame-Level Data` --semantically_similar_to--> `Unavailable Data Contract`  [INFERRED] [semantically similar]
  tracking/README.md → PHASE_D_IMPLEMENTATION_PLAN.md
- `ExecutionGateway` --uses--> `OrderBookView`  [INFERRED]
  gateway/gateway.py → evaluation/decision.py
- `EvaluationConfig` --uses--> `LatencyRegistry`  [INFERRED]
  evaluation/evaluator.py → streaming/latency.py
- `MarketEvaluator` --uses--> `LatencyRegistry`  [INFERRED]
  evaluation/evaluator.py → streaming/latency.py
- `Stage` --uses--> `LatencyRegistry`  [INFERRED]
  gateway/benchmark.py → streaming/latency.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Graphify Build Query Update Lifecycle** — codex_skills_graphify_skill_graphify_pipeline, codex_skills_graphify_references_extraction_spec_semantic_extraction_contract, codex_skills_graphify_references_query_graph_traversal, codex_skills_graphify_references_update_incremental_graph_update, codex_skills_graphify_references_exports_graph_exports [EXTRACTED 1.00]
- **Historical Data Integrity System** — data_architecture_raw_response_provenance, data_architecture_append_only_history, point_in_time_data_bitemporal_model, point_in_time_data_observed_at_cutoff, point_in_time_data_leakage_prevention [INFERRED 0.95]
- **Safe Read-Only Recommendation Architecture** — claude_research_lane, claude_hot_path_constraints, read_only_architecture_read_only_recommendation_engine, read_only_architecture_execution_quarantine, gateway_phases_legacy_gateway [INFERRED 0.95]

## Communities (131 total, 7 thin omitted)

### Community 0 - "Sports Quant Odds Ingestor"
Cohesion: 0.05
Nodes (98): ClientFactory, normalized_key(), Convenience: just the normalized string, suffix removed., test_normalization_is_idempotent(), Ingestion lane: read-only provider fetches persisted into the corpus.  Each inge, american_to_decimal(), american_to_implied(), _append_reason() (+90 more)

### Community 1 - "Sports Quant Sqlite Sportsbook Repository"
Cohesion: 0.05
Nodes (55): The stable identity of a betting line, separate from its price.      A changed p, One append-only observation of a price. ``price_american`` is exact., SportsbookEvent, SportsbookMarket, SportsbookOutcome, SportsbookPriceSnapshot, point_key(), price_content_hash() (+47 more)

### Community 2 - "Sports Quant Test Phase D1"
Cohesion: 0.07
Nodes (71): audit_provider(), build_balldontlie_probes(), build_mlb_statsapi_probes(), Audit a provider by running each capability-group probe independently., Dependency-aware probes for the documented BALLDONTLIE GOAT endpoints.      Inde, Dependency-aware probes for the MLB StatsAPI endpoint families D1 verifies., _bdl_client(), _bdl_decl() (+63 more)

### Community 3 - "Streaming Latency Registry"
Cohesion: 0.07
Nodes (41): EventHandler, CorrectionHandler, Tracks the authoritative version of each sequenced event., Deduplicator, Idempotency layer for at-least-once delivery.  JetStream (and any at-least-once, Content-hash based idempotency guard., DeadLetter, DeadLetterQueue (+33 more)

### Community 4 - "Backtest Backtester"
Cohesion: 0.09
Nodes (45): BacktestConfig, BacktestReport, DecisionPoint, EdgeStrategy, Protocol, Replay backtester: replay events, apply latency, simulate fills, report.  The st, Reference strategy: take the side whose edge clears a threshold.      Reads the, ReplayBacktester (+37 more)

### Community 5 - "Sports Quant Sqlite Kalshi Repository"
Cohesion: 0.06
Nodes (46): KalshiEvent, KalshiMarket, KalshiOrderbookLevel, KalshiOrderbookSnapshot, KalshiPublicTrade, Public Kalshi event. ``event_ticker`` is the stable provider identity;     ``gam, Public Kalshi market. ``market_ticker`` is the stable provider identity.      Pr, One ladder level of an order-book snapshot. ``level_index`` 0 = best. (+38 more)

### Community 6 - "Evaluation Components"
Cohesion: 0.08
Nodes (41): Action, Decision, LimitOrder, MarketEvent, MarketSnapshot, str, Data types for market evaluation: events, snapshots, decisions, orders., A BET decision's trade parameters, proven complete and in range.      :class:`De (+33 more)

### Community 7 - "Sports Quant Test Repositories"
Cohesion: 0.06
Nodes (55): PlayerAlias, Row, Player-alias storage and deterministic resolution., Flag aliases whose (normalized, suffix) maps to more than one player.          T, Player storage. ``player_id`` is a surrogate ULID.      Surrogate rather than na, SqlitePlayerAliasRepository, SqlitePlayerRepository, _game() (+47 more)

### Community 8 - "Sports Quant Cli"
Cohesion: 0.06
Nodes (51): _make_audit_probes(), _mlb_json(), Any, Command-line entry points for the read-only engine.  Currently exposes ``provide, Build ``(probes, client, declaration)`` for a provider audit.      One minimal a, Configuration + read-only startup invariants.  Loads the provider/safety setting, DatabaseError, RuntimeError (+43 more)

### Community 9 - "Sports Quant Db"
Cohesion: 0.06
Nodes (52): Historical-corpus database layer (Phase A).  SQLite storage for the canonical en, AliasCandidate, AliasMatchStatus, AliasResolution, _collapse_initials(), _fold_punctuation(), normalize_name(), NormalizedName (+44 more)

### Community 10 - "Sports Quant Ids"
Cohesion: 0.07
Nodes (48): new_game_id(), new_game_status_id(), new_ingestion_run_id(), new_inning_line_id(), new_kalshi_book_id(), new_kalshi_event_id(), new_kalshi_level_id(), new_kalshi_market_id() (+40 more)

### Community 11 - "Intel Base Adapter"
Cohesion: 0.09
Nodes (40): ParseResult, PollingReportAdapter, PollResult, Shared adapter machinery: resolution, scheduling and new-report detection., A source polled as whole reports, with byte-level new-report detection., A report row that could not be confidently matched to one player.      Ambiguous, Base for all source adapters., SourceAdapter (+32 more)

### Community 12 - "Sports Quant Database"
Cohesion: 0.06
Nodes (54): Database, _is_statement_end(), Statement text with comment-only lines removed, for emptiness checks., Whether a ``;`` at this point terminates the statement.      Only a ``CREATE TRI, A SQLite database file plus its connection and migration policy., Split a migration script into individual statements.      ``sqlite3.Cursor.execu, split_sql_statements(), _strip_comments() (+46 more)

### Community 13 - "State Event Envelope"
Cohesion: 0.07
Nodes (33): ApplyResult, ApplyStatus, compute_state_hash(), DataQuality, _deep_freeze(), LiveState, now_ns(), Any (+25 more)

### Community 14 - "Sports Quant Test Phase D2"
Cohesion: 0.18
Nodes (50): boxscore(), _client(), _count(), game(), _ingest(), linescore(), Any, Request (+42 more)

### Community 15 - "Sports Quant MLB Stats API"
Cohesion: 0.06
Nodes (33): _fetch_schedule(), _clamp_per_page(), GET /v1/games -- games / schedules / results group., GET /v1/stats -- per-player game statistics group (GOAT-gated)., GET /v1/player_injuries -- injuries group (tier-gated)., GET /v1/plays?game_id=ID -- play-by-play for one game (GOAT-gated).          ``g, GET /v1/lineups?game_ids[]=ID -- lineups for one or more games (GOAT)., ProviderResponse (+25 more)

### Community 16 - "Streaming Event Envelope"
Cohesion: 0.06
Nodes (25): kalshi_events(), load_events(), mlb_events(), nba_events(), Fixture loading for live-state tests.  Each JSON fixture describes a subject/pro, CorrectionResult, CorrectionStatus, _Current (+17 more)

### Community 17 - "Sports Quant Test Seeds"
Cohesion: 0.07
Nodes (34): ``db-init``: create the database, apply migrations, seed canonical data.  Kept s, Deterministic, offline seed data for canonical leagues and teams., alias_specs(), LeagueSeedResult, Connection, Idempotent application of the canonical league and team seeds.  Everything here, Seed one league and its teams. Idempotent., Seed both leagues, their teams, and their aliases.      The caller supplies the (+26 more)

### Community 18 - "Sports Quant MLB Ingestor"
Cohesion: 0.10
Nodes (45): _as_dict(), _dry_run_count(), _game_ref(), ingest_lineups(), ingest_mlb(), MlbIngestResult, _normalize_schedule_game(), _NormGame (+37 more)

### Community 19 - "Sports Quant Repositories"
Cohesion: 0.09
Nodes (28): Team + player game-statistics repositories (append-only, transition-aware).  Anc, Append-only team box lines., Append-only player box lines (batting or pitching)., SqlitePlayerGameStatRepository, SqliteTeamGameStatRepository, Typed repositories for the Phase A tables.  Every repository is a ``Protocol`` p, append_transition(), observation_content_hash() (+20 more)

### Community 20 - "Sports Quant Read Only Httppolicy"
Cohesion: 0.06
Nodes (30): AsyncBaseTransport, balldontlie_host_rule(), HostRule, kalshi_host_rule(), mlb_statsapi_host_rule(), nws_host_rule(), odds_api_host_rule(), open_meteo_host_rule() (+22 more)

### Community 21 - "Sports Quant Repository Error"
Cohesion: 0.09
Nodes (34): MatchCandidate, MatchDecision, RuntimeError, Raised when a repository operation cannot be completed., SQLite has no boolean type; store 0/1 with a CHECK behind it., RepositoryError, to_db_bool(), CandidateInput (+26 more)

### Community 22 - "Sports Quant Test Kalshi Ingestor"
Cohesion: 0.16
Nodes (43): KalshiHandler, ingest_kalshi(), Ingest Kalshi public events, markets, and optionally books and trades.      ``--, kalshi_events_body(), kalshi_markets_body(), kalshi_orderbook_body(), kalshi_router(), kalshi_trades_body() (+35 more)

### Community 23 - "Sports Quant Models"
Cohesion: 0.07
Nodes (25): Level, Typed row models for the Phase A tables.  Frozen dataclasses rather than raw ``s, Venue, VenueAlias, KalshiRepositoryProtocol, Protocol, str, Operations the Kalshi ingestor and the historical queries need. (+17 more)

### Community 24 - "Streaming Test"
Cohesion: 0.09
Nodes (30): ReplayPipeline, EventProcessor, InMemoryEventBus, Idempotent, gap-aware, dead-letter-capable processing pipeline., NATS-style subject matching supporting ``*`` and ``>`` wildcards., In-process bus that faithfully models at-least-once redelivery.      On a ``RETR, Deliver once with an explicit redelivered flag (test hook)., subject_matches() (+22 more)

### Community 25 - "Sports Quant Test Phase D"
Cohesion: 0.08
Nodes (39): classify_http_status(), _has_tier_evidence(), is_tier_restriction(), ProviderErrorKind, Distinct, non-overlapping classifications of a provider failure.      Each kind, Whether a sanitized body carries explicit plan/tier-restriction evidence.      R, Classify an HTTP failure into a :class:`ProviderErrorKind`.      ``401`` is auth, Whether a failure means a capability is unavailable at the current tier.      A (+31 more)

### Community 26 - "Gateway Components"
Cohesion: 0.09
Nodes (18): Arming controller: the hard gate between demo and live orders.  Demo orders are, ClientOrderIdFactory, IdempotencyRegistry, Unique client order IDs and an idempotency registry.  Each order intent gets a u, Maps client_order_id -> the ack we already got for it., Execution-gateway configuration.  Demo by default (``CLAUDE.md``: "demo by defau, Phase 1 execution gateway (Python asyncio, Kalshi demo).  Consumes market-data e, Benchmarked execution gateway (Module 8) -- QUARANTINED.  The project is now a s (+10 more)

### Community 27 - "Sports Quant Sqlite Season Repository"
Cohesion: 0.08
Nodes (19): League, Season, LeagueRepositoryProtocol, Protocol, Row, Season storage. Seasons are not seeded; Phase D populates them., Operations Phase A needs from a league store., League storage. The canonical ``league_id`` is derived from the code. (+11 more)

### Community 28 - "State Order Book"
Cohesion: 0.08
Nodes (22): OrderBookState, Any, Cheapest executable price to BUY Yes, derived from best No bid., Cheapest executable price to BUY No, derived from best Yes bid., Set an absolute quantity at a price. qty <= 0 removes the level., LiveStateStore, Thread-safe container of live states with single-writer semantics., ob_event() (+14 more)

### Community 29 - "Tracking Base"
Cohesion: 0.09
Nodes (25): _frame_to_rows(), FrameDataUnavailable, FrameParquetStore, FrameSource, Any, Tracking data architecture: the hard line between event and frame data.  This mo, A single optical-tracking frame: all tracked entities at one instant., Abstract, optional source of frame-level tracking.      Concrete adapters (optic (+17 more)

### Community 30 - "Sports Quant Repositories Capabilities"
Cohesion: 0.09
Nodes (31): new_provider_capability_id(), ProviderCapabilityRecord, One append-only provider-capability record at a tier.      ``is_observed`` disti, capability_content_hash(), CapabilityRepositoryProtocol, Protocol, Row, Provider-capability snapshot repository (append-only, historically auditable). (+23 more)

### Community 31 - "Sports Quant Sqlite Game Repository"
Cohesion: 0.08
Nodes (35): Game storage plus append-only status history., Whether this observation reports the same state as the one before it.          ", Set the game's current state from its newest observation.          Ordered by ``, SqliteGameRepository, game_id(), Connection, Game-status history: stale-backfill protection and transition deduplication.  Tw, Whatever the arrival order, current state is the newest observation. (+27 more)

### Community 32 - "Sports Quant Test Migrations"
Cohesion: 0.10
Nodes (36): discover_migrations(), MigrationChecksumError, MigrationError, Path, Raised when migrations cannot be discovered, ordered, or applied., Raised when an applied migration's file has changed on disk., Load and order every migration file in ``directory``.      Ordering is by the nu, Connection (+28 more)

### Community 33 - "Sports Quant Test Phase D1"
Cohesion: 0.09
Nodes (32): _pinned_url_violation(), Return a human-readable violation for a pinned base URL, or ``None``.      Stric, Any, AsyncClient, SecretStr, _client(), _ExplodingStream, _json_body_of_length() (+24 more)

### Community 34 - "Sports Quant Kalshi Ingestor"
Cohesion: 0.12
Nodes (34): orderbook_content_hash(), Identity of an order-book *state*: the full yes/no ladders.      Excludes ``obse, _Ctx, _finish_failed(), _has_duplicate_prices(), _ingest_dry_run(), _ingest_events(), _ingest_markets() (+26 more)

### Community 35 - "Sports Quant Raw Exchange"
Cohesion: 0.09
Nodes (24): KalshiOrderBook, Typed asynchronous adapter for Kalshi's public REST API.  Read-only and unauthen, Cheapest executable price to BUY No, derived from the best Yes bid., Fetch a market's order book and return the raw payload unparsed.          The in, A parsed Kalshi order book with derived executable asks.      Levels are ``(pric, Cheapest executable price to BUY Yes, derived from the best No bid., build_exchange(), build_exchange_from_parts() (+16 more)

### Community 36 - "Intel Material Change Detector"
Cohesion: 0.10
Nodes (17): MaterialChange, An immutable, append-only observation of a subject's status., A detected, materially-relevant change with before/after model inputs., StatusSnapshot, An append-only log of status snapshots, indexed by subject., Append a snapshot. Returns False if an identical one already exists.          "I, Most recent snapshot for the subject from a *different* source.          Used to, StatusHistory (+9 more)

### Community 37 - "Sports Quant Odds API Client"
Cohesion: 0.10
Nodes (21): Headers, CreditHeaders, OddsApiClient, OddsApiResult, Any, datetime, Odds for one sport: raw payload preserved alongside normalized events., Async, read-only adapter for The Odds API. (+13 more)

### Community 38 - "Sports Quant Sqlite Team Repository"
Cohesion: 0.08
Nodes (14): Team, TeamAlias, Protocol, Row, Operations Phase A needs from a team-alias store., Operations Phase A needs from a team store., Team storage. ``team_id`` is deterministic from (league, abbreviation)., SqliteTeamRepository (+6 more)

### Community 39 - "Sports Quant Sqlite Team Alias"
Cohesion: 0.08
Nodes (29): Team-alias storage and deterministic resolution.      Uniqueness is scoped to th, Flag every alias whose normalized form maps to more than one team.          Comp, SqliteTeamAliasRepository, test_alias_add_is_idempotent(), test_alias_normalization_makes_spelling_variants_equal(), test_historical_team_names_resolve(), test_resolve_is_league_scoped(), test_resolve_matches_abbreviation_and_nickname() (+21 more)

### Community 40 - "Gateway Latency Histogram"
Cohesion: 0.09
Nodes (13): GatewayReport, LatencyBenchmark, str, Stage-by-stage latency benchmarking for the execution gateway.  Records the nine, Threads through a single event, recording per-stage deltas., Stage, StageTimer, _stats() (+5 more)

### Community 41 - "Sports Quant Transaction"
Cohesion: 0.12
Nodes (27): Run a block inside one explicit transaction on an existing connection.      Nest, transaction(), Data-quality issue storage., SqliteDataQualityRepository, Ingestion-run storage., SqliteIngestionRunRepository, Dedup identity of a response: provider + endpoint + params + body.      Identica, Append-only raw-response storage. (+19 more)

### Community 42 - "Probability Inference Engine"
Cohesion: 0.10
Nodes (16): FeatureSpec, InferenceEngine, PredictionResult, ndarray, In-memory live inference engine.  Loads the champion (and its uncertainty ensemb, Predict from an already-vectorized, fixed-size feature array., Load the model once from disk at process startup., Single-load, in-memory win-probability inference. (+8 more)

### Community 43 - "Sports Quant Utc Now Iso"
Cohesion: 0.08
Nodes (19): new_data_quality_id(), DataQualityIssue, DataQualityRepositoryProtocol, Protocol, Row, Data-quality issue repository.  Records quality/capability gaps (``DQ-CAP-*``, `, Record one data-quality issue., Append a status observation and refresh the game's current state.          Retur (+11 more)

### Community 44 - "Sports Quant Settings"
Cohesion: 0.09
Nodes (24): BaseSettings, CheckStatus, Enum, str, Outcome of a single provider check., Path, RuntimeError, Raised when the read-only startup invariants are not satisfied.      The message (+16 more)

### Community 45 - "Gateway Test"
Cohesion: 0.16
Nodes (23): GatewayConfig, LimitOrderRequest, big_limits(), build_gateway(), make_strategy(), ob_event(), Tests for the benchmarked execution gateway (Module 8, Phase 1)., test_automatic_disarm_after_consecutive_failures() (+15 more)

### Community 46 - "Sports Quant Leagues"
Cohesion: 0.08
Nodes (26): _encode(), league_id(), _MonotonicUlidFactory, new_ulid(), Generates ULIDs that strictly increase, even within one millisecond.      Plain, A fresh, monotonically increasing ULID., Lowercase alphanumeric slug used inside deterministic identifiers., ``'MLB'`` -> ``'lg_mlb'``. (+18 more)

### Community 47 - "Tracking Test"
Cohesion: 0.13
Nodes (28): defender_distance(), lineup_spacing(), movement_speed(), movement_speed_between(), Instantaneous speed of a tracked player.      Uses provider-reported velocity wh, Speed (distance/time) for a player between two consecutive frames.      Derived, Mean pairwise distance (ft) among a team's tracked players.      A simple spacin, Distance (ft) from the ball handler to the nearest defender.      Requires frame (+20 more)

### Community 48 - "Sports Quant Migrate"
Cohesion: 0.09
Nodes (18): AppliedMigration, configure_connection(), Migration, MigrationResult, _now_ms(), Connection, One forward-only migration file., A row of the schema-version table. (+10 more)

### Community 49 - "Sports Quant Test Price Snapshot"
Cohesion: 0.14
Nodes (27): foreign_keys_enabled(), test_foreign_keys_are_enabled_on_every_connection(), Connection, Schema-level guarantees for the Kalshi tables (migration c007).  Inspects ``sqli, The public corpus must never carry an account/order/fill/position column., _seed_book_and_trade(), test_duplicate_price_level_rejected_by_unique(), test_foreign_keys_enforced() (+19 more)

### Community 50 - "Tracking Components"
Cohesion: 0.13
Nodes (17): Coordinates, EventCoordinate, A single coordinate attached to a discrete game event.      This is NOT player t, A spatial point. Every axis is optional and present only if sourced., Optional tracking and positional-data architecture (Module 3).  The organizing p, Event-level landing coordinate (2-D as reported; no z invented)., NBACourt, NBAPlayEvent (+9 more)

### Community 51 - "Intel Components"
Cohesion: 0.16
Nodes (21): Alert, ChangeType, PlayerStatus, str, Core models for injury / lineup / material-news intelligence (Module 4).  Design, A human/machine-facing alert derived from a material change., Ordered loosely by trust; see :mod:`intel.confidence` for scores., Provenance for one observation. (+13 more)

### Community 52 - "Intel Test"
Cohesion: 0.21
Nodes (26): NBAInjuryReportAdapter, Adapter for the official league injury report., Adapter for authorized social/news feeds (unconfirmed by definition)., SocialNewsAdapter, Projection, The projected inputs a prediction was built on, per subject., injury_report(), luka_key() (+18 more)

### Community 53 - "Evaluation Test"
Cohesion: 0.23
Nodes (24): evaluator(), make_event(), make_snapshot(), Tests for event-driven market evaluation (Module 6)., A rejection must not strand risk-reducing cancels., StubEngine, StubPred, test_bet_and_submit() (+16 more)

### Community 54 - "Probability Components"
Cohesion: 0.11
Nodes (12): Live probability updates (Module 5).  Fast, calibrated live win-probability that, build_onnx_model(), export_to_onnx(), onnx_available(), onnxruntime_available(), Champion-model ONNX export.  The champion is a logistic model, so its ONNX graph, Build an in-memory ONNX ModelProto for the logistic champion., Serialize the champion to an .onnx file. Requires the ``onnx`` package. (+4 more)

### Community 55 - "Sports Quant Test Integrity Guards"
Cohesion: 0.13
Nodes (26): _insert_game(), mlb_season(), nba_season(), Connection, Migration a003: cross-table league consistency enforced by the database.  Foreig, The guard covers UPDATE, not only INSERT., The UPDATE guard is column-scoped, so status writes stay cheap., A no-op rewrite is not a change; only a differing value is rejected. (+18 more)

### Community 56 - "Tracking Kinematics"
Cohesion: 0.12
Nodes (20): batted_ball_measures(), pitch_measures(), Event-level batted-ball features., Event-level pitch features. Only fields the provider gave are computed., Kinematics, BaseModel, Motion attributes. Populated only when a provider reports them., _f() (+12 more)

### Community 57 - "Gateway Kalshi Limits"
Cohesion: 0.11
Nodes (11): Query current Kalshi limits/costs and build the local token budget., KalshiLimits, KalshiLimitsProvider, LimitsProvider, Protocol, Kalshi rate limits and endpoint costs, queried at startup.  The gateway queries, Fetches limits/costs from Kalshi at startup (lazy httpx)., StaticLimitsProvider (+3 more)

### Community 58 - "Gateway Kalshi Rest Transport"
Cohesion: 0.13
Nodes (14): ensure_execution_allowed(), ExecutionQuarantinedError, RuntimeError, Execution quarantine.  The project is now a strictly read-only MLB/NBA betting *, Raised when quarantined execution code attempts to contact an exchange., Raise unless execution has been explicitly un-quarantined in source., KalshiRestTransport, KalshiWsFeed (+6 more)

### Community 59 - "Probability Pipeline"
Cohesion: 0.16
Nodes (17): CalibrationReport, build_mlb_dataset(), build_nba_dataset(), _finish(), GameStateDataset, ndarray, Historical game-state datasets for MLB and NBA.  Each row is one in-game state p, Split by time: earliest ``train_frac`` for training, rest for test. (+9 more)

### Community 60 - "Data Architecture Foundation Roadmap"
Cohesion: 0.09
Nodes (25): Hot Decision Path Constraints, Optional Low-Latency Lane, Research Lane, Append-Only History, Canonical Identifier System, Raw Response Provenance, SQLite Historical Corpus, Data Foundation Roadmap (+17 more)

### Community 61 - "Sports Quant Odds API"
Cohesion: 0.17
Nodes (17): A tiny monotonic-clock TTL cache for provider responses.  The Odds API bills per, In-memory TTL cache. Not shared across processes; safe for a single loop., ResponseCache, Read-only public-data provider adapters (The Odds API, Kalshi public REST)., normalize_event(), NormalizedBookmaker, NormalizedEvent, NormalizedMarket (+9 more)

### Community 62 - "Sports Quant Test Db Cli"
Cohesion: 0.19
Nodes (22): CaptureFixture, main(), CLI dispatch.      Usage::          python -m sports_quant providers-check, Create, migrate and seed the local corpus database.      Offline: no network cal, run_db_init(), Path, ``python -m sports_quant db-init``: output, exit codes, repeat safety.  Every te, The read-only invariants gate db-init exactly as they gate everything else. (+14 more)

### Community 63 - "Sports Quant Test Phase D"
Cohesion: 0.22
Nodes (23): Audit one provider's capabilities/tier. GET-only; ``--dry-run`` persists nothing, run_provider_audit(), declaration_for(), The static capability declaration for a provider (BALLDONTLIE by tier)., _bdl_client(), _bdl_decl(), _decl(), _mlb_client() (+15 more)

### Community 64 - "Sports Quant Game Repository Protocol"
Cohesion: 0.12
Nodes (10): Game, GameStatusRecord, One append-only observation of a game's status.      ``observed_at`` is the poin, GameRepositoryProtocol, Protocol, Row, Create a game.          ``original_start`` is set from ``scheduled_start`` here, Every status observation for a game, oldest observation first. (+2 more)

### Community 65 - "Backtest Test"
Cohesion: 0.15
Nodes (21): grade_dataset(), break_even_latency(), Latency at which mean expected profit crosses zero (linear interp).      Returns, ev(), Tests for event-replay and latency backtesting (Module 7)., One market exercising every event type and all fill outcomes across     the late, run_scenario(), scenario() (+13 more)

### Community 66 - "Probability Features"
Cohesion: 0.15
Nodes (19): mlb_vector(), MLBLiveState, nba_vector(), NBALiveState, _possession_val(), ndarray, Fixed-size feature vectors for live win-probability inference.  Every live event, Vectorize an MLB live state into MLB_SPEC layout (float32). (+11 more)

### Community 67 - "Sports Quant Balldontlie"
Cohesion: 0.11
Nodes (19): first_game_id_and_date(), game_id_from_payload(), BALLDONTLIE client (read-only, GET-only) for NBA data.  Authentication is a sing, Extract the first valid provider game id from a ``/v1/games`` payload.      Retu, Extract ``(game_id, game_date)`` from the first valid game in a payload.      ``, Whether a ``/v1/plays`` payload actually contains substitution events.      Subs, GET /v1/box_scores?date=YYYY-MM-DD -- team/box statistics (GOAT-gated)., GET /nba/v1/stats/advanced -- advanced statistics (GOAT-gated).          Uses th (+11 more)

### Community 68 - "Sports Quant Provider Error"
Cohesion: 0.15
Nodes (11): BaseProviderClient, ProviderError, Any, AsyncClient, Response, RuntimeError, Perform one GET, returning parsed JSON + a sanitized RawExchange.          ``sec, Read a streamed body, aborting once it exceeds the size cap.          Returns `` (+3 more)

### Community 69 - "Sports Quant Kalshi Client"
Cohesion: 0.18
Nodes (8): KalshiCapturedPage, KalshiClient, KalshiPage, Any, BaseModel, A paginated Kalshi listing plus the sanitized raw exchange of each page.      Th, Async, read-only, unauthenticated adapter for Kalshi public REST., One or more pages of a paginated list endpoint.      ``items`` aggregates every

### Community 70 - "Tracking Frame Manifest"
Cohesion: 0.14
Nodes (10): FrameManifest, InMemoryManifestRepository, ManifestRepository, PostgresManifestRepository, Protocol, Metadata describing one stored batch/partition of frame data., Non-durable manifest store for tests and local runs., Durable manifest store in PostgreSQL.      This is metadata only and lives off t (+2 more)

### Community 71 - "Gateway Execution"
Cohesion: 0.17
Nodes (6): ExecutionGateway, Idempotent transport submit. A repeated client order id returns the         cach, Cancel all resting orders for a market (used on pause)., Fill, OrderIntent, OrderRecord

### Community 72 - "Sports Quant Initialize Database"
Cohesion: 0.30
Nodes (20): Ingest MLB official data. GET-only, no key; ``--dry-run`` persists nothing., run_ingest_mlb(), initialize_database(), Path, Create, migrate and seed the corpus database.      Safe to run repeatedly: migra, _client(), Any, Path (+12 more)

### Community 73 - "Sports Quant Build Readonly Client"
Cohesion: 0.14
Nodes (19): build_readonly_client(), AsyncClient, Build an ``httpx.AsyncClient`` whose every request clears the policy., _mlb_client(), Two venues sharing a provider id is an ambiguity: surfaced, never resolved., A second audit appends new point-in-time rows; the first belief survives., test_audit_appends_history_never_overwrites(), test_every_request_is_get() (+11 more)

### Community 74 - "Sports Quant Ingestion Runs"
Cohesion: 0.14
Nodes (9): IngestionRun, One invocation of an ingest command, from request to terminal status.      ``rec, IngestionRunRepositoryProtocol, Protocol, Row, Ingestion-run repository.  One row records the whole life of an ingest invocatio, Close a run with its terminal status and counters.          ``status`` must be a, Operations the ingestion lane needs from a run store. (+1 more)

### Community 75 - "Intel Report Registry"
Cohesion: 0.13
Nodes (10): date, PollSchedule, datetime, Fetch-and-parse a report, skipping unchanged re-fetches.          Computing the, Extract player/team/status/reason observations from a raw report., A daily release schedule (requirement 4: poll on the published cadence).      ``, Tracks the content hash of fetched reports per source to spot new ones., Whether this report differs from the last one seen for the source. (+2 more)

### Community 76 - "Intel Player Ref"
Cohesion: 0.15
Nodes (11): PlayerRef, Identity of a player. ``player_id`` is a provider-stable id when known., MatchResult, MatchStatus, normalize_name(), PlayerDirectory, str, Deterministic player matching.  Matching is intentionally *not* fuzzy: it normal (+3 more)

### Community 77 - "Probability Reference"
Cohesion: 0.15
Nodes (13): AnalyticReference, approximation_report(), ApproxReport, ApproxThresholds, GenerativeTruth, ndarray, Protocol, Reference model and approximation-error accounting.  The full Monte Carlo simula (+5 more)

### Community 78 - "Tracking Missing Dependency Error"
Cohesion: 0.12
Nodes (17): ImportError, MissingTrackingDependencyError, pyarrow_available(), Raised when frame-level Parquet storage is used without pyarrow.      Subclasses, Whether pyarrow can be imported.      Lets tests skip frame-storage cases cleanl, pyarrow_hidden(), Make pyarrow un-importable for the duration of the block., The module must import in an environment that omits the optional extra. (+9 more)

### Community 79 - "Probability Residual Win Prob Model"
Cohesion: 0.23
Nodes (10): _fit_linear(), _logloss(), ndarray, Fast live residual win-probability models.  The model is a logistic regression w, Fit candidate models, select the champion by validation log-loss, and     train, Champion model plus a bootstrap ensemble for uncertainty., (K, N) matrix of ensemble member probabilities., ResidualWinProbModel (+2 more)

### Community 80 - "Sports Quant Player"
Cohesion: 0.14
Nodes (7): Player, PlayerAliasRepositoryProtocol, PlayerRepositoryProtocol, Protocol, Operations Phase A needs from a player-alias store., Operations Phase A needs from a player store., Create a player.          ``suffix`` is stored separately from ``full_name``: "K

### Community 81 - "Sports Quant Repository"
Cohesion: 0.17
Nodes (7): Any, Connection, Row, Base class holding the connection and small row helpers., Repository, Append-only posted-lineup observations with ordered players., SqliteLineupRepository

### Community 82 - "Sports Quant Read Only Policy"
Cohesion: 0.18
Nodes (16): RuntimeError, Raised when a request violates the hard read-only networking policy., ReadOnlyPolicyError, test_unapproved_mlb_paths_blocked(), Read-only networking policy: the hard safety boundary.  Proves that write verbs, test_account_and_portfolio_paths_blocked(), test_approved_get_paths_allowed(), test_only_approved_hosts_accepted() (+8 more)

### Community 83 - "State Mlbgame"
Cohesion: 0.18
Nodes (3): MLBGameState, Any, test_mlb_state_hash_is_deterministic()

### Community 85 - "Sports Quant Run Ingest Odds"
Cohesion: 0.35
Nodes (15): Ingest current Odds API prices for one sport into the corpus.      Read-only and, run_ingest_odds(), _client(), Path, Request, ``ingest-odds`` CLI: exit codes, dry-run, key never printed, GET-only.  The co, _settings(), test_active_failure_exits_one() (+7 more)

### Community 86 - "Sports Quant Test Kalshi Ingest"
Cohesion: 0.35
Nodes (15): Ingest Kalshi public events/markets (and optionally books/trades).      Read-onl, run_ingest_kalshi(), _client(), _ok_handler(), Path, Request, Response, ``ingest-kalshi`` CLI: exit codes, dry-run, zero-results, GET-only, no credentia (+7 more)

### Community 87 - "Sports Quant Sqlite Provider Reference"
Cohesion: 0.23
Nodes (8): ProviderReference, A provider id crosswalk to a canonical entity.      Used for teams/players/games, ProviderReferenceRepositoryProtocol, Protocol, Row, Storage for the three provider crosswalk tables., Insert a reference, or refresh its current provenance if newer.          Returns, SqliteProviderReferenceRepository

### Community 88 - "Sports Quant Raw Response"
Cohesion: 0.17
Nodes (7): A provider response preserved exactly as received, minus any credential.      ``, RawResponse, Protocol, Row, The earliest response with this content hash, if any.          Traceability, not, Operations the ingestion lane needs from a raw-response store., RawResponseRepositoryProtocol

### Community 89 - "Sports Quant Run Ingest Venues"
Cohesion: 0.20
Nodes (15): Printer, _db_ready_or_exit(), Path, Print a sanitized Kalshi ingestion summary. No credential is ever shown., Return an exit code if the DB is missing/unmigrated, else ``None``., Seed venues from MLB StatsAPI. GET-only, no key; ``--dry-run`` persists nothing., Ingest posted lineups. Only ``--sport mlb`` is supported in D2., _report_audit() (+7 more)

### Community 90 - "Sports Quant Check Odds API"
Cohesion: 0.17
Nodes (14): _check_kalshi(), _check_odds_api(), _check_sport(), CheckResult, _describe(), _fmt_credits(), BaseException, Render an exception safely (provider adapters already sanitize URLs). (+6 more)

### Community 91 - "Streaming NATS Event Bus"
Cohesion: 0.19
Nodes (5): NatsEventBus, A stand-in envelope for a message we could not decode.      Lets the dead-letter, Publishes and consumes envelopes over NATS JetStream., Return the JetStream context, or explain that connect() is required.          Ev, _unparseable_placeholder()

### Community 92 - "Sports Quant Run Providers Check"
Cohesion: 0.36
Nodes (13): MonkeyPatch, Run the read-only provider check.      Returns ``0`` when nothing active failed, run_providers_check(), _install_mock(), Request, ``providers-check`` behaviour: status classification, exit codes, redaction.  Th, Make ``build_readonly_client`` wrap a MockTransport instead of the network., _settings() (+5 more)

### Community 93 - "Sports Quant Schema"
Cohesion: 0.18
Nodes (12): from_iso(), normalize_optional(), datetime, Storage conventions: timestamp format, enumerations, table registry.  SQLite has, Current UTC time. The one wall-clock entry point for the db package., Human label for a season.      Baseball seasons sit inside one calendar year; ba, Collapse an empty-or-whitespace string to None., Parse a stored timestamp back into an aware UTC datetime. (+4 more)

### Community 94 - "Sports Quant Validate Trade"
Cohesion: 0.14
Nodes (14): normalize_provider_time(), _NormTrade, Classify a supplied timestamp.      Returns the normalized ISO string when suppl, Validate and normalize one raw public trade, with a strengthened identity., Normalize a provider timestamp to the corpus ISO format, or ``None``.      Accep, _valid_price(), validate_trade(), _validated_time() (+6 more)

### Community 95 - "Sports Quant Test Isolation"
Cohesion: 0.21
Nodes (12): Path, _python_files(), Hot-path isolation: the database must not reach the live decision path.  ``CLAUD, The quarantined execution lane must stay unreachable from the corpus., Source files of a package, excluding caches and its own test modules.      Test, Catches an indirect import that a source-text scan would miss., Phase A is entirely offline; no provider client belongs in it., test_db_package_does_not_import_execution_code() (+4 more)

### Community 96 - "Streaming Sqlite Dedup Store"
Cohesion: 0.18
Nodes (4): Record the event as processed. Returns ``True`` if newly recorded., Atomically test-and-set.          Returns ``True`` if this is the first time we', Durable dedup store backed by a single sqlite table.      Durability is what let, SqliteDedupStore

### Community 97 - "Streaming In Memory Dedup Store"
Cohesion: 0.15
Nodes (5): DedupStore, InMemoryDedupStore, Protocol, Record ``key``. Returns ``True`` if newly added, ``False`` if it was         alr, Bounded LRU set. Non-durable.

### Community 98 - "Codex Graphify Pipeline"
Cohesion: 0.18
Nodes (12): Graphify-First Codebase Navigation, Incremental Folder Watch, URL Ingestion, Graph Export Formats, Semantic Extraction Contract, Cross-Repository Graph Merge, Graphify Commit Hooks, Constrained Query Expansion (+4 more)

### Community 99 - "Backtest Latency Model"
Cohesion: 0.20
Nodes (4): _default_delays(), LatencySample, Configurable latency model for the eight pipeline stages.  Each stage has a mean, StageDelay

### Community 100 - "Evaluation Portfolio"
Cohesion: 0.22
Nodes (3): Portfolio, Largest size we may add for ``market`` without breaching limits., Open (risk-increasing) orders for the market that should be pulled.

### Community 101 - "Probability Train And Build"
Cohesion: 0.27
Nodes (9): brier_score(), calibration_report(), ndarray, Probability calibration metrics.  A live win probability is only useful if it is, ReliabilityBin, train_and_build(), mlb_artifacts(), nba_artifacts() (+1 more)

### Community 102 - "Tracking Aggregations"
Cohesion: 0.29
Nodes (10): ShotLike, _distance_2d(), Feature aggregators over event-level and frame-level tracking data.  Two clearly, Straight-line distance (ft) from the shot location to the rim.      Returns ``No, Classify a shot into a court zone from its event coordinate.      Zones: ``restr, shot_distance(), shot_zone(), _xy() (+2 more)

### Community 103 - "Gateway Arming Controller"
Cohesion: 0.24
Nodes (6): ArmError, ArmingController, Exception, Raised when a live order is attempted without valid arming., Gate every order. Demo passes; live requires being armed., test_arm_requires_correct_token()

### Community 104 - "Sports Quant Validate Market"
Cohesion: 0.22
Nodes (9): _NormMarket, Validate and normalize one raw Kalshi market.      Rejects a missing/blank ticke, _rules_hash(), validate_market(), test_validate_market_absent_times_are_none(), test_validate_market_expected_expiration_fallback(), test_validate_market_malformed_time_is_rejected_not_nulled(), test_validate_market_rejects_close_before_open() (+1 more)

### Community 105 - "Sports Quant Normalize Venue"
Cohesion: 0.32
Nodes (8): test_normalize_venue_blank_name_rejected(), test_normalize_venue_maps_roof_and_coords(), normalize_venue(), _NormVenue, _opt_float(), _opt_str(), Any, Validate + normalize one StatsAPI venue object.      Rejects a blank name and cl

### Community 106 - "Sports Quant Test Kalshi"
Cohesion: 0.36
Nodes (7): _client_with_handler(), Kalshi public-data adapter: order-book derivation, pagination, GET-only., test_empty_orderbook_yields_no_asks(), test_exchange_status_parsed(), test_get_market_orderbook_is_get_only_and_parsed(), test_orderbook_asks_derived_from_opposing_bids(), test_pagination_follows_cursor()

### Community 107 - "Sports Quant Test Phase D"
Cohesion: 0.29
Nodes (6): Path, Phase D1 isolation + safety: gateway quarantine, no credential/signing.  Static, The new repositories must not import the quarantined execution gateway., test_db_package_still_isolated_from_execution(), test_phase_d_provider_source_has_no_signing_tokens(), test_phase_d_source_does_not_import_gateway()

### Community 108 - "Sports Quant Test D011 Official"
Cohesion: 0.43
Nodes (6): Connection, Migration d011: Phase D2 official-snapshot tables + append-only guards.  Schema-, test_all_d2_tables_exist(), test_d2_tables_are_append_only(), test_d2_tables_registered_in_append_only(), test_mapped_status_check_rejects_unknown_value()

### Community 109 - "Sports Quant Test Kalshi Safety"
Cohesion: 0.33
Nodes (6): Path, Static safety guarantees for the Kalshi public ingestion path.  These assert, by, The Kalshi client is built with no auth/signing headers., test_kalshi_client_sends_no_default_headers(), test_kalshi_source_does_not_import_gateway(), test_no_credential_or_signing_in_kalshi_source()

### Community 110 - "Phase D Implementation Plan Provider"
Cohesion: 0.33
Nodes (6): Provider Capability System, Unavailable Data Contract, Provider Licensing and Reliability Risks, Selected Provider Stack, Optional Frame-Level Data, Positional Data Architecture

### Community 112 - "Sports Quant Provider Audit Result"
Cohesion: 0.33
Nodes (3): ProviderAuditResult, Sanitized outcome of one provider audit, safe to print/JSON., CLI must return a non-zero exit for a failed OR partially-failed audit.

### Community 113 - "Sports Quant"
Cohesion: 0.33
Nodes (4): AsyncClient, Request, Response, SecretStr

### Community 115 - "Tracking Error"
Cohesion: 0.40
Nodes (5): Exception, Base class for tracking errors., Raised when an optional adapter is used without being configured., TrackingError, TrackingNotConfigured

## Knowledge Gaps
- **15 isolated node(s):** `sports-quant`, `URL Ingestion`, `Incremental Folder Watch`, `Graph Export Formats`, `Cross-Repository Graph Merge` (+10 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LatencyRegistry` connect `Streaming Latency Registry` to `Evaluation Portfolio`, `Evaluation Components`, `Gateway Latency Histogram`, `Probability Inference Engine`, `State Event Envelope`, `Streaming Latency Snapshot`, `Streaming Test`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Why does `Database` connect `Sports Quant Database` to `Sports Quant Odds Ingestor`, `Sports Quant Test Phase D1`, `Sports Quant Test Repositories`, `Sports Quant Cli`, `Sports Quant Db`, `Sports Quant Test Phase D2`, `Sports Quant Test Seeds`, `Sports Quant MLB Ingestor`, `Sports Quant Test Kalshi Ingestor`, `Sports Quant Test Migrations`, `Sports Quant Kalshi Ingestor`, `Sports Quant Sqlite Team Repository`, `Sports Quant Transaction`, `Sports Quant Migrate`, `Sports Quant Test Price Snapshot`, `Sports Quant Test Phase D`, `Sports Quant Initialize Database`, `Sports Quant Build Readonly Client`, `Sports Quant Run Ingest Odds`, `Sports Quant Test Kalshi Ingest`, `Sports Quant Run Ingest Venues`?**
  _High betweenness centrality (0.094) - this node is a cross-community bridge._
- **Why does `monotonic_ns()` connect `Evaluation Components` to `Streaming Latency Registry`, `Gateway Execution`, `Gateway Latency Histogram`, `Probability Inference Engine`, `State Event Envelope`, `Gateway Kalshi Limits`, `Gateway Components`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Are the 38 inferred relationships involving `EventEnvelope` (e.g. with `ApplyResult` and `ApplyStatus`) actually correct?**
  _`EventEnvelope` has 38 INFERRED edges - model-reasoned connections that need verification._
- **Are the 44 inferred relationships involving `Repository` (e.g. with `CapabilityRepositoryProtocol` and `SqliteCapabilityRepository`) actually correct?**
  _`Repository` has 44 INFERRED edges - model-reasoned connections that need verification._
- **What connects `sports-quant`, `URL Ingestion`, `Incremental Folder Watch` to the rest of the system?**
  _15 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Sports Quant Odds Ingestor` be split into smaller, more focused modules?**
  _Cohesion score 0.051392632524708 - nodes in this community are weakly interconnected._