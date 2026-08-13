"""``RetrospectiveResearchReader`` -- Lane-R admission contract.

Everything here is synthetic and offline. The load-bearing tests are the ones
that prove a refusal is *structural* rather than conventional: a FORWARD_ONLY
family has no code path to a return value, and the strict-forward reader gains
nothing at all from this module existing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.schema import utc_now_iso
from sports_quant.retrospective.families import (
    FEATURE_FAMILIES,
    FamilyClass,
    ForwardOnlyFamilyError,
    UnknownFeatureFamilyError,
    lookup_family,
)
from sports_quant.retrospective.provenance import (
    AuditVerdict,
    AvailabilityBasis,
    EligibilityVerdict,
    EntityType,
    G1Variant,
    ProvenanceClass,
    ProviderNamespace,
)
from sports_quant.retrospective.reader import (
    READER_POLICY_VERSION,
    AdmissionOutcome,
    AdmittedInput,
    LaneRAdmissionError,
    RejectedInput,
    RetrospectiveResearchReader,
)
from sports_quant.retrospective.rules import AVAILABILITY_RULES

MLB = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.GAME, "v1")
NBA = ProviderNamespace("lg_nba", "balldontlie", EntityType.GAME, "v1")
MLB_TEAM = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.TEAM, "v1")

TARGET = "700500"
PRIOR = "700100"
#: Target game starts 2026-06-10T23:00Z; the pregame cutoff is an hour before.
CUTOFF = "2026-06-10T22:00:00.000000Z"
#: A prior game that finished well before the cutoff (conservative rule = +6h).
PRIOR_COMPLETED = "2026-06-09T03:00:00.000000Z"
CONSERVATIVE = "prior_event_completion_conservative_v1"
IMMEDIATE = "prior_event_completion_immediate_v1"


class Corpus:
    """A v19 output database with one reconstruction corpus prepared."""

    def __init__(self, path: Path, *, league: str = "lg_mlb",
                 g1_variant: G1Variant = G1Variant.G1_B_CORE) -> None:
        initialize_database(path)
        self.path = path
        self.league = league
        self._n = 0
        with Database(path).connection() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            with transaction(conn):
                repo = SqliteRetrospectiveProvenanceRepository(conn)
                self.corpus = repo.record_corpus_version(
                    provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                    league_id=league, reconstruction_policy_version="reader-test-v1",
                    cutoff_policy_id="pregame", cutoff_policy_version="1",
                    source_corpus_digest="src-digest", target_set_digest="targets",
                    g1_variant=g1_variant, code_version="test")
        self.corpus_version_id = self.corpus.corpus_version_id

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """An open connection with FKs relaxed for synthetic evidence rows.

        A real context manager: returning ``Database(...).connection().__enter__()``
        lets the manager be collected, which closes the connection underneath the
        caller.
        """

        with Database(self.path).connection() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            yield conn

    def evidence_row(self, conn: sqlite3.Connection, *, table: str,
                     row_id: str, provider_game_id: str) -> None:
        """A minimal cited evidence row, only as much as the FK check needs."""

        self._n += 1
        now = utc_now_iso()
        if table == "team_game_statistics":
            conn.execute(
                "INSERT INTO team_game_statistics (stat_id, game_ref_id, provider, "
                "provider_game_id, provider_team_id, home_away, observed_at, "
                "ingested_at, raw_response_id, raw_response_hash, content_hash, "
                "created_at) VALUES (?,?,?,?,'147','home',?,?,'raw','h',?,?)",
                (row_id, f"pgr_{provider_game_id}", "mlb_statsapi",
                 provider_game_id, now, now, f"ch_{self._n}", now))
        else:   # game_result_snapshots
            conn.execute(
                "INSERT INTO game_result_snapshots (result_id, game_ref_id, "
                "provider, provider_game_id, mapped_status, is_correction, "
                "observed_at, ingested_at, raw_response_id, raw_response_hash, "
                "content_hash, created_at) "
                "VALUES (?,?,?,?,'final',0,?,?,'raw','h',?,?)",
                (row_id, f"pgr_{provider_game_id}", "mlb_statsapi",
                 provider_game_id, now, now, f"ch_{self._n}", now))

    def certify(self, conn: sqlite3.Connection, *, family: str,
                namespace: ProviderNamespace = MLB,
                provider_game_id: str = TARGET,
                provenance_class: ProvenanceClass =
                ProvenanceClass.RECONSTRUCTED_RESEARCH,
                eligibility: EligibilityVerdict = EligibilityVerdict.ELIGIBLE,
                basis: Optional[AvailabilityBasis] = None,
                rule_id: Optional[str] = None,
                completed_at: Optional[str] = None,
                snapshot_at: Optional[str] = None,
                evidence_table: Optional[str] = None,
                evidence_id: Optional[str] = None,
                crosswalk_id: Optional[str] = None,
                availability_source: Optional[str] = None,
                exclusion_code: Optional[str] = None,
                corpus_version_id: Optional[str] = None) -> Any:
        if availability_source is None and basis is not None:
            availability_source = "reader-test availability documentation"
        repo = SqliteRetrospectiveProvenanceRepository(conn)
        return repo.certify_input(
            corpus_version_id=corpus_version_id or self.corpus_version_id,
            namespace=namespace, provider_game_id=provider_game_id,
            feature_family=family, provenance_class=provenance_class,
            reconstruction_policy_version="reader-test-v1",
            eligibility=eligibility, availability_basis=basis,
            availability_rule_id=rule_id, availability_source=availability_source,
            source_evidence_table=evidence_table, source_evidence_id=evidence_id,
            source_event_completed_at=completed_at, source_snapshot_at=snapshot_at,
            crosswalk_id=crosswalk_id, exclusion_code=exclusion_code)

    def attest_team(self, conn: sqlite3.Connection, *,
                    provider_team_id: str = "147",
                    canonical: str = "tm_mlb_nyy",
                    corpus_version_id: Optional[str] = None) -> Any:
        """A real audited crosswalk, written the way TEAM-A writes one."""

        repo = SqliteRetrospectiveProvenanceRepository(conn)
        target_corpus = corpus_version_id or self.corpus_version_id
        corpus = repo.corpus_version(target_corpus)
        assert corpus is not None
        audit = repo.record_identity_audit(
            namespace=MLB_TEAM, source_corpus_digest=corpus.source_corpus_digest,
            audit_policy_version="reader-test", distinct_ids=1,
            total_observations=2, collision_count=0,
            verdict=AuditVerdict.ACCEPTED)
        return repo.record_static_crosswalk(
            corpus_version_id=target_corpus, namespace=MLB_TEAM,
            provider_id=provider_team_id, canonical_entity_id=canonical,
            identity_audit_id=audit.identity_audit_id,
            provenance_policy_version="g5-team-attestation-v1")

    def reader(self, conn: sqlite3.Connection, *,
               cutoff: str = CUTOFF,
               corpus_version_id: Optional[str] = None
               ) -> RetrospectiveResearchReader:
        return RetrospectiveResearchReader(
            conn, corpus_version_id=corpus_version_id or self.corpus_version_id,
            cutoff=cutoff)


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    return Corpus(tmp_path / "out.db")


# --------------------------------------------------------------------------- #
# 1-2. Strict PIT is untouched, and no bypass exists on either reader
# --------------------------------------------------------------------------- #
def test_the_strict_reader_gained_nothing_from_this_module() -> None:
    """§I.1/§I.2: the strict reader must be exactly as it was."""

    import hashlib
    import inspect

    from sports_quant.pit.asof import AsOfReader
    from sports_quant.pit.dataset import _feature_cutoff

    digest = hashlib.sha256(
        inspect.getsource(_feature_cutoff).encode("utf-8")).hexdigest()[:32]
    assert digest == "5d55345b6e2d8836df83428de82462df", (
        "_feature_cutoff moved while implementing the Lane-R reader")

    names = set(dir(AsOfReader))
    for banned in ("retrospective", "reconstructed", "lane_r", "research",
                   "effective_at", "ignore_pit", "bypass", "override", "unsafe",
                   "admit"):
        assert not any(banned in n for n in names), banned
    params = set(AsOfReader.__init__.__code__.co_varnames)
    assert not {p for p in params
                if any(b in p for b in ("retro", "research", "effective", "lane"))}


def test_the_two_readers_share_no_type_and_no_construction_path() -> None:
    """Lane selection is a type, not a flag: neither can become the other."""

    from sports_quant.pit.asof import AsOfReader

    assert not issubclass(RetrospectiveResearchReader, AsOfReader)
    assert not issubclass(AsOfReader, RetrospectiveResearchReader)
    # The Lane-R reader must not be reachable from the strict module.
    import sports_quant.pit.asof as asof_module

    assert not hasattr(asof_module, "RetrospectiveResearchReader")
    source = "".join(inspect_source(asof_module).split('"""')[::2])
    for banned in ("retrospective", "effective_at", "reconstructed"):
        assert banned not in source, banned


