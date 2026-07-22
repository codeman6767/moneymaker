"""Authorized social / news adapter interface.

This adapter only accepts items from an explicit allowlist of authorized
authors/handles. Anything else raises :class:`UnauthorizedSourceError` -- there
is no scraping of arbitrary or unauthorized sources (see ``CLAUDE.md``: "Do not
use unauthorized social-media scraping.").

Observations it produces are always **unconfirmed**: low confidence and,
critically, never automatically actionable. They can inform predictions (a
material input changed) but must not, on their own, drive an automated trade
(requirement 9). Confirmation must come from an official source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from ..base import PlayerStatus, SourceType, make_snapshot
from .base_adapter import ParseResult, SourceAdapter, UnresolvedEntry


class UnauthorizedSourceError(Exception):
    """Raised when an item comes from a source not on the allowlist."""


_STATUS_MAP = {
    "out": PlayerStatus.OUT,
    "available": PlayerStatus.AVAILABLE,
    "questionable": PlayerStatus.QUESTIONABLE,
    "doubtful": PlayerStatus.DOUBTFUL,
    "scratched": PlayerStatus.SCRATCHED,
    "gtd": PlayerStatus.GAME_TIME_DECISION,
    "game time decision": PlayerStatus.GAME_TIME_DECISION,
}


class SocialNewsAdapter(SourceAdapter):
    """Adapter for authorized social/news feeds (unconfirmed by definition)."""

    def __init__(
        self,
        directory,
        authorized_authors: Iterable[str],
        source_id: str = "authorized_social",
        source_type: SourceType = SourceType.SOCIAL,
    ) -> None:
        super().__init__(source_id, source_type, directory)
        # Case-insensitive allowlist of permitted authors/handles.
        self._authorized = {a.strip().lower() for a in authorized_authors}
        if not self._authorized:
            raise ValueError("SocialNewsAdapter requires a non-empty allowlist of authorized authors")

    def is_authorized(self, author: Optional[str]) -> bool:
        return author is not None and author.strip().lower() in self._authorized

    def parse(self, raw: dict, retrieved_at: datetime) -> ParseResult:
        author = raw.get("author")
        if not self.is_authorized(author):
            raise UnauthorizedSourceError(
                f"author {author!r} is not on the authorized allowlist; refusing to ingest"
            )

        published_at = _parse_dt(raw.get("published_at"), retrieved_at)
        # Never confirmed: social/news is advisory only.
        source = self._source(published_at, retrieved_at, confirmed=False, reference=raw.get("url"))
        confidence = self._confidence(source)

        name = raw.get("player", "")
        team = raw.get("team")
        status = _STATUS_MAP.get(str(raw.get("status", "")).strip().lower(), PlayerStatus.UNKNOWN)

        match = self._resolve(name, team, player_id=raw.get("player_id"))
        player = match.matched_player()
        if player is None:
            return ParseResult(
                snapshots=[],
                unresolved=[
                    UnresolvedEntry(
                        raw_name=name, team=team, match_status=match.status,
                        candidates=match.candidates, raw=raw,
                    )
                ],
            )

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
