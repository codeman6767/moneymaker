"""Shared adapter machinery: resolution, scheduling and new-report detection."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional

from ..base import (
    PlayerRef,
    SourceMeta,
    SourceType,
    StatusSnapshot,
    content_hash,
)
from ..confidence import score_source
from ..history import ReportRegistry
from ..player_matching import MatchResult, MatchStatus, PlayerDirectory


@dataclass
class UnresolvedEntry:
    """A report row that could not be confidently matched to one player.

    Ambiguous or unmatched rows become these rather than guessed snapshots, so a
    caller can surface them for review instead of acting on a wrong player.
    """

    raw_name: str
    team: Optional[str]
    match_status: MatchStatus
    candidates: tuple[PlayerRef, ...] = ()
    raw: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    snapshots: List[StatusSnapshot]
    unresolved: List[UnresolvedEntry] = field(default_factory=list)


@dataclass
class PollResult:
    is_new: bool
    report_hash: str
    retrieved_at: datetime
    result: Optional[ParseResult] = None


class SourceAdapter(abc.ABC):
    """Base for all source adapters."""

    def __init__(
        self,
        source_id: str,
        source_type: SourceType,
        directory: PlayerDirectory,
    ) -> None:
        self.source_id = source_id
        self.source_type = source_type
        self.directory = directory

    def _source(
        self,
        published_at: datetime,
        retrieved_at: datetime,
        *,
        confirmed: bool = False,
        reference: Optional[str] = None,
    ) -> SourceMeta:
        return SourceMeta(
            source_id=self.source_id,
            source_type=self.source_type,
            published_at=published_at,
            retrieved_at=retrieved_at,
            confirmed=confirmed,
            reference=reference,
        )

    def _resolve(
        self, name: str, team: Optional[str], player_id: Optional[str] = None
    ) -> MatchResult:
        return self.directory.match(name, team=team, player_id=player_id)

    def _confidence(self, source: SourceMeta) -> float:
        return score_source(source)

    @abc.abstractmethod
    def parse(self, raw: dict, retrieved_at: datetime) -> ParseResult:
        """Extract player/team/status/reason observations from a raw report."""


@dataclass
class PollSchedule:
    """A daily release schedule (requirement 4: poll on the published cadence).

    ``release_times_utc`` are ``(hour, minute)`` tuples at which the source
    publishes. :meth:`is_due` reports whether a release has occurred since the
    last poll. An optional ``min_interval`` provides a simple fallback cadence.
    """

    release_times_utc: tuple[tuple[int, int], ...] = ()
    min_interval: Optional[timedelta] = None

    def due_times_on(self, day: date) -> List[datetime]:
        from datetime import timezone

        return [
            datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
            for (h, m) in self.release_times_utc
        ]

    def is_due(self, now: datetime, last_polled: Optional[datetime]) -> bool:
        # Scheduled releases: due if a release time falls in (last_polled, now].
        for rt in self.due_times_on(now.date()):
            if rt <= now and (last_polled is None or rt > last_polled):
                return True
        # Interval fallback.
        if self.min_interval is not None:
            if last_polled is None:
                return True
            return (now - last_polled) >= self.min_interval
        # With no interval and no release passed, only poll if never polled.
        return last_polled is None and not self.release_times_utc


class PollingReportAdapter(SourceAdapter):
    """A source polled as whole reports, with byte-level new-report detection."""

    def __init__(
        self,
        source_id: str,
        source_type: SourceType,
        directory: PlayerDirectory,
        schedule: Optional[PollSchedule] = None,
    ) -> None:
        super().__init__(source_id, source_type, directory)
        self.schedule = schedule or PollSchedule()

    def is_due(self, now: datetime, last_polled: Optional[datetime]) -> bool:
        return self.schedule.is_due(now, last_polled)

    def poll(self, raw_report: dict, now: datetime, registry: ReportRegistry) -> PollResult:
        """Fetch-and-parse a report, skipping unchanged re-fetches.

        Computing the hash over the raw report means an identical file polled
        again is recognized as not-new and produces no snapshots (requirement
        5), while a changed/corrected report parses normally.
        """

        report_hash = content_hash(raw_report)
        if not registry.is_new(self.source_id, report_hash):
            return PollResult(is_new=False, report_hash=report_hash, retrieved_at=now)
        registry.register(self.source_id, report_hash)
        result = self.parse(raw_report, retrieved_at=now)
        return PollResult(is_new=True, report_hash=report_hash, retrieved_at=now, result=result)
