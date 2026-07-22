"""Append-only status history and report-level deduplication.

Two guarantees:

* **Nothing is ever overwritten.** :meth:`StatusHistory.append` only ever adds;
  earlier snapshots for a subject remain forever, preserving the full timeline
  (requirements 1 and 3). Corrections are appended, flagged, and kept alongside
  what they correct.
* **The same file is not stored repeatedly.** :class:`ReportRegistry` remembers
  the content hash of each fetched report so an unchanged re-fetch is recognized
  as "not new" rather than duplicated (requirement 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .base import StatusSnapshot


class StatusHistory:
    """An append-only log of status snapshots, indexed by subject."""

    def __init__(self) -> None:
        self._by_subject: Dict[str, List[StatusSnapshot]] = {}
        self._all: List[StatusSnapshot] = []
        # Guards against storing a byte-identical observation twice.
        self._seen_hashes: set[str] = set()

    def append(self, snapshot: StatusSnapshot) -> bool:
        """Append a snapshot. Returns False if an identical one already exists.

        "Identical" means the same content hash: the same source publishing the
        same status at the same publication time. A genuinely new observation
        (new status, new publication time, or different source) always appends.
        """

        if snapshot.content_hash in self._seen_hashes:
            return False
        self._seen_hashes.add(snapshot.content_hash)
        self._by_subject.setdefault(snapshot.subject_key, []).append(snapshot)
        self._all.append(snapshot)
        return True

    def history(self, subject_key: str) -> List[StatusSnapshot]:
        return list(self._by_subject.get(subject_key, []))

    def latest(self, subject_key: str) -> Optional[StatusSnapshot]:
        items = self._by_subject.get(subject_key)
        return items[-1] if items else None

    def latest_from_other_source(
        self, subject_key: str, source_id: str
    ) -> Optional[StatusSnapshot]:
        """Most recent snapshot for the subject from a *different* source.

        Used to detect conflicting sources.
        """

        for snap in reversed(self._by_subject.get(subject_key, [])):
            if snap.source.source_id != source_id:
                return snap
        return None

    def all(self) -> List[StatusSnapshot]:
        return list(self._all)


@dataclass
class ReportRegistry:
    """Tracks the content hash of fetched reports per source to spot new ones."""

    _last_hash: Dict[str, str] = field(default_factory=dict)
    _all_hashes: Dict[str, set] = field(default_factory=dict)

    def is_new(self, source_id: str, report_hash: str) -> bool:
        """Whether this report differs from the last one seen for the source."""

        return self._last_hash.get(source_id) != report_hash

    def register(self, source_id: str, report_hash: str) -> bool:
        """Record a report hash. Returns True if it was new for this source."""

        new = self.is_new(source_id, report_hash)
        self._last_hash[source_id] = report_hash
        self._all_hashes.setdefault(source_id, set()).add(report_hash)
        return new

    def seen_before(self, source_id: str, report_hash: str) -> bool:
        return report_hash in self._all_hashes.get(source_id, set())
