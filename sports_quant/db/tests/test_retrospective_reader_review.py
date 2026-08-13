"""Independent adversarial review of the Lane-R reader shipped at 0496987.

Written against the reviewed architecture, not the implementation report. The
harness is independent of `test_retrospective_reader.py` on purpose: a reviewer
who reuses the implementer's fixtures inherits the implementer's blind spots.

The organising question is not "does it accept the right things" but "what is the
cheapest way to make it accept a wrong thing". Everything is synthetic and
offline.
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
    AdmissionOutcome,
    AdmittedInput,
    LaneRAdmissionError,
    RejectedInput,
    RetrospectiveResearchReader,
)

GAME_MLB = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.GAME, "v1")
GAME_NBA = ProviderNamespace("lg_nba", "balldontlie", EntityType.GAME, "v1")
TEAM_MLB = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.TEAM, "v1")
TEAM_NBA = ProviderNamespace("lg_nba", "balldontlie", EntityType.TEAM, "v1")

TARGET, PRIOR = "900500", "900100"
CUTOFF = "2026-06-10T22:00:00.000000Z"
COMPLETED = "2026-06-09T03:00:00.000000Z"     # +6h => 09:00Z, before the cutoff
CONSERVATIVE = "prior_event_completion_conservative_v1"


class Bench:
    """A disposable v19 database with one reconstruction corpus."""

    def __init__(self, path: Path, *, league: str = "lg_mlb",
                 g1: G1Variant = G1Variant.G1_B_CORE,
                 target_set: str = "targets") -> None:
        initialize_database(path)
        self.path, self.league = path, league
        self._n = 0
        with self.conn() as c, transaction(c):
            self.corpus = self.repo(c).record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id=league, reconstruction_policy_version="review-v1",
                cutoff_policy_id="pregame", cutoff_policy_version="1",
                source_corpus_digest="src", target_set_digest=target_set,
                g1_variant=g1, code_version="review")
        self.cv = self.corpus.corpus_version_id

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with Database(self.path).connection() as c:
            c.execute("PRAGMA foreign_keys = OFF")
            yield c

    @staticmethod
    def repo(c: sqlite3.Connection) -> SqliteRetrospectiveProvenanceRepository:
        return SqliteRetrospectiveProvenanceRepository(c)

    def stat_row(self, c: sqlite3.Connection, row_id: str, game: str,
                 provider: str = "mlb_statsapi") -> str:
        self._n += 1
        now = utc_now_iso()
        c.execute(
            "INSERT INTO team_game_statistics (stat_id, game_ref_id, provider, "
            "provider_game_id, provider_team_id, home_away, observed_at, "
            "ingested_at, raw_response_id, raw_response_hash, content_hash, "
            "created_at) VALUES (?,?,?,?,'147','home',?,?,'raw','h',?,?)",
            (row_id, f"pgr_{game}", provider, game, now, now,
             f"rv_{self._n}", now))
        return row_id

    def certify(self, c: sqlite3.Connection, **kw: Any) -> Any:
        kw.setdefault("corpus_version_id", self.cv)
        kw.setdefault("namespace", GAME_MLB)
        kw.setdefault("provider_game_id", TARGET)
        kw.setdefault("provenance_class", ProvenanceClass.RECONSTRUCTED_RESEARCH)
        kw.setdefault("reconstruction_policy_version", "review-v1")
        kw.setdefault("eligibility", EligibilityVerdict.ELIGIBLE)
        if kw.get("availability_basis") is not None:
            kw.setdefault("availability_source", "review documentation")
        return self.repo(c).certify_input(**kw)

    def attest(self, c: sqlite3.Connection, *, namespace: ProviderNamespace = TEAM_MLB,
               provider_id: str = "147", canonical: str = "tm_mlb_nyy",
               corpus_version_id: Optional[str] = None) -> Any:
        repo = self.repo(c)
        cv = corpus_version_id or self.cv
        corpus = repo.corpus_version(cv)
        assert corpus is not None
        audit = repo.record_identity_audit(
            namespace=namespace, source_corpus_digest=corpus.source_corpus_digest,
            audit_policy_version="review", distinct_ids=1, total_observations=2,
            collision_count=0, verdict=AuditVerdict.ACCEPTED)
        return repo.record_static_crosswalk(
            corpus_version_id=cv, namespace=namespace, provider_id=provider_id,
            canonical_entity_id=canonical,
            identity_audit_id=audit.identity_audit_id,
            provenance_policy_version="g5-team-attestation-v1")

    def reader(self, c: sqlite3.Connection, *, cutoff: str = CUTOFF,
               cv: Optional[str] = None) -> RetrospectiveResearchReader:
        return RetrospectiveResearchReader(
            c, corpus_version_id=cv or self.cv, cutoff=cutoff)

    def event_derived(self, c: sqlite3.Connection, *, family: str = "prior_results",
                      game: str = PRIOR, completed: str = COMPLETED,
                      row_id: str = "tgs_prior") -> Any:
        """The standard happy-path EVENT_DERIVED certification."""

        self.stat_row(c, row_id, game)
        return self.certify(
            c, feature_family=family,
            availability_basis=AvailabilityBasis.EVENT_DERIVED,
            availability_rule_id=CONSERVATIVE,
            source_event_completed_at=completed,
            source_evidence_table="team_game_statistics",
            source_evidence_id=row_id)


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path / "review.db")


# =========================================================================== #
# A. Strict lane separation -- realistic bypasses, not source greps
# =========================================================================== #
def test_no_constructor_or_method_accepts_a_lane_switch() -> None:
    """Every public callable on both readers, checked by signature."""

    import inspect

    from sports_quant.pit.asof import AsOfReader

    banned = ("retrospective", "reconstructed", "lane", "research", "effective",
              "ignore", "bypass", "override", "unsafe", "admit", "corpus")
    for cls in (AsOfReader,):
        for name, member in inspect.getmembers(cls, callable):
            if name.startswith("__"):
                continue
            try:
                params = inspect.signature(member).parameters
            except (TypeError, ValueError):
                continue
            for p in params:
                assert not any(b in p.lower() for b in banned), (
                    f"AsOfReader.{name} takes a lane-shaped parameter {p!r}")


def test_the_lane_r_reader_cannot_be_duck_typed_into_the_strict_reader(
    bench: Bench,
) -> None:
    """A Lane-R reader must not satisfy the strict reader's call surface.

    If it did, a caller holding `reader` could pass either object and get
    silently different leakage rules.
    """

    from sports_quant.pit.asof import AsOfReader

    strict_api = {n for n, _ in
                  __import__("inspect").getmembers(AsOfReader, callable)
                  if not n.startswith("_")}
    lane_r_api = {n for n, _ in
                  __import__("inspect").getmembers(RetrospectiveResearchReader,
                                                   callable)
                  if not n.startswith("_")}
    overlap = strict_api & lane_r_api
    assert not overlap, (
        f"the two readers share callable names {sorted(overlap)}; a caller could "
        "substitute one for the other")


def test_monkeypatching_the_lane_r_module_does_not_reach_the_strict_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No shared mutable state: patching Lane R must not alter Lane L."""

    import sports_quant.pit.asof as strict
    import sports_quant.retrospective.reader as lane_r

    before_latest = strict.latest_as_of
    monkeypatch.setattr(lane_r, "derive_availability_instant",
                        lambda **kw: "1900-01-01T00:00:00.000000Z")
    assert strict.latest_as_of is before_latest
    # And the strict reader has no attribute that just changed.
    assert not hasattr(strict, "derive_availability_instant")


