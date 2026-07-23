"""Entity-match decision repository (decisions + normalized candidates).

Every match attempt writes exactly one ``entity_match_decisions`` row plus one
``match_candidates`` row per candidate considered (including the losers), in one
transaction. Candidates are a normalized child table, never a JSON blob. The
decision is append-only except its review columns (enforced by a d009 trigger);
D1 provides the storage, D5 the matchers that call it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Protocol

from ..engine import transaction
from ..ids import new_match_candidate_id, new_match_decision_id
from ..models import MatchCandidate, MatchDecision
from ..schema import MATCH_ENTITY_TYPES, MATCH_OUTCOMES, utc_now_iso
from .base import Repository, RepositoryError, to_db_bool


@dataclass(frozen=True)
class CandidateInput:
    """One candidate to record under a decision (score/tier/evidence)."""

    score: float
    tier: str
    candidate_entity_id: Optional[str] = None
    method: Optional[str] = None
    evidence: Optional[str] = None


class MatchingRepositoryProtocol(Protocol):
    def record_decision(
        self,
        *,
        entity_type: str,
        source_provider: str,
        source_ref: str,
        outcome: str,
        method: str,
        score: float,
        threshold: float,
        matcher_version: str,
        candidates: list[CandidateInput],
        **fields: object,
    ) -> MatchDecision: ...


class SqliteMatchingRepository(Repository):
    """Match-decision + candidate storage."""

    _DECISION_COLUMNS = (
        "match_id, entity_type, source_provider, source_ref, matched_entity_id, outcome, "
        "method, score, threshold, rejection_reason, needs_manual_review, reviewed_by, "
        "reviewed_at, matcher_version, raw_response_id, run_id, decided_at, created_at"
    )
    _CANDIDATE_COLUMNS = (
        "candidate_id, match_id, candidate_entity_id, score, tier, method, evidence, rank, "
        "created_at"
    )

    def record_decision(
        self,
        *,
        entity_type: str,
        source_provider: str,
        source_ref: str,
        outcome: str,
        method: str,
        score: float,
        threshold: float,
        matcher_version: str,
        candidates: list[CandidateInput],
        matched_entity_id: Optional[str] = None,
        rejection_reason: Optional[str] = None,
        needs_manual_review: bool = False,
        raw_response_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> MatchDecision:
        """Record one decision + its candidates atomically.

        Enforces the schema-level rules in Python too: an accepted decision must
        name an entity; a non-accepted one must state a reason. Candidates are
        written in the caller's order (rank 0, 1, ...), deterministically.
        """

        if entity_type not in MATCH_ENTITY_TYPES:
            raise RepositoryError(f"unknown match entity_type {entity_type!r}")
        if outcome not in MATCH_OUTCOMES:
            raise RepositoryError(f"unknown match outcome {outcome!r}")
        if outcome == "accepted" and matched_entity_id is None:
            raise RepositoryError("an accepted match decision must name an entity")
        if outcome != "accepted" and rejection_reason is None:
            raise RepositoryError("a non-accepted match decision must give a rejection_reason")

        match_id = new_match_decision_id()
        now = utc_now_iso()
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO entity_match_decisions "
                "(match_id, entity_type, source_provider, source_ref, matched_entity_id, "
                " outcome, method, score, threshold, rejection_reason, needs_manual_review, "
                " reviewed_by, reviewed_at, matcher_version, raw_response_id, run_id, "
                " decided_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)",
                (
                    match_id, entity_type, source_provider, source_ref, matched_entity_id,
                    outcome, method, score, threshold, rejection_reason,
                    to_db_bool(needs_manual_review), matcher_version, raw_response_id, run_id,
                    now, now,
                ),
            )
            for rank, cand in enumerate(candidates):
                self._conn.execute(
                    "INSERT INTO match_candidates "
                    "(candidate_id, match_id, candidate_entity_id, score, tier, method, "
                    " evidence, rank, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_match_candidate_id(), match_id, cand.candidate_entity_id, cand.score,
                        cand.tier, cand.method, cand.evidence, rank, now,
                    ),
                )
        decision = self.get(match_id)
        assert decision is not None  # noqa: S101
        return decision

    def mark_reviewed(
        self, match_id: str, *, reviewed_by: str, needs_manual_review: bool = False
    ) -> MatchDecision:
        """Set the review columns (the only permitted decision mutation)."""

        now = utc_now_iso()
        self._conn.execute(
            "UPDATE entity_match_decisions SET needs_manual_review = ?, reviewed_by = ?, "
            "reviewed_at = ? WHERE match_id = ?",
            (to_db_bool(needs_manual_review), reviewed_by, now, match_id),
        )
        decision = self.get(match_id)
        if decision is None:
            raise RepositoryError(f"match decision {match_id!r} not found")
        return decision

    def get(self, match_id: str) -> Optional[MatchDecision]:
        row = self._fetch_one(
            f"SELECT {self._DECISION_COLUMNS} FROM entity_match_decisions WHERE match_id = ?",
            (match_id,),
        )
        return None if row is None else self._to_decision(row)

    def candidates(self, match_id: str) -> list[MatchCandidate]:
        return [
            self._to_candidate(r)
            for r in self._fetch_all(
                f"SELECT {self._CANDIDATE_COLUMNS} FROM match_candidates WHERE match_id = ? "
                "ORDER BY rank",
                (match_id,),
            )
        ]

    def list_needs_review(
        self, *, entity_type: Optional[str] = None, limit: int = 100
    ) -> list[MatchDecision]:
        if entity_type is None:
            rows = self._fetch_all(
                f"SELECT {self._DECISION_COLUMNS} FROM entity_match_decisions "
                "WHERE needs_manual_review = 1 ORDER BY decided_at, match_id LIMIT ?",
                (limit,),
            )
        else:
            rows = self._fetch_all(
                f"SELECT {self._DECISION_COLUMNS} FROM entity_match_decisions "
                "WHERE needs_manual_review = 1 AND entity_type = ? "
                "ORDER BY decided_at, match_id LIMIT ?",
                (entity_type, limit),
            )
        return [self._to_decision(r) for r in rows]

    def count(self) -> int:
        return self._count("SELECT COUNT(*) FROM entity_match_decisions")

    def count_candidates(self) -> int:
        return self._count("SELECT COUNT(*) FROM match_candidates")

    def _to_decision(self, row: sqlite3.Row) -> MatchDecision:
        return MatchDecision(
            match_id=str(row["match_id"]),
            entity_type=str(row["entity_type"]),
            source_provider=str(row["source_provider"]),
            source_ref=str(row["source_ref"]),
            outcome=str(row["outcome"]),
            method=str(row["method"]),
            score=float(row["score"]),
            threshold=float(row["threshold"]),
            matcher_version=str(row["matcher_version"]),
            decided_at=str(row["decided_at"]),
            created_at=str(row["created_at"]),
            matched_entity_id=self._opt_str(row, "matched_entity_id"),
            rejection_reason=self._opt_str(row, "rejection_reason"),
            needs_manual_review=bool(row["needs_manual_review"]),
            reviewed_by=self._opt_str(row, "reviewed_by"),
            reviewed_at=self._opt_str(row, "reviewed_at"),
            raw_response_id=self._opt_str(row, "raw_response_id"),
            run_id=self._opt_str(row, "run_id"),
        )

    def _to_candidate(self, row: sqlite3.Row) -> MatchCandidate:
        return MatchCandidate(
            candidate_id=str(row["candidate_id"]),
            match_id=str(row["match_id"]),
            score=float(row["score"]),
            tier=str(row["tier"]),
            rank=int(row["rank"]),
            created_at=str(row["created_at"]),
            candidate_entity_id=self._opt_str(row, "candidate_entity_id"),
            method=self._opt_str(row, "method"),
            evidence=self._opt_str(row, "evidence"),
        )
