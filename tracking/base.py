"""Tracking data architecture: the hard line between event and frame data.

This module encodes one non-negotiable distinction (see ``CLAUDE.md``: "Do not
assume access to frame-level optical tracking. Frame-level tracking providers
must remain optional adapters."):

* **Event-level data** -- a single coordinate attached to a discrete game event
  (a shot location, a batted-ball landing spot). Always available from ordinary
  play-by-play feeds. Represented by :class:`EventCoordinate`.

* **Frame-level tracking** -- per-player positions (and optionally velocity /
  acceleration / confidence) sampled many times per second by an optical
  system. Proprietary, licensed, and frequently *unavailable*. Represented by
  :class:`TrackingFrame` / :class:`PlayerFrameSample`, and only ever produced by
  a configured :class:`FrameSource` adapter.

The two are deliberately *incompatible types*. There is no method that turns an
event coordinate into a tracking frame, ``level`` is a read-only property fixed
per class (never a settable field), and :func:`assert_frame_level` rejects
anything that is not genuinely frame-level. Requirement: event-only data must
never be silently treated as player tracking.

Coordinates and kinematics are all optional and are populated *only* when a
provider supplies them -- we never invent coordinates that a source did not
give us (requirement 9).

I/O policy: frame-level data is bulk/offline research data. It is stored in
partitioned Parquet, with metadata/manifests in PostgreSQL. None of this is on
the hot decision path, and none of it is imported at module load time.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict


class TrackingLevel(str, enum.Enum):
    EVENT = "event"
    FRAME = "frame"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class TrackingError(Exception):
    """Base class for tracking errors."""


class FrameDataUnavailable(TrackingError):
    """Raised when frame-level data is requested but no source can supply it.

    Catch this to degrade gracefully: the system must keep functioning on
    event-level data alone (requirement 11).
    """


class TrackingNotConfigured(TrackingError):
    """Raised when an optional adapter is used without being configured."""


class InvalidCoordinateAccess(TrackingError, TypeError):
    """Raised when event-level data is used where frame-level data is required.

    Subclasses :class:`TypeError` because it signals a programming error --
    confusing a discrete event coordinate for player tracking.
    """


# --------------------------------------------------------------------------- #
# Coordinate + kinematics primitives (all fields optional)
# --------------------------------------------------------------------------- #
class Coordinates(BaseModel):
    """A spatial point. Every axis is optional and present only if sourced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None

    @property
    def has_z(self) -> bool:
        return self.z is not None

    @property
    def is_empty(self) -> bool:
        return self.x is None and self.y is None and self.z is None


class Kinematics(BaseModel):
    """Motion attributes. Populated only when a provider reports them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Scalar magnitudes.
    velocity: Optional[float] = None
    acceleration: Optional[float] = None
    # Vector components, when a provider gives them.
    vx: Optional[float] = None
    vy: Optional[float] = None
    vz: Optional[float] = None
    ax: Optional[float] = None
    ay: Optional[float] = None
    az: Optional[float] = None
    # Provider-reported tracking confidence in [0, 1].
    confidence: Optional[float] = None

    @property
    def has_velocity(self) -> bool:
        return self.velocity is not None or self.vx is not None


# --------------------------------------------------------------------------- #
# Event-level coordinate
# --------------------------------------------------------------------------- #
class EventCoordinate(BaseModel):
    """A single coordinate attached to a discrete game event.

    This is NOT player tracking. It has no per-player samples, no time series,
    and its :attr:`level` is permanently ``EVENT``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sport: str
    event_id: str
    event_type: str
    coordinates: Coordinates
    source: str
    # Optional kinematics only for event instants a provider measures (e.g. a
    # Statcast pitch release). Still event-level, NOT a tracking frame.
    kinematics: Optional[Kinematics] = None

    @property
    def level(self) -> TrackingLevel:
        # Read-only property, never a field: an EventCoordinate can never be
        # relabelled as frame-level.
        return TrackingLevel.EVENT