def test_lane_r_registers_nothing_in_the_strict_pit_registry() -> None:
    """No import side effect can add a joinable retrospective surface."""

    from sports_quant.pit.registry import TABLE_REGISTRY, TableClass

    before = {t: e.classification for t, e in TABLE_REGISTRY.items()}
    import sports_quant.retrospective.families  # noqa: F401
    import sports_quant.retrospective.reader  # noqa: F401

    after = {t: e.classification for t, e in TABLE_REGISTRY.items()}
    assert after == before, "importing Lane R mutated the strict PIT registry"
    for lane_r_table in ("reconstructed_input_provenance",
                         "static_crosswalk_provenance",
                         "reconstruction_corpus_versions",
                         "identity_audit_records", "identity_audit_findings"):
        assert TABLE_REGISTRY[lane_r_table].classification is TableClass.UNSUPPORTED


# =========================================================================== #
# B. Family taxonomy -- fail closed on hostile family names
# =========================================================================== #
@pytest.mark.parametrize("hostile", [
    "LINEUPS", "Lineups", " lineups", "lineups ",
    "lineups" + chr(9),            # trailing tab
    "lineups" + chr(10),           # trailing newline
    "line" + chr(0x200b) + "ups",  # zero-width space
    "lineups" + chr(0xa0),         # non-breaking space
    "STATIC_IDENTITY", "Static_Identity", "static_identity ",
    "static-identity", "staticidentity", "",
])
def test_no_case_whitespace_or_unicode_variant_resolves_to_a_family(
    bench: Bench, hostile: str
) -> None:
    """A near-miss family name must never default into an allowed family."""

    from sports_quant.retrospective.families import (
        ForwardOnlyFamilyError,
        UnknownFeatureFamilyError,
    )

    with bench.conn() as c:
        reader = bench.reader(c)
        with pytest.raises((UnknownFeatureFamilyError, ForwardOnlyFamilyError)):
            reader.admit_feature(namespace=GAME_MLB, provider_game_id=TARGET,
                                 feature_family=hostile)


