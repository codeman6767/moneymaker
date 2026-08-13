"""TEAM-A team static-crosswalk generation, with map-membership enforcement.

This module implements review repair **RV1**, which is the load-bearing part of
TEAM-A. The independent review proved that v19 alone will accept a crosswalk
contradicting the committed map: the corpus records
``static_identity_map_digest``, but the database has no access to the map's
*contents* and therefore cannot check membership. The database is not at fault --
it can enforce relationships among rows it holds, and the map is an **external
artifact**.

So the invariant is enforced here, in code, and re-checked in CI by
``verifier``. That is **weaker than the DB-enforced G5 bindings** (audit/corpus,
entity type, league), and the weakness is stated rather than glossed.

Before a single team crosswalk is written, all of these must hold:

1. the corpus declares a ``static_identity_map_digest``;
2. it equals the recomputed digest of the committed map;
3. the corpus declares a ``code_version`` (reproducibility contract);
4. the exact ``(league, provider, generation, team, provider_id)`` key is a
   member of the committed map, mapping to the canonical team about to be written;
5. an ACCEPTED G5 team audit exists for **this corpus's exact source digest**;
6. the namespace generation is attested;
7. no conflicting live/current canonical mapping exists for the same provider key.

Any failure writes nothing and fails closed. There is **no runtime alias or name
lookup anywhere in this file** -- resolution is an exact dictionary lookup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..db.repositories.retrospective import SqliteRetrospectiveProvenanceRepository
from .attestations import (
    TEAM_ATTESTATION_POLICY_VERSION,
    AttestationError,
    attestation_map_digest,
    attested_canonical_team,
)
from .identity_audit import AuditPlan
from .provenance import EntityType, ProviderNamespace

__all__ = [
    "TeamCrosswalkPlan",
    "TeamCrosswalkResult",
    "plan_team_crosswalks",
    "write_team_crosswalks",
]


@dataclass(frozen=True)
class TeamCrosswalkPlan:
    """What TEAM-A would write, decided before anything is persisted."""

    namespace: ProviderNamespace
    map_digest: str
    attested: tuple[tuple[str, str], ...] = ()      # (provider_id, canonical_team_id)
    unresolved: tuple[str, ...] = ()                # audited but not in the map
    conflicts: tuple[tuple[str, str, str], ...] = ()  # (provider_id, teamA, teamB)

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)

    def as_json(self) -> dict[str, object]:
        return {
            "namespace": self.namespace.as_dict(),
            "attestation_map_digest": self.map_digest,
            "attestation_policy_version": TEAM_ATTESTATION_POLICY_VERSION,
            "attested": len(self.attested),
            "unresolved": list(self.unresolved),
            "unresolved_count": len(self.unresolved),
            "conflicts": [list(c) for c in self.conflicts],
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class TeamCrosswalkResult:
    """Outcome of writing one TEAM-A plan."""

    plan: TeamCrosswalkPlan
    written: int = 0
    reused: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def _require_corpus_provenance(
    repo: SqliteRetrospectiveProvenanceRepository, corpus_version_id: str
) -> tuple[str, str]:
    """The corpus must carry the identity-map digest and a code version (§13)."""

    corpus = repo.corpus_version(corpus_version_id)
    if corpus is None:
        raise AttestationError(
            f"unknown corpus version {corpus_version_id!r}; TEAM-A cannot attest "
            "into a reconstruction that does not exist")
    if not corpus.static_identity_map_digest:
        raise AttestationError(
            f"corpus {corpus_version_id!r} declares no static_identity_map_digest. "
            "A TEAM-A corpus must record the exact map it used; corpus rows are "
            "append-only, so create a NEW corpus version rather than filling it in."
        )
    expected = attestation_map_digest()
    if corpus.static_identity_map_digest != expected:
        raise AttestationError(
            f"corpus {corpus_version_id!r} declares attestation map digest "
            f"{corpus.static_identity_map_digest[:16]}..., but the committed map "
            f"digests to {expected[:16]}.... Either the map changed since this "
            "corpus was created -- in which case a NEW corpus version is required "
            "-- or the corpus is not a TEAM-A corpus."
        )
    if not corpus.code_version:
        raise AttestationError(
            f"corpus {corpus_version_id!r} declares no code_version. The TEAM-A "
            "reproducibility contract uses the repository revision as the map's "
            "version axis, so it must be recorded."
        )
    return corpus.source_corpus_digest, corpus.static_identity_map_digest


def _live_conflict(
    conn: sqlite3.Connection, namespace: ProviderNamespace, provider_id: str
) -> Optional[str]:
    """An existing accepted live/current canonical binding for the same key (§19).

    Read-only. If the live matcher already resolved this provider team to some
    canonical team, TEAM-A must agree with it or refuse; silently preferring
    either answer would hide a real disagreement about identity.
    """

    row = conn.execute(
        "SELECT team_id FROM provider_team_references "
        "WHERE provider = ? AND provider_team_id = ? AND team_id IS NOT NULL",
        (namespace.provider, provider_id)).fetchone()
    return None if row is None else str(row[0])


def plan_team_crosswalks(
    output: sqlite3.Connection,
    *,
    plan: AuditPlan,
    corpus_version_id: str,
) -> TeamCrosswalkPlan:
    """Decide every TEAM-A crosswalk for one accepted team audit. Writes nothing.

    ``plan`` must be an ACCEPTED team audit; the ids it cleared are exactly the
    ids considered. An id the audit cleared but the map does not contain is
    reported **unresolved** -- never guessed, never alias-matched.
    """

    namespace = plan.namespace
    if namespace.entity_type is not EntityType.TEAM:
        raise AttestationError(
            f"TEAM-A plans team namespaces only, not {namespace.entity_type.value!r}")
    if not plan.accepted:
        raise AttestationError(
            f"audit verdict is {plan.verdict.value!r}; only an accepted audit clears "
            "a namespace for attestation")

    repo = SqliteRetrospectiveProvenanceRepository(output)
    source_digest, map_digest = _require_corpus_provenance(repo, corpus_version_id)
    if source_digest != plan.source_corpus_digest:
        raise AttestationError(
            f"corpus {corpus_version_id!r} is built over {source_digest!r} but the "
            f"audit examined {plan.source_corpus_digest!r}; an audit never transfers "
            "to a different corpus")

    attested: list[tuple[str, str]] = []
    unresolved: list[str] = []
    conflicts: list[tuple[str, str, str]] = []
    for provider_id in plan.cleared_provider_ids:
        canonical = attested_canonical_team(namespace, provider_id)
        if canonical is None:
            unresolved.append(provider_id)
            continue
        live = _live_conflict(output, namespace, provider_id)
        if live is not None and live != canonical:
            conflicts.append((provider_id, canonical, live))
            continue
        attested.append((provider_id, canonical))

    return TeamCrosswalkPlan(
        namespace=namespace, map_digest=map_digest,
        attested=tuple(sorted(attested)), unresolved=tuple(sorted(unresolved)),
        conflicts=tuple(sorted(conflicts)),
    )


def write_team_crosswalks(
    output: sqlite3.Connection,
    *,
    plan: AuditPlan,
    corpus_version_id: str,
    identity_audit_id: str,
) -> TeamCrosswalkResult:
    """Persist a TEAM-A plan. The caller owns the transaction.

    Refuses outright when the plan is blocked by a live/Lane-R conflict: a
    disagreement about who a provider team is must be reviewed, not resolved by
    whichever writer ran last.
    """

    team_plan = plan_team_crosswalks(output, plan=plan,
                                    corpus_version_id=corpus_version_id)
    if team_plan.blocked:
        raise AttestationError(
            f"{len(team_plan.conflicts)} provider team id(s) disagree with an "
            f"existing canonical mapping: {list(team_plan.conflicts)}. TEAM-A writes "
            "nothing until the disagreement is reviewed."
        )

    repo = SqliteRetrospectiveProvenanceRepository(output)
    written = reused = 0
    for provider_id, canonical_team_id in team_plan.attested:
        existing = repo.static_crosswalk(
            corpus_version_id=corpus_version_id, namespace=team_plan.namespace,
            provider_id=provider_id)
        if existing is not None:
            if existing.canonical_entity_id != canonical_team_id:
                raise AttestationError(
                    f"provider key {team_plan.namespace.key(provider_id)} is already "
                    f"bound to {existing.canonical_entity_id!r} in this corpus; the "
                    "committed map says "
                    f"{canonical_team_id!r}. Refusing rather than choosing.")
            reused += 1
            continue
        repo.record_static_crosswalk(
            corpus_version_id=corpus_version_id, namespace=team_plan.namespace,
            provider_id=provider_id, canonical_entity_id=canonical_team_id,
            identity_audit_id=identity_audit_id,
            provenance_policy_version=TEAM_ATTESTATION_POLICY_VERSION,
            # RV1 repair #2: the crosswalk is cryptographically bound to the map.
            attestation_map_digest=team_plan.map_digest,
        )
        written += 1
    return TeamCrosswalkResult(plan=team_plan, written=written, reused=reused)