# --------------------------------------------------------------------------- #
# Frame-level tracking
# --------------------------------------------------------------------------- #
class PlayerFrameSample(BaseModel):
    """One player's position (and optional motion) within a tracking frame."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_id: str
    team: Optional[str] = None
    coordinates: Coordinates
    kinematics: Kinematics = Kinematics()

    @property
    def level(self) -> TrackingLevel:
        return TrackingLevel.FRAME


class TrackingFrame(BaseModel):
    """A single optical-tracking frame: all tracked entities at one instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sport: str
    game_id: str
    frame_time: datetime
    period: Optional[int] = None
    samples: tuple[PlayerFrameSample, ...] = ()
    source: str = "unknown"
    # Optional ball position, when the provider tracks it.
    ball: Optional[Coordinates] = None

    @property
    def level(self) -> TrackingLevel:
        return TrackingLevel.FRAME

    def player(self, player_id: str) -> Optional[PlayerFrameSample]:
        for s in self.samples:
            if s.player_id == player_id:
                return s
        return None

    def team_samples(self, team: str) -> List[PlayerFrameSample]:
        return [s for s in self.samples if s.team == team]


# --------------------------------------------------------------------------- #
# Type guards -- the safeguard against event/frame confusion
# --------------------------------------------------------------------------- #
def is_frame_level(obj: Any) -> bool:
    return isinstance(obj, (TrackingFrame, PlayerFrameSample)) and obj.level is TrackingLevel.FRAME


def assert_frame_level(obj: Any) -> None:
    """Reject anything that is not genuine frame-level tracking data.

    Every aggregator that consumes player tracking calls this first, so an
    :class:`EventCoordinate` (or any event-level value) passed by mistake fails
    loudly instead of being silently misinterpreted as player positions.
    """

    if not is_frame_level(obj):
        raise InvalidCoordinateAccess(
            f"expected frame-level tracking data (TrackingFrame/PlayerFrameSample), "
            f"got {type(obj).__name__}; event-level coordinates cannot be used as "
            f"player tracking"
        )


