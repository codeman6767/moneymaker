"""Source adapters for injury / lineup / material-news intelligence.

Each adapter turns a provider's raw report into append-only
:class:`~intel.base.StatusSnapshot` observations with full provenance, using
deterministic player matching and source-confidence scoring. Optical/social
sources that require authorization enforce it here.
"""

from .base_adapter import (
    ParseResult,
    PollingReportAdapter,
    PollResult,
    PollSchedule,
    SourceAdapter,
    UnresolvedEntry,
)
from .lineup_adapter import LineupAdapter, LineupObservation
from .mlb_probable_pitcher_adapter import MLBProbablePitcherAdapter
from .nba_injury_adapter import NBA_INJURY_REPORT_SCHEDULE, NBAInjuryReportAdapter
from .social_news_adapter import SocialNewsAdapter, UnauthorizedSourceError
from .team_announcement_adapter import TeamAnnouncementAdapter

__all__ = [
    "SourceAdapter",
    "PollingReportAdapter",
    "PollSchedule",
    "PollResult",
    "ParseResult",
    "UnresolvedEntry",
    "NBAInjuryReportAdapter",
    "NBA_INJURY_REPORT_SCHEDULE",
    "MLBProbablePitcherAdapter",
    "LineupAdapter",
    "LineupObservation",
    "TeamAnnouncementAdapter",
    "SocialNewsAdapter",
    "UnauthorizedSourceError",
]