def inspect_source(module: Any) -> str:
    import inspect

    return inspect.getsource(module)


# --------------------------------------------------------------------------- #
# 3-4. The cutoff, both sides of the boundary
# --------------------------------------------------------------------------- #
def test_event_derived_evidence_before_the_cutoff_is_admitted(
    corpus: Corpus,
) -> None:
    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")

    assert isinstance(decision, AdmittedInput), decision
    assert decision.availability_basis is AvailabilityBasis.EVENT_DERIVED
    # completion 2026-06-09T03:00Z + 6h = 09:00Z, comfortably before the cutoff.
    assert decision.effective_at == "2026-06-09T09:00:00.000000Z"
    assert decision.cutoff == CUTOFF
    assert decision.availability_rule_id == CONSERVATIVE
    assert decision.availability_rule_digest == AVAILABILITY_RULES[CONSERVATIVE].digest
    assert decision.reader_policy_version == READER_POLICY_VERSION


def test_the_same_evidence_after_the_cutoff_is_rejected(corpus: Corpus) -> None:
    """Identical certification, earlier cutoff: the answer must flip."""

    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
        early = corpus.reader(conn, cutoff="2026-06-09T08:59:59.999999Z")
        decision = early.admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.NOT_YET_AVAILABLE
    assert decision.effective_at == "2026-06-09T09:00:00.000000Z"


