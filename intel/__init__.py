"""Injury / lineup / material-news intelligence (Module 4).

Turns injury reports, probable pitchers, lineups, team announcements and
authorized social/news into an append-only, provenance-rich timeline; scores
source confidence; and detects the *material* changes that should trigger a
re-prediction -- estimating the resulting change in active probability and
expected minutes, storing before/after model inputs, and raising alerts.

See ``CLAUDE.md``: no unauthorized scraping, and speed/automation never bypass
confirmation and risk gates.
"""

from .adapters import (
    NBA_INJURY_REPORT_SCHEDULE,
    LineupAdapter,
    LineupObservation,
    MLBProbablePitcherAdapter,
    NBAInjuryReportAdapter,
    ParseResult,
    PollingReportAdapter,
    PollResult,
    PollSchedule,
    SocialNewsAdapter,
    SourceAdapter,
    TeamAnnouncementAdapter,
    UnauthorizedSourceError,
    UnresolvedEntry,
)
from .base import (
    Alert,
    ChangeType,
    MaterialChange,
    PlayerRef,
    PlayerStatus,
    Severity,
    SourceMeta,
    SourceType,
    StatusSnapshot,
    make_snapshot,
)
from .confidence import (
    ACTIONABLE_THRESHOLD,
    BASE_CONFIDENCE,
    is_actionable,
    score_source,
)
from .history import ReportRegistry, StatusHistory
from .material_change import (
    MaterialChangeDetector,
    Projection,
    active_probability,
    expected_minutes,
)
from .player_matching import (
    MatchResult,
    MatchStatus,
    PlayerDirectory,
    normalize_name,
)

__all__ = [
    # base
    "PlayerRef",
    "PlayerStatus",
    "SourceType",
    "SourceMeta",
    "StatusSnapshot",
    "make_snapshot",
    "ChangeType",
    "MaterialChange",
    "Severity",
    "Alert",
    # matching
    "normalize_name",
    "MatchStatus",
    "MatchResult",
    "PlayerDirectory",
    # confidence
    "score_source",
    "is_actionable",
    "ACTIONABLE_THRESHOLD",
    "BASE_CONFIDENCE",
    # history
    "StatusHistory",
    "ReportRegistry",
    # adapters
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
    # detector
    "MaterialChangeDetector",
    "Projection",
    "active_probability",
    "expected_minutes",
]