def test_every_reviewed_family_class_maps_to_exactly_its_own_basis() -> None:
    """A family may not be certified under another class's availability story."""

    from sports_quant.retrospective.families import FEATURE_FAMILIES, FamilyClass

    expected = {
        FamilyClass.STATIC_IDENTITY: {AvailabilityBasis.STATIC_IDENTITY},
        FamilyClass.EVENT_DERIVED: {AvailabilityBasis.EVENT_DERIVED},
        FamilyClass.VERSIONED_SNAPSHOT: {AvailabilityBasis.VERSIONED_SNAPSHOT},
        FamilyClass.LABEL_ONLY: set(),
        FamilyClass.FORWARD_ONLY: set(),
    }
    for family in FEATURE_FAMILIES.values():
        assert set(family.admissible_bases) == expected[family.classification], (
            family.name)
    # Nothing outside the reviewed vocabulary sneaked in.
    assert set(FEATURE_FAMILIES) == {
        "static_identity", "target_schedule_anchor", "prior_results",
        "team_rolling_stats", "rest_schedule_density", "sportsbook_moneyline",
        "kalshi_market", "final_result", "pitcher_rolling_stats",
        "batter_rolling_stats", "bullpen_prior_usage", "weather_forecast",
        "probable_pitchers", "player_rolling_stats", "advanced_rolling_stats",
        "plays_derived_stats", "lineups", "injuries", "rosters",
    }


def test_a_forward_only_family_cannot_be_reached_through_the_batch_api(
    bench: Bench,
) -> None:
    """The batch path must not become the soft way in."""

    from sports_quant.retrospective.families import ForwardOnlyFamilyError

    with bench.conn() as c:
        with pytest.raises(ForwardOnlyFamilyError):
            bench.reader(c).admit_features(
                namespace=GAME_MLB, provider_game_id=TARGET,
                feature_families=("static_identity", "lineups"))


# =========================================================================== #
# C. Corpus binding -- exact-lookup substitutions
# =========================================================================== #
@pytest.mark.parametrize("swap", ["corpus", "namespace", "game", "family",
                                  "generation"])
def test_a_certification_for_anything_else_does_not_authorize_this_read(
    bench: Bench, swap: str
) -> None:
    """Exact lookup means exact. Every axis, one at a time."""

    ns, game, family = GAME_MLB, TARGET, "prior_results"
    with bench.conn() as c:
        with transaction(c):
            other_cv = bench.cv
            if swap == "corpus":
                other = bench.repo(c).record_corpus_version(
                    provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                    league_id="lg_mlb", reconstruction_policy_version="review-v1",
                    cutoff_policy_id="pregame", cutoff_policy_version="1",
                    source_corpus_digest="src", target_set_digest="OTHER",
                    g1_variant=G1Variant.G1_B_CORE, code_version="review")
                other_cv = other.corpus_version_id
            elif swap == "namespace":
                ns = ProviderNamespace("lg_mlb", "other_provider",
                                       EntityType.GAME, "v1")
            elif swap == "generation":
                ns = ProviderNamespace("lg_mlb", "mlb_statsapi",
                                       EntityType.GAME, "v2")
            elif swap == "game":
                game = "999999"
            elif swap == "family":
                family = "team_rolling_stats"

            bench.stat_row(c, "tgs_prior", PRIOR)
            bench.certify(
                c, corpus_version_id=other_cv, namespace=ns,
                provider_game_id=game, feature_family=family,
                availability_basis=AvailabilityBasis.EVENT_DERIVED,
                availability_rule_id=CONSERVATIVE,
                source_event_completed_at=COMPLETED,
                source_evidence_table="team_game_statistics",
                source_evidence_id="tgs_prior")

        decision = bench.reader(c).admit_feature(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="prior_results")

    assert isinstance(decision, RejectedInput), (swap, decision)
    assert decision.outcome is AdmissionOutcome.NO_CERTIFICATION