# --------------------------------------------------------------------------- #
# Optional frame-source adapters
# --------------------------------------------------------------------------- #
class FrameSource(abc.ABC):
    """Abstract, optional source of frame-level tracking.

    Concrete adapters (optical providers) are configured with real data
    locations/credentials. When unconfigured they report ``is_available()`` ==
    False and refuse to produce data -- they never synthesize frames.
    """

    #: Human-readable provider name.
    provider_name: str = "abstract"
    #: Licensing / historical-data notes for operators (see module docstrings).
    licensing: str = ""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether this adapter is configured AND has data to serve."""

    @abc.abstractmethod
    def iter_frames(
        self, game_id: str, *, period: Optional[int] = None
    ) -> Iterator[TrackingFrame]:
        """Yield frames for a game. Raises :class:`FrameDataUnavailable` if not
        available."""

    def require_available(self) -> None:
        if not self.is_available():
            raise FrameDataUnavailable(
                f"{self.provider_name}: frame-level tracking is not available "
                f"(adapter not configured or no data present)"
            )


# --------------------------------------------------------------------------- #
# Partitioned Parquet storage for frame-level data
# --------------------------------------------------------------------------- #
# Columns persisted per frame sample. Coordinate/motion columns are nullable and
# written only when the provider supplied them.
FRAME_COLUMNS = (
    "sport",
    "game_id",
    "period",
    "frame_time",
    "player_id",
    "team",
    "x",
    "y",
    "z",
    "velocity",
    "acceleration",
    "vx",
    "vy",
    "vz",
    "confidence",
    "source",
)

# Partition layout on disk: sport=<>/game_id=<>/date=<>/...
PARTITION_COLUMNS = ("sport", "game_id", "date")


def _frame_to_rows(frame: TrackingFrame) -> List[Dict[str, Any]]:
    date_str = frame.frame_time.date().isoformat()
    rows: List[Dict[str, Any]] = []
    for s in frame.samples:
        rows.append(
            {
                "sport": frame.sport,
                "game_id": frame.game_id,
                "date": date_str,
                "period": frame.period,
                "frame_time": frame.frame_time,
                "player_id": s.player_id,
                "team": s.team,
                "x": s.coordinates.x,
                "y": s.coordinates.y,
                "z": s.coordinates.z,
                "velocity": s.kinematics.velocity,
                "acceleration": s.kinematics.acceleration,
                "vx": s.kinematics.vx,
                "vy": s.kinematics.vy,
                "vz": s.kinematics.vz,
                "confidence": s.kinematics.confidence,
                "source": frame.source,
            }
        )
    return rows


#: Actionable message for the optional frame-storage dependency. A bare
#: ModuleNotFoundError names the missing module but not the extra that supplies
#: it, which leaves the reader to guess.
PYARROW_REQUIRED_MESSAGE = (
    "PyArrow is required for frame-level Parquet storage. "
    "Install sports-quant[tracking]."
)


class MissingTrackingDependencyError(ImportError):
    """Raised when frame-level Parquet storage is used without pyarrow.

    Subclasses :class:`ImportError` so existing ``except ImportError`` handlers
    keep working, while the message names the extra to install.
    """


def pyarrow_available() -> bool:
    """Whether pyarrow can be imported.

    Lets tests skip frame-storage cases cleanly rather than failing in an
    environment that deliberately omits the optional dependency -- the same
    pattern ``probability.onnx_export.onnx_available`` already uses for ONNX.
    """

    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


def _require_pyarrow() -> Any:
    """Import pyarrow, or explain which extra provides it."""

    try:
        import pyarrow as pa
    except ImportError as exc:
        raise MissingTrackingDependencyError(PYARROW_REQUIRED_MESSAGE) from exc
    return pa


def _require_pyarrow_dataset() -> Any:
    """Import pyarrow.dataset, or explain which extra provides it."""

    try:
        import pyarrow.dataset as ds
    except ImportError as exc:
        raise MissingTrackingDependencyError(PYARROW_REQUIRED_MESSAGE) from exc
    return ds


class FrameParquetStore:
    """Writes/reads frame-level data as partitioned Parquet.

    pyarrow is imported lazily so importing this module never requires it; only
    actually writing/reading frames does. It is an optional dependency
    (``sports-quant[tracking]``) because ``CLAUDE.md`` keeps frame-level
    tracking an optional adapter -- the read-only recommendation application
    never reads a frame. Using this class without it raises
    :class:`MissingTrackingDependencyError`, which names the extra rather than
    leaving a bare ``ModuleNotFoundError`` for the reader to decode.
    """

    def __init__(self, root_path: str) -> None:
        self.root_path = root_path

    @staticmethod
    def _arrow_schema() -> Any:
        pa = _require_pyarrow()

        # Explicit schema so optional coordinate/motion columns that happen to
        # be entirely null in a batch still get a concrete type (not Arrow's
        # ``null`` type, which cannot round-trip through Parquet cleanly).
        return pa.schema(
            [
                ("sport", pa.string()),
                ("game_id", pa.string()),
                ("date", pa.string()),
                ("period", pa.int32()),
                ("frame_time", pa.timestamp("us", tz="UTC")),
                ("player_id", pa.string()),
                ("team", pa.string()),
                ("x", pa.float64()),
                ("y", pa.float64()),
                ("z", pa.float64()),
                ("velocity", pa.float64()),
                ("acceleration", pa.float64()),
                ("vx", pa.float64()),
                ("vy", pa.float64()),
                ("vz", pa.float64()),
                ("confidence", pa.float64()),
                ("source", pa.string()),
            ]
        )

    def write_frames(self, frames: Sequence[TrackingFrame]) -> int:
        pa = _require_pyarrow()
        ds = _require_pyarrow_dataset()

        rows: List[Dict[str, Any]] = []
        for f in frames:
            rows.extend(_frame_to_rows(f))
        if not rows:
            return 0
        table = pa.Table.from_pylist(rows, schema=self._arrow_schema())
        ds.write_dataset(
            table,
            base_dir=self.root_path,
            format="parquet",
            partitioning=list(PARTITION_COLUMNS),
            partitioning_flavor="hive",
            existing_data_behavior="overwrite_or_ignore",
        )
        return len(rows)

    def read_frames(self, game_id: str, sport: Optional[str] = None) -> List[TrackingFrame]:
        ds = _require_pyarrow_dataset()

        dataset = ds.dataset(self.root_path, format="parquet", partitioning="hive")
        filt = ds.field("game_id") == game_id
        if sport is not None:
            filt = filt & (ds.field("sport") == sport)
        table = dataset.to_table(filter=filt)
        return _rows_to_frames(table.to_pylist())


def _rows_to_frames(rows: List[Dict[str, Any]]) -> List[TrackingFrame]:
    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r["sport"], r["game_id"], r["frame_time"], r.get("period"))
        grouped.setdefault(key, []).append(r)

    frames: List[TrackingFrame] = []
    for (sport, game_id, frame_time, period), group in grouped.items():
        samples = [
            PlayerFrameSample(
                player_id=r["player_id"],
                team=r.get("team"),
                coordinates=Coordinates(x=r.get("x"), y=r.get("y"), z=r.get("z")),
                kinematics=Kinematics(
                    velocity=r.get("velocity"),
                    acceleration=r.get("acceleration"),
                    vx=r.get("vx"),
                    vy=r.get("vy"),
                    vz=r.get("vz"),
                    confidence=r.get("confidence"),
                ),
            )
            for r in group
        ]
        frames.append(
            TrackingFrame(
                sport=sport,
                game_id=game_id,
                frame_time=frame_time,
                period=period,
                samples=tuple(samples),
                source=group[0].get("source", "unknown"),
            )
        )
    frames.sort(key=lambda f: f.frame_time)
    return frames


# --------------------------------------------------------------------------- #
# Manifest metadata in PostgreSQL
# --------------------------------------------------------------------------- #
@dataclass
class FrameManifest:
    """Metadata describing one stored batch/partition of frame data."""

    manifest_id: str
    sport: str
    game_id: str
    provider: str
    path: str
    row_count: int
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    licensing: str
    created_at: Optional[datetime] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class ManifestRepository(Protocol):
    def upsert(self, manifest: FrameManifest) -> None: ...

    def get(self, manifest_id: str) -> Optional[FrameManifest]: ...

    def list_for_game(self, game_id: str) -> List[FrameManifest]: ...


class InMemoryManifestRepository:
    """Non-durable manifest store for tests and local runs."""

    def __init__(self) -> None:
        self._items: Dict[str, FrameManifest] = {}

    def upsert(self, manifest: FrameManifest) -> None:
        self._items[manifest.manifest_id] = manifest

    def get(self, manifest_id: str) -> Optional[FrameManifest]:
        return self._items.get(manifest_id)

    def list_for_game(self, game_id: str) -> List[FrameManifest]:
        return [m for m in self._items.values() if m.game_id == game_id]


class PostgresManifestRepository:
    """Durable manifest store in PostgreSQL.

    This is metadata only and lives off the hot path (frame data is bulk
    research data). ``psycopg`` is imported lazily so the module imports without
    a database present.
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS tracking_manifests (
        manifest_id TEXT PRIMARY KEY,
        sport       TEXT NOT NULL,
        game_id     TEXT NOT NULL,
        provider    TEXT NOT NULL,
        path        TEXT NOT NULL,
        row_count   BIGINT NOT NULL,
        start_time  TIMESTAMPTZ,
        end_time    TIMESTAMPTZ,
        licensing   TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        extra       JSONB NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX IF NOT EXISTS idx_tracking_manifests_game
        ON tracking_manifests (game_id);
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self):
        import psycopg  # type: ignore

        return psycopg.connect(self._dsn)

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(self.DDL)
            conn.commit()

    def upsert(self, manifest: FrameManifest) -> None:
        import json

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tracking_manifests
                    (manifest_id, sport, game_id, provider, path, row_count,
                     start_time, end_time, licensing, extra)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (manifest_id) DO UPDATE SET
                    row_count = EXCLUDED.row_count,
                    start_time = EXCLUDED.start_time,
                    end_time = EXCLUDED.end_time,
                    path = EXCLUDED.path,
                    extra = EXCLUDED.extra
                """,
                (
                    manifest.manifest_id,
                    manifest.sport,
                    manifest.game_id,
                    manifest.provider,
                    manifest.path,
                    manifest.row_count,
                    manifest.start_time,
                    manifest.end_time,
                    manifest.licensing,
                    json.dumps(manifest.extra),
                ),
            )
            conn.commit()

    def get(self, manifest_id: str) -> Optional[FrameManifest]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT manifest_id, sport, game_id, provider, path, row_count, "
                "start_time, end_time, licensing, created_at, extra "
                "FROM tracking_manifests WHERE manifest_id = %s",
                (manifest_id,),
            ).fetchone()
        return _row_to_manifest(row) if row else None

    def list_for_game(self, game_id: str) -> List[FrameManifest]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT manifest_id, sport, game_id, provider, path, row_count, "
                "start_time, end_time, licensing, created_at, extra "
                "FROM tracking_manifests WHERE game_id = %s ORDER BY created_at",
                (game_id,),
            ).fetchall()
        return [_row_to_manifest(r) for r in rows]


def _row_to_manifest(row) -> FrameManifest:
    return FrameManifest(
        manifest_id=row[0],
        sport=row[1],
        game_id=row[2],
        provider=row[3],
        path=row[4],
        row_count=row[5],
        start_time=row[6],
        end_time=row[7],
        licensing=row[8],
        created_at=row[9],
        extra=row[10] or {},
    )
