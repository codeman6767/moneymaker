# Graph Report - moneymaker  (2026-07-23)

## Corpus Check
- 239 files · ~202,091 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 3581 nodes · 9700 edges · 149 communities (138 shown, 11 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 592 edges (avg confidence: 0.55)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `74ea14ea`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

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
- test_kalshi_schema.py
- AliasResolution
- ProviderCapability
- engine.py
- Migration
- db/__init__.py
- graphify reference: extra exports and benchmark
- graphify reference: query, path, explain
- graphify reference: add a URL and watch a folder
- graphify reference: commit hook and native CLAUDE.md integration
- graphify reference: incremental update and cluster-only
- .make
- graphify reference: GitHub clone and cross-repo merge
- graphify reference: transcribe video and audio
- db
- CLAUDE.md
- extraction-spec.md
- db

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
- `StrategyDecision` --uses--> `FeeModel`  [INFERRED]
  backtest/backtester.py → evaluation/pricing.py
- `Strategy` --uses--> `FeeModel`  [INFERRED]
  backtest/backtester.py → evaluation/pricing.py
- `EdgeStrategy` --uses--> `FeeModel`  [INFERRED]
  backtest/backtester.py → evaluation/pricing.py
- `DecisionPoint` --uses--> `FeeModel`  [INFERRED]
  backtest/backtester.py → evaluation/pricing.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Graphify Build Query Update Lifecycle** — codex_skills_graphify_skill_graphify_pipeline, codex_skills_graphify_references_extraction_spec_semantic_extraction_contract, codex_skills_graphify_references_query_graph_traversal, codex_skills_graphify_references_update_incremental_graph_update, codex_skills_graphify_references_exports_graph_exports [EXTRACTED 1.00]
- **Historical Data Integrity System** — data_architecture_raw_response_provenance, data_architecture_append_only_history, point_in_time_data_bitemporal_model, point_in_time_data_observed_at_cutoff, point_in_time_data_leakage_prevention [INFERRED 0.95]
- **Safe Read-Only Recommendation Architecture** — claude_research_lane, claude_hot_path_constraints, read_only_architecture_read_only_recommendation_engine, read_only_architecture_execution_quarantine, gateway_phases_legacy_gateway [INFERRED 0.95]

## Communities (149 total, 11 thin omitted)

### Community 0 - "Sports Quant Odds Ingestor"
Cohesion: 0.06
Nodes (101): ClientFactory, KalshiHandler, ingest_kalshi(), Ingest Kalshi public events, markets, and optionally books and trades.      ``--, ingest_odds(), Ingest current odds for one sport.      ``sport`` is the CLI argument (``'mlb'``, client_for(), database() (+93 more)

### Community 1 - "Sports Quant Sqlite Sportsbook Repository"
Cohesion: 0.06
Nodes (54): The stable identity of a betting line, separate from its price.      A changed p, One append-only observation of a price. ``price_american`` is exact., SportsbookEvent, SportsbookMarket, SportsbookOutcome, SportsbookPriceSnapshot, point_key(), price_content_hash() (+46 more)

### Community 2 - "Sports Quant Test Phase D1"
Cohesion: 0.10
Nodes (55): audit_provider(), build_balldontlie_probes(), Audit a provider by running each capability-group probe independently., Dependency-aware probes for the documented BALLDONTLIE GOAT endpoints.      Inde, _bdl_client(), _bdl_decl(), _bdl_routing_handler(), _bdl_status_client() (+47 more)

### Community 3 - "Streaming Latency Registry"
Cohesion: 0.08
Nodes (26): Idempotency layer for at-least-once delivery.  JetStream (and any at-least-once, DeadLetter, DeadLetterQueue, Delivery, EventBus, ProcessAction, ProcessStatus, Enum (+18 more)

### Community 4 - "Backtest Backtester"
Cohesion: 0.09
Nodes (47): BacktestConfig, BacktestReport, DecisionPoint, EdgeStrategy, Protocol, Replay backtester: replay events, apply latency, simulate fills, report.  The st, Reference strategy: take the side whose edge clears a threshold.      Reads the, ReplayBacktester (+39 more)

### Community 5 - "Sports Quant Sqlite Kalshi Repository"
Cohesion: 0.07
Nodes (27): Level, new_kalshi_book_id(), new_kalshi_level_id(), KalshiEvent, KalshiMarket, KalshiOrderbookLevel, KalshiOrderbookSnapshot, KalshiPublicTrade (+19 more)

### Community 6 - "Evaluation Components"
Cohesion: 0.09
Nodes (32): Action, Decision, LimitOrder, MarketEvent, MarketSnapshot, str, Data types for market evaluation: events, snapshots, decisions, orders., A BET decision's trade parameters, proven complete and in range.      :class:`De (+24 more)

### Community 7 - "Sports Quant Test Repositories"
Cohesion: 0.09
Nodes (47): Hash of the *state* an observation reports.      Covers the reported state only,, status_content_hash(), _game(), Connection, Repository behaviour: CRUD, constraints, foreign keys, point-in-time reads., One provider's spelling must not block another's., Shared aliases are ambiguity to record, not a write to reject., The Clippers brand as "LA", but "Los Angeles" could still mean either. (+39 more)

### Community 8 - "Sports Quant Cli"
Cohesion: 0.08
Nodes (33): Printer, CheckStatus, _make_audit_probes(), _mlb_json(), Any, Enum, str, Command-line entry points for the read-only engine.  Currently exposes ``provide (+25 more)

### Community 9 - "Sports Quant Db"
Cohesion: 0.09
Nodes (42): AliasCandidate, _collapse_initials(), _fold_punctuation(), normalize_name(), _ordered(), Deterministic name normalization for team and player aliases.  One function, app, One alias row considered during resolution., Resolve ``raw_name`` against ``candidates`` deterministically.      ``candidates (+34 more)

### Community 10 - "Sports Quant Ids"
Cohesion: 0.05
Nodes (65): _encode(), league_id(), _MonotonicUlidFactory, new_data_quality_id(), new_game_id(), new_game_status_id(), new_ingestion_run_id(), new_inning_line_id() (+57 more)

### Community 11 - "Intel Base Adapter"
Cohesion: 0.09
Nodes (40): ParseResult, PollingReportAdapter, PollResult, Shared adapter machinery: resolution, scheduling and new-report detection., A source polled as whole reports, with byte-level new-report detection., A report row that could not be confidently matched to one player.      Ambiguous, Base for all source adapters., SourceAdapter (+32 more)

### Community 12 - "Sports Quant Database"
Cohesion: 0.14
Nodes (19): Split a migration script into individual statements.      ``sqlite3.Cursor.execu, split_sql_statements(), table_exists(), Connection, Path, Connection policy, PRAGMAs, transactions, and the SQL statement splitter., Guards the whole suite: tests must never touch the real corpus., No shared global connection: two units of work never alias each other. (+11 more)

### Community 13 - "State Event Envelope"
Cohesion: 0.06
Nodes (34): ApplyResult, ApplyStatus, compute_state_hash(), DataQuality, _deep_freeze(), LiveState, now_ns(), Any (+26 more)

### Community 14 - "Sports Quant Test Phase D2"
Cohesion: 0.21
Nodes (49): Database, A SQLite database file plus its connection and migration policy., boxscore(), _count(), game(), _ingest(), linescore(), Any (+41 more)

### Community 15 - "Sports Quant MLB Stats API"
Cohesion: 0.08
Nodes (17): _first_id(), first_person_id_from_roster(), Extract the first valid person id from a StatsAPI ``/roster`` response.      Ros, GET /teams -- teams group; verifies the teams surface for the audit., GET /schedule -- schedules/games, optionally by date and hydrated.          ``da, GET /game/{gamePk}/boxscore -- team + player game statistics.          ``game_pk, GET /game/{gamePk}/linescore -- inning-by-inning + final R/H/E., GET /teams/{id}/roster -- a team's roster (players group).          ``team_id`` (+9 more)

### Community 16 - "Streaming Event Envelope"
Cohesion: 0.07
Nodes (24): kalshi_events(), load_events(), mlb_events(), nba_events(), Fixture loading for live-state tests.  Each JSON fixture describes a subject/pro, CorrectionResult, CorrectionStatus, _Current (+16 more)

### Community 17 - "Sports Quant Test Seeds"
Cohesion: 0.13
Nodes (17): ``db-init``: create the database, apply migrations, seed canonical data.  Kept s, Deterministic, offline seed data for canonical leagues and teams., LeagueSeedResult, Connection, Idempotent application of the canonical league and team seeds.  Everything here, Seed one league and its teams. Idempotent., Seed both leagues, their teams, and their aliases.      The caller supplies the, What one league's seed run did. (+9 more)

### Community 18 - "Sports Quant MLB Ingestor"
Cohesion: 0.14
Nodes (33): Append-only player box lines (batting or pitching)., SqlitePlayerGameStatRepository, _as_dict(), _dry_run_count(), _game_ref(), _normalize_schedule_game(), _NormGame, _now() (+25 more)

### Community 19 - "Sports Quant Repositories"
Cohesion: 0.08
Nodes (34): Team + player game-statistics repositories (append-only, transition-aware).  Anc, Append-only team box lines., SqliteTeamGameStatRepository, Typed repositories for the Phase A tables.  Every repository is a ``Protocol`` p, LineupPlayerInput, Lineup snapshot repository: parent observation + ordered player children.  Appen, One ordered lineup entry as supplied by the provider., Append-only posted-lineup observations with ordered players. (+26 more)

### Community 20 - "Sports Quant Read Only Httppolicy"
Cohesion: 0.06
Nodes (32): AsyncBaseTransport, balldontlie_host_rule(), HostRule, kalshi_host_rule(), mlb_statsapi_host_rule(), nws_host_rule(), odds_api_host_rule(), open_meteo_host_rule() (+24 more)

### Community 21 - "Sports Quant Repository Error"
Cohesion: 0.24
Nodes (17): Connection, Phase D1 database infrastructure: d009 schema, triggers, repositories.  Uses the, _raw(), test_all_d1_tables_present_and_no_account_columns(), test_capability_repository_append_only_and_asof(), test_data_quality_record_and_resolve(), test_foreign_keys_enforced(), test_invalid_enum_values_rejected() (+9 more)

### Community 22 - "Sports Quant Test Kalshi Ingestor"
Cohesion: 0.11
Nodes (40): normalized_key(), Convenience: just the normalized string, suffix removed., Render a datetime in the storage format, normalizing to UTC.      A naive dateti, to_iso(), Ingestion lane: read-only provider fetches persisted into the corpus.  Each inge, american_to_decimal(), american_to_implied(), _append_reason() (+32 more)

### Community 23 - "Sports Quant Models"
Cohesion: 0.08
Nodes (24): new_venue_alias_id(), new_venue_id(), Venue, VenueAlias, str, The result of a mutable-entity upsert, so the ingestor counts accurately.      *, UpsertOutcome, ProviderReferenceRepositoryProtocol (+16 more)

### Community 24 - "Streaming Test"
Cohesion: 0.09
Nodes (30): EventHandler, ReplayPipeline, CorrectionHandler, Tracks the authoritative version of each sequenced event., EventProcessor, InMemoryEventBus, Idempotent, gap-aware, dead-letter-capable processing pipeline., In-process bus that faithfully models at-least-once redelivery.      On a ``RETR (+22 more)

### Community 25 - "Sports Quant Test Phase D"
Cohesion: 0.13
Nodes (24): _bdl_client(), _contract_handler(), _mlb_client(), _ok_handler(), Request, Phase D1 provider infrastructure: capabilities, policies, clients, safety.  Ever, A strict, documentation-faithful mock: it 400s a request that violates the     B, test_401_body_naming_bad_key_is_invalid_key() (+16 more)

### Community 26 - "Gateway Components"
Cohesion: 0.14
Nodes (11): ClientOrderIdFactory, IdempotencyRegistry, Unique client order IDs and an idempotency registry.  Each order intent gets a u, Maps client_order_id -> the ack we already got for it., Execution-gateway configuration.  Demo by default (``CLAUDE.md``: "demo by defau, Phase 1 execution gateway (Python asyncio, Kalshi demo).  Consumes market-data e, OrderAck, OrderState (+3 more)

### Community 27 - "Sports Quant Sqlite Season Repository"
Cohesion: 0.16
Nodes (9): Season, Protocol, Season storage. Seasons are not seeded; Phase D populates them., Operations Phase A needs from a season store., SeasonRepositoryProtocol, SqliteSeasonRepository, season(), test_season_phases_are_separate_rows() (+1 more)

### Community 28 - "State Order Book"
Cohesion: 0.08
Nodes (22): OrderBookState, Any, Cheapest executable price to BUY Yes, derived from best No bid., Cheapest executable price to BUY No, derived from best Yes bid., Set an absolute quantity at a price. qty <= 0 removes the level., ob_event(), Tests for in-memory live state (Module 2)., test_benchmark_runs_and_reports_percentiles() (+14 more)

### Community 29 - "Tracking Base"
Cohesion: 0.09
Nodes (25): _frame_to_rows(), FrameDataUnavailable, FrameParquetStore, FrameSource, Any, Tracking data architecture: the hard line between event and frame data.  This mo, A single optical-tracking frame: all tracked entities at one instant., Abstract, optional source of frame-level tracking.      Concrete adapters (optic (+17 more)

### Community 30 - "Sports Quant Repositories Capabilities"
Cohesion: 0.08
Nodes (32): DataQualityIssue, ProviderCapabilityRecord, One append-only provider-capability record at a tier.      ``is_observed`` disti, capability_content_hash(), Row, Append a capability record. Returns ``(record, inserted)``.          Idempotent, The latest capability observation at or before ``as_of``., The latest **externally observed** record at or before ``as_of``.          Filte (+24 more)

### Community 31 - "Sports Quant Sqlite Game Repository"
Cohesion: 0.08
Nodes (36): Game storage plus append-only status history., Append a status observation and refresh the game's current state.          Retur, Whether this observation reports the same state as the one before it.          ", Set the game's current state from its newest observation.          Ordered by ``, SqliteGameRepository, game_id(), Connection, Game-status history: stale-backfill protection and transition deduplication.  Tw (+28 more)

### Community 32 - "Sports Quant Test Migrations"
Cohesion: 0.09
Nodes (39): discover_migrations(), MigrationChecksumError, MigrationError, Path, Raised when migrations cannot be discovered, ordered, or applied., Raised when an applied migration's file has changed on disk., Load and order every migration file in ``directory``.      Ordering is by the nu, Connection (+31 more)

### Community 33 - "Sports Quant Test Phase D1"
Cohesion: 0.15
Nodes (21): ProviderError, RuntimeError, A sanitized provider failure carrying its classification and exchange.      ``ki, _client(), _ExplodingStream, _json_body_of_length(), A Content-Length that lies (claims tiny) does not defeat the byte counter., A body stream that fails if iterated -- proves the body was never read. (+13 more)

### Community 34 - "Sports Quant Kalshi Ingestor"
Cohesion: 0.07
Nodes (57): orderbook_content_hash(), Identity of an order-book *state*: the full yes/no ladders.      Excludes ``obse, _Ctx, _finish_failed(), _has_duplicate_prices(), _ingest_dry_run(), _ingest_events(), _ingest_markets() (+49 more)

### Community 35 - "Sports Quant Raw Exchange"
Cohesion: 0.11
Nodes (26): merge_secrets(), Shared GET-only provider-client foundation for the Phase D data providers.  Ever, Non-empty secret values, for :func:`build_exchange`'s ``secrets``., build_exchange(), build_exchange_from_parts(), Any, datetime, Response (+18 more)

### Community 36 - "Intel Material Change Detector"
Cohesion: 0.10
Nodes (17): MaterialChange, An immutable, append-only observation of a subject's status., A detected, materially-relevant change with before/after model inputs., StatusSnapshot, An append-only log of status snapshots, indexed by subject., Append a snapshot. Returns False if an identical one already exists.          "I, Most recent snapshot for the subject from a *different* source.          Used to, StatusHistory (+9 more)

### Community 37 - "Sports Quant Odds API Client"
Cohesion: 0.12
Nodes (13): Headers, OddsApiClient, Any, AsyncClient, datetime, Request, Response, SecretStr (+5 more)

### Community 38 - "Sports Quant Sqlite Team Repository"
Cohesion: 0.13
Nodes (8): Team, TeamAlias, Row, Operations Phase A needs from a team store., Team storage. ``team_id`` is deterministic from (league, abbreviation)., SqliteTeamRepository, TeamRepositoryProtocol, test_reseeding_does_not_duplicate_rows()

### Community 39 - "Sports Quant Sqlite Team Alias"
Cohesion: 0.08
Nodes (29): Team-alias storage and deterministic resolution.      Uniqueness is scoped to th, Flag every alias whose normalized form maps to more than one team.          Comp, SqliteTeamAliasRepository, test_alias_add_is_idempotent(), test_alias_normalization_makes_spelling_variants_equal(), test_historical_team_names_resolve(), test_resolve_is_league_scoped(), test_resolve_matches_abbreviation_and_nickname() (+21 more)

### Community 40 - "Gateway Latency Histogram"
Cohesion: 0.08
Nodes (15): GatewayReport, LatencyBenchmark, str, Stage-by-stage latency benchmarking for the execution gateway.  Records the nine, Threads through a single event, recording per-stage deltas., Stage, StageTimer, _stats() (+7 more)

### Community 41 - "Sports Quant Transaction"
Cohesion: 0.07
Nodes (44): Run a block inside one explicit transaction on an existing connection.      Nest, transaction(), Ingestion-run storage., SqliteIngestionRunRepository, Dedup identity of a response: provider + endpoint + params + body.      Identica, response_content_hash(), conn(), Connection (+36 more)

### Community 42 - "Probability Inference Engine"
Cohesion: 0.10
Nodes (16): FeatureSpec, InferenceEngine, PredictionResult, ndarray, In-memory live inference engine.  Loads the champion (and its uncertainty ensemb, Predict from an already-vectorized, fixed-size feature array., Load the model once from disk at process startup., Single-load, in-memory win-probability inference. (+8 more)

### Community 43 - "Sports Quant Utc Now Iso"
Cohesion: 0.20
Nodes (28): Deterministic identity of a public trade.      When the provider supplies a stab, trade_content_hash(), _append_book(), _append_trade(), _extra_raw(), Connection, Kalshi repository: stale-metadata protection, transition-aware order-book dedupl, The current metadata is always traceable to a real raw response. (+20 more)

### Community 44 - "Sports Quant Settings"
Cohesion: 0.22
Nodes (12): RuntimeError, Configuration + read-only startup invariants.  Loads the provider/safety setting, Raised when the read-only startup invariants are not satisfied.      The message, ReadOnlyStartupError, _good_settings(), Read-only startup invariants and secret handling., test_api_key_is_not_revealed_in_repr(), test_canonical_kalshi_url_is_accepted_with_or_without_trailing_slash() (+4 more)

### Community 45 - "Gateway Test"
Cohesion: 0.17
Nodes (22): GatewayConfig, LimitOrderRequest, big_limits(), build_gateway(), make_strategy(), ob_event(), Tests for the benchmarked execution gateway (Module 8, Phase 1)., test_automatic_disarm_after_consecutive_failures() (+14 more)

### Community 46 - "Sports Quant Leagues"
Cohesion: 0.12
Nodes (19): clv_cents(), expected_profit_cents(), FillOutcome, FillResult, str, Fill simulation.  Given the book at the moment an order reaches the exchange (pl, Model-expected edge captured, net of fees. Zero when nothing fills., Closing-line value per contract: closing price minus our fill price. (+11 more)

### Community 47 - "Tracking Test"
Cohesion: 0.13
Nodes (28): defender_distance(), lineup_spacing(), movement_speed(), movement_speed_between(), Instantaneous speed of a tracked player.      Uses provider-reported velocity wh, Speed (distance/time) for a player between two consecutive frames.      Derived, Mean pairwise distance (ft) among a team's tracked players.      A simple spacin, Distance (ft) from the ball handler to the nearest defender.      Requires frame (+20 more)

### Community 48 - "Sports Quant Migrate"
Cohesion: 0.17
Nodes (10): configure_connection(), _now_ms(), Connection, Create the containing directory. The corpus lives outside source., Open a new, fully configured connection. The caller must close it.          Pref, A connection closed deterministically, even on failure., A connection wrapped in one explicit transaction.          Commits on clean exit, Highest applied migration version, or 0 on an empty database. (+2 more)

### Community 49 - "Sports Quant Test Price Snapshot"
Cohesion: 0.27
Nodes (14): Connection, Schema-level guarantees for sportsbook_price_snapshots after migration b006.  Th, Insert a minimal run -> raw -> event -> market -> outcome -> snapshot.      Retu, SQLite backs an inline UNIQUE with an auto-index over the three columns., _seed_one_snapshot(), _table_sql(), test_append_only_triggers_still_present(), test_delete_remains_blocked() (+6 more)

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
Cohesion: 0.14
Nodes (11): Benchmarked execution gateway (Module 8) -- QUARANTINED.  The project is now a s, KalshiLimits, KalshiLimitsProvider, LimitsProvider, Protocol, Kalshi rate limits and endpoint costs, queried at startup.  The gateway queries, Fetches limits/costs from Kalshi at startup (lazy httpx)., StaticLimitsProvider (+3 more)

### Community 58 - "Gateway Kalshi Rest Transport"
Cohesion: 0.13
Nodes (15): ensure_execution_allowed(), ExecutionQuarantinedError, RuntimeError, Execution quarantine.  The project is now a strictly read-only MLB/NBA betting *, Raised when quarantined execution code attempts to contact an exchange., Raise unless execution has been explicitly un-quarantined in source., KalshiRestTransport, KalshiWsFeed (+7 more)

### Community 59 - "Probability Pipeline"
Cohesion: 0.16
Nodes (17): CalibrationReport, build_mlb_dataset(), build_nba_dataset(), _finish(), GameStateDataset, ndarray, Historical game-state datasets for MLB and NBA.  Each row is one in-game state p, Split by time: earliest ``train_frac`` for training, rest for test. (+9 more)

### Community 60 - "Data Architecture Foundation Roadmap"
Cohesion: 0.09
Nodes (25): Hot Decision Path Constraints, Optional Low-Latency Lane, Research Lane, Append-Only History, Canonical Identifier System, Raw Response Provenance, SQLite Historical Corpus, Data Foundation Roadmap (+17 more)

### Community 61 - "Sports Quant Odds API"
Cohesion: 0.15
Nodes (24): A tiny monotonic-clock TTL cache for provider responses.  The Odds API bills per, In-memory TTL cache. Not shared across processes; safe for a single loop., ResponseCache, Read-only public-data provider adapters (The Odds API, Kalshi public REST)., CreditHeaders, normalize_event(), NormalizedBookmaker, NormalizedEvent (+16 more)

### Community 62 - "Sports Quant Test Db Cli"
Cohesion: 0.19
Nodes (22): CaptureFixture, main(), CLI dispatch.      Usage::          python -m sports_quant providers-check, Create, migrate and seed the local corpus database.      Offline: no network cal, run_db_init(), Path, ``python -m sports_quant db-init``: output, exit codes, repeat safety.  Every te, The read-only invariants gate db-init exactly as they gate everything else. (+14 more)

### Community 63 - "Sports Quant Test Phase D"
Cohesion: 0.13
Nodes (33): Audit one provider's capabilities/tier. GET-only; ``--dry-run`` persists nothing, Seed venues from MLB StatsAPI. GET-only, no key; ``--dry-run`` persists nothing., run_ingest_venues(), run_provider_audit(), declaration_for(), The static capability declaration for a provider (BALLDONTLIE by tier)., balldontlie_declaration(), BalldontlieTier (+25 more)

### Community 64 - "Sports Quant Game Repository Protocol"
Cohesion: 0.12
Nodes (10): Game, GameStatusRecord, One append-only observation of a game's status.      ``observed_at`` is the poin, GameRepositoryProtocol, Protocol, Row, Create a game.          ``original_start`` is set from ``scheduled_start`` here, Every status observation for a game, oldest observation first. (+2 more)

### Community 65 - "Backtest Test"
Cohesion: 0.33
Nodes (6): break_even_latency(), build_distributions(), _dist(), Latency analytics: curves, distributions and break-even latency., Latency at which mean expected profit crosses zero (linear interp).      Returns, test_break_even_interpolation()

### Community 66 - "Probability Features"
Cohesion: 0.15
Nodes (19): mlb_vector(), MLBLiveState, nba_vector(), NBALiveState, _possession_val(), ndarray, Fixed-size feature vectors for live win-probability inference.  Every live event, Vectorize an MLB live state into MLB_SPEC layout (float32). (+11 more)

### Community 67 - "Sports Quant Balldontlie"
Cohesion: 0.09
Nodes (25): BalldontlieClient, _clamp_per_page(), first_game_id_and_date(), game_id_from_payload(), BALLDONTLIE client (read-only, GET-only) for NBA data.  Authentication is a sing, Extract the first valid provider game id from a ``/v1/games`` payload.      Retu, Extract ``(game_id, game_date)`` from the first valid game in a payload.      ``, Async, read-only adapter for BALLDONTLIE.      The key is sent as the ``Authoriz (+17 more)

### Community 68 - "Sports Quant Provider Error"
Cohesion: 0.17
Nodes (8): BaseProviderClient, Any, AsyncClient, Response, Perform one GET, returning parsed JSON + a sanitized RawExchange.          ``sec, Read a streamed body, aborting once it exceeds the size cap.          Returns ``, Delay before a retry, honouring ``Retry-After`` when present., Async, read-only, GET-only base client for a single provider host.

### Community 69 - "Sports Quant Kalshi Client"
Cohesion: 0.14
Nodes (10): KalshiCapturedPage, KalshiClient, KalshiPage, Any, BaseModel, Typed asynchronous adapter for Kalshi's public REST API.  Read-only and unauthen, A paginated Kalshi listing plus the sanitized raw exchange of each page.      Th, Async, read-only, unauthenticated adapter for Kalshi public REST. (+2 more)

### Community 70 - "Tracking Frame Manifest"
Cohesion: 0.14
Nodes (10): FrameManifest, InMemoryManifestRepository, ManifestRepository, PostgresManifestRepository, Protocol, Metadata describing one stored batch/partition of frame data., Non-durable manifest store for tests and local runs., Durable manifest store in PostgreSQL.      This is metadata only and lives off t (+2 more)

### Community 71 - "Gateway Execution"
Cohesion: 0.13
Nodes (8): ExecutionGateway, Idempotent transport submit. A repeated client order id returns the         cach, Resolve orders that were submitted but never acknowledged in time., Cancel all resting orders for a market (used on pause)., Query current Kalshi limits/costs and build the local token budget., Fill, OrderIntent, OrderRecord

### Community 72 - "Sports Quant Initialize Database"
Cohesion: 0.23
Nodes (25): _db_ready_or_exit(), Path, Return an exit code if the DB is missing/unmigrated, else ``None``., Ingest MLB official data. GET-only, no key; ``--dry-run`` persists nothing., Ingest posted lineups. Only ``--sport mlb`` is supported in D2., run_ingest_lineups(), run_ingest_mlb(), initialize_database() (+17 more)

### Community 73 - "Sports Quant Build Readonly Client"
Cohesion: 0.07
Nodes (45): build_readonly_client(), AsyncClient, Build an ``httpx.AsyncClient`` whose every request clears the policy., _fetch_schedule(), ingest_lineups(), ingest_mlb(), Ingest posted MLB lineups for a date or a game. ``--dry-run`` persists nothing., Ingest MLB schedule (+ optional per-game data). ``--dry-run`` persists nothing. (+37 more)

### Community 74 - "Sports Quant Ingestion Runs"
Cohesion: 0.16
Nodes (8): IngestionRun, One invocation of an ingest command, from request to terminal status.      ``rec, IngestionRunRepositoryProtocol, Protocol, Row, Close a run with its terminal status and counters.          ``status`` must be a, Operations the ingestion lane needs from a run store., Open a run in the ``started`` state.          ``requested_at`` records when the

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
Cohesion: 0.07
Nodes (22): Player, PlayerAlias, PlayerAliasRepositoryProtocol, PlayerRepositoryProtocol, Protocol, Row, Operations Phase A needs from a player-alias store., Player-alias storage and deterministic resolution. (+14 more)

### Community 81 - "Sports Quant Repository"
Cohesion: 0.08
Nodes (24): For /graphify add and --watch, For /graphify query, For the commit hook and native CLAUDE.md integration, For --update and --cluster-only, /graphify, Honesty Rules, Interpreter guard for subcommands, Part A - Structural extraction for code files (+16 more)

### Community 82 - "Sports Quant Read Only Policy"
Cohesion: 0.18
Nodes (16): RuntimeError, Raised when a request violates the hard read-only networking policy., ReadOnlyPolicyError, test_unapproved_mlb_paths_blocked(), Read-only networking policy: the hard safety boundary.  Proves that write verbs, test_account_and_portfolio_paths_blocked(), test_approved_get_paths_allowed(), test_only_approved_hosts_accepted() (+8 more)

### Community 83 - "State Mlbgame"
Cohesion: 0.18
Nodes (3): MLBGameState, Any, test_mlb_state_hash_is_deterministic()

### Community 85 - "Sports Quant Run Ingest Odds"
Cohesion: 0.13
Nodes (26): BaseSettings, Ingest current Odds API prices for one sport into the corpus.      Read-only and, run_ingest_odds(), load_settings(), Path, Typed application settings loaded from the environment / ``.env``., Return a human-readable list of violated read-only invariants., Raise :class:`ReadOnlyStartupError` unless every invariant holds. (+18 more)

### Community 86 - "Sports Quant Test Kalshi Ingest"
Cohesion: 0.35
Nodes (15): Ingest Kalshi public events/markets (and optionally books/trades).      Read-onl, run_ingest_kalshi(), _client(), _ok_handler(), Path, Request, Response, ``ingest-kalshi`` CLI: exit codes, dry-run, zero-results, GET-only, no credentia (+7 more)

### Community 87 - "Sports Quant Sqlite Provider Reference"
Cohesion: 0.35
Nodes (6): ProviderReference, A provider id crosswalk to a canonical entity.      Used for teams/players/games, Row, Storage for the three provider crosswalk tables., Insert a reference, or refresh its current provenance if newer.          Returns, SqliteProviderReferenceRepository

### Community 88 - "Sports Quant Raw Response"
Cohesion: 0.16
Nodes (9): A provider response preserved exactly as received, minus any credential.      ``, RawResponse, Protocol, Row, The earliest response with this content hash, if any.          Traceability, not, Operations the ingestion lane needs from a raw-response store., Append-only raw-response storage., RawResponseRepositoryProtocol (+1 more)

### Community 89 - "Sports Quant Run Ingest Venues"
Cohesion: 0.12
Nodes (18): alias_specs(), Every ``(alias, alias_type)`` pair implied by a team seed.      Derived rather t, Connection, Seed data: counts, determinism, idempotency, and derived ambiguity., Two fresh corpora must contain byte-identical seed rows., Chicago, New York and Los Angeles each host two MLB teams., The Athletics' canonical name equals their nickname; store it once., test_alias_specs_are_deterministic() (+10 more)

### Community 90 - "Sports Quant Check Odds API"
Cohesion: 0.15
Nodes (8): League, LeagueRepositoryProtocol, Row, Operations Phase A needs from a league store., League storage. The canonical ``league_id`` is derived from the code., Insert a league, or return the existing row unchanged.          Idempotent by de, SqliteLeagueRepository, test_seeded_leagues()

### Community 91 - "Streaming NATS Event Bus"
Cohesion: 0.18
Nodes (6): NatsEventBus, NATS JetStream implementation of :class:`EventBus`.  JetStream is the chosen tra, A stand-in envelope for a message we could not decode.      Lets the dead-letter, Publishes and consumes envelopes over NATS JetStream., Return the JetStream context, or explain that connect() is required.          Ev, _unparseable_placeholder()

### Community 92 - "Sports Quant Run Providers Check"
Cohesion: 0.15
Nodes (25): MonkeyPatch, _check_kalshi(), _check_odds_api(), _check_sport(), CheckResult, _describe(), _fmt_credits(), BaseException (+17 more)

### Community 93 - "Sports Quant Schema"
Cohesion: 0.05
Nodes (43): Typed row models for the Phase A tables.  Frozen dataclasses rather than raw ``s, Any, Connection, Row, RuntimeError, Shared repository plumbing.  Every repository takes a ``sqlite3.Connection`` rat, Raised when a repository operation cannot be completed., Base class holding the connection and small row helpers. (+35 more)

### Community 94 - "Sports Quant Validate Trade"
Cohesion: 0.13
Nodes (5): OrderStatusReport, FakeMarketDataFeed, MarketDataFeed, OrderTransport, Protocol

### Community 95 - "Sports Quant Test Isolation"
Cohesion: 0.21
Nodes (12): Path, _python_files(), Hot-path isolation: the database must not reach the live decision path.  ``CLAUD, The quarantined execution lane must stay unreachable from the corpus., Source files of a package, excluding caches and its own test modules.      Test, Catches an indirect import that a source-text scan would miss., Phase A is entirely offline; no provider client belongs in it., test_db_package_does_not_import_execution_code() (+4 more)

### Community 96 - "Streaming Sqlite Dedup Store"
Cohesion: 0.19
Nodes (6): Deduplicator, Content-hash based idempotency guard., Record the event as processed. Returns ``True`` if newly recorded., Atomically test-and-set.          Returns ``True`` if this is the first time we', Durable dedup store backed by a single sqlite table.      Durability is what let, SqliteDedupStore

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
Cohesion: 0.16
Nodes (14): _pinned_url_violation(), Return a human-readable violation for a pinned base URL, or ``None``.      Stric, Any, AsyncClient, SecretStr, Phase D1 integrity repair: streaming size guard, base-URL pinning, allowlist.  U, test_balldontlie_documented_endpoints_are_allowed(), test_balldontlie_query_params_do_not_affect_path_authorization() (+6 more)

### Community 101 - "Probability Train And Build"
Cohesion: 0.27
Nodes (9): brier_score(), calibration_report(), ndarray, Probability calibration metrics.  A live win probability is only useful if it is, ReliabilityBin, train_and_build(), mlb_artifacts(), nba_artifacts() (+1 more)

### Community 102 - "Tracking Aggregations"
Cohesion: 0.29
Nodes (10): ShotLike, _distance_2d(), Feature aggregators over event-level and frame-level tracking data.  Two clearly, Straight-line distance (ft) from the shot location to the rim.      Returns ``No, Classify a shot into a court zone from its event coordinate.      Zones: ``restr, shot_distance(), shot_zone(), _xy() (+2 more)

### Community 103 - "Gateway Arming Controller"
Cohesion: 0.21
Nodes (7): ArmError, ArmingController, Exception, Arming controller: the hard gate between demo and live orders.  Demo orders are, Raised when a live order is attempted without valid arming., Gate every order. Demo passes; live requires being armed., test_arm_requires_correct_token()

### Community 104 - "Sports Quant Validate Market"
Cohesion: 0.15
Nodes (14): DbInitResult, Outcome of one ``db-init`` run., True when nothing needed applying and no seed row was new., database(), db_path(), initialized(), mlb_league_id(), nba_league_id() (+6 more)

### Community 105 - "Sports Quant Normalize Venue"
Cohesion: 0.22
Nodes (12): test_normalize_venue_blank_name_rejected(), test_normalize_venue_maps_roof_and_coords(), normalize_venue(), _NormVenue, _opt_float(), _opt_str(), _persist(), Any (+4 more)

### Community 106 - "Sports Quant Test Kalshi"
Cohesion: 0.15
Nodes (11): KalshiOrderBook, Cheapest executable price to BUY No, derived from the best Yes bid., A parsed Kalshi order book with derived executable asks.      Levels are ``(pric, Cheapest executable price to BUY Yes, derived from the best No bid., _client_with_handler(), Kalshi public-data adapter: order-book derivation, pagination, GET-only., test_empty_orderbook_yields_no_asks(), test_exchange_status_parsed() (+3 more)

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

### Community 111 - "Streaming Latency Snapshot"
Cohesion: 0.11
Nodes (17): SnapshotDict, MessageContext, ProcessResult, Deliver a run of ready envelopes to the handler in order.          Returns a ``P, NATS-style subject matching supporting ``*`` and ``>`` wildcards., Deliver once with an explicit redelivered flag (test hook)., Transport-level metadata handed to the processor for one delivery., subject_matches() (+9 more)

### Community 112 - "Sports Quant Provider Audit Result"
Cohesion: 0.33
Nodes (3): ProviderAuditResult, Sanitized outcome of one provider audit, safe to print/JSON., CLI must return a non-zero exit for a failed OR partially-failed audit.

### Community 113 - "Sports Quant"
Cohesion: 0.22
Nodes (7): MatchCandidate, MatchDecision, Row, Set the review columns (the only permitted decision mutation)., Match-decision + candidate storage., Record one decision + its candidates atomically.          Enforces the schema-le, SqliteMatchingRepository

### Community 115 - "Tracking Error"
Cohesion: 0.40
Nodes (5): Exception, Base class for tracking errors., Raised when an optional adapter is used without being configured., TrackingError, TrackingNotConfigured

### Community 117 - "Streaming Default Bucket Bounds Ns"
Cohesion: 0.15
Nodes (15): classify_http_status(), _has_tier_evidence(), is_tier_restriction(), ProviderErrorKind, Distinct, non-overlapping classifications of a provider failure.      Each kind, Whether a sanitized body carries explicit plan/tier-restriction evidence.      R, Classify an HTTP failure into a :class:`ProviderErrorKind`.      ``401`` is auth, Whether a failure means a capability is unavailable at the current tier.      A (+7 more)

### Community 131 - "test_kalshi_schema.py"
Cohesion: 0.33
Nodes (12): foreign_keys_enabled(), Connection, Schema-level guarantees for the Kalshi tables (migration c007).  Inspects ``sqli, The public corpus must never carry an account/order/fill/position column., _seed_book_and_trade(), test_duplicate_price_level_rejected_by_unique(), test_foreign_keys_enforced(), test_no_account_scoped_column_exists_anywhere() (+4 more)

### Community 132 - "AliasResolution"
Cohesion: 0.15
Nodes (6): AliasResolution, The result of resolving a raw name.      ``entity_id`` is populated only when ``, The entity id, or ``None`` unless this resolution actually matched.          Pai, Protocol, Operations Phase A needs from a team-alias store., TeamAliasRepositoryProtocol

### Community 133 - "ProviderCapability"
Cohesion: 0.19
Nodes (11): Keep a declaration's nuance when a probe merely proves accessibility.      A 2xx, _reconcile_observed(), CapabilityState, _lower_tier_states(), ProviderCapability, str, The declared state, or ``UNKNOWN_UNTIL_AUDITED`` if undeclared., Whether an ingestor may request this capability now.          ``SUPPORTED``, ``B (+3 more)

### Community 134 - "engine.py"
Cohesion: 0.18
Nodes (10): DatabaseError, _is_statement_end(), MigrationResult, RuntimeError, SQLite engine: connections, transactions, and the migration runner.  Connection, Statement text with comment-only lines removed, for emptiness checks., Whether a ``;`` at this point terminates the statement.      Only a ``CREATE TRI, Base class for database-layer failures. (+2 more)

### Community 135 - "Migration"
Cohesion: 0.24
Nodes (6): AppliedMigration, Migration, One forward-only migration file., A row of the schema-version table., Rows of the schema-version table, oldest first., Migrations not yet applied, verifying the ones that are.

### Community 136 - "db/__init__.py"
Cohesion: 0.20
Nodes (7): Historical-corpus database layer (Phase A).  SQLite storage for the canonical en, AliasMatchStatus, NormalizedName, str, Outcome of resolving a raw name against alias rows.      Mirrors ``intel.player_, The result of normalizing one raw name string., Normalized form with the suffix reattached, for display/debugging.

### Community 137 - "graphify reference: extra exports and benchmark"
Cohesion: 0.22
Nodes (8): graphify reference: extra exports and benchmark, Step 6b - Wiki (only if --wiki flag), Step 7 - Neo4j export (only if --neo4j or --neo4j-push flag), Step 7a - FalkorDB export (only if --falkordb or --falkordb-push flag), Step 7b - SVG export (only if --svg flag), Step 7c - GraphML export (only if --graphml flag), Step 7d - MCP server (only if --mcp flag), Step 8 - Token reduction benchmark (only if total_words > 5000)

### Community 138 - "graphify reference: query, path, explain"
Cohesion: 0.33
Nodes (5): For /graphify explain, For /graphify path, graphify reference: query, path, explain, Step 0 — Constrained query expansion (REQUIRED before traversal), Step 1 — Traversal

### Community 139 - "graphify reference: add a URL and watch a folder"
Cohesion: 0.50
Nodes (3): For /graphify add, For --watch, graphify reference: add a URL and watch a folder

### Community 140 - "graphify reference: commit hook and native CLAUDE.md integration"
Cohesion: 0.50
Nodes (3): For git commit hook, For native CLAUDE.md integration, graphify reference: commit hook and native CLAUDE.md integration

### Community 141 - "graphify reference: incremental update and cluster-only"
Cohesion: 0.50
Nodes (3): For --cluster-only, For --update (incremental re-extraction), graphify reference: incremental update and cluster-only

### Community 145 - "db"
Cohesion: 0.67
Nodes (3): db(), Path, test_no_lineups_module_imports_gateway()

## Knowledge Gaps
- **57 isolated node(s):** `sports-quant`, `graphify`, `Usage`, `What graphify is for`, `Step 0 - GitHub repos and multi-path merge (only if a URL or several paths)` (+52 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **11 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LatencyRegistry` connect `Streaming Latency Snapshot` to `Streaming Latency Registry`, `Evaluation Components`, `Gateway Latency Histogram`, `Probability Inference Engine`, `State Event Envelope`, `Streaming Test`?**
  _High betweenness centrality (0.103) - this node is a cross-community bridge._
- **Why does `Database` connect `Sports Quant Test Phase D2` to `Sports Quant Odds Ingestor`, `Sports Quant Test Phase D1`, `engine.py`, `Migration`, `Sports Quant Cli`, `db/__init__.py`, `Sports Quant Database`, `db`, `Sports Quant MLB Ingestor`, `db`, `Sports Quant Test Kalshi Ingestor`, `Sports Quant Test Migrations`, `Sports Quant Kalshi Ingestor`, `Sports Quant Sqlite Team Repository`, `Sports Quant Transaction`, `Sports Quant Migrate`, `Sports Quant Test Phase D`, `Sports Quant Initialize Database`, `Sports Quant Build Readonly Client`, `Sports Quant Player`, `Sports Quant Run Ingest Odds`, `Sports Quant Test Kalshi Ingest`, `Sports Quant Run Ingest Venues`, `Sports Quant Validate Market`, `Sports Quant Normalize Venue`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._
- **Why does `canonical_json()` connect `Sports Quant Transaction` to `Sports Quant Odds Ingestor`, `Sports Quant Sqlite Sportsbook Repository`, `Sports Quant Kalshi Ingestor`, `Sports Quant Sqlite Kalshi Repository`, `Sports Quant Test Repositories`, `Sports Quant Normalize Venue`, `Sports Quant Ids`, `Intel Base Adapter`, `Sports Quant Utc Now Iso`, `State Event Envelope`, `Streaming Event Envelope`, `Sports Quant MLB Ingestor`, `Intel Components`, `Sports Quant Repositories`, `Sports Quant Test Kalshi Ingestor`, `State Order Book`, `Sports Quant Schema`, `Sports Quant Repositories Capabilities`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._
- **Are the 38 inferred relationships involving `EventEnvelope` (e.g. with `ApplyResult` and `ApplyStatus`) actually correct?**
  _`EventEnvelope` has 38 INFERRED edges - model-reasoned connections that need verification._
- **Are the 44 inferred relationships involving `Repository` (e.g. with `CapabilityRepositoryProtocol` and `SqliteCapabilityRepository`) actually correct?**
  _`Repository` has 44 INFERRED edges - model-reasoned connections that need verification._
- **What connects `sports-quant`, `graphify`, `Usage` to the rest of the system?**
  _57 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Sports Quant Odds Ingestor` be split into smaller, more focused modules?**
  _Cohesion score 0.06025641025641026 - nodes in this community are weakly interconnected._