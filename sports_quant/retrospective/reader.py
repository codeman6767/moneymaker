"""``RetrospectiveResearchReader`` -- the Lane-R reader (architecture §12).

Two readers share one leakage contract and differ only in admissible evidence:

* ``pit.asof.AsOfReader`` -- strict forward. Evidence: ``observed_at`` /
  ``decided_at``. **Untouched by this module.**
* ``RetrospectiveResearchReader`` -- this one. Evidence: ``effective_at``,
  *derived* from the reviewed availability taxonomy.

Lane selection is a **distinct type, not a flag**. There is deliberately no
``ignore_pit=``, no ``retrospective=``, no mode boolean anywhere: an unsafe read
has to be a different object, because a flag makes every call site responsible
for remembering which lane it is in, and the default eventually wins.

What this reader is, and is not
-------------------------------
It is an **admission decision procedure**. It answers *"may family F for target
game G in corpus C be used at cutoff T, and on what recorded basis?"* and returns
the provenance needed to defend that answer.

It does **not** compute feature values, aggregate anything, or read a feature out
of an evidence table. That is feature engineering and is not authorized. The
separation is also principled: whether a fact was knowable is a provenance
question, and mixing it with what the fact equals is how the two stop being
independently checkable.

Where the gates live
--------------------
Nothing here re-derives a policy that is already recorded. The reader composes
existing, independently reviewed machinery:

1. ``families`` -- is this family retrospectively usable at all? FORWARD_ONLY is
   refused before any database access.
2. ``reconstructed_input_provenance`` -- is there a persisted certification for
   this exact corpus, namespace, target game and family? An in-memory claim is
   never enough.
3. ``rules.derive_availability_instant`` -- for EVENT_DERIVED, ``effective_at``
   is derived on read from the completion instant and the digest-bound rule.
   It is never stored, so it cannot go stale or be quietly backdated.
4. ``static_crosswalk_provenance`` -- static identity resolves only through the
   corpus's own audited crosswalk.

Then, and only then, ``effective_at <= T_cut``.
"""

from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass
from typing import Any, Final, Optional, TypeVar

from ..db.models import (
    ReconstructedInputProvenance,
    StaticCrosswalkProvenance,
)
from ..db.repositories.retrospective import SqliteRetrospectiveProvenanceRepository, semantic_digest
from ..db.schema import from_iso
from .evidence import evidence_id_column
from .families import (
    FamilyClass,
    FeatureFamily,
    ForwardOnlyFamilyError,
    lookup_family,
)
from .provenance import (
    AvailabilityBasis,
    EligibilityVerdict,
    EntityType,
    ProvenanceClass,
    ProviderNamespace,
    RetrospectiveProvenanceError,
)
from .rules import derive_availability_instant

__all__ = [
    "READER_POLICY_VERSION",
    "AdmissionOutcome",
    "AdmittedInput",
    "LaneRAdmissionError",
    "RejectedInput",
    "RetrospectiveResearchReader",
    "StaticIdentity",
]

#: Bump this when the ADMISSION MEANING changes, not when prose moves. A reader
#: decision recorded under v1 must always mean what v1 meant.
READER_POLICY_VERSION: Final = "lane-r-reader-v1"

_E = TypeVar("_E", bound=enum.Enum)


def _parse(enum_cls: type[_E], raw: str, field_name: str) -> _E:
    """Parse a stored string into its enum member, failing closed.

    The provenance models hold these columns as plain ``str``. Comparing a plain
    string to an enum member with ``is`` is False forever, which would silently
    admit an EXCLUDED certification, so every such value is parsed explicitly
    here and an unrecognized one is refused rather than defaulted.
    """

    try:
        return enum_cls(raw)
    except ValueError:
        raise LaneRAdmissionError(
            f"certification carries {field_name}={raw!r}, which this build does "
            f"not recognize (known: {sorted(m.value for m in enum_cls)}). A value "
            "with no known meaning cannot be admitted."
        ) from None


class LaneRAdmissionError(RetrospectiveProvenanceError):
    """The reader was asked for something it must refuse outright."""


