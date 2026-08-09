"""Decision-backed canonical resolution for normalized observations.

Why this exists
---------------
Every normalized observation table in schema v17 is APPEND-ONLY and carries only
provider identifiers plus, in some cases, a nullable canonical column that
ingestion could not populate (matching had not run). Those columns cannot be
backfilled: ``lineup_players``, ``nba_player_statistics``, ``team_game_statistics``
and the rest all carry ``BEFORE UPDATE`` guards that ``RAISE(ABORT)``, and
weakening a guard to fill a convenience column would trade an audit property for
a join shortcut.

So canonical identity is made available by RESOLUTION, not propagation: an
observation carries a provider id, and the provider reference plus **its own
backing accepted decision** say what that provider id means canonically.

The source-of-truth contract
----------------------------
An accepted canonical mapping is authoritative only when ALL of the following
hold. Anything else resolves to ``None`` -- ambiguous, rejected, unmatched,
review-pending and structurally broken references never acquire an identity:

* the provider reference exists and carries a canonical id;
* it carries a ``match_decision_id`` -- the exact decision that justified the
  link, never an unconstrained "latest decision for this source" lookup that a
  same-timestamp or later unrelated decision could win;
* that decision exists, its ``outcome`` is ``accepted``, its ``entity_type``
  matches the kind being resolved, and its ``matched_entity_id`` is the same
  canonical id the reference points at.

Knowledge time
--------------
``as_of`` gates on the decision's ``decided_at``. Matching a reference today must
not make it resolvable at a cutoff before that knowledge existed, so a decision
decided after the cutoff is invisible and the observation stays unresolved. This
mirrors :meth:`sports_quant.pit.asof.AsOfReader.matched_entity`, which remains the
only feature-facing path (it additionally enforces the manual-review gate); this
module is for matching-side and reporting-side joins that need the same rule
without the full point-in-time reader.

This module never writes. It creates no canonical entity, no alias, no decision
and no link.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from ..db.schema import from_iso
from ..pit.models import Cutoff

#: Reference table and canonical column for each resolvable kind.
_KINDS = {
    "team": ("provider_team_references", "provider_team_id", "team_id"),
    "player": ("provider_player_references", "provider_player_id", "player_id"),
    "game": ("provider_game_references", "provider_game_id", "game_id"),
}


class CanonicalResolutionError(ValueError):
    """An unsupported entity kind was requested."""


def _instant(stored: str) -> datetime:
    """A stored corpus timestamp as an aware UTC instant."""

    return from_iso(stored)


def _cutoff_instant(as_of: str) -> datetime:
    """Parse a caller-supplied cutoff into an aware UTC instant.

    Ordering is CHRONOLOGICAL, never lexical. Comparing the stored strings looked
    right for the corpus canonical form and was wrong everywhere else: an
    equivalent instant written ``...+00:00`` sorts before the same ``...Z`` value,
    a whole-second cutoff sorts *after* a sub-second decision on the same second
    (``'Z' > '.'``), and a malformed cutoff such as ``'not-a-timestamp'`` sorts
    after every real timestamp and therefore RESOLVED the mapping instead of
    failing closed.

    :class:`Cutoff` is the repository's single cutoff contract, so this shares one
    semantic ordering with :meth:`sports_quant.pit.asof.AsOfReader.matched_entity`.
    Naive and unparseable values raise rather than being guessed at.
    """

    return Cutoff.parse(as_of).datetime


@dataclass(frozen=True)
class CanonicalLink:
    """An accepted canonical identity and the decision that justifies it."""

    kind: str
    provider: str
    provider_entity_id: str
    canonical_id: str
    match_decision_id: str
    decided_at: str


def _reference_row(conn: sqlite3.Connection, kind: str, provider: str,
                   provider_entity_id: str) -> Optional[sqlite3.Row]:
    table, provider_column, canonical_column = _KINDS[kind]
    return conn.execute(
        f"SELECT {canonical_column} AS canonical_id, match_decision_id "  # noqa: S608
        f"FROM {table} WHERE provider = ? AND {provider_column} = ?",
        (provider, provider_entity_id),
    ).fetchone()


def resolve_canonical(
    conn: sqlite3.Connection,
    *,
    kind: str,
    provider: str,
    provider_entity_id: str,
    as_of: Optional[str] = None,
) -> Optional[CanonicalLink]:
    """The canonical identity a provider id maps to, or ``None``.

    ``None`` is returned for every state that is not a fully justified accepted
    link, so a caller can never mistake "not matched yet" for an identity.
    """

    if kind not in _KINDS:
        raise CanonicalResolutionError(
            f"unsupported kind {kind!r}; expected one of {sorted(_KINDS)}")
    row = _reference_row(conn, kind, provider, provider_entity_id)
    if row is None:
        return None
    canonical_id = row["canonical_id"]
    decision_id = row["match_decision_id"]
    if canonical_id is None or decision_id is None:
        # Unmatched, or a link with no provenance: not an identity.
        return None
    decision = conn.execute(
        "SELECT match_id, entity_type, outcome, matched_entity_id, decided_at, "
        "source_provider, source_ref FROM entity_match_decisions WHERE match_id = ?",
        (decision_id,),
    ).fetchone()
    if decision is None:
        return None
    if (str(decision["outcome"]) != "accepted"
            or str(decision["entity_type"]) != kind
            or decision["matched_entity_id"] != canonical_id):
        # The link's own decision does not justify it: fail closed rather than
        # trusting the convenience column.
        return None
    if (str(decision["source_provider"]) != provider
            or str(decision["source_ref"]) != str(provider_entity_id)):
        # A matching canonical target is NOT sufficient. The decision must have
        # adjudicated THIS provider reference; one recorded for a different
        # provider or a different provider id may name the same canonical entity
        # by coincidence without ever having judged this one.
        return None
    decided_at = str(decision["decided_at"])
    if as_of is not None and _instant(decided_at) > _cutoff_instant(as_of):
        # Known only after the cutoff: invisible at that point in time.
        return None
    return CanonicalLink(
        kind=kind, provider=provider, provider_entity_id=str(provider_entity_id),
        canonical_id=str(canonical_id), match_decision_id=str(decision["match_id"]),
        decided_at=decided_at,
    )


def resolve_many(
    conn: sqlite3.Connection,
    *,
    kind: str,
    provider: str,
    provider_entity_ids: Iterable[str],
    as_of: Optional[str] = None,
) -> dict[str, Optional[CanonicalLink]]:
    """Resolve a batch, preserving the unresolved entries as explicit ``None``.

    Deterministic: keyed by provider id, so the result never depends on the order
    the ids were supplied or on SQLite row order.
    """

    return {
        str(pid): resolve_canonical(conn, kind=kind, provider=provider,
                                    provider_entity_id=str(pid), as_of=as_of)
        for pid in sorted({str(p) for p in provider_entity_ids})
    }
