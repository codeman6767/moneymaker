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
* **No identity-audit engine.** ``identity_audit_records`` stores the *result*
  of the G5 audit; the corpus-scanning implementation is separate work.
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

__all__ = [
    "AVAILABILITY_RULES",
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