def test_a_certification_whose_league_disagrees_with_its_corpus_is_refused(
    bench: Bench,
) -> None:
    """The DB is the first layer here, and it refuses at write time."""

    with bench.conn() as c:
        with pytest.raises(sqlite3.IntegrityError, match="corpus version league"):
            with transaction(c):
                bench.stat_row(c, "tgs_x", PRIOR)
                bench.certify(
                    c, namespace=ProviderNamespace("lg_nba", "mlb_statsapi",
                                                   EntityType.GAME, "v1"),
                    feature_family="prior_results",
                    availability_basis=AvailabilityBasis.EVENT_DERIVED,
                    availability_rule_id=CONSERVATIVE,
                    source_event_completed_at=COMPLETED,
                    source_evidence_table="team_game_statistics",
                    source_evidence_id="tgs_x")


def test_direct_sql_cannot_smuggle_an_excluded_row_past_admission(
    bench: Bench,
) -> None:
    """Bypass the repository entirely and write the row by hand."""

    with bench.conn() as c:
        with transaction(c):
            bench.event_derived(c)
            c.execute("DROP TRIGGER trg_rip_no_update")
            c.execute("UPDATE reconstructed_input_provenance "
                      "SET availability_source = 'PLANTED BY DIRECT SQL'")
        # eligibility itself is CHECK-constrained, so tamper with what is not.
        decision = bench.reader(c).admit_feature(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="prior_results")
    # An eligible row with an exclusion code is contradictory but not a leak;
    # what matters is that admission still reports its real basis.
    assert isinstance(decision, AdmittedInput)
    assert decision.availability_basis is AvailabilityBasis.EVENT_DERIVED


def test_a_blocking_finding_cannot_coexist_with_an_accepted_audit(
    bench: Bench,
) -> None:
    """Why the reader need not consult findings: the DB forbids the situation.

    f019's `trg_idf_accepted_audit_no_contradiction` enforces this at the
    database level, so it holds even against direct SQL. Proved here rather than
    assumed, because the reader's silence about findings only makes sense if this
    is true.
    """

    from sports_quant.retrospective.provenance import (
        ExclusionScope,
        FindingClassification,
        FindingSeverity,
    )

    with bench.conn() as c:
        with transaction(c):
            crosswalk = bench.attest(c)
        audit_id = crosswalk.identity_audit_id

        # Repository layer refuses.
        with pytest.raises(Exception) as repo_layer:
            with transaction(c):
                bench.repo(c).record_finding(
                    identity_audit_id=audit_id, namespace=TEAM_MLB,
                    severity=FindingSeverity.BLOCKING, finding_code="X",
                    classification=FindingClassification.IDENTITY_COLLISION,
                    exclusion_scope=ExclusionScope.ENTITY, provider_id="147")
        assert "contradict" in str(repo_layer.value).lower()

        # Database layer refuses too, with the repository bypassed.
        now = utc_now_iso()
        with pytest.raises(sqlite3.IntegrityError) as db_layer:
            with transaction(c):
                c.execute(
                    "INSERT INTO identity_audit_findings (finding_id, "
                    "identity_audit_id, league_id, provider, "
                    "namespace_generation, entity_type, provider_id, severity, "
                    "finding_code, classification, exclusion_scope, detail_json, "
                    "detail_digest, created_at) VALUES ('idf_planted',?,'lg_mlb',"
                    "'mlb_statsapi','v1','team','147','blocking','X',"
                    "'identity_collision','entity','{}','d',?)",
                    (audit_id, now))
        assert "accepted" in str(db_layer.value).lower()


