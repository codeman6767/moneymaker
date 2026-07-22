"""Official / licensed lineup adapter.

Parses projected and confirmed lineups. Beyond per-player snapshots it returns a
:class:`LineupObservation` (the ordered set of starters, with a ``confirmed``
flag) so the detector can compare a confirmed lineup against the projected one
(``confirmed differs from projected``) and spot unexpected starters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from ..base import PlayerRef, PlayerStatus, SourceType, make_snapshot
from ..player_matching import MatchStatus
from .base_adapter import ParseResult, PollingReportAdapter, UnresolvedEntry


@dataclass
class LineupObservation:
    game_id: str
    team: str
    confirmed: bool
    published_at: datetime
    retrieved_at: datetime
    starters: Tuple[PlayerRef, ...] = ()

    @property
    def starter_keys(self) -> frozenset:
        return frozenset(p.key() for p in self.starters)


@dataclass
class LineupParseResult(ParseResult):
    lineup: Optional[LineupObservation] = None


class LineupAdapter(PollingReportAdapter):
    """Adapter for a lineup feed (projected or confirmed)."""

    def __init__(
        self,
        directory,
        source_id: str = "lineup_provider",
        source_type: SourceType = SourceType.LICENSED_DATA,
    ) -> None:
        super().__init__(source_id=source_id, source_type=source_type, directory=directory)

    def parse(self, raw: dict, retrieved_at: datetime) -> LineupParseResult:
        published_at = _parse_dt(raw.get("published_at"), retrieved_at)
        game_id = raw.get("game_id", "")
        team = raw.get("team", "")
        confirmed = bool(raw.get("confirmed", False))
        # A confirmed lineup from the provider is treated as an authoritative
        # confirmation; a projected one is not.
        source = self._source(published_at, retrieved_at, confirmed=confirmed)
        confidence = self._confidence(source)
        status = PlayerStatus.CONFIRMED_STARTER if confirmed else PlayerStatus.PROBABLE_STARTER

        snapshots = []
        unresolved: List[UnresolvedEntry] = []
        starters: List[PlayerRef] = []
        for row in raw.get("lineup", []):
            name = row.get("name", "") if isinstance(row, dict) else str(row)
            player_id = row.get("player_id") if isinstance(row, dict) else None
            match = self._resolve(name, team, player_id=player_id)
            if match.status is not MatchStatus.MATCHED:
                unresolved.append(
                    UnresolvedEntry(
                        raw_name=name, team=team, match_status=match.status,
                        candidates=match.candidates,
                        raw=row if isinstance(row, dict) else {"name": name},
                    )
                )
                continue
            player = match.player
            starters.append(player)
            snapshots.append(
                make_snapshot(
                    subject_key=player.key(),
                    player=player,
                    status=status,
                    source=source,
                    confidence=confidence,
                    raw={"game_id": game_id, "team": team, "confirmed": confirmed},
                )
            )

        lineup = LineupObservation(
            game_id=game_id, team=team, confirmed=confirmed,
            published_at=published_at, retrieved_at=retrieved_at,
            starters=tuple(starters),
        )
        return LineupParseResult(snapshots=snapshots, unresolved=unresolved, lineup=lineup)


def _parse_dt(value: Optional[str], fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
