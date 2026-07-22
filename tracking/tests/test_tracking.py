"""Tests for the tracking / positional-data architecture (Module 3).

The headline requirement -- event-only data must never be usable as frame-level
player tracking -- is exercised first and hardest, in
``test_event_data_cannot_be_used_as_frame_level`` and the guard tests around it.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tracking import (
    PYARROW_REQUIRED_MESSAGE,
    Coordinates,
    EventCoordinate,
    FrameDataUnavailable,
    FrameManifest,
    FrameParquetStore,
    InMemoryManifestRepository,
    InvalidCoordinateAccess,
    Kinematics,
    MissingTrackingDependencyError,
    MLBHawkEyeAdapter,
    MLBStatcastAdapter,
    NBAOpticalAdapter,
    NBAShotEvent,
    OnCourtLineup,
    PlayerFrameSample,
    TrackingFrame,
    TrackingLevel,
    assert_frame_level,
    batted_ball_measures,
    defender_distance,
    is_frame_level,
    lineup_spacing,
    movement_speed,
    movement_speed_between,
    pitch_measures,
    pyarrow_available,
    shot_distance,
    shot_zone,
)

T0 = datetime(2026, 7, 1, 19, 0, 0, tzinfo=timezone.utc)


def make_frame(period=1, game_id="nba-1", **players) -> TrackingFrame:
    """players: player_id -> (team, x, y, [velocity])."""

    samples = []
    for pid, spec in players.items():
        team, x, y = spec[0], spec[1], spec[2]
        vel = spec[3] if len(spec) > 3 else None
        samples.append(
            PlayerFrameSample(
                player_id=pid,
                team=team,
                coordinates=Coordinates(x=x, y=y),
                kinematics=Kinematics(velocity=vel),
            )
        )
    return TrackingFrame(
        sport="nba", game_id=game_id, frame_time=T0, period=period,
        samples=tuple(samples), source="test_optical",
    )


# --------------------------------------------------------------------------- #
# THE CRUX: event-level data can never be treated as frame-level tracking
# --------------------------------------------------------------------------- #
def test_event_data_cannot_be_used_as_frame_level():
    shot = NBAShotEvent(game_id="nba-1", player_id="p1", x=5.0, y=10.0)
    event_coord = shot.to_event_coordinate()

    # It is unambiguously event-level.
    assert event_coord.level is TrackingLevel.EVENT
    assert is_frame_level(event_coord) is False

    # The guard rejects it outright...
    with pytest.raises(InvalidCoordinateAccess):
        assert_frame_level(event_coord)

    # ...and every frame-level aggregator rejects it too.
    for fn in (movement_speed, lambda o: lineup_spacing(o, "home")):
        with pytest.raises(InvalidCoordinateAccess):
            fn(event_coord)
    with pytest.raises(InvalidCoordinateAccess):
        defender_distance(event_coord, "p1", "away")

    # InvalidCoordinateAccess is a TypeError so it also trips type-based guards.
    assert issubclass(InvalidCoordinateAccess, TypeError)


def test_event_coordinate_has_no_frame_structure():
    coord = EventCoordinate(
        sport="nba", event_id="e1", event_type="shot",
        coordinates=Coordinates(x=1.0, y=2.0), source="pbp",
    )
    # No per-player tracking surface exists on an event coordinate.
    assert not hasattr(coord, "samples")
    assert not hasattr(coord, "player")
    # level is a read-only property; it cannot be set to FRAME, and passing it
    # as a field is rejected by the (extra=forbid) model.
    with pytest.raises(ValidationError):
        EventCoordinate(
            sport="nba", event_id="e1", event_type="shot",
            coordinates=Coordinates(x=1.0, y=2.0), source="pbp",
            level=TrackingLevel.FRAME,
        )


def test_lineup_indicator_is_not_positional():
    lineup = OnCourtLineup(
        game_id="nba-1", period=1,
        home_on_court=("h1", "h2", "h3", "h4", "h5"),
        away_on_court=("a1", "a2", "a3", "a4", "a5"),
    )
    assert lineup.is_positional is False
    assert lineup.is_on_court("h3")
    assert lineup.count("home") == 5
    # An on-court indicator is not frame data.
    with pytest.raises(InvalidCoordinateAccess):
        assert_frame_level(lineup)


# --------------------------------------------------------------------------- #
# Event-level features work with event-only data
# --------------------------------------------------------------------------- #
def test_shot_distance_and_zone_event_only():
    assert shot_distance(NBAShotEvent(game_id="g", player_id="p", x=0.0, y=0.0)) == 0.0
    assert shot_zone(NBAShotEvent(game_id="g", player_id="p", x=0.0, y=3.0)) == "restricted_area"
    assert shot_zone(NBAShotEvent(game_id="g", player_id="p", x=0.0, y=10.0)) == "paint"
    assert shot_zone(NBAShotEvent(game_id="g", player_id="p", x=10.0, y=15.0)) == "mid_range"
    assert shot_zone(NBAShotEvent(game_id="g", player_id="p", x=0.0, y=25.0)) == "above_the_break_three"
    assert shot_zone(NBAShotEvent(game_id="g", player_id="p", x=23.0, y=5.0)) == "corner_three"


def test_no_invented_coordinates():
    # A shot with no reported location must not gain one.
    shot = NBAShotEvent(game_id="g", player_id="p")
    assert shot.has_location is False
    coord = shot.to_event_coordinate()
    assert coord.coordinates.is_empty
    assert shot_distance(shot) is None
    assert shot_zone(shot) is None

    # A pitch with no release position -> empty coordinate, no fabricated z.
    adapter = MLBStatcastAdapter()
    pitch = adapter.pitch("mlb-1", "pit-1", {"release_speed": 95.2, "pitch_type": "FF"})
    rc = pitch.release_coordinate()
    assert rc.coordinates.x is None and rc.coordinates.z is None
    assert rc.kinematics.velocity == 95.2


def test_mlb_event_measures():
    adapter = MLBStatcastAdapter()
    pitch = adapter.pitch(
        "mlb-1", "pit-1",
        {"release_speed": 95.0, "release_spin_rate": 2400, "pfx_x": 0.9, "pfx_z": 1.2},
    )
    pm = pitch_measures(pitch)
    assert pm["release_speed"] == 95.0
    assert pm["spin_rate"] == 2400.0
    assert pm["total_movement"] == pytest.approx((0.9**2 + 1.2**2) ** 0.5)

    bb = adapter.batted_ball(
        "mlb-1", "bb-1",
        {"launch_speed": 99.0, "launch_angle": 28.0, "hit_distance_sc": 405, "bb_type": "fly_ball"},
    )
    bm = batted_ball_measures(bb)
    assert bm["hard_hit"] is True
    assert bm["barrel"] is True
    assert bm["hit_distance"] == 405.0

    # Missing inputs -> unknown, not fabricated.
    bb2 = adapter.batted_ball("mlb-1", "bb-2", {"bb_type": "ground_ball"})
    assert batted_ball_measures(bb2)["hard_hit"] is None
    assert batted_ball_measures(bb2)["barrel"] is None


# --------------------------------------------------------------------------- #
# System functions when frame-level tracking is unavailable
# --------------------------------------------------------------------------- #
def test_optical_adapters_unavailable_by_default():
    nba = NBAOpticalAdapter()  # no store configured
    mlb = MLBHawkEyeAdapter()
    assert nba.is_available() is False
    assert mlb.is_available() is False

    # Requesting frames fails loudly and catchably; the caller can degrade.
    for adapter in (nba, mlb):
        with pytest.raises(FrameDataUnavailable):
            list(adapter.iter_frames("game-x"))

    # Licensing is documented on the adapter for operators.
    assert "license" in nba.licensing.lower()
    assert "mlbam" in mlb.licensing.lower()


def test_graceful_degradation_pattern():
    """A consumer using try/except keeps working without frame data."""

    adapter = NBAOpticalAdapter()

    def spacing_or_none(game_id):
        try:
            frames = list(adapter.iter_frames(game_id))
        except FrameDataUnavailable:
            return None
        return lineup_spacing(frames[0], "home") if frames else None

    assert spacing_or_none("nba-1") is None  # no crash, no fabricated value


# --------------------------------------------------------------------------- #
# Frame-level features work with genuine tracking frames
# --------------------------------------------------------------------------- #
def test_frame_level_features():
    frame = make_frame(
        h1=("home", 0.0, 0.0, 5.5),
        h2=("home", 3.0, 4.0),
        h3=("home", 6.0, 8.0),
        a1=("away", 1.0, 1.0),
    )
    # movement_speed uses provider velocity when present.
    assert movement_speed(frame.player("h1")) == 5.5
    # lineup spacing among the 3 home players (mean pairwise distance).
    spacing = lineup_spacing(frame, "home")
    assert spacing is not None and spacing > 0
    # nearest defender (only a1) to h1 at (0,0): distance sqrt(2).
    assert defender_distance(frame, "h1", "away") == pytest.approx(2**0.5)


def test_movement_speed_between_frames():
    f1 = make_frame(game_id="nba-9", h1=("home", 0.0, 0.0))
    f2 = TrackingFrame(
        sport="nba", game_id="nba-9", frame_time=T0 + timedelta(seconds=2),
        period=1,
        samples=(PlayerFrameSample(player_id="h1", team="home", coordinates=Coordinates(x=0.0, y=10.0)),),
        source="test_optical",
    )
    # 10 ft over 2 s = 5 ft/s, derived from real positions (no velocity field).
    assert movement_speed_between(f1, f2, "h1") == pytest.approx(5.0)


def test_movement_speed_between_rejects_event_data():
    ec = EventCoordinate(sport="nba", event_id="e", event_type="shot",
                         coordinates=Coordinates(x=0.0, y=0.0), source="pbp")
    frame = make_frame(h1=("home", 0.0, 0.0))
    with pytest.raises(InvalidCoordinateAccess):
        movement_speed_between(ec, frame, "h1")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Partitioned Parquet storage round-trip + manifest metadata
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not pyarrow_available(), reason="pyarrow not installed (optional extra)")
def test_frame_parquet_roundtrip_and_manifest(tmp_path):
    store = FrameParquetStore(str(tmp_path / "frames"))
    frames = [
        TrackingFrame(
            sport="nba", game_id="nba-77", frame_time=T0, period=1,
            samples=(
                PlayerFrameSample(player_id="h1", team="home",
                                  coordinates=Coordinates(x=1.0, y=2.0, z=None),
                                  kinematics=Kinematics(velocity=4.0)),
                PlayerFrameSample(player_id="a1", team="away",
                                  coordinates=Coordinates(x=3.0, y=4.0)),
            ),
            source="test_optical",
        ),
        TrackingFrame(
            sport="nba", game_id="nba-77", frame_time=T0 + timedelta(seconds=1), period=1,
            samples=(
                PlayerFrameSample(player_id="h1", team="home",
                                  coordinates=Coordinates(x=1.5, y=2.5)),
            ),
            source="test_optical",
        ),
    ]
    rows = store.write_frames(frames)
    assert rows == 3

    read_back = store.read_frames("nba-77", sport="nba")
    assert len(read_back) == 2
    first = read_back[0]
    assert first.game_id == "nba-77"
    assert first.player("h1").coordinates.x == 1.0
    assert first.player("h1").kinematics.velocity == 4.0
    # Reading back yields real frame-level data.
    assert is_frame_level(first)

    # Manifest metadata (would live in PostgreSQL in production).
    repo = InMemoryManifestRepository()
    repo.upsert(
        FrameManifest(
            manifest_id="m1", sport="nba", game_id="nba-77", provider="nba_optical",
            path=str(tmp_path / "frames"), row_count=rows,
            start_time=T0, end_time=T0 + timedelta(seconds=1),
            licensing="licensed; not redistributable",
        )
    )
    assert repo.get("m1").row_count == 3
    assert [m.manifest_id for m in repo.list_for_game("nba-77")] == ["m1"]


@pytest.mark.skipif(not pyarrow_available(), reason="pyarrow not installed (optional extra)")
def test_configured_optical_adapter_reads_store(tmp_path):
    store = FrameParquetStore(str(tmp_path / "frames"))
    store.write_frames([make_frame(game_id="nba-5", h1=("home", 0.0, 0.0, 3.0))])
    adapter = NBAOpticalAdapter(store)  # now backed by a real store
    assert adapter.is_available() is True
    frames = list(adapter.iter_frames("nba-5"))
    assert len(frames) == 1
    assert movement_speed(frames[0].player("h1")) == 3.0


# --------------------------------------------------------------------------- #
# pyarrow is optional: importing must work without it, and using frame storage
# without it must explain which extra supplies it.
#
# `sys.modules[name] = None` makes a subsequent `import name` raise ImportError,
# so these run identically whether or not pyarrow is actually installed.
# --------------------------------------------------------------------------- #
@contextmanager
def pyarrow_hidden():
    """Make pyarrow un-importable for the duration of the block."""

    blocked = {name: None for name in ("pyarrow", "pyarrow.dataset")}
    saved = {name: sys.modules.get(name) for name in blocked}
    sys.modules.update(blocked)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_tracking_base_imports_without_pyarrow():
    """The module must import in an environment that omits the optional extra."""

    with pyarrow_hidden():
        for name in [m for m in sys.modules if m.startswith("tracking")]:
            sys.modules.pop(name, None)
        import tracking.base as reimported

        assert reimported.FrameParquetStore is not None
        assert reimported.pyarrow_available() is False


def test_constructing_the_store_without_pyarrow_is_allowed(tmp_path):
    """Construction is cheap and import-free; only I/O needs pyarrow."""

    with pyarrow_hidden():
        assert FrameParquetStore(str(tmp_path / "frames")) is not None


def test_write_frames_without_pyarrow_raises_a_clear_error(tmp_path):
    store = FrameParquetStore(str(tmp_path / "frames"))
    frames = [make_frame(game_id="nba-9", h1=("home", 0.0, 0.0))]

    with pyarrow_hidden():
        with pytest.raises(MissingTrackingDependencyError) as exc_info:
            store.write_frames(frames)

    message = str(exc_info.value)
    assert message == PYARROW_REQUIRED_MESSAGE
    # The message names the extra, not just the missing module.
    assert "sports-quant[tracking]" in message


def test_read_frames_without_pyarrow_raises_a_clear_error(tmp_path):
    store = FrameParquetStore(str(tmp_path / "frames"))

    with pyarrow_hidden():
        with pytest.raises(MissingTrackingDependencyError) as exc_info:
            store.read_frames("nba-9")

    assert "sports-quant[tracking]" in str(exc_info.value)


def test_missing_dependency_error_is_an_import_error():
    """Existing `except ImportError` handlers keep working."""

    assert issubclass(MissingTrackingDependencyError, ImportError)


def test_missing_dependency_error_chains_the_original_cause(tmp_path):
    """The underlying ImportError is preserved for debugging."""

    store = FrameParquetStore(str(tmp_path / "frames"))
    with pyarrow_hidden():
        with pytest.raises(MissingTrackingDependencyError) as exc_info:
            store.read_frames("nba-9")
    assert isinstance(exc_info.value.__cause__, ImportError)


def test_pyarrow_available_reports_absence():
    with pyarrow_hidden():
        assert pyarrow_available() is False