class AdmissionOutcome(str, enum.Enum):
    """Why one family was admitted or refused. Every refusal is nameable."""

    ADMITTED = "admitted"
    #: Structural refusals -- no provenance is even consulted.
    FORWARD_ONLY_FAMILY = "forward_only_family"
    WRONG_LEAGUE_FOR_FAMILY = "wrong_league_for_family"
    #: Provenance refusals.
    NO_CERTIFICATION = "no_certification"
    CERTIFIED_EXCLUDED = "certified_excluded"
    WRONG_LANE = "wrong_lane"
    BASIS_CONTRADICTS_FAMILY = "basis_contradicts_family"
    MISSING_CROSSWALK = "missing_crosswalk"
    CROSSWALK_FROM_ANOTHER_CORPUS = "crosswalk_from_another_corpus"
    CROSSWALK_DIGEST_MISMATCH = "crosswalk_digest_mismatch"
    CORPUS_SUPERSEDED = "corpus_superseded"
    #: Availability refusals.
    NOT_YET_AVAILABLE = "not_yet_available"
    TARGET_GAME_SELF_REFERENCE = "target_game_self_reference"
    #: Label handling.
    LABEL_REQUESTED_AS_FEATURE = "label_requested_as_feature"
    FEATURE_REQUESTED_AS_LABEL = "feature_requested_as_label"


@dataclass(frozen=True)
class AdmittedInput:
    """One family the reader will vouch for, with the defence attached.

    Everything here is either read from a persisted record or derived
    deterministically from one. Nothing is duplicated that a caller could
    recompute from ``certification``.
    """

    feature_family: str
    family_class: FamilyClass
    corpus_version_id: str
    namespace: ProviderNamespace
    provider_game_id: str
    provenance_class: ProvenanceClass
    availability_basis: AvailabilityBasis
    #: ``None`` for STATIC_IDENTITY: a timeless fact has no instant, and
    #: inventing one would be the backdating this design exists to prevent.
    effective_at: Optional[str]
    cutoff: str
    availability_rule_id: Optional[str]
    availability_rule_digest: Optional[str]
    reader_policy_version: str
    #: True when the corpus admits correction-sensitive extended evidence, so a
    #: caller can never describe this as transaction-time-exact by accident.
    correction_sensitive: bool
    certification: ReconstructedInputProvenance

    @property
    def outcome(self) -> AdmissionOutcome:
        return AdmissionOutcome.ADMITTED

    def as_json(self) -> dict[str, object]:
        return {
            "outcome": AdmissionOutcome.ADMITTED.value,
            "feature_family": self.feature_family,
            "family_class": self.family_class.value,
            "corpus_version_id": self.corpus_version_id,
            "namespace": self.namespace.as_dict(),
            "provider_game_id": self.provider_game_id,
            "provenance_class": self.provenance_class.value,
            "availability_basis": self.availability_basis.value,
            "effective_at": self.effective_at,
            "cutoff": self.cutoff,
            "availability_rule_id": self.availability_rule_id,
            "availability_rule_digest": self.availability_rule_digest,
            "reader_policy_version": self.reader_policy_version,
            "correction_sensitive": self.correction_sensitive,
            "input_provenance_id": self.certification.input_provenance_id,
            "source_evidence_table": self.certification.source_evidence_table,
            "source_evidence_id": self.certification.source_evidence_id,
        }


@dataclass(frozen=True)
class RejectedInput:
    """One family the reader refuses, and the reason in the caller's terms."""

    feature_family: str
    outcome: AdmissionOutcome
    detail: str
    corpus_version_id: str
    provider_game_id: str
    cutoff: str
    effective_at: Optional[str] = None

    def as_json(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "feature_family": self.feature_family,
            "detail": self.detail,
            "corpus_version_id": self.corpus_version_id,
            "provider_game_id": self.provider_game_id,
            "cutoff": self.cutoff,
            "effective_at": self.effective_at,
        }


@dataclass(frozen=True)
class StaticIdentity:
    """A canonical entity resolved through the corpus's own audited crosswalk."""

    provider_id: str
    entity_type: EntityType
    canonical_entity_id: str
    corpus_version_id: str
    crosswalk_id: str
    identity_audit_id: str
    provenance_policy_version: str


