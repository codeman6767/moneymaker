"""Phase D4 weather ingestion (NWS primary, Open-Meteo secondary/historical).

Attaches weather observations to official MLB games via the existing schedule
snapshots + provider game references + canonical venues -- no second venue/game
system, no D5 canonical matching required. Everything is GET-only through the
shared provider clients; each raw response is preserved before it is normalized;
every derived weather row is append-only and traces to the exact raw response.

Correctness boundaries (permanent, see CLAUDE.md + POINT_IN_TIME_DATA.md):

* Roof gating: an open/outdoor venue gets weather; a dome/fixed/indoor venue is
  SKIPPED with no provider request (never synthetic indoor weather); a retractable
  roof is CONDITIONAL (roof status unknown -- never assumed open). Missing venue
  coordinates/timezone/roof are skipped honestly, never guessed.
* Kinds are distinct: a forecast is not an observation; reanalysis is not a
  pregame forecast.
* ``observed_at`` is when THIS project received the response and is NEVER
  backdated to a historical model-run time. A historical-forecast row's
  point-in-time eligibility is UNKNOWN (never asserted) unless availability before
  a cutoff can be proven; reanalysis/observations are never PIT-eligible pregame
  forecasts.
* ``--dry-run`` runs identical routing/parsing/validation/counting and persists
  absolutely nothing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from streaming.event_envelope import canonical_json

from ..db.engine import Database, transaction
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.ingestion_runs import SqliteIngestionRunRepository
from ..db.repositories.official_games import SqliteScheduleRepository
from ..db.repositories.raw_responses import SqliteRawResponseRepository, response_content_hash
from ..db.repositories.venues import SqliteVenueRepository
from ..db.repositories.weather import (
    SqliteWeatherRepository,
    WeatherOutcome,
    WeatherValues,
)
from ..db.schema import to_iso
from ..providers.base_provider import ProviderError, ProviderResponse
from ..providers.capabilities import (
    PROVIDER_MLB_STATSAPI,
    PROVIDER_NWS,
    PROVIDER_OPEN_METEO,
    ProviderErrorKind,
)
from ..providers.nws import NwsClient
from ..providers.open_meteo import OpenMeteoClient
from .runner import sanitize_error

_TOOL_VERSION = "sports_quant 0.1.0"
_COMMAND = "ingest-weather"

#: The deterministic bounded window around scheduled first pitch: one hour before
#: through four hours after -- enough to cover a typical MLB game without an
#: unbounded request. Documented so the corpus records exactly what was requested.
WINDOW_BEFORE = timedelta(hours=1)
WINDOW_AFTER = timedelta(hours=4)

VALID_MODES = ("forecast", "actual", "historical-forecast")

#: US country strings the seed data uses; NWS is US-only.
_US_COUNTRIES = frozenset({"usa", "us", "united states", "united states of america"})

_INDOOR_ROOFS = frozenset({"dome", "fixed", "indoor"})


# --------------------------------------------------------------------------- #
# Result counters
# --------------------------------------------------------------------------- #
@dataclass
class WeatherIngestResult:
    dry_run: bool
    status: str
    mode: str
    command: str = _COMMAND
    run_id: Optional[str] = None
    requests_made: int = 0
    raw_responses_received: int = 0
    games_considered: int = 0
    games_eligible: int = 0
    outdoor_games: int = 0
    retractable_conditional_games: int = 0
    indoor_games_skipped: int = 0
    games_skipped_missing_venue: int = 0
    nws_requests: int = 0
    open_meteo_requests: int = 0
    provider_fallbacks: int = 0
    request_groups_deduplicated: int = 0
    forecast_observations: int = 0
    station_observations: int = 0
    historical_forecast_observations: int = 0
    reanalysis_observations: int = 0
    observations_normalized: int = 0
    rows_persisted: int = 0
    records_inserted: int = 0
    records_changed: int = 0
    records_unchanged: int = 0
    records_rejected: int = 0
    data_quality_issues: int = 0
    capability_gaps: int = 0
    active_failures: int = 0
    records_truncated: int = 0
    notes: list[str] = field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def has_active_failure(self) -> bool:
        return self.active_failures > 0

    @property
    def needs_failure_exit(self) -> bool:
        return self.status in ("failed", "partially_failed")

    def note(self, reason: str) -> None:
        if len(self.notes) < 50:
            self.notes.append(reason)

    def record_active_failure(self, error_type: str, message: str) -> None:
        self.active_failures += 1
        if self.error_message is None:
            self.error_type, self.error_message = error_type, message


@dataclass
class WeatherClients:
    """The four GET-only clients the ingestor routes across (injectable for tests)."""

    nws: NwsClient
    om_forecast: OpenMeteoClient
    om_historical: OpenMeteoClient
    om_archive: OpenMeteoClient

    async def aclose(self) -> None:
        for c in (self.nws, self.om_forecast, self.om_historical, self.om_archive):
            await c.aclose()


# --------------------------------------------------------------------------- #
# Unit normalization (pure; unknown unit -> (None, note), never a guess)
# --------------------------------------------------------------------------- #
def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _leading_number(value: Any) -> Optional[float]:
    """First numeric token of a value (handles ``"10 mph"``, ``"5 to 10 mph"``)."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUM_RE.search(str(value))
    return float(match.group()) if match else None


