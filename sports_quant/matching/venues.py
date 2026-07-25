"""Conservative canonical venue resolution (task §7, ENTITY_MATCHING.md).

Evidence order, strongest first:

1. ``exact_provider_id`` (1.00) -- the provider venue id already maps to a venue;
2. ``exact_alias`` (0.99) -- a provider-scoped venue alias matches verbatim;
3. ``normalized_alias`` (0.95) -- a provider-scoped normalized alias matches;
4. no candidate.

A venue is never matched merely because coordinates are geographically close.
Coordinates *validate* a match or *expose a contradiction* -- a material
coordinate / timezone / country / roof-type disagreement produces a data-quality
issue and manual review, and never silently merges two canonical venues.
Temporary, neutral, relocated and international venues stay distinct when the
canonical data says they are distinct; an unknown venue stays unresolved.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..db.models import Venue
from ..db.normalize import normalize_name
from .model import (
    MATCHED,
    SCORE_EXACT_ALIAS,
    SCORE_EXACT_PROVIDER_ID,
    SCORE_NORMALIZED_ALIAS,
    SCORE_NORMALIZED_ALIAS_UNSCOPED,
    TIER_EXACT_ALIAS,
    TIER_EXACT_PROVIDER_ID,
    TIER_NORMALIZED_ALIAS,
    TIER_NORMALIZED_ALIAS_UNSCOPED,
    UNMATCHED,
    Resolution,
)
from .tiers import AliasRow, evaluate_pool

#: Coordinate disagreement (in degrees) treated as material. ~0.1 deg lat is
#: ~11 km -- comfortably larger than the difference between a stadium's several
#: published coordinates, but far smaller than two different cities.
_COORD_EPSILON = 0.1


@dataclass(frozen=True)
class VenueContradiction:
    """A material disagreement between a resolved venue and provider metadata."""

    field: str
    canonical: object
    provider: object


class VenueResolver:
    """Resolves a provider venue reference to a canonical venue, conservatively."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def resolve(
        self,
        *,
        provider: str,
        provider_venue_id: Optional[str] = None,
        raw_name: Optional[str] = None,
    ) -> Resolution:
        # Tier 1 -- the provider venue id already maps to exactly one venue.
        if provider_venue_id:
            link_rows = self._rows(
                "SELECT venue_id, alias, normalized, provider FROM venue_aliases "
                "WHERE provider = ? AND provider_venue_id = ?",
                (provider, provider_venue_id),
            )
            outcome = evaluate_pool(
                link_rows,
                tier=TIER_EXACT_PROVIDER_ID,
                score=SCORE_EXACT_PROVIDER_ID,
                entity_label="venue",
            )
            if outcome is not None:
                return self._resolution(outcome, TIER_EXACT_PROVIDER_ID, SCORE_EXACT_PROVIDER_ID)

        raw = raw_name
        if not raw:
            return Resolution(
                status=UNMATCHED,
                method="none",
                score=0.0,
                tier="none",
                reason="no provider venue id or name resolved to a canonical venue",
                needs_review=True,
            )

        query = normalize_name(raw)
        if not query.normalized:
            return Resolution(
                status=UNMATCHED, method="none", score=0.0, tier="none",
                reason="empty venue name after normalization", needs_review=True,
            )

        rows = self._rows(
            "SELECT venue_id, alias, normalized, provider FROM venue_aliases", ()
        )
        exact = [r for r in rows if r.alias == raw and (not provider or r.provider == provider)]
        scoped = (
            [r for r in rows if r.normalized == query.normalized and r.provider == provider]
            if provider
            else []
        )
        # A venue only falls back to an unscoped normalized alias when no provider
        # scope was supplied; with a provider we stay provider-scoped (§7).
        unscoped = (
            [] if provider else [r for r in rows if r.normalized == query.normalized]
        )
        tiers = [
            (exact, TIER_EXACT_ALIAS, SCORE_EXACT_ALIAS),
            (scoped, TIER_NORMALIZED_ALIAS, SCORE_NORMALIZED_ALIAS),
            (unscoped, TIER_NORMALIZED_ALIAS_UNSCOPED, SCORE_NORMALIZED_ALIAS_UNSCOPED),
        ]
        for pool, tier, score in tiers:
            outcome = evaluate_pool(pool, tier=tier, score=score, entity_label="venue")
            if outcome is not None:
                return self._resolution(outcome, tier, score)

        return Resolution(
            status=UNMATCHED, method="none", score=0.0, tier="none",
            reason=f"no venue alias matches {query.normalized!r}", needs_review=True,
        )

    def contradictions(
        self,
        venue: Venue,
        *,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        timezone: Optional[str] = None,
        country: Optional[str] = None,
        roof_type: Optional[str] = None,
    ) -> list[VenueContradiction]:
        """Material disagreements between ``venue`` and supplied provider metadata.

        Coordinates never *drive* a match; here they only validate one or surface
        a contradiction for review. An empty list means no material conflict.
        """

        out: list[VenueContradiction] = []
        if (
            latitude is not None
            and longitude is not None
            and venue.latitude is not None
            and venue.longitude is not None
            and (
                abs(venue.latitude - latitude) > _COORD_EPSILON
                or abs(venue.longitude - longitude) > _COORD_EPSILON
            )
        ):
            out.append(
                VenueContradiction(
                    field="coordinates",
                    canonical=(venue.latitude, venue.longitude),
                    provider=(latitude, longitude),
                )
            )
        for field_name, canon, prov in (
            ("timezone", venue.timezone, timezone),
            ("country", venue.country, country),
            ("roof_type", venue.roof_type, roof_type),
        ):
            if prov is not None and canon is not None and canon != prov:
                out.append(VenueContradiction(field=field_name, canonical=canon, provider=prov))
        return out

    # -- internals ----------------------------------------------------------- #
    def _rows(self, sql: str, params: tuple[object, ...]) -> list[AliasRow]:
        return [
            AliasRow(
                entity_id=str(r["venue_id"]),
                alias=str(r["alias"]),
                normalized=str(r["normalized"]),
                provider=str(r["provider"]),
                is_ambiguous=False,
                curated=False,
            )
            for r in self._conn.execute(sql, params).fetchall()
        ]

    @staticmethod
    def _resolution(outcome, tier: str, score: float) -> Resolution:  # type: ignore[no-untyped-def]
        return Resolution(
            status=outcome.status,
            method=tier,
            score=score if outcome.status == MATCHED else 0.0,
            tier=tier,
            entity_id=outcome.entity_id,
            candidates=outcome.candidates,
            reason=outcome.reason,
            needs_review=outcome.status != MATCHED,
            via_ambiguous_alias=outcome.via_ambiguous_alias,
        )
