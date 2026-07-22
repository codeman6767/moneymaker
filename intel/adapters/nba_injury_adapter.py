"""Official NBA injury-report adapter.

Parses the league's official injury report into append-only observations. The
report is published on a fixed daily cadence on game days; :data:`NBA_INJURY_
REPORT_SCHEDULE` encodes those release times so a poller only fetches when a new
report is due (requirement 4), and the polling base class skips unchanged
re-fetches (requirement 5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..base import PlayerStatus, SourceType, make_snapshot
from ..player_matching import MatchStatus
from .base_adapter import (
    ParseResult,
    PollingReportAdapter,
    PollSchedule,
    UnresolvedEntry,
)

# Official NBA injury-report release times are defined in US Eastern; here they
# are given in UTC for game-day releases (approx: 1pm/6pm/9pm ET ~ 17/22/01 UTC),
# plus a placeholder for the pre-tip update handled by the poller near tip-off.
NBA_INJURY_REPORT_SCHEDULE = PollSchedule(
    release_times_utc=((17, 0), (22, 0), (1, 0)),
)

_STATUS_MAP = {
    "available": PlayerStatus.AVAILABLE,
    "active": PlayerStatus.AVAILABLE,
    "out": PlayerStatus.OUT,
    "questionable": PlayerStatus.QUESTIONABLE,
    "doubtful": PlayerStatus.DOUBTFUL,
    "probable": PlayerStatus.PROBABLE,
    "game time decision": PlayerStatus.GAME_TIME_DECISION,
    "gtd": PlayerStatus.GAME_TIME_DECISION,
}


class NBAInjuryReportAdapter(PollingReportAdapter):
    """Adapter for the official league injury report."""

    def __init__(self, directory, source_id: str = "nba_official_injury_report") -> None:
        super().__init__(
            source_id=source_id,
            source_type=SourceType.OFFICIAL_LEAGUE,
            directory=directory,
            schedule=NBA_INJURY_REPORT_SCHEDULE,
        )

    def parse(self, raw: dict, retrieved_at: datetime) -> ParseResult:
        published_at = _parse_dt(raw.get("published_at"), retrieved_at)
        source = self._source(published_at, retrieved_at)
        confidence = self._confidence(source)

        snapshots = []
        unresolved = []
        for row in raw.get("players", []):
            name = row.get("name", "")
            team = row.get("team")
            status = _STATUS_MAP.get(str(row.get("status", "")).strip().lower(), PlayerStatus.UNKNOWN)
            reason = row.get("reason")

            match = self._resolve(name, team, player_id=row.get("player_id"))
            if match.status is not MatchStatus.MATCHED:
                # Do not guess: surface ambiguous/unmatched rows for review.
                unresolved.append(
                    UnresolvedEntry(
                        raw_name=name, team=team, match_status=match.status,
                        candidates=match.candidates, raw=row,
                    )
                )
                continue

            player = match.player
            snapshots.append(
                make_snapshot(
                    subject_key=player.key(),
                    player=player,
                    status=status,
                    source=source,
                    confidence=confidence,
                    reason=reason,
                    raw=row,
                )
            )
        return ParseResult(snapshots=snapshots, unresolved=unresolved)


def _parse_dt(value: Optional[str], fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
