"""The production corpus-scoped G5 identity audit engine.

Implements the contract closed in `G5_PROVIDER_ID_STABILITY_REVIEW.md`: *within
this exact corpus*, is every observation of
``(league, provider, namespace_generation, entity_type, provider_id)``
compatible with one canonical entity?

The audit is a **closed-world** statement. It says nothing about identifiers
outside the corpus it scanned, because neither BALLDONTLIE nor MLB StatsAPI
documents global permanent non-reuse and no amount of scanning could establish
it. That is why every audit record binds the exact ``source_corpus_digest`` it
read: a clean one-month result never transfers to a wider window.

Shape of the run
----------------
1. Read the audited subset read-only (``sources``).
2. Build the **complete** conclusion in memory -- every finding, every count.
3. Reconcile: counts must equal what the findings actually say (§11).
4. Persist the summary and all findings in ONE transaction, or nothing.

Step 3 exists because the schema review deliberately left one completeness gap:
the database can store ``collision_count = 5`` on a rejected audit that carries
no collision findings. Closing that needs a deferred constraint SQLite does not
have, or a mutable sealing flag that would weaken the append-only guarantee --
so the *engine* guarantees it instead, and refuses to persist an audit whose
summary disagrees with its own evidence.

What the audit may never read
-----------------------------
Final scores, winners, any result or box-score outcome, and any match decision.
Those are downstream conclusions; using one as identity evidence would let a
fact from after the decision point define who the participants were.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Optional

from ..db.repositories.retrospective import SqliteRetrospectiveProvenanceRepository
from .provenance import (
    AuditVerdict,
    EntityType,
    ExclusionScope,
    FindingClassification,
    FindingSeverity,
    ProviderNamespace,
    RetrospectiveProvenanceError,
    semantic_digest,
)
from .sources import (
    GameObservation,
    PlayerObservation,
    TeamObservation,
    observations_for,
    require_provider_league,
)

__all__ = [
    "AUDIT_POLICY_VERSION",
    "AuditPlan",
    "IdentityAuditError",
    "PlannedFinding",
    "audit_namespace",
    "persist_audit_plan",
]


class IdentityAuditError(RetrospectiveProvenanceError):
    """The audit cannot be completed or its conclusion cannot be trusted."""


#: The exact compatibility rules implemented below. Changing ANY criterion --
#: which fields are identity-defining, which mutations are lawful, what counts as
#: a flag -- requires a new version, because an audit record's digest binds this
#: string and old records must never be silently reinterpreted.
#:
#: v2 (independent review, 2026-08-13). v1 was materially under-powered:
#:   * a game id reused for the SAME matchup on another date, and a game id
#:     reused across both halves of a doubleheader, were both reported clean;
#:   * `reschedule_info` was bound into the source digest but never reached the
#:     compatibility rules, so a postponement and a reuse looked identical;
#:   * an empty namespace was reported as an ACCEPTED clean audit;
#:   * detection power was never recorded, so "zero collisions" read as
#:     "identity verified" when for games it meant "nothing was comparable".
AUDIT_POLICY_VERSION: Final = "g5-identity-audit-v2"

# -- Finding codes. Stable strings; each documented in the implementation report.
GAME_INCOMPATIBLE_EVENT: Final = "GAME_ID_TWO_DIFFERENT_EVENTS"
GAME_LEGITIMATE_MUTATION: Final = "GAME_ID_LAWFUL_MUTATION"
GAME_INSUFFICIENT: Final = "GAME_ID_INSUFFICIENT_EVIDENCE"
GAME_DOUBLEHEADER_REUSE: Final = "GAME_ID_TWO_EVENTS_SAME_DAY"
GAME_UNEXPLAINED_DATE_CHANGE: Final = "GAME_ID_DATE_MOVED_WITHOUT_CONTINUITY"
TEAM_INCOMPATIBLE_LEAGUE: Final = "TEAM_ID_TWO_LEAGUES"
TEAM_LABEL_VARIANCE: Final = "TEAM_ID_LABEL_CHANGED"
PLAYER_INCOMPATIBLE_LEAGUE: Final = "PLAYER_ID_TWO_LEAGUES"
PLAYER_INCOMPATIBLE_BIRTH_DATE: Final = "PLAYER_ID_TWO_BIRTH_DATES"
PLAYER_NAME_VARIANCE: Final = "PLAYER_ID_NAME_CHANGED"
PLAYER_INSUFFICIENT: Final = "PLAYER_ID_NO_SECONDARY_EVIDENCE"
NAMESPACE_UNVERIFIED_CODE: Final = "NAMESPACE_GENERATION_UNVERIFIED"
DETECTION_POWER_CODE: Final = "NAMESPACE_DETECTION_POWER"


@dataclass(frozen=True)
class PlannedFinding:
    """One finding, decided in memory before anything is written."""

    severity: FindingSeverity
    finding_code: str
    classification: FindingClassification
    exclusion_scope: ExclusionScope
    provider_id: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_collision(self) -> bool:
        return self.classification is FindingClassification.IDENTITY_COLLISION

    @property
    def is_flag(self) -> bool:
        """A flag is a WARNING: something a human should look at, not a refusal."""

        return self.severity is FindingSeverity.WARNING

    def digest_key(self) -> tuple[str, str, str, str, str]:
        return (self.severity.value, self.finding_code, self.classification.value,
                self.exclusion_scope.value, self.provider_id or "")


@dataclass(frozen=True)
class AuditPlan:
    """A complete, reconciled audit conclusion that has not yet been written.

    Every count is derived from the findings, never asserted alongside them, so
    the summary cannot disagree with its own evidence.
    """

    namespace: ProviderNamespace
    source_corpus_digest: str
    audit_policy_version: str
    distinct_ids: int
    total_observations: int
    collision_count: int
    flagged_count: int
    verdict: AuditVerdict
    findings: tuple[PlannedFinding, ...]
    semantic_digest: str

    @property
    def accepted(self) -> bool:
        return self.verdict is AuditVerdict.ACCEPTED

    @property
    def cleared_provider_ids(self) -> tuple[str, ...]:
        """Provider ids an ACCEPTED audit clears for crosswalk use.

        Empty unless the audit is accepted: the schema makes any real collision a
        REJECTED namespace audit, not "accepted except these ids" (see §20 of the
        implementation report).
        """

        return self._cleared

    _cleared: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Compatibility rules
# --------------------------------------------------------------------------- #
def _game_identity(obs: GameObservation) -> tuple[Any, ...]:
    """The triple that defines WHICH EVENT a game id denotes.

    Season plus the two official team ids. A game's participants do not change;
    if two observations disagree, the id has been used for two different events.

    Everything else -- scheduled start, local date, status, venue, game number,
    doubleheader code -- is lawful mutation of the same event. A postponement
    that moves a game by three days is not id reuse, and treating it as one would
    make every rescheduled game a false collision.
    """

    return (obs.season, obs.home_provider_team_id, obs.away_provider_team_id)


def _game_mutation_fields(obs: GameObservation) -> dict[str, Any]:
    return {
        "game_date_local": obs.game_date_local,
        "scheduled_start": obs.scheduled_start,
        "mapped_status": obs.mapped_status,
        "game_number": obs.game_number,
        "doubleheader_code": obs.doubleheader_code,
        "venue_provider_id": obs.venue_provider_id,
    }


#: Fields where an empty string is a REAL value rather than "not supplied".
#: ``suffix`` is the only one: e017 stores it NOT NULL DEFAULT '', so "" means
#: "this person has no generational suffix" -- and "Ken Griffey" is not "Ken
#: Griffey Jr.". Treating "" as absent there would hide exactly the distinction
#: the column was added to make.
_EMPTY_IS_MEANINGFUL: Final[frozenset[str]] = frozenset({"suffix"})


def _supplied_disagreements(
    group: Sequence[Any], fields: tuple[str, ...]
) -> set[str]:
    """Fields on which two observations SUPPLY different values.

    A field absent in one observation and present in another has not changed --
    the provider simply returned a thinner payload from a different endpoint,
    which is routine. Treating that as a rename flagged all 30 MLB franchises in
    the first pass while ``normalized_name`` was in fact identical throughout.
    Only genuinely conflicting SUPPLIED values count.
    """

    changed: set[str] = set()
    for name in fields:
        values = {getattr(o, name) for o in group}
        if name in _EMPTY_IS_MEANINGFUL:
            supplied = {v for v in values if v is not None}
        else:
            supplied = {v for v in values if v not in (None, "")}
        if len(supplied) > 1:
            changed.add(name)
    return changed


#: Statuses that are themselves evidence an event was moved or interrupted.
_MOVED_STATUSES: Final[frozenset[str]] = frozenset(
    {"postponed", "suspended", "delayed", "rescheduled", "cancelled"})


def _has_continuity_evidence(group: Sequence[GameObservation]) -> bool:
    """Did the provider itself say this event moved?

    Either an explicit ``reschedule_info`` payload, or an observed status that
    only occurs when an event is postponed/suspended/moved. Without one of those,
    a date change is unexplained -- and an unexplained date change under one id is
    indistinguishable from that id being reused for a second event.
    """

    return any(o.reschedule_info for o in group) or any(
        (o.mapped_status or "").lower() in _MOVED_STATUSES for o in group)


def _audit_games(
    namespace: ProviderNamespace, observations: Sequence[GameObservation]
) -> tuple[list[PlannedFinding], dict[str, int]]:
    by_id: dict[str, list[GameObservation]] = defaultdict(list)
    for obs in observations:
        by_id[obs.provider_game_id].append(obs)

    findings: list[PlannedFinding] = []
    for provider_id in sorted(by_id):
        group = by_id[provider_id]
        identities = {_game_identity(o) for o in group}
        if len(identities) > 1:
            findings.append(PlannedFinding(
                severity=FindingSeverity.BLOCKING,
                finding_code=GAME_INCOMPATIBLE_EVENT,
                classification=FindingClassification.IDENTITY_COLLISION,
                exclusion_scope=ExclusionScope.ENTITY,
                provider_id=provider_id,
                detail={"distinct_event_identities": len(identities),
                        "observations": len(group)},
            ))
            continue

        # Two events on ONE day under one id. MLB assigns distinct game numbers to
        # the halves of a doubleheader, so the same id carrying both is provably
        # two events -- the matchup matching is exactly what makes it invisible to
        # the season/home/away triple.
        same_day_numbers = defaultdict(set)
        for o in group:
            if o.game_date_local is not None and o.game_number is not None:
                same_day_numbers[o.game_date_local].add(o.game_number)
        doubled = {d: sorted(n) for d, n in same_day_numbers.items() if len(n) > 1}
        if doubled:
            findings.append(PlannedFinding(
                severity=FindingSeverity.BLOCKING,
                finding_code=GAME_DOUBLEHEADER_REUSE,
                classification=FindingClassification.IDENTITY_COLLISION,
                exclusion_scope=ExclusionScope.ENTITY,
                provider_id=provider_id,
                detail={"dates_with_multiple_game_numbers": sorted(doubled),
                        "observations": len(group)},
            ))
            continue

        if any(v is None for v in next(iter(identities))):
            findings.append(PlannedFinding(
                severity=FindingSeverity.INFO,
                finding_code=GAME_INSUFFICIENT,
                classification=FindingClassification.INSUFFICIENT_EVIDENCE,
                exclusion_scope=ExclusionScope.NONE,
                provider_id=provider_id,
                detail={"observations": len(group)},
            ))
            continue

        dates = {o.game_date_local for o in group if o.game_date_local}
        if len(dates) > 1 and not _has_continuity_evidence(group):
            # A postponement legitimately moves a date, so this is NOT called a
            # collision. But nor is it lawful mutation: with no reschedule payload
            # and no moved-status observation, the corpus cannot distinguish "the
            # same game moved" from "this id was reused for another game between
            # the same teams". Recorded as a detection gap, honestly.
            findings.append(PlannedFinding(
                severity=FindingSeverity.WARNING,
                finding_code=GAME_UNEXPLAINED_DATE_CHANGE,
                classification=FindingClassification.INSUFFICIENT_EVIDENCE,
                exclusion_scope=ExclusionScope.NONE,
                provider_id=provider_id,
                detail={"distinct_dates": len(dates), "observations": len(group)},
            ))
            continue

        mutations = {k for k in _game_mutation_fields(group[0])
                     if len({_game_mutation_fields(o)[k] for o in group}) > 1}
        if mutations:
            findings.append(PlannedFinding(
                severity=FindingSeverity.INFO,
                finding_code=GAME_LEGITIMATE_MUTATION,
                classification=FindingClassification.LEGITIMATE_MUTATION,
                exclusion_scope=ExclusionScope.NONE,
                provider_id=provider_id,
                detail={"changed_fields": sorted(mutations),
                        "observations": len(group)},
            ))
    return findings, {"distinct_ids": len(by_id), "total_observations": len(observations)}


def _audit_teams(
    namespace: ProviderNamespace, observations: Sequence[TeamObservation]
) -> tuple[list[PlannedFinding], dict[str, int]]:
    """A team id is FRANCHISE identity.

    Identity-defining: the league alone. Rename, relocation, rebrand and
    abbreviation changes are lawful and are detected only as label variance --
    making the display name the key would turn every rebrand into a collision,
    which is precisely the mistake G5 §7 forbids.
    """

    by_id: dict[str, list[TeamObservation]] = defaultdict(list)
    for obs in observations:
        by_id[obs.provider_team_id].append(obs)

    findings: list[PlannedFinding] = []
    for provider_id in sorted(by_id):
        group = by_id[provider_id]
        leagues = {o.league_id for o in group}
        if len(leagues) > 1:
            findings.append(PlannedFinding(
                severity=FindingSeverity.BLOCKING,
                finding_code=TEAM_INCOMPATIBLE_LEAGUE,
                classification=FindingClassification.IDENTITY_COLLISION,
                # A franchise collision takes its games with it.
                exclusion_scope=ExclusionScope.DEPENDENT_GAMES,
                provider_id=provider_id,
                detail={"distinct_leagues": len(leagues), "observations": len(group)},
            ))
            continue
        changed = _supplied_disagreements(group, ("normalized_name", "abbreviation",
                                                  "city", "nickname"))
        if changed:
            findings.append(PlannedFinding(
                severity=FindingSeverity.WARNING,
                finding_code=TEAM_LABEL_VARIANCE,
                classification=FindingClassification.NAME_VARIANCE,
                exclusion_scope=ExclusionScope.NONE,
                provider_id=provider_id,
                detail={"changed_labels": sorted(changed), "observations": len(group)},
            ))
    return findings, {"distinct_ids": len(by_id), "total_observations": len(observations)}


def _audit_players(
    namespace: ProviderNamespace, observations: Sequence[PlayerObservation]
) -> tuple[list[PlannedFinding], dict[str, int]]:
    """A player id is PERSON identity -- the hardest class, treated conservatively.

    Identity-defining: league, and birth date **only when the provider genuinely
    supplied it on more than one observation**. Team affiliation, position and
    jersey are time-varying and are never identity. A name difference raises a
    flag and can never merge or split ids; two different ids sharing a name
    remain two people, because the provider says so and a name does not.
    """

    by_id: dict[str, list[PlayerObservation]] = defaultdict(list)
    for obs in observations:
        by_id[obs.provider_player_id].append(obs)

    findings: list[PlannedFinding] = []
    without_secondary: list[str] = []
    for provider_id in sorted(by_id):
        group = by_id[provider_id]
        leagues = {o.league_id for o in group}
        if len(leagues) > 1:
            findings.append(PlannedFinding(
                severity=FindingSeverity.BLOCKING,
                finding_code=PLAYER_INCOMPATIBLE_LEAGUE,
                classification=FindingClassification.IDENTITY_COLLISION,
                exclusion_scope=ExclusionScope.ENTITY,
                provider_id=provider_id,
                detail={"distinct_leagues": len(leagues), "observations": len(group)},
            ))
            continue
        births = {o.birth_date for o in group if o.birth_date}
        if len(births) > 1:
            # Two different birth dates under one id are two different people.
            findings.append(PlannedFinding(
                severity=FindingSeverity.BLOCKING,
                finding_code=PLAYER_INCOMPATIBLE_BIRTH_DATE,
                classification=FindingClassification.IDENTITY_COLLISION,
                exclusion_scope=ExclusionScope.ENTITY,
                provider_id=provider_id,
                detail={"distinct_birth_dates": len(births),
                        "observations": len(group)},
            ))
            continue
        changed = _supplied_disagreements(group, ("normalized_name", "suffix"))
        if changed:
            findings.append(PlannedFinding(
                severity=FindingSeverity.WARNING,
                finding_code=PLAYER_NAME_VARIANCE,
                classification=FindingClassification.NAME_VARIANCE,
                exclusion_scope=ExclusionScope.NONE,
                provider_id=provider_id,
                detail={"changed_fields": sorted(changed), "observations": len(group)},
            ))
        if not births:
            without_secondary.append(provider_id)

    if without_secondary:
        # ONE namespace-level record, not one per id. How much independent
        # evidence the corpus offered is a property of the corpus, and 1,053
        # identical per-entity rows would bury the finding that matters while
        # asserting nothing extra. Reach is `none`: thin evidence excludes
        # nothing, it only bounds what a clean result can be said to prove.
        findings.append(PlannedFinding(
            severity=FindingSeverity.INFO,
            finding_code=PLAYER_INSUFFICIENT,
            classification=FindingClassification.INSUFFICIENT_EVIDENCE,
            exclusion_scope=ExclusionScope.NONE,
            provider_id=None,
            detail={"ids_without_secondary_evidence": len(without_secondary),
                    "ids_audited": len(by_id),
                    "secondary_evidence_field": "birth_date"},
        ))
    return findings, {"distinct_ids": len(by_id), "total_observations": len(observations)}


_AUDITORS: Final = {
    EntityType.GAME: _audit_games,
    EntityType.TEAM: _audit_teams,
    EntityType.PLAYER: _audit_players,
}


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def audit_namespace(
    source: sqlite3.Connection,
    *,
    namespace: ProviderNamespace,
    source_corpus_digest: str,
    audit_policy_version: str = AUDIT_POLICY_VERSION,
) -> AuditPlan:
    """Audit one namespace over one corpus, entirely in memory.

    Writes nothing. The returned plan is complete and self-consistent: its counts
    are derived from its findings, so :func:`persist_audit_plan` can insert it
    atomically without a second opinion.
    """

    if audit_policy_version != AUDIT_POLICY_VERSION:
        raise IdentityAuditError(
            f"audit policy {audit_policy_version!r} is not implemented by this build "
            f"(this build implements {AUDIT_POLICY_VERSION!r}). A corpus audited under "
            "a different policy cannot be reproduced here, so it is refused."
        )

    require_provider_league(namespace.provider, namespace.league_id)
    observations = observations_for(source, namespace.entity_type,
                                    provider=namespace.provider)
    if not observations:
        # An audit of nothing found no contradiction, which is true and useless.
        # Recording it as ACCEPTED would put a clean namespace verdict on a corpus
        # that holds no evidence for that entity type at all.
        raise IdentityAuditError(
            f"source corpus holds no {namespace.entity_type.value} identity evidence "
            f"for provider {namespace.provider!r}. An empty namespace cannot be "
            "audited clean -- refusing rather than recording a vacuous ACCEPTED."
        )
    auditor = _AUDITORS[namespace.entity_type]
    findings, counts = auditor(namespace, observations)  # type: ignore[operator]
    findings = [*findings, _detection_power(namespace, observations, counts)]

    # Entity-type audits scope by provider; the team/player tables also carry a
    # league, and an observation from another league is not this namespace's
    # evidence. Refuse rather than silently mixing.
    _assert_single_league(namespace, observations)

    if not namespace.verified:
        # An unverified API generation blocks the league namespace outright: the
        # ids may not even share a namespace with themselves across versions.
        findings = [
            PlannedFinding(
                severity=FindingSeverity.BLOCKING,
                finding_code=NAMESPACE_UNVERIFIED_CODE,
                classification=FindingClassification.NAMESPACE_UNVERIFIED,
                exclusion_scope=ExclusionScope.LEAGUE_NAMESPACE,
                provider_id=None,
                detail={"audited_ids": counts["distinct_ids"]},
            ),
            *findings,
        ]

    collision_ids = {f.provider_id for f in findings if f.is_collision}
    collision_count = len(collision_ids)
    flagged_count = sum(1 for f in findings if f.is_flag)

    if not namespace.verified:
        verdict = AuditVerdict.REJECTED_NAMESPACE_UNVERIFIED
    elif collision_count:
        verdict = AuditVerdict.REJECTED_COLLISION
    else:
        verdict = AuditVerdict.ACCEPTED

    cleared: tuple[str, ...] = ()
    if verdict is AuditVerdict.ACCEPTED:
        cleared = tuple(sorted(_provider_ids(namespace.entity_type, observations)))

    plan = AuditPlan(
        namespace=namespace,
        source_corpus_digest=source_corpus_digest,
        audit_policy_version=audit_policy_version,
        distinct_ids=counts["distinct_ids"],
        total_observations=counts["total_observations"],
        collision_count=collision_count,
        flagged_count=flagged_count,
        verdict=verdict,
        findings=tuple(sorted(findings, key=lambda f: f.digest_key())),
        semantic_digest=_plan_digest(
            namespace=namespace, source_corpus_digest=source_corpus_digest,
            audit_policy_version=audit_policy_version, counts=counts,
            collision_count=collision_count, flagged_count=flagged_count,
            verdict=verdict, findings=findings,
            provider_ids=_provider_ids(namespace.entity_type, observations),
        ),
        _cleared=cleared,
    )
    _reconcile(plan, observations)
    return plan


def _detection_power(
    namespace: ProviderNamespace,
    observations: Sequence[Any],
    counts: dict[str, int],
) -> PlannedFinding:
    """Record what this audit was ABLE to detect, not just what it found.

    The independent review's central objection: "zero collisions" was being read
    as "identity verified", when for the real one-month game namespaces it meant
    "no id was observed twice, so nothing was compared at all". A clean verdict
    over uncomparable evidence is not evidence of stability, and the audit record
    must say so itself rather than leaving it to a report someone may not read.

    ``comparable_ids`` is the number of ids observed more than once -- the only
    ids where a contradiction could possibly have surfaced.
    ``discriminating_ids`` is the number carrying independent secondary evidence
    (a birth date, for persons); for games and teams the discriminating evidence
    is structural and equals the comparable count.
    """

    attribute = {
        EntityType.GAME: "provider_game_id",
        EntityType.TEAM: "provider_team_id",
        EntityType.PLAYER: "provider_player_id",
    }[namespace.entity_type]
    groups: dict[str, list[Any]] = defaultdict(list)
    for obs in observations:
        groups[str(getattr(obs, attribute))].append(obs)
    comparable = sum(1 for g in groups.values() if len(g) > 1)
    if namespace.entity_type is EntityType.PLAYER:
        discriminating = sum(
            1 for g in groups.values()
            if len({o.birth_date for o in g if o.birth_date}) >= 1 and len(g) > 1)
    else:
        discriminating = comparable
    return PlannedFinding(
        severity=FindingSeverity.INFO,
        finding_code=DETECTION_POWER_CODE,
        classification=FindingClassification.INSUFFICIENT_EVIDENCE,
        exclusion_scope=ExclusionScope.NONE,
        provider_id=None,
        detail={
            "ids_audited": counts["distinct_ids"],
            "ids_observed_more_than_once": comparable,
            "ids_with_discriminating_evidence": discriminating,
            "entity_type": namespace.entity_type.value,
        },
    )


def _provider_ids(entity_type: EntityType, observations: Sequence[Any]) -> set[str]:
    attribute = {
        EntityType.GAME: "provider_game_id",
        EntityType.TEAM: "provider_team_id",
        EntityType.PLAYER: "provider_player_id",
    }[entity_type]
    return {str(getattr(o, attribute)) for o in observations}


def _assert_single_league(
    namespace: ProviderNamespace, observations: Sequence[Any]
) -> None:
    leagues = {o.league_id for o in observations
               if hasattr(o, "league_id")}
    unexpected = leagues - {namespace.league_id}
    if unexpected:
        raise IdentityAuditError(
            f"source corpus holds {namespace.entity_type.value} identity evidence for "
            f"leagues {sorted(unexpected)} under provider {namespace.provider!r}, but "
            f"the audit was asked about {namespace.league_id!r}. Refusing rather than "
            "auditing a mixed namespace."
        )


def _plan_digest(
    *,
    namespace: ProviderNamespace,
    source_corpus_digest: str,
    audit_policy_version: str,
    counts: dict[str, int],
    collision_count: int,
    flagged_count: int,
    verdict: AuditVerdict,
    findings: Sequence[PlannedFinding],
    provider_ids: set[str],
) -> str:
    """Bind the audit's semantic identity.

    Includes the namespace, the exact source digest, the policy version, the
    canonical provider-id set, the reconciled counts, the verdict, and the
    canonical finding set. Excludes wall-clock, surrogate ids and traversal order,
    so two runs over identical evidence agree and a changed corpus does not.
    """

    return semantic_digest({
        "kind": "identity_audit_plan",
        **namespace.as_dict(),
        "namespace_verified": namespace.verified,
        "source_corpus_digest": source_corpus_digest,
        "audit_policy_version": audit_policy_version,
        # The id SET, sorted: order-independent, and a changed population changes
        # the digest even when every count coincidentally matches.
        "provider_ids": sorted(provider_ids),
        "distinct_ids": counts["distinct_ids"],
        "total_observations": counts["total_observations"],
        "collision_count": collision_count,
        "flagged_count": flagged_count,
        "verdict": verdict,
        "findings": sorted(
            [f.severity.value, f.finding_code, f.classification.value,
             f.exclusion_scope.value, f.provider_id or "",
             semantic_digest(f.detail)]
            for f in findings
        ),
    })


def _reconcile(plan: AuditPlan, observations: Sequence[Any]) -> None:
    """Refuse a plan whose summary disagrees with its own findings (§11)."""

    ids = _provider_ids(plan.namespace.entity_type, observations)
    if plan.distinct_ids != len(ids):
        raise IdentityAuditError(
            f"distinct_ids={plan.distinct_ids} but {len(ids)} distinct provider ids "
            "were audited")
    if plan.total_observations != len(observations):
        raise IdentityAuditError(
            f"total_observations={plan.total_observations} but {len(observations)} "
            "observations were scanned")
    collisions = {f.provider_id for f in plan.findings if f.is_collision}
    if plan.collision_count != len(collisions):
        raise IdentityAuditError(
            f"collision_count={plan.collision_count} but {len(collisions)} distinct "
            "collision findings exist")
    flags = sum(1 for f in plan.findings if f.is_flag)
    if plan.flagged_count != flags:
        raise IdentityAuditError(
            f"flagged_count={plan.flagged_count} but {flags} flag findings exist")
    if plan.verdict is AuditVerdict.ACCEPTED and collisions:
        raise IdentityAuditError("an accepted audit cannot carry collision findings")
    if plan.verdict is AuditVerdict.REJECTED_COLLISION and not collisions:
        raise IdentityAuditError("a collision verdict requires a collision finding")
    if (plan.verdict is AuditVerdict.REJECTED_NAMESPACE_UNVERIFIED
            and plan.namespace.verified):
        raise IdentityAuditError(
            "a namespace-unverified verdict requires an unverified generation")
    keys = [f.digest_key() for f in plan.findings]
    if len(keys) != len(set(keys)):
        raise IdentityAuditError("duplicate semantic finding in one audit")


def persist_audit_plan(
    output: sqlite3.Connection, plan: AuditPlan
) -> tuple[str, int]:
    """Write the summary and every finding, atomically.

    Returns ``(identity_audit_id, findings_written)``. The caller owns the
    transaction: on any exception the whole audit rolls back, so a half-written
    audit can never become consumable.
    """

    repo = SqliteRetrospectiveProvenanceRepository(output)
    record = repo.record_identity_audit(
        namespace=plan.namespace,
        source_corpus_digest=plan.source_corpus_digest,
        audit_policy_version=plan.audit_policy_version,
        distinct_ids=plan.distinct_ids,
        total_observations=plan.total_observations,
        collision_count=plan.collision_count,
        flagged_count=plan.flagged_count,
        verdict=plan.verdict,
    )
    written = 0
    for finding in plan.findings:
        repo.record_finding(
            identity_audit_id=record.identity_audit_id,
            namespace=plan.namespace,
            severity=finding.severity,
            finding_code=finding.finding_code,
            classification=finding.classification,
            exclusion_scope=finding.exclusion_scope,
            provider_id=finding.provider_id,
            detail=finding.detail,
        )
        written += 1
    return record.identity_audit_id, written
