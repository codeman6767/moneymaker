"""Independent review of the v18 provenance foundation: defect reproducers (f019).

Every test here was written as a FAILING reproducer against schema v18 before the
repair existed, and each names the defect it pins. They are deliberately split
between the repository API and raw SQL, because f018's central claim is that its
invariants are "enforced by the database, not caller discipline" -- a claim that
only means something if the raw-SQL half also refuses.

Defects proven and closed:

  D1  cross-corpus audit reuse (a one-month audit vouching for five seasons)
  D2  a contradictory blocking finding appended to an ACCEPTED audit
  D3  an eligible reconstructed input with no source-evidence pointer
  D4  an arbitrary string accepted as ``source_evidence_table``
  D5  shape-only timestamps (month 99, Feb 30, lower-case z, offsets)
  D6  cross-league supersession refused only by the repository
  D7  credential-shaped values and non-JSON floats in finding detail
  D8  ``import sports_quant.retrospective`` first raised ImportError
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.base import RepositoryError
from sports_quant.db.repositories.retrospective import (
    ProvenanceConflictError,
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.retrospective import (
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
)
from sports_quant.retrospective.evidence import SOURCE_EVIDENCE_TABLES
from sports_quant.retrospective.provenance import canonical_detail_json

ISO = "2026-08-11T00:00:00.000000Z"
MONTH_A = "month-A"
SEASONS_B = "five-season-B"


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqliteRetrospectiveProvenanceRepository:
    return SqliteRetrospectiveProvenanceRepository(conn)


@pytest.fixture
def ns(nba_league_id: str) -> ProviderNamespace:
    return ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM, "v1")


def _corpus(repo, league: str, **kw: Any) -> Any:
    p: dict[str, Any] = dict(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id=league,
        reconstruction_policy_version="rp", cutoff_policy_id="cp",
        cutoff_policy_version="1", source_corpus_digest=MONTH_A,
        target_set_digest="tA", g1_variant=G1Variant.G1_B_CORE)
    p.update(kw)
    return repo.record_corpus_version(**p)


def _audit(repo, ns: ProviderNamespace, **kw: Any) -> Any:
    p: dict[str, Any] = dict(
        namespace=ns, source_corpus_digest=MONTH_A, audit_policy_version="ap",
        distinct_ids=30, total_observations=239, collision_count=0,
        verdict=AuditVerdict.ACCEPTED)
    p.update(kw)
    return repo.record_identity_audit(**p)


def _team(conn: sqlite3.Connection, league: str) -> str:
    return str(conn.execute(
        "SELECT team_id FROM teams WHERE league_id=? ORDER BY team_id LIMIT 1",
        (league,)).fetchone()[0])


def _raw_crosswalk_sql() -> str:
    return (
        "INSERT INTO static_crosswalk_provenance (crosswalk_id, corpus_version_id, "
        "league_id, provider, namespace_generation, entity_type, provider_id, "
        "canonical_entity_id, identity_audit_id, identity_audit_digest, "
        "provenance_policy_version, semantic_digest, curated_at, created_at) "
        "VALUES (?, ?, ?, 'balldontlie', 'v1', 'team', '1', ?, ?, ?, 'pp', ?, ?, ?)")


# --------------------------------------------------------------------------- #
# D1 -- cross-corpus audit reuse
# --------------------------------------------------------------------------- #
def test_d1_repository_refuses_an_audit_from_a_different_source_corpus(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """A clean ONE-MONTH audit must not vouch for a five-season corpus (G5 §16)."""

    with transaction(conn):
        _corpus(repo, nba_league_id, source_corpus_digest=MONTH_A)
        month_audit = _audit(repo, ns, source_corpus_digest=MONTH_A)
        seasons = _corpus(repo, nba_league_id, source_corpus_digest=SEASONS_B,
                          target_set_digest="tB")
    with pytest.raises(RepositoryError, match="only ever a statement about the evidence"):
        with transaction(conn):
            repo.record_static_crosswalk(
                corpus_version_id=seasons.corpus_version_id, namespace=ns,
                provider_id="1", canonical_entity_id=_team(conn, nba_league_id),
                identity_audit_id=month_audit.identity_audit_id,
                provenance_policy_version="pp")


@pytest.mark.parametrize("foreign_keys", [1, 0])
def test_d1_raw_sql_refuses_cross_corpus_audit_reuse(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str,
    foreign_keys: int,
) -> None:
    """The claim is DATABASE enforcement, so it must hold with FKs off too."""

    with transaction(conn):
        _corpus(repo, nba_league_id, source_corpus_digest=MONTH_A)
        month_audit = _audit(repo, ns, source_corpus_digest=MONTH_A)
        seasons = _corpus(repo, nba_league_id, source_corpus_digest=SEASONS_B,
                          target_set_digest="tB")
    conn.execute(f"PRAGMA foreign_keys = {foreign_keys}")
    with pytest.raises(sqlite3.IntegrityError, match="different source corpus"):
        conn.execute(_raw_crosswalk_sql(), (
            f"xwk_raw{foreign_keys}", seasons.corpus_version_id, nba_league_id,
            _team(conn, nba_league_id), month_audit.identity_audit_id,
            month_audit.semantic_digest, f"draw{foreign_keys}", ISO, ISO))
    conn.execute("PRAGMA foreign_keys = ON")


def test_d1_matching_source_corpus_still_works(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """The repair must not break the legitimate case."""

    with transaction(conn):
        c = _corpus(repo, nba_league_id, source_corpus_digest=MONTH_A)
        a = _audit(repo, ns, source_corpus_digest=MONTH_A)
        xw = repo.record_static_crosswalk(
            corpus_version_id=c.corpus_version_id, namespace=ns, provider_id="1",
            canonical_entity_id=_team(conn, nba_league_id),
            identity_audit_id=a.identity_audit_id, provenance_policy_version="pp")
    assert xw.crosswalk_id.startswith("xwk_")


def test_d1_crosswalk_into_an_unknown_corpus_is_refused(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    with transaction(conn):
        _corpus(repo, nba_league_id)
        a = _audit(repo, ns)
    with pytest.raises(RepositoryError, match="unknown corpus version"):
        with transaction(conn):
            repo.record_static_crosswalk(
                corpus_version_id="rcv_ghost", namespace=ns, provider_id="1",
                canonical_entity_id=_team(conn, nba_league_id),
                identity_audit_id=a.identity_audit_id, provenance_policy_version="pp")


# --------------------------------------------------------------------------- #
# D2 -- an accepted audit may not acquire contradictory findings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "severity,classification,scope",
    [
        (FindingSeverity.BLOCKING, FindingClassification.IDENTITY_COLLISION,
         ExclusionScope.ENTITY),
        (FindingSeverity.BLOCKING, FindingClassification.NAMESPACE_UNVERIFIED,
         ExclusionScope.LEAGUE_NAMESPACE),
        (FindingSeverity.WARNING, FindingClassification.INSUFFICIENT_EVIDENCE,
         ExclusionScope.ENTITY),
    ],
)
def test_d2_repository_refuses_contradictory_finding_under_accepted_audit(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str,
    severity: FindingSeverity, classification: FindingClassification,
    scope: ExclusionScope,
) -> None:
    """An audit cannot mean both "accepted, zero collisions" and "blocking collision"."""

    with transaction(conn):
        _corpus(repo, nba_league_id)
        acc = _audit(repo, ns)
    with pytest.raises(ProvenanceConflictError, match="never rewritten"):
        with transaction(conn):
            repo.record_finding(
                identity_audit_id=acc.identity_audit_id, namespace=ns,
                severity=severity, finding_code="C1", classification=classification,
                exclusion_scope=scope, provider_id="1")


def test_d2_raw_sql_refuses_contradictory_finding_under_accepted_audit(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    with transaction(conn):
        _corpus(repo, nba_league_id)
        acc = _audit(repo, ns)
    with pytest.raises(sqlite3.IntegrityError, match="contradictory finding"):
        conn.execute(
            "INSERT INTO identity_audit_findings (finding_id, identity_audit_id, "
            "league_id, provider, namespace_generation, entity_type, provider_id, "
            "severity, finding_code, classification, exclusion_scope, detail_json, "
            "detail_digest, created_at) VALUES ('idf_bad', ?, ?, 'balldontlie', 'v1', "
            "'team', '1', 'blocking', 'C', 'identity_collision', 'entity', '{}', "
            "'d', ?)",
            (acc.identity_audit_id, nba_league_id, ISO))


def test_d2_a_benign_finding_under_an_accepted_audit_is_still_allowed(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """"We looked and it was lawful" must remain recordable."""

    with transaction(conn):
        _corpus(repo, nba_league_id)
        acc = _audit(repo, ns)
        f = repo.record_finding(
            identity_audit_id=acc.identity_audit_id, namespace=ns,
            severity=FindingSeverity.INFO, finding_code="RENAMED",
            classification=FindingClassification.LEGITIMATE_MUTATION,
            exclusion_scope=ExclusionScope.NONE, provider_id="1")
    assert f.exclusion_scope == "none"


def test_d2_a_counted_flag_is_allowed_but_an_uncounted_one_is_not(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """``flagged_count`` must already account for the audit's own warnings."""

    with transaction(conn):
        _corpus(repo, nba_league_id)
        counted = _audit(repo, ns, flagged_count=1)
        f = repo.record_finding(
            identity_audit_id=counted.identity_audit_id, namespace=ns,
            severity=FindingSeverity.WARNING, finding_code="NAMEVAR",
            classification=FindingClassification.NAME_VARIANCE,
            exclusion_scope=ExclusionScope.NONE, provider_id="1")
        assert f.severity == "warning"

    with transaction(conn):
        uncounted = _audit(repo, ns, flagged_count=0, distinct_ids=31,
                           total_observations=240)
    with pytest.raises(sqlite3.IntegrityError, match="understates flagged_count"):
        with transaction(conn):
            repo.record_finding(
                identity_audit_id=uncounted.identity_audit_id, namespace=ns,
                severity=FindingSeverity.WARNING, finding_code="NAMEVAR2",
                classification=FindingClassification.NAME_VARIANCE,
                exclusion_scope=ExclusionScope.NONE, provider_id="2")


