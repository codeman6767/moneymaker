"""Deterministic canonical player resolution (task §6, ENTITY_MATCHING.md §3.1).

Evidence order, strongest first:

1. ``exact_provider_id`` (1.00) -- the provider player id is already linked.
2. ``team_normalized_name`` (0.95) -- canonical team membership + normalized name.
3. ``league_normalized_name`` (0.90) -- league + normalized name.
4. birth date is used only as a *collision breaker* when genuinely supplied.

A suffix present in the input is **binding** (``Guerrero Jr.`` never resolves to
the father); an absent suffix is **permissive** only when exactly one candidate
survives. Two same-name players stay ambiguous unless structured evidence (team
or an actually-supplied birth date) resolves them. Birth dates are never
invented; a season filter uses the players' career window when the season is
known. A canonical player is never created from a provider name.

This mirrors -- and shares the normalizer with -- ``intel.player_matching``; the
older module keeps its ``MATCHED / AMBIGUOUS / UNMATCHED`` contract but now
normalizes through ``db.normalize`` so the two never diverge.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..db.normalize import NO_SUFFIX, normalize_name
from .model import (
    AMBIGUOUS,
    MATCHED,
    SCORE_EXACT_PROVIDER_ID,
    SCORE_LEAGUE_NAME,
    SCORE_TEAM_NAME,
    TIER_EXACT_PROVIDER_ID,
    TIER_LEAGUE_NAME,
    TIER_TEAM_NAME,
    UNMATCHED,
    Candidate,
    Resolution,
)


@dataclass(frozen=True)
class _PlayerRow:
    player_id: str
    normalized: str
    suffix: str
    provider: str
    is_ambiguous: bool


class PlayerResolver:
    """Resolves a provider player reference to a canonical player."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def resolve(
        self,
        *,
        provider: str,
        provider_player_id: str,
        league_id: str,
        raw_name: Optional[str] = None,
        team_id: Optional[str] = None,
        birth_date: Optional[str] = None,
        season_year: Optional[int] = None,
    ) -> Resolution:
        raw = raw_name if raw_name is not None else provider_player_id

        linked = self._linked_player(provider, provider_player_id)
        if linked is not None:
            linked_id, linked_league = linked
            if linked_league is not None and linked_league != league_id:
                return Resolution(
                    status=UNMATCHED,
                    method=TIER_EXACT_PROVIDER_ID,
                    score=0.0,
                    tier=TIER_EXACT_PROVIDER_ID,
                    candidates=(
                        Candidate(
                            entity_id=linked_id, score=0.0, tier=TIER_EXACT_PROVIDER_ID,
                            method=TIER_EXACT_PROVIDER_ID,
                            evidence=f"linked player is in league {linked_league}",
                        ),
                    ),
                    reason=(
                        f"exact provider link {provider}:{provider_player_id} resolves to player "
                        f"{linked_id} in league {linked_league}, not requested {league_id}"
                    ),
                    needs_review=True,
                    scope_conflict=True,
                    season_scoped=season_year is not None,
                )
            return Resolution(
                status=MATCHED,
                method=TIER_EXACT_PROVIDER_ID,
                score=SCORE_EXACT_PROVIDER_ID,
                tier=TIER_EXACT_PROVIDER_ID,
                entity_id=linked_id,
                candidates=(
                    Candidate(
                        entity_id=linked_id,
                        score=SCORE_EXACT_PROVIDER_ID,
                        tier=TIER_EXACT_PROVIDER_ID,
                        method=TIER_EXACT_PROVIDER_ID,
                        evidence=f"{provider}:{provider_player_id} already linked",
                    ),
                ),
                season_scoped=season_year is not None,
            )

        query = normalize_name(raw)
        if not query.normalized:
            return self._unmatched("empty player name after normalization")

        rows = self._name_rows(league_id, query.normalized, provider)
        scope = "neutral" if all(r.provider == "" for r in rows) else provider
        # Suffix in the input is binding; absent suffix is permissive.
        if query.suffix != NO_SUFFIX:
            rows = [r for r in rows if r.suffix == query.suffix]

        # Any is_ambiguous alias forces refusal even with a single row.
        if rows and any(r.is_ambiguous for r in rows):
            ids = sorted({r.player_id for r in rows})
            return self._ambiguous(
                ids, TIER_LEAGUE_NAME, "player alias is flagged ambiguous", via_ambiguous=True
            )

        candidate_ids = sorted({r.player_id for r in rows})
        candidate_ids = self._season_filter(candidate_ids, season_year)

        if not candidate_ids:
            return self._unmatched(f"no player matches {query.normalized!r}")

        # Tier 2 -- disambiguate by season-valid canonical team membership.
        tier = TIER_LEAGUE_NAME
        score = SCORE_LEAGUE_NAME
        if len(candidate_ids) > 1 and team_id is not None:
            on_team = self._on_team(candidate_ids, team_id, season_year)
            if len(on_team) == 1:
                candidate_ids = on_team
                tier, score = TIER_TEAM_NAME, SCORE_TEAM_NAME
            elif len(on_team) > 1:
                candidate_ids = on_team
        elif (
            len(candidate_ids) == 1
            and team_id is not None
            and self._on_team(candidate_ids, team_id, season_year)
        ):
            tier, score = TIER_TEAM_NAME, SCORE_TEAM_NAME

        # Birth date is only ever a collision breaker, never invented.
        if len(candidate_ids) > 1 and birth_date:
            by_birth = self._birth_filter(candidate_ids, birth_date)
            if len(by_birth) == 1:
                candidate_ids = by_birth

        if len(candidate_ids) == 1:
            cid = candidate_ids[0]
            return Resolution(
                status=MATCHED,
                method=tier,
                score=score,
                tier=tier,
                entity_id=cid,
                candidates=(
                    Candidate(
                        entity_id=cid, score=score, tier=tier, method=tier,
                        evidence=f"provider scope={scope}",
                    ),
                ),
                season_scoped=season_year is not None,
            )

        return self._ambiguous(
            candidate_ids,
            tier,
            f"{len(candidate_ids)} players share the name; needs team or birth-date evidence",
        )

    # -- internals ----------------------------------------------------------- #
    def _unmatched(self, reason: str) -> Resolution:
        return Resolution(
            status=UNMATCHED, method="none", score=0.0, tier="none",
            reason=reason, needs_review=True,
        )

    def _ambiguous(
        self, ids: list[str], tier: str, reason: str, *, via_ambiguous: bool = False
    ) -> Resolution:
        return Resolution(
            status=AMBIGUOUS,
            method=tier,
            score=0.0,
            tier=tier,
            candidates=tuple(
                Candidate(entity_id=i, score=0.0, tier=tier, method=tier) for i in sorted(ids)
            ),
            reason=reason,
            needs_review=True,
            via_ambiguous_alias=via_ambiguous,
        )

    def _linked_player(
        self, provider: str, provider_player_id: str
    ) -> Optional[tuple[str, Optional[str]]]:
        """The linked ``(player_id, league_id)``, or ``None`` when unlinked.

        ``players.league_id`` is the strongest reliable scope the schema proves
        for a player, so it is what a crosswalk is validated against.
        """

        row = self._conn.execute(
            "SELECT ppr.player_id AS player_id, p.league_id AS league_id "
            "FROM provider_player_references ppr "
            "LEFT JOIN players p ON ppr.player_id = p.player_id "
            "WHERE ppr.provider = ? AND ppr.provider_player_id = ?",
            (provider, provider_player_id),
        ).fetchone()
        if row is None or row["player_id"] is None:
            return None
        league = None if row["league_id"] is None else str(row["league_id"])
        return str(row["player_id"]), league

    def _name_rows(self, league_id: str, normalized: str, provider: str) -> list[_PlayerRow]:
        """Alias rows for the name, scoped to the resolving provider.

        Only the resolving provider's own aliases and intentionally
        provider-neutral (``provider = ''``) aliases are candidates; a different
        provider's alias is never used, so two providers that happen to share a
        normalized alias text or provider-id value cannot cross-match (provider
        scope is enforced here, before any team / season / suffix / birth-date
        disambiguation). League scope is enforced by the ``league_id`` filter.
        """

        rows = self._conn.execute(
            "SELECT player_id, normalized, suffix, provider, is_ambiguous "
            "FROM player_aliases WHERE league_id = ? AND normalized = ? "
            "AND provider IN (?, '') ORDER BY player_id, provider",
            (league_id, normalized, provider),
        ).fetchall()
        return [
            _PlayerRow(
                player_id=str(r["player_id"]),
                normalized=str(r["normalized"]),
                suffix=str(r["suffix"]),
                provider=str(r["provider"]),
                is_ambiguous=bool(r["is_ambiguous"]),
            )
            for r in rows
        ]

    def _season_filter(self, ids: list[str], season_year: Optional[int]) -> list[str]:
        """Keep players whose career window includes the season (when known).

        A player with no curated debut/final date is kept -- an absent window is
        not evidence of absence.
        """

        if season_year is None or not ids:
            return ids
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT player_id, debut_date, final_game_date FROM players "  # noqa: S608
            f"WHERE player_id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        kept: list[str] = []
        for r in rows:
            debut = r["debut_date"]
            final = r["final_game_date"]
            debut_ok = debut is None or int(str(debut)[:4]) <= season_year
            final_ok = final is None or int(str(final)[:4]) >= season_year
            if debut_ok and final_ok:
                kept.append(str(r["player_id"]))
        return sorted(kept)

    def _on_team(
        self, ids: list[str], team_id: str, season_year: Optional[int] = None
    ) -> list[str]:
        """Which candidate players were on ``team_id`` -- season-restricted.

        When ``season_year`` is supplied, only roster observations dated within
        that season count, so a later-season (e.g. post-trade) roster cannot
        resolve an earlier-season reference. An undated roster is not accepted as
        season-valid evidence. Absence from a roster is never evidence against a
        player. Deterministically ordered.
        """

        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT DISTINCT rs.player_id FROM roster_snapshots rs "
            "JOIN provider_team_references ptr ON rs.team_ref_id = ptr.reference_id "
            f"WHERE ptr.team_id = ? AND rs.player_id IN ({placeholders})"  # noqa: S608
        )
        params: list[object] = [team_id, *ids]
        if season_year is not None:
            sql += " AND rs.roster_date LIKE ?"
            params.append(f"{season_year}-%")
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return sorted(str(r["player_id"]) for r in rows)

    def _birth_filter(self, ids: list[str], birth_date: str) -> list[str]:
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT player_id FROM players WHERE birth_date = ? AND player_id IN ({placeholders})",  # noqa: S608
            (birth_date, *ids),
        ).fetchall()
        return sorted(str(r["player_id"]) for r in rows)
