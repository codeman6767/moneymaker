"""Optional tracking and positional-data architecture (Module 3).

The organizing principle: **event-level coordinates are always available;
frame-level optical tracking is an optional, licensed add-on.** The two are
distinct, non-interchangeable types, and the whole system must function on
event-level data alone.

* Event-level: :class:`EventCoordinate`, NBA shot/play coordinates, on-court
  lineup indicators, MLB Statcast pitch/batted-ball models.
* Frame-level: :class:`TrackingFrame` / :class:`PlayerFrameSample`, served only
  by an optional :class:`FrameSource` adapter, stored in partitioned Parquet
  with manifests in PostgreSQL.

See ``tracking/README.md`` for licensing and historical-data requirements, and
``CLAUDE.md`` for the rule these modules uphold.
"""

from .aggregations import (
    batted_ball_measures,
    defender_distance,
    lineup_spacing,
    movement_speed,
    movement_speed_between,
    pitch_measures,
    shot_distance,
    shot_zone,
)
from .base import (
    FRAME_COLUMNS,
    PARTITION_COLUMNS,
    Coordinates,
    EventCoordinate,
    FrameDataUnavailable,
    FrameManifest,
    FrameParquetStore,
    FrameSource,
    InMemoryManifestRepository,
    InvalidCoordinateAccess,
    Kinematics,
    ManifestRepository,
    PlayerFrameSample,
    PostgresManifestRepository,
    TrackingError,
    TrackingLevel,
    TrackingNotConfigured,
    TrackingFrame,
    assert_frame_level,
    is_frame_level,
)
from .mlb_hawkeye_adapter import MLBHawkEyeAdapter
from .mlb_statcast_adapter import MLBStatcastAdapter, StatcastBattedBall, StatcastPitch
from .nba_event_coordinates import (
    NBACourt,
    NBAPlayEvent,
    NBAShotEvent,
    OnCourtLineup,
)
from .nba_optical_adapter import NBAOpticalAdapter

__all__ = [
    # base models
    "TrackingLevel",
    "Coordinates",
    "Kinematics",
    "EventCoordinate",
    "PlayerFrameSample",
    "TrackingFrame",
    "is_frame_level",
    "assert_frame_level",
    # errors
    "TrackingError",
    "FrameDataUnavailable",
    "TrackingNotConfigured",
    "InvalidCoordinateAccess",
    # sources + storage
    "FrameSource",
    "FrameParquetStore",
    "FRAME_COLUMNS",
    "PARTITION_COLUMNS",
    "FrameManifest",
    "ManifestRepository",
    "InMemoryManifestRepository",
    "PostgresManifestRepository",
    # nba event
    "NBACourt",
    "NBAShotEvent",
    "NBAPlayEvent",
    "OnCourtLineup",
    # nba optical (optional frame)
    "NBAOpticalAdapter",
    # mlb statcast (event)
    "MLBStatcastAdapter",
    "StatcastPitch",
    "StatcastBattedBall",
    # mlb hawkeye (optional frame)
    "MLBHawkEyeAdapter",
    # aggregations
    "shot_distance",
    "shot_zone",
    "pitch_measures",
    "batted_ball_measures",
    "movement_speed",
    "movement_speed_between",
    "lineup_spacing",
    "defender_distance",
]