@pytest.mark.parametrize("cutoff,admitted", [
    ("2026-06-09T08:59:59.999999Z", False),   # one microsecond early
    ("2026-06-09T09:00:00.000000Z", True),    # exactly at availability
    ("2026-06-09T09:00:00.000001Z", True),    # one microsecond late
])
def test_event_derived_lag_is_enforced_at_the_exact_boundary(
    corpus: Corpus, cutoff: str, admitted: bool
) -> None:
    """§I.12: `effective_at <= T_cut`, inclusive, to the microsecond."""

    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
        decision = corpus.reader(conn, cutoff=cutoff).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")
    assert isinstance(decision, AdmittedInput) is admitted, decision


def test_the_zero_lag_rule_is_a_different_answer(corpus: Corpus) -> None:
    """The optimistic bound exists and genuinely differs from the conservative."""

    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="game_result_snapshots",
                                row_id="grs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="prior_results",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=IMMEDIATE, completed_at=PRIOR_COMPLETED,
                           evidence_table="game_result_snapshots",
                           evidence_id="grs_prior")
        decision = corpus.reader(
            conn, cutoff="2026-06-09T04:00:00.000000Z").admit_feature(
            namespace=MLB, provider_game_id=TARGET, feature_family="prior_results")

    assert isinstance(decision, AdmittedInput), decision
    assert decision.effective_at == PRIOR_COMPLETED   # +0h
    assert decision.availability_rule_id == IMMEDIATE


# --------------------------------------------------------------------------- #
# 5. FORWARD_ONLY families are structurally unreachable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("family", ["lineups", "injuries", "rosters",
                                    "probable_pitchers"])
def test_a_forward_only_family_cannot_be_read_at_any_cutoff(
    corpus: Corpus, family: str
) -> None:
    """§I.5: not filtered -- refused, before any provenance is consulted."""

    assert lookup_family(family).classification is FamilyClass.FORWARD_ONLY

    with corpus.connection() as conn:
        # Even WITH a certification present and a cutoff far in the future.
        with transaction(conn):
            corpus.evidence_row(conn, table="game_result_snapshots",
                                row_id="grs_x", provider_game_id=PRIOR)
            corpus.certify(conn, family=family,
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="game_result_snapshots",
                           evidence_id="grs_x")
        reader = corpus.reader(conn, cutoff="2099-01-01T00:00:00.000000Z")
        with pytest.raises(ForwardOnlyFamilyError, match="FORWARD_ONLY"):
            reader.admit_feature(namespace=MLB, provider_game_id=TARGET,
                                 feature_family=family)
        # And it cannot be smuggled in as a label either.
        with pytest.raises(ForwardOnlyFamilyError):
            reader.admit_label(namespace=MLB, provider_game_id=TARGET,
                               feature_family=family)


