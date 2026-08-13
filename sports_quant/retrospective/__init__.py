"""Lane R (retrospective research) domain vocabulary and availability policy.

This package is the schema-v18 provenance FOUNDATION and nothing more. It holds
the enumerations the provenance tables constrain themselves to, the code-defined
availability-rule registry, and the deterministic digest helpers that give a
reconstruction corpus a stable identity.

Deliberately absent, and each for a stated reason:

* **No ``RetrospectiveResearchReader``.** The reader belongs in the next
  implementation phase, after this storage contract has been independently
  reviewed. Building the consumer first would mean reviewing the contract by
  reading the code that already depends on it.
* **TEAM-A team attestation and canonical game bootstrap ARE implemented**
  (``attestations``, ``namespaces``, ``team_crosswalks``, ``game_bootstrap``,
  ``verifier``) as of 2026-08-12, and have **not** been independently reviewed.
* **The identity-audit engine IS implemented** (``identity_audit``, ``sources``,
  ``crosswalks``, ``runner``) as of 2026-08-12, and has **not** been
  independently reviewed. It audits game, team and person namespaces; crosswalk
  generation is supported for **person only**, because the canonical team
  dimension cannot be prepared deterministically without name matching (see
  ``crosswalks``).
* **No feature builder and no market client.** This lane certifies provenance;
  it computes no feature values and fetches nothing.

Nothing in this package touches the strict-forward point-in-time path. Lane L
still reads through ``AsOfReader`` exactly as before, and the v18 tables are
registered as ``unsupported`` joins so a dataset builder cannot reach them.
"""

from __future__ import annotations

from .provenance import (
    AuditVerdict,
    AvailabilityBasis,
    EligibilityVerdict,
    EntityType,
    ExclusionScope,
    FindingClassification,
    FindingSeverity,
    G1Variant,
    ProvenanceClass,
    ProviderNamespace,
    RetrospectiveProvenanceError,
    canonical_detail_json,
    detail_digest,
    semantic_digest,
)
from .rules import (
    AVAILABILITY_RULES,
    AvailabilityRule,
    UnknownAvailabilityRuleError,
    derive_availability_instant,
    lookup_rule,
    verify_rule_digest,
)
from .sources import (
    AUDITED_SOURCE_TABLES,
    SOURCE_DIGEST_POLICY_VERSION,
    GameObservation,
    PlayerObservation,
    SourceCorpusError,
    TeamObservation,
    open_source_corpus,
    source_corpus_digest,
)

__all__ = [
    "AUDITED_SOURCE_TABLES",
    "AUDIT_POLICY_VERSION",
    "AVAILABILITY_RULES",
    "AuditPlan",
    "AuditRunResult",
    "CROSSWALK_POLICY_VERSION",
    "CROSSWALK_SUPPORTED_ENTITY_TYPES",
    "CanonicalPreparationBlocked",
    "CrosswalkResult",
    "GameObservation",
    "IdentityAuditError",
    "PlannedFinding",
    "PlayerObservation",
    "SOURCE_DIGEST_POLICY_VERSION",
    "SourceCorpusError",
    "TeamObservation",
    "audit_namespace",
    "canonical_player_id",
    "generate_crosswalks",
    "open_source_corpus",
    "persist_audit_plan",
    "run_identity_audit",
    "source_corpus_digest",
    "AuditVerdict",
    "AvailabilityBasis",
    "AvailabilityRule",
    "EligibilityVerdict",
    "EntityType",
    "ExclusionScope",
    "FindingClassification",
    "FindingSeverity",
    "G1Variant",
    "ProvenanceClass",
    "ProviderNamespace",
    "RetrospectiveProvenanceError",
    "UnknownAvailabilityRuleError",
    "canonical_detail_json",
    "derive_availability_instant",
    "detail_digest",
    "lookup_rule",
    "semantic_digest",
    "verify_rule_digest",
]


# --------------------------------------------------------------------------- #
# Engine surface, resolved lazily
# --------------------------------------------------------------------------- #
# `identity_audit`, `crosswalks` and `runner` CONSUME `sports_quant.db`, whereas
# `provenance`/`rules`/`evidence` are consumed BY it. Importing both eagerly here
# put a cycle back in place: `db.repositories.retrospective` imports
# `retrospective.evidence`, which runs this module, which would import the engine,
# which imports `db.repositories` mid-initialization. PEP 562 keeps the ergonomic
# `from sports_quant.retrospective import run_identity_audit` while resolving the
# engine only when it is actually asked for.
_LAZY: dict[str, str] = {
    "MAP_FORMAT_VERSION": "attestations",
    "TEAM_ATTESTATIONS": "attestations",
    "TEAM_ATTESTATION_POLICY_VERSION": "attestations",
    "AttestationError": "attestations",
    "TeamAttestation": "attestations",
    "attestation_map_digest": "attestations",
    "attested_canonical_team": "attestations",
    "canonical_team_seed_digest": "attestations",
    "describe_map_shape": "attestations",
    "QUALIFIED_PROVIDERS": "namespaces",
    "QualifiedProvider": "namespaces",
    "qualified_provider": "namespaces",
    "qualified_provider_for": "namespaces",
    "TeamCrosswalkPlan": "team_crosswalks",
    "TeamCrosswalkResult": "team_crosswalks",
    "plan_team_crosswalks": "team_crosswalks",
    "write_team_crosswalks": "team_crosswalks",
    "GAME_BOOTSTRAP_POLICY_VERSION": "game_bootstrap",
    "GameBootstrapPlan": "game_bootstrap",
    "GameBootstrapResult": "game_bootstrap",
    "canonical_game_id": "game_bootstrap",
    "plan_game_bootstrap": "game_bootstrap",
    "write_game_bootstrap": "game_bootstrap",
    "VerificationReport": "verifier",
    "verify_corpus": "verifier",
    "verify_database": "verifier",
    "AUDIT_POLICY_VERSION": "identity_audit",
    "AuditPlan": "identity_audit",
    "IdentityAuditError": "identity_audit",
    "PlannedFinding": "identity_audit",
    "audit_namespace": "identity_audit",
    "persist_audit_plan": "identity_audit",
    "CROSSWALK_POLICY_VERSION": "crosswalks",
    "CROSSWALK_SUPPORTED_ENTITY_TYPES": "crosswalks",
    "CanonicalPreparationBlocked": "crosswalks",
    "CrosswalkResult": "crosswalks",
    "canonical_player_id": "crosswalks",
    "generate_crosswalks": "crosswalks",
    "AuditRunResult": "runner",
    "run_identity_audit": "runner",
}


def __getattr__(name: str) -> object:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f".{module_name}", __name__), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
