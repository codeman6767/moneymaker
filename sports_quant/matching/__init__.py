"""Phase D5A: deterministic canonical entity and official-game matching.

This package resolves provider team / player / venue references and official
MLB/NBA provider games to the *existing* canonical entities (``teams``,
``players``, ``venues``, ``games``). It never trains a model, never contacts a
provider, and never invents a canonical entity from a name: an unknown or
ambiguous input is recorded and left for review, not guessed.

Every resolution writes exactly one ``entity_match_decisions`` row plus one
``match_candidates`` row per candidate considered (via the D1
``SqliteMatchingRepository``), and links a provider reference only after an
accepted decision. Determinism is a hard requirement -- identical inputs and
alias data always produce the identical decision, candidate order, and score.

D5A deliberately excludes sportsbook-event and Kalshi market matching; those are
D5B, after D5A passes an independent review.
"""

from __future__ import annotations

from .model import (
    AMBIGUOUS,
    MATCHED,
    MATCHER_VERSION,
    THRESHOLD,
    UNMATCHED,
    Candidate,
    Resolution,
)

__all__ = [
    "AMBIGUOUS",
    "MATCHED",
    "MATCHER_VERSION",
    "THRESHOLD",
    "UNMATCHED",
    "Candidate",
    "Resolution",
]