def test_an_unknown_family_is_refused_rather_than_defaulted(
    corpus: Corpus,
) -> None:
    """A typo must not inherit some other family's leakage rules."""

    with corpus.connection() as conn:
        reader = corpus.reader(conn)
        with pytest.raises(UnknownFeatureFamilyError):
            reader.admit_feature(namespace=MLB, provider_game_id=TARGET,
                                 feature_family="team_rolling_statz")


def test_a_forward_only_family_has_no_admissible_basis() -> None:
    """The taxonomy itself forbids it, independently of the reader."""

    for family in FEATURE_FAMILIES.values():
        if family.classification is FamilyClass.FORWARD_ONLY:
            assert family.admissible_bases == frozenset()
            assert not family.is_feature


# --------------------------------------------------------------------------- #
# 6. provider_*_references can never be Lane-R feature evidence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table", ["provider_team_references",
                                   "provider_game_references",
                                   "provider_player_references"])
def test_provider_reference_tables_are_not_admissible_evidence(
    corpus: Corpus, table: str
) -> None:
    """§I.6: forbidden in BOTH lanes, and refused at certification time."""

    from sports_quant.retrospective.evidence import SOURCE_EVIDENCE_TABLES

    assert table not in SOURCE_EVIDENCE_TABLES
    with corpus.connection() as conn:
        with pytest.raises(Exception) as caught:
            with transaction(conn):
                corpus.certify(conn, family="team_rolling_stats",
                               basis=AvailabilityBasis.EVENT_DERIVED,
                               rule_id=CONSERVATIVE,
                               completed_at=PRIOR_COMPLETED,
                               evidence_table=table, evidence_id="ptr_1")
        assert "evidence" in str(caught.value).lower(), caught.value


# --------------------------------------------------------------------------- #
# 7. Static identity resolves only through audited crosswalk provenance
# --------------------------------------------------------------------------- #
def test_static_identity_resolves_through_the_crosswalk_not_by_name(
    corpus: Corpus,
) -> None:
    with corpus.connection() as conn:
        with transaction(conn):
            crosswalk = corpus.attest_team(conn)
        identity = corpus.reader(conn).static_identity(
            namespace=MLB_TEAM, provider_id="147")

    assert identity.canonical_entity_id == "tm_mlb_nyy"
    assert identity.crosswalk_id == crosswalk.crosswalk_id
    assert identity.identity_audit_id == crosswalk.identity_audit_id
    assert identity.corpus_version_id == corpus.corpus_version_id


def test_static_identity_has_no_name_or_fuzzy_fallback_in_code() -> None:
    """Structural: the resolution path must not mention name evidence at all."""

    import inspect

    from sports_quant.retrospective import reader as reader_module

    source = "".join(inspect.getsource(reader_module).split('"""')[::2])
    for banned in ("normalize_name", "normalized_name", "full_name", "alias",
                   "abbreviation", "nickname", "difflib", "fuzz", "levenshtein"):
        assert banned not in source, banned


def test_an_unattested_provider_id_has_no_static_identity(corpus: Corpus) -> None:
    with corpus.connection() as conn:
        with pytest.raises(LaneRAdmissionError, match="no static crosswalk"):
            corpus.reader(conn).static_identity(
                namespace=MLB_TEAM, provider_id="999999")


def test_static_identity_is_not_wall_clock_gated(corpus: Corpus) -> None:
    """A timeless fact must resolve at ANY cutoff, including a very early one."""

    with corpus.connection() as conn:
        with transaction(conn):
            crosswalk = corpus.attest_team(conn)
            corpus.certify(conn, family="static_identity",
                           basis=AvailabilityBasis.STATIC_IDENTITY,
                           crosswalk_id=crosswalk.crosswalk_id,
                           availability_source="team-a attestation map")
        ancient = corpus.reader(conn, cutoff="1901-01-01T00:00:00.000000Z")
        decision = ancient.admit_feature(namespace=MLB, provider_game_id=TARGET,
                                         feature_family="static_identity")

    assert isinstance(decision, AdmittedInput), decision
    assert decision.effective_at is None, (
        "a static identity was given an availability instant it does not have")
    assert decision.availability_basis is AvailabilityBasis.STATIC_IDENTITY


