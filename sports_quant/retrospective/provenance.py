"""Lane-R provenance vocabulary, namespace key, and deterministic digests.

Every enumeration here mirrors a CHECK constraint in migration f018. The pair is
deliberate rather than redundant: the CHECK is the guarantee (it holds against
any connection, including a future non-Python writer), and the enum is what
makes a wrong value a mypy error instead of a runtime abort.

Two things this module is careful about.

**Namespace safety.** :class:`ProviderNamespace` is the G5 identity key --
``(league, provider, entity_type, provider_id)`` -- plus the provider's API
generation. The generation is always explicit and is never inferred from the
shape of an id, because neither BALLDONTLIE nor MLB StatsAPI documents whether
identifiers are stable across API versions. An unknown generation is
representable (:data:`UNVERIFIED_GENERATION`) and is refused an accepted audit.

**Digest determinism.** :func:`semantic_digest` is canonical over content and
blind to order: same facts in, same digest out, regardless of dict ordering,
insertion order, or the order SQLite happens to return rows in. Volatile audit
wall-clocks are excluded by the caller, which is why the tables keep
``created_at`` out of every digest -- with one deliberate exception, a
provider-published snapshot instant, which is source evidence rather than our
own bookkeeping.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from typing import Any, Final, Mapping

from streaming.event_envelope import canonical_json

__all__ = [
    "AuditVerdict",
    "AvailabilityBasis",
    "EligibilityVerdict",
    "EntityType",
    "ExclusionScope",
    "FindingClassification",
    "FindingSeverity",
    "G1Variant",
    "ProvenanceClass",
    "ProviderNamespace",
    "RetrospectiveProvenanceError",
    "UNVERIFIED_GENERATION",
    "canonical_detail_json",
    "detail_digest",
    "semantic_digest",
]


class RetrospectiveProvenanceError(RuntimeError):
    """A Lane-R provenance invariant was violated before it reached SQLite."""


#: The generation string to use when the provider's API generation could not be
#: established from primary evidence. Storable, and never accepted: an audit
#: carrying it must be recorded as ``rejected_namespace_unverified``.
UNVERIFIED_GENERATION = "unverified"


class ProvenanceClass(str, enum.Enum):
    """Which evidence lane a record belongs to.

    ``STRICT_FORWARD_PIT`` exists in the vocabulary so a forward corpus can be
    described with the same words, but it is refused by
    ``reconstructed_input_provenance``: forward evidence is read through
    ``AsOfReader``, and letting it appear in the retrospective table would be the
    first step toward the two lanes quietly merging.
    """

    STRICT_FORWARD_PIT = "strict_forward_pit"
    RECONSTRUCTED_RESEARCH = "reconstructed_research"
    LABEL_ONLY_RETROSPECTIVE = "label_only_retrospective"


class AvailabilityBasis(str, enum.Enum):
    """Why a reconstructed input is claimed to have been knowable in time.

    * ``STATIC_IDENTITY`` -- the fact is an identity that does not vary in time;
      the provider id is in the historical evidence row and its namespace passed
      the corpus-scoped G5 audit. No timestamp is involved, which is precisely
      what makes it static.
    * ``EVENT_DERIVED`` -- the fact is derived from an event that had completed;
      availability is ``source_event_completed_at`` advanced by a versioned rule.
    * ``VERSIONED_SNAPSHOT`` -- the provider published a snapshot and stamped it;
      that stamp is the availability evidence, and it is theirs, not ours.

    There is no forward-only member. FORWARD_ONLY evidence has no retrospective
    basis by construction, so it cannot be named here.
    """

    STATIC_IDENTITY = "static_identity"
    EVENT_DERIVED = "event_derived"
    VERSIONED_SNAPSHOT = "versioned_snapshot"


class EntityType(str, enum.Enum):
    """The entity class a provider id denotes.

    Part of the identity key, and not optional: without it, MLB team ``147`` and
    MLB person ``147`` are the same string and collide trivially.
    """

    GAME = "game"
    TEAM = "team"
    PLAYER = "player"


class AuditVerdict(str, enum.Enum):
    """Outcome of one corpus-scoped identity audit.

    Only ``ACCEPTED`` clears a namespace for crosswalk use, and f018 refuses it
    unless the collision count is zero and the generation is verified.
    """

    ACCEPTED = "accepted"
    REJECTED_COLLISION = "rejected_collision"
    REJECTED_NAMESPACE_UNVERIFIED = "rejected_namespace_unverified"


class FindingSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class FindingClassification(str, enum.Enum):
    """What kind of thing the audit observed.

    ``NAME_VARIANCE`` is detection-only. A name may raise a flag for review; it
    may never override a stable id and may never merge two ids (G5 review §8).
    ``LEGITIMATE_MUTATION`` records that a difference was examined and found
    lawful -- a rename, a relocation, a reschedule -- so "we looked" is evidence
    rather than silence.
    """

    IDENTITY_COLLISION = "identity_collision"
    NAME_VARIANCE = "name_variance"
    NAMESPACE_UNVERIFIED = "namespace_unverified"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    LEGITIMATE_MUTATION = "legitimate_mutation"


class ExclusionScope(str, enum.Enum):
    """The blast radius of a finding, as observed.

    This records reach, not policy. How many entity-scoped exclusions should
    escalate to refusing the corpus is a versioned decision that stays in code:
    freezing a threshold into an evidence row would make old evidence carry a
    policy it never agreed to.
    """

    NONE = "none"
    ENTITY = "entity"
    DEPENDENT_GAMES = "dependent_games"
    LEAGUE_NAMESPACE = "league_namespace"
    CORPUS = "corpus"


class EligibilityVerdict(str, enum.Enum):
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"


class G1Variant(str, enum.Enum):
    """Which G1 feature-set variant a corpus was built under.

    ``G1_B_CORE`` admits only facts that are effectively immutable or
    independently reconstructable. ``G1_A_EXTENDED`` additionally admits
    correction-sensitive box-score detail, and results using it may never be
    described as transaction-time-exact. The two are separate variants by
    construction and are never silently merged into one "Lane R" number.
    """

    G1_B_CORE = "g1_b_core"
    G1_A_EXTENDED = "g1_a_extended"


@dataclass(frozen=True)
class ProviderNamespace:
    """The G5 identity key plus the provider's API generation.

    Frozen and hashable so it can key a dict without anyone being tempted to
    mutate one half of a composite identity.
    """

    league_id: str
    provider: str
    entity_type: EntityType
    generation: str = UNVERIFIED_GENERATION

    def __post_init__(self) -> None:
        for field_name in ("league_id", "provider", "generation"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RetrospectiveProvenanceError(
                    f"provider namespace {field_name} must be a non-empty string; "
                    f"got {value!r}. A namespace component is never inferred."
                )
        if not isinstance(self.entity_type, EntityType):
            raise RetrospectiveProvenanceError(
                f"entity_type must be an EntityType, got {self.entity_type!r}"
            )

    @property
    def verified(self) -> bool:
        """False when the API generation could not be established."""

        return self.generation != UNVERIFIED_GENERATION

    def key(self, provider_id: str) -> tuple[str, str, str, str, str]:
        """The full comparison key for one provider id.

        Returned as a tuple rather than a joined string so no separator can ever
        be part of a component and make two different keys compare equal.
        """

        return (
            self.league_id,
            self.provider,
            self.generation,
            self.entity_type.value,
            provider_id,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "league_id": self.league_id,
            "provider": self.provider,
            "namespace_generation": self.generation,
            "entity_type": self.entity_type.value,
        }


def _canonical(payload: Mapping[str, Any]) -> str:
    """Canonical JSON for a digest input, with enums reduced to their values.

    ``canonical_json`` sorts keys, so the result is insensitive to dict ordering.
    Sequences keep their order, because for a list of digests order can be
    meaningful; callers that need order-independence sort before passing.
    """

    def reduce(value: Any) -> Any:
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, Mapping):
            return {str(k): reduce(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [reduce(v) for v in value]
        return value

    return canonical_json({str(k): reduce(v) for k, v in payload.items()})


def semantic_digest(payload: Mapping[str, Any]) -> str:
    """A stable SHA-256 over the semantic content of a provenance record.

    Insensitive to key ordering, to insertion order, and to the order SQLite
    returns rows in. Sensitive to every semantic field, so a changed policy
    version, source fingerprint or variant yields a different corpus identity.

    Callers exclude surrogate ids and audit wall-clocks. A provider-published
    snapshot instant is NOT excluded: it is evidence the provider stamped, so two
    corpora built from different snapshots must not share a digest.
    """

    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def canonical_detail_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON for a sanitized finding detail.

    Refuses anything that is not a scalar, list or mapping of scalars. Raw
    provider bodies are stored once, in ``raw_responses``; copying one into a
    finding would duplicate unsanitized content into a table that is read for
    reporting.
    """

    def check(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                check(item, f"{path}.{key}")
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                check(item, f"{path}[{index}]")
            return
        if value is None or isinstance(value, (bool, int, enum.Enum)):
            return
        if isinstance(value, float):
            # NaN/Infinity are not JSON. ``canonical_json`` would emit the bare
            # tokens ``NaN``/``Infinity``, which a strict RFC-8259 parser rejects
            # -- so the stored detail, and the digest over it, would not be
            # readable by anything but Python.
            if value != value or value in (float("inf"), float("-inf")):
                raise RetrospectiveProvenanceError(
                    f"finding detail {path} is {value!r}, which is not representable "
                    "in JSON; provenance must be readable outside this process"
                )
            return
        if isinstance(value, str):
            # A long free-text blob is how a raw body gets in by accident.
            if len(value) > _MAX_DETAIL_STRING:
                raise RetrospectiveProvenanceError(
                    f"finding detail {path} is {len(value)} characters; findings carry "
                    "stable codes and digests, never provider bodies"
                )
            _refuse_secret_shaped(value, path)
            return
        raise RetrospectiveProvenanceError(
            f"finding detail {path} has unsupported type {type(value).__name__}"
        )

    check(dict(payload), "detail")
    return _canonical(payload)


#: Long enough for a code, a digest or a short structured note; far too short
#: for a provider payload.
_MAX_DETAIL_STRING = 200

#: Shapes that must never reach a provenance record. The review found that a
#: short credential slipped straight through the length bound: an API key is
#: ~40 characters, well under the blob threshold. These are conservative
#: prefixes/markers rather than entropy heuristics, so a false positive is a
#: sentence telling the caller to store a digest instead -- never a silent pass.
_SECRET_MARKERS: Final[tuple[str, ...]] = (
    "sk_live_", "sk_test_", "pk_live_", "rk_live_",
    "bearer ", "basic ", "authorization:", "x-api-key",
    "api_key=", "apikey=", "access_token=", "token=", "password=", "secret=",
    "aws_secret", "aws_access_key", "private_key", "-----begin",
    "eyj",  # a base64url-encoded JWT header always starts {"alg...
)


def _refuse_secret_shaped(value: str, path: str) -> None:
    """Refuse a string that looks like a credential or a URL carrying one.

    A finding is meant to hold a stable code plus a digest. Nothing about a
    provenance record needs a token in it, so the safe default is to refuse
    anything credential-shaped rather than to store it and hope.
    """

    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise RetrospectiveProvenanceError(
                f"finding detail {path} contains a credential-shaped value "
                f"({marker!r}); findings carry stable codes and digests. Store a "
                "hash of the value if you need to correlate it."
            )
    if "://" in lowered and ("?" in lowered or "@" in lowered):
        raise RetrospectiveProvenanceError(
            f"finding detail {path} is a URL carrying a query string or userinfo; "
            "these routinely embed credentials. Store a stable citation key instead."
        )


def detail_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the sanitized canonical finding detail."""

    return hashlib.sha256(canonical_detail_json(payload).encode("utf-8")).hexdigest()
