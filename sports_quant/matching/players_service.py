"""Local, network-free orchestration of canonical player matching (task §2).

Loads the unresolved ``provider_player_references`` for a bounded scope, resolves
each deterministically through :class:`PlayerResolver`, records exactly one
``entity_match_decisions`` row (plus normalized ``match_candidates`` children),
and links ``provider_player_references.player_id`` only after an accepted
decision. Ambiguous / unmatched outcomes are preserved for review; ``--dry-run``
persists nothing.

Canonical player creation (F1, e017)
------------------------------------
The rule is no longer "a canonical player is never created". It is:

* an UNKNOWN or NONOFFICIAL provider name never creates a canonical player -- not
  a sportsbook, not Kalshi, not an offline import, not a manually supplied
  string, not an unrecognised provider; and
* the league's DESIGNATED OFFICIAL provider's stable player id, together with a
  structured ``provider_player_identity_snapshots`` observation carrying a
  nonempty provider-written name, MAY bootstrap the canonical player -- under an
  explicit ``official_provider_bootstrap`` method scored 1.00, because identity
  is anchored by that permanent official id and not by a fuzzy name guess.

This is what closed the F1 pilot's 0% coverage. Before e017 the provider player
*names* existed only inside ``raw_responses`` bodies, so the candidate pool was
empty by construction and every reference correctly returned ``no_candidate``.
Alias/name evidence is still attempted FIRST; bootstrap is the last resort, an
AMBIGUOUS result never reaches it, and a name that lands on a canonical player
another id from the same provider already owns is treated as a same-name
collision rather than a discovery. Birth date remains a supplied collision
breaker and is never invented, and no career window is ever fabricated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..db.models import ProviderPlayerIdentity
from ..db.repositories.data_quality import SqliteDataQualityRepository
from ..db.repositories.identity import SqliteProviderIdentityRepository
from ..db.repositories.matching import (
    CandidateInput,
    DecisionWrite,
    SqliteMatchingRepository,
)
from ..db.repositories.players import SqlitePlayerAliasRepository, SqlitePlayerRepository
from ..db.repositories.references import LinkOutcome, SqliteProviderReferenceRepository
from .linkatomic import LinkAttempt, MatchLinkError, classify_link_attempt
from .model import (
    AMBIGUOUS,
    MATCHED,
    MATCHER_VERSION,
    SCORE_OFFICIAL_PROVIDER_BOOTSTRAP,
    THRESHOLD,
    TIER_EXACT_PROVIDER_ID,
    TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
    Candidate,
    Resolution,
)
from .players import PlayerResolver
from .season import league_code_from_id, season_bounds
from .service import _PROVIDER_LEAGUE, OFFICIAL_PROVIDER_BY_LEAGUE, MatchCounters


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
        self._identities = SqliteProviderIdentityRepository(conn)
        self._players = SqlitePlayerRepository(conn)
        self._aliases = SqlitePlayerAliasRepository(conn)
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
        team_id = self._roster_team(provider, provider_player_id, league_id, season_year)
        # e017: the structured provider-written name, bounded by nothing here
        # because match-players has no as-of cutoff; the newest observation is the
        # provider's current answer. Resolution order is unchanged and alias/name
        # evidence is always tried FIRST -- bootstrap is the last resort, never a
        # shortcut past an existing canonical player.
        identity = self._identities.latest_player(provider, provider_player_id)
        res = self._resolver.resolve(
            provider=provider, provider_player_id=provider_player_id, league_id=league_id,
            raw_name=identity.full_name if identity is not None else None,
            team_id=team_id,
            birth_date=identity.birth_date if identity is not None else None,
            season_year=season_year,
        )
        raw_id = self._reference_raw_id(provider, provider_player_id)
        if res.scope_conflict:
            self._record(provider, provider_player_id, res, raw_id, result)
            self._dq_issue(
                result, rule_code="DQ-MATCH-015", entity_id=provider_player_id, provider=provider,
                description="exact provider player link resolves into the wrong league",
            )
            return
        # An official provider's player ids are distinct identities BY CONSTRUCTION:
        # one stable id is one person. So a name-based match onto a canonical player
        # that another id from the SAME provider already owns is a same-name
        # collision, not a discovery -- two Will Smiths must stay two people. The
        # bootstrap's own provider-scoped alias makes this reachable, so the check
        # lives here rather than being left to chance.
        if (
            res.is_matched
            and res.tier != TIER_EXACT_PROVIDER_ID
            and res.entity_id is not None
            and self._claimed_by_another_provider_id(
                provider, provider_player_id, res.entity_id)
        ):
            res = self._collision(provider, provider_player_id, res, season_year)

        if not res.is_matched:
            # An AMBIGUOUS result is a refusal, never a licence to create a second
            # player: two existing candidates mean the evidence is insufficient, so
            # bootstrap is only ever attempted when NOTHING resolved.
            bootstrap = None
            if res.status != AMBIGUOUS and not res.scope_conflict:
                bootstrap = self._bootstrap_official_player(
                    provider, provider_player_id, league_id, identity, raw_id, result)
            if bootstrap is None:
                # Ambiguous / no-candidate / rejected: recorded normally, never links.
                self._record(provider, provider_player_id, res, raw_id, result)
            return

        # Matched (accepted): record the accepted decision and apply the link as
        # one atomic unit (task §7). The player matcher only processes references
        # with a NULL link (`_unresolved`), so a clean link is the normal path;
        # the replay/conflict branches are defensive against a corrupt or
        # already-linked reference (a non-LINKED result raises and rolls back).
        assert res.entity_id is not None  # noqa: S101 - is_matched implies an entity
        if not self._dry_run:
            ref = self._refs.get("player", provider, provider_player_id)
            current = ref.canonical_id if ref is not None else None
            attempt = classify_link_attempt(
                current_canonical_id=current, proposed_canonical_id=res.entity_id,
                decision_valid=self._player_decision_valid(
                    provider, provider_player_id,
                    ref.match_decision_id if ref is not None else None, res.entity_id),
            )
            if attempt is LinkAttempt.REPLAY:
                result.counters.already_linked += 1
                return
            if attempt is LinkAttempt.CONFLICT:
                self._dq_issue(
                    result, rule_code="DQ-MATCH-016", entity_id=provider_player_id,
                    provider=provider,
                    description="provider player already linked to a different canonical player",
                )
                self._record_link_conflict(provider, provider_player_id, current, raw_id, result)
                return
        decision_id = self._record(provider, provider_player_id, res, raw_id, result)
        self._link(provider, provider_player_id, res, decision_id, result)

    def _claimed_by_another_provider_id(
        self, provider: str, provider_player_id: str, player_id: str
    ) -> bool:
        """Whether a DIFFERENT id from the same provider already owns this player."""

        row = self._conn.execute(
            "SELECT 1 FROM provider_player_references WHERE provider = ? "
            "AND player_id = ? AND provider_player_id <> ? LIMIT 1",
            (provider, player_id, provider_player_id),
        ).fetchone()
        return row is not None

    def _collision(
        self, provider: str, provider_player_id: str, res: Resolution,
        season_year: Optional[int],
    ) -> Resolution:
        """Turn a same-provider identity collision into an explicit refusal.

        The candidate is preserved on the decision so a reviewer can see exactly
        which canonical player the name pointed at and why it was not taken.
        """

        return Resolution(
            status=AMBIGUOUS,
            method=res.method,
            score=0.0,
            tier=res.tier,
            candidates=res.candidates,
            reason=(
                f"name matches canonical player {res.entity_id} which another "
                f"{provider} player id already owns; two distinct official ids are "
                "two identities without stronger evidence"
            ),
            needs_review=True,
            season_scoped=season_year is not None,
        )

    # -- official-provider canonical bootstrap (F1, e017) -------------------- #
    def _bootstrap_official_player(
        self,
        provider: str,
        provider_player_id: str,
        league_id: str,
        identity: Optional[ProviderPlayerIdentity],
        raw_id: Optional[str],
        result: MatchPlayersResult,
    ) -> Optional[str]:
        """Create the canonical player for a DESIGNATED OFFICIAL provider id.

        This is the narrow, deliberate exception to "a canonical player is never
        created from a provider name". The distinction that makes it safe is that
        identity here is anchored on the official provider's own *stable id*,
        which is why the decision scores 1.00 under
        ``official_provider_bootstrap`` rather than borrowing a name tier's score.

        Every precondition must hold, and each one is a real refusal, not a
        formality:

        * the provider is the league's designated official source -- a
          sportsbook, Kalshi, an offline import or an unknown provider can never
          create a canonical identity;
        * a structured identity observation exists with a nonempty
          provider-written full name (never a name inferred from the id);
        * the league is known;
        * the reference exists and is not already linked (a link to a different
          player is a blocking conflict handled by the caller's link classifier).

        Returns the created ``player_id``, or ``None`` when bootstrap does not
        apply -- in which case the caller records the ordinary refusal, so the
        honest no-candidate path is preserved rather than replaced.

        Creation, alias, provider link and accepted decision are applied inside
        the caller's transaction, so a failure in any one rolls back all four.
        """

        if OFFICIAL_PROVIDER_BY_LEAGUE.get(league_id) != provider:
            return None
        if identity is None or not identity.full_name.strip():
            return None
        if not provider_player_id.strip():
            return None
        ref = self._refs.get("player", provider, provider_player_id)
        if ref is None or ref.canonical_id is not None:
            # No reference to link, or already linked: the caller's conflict and
            # replay paths own those cases.
            return None

        result.counters.decisions_evaluated += 1
        result.counters.accepted += 1
        result.counters.candidates_recorded += 1
        if self._dry_run:
            # Report the would-be creation truthfully; write nothing.
            result.counters.canonical_players_created += 1
            result.counters.provider_references_linked += 1
            return "dry-run"

        player = self._players.create(
            league_id=league_id,
            full_name=identity.full_name,
            # Only provider-supplied parts. MLB StatsAPI sends no name parts, so
            # these stay NULL rather than being split out of the full name.
            first_name=identity.first_name,
            last_name=identity.last_name,
            suffix=identity.suffix or None,
            birth_date=identity.birth_date,
            primary_position=identity.position,
            # debut_date / final_game_date are deliberately NOT set: a career
            # window is not observable from one identity snapshot, and inventing
            # one would silently corrupt every season-scoped query later.
        )
        result.counters.canonical_players_created += 1
        # A provider-scoped alias from the EXACT provider-written string, so the
        # next run resolves by alias evidence rather than re-bootstrapping.
        self._aliases.add(
            player_id=player.player_id, league_id=league_id, alias=identity.full_name,
            alias_type="provider", provider=provider, source="provider_observed",
        )
        decision = self._match.record_decision(
            entity_type="player", source_provider=provider, source_ref=provider_player_id,
            outcome="accepted", method=TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
            score=SCORE_OFFICIAL_PROVIDER_BOOTSTRAP, threshold=THRESHOLD,
            matcher_version=self._version,
            candidates=[CandidateInput(
                score=SCORE_OFFICIAL_PROVIDER_BOOTSTRAP,
                tier=TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
                candidate_entity_id=player.player_id,
                method=TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
                # Provenance: which official id, and which identity observation.
                evidence=(
                    f"official provider {provider} id {provider_player_id}; "
                    f"identity {identity.identity_id} observed {identity.observed_at}"
                ),
            )],
            matched_entity_id=player.player_id, rejection_reason=None,
            needs_manual_review=False, run_id=self._run_id,
            raw_response_id=identity.raw_response_id or raw_id,
        )
        bootstrap_res = Resolution(
            status=MATCHED, method=TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
            score=SCORE_OFFICIAL_PROVIDER_BOOTSTRAP,
            tier=TIER_OFFICIAL_PROVIDER_BOOTSTRAP, entity_id=player.player_id,
            candidates=(Candidate(
                entity_id=player.player_id, score=SCORE_OFFICIAL_PROVIDER_BOOTSTRAP,
                tier=TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
                method=TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
                evidence=f"bootstrapped from {provider}:{provider_player_id}",
            ),),
        )
        self._link(provider, provider_player_id, bootstrap_res, decision.match_id, result)
        return player.player_id

    def _player_decision_valid(
        self, provider: str, provider_player_id: str, decision_id: Optional[str], player_id: str
    ) -> bool:
        if decision_id is None:
            return False
        d = self._match.get(decision_id)
        return (
            d is not None
            and d.entity_type == "player"
            and d.source_provider == provider
            and d.source_ref == provider_player_id
            and d.outcome == "accepted"
            and d.matched_entity_id == player_id
        )

    def _record_link_conflict(
        self, provider: str, provider_player_id: str, current: Optional[str],
        raw_id: Optional[str], result: MatchPlayersResult,
    ) -> None:
        """Record a blocking rejected decision for a corrupt/conflicting link."""

        result.counters.decisions_evaluated += 1
        result.counters.rejected += 1
        result.counters.manual_review_required += 1
        if self._dry_run:
            return
        self._match.record_decision(
            entity_type="player", source_provider=provider, source_ref=provider_player_id,
            outcome="rejected", method="conflict", score=0.0, threshold=THRESHOLD,
            matcher_version=self._version,
            candidates=[CandidateInput(score=0.0, tier="conflict", candidate_entity_id=current)],
            matched_entity_id=None,
            rejection_reason="provider player already linked to a different canonical player",
            needs_manual_review=True, run_id=self._run_id, raw_response_id=raw_id,
        )

    def _reference_raw_id(self, provider: str, provider_player_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT current_raw_response_id FROM provider_player_references "
            "WHERE provider = ? AND provider_player_id = ?",
            (provider, provider_player_id),
        ).fetchone()
        return None if row is None else str(row["current_raw_response_id"])

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

    def _roster_team(
        self, provider: str, provider_player_id: str, league_id: str,
        season_year: Optional[int],
    ) -> Optional[str]:
        """The canonical team the provider player is on, as season-valid evidence.

        When ``--season`` is supplied, only roster observations dated within that
        league's season *interval* are considered (MLB calendar year; NBA
        July-to-June, spanning two calendar years -- see ``season.season_bounds``),
        so a traded player's newer team cannot resolve an earlier-season reference.
        If the season's rosters name **more than one** distinct team (a genuine
        mid-season conflict), ``None`` is returned so the team tier is omitted
        rather than chosen by row order. Absence from any roster returns ``None``
        (never evidence against the player).
        """

        sql = (
            "SELECT DISTINCT ptr.team_id AS team_id FROM roster_snapshots rs "
            "JOIN provider_team_references ptr ON rs.team_ref_id = ptr.reference_id "
            "WHERE rs.provider = ? AND rs.provider_player_id = ? AND ptr.team_id IS NOT NULL"
        )
        params: list[object] = [provider, provider_player_id]
        if season_year is not None:
            lo, hi = season_bounds(league_code_from_id(league_id), season_year)
            sql += " AND rs.roster_date IS NOT NULL AND rs.roster_date >= ? AND rs.roster_date <= ?"
            params.extend((lo, hi))
        teams = sorted({str(r["team_id"]) for r in self._conn.execute(sql, tuple(params))})
        return teams[0] if len(teams) == 1 else None

    # -- decision + link ----------------------------------------------------- #
    def _record(
        self, provider: str, provider_player_id: str, res: Resolution,
        raw_response_id: Optional[str], result: MatchPlayersResult,
    ) -> Optional[str]:
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
            result.counters.decisions_recorded += 1
            return None
        common = {
            "entity_type": "player", "source_provider": provider,
            "source_ref": provider_player_id, "outcome": outcome, "method": res.method,
            "score": res.score, "threshold": THRESHOLD,
            "matcher_version": self._version,
            "candidates": [
                CandidateInput(score=c.score, tier=c.tier, candidate_entity_id=c.entity_id,
                               method=c.method, evidence=c.evidence)
                for c in res.candidates
            ],
            "matched_entity_id": res.entity_id,
            "rejection_reason": (
                None if outcome == "accepted" else (res.reason or "unresolved")),
            "needs_manual_review": res.needs_review, "run_id": self._run_id,
            "raw_response_id": raw_response_id,
        }
        if outcome == "accepted":
            decision = self._match.record_decision(**common)  # type: ignore[arg-type]
            result.counters.decisions_recorded += 1
            return decision.match_id
        decision, write = self._match.record_unresolved_decision(
            **common)  # type: ignore[arg-type]
        if write is DecisionWrite.REPLAY:
            result.counters.decisions_replayed += 1
        elif write is DecisionWrite.CHANGED:
            result.counters.decisions_changed += 1
        else:
            result.counters.decisions_recorded += 1
        return decision.match_id

    def _link(
        self, provider: str, provider_player_id: str, res: Resolution,
        decision_id: Optional[str], result: MatchPlayersResult,
    ) -> None:
        # Links to the exact decision from THIS attempt, not a "latest decision"
        # lookup that a same-timestamp sibling could win.
        if self._dry_run or res.entity_id is None or decision_id is None:
            return
        _ref, outcome = self._refs.link_canonical(
            kind="player", provider=provider, provider_entity_id=provider_player_id,
            canonical_id=res.entity_id, match_decision_id=decision_id,
        )
        if outcome == LinkOutcome.LINKED:
            result.counters.provider_references_linked += 1
            return
        # The pre-check verified a NULL link, so any other result is a
        # concurrent/corrupt state; raise so the run rolls back rather than commit
        # an accepted decision without its link (task §7).
        raise MatchLinkError(
            f"player link for {provider}:{provider_player_id} -> {res.entity_id} returned "
            f"{outcome.value}; expected a clean LINKED"
        )

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
