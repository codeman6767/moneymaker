"""Deterministic player matching.

Matching is intentionally *not* fuzzy: it normalizes names by a fixed set of
rules and matches against a known roster/directory. When a normalized name maps
to more than one player it returns :class:`MatchStatus.AMBIGUOUS` -- it never
picks one, and it never does probabilistic/random tie-breaking. This guarantees
the same inputs always produce the same result and that an ambiguous player is
surfaced rather than silently mis-assigned (requirement 7).
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .base import PlayerRef

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Deterministically normalize a display name to a match key.

    Steps: strip accents -> lowercase -> drop punctuation -> collapse
    whitespace -> remove common generational suffixes.
    """

    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    depunct = _PUNCT.sub(" ", lowered)
    collapsed = _WS.sub(" ", depunct).strip()
    tokens = [t for t in collapsed.split(" ") if t and t not in _SUFFIXES]
    return " ".join(tokens)


class MatchStatus(str, enum.Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    player: Optional[PlayerRef] = None
    candidates: tuple[PlayerRef, ...] = ()

    @property
    def is_confident(self) -> bool:
        return self.status is MatchStatus.MATCHED


@dataclass
class PlayerDirectory:
    """A roster used for deterministic matching.

    Indexes players by provider id (exact) and by ``(team, normalized_name)``.
    A normalized name shared by two players on the same team is genuinely
    ambiguous and reported as such.
    """

    _by_id: Dict[str, PlayerRef] = field(default_factory=dict)
    # (team|None, normalized_name) -> list of players
    _by_name_team: Dict[tuple, List[PlayerRef]] = field(default_factory=dict)

    def add(self, full_name: str, team: Optional[str], player_id: str) -> PlayerRef:
        normalized = normalize_name(full_name)
        ref = PlayerRef(full_name=full_name, team=team, player_id=player_id, normalized=normalized)
        self._by_id[player_id] = ref
        self._by_name_team.setdefault((team, normalized), []).append(ref)
        # Also index without team for team-less lookups.
        self._by_name_team.setdefault((None, normalized), []).append(ref)
        return ref

    def match(self, full_name: str, team: Optional[str] = None, player_id: Optional[str] = None) -> MatchResult:
        # 1. Exact provider id wins outright and is fully deterministic.
        if player_id is not None and player_id in self._by_id:
            return MatchResult(MatchStatus.MATCHED, self._by_id[player_id])

        normalized = normalize_name(full_name)

        # 2. Name + team.
        if team is not None:
            candidates = self._by_name_team.get((team, normalized), [])
            if len(candidates) == 1:
                return MatchResult(MatchStatus.MATCHED, candidates[0])
            if len(candidates) > 1:
                return MatchResult(MatchStatus.AMBIGUOUS, candidates=tuple(candidates))

        # 3. Name only (dedupe by player id to avoid the team/no-team double index).
        raw = self._by_name_team.get((None, normalized), [])
        seen: Dict[str, PlayerRef] = {}
        for r in raw:
            if r.player_id is not None:
                seen[r.player_id] = r
        candidates = list(seen.values())
        if len(candidates) == 1:
            return MatchResult(MatchStatus.MATCHED, candidates[0])
        if len(candidates) > 1:
            return MatchResult(MatchStatus.AMBIGUOUS, candidates=tuple(candidates))

        return MatchResult(MatchStatus.UNMATCHED)