# --------------------------------------------------------------------------- #
# 8-10. Provenance failures all fail closed
# --------------------------------------------------------------------------- #
def test_a_crosswalk_from_another_corpus_does_not_authorize_this_one(
    tmp_path: Path,
) -> None:
    """§I.8: an audit of one corpus never authorizes another.

    Two independent layers, both proved here. The f018 trigger refuses the
    certification outright, so the bad row normally cannot exist; the reader
    checks again anyway, because a reader that trusts its inputs is how a corpus
    boundary leaks when someone writes with direct SQL.
    """

    corpus = Corpus(tmp_path / "out.db")
    with corpus.connection() as conn:
        repo = SqliteRetrospectiveProvenanceRepository(conn)
        with transaction(conn):
            other = repo.record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id="lg_mlb", reconstruction_policy_version="other-v1",
                cutoff_policy_id="pregame", cutoff_policy_version="1",
                source_corpus_digest="src-digest",
                target_set_digest="OTHER-targets",
                g1_variant=G1Variant.G1_B_CORE, code_version="test")
            foreign = corpus.attest_team(
                conn, corpus_version_id=other.corpus_version_id)

        # Layer 1: the database refuses to record it at all.
        with pytest.raises(Exception) as caught:
            with transaction(conn):
                corpus.certify(conn, family="static_identity",
                               basis=AvailabilityBasis.STATIC_IDENTITY,
                               crosswalk_id=foreign.crosswalk_id,
                               availability_source="cross-corpus attempt")
        assert "same corpus version" in str(caught.value), caught.value

        # Layer 2: plant it behind the trigger's back and read it anyway.
        with transaction(conn):
            conn.execute("DROP TRIGGER trg_rip_crosswalk_same_corpus")
            corpus.certify(conn, family="static_identity",
                           basis=AvailabilityBasis.STATIC_IDENTITY,
                           crosswalk_id=foreign.crosswalk_id,
                           availability_source="cross-corpus attempt")
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="static_identity")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.CROSSWALK_FROM_ANOTHER_CORPUS


def test_missing_provenance_fails_closed(corpus: Corpus) -> None:
    """§I.10: no certification means no admission, not a default."""

    with corpus.connection() as conn:
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")
    assert isinstance(decision, RejectedInput)
    assert decision.outcome is AdmissionOutcome.NO_CERTIFICATION


def test_an_excluded_certification_is_never_admitted(corpus: Corpus) -> None:
    """§I.9: a blocking verdict blocks.

    This is the case the plain-string/enum mismatch would have silently passed.
    """

    with corpus.connection() as conn:
        with transaction(conn):
            corpus.certify(conn, family="team_rolling_stats",
                           eligibility=EligibilityVerdict.EXCLUDED,
                           exclusion_code="G5_NAMESPACE_REJECTED",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED)
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.CERTIFIED_EXCLUDED
    assert "G5_NAMESPACE_REJECTED" in decision.detail


def test_a_superseded_corpus_cannot_be_read_at_all(tmp_path: Path) -> None:
    """§I.9: superseded evidence must not silently back a result."""

    corpus = Corpus(tmp_path / "out.db")
    with corpus.connection() as conn:
        repo = SqliteRetrospectiveProvenanceRepository(conn)
        with transaction(conn):
            repo.record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id="lg_mlb", reconstruction_policy_version="reader-test-v1",
                cutoff_policy_id="pregame", cutoff_policy_version="1",
                source_corpus_digest="src-digest", target_set_digest="v2-targets",
                g1_variant=G1Variant.G1_B_CORE, code_version="test",
                supersedes_corpus_version_id=corpus.corpus_version_id)
        with pytest.raises(LaneRAdmissionError, match="superseded"):
            corpus.reader(conn)


def test_a_nonexistent_corpus_cannot_be_read(corpus: Corpus) -> None:
    with corpus.connection() as conn:
        with pytest.raises(LaneRAdmissionError, match="does not exist"):
            corpus.reader(conn, corpus_version_id="rcv_not_real")