def normalize_temperature_c(value: Any, unit: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Return ``(celsius, note)``. Unknown unit -> ``(None, note)``, never guessed."""

    num = _leading_number(value)
    if num is None:
        return None, None
    u = (unit or "").strip().lower().replace("wmounit:", "")
    if u in ("c", "degc", "celsius", "°c"):
        return num, None
    if u in ("f", "degf", "fahrenheit", "°f"):
        return _f_to_c(num), None
    return None, f"unknown temperature unit {unit!r}; value preserved in extra"


def normalize_speed_ms(value: Any, unit: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Return ``(m/s, note)``. Unknown unit -> ``(None, note)``.

    When ``unit`` is omitted but ``value`` is a string carrying its own unit
    (NWS hourly ``windSpeed`` like ``"10 mph"``), the unit is read from the string;
    it is never guessed when genuinely absent.
    """

    num = _leading_number(value)
    if num is None:
        return None, None
    if unit is None and isinstance(value, str):
        low = value.lower()
        if "mph" in low:
            unit = "mph"
        elif "km/h" in low or "kmh" in low:
            unit = "km/h"
        elif "m/s" in low:
            unit = "m/s"
    u = (unit or "").strip().lower().replace("wmounit:", "")
    if u in ("m_s-1", "m/s", "ms", "mps", "meters per second"):
        return num, None
    if u in ("km_h-1", "km/h", "kmh", "kph"):
        return num / 3.6, None
    if u in ("mph", "mi_h-1", "miles per hour"):
        return num * 0.44704, None
    return None, f"unknown wind-speed unit {unit!r}; value preserved in extra"


_COMPASS = {
    "n": 0.0, "nne": 22.5, "ne": 45.0, "ene": 67.5, "e": 90.0, "ese": 112.5,
    "se": 135.0, "sse": 157.5, "s": 180.0, "ssw": 202.5, "sw": 225.0, "wsw": 247.5,
    "w": 270.0, "wnw": 292.5, "nw": 315.0, "nnw": 337.5,
}


def normalize_direction_deg(value: Any) -> tuple[Optional[float], Optional[str]]:
    """Return ``(degrees, note)`` from a numeric or 16-point compass value."""

    if value is None:
        return None, None
    if isinstance(value, str) and value.strip().lower() in _COMPASS:
        return _COMPASS[value.strip().lower()], None
    num = _leading_number(value)
    if num is None:
        return None, f"unparseable wind direction {value!r}; value preserved in extra"
    if not (0.0 <= num <= 360.0):
        return None, f"wind direction {num} out of range; value preserved in extra"
    return num, None


def _opt_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Open-Meteo hourly times ("2026-07-22T18:00") are naive -> assume UTC only
        # for a value that already parses; a truly malformed value returns None.
        try:
            dt = datetime.fromisoformat(text + "+00:00")
        except ValueError:
            return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --------------------------------------------------------------------------- #
# Per-provider normalizers (pure; window-bounded)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _WxObs:
    """One normalized hourly weather observation (canonical units)."""

    valid_time: str
    values: WeatherValues
    source_station: Optional[str] = None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _in_window(valid: Optional[datetime], start: datetime, end: datetime) -> bool:
    return valid is not None and start <= valid <= end


def _pct(value: Any) -> Optional[float]:
    num = _opt_float(value)
    if num is None:
        return None
    return num if 0.0 <= num <= 100.0 else None


def _collect_notes(notes: list[str], note: Optional[str]) -> None:
    if note and note not in notes:
        notes.append(note)


def normalize_nws_forecast(
    data: Any, *, window_start: datetime, window_end: datetime
) -> tuple[list[_WxObs], list[str]]:
    """Normalize an NWS hourly-forecast response into windowed observations."""

    notes: list[str] = []
    out: list[_WxObs] = []
    periods = _as_dict(_as_dict(data).get("properties")).get("periods")
    for period in periods if isinstance(periods, list) else []:
        if not isinstance(period, dict):
            continue
        valid_dt = _parse_iso(period.get("startTime"))
        if not _in_window(valid_dt, window_start, window_end):
            continue
        assert valid_dt is not None  # noqa: S101 - guarded by _in_window
        temp_c, note = normalize_temperature_c(period.get("temperature"),
                                               period.get("temperatureUnit"))
        _collect_notes(notes, note)
        wind_ms, note = normalize_speed_ms(period.get("windSpeed"), None)
        _collect_notes(notes, note)
        dir_deg, note = normalize_direction_deg(period.get("windDirection"))
        _collect_notes(notes, note)
        dew = _as_dict(period.get("dewpoint"))
        dew_c, note = normalize_temperature_c(dew.get("value"), dew.get("unitCode"))
        _collect_notes(notes, note)
        humidity = _pct(_as_dict(period.get("relativeHumidity")).get("value"))
        precip_prob = _pct(_as_dict(period.get("probabilityOfPrecipitation")).get("value"))
        out.append(_WxObs(
            valid_time=_iso_z(valid_dt),
            values=WeatherValues(
                temperature_c=temp_c, dew_point_c=dew_c, relative_humidity_pct=humidity,
                wind_speed_ms=wind_ms, wind_direction_deg=dir_deg,
                precip_probability_pct=precip_prob,
                condition_text=_opt_str(period.get("shortForecast")),
            ),
        ))
    return out, notes


def normalize_nws_observations(
    data: Any, *, window_start: datetime, window_end: datetime, station: Optional[str]
) -> tuple[list[_WxObs], list[str]]:
    """Normalize an NWS station-observations response into windowed observations."""

    notes: list[str] = []
    out: list[_WxObs] = []
    features = _as_dict(data).get("features")
    for feature in features if isinstance(features, list) else []:
        props = _as_dict(_as_dict(feature).get("properties"))
        valid_dt = _parse_iso(props.get("timestamp"))
        if not _in_window(valid_dt, window_start, window_end):
            continue
        assert valid_dt is not None  # noqa: S101
        temp = _as_dict(props.get("temperature"))
        temp_c, note = normalize_temperature_c(temp.get("value"), temp.get("unitCode"))
        _collect_notes(notes, note)
        wind = _as_dict(props.get("windSpeed"))
        wind_ms, note = normalize_speed_ms(wind.get("value"), wind.get("unitCode"))
        _collect_notes(notes, note)
        gust = _as_dict(props.get("windGust"))
        gust_ms, note = normalize_speed_ms(gust.get("value"), gust.get("unitCode"))
        _collect_notes(notes, note)
        wdir = _as_dict(props.get("windDirection"))
        dir_deg, note = normalize_direction_deg(wdir.get("value"))
        _collect_notes(notes, note)
        dew = _as_dict(props.get("dewpoint"))
        dew_c, note = normalize_temperature_c(dew.get("value"), dew.get("unitCode"))
        _collect_notes(notes, note)
        humidity = _pct(_as_dict(props.get("relativeHumidity")).get("value"))
        precip_mm = _opt_float(_as_dict(props.get("precipitationLastHour")).get("value"))
        if precip_mm is not None and precip_mm < 0:
            precip_mm = None
        out.append(_WxObs(
            valid_time=_iso_z(valid_dt),
            source_station=station,
            values=WeatherValues(
                temperature_c=temp_c, dew_point_c=dew_c, relative_humidity_pct=humidity,
                wind_speed_ms=wind_ms, wind_gust_ms=gust_ms, wind_direction_deg=dir_deg,
                precip_amount_mm=precip_mm,
                condition_text=_opt_str(props.get("textDescription")),
            ),
        ))
    return out, notes


_OM_FIELDS = (
    "temperature_2m", "apparent_temperature", "dew_point_2m", "relative_humidity_2m",
    "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "precipitation",
    "precipitation_probability", "weather_code",
)


def normalize_open_meteo(
    data: Any, *, window_start: datetime, window_end: datetime
) -> tuple[list[_WxObs], list[str], bool]:
    """Normalize an Open-Meteo hourly response. Returns ``(rows, notes, rejected)``.

    Canonical units are requested from the API, so no conversion is done here.
    Every supplied hourly array must have the SAME length as ``time`` and matching
    timestamps; a length mismatch is rejected honestly (``rejected=True``, no rows).
    A missing element stays missing (never zero).
    """

    notes: list[str] = []
    hourly = _as_dict(_as_dict(data).get("hourly"))
    times = hourly.get("time")
    if not isinstance(times, list):
        return [], ["open-meteo response has no hourly.time array"], True
    n = len(times)
    present: dict[str, list[Any]] = {}
    for name in _OM_FIELDS:
        arr = hourly.get(name)
        if arr is None:
            continue
        if not isinstance(arr, list) or len(arr) != n:
            return [], [f"open-meteo hourly.{name} length != time length"], True
        present[name] = arr
    out: list[_WxObs] = []
    for i, t in enumerate(times):
        valid_dt = _parse_iso(t)
        if not _in_window(valid_dt, window_start, window_end):
            continue
        assert valid_dt is not None  # noqa: S101

        def val(name: str, idx: int = i) -> Any:
            arr = present.get(name)
            return arr[idx] if arr is not None else None

        out.append(_WxObs(
            valid_time=_iso_z(valid_dt),
            values=WeatherValues(
                temperature_c=_opt_float(val("temperature_2m")),
                apparent_temperature_c=_opt_float(val("apparent_temperature")),
                dew_point_c=_opt_float(val("dew_point_2m")),
                relative_humidity_pct=_pct(val("relative_humidity_2m")),
                wind_speed_ms=_opt_float(val("wind_speed_10m")),
                wind_gust_ms=_opt_float(val("wind_gusts_10m")),
                wind_direction_deg=_opt_float(val("wind_direction_10m")),
                precip_amount_mm=_opt_float(val("precipitation")),
                precip_probability_pct=_pct(val("precipitation_probability")),
                weather_code=_opt_str(val("weather_code")),
            ),
        ))
    return out, notes, False


def nws_station_id_from_list(data: Any) -> Optional[str]:
    """First station id from an NWS ``/stations`` (or gridpoint stations) response."""

    features = _as_dict(data).get("features")
    for feature in features if isinstance(features, list) else []:
        props = _as_dict(_as_dict(feature).get("properties"))
        sid = _opt_str(props.get("stationIdentifier")) or _opt_str(_as_dict(feature).get("id"))
        if sid:
            return sid.rsplit("/", 1)[-1]
    return None


# --------------------------------------------------------------------------- #
# Game resolution + roof gating
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Game:
    game_ref_id: str
    provider_game_id: str
    venue_id: str
    country: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    timezone: Optional[str]
    roof_type: Optional[str]
    scheduled_start: Optional[str]
    game_date_local: Optional[str]


def _roof_applicability(roof_type: Optional[str]) -> str:
    """Map a venue roof type to a weather decision.

    ``open`` -> applicable (outdoor); ``retractable`` -> conditional (roof status
    unknown, never assumed open); ``dome``/``fixed``/``indoor`` -> skip (no request,
    no synthetic weather); missing roof -> ``missing`` (skip honestly, never guess).
    """

    if roof_type is None:
        return "missing"
    if roof_type == "open":
        return "applicable"
    if roof_type == "retractable":
        return "conditional"
    if roof_type in _INDOOR_ROOFS:
        return "indoor"
    return "missing"


def _route(mode: str, country: Optional[str]) -> tuple[str, str, str, str]:
    """``(provider, surface, weather_kind, forecast_mode)`` for a mode + country."""

    is_us = (country or "").strip().lower() in _US_COUNTRIES
    if mode == "forecast":
        if is_us:
            return PROVIDER_NWS, "nws_forecast", "current_forecast", "nws_hourly_forecast"
        return PROVIDER_OPEN_METEO, "om_forecast", "current_forecast", "open_meteo_forecast"
    if mode == "actual":
        if is_us:
            return PROVIDER_NWS, "nws_observations", "station_observation", "nws_station_observation"
        return PROVIDER_OPEN_METEO, "om_archive", "reanalysis", "open_meteo_archive"
    return (PROVIDER_OPEN_METEO, "om_historical", "historical_forecast",
            "open_meteo_historical_forecast")


def _resolve_games(
    conn: Any, *, from_date: Optional[str], to_date: Optional[str], game_pk: Optional[int]
) -> list[_Game]:
    schedule = SqliteScheduleRepository(conn)
    venues = SqliteVenueRepository(conn)
    rows: list[Any] = []
    if game_pk is not None:
        row = schedule.latest_for_provider_game(PROVIDER_MLB_STATSAPI, str(game_pk))
        if row is not None:
            rows = [row]
    elif from_date is not None and to_date is not None:
        rows = schedule.latest_games_in_date_range(PROVIDER_MLB_STATSAPI, from_date, to_date)
    games: list[_Game] = []
    for row in rows:
        venue_pid = _opt_str(row["venue_provider_id"])
        venue = (venues.get_by_provider_venue_id(PROVIDER_MLB_STATSAPI, venue_pid)
                 if venue_pid else None)
        games.append(_Game(
            game_ref_id=str(row["game_ref_id"]),
            provider_game_id=str(row["provider_game_id"]),
            venue_id="" if venue is None else venue.venue_id,
            country=None if venue is None else venue.country,
            latitude=None if venue is None else venue.latitude,
            longitude=None if venue is None else venue.longitude,
            timezone=None if venue is None else venue.timezone,
            roof_type=None if venue is None else venue.roof_type,
            scheduled_start=_opt_str(row["scheduled_start"]),
            game_date_local=_opt_str(row["game_date_local"]),
        ))
    return games


def _window(scheduled_start: Optional[str]) -> Optional[tuple[datetime, datetime]]:
    start = _parse_iso(scheduled_start)
    if start is None:
        return None
    return start - WINDOW_BEFORE, start + WINDOW_AFTER


# --------------------------------------------------------------------------- #
# Fetch (GET-only, sequential, with honest NWS geographic fallback)
# --------------------------------------------------------------------------- #
@dataclass
class _GroupFetch:
    provider: str
    weather_kind: str
    forecast_mode: str
    responses: list[ProviderResponse] = field(default_factory=list)
    weather_response: Optional[ProviderResponse] = None
    source_station: Optional[str] = None
    weather_model: Optional[str] = None
    active_failure: bool = False


async def _fetch_group(
    clients: WeatherClients, *, mode: str, game: _Game,
    req_start: datetime, req_end: datetime, result: WeatherIngestResult,
) -> _GroupFetch:
    provider, surface, weather_kind, forecast_mode = _route(mode, game.country)
    lat, lon = game.latitude, game.longitude
    tz = game.timezone or "UTC"
    date = game.game_date_local
    assert lat is not None and lon is not None and date is not None  # eligibility-checked
    fetched = _GroupFetch(provider=provider, weather_kind=weather_kind, forecast_mode=forecast_mode)

    async def _open_meteo_forecast() -> None:
        result.open_meteo_requests += 1
        resp = await clients.om_forecast.fetch_forecast(
            lat, lon, start_date=date, end_date=date, timezone=tz)
        result.requests_made += 1
        fetched.responses.append(resp)
        fetched.weather_response = resp
        fetched.weather_model = "best_match"

    async def _open_meteo_archive() -> None:
        result.open_meteo_requests += 1
        resp = await clients.om_archive.fetch_archive(
            lat, lon, start_date=date, end_date=date, timezone=tz)
        result.requests_made += 1
        fetched.responses.append(resp)
        fetched.weather_response = resp
        fetched.weather_model = "era5"

    try:
        if surface == "nws_forecast":
            result.nws_requests += 1
            point = await clients.nws.fetch_point(lat, lon)
            result.requests_made += 1
            fetched.responses.append(point)
            url = _as_dict(_as_dict(point.data).get("properties")).get("forecastHourly")
            hourly = await clients.nws.fetch_returned_url(url)
            result.requests_made += 1
            fetched.responses.append(hourly)
            fetched.weather_response = hourly
        elif surface == "nws_observations":
            result.nws_requests += 1
            point = await clients.nws.fetch_point(lat, lon)
            result.requests_made += 1
            fetched.responses.append(point)
            stations_url = _as_dict(_as_dict(point.data).get("properties")).get(
                "observationStations")
            stations = await clients.nws.fetch_returned_url(stations_url)
            result.requests_made += 1
            fetched.responses.append(stations)
            station_id = nws_station_id_from_list(stations.data)
            if station_id is None:
                result.note(f"no NWS observation station for game {game.provider_game_id}")
                result.capability_gaps += 1
                return fetched
            fetched.source_station = station_id
            obs = await clients.nws.fetch_station_observations(
                station_id, start=_iso_z(req_start), end=_iso_z(req_end))
            result.requests_made += 1
            fetched.responses.append(obs)
            fetched.weather_response = obs
        elif surface == "om_forecast":
            await _open_meteo_forecast()
        elif surface == "om_archive":
            await _open_meteo_archive()
        elif surface == "om_historical":
            result.open_meteo_requests += 1
            resp = await clients.om_historical.fetch_historical_forecast(
                lat, lon, start_date=date, end_date=date, timezone=tz)
            result.requests_made += 1
            fetched.responses.append(resp)
            fetched.weather_response = resp
            fetched.weather_model = "best_match"
        return fetched
    except ProviderError as exc:
        # NWS geographic unavailability (a 404 for a location NWS does not cover) is
        # an HONEST fallback to Open-Meteo -- NOT a 5xx/network/parse failure.
        if provider == PROVIDER_NWS and exc.kind is ProviderErrorKind.NOT_FOUND:
            result.provider_fallbacks += 1
            result.capability_gaps += 1
            result.note(f"NWS unavailable for game {game.provider_game_id}; using Open-Meteo")
            try:
                if mode == "forecast":
                    fetched.provider = PROVIDER_OPEN_METEO
                    fetched.weather_kind = "current_forecast"
                    fetched.forecast_mode = "open_meteo_forecast"
                    await _open_meteo_forecast()
                else:  # actual -> reanalysis
                    fetched.provider = PROVIDER_OPEN_METEO
                    fetched.weather_kind = "reanalysis"
                    fetched.forecast_mode = "open_meteo_archive"
                    await _open_meteo_archive()
                return fetched
            except Exception as exc2:  # noqa: BLE001
                et, msg = sanitize_error(exc2)
                result.record_active_failure(et, f"fallback {game.provider_game_id}: {msg}")
                fetched.active_failure = True
                return fetched
        et, msg = sanitize_error(exc)
        result.record_active_failure(et, f"{surface} {game.provider_game_id}: {msg}")
        fetched.active_failure = True
        return fetched
    except Exception as exc:  # noqa: BLE001
        et, msg = sanitize_error(exc)
        result.record_active_failure(et, f"{surface} {game.provider_game_id}: {msg}")
        fetched.active_failure = True
        return fetched


def _normalize(
    fetched: _GroupFetch, *, window_start: datetime, window_end: datetime
) -> tuple[list[_WxObs], list[str], bool]:
    resp = fetched.weather_response
    if resp is None:
        return [], [], False
    if fetched.forecast_mode == "nws_hourly_forecast":
        rows, notes = normalize_nws_forecast(
            resp.data, window_start=window_start, window_end=window_end)
        return rows, notes, False
    if fetched.forecast_mode == "nws_station_observation":
        rows, notes = normalize_nws_observations(
            resp.data, window_start=window_start, window_end=window_end,
            station=fetched.source_station)
        return rows, notes, False
    rows, notes, rejected = normalize_open_meteo(
        resp.data, window_start=window_start, window_end=window_end)
    return rows, notes, rejected


def _pit_for(kind: str, valid_time: str, observed_at: str, scheduled_start: Optional[str]
             ) -> tuple[Optional[bool], Optional[str], Optional[int]]:
    """``(pit_eligible, forecast_target_time, lead_time_seconds)`` for a kind.

    A current forecast is PIT-eligible only when it was received before first pitch
    (available before the game). Station observations and reanalysis are never a
    pregame forecast (eligible = False). A stitched historical forecast's exact
    availability cannot be proven, so eligibility is UNKNOWN (None) -- the caller
    records an honest data-quality note.
    """

    if kind in ("current_forecast", "historical_forecast"):
        target = valid_time
        lead = None
        vt, obs = _parse_iso(valid_time), _parse_iso(observed_at)
        if vt is not None and obs is not None:
            delta = int((vt - obs).total_seconds())
            lead = delta if delta >= 0 else None
        if kind == "historical_forecast":
            return None, target, lead  # unknown eligibility
        eligible = scheduled_start is not None and observed_at <= scheduled_start
        return eligible, target, lead
    # station_observation / reanalysis -> never a pregame forecast feature.
    return False, None, None


# --------------------------------------------------------------------------- #
# Per-game build (shared by count + persist)
# --------------------------------------------------------------------------- #
@dataclass
class _PreparedRow:
    valid_time: str
    values: WeatherValues
    pit_eligible: Optional[bool]
    forecast_target_time: Optional[str]
    lead_time_seconds: Optional[int]
    source_station: Optional[str]


@dataclass
class _GamePlan:
    kind: str
    forecast_mode: str
    weather_model: Optional[str]
    observed_at: Optional[str]
    retrieved_at: Optional[str]
    rows: list[_PreparedRow]
    notes: list[str]
    rejected: bool
    weather_response: Optional[ProviderResponse]


def _build_game_plan(game: _Game, fetched: _GroupFetch, mode: str) -> _GamePlan:
    resp = fetched.weather_response
    if resp is None or fetched.active_failure:
        return _GamePlan(fetched.weather_kind, fetched.forecast_mode, fetched.weather_model,
                         None, None, [], [], False, None)
    win = _window(game.scheduled_start)
    assert win is not None  # eligibility-checked
    obs_rows, notes, rejected = _normalize(fetched, window_start=win[0], window_end=win[1])
    observed_at = to_iso(resp.exchange.received_at)
    retrieved_at = observed_at
    prepared: list[_PreparedRow] = []
    for o in obs_rows:
        pit, target, lead = _pit_for(fetched.weather_kind, o.valid_time, observed_at,
                                     game.scheduled_start)
        prepared.append(_PreparedRow(
            valid_time=o.valid_time, values=o.values, pit_eligible=pit,
            forecast_target_time=target, lead_time_seconds=lead,
            source_station=o.source_station or fetched.source_station,
        ))
    return _GamePlan(fetched.weather_kind, fetched.forecast_mode, fetched.weather_model,
                     observed_at, retrieved_at, prepared, notes, rejected, resp)


_KIND_COUNTER = {
    "current_forecast": "forecast_observations",
    "station_observation": "station_observations",
    "historical_forecast": "historical_forecast_observations",
    "reanalysis": "reanalysis_observations",
}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
async def ingest_weather(
    *,
    database: Database,
    clients: WeatherClients,
    mode: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    game_pk: Optional[int] = None,
    dry_run: bool = False,
    tool_version: str = _TOOL_VERSION,
) -> WeatherIngestResult:
    """Ingest weather for official MLB games. ``--dry-run`` persists nothing."""

    result = WeatherIngestResult(dry_run=dry_run, status="succeeded", mode=mode)
    if mode not in VALID_MODES:
        result.status = "failed"
        result.error_type, result.error_message = "ValueError", f"unknown weather mode {mode!r}"
        return result

    # Dry-run must never CREATE a database; only read an existing corpus.
    if dry_run and not database.path.exists():
        return result

    with database.connection() as conn:
        games = _resolve_games(conn, from_date=from_date, to_date=to_date, game_pk=game_pk)
        result.games_considered = len(games)

        eligible: list[tuple[_Game, str]] = []
        for g in games:
            if g.venue_id == "":
                result.games_skipped_missing_venue += 1
                result.note(f"game {g.provider_game_id}: no resolvable venue")
                continue
            label = _roof_applicability(g.roof_type)
            if label == "indoor":
                result.indoor_games_skipped += 1
                continue
            if label == "missing":
                result.games_skipped_missing_venue += 1
                result.note(f"game {g.provider_game_id}: missing/unknown roof type")
                continue
            if g.latitude is None or g.longitude is None or g.timezone is None:
                result.games_skipped_missing_venue += 1
                result.note(f"game {g.provider_game_id}: missing coordinates/timezone")
                continue
            if g.game_date_local is None or _window(g.scheduled_start) is None:
                result.games_skipped_missing_venue += 1
                result.note(f"game {g.provider_game_id}: missing scheduled start/date")
                continue
            applicability = "applicable" if label == "applicable" else "conditional_roof_unknown"
            if label == "applicable":
                result.outdoor_games += 1
            else:
                result.retractable_conditional_games += 1
            result.games_eligible += 1
            eligible.append((g, applicability))

        # Dedup: group eligible games by (surface, venue, date) -> one provider fetch.
        groups: dict[tuple[str, str, str], list[tuple[_Game, str]]] = {}
        for g, appl in eligible:
            _p, surface, _k, _fm = _route(mode, g.country)
            key = (surface, g.venue_id, g.game_date_local or "")
            groups.setdefault(key, []).append((g, appl))

        group_fetch: dict[tuple[str, str, str], _GroupFetch] = {}
        for key, members in groups.items():
            wins = [w for w in (_window(g.scheduled_start) for g, _ in members) if w is not None]
            req_start = min(w[0] for w in wins)
            req_end = max(w[1] for w in wins)
            fetched = await _fetch_group(
                clients, mode=mode, game=members[0][0], req_start=req_start, req_end=req_end,
                result=result)
            group_fetch[key] = fetched
            if len(members) > 1:
                result.request_groups_deduplicated += len(members) - 1

        if dry_run:
            _count_plan(eligible, mode, group_fetch, result)
            result.status = "partially_failed" if result.has_active_failure else "succeeded"
            return result

        return _persist(conn, database, eligible, mode, group_fetch, result, tool_version)


def _count_plan(
    eligible: list[tuple[_Game, str]], mode: str,
    group_fetch: dict[tuple[str, str, str], _GroupFetch], result: WeatherIngestResult,
) -> None:
    for g, _appl in eligible:
        _p, surface, _k, _fm = _route(mode, g.country)
        fetched = group_fetch[(surface, g.venue_id, g.game_date_local or "")]
        plan = _build_game_plan(g, fetched, mode)
        if plan.rejected:
            result.records_rejected += 1
            result.data_quality_issues += 1
            continue
        counter = _KIND_COUNTER[plan.kind]
        for _row in plan.rows:
            setattr(result, counter, getattr(result, counter) + 1)
            result.observations_normalized += 1
            result.records_inserted += 1
        if plan.kind == "historical_forecast" and plan.rows:
            result.data_quality_issues += 1  # one honest PIT-unknown note per game


def _persist(
    conn: Any, database: Database, eligible: list[tuple[_Game, str]], mode: str,
    group_fetch: dict[tuple[str, str, str], _GroupFetch], result: WeatherIngestResult,
    tool_version: str,
) -> WeatherIngestResult:
    started = time.monotonic_ns()
    runs = SqliteIngestionRunRepository(conn)
    with transaction(conn):
        run = runs.start(
            command=_COMMAND, provider="weather", operation=f"ingest_weather_{mode}",
            args_json=canonical_json({"mode": mode}), started_monotonic_ns=started,
            tool_version=tool_version, sport="mlb",
        )
    result.run_id = run.run_id
    raw_repo = SqliteRawResponseRepository(conn)
    dq = SqliteDataQualityRepository(conn)
    weather_repo = SqliteWeatherRepository(conn)

    # Store each group's raw responses ONCE; remember (raw_id, hash) per response.
    raws: dict[int, tuple[str, str]] = {}
    for fetched in group_fetch.values():
        for resp in fetched.responses:
            if id(resp) in raws:
                continue
            provider = fetched.provider
            exchange = resp.exchange
            ch = response_content_hash(
                provider=provider, endpoint=exchange.endpoint,
                request_params=exchange.request_params, body=exchange.body)
            with transaction(conn):
                stored = raw_repo.store(
                    run_id=run.run_id, provider=provider, endpoint=exchange.endpoint,
                    request_params_json=canonical_json(exchange.request_params),
                    http_status=exchange.http_status,
                    response_headers_json=canonical_json(exchange.response_headers),
                    requested_at=to_iso(exchange.requested_at),
                    received_at=to_iso(exchange.received_at), elapsed_ns=exchange.elapsed_ns,
                    body=exchange.body, content_hash=ch, content_type=exchange.content_type)
            raws[id(resp)] = (stored.raw_response_id, ch)
            result.raw_responses_received += 1

    ingested = to_iso(datetime.now(timezone.utc))
    for g, appl in eligible:
        _p, surface, _k, _fm = _route(mode, g.country)
        fetched = group_fetch[(surface, g.venue_id, g.game_date_local or "")]
        plan = _build_game_plan(g, fetched, mode)
        if plan.weather_response is None:
            continue
        raw_id, raw_hash = raws[id(plan.weather_response)]
        try:
            with transaction(conn):
                if plan.rejected:
                    result.records_rejected += 1
                    dq.record(severity="issue", rule_code="DQ-WX-PARSE-001", entity_type="game",
                              description=f"weather response for game {g.provider_game_id} "
                              "failed array-length validation; no rows normalized",
                              provider=fetched.provider, run_id=run.run_id,
                              raw_response_id=raw_id, entity_id=g.provider_game_id)
                    result.data_quality_issues += 1
                    continue
                for row in plan.rows:
                    _wid, outcome = weather_repo.append(
                        game_ref_id=g.game_ref_id, provider=fetched.provider,
                        provider_game_id=g.provider_game_id, venue_id=g.venue_id,
                        weather_kind=plan.kind, applicability=appl,
                        forecast_mode=plan.forecast_mode, valid_time=row.valid_time,
                        observed_at=plan.observed_at or ingested,
                        retrieved_at=plan.retrieved_at or ingested, ingested_at=ingested,
                        run_id=run.run_id, raw_response_id=raw_id, raw_response_hash=raw_hash,
                        values=row.values, roof_type_at_decision=g.roof_type,
                        requested_latitude=g.latitude, requested_longitude=g.longitude,
                        source_station=row.source_station, weather_model=plan.weather_model,
                        forecast_target_time=row.forecast_target_time,
                        lead_time_seconds=row.lead_time_seconds, pit_eligible=row.pit_eligible)
                    counter = _KIND_COUNTER[plan.kind]
                    if outcome is WeatherOutcome.INSERTED:
                        result.records_inserted += 1
                    elif outcome is WeatherOutcome.CHANGED:
                        result.records_changed += 1
                    elif outcome is WeatherOutcome.UNCHANGED:
                        result.records_unchanged += 1
                    if outcome in (WeatherOutcome.INSERTED, WeatherOutcome.CHANGED):
                        result.rows_persisted += 1
                        setattr(result, counter, getattr(result, counter) + 1)
                        result.observations_normalized += 1
                if plan.kind == "historical_forecast" and plan.rows:
                    dq.record(severity="note", rule_code="DQ-WX-PIT-001", entity_type="game",
                              description=f"historical-forecast rows for game {g.provider_game_id} "
                              "have UNKNOWN point-in-time eligibility (stitched product; provider "
                              "availability before a cutoff is not proven)",
                              provider=fetched.provider, run_id=run.run_id,
                              raw_response_id=raw_id, entity_id=g.provider_game_id)
                    result.data_quality_issues += 1
        except Exception as exc:  # noqa: BLE001
            et, msg = sanitize_error(exc)
            result.record_active_failure(et, f"game {g.provider_game_id}: {msg}")

    result.status = "partially_failed" if result.has_active_failure else "succeeded"
    run_status = "partially_succeeded" if result.status == "partially_failed" else "succeeded"
    with transaction(conn):
        runs.complete(
            run.run_id, status=run_status, duration_ns=time.monotonic_ns() - started,
            requests_made=result.requests_made, records_received=result.games_considered,
            records_normalized=result.observations_normalized,
            records_inserted=result.records_inserted + result.records_changed,
            records_deduplicated=result.records_unchanged, records_rejected=result.records_rejected)
    return result
