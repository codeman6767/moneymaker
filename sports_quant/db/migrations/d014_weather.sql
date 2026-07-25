-- Migration d014: Phase D4 weather observations (forward-only).
--
-- Version 014: migration numbers are a single global forward-only sequence;
-- a001..d013 are immutable and are NOT edited. This migration adds the single
-- append-only `weather_snapshots` table the D4 ingestor writes.
--
-- Design (consistent with the append-only official-observation tables):
--   * One row per (game, weather kind, valid time, product/source, observation).
--     Anchored on `provider_game_references` (its official game identity) and the
--     existing canonical `venues` row -- NO second venue/game/canonical system.
--   * Append-only with transition-aware dedup
--     `UNIQUE (game_ref_id, weather_kind, valid_time, forecast_mode, observed_at,
--     content_hash)` and BEFORE UPDATE/DELETE triggers. Current state derives from
--     the newest observation; A -> B -> A and out-of-order backfills are preserved
--     (a GLOBAL content-hash uniqueness rule is deliberately avoided).
--   * FORECASTS, STATION OBSERVATIONS, HISTORICAL FORECASTS, and REANALYSIS are
--     distinct `weather_kind` values -- never conflated. `observed_at`
--     (= raw_responses.received_at) is when THIS project received the response; it
--     is NEVER backdated to a historical model-run time. `model_reference_time`,
--     `provider_available_at`, and `lead_time_seconds` are recorded only when the
--     provider genuinely supplies/derives them, never invented.
--   * Point-in-time eligibility (`pit_eligible`) is 1 ONLY when the implementation
--     can prove the forecast was available before a cutoff; 0 when it cannot; NULL
--     when unknown. A reanalysis/observation row is never PIT-eligible as a pregame
--     forecast. Phase E must consult this flag, not the endpoint of origin.
--   * Canonical internal units: temperature degC, wind m/s, direction degrees,
--     precipitation mm, humidity + precip probability percent. Missing stays NULL;
--     an explicit provider zero stays 0.