@dataclass(frozen=True)
class AdmissionReport:
    """The full decision set for one target game."""

    corpus_version_id: str
    namespace: ProviderNamespace
    provider_game_id: str
    cutoff: str
    admitted: tuple[AdmittedInput, ...] = ()
    rejected: tuple[RejectedInput, ...] = ()
    reader_policy_version: str = READER_POLICY_VERSION

    def as_json(self) -> dict[str, object]:
        return {
            "corpus_version_id": self.corpus_version_id,
            "namespace": self.namespace.as_dict(),
            "provider_game_id": self.provider_game_id,
            "cutoff": self.cutoff,
            "reader_policy_version": self.reader_policy_version,
            "admitted": [a.as_json() for a in self.admitted],
            "rejected": [r.as_json() for r in self.rejected],
        }


class RetrospectiveResearchReader:
    """Admission decisions for Lane-R evidence, at one cutoff, in one corpus.

    Construction binds a corpus and a cutoff, so a single reader instance cannot
    be talked into answering for two different reconstructions. The connection is
    used read-only: this class issues SELECTs and nothing else.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        corpus_version_id: str,
        cutoff: str,
    ) -> None:
        self._conn = conn
        self._repo = SqliteRetrospectiveProvenanceRepository(conn)
        self._corpus_version_id = corpus_version_id
        # Parsed eagerly so a malformed cutoff fails at construction rather than
        # comparing as a string somewhere deep in an admission decision.
        self._cutoff_dt = from_iso(cutoff)
        self._cutoff = cutoff

        corpus = self._repo.corpus_version(corpus_version_id)
        if corpus is None:
            raise LaneRAdmissionError(
                f"corpus version {corpus_version_id!r} does not exist; a reader "
                "cannot admit evidence for a reconstruction that was never recorded"
            )
        # Parsed, not compared with `is`: the model holds this as a plain str,
        # so an identity check against the enum would be False forever and a
        # strict-forward corpus would sail straight through the lane gate.
        if (_parse(ProvenanceClass, corpus.provenance_class, "provenance_class")
                is ProvenanceClass.STRICT_FORWARD_PIT):
            raise LaneRAdmissionError(
                f"corpus {corpus_version_id!r} is strict-forward; forward evidence "
                "is read through AsOfReader and never through this lane"
            )
        superseding = self._repo.superseded_by(corpus_version_id)
        if superseding:
            raise LaneRAdmissionError(
                f"corpus {corpus_version_id!r} has been superseded by "
                f"{[c.corpus_version_id for c in superseding]}. Reading a superseded "
                "reconstruction would attribute results to evidence that has since "
                "been replaced."
            )
        self._corpus = corpus

    # -- properties ---------------------------------------------------------- #
    @property
    def cutoff(self) -> str:
        return self._cutoff

    @property
    def corpus_version_id(self) -> str:
        return self._corpus_version_id

    @property
    def correction_sensitive(self) -> bool:
        """Does this corpus admit correction-sensitive extended evidence?

        ``G1_A_EXTENDED`` results may never be described as transaction-time-exact
        (architecture §"core vs extended"). Surfaced on every admission so a
        downstream caller cannot lose the distinction.
        """

        from .provenance import G1Variant

        # Same plain-string trap as the lane gate. Getting this wrong would
        # report correction-sensitive extended evidence as core -- exactly the
        # honesty failure the core/extended split exists to prevent.
        return (_parse(G1Variant, self._corpus.g1_variant, "g1_variant")
                is G1Variant.G1_A_EXTENDED)


    def _crosswalk_integrity_error(
        self, crosswalk: StaticCrosswalkProvenance
    ) -> Optional[str]:
        """Recompute a crosswalk's semantic digest from its own stored contents.

        Review repair R2. Before this, the STATIC_IDENTITY path checked only that
        the crosswalk existed and named this corpus, so direct SQL that flipped
        ``canonical_entity_id`` produced an ADMITTED identity pointing at the
        wrong franchise -- and ``static_identity()`` returned that wrong id.

        The out-of-band verifier already catches it, but a reader whose whole job
        is admission cannot depend on someone remembering to run a CLI later. So
        the check is done in-band, here, on every identity read.

        The attestation map digest is optional in the stored payload (player
        crosswalks have no map), so both forms are tried: a row is intact if it
        matches EITHER the plain payload or the payload bound to the currently
        committed TEAM-A map. A row matching neither has been altered since it
        was written, or was built under a different map -- both fail closed.
        """

        base: dict[str, Any] = {
            "kind": "static_crosswalk",
            "corpus_version_id": crosswalk.corpus_version_id,
            **ProviderNamespace(
                crosswalk.league_id, crosswalk.provider,
                _parse(EntityType, crosswalk.entity_type, "entity_type"),
                crosswalk.namespace_generation).as_dict(),
            "provider_id": crosswalk.provider_id,
            "canonical_entity_id": crosswalk.canonical_entity_id,
            "identity_audit_digest": crosswalk.identity_audit_digest,
            "provenance_policy_version": crosswalk.provenance_policy_version,
        }
        candidates = [semantic_digest(base)]
        try:
            from .attestations import attestation_map_digest

            candidates.append(semantic_digest(
                {**base, "attestation_map_digest": attestation_map_digest()}))
        except Exception:      # pragma: no cover - map import is not required
            pass
        if crosswalk.semantic_digest in candidates:
            return None
        return (
            f"crosswalk {crosswalk.crosswalk_id!r} does not match its own "
            f"semantic digest; its stored contents have been altered since it "
            "was written, or it was built under a different attestation map"
        )

    # -- static identity ------------------------------------------------------ #
    def static_identity(
        self, *, namespace: ProviderNamespace, provider_id: str
    ) -> StaticIdentity:
        """Resolve one provider id through THIS corpus's audited crosswalk.

        Not wall-clock gated, and correctly so: a static identity is timeless, and
        gating it on the cutoff would be a category error dressed up as caution.
        What is gated is the *provenance* -- the crosswalk must belong to this
        corpus and cite an accepted audit, which the schema enforces on write.

        No name, alias, abbreviation or fuzzy match is consulted here or
        anywhere beneath here.
        """

        crosswalk = self._repo.static_crosswalk(
            corpus_version_id=self._corpus_version_id, namespace=namespace,
            provider_id=provider_id)
        if crosswalk is None:
            raise LaneRAdmissionError(
                f"no static crosswalk for {namespace.key(provider_id)} in corpus "
                f"{self._corpus_version_id!r}. Identity reaches Lane R only through "
                "an audited crosswalk; there is no name-matching fallback."
            )
        # Defensive: the write path and schema already guarantee this, but a
        # reader that trusts its inputs is how a corpus boundary leaks.
        if crosswalk.corpus_version_id != self._corpus_version_id:
            raise LaneRAdmissionError(
                f"crosswalk {crosswalk.crosswalk_id!r} belongs to corpus "
                f"{crosswalk.corpus_version_id!r}, not {self._corpus_version_id!r}")
        problem = self._crosswalk_integrity_error(crosswalk)
        if problem is not None:
            raise LaneRAdmissionError(problem)
        return StaticIdentity(
            provider_id=provider_id, entity_type=namespace.entity_type,
            canonical_entity_id=crosswalk.canonical_entity_id,
            corpus_version_id=crosswalk.corpus_version_id,
            crosswalk_id=crosswalk.crosswalk_id,
            identity_audit_id=crosswalk.identity_audit_id,
            provenance_policy_version=crosswalk.provenance_policy_version,
        )

    # -- admission ------------------------------------------------------------ #
    def admit_feature(
        self, *, namespace: ProviderNamespace, provider_game_id: str,
        feature_family: str,
    ) -> AdmittedInput | RejectedInput:
        """Decide one family as a PREDICTIVE INPUT for one target game."""

        return self._decide(namespace=namespace, provider_game_id=provider_game_id,
                            feature_family=feature_family, as_label=False)

    def admit_label(
        self, *, namespace: ProviderNamespace, provider_game_id: str,
        feature_family: str,
    ) -> AdmittedInput | RejectedInput:
        """Decide one family as a TARGET/LABEL.

        A separate method on purpose: the architecture requires that a label can
        never be obtained by the same call that obtains a feature, so the caller
        has to say which it wants and the reader can refuse the mismatch.
        """

        return self._decide(namespace=namespace, provider_game_id=provider_game_id,
                            feature_family=feature_family, as_label=True)

    def admit_features(
        self, *, namespace: ProviderNamespace, provider_game_id: str,
        feature_families: tuple[str, ...],
    ) -> AdmissionReport:
        """Decide several families at once, preserving every refusal reason.

        Deterministic: families are decided in sorted order, so two runs over the
        same corpus produce byte-identical reports.
        """

        admitted: list[AdmittedInput] = []
        rejected: list[RejectedInput] = []
        for family in sorted(set(feature_families)):
            decision = self.admit_feature(
                namespace=namespace, provider_game_id=provider_game_id,
                feature_family=family)
            (admitted if isinstance(decision, AdmittedInput) else
             rejected).append(decision)   # type: ignore[arg-type]
        return AdmissionReport(
            corpus_version_id=self._corpus_version_id, namespace=namespace,
            provider_game_id=provider_game_id, cutoff=self._cutoff,
            admitted=tuple(admitted), rejected=tuple(rejected))

    # -- the decision procedure ----------------------------------------------- #
    def _reject(self, family: str, outcome: AdmissionOutcome, detail: str,
                provider_game_id: str,
                effective_at: Optional[str] = None) -> RejectedInput:
        return RejectedInput(
            feature_family=family, outcome=outcome, detail=detail,
            corpus_version_id=self._corpus_version_id,
            provider_game_id=provider_game_id, cutoff=self._cutoff,
            effective_at=effective_at)

    def _decide(
        self, *, namespace: ProviderNamespace, provider_game_id: str,
        feature_family: str, as_label: bool,
    ) -> AdmittedInput | RejectedInput:
        # Review repair R1. `reconstructed_input_provenance` has no entity_type
        # column -- correctly, since a certification is about a feature family
        # for a target GAME. The API took a full namespace anyway and silently
        # ignored that component, so a caller passing a TEAM namespace quietly
        # received game-scoped certifications. An argument the type checker reads
        # and the runtime ignores is an invitation to misuse, so it is now
        # required to be what it actually means.
        if namespace.entity_type is not EntityType.GAME:
            raise LaneRAdmissionError(
                f"admission is per TARGET GAME, so it requires a game namespace; "
                f"got entity_type={namespace.entity_type.value!r}. Entity identity "
                "is resolved through static_identity(), which does use the entity "
                "type."
            )
        # ---- 1. structural: the family itself ------------------------------ #
        # Before any database access. A FORWARD_ONLY family is not "filtered";
        # there is no path from here to one.
        family = lookup_family(feature_family)

        if family.classification is FamilyClass.FORWARD_ONLY:
            raise ForwardOnlyFamilyError(
                f"{feature_family!r} is FORWARD_ONLY: it has no trustworthy "
                "retrospective availability evidence, so the Lane-R reader cannot "
                "return it at any cutoff, under any provenance. Read it through "
                "AsOfReader in the forward lane or not at all."
            )
        if family.league_id is not None and family.league_id != namespace.league_id:
            return self._reject(
                feature_family, AdmissionOutcome.WRONG_LEAGUE_FOR_FAMILY,
                f"{feature_family!r} is defined for {family.league_id!r}, not "
                f"{namespace.league_id!r}", provider_game_id)

        # ---- 2. label / feature must match what was asked for --------------- #
        is_label_family = family.classification is FamilyClass.LABEL_ONLY
        if is_label_family and not as_label:
            return self._reject(
                feature_family, AdmissionOutcome.LABEL_REQUESTED_AS_FEATURE,
                f"{feature_family!r} is LABEL_ONLY. It is the outcome being "
                "predicted and can never be a predictive input; request it with "
                "admit_label() if you want the target.", provider_game_id)
        if as_label and not is_label_family:
            return self._reject(
                feature_family, AdmissionOutcome.FEATURE_REQUESTED_AS_LABEL,
                f"{feature_family!r} is a predictive input, not a label",
                provider_game_id)

        # ---- 3. persisted certification, for THIS exact corpus -------------- #
        cert = self._repo.certified_input(
            corpus_version_id=self._corpus_version_id, namespace=namespace,
            provider_game_id=provider_game_id, feature_family=feature_family)
        if cert is None:
            return self._reject(
                feature_family, AdmissionOutcome.NO_CERTIFICATION,
                "no persisted certification for this corpus, namespace, target "
                "game and family; an uncertified input is an unproven claim",
                provider_game_id)
        # The repository returns these as PLAIN STRINGS, not enum members, so an
        # `is` comparison against an enum would be silently False forever -- and
        # an EXCLUDED certification would have been admitted. Parse once, here,
        # and fail closed on any value this build does not recognize.
        eligibility = _parse(EligibilityVerdict, cert.eligibility, "eligibility")
        provenance_class = _parse(ProvenanceClass, cert.provenance_class,
                                  "provenance_class")

        if eligibility is EligibilityVerdict.EXCLUDED:
            return self._reject(
                feature_family, AdmissionOutcome.CERTIFIED_EXCLUDED,
                f"certified EXCLUDED ({cert.exclusion_code or 'no code'})",
                provider_game_id)

        # ---- 4. the lane must match the request ----------------------------- #
        if as_label:
            if provenance_class is not ProvenanceClass.LABEL_ONLY_RETROSPECTIVE:
                return self._reject(
                    feature_family, AdmissionOutcome.WRONG_LANE,
                    f"label requested but certification is "
                    f"{provenance_class.value!r}", provider_game_id)
            return self._admit(family, cert, namespace, provider_game_id,
                               provenance_class=provenance_class,
                               basis=None, effective_at=None)
        if provenance_class is not ProvenanceClass.RECONSTRUCTED_RESEARCH:
            return self._reject(
                feature_family, AdmissionOutcome.WRONG_LANE,
                f"feature requested but certification is "
                f"{provenance_class.value!r}; a label-only record has no "
                "availability story and cannot become a feature", provider_game_id)

        basis = (None if cert.availability_basis is None
                 else _parse(AvailabilityBasis, cert.availability_basis,
                             "availability_basis"))
        if basis is None or basis not in family.admissible_bases:
            return self._reject(
                feature_family, AdmissionOutcome.BASIS_CONTRADICTS_FAMILY,
                f"certified basis {basis.value if basis else None!r} is not "
                f"admissible for a {family.classification.value} family "
                f"(expected one of "
                f"{sorted(b.value for b in family.admissible_bases)})",
                provider_game_id)

        # ---- 5. availability, per basis ------------------------------------- #
        if basis is AvailabilityBasis.STATIC_IDENTITY:
            # Timeless. The crosswalk is the gate, not the clock.
            if cert.crosswalk_id is None:
                return self._reject(
                    feature_family, AdmissionOutcome.MISSING_CROSSWALK,
                    "STATIC_IDENTITY without a crosswalk has no identity "
                    "provenance at all", provider_game_id)
            owner = self._crosswalk_corpus(cert.crosswalk_id)
            if owner is None:
                return self._reject(
                    feature_family, AdmissionOutcome.MISSING_CROSSWALK,
                    f"crosswalk {cert.crosswalk_id!r} does not exist",
                    provider_game_id)
            if owner != self._corpus_version_id:
                return self._reject(
                    feature_family, AdmissionOutcome.CROSSWALK_FROM_ANOTHER_CORPUS,
                    f"crosswalk {cert.crosswalk_id!r} belongs to corpus {owner!r}; "
                    "an audit of one corpus never authorizes another",
                    provider_game_id)
            cited = self._repo.static_crosswalk_by_id(cert.crosswalk_id)
            if cited is None:
                return self._reject(
                    feature_family, AdmissionOutcome.MISSING_CROSSWALK,
                    f"crosswalk {cert.crosswalk_id!r} does not exist",
                    provider_game_id)
            problem = self._crosswalk_integrity_error(cited)
            if problem is not None:
                return self._reject(
                    feature_family, AdmissionOutcome.CROSSWALK_DIGEST_MISMATCH,
                    problem, provider_game_id)
            return self._admit(family, cert, namespace, provider_game_id,
                               provenance_class=provenance_class,
                               basis=basis, effective_at=None)

        if basis is AvailabilityBasis.EVENT_DERIVED:
            completed = cert.source_event_completed_at
            if completed is None or cert.availability_rule_id is None:
                return self._reject(
                    feature_family, AdmissionOutcome.BASIS_CONTRADICTS_FAMILY,
                    "EVENT_DERIVED without a completion instant and a rule",
                    provider_game_id)
            # A prior-event feature may never be derived from the target game's
            # own evidence. The cutoff check below would normally catch it, but
            # this is the leak the architecture names explicitly, so it is
            # refused structurally too rather than relying on arithmetic.
            if self._evidence_is_target_game(cert, provider_game_id):
                return self._reject(
                    feature_family, AdmissionOutcome.TARGET_GAME_SELF_REFERENCE,
                    f"the cited evidence row belongs to target game "
                    f"{provider_game_id!r}; a game's own statistics can never "
                    "predict it", provider_game_id)
            effective_at = derive_availability_instant(
                rule_id=cert.availability_rule_id,
                rule_digest=cert.availability_rule_digest or "",
                source_event_completed_at=completed)
        else:   # VERSIONED_SNAPSHOT
            snapshot_at = cert.source_snapshot_at
            if snapshot_at is None:
                return self._reject(
                    feature_family, AdmissionOutcome.BASIS_CONTRADICTS_FAMILY,
                    "VERSIONED_SNAPSHOT without a provider snapshot instant",
                    provider_game_id)
            effective_at = snapshot_at

        # ---- 6. the cutoff ---------------------------------------------------#
        if from_iso(effective_at) > self._cutoff_dt:
            return self._reject(
                feature_family, AdmissionOutcome.NOT_YET_AVAILABLE,
                f"effective_at {effective_at} is after the cutoff {self._cutoff}",
                provider_game_id, effective_at=effective_at)
        return self._admit(family, cert, namespace, provider_game_id,
                           provenance_class=provenance_class,
                           basis=basis, effective_at=effective_at)

    def _admit(
        self, family: FeatureFamily, cert: ReconstructedInputProvenance,
        namespace: ProviderNamespace, provider_game_id: str, *,
        provenance_class: ProvenanceClass,
        basis: Optional[AvailabilityBasis], effective_at: Optional[str],
    ) -> AdmittedInput:
        return AdmittedInput(
            feature_family=family.name, family_class=family.classification,
            corpus_version_id=self._corpus_version_id, namespace=namespace,
            provider_game_id=provider_game_id,
            provenance_class=provenance_class,
            # A label has no basis; the dataclass keeps the field non-optional for
            # features by only ever reaching here with one for those.
            availability_basis=basis or AvailabilityBasis.STATIC_IDENTITY,
            effective_at=effective_at, cutoff=self._cutoff,
            availability_rule_id=cert.availability_rule_id,
            availability_rule_digest=cert.availability_rule_digest,
            reader_policy_version=READER_POLICY_VERSION,
            correction_sensitive=self.correction_sensitive,
            certification=cert,
        )

    # -- helpers -------------------------------------------------------------- #
    def _crosswalk_corpus(self, crosswalk_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT corpus_version_id FROM static_crosswalk_provenance "
            "WHERE crosswalk_id = ?", (crosswalk_id,)).fetchone()
        return None if row is None else str(row[0])

    def _evidence_is_target_game(
        self, cert: ReconstructedInputProvenance, provider_game_id: str
    ) -> bool:
        """Does the cited evidence row belong to the target game itself?

        Only answerable for evidence tables that carry ``provider_game_id``; for
        the rest (``raw_responses``, ``roster_snapshots``,
        ``sportsbook_price_snapshots``, ``lineup_players``) the temporal gate is
        the control. Returning False there is honest rather than pretending to a
        check that cannot be made.
        """

        table, row_id = cert.source_evidence_table, cert.source_evidence_id
        if table is None or row_id is None:
            return False
        try:
            id_column = evidence_id_column(table)
        except RetrospectiveProvenanceError:
            return False
        columns = {
            str(r[1]) for r in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if "provider_game_id" not in columns:
            return False
        row = self._conn.execute(
            f"SELECT provider_game_id FROM {table} WHERE {id_column} = ?",
            (row_id,)).fetchone()
        return row is not None and str(row[0]) == provider_game_id
