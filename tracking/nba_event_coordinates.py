"""NBA event-level coordinates and on-court lineup indicators.

Everything here is *event-level* and derived from ordinary play-by-play feeds
(shot charts, play locations, substitution-derived lineups). None of it is
player tracking: an on-court lineup tells you *who* is on the floor, never
*where* they are standing. Frame-level positions come only from an optical
:class:`~tracking.base.FrameSource` adapter.

Coordinate convention: shot/play ``x``/``y`` are in feet relative to the
attacking basket at the origin ``(0, 0)``, matching how the aggregators measure
shot distance. A provider that reports different units must convert before
constructing these models -- we store what we are given and never fabricate a
third (``z``) axis for a 2-D shot chart (requirement 9).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from .base import Coordinates, EventCoordinate


class NBACourt:
    """Standard NBA court geometry (feet), basket at the origin."""

    RIM_X = 0.0
    RIM_Y = 0.0
    RESTRICTED_AREA_RADIUS = 4.0
    PAINT_HALF_WIDTH = 8.0  # lane is 16 ft wide
    FREE_THROW_DISTANCE = 15.0
    THREE_POINT_ARC_RADIUS = 23.75  # above the break
    THREE_POINT_CORNER_DISTANCE = 22.0
    CORNER_SIDE_X = 22.0  # |x| beyond which the 3 is a corner three


class NBAShotEvent(BaseModel):
    """A single shot with an event-level location."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    game_id: str
    player_id: str
    team: Optional[str] = None
    period: Optional[int] = None
    # Location relative to the attacking basket, in feet. Optional: a feed may
    # report a shot with no location, and we must not invent one.
    x: Optional[float] = None
    y: Optional[float] = None
    shot_type: Optional[str] = None  # e.g. "jump_shot", "layup"
    shot_value: Optional[int] = None  # 2 or 3, if the feed states it
    made: Optional[bool] = None
    source: str = "play_by_play"

    @property
    def has_location(self) -> bool:
        return self.x is not None and self.y is not None

    def to_event_coordinate(self) -> EventCoordinate:
        return EventCoordinate(
            sport="nba",
            event_id=f"{self.game_id}:{self.player_id}:{self.period}:shot",
            event_type="shot",
            # A 2-D shot chart: x, y only. No z is fabricated.
            coordinates=Coordinates(x=self.x, y=self.y),
            source=self.source,
        )


class NBAPlayEvent(BaseModel):
    """A generic located play event (rebound, turnover, etc.)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    game_id: str
    event_id: str
    event_type: str
    player_id: Optional[str] = None
    team: Optional[str] = None
    period: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None
    source: str = "play_by_play"

    def to_event_coordinate(self) -> EventCoordinate:
        return EventCoordinate(
            sport="nba",
            event_id=self.event_id,
            event_type=self.event_type,
            coordinates=Coordinates(x=self.x, y=self.y),
            source=self.source,
        )


class OnCourtLineup(BaseModel):
    """Which five players per team are on the floor -- an *indicator*, not a
    position.

    Derived from substitutions (see the live NBA state model). It is explicitly
    non-positional: :attr:`is_positional` is always ``False`` so it can never be
    mistaken for tracking data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    game_id: str
    period: Optional[int] = None
    home_on_court: tuple[str, ...] = ()
    away_on_court: tuple[str, ...] = ()

    @property
    def is_positional(self) -> bool:
        return False

    def on_court(self, team: str) -> tuple[str, ...]:
        return self.home_on_court if team == "home" else self.away_on_court

    def is_on_court(self, player_id: str) -> bool:
        return player_id in self.home_on_court or player_id in self.away_on_court

    def count(self, team: str) -> int:
        return len(self.on_court(team))