def test_a_basis_that_contradicts_the_family_fails_closed(
    corpus: Corpus,
) -> None:
    """A rolling-stats family certified as a market snapshot is a contradiction."""

    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="game_result_snapshots",
                                row_id="grs_snap", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.VERSIONED_SNAPSHOT,
                           snapshot_at="2026-06-01T00:00:00.000000Z",
                           evidence_table="game_result_snapshots",
                           evidence_id="grs_snap",
                           availability_source="odds archive")
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.BASIS_CONTRADICTS_FAMILY


# --------------------------------------------------------------------------- #
# 11. The target game's own statistics can never become a feature
# --------------------------------------------------------------------------- #
def test_target_game_statistics_cannot_leak_into_prior_event_features(
    corpus: Corpus,
) -> None:
    """§I.11: refused structurally, not merely by arithmetic.

    The cited evidence row belongs to the TARGET game. Even with a completion
    instant and cutoff that would otherwise pass, this must be refused.
    """

    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_target", provider_game_id=TARGET)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE,
                           completed_at=PRIOR_COMPLETED,     # a lie that passes time
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_target")
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.TARGET_GAME_SELF_REFERENCE


# --------------------------------------------------------------------------- #
# 13. Labels are not features, in either direction
# --------------------------------------------------------------------------- #
def test_a_label_can_never_be_returned_as_a_feature(corpus: Corpus) -> None:
    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="game_result_snapshots",
                                row_id="grs_label", provider_game_id=TARGET)
            corpus.certify(
                conn, family="final_result",
                provenance_class=ProvenanceClass.LABEL_ONLY_RETROSPECTIVE,
                evidence_table="game_result_snapshots", evidence_id="grs_label")
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET, feature_family="final_result")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.LABEL_REQUESTED_AS_FEATURE


def test_a_label_is_returned_only_when_explicitly_requested(
    corpus: Corpus,
) -> None:
    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="game_result_snapshots",
                                row_id="grs_label", provider_game_id=TARGET)
            corpus.certify(
                conn, family="final_result",
                provenance_class=ProvenanceClass.LABEL_ONLY_RETROSPECTIVE,
                evidence_table="game_result_snapshots", evidence_id="grs_label")
        decision = corpus.reader(conn).admit_label(
            namespace=MLB, provider_game_id=TARGET, feature_family="final_result")

    assert isinstance(decision, AdmittedInput), decision
    assert decision.family_class is FamilyClass.LABEL_ONLY
    assert decision.effective_at is None


def test_a_feature_cannot_be_obtained_through_the_label_method(
    corpus: Corpus,
) -> None:
    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
        decision = corpus.reader(conn).admit_label(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.FEATURE_REQUESTED_AS_LABEL


# --------------------------------------------------------------------------- #
# 14. MLB / NBA symmetry where the contract is league-neutral
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("league,namespace", [("lg_mlb", MLB), ("lg_nba", NBA)])
def test_league_neutral_families_behave_identically(
    tmp_path: Path, league: str, namespace: ProviderNamespace
) -> None:
    corpus = Corpus(tmp_path / f"{league}.db", league=league)
    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="game_result_snapshots",
                                row_id="grs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="prior_results", namespace=namespace,
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="game_result_snapshots",
                           evidence_id="grs_prior")
        decision = corpus.reader(conn).admit_feature(
            namespace=namespace, provider_game_id=TARGET,
            feature_family="prior_results")

    assert isinstance(decision, AdmittedInput), decision
    assert decision.effective_at == "2026-06-09T09:00:00.000000Z"


def test_a_league_specific_family_is_refused_in_the_other_league(
    tmp_path: Path,
) -> None:
    """`plays_derived_stats` is NBA-only; asking for it in MLB is a category error."""

    corpus = Corpus(tmp_path / "mlb.db", league="lg_mlb")
    with corpus.connection() as conn:
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="plays_derived_stats")
    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.WRONG_LEAGUE_FOR_FAMILY


