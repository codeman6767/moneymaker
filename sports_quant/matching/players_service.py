"""Local, network-free orchestration of canonical player matching (task §2).

Loads the unresolved ``provider_player_references`` for a bounded scope, resolves
each deterministically through :class:`PlayerResolver`, records exactly one
``entity_match_decisions`` row (plus normalized ``match_candidates`` children),
and links ``provider_player_references.player_id`` only after an accepted
decision. Ambiguous / unmatched outcomes are preserved for review; a canonical
player is never created from an unknown provider name; ``--dry-run`` persists
nothing.

**Schema limitation (documented, not hidden).** Provider player *names* are not
stored in any structured Phase D table -- only ``provider_player_id`` is (rosters,
lineups, probables, stats). So resolution matches the provider player identifier
through provider-scoped ``player_aliases`` (curated), using roster-derived
canonical team membership as the disambiguator. League scope is provable from
``players.league_id`` + the provider->league map; birth date is only ever a
supplied collision breaker and is never invented.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.matching import CandidateInput, SqliteMatchingRepository
from ..db.repositories.references import LinkOutcome, SqliteProviderReferenceRepository
from .model import MATCHER_VERSION, THRESHOLD, Resolution
from .players import PlayerResolver
from .service import _PROVIDER_LEAGUE, MatchCounters


@dataclass
class MatchPlayersResult:
    """Outcome of a ``match-players`` run (reuses the shared counters)."""

    dry_run: bool
    status: str = "succeeded"
    references_considered: int = 0
    counters: MatchCounters = field(default_factory=MatchCounters)
    run_id: Optional[str] = None

    @property
    def needs_failure_exit(self) -> bool:
        return self.counters.blocking_issues > 0


class MatchPlayersService:
    """Resolves unresolved provider-player references for a bounded scope."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        dry_run: bool = False,
        run_id: Optional[str] = None,
        matcher_version: str = MATCHER_VERSION,
    ) -> None:
        self._conn = conn
        self._dry_run = dry_run
        self._run_id = run_id
        self._version = matcher_version
        self._resolver = PlayerResolver(conn)
        self._match = SqliteMatchingRepository(conn)
        self._refs = SqliteProviderReferenceRepository(conn)
        self._dq = SqliteDataQualityRepository(conn)

    def match_range(
        self,
        *,
        provider: str,
        provider_player_id: Optional[str] = None,
        season_year: Optional[int] = None,
    ) -> MatchPlayersResult:
        result = MatchPlayersResult(dry_run=self._dry_run, run_id=self._run_id)
        league_code = _PROVIDER_LEAGUE.get(provider)
        if league_code is None:
            result.status = "failed"
            return result
        league_id = f"lg_{league_code.lower()}"

        refs = self._unresolved(provider, provider_player_id)
        result.references_considered = len(refs)
        for ppid in sorted(refs):  # deterministic order
            self._resolve_one(provider, ppid, league_id, season_year, result)
        result.status = "partially_failed" if result.needs_failure_exit else "succeeded"
        return result

    # -- one reference ------------------------------------------------------- #
    def _resolve_one(
        self, provider: str, provider_player_id: str, league_id: str,
        season_year: Optional[int], result: MatchPlayersResult,
    ) -> None:
        team_id = self._roster_team(provider, provider_player_id)
        res = self._resolver.resolve(
            provider=provider, provider_player_id=provider_player_id, league_id=league_id,
            team_id=team_id, season_year=season_year,
        )
        self._record(provider, provider_player_id, res, result)
        if res.scope_conflict:
            self._dq_issue(
                result, rule_code="DQ-MATCH-015", entity_id=provider_player_id, provider=provider,
                description="exact provider player link resolves into the wrong league",
            )
        elif res.is_matched:
            self._link(provider, provider_player_id, res, result)

    def _unresolved(self, provider: str, provider_player_id: Optional[str]) -> list[str]:
        sql = (
            "SELECT provider_player_id FROM provider_player_references "
            "WHERE provider = ? AND player_id IS NULL"
        )
        params: list[object] = [provider]
        if provider_player_id is not None:
            sql += " AND provider_player_id = ?"
            params.append(provider_player_id)
        return [str(r["provider_player_id"]) for r in self._conn.execute(sql, tuple(params))]

    def _roster_team(self, provider: str, provider_player_id: str) -> Optional[str]:
        """The canonical team the player most recently appeared under, if linked.

        Absence from any roster returns ``None`` -- never used as evidence
        against a player's identity, only as a positive disambiguator.
        """

        row = self._conn.execute(
            "SELECT ptr.team_id AS team_id FROM roster_snapshots rs "
            "JOIN provider_team_references ptr ON rs.team_ref_id = ptr.reference_id "
            "WHERE rs.provider = ? AND rs.provider_player_id = ? AND ptr.team_id IS NOT NULL "
            "ORDER BY rs.observed_at DESC, rs.roster_id DESC LIMIT 1",
            (provider, provider_player_id),
        ).fetchone()
        return None if row is None else str(row["team_id"])

    # -- decision + link ----------------------------------------------------- #
    def _record(
        self, provider: str, provider_player_id: str, res: Resolution, result: MatchPlayersResult
    ) -> None:
        outcome = "rejected" if res.scope_conflict else res.outcome()
        result.counters.decisions_evaluated += 1
        result.counters.candidates_recorded += len(res.candidates)
        if outcome == "accepted":
            result.counters.accepted += 1
        elif outcome == "ambiguous":
            result.counters.ambiguous += 1
        elif outcome == "rejected":
            result.counters.rejected += 1
        else:
            result.counters.no_candidate += 1
        if res.needs_review:
            result.counters.manual_review_required += 1
        if self._dry_run:
            return
        self._match.record_decision(
            entity_type="player", source_provider=provider, source_ref=provider_player_id,
            outcome=outcome, method=res.method, score=res.score, threshold=THRESHOLD,
            matcher_version=self._version,
            candidates=[
                CandidateInput(score=c.score, tier=c.tier, candidate_entity_id=c.entity_id,
                               method=c.method, evidence=c.evidence)
                for c in res.candidates
            ],
            matched_entity_id=res.entity_id,
            rejection_reason=None if outcome == "accepted" else (res.reason or "unresolved"),
            needs_manual_review=res.needs_review, run_id=self._run_id,
        )

    def _link(
        self, provider: str, provider_player_id: str, res: Resolution, result: MatchPlayersResult
    ) -> None:
        if self._dry_run or res.entity_id is None:
            return
        decisions = self._match.decisions_for_source(
            source_provider=provider, source_ref=provider_player_id, entity_type="player")
        decision_id = decisions[-1].match_id if decisions else ""
        _ref, outcome = self._refs.link_canonical(
            kind="player", provider=provider, provider_entity_id=provider_player_id,
            canonical_id=res.entity_id, match_decision_id=decision_id,
        )
        if outcome == LinkOutcome.LINKED:
            result.counters.provider_references_linked += 1

    def _dq_issue(
        self, result: MatchPlayersResult, *, rule_code: str, entity_id: str, provider: str,
        description: str,
    ) -> None:
        existing = self._conn.execute(
            "SELECT 1 FROM data_quality_issues WHERE rule_code = ? AND entity_type = 'player' "
            "AND entity_id IS ? AND provider IS ? AND resolved_at IS NULL LIMIT 1",
            (rule_code, entity_id, provider),
        ).fetchone()
        if existing is not None:
            return
        result.counters.dq_issues += 1
        result.counters.blocking_issues += 1
        if self._dry_run:
            return
        self._dq.record(
            severity="blocking", rule_code=rule_code, entity_type="player",
            description=description, entity_id=entity_id, provider=provider, run_id=self._run_id,
        )
