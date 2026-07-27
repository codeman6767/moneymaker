"""Shared classification for atomic decision-and-link application (D5A + D5B1).

An accepted match decision and the provider-reference (or sportsbook-event) link
it supports must be one atomic unit: recorded, applied, and verified together in
a single transaction, so a conflict or a persistence failure never leaves an
accepted decision claiming a link it does not have. Both the D5A official/player
matcher (``service.py`` / ``players_service.py``) and the D5B1 sportsbook matcher
(``sportsbook.py``) classify a proposed link against the reference's CURRENT link
the same way, through this one helper, so the two paths cannot drift.
"""

from __future__ import annotations

import enum
from typing import Optional


class LinkAttempt(enum.Enum):
    """How a proposed accepted link relates to the reference's current link."""

    #: No current link -> record the accepted decision and apply the link.
    CLEAN = "clean"
    #: The exact same link is already present and valid -> recognize it and record
    #: NO new accepted decision (idempotent replay).
    REPLAY = "replay"
    #: Already linked to a different entity/orientation, or the supporting decision
    #: is corrupt/mismatched -> blocking; never record a fresh accepted decision.
    CONFLICT = "conflict"


class MatchLinkError(RuntimeError):
    """A link application failed after its accepted decision was recorded.

    Raised so the enclosing run transaction rolls back rather than committing an
    accepted decision without its exact, verified link. The runner surfaces it as
    an active failure (exit 1)."""


def classify_link_attempt(
    *,
    current_canonical_id: Optional[str],
    proposed_canonical_id: Optional[str],
    decision_valid: bool,
    current_orientation: Optional[str] = None,
    proposed_orientation: Optional[str] = None,
) -> LinkAttempt:
    """Classify a proposed accepted link against the reference's current link.

    ``decision_valid`` is the caller's verdict on whether the CURRENT link's
    supporting decision genuinely backs it (exists, accepted, owned by this
    source reference, and names this same canonical entity). Orientation is
    compared only when the caller supplies it (sportsbook events carry one;
    provider references do not).
    """

    if current_canonical_id is None:
        return LinkAttempt.CLEAN
    same_entity = current_canonical_id == proposed_canonical_id
    same_orientation = proposed_orientation is None or current_orientation == proposed_orientation
    if same_entity and same_orientation and decision_valid:
        return LinkAttempt.REPLAY
    return LinkAttempt.CONFLICT
