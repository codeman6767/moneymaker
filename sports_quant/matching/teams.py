"""Deterministic canonical team resolution (task §5, ENTITY_MATCHING.md §3.3).

Evidence tiers, strongest first:

1. ``exact_provider_id`` (1.00) -- the provider id is already linked to a team.
2. ``exact_alias`` (0.99) -- the raw string matches an alias verbatim,
   provider- and season-scoped.
3. ``normalized_alias`` (0.95) -- normalized forms match, provider-scoped.
4. ``normalized_alias_unscoped`` (0.90) -- normalized match ignoring provider.

Resolution stops at the first tier that yields candidates. One candidate ->
accept; two or more (or an ``is_ambiguous`` alias) -> refuse as AMBIGUOUS,
never fall through to a weaker tier. Zero across every tier -> UNMATCHED. A
canonical team is never invented from an unknown string.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from ..db.normalize import normalize_name
from ..db.schema import SEASON_UNBOUNDED_END, SEASON_UNBOUNDED_START
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
    Candidate,
    Resolution,
)
from .tiers import AliasRow, evaluate_pool


class TeamResolver:
    """Resolves a provider team reference to a canonical team, deterministically."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def resolve(
        self,
        *,
        provider: str,
        provider_team_id: str,
        league_id: str,
        raw_name: Optional[str] = None,
        season_year: Optional[int] = None,
    ) -> Resolution:
        raw = raw_name if raw_name is not None else provider_team_id

        # Tier 1 -- the provider id is already linked to a canonical team.
        linked = self._linked_team(provider, provider_team_id)
        if linked is not None:
            return Resolution(
                status=MATCHED,
                method=TIER_EXACT_PROVIDER_ID,
                score=SCORE_EXACT_PROVIDER_ID,
                tier=TIER_EXACT_PROVIDER_ID,
                entity_id=linked,
                candidates=(
                    Candidate(
                        entity_id=linked,
                        score=SCORE_EXACT_PROVIDER_ID,
                        tier=TIER_EXACT_PROVIDER_ID,
                        method=TIER_EXACT_PROVIDER_ID,
                        evidence=f"{provider}:{provider_team_id} already linked",
                    ),
                ),
                season_scoped=season_year is not None,
            )

        query = normalize_name(raw)
        if not query.normalized:
            return Resolution(
                status=UNMATCHED,
                method="none",
                score=0.0,
                tier="none",
                reason="empty team name after normalization",
                needs_review=True,
                season_scoped=season_year is not None,
            )

        rows = self._alias_rows(league_id, season_year)

        exact = [r for r in rows if r.alias == raw and (not provider or r.provider == provider)]
        scoped = (
            [r for r in rows if r.normalized == query.normalized and r.provider == provider]
            if provider
            else []
        )
        unscoped = [r for r in rows if r.normalized == query.normalized]

        tiers = [
            (exact, TIER_EXACT_ALIAS, SCORE_EXACT_ALIAS),
            (scoped, TIER_NORMALIZED_ALIAS, SCORE_NORMALIZED_ALIAS),
            (unscoped, TIER_NORMALIZED_ALIAS_UNSCOPED, SCORE_NORMALIZED_ALIAS_UNSCOPED),
        ]
        for pool, tier, score in tiers:
            outcome = evaluate_pool(pool, tier=tier, score=score, entity_label="team")
            if outcome is None:
                continue
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
                season_scoped=season_year is not None,
                season_validity_verified=season_year is not None and outcome.curated,
            )

        return Resolution(
            status=UNMATCHED,
            method="none",
            score=0.0,
            tier="none",
            reason=f"no team alias matches {query.normalized!r}",
            needs_review=True,
            season_scoped=season_year is not None,
        )

    # -- internals ----------------------------------------------------------- #
    def _linked_team(self, provider: str, provider_team_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT team_id FROM provider_team_references "
            "WHERE provider = ? AND provider_team_id = ?",
            (provider, provider_team_id),
        ).fetchone()
        if row is None:
            return None
        return None if row["team_id"] is None else str(row["team_id"])

    def _alias_rows(self, league_id: str, season_year: Optional[int]) -> list[AliasRow]:
        sql = (
            "SELECT team_id, alias, normalized, provider, is_ambiguous, "
            "valid_from_season, valid_to_season FROM team_aliases WHERE league_id = ?"
        )
        params: tuple[object, ...] = (league_id,)
        if season_year is not None:
            sql += " AND valid_from_season <= ? AND valid_to_season >= ?"
            params = (*params, season_year, season_year)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            AliasRow(
                entity_id=str(r["team_id"]),
                alias=str(r["alias"]),
                normalized=str(r["normalized"]),
                provider=str(r["provider"]),
                is_ambiguous=bool(r["is_ambiguous"]),
                curated=(
                    int(r["valid_from_season"]) != SEASON_UNBOUNDED_START
                    or int(r["valid_to_season"]) != SEASON_UNBOUNDED_END
                ),
            )
            for r in rows
        ]
