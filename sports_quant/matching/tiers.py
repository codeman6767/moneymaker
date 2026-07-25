"""Shared, deterministic tier evaluation for alias-based resolution.

A *tier* is a pool of alias rows that all match the query at one strength (an
exact-provider link, a verbatim alias, a normalized alias, ...). The rule is the
same for teams, players and venues and lives here once:

* zero rows -> the tier produced nothing; try the next (weaker) tier;
* two or more distinct canonical entities -> ``AMBIGUOUS``; **stop**, never fall
  through to a weaker tier (a lower tier cannot resolve what a stronger one
  could not, and trying is how a wrong answer is manufactured);
* any ``is_ambiguous`` alias row -> ``AMBIGUOUS`` even with a single entity, and
  the result records that it resolved through an ambiguous alias (DQ-MATCH-006);
* exactly one entity, no ambiguous flag -> ``MATCHED``.

Candidates are always emitted sorted by canonical id, so a decision and its
child candidate rows read identically on every run regardless of DB row order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import AMBIGUOUS, MATCHED, Candidate


@dataclass(frozen=True)
class AliasRow:
    """One alias row considered in a tier (already league/season filtered)."""

    entity_id: str
    alias: str
    normalized: str
    provider: str
    is_ambiguous: bool
    curated: bool  # True when a curated (non-sentinel) validity window applies


@dataclass(frozen=True)
class TierOutcome:
    """The evaluation of one non-empty tier."""

    status: str
    entity_id: Optional[str]
    candidates: tuple[Candidate, ...]
    via_ambiguous_alias: bool
    reason: Optional[str]
    curated: bool  # every row in the winning pool carries a curated window


def evaluate_pool(
    rows: list[AliasRow], *, tier: str, score: float, entity_label: str
) -> Optional[TierOutcome]:
    """Evaluate one tier's pool; ``None`` when the pool is empty (try next tier)."""

    if not rows:
        return None
    entity_ids = sorted({r.entity_id for r in rows})
    candidates = tuple(
        Candidate(entity_id=eid, score=score, tier=tier, method=tier) for eid in entity_ids
    )
    curated = all(r.curated for r in rows)
    flagged = any(r.is_ambiguous for r in rows)
    if len(entity_ids) > 1:
        return TierOutcome(
            status=AMBIGUOUS,
            entity_id=None,
            candidates=candidates,
            # A shared alias flagged is_ambiguous drives DQ-MATCH-006.
            via_ambiguous_alias=flagged,
            reason=(
                f"{len(entity_ids)} {entity_label}s share the alias at tier {tier!r}; "
                "needs an additional discriminator"
            ),
            curated=curated,
        )
    if flagged:
        return TierOutcome(
            status=AMBIGUOUS,
            entity_id=None,
            candidates=candidates,
            via_ambiguous_alias=True,
            reason=f"alias for this {entity_label} is flagged ambiguous ({tier})",
            curated=curated,
        )
    return TierOutcome(
        status=MATCHED,
        entity_id=entity_ids[0],
        candidates=candidates,
        via_ambiguous_alias=False,
        reason=None,
        curated=curated,
    )
