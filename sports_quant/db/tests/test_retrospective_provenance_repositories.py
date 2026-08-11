"""Lane-R provenance domain and repositories (f018).

Task §24-§28. Everything here is synthetic: fixtures write accepted and rejected
audit records through the repository API to prove the storage CONTRACT. There is
no corpus scan, because the identity-audit engine is deliberately not part of
this phase.

Nothing in this file constructs a provider client, reads a settings file, or
touches a protected database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.base import RepositoryError
from sports_quant.db.repositories.retrospective import (
    AmbiguousProvenanceError,
    ProvenanceConflictError,
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.retrospective import (
    AVAILABILITY_RULES,
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
    UnknownAvailabilityRuleError,
    derive_availability_instant,
    detail_digest,
    lookup_rule,
    semantic_digest,
    verify_rule_digest,
)
from sports_quant.retrospective.provenance import UNVERIFIED_GENERATION

SRC = "source-corpus-digest-a"
COMPLETED = "2026-03-01T03:00:00.000000Z"
SNAPSHOT = "2026-02-28T18:00:00.000000Z"


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqliteRetrospectiveProvenanceRepository:
    return SqliteRetrospectiveProvenanceRepository(conn)


@pytest.fixture
def nba_team_ns(nba_league_id: str) -> ProviderNamespace:
    return ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM, "v1")


@pytest.fixture
def nba_player_ns(nba_league_id: str) -> ProviderNamespace:
    return ProviderNamespace(nba_league_id, "balldontlie", EntityType.PLAYER, "v1")


def _corpus(
    repo: SqliteRetrospectiveProvenanceRepository, league_id: str, **kw: Any
) -> Any:
    params: dict[str, Any] = dict(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        league_id=league_id,
        reconstruction_policy_version="rp-1",
        cutoff_policy_id="pregame_lock",
        cutoff_policy_version="1",
        source_corpus_digest=SRC,
        target_set_digest="targets-a",
        g1_variant=G1Variant.G1_B_CORE,
    )
    params.update(kw)
    return repo.record_corpus_version(**params)


def _accepted_audit(
    repo: SqliteRetrospectiveProvenanceRepository, ns: ProviderNamespace, **kw: Any
) -> Any:
    params: dict[str, Any] = dict(
        namespace=ns, source_corpus_digest=SRC, audit_policy_version="ap-1",
        distinct_ids=30, total_observations=239, collision_count=0,
        verdict=AuditVerdict.ACCEPTED,
    )
    params.update(kw)
    return repo.record_identity_audit(**params)


def _raw_evidence(conn: sqlite3.Connection) -> str:
    """A real ``raw_responses`` row to satisfy the f019 traceability rule.

    Added by the independent review: before f019 an ELIGIBLE reconstructed input
    could be certified with no source-evidence pointer at all, so these fixtures
    never needed one.
    """

    from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
    from sports_quant.db.repositories.raw_responses import (
        SqliteRawResponseRepository,
        response_content_hash,
    )

    existing = conn.execute(
        "SELECT raw_response_id FROM raw_responses LIMIT 1").fetchone()
    if existing is not None:
        return str(existing[0])
    run = SqliteIngestionRunRepository(conn).start(
        command="repo-test", provider="test", operation="seed", args_json="{}",
        started_monotonic_ns=0, tool_version="t")
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run.run_id, provider="test", endpoint="/seed", request_params_json="{}",
        http_status=200, response_headers_json="{}", requested_at=COMPLETED,
        received_at=COMPLETED, elapsed_ns=1, body="{}",
        content_hash=response_content_hash(
            provider="test", endpoint="/seed", request_params={}, body="{}"))
    return raw.raw_response_id


def _first_team(conn: sqlite3.Connection, league_id: str) -> str:
    return str(conn.execute(
        "SELECT team_id FROM teams WHERE league_id = ? ORDER BY team_id LIMIT 1",
        (league_id,)).fetchone()[0])


def _seed_player(conn: sqlite3.Connection, league_id: str, player_id: str) -> str:
    conn.execute(
        "INSERT INTO players (player_id, league_id, full_name, created_at, updated_at) "
        "VALUES (?, ?, 'Test Player', '2026-01-01T00:00:00.000000Z', "
        "'2026-01-01T00:00:00.000000Z')",
        (player_id, league_id),
    )
    return player_id


# --------------------------------------------------------------------------- #
# §24 identity audits
# --------------------------------------------------------------------------- #
def test_accepted_identity_audit_round_trips(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace
) -> None:
    audit = _accepted_audit(repo, nba_team_ns)
    assert audit.verdict == "accepted"
    assert audit.namespace_verified is True
    assert repo.identity_audit(audit.identity_audit_id) == audit
    assert repo.accepted_audit_for(nba_team_ns, source_corpus_digest=SRC) == audit


def test_collision_audit_is_recorded_and_never_accepted(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace
) -> None:
    audit = repo.record_identity_audit(
        namespace=nba_team_ns, source_corpus_digest=SRC, audit_policy_version="ap-1",
        distinct_ids=30, total_observations=239, collision_count=2,
        verdict=AuditVerdict.REJECTED_COLLISION)
    assert audit.verdict == "rejected_collision"
    assert repo.accepted_audit_for(nba_team_ns, source_corpus_digest=SRC) is None

    with pytest.raises(RepositoryError, match="fails closed"):
        _accepted_audit(repo, nba_team_ns, collision_count=2)


def test_namespace_uncertain_audit_cannot_be_accepted(
    repo: SqliteRetrospectiveProvenanceRepository, nba_league_id: str
) -> None:
    """G5: an unverified API generation is representable and never eligible."""

    unknown = ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM,
                                UNVERIFIED_GENERATION)
    assert unknown.verified is False
    audit = repo.record_identity_audit(
        namespace=unknown, source_corpus_digest=SRC, audit_policy_version="ap-1",
        distinct_ids=30, total_observations=239, collision_count=0,
        verdict=AuditVerdict.REJECTED_NAMESPACE_UNVERIFIED)
    assert audit.verdict == "rejected_namespace_unverified"
    with pytest.raises(RepositoryError, match="unverified"):
        _accepted_audit(repo, unknown)


def test_audit_lookup_is_exact_on_the_source_corpus(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace
) -> None:
    """A clean audit over one window never transfers to another (G5 §16)."""

    _accepted_audit(repo, nba_team_ns)
    assert repo.accepted_audit_for(nba_team_ns, source_corpus_digest="a-wider-window") is None


def test_audit_replay_is_idempotent(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    conn: sqlite3.Connection,
) -> None:
    first = _accepted_audit(repo, nba_team_ns)
    second = _accepted_audit(repo, nba_team_ns)
    assert first.identity_audit_id == second.identity_audit_id
    assert conn.execute("SELECT COUNT(*) FROM identity_audit_records").fetchone()[0] == 1


def test_audit_digest_is_deterministic_and_order_independent(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace
) -> None:
    audit = _accepted_audit(repo, nba_team_ns)
    # Same facts, keys supplied in a different order -> same digest.
    forward = semantic_digest({"a": 1, "b": 2, "c": [1, 2]})
    reverse = semantic_digest({"c": [1, 2], "b": 2, "a": 1})
    assert forward == reverse
    # A semantic change changes it.
    assert semantic_digest({"a": 1, "b": 3, "c": [1, 2]}) != forward
    assert len(audit.semantic_digest) == 64


def test_one_provider_id_in_two_entity_types_stays_distinct(
    repo: SqliteRetrospectiveProvenanceRepository, nba_league_id: str,
    conn: sqlite3.Connection,
) -> None:
    """MLB team 147 and MLB person 147 are different keys."""

    team_ns = ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM, "v1")
    player_ns = ProviderNamespace(nba_league_id, "balldontlie", EntityType.PLAYER, "v1")
    assert team_ns.key("147") != player_ns.key("147")

    corpus = _corpus(repo, nba_league_id)
    team_audit = _accepted_audit(repo, team_ns)
    player_audit = _accepted_audit(repo, player_ns, distinct_ids=550,
                                   total_observations=1200)
    team_id = _first_team(conn, nba_league_id)
    player_id = _seed_player(conn, nba_league_id, "pl_test_147")

    a = repo.record_static_crosswalk(
        corpus_version_id=corpus.corpus_version_id, namespace=team_ns,
        provider_id="147", canonical_entity_id=team_id,
        identity_audit_id=team_audit.identity_audit_id,
        provenance_policy_version="pp-1")
    b = repo.record_static_crosswalk(
        corpus_version_id=corpus.corpus_version_id, namespace=player_ns,
        provider_id="147", canonical_entity_id=player_id,
        identity_audit_id=player_audit.identity_audit_id,
        provenance_policy_version="pp-1")
    assert a.crosswalk_id != b.crosswalk_id
    assert a.canonical_entity_id != b.canonical_entity_id


def test_same_numeric_id_across_providers_stays_distinct(nba_league_id: str) -> None:
    a = ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM, "v1")
    b = ProviderNamespace(nba_league_id, "mlb_statsapi", EntityType.TEAM, "v1")
    assert a.key("1") != b.key("1")


def test_namespace_generations_are_never_silently_equated(nba_league_id: str) -> None:
    """BALLDONTLIE v1 and v2 ids are not assumed to share a namespace."""

    v1 = ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM, "v1")
    v2 = ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM, "v2")
    assert v1.key("1") != v2.key("1")


def test_namespace_components_are_never_inferred(nba_league_id: str) -> None:
    with pytest.raises(RetrospectiveProvenanceError):
        ProviderNamespace(nba_league_id, "", EntityType.TEAM, "v1")
    with pytest.raises(RetrospectiveProvenanceError):
        ProviderNamespace(nba_league_id, "balldontlie", EntityType.TEAM, "  ")
    with pytest.raises(RetrospectiveProvenanceError):
        ProviderNamespace(nba_league_id, "balldontlie", "team", "v1")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# §24 findings
# --------------------------------------------------------------------------- #
def test_finding_round_trips_and_replays_idempotently(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    conn: sqlite3.Connection,
) -> None:
    audit = repo.record_identity_audit(
        namespace=nba_team_ns, source_corpus_digest=SRC, audit_policy_version="ap-1",
        distinct_ids=30, total_observations=239, collision_count=1,
        verdict=AuditVerdict.REJECTED_COLLISION)
    detail = {"observed_leagues": 2, "distinct_franchises": 2}
    first = repo.record_finding(
        identity_audit_id=audit.identity_audit_id, namespace=nba_team_ns,
        severity=FindingSeverity.BLOCKING, finding_code="TEAM_ID_TWO_FRANCHISES",
        classification=FindingClassification.IDENTITY_COLLISION,
        exclusion_scope=ExclusionScope.DEPENDENT_GAMES, provider_id="1", detail=detail)
    second = repo.record_finding(
        identity_audit_id=audit.identity_audit_id, namespace=nba_team_ns,
        severity=FindingSeverity.BLOCKING, finding_code="TEAM_ID_TWO_FRANCHISES",
        classification=FindingClassification.IDENTITY_COLLISION,
        exclusion_scope=ExclusionScope.DEPENDENT_GAMES, provider_id="1", detail=detail)
    assert first.finding_id == second.finding_id
    assert conn.execute("SELECT COUNT(*) FROM identity_audit_findings").fetchone()[0] == 1
    assert first.detail_digest == detail_digest(detail)
    assert repo.findings_for_audit(audit.identity_audit_id) == (first,)


def test_name_variance_is_detection_only(
    repo: SqliteRetrospectiveProvenanceRepository, nba_player_ns: ProviderNamespace
) -> None:
    """A name may raise a flag; it may never exclude anything on its own."""

    audit = _accepted_audit(repo, nba_player_ns, flagged_count=1)
    finding = repo.record_finding(
        identity_audit_id=audit.identity_audit_id, namespace=nba_player_ns,
        severity=FindingSeverity.WARNING, finding_code="PLAYER_NAME_CHANGED",
        classification=FindingClassification.NAME_VARIANCE,
        exclusion_scope=ExclusionScope.NONE, provider_id="115",
        detail={"distinct_names": 2})
    assert finding.exclusion_scope == "none"
    # The audit that carries it is still accepted: a flag is not a collision.
    assert audit.verdict == "accepted"


@pytest.mark.parametrize(
    "detail",
    [
        {"body": "x" * 5000},
        {"nested": {"payload": "y" * 400}},
        {"raw": ["ok", "z" * 900]},
    ],
)
def test_findings_refuse_anything_that_looks_like_a_provider_body(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    detail: dict[str, Any],
) -> None:
    """§28: no raw provider body is copied into a provenance message."""

    audit = _accepted_audit(repo, nba_team_ns)
    with pytest.raises(RetrospectiveProvenanceError, match="never provider bodies"):
        repo.record_finding(
            identity_audit_id=audit.identity_audit_id, namespace=nba_team_ns,
            severity=FindingSeverity.INFO, finding_code="X",
            classification=FindingClassification.LEGITIMATE_MUTATION,
            exclusion_scope=ExclusionScope.NONE, detail=detail)


def test_findings_refuse_unsupported_detail_types(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace
) -> None:
    audit = _accepted_audit(repo, nba_team_ns)
    with pytest.raises(RetrospectiveProvenanceError, match="unsupported type"):
        repo.record_finding(
            identity_audit_id=audit.identity_audit_id, namespace=nba_team_ns,
            severity=FindingSeverity.INFO, finding_code="X",
            classification=FindingClassification.LEGITIMATE_MUTATION,
            exclusion_scope=ExclusionScope.NONE, detail={"when": object()})


# --------------------------------------------------------------------------- #
# §24 static crosswalks
# --------------------------------------------------------------------------- #
def test_crosswalk_binds_the_exact_audit(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    audit = _accepted_audit(repo, nba_team_ns)
    team_id = _first_team(conn, nba_league_id)
    xw = repo.record_static_crosswalk(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_id="1", canonical_entity_id=team_id,
        identity_audit_id=audit.identity_audit_id, provenance_policy_version="pp-1")
    assert xw.identity_audit_id == audit.identity_audit_id
    assert xw.identity_audit_digest == audit.semantic_digest
    assert repo.static_crosswalk(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_id="1") == xw


def test_crosswalk_requires_no_name(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    """The crosswalk has no name column at all, and needs none to be created."""

    columns = {str(r["name"]) for r in conn.execute(
        "PRAGMA table_info(static_crosswalk_provenance)")}
    # No display name in any spelling. `namespace_generation` contains "name" as
    # a substring and is a namespace component, not a name, so it is exempt.
    assert not {c for c in columns
                if "name" in c and c != "namespace_generation"}
    # And nothing affiliation-ish, roster-ish, or outcome-ish either -- none of
    # those is identity evidence under the reviewed contract.
    for banned in ("full_name", "normalized_name", "display_name", "position",
                   "jersey", "status", "provider_team_id", "team_id", "score",
                   "outcome", "roster", "active"):
        assert banned not in columns

    corpus = _corpus(repo, nba_league_id)
    audit = _accepted_audit(repo, nba_team_ns)
    xw = repo.record_static_crosswalk(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_id="1", canonical_entity_id=_first_team(conn, nba_league_id),
        identity_audit_id=audit.identity_audit_id, provenance_policy_version="pp-1")
    assert xw.crosswalk_id


def test_player_crosswalk_stores_no_team_affiliation(
    conn: sqlite3.Connection,
) -> None:
    """Team affiliation is never person-identity evidence (architecture review)."""

    columns = {str(r["name"]) for r in conn.execute(
        "PRAGMA table_info(static_crosswalk_provenance)")}
    assert "provider_team_id" not in columns
    assert "team_id" not in columns


def test_crosswalk_cannot_bind_a_failed_audit(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    failed = repo.record_identity_audit(
        namespace=nba_team_ns, source_corpus_digest=SRC, audit_policy_version="ap-1",
        distinct_ids=30, total_observations=239, collision_count=1,
        verdict=AuditVerdict.REJECTED_COLLISION)
    with pytest.raises(RepositoryError, match="only an accepted audit"):
        repo.record_static_crosswalk(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
            provider_id="1", canonical_entity_id=_first_team(conn, nba_league_id),
            identity_audit_id=failed.identity_audit_id,
            provenance_policy_version="pp-1")


def test_crosswalk_cannot_bind_the_wrong_entity_type(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_player_ns: ProviderNamespace, nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    """An NBA player crosswalk cannot bind to a team canonical id."""

    corpus = _corpus(repo, nba_league_id)
    player_audit = _accepted_audit(repo, nba_player_ns)
    team_id = _first_team(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError, match="player crosswalk"):
        repo.record_static_crosswalk(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_player_ns,
            provider_id="115", canonical_entity_id=team_id,
            identity_audit_id=player_audit.identity_audit_id,
            provenance_policy_version="pp-1")

    # And the audit's own entity type must match the namespace being bound.
    team_audit = _accepted_audit(repo, nba_team_ns)
    with pytest.raises(RepositoryError, match="not the namespace being bound"):
        repo.record_static_crosswalk(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_player_ns,
            provider_id="115", canonical_entity_id=_seed_player(
                conn, nba_league_id, "pl_x"),
            identity_audit_id=team_audit.identity_audit_id,
            provenance_policy_version="pp-1")


def test_crosswalk_cannot_bind_the_wrong_league(
    repo: SqliteRetrospectiveProvenanceRepository, nba_league_id: str,
    mlb_league_id: str, conn: sqlite3.Connection,
) -> None:
    """An MLB provider key cannot bind to NBA canonical identity."""

    mlb_ns = ProviderNamespace(mlb_league_id, "mlb_statsapi", EntityType.TEAM, "v1")
    corpus = _corpus(repo, mlb_league_id)
    audit = _accepted_audit(repo, mlb_ns)
    nba_team = _first_team(conn, nba_league_id)
    with pytest.raises(sqlite3.IntegrityError, match="team crosswalk"):
        repo.record_static_crosswalk(
            corpus_version_id=corpus.corpus_version_id, namespace=mlb_ns,
            provider_id="147", canonical_entity_id=nba_team,
            identity_audit_id=audit.identity_audit_id,
            provenance_policy_version="pp-1")


def test_crosswalk_replay_is_idempotent_but_a_contradiction_raises(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    audit = _accepted_audit(repo, nba_team_ns)
    teams = [str(r[0]) for r in conn.execute(
        "SELECT team_id FROM teams WHERE league_id = ? ORDER BY team_id LIMIT 2",
        (nba_league_id,))]
    args = dict(corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
                provider_id="1", identity_audit_id=audit.identity_audit_id,
                provenance_policy_version="pp-1")
    first = repo.record_static_crosswalk(canonical_entity_id=teams[0], **args)
    again = repo.record_static_crosswalk(canonical_entity_id=teams[0], **args)
    assert first.crosswalk_id == again.crosswalk_id
    with pytest.raises(ProvenanceConflictError, match="contradiction, not an update"):
        repo.record_static_crosswalk(canonical_entity_id=teams[1], **args)


def test_crosswalk_curated_at_is_audit_time_and_is_not_backdated(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    audit = _accepted_audit(repo, nba_team_ns)
    xw = repo.record_static_crosswalk(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_id="1", canonical_entity_id=_first_team(conn, nba_league_id),
        identity_audit_id=audit.identity_audit_id, provenance_policy_version="pp-1")
    # Curation time is "now", never a historical instant borrowed from the data.
    assert xw.curated_at >= audit.created_at
    assert xw.curated_at > "2026-01-01T00:00:00.000000Z"
    # And it is not a reused decided_at: no match decision was consulted at all.
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# §25 reconstructed input provenance
# --------------------------------------------------------------------------- #
def _static_setup(
    repo: SqliteRetrospectiveProvenanceRepository, ns: ProviderNamespace,
    league_id: str, conn: sqlite3.Connection,
) -> tuple[Any, Any]:
    corpus = _corpus(repo, league_id)
    audit = _accepted_audit(repo, ns)
    xw = repo.record_static_crosswalk(
        corpus_version_id=corpus.corpus_version_id, namespace=ns, provider_id="1",
        canonical_entity_id=_first_team(conn, league_id),
        identity_audit_id=audit.identity_audit_id, provenance_policy_version="pp-1")
    return corpus, xw


def test_static_identity_needs_no_timestamp_but_needs_a_crosswalk(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus, xw = _static_setup(repo, nba_team_ns, nba_league_id, conn)
    cert = repo.certify_input(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_game_id="g1", feature_family="team_identity",
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        reconstruction_policy_version="rp-1",
        eligibility=EligibilityVerdict.ELIGIBLE,
        availability_basis=AvailabilityBasis.STATIC_IDENTITY,
        crosswalk_id=xw.crosswalk_id)
    assert cert.source_event_completed_at is None
    assert cert.source_snapshot_at is None
    assert cert.availability_rule_id is None
    assert cert.crosswalk_id == xw.crosswalk_id

    with pytest.raises(RepositoryError, match="must cite the static crosswalk"):
        repo.certify_input(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
            provider_game_id="g2", feature_family="team_identity",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE,
            availability_basis=AvailabilityBasis.STATIC_IDENTITY)


def test_static_identity_refuses_a_timestamp(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus, xw = _static_setup(repo, nba_team_ns, nba_league_id, conn)
    with pytest.raises(RepositoryError, match="would not be static"):
        repo.certify_input(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
            provider_game_id="g1", feature_family="team_identity",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE,
            availability_basis=AvailabilityBasis.STATIC_IDENTITY,
            crosswalk_id=xw.crosswalk_id, source_event_completed_at=COMPLETED)


def test_static_identity_requires_a_passed_audit_transitively(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    """A crosswalk cannot exist without an accepted audit, so a STATIC_IDENTITY
    certification cannot exist without one either."""

    corpus, xw = _static_setup(repo, nba_team_ns, nba_league_id, conn)
    row = conn.execute(
        "SELECT identity_audit_id FROM static_crosswalk_provenance "
        "WHERE crosswalk_id = ?", (xw.crosswalk_id,)).fetchone()
    audit = repo.identity_audit(str(row[0]))
    assert audit is not None and audit.verdict == "accepted"


def test_event_derived_requires_completion_rule_and_policy(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    rid = _raw_evidence(conn)
    base = dict(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_game_id="g1", feature_family="team_rolling_core",
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        reconstruction_policy_version="rp-1",
        eligibility=EligibilityVerdict.ELIGIBLE,
        availability_basis=AvailabilityBasis.EVENT_DERIVED,
        availability_source="official_box_score_publication_lag_v1",
        source_evidence_table="raw_responses", source_evidence_id=rid,
    )
    cert = repo.certify_input(
        **base, availability_rule_id="prior_event_completion_conservative_v1",
        source_event_completed_at=COMPLETED)
    assert cert.source_event_completed_at == COMPLETED
    assert cert.availability_rule_digest == lookup_rule(
        "prior_event_completion_conservative_v1").digest

    with pytest.raises(RepositoryError, match="must cite an availability rule"):
        repo.certify_input(**{**base, "provider_game_id": "g2"},
                           source_event_completed_at=COMPLETED)
    with pytest.raises(RepositoryError, match="must record the instant"):
        repo.certify_input(
            **{**base, "provider_game_id": "g3"},
            availability_rule_id="prior_event_completion_conservative_v1")


def test_event_derived_does_not_materialize_effective_at(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    """Availability is derived on read, never stored."""

    corpus = _corpus(repo, nba_league_id)
    cert = repo.certify_input(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_game_id="g1", feature_family="team_rolling_core",
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        reconstruction_policy_version="rp-1",
        eligibility=EligibilityVerdict.ELIGIBLE,
        availability_basis=AvailabilityBasis.EVENT_DERIVED,
        availability_rule_id="prior_event_completion_conservative_v1",
        availability_source="official_box_score_publication_lag_v1",
        source_evidence_table="raw_responses", source_evidence_id=_raw_evidence(conn),
        source_event_completed_at=COMPLETED)
    assert not hasattr(cert, "effective_at")
    derived = derive_availability_instant(
        rule_id=cert.availability_rule_id or "",
        rule_digest=cert.availability_rule_digest or "",
        source_event_completed_at=COMPLETED)
    assert derived == "2026-03-01T09:00:00.000000Z"  # +6h conservative lag


def test_versioned_snapshot_requires_the_provider_stamp(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    cert = repo.certify_input(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_game_id="g1", feature_family="weather_previous_day1",
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        reconstruction_policy_version="rp-1",
        eligibility=EligibilityVerdict.ELIGIBLE,
        availability_basis=AvailabilityBasis.VERSIONED_SNAPSHOT,
        source_evidence_table="raw_responses", source_evidence_id=_raw_evidence(conn),
        source_snapshot_at=SNAPSHOT, availability_source="open_meteo_previous_runs")
    assert cert.source_snapshot_at == SNAPSHOT

    # Evidence and availability source supplied, so the ONLY thing missing is the
    # provider's snapshot stamp -- which is what this case is about.
    with pytest.raises(RepositoryError, match="must record the provider"):
        repo.certify_input(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
            provider_game_id="g2", feature_family="weather_previous_day1",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE,
            availability_source="open_meteo_previous_runs",
            source_evidence_table="raw_responses",
            source_evidence_id=_raw_evidence(conn),
            availability_basis=AvailabilityBasis.VERSIONED_SNAPSHOT)


def test_forward_only_cannot_be_certified_as_reconstructed_research(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    with pytest.raises(RepositoryError, match="never enters the Lane-R path"):
        repo.certify_input(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
            provider_game_id="g1", feature_family="live_lineup",
            provenance_class=ProvenanceClass.STRICT_FORWARD_PIT,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE)
    with pytest.raises(RepositoryError, match="not a reconstruction"):
        _corpus(repo, nba_league_id,
                provenance_class=ProvenanceClass.STRICT_FORWARD_PIT)


def test_missing_rule_version_is_rejected(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    with pytest.raises(UnknownAvailabilityRuleError, match="not implemented"):
        repo.certify_input(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
            provider_game_id="g1", feature_family="team_rolling_core",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE,
            availability_basis=AvailabilityBasis.EVENT_DERIVED,
            availability_rule_id="a_rule_from_a_future_build",
            source_event_completed_at=COMPLETED)


def test_wrong_corpus_version_is_rejected(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    rid = _raw_evidence(conn)
    with pytest.raises(sqlite3.IntegrityError, match="match its corpus version"):
        repo.certify_input(
            corpus_version_id="rcv_does_not_exist", namespace=nba_team_ns,
            provider_game_id="g1", feature_family="team_rolling_core",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE,
            availability_basis=AvailabilityBasis.EVENT_DERIVED,
            availability_rule_id="prior_event_completion_conservative_v1",
            availability_source="official_box_score_publication_lag_v1",
            source_evidence_table="raw_responses", source_evidence_id=rid,
            source_event_completed_at=COMPLETED)


def test_crosswalk_from_another_corpus_cannot_vouch_for_an_input(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    corpus_a, xw = _static_setup(repo, nba_team_ns, nba_league_id, conn)
    corpus_b = _corpus(repo, nba_league_id, target_set_digest="targets-b")
    with pytest.raises(sqlite3.IntegrityError, match="same corpus version"):
        repo.certify_input(
            corpus_version_id=corpus_b.corpus_version_id, namespace=nba_team_ns,
            provider_game_id="g1", feature_family="team_identity",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE,
            availability_basis=AvailabilityBasis.STATIC_IDENTITY,
            crosswalk_id=xw.crosswalk_id)


def test_excluded_input_records_a_reason(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    cert = repo.certify_input(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_game_id="g1", feature_family="weather_previous_day1",
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        reconstruction_policy_version="rp-1",
        eligibility=EligibilityVerdict.EXCLUDED,
        exclusion_code="WEATHER_PRE_2024_UNAVAILABLE",
        availability_basis=AvailabilityBasis.VERSIONED_SNAPSHOT,
        source_snapshot_at=SNAPSHOT)
    assert cert.eligibility == "excluded"
    assert cert.exclusion_code == "WEATHER_PRE_2024_UNAVAILABLE"
    with pytest.raises(RepositoryError, match="mutually determined"):
        repo.certify_input(
            corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
            provider_game_id="g2", feature_family="weather_previous_day1",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.EXCLUDED,
            availability_basis=AvailabilityBasis.VERSIONED_SNAPSHOT,
            source_snapshot_at=SNAPSHOT)


def test_no_feature_value_is_stored(conn: sqlite3.Connection) -> None:
    """§8: prefer certification metadata over a premature feature store."""

    columns = {str(r["name"]) for r in conn.execute(
        "PRAGMA table_info(reconstructed_input_provenance)")}
    for banned in ("value", "feature_value", "features", "payload", "numeric_value"):
        assert banned not in columns


# --------------------------------------------------------------------------- #
# §21 lookup discipline
# --------------------------------------------------------------------------- #
def test_lookups_are_exact_and_absent_is_none(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str,
) -> None:
    corpus = _corpus(repo, nba_league_id)
    assert repo.corpus_version("rcv_nope") is None
    assert repo.corpus_version_by_digest("nope") is None
    assert repo.identity_audit("ida_nope") is None
    assert repo.static_crosswalk(corpus_version_id=corpus.corpus_version_id,
                                 namespace=nba_team_ns, provider_id="nope") is None
    assert repo.certified_input(
        corpus_version_id=corpus.corpus_version_id, namespace=nba_team_ns,
        provider_game_id="nope", feature_family="nope") is None
    assert repo.corpus_version_by_digest(corpus.semantic_digest) == corpus


def test_ambiguous_lookup_fails_closed(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, conn: sqlite3.Connection,
) -> None:
    """Structurally impossible while the UNIQUE constraints hold.

    Reproduced by asking the helper directly, because the point is what the
    repository does IF corruption ever reaches it: refuse, rather than silently
    take the first row SQLite returns.
    """

    _corpus(repo, nba_league_id)
    rows = conn.execute("SELECT * FROM reconstruction_corpus_versions").fetchall()
    with pytest.raises(AmbiguousProvenanceError, match="must match at most one"):
        repo._exactly_one(list(rows) * 2, "a duplicated lookup", repo._to_corpus)


# --------------------------------------------------------------------------- #
# §14 supersession through the repository
# --------------------------------------------------------------------------- #
def test_supersession_creates_a_new_version_and_preserves_the_old(
    repo: SqliteRetrospectiveProvenanceRepository, nba_league_id: str
) -> None:
    old = _corpus(repo, nba_league_id)
    new = _corpus(repo, nba_league_id, source_corpus_digest="source-corpus-digest-b",
                  supersedes_corpus_version_id=old.corpus_version_id)
    assert new.corpus_version_id != old.corpus_version_id
    assert new.semantic_digest != old.semantic_digest
    assert repo.corpus_version(old.corpus_version_id) == old
    assert repo.superseded_by(old.corpus_version_id) == (new,)


def test_corpus_replay_is_idempotent(
    repo: SqliteRetrospectiveProvenanceRepository, nba_league_id: str,
    conn: sqlite3.Connection,
) -> None:
    first = _corpus(repo, nba_league_id)
    second = _corpus(repo, nba_league_id)
    assert first == second
    assert conn.execute(
        "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0] == 1


def test_a_changed_input_changes_the_corpus_digest(
    repo: SqliteRetrospectiveProvenanceRepository, nba_league_id: str
) -> None:
    base = _corpus(repo, nba_league_id)
    for change in (
        {"reconstruction_policy_version": "rp-2"},
        {"cutoff_policy_version": "2"},
        {"source_corpus_digest": "other"},
        {"target_set_digest": "other"},
        {"g1_variant": G1Variant.G1_A_EXTENDED},
        {"evidence_registry_digest": "reg-1"},
    ):
        other = _corpus(repo, nba_league_id, **change)
        assert other.semantic_digest != base.semantic_digest, change


def test_corpus_cannot_supersede_another_league(
    repo: SqliteRetrospectiveProvenanceRepository, nba_league_id: str,
    mlb_league_id: str,
) -> None:
    nba = _corpus(repo, nba_league_id)
    with pytest.raises(RepositoryError, match="same league"):
        _corpus(repo, mlb_league_id, supersedes_corpus_version_id=nba.corpus_version_id)


# --------------------------------------------------------------------------- #
# §9/§15 availability rule registry
# --------------------------------------------------------------------------- #
def test_rule_digest_covers_the_parameters_and_the_evaluation_form() -> None:
    rule = lookup_rule("prior_event_completion_conservative_v1")
    assert rule.digest == verify_rule_digest(rule.rule_id, rule.digest).digest
    # Two rules with different lags have different digests.
    other = lookup_rule("prior_event_completion_immediate_v1")
    assert rule.digest != other.digest


def test_an_edited_rule_fails_closed_rather_than_reinterpreting_a_corpus() -> None:
    """The whole reason the digest is stored (task §9)."""

    with pytest.raises(RetrospectiveProvenanceError, match="has changed since"):
        verify_rule_digest("prior_event_completion_conservative_v1",
                           "a" * 64)
    with pytest.raises(RetrospectiveProvenanceError, match="has changed since"):
        derive_availability_instant(
            rule_id="prior_event_completion_conservative_v1",
            rule_digest="b" * 64, source_event_completed_at=COMPLETED)


def test_every_registered_rule_is_self_consistent() -> None:
    for rule_id, rule in AVAILABILITY_RULES.items():
        assert rule.rule_id == rule_id
        assert rule.lag_seconds >= 0
        assert verify_rule_digest(rule_id, rule.digest) is rule


def test_unknown_rule_lookup_fails_closed() -> None:
    with pytest.raises(UnknownAvailabilityRuleError):
        lookup_rule("nope")


def test_digest_is_insensitive_to_row_order_but_not_to_content() -> None:
    """§15: order-independent, semantically sensitive."""

    rows = [{"id": "b", "n": 2}, {"id": "a", "n": 1}]
    a = semantic_digest({"rows": sorted(rows, key=lambda r: str(r["id"]))})
    b = semantic_digest({"rows": sorted(reversed(rows), key=lambda r: str(r["id"]))})
    assert a == b
    changed = semantic_digest({"rows": [{"id": "a", "n": 99}, {"id": "b", "n": 2}]})
    assert changed != a


def test_digests_exclude_volatile_audit_wall_clock(
    repo: SqliteRetrospectiveProvenanceRepository, nba_team_ns: ProviderNamespace,
    nba_league_id: str, tmp_path: Path,
) -> None:
    """Two identical audits recorded at different times share a digest.

    Which is what makes the digest a statement about the evidence rather than
    about when someone happened to run the audit.
    """

    first = _accepted_audit(repo, nba_team_ns)
    other_path = tmp_path / "second.db"
    initialize_database(other_path)
    with Database(other_path).connection() as conn2, transaction(conn2):
        repo2 = SqliteRetrospectiveProvenanceRepository(conn2)
        league2 = str(conn2.execute(
            "SELECT league_id FROM leagues WHERE code = 'NBA'").fetchone()[0])
        ns2 = ProviderNamespace(league2, "balldontlie", EntityType.TEAM, "v1")
        second = _accepted_audit(repo2, ns2)
    assert first.semantic_digest == second.semantic_digest
    assert first.created_at != "" and second.created_at != ""


# --------------------------------------------------------------------------- #
# §27 atomicity
# --------------------------------------------------------------------------- #
def test_audit_and_finding_roll_back_together(db_path: Path) -> None:
    initialize_database(db_path)
    db = Database(db_path)
    with db.connection() as conn:
        league = str(conn.execute(
            "SELECT league_id FROM leagues WHERE code='NBA'").fetchone()[0])
        ns = ProviderNamespace(league, "balldontlie", EntityType.TEAM, "v1")
        with pytest.raises(RepositoryError):
            with transaction(conn):
                repo = SqliteRetrospectiveProvenanceRepository(conn)
                audit = repo.record_identity_audit(
                    namespace=ns, source_corpus_digest=SRC,
                    audit_policy_version="ap-1", distinct_ids=30,
                    total_observations=239, collision_count=1,
                    verdict=AuditVerdict.REJECTED_COLLISION)
                repo.record_finding(
                    identity_audit_id=audit.identity_audit_id, namespace=ns,
                    severity=FindingSeverity.BLOCKING, finding_code="C1",
                    classification=FindingClassification.IDENTITY_COLLISION,
                    exclusion_scope=ExclusionScope.ENTITY, provider_id="1")
                raise RepositoryError("simulated failure after both writes")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM identity_audit_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM identity_audit_findings").fetchone()[0] == 0


def test_crosswalk_creation_rolls_back_with_its_corpus(db_path: Path) -> None:
    initialize_database(db_path)
    db = Database(db_path)
    with db.connection() as conn:
        league = str(conn.execute(
            "SELECT league_id FROM leagues WHERE code='NBA'").fetchone()[0])
        ns = ProviderNamespace(league, "balldontlie", EntityType.TEAM, "v1")
        team = _first_team(conn, league)
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn):
                repo = SqliteRetrospectiveProvenanceRepository(conn)
                corpus = _corpus(repo, league)
                audit = _accepted_audit(repo, ns)
                repo.record_static_crosswalk(
                    corpus_version_id=corpus.corpus_version_id, namespace=ns,
                    provider_id="1", canonical_entity_id=team,
                    identity_audit_id=audit.identity_audit_id,
                    provenance_policy_version="pp-1")
                # A player crosswalk onto a team id: refused by the f018 trigger.
                player_ns = ProviderNamespace(league, "balldontlie",
                                              EntityType.PLAYER, "v1")
                player_audit = _accepted_audit(repo, player_ns)
                repo.record_static_crosswalk(
                    corpus_version_id=corpus.corpus_version_id, namespace=player_ns,
                    provider_id="9", canonical_entity_id=team,
                    identity_audit_id=player_audit.identity_audit_id,
                    provenance_policy_version="pp-1")
    with db.connection() as conn:
        # No partially accepted provenance state survived.
        for table in ("reconstruction_corpus_versions", "identity_audit_records",
                      "static_crosswalk_provenance"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608


def test_reconstruction_record_and_certification_roll_back_together(
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
                corpus = _corpus(repo, league)
                repo.certify_input(
                    corpus_version_id=corpus.corpus_version_id, namespace=ns,
                    provider_game_id="g1", feature_family="rest_days",
                    provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                    reconstruction_policy_version="rp-1",
                    eligibility=EligibilityVerdict.ELIGIBLE,
                    availability_basis=AvailabilityBasis.EVENT_DERIVED,
                    availability_rule_id="prior_event_completion_conservative_v1",
                    source_event_completed_at=COMPLETED)
                raise RepositoryError("simulated failure")
    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reconstructed_input_provenance").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0] == 0


def test_concurrent_duplicate_insert_converges(db_path: Path) -> None:
    """Two writers recording the same corpus converge on one row.

    Serialized rather than truly parallel: SQLite takes a write lock anyway, so
    the property under test is convergence, not thread interleaving.
    """

    initialize_database(db_path)
    db = Database(db_path)
    digests = []
    for _ in range(2):
        with db.connection() as conn, transaction(conn):
            league = str(conn.execute(
                "SELECT league_id FROM leagues WHERE code='NBA'").fetchone()[0])
            digests.append(
                _corpus(SqliteRetrospectiveProvenanceRepository(conn), league)
                .semantic_digest)
    assert digests[0] == digests[1]
    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reconstruction_corpus_versions").fetchone()[0] == 1


def test_conflicting_digest_insert_fails_closed(db_path: Path) -> None:
    """A second row claiming an existing corpus digest is refused."""

    initialize_database(db_path)
    db = Database(db_path)
    with db.connection() as conn:
        league = str(conn.execute(
            "SELECT league_id FROM leagues WHERE code='NBA'").fetchone()[0])
        with transaction(conn):
            corpus = _corpus(SqliteRetrospectiveProvenanceRepository(conn), league)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reconstruction_corpus_versions "
                "(corpus_version_id, provenance_class, league_id, "
                " reconstruction_policy_version, cutoff_policy_id, "
                " cutoff_policy_version, source_corpus_digest, target_set_digest, "
                " g1_variant, semantic_digest, created_at) "
                "VALUES ('rcv_impostor', 'reconstructed_research', ?, 'other', 'cp', "
                "'9', 'different', 'different', 'g1_a_extended', ?, "
                "'2026-08-11T00:00:00.000000Z')",
                (league, corpus.semantic_digest),
            )


def test_supersession_race_leaves_both_versions_intact(db_path: Path) -> None:
    """Two corpora superseding the same parent both succeed; the parent is untouched.

    Recorded rather than resolved: which of two competing successors is
    authoritative is a question for the reader, and silently picking one here
    would be exactly the "latest wins" shortcut this lane refuses.
    """

    initialize_database(db_path)
    db = Database(db_path)
    with db.connection() as conn, transaction(conn):
        repo = SqliteRetrospectiveProvenanceRepository(conn)
        league = str(conn.execute(
            "SELECT league_id FROM leagues WHERE code='NBA'").fetchone()[0])
        parent = _corpus(repo, league)
        before = repo.corpus_version(parent.corpus_version_id)
        a = _corpus(repo, league, source_corpus_digest="b",
                    supersedes_corpus_version_id=parent.corpus_version_id)
        b = _corpus(repo, league, source_corpus_digest="c",
                    supersedes_corpus_version_id=parent.corpus_version_id)
        assert repo.corpus_version(parent.corpus_version_id) == before
        assert set(repo.superseded_by(parent.corpus_version_id)) == {a, b}
