"""Phase D4 weather ingestion (NWS + Open-Meteo), mocked transports only.

No live provider call. Every NWS/Open-Meteo interaction is a ``httpx.MockTransport``
wrapped in the real read-only policy, so GET-only + host/path allow-lists apply
exactly as live. Covers routing, roof gating, point-in-time honesty, unit
normalization, dedup, dry-run zero-persistence, provenance, and SSRF rejection.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.official_games import SqliteScheduleRepository
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.db.repositories.venues import SqliteVenueRepository
from sports_quant.db.repositories.weather import (
    SqliteWeatherRepository,
    WeatherOutcome,
    WeatherValues,
)
from sports_quant.db.schema import to_iso, utc_now
from sports_quant.http_policy import (
    ReadOnlyHTTPPolicy,
    ReadOnlyPolicyError,
    build_readonly_client,
)
from sports_quant.ingest.weather_ingestor import (
    WeatherClients,
    _pit_for,
    ingest_weather,
    normalize_direction_deg,
    normalize_nws_observations,
    normalize_open_meteo,
    normalize_speed_ms,
    normalize_temperature_c,
)
from sports_quant.providers.base_provider import ProviderError
from sports_quant.providers.nws import NwsClient
from sports_quant.providers.open_meteo import OpenMeteoClient

MLB = "mlb_statsapi"
# Default game start matches the mocked hourly data (2026-07-22 evening) so the
# per-game weather window covers the mocked periods.
DEFAULT_START = "2026-07-22T23:05:00Z"
FUTURE_START = "2999-07-22T23:05:00Z"  # far future -> a forecast now is a pregame forecast
PAST_START = "2020-07-22T23:05:00Z"


# --------------------------------------------------------------------------- #
# Corpus seeding (venue + schedule snapshot + provider game reference)
# --------------------------------------------------------------------------- #
def seed_game(
    db: Database,
    *,
    game_pk: str = "745804",
    venue_pid: str = "3",
    roof: Optional[str] = "open",
    country: Optional[str] = "USA",
    lat: Optional[float] = 42.35,
    lon: Optional[float] = -71.10,
    tz: Optional[str] = "America/New_York",
    start: Optional[str] = DEFAULT_START,
    date: Optional[str] = "2026-07-22",
    venue_name: str = "Fenway Park",
    seed_venue: bool = True,
) -> None:
    with db.connection() as c, transaction(c):
        runs = SqliteIngestionRunRepository(c)
        run = runs.start(command="seed", provider=MLB, operation="seed", args_json="{}",
                         started_monotonic_ns=0, tool_version="t")
        raw = SqliteRawResponseRepository(c).store(
            run_id=run.run_id, provider=MLB, endpoint="/api/v1/schedule",
            request_params_json="{}", http_status=200, response_headers_json="{}",
            requested_at=to_iso(utc_now()), received_at=to_iso(utc_now()), elapsed_ns=1,
            body="{}", content_hash=response_content_hash(
                provider=MLB, endpoint="/x", request_params={}, body=game_pk))
        if seed_venue:
            v = SqliteVenueRepository(c)
            venue, _ = v.upsert(
                name=venue_name, raw_response_id=raw.raw_response_id,
                raw_response_hash=raw.content_hash, observed_at=raw.received_at,
                country=country, latitude=lat, longitude=lon, timezone=tz, roof_type=roof)
            v.add_alias(venue_id=venue.venue_id, alias=venue_name, provider=MLB,
                        provider_venue_id=venue_pid)
        refs = SqliteProviderReferenceRepository(c)
        gref, _ = refs.upsert(
            kind="game", provider=MLB, provider_entity_id=game_pk,
            raw_response_id=raw.raw_response_id, raw_response_hash=raw.content_hash,
            observed_at=raw.received_at)
        SqliteScheduleRepository(c).append(
            game_ref_id=gref.reference_id, provider=MLB, provider_game_id=game_pk,
            observed_at=raw.received_at, ingested_at=to_iso(utc_now()), run_id=run.run_id,
            raw_response_id=raw.raw_response_id, raw_response_hash=raw.content_hash,
            mapped_status="scheduled", season=2026, game_date_local=date,
            scheduled_start=start, venue_provider_id=venue_pid)


# --------------------------------------------------------------------------- #
# Mocked provider payloads + clients
# --------------------------------------------------------------------------- #
def nws_point(*, forecast_status: int = 200) -> dict[str, Any]:
    return {"properties": {
        "forecastHourly": "https://api.weather.gov/gridpoints/BOX/70,76/forecast/hourly",
        "observationStations": "https://api.weather.gov/gridpoints/BOX/70,76/stations"}}


def nws_hourly(periods: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    if periods is None:
        periods = [
            {"startTime": "2026-07-22T23:00:00+00:00", "temperature": 72,
             "temperatureUnit": "F", "windSpeed": "10 mph", "windDirection": "NW",
             "dewpoint": {"value": 15.0, "unitCode": "wmoUnit:degC"},
             "relativeHumidity": {"value": 55},
             "probabilityOfPrecipitation": {"value": 20}, "shortForecast": "Clear"},
            {"startTime": "2026-07-23T00:00:00+00:00", "temperature": 70,
             "temperatureUnit": "F", "windSpeed": "8 mph", "windDirection": "N",
             "dewpoint": {"value": 14.0, "unitCode": "wmoUnit:degC"},
             "relativeHumidity": {"value": 58},
             "probabilityOfPrecipitation": {"value": 10}, "shortForecast": "Clear"},
        ]
    return {"properties": {"periods": periods}}


def nws_stations() -> dict[str, Any]:
    return {"features": [{"properties": {"stationIdentifier": "KBOS"}}]}


def nws_observations(features: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    if features is None:
        features = [{"properties": {
            "timestamp": "2026-07-22T23:00:00+00:00",
            "temperature": {"value": 22.0, "unitCode": "wmoUnit:degC"},
            "windSpeed": {"value": 16.0, "unitCode": "wmoUnit:km_h-1"},
            "windGust": {"value": 25.0, "unitCode": "wmoUnit:km_h-1"},
            "windDirection": {"value": 315, "unitCode": "wmoUnit:degree_(angle)"},
            "relativeHumidity": {"value": 55},
            "dewpoint": {"value": 15.0, "unitCode": "wmoUnit:degC"},
            "precipitationLastHour": {"value": 0.0, "unitCode": "wmoUnit:mm"},
            "textDescription": "Clear"}}]
    return {"features": features}


#: Canonical Open-Meteo hourly_units (what the client requests / validates).
OM_UNITS = {
    "time": "unixtime", "temperature_2m": "°C", "apparent_temperature": "°C",
    "dew_point_2m": "°C", "relative_humidity_2m": "%", "wind_speed_10m": "m/s",
    "wind_gusts_10m": "m/s", "wind_direction_10m": "°", "precipitation": "mm",
    "precipitation_probability": "%", "weather_code": "wmo code",
}


def _epoch(utc_str: str) -> int:
    """A naive-UTC wall time string -> Unix seconds (mirrors Open-Meteo unixtime)."""
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(utc_str).replace(tzinfo=timezone.utc).timestamp())


def om_hourly(*, utc_times: Optional[list[str]] = None, temps: Optional[list[Any]] = None,
              extra: Optional[dict[str, list[Any]]] = None,
              units: Optional[dict[str, str]] = None,
              with_probability: bool = True) -> dict[str, Any]:
    """A contract-shaped Open-Meteo response: hourly time as Unix seconds (the
    client requests ``timeformat=unixtime`` + ``timezone=UTC``), plus ``hourly_units``."""

    if utc_times is None:
        utc_times = ["2026-07-22T23:00", "2026-07-23T00:00"]
    if temps is None:
        temps = [21.0, 20.5]
    times = [_epoch(t) for t in utc_times]
    hourly: dict[str, Any] = {"time": times, "temperature_2m": temps,
                              "wind_speed_10m": [3.0] * len(times),
                              "wind_direction_10m": [270.0] * len(times),
                              "precipitation": [0.0] * len(times)}
    u = dict(OM_UNITS)
    if with_probability:
        hourly["precipitation_probability"] = [10] * len(times)
    if extra:
        hourly.update(extra)
    if units:
        u.update(units)
    return {"hourly": hourly, "hourly_units": u, "timezone": "GMT",
            "utc_offset_seconds": 0}


def _nws_client(handler: Callable[[httpx.Request], httpx.Response]) -> NwsClient:
    http = build_readonly_client(
        base_url="https://api.weather.gov", policy=ReadOnlyHTTPPolicy.for_nws(),
        inner_transport=httpx.MockTransport(handler))
    return NwsClient(client=http)


def _om_client(base: str, body: dict[str, Any], *, status: int = 200) -> OpenMeteoClient:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers={"content-type": "application/json"})
    http = build_readonly_client(
        base_url=base, policy=ReadOnlyHTTPPolicy.for_open_meteo_all(),
        inner_transport=httpx.MockTransport(handler))
    return OpenMeteoClient(base_url=base, client=http)


def default_nws_handler(
    *, point_status: int = 200, hourly: Optional[dict[str, Any]] = None,
    obs: Optional[dict[str, Any]] = None,
) -> Callable[[httpx.Request], httpx.Response]:
    hourly = hourly if hourly is not None else nws_hourly()
    obs = obs if obs is not None else nws_observations()

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.startswith("/points/"):
            if point_status != 200:
                return httpx.Response(point_status, json={"detail": "not found"},
                                      headers={"content-type": "application/geo+json"})
            body: Any = nws_point()
        elif p.endswith("/forecast/hourly"):
            body = hourly
        elif p.endswith("/stations"):
            body = nws_stations()
        elif "/observations" in p:
            body = obs
        else:
            body = {}
        return httpx.Response(200, json=body, headers={"content-type": "application/geo+json"})

    return handler


def make_clients(
    *, nws_handler: Optional[Callable[[httpx.Request], httpx.Response]] = None,
    om_forecast: Optional[dict[str, Any]] = None,
    om_archive: Optional[dict[str, Any]] = None,
    om_historical: Optional[dict[str, Any]] = None,
) -> WeatherClients:
    return WeatherClients(
        nws=_nws_client(nws_handler or default_nws_handler()),
        om_forecast=_om_client("https://api.open-meteo.com",
                               om_forecast if om_forecast is not None else om_hourly()),
        om_historical=_om_client("https://historical-forecast-api.open-meteo.com",
                                 om_historical if om_historical is not None else om_hourly()),
        om_archive=_om_client("https://archive-api.open-meteo.com",
                              om_archive if om_archive is not None else
                              om_hourly(with_probability=False)),
    )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    p = tmp_path / "corpus.db"
    initialize_database(p)
    return Database(p)


def _count(db: Database, table: str, where: str = "") -> int:
    with db.connection() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0])


async def _run(db: Database, clients: WeatherClients, **kwargs: Any) -> Any:
    kwargs.setdefault("from_date", "2026-07-22")
    kwargs.setdefault("to_date", "2026-07-22")
    try:
        return await ingest_weather(database=db, clients=clients, **kwargs)
    finally:
        await clients.aclose()


# --------------------------------------------------------------------------- #
# 1-2. Migration v14 + append-only triggers
# --------------------------------------------------------------------------- #
def test_migration_v14_table_exists(db: Database) -> None:
    with db.connection() as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 14
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "weather_snapshots" in names


async def test_weather_table_is_append_only(db: Database) -> None:
    seed_game(db)
    await _run(db, make_clients(), mode="forecast")
    with db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE weather_snapshots SET provider='x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM weather_snapshots")


# --------------------------------------------------------------------------- #
# 3-8. Routing / provider selection
# --------------------------------------------------------------------------- #
async def test_us_outdoor_game_uses_nws(db: Database) -> None:
    seed_game(db, country="USA", roof="open")
    r = await _run(db, make_clients(), mode="forecast")
    assert r.status == "succeeded"
    assert r.nws_requests == 2 and r.open_meteo_requests == 0  # point + hourly forecast GETs
    assert r.forecast_observations == 2
    with db.connection() as conn:
        providers = {x[0] for x in conn.execute("SELECT DISTINCT provider FROM weather_snapshots")}
    assert providers == {"nws"}


async def test_nws_point_then_validated_hourly_forecast(db: Database) -> None:
    seed_game(db)
    await _run(db, make_clients(), mode="forecast")
    with db.connection() as conn:
        endpoints = {x[0] for x in conn.execute(
            "SELECT DISTINCT endpoint FROM raw_responses WHERE provider='nws'")}
    # point resolution + the validated returned hourly-forecast URL were both stored.
    assert "/points/42.35,-71.1" in endpoints
    assert "/gridpoints/BOX/70,76/forecast/hourly" in endpoints


async def test_nws_station_observation_ingestion(db: Database) -> None:
    seed_game(db)
    r = await _run(db, make_clients(), mode="actual")
    assert r.station_observations == 1
    with db.connection() as conn:
        row = conn.execute(
            "SELECT weather_kind, source_station, temperature_c, wind_speed_ms, precip_amount_mm "
            "FROM weather_snapshots").fetchone()
    assert row[0] == "station_observation" and row[1] == "KBOS"


async def test_non_us_game_uses_open_meteo(db: Database) -> None:
    seed_game(db, country="Canada", venue_name="Rogers Centre", roof="open",
              lat=43.64, lon=-79.39, tz="America/Toronto")
    r = await _run(db, make_clients(), mode="forecast")
    assert r.nws_requests == 0 and r.open_meteo_requests == 1
    with db.connection() as conn:
        providers = {x[0] for x in conn.execute("SELECT DISTINCT provider FROM weather_snapshots")}
    assert providers == {"open_meteo"}


async def test_nws_geographic_unavailability_falls_back(db: Database) -> None:
    seed_game(db, country="USA")
    clients = make_clients(nws_handler=default_nws_handler(point_status=404))
    r = await _run(db, clients, mode="forecast")
    assert r.status == "succeeded"
    assert r.provider_fallbacks == 1 and r.open_meteo_requests == 1
    assert r.active_failures == 0
    with db.connection() as conn:
        providers = {x[0] for x in conn.execute("SELECT DISTINCT provider FROM weather_snapshots")}
    assert providers == {"open_meteo"}  # fell back honestly


async def test_nws_active_failure_is_not_a_geographic_fallback(db: Database) -> None:
    seed_game(db, country="USA")
    clients = make_clients(nws_handler=default_nws_handler(point_status=503))
    r = await _run(db, clients, mode="forecast")
    assert r.active_failures == 1 and r.provider_fallbacks == 0
    assert r.needs_failure_exit
    assert _count(db, "weather_snapshots") == 0


# --------------------------------------------------------------------------- #
# 9-13. Roof / venue gating
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("roof", ["dome", "fixed", "indoor"])
async def test_indoor_game_makes_no_request(db: Database, roof: str) -> None:
    seed_game(db, roof=roof)
    r = await _run(db, make_clients(), mode="forecast")
    assert r.indoor_games_skipped == 1 and r.games_eligible == 0
    assert r.nws_requests == 0 and r.open_meteo_requests == 0
    assert _count(db, "weather_snapshots") == 0


async def test_retractable_roof_is_conditional_not_assumed_open(db: Database) -> None:
    seed_game(db, roof="retractable")
    r = await _run(db, make_clients(), mode="forecast")
    assert r.retractable_conditional_games == 1
    with db.connection() as conn:
        appl = {x[0] for x in conn.execute("SELECT DISTINCT applicability FROM weather_snapshots")}
    assert appl == {"conditional_roof_unknown"}  # never 'applicable'


async def test_missing_coordinates_skip_without_guessing(db: Database) -> None:
    seed_game(db, lat=None, lon=None)
    r = await _run(db, make_clients(), mode="forecast")
    assert r.games_skipped_missing_venue == 1 and r.games_eligible == 0
    assert r.nws_requests == 0 and r.open_meteo_requests == 0


async def test_missing_roof_type_skips_or_notes(db: Database) -> None:
    seed_game(db, roof=None)
    r = await _run(db, make_clients(), mode="forecast")
    assert r.games_skipped_missing_venue == 1 and r.games_eligible == 0


async def test_missing_timezone_not_silently_guessed(db: Database) -> None:
    seed_game(db, tz=None)
    r = await _run(db, make_clients(), mode="forecast")
    assert r.games_skipped_missing_venue == 1 and r.games_eligible == 0
    assert r.open_meteo_requests == 0 and r.nws_requests == 0


async def test_missing_venue_alias_skips(db: Database) -> None:
    seed_game(db, seed_venue=True, venue_pid="3")
    # A game whose venue provider id has no seeded venue -> unresolvable venue.
    seed_game(db, game_pk="999", venue_pid="404", seed_venue=False, date="2026-07-22")
    r = await _run(db, make_clients(), mode="forecast")
    assert r.games_skipped_missing_venue >= 1


# --------------------------------------------------------------------------- #
# 14-19. Kinds distinct + point-in-time honesty
# --------------------------------------------------------------------------- #
async def test_forecast_and_actual_are_distinct_kinds(db: Database) -> None:
    seed_game(db)
    await _run(db, make_clients(), mode="forecast")
    await _run(db, make_clients(), mode="actual")
    with db.connection() as conn:
        kinds = {x[0] for x in conn.execute("SELECT DISTINCT weather_kind FROM weather_snapshots")}
    assert kinds == {"current_forecast", "station_observation"}


async def test_historical_forecast_and_reanalysis_are_distinct(db: Database) -> None:
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto")
    await _run(db, make_clients(), mode="historical-forecast")
    await _run(db, make_clients(), mode="actual")  # non-US actual -> reanalysis
    with db.connection() as conn:
        kinds = {x[0] for x in conn.execute("SELECT DISTINCT weather_kind FROM weather_snapshots")}
    assert kinds == {"historical_forecast", "reanalysis"}


async def test_historical_response_does_not_backdate_observed_at(db: Database) -> None:
    # A completed 2026 game; the stitched historical-forecast response's valid times
    # are in 2026, but observed_at must be when we received it (now, i.e. > 2026).
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto", start=DEFAULT_START)
    await _run(db, make_clients(), mode="historical-forecast")
    with db.connection() as conn:
        observed, valid = conn.execute(
            "SELECT observed_at, valid_time FROM weather_snapshots LIMIT 1").fetchone()
    # observed_at (received now) is AFTER the historical valid time -> never backdated.
    assert observed > valid


async def test_unproven_historical_availability_is_not_pit_eligible(db: Database) -> None:
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto")
    r = await _run(db, make_clients(), mode="historical-forecast")
    with db.connection() as conn:
        pit = {row[0] for row in conn.execute("SELECT pit_eligible FROM weather_snapshots")}
        note = conn.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE rule_code='DQ-WX-PIT-001'").fetchone()[0]
    assert pit == {None}  # UNKNOWN, never asserted eligible
    assert note == 1 and r.data_quality_issues >= 1


async def test_reanalysis_is_never_pit_eligible_as_pregame_forecast(db: Database) -> None:
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto")
    await _run(db, make_clients(), mode="actual")
    with db.connection() as conn:
        pit = {row[0] for row in conn.execute(
            "SELECT pit_eligible FROM weather_snapshots WHERE weather_kind='reanalysis'")}
    assert pit == {0}  # observation-grade, never a pregame forecast


def test_pit_helper_semantics() -> None:
    # current forecast received before first pitch -> eligible; after -> not.
    elig, target, lead = _pit_for("current_forecast", "2999-07-22T23:00:00.000000Z",
                                  "2999-07-22T20:00:00.000000Z", "2999-07-22T23:05:00Z")
    assert elig is True and target == "2999-07-22T23:00:00.000000Z" and lead == 3 * 3600
    elig2, _t, _l = _pit_for("current_forecast", "2020-07-22T23:00:00.000000Z",
                             "2024-07-22T20:00:00.000000Z", "2020-07-22T23:05:00Z")
    assert elig2 is False
    assert _pit_for("station_observation", "x", "y", "z") == (False, None, None)
    assert _pit_for("historical_forecast", "2020-01-01T00:00:00.000000Z",
                    "2024-01-01T00:00:00.000000Z", None)[0] is None


async def test_future_game_forecast_is_pit_eligible(db: Database) -> None:
    future_hourly = nws_hourly(periods=[
        {"startTime": "2999-07-22T23:00:00+00:00", "temperature": 72, "temperatureUnit": "F",
         "windSpeed": "10 mph", "windDirection": "NW", "shortForecast": "Clear"}])
    seed_game(db, start=FUTURE_START, date="2999-07-22")  # scheduled far in the future
    clients = make_clients(nws_handler=default_nws_handler(hourly=future_hourly))
    await ingest_weather(database=db, clients=clients, mode="forecast",
                         from_date="2999-07-22", to_date="2999-07-22")
    await clients.aclose()
    with db.connection() as conn:
        pit = {row[0] for row in conn.execute("SELECT pit_eligible FROM weather_snapshots")}
    assert pit == {1}  # a forecast retrieved now, before a future first pitch, is pregame


# --------------------------------------------------------------------------- #
# 20-24. Value + unit normalization
# --------------------------------------------------------------------------- #
async def test_explicit_zero_preserved_null_not_zeroed(db: Database) -> None:
    obs = nws_observations(features=[{"properties": {
        "timestamp": "2026-07-22T23:00:00+00:00",
        "temperature": {"value": 0.0, "unitCode": "wmoUnit:degC"},   # explicit 0 C
        "windSpeed": {"value": None, "unitCode": "wmoUnit:km_h-1"},   # missing -> null
        "precipitationLastHour": {"value": 0.0, "unitCode": "wmoUnit:mm"},  # explicit 0 mm
        "textDescription": "Cold"}}])
    seed_game(db)
    await _run(db, make_clients(nws_handler=default_nws_handler(obs=obs)), mode="actual")
    with db.connection() as conn:
        row = conn.execute(
            "SELECT temperature_c, wind_speed_ms, precip_amount_mm FROM weather_snapshots"
        ).fetchone()
    assert row[0] == 0.0 and row[2] == 0.0  # explicit zeros preserved
    assert row[1] is None  # missing wind stays NULL, never coerced to 0


def test_temperature_conversion() -> None:
    assert normalize_temperature_c(72, "F")[0] == pytest.approx(22.2222, abs=1e-3)
    assert normalize_temperature_c(20, "wmoUnit:degC")[0] == 20.0
    val, note = normalize_temperature_c(20, "kelvin")
    assert val is None and note is not None  # unknown unit -> null + note


def test_wind_unit_conversion() -> None:
    assert normalize_speed_ms("10 mph", None)[0] == pytest.approx(4.4704, abs=1e-4)
    assert normalize_speed_ms(36.0, "wmoUnit:km_h-1")[0] == pytest.approx(10.0, abs=1e-6)
    assert normalize_speed_ms(5.0, "m_s-1")[0] == 5.0
    val, note = normalize_speed_ms(5.0, "furlongs")
    assert val is None and note is not None


def test_direction_normalization() -> None:
    assert normalize_direction_deg("NW")[0] == 315.0
    assert normalize_direction_deg(90)[0] == 90.0
    val, note = normalize_direction_deg("sideways")
    assert val is None and note is not None


def test_open_meteo_array_length_mismatch_is_rejected() -> None:
    bad = {"hourly": {"time": ["2026-07-22T23:00", "2026-07-23T00:00"],
                      "temperature_2m": [21.0]}}  # length 1 != 2
    from datetime import datetime, timezone
    rows, notes, rejected = normalize_open_meteo(
        bad, window_start=datetime(2026, 7, 22, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 23, 23, tzinfo=timezone.utc))
    assert rejected is True and rows == [] and notes


async def test_open_meteo_mismatch_rejected_in_ingest(db: Database) -> None:
    bad = {"hourly": {"time": ["2026-07-22T23:00", "2026-07-23T00:00"], "temperature_2m": [21.0]}}
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto")
    r = await _run(db, make_clients(om_forecast=bad), mode="forecast")
    assert r.records_rejected >= 1
    assert _count(db, "weather_snapshots") == 0


# --------------------------------------------------------------------------- #
# 25-30. Repository transition semantics (deterministic observed_at)
# --------------------------------------------------------------------------- #
def _seed_ref(conn: sqlite3.Connection) -> tuple[str, str]:
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, status, "
        "requested_at, started_at, started_monotonic_ns, requests_made, records_received, "
        "records_normalized, records_inserted, records_deduplicated, records_rejected, "
        "tool_version, created_at) VALUES ('run_w','c','nws','o','{}','started',"
        "'2024-01-01T00:00:00.000000Z','2024-01-01T00:00:00.000000Z',0,0,0,0,0,0,0,'t',"
        "'2024-01-01T00:00:00.000000Z')")
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_method, http_status, response_headers_json, requested_at, "
        "received_at, elapsed_ns, body, body_bytes, body_hash, content_hash, created_at) VALUES "
        "('raw_w','run_w','nws','/points/1,1','{}','GET',200,'{}',"
        "'2024-01-01T00:00:00.000000Z','2024-01-01T00:00:00.000000Z',0,'{}',2,'h','c',"
        "'2024-01-01T00:00:00.000000Z')")
    v = SqliteVenueRepository(conn)
    venue, _ = v.upsert(name="Park", raw_response_id="raw_w", raw_response_hash="c",
                        observed_at="2024-01-01T00:00:00.000000Z", country="USA",
                        latitude=1.0, longitude=1.0, timezone="UTC", roof_type="open")
    refs = SqliteProviderReferenceRepository(conn)
    gref, _ = refs.upsert(kind="game", provider="mlb_statsapi", provider_entity_id="1",
                          raw_response_id="raw_w", raw_response_hash="c",
                          observed_at="2024-01-01T00:00:00.000000Z")
    return gref.reference_id, venue.venue_id


def _t(n: int) -> str:
    return f"2026-07-22T{n:02d}:00:00.000000Z"


def _append(repo: SqliteWeatherRepository, gref: str, venue: str, *, observed_at: str,
            temp: Optional[float], mode: str = "nws_hourly_forecast") -> Any:
    return repo.append(
        game_ref_id=gref, provider="nws", provider_game_id="1", venue_id=venue,
        weather_kind="current_forecast", applicability="applicable", forecast_mode=mode,
        valid_time="2026-07-22T23:00:00.000000Z", observed_at=observed_at,
        retrieved_at=observed_at, ingested_at=observed_at, run_id="run_w",
        raw_response_id="raw_w", raw_response_hash="c",
        values=WeatherValues(temperature_c=temp))


def test_forecast_update_appends(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, venue = _seed_ref(conn)
        repo = SqliteWeatherRepository(conn)
        _id1, o1 = _append(repo, gref, venue, observed_at=_t(1), temp=20.0)
        _id2, o2 = _append(repo, gref, venue, observed_at=_t(2), temp=22.0)  # changed value
        assert o1 is WeatherOutcome.INSERTED and o2 is WeatherOutcome.CHANGED
        assert repo.count() == 2


def test_identical_replay_unchanged(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, venue = _seed_ref(conn)
        repo = SqliteWeatherRepository(conn)
        _append(repo, gref, venue, observed_at=_t(1), temp=20.0)
        _id, out = _append(repo, gref, venue, observed_at=_t(2), temp=20.0)  # identical
        assert out is WeatherOutcome.UNCHANGED
        assert repo.count() == 1


def test_a_b_a_history_preserved(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, venue = _seed_ref(conn)
        repo = SqliteWeatherRepository(conn)
        for temp, ts in ((20.0, 1), (22.0, 2), (20.0, 3)):  # A -> B -> A
            _append(repo, gref, venue, observed_at=_t(ts), temp=temp)
        assert repo.count() == 3


def test_out_of_order_does_not_regress_current_state(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, venue = _seed_ref(conn)
        repo = SqliteWeatherRepository(conn)
        _append(repo, gref, venue, observed_at=_t(5), temp=25.0)          # later
        _id, out = _append(repo, gref, venue, observed_at=_t(2), temp=18.0)  # earlier backfill
        # The earlier backfill has no temporal predecessor -> a new historical row
        # (INSERTED), and it never regresses current state (still the newest).
        assert out is WeatherOutcome.INSERTED
        assert repo.count() == 2
        latest = repo.latest(gref, "current_forecast", "nws_hourly_forecast",
                             "2026-07-22T23:00:00.000000Z")
        assert latest is not None and latest["temperature_c"] == 25.0  # newest unchanged


def test_station_observation_correction_appends(db: Database) -> None:
    with db.connection() as conn, transaction(conn):
        gref, venue = _seed_ref(conn)
        repo = SqliteWeatherRepository(conn)
        base: dict[str, Any] = dict(
            game_ref_id=gref, provider="nws", provider_game_id="1", venue_id=venue,
            weather_kind="station_observation", applicability="applicable",
            forecast_mode="nws_station_observation",
            valid_time="2026-07-22T23:00:00.000000Z", run_id="run_w",
            raw_response_id="raw_w", raw_response_hash="c")
        _i1, o1 = repo.append(observed_at=_t(1), retrieved_at=_t(1), ingested_at=_t(1),
                              values=WeatherValues(temperature_c=22.0), **base)
        _i2, o2 = repo.append(observed_at=_t(2), retrieved_at=_t(2), ingested_at=_t(2),
                              values=WeatherValues(temperature_c=21.0), **base)  # correction
        assert o1 is WeatherOutcome.INSERTED and o2 is WeatherOutcome.CHANGED
        assert repo.count() == 2


# --------------------------------------------------------------------------- #
# 31-34. Dedup + reschedule
# --------------------------------------------------------------------------- #
async def test_doubleheader_shares_one_provider_call(db: Database) -> None:
    # Two games, same venue + date -> one NWS point/forecast fetch, reused.
    hourly = nws_hourly(periods=[
        {"startTime": "2026-07-22T17:00:00+00:00", "temperature": 80, "temperatureUnit": "F",
         "windSpeed": "5 mph", "windDirection": "S", "shortForecast": "Hot"},
        {"startTime": "2026-07-22T20:00:00+00:00", "temperature": 75, "temperatureUnit": "F",
         "windSpeed": "6 mph", "windDirection": "SW", "shortForecast": "Warm"}])
    seed_game(db, game_pk="1", venue_pid="3", start="2026-07-22T17:05:00Z")
    seed_game(db, game_pk="2", venue_pid="3", seed_venue=False, start="2026-07-22T20:05:00Z")
    r = await _run(db, make_clients(nws_handler=default_nws_handler(hourly=hourly)),
                   mode="forecast")
    assert r.games_eligible == 2
    assert r.request_groups_deduplicated == 1  # 2 games shared 1 fetch group
    with db.connection() as conn:
        games = {x[0] for x in conn.execute("SELECT DISTINCT provider_game_id FROM weather_snapshots")}
    assert games == {"1", "2"}  # both games got rows; not attached to only one


async def test_same_venue_separate_dates_receive_separate_requests(db: Database) -> None:
    seed_game(db, game_pk="1", venue_pid="3", date="2026-07-22", start="2026-07-22T23:05:00Z")
    seed_game(db, game_pk="2", venue_pid="3", seed_venue=False, date="2026-07-23",
              start="2026-07-23T23:05:00Z")
    r = await ingest_weather(database=db, clients=make_clients(), mode="forecast",
                             from_date="2026-07-22", to_date="2026-07-23")
    await make_clients().aclose()
    assert r.request_groups_deduplicated == 0  # different dates -> separate groups
    assert r.nws_requests == 4  # two dates x (point + hourly forecast) GETs


async def test_doubleheader_windows_do_not_cross_attach(db: Database) -> None:
    # Game 1 at 17:05 (window 16:05-21:05), game 2 at 23:05 (window 22:05-03:05).
    hourly = nws_hourly(periods=[
        {"startTime": "2026-07-22T17:00:00+00:00", "temperature": 80, "temperatureUnit": "F",
         "windSpeed": "5 mph", "windDirection": "S", "shortForecast": "Hot"},
        {"startTime": "2026-07-22T23:00:00+00:00", "temperature": 68, "temperatureUnit": "F",
         "windSpeed": "3 mph", "windDirection": "N", "shortForecast": "Cool"}])
    seed_game(db, game_pk="1", venue_pid="3", start="2026-07-22T17:05:00Z")
    seed_game(db, game_pk="2", venue_pid="3", seed_venue=False, start="2026-07-22T23:05:00Z")
    await _run(db, make_clients(nws_handler=default_nws_handler(hourly=hourly)), mode="forecast")
    with db.connection() as conn:
        g1 = conn.execute("SELECT valid_time FROM weather_snapshots WHERE provider_game_id='1'"
                          ).fetchall()
        g2 = conn.execute("SELECT valid_time FROM weather_snapshots WHERE provider_game_id='2'"
                          ).fetchall()
    # Each game only gets the hour inside its own window.
    assert {r[0] for r in g1} == {"2026-07-22T17:00:00.000000Z"}
    assert {r[0] for r in g2} == {"2026-07-22T23:00:00.000000Z"}


async def test_rescheduled_game_uses_latest_venue_and_time(db: Database) -> None:
    # A later schedule observation moves the game to a new date; weather uses the newest.
    seed_game(db, game_pk="55", venue_pid="3", date="2026-07-22", start="2026-07-22T23:05:00Z")
    with db.connection() as c, transaction(c):
        sched = SqliteScheduleRepository(c)
        prior = sched.latest_for_provider_game(MLB, "55")
        assert prior is not None
        raw = SqliteRawResponseRepository(c).store(
            run_id=prior["run_id"], provider=MLB, endpoint="/api/v1/schedule",
            request_params_json="{}", http_status=200, response_headers_json="{}",
            requested_at=to_iso(utc_now()), received_at="2999-01-01T00:00:00.000000Z", elapsed_ns=1,
            body="{}", content_hash=response_content_hash(provider=MLB, endpoint="/y",
                                                          request_params={}, body="resched"))
        sched.append(game_ref_id=prior["game_ref_id"], provider=MLB, provider_game_id="55",
                     observed_at="2999-01-01T00:00:00.000000Z", ingested_at=to_iso(utc_now()),
                     run_id=prior["run_id"], raw_response_id=raw.raw_response_id,
                     raw_response_hash=raw.content_hash, mapped_status="rescheduled",
                     game_date_local="2026-07-23", scheduled_start="2026-07-23T23:05:00Z",
                     venue_provider_id="3")
    r = await ingest_weather(database=db, clients=make_clients(), mode="forecast",
                             from_date="2026-07-23", to_date="2026-07-23")
    await make_clients().aclose()
    assert r.games_eligible == 1  # resolved on the NEW date, not the old one


# --------------------------------------------------------------------------- #
# 35-38. game-pk, bounded range, provenance, dry-run
# --------------------------------------------------------------------------- #
async def test_game_pk_path(db: Database) -> None:
    seed_game(db, game_pk="745804")
    r = await ingest_weather(database=db, clients=make_clients(), mode="forecast", game_pk=745804)
    await make_clients().aclose()
    assert r.games_considered == 1 and r.forecast_observations == 2


async def test_inclusive_bounded_date_range(db: Database) -> None:
    seed_game(db, game_pk="1", date="2026-07-22", start="2026-07-22T23:05:00Z")
    seed_game(db, game_pk="2", venue_pid="3", seed_venue=False, date="2026-07-24",
              start="2026-07-24T23:05:00Z")
    r = await ingest_weather(database=db, clients=make_clients(), mode="forecast",
                             from_date="2026-07-22", to_date="2026-07-23")
    await make_clients().aclose()
    assert r.games_considered == 1  # only the 07-22 game is in [22, 23]


async def test_every_weather_row_traces_to_a_raw_response(db: Database) -> None:
    seed_game(db)
    await _run(db, make_clients(), mode="forecast")
    with db.connection() as conn:
        dangling = conn.execute(
            "SELECT COUNT(*) FROM weather_snapshots w LEFT JOIN raw_responses r "
            "ON w.raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
        ).fetchone()[0]
        # weather rows attach to the hourly-FORECAST response, not the point response.
        endpoints = {conn.execute(
            "SELECT endpoint FROM raw_responses WHERE raw_response_id=?", (rid,)
        ).fetchone()[0] for (rid,) in conn.execute(
            "SELECT DISTINCT raw_response_id FROM weather_snapshots")}
    assert dangling == 0
    assert endpoints == {"/gridpoints/BOX/70,76/forecast/hourly"}


async def test_dry_run_creates_no_database_and_persists_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "never.db"
    r = await ingest_weather(database=Database(missing), clients=make_clients(), mode="forecast",
                             from_date="2026-07-22", to_date="2026-07-22", dry_run=True)
    await make_clients().aclose()
    assert r.dry_run and r.rows_persisted == 0 and r.run_id is None
    assert not missing.exists()  # a non-existent corpus is never created


async def test_dry_run_against_corpus_persists_nothing(db: Database) -> None:
    seed_game(db)
    before = _count(db, "weather_snapshots")
    r = await _run(db, make_clients(), mode="forecast", dry_run=True)
    assert r.observations_normalized == 2 and r.rows_persisted == 0
    assert _count(db, "weather_snapshots") == before
    assert _count(db, "raw_responses", "WHERE provider='nws'") == 0


async def test_persisted_ingestion_is_idempotent(db: Database) -> None:
    seed_game(db)
    await _run(db, make_clients(), mode="forecast")
    before = _count(db, "weather_snapshots")
    r2 = await _run(db, make_clients(), mode="forecast")
    assert r2.records_inserted == 0 and r2.records_changed == 0 and r2.records_unchanged == 2
    assert _count(db, "weather_snapshots") == before


# --------------------------------------------------------------------------- #
# 39-42. CLI, SSRF policy, no secrets, licensing note
# --------------------------------------------------------------------------- #
def test_cli_json_and_exit_codes(tmp_path: Path) -> None:
    from sports_quant.cli import run_ingest_weather
    from sports_quant.config import Settings

    dbp = tmp_path / "corpus.db"
    initialize_database(dbp)
    db = Database(dbp)
    seed_game(db)
    settings = Settings(database_path=str(dbp))
    lines: list[str] = []
    code = run_ingest_weather(settings, mode="forecast", from_date="2026-07-22",
                              to_date="2026-07-22", database_path=dbp, as_json=True,
                              out=lines.append, clients=make_clients())
    assert code == 0
    payload = json.loads(lines[-1])
    assert payload["command"] == "ingest-weather" and payload["mode"] == "forecast"

    missing = tmp_path / "missing.db"
    code3 = run_ingest_weather(settings, mode="forecast", from_date="2026-07-22",
                               database_path=missing, out=lambda _s: None, clients=make_clients())
    assert code3 == 3
    code1 = run_ingest_weather(settings, mode="forecast", from_date="2026-07-22", game_pk=1,
                               database_path=dbp, out=lambda _s: None, clients=make_clients())
    assert code1 == 1


async def test_arbitrary_returned_url_is_rejected_by_policy() -> None:
    client = _nws_client(default_nws_handler())
    try:
        with pytest.raises(ProviderError, match="unapproved host"):
            await client.fetch_returned_url("https://evil.example.com/gridpoints/x/1,1/forecast")
    finally:
        await client.aclose()


async def test_policy_blocks_unapproved_open_meteo_host() -> None:
    # A client pinned to the forecast host cannot reach an arbitrary host.
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers={"content-type": "application/json"})
    http = build_readonly_client(
        base_url="https://evil.example.com/v1", policy=ReadOnlyHTTPPolicy.for_open_meteo_all(),
        inner_transport=httpx.MockTransport(handler))
    client = OpenMeteoClient(base_url="https://evil.example.com/v1", client=http)
    try:
        with pytest.raises(ReadOnlyPolicyError):
            await client.fetch_forecast(1.0, 1.0)
    finally:
        await client.aclose()


async def test_no_credential_or_unsafe_header_is_stored(db: Database) -> None:
    seed_game(db)
    await _run(db, make_clients(), mode="forecast")
    with db.connection() as conn:
        for (headers,) in conn.execute("SELECT response_headers_json FROM raw_responses"):
            h = json.loads(headers)
            assert "authorization" not in h and "set-cookie" not in h


def test_open_meteo_licensing_is_documented_not_a_failure() -> None:
    # The commercial-licensing limitation is a NOTE, not an unavailable capability.
    from sports_quant.providers.capabilities import (
        OPEN_METEO_DECLARATION,
        CapabilityState,
        ProviderCapability,
    )
    assert OPEN_METEO_DECLARATION.state(ProviderCapability.LIVE_AVAILABILITY) == \
        CapabilityState.SUPPORTED
    assert OPEN_METEO_DECLARATION.notes is not None
    assert ProviderCapability.LIVE_AVAILABILITY in OPEN_METEO_DECLARATION.notes


# --------------------------------------------------------------------------- #
# 43-44. Regressions + no gateway import
# --------------------------------------------------------------------------- #
def test_no_weather_module_imports_gateway() -> None:
    text = (Path(__file__).resolve().parents[1] / "weather_ingestor.py").read_text(encoding="utf-8")
    assert "import gateway" not in text and "from gateway" not in text


async def test_mlb_and_nba_tables_untouched_by_weather(db: Database) -> None:
    seed_game(db)
    await _run(db, make_clients(), mode="forecast")
    # Weather writes only weather_snapshots + raw/run rows, never NBA/MLB observation tables.
    assert _count(db, "nba_game_results") == 0
    assert _count(db, "game_result_snapshots") == 0
    assert _count(db, "weather_snapshots") == 2


# --------------------------------------------------------------------------- #
# REPAIR: Open-Meteo epoch timestamps + timezone correctness
# --------------------------------------------------------------------------- #
async def test_open_meteo_epoch_converts_to_correct_utc(db: Database) -> None:
    # Two epoch hours 23:00 and 00:00 UTC; the game window covers both.
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto", start="2026-07-22T23:05:00Z")
    await _run(db, make_clients(), mode="forecast")
    with db.connection() as conn:
        vts = sorted(r[0] for r in conn.execute("SELECT valid_time FROM weather_snapshots"))
    assert vts == ["2026-07-22T23:00:00.000000Z", "2026-07-23T00:00:00.000000Z"]


async def test_non_utc_venue_does_not_shift_into_wrong_window(db: Database) -> None:
    # A Pacific venue whose UTC window is 05:05-10:05 (next day) for a 06:05Z start.
    # Only the epoch hour inside the UTC window is attached; a local-as-UTC bug would
    # have shifted a 23:00-local hour in.
    body = om_hourly(utc_times=["2026-07-23T06:00", "2026-07-23T07:00", "2026-07-22T13:00"],
                     temps=[18.0, 17.5, 30.0])
    seed_game(db, country="Canada", venue_name="BC Place", lat=49.28, lon=-123.11,
              tz="America/Vancouver", start="2026-07-23T06:05:00Z", date="2026-07-23")
    await _run(db, make_clients(om_forecast=body), from_date="2026-07-23", to_date="2026-07-23",
               mode="forecast")
    with db.connection() as conn:
        vts = sorted(r[0] for r in conn.execute("SELECT valid_time FROM weather_snapshots"))
    # 13:00Z (would be an in-window hour only under a wrong-offset bug) is excluded.
    assert vts == ["2026-07-23T06:00:00.000000Z", "2026-07-23T07:00:00.000000Z"]


def test_open_meteo_malformed_timestamp_is_noted_not_crashed() -> None:
    from datetime import datetime, timezone
    body = {"hourly": {"time": [_epoch("2026-07-22T23:00"), "not-an-epoch"],
                       "temperature_2m": [21.0, 22.0]},
            "hourly_units": OM_UNITS}
    rows, notes, rejected = normalize_open_meteo(
        body, window_start=datetime(2026, 7, 22, 20, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 23, 3, tzinfo=timezone.utc))
    assert rejected is False and len(rows) == 1
    assert any("malformed hourly time" in n for n in notes)


# --------------------------------------------------------------------------- #
# REPAIR: cross-midnight request date ranges
# --------------------------------------------------------------------------- #
async def test_late_night_window_requests_both_dates(db: Database) -> None:
    seen: dict[str, list[tuple[str, str]]] = {"dates": []}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/forecast":
            seen["dates"].append((req.url.params.get("start_date"),
                                  req.url.params.get("end_date")))
        return httpx.Response(200, json=om_hourly(), headers={"content-type": "application/json"})
    http = build_readonly_client(base_url="https://api.open-meteo.com",
                                 policy=ReadOnlyHTTPPolicy.for_open_meteo_all(),
                                 inner_transport=httpx.MockTransport(handler))
    om = OpenMeteoClient(base_url="https://api.open-meteo.com", client=http)
    clients = WeatherClients(nws=_nws_client(default_nws_handler()), om_forecast=om,
                             om_historical=_om_client("https://historical-forecast-api.open-meteo.com", om_hourly()),
                             om_archive=_om_client("https://archive-api.open-meteo.com", om_hourly()))
    # 23:45Z start -> window 22:45Z .. 03:45Z next day: crosses midnight -> two dates.
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto", start="2026-07-22T23:45:00Z")
    await ingest_weather(database=db, clients=clients, mode="forecast",
                         from_date="2026-07-22", to_date="2026-07-22")
    await clients.aclose()
    assert seen["dates"] == [("2026-07-22", "2026-07-23")]  # both calendar dates requested


async def test_early_game_previous_date_window(db: Database) -> None:
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/forecast":
            seen.append((req.url.params.get("start_date"), req.url.params.get("end_date")))
        return httpx.Response(200, json=om_hourly(), headers={"content-type": "application/json"})
    http = build_readonly_client(base_url="https://api.open-meteo.com",
                                 policy=ReadOnlyHTTPPolicy.for_open_meteo_all(),
                                 inner_transport=httpx.MockTransport(handler))
    om = OpenMeteoClient(base_url="https://api.open-meteo.com", client=http)
    clients = WeatherClients(nws=_nws_client(default_nws_handler()), om_forecast=om,
                             om_historical=_om_client("https://historical-forecast-api.open-meteo.com", om_hourly()),
                             om_archive=_om_client("https://archive-api.open-meteo.com", om_hourly()))
    # 00:30Z start -> window 23:30Z (previous date) .. 04:30Z: spans the previous date.
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto", start="2026-07-23T00:30:00Z", date="2026-07-23")
    await ingest_weather(database=db, clients=clients, mode="forecast",
                         from_date="2026-07-23", to_date="2026-07-23")
    await clients.aclose()
    assert seen == [("2026-07-22", "2026-07-23")]


async def test_cross_midnight_doubleheader_dedup_and_dates(db: Database) -> None:
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/forecast":
            seen.append((req.url.params.get("start_date"), req.url.params.get("end_date")))
        return httpx.Response(200, json=om_hourly(
            utc_times=["2026-07-22T18:00", "2026-07-22T23:00", "2026-07-23T00:00"],
            temps=[25.0, 20.0, 19.0]), headers={"content-type": "application/json"})
    http = build_readonly_client(base_url="https://api.open-meteo.com",
                                 policy=ReadOnlyHTTPPolicy.for_open_meteo_all(),
                                 inner_transport=httpx.MockTransport(handler))
    om = OpenMeteoClient(base_url="https://api.open-meteo.com", client=http)
    clients = WeatherClients(nws=_nws_client(default_nws_handler()), om_forecast=om,
                             om_historical=_om_client("https://historical-forecast-api.open-meteo.com", om_hourly()),
                             om_archive=_om_client("https://archive-api.open-meteo.com", om_hourly()))
    # DH: game 1 18:05Z (window 17:05-22:05), game 2 23:45Z (window 22:45-03:45 next day).
    seed_game(db, game_pk="1", venue_pid="3", country="Canada", venue_name="Rogers Centre",
              lat=43.64, lon=-79.39, tz="America/Toronto", start="2026-07-22T18:05:00Z")
    seed_game(db, game_pk="2", venue_pid="3", seed_venue=False, start="2026-07-22T23:45:00Z")
    r = await ingest_weather(database=db, clients=clients, mode="forecast",
                             from_date="2026-07-22", to_date="2026-07-22")
    await clients.aclose()
    assert seen == [("2026-07-22", "2026-07-23")]  # ONE request spanning both dates
    assert r.request_groups_deduplicated == 1
    with db.connection() as conn:
        g1 = {x[0] for x in conn.execute(
            "SELECT valid_time FROM weather_snapshots WHERE provider_game_id='1'")}
        g2 = {x[0] for x in conn.execute(
            "SELECT valid_time FROM weather_snapshots WHERE provider_game_id='2'")}
    assert g1 == {"2026-07-22T18:00:00.000000Z"}  # only game 1's window
    assert g2 == {"2026-07-22T23:00:00.000000Z", "2026-07-23T00:00:00.000000Z"}


# --------------------------------------------------------------------------- #
# REPAIR: unit validation, extra, wind ranges
# --------------------------------------------------------------------------- #
async def test_open_meteo_hourly_units_validated_unexpected_becomes_null(db: Database) -> None:
    # temperature returned in °F (not the requested °C) -> canonical value NULL + extra + note.
    body = om_hourly(units={"temperature_2m": "°F"})
    seed_game(db, country="Canada", venue_name="Rogers Centre", lat=43.64, lon=-79.39,
              tz="America/Toronto")
    r = await _run(db, make_clients(om_forecast=body), mode="forecast")
    with db.connection() as conn:
        rows = conn.execute("SELECT temperature_c, wind_speed_ms, extra FROM weather_snapshots"
                            ).fetchall()
        note = conn.execute("SELECT COUNT(*) FROM data_quality_issues "
                            "WHERE rule_code='DQ-WX-NORM-001'").fetchone()[0]
    assert all(row[0] is None for row in rows)          # temperature not trusted -> NULL
    assert all(row[1] == 3.0 for row in rows)           # wind (correct unit) still normalized
    assert all("temperature_2m" in (row[2] or "") for row in rows)  # preserved in extra
    assert note >= 1 and r.data_quality_issues >= 1


def test_nws_precipitation_unit_validated() -> None:
    from datetime import datetime, timezone
    feats = nws_observations(features=[{"properties": {
        "timestamp": "2026-07-22T23:00:00+00:00",
        "precipitationLastHour": {"value": 2.0, "unitCode": "wmoUnit:m"}}}])  # metres!
    rows, _notes = normalize_nws_observations(
        feats, window_start=datetime(2026, 7, 22, 22, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 23, 3, 5, tzinfo=timezone.utc), station="KBOS")
    assert len(rows) == 1
    assert rows[0].values.precip_amount_mm == 2000.0  # 2 m -> 2000 mm (converted, not assumed)


def test_scalar_wind_normalizes_range_does_not() -> None:
    assert normalize_speed_ms("10 mph", None)[0] == pytest.approx(4.4704, abs=1e-4)
    val, note = normalize_speed_ms("5 to 10 mph", None)
    assert val is None and note is not None and "range" in note


async def test_wind_range_preserved_in_extra_with_null_speed(db: Database) -> None:
    hourly = nws_hourly(periods=[{
        "startTime": "2026-07-22T23:00:00+00:00", "temperature": 70, "temperatureUnit": "F",
        "windSpeed": "5 to 10 mph", "windDirection": "N", "shortForecast": "Breezy"}])
    seed_game(db)
    r = await _run(db, make_clients(nws_handler=default_nws_handler(hourly=hourly)),
                   mode="forecast")
    with db.connection() as conn:
        speed, extra = conn.execute(
            "SELECT wind_speed_ms, extra FROM weather_snapshots").fetchone()
    assert speed is None  # a range is never collapsed to a scalar
    assert "5 to 10 mph" in (extra or "")
    assert r.data_quality_issues >= 1  # a normalization note recorded


# --------------------------------------------------------------------------- #
# REPAIR: normalization DQ notes (dry-run vs persisted)
# --------------------------------------------------------------------------- #
async def test_normalization_notes_counted_in_dry_run(db: Database) -> None:
    hourly = nws_hourly(periods=[{
        "startTime": "2026-07-22T23:00:00+00:00", "temperature": 70, "temperatureUnit": "F",
        "windSpeed": "5 to 10 mph", "windDirection": "N", "shortForecast": "Breezy"}])
    seed_game(db)
    r = await _run(db, make_clients(nws_handler=default_nws_handler(hourly=hourly)),
                   mode="forecast", dry_run=True)
    assert r.data_quality_issues >= 1 and r.rows_persisted == 0
    assert _count(db, "data_quality_issues") == 0  # dry-run persisted nothing


async def test_normalization_notes_persisted_with_provenance(db: Database) -> None:
    hourly = nws_hourly(periods=[{
        "startTime": "2026-07-22T23:00:00+00:00", "temperature": 70, "temperatureUnit": "F",
        "windSpeed": "5 to 10 mph", "windDirection": "N", "shortForecast": "Breezy"}])
    seed_game(db)
    await _run(db, make_clients(nws_handler=default_nws_handler(hourly=hourly)), mode="forecast")
    with db.connection() as conn:
        row = conn.execute(
            "SELECT rule_code, provider, run_id, raw_response_id, entity_id "
            "FROM data_quality_issues WHERE rule_code='DQ-WX-NORM-001'").fetchone()
    assert row is not None
    assert row[1] == "nws" and row[2] is not None and row[3] is not None and row[4] == "745804"


# --------------------------------------------------------------------------- #
# REPAIR: per-response provider identity during fallback
# --------------------------------------------------------------------------- #
async def test_nws_discovery_keeps_provider_after_fallback(db: Database) -> None:
    # /points succeeds (nws); the returned hourly URL 404s (geographic) -> Open-Meteo
    # fallback. The point response must stay `nws`; weather rows use `open_meteo`.
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.startswith("/points/"):
            return httpx.Response(200, json=nws_point(),
                                  headers={"content-type": "application/geo+json"})
        if p.endswith("/forecast/hourly"):
            return httpx.Response(404, json={"detail": "not found"},
                                  headers={"content-type": "application/geo+json"})
        return httpx.Response(200, json={}, headers={"content-type": "application/geo+json"})
    clients = make_clients(nws_handler=handler)
    seed_game(db, country="USA")
    r = await _run(db, clients, mode="forecast")
    assert r.provider_fallbacks == 1 and r.active_failures == 0
    with db.connection() as conn:
        points = conn.execute(
            "SELECT provider FROM raw_responses WHERE endpoint LIKE '/points/%'").fetchall()
        wx_provider = {x[0] for x in conn.execute(
            "SELECT DISTINCT provider FROM weather_snapshots")}
        wx_raw_provider = conn.execute(
            "SELECT r.provider FROM weather_snapshots w JOIN raw_responses r "
            "ON w.raw_response_id = r.raw_response_id LIMIT 1").fetchone()[0]
    assert all(p[0] == "nws" for p in points)  # discovery response stays NWS
    assert wx_provider == {"open_meteo"}       # weather rows are Open-Meteo
    assert wx_raw_provider == "open_meteo"     # and reference the OM response


# --------------------------------------------------------------------------- #
# REPAIR: request counters + fallback classification
# --------------------------------------------------------------------------- #
async def test_actual_nws_request_count(db: Database) -> None:
    seed_game(db)
    r = await _run(db, make_clients(), mode="actual")
    assert r.nws_requests == 3  # point + station discovery + station observations
    assert r.requests_made == 3 and r.open_meteo_requests == 0


async def test_fallback_request_totals(db: Database) -> None:
    seed_game(db, country="USA")
    r = await _run(db, make_clients(nws_handler=default_nws_handler(point_status=404)),
                   mode="forecast")
    # 1 NWS point attempt (404) + 1 Open-Meteo forecast = 2 total GETs.
    assert r.nws_requests == 1 and r.open_meteo_requests == 1 and r.requests_made == 2
    assert r.provider_fallbacks == 1


async def test_dry_run_and_persisted_report_same_network_counts(db: Database) -> None:
    seed_game(db)
    r_dry = await _run(db, make_clients(), mode="forecast", dry_run=True)
    r_wet = await _run(db, make_clients(), mode="forecast")
    assert (r_dry.requests_made, r_dry.nws_requests, r_dry.open_meteo_requests) == \
        (r_wet.requests_made, r_wet.nws_requests, r_wet.open_meteo_requests) == (2, 2, 0)


async def test_nws_5xx_does_not_fall_back(db: Database) -> None:
    seed_game(db, country="USA")
    r = await _run(db, make_clients(nws_handler=default_nws_handler(point_status=503)),
                   mode="forecast")
    assert r.active_failures == 1 and r.provider_fallbacks == 0
    assert _count(db, "weather_snapshots") == 0


async def test_invalid_returned_url_is_active_failure_not_fallback(db: Database) -> None:
    # /points returns an OFF-HOST forecast URL -> SSRF-blocked -> active failure, NOT fallback.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/points/"):
            return httpx.Response(200, json={"properties": {
                "forecastHourly": "https://evil.example.com/gridpoints/x/1,1/forecast/hourly",
                "observationStations": "https://api.weather.gov/gridpoints/BOX/70,76/stations"}},
                headers={"content-type": "application/geo+json"})
        return httpx.Response(200, json={}, headers={"content-type": "application/geo+json"})
    seed_game(db, country="USA")
    r = await _run(db, make_clients(nws_handler=handler), mode="forecast")
    assert r.active_failures == 1 and r.provider_fallbacks == 0
    assert _count(db, "weather_snapshots") == 0


# --------------------------------------------------------------------------- #
# REPAIR: missing-venue DQ evidence (persist + dry-run)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs,code", [
    ({"lat": None, "lon": None}, "DQ-WX-COORD-001"),
    ({"tz": None}, "DQ-WX-TZ-001"),
    ({"roof": None}, "DQ-WX-ROOF-001"),
    ({"start": None}, "DQ-WX-SCHED-001"),
])
async def test_missing_metadata_persists_dq_and_makes_no_request(
    db: Database, kwargs: dict[str, Any], code: str
) -> None:
    seed_game(db, **kwargs)
    r = await _run(db, make_clients(), mode="forecast")
    assert r.games_skipped_missing_venue == 1 and r.games_eligible == 0
    assert r.nws_requests == 0 and r.open_meteo_requests == 0
    with db.connection() as conn:
        got = conn.execute("SELECT COUNT(*) FROM data_quality_issues WHERE rule_code=?",
                           (code,)).fetchone()[0]
    assert got == 1


async def test_missing_metadata_dry_run_persists_no_dq(db: Database) -> None:
    seed_game(db, lat=None, lon=None)
    r = await _run(db, make_clients(), mode="forecast", dry_run=True)
    assert r.games_skipped_missing_venue == 1 and r.data_quality_issues >= 1
    assert _count(db, "data_quality_issues") == 0  # dry-run persisted nothing


async def test_indoor_skip_is_not_a_data_quality_issue(db: Database) -> None:
    seed_game(db, roof="dome")
    r = await _run(db, make_clients(), mode="forecast")
    assert r.indoor_games_skipped == 1
    with db.connection() as conn:
        dq = conn.execute("SELECT COUNT(*) FROM data_quality_issues "
                         "WHERE rule_code LIKE 'DQ-WX-%'").fetchone()[0]
    assert dq == 0  # an intentional not-applicable skip is not bad data