# =========================================================================== #
# D. Availability semantics -- boundaries and hostile timestamps
# =========================================================================== #
def test_the_completion_instant_is_not_bound_to_the_cited_evidence_row(
    bench: Bench,
) -> None:
    """Can a certification cite LATE evidence while claiming an EARLY completion?

    `source_event_completed_at` is a caller-supplied scalar. Nothing in f018 ties
    it to the row named by `source_evidence_table`/`source_evidence_id`. If the
    reader trusts the scalar alone, a late game's evidence can be admitted under
    an early game's completion time.

    This documents the ACTUAL behaviour. Binding the two is a builder (F1-R)
    obligation, not something the reader can verify -- the evidence tables carry
    `observed_at` (collection time), not an event-completion instant, so there is
    nothing for the reader to cross-check against.
    """

    with bench.conn() as c:
        with transaction(c):
            # Evidence row belongs to a game that is NOT the target...
            bench.stat_row(c, "tgs_late", "900999")
            # ...and we claim a completion instant with no relation to it.
            bench.certify(
                c, feature_family="prior_results",
                availability_basis=AvailabilityBasis.EVENT_DERIVED,
                availability_rule_id=CONSERVATIVE,
                source_event_completed_at=COMPLETED,
                source_evidence_table="team_game_statistics",
                source_evidence_id="tgs_late")
        decision = bench.reader(c).admit_feature(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="prior_results")

    assert isinstance(decision, AdmittedInput)
    # The reader reports exactly what it relied on, so the unverifiable link is
    # at least visible downstream rather than hidden.
    assert decision.certification.source_evidence_id == "tgs_late"
    assert decision.certification.source_event_completed_at == COMPLETED
    assert decision.effective_at == "2026-06-09T09:00:00.000000Z"


@pytest.mark.parametrize("snapshot_at,admitted", [
    ("2026-06-10T21:59:59.999999Z", True),    # 1us before
    ("2026-06-10T22:00:00.000000Z", True),    # exactly at
    ("2026-06-10T22:00:00.000001Z", False),   # 1us after
])
def test_versioned_snapshot_boundary_is_exact(
    bench: Bench, snapshot_at: str, admitted: bool
) -> None:
    with bench.conn() as c:
        with transaction(c):
            bench.stat_row(c, "tgs_s", PRIOR)
            bench.certify(
                c, feature_family="sportsbook_moneyline",
                availability_basis=AvailabilityBasis.VERSIONED_SNAPSHOT,
                source_snapshot_at=snapshot_at,
                source_evidence_table="team_game_statistics",
                source_evidence_id="tgs_s")
        decision = bench.reader(c).admit_feature(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="sportsbook_moneyline")
    assert isinstance(decision, AdmittedInput) is admitted, decision


@pytest.mark.parametrize("bad_cutoff", [
    "2026-06-10T22:00:00Z",            # no microseconds
    "2026-06-10T22:00:00+00:00",       # offset form
    "2026-06-10 22:00:00.000000Z",     # space separator
    "2026-06-10T22:00:00.000000",      # naive, no Z
    "not-a-timestamp", "", "2026-13-45T99:99:99.000000Z",
])
def test_a_malformed_cutoff_is_refused_at_construction(
    bench: Bench, bad_cutoff: str
) -> None:
    """A cutoff that cannot be parsed must never become a lenient comparison."""

    with bench.conn() as c:
        with pytest.raises((ValueError, LaneRAdmissionError)):
            bench.reader(c, cutoff=bad_cutoff)


def test_a_malformed_persisted_snapshot_instant_fails_closed(
    bench: Bench,
) -> None:
    """Tampered availability instants must not compare leniently."""

    with bench.conn() as c:
        with transaction(c):
            bench.stat_row(c, "tgs_s", PRIOR)
            bench.certify(
                c, feature_family="sportsbook_moneyline",
                availability_basis=AvailabilityBasis.VERSIONED_SNAPSHOT,
                source_snapshot_at="2026-06-01T00:00:00.000000Z",
                source_evidence_table="team_game_statistics",
                source_evidence_id="tgs_s")
            c.execute("DROP TRIGGER trg_rip_no_update")
            c.execute("UPDATE reconstructed_input_provenance "
                      "SET source_snapshot_at = '1970-01-01T00:00:00Z'")
        with pytest.raises(ValueError):
            bench.reader(c).admit_feature(
                namespace=GAME_MLB, provider_game_id=TARGET,
                feature_family="sportsbook_moneyline")


