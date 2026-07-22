"""MLB Statcast event-level models.

Statcast exposes *per-event* measurements -- one row per pitch and per batted
ball -- not raw optical frames. These are event-level and are safe to treat as
always available (subject to the licensing note below); the frame-level
counterpart is the optional :mod:`tracking.mlb_hawkeye_adapter`.

Every field is optional and copied verbatim from the provider. We never invent a
coordinate or a physics value that the source did not report (requirement 9);
e.g. a pitch with no reported release position yields a coordinate with those
axes left ``None``.

LICENSING / HISTORICAL DATA
---------------------------
Statcast event-level data is publicly accessible via MLB's Baseball Savant
(and community tools such as pybaseball) but remains subject to MLB's terms of
use; it is intended for personal/non-commercial analysis unless a separate
agreement is in place. Historical coverage varies by metric and season (pitch
tracking from ~2008 via PITCHf/x, full Statcast from 2015). Confirm entitlement
before any commercial use or redistribution.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict

from .base import Coordinates, EventCoordinate, Kinematics


def _f(value: Any) -> Optional[float]:
    """Coerce a provider value to float, preserving 'absent' as None."""

    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class StatcastPitch(BaseModel):
    """Event-level measurements for a single pitch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    game_id: str
    pitch_id: str
    pitcher: Optional[str] = None
    batter: Optional[str] = None
    inning: Optional[int] = None
    pitch_type: Optional[str] = None

    # Velocity / spin.
    release_speed: Optional[float] = None
    effective_speed: Optional[float] = None
    release_spin_rate: Optional[float] = None

    # Release position (ft).
    release_pos_x: Optional[float] = None
    release_pos_y: Optional[float] = None
    release_pos_z: Optional[float] = None

    # Plate location (ft) and movement (ft).
    plate_x: Optional[float] = None
    plate_z: Optional[float] = None
    pfx_x: Optional[float] = None
    pfx_z: Optional[float] = None

    # Release-instant velocity/acceleration components (event-level physics).
    vx0: Optional[float] = None
    vy0: Optional[float] = None
    vz0: Optional[float] = None
    ax: Optional[float] = None
    ay: Optional[float] = None
    az: Optional[float] = None

    source: str = "statcast"

    @classmethod
    def from_raw(cls, game_id: str, pitch_id: str, raw: Dict[str, Any]) -> "StatcastPitch":
        return cls(
            game_id=game_id,
            pitch_id=pitch_id,
            pitcher=raw.get("pitcher"),
            batter=raw.get("batter"),
            inning=raw.get("inning"),
            pitch_type=raw.get("pitch_type"),
            release_speed=_f(raw.get("release_speed")),
            effective_speed=_f(raw.get("effective_speed")),
            release_spin_rate=_f(raw.get("release_spin_rate")),
            release_pos_x=_f(raw.get("release_pos_x")),
            release_pos_y=_f(raw.get("release_pos_y")),
            release_pos_z=_f(raw.get("release_pos_z")),
            plate_x=_f(raw.get("plate_x")),
            plate_z=_f(raw.get("plate_z")),
            pfx_x=_f(raw.get("pfx_x")),
            pfx_z=_f(raw.get("pfx_z")),
            vx0=_f(raw.get("vx0")),
            vy0=_f(raw.get("vy0")),
            vz0=_f(raw.get("vz0")),
            ax=_f(raw.get("ax")),
            ay=_f(raw.get("ay")),
            az=_f(raw.get("az")),
        )

    def release_coordinate(self) -> EventCoordinate:
        """Event-level coordinate at the release point (NOT a tracking frame)."""

        return EventCoordinate(
            sport="mlb",
            event_id=f"{self.game_id}:{self.pitch_id}:release",
            event_type="pitch_release",
            coordinates=Coordinates(
                x=self.release_pos_x, y=self.release_pos_y, z=self.release_pos_z
            ),
            source=self.source,
            kinematics=Kinematics(
                velocity=self.release_speed,
                vx=self.vx0,
                vy=self.vy0,
                vz=self.vz0,
                ax=self.ax,
                ay=self.ay,
                az=self.az,
            ),
        )


class StatcastBattedBall(BaseModel):
    """Event-level measurements for a single batted ball."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    game_id: str
    event_id: str
    batter: Optional[str] = None
    inning: Optional[int] = None

    launch_speed: Optional[float] = None
    launch_angle: Optional[float] = None
    hit_distance: Optional[float] = None
    bb_type: Optional[str] = None  # e.g. "line_drive", "fly_ball"

    # Batted-ball location as reported (Savant hc_x / hc_y coordinate space).
    hc_x: Optional[float] = None
    hc_y: Optional[float] = None

    estimated_ba: Optional[float] = None
    estimated_woba: Optional[float] = None

    source: str = "statcast"

    @classmethod
    def from_raw(cls, game_id: str, event_id: str, raw: Dict[str, Any]) -> "StatcastBattedBall":
        return cls(
            game_id=game_id,
            event_id=event_id,
            batter=raw.get("batter"),
            inning=raw.get("inning"),
            launch_speed=_f(raw.get("launch_speed")),
            launch_angle=_f(raw.get("launch_angle")),
            hit_distance=_f(raw.get("hit_distance_sc")),
            bb_type=raw.get("bb_type"),
            hc_x=_f(raw.get("hc_x")),
            hc_y=_f(raw.get("hc_y")),
            estimated_ba=_f(raw.get("estimated_ba_using_speedangle")),
            estimated_woba=_f(raw.get("estimated_woba_using_speedangle")),
        )

    def landing_coordinate(self) -> EventCoordinate:
        """Event-level landing coordinate (2-D as reported; no z invented)."""

        return EventCoordinate(
            sport="mlb",
            event_id=f"{self.game_id}:{self.event_id}:landing",
            event_type="batted_ball",
            coordinates=Coordinates(x=self.hc_x, y=self.hc_y),
            source=self.source,
        )


class MLBStatcastAdapter:
    """Builds event-level Statcast models from raw provider rows.

    This is NOT a :class:`~tracking.base.FrameSource`; it produces event-level
    data only. It is marked event-level explicitly so callers cannot mistake it
    for a frame provider.
    """

    provider_name = "mlb_statcast"
    is_event_level = True

    def pitch(self, game_id: str, pitch_id: str, raw: Dict[str, Any]) -> StatcastPitch:
        return StatcastPitch.from_raw(game_id, pitch_id, raw)

    def batted_ball(self, game_id: str, event_id: str, raw: Dict[str, Any]) -> StatcastBattedBall:
        return StatcastBattedBall.from_raw(game_id, event_id, raw)
