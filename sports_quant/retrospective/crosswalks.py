"""Static-crosswalk construction from an ACCEPTED identity audit (G5 §16-§18).

A crosswalk says: *within corpus version X, official provider key K denotes
canonical entity Y, and audit Z cleared that namespace.* It is built from the
exact official key and nothing else -- no name match, no roster, no outcome, no
match decision, no `decided_at` gate.

The canonical-entity problem, and what this module actually delivers
--------------------------------------------------------------------
A crosswalk has an FK/trigger obligation to an existing canonical row in the
OUTPUT database. Task §17 asks for the narrowest deterministic way to satisfy it
without fuzzy matching. The answer differs by entity type, and the difference is
a measured property of this repository rather than a preference:

* **player -- SUPPORTED.** ``players`` is empty in a fresh output database and
  carries no uniqueness constraint beyond its primary key, so a canonical person
  can be bootstrapped whose id is a pure deterministic function of the official
  provider key. Names are stored as descriptive metadata; they never participate
  in identity.

* **team -- BLOCKED.** ``teams`` is pre-seeded with 60 name-based franchises and
  constrained ``UNIQUE (league_id, canonical_name)`` and
  ``UNIQUE (league_id, abbreviation)``. Bootstrapping a provider-keyed franchise
  with the provider-written name is refused by those constraints (verified), and
  the only way to reuse a seeded row is to decide that the provider's
  "Houston Astros" denotes the seed ``tm_mlb_hou`` -- which is name matching,
  exactly what §16 forbids as historical identity evidence. Dodging the
  constraint by mangling the canonical label would corrupt a canonical dimension
  to satisfy a foreign key.

* **game -- BLOCKED, transitively.** ``games.home_team_id`` and
  ``games.away_team_id`` are NOT NULL references to ``teams``, so a game
  crosswalk cannot exist until the team question is answered.

That is reported as a blocker rather than forced. The audit engine audits all
three entity types regardless; only crosswalk *generation* is limited.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Final, Optional

from ..db.repositories.retrospective import SqliteRetrospectiveProvenanceRepository
from ..db.schema import utc_now_iso
from .identity_audit import AuditPlan
from .provenance import EntityType, ProviderNamespace, RetrospectiveProvenanceError
from .sources import PlayerObservation, iter_player_observations

__all__ = [
    "CROSSWALK_POLICY_VERSION",
    "CROSSWALK_SUPPORTED_ENTITY_TYPES",
    "CanonicalPreparationBlocked",
    "CrosswalkResult",
    "canonical_player_id",
    "generate_crosswalks",
]


class CanonicalPreparationBlocked(RetrospectiveProvenanceError):
    """No deterministic, non-fuzzy canonical target can be prepared."""


#: Bumping this changes every crosswalk's semantic digest, which is correct: the
#: rule by which a provider key was bound to a canonical entity would have moved.
CROSSWALK_POLICY_VERSION: Final = "g5-static-crosswalk-v1"

#: Only the entity type whose canonical dimension can be prepared deterministically.
CROSSWALK_SUPPORTED_ENTITY_TYPES: Final[frozenset[EntityType]] = frozenset(
    {EntityType.PLAYER}
)

_BLOCKED_REASON: Final[dict[EntityType, str]] = {
    EntityType.TEAM: (
        "canonical `teams` is pre-seeded from names and constrained UNIQUE on "
        "(league_id, canonical_name) and (league_id, abbreviation). A provider-keyed "
        "franchise cannot be bootstrapped alongside it, and reusing a seeded row "
        "would require matching the provider-written name to a seed -- name matching, "
        "which G5 forbids as historical identity evidence."
    ),
    EntityType.GAME: (
        "canonical `games` requires NOT NULL home_team_id/away_team_id referencing "
        "`teams`, so a game crosswalk inherits the unresolved team-canonical question."
    ),
}


@dataclass(frozen=True)
class CrosswalkResult:
    """Outcome of crosswalk generation for one accepted audit."""

    entity_type: EntityType
    supported: bool
    canonical_bootstrapped: int
    crosswalks_written: int
    reused_existing: int
    blocked_reason: Optional[str] = None


def canonical_player_id(namespace: ProviderNamespace, provider_id: str) -> str:
    """A canonical person id that is a pure function of the official key.

    Deterministic and future-blind: the same key always yields the same id, on any
    machine, in any order, without consulting a name, a roster, an outcome or a
    match decision. Rebuilding the corpus reproduces every id exactly, which is
    what makes a corpus diff meaningful.

    A digest rather than a readable slug on purpose -- a readable id would embed a
    provider label, and labels change.
    """

    key = "|".join(namespace.key(provider_id))
    return "pl_r" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _earliest_identity(
    observations: list[PlayerObservation],
) -> PlayerObservation:
    """The canonical descriptive metadata for a bootstrapped person.

    Chosen by earliest ``observed_at``, tie-broken by the normalized name, so the
    label is a deterministic function of the evidence rather than of row order.
    The choice is descriptive only: nothing downstream may match on it.
    """

    return sorted(observations, key=lambda o: (o.observed_at, o.normalized_name,
                                               o.full_name))[0]


def generate_crosswalks(
    output: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    plan: AuditPlan,
    corpus_version_id: str,
    identity_audit_id: str,
    dry_run: bool = False,
) -> CrosswalkResult:
    """Bind every provider id an ACCEPTED audit cleared to a canonical entity.

    Refuses outright unless the audit was accepted: the schema makes any real
    collision a REJECTED namespace audit, so there is no "accepted except these
    ids" path to implement (see the implementation report §20).
    """

    entity_type = plan.namespace.entity_type
    if not plan.accepted:
        raise RetrospectiveProvenanceError(
            f"audit verdict is {plan.verdict.value!r}; only an accepted audit clears a "
            "namespace for crosswalk use"
        )
    if entity_type not in CROSSWALK_SUPPORTED_ENTITY_TYPES:
        return CrosswalkResult(
            entity_type=entity_type, supported=False, canonical_bootstrapped=0,
            crosswalks_written=0, reused_existing=0,
            blocked_reason=_BLOCKED_REASON[entity_type],
        )

    by_id: dict[str, list[PlayerObservation]] = {}
    for obs in iter_player_observations(source, provider=plan.namespace.provider):
        by_id.setdefault(obs.provider_player_id, []).append(obs)

    repo = SqliteRetrospectiveProvenanceRepository(output)
    bootstrapped = reused = written = 0

    for provider_id in plan.cleared_provider_ids:
        group = by_id.get(provider_id)
        if not group:  # pragma: no cover - cleared ids come from these observations
            raise RetrospectiveProvenanceError(
                f"cleared provider id {provider_id!r} has no identity observation")
        canonical_id = canonical_player_id(plan.namespace, provider_id)

        existing = output.execute(
            "SELECT league_id FROM players WHERE player_id = ?", (canonical_id,)
        ).fetchone()
        if existing is None:
            if dry_run:
                bootstrapped += 1
            else:
                label = _earliest_identity(group)
                now = utc_now_iso()
                output.execute(
                    "INSERT INTO players (player_id, league_id, full_name, suffix, "
                    " primary_position, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (canonical_id, plan.namespace.league_id, label.full_name,
                     label.suffix or None, now, now),
                )
                bootstrapped += 1
        else:
            if str(existing[0]) != plan.namespace.league_id:
                raise RetrospectiveProvenanceError(
                    f"canonical player {canonical_id!r} already exists in league "
                    f"{existing[0]!r}, not {plan.namespace.league_id!r}"
                )
            reused += 1

        if dry_run:
            written += 1
            continue

        prior = repo.static_crosswalk(
            corpus_version_id=corpus_version_id, namespace=plan.namespace,
            provider_id=provider_id,
        )
        if prior is not None and prior.canonical_entity_id != canonical_id:
            # Cannot happen while the id is a pure function of the key, but a
            # conflicting target must fail closed rather than pick a winner.
            raise RetrospectiveProvenanceError(
                f"provider key {plan.namespace.key(provider_id)} is already bound to "
                f"{prior.canonical_entity_id!r} in this corpus version"
            )
        repo.record_static_crosswalk(
            corpus_version_id=corpus_version_id, namespace=plan.namespace,
            provider_id=provider_id, canonical_entity_id=canonical_id,
            identity_audit_id=identity_audit_id,
            provenance_policy_version=CROSSWALK_POLICY_VERSION,
        )
        written += 1

    return CrosswalkResult(
        entity_type=entity_type, supported=True,
        canonical_bootstrapped=bootstrapped, crosswalks_written=written,
        reused_existing=reused,
    )
