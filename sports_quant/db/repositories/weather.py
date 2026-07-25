"""Weather snapshot repository (Phase D4, append-only, transition-aware).

Anchored on a ``provider_game_references`` row (official game identity) and the
existing canonical ``venues`` row -- no second venue/game system. Follows the
shared transition-aware discipline (see :mod:`.observations`): a new row is
written only when its ``content_hash`` differs from the immediate temporal
predecessor for the same anchor ``(game_ref_id, weather_kind, valid_time,
forecast_mode)``. Ordinary forecast evolution and station-observation corrections
both APPEND (they are never overwrites and are never invented "provider
corrections"). ``observed_at`` is the point-in-time cutoff and is never backdated
to a historical model-run time.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from ..ids import new_weather_snapshot_id
from ..schema import WEATHER_APPLICABILITIES, WEATHER_KINDS, utc_now_iso
from .base import Repository, RepositoryError
from .observations import ObservationOutcome, append_transition, observation_content_hash


class WeatherOutcome(str, enum.Enum):
    """Result of appending one weather observation.

    * ``INSERTED``  -- a first observation for this anchor was written.
    * ``CHANGED``   -- a superseding observation (a genuine predecessor existed).
    * ``UNCHANGED`` -- identical to the immediate predecessor / an exact replay.
    * ``REJECTED``  -- refused by validation (never a silent drop).
    """

    INSERTED = "inserted"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"


@dataclass(frozen=True)
class WeatherValues:
    """The normalized (canonical-unit) weather fields for one observation.

    Every field is optional: missing stays ``None`` (never coerced to zero); an
    explicit provider ``0`` is preserved as ``0``.
    """

    temperature_c: Optional[float] = None
    apparent_temperature_c: Optional[float] = None
    dew_point_c: Optional[float] = None
    relative_humidity_pct: Optional[float] = None
    wind_speed_ms: Optional[float] = None
    wind_gust_ms: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    precip_probability_pct: Optional[float] = None
    precip_amount_mm: Optional[float] = None
    weather_code: Optional[str] = None
    condition_text: Optional[str] = None
    extra: Optional[str] = None


class WeatherRepositoryProtocol(Protocol):
    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        venue_id: str,
        weather_kind: str,
        applicability: str,
        forecast_mode: str,
        valid_time: Optional[str],
        observed_at: str,
        retrieved_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        values: WeatherValues,
        **fields: object,
    ) -> tuple[Optional[str], WeatherOutcome]: ...


class SqliteWeatherRepository(Repository):
    """Append-only weather observation storage."""

    def append(
        self,
        *,
        game_ref_id: str,
        provider: str,
        provider_game_id: str,
        venue_id: str,
        weather_kind: str,
        applicability: str,
        forecast_mode: str,
        valid_time: Optional[str],
        observed_at: str,
        retrieved_at: str,
        ingested_at: str,
        run_id: Optional[str],
        raw_response_id: str,
        raw_response_hash: str,
        values: WeatherValues,
        roof_type_at_decision: Optional[str] = None,
        requested_latitude: Optional[float] = None,
        requested_longitude: Optional[float] = None,
        source_station: Optional[str] = None,
        weather_model: Optional[str] = None,
        forecast_target_time: Optional[str] = None,
        model_reference_time: Optional[str] = None,
        provider_available_at: Optional[str] = None,
        lead_time_seconds: Optional[int] = None,
        pit_eligible: Optional[bool] = None,
        provider_timestamp: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> tuple[Optional[str], WeatherOutcome]:
        if weather_kind not in WEATHER_KINDS:
            raise RepositoryError(
                f"invalid weather_kind {weather_kind!r}; expected one of {list(WEATHER_KINDS)}"
            )
        if applicability not in WEATHER_APPLICABILITIES:
            raise RepositoryError(
                f"invalid applicability {applicability!r}; "
                f"expected one of {list(WEATHER_APPLICABILITIES)}"
            )
        if not forecast_mode.strip():
            raise RepositoryError("forecast_mode must be non-blank")

        content = {
            "weather_kind": weather_kind,
            "applicability": applicability,
            "forecast_mode": forecast_mode,
            "valid_time": valid_time,
            "forecast_target_time": forecast_target_time,
            "model_reference_time": model_reference_time,
            "provider_available_at": provider_available_at,
            "lead_time_seconds": lead_time_seconds,
            "pit_eligible": pit_eligible,
            "roof_type_at_decision": roof_type_at_decision,
            "source_station": source_station,
            "weather_model": weather_model,
            "requested_latitude": requested_latitude,
            "requested_longitude": requested_longitude,
            "temperature_c": values.temperature_c,
            "apparent_temperature_c": values.apparent_temperature_c,
            "dew_point_c": values.dew_point_c,
            "relative_humidity_pct": values.relative_humidity_pct,
            "wind_speed_ms": values.wind_speed_ms,
            "wind_gust_ms": values.wind_gust_ms,
            "wind_direction_deg": values.wind_direction_deg,
            "precip_probability_pct": values.precip_probability_pct,
            "precip_amount_mm": values.precip_amount_mm,
            "weather_code": values.weather_code,
            "condition_text": values.condition_text,
            "extra": values.extra,
        }
        content_hash = observation_content_hash(content)
        new_id = new_weather_snapshot_id()
        now = utc_now_iso()
        pit_db = None if pit_eligible is None else (1 if pit_eligible else 0)
        columns = (
            "weather_id", "game_ref_id", "provider", "provider_game_id", "venue_id",
            "weather_kind", "applicability", "forecast_mode", "roof_type_at_decision",
            "requested_latitude", "requested_longitude", "source_station", "weather_model",
            "valid_time", "forecast_target_time", "model_reference_time", "provider_available_at",
            "lead_time_seconds", "pit_eligible", "temperature_c", "apparent_temperature_c",
            "dew_point_c", "relative_humidity_pct", "wind_speed_ms", "wind_gust_ms",
            "wind_direction_deg", "precip_probability_pct", "precip_amount_mm", "weather_code",
            "condition_text", "extra", "provider_timestamp", "published_at", "observed_at",
            "retrieved_at", "ingested_at", "run_id", "raw_response_id", "raw_response_hash",
            "content_hash", "created_at",
        )
        values_row: tuple[Any, ...] = (
            new_id, game_ref_id, provider, provider_game_id, venue_id, weather_kind,
            applicability, forecast_mode, roof_type_at_decision, requested_latitude,
            requested_longitude, source_station, weather_model, valid_time,
            forecast_target_time, model_reference_time, provider_available_at,
            lead_time_seconds, pit_db, values.temperature_c, values.apparent_temperature_c,
            values.dew_point_c, values.relative_humidity_pct, values.wind_speed_ms,
            values.wind_gust_ms, values.wind_direction_deg, values.precip_probability_pct,
            values.precip_amount_mm, values.weather_code, values.condition_text, values.extra,
            provider_timestamp, published_at, observed_at, retrieved_at, ingested_at, run_id,
            raw_response_id, raw_response_hash, content_hash, now,
        )

        # A genuine predecessor (any earlier row for this anchor) means an INSERTED
        # here is a superseding CHANGED, not a first observation.
        predecessor = self._fetch_one(
            "SELECT weather_id FROM weather_snapshots "
            "WHERE game_ref_id = ? AND weather_kind = ? AND forecast_mode = ? "
            "AND ((valid_time IS NULL AND ? IS NULL) OR valid_time = ?) "
            "AND observed_at <= ? "
            "ORDER BY observed_at DESC, weather_id DESC LIMIT 1",
            (game_ref_id, weather_kind, forecast_mode, valid_time, valid_time, observed_at),
        )
        outcome = append_transition(
            self._conn, table="weather_snapshots", id_column="weather_id",
            anchor_where=(
                "game_ref_id = ? AND weather_kind = ? AND forecast_mode = ? "
                "AND ((valid_time IS NULL AND ? IS NULL) OR valid_time = ?)"
            ),
            anchor_params=(game_ref_id, weather_kind, forecast_mode, valid_time, valid_time),
            observed_at=observed_at, content_hash=content_hash, columns=columns, values=values_row,
        )
        if outcome is ObservationOutcome.UNCHANGED:
            return None, WeatherOutcome.UNCHANGED
        if predecessor is not None:
            return new_id, WeatherOutcome.CHANGED
        return new_id, WeatherOutcome.INSERTED

    def latest(
        self, game_ref_id: str, weather_kind: str, forecast_mode: str, valid_time: Optional[str]
    ) -> Optional[dict[str, Any]]:
        """The current-state (newest observed_at) row for one anchor, or None."""

        row = self._fetch_one(
            "SELECT * FROM weather_snapshots "
            "WHERE game_ref_id = ? AND weather_kind = ? AND forecast_mode = ? "
            "AND ((valid_time IS NULL AND ? IS NULL) OR valid_time = ?) "
            "ORDER BY observed_at DESC, weather_id DESC LIMIT 1",
            (game_ref_id, weather_kind, forecast_mode, valid_time, valid_time),
        )
        return dict(row) if row is not None else None

    def as_of(
        self, game_ref_id: str, weather_kind: str, forecast_mode: str,
        valid_time: Optional[str], cutoff: str,
    ) -> Optional[dict[str, Any]]:
        """The observation for one anchor as it stood at ``cutoff`` (PIT read)."""

        row = self._fetch_one(
            "SELECT * FROM weather_snapshots "
            "WHERE game_ref_id = ? AND weather_kind = ? AND forecast_mode = ? "
            "AND ((valid_time IS NULL AND ? IS NULL) OR valid_time = ?) "
            "AND observed_at <= ? "
            "ORDER BY observed_at DESC, weather_id DESC LIMIT 1",
            (game_ref_id, weather_kind, forecast_mode, valid_time, valid_time, cutoff),
        )
        return dict(row) if row is not None else None

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM weather_snapshots")