CREATE TABLE weather_snapshots (
    weather_id             TEXT PRIMARY KEY,
    game_ref_id            TEXT NOT NULL REFERENCES provider_game_references(reference_id),
    provider               TEXT NOT NULL,
    provider_game_id       TEXT NOT NULL,
    venue_id               TEXT NOT NULL REFERENCES venues(venue_id),
    -- current_forecast | station_observation | historical_forecast | reanalysis
    weather_kind           TEXT NOT NULL,
    -- applicable | conditional_roof_unknown  (not-applicable venues are skipped,
    -- never persisted as a synthetic observation).
    applicability          TEXT NOT NULL,
    -- The exact product/source the row came from (e.g. nws_hourly_forecast,
    -- nws_station_observation, open_meteo_forecast, open_meteo_historical_forecast,
    -- open_meteo_archive). Participates in the transition anchor.
    forecast_mode          TEXT NOT NULL,
    roof_type_at_decision  TEXT,
    requested_latitude     REAL,
    requested_longitude    REAL,
    source_station         TEXT,
    weather_model          TEXT,
    -- Times: valid_time is the hour this weather is valid for. forecast_target_time
    -- is the target instant of a forecast (usually == valid_time). The provider
    -- model run/reference time and provider-availability time are recorded ONLY
    -- when genuinely supplied. lead_time_seconds only when genuinely derivable.
    valid_time             TEXT,
    forecast_target_time   TEXT,
    model_reference_time   TEXT,
    provider_available_at  TEXT,
    lead_time_seconds      INTEGER,
    -- 1 = proven available before a cutoff; 0 = not eligible; NULL = unknown.
    pit_eligible           INTEGER,
    temperature_c          REAL,
    apparent_temperature_c REAL,
    dew_point_c            REAL,
    relative_humidity_pct  REAL,
    wind_speed_ms          REAL,
    wind_gust_ms           REAL,
    wind_direction_deg     REAL,
    precip_probability_pct REAL,
    precip_amount_mm       REAL,
    weather_code           TEXT,
    condition_text         TEXT,
    -- Typed extra metadata: the provider's own unit/representation for any value
    -- that could not be normalized deterministically (canonical JSON). Never a
    -- fabricated value; the normalized field stays NULL and a DQ note is recorded.
    extra                  TEXT,
    provider_timestamp     TEXT,
    published_at           TEXT,
    observed_at            TEXT NOT NULL,
    retrieved_at           TEXT NOT NULL,
    ingested_at            TEXT NOT NULL,
    run_id                 TEXT REFERENCES ingestion_runs(run_id),
    raw_response_id        TEXT NOT NULL REFERENCES raw_responses(raw_response_id),
    raw_response_hash      TEXT NOT NULL,
    content_hash           TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    CONSTRAINT wx_id_prefix CHECK (weather_id LIKE 'wx\_%' ESCAPE '\'),
    CONSTRAINT wx_provider_present CHECK (provider <> ''),
    CONSTRAINT wx_provider_game_present CHECK (provider_game_id <> ''),
    CONSTRAINT wx_kind_valid CHECK (weather_kind IN (
        'current_forecast', 'station_observation', 'historical_forecast', 'reanalysis'
    )),
    CONSTRAINT wx_applicability_valid CHECK (applicability IN (
        'applicable', 'conditional_roof_unknown'
    )),
    CONSTRAINT wx_forecast_mode_present CHECK (forecast_mode <> ''),
    CONSTRAINT wx_roof_valid CHECK (roof_type_at_decision IS NULL OR roof_type_at_decision IN (
        'open', 'retractable', 'dome', 'fixed', 'indoor'
    )),
    CONSTRAINT wx_lat_range CHECK (requested_latitude IS NULL
        OR (requested_latitude >= -90.0 AND requested_latitude <= 90.0)),
    CONSTRAINT wx_lon_range CHECK (requested_longitude IS NULL
        OR (requested_longitude >= -180.0 AND requested_longitude <= 180.0)),
    CONSTRAINT wx_lead_nonneg CHECK (lead_time_seconds IS NULL OR lead_time_seconds >= 0),
    CONSTRAINT wx_pit_bool CHECK (pit_eligible IS NULL OR pit_eligible IN (0, 1)),
    CONSTRAINT wx_humidity_pct CHECK (relative_humidity_pct IS NULL
        OR (relative_humidity_pct >= 0.0 AND relative_humidity_pct <= 100.0)),
    CONSTRAINT wx_precip_prob_pct CHECK (precip_probability_pct IS NULL
        OR (precip_probability_pct >= 0.0 AND precip_probability_pct <= 100.0)),
    CONSTRAINT wx_wind_dir CHECK (wind_direction_deg IS NULL
        OR (wind_direction_deg >= 0.0 AND wind_direction_deg <= 360.0)),
    CONSTRAINT wx_wind_speed_nonneg CHECK (wind_speed_ms IS NULL OR wind_speed_ms >= 0.0),
    CONSTRAINT wx_wind_gust_nonneg CHECK (wind_gust_ms IS NULL OR wind_gust_ms >= 0.0),
    CONSTRAINT wx_precip_nonneg CHECK (precip_amount_mm IS NULL OR precip_amount_mm >= 0.0),
    CONSTRAINT wx_observed_iso CHECK (observed_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT wx_retrieved_iso CHECK (retrieved_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT wx_ingested_iso CHECK (ingested_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT wx_created_iso CHECK (created_at LIKE '____-__-__T__:__:__%Z'),
    CONSTRAINT wx_transition_unique
        UNIQUE (game_ref_id, weather_kind, valid_time, forecast_mode, observed_at, content_hash)
);

CREATE INDEX idx_wx_game
    ON weather_snapshots (game_ref_id, weather_kind, valid_time, observed_at);
CREATE INDEX idx_wx_venue ON weather_snapshots (venue_id, observed_at);

CREATE TRIGGER trg_wx_no_update
BEFORE UPDATE ON weather_snapshots
BEGIN
    SELECT RAISE(ABORT, 'weather_snapshots is append-only');
END;
CREATE TRIGGER trg_wx_no_delete
BEFORE DELETE ON weather_snapshots
BEGIN
    SELECT RAISE(ABORT, 'weather_snapshots is append-only');
END;