def test_d2_blocking_findings_remain_recordable_under_a_rejected_audit(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """The repair must not make a genuine collision unrecordable."""

    with transaction(conn):
        _corpus(repo, nba_league_id)
        rej = _audit(repo, ns, collision_count=2,
                     verdict=AuditVerdict.REJECTED_COLLISION)
        f = repo.record_finding(
            identity_audit_id=rej.identity_audit_id, namespace=ns,
            severity=FindingSeverity.BLOCKING, finding_code="COLLIDE",
            classification=FindingClassification.IDENTITY_COLLISION,
            exclusion_scope=ExclusionScope.ENTITY, provider_id="1")
    assert f.classification == "identity_collision"


def test_d2_finding_under_an_unknown_audit_is_refused(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace
) -> None:
    with pytest.raises(RepositoryError, match="unknown identity audit"):
        with transaction(conn):
            repo.record_finding(
                identity_audit_id="ida_ghost", namespace=ns,
                severity=FindingSeverity.INFO, finding_code="X",
                classification=FindingClassification.LEGITIMATE_MUTATION,
                exclusion_scope=ExclusionScope.NONE)


# --------------------------------------------------------------------------- #
# D3 / D4 -- traceability and evidence-pointer integrity
# --------------------------------------------------------------------------- #
def _seed_evidence_row(conn: sqlite3.Connection) -> str:
    """One real ``raw_responses`` row to point at."""

    from sports_quant.db.repositories.ingestion_runs import (
        SqliteIngestionRunRepository,
    )
    from sports_quant.db.repositories.raw_responses import (
        SqliteRawResponseRepository,
        response_content_hash,
    )

    existing = conn.execute(
        "SELECT raw_response_id FROM raw_responses LIMIT 1").fetchone()
    if existing is not None:
        return str(existing[0])
    run = SqliteIngestionRunRepository(conn).start(
        command="review", provider="balldontlie", operation="review",
        args_json="{}", started_monotonic_ns=0, tool_version="review")
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run.run_id, provider="balldontlie", endpoint="/review",
        request_params_json="{}", http_status=200, response_headers_json="{}",
        requested_at=ISO, received_at=ISO, elapsed_ns=1, body="{}",
        content_hash=response_content_hash(
            provider="balldontlie", endpoint="/review", request_params={}, body="{}"))
    return raw.raw_response_id