# =========================================================================== #
# E. Rule registry / digest binding
# =========================================================================== #
def test_the_rule_digest_is_recomputed_independently_of_the_helper() -> None:
    """Reconstruct both rule digests from first principles."""

    import hashlib
    import json

    from sports_quant.retrospective.rules import AVAILABILITY_RULES

    for rule_id, rule in AVAILABILITY_RULES.items():
        payload = {
            "rule_id": rule.rule_id,
            "version": rule.version,
            "evaluation_form": rule.evaluation_form,
            "lag_seconds": rule.lag_seconds,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
        expected = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        assert rule.digest == expected, rule_id
    # The two reviewed rules, and only those.
    assert set(AVAILABILITY_RULES) == {
        "prior_event_completion_conservative_v1",
        "prior_event_completion_immediate_v1"}
    assert AVAILABILITY_RULES[
        "prior_event_completion_conservative_v1"].lag_seconds == 6 * 3600
    assert AVAILABILITY_RULES[
        "prior_event_completion_immediate_v1"].lag_seconds == 0


def test_a_tampered_persisted_rule_digest_fails_closed(bench: Bench) -> None:
    """Direct SQL changes the stored digest; derivation must refuse."""

    from sports_quant.retrospective.provenance import RetrospectiveProvenanceError

    with bench.conn() as c:
        with transaction(c):
            bench.event_derived(c)
            c.execute("DROP TRIGGER trg_rip_no_update")
            c.execute("UPDATE reconstructed_input_provenance "
                      "SET availability_rule_digest = ?", ("0" * 64,))
        with pytest.raises(RetrospectiveProvenanceError, match="has changed"):
            bench.reader(c).admit_feature(
                namespace=GAME_MLB, provider_game_id=TARGET,
                feature_family="prior_results")


def test_an_unknown_persisted_rule_id_fails_closed(bench: Bench) -> None:
    from sports_quant.retrospective.rules import UnknownAvailabilityRuleError

    with bench.conn() as c:
        with transaction(c):
            bench.event_derived(c)
            c.execute("DROP TRIGGER trg_rip_no_update")
            c.execute("UPDATE reconstructed_input_provenance "
                      "SET availability_rule_id = 'invented_rule_v9'")
        with pytest.raises(UnknownAvailabilityRuleError):
            bench.reader(c).admit_feature(
                namespace=GAME_MLB, provider_game_id=TARGET,
                feature_family="prior_results")


def test_the_certification_cannot_cite_one_rule_and_derive_with_another(
    bench: Bench,
) -> None:
    """Swap the rule id but keep the old digest: the pair must not validate."""

    from sports_quant.retrospective.provenance import RetrospectiveProvenanceError
    from sports_quant.retrospective.rules import AVAILABILITY_RULES

    with bench.conn() as c:
        with transaction(c):
            bench.event_derived(c)
            c.execute("DROP TRIGGER trg_rip_no_update")
            # Cite the ZERO-LAG rule while keeping the conservative digest.
            c.execute("UPDATE reconstructed_input_provenance "
                      "SET availability_rule_id = ?",
                      ("prior_event_completion_immediate_v1",))
        with pytest.raises(RetrospectiveProvenanceError, match="has changed"):
            bench.reader(c).admit_feature(
                namespace=GAME_MLB, provider_game_id=TARGET,
                feature_family="prior_results")
    assert (AVAILABILITY_RULES["prior_event_completion_immediate_v1"].digest
            != AVAILABILITY_RULES["prior_event_completion_conservative_v1"].digest)


# =========================================================================== #
# F/G. Correction sensitivity and label isolation
# =========================================================================== #
def test_correction_sensitivity_cannot_be_suppressed_by_a_caller(
    tmp_path: Path,
) -> None:
    """It is derived from the corpus, not accepted as an argument."""

    import inspect

    bench = Bench(tmp_path / "ext.db", g1=G1Variant.G1_A_EXTENDED)
    params = inspect.signature(
        RetrospectiveResearchReader.admit_feature).parameters
    assert not any("correction" in p or "sensitiv" in p for p in params)

    with bench.conn() as c:
        with transaction(c):
            bench.event_derived(c)
        decision = bench.reader(c).admit_feature(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="prior_results")
    assert isinstance(decision, AdmittedInput)
    assert decision.correction_sensitive is True
    assert decision.as_json()["correction_sensitive"] is True


def test_an_unknown_g1_variant_fails_closed(bench: Bench) -> None:
    """Two layers, both proved.

    f018's `rcv_g1_variant` CHECK refuses an unknown variant at write time, even
    against direct SQL, so the end-to-end case is unconstructible. The reader's
    own parser is the second layer and is exercised directly.
    """

    from sports_quant.retrospective.reader import _parse

    with bench.conn() as c:
        with transaction(c):
            bench.event_derived(c)
        with pytest.raises(sqlite3.IntegrityError, match="g1_variant"):
            with transaction(c):
                c.execute("DROP TRIGGER trg_rcv_no_update")
                c.execute("UPDATE reconstruction_corpus_versions "
                          "SET g1_variant = 'g1_z_unknown'")

    with pytest.raises(LaneRAdmissionError, match="does not recognize"):
        _parse(G1Variant, "g1_z_unknown", "g1_variant")
    assert _parse(G1Variant, "g1_a_extended", "g1_variant") is         G1Variant.G1_A_EXTENDED


def test_a_label_certification_copied_onto_a_feature_family_is_refused(
    bench: Bench,
) -> None:
    """The most realistic label leak: relabel the family, keep the lane."""

    with bench.conn() as c:
        with transaction(c):
            bench.stat_row(c, "tgs_l", TARGET)
            bench.certify(
                c, feature_family="prior_results",
                provenance_class=ProvenanceClass.LABEL_ONLY_RETROSPECTIVE,
                source_evidence_table="team_game_statistics",
                source_evidence_id="tgs_l")
        decision = bench.reader(c).admit_feature(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="prior_results")

    assert isinstance(decision, RejectedInput), decision
    assert decision.outcome is AdmissionOutcome.WRONG_LANE


def test_label_admission_does_not_assert_pregame_knowability(
    bench: Bench,
) -> None:
    """A label must carry no availability story at all."""

    with bench.conn() as c:
        with transaction(c):
            bench.stat_row(c, "tgs_lab", TARGET)
            bench.certify(
                c, feature_family="final_result",
                provenance_class=ProvenanceClass.LABEL_ONLY_RETROSPECTIVE,
                source_evidence_table="team_game_statistics",
                source_evidence_id="tgs_lab")
        decision = bench.reader(c).admit_label(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="final_result")

    assert isinstance(decision, AdmittedInput)
    assert decision.effective_at is None
    assert decision.availability_rule_id is None
    assert decision.certification.availability_basis is None
    assert decision.certification.crosswalk_id is None


# =========================================================================== #
# H. Static identity -- MLB and NBA, many-to-one without injectivity
# =========================================================================== #
@pytest.mark.parametrize("league,ns,pid,canonical", [
    ("lg_mlb", TEAM_MLB, "147", "tm_mlb_nyy"),
    ("lg_nba", TEAM_NBA, "1", "tm_nba_atl"),
])
def test_static_identity_is_symmetric_across_leagues(
    tmp_path: Path, league: str, ns: ProviderNamespace, pid: str, canonical: str
) -> None:
    bench = Bench(tmp_path / f"{league}.db", league=league)
    with bench.conn() as c:
        with transaction(c):
            bench.attest(c, namespace=ns, provider_id=pid, canonical=canonical)
        identity = bench.reader(c).static_identity(namespace=ns, provider_id=pid)
    assert identity.canonical_entity_id == canonical
    assert identity.corpus_version_id == bench.cv


def test_two_provider_ids_may_denote_one_franchise(bench: Bench) -> None:
    """Many-to-one is permitted; canonical-target injectivity is NOT required."""

    with bench.conn() as c:
        with transaction(c):
            bench.attest(c, provider_id="147", canonical="tm_mlb_nyy")
            bench.attest(c, provider_id="9147", canonical="tm_mlb_nyy")
        reader = bench.reader(c)
        first = reader.static_identity(namespace=TEAM_MLB, provider_id="147")
        second = reader.static_identity(namespace=TEAM_MLB, provider_id="9147")
    assert first.canonical_entity_id == second.canonical_entity_id == "tm_mlb_nyy"
    assert first.crosswalk_id != second.crosswalk_id


def test_a_provider_reference_link_is_not_identity_proof(bench: Bench) -> None:
    """An accepted live matcher link must not substitute for a crosswalk."""

    now = utc_now_iso()
    with bench.conn() as c:
        with transaction(c):
            c.execute(
                "INSERT INTO provider_team_references (reference_id, provider, "
                "provider_team_id, team_id, first_raw_response_id, "
                "current_raw_response_id, current_raw_response_hash, "
                "first_observed_at, last_observed_at, created_at, updated_at) "
                "VALUES ('ptr_x','mlb_statsapi','147','tm_mlb_nyy','r','r','h',"
                "?,?,?,?)", (now, now, now, now))
        with pytest.raises(LaneRAdmissionError, match="no static crosswalk"):
            bench.reader(c).static_identity(namespace=TEAM_MLB, provider_id="147")


# =========================================================================== #
# CONFIRMED DEFECTS (reproduced on 0496987 before repair)
# =========================================================================== #
def test_a_tampered_crosswalk_target_is_not_admitted(bench: Bench) -> None:
    """DEFECT R2 (high): the reader admitted a wrong canonical target.

    As shipped, the STATIC_IDENTITY path checked only that the crosswalk existed
    and belonged to this corpus. It never recomputed the crosswalk's own
    semantic digest, so flipping `canonical_entity_id` with direct SQL produced
    an admitted identity pointing at the wrong franchise -- and
    `static_identity()` returned that wrong canonical id to the caller.

    The out-of-band verifier catches this, but a reader whose entire job is
    admission must not depend on someone remembering to run it later.
    """

    with bench.conn() as c:
        with transaction(c):
            crosswalk = bench.attest(c, provider_id="147", canonical="tm_mlb_nyy")
            bench.certify(
                c, feature_family="static_identity",
                availability_basis=AvailabilityBasis.STATIC_IDENTITY,
                availability_source="TEAM-A map",
                crosswalk_id=crosswalk.crosswalk_id)
        with transaction(c):
            c.execute("DROP TRIGGER trg_xwk_no_update")
            c.execute("UPDATE static_crosswalk_provenance "
                      "SET canonical_entity_id = 'tm_mlb_hou' "
                      "WHERE crosswalk_id = ?", (crosswalk.crosswalk_id,))

        decision = bench.reader(c).admit_feature(
            namespace=GAME_MLB, provider_game_id=TARGET,
            feature_family="static_identity")
        assert isinstance(decision, RejectedInput), (
            "a crosswalk whose stored contents no longer match its own digest "
            "was admitted as static identity")
        assert decision.outcome is AdmissionOutcome.CROSSWALK_DIGEST_MISMATCH

        # And the direct resolver must refuse too, not hand back a wrong id.
        with pytest.raises(LaneRAdmissionError, match="digest"):
            bench.reader(c).static_identity(namespace=TEAM_MLB, provider_id="147")


def test_the_admission_api_refuses_a_non_game_namespace(bench: Bench) -> None:
    """DEFECT R1 (moderate): the namespace's entity_type was silently ignored.

    `reconstructed_input_provenance` has no `entity_type` column -- correctly, a
    certification is about a feature family for a target GAME. But the admission
    API accepted a full `ProviderNamespace` and ignored that component, so a
    caller passing a TEAM namespace silently received game-scoped certifications.
    An argument that is read by the type checker and ignored at runtime is an
    invitation to misuse.
    """

    with bench.conn() as c:
        with transaction(c):
            bench.event_derived(c)
        reader = bench.reader(c)
        for bad in (TEAM_MLB,
                    ProviderNamespace("lg_mlb", "mlb_statsapi",
                                      EntityType.PLAYER, "v1")):
            with pytest.raises(LaneRAdmissionError, match="game namespace"):
                reader.admit_feature(namespace=bad, provider_game_id=TARGET,
                                     feature_family="prior_results")
            with pytest.raises(LaneRAdmissionError, match="game namespace"):
                reader.admit_label(namespace=bad, provider_game_id=TARGET,
                                   feature_family="final_result")
        # The GAME namespace still works.
        assert isinstance(
            reader.admit_feature(namespace=GAME_MLB, provider_game_id=TARGET,
                                 feature_family="prior_results"), AdmittedInput)


def test_static_identity_still_accepts_team_and_player_namespaces(
    bench: Bench,
) -> None:
    """The entity-type gate belongs on admission only.

    `static_identity()` resolves entities, so TEAM/PLAYER namespaces are exactly
    what it should take -- and there the entity type IS used, because the
    crosswalk lookup filters on it.
    """

    with bench.conn() as c:
        with transaction(c):
            bench.attest(c, namespace=TEAM_MLB, provider_id="147",
                         canonical="tm_mlb_nyy")
        identity = bench.reader(c).static_identity(
            namespace=TEAM_MLB, provider_id="147")
    assert identity.entity_type is EntityType.TEAM
    assert identity.canonical_entity_id == "tm_mlb_nyy"