# --------------------------------------------------------------------------- #
# 15. Deterministic replay
# --------------------------------------------------------------------------- #
def test_repeated_reads_are_byte_identical(corpus: Corpus) -> None:
    import json

    families = ("team_rolling_stats", "prior_results", "static_identity",
                "final_result", "sportsbook_moneyline")
    with corpus.connection() as conn:
        with transaction(conn):
            crosswalk = corpus.attest_team(conn)
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
            corpus.certify(conn, family="static_identity",
                           basis=AvailabilityBasis.STATIC_IDENTITY,
                           crosswalk_id=crosswalk.crosswalk_id,
                           availability_source="team-a map")
            corpus.evidence_row(conn, table="game_result_snapshots",
                                row_id="grs_label", provider_game_id=TARGET)
            corpus.certify(
                conn, family="final_result",
                provenance_class=ProvenanceClass.LABEL_ONLY_RETROSPECTIVE,
                evidence_table="game_result_snapshots", evidence_id="grs_label")

        first = corpus.reader(conn).admit_features(
            namespace=MLB, provider_game_id=TARGET, feature_families=families)
        second = corpus.reader(conn).admit_features(
            namespace=MLB, provider_game_id=TARGET,
            feature_families=tuple(reversed(families)))

    assert json.dumps(first.as_json(), sort_keys=True) == \
        json.dumps(second.as_json(), sort_keys=True), (
        "the reader is order-dependent or otherwise non-deterministic")
    assert len(first.admitted) == 2       # rolling stats + static identity
    outcomes = {r.feature_family: r.outcome for r in first.rejected}
    assert outcomes["final_result"] is AdmissionOutcome.LABEL_REQUESTED_AS_FEATURE
    assert outcomes["sportsbook_moneyline"] is AdmissionOutcome.NO_CERTIFICATION


# --------------------------------------------------------------------------- #
# Core vs extended honesty (§E)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("variant,sensitive", [
    (G1Variant.G1_B_CORE, False),
    (G1Variant.G1_A_EXTENDED, True),
])
def test_correction_sensitivity_is_carried_on_every_admission(
    tmp_path: Path, variant: G1Variant, sensitive: bool
) -> None:
    """Extended evidence may never be described as transaction-time-exact."""

    corpus = Corpus(tmp_path / f"{variant.value}.db", g1_variant=variant)
    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
        decision = corpus.reader(conn).admit_feature(
            namespace=MLB, provider_game_id=TARGET,
            feature_family="team_rolling_stats")

    assert isinstance(decision, AdmittedInput)
    assert decision.correction_sensitive is sensitive
    assert decision.as_json()["correction_sensitive"] is sensitive


# --------------------------------------------------------------------------- #
# The reader does not compute features, and touches no forward table
# --------------------------------------------------------------------------- #
def test_the_reader_never_reads_a_forward_only_evidence_table(
    corpus: Corpus,
) -> None:
    """Traced at the SQL level, not inferred from source."""

    statements: list[str] = []
    with corpus.connection() as conn:
        with transaction(conn):
            crosswalk = corpus.attest_team(conn)
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
            corpus.certify(conn, family="static_identity",
                           basis=AvailabilityBasis.STATIC_IDENTITY,
                           crosswalk_id=crosswalk.crosswalk_id,
                           availability_source="team-a map")
        conn.set_trace_callback(statements.append)
        corpus.reader(conn).admit_features(
            namespace=MLB, provider_game_id=TARGET,
            feature_families=("team_rolling_stats", "static_identity"))
        conn.set_trace_callback(None)

    executed = " ".join(statements).lower()
    for forbidden in ("lineup_snapshots", "lineup_players", "injury_snapshots",
                      "roster_snapshots", "probable_pitcher_snapshots",
                      "provider_team_references", "provider_game_references"):
        assert forbidden not in executed, f"the reader touched {forbidden}"
    # And it must not be writing anything.
    for write in ("insert ", "update ", "delete ", "drop ", "alter "):
        assert write not in executed, f"the reader issued a {write.strip()}"