def _certify(repo, corpus_id, ns, **kw: Any) -> Any:
    p: dict[str, Any] = dict(
        corpus_version_id=corpus_id, namespace=ns, provider_game_id="g1",
        feature_family="team_rolling_core",
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        reconstruction_policy_version="rp",
        eligibility=EligibilityVerdict.ELIGIBLE,
        availability_basis=AvailabilityBasis.EVENT_DERIVED,
        availability_rule_id="prior_event_completion_conservative_v1",
        availability_source="official_box_score_publication_lag_v1",
        source_event_completed_at=ISO)
    p.update(kw)
    return repo.certify_input(**p)


def test_d3_eligible_event_derived_needs_a_source_evidence_pointer(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """A completion timestamp is not proof that the source event exists."""

    with transaction(conn):
        c = _corpus(repo, nba_league_id)
    with pytest.raises(RepositoryError, match="not proof that the source data exists"):
        with transaction(conn):
            _certify(repo, c.corpus_version_id, ns)


def test_d3_eligible_versioned_snapshot_needs_a_source_evidence_pointer(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    with transaction(conn):
        c = _corpus(repo, nba_league_id)
    with pytest.raises(RepositoryError, match="not proof that the source data exists"):
        with transaction(conn):
            _certify(repo, c.corpus_version_id, ns,
                     availability_basis=AvailabilityBasis.VERSIONED_SNAPSHOT,
                     availability_rule_id=None, source_event_completed_at=None,
                     source_snapshot_at=ISO,
                     availability_source="open_meteo_previous_runs")


def test_d3_eligible_label_needs_a_source_evidence_pointer(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """A label must not become an untraceable assertion."""

    with transaction(conn):
        c = _corpus(repo, nba_league_id)
    with pytest.raises(RepositoryError, match="not proof that the source data exists"):
        with transaction(conn):
            _certify(repo, c.corpus_version_id, ns, feature_family="final_score",
                     provenance_class=ProvenanceClass.LABEL_ONLY_RETROSPECTIVE,
                     availability_basis=None, availability_rule_id=None,
                     availability_source=None, source_event_completed_at=None)


def test_d3_static_identity_is_traceable_through_its_crosswalk(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """STATIC_IDENTITY needs no extra pointer: the crosswalk IS the evidence."""

    with transaction(conn):
        c = _corpus(repo, nba_league_id)
        a = _audit(repo, ns)
        xw = repo.record_static_crosswalk(
            corpus_version_id=c.corpus_version_id, namespace=ns, provider_id="1",
            canonical_entity_id=_team(conn, nba_league_id),
            identity_audit_id=a.identity_audit_id, provenance_policy_version="pp")
        cert = _certify(repo, c.corpus_version_id, ns, feature_family="team_identity",
                        availability_basis=AvailabilityBasis.STATIC_IDENTITY,
                        availability_rule_id=None, availability_source=None,
                        source_event_completed_at=None, crosswalk_id=xw.crosswalk_id)
    assert cert.crosswalk_id == xw.crosswalk_id
    # ...and that crosswalk's audit is accepted over this corpus's own evidence.
    audit = repo.identity_audit(xw.identity_audit_id)
    assert audit is not None and audit.verdict == "accepted"
    assert audit.source_corpus_digest == c.source_corpus_digest


def test_d3_excluded_rows_stay_exempt(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """"Not admissible" is often a statement that the evidence does not exist."""

    with transaction(conn):
        c = _corpus(repo, nba_league_id)
        cert = _certify(repo, c.corpus_version_id, ns,
                        eligibility=EligibilityVerdict.EXCLUDED,
                        exclusion_code="NO_PRIOR_GAME")
    assert cert.eligibility == "excluded"
    assert cert.source_evidence_id is None


def test_d3_a_real_evidence_pointer_is_accepted(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    with transaction(conn):
        rid = _seed_evidence_row(conn)
        c = _corpus(repo, nba_league_id)
        cert = _certify(repo, c.corpus_version_id, ns,
                        source_evidence_table="raw_responses", source_evidence_id=rid)
    assert cert.source_evidence_table == "raw_responses"


@pytest.mark.parametrize("table", [
    "not_a_real_table",
    "teams",                              # canonical dimension
    "kalshi_markets",                     # mutable current-state
    "entity_match_decisions",             # a conclusion, not an observation
    "reconstructed_input_provenance",     # provenance citing itself
    "'; DROP TABLE teams; --",
])
def test_d4_disallowed_evidence_tables_are_refused_by_the_repository(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str,
    table: str,
) -> None:
    with transaction(conn):
        c = _corpus(repo, nba_league_id)
    with pytest.raises(RetrospectiveProvenanceError, match="not an allowed"):
        with transaction(conn):
            _certify(repo, c.corpus_version_id, ns,
                     source_evidence_table=table, source_evidence_id="x")


def test_d4_disallowed_evidence_table_is_refused_by_raw_sql_too(
    repo, conn: sqlite3.Connection, nba_league_id: str
) -> None:
    with transaction(conn):
        c = _corpus(repo, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError, match="not an allowed"):
        conn.execute(
            "INSERT INTO reconstructed_input_provenance (input_provenance_id, "
            "corpus_version_id, league_id, provider, namespace_generation, "
            "provider_game_id, feature_family, provenance_class, availability_basis, "
            "availability_rule_id, availability_rule_digest, availability_source, "
            "reconstruction_policy_version, source_evidence_table, source_evidence_id, "
            "source_event_completed_at, eligibility, semantic_digest, created_at) "
            "VALUES ('rip_bad', ?, ?, 'p', 'v1', 'g', 'f', 'reconstructed_research', "
            "'event_derived', 'r', 'd', 'src', 'rp', 'not_a_real_table', 'x', ?, "
            "'eligible', 'dbad', ?)",
            (c.corpus_version_id, nba_league_id, ISO, ISO))


def test_d4_a_pointer_to_a_nonexistent_row_is_refused(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    """An allowed table name is not enough; the row has to be there."""

    with transaction(conn):
        c = _corpus(repo, nba_league_id)
    with pytest.raises(RepositoryError, match="resolves to nothing"):
        with transaction(conn):
            _certify(repo, c.corpus_version_id, ns,
                     source_evidence_table="raw_responses",
                     source_evidence_id="raw_does_not_exist")


def test_d4_allowlist_matches_the_f019_trigger(conn: sqlite3.Connection) -> None:
    """Two copies of a list that can drift is worse than one that cannot."""

    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='trg_rip_evidence_table_allowed'").fetchone()[0]
    for table in SOURCE_EVIDENCE_TABLES:
        assert f"'{table}'" in sql, f"{table} missing from the f019 trigger"
    quoted = {
        part.strip().strip("',")
        for part in sql.split("NOT IN (")[1].split(")")[0].replace("\n", " ").split()
    }
    assert quoted == set(SOURCE_EVIDENCE_TABLES)


# --------------------------------------------------------------------------- #
# D3b -- documented availability evidence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("basis,extra", [
    (AvailabilityBasis.EVENT_DERIVED,
     {"availability_rule_id": "prior_event_completion_conservative_v1",
      "source_event_completed_at": ISO}),
    (AvailabilityBasis.VERSIONED_SNAPSHOT,
     {"availability_rule_id": None, "source_event_completed_at": None,
      "source_snapshot_at": ISO}),
])
def test_d3b_eligible_rows_must_name_their_availability_evidence(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str,
    basis: AvailabilityBasis, extra: dict[str, Any],
) -> None:
    """A stated lag with no documented basis is an unsupported assertion."""

    with transaction(conn):
        rid = _seed_evidence_row(conn)
        c = _corpus(repo, nba_league_id)
    with pytest.raises(RepositoryError, match="documenting its availability claim"):
        with transaction(conn):
            _certify(repo, c.corpus_version_id, ns, availability_basis=basis,
                     availability_source=None, source_evidence_table="raw_responses",
                     source_evidence_id=rid, **extra)


# --------------------------------------------------------------------------- #
# D5 -- timestamps must be real instants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "2026-99-01T00:00:00.000000Z",   # month 99
    "2026-01-99T00:00:00.000000Z",   # day 99
    "2026-01-01T77:00:00.000000Z",   # hour 77
    "2026-02-30T00:00:00.000000Z",   # impossible calendar date
    "2026-02-29T00:00:00.000000Z",   # not a leap year
    "2026-01-01T00:00:00.000000z",   # lower-case z
    "2026-01-01T00:00:00+01:00Z",    # offset
    "2026-01-01T24:00:00.000000Z",   # ISO end-of-day, not our canonical spelling
    "2026-01-01T00:60:00.000000Z",   # minute 60
])
def test_d5_impossible_timestamps_are_refused_by_raw_sql(
    repo, conn: sqlite3.Connection, nba_league_id: str, bad: str
) -> None:
    """These are TEXT columns compared lexicographically; a bad one mis-orders."""

    with transaction(conn):
        rid = _seed_evidence_row(conn)
        c = _corpus(repo, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reconstructed_input_provenance (input_provenance_id, "
            "corpus_version_id, league_id, provider, namespace_generation, "
            "provider_game_id, feature_family, provenance_class, availability_basis, "
            "availability_rule_id, availability_rule_digest, availability_source, "
            "reconstruction_policy_version, source_evidence_table, source_evidence_id, "
            "source_event_completed_at, eligibility, semantic_digest, created_at) "
            "VALUES ('rip_ts', ?, ?, 'p', 'v1', 'g', 'f', 'reconstructed_research', "
            "'event_derived', 'r', 'd', 'src', 'rp', 'raw_responses', ?, ?, "
            "'eligible', 'dts', ?)",
            (c.corpus_version_id, nba_league_id, rid, bad, ISO))


@pytest.mark.parametrize("good", [
    "2026-08-11T00:00:00.000000Z",
    "2028-02-29T00:00:00.000000Z",   # a real leap day
    "2026-12-31T23:59:59.999999Z",
    "2020-06-06T00:00:00.000000Z",
])
def test_d5_real_instants_are_still_accepted(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str,
    good: str,
) -> None:
    with transaction(conn):
        rid = _seed_evidence_row(conn)
        c = _corpus(repo, nba_league_id)
        cert = _certify(repo, c.corpus_version_id, ns,
                        source_evidence_table="raw_responses", source_evidence_id=rid,
                        source_event_completed_at=good)
    assert cert.source_event_completed_at == good


def test_d5_curated_at_must_also_be_a_real_instant(
    repo, conn: sqlite3.Connection, ns: ProviderNamespace, nba_league_id: str
) -> None:
    with transaction(conn):
        c = _corpus(repo, nba_league_id)
        a = _audit(repo, ns)
    with pytest.raises(sqlite3.IntegrityError, match="curated_at"):
        conn.execute(_raw_crosswalk_sql(), (
            "xwk_ts", c.corpus_version_id, nba_league_id, _team(conn, nba_league_id),
            a.identity_audit_id, a.semantic_digest, "dts",
            "2026-13-45T00:00:00.000000Z", ISO))


# --------------------------------------------------------------------------- #
# D6 -- cross-league supersession, at the database
# --------------------------------------------------------------------------- #
def test_d6_raw_sql_refuses_cross_league_supersession(
    repo, conn: sqlite3.Connection, nba_league_id: str, mlb_league_id: str
) -> None:
    with transaction(conn):
        nba = _corpus(repo, nba_league_id, source_corpus_digest="nba")
    with pytest.raises(sqlite3.IntegrityError, match="same league"):
        conn.execute(
            "INSERT INTO reconstruction_corpus_versions (corpus_version_id, "
            "provenance_class, league_id, reconstruction_policy_version, "
            "cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
            "target_set_digest, g1_variant, semantic_digest, "
            "supersedes_corpus_version_id, created_at) VALUES ('rcv_xl', "
            "'reconstructed_research', ?, 'rp', 'cp', '1', 'm', 't', 'g1_b_core', "
            "'dxl', ?, ?)",
            (mlb_league_id, nba.corpus_version_id, ISO))


def test_d6_same_league_supersession_still_works(
    repo, conn: sqlite3.Connection, nba_league_id: str
) -> None:
    with transaction(conn):
        parent = _corpus(repo, nba_league_id, source_corpus_digest="p")
        child = _corpus(repo, nba_league_id, source_corpus_digest="c",
                        supersedes_corpus_version_id=parent.corpus_version_id)
    assert repo.superseded_by(parent.corpus_version_id) == (child,)


# --------------------------------------------------------------------------- #
# D7 -- sanitization
# --------------------------------------------------------------------------- #
# Every credential-shaped fixture below is ASSEMBLED AT RUNTIME from harmless
# fragments rather than written as a literal. The screen under test keys on the
# marker prefix, so the fixtures exercise it exactly as a literal would -- but no
# string in this file matches a real provider's key pattern, so a scanner cannot
# mistake a test for a leak. (GitHub push protection flagged the literal form of
# the first case as a Stripe key; the fix is a fixture that cannot be confused
# with a credential, never a scanner bypass.)
_FAKE_KEY = "sk_" + "live_" + ("0" * 24)
_FAKE_JWT = "Bearer " + "eyJ" + ("a" * 20)
_FAKE_URL = "https://api.example.invalid/v1?" + "api" + "_key=" + ("x" * 12)
_FAKE_BASIC = "Authorization: " + "Basic " + ("y" * 16)
_FAKE_PEM = "-----" + "BEGIN" + " RSA PRIVATE KEY-----"


@pytest.mark.parametrize("payload,why", [
    ({"k": _FAKE_KEY}, "API key"),
    ({"h": _FAKE_JWT}, "bearer token"),
    ({"u": _FAKE_URL}, "URL with a query secret"),
    ({"a": _FAKE_BASIC}, "authorization header"),
    ({"p": _FAKE_PEM}, "private key"),
    ({"n": float("nan")}, "NaN, which is not JSON"),
    ({"i": float("inf")}, "Infinity, which is not JSON"),
])
def test_d7_credential_shaped_and_non_json_values_are_refused(
    payload: dict[str, Any], why: str
) -> None:
    """A short credential slips straight through a length bound: a key is ~40 chars."""

    with pytest.raises(RetrospectiveProvenanceError):
        canonical_detail_json(payload)


@pytest.mark.parametrize("payload", [
    {"code": "TEAM_ID_TWO_FRANCHISES", "distinct_franchises": 2},
    {"digest": "a3f9" * 16},
    {"doc": "https://docs.balldontlie.io/teams"},
    {"nested": {"observed": 3, "expected": 3}},
])
def test_d7_legitimate_detail_still_passes(payload: dict[str, Any]) -> None:
    """The screen must not make ordinary structured detail unrecordable."""

    assert canonical_detail_json(payload)


# --------------------------------------------------------------------------- #
# D8 -- import order independence
# --------------------------------------------------------------------------- #
def test_d8_retrospective_package_imports_first_in_a_clean_process() -> None:
    """``import sports_quant.retrospective`` first used to raise ImportError.

    Run in a SUBPROCESS on purpose: inside pytest the conftest has already
    imported ``sports_quant.db``, which is exactly what masked the cycle.
    """

    repo_root = Path(__file__).resolve().parents[3]
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sports_quant.retrospective as r; "
         "assert r.ProvenanceClass and r.AVAILABILITY_RULES; print('ok')"],
        cwd=repo_root, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# --------------------------------------------------------------------------- #
# §13 -- enum / SQL CHECK parity, so future additions cannot drift
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table,column,enum_cls", [
    ("reconstruction_corpus_versions", "provenance_class", ProvenanceClass),
    ("reconstruction_corpus_versions", "g1_variant", G1Variant),
    ("identity_audit_records", "entity_type", EntityType),
    ("identity_audit_records", "verdict", AuditVerdict),
    ("identity_audit_findings", "severity", FindingSeverity),
    ("identity_audit_findings", "classification", FindingClassification),
    ("identity_audit_findings", "exclusion_scope", ExclusionScope),
    ("reconstructed_input_provenance", "availability_basis", AvailabilityBasis),
    ("reconstructed_input_provenance", "eligibility", EligibilityVerdict),
])
def test_every_enum_value_appears_in_its_sql_check(
    conn: sqlite3.Connection, table: str, column: str, enum_cls: Any
) -> None:
    """Adding a Python member without the CHECK (or vice versa) fails here."""

    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()[0]
    # Strip SQL line comments first: several CHECK lists carry an inline
    # explanation per member, and a naive split would read the prose as values.
    stripped = "\n".join(line.split("--")[0] for line in sql.splitlines())
    fragment = stripped.split(f"{column} IN (")[1].split(")")[0]
    literals = set(re.findall(r"'([^']*)'", fragment))
    assert literals == {e.value for e in enum_cls}, (table, column)


def test_v19_schema_version_and_migration_count(conn: sqlite3.Connection) -> None:
    from sports_quant.db.schema import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == 20
    rows = conn.execute(
        "SELECT version, name FROM schema_versions ORDER BY version").fetchall()
    assert [int(r["version"]) for r in rows] == list(
        range(1, CURRENT_SCHEMA_VERSION + 1))
    # f018/f019 keep their applied positions: later migrations append, never
    # renumber or rewrite what was already applied.
    by_version = {int(r["version"]): r["name"] for r in rows}
    assert by_version[18] == "f018_retrospective_provenance"
    assert by_version[19] == "f019_retrospective_provenance_repairs"


def test_f018_is_preserved_byte_for_byte() -> None:
    """The repair appended; it did not rewrite applied migration evidence."""

    import hashlib

    path = (Path(__file__).resolve().parents[1] / "migrations"
            / "f018_retrospective_provenance.sql")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == (
        "121619d4ad014b5746aff2f40f444d99cbbaf8d80f9e437e4e45dc5f32c41ec2"
    ), "f018 was edited; applied migrations are immutable -- add a new one instead"


# --------------------------------------------------------------------------- #
# §17 -- atomicity of the complete identity-audit workflow
# --------------------------------------------------------------------------- #
def test_no_accepted_crosswalk_survives_a_failed_audit_transaction(
    db_path: Path,
) -> None:
    initialize_database(db_path)
    db = Database(db_path)
    with db.connection() as conn:
        league = str(conn.execute(
            "SELECT league_id FROM leagues WHERE code='NBA'").fetchone()[0])
        ns = ProviderNamespace(league, "balldontlie", EntityType.TEAM, "v1")
        with pytest.raises(RepositoryError):
            with transaction(conn):
                repo = SqliteRetrospectiveProvenanceRepository(conn)
                c = _corpus(repo, league)
                a = _audit(repo, ns)
                repo.record_static_crosswalk(
                    corpus_version_id=c.corpus_version_id, namespace=ns,
                    provider_id="1", canonical_entity_id=_team(conn, league),
                    identity_audit_id=a.identity_audit_id,
                    provenance_policy_version="pp")
                raise RepositoryError("audit workflow failed after the crosswalk")
    with db.connection() as conn:
        for table in ("reconstruction_corpus_versions", "identity_audit_records",
                      "static_crosswalk_provenance"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608
