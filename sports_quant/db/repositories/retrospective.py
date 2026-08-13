"""Append-only repositories for the Lane-R provenance tables (f018).

Five tables, one repository each in spirit and one class in practice, because
they form a single referential unit: a crosswalk is meaningless without the
audit that cleared it, and a certification is meaningless without its corpus.

Design rules this module holds to (task §21), and why:

* **Explicit creation, no upsert.** Every ``record_*`` inserts or refuses.
  Provenance is evidence; an upsert would let a second, different claim
  overwrite the first and leave no trace that it ever existed.
* **Deterministic lookup only.** Reads are by exact id or exact
  ``(corpus version, namespace, key)``. There is deliberately no "latest wins"
  and no fuzzy match: "the most recent crosswalk" is not a scientific answer,
  and picking one silently is how a corpus stops being reproducible.
* **Not-found is None; ambiguous raises.** A missing record is a normal answer.
  Two records that should have been one is a corruption, and it fails closed.
* **Immutable reads.** Every read returns a frozen dataclass, never a live row.
* **Idempotent replay, by digest.** Recording the identical record twice returns
  the existing row and writes nothing, because the semantic digest already
  identifies it. Recording a *different* record under the same natural key
  raises -- that is a contradiction, not an update.

The SQL lives here and nowhere else, so a schema change has one blast radius.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ...retrospective.evidence import require_source_evidence_table
from ...retrospective.provenance import (
    AuditVerdict,
    AvailabilityBasis,
    EligibilityVerdict,
    ExclusionScope,
    FindingClassification,
    FindingSeverity,
    G1Variant,
    ProvenanceClass,
    ProviderNamespace,
    canonical_detail_json,
    detail_digest,
    semantic_digest,
)
from ...retrospective.rules import lookup_rule
from ..ids import (
    new_identity_audit_id,
    new_identity_finding_id,
    new_reconstructed_input_id,
    new_reconstruction_corpus_id,
    new_static_crosswalk_id,
)
from ..models import (
    IdentityAuditFinding,
    IdentityAuditRecord,
    ReconstructedInputProvenance,
    ReconstructionCorpusVersion,
    StaticCrosswalkProvenance,
)
from ..schema import utc_now_iso
from .base import Repository, RepositoryError

__all__ = [
    "AmbiguousProvenanceError",
    "ProvenanceConflictError",
    "SqliteRetrospectiveProvenanceRepository",
]

_CORPUS_TABLE = "reconstruction_corpus_versions"
_AUDIT_TABLE = "identity_audit_records"
_FINDING_TABLE = "identity_audit_findings"
_CROSSWALK_TABLE = "static_crosswalk_provenance"
_INPUT_TABLE = "reconstructed_input_provenance"


class ProvenanceConflictError(RepositoryError):
    """A second, different claim was made under an existing natural key."""


class AmbiguousProvenanceError(RepositoryError):
    """A deterministic lookup matched more than one row.

    Structurally impossible while the f018 UNIQUE constraints hold. Checked
    anyway, because the alternative to noticing corruption is silently using the
    first row SQLite happens to return.
    """


class SqliteRetrospectiveProvenanceRepository(Repository):
    """Storage for the five append-only Lane-R provenance tables."""

    # -- corpus versions ----------------------------------------------------- #
    def record_corpus_version(
        self,
        *,
        provenance_class: ProvenanceClass,
        league_id: str,
        reconstruction_policy_version: str,
        cutoff_policy_id: str,
        cutoff_policy_version: str,
        source_corpus_digest: str,
        target_set_digest: str,
        g1_variant: G1Variant,
        evidence_registry_digest: Optional[str] = None,
        static_identity_map_digest: Optional[str] = None,
        market_evidence_digest: Optional[str] = None,
        code_version: Optional[str] = None,
        supersedes_corpus_version_id: Optional[str] = None,
    ) -> ReconstructionCorpusVersion:
        """Append one corpus version, or return the identical existing one.

        Supersession appends: the superseded row is never touched, so every
        experiment already attributed to it stays attributable.
        """

        if provenance_class is ProvenanceClass.STRICT_FORWARD_PIT:
            raise RepositoryError(
                "a strict-forward corpus is not a reconstruction; forward evidence is "
                "read through AsOfReader and never recorded in the Lane-R tables"
            )
        digest = semantic_digest({
            "kind": "reconstruction_corpus_version",
            "provenance_class": provenance_class,
            "league_id": league_id,
            "reconstruction_policy_version": reconstruction_policy_version,
            "cutoff_policy_id": cutoff_policy_id,
            "cutoff_policy_version": cutoff_policy_version,
            "source_corpus_digest": source_corpus_digest,
            "target_set_digest": target_set_digest,
            "evidence_registry_digest": evidence_registry_digest,
            "static_identity_map_digest": static_identity_map_digest,
            "market_evidence_digest": market_evidence_digest,
            "g1_variant": g1_variant,
            "code_version": code_version,
            "supersedes_corpus_version_id": supersedes_corpus_version_id,
        })
        existing = self._fetch_one(
            f"SELECT * FROM {_CORPUS_TABLE} WHERE semantic_digest = ?", (digest,)
        )
        if existing is not None:
            return self._to_corpus(existing)

        if supersedes_corpus_version_id is not None:
            prior = self.corpus_version(supersedes_corpus_version_id)
            if prior is None:
                raise RepositoryError(
                    f"cannot supersede unknown corpus version "
                    f"{supersedes_corpus_version_id!r}"
                )
            if prior.league_id != league_id:
                raise RepositoryError(
                    "a corpus version may only supersede one for the same league "
                    f"(superseding {prior.league_id!r} with {league_id!r})"
                )

        corpus_version_id = new_reconstruction_corpus_id()
        self._conn.execute(
            f"INSERT INTO {_CORPUS_TABLE} "
            "(corpus_version_id, provenance_class, league_id, "
            " reconstruction_policy_version, cutoff_policy_id, cutoff_policy_version, "
            " source_corpus_digest, target_set_digest, evidence_registry_digest, "
            " static_identity_map_digest, market_evidence_digest, g1_variant, "
            " code_version, semantic_digest, supersedes_corpus_version_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (corpus_version_id, provenance_class.value, league_id,
             reconstruction_policy_version, cutoff_policy_id, cutoff_policy_version,
             source_corpus_digest, target_set_digest, evidence_registry_digest,
             static_identity_map_digest, market_evidence_digest, g1_variant.value,
             code_version, digest, supersedes_corpus_version_id, utc_now_iso()),
        )
        return self._require_corpus(corpus_version_id)

    def corpus_version(self, corpus_version_id: str) -> Optional[ReconstructionCorpusVersion]:
        """Exact lookup by id. ``None`` when absent -- never a nearest match."""

        row = self._fetch_one(
            f"SELECT * FROM {_CORPUS_TABLE} WHERE corpus_version_id = ?",
            (corpus_version_id,),
        )
        return None if row is None else self._to_corpus(row)

    def corpus_version_by_digest(self, digest: str) -> Optional[ReconstructionCorpusVersion]:
        """Exact lookup by semantic digest -- the corpus's real identity."""

        rows = self._fetch_all(
            f"SELECT * FROM {_CORPUS_TABLE} WHERE semantic_digest = ?", (digest,)
        )
        return self._exactly_one(rows, f"corpus version digest {digest[:16]}...",
                                 self._to_corpus)

    def superseded_by(self, corpus_version_id: str) -> tuple[ReconstructionCorpusVersion, ...]:
        """Every corpus version that declares this one superseded."""

        rows = self._fetch_all(
            f"SELECT * FROM {_CORPUS_TABLE} WHERE supersedes_corpus_version_id = ? "
            "ORDER BY corpus_version_id",
            (corpus_version_id,),
        )
        return tuple(self._to_corpus(r) for r in rows)

    # -- identity audits ----------------------------------------------------- #
    def record_identity_audit(
        self,
        *,
        namespace: ProviderNamespace,
        source_corpus_digest: str,
        audit_policy_version: str,
        distinct_ids: int,
        total_observations: int,
        collision_count: int,
        verdict: AuditVerdict,
        flagged_count: int = 0,
    ) -> IdentityAuditRecord:
        """Append one audit result, or return the identical existing one.

        Refuses an ACCEPTED verdict that contradicts its own counts before SQLite
        does, so the caller gets a sentence rather than a CHECK abort. The
        database enforces it too: this is the G5 fail-closed rule, and it should
        hold against any writer.
        """

        if verdict is AuditVerdict.ACCEPTED:
            if collision_count != 0:
                raise RepositoryError(
                    f"cannot accept an identity audit with {collision_count} "
                    "collisions; G5 fails closed on any incompatibility"
                )
            if not namespace.verified:
                raise RepositoryError(
                    "cannot accept an identity audit whose provider API generation is "
                    "unverified; record it as rejected_namespace_unverified instead"
                )
        if verdict is AuditVerdict.REJECTED_COLLISION and collision_count == 0:
            raise RepositoryError(
                "rejected_collision requires at least one collision; use a different "
                "verdict for a clean audit"
            )
        if verdict is AuditVerdict.REJECTED_NAMESPACE_UNVERIFIED and namespace.verified:
            raise RepositoryError(
                "rejected_namespace_unverified requires an unverified generation"
            )

        digest = semantic_digest({
            "kind": "identity_audit",
            **namespace.as_dict(),
            "namespace_verified": namespace.verified,
            "source_corpus_digest": source_corpus_digest,
            "audit_policy_version": audit_policy_version,
            "distinct_ids": distinct_ids,
            "total_observations": total_observations,
            "collision_count": collision_count,
            "flagged_count": flagged_count,
            "verdict": verdict,
        })
        existing = self._fetch_one(
            f"SELECT * FROM {_AUDIT_TABLE} WHERE semantic_digest = ?", (digest,)
        )
        if existing is not None:
            return self._to_audit(existing)

        identity_audit_id = new_identity_audit_id()
        self._conn.execute(
            f"INSERT INTO {_AUDIT_TABLE} "
            "(identity_audit_id, league_id, provider, namespace_generation, "
            " namespace_verified, entity_type, source_corpus_digest, "
            " audit_policy_version, distinct_ids, total_observations, collision_count, "
            " flagged_count, verdict, semantic_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identity_audit_id, namespace.league_id, namespace.provider,
             namespace.generation, 1 if namespace.verified else 0,
             namespace.entity_type.value, source_corpus_digest, audit_policy_version,
             distinct_ids, total_observations, collision_count, flagged_count,
             verdict.value, digest, utc_now_iso()),
        )
        row = self._fetch_one(
            f"SELECT * FROM {_AUDIT_TABLE} WHERE identity_audit_id = ?",
            (identity_audit_id,),
        )
        if row is None:  # pragma: no cover - insert just succeeded
            raise RepositoryError("identity audit vanished immediately after insert")
        return self._to_audit(row)

    def identity_audit(self, identity_audit_id: str) -> Optional[IdentityAuditRecord]:
        row = self._fetch_one(
            f"SELECT * FROM {_AUDIT_TABLE} WHERE identity_audit_id = ?",
            (identity_audit_id,),
        )
        return None if row is None else self._to_audit(row)

    def accepted_audit_for(
        self, namespace: ProviderNamespace, *, source_corpus_digest: str
    ) -> Optional[IdentityAuditRecord]:
        """The accepted audit for this exact namespace over this exact corpus.

        Exact on all five components. An audit of a different source corpus is a
        different statement and is not returned as a near-enough answer -- that
        is the whole content of the G5 scope limit.
        """

        rows = self._fetch_all(
            f"SELECT * FROM {_AUDIT_TABLE} WHERE league_id = ? AND provider = ? "
            "AND namespace_generation = ? AND entity_type = ? "
            "AND source_corpus_digest = ? AND verdict = 'accepted'",
            (namespace.league_id, namespace.provider, namespace.generation,
             namespace.entity_type.value, source_corpus_digest),
        )
        return self._exactly_one(
            rows,
            f"accepted audit for {namespace.key('*')} over "
            f"{source_corpus_digest[:16]}...",
            self._to_audit,
        )

    # -- findings ------------------------------------------------------------ #
    def record_finding(
        self,
        *,
        identity_audit_id: str,
        namespace: ProviderNamespace,
        severity: FindingSeverity,
        finding_code: str,
        classification: FindingClassification,
        exclusion_scope: ExclusionScope,
        provider_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> IdentityAuditFinding:
        """Append one sanitized finding, or return the identical existing one.

        ``detail`` is validated to be scalars/lists/mappings of bounded strings.
        Raw provider bodies live in ``raw_responses``; copying one here would
        duplicate unsanitized content into a table that gets read for reporting.
        """

        parent = self.identity_audit(identity_audit_id)
        if parent is None:
            raise RepositoryError(
                f"unknown identity audit {identity_audit_id!r}; a finding cannot "
                "belong to an audit that does not exist"
            )
        # An ACCEPTED audit is a completed statement that the namespace is clean
        # over its corpus. It may carry flags; it may not later be handed a
        # finding asserting the opposite of its own verdict, because crosswalks
        # already built from it would silently keep their authority.
        contradicts = (
            severity is FindingSeverity.BLOCKING
            or classification in (FindingClassification.IDENTITY_COLLISION,
                                  FindingClassification.NAMESPACE_UNVERIFIED)
            or exclusion_scope is not ExclusionScope.NONE
        )
        if contradicts and parent.verdict == AuditVerdict.ACCEPTED.value:
            raise ProvenanceConflictError(
                f"identity audit {identity_audit_id!r} is ACCEPTED with "
                f"{parent.collision_count} collisions; refusing to append a "
                f"{classification.value}/{severity.value} finding that contradicts "
                "it. Record a NEW audit over the corpus -- accepted evidence is "
                "never rewritten."
            )
        detail_payload = dict(detail or {})
        detail_text = canonical_detail_json(detail_payload)
        digest = detail_digest(detail_payload)
        existing = self._fetch_one(
            f"SELECT * FROM {_FINDING_TABLE} WHERE identity_audit_id = ? "
            "AND entity_type = ? AND provider_id IS ? AND finding_code = ? "
            "AND detail_digest = ?",
            (identity_audit_id, namespace.entity_type.value, provider_id,
             finding_code, digest),
        )
        if existing is not None:
            return self._to_finding(existing)

        finding_id = new_identity_finding_id()
        self._conn.execute(
            f"INSERT INTO {_FINDING_TABLE} "
            "(finding_id, identity_audit_id, league_id, provider, "
            " namespace_generation, entity_type, provider_id, severity, finding_code, "
            " classification, exclusion_scope, detail_json, detail_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (finding_id, identity_audit_id, namespace.league_id, namespace.provider,
             namespace.generation, namespace.entity_type.value, provider_id,
             severity.value, finding_code, classification.value,
             exclusion_scope.value, detail_text, digest, utc_now_iso()),
        )
        row = self._fetch_one(
            f"SELECT * FROM {_FINDING_TABLE} WHERE finding_id = ?", (finding_id,)
        )
        if row is None:  # pragma: no cover - insert just succeeded
            raise RepositoryError("finding vanished immediately after insert")
        return self._to_finding(row)

    def findings_for_audit(self, identity_audit_id: str) -> tuple[IdentityAuditFinding, ...]:
        """Every finding of one audit, in a deterministic order."""

        rows = self._fetch_all(
            f"SELECT * FROM {_FINDING_TABLE} WHERE identity_audit_id = ? "
            "ORDER BY severity, entity_type, COALESCE(provider_id, ''), finding_code, "
            "detail_digest",
            (identity_audit_id,),
        )
        return tuple(self._to_finding(r) for r in rows)

    # -- static crosswalks --------------------------------------------------- #
    def record_static_crosswalk(
        self,
        *,
        corpus_version_id: str,
        namespace: ProviderNamespace,
        provider_id: str,
        canonical_entity_id: str,
        identity_audit_id: str,
        provenance_policy_version: str,
        curated_at: Optional[str] = None,
        attestation_map_digest: Optional[str] = None,
    ) -> StaticCrosswalkProvenance:
        """Bind one provider key to a canonical entity, under a cleared audit.

        Refuses anything but an ACCEPTED audit for the identical namespace. The
        audit's digest is copied into the row so the binding survives an export
        and a mismatch is detectable without a join; f018 re-checks all of it in
        a trigger.

        ``curated_at`` defaults to now and is AUDIT time. It is not backdated and
        it is not a reused ``decided_at`` -- a matcher wall-clock is not a
        historical effective time.

        ``attestation_map_digest`` (review repair RV1) binds a crosswalk to the
        exact source-controlled map it came from, by participating in the semantic
        digest. It is **optional and omitted by default**, so player crosswalks --
        which are derived from the provider key alone and have no map -- keep
        their existing digests and remain valid. Passing it makes a crosswalk
        built under map M cryptographically distinct from one built under map M'.
        """

        audit = self.identity_audit(identity_audit_id)
        if audit is None:
            raise RepositoryError(
                f"unknown identity audit {identity_audit_id!r}; a crosswalk cannot "
                "cite an audit that does not exist"
            )
        if audit.verdict != AuditVerdict.ACCEPTED.value:
            raise RepositoryError(
                f"identity audit {identity_audit_id!r} has verdict {audit.verdict!r}; "
                "only an accepted audit clears a namespace for crosswalk use"
            )
        corpus = self.corpus_version(corpus_version_id)
        if corpus is None:
            raise RepositoryError(
                f"unknown corpus version {corpus_version_id!r}; a crosswalk cannot "
                "belong to a reconstruction that does not exist"
            )
        # The defect the independent review of f018 proved: without this, a clean
        # ONE-MONTH audit could vouch for a five-season corpus. G5 §16 -- a
        # narrower window's pass never transfers to a wider one.
        if audit.source_corpus_digest != corpus.source_corpus_digest:
            raise RepositoryError(
                f"identity audit {identity_audit_id!r} examined source corpus "
                f"{audit.source_corpus_digest!r}, but corpus version "
                f"{corpus_version_id!r} is built over "
                f"{corpus.source_corpus_digest!r}. An audit is only ever a statement "
                "about the evidence it actually read; re-run it over this corpus."
            )
        if (audit.league_id, audit.provider, audit.namespace_generation,
                audit.entity_type) != (namespace.league_id, namespace.provider,
                                       namespace.generation,
                                       namespace.entity_type.value):
            raise RepositoryError(
                f"identity audit {identity_audit_id!r} cleared "
                f"({audit.league_id}, {audit.provider}, {audit.namespace_generation}, "
                f"{audit.entity_type}), which is not the namespace being bound "
                f"({namespace.league_id}, {namespace.provider}, "
                f"{namespace.generation}, {namespace.entity_type.value})"
            )

        digest_payload: dict[str, Any] = {
            "kind": "static_crosswalk",
            "corpus_version_id": corpus_version_id,
            **namespace.as_dict(),
            "provider_id": provider_id,
            "canonical_entity_id": canonical_entity_id,
            "identity_audit_digest": audit.semantic_digest,
            "provenance_policy_version": provenance_policy_version,
        }
        if attestation_map_digest is not None:
            # Only present for map-backed (TEAM-A) crosswalks, so existing
            # player-crosswalk digests are byte-identical to before.
            digest_payload["attestation_map_digest"] = attestation_map_digest
        digest = semantic_digest(digest_payload)
        existing = self._fetch_one(
            f"SELECT * FROM {_CROSSWALK_TABLE} WHERE semantic_digest = ?", (digest,)
        )
        if existing is not None:
            return self._to_crosswalk(existing)

        conflict = self.static_crosswalk(
            corpus_version_id=corpus_version_id, namespace=namespace,
            provider_id=provider_id,
        )
        if conflict is not None:
            raise ProvenanceConflictError(
                f"provider key {namespace.key(provider_id)} is already bound to "
                f"{conflict.canonical_entity_id!r} in corpus {corpus_version_id!r}; a "
                f"different answer ({canonical_entity_id!r}) is a contradiction, not an "
                "update. Record it in a new corpus version."
            )

        crosswalk_id = new_static_crosswalk_id()
        self._conn.execute(
            f"INSERT INTO {_CROSSWALK_TABLE} "
            "(crosswalk_id, corpus_version_id, league_id, provider, "
            " namespace_generation, entity_type, provider_id, canonical_entity_id, "
            " identity_audit_id, identity_audit_digest, provenance_policy_version, "
            " semantic_digest, curated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (crosswalk_id, corpus_version_id, namespace.league_id, namespace.provider,
             namespace.generation, namespace.entity_type.value, provider_id,
             canonical_entity_id, identity_audit_id, audit.semantic_digest,
             provenance_policy_version, digest, curated_at or utc_now_iso(),
             utc_now_iso()),
        )
        row = self._fetch_one(
            f"SELECT * FROM {_CROSSWALK_TABLE} WHERE crosswalk_id = ?", (crosswalk_id,)
        )
        if row is None:  # pragma: no cover - insert just succeeded
            raise RepositoryError("crosswalk vanished immediately after insert")
        return self._to_crosswalk(row)

    def static_crosswalk(
        self,
        *,
        corpus_version_id: str,
        namespace: ProviderNamespace,
        provider_id: str,
    ) -> Optional[StaticCrosswalkProvenance]:
        """Exact lookup on the full five-part key within one corpus version."""

        rows = self._fetch_all(
            f"SELECT * FROM {_CROSSWALK_TABLE} WHERE corpus_version_id = ? "
            "AND league_id = ? AND provider = ? AND namespace_generation = ? "
            "AND entity_type = ? AND provider_id = ?",
            (corpus_version_id, namespace.league_id, namespace.provider,
             namespace.generation, namespace.entity_type.value, provider_id),
        )
        return self._exactly_one(
            rows, f"crosswalk {namespace.key(provider_id)} in {corpus_version_id}",
            self._to_crosswalk,
        )

    # -- reconstructed input certifications ---------------------------------- #
    def static_crosswalk_by_id(
        self, crosswalk_id: str
    ) -> Optional[StaticCrosswalkProvenance]:
        """Fetch one crosswalk by its own id.

        Needed by the Lane-R reader, which cites a crosswalk by id and must
        re-derive that row's semantic digest before trusting its canonical
        target.
        """

        row = self._fetch_one(
            f"SELECT * FROM {_CROSSWALK_TABLE} WHERE crosswalk_id = ?",
            (crosswalk_id,),
        )
        return None if row is None else self._to_crosswalk(row)

    def certify_input(
        self,
        *,
        corpus_version_id: str,
        namespace: ProviderNamespace,
        provider_game_id: str,
        feature_family: str,
        provenance_class: ProvenanceClass,
        reconstruction_policy_version: str,
        eligibility: EligibilityVerdict,
        availability_basis: Optional[AvailabilityBasis] = None,
        availability_rule_id: Optional[str] = None,
        availability_source: Optional[str] = None,
        source_evidence_table: Optional[str] = None,
        source_evidence_id: Optional[str] = None,
        source_event_completed_at: Optional[str] = None,
        source_snapshot_at: Optional[str] = None,
        crosswalk_id: Optional[str] = None,
        exclusion_code: Optional[str] = None,
    ) -> ReconstructedInputProvenance:
        """Certify (or refuse) one input family for one target game.

        No feature value is stored: this records whether an input MAY be used and
        on what basis, not what it equals.

        The per-basis obligations are checked here and again by f018 CHECKs. The
        rule digest is resolved from the code-defined registry rather than
        accepted from the caller, so a provenance row can never claim a rule
        version this build does not implement.
        """

        if provenance_class is ProvenanceClass.STRICT_FORWARD_PIT:
            raise RepositoryError(
                "FORWARD_ONLY evidence cannot be certified as retrospective research; "
                "it is read through AsOfReader and never enters the Lane-R path"
            )
        if (provenance_class is ProvenanceClass.RECONSTRUCTED_RESEARCH
                and availability_basis is None):
            raise RepositoryError(
                f"reconstructed research input {feature_family!r} needs an availability "
                "basis; an input with no stated basis is an unproven claim"
            )
        if provenance_class is ProvenanceClass.LABEL_ONLY_RETROSPECTIVE:
            if availability_basis is not None or crosswalk_id is not None:
                raise RepositoryError(
                    "a retrospective label has no availability story; it is "
                    "distinguishable from a predictive input precisely because it "
                    "carries no basis, rule or crosswalk"
                )
            if source_event_completed_at is not None or source_snapshot_at is not None:
                raise RepositoryError(
                    "a retrospective label carries no availability timestamps"
                )
        if (eligibility is EligibilityVerdict.EXCLUDED) != (exclusion_code is not None):
            raise RepositoryError(
                "an excluded input must carry an exclusion code and an eligible one "
                "must not; eligibility and its reason are mutually determined"
            )

        rule_digest = self._resolve_rule_digest(availability_basis, availability_rule_id)
        self._check_evidence_pointer(
            eligibility, availability_basis,
            source_evidence_table=source_evidence_table,
            source_evidence_id=source_evidence_id,
            availability_source=availability_source,
        )
        self._check_basis_shape(
            availability_basis, crosswalk_id=crosswalk_id,
            source_event_completed_at=source_event_completed_at,
            source_snapshot_at=source_snapshot_at,
        )

        digest = semantic_digest({
            "kind": "reconstructed_input",
            "corpus_version_id": corpus_version_id,
            **namespace.as_dict(),
            "provider_game_id": provider_game_id,
            "feature_family": feature_family,
            "provenance_class": provenance_class,
            "availability_basis": availability_basis,
            "availability_rule_id": availability_rule_id,
            "availability_rule_digest": rule_digest,
            "availability_source": availability_source,
            "reconstruction_policy_version": reconstruction_policy_version,
            "source_evidence_table": source_evidence_table,
            "source_evidence_id": source_evidence_id,
            # Source evidence, published by the provider -- part of the identity.
            "source_event_completed_at": source_event_completed_at,
            "source_snapshot_at": source_snapshot_at,
            "crosswalk_id": crosswalk_id,
            "eligibility": eligibility,
            "exclusion_code": exclusion_code,
        })
        existing = self._fetch_one(
            f"SELECT * FROM {_INPUT_TABLE} WHERE semantic_digest = ?", (digest,)
        )
        if existing is not None:
            return self._to_input(existing)

        conflict = self.certified_input(
            corpus_version_id=corpus_version_id, namespace=namespace,
            provider_game_id=provider_game_id, feature_family=feature_family,
        )
        if conflict is not None:
            raise ProvenanceConflictError(
                f"{feature_family!r} is already certified for {provider_game_id!r} in "
                f"corpus {corpus_version_id!r} with a different provenance; a changed "
                "certification is a new corpus version, not an overwrite"
            )

        # `namespace.entity_type` describes the crosswalk class, not the target;
        # the target of a certification is always a game.
        input_provenance_id = new_reconstructed_input_id()
        self._conn.execute(
            f"INSERT INTO {_INPUT_TABLE} "
            "(input_provenance_id, corpus_version_id, league_id, provider, "
            " namespace_generation, provider_game_id, feature_family, "
            " provenance_class, availability_basis, availability_rule_id, "
            " availability_rule_digest, availability_source, "
            " reconstruction_policy_version, source_evidence_table, "
            " source_evidence_id, source_event_completed_at, source_snapshot_at, "
            " crosswalk_id, eligibility, exclusion_code, semantic_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (input_provenance_id, corpus_version_id, namespace.league_id,
             namespace.provider, namespace.generation, provider_game_id,
             feature_family, provenance_class.value,
             None if availability_basis is None else availability_basis.value,
             availability_rule_id, rule_digest, availability_source,
             reconstruction_policy_version, source_evidence_table, source_evidence_id,
             source_event_completed_at, source_snapshot_at, crosswalk_id,
             eligibility.value, exclusion_code, digest, utc_now_iso()),
        )
        row = self._fetch_one(
            f"SELECT * FROM {_INPUT_TABLE} WHERE input_provenance_id = ?",
            (input_provenance_id,),
        )
        if row is None:  # pragma: no cover - insert just succeeded
            raise RepositoryError("input certification vanished immediately after insert")
        return self._to_input(row)

    def certified_input(
        self,
        *,
        corpus_version_id: str,
        namespace: ProviderNamespace,
        provider_game_id: str,
        feature_family: str,
    ) -> Optional[ReconstructedInputProvenance]:
        """Exact lookup of one certification. No latest-wins, no fuzzy match."""

        rows = self._fetch_all(
            f"SELECT * FROM {_INPUT_TABLE} WHERE corpus_version_id = ? "
            "AND league_id = ? AND provider = ? AND namespace_generation = ? "
            "AND provider_game_id = ? AND feature_family = ?",
            (corpus_version_id, namespace.league_id, namespace.provider,
             namespace.generation, provider_game_id, feature_family),
        )
        return self._exactly_one(
            rows,
            f"certification {feature_family!r} for {provider_game_id!r} in "
            f"{corpus_version_id}",
            self._to_input,
        )

    def certifications_for_corpus(
        self, corpus_version_id: str
    ) -> tuple[ReconstructedInputProvenance, ...]:
        """Every certification in one corpus, in a deterministic order."""

        rows = self._fetch_all(
            f"SELECT * FROM {_INPUT_TABLE} WHERE corpus_version_id = ? "
            "ORDER BY provider_game_id, feature_family, semantic_digest",
            (corpus_version_id,),
        )
        return tuple(self._to_input(r) for r in rows)

    # -- validation helpers -------------------------------------------------- #
    @staticmethod
    def _resolve_rule_digest(
        basis: Optional[AvailabilityBasis], rule_id: Optional[str]
    ) -> Optional[str]:
        """Resolve a cited rule to this build's digest, failing closed.

        The digest is taken from the code registry, never from the caller: a
        caller-supplied digest could assert a rule version that does not exist
        here, which is the opposite of reproducible.
        """

        if rule_id is None:
            if basis is AvailabilityBasis.EVENT_DERIVED:
                raise RepositoryError(
                    "an EVENT_DERIVED input must cite an availability rule; without a "
                    "versioned rule the completion timestamp implies nothing"
                )
            return None
        if basis is not AvailabilityBasis.EVENT_DERIVED:
            raise RepositoryError(
                f"availability rule {rule_id!r} was cited for basis "
                f"{basis.value if basis else None!r}; rules apply to EVENT_DERIVED only"
            )
        # Raises UnknownAvailabilityRuleError when this build cannot reproduce the
        # rule, so a corpus can never cite a policy that does not exist here. The
        # stored digest is re-verified on READ (``derive_availability_instant``),
        # which is where a later edit to the rule must fail closed.
        return lookup_rule(rule_id).digest

    def _check_evidence_pointer(
        self,
        eligibility: EligibilityVerdict,
        basis: Optional[AvailabilityBasis],
        *,
        source_evidence_table: Optional[str],
        source_evidence_id: Optional[str],
        availability_source: Optional[str],
    ) -> None:
        """Traceability, proven rather than asserted (f019 D3/D4).

        An EXCLUDED row is exempt: "not admissible" is often precisely a statement
        that the evidence does not exist, and demanding a pointer to absent
        evidence would force a fabricated one.
        """

        if source_evidence_table is not None:
            id_column = require_source_evidence_table(source_evidence_table)
            if source_evidence_id is not None:
                # The table name came from a fixed allowlist, never from a caller
                # string, so interpolating it here cannot be an injection.
                row = self._fetch_one(
                    f"SELECT 1 FROM {source_evidence_table} "  # noqa: S608
                    f"WHERE {id_column} = ?",
                    (source_evidence_id,),
                )
                if row is None:
                    raise RepositoryError(
                        f"source evidence {source_evidence_table}.{id_column} = "
                        f"{source_evidence_id!r} does not exist; a provenance pointer "
                        "that resolves to nothing is not provenance"
                    )
        if eligibility is not EligibilityVerdict.ELIGIBLE:
            return
        if basis is not AvailabilityBasis.STATIC_IDENTITY and source_evidence_id is None:
            raise RepositoryError(
                "an eligible reconstructed input must cite preserved source evidence; "
                "a completion timestamp or a provider snapshot stamp is evidence about "
                "AVAILABILITY, not proof that the source data exists"
            )
        if basis in (AvailabilityBasis.EVENT_DERIVED,
                     AvailabilityBasis.VERSIONED_SNAPSHOT) and not (
                availability_source or "").strip():
            raise RepositoryError(
                f"an eligible {basis.value} input must name the evidence documenting "
                "its availability claim; a stated lag or a snapshot cadence without a "
                "documented basis is an unsupported assertion"
            )

    @staticmethod
    def _check_basis_shape(
        basis: Optional[AvailabilityBasis],
        *,
        crosswalk_id: Optional[str],
        source_event_completed_at: Optional[str],
        source_snapshot_at: Optional[str],
    ) -> None:
        """The three per-basis obligations, stated once."""

        if basis is AvailabilityBasis.STATIC_IDENTITY:
            if crosswalk_id is None:
                raise RepositoryError(
                    "a STATIC_IDENTITY input must cite the static crosswalk that "
                    "resolved it; the crosswalk is the entire evidence"
                )
            if source_event_completed_at is not None or source_snapshot_at is not None:
                raise RepositoryError(
                    "a STATIC_IDENTITY input carries no timestamps; an identity that "
                    "needed an effective time would not be static"
                )
        elif basis is AvailabilityBasis.EVENT_DERIVED:
            if source_event_completed_at is None:
                raise RepositoryError(
                    "an EVENT_DERIVED input must record the instant its source event "
                    "completed; that instant is what the availability rule advances"
                )
            if source_snapshot_at is not None:
                raise RepositoryError(
                    "an EVENT_DERIVED input has no provider snapshot timestamp"
                )
        elif basis is AvailabilityBasis.VERSIONED_SNAPSHOT:
            if source_snapshot_at is None:
                raise RepositoryError(
                    "a VERSIONED_SNAPSHOT input must record the provider's published "
                    "snapshot instant; that stamp is the availability evidence"
                )
            if source_event_completed_at is not None:
                raise RepositoryError(
                    "a VERSIONED_SNAPSHOT input derives availability from the "
                    "provider's stamp, not from an event completion"
                )

    def _require_corpus(self, corpus_version_id: str) -> ReconstructionCorpusVersion:
        corpus = self.corpus_version(corpus_version_id)
        if corpus is None:  # pragma: no cover - insert just succeeded
            raise RepositoryError("corpus version vanished immediately after insert")
        return corpus

    @staticmethod
    def _exactly_one(rows: list[sqlite3.Row], what: str, convert: Any) -> Any:
        """None for no match, the record for one, and a hard failure for many."""

        if not rows:
            return None
        if len(rows) > 1:
            raise AmbiguousProvenanceError(
                f"{len(rows)} rows matched {what}; a deterministic provenance lookup "
                "must match at most one, so this is refused rather than resolved"
            )
        return convert(rows[0])

    # -- row mapping --------------------------------------------------------- #
    def _to_corpus(self, row: sqlite3.Row) -> ReconstructionCorpusVersion:
        return ReconstructionCorpusVersion(
            corpus_version_id=str(row["corpus_version_id"]),
            provenance_class=str(row["provenance_class"]),
            league_id=str(row["league_id"]),
            reconstruction_policy_version=str(row["reconstruction_policy_version"]),
            cutoff_policy_id=str(row["cutoff_policy_id"]),
            cutoff_policy_version=str(row["cutoff_policy_version"]),
            source_corpus_digest=str(row["source_corpus_digest"]),
            target_set_digest=str(row["target_set_digest"]),
            g1_variant=str(row["g1_variant"]),
            semantic_digest=str(row["semantic_digest"]),
            created_at=str(row["created_at"]),
            evidence_registry_digest=self._opt_str(row, "evidence_registry_digest"),
            static_identity_map_digest=self._opt_str(row, "static_identity_map_digest"),
            market_evidence_digest=self._opt_str(row, "market_evidence_digest"),
            code_version=self._opt_str(row, "code_version"),
            supersedes_corpus_version_id=self._opt_str(
                row, "supersedes_corpus_version_id"),
        )

    def _to_audit(self, row: sqlite3.Row) -> IdentityAuditRecord:
        return IdentityAuditRecord(
            identity_audit_id=str(row["identity_audit_id"]),
            league_id=str(row["league_id"]),
            provider=str(row["provider"]),
            namespace_generation=str(row["namespace_generation"]),
            namespace_verified=bool(row["namespace_verified"]),
            entity_type=str(row["entity_type"]),
            source_corpus_digest=str(row["source_corpus_digest"]),
            audit_policy_version=str(row["audit_policy_version"]),
            distinct_ids=int(row["distinct_ids"]),
            total_observations=int(row["total_observations"]),
            collision_count=int(row["collision_count"]),
            flagged_count=int(row["flagged_count"]),
            verdict=str(row["verdict"]),
            semantic_digest=str(row["semantic_digest"]),
            created_at=str(row["created_at"]),
        )

    def _to_finding(self, row: sqlite3.Row) -> IdentityAuditFinding:
        return IdentityAuditFinding(
            finding_id=str(row["finding_id"]),
            identity_audit_id=str(row["identity_audit_id"]),
            league_id=str(row["league_id"]),
            provider=str(row["provider"]),
            namespace_generation=str(row["namespace_generation"]),
            entity_type=str(row["entity_type"]),
            severity=str(row["severity"]),
            finding_code=str(row["finding_code"]),
            classification=str(row["classification"]),
            exclusion_scope=str(row["exclusion_scope"]),
            detail_json=str(row["detail_json"]),
            detail_digest=str(row["detail_digest"]),
            created_at=str(row["created_at"]),
            provider_id=self._opt_str(row, "provider_id"),
        )

    def _to_crosswalk(self, row: sqlite3.Row) -> StaticCrosswalkProvenance:
        return StaticCrosswalkProvenance(
            crosswalk_id=str(row["crosswalk_id"]),
            corpus_version_id=str(row["corpus_version_id"]),
            league_id=str(row["league_id"]),
            provider=str(row["provider"]),
            namespace_generation=str(row["namespace_generation"]),
            entity_type=str(row["entity_type"]),
            provider_id=str(row["provider_id"]),
            canonical_entity_id=str(row["canonical_entity_id"]),
            identity_audit_id=str(row["identity_audit_id"]),
            identity_audit_digest=str(row["identity_audit_digest"]),
            provenance_policy_version=str(row["provenance_policy_version"]),
            semantic_digest=str(row["semantic_digest"]),
            curated_at=str(row["curated_at"]),
            created_at=str(row["created_at"]),
        )

    def _to_input(self, row: sqlite3.Row) -> ReconstructedInputProvenance:
        return ReconstructedInputProvenance(
            input_provenance_id=str(row["input_provenance_id"]),
            corpus_version_id=str(row["corpus_version_id"]),
            league_id=str(row["league_id"]),
            provider=str(row["provider"]),
            namespace_generation=str(row["namespace_generation"]),
            provider_game_id=str(row["provider_game_id"]),
            feature_family=str(row["feature_family"]),
            provenance_class=str(row["provenance_class"]),
            reconstruction_policy_version=str(row["reconstruction_policy_version"]),
            eligibility=str(row["eligibility"]),
            semantic_digest=str(row["semantic_digest"]),
            created_at=str(row["created_at"]),
            availability_basis=self._opt_str(row, "availability_basis"),
            availability_rule_id=self._opt_str(row, "availability_rule_id"),
            availability_rule_digest=self._opt_str(row, "availability_rule_digest"),
            availability_source=self._opt_str(row, "availability_source"),
            source_evidence_table=self._opt_str(row, "source_evidence_table"),
            source_evidence_id=self._opt_str(row, "source_evidence_id"),
            source_event_completed_at=self._opt_str(row, "source_event_completed_at"),
            source_snapshot_at=self._opt_str(row, "source_snapshot_at"),
            crosswalk_id=self._opt_str(row, "crosswalk_id"),
            exclusion_code=self._opt_str(row, "exclusion_code"),
        )