# --------------------------------------------------------------------------- #
# Regressions for the plain-string/enum trap
#
# The provenance models hold `provenance_class`, `eligibility`, `availability_basis`
# and `g1_variant` as plain `str`. Comparing one to an enum member with `is` is
# False forever -- silently, with no error anywhere. Three separate gates in this
# reader were written that way and every one of them failed OPEN. These tests pin
# the parsed behaviour so the trap cannot be reintroduced.
# --------------------------------------------------------------------------- #
def test_a_strict_forward_corpus_cannot_be_opened_by_the_lane_r_reader(
    tmp_path: Path,
) -> None:
    """The lane gate must actually close.

    `certify_input`/`record_corpus_version` refuse a strict-forward corpus, so
    one is planted with direct SQL to test the reader's own gate in isolation.
    """

    corpus = Corpus(tmp_path / "out.db")
    with corpus.connection() as corpus_conn:
        with transaction(corpus_conn):
            corpus_conn.execute("DROP TRIGGER trg_rcv_no_update")
            corpus_conn.execute(
                "UPDATE reconstruction_corpus_versions "
                "SET provenance_class = 'strict_forward_pit' "
                "WHERE corpus_version_id = ?", (corpus.corpus_version_id,))
        with pytest.raises(LaneRAdmissionError, match="strict-forward"):
            corpus.reader(corpus_conn)


def test_an_unrecognized_stored_enum_value_fails_closed(
    corpus: Corpus,
) -> None:
    """A value with no known meaning is refused, never defaulted.

    f018 CHECK constraints already restrict these columns, so an unknown value
    cannot normally be stored at all -- that is the first layer. The reader's
    parser is the second, and it is tested directly here because the first layer
    makes the end-to-end case unconstructible.
    """

    from sports_quant.retrospective.reader import _parse

    with corpus.connection() as conn:
        with transaction(conn):
            corpus.evidence_row(conn, table="team_game_statistics",
                                row_id="tgs_prior", provider_game_id=PRIOR)
            corpus.certify(conn, family="team_rolling_stats",
                           basis=AvailabilityBasis.EVENT_DERIVED,
                           rule_id=CONSERVATIVE, completed_at=PRIOR_COMPLETED,
                           evidence_table="team_game_statistics",
                           evidence_id="tgs_prior")
        assert conn.execute(
            "SELECT COUNT(*) FROM reconstructed_input_provenance"
            ).fetchone()[0] == 1
        with pytest.raises(Exception) as db_layer:
            with transaction(conn):
                conn.execute("DROP TRIGGER trg_rip_no_update")
                conn.execute(
                    "UPDATE reconstructed_input_provenance "
                    "SET eligibility = 'maybe'")
        assert "eligibility" in str(db_layer.value)

    with pytest.raises(LaneRAdmissionError, match="does not recognize"):
        _parse(EligibilityVerdict, "not-a-real-value", "eligibility")
    with pytest.raises(LaneRAdmissionError, match="does not recognize"):
        _parse(ProvenanceClass, "not-a-real-value", "provenance_class")
    with pytest.raises(LaneRAdmissionError, match="does not recognize"):
        _parse(AvailabilityBasis, "not-a-real-value", "availability_basis")
    with pytest.raises(LaneRAdmissionError, match="does not recognize"):
        _parse(G1Variant, "not-a-real-value", "g1_variant")

    # And every legitimate value still round-trips.
    assert _parse(EligibilityVerdict, "eligible", "eligibility") is         EligibilityVerdict.ELIGIBLE
    assert _parse(ProvenanceClass, "reconstructed_research", "provenance_class") is         ProvenanceClass.RECONSTRUCTED_RESEARCH
    assert _parse(AvailabilityBasis, "event_derived", "availability_basis") is         AvailabilityBasis.EVENT_DERIVED
    assert _parse(G1Variant, "g1_a_extended", "g1_variant") is         G1Variant.G1_A_EXTENDED


def test_no_reader_gate_compares_a_stored_string_to_an_enum_with_is() -> None:
    """Structural guard against reintroducing the trap.

    Every stored-enum column must reach a comparison through `_parse`. A bare
    `cert.<field> is <Enum>.<MEMBER>` is the exact shape that failed open three
    times, so it is banned outright.
    """

    import inspect
    import re

    from sports_quant.retrospective import reader as reader_module

    source = "".join(inspect.getsource(reader_module).split('"""')[::2])
    for field in ("eligibility", "provenance_class", "availability_basis",
                  "g1_variant"):
        bad = [m for m in re.findall(
            rf"\.{field}\s+is(?:\s+not)?\s+(\w+)", source) if m != "None"]
        assert not bad, (
            f"{field} is compared to an enum with `is` without parsing: {bad}. "
            "The model holds it as a plain str, so that comparison is always "
            "False and the gate fails open.")
