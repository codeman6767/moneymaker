"""Orchestration for one retrospective identity-audit run (dry-run or apply).

Offline by construction: this module opens two SQLite files and no sockets. It
imports no provider client and reads no settings, so there is no code path from
here to a credential or a request.

Dry-run is a real dry run
-------------------------
It performs the identical audit -- the same scan, the same compatibility rules,
the same counts, the same findings, the same semantic digest -- and then writes
nothing at all: no canonical entity, no audit row, no finding, no crosswalk. It
is not a separate, weaker code path; it is the same computation with persistence
withheld, which is the only way a dry run can honestly predict an apply.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..db.engine import Database, transaction
from ..db.init import initialize_database
from ..db.repositories.retrospective import SqliteRetrospectiveProvenanceRepository
from ..db.schema import CURRENT_SCHEMA_VERSION
from .crosswalks import CrosswalkResult, generate_crosswalks
from .identity_audit import (
    AUDIT_POLICY_VERSION,
    AuditPlan,
    IdentityAuditError,
    audit_namespace,
    persist_audit_plan,
)
from .provenance import EntityType, G1Variant, ProvenanceClass, ProviderNamespace
from .sources import open_source_corpus, source_corpus_digest

__all__ = ["AuditRunResult", "run_identity_audit"]


@dataclass(frozen=True)
class AuditRunResult:
    """Everything one namespace audit produced, for reporting."""

    plan: AuditPlan
    applied: bool
    identity_audit_id: Optional[str]
    findings_written: int
    crosswalk: Optional[CrosswalkResult]
    corpus_version_id: Optional[str]

    def to_json(self) -> dict[str, Any]:
        plan = self.plan
        by_classification: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        for finding in plan.findings:
            by_classification[finding.classification.value] = (
                by_classification.get(finding.classification.value, 0) + 1)
            by_scope[finding.exclusion_scope.value] = (
                by_scope.get(finding.exclusion_scope.value, 0) + 1)
        payload: dict[str, Any] = {
            "source_corpus_digest": plan.source_corpus_digest,
            "namespace": plan.namespace.as_dict(),
            "namespace_verified": plan.namespace.verified,
            "audit_policy_version": plan.audit_policy_version,
            "distinct_ids": plan.distinct_ids,
            "total_observations": plan.total_observations,
            "collision_count": plan.collision_count,
            "flagged_count": plan.flagged_count,
            "verdict": plan.verdict.value,
            "findings_total": len(plan.findings),
            "findings_by_classification": dict(sorted(by_classification.items())),
            "findings_by_exclusion_scope": dict(sorted(by_scope.items())),
            "semantic_digest": plan.semantic_digest,
            "applied": self.applied,
            "identity_audit_id": self.identity_audit_id,
            "findings_written": self.findings_written,
            "corpus_version_id": self.corpus_version_id,
            # This engine has no provider-access path at all; the field is stated
            # so a report can be checked rather than trusted.
            "network_occurred": False,
        }
        if self.crosswalk is not None:
            payload["crosswalk"] = {
                "entity_type": self.crosswalk.entity_type.value,
                "supported": self.crosswalk.supported,
                "canonical_bootstrapped": self.crosswalk.canonical_bootstrapped,
                "crosswalks_written": self.crosswalk.crosswalks_written,
                "reused_existing": self.crosswalk.reused_existing,
                "blocked_reason": self.crosswalk.blocked_reason,
            }
        return payload


def _require_distinct_databases(source_db: Path, output_db: Path) -> None:
    """The source corpus is evidence; it may never also be the write target.

    Found by the independent review: `--source-db X --output-db X --apply` wrote
    provenance straight into the corpus being audited. The schema check happened
    to stop it for a v17 corpus, but that is incidental protection -- a v19 source
    was written to happily.

    Compares resolved paths AND the filesystem identity, so a hardlink or a
    symlink alias cannot smuggle the same file in under a second name.
    """

    if source_db.resolve() == output_db.resolve():
        raise IdentityAuditError(
            f"source and output are the same database ({source_db}). A historical "
            "corpus is read-only evidence and must never receive provenance."
        )
    try:
        source_stat, output_stat = source_db.stat(), output_db.stat()
    except OSError:
        return  # the output may not exist yet; the resolved-path check stands
    if (source_stat.st_dev, source_stat.st_ino) == (output_stat.st_dev,
                                                    output_stat.st_ino):
        raise IdentityAuditError(
            f"source {source_db} and output {output_db} are the same file "
            "(hardlink or alias). Provenance must never be written into evidence."
        )


def _require_output_schema(conn: sqlite3.Connection, path: Path) -> None:
    version = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
    if int(version or 0) != CURRENT_SCHEMA_VERSION:
        raise IdentityAuditError(
            f"output database {path} is schema v{version}, but provenance requires "
            f"v{CURRENT_SCHEMA_VERSION}. The SOURCE corpus is never migrated to run an "
            "audit -- create a fresh output database instead."
        )


def run_identity_audit(
    *,
    source_db: Path,
    output_db: Path,
    league_id: str,
    provider: str,
    namespace_generation: str,
    entity_type: EntityType,
    audit_policy_version: str = AUDIT_POLICY_VERSION,
    expected_source_corpus_digest: Optional[str] = None,
    apply: bool = False,
    build_crosswalks: bool = False,
    reconstruction_policy_version: str = "identity-audit-only-v1",
    cutoff_policy_id: str = "identity-audit-only",
    cutoff_policy_version: str = "1",
    target_set_digest: str = "identity-audit-no-targets",
    g1_variant: G1Variant = G1Variant.G1_B_CORE,
) -> AuditRunResult:
    """Audit one namespace over one corpus, optionally persisting the result.

    The source is opened read-only and is never migrated: a historical corpus may
    legitimately still be schema v17, and requiring a migration to audit it would
    mean writing to protected evidence.
    """

    _require_distinct_databases(source_db, output_db)
    source = open_source_corpus(source_db)
    try:
        digest = source_corpus_digest(source, league_id=league_id, provider=provider)
        if (expected_source_corpus_digest is not None
                and expected_source_corpus_digest != digest):
            raise IdentityAuditError(
                f"source corpus digest {digest} does not match the expected "
                f"{expected_source_corpus_digest}; the corpus is not the evidence this "
                "audit was authorized for"
            )
        namespace = ProviderNamespace(league_id, provider, entity_type,
                                      namespace_generation)
        plan = audit_namespace(
            source, namespace=namespace, source_corpus_digest=digest,
            audit_policy_version=audit_policy_version,
        )

        if not apply:
            crosswalk = None
            if build_crosswalks and plan.accepted:
                # Dry run still exercises the crosswalk path so the prediction is
                # real, against a throwaway in-memory database that is discarded.
                crosswalk = _dry_run_crosswalks(source, plan)
            return AuditRunResult(plan=plan, applied=False, identity_audit_id=None,
                                  findings_written=0, crosswalk=crosswalk,
                                  corpus_version_id=None)

        database = Database(output_db)
        with database.connection() as output:
            _require_output_schema(output, output_db)
            with transaction(output):
                audit_id, written = persist_audit_plan(output, plan)
                corpus_version_id = None
                crosswalk = None
                if build_crosswalks and plan.accepted:
                    repo = SqliteRetrospectiveProvenanceRepository(output)
                    corpus = repo.record_corpus_version(
                        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                        league_id=league_id,
                        reconstruction_policy_version=reconstruction_policy_version,
                        cutoff_policy_id=cutoff_policy_id,
                        cutoff_policy_version=cutoff_policy_version,
                        source_corpus_digest=digest,
                        target_set_digest=target_set_digest,
                        g1_variant=g1_variant,
                    )
                    corpus_version_id = corpus.corpus_version_id
                    crosswalk = generate_crosswalks(
                        output, source, plan=plan,
                        corpus_version_id=corpus_version_id,
                        identity_audit_id=audit_id,
                    )
            return AuditRunResult(
                plan=plan, applied=True, identity_audit_id=audit_id,
                findings_written=written, crosswalk=crosswalk,
                corpus_version_id=corpus_version_id,
            )
    finally:
        source.close()


def _dry_run_crosswalks(
    source: sqlite3.Connection, plan: AuditPlan
) -> CrosswalkResult:
    """Predict crosswalk generation without touching the real output database.

    The scratch database is a REAL, fully migrated schema-v19 database in a
    temporary directory, not a hand-written stub. The review found the stub
    version modelled ``players`` as two nullable columns with no CHECKs, so a dry
    run could have predicted a crosswalk the real output schema would refuse --
    which is precisely the failure a dry run exists to prevent.
    """

    with tempfile.TemporaryDirectory(prefix="lane_r_dry_run_") as directory:
        scratch_path = Path(directory) / "dry_run.db"
        initialize_database(scratch_path)
        scratch = sqlite3.connect(scratch_path)
        try:
            scratch.row_factory = sqlite3.Row
            return generate_crosswalks(
                scratch, source, plan=plan, corpus_version_id="rcv_dry_run",
                identity_audit_id="ida_dry_run", dry_run=True,
            )
        finally:
            scratch.close()
