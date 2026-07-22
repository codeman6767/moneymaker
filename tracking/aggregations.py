"""Feature aggregators over event-level and frame-level tracking data.

Two clearly separated groups:

* **Event-level features** (shot distance, shot zone, MLB pitch and batted-ball
  measures) depend only on event coordinates and therefore work whether or not
  optical tracking exists. This is what keeps the whole system functional when
  frame-level tracking is unavailable (requirement 11).

* **Frame-level features** (lineup spacing, defender distance, movement speed)
  require genuine player tracking. Each one calls
  :func:`~tracking.base.assert_frame_level` first, so passing event-level data
  raises :class:`~tracking.base.InvalidCoordinateAccess` instead of silently
  producing a meaningless number.

All feature functions return ``None`` when the *specific* inputs they need are
absent (e.g. a shot with no location) -- they never fabricate values.
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import List, Optional, Union

from .base import (
    EventCoordinate,
    PlayerFrameSample,
    TrackingFrame,
    assert_frame_level,
)
from .mlb_statcast_adapter import StatcastBattedBall, StatcastPitch
from .nba_event_coordinates import NBACourt, NBAShotEvent

# --------------------------------------------------------------------------- #
# Small geometry helpers
# --------------------------------------------------------------------------- #
def _distance_2d(x1, y1, x2, y2) -> Optional[float]:
    if None in (x1, y1, x2, y2):
        return None
    return math.hypot(x1 - x2, y1 - y2)


def _xy(obj) -> tuple[Optional[float], Optional[float]]:
    coord = obj.coordinates if hasattr(obj, "coordinates") else obj
    return coord.x, coord.y


# --------------------------------------------------------------------------- #
# NBA event-level features (always available)
# --------------------------------------------------------------------------- #
ShotLike = Union[NBAShotEvent, EventCoordinate]


def shot_distance(shot: ShotLike) -> Optional[float]:
    """Straight-line distance (ft) from the shot location to the rim.

    Returns ``None`` if the shot has no reported location.
    """

    x, y = _xy(shot)
    return _distance_2d(x, y, NBACourt.RIM_X, NBACourt.RIM_Y)


def shot_zone(shot: ShotLike) -> Optional[str]:
    """Classify a shot into a court zone from its event coordinate.

    Zones: ``restricted_area``, ``paint``, ``mid_range``, ``corner_three``,
    ``above_the_break_three``. Returns ``None`` with no location.
    """

    x, y = _xy(shot)
    if x is None or y is None:
        return None
    dist = math.hypot(x, y)

    # Three-point classification first (corner vs above-the-break).
    is_corner = abs(x) >= NBACourt.CORNER_SIDE_X and 0 <= y <= 14.0
    if is_corner and dist >= NBACourt.THREE_POINT_CORNER_DISTANCE:
        return "corner_three"
    if dist >= NBACourt.THREE_POINT_ARC_RADIUS:
        return "above_the_break_three"

    if dist <= NBACourt.RESTRICTED_AREA_RADIUS:
        return "restricted_area"
    if abs(x) <= NBACourt.PAINT_HALF_WIDTH and y <= NBACourt.FREE_THROW_DISTANCE + 4.0:
        return "paint"
    return "mid_range"


# --------------------------------------------------------------------------- #
# MLB event-level features (always available)
# --------------------------------------------------------------------------- #
def pitch_measures(pitch: StatcastPitch) -> dict:
    """Event-level pitch features. Only fields the provider gave are computed."""

    movement = None
    if pitch.pfx_x is not None and pitch.pfx_z is not None:
        movement = math.hypot(pitch.pfx_x, pitch.pfx_z)

    return {
        "release_speed": pitch.release_speed,
        "effective_speed": pitch.effective_speed,
        "spin_rate": pitch.release_spin_rate,
        "horizontal_break": pitch.pfx_x,
        "vertical_break": pitch.pfx_z,
        "total_movement": movement,
        "plate_x": pitch.plate_x,
        "plate_z": pitch.plate_z,
    }


def batted_ball_measures(bb: StatcastBattedBall) -> dict:
    """Event-level batted-ball features."""

    hard_hit = bb.launch_speed >= 95.0 if bb.launch_speed is not None else None
    # "Barrel"-ish flag only when both inputs exist; otherwise unknown (None).
    barrel = None
    if bb.launch_speed is not None and bb.launch_angle is not None:
        barrel = bb.launch_speed >= 98.0 and 26.0 <= bb.launch_angle <= 30.0

    return {
        "launch_speed": bb.launch_speed,
        "launch_angle": bb.launch_angle,
        "hit_distance": bb.hit_distance,
        "bb_type": bb.bb_type,
        "hard_hit": hard_hit,
        "barrel": barrel,
        "estimated_woba": bb.estimated_woba,
    }


# --------------------------------------------------------------------------- #
# Frame-level features (require genuine tracking; reject event data)
# --------------------------------------------------------------------------- #
def movement_speed(sample: PlayerFrameSample) -> Optional[float]:
    """Instantaneous speed of a tracked player.

    Uses provider-reported velocity when present, else the magnitude of the
    velocity vector. Requires frame-level data -- an event coordinate raises.
    """

    assert_frame_level(sample)
    k = sample.kinematics
    if k.velocity is not None:
        return k.velocity
    if k.vx is not None and k.vy is not None:
        if k.vz is not None:
            return math.sqrt(k.vx**2 + k.vy**2 + k.vz**2)
        return math.hypot(k.vx, k.vy)
    return None


def movement_speed_between(
    frame_a: TrackingFrame, frame_b: TrackingFrame, player_id: str
) -> Optional[float]:
    """Speed (distance/time) for a player between two consecutive frames.

    Derived purely from real tracked positions and frame timestamps -- valid
    even when the provider does not report a velocity field.
    """

    assert_frame_level(frame_a)
    assert_frame_level(frame_b)
    a = frame_a.player(player_id)
    b = frame_b.player(player_id)
    if a is None or b is None:
        return None
    dist = _distance_2d(a.coordinates.x, a.coordinates.y, b.coordinates.x, b.coordinates.y)
    if dist is None:
        return None
    dt = (frame_b.frame_time - frame_a.frame_time).total_seconds()
    if dt <= 0:
        return None
    return dist / dt


def lineup_spacing(frame: TrackingFrame, team: str) -> Optional[float]:
    """Mean pairwise distance (ft) among a team's tracked players.

    A simple spacing proxy. Requires frame-level data. Returns ``None`` if fewer
    than two of the team's players have positions.
    """

    assert_frame_level(frame)
    points = [
        (s.coordinates.x, s.coordinates.y)
        for s in frame.team_samples(team)
        if s.coordinates.x is not None and s.coordinates.y is not None
    ]
    if len(points) < 2:
        return None
    dists = [_distance_2d(a[0], a[1], b[0], b[1]) for a, b in combinations(points, 2)]
    dists = [d for d in dists if d is not None]
    return sum(dists) / len(dists) if dists else None


def defender_distance(
    frame: TrackingFrame, ball_handler_id: str, defending_team: str
) -> Optional[float]:
    """Distance (ft) from the ball handler to the nearest defender.

    Requires frame-level data. Returns ``None`` if positions are missing.
    """

    assert_frame_level(frame)
    handler = frame.player(ball_handler_id)
    if handler is None or handler.coordinates.x is None or handler.coordinates.y is None:
        return None
    defenders: List[float] = []
    for d in frame.team_samples(defending_team):
        dist = _distance_2d(
            handler.coordinates.x, handler.coordinates.y, d.coordinates.x, d.coordinates.y
        )
        if dist is not None:
            defenders.append(dist)
    return min(defenders) if defenders else None
