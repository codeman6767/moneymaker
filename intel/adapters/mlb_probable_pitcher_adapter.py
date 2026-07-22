"""Official / licensed MLB probable-pitcher adapter.

Emits append-only observations of probable / confirmed / scratched starting
pitchers. Each observation carries ``role = "starting_pitcher"`` in its raw
payload so the material-change detector can recognize a scratched starter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..base import PlayerStatus, SourceType, make_snapshot
from .base_adapter import (
    ParseResult,
    PollingReportAdapter,
    UnresolvedEntry,
)

_STATUS_MAP = {
    "probable": PlayerStatus.PROBABLE_STARTER,
    "confirmed": PlayerStatus.CONFIRMED_STARTER,
    "scratched": PlayerStatus.SCRATCHED,
    "scratch": PlayerStatus.SCRATCHED,
}


class MLBProbablePitcherAdapter(PollingReportAdapter):
    """Adapter for an official/licensed probable-pitcher feed."""

    def __init__(self, directory, source_id: str = "mlb_probable_pitchers") -> None:
        super().__init__(
            source_id=source_id,
            source_type=SourceType.OFFICIAL_LEAGUE,
            directory=directory,
        )

    def parse(self, raw: dict, retrieved_at: datetime) -> ParseResult:
        published_at = _parse_dt(raw.get("published_at"), retrieved_at)
        game_id = raw.get("game_id", "")
        # A confirmed feed marks its rows confirmed; probables are not.
        source = self._source(published_at, retrieved_at)
        confidence = self._confidence(source)

        snapshots = []
        unresolved = []
        for row in raw.get("probables", []):
            name = row.get("pitcher", "")
            team = row.get("team")
            status = _STATUS_MAP.get(str(row.get("status", "")).strip().lower(), PlayerStatus.UNKNOWN)

            match = self._resolve(name, team, player_id=row.get("player_id"))
            player = match.matched_player()
            if player is None:
                unresolved.append(
                    UnresolvedEntry(
                        raw_name=name, team=team, match_status=match.status,
                        candidates=match.candidates, raw=row,
                    )
                )
                continue

            enriched = dict(row)
            enriched["role"] = "starting_pitcher"
            enriched["game_id"] = game_id
            # Subject is the pitcher slot for this game, so a replacement starter
            # is compared against the scratched one.
            subject_key = f"{game_id}:sp:{team}"
            snapshots.append(
                make_snapshot(
                    subject_key=subject_key,
                    player=player,
                    status=status,
                    source=source,
                    confidence=confidence,
                    reason=row.get("reason"),
                    raw=enriched,
                )
            )
        return ParseResult(snapshots=snapshots, unresolved=unresolved)


def _parse_dt(value: Optional[str], fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
