"""Official team-announcement adapter interface.

Team announcements (a club stating a player will not play, or confirming a
starter) are official and treated as confirmed. This is a thin, deterministic
interface over *structured* announcements: it does not scrape or interpret free
text -- a caller passes already-structured fields. Subclass or configure it per
team feed as needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..base import PlayerStatus, SourceType, make_snapshot
from ..player_matching import MatchStatus
from .base_adapter import ParseResult, SourceAdapter, UnresolvedEntry

_STATUS_MAP = {
    "out": PlayerStatus.OUT,
    "available": PlayerStatus.AVAILABLE,
    "active": PlayerStatus.AVAILABLE,
    "questionable": PlayerStatus.QUESTIONABLE,
    "doubtful": PlayerStatus.DOUBTFUL,
    "confirmed_starter": PlayerStatus.CONFIRMED_STARTER,
    "scratched": PlayerStatus.SCRATCHED,
}


class TeamAnnouncementAdapter(SourceAdapter):
    """Interface adapter for official team announcements (confirmed source)."""

    def __init__(self, directory, source_id: str = "team_announcement") -> None:
        super().__init__(source_id, SourceType.OFFICIAL_TEAM, directory)

    def parse(self, raw: dict, retrieved_at: datetime) -> ParseResult:
        published_at = _parse_dt(raw.get("published_at"), retrieved_at)
        # Team announcements are official confirmations.
        source = self._source(published_at, retrieved_at, confirmed=True)
        confidence = self._confidence(source)

        name = raw.get("player", "")
        team = raw.get("team")
        status = _STATUS_MAP.get(str(raw.get("status", "")).strip().lower(), PlayerStatus.UNKNOWN)

        match = self._resolve(name, team, player_id=raw.get("player_id"))
        if match.status is not MatchStatus.MATCHED:
            return ParseResult(
                snapshots=[],
                unresolved=[
                    UnresolvedEntry(
                        raw_name=name, team=team, match_status=match.status,
                        candidates=match.candidates, raw=raw,
                    )
                ],
            )

        player = match.player
        snap = make_snapshot(
            subject_key=player.key(),
            player=player,
            status=status,
            source=source,
            confidence=confidence,
            reason=raw.get("reason"),
            raw=raw,
        )
        return ParseResult(snapshots=[snap])


def _parse_dt(value: Optional[str], fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
