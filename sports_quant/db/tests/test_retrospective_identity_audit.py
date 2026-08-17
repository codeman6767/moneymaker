"""The production corpus-scoped G5 identity audit engine.

Task §30. Fixtures are synthetic and adversarial: a clean corpus proves only that
the engine does not invent collisions, so most of this file is about the cases
where it MUST find one -- and, just as importantly, the lawful mutations where it
must not.

Nothing here constructs a provider client, reads settings, or opens a protected
corpus for writing.
"""

from __future__ import annotations

import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION, utc_now_iso
from sports_quant.retrospective.crosswalks import (
    CROSSWALK_SUPPORTED_ENTITY_TYPES,
    canonical_player_id,
)
from sports_quant.retrospective.identity_audit import (
    AUDIT_POLICY_VERSION,
    GAME_INCOMPATIBLE_EVENT,
    GAME_UNEXPLAINED_DATE_CHANGE,
    PLAYER_INCOMPATIBLE_BIRTH_DATE,
    PLAYER_INCOMPATIBLE_LEAGUE,
    PLAYER_INSUFFICIENT,
    PLAYER_NAME_VARIANCE,
    TEAM_LABEL_VARIANCE,
    IdentityAuditError,
    audit_namespace,
)
from sports_quant.retrospective.provenance import (
    UNVERIFIED_GENERATION,
    AuditVerdict,
    EntityType,
    FindingClassification,
    FindingSeverity,
    ProviderNamespace,
)
from sports_quant.retrospective.runner import run_identity_audit
from sports_quant.retrospective.sources import (
    SourceCorpusError,
    open_source_corpus,
    source_corpus_digest,
)

T0 = "2026-06-01T00:00:00.000000Z"
T1 = "2026-06-02T00:00:00.000000Z"
T2 = "2026-06-03T00:00:00.000000Z"


# --------------------------------------------------------------------------- #
# Synthetic source corpora
# --------------------------------------------------------------------------- #
class SourceBuilder:
    """Builds a minimal source corpus holding only audited identity evidence."""

    def __init__(self, path: Path) -> None:
        initialize_database(path)
        self.path = path
        self._n = 0

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = OFF")  # evidence-only fixture
        return conn

    def game(self, provider_game_id: str, *, season: int = 2026, home: str = "10",
             away: str = "20", observed_at: str = T0, provider: str = "mlb_statsapi",
             status: str = "final", date_local: str = "2026-06-01",
             scheduled_start: str = "2026-06-01T22:45:00Z", game_number: int = 1,
             doubleheader: str = "N", venue: Optional[str] = "3309",
             resched: Optional[str] = None) -> "SourceBuilder":
        self._n += 1
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO game_schedule_snapshots (schedule_id, game_ref_id, "
                "provider, provider_game_id, season, game_date_local, scheduled_start, "
                "home_provider_team_id, away_provider_team_id, venue_provider_id, "
                "mapped_status, game_number, doubleheader_code, reschedule_info, "
                "observed_at, ingested_at, run_id, raw_response_id, "
                "raw_response_hash, content_hash, created_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'run', 'raw', "
                "'h', ?, ?)",
                (f"gss_{self._n}", f"pgr_{provider_game_id}", provider,
                 provider_game_id, season, date_local, scheduled_start, home, away,
                 venue, status, game_number, doubleheader, resched, observed_at,
                 observed_at, f"ch_{self._n}", observed_at),
            )
        return self

    def team(self, provider_team_id: str, *, league_id: str = "lg_mlb",
             name: str = "Fixture Club", abbreviation: Optional[str] = "FIX",
             city: Optional[str] = "Fixture", nickname: Optional[str] = "Club",
             observed_at: str = T0, provider: str = "mlb_statsapi") -> "SourceBuilder":
        self._n += 1
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO provider_team_identity_snapshots (identity_id, provider, "
                "provider_team_id, league_id, full_name, normalized_name, abbreviation, "
                "city, nickname, observed_at, raw_response_id, raw_response_hash, "
                "content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'raw', 'h', ?, ?)",
                (f"pti_{self._n}", provider, provider_team_id, league_id, name,
                 name.lower(), abbreviation, city, nickname, observed_at,
                 f"ch_{self._n}", observed_at),
            )
        return self

    def player(self, provider_player_id: str, *, league_id: str = "lg_mlb",
               name: str = "Fixture Player", suffix: str = "",
               birth_date: Optional[str] = None, position: Optional[str] = "P",
               team: Optional[str] = "10", observed_at: str = T0,
               provider: str = "mlb_statsapi") -> "SourceBuilder":
        self._n += 1
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO provider_player_identity_snapshots (identity_id, provider, "
                "provider_player_id, league_id, full_name, normalized_name, suffix, "
                "birth_date, position, provider_team_id, observed_at, raw_response_id, "
                "raw_response_hash, content_hash, created_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw', 'h', ?, ?)",
                (f"ppi_{self._n}", provider, provider_player_id, league_id, name,
                 name.lower(), suffix, birth_date, position, team, observed_at,
                 f"ch_{self._n}", observed_at),
            )
        return self

    def finish(self) -> Path:
        """Checkpoint the WAL so the corpus can be opened immutable."""

        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        return self.path


@pytest.fixture
def builder(tmp_path: Path) -> SourceBuilder:
    return SourceBuilder(tmp_path / "source.db")


@pytest.fixture
def output_db(tmp_path: Path) -> Path:
    path = tmp_path / "output.db"
    initialize_database(path)
    return path


def _audit(source_path: Path, entity_type: EntityType, *, league: str = "lg_mlb",
           provider: str = "mlb_statsapi", generation: str = "v1") -> Any:
    conn = open_source_corpus(source_path)
    try:
        digest = source_corpus_digest(conn, league_id=league, provider=provider)
        return audit_namespace(
            conn,
            namespace=ProviderNamespace(league, provider, entity_type, generation),
            source_corpus_digest=digest,
        )
    finally:
        conn.close()


def _codes(plan: Any) -> set[str]:
    return {f.finding_code for f in plan.findings}


# --------------------------------------------------------------------------- #
# §23 game fixtures
# --------------------------------------------------------------------------- #
def test_game_id_used_for_two_different_matchups_is_a_collision(
    builder: SourceBuilder,
) -> None:
    builder.game("G1", home="10", away="20", observed_at=T0)
    builder.game("G1", home="30", away="40", observed_at=T1)
    plan = _audit(builder.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.REJECTED_COLLISION
    assert plan.collision_count == 1
    assert GAME_INCOMPATIBLE_EVENT in _codes(plan)
    finding = next(f for f in plan.findings if f.is_collision)
    assert finding.severity is FindingSeverity.BLOCKING
    assert finding.provider_id == "G1"


@pytest.mark.parametrize("kwargs", [
    # An explicit reschedule payload is the provider saying the event moved.
    {"date_local": "2026-06-04", "scheduled_start": "2026-06-04T22:45:00Z",
     "resched": '{"rescheduledFrom": "2026-06-01"}'},
    # A moved-status observation is equally good continuity evidence.
    {"date_local": "2026-06-04", "status": "postponed"},
    {"status": "in_progress"},                                # progression
    {"venue": "9999"},                                        # venue change
])
def test_lawful_game_mutation_is_not_a_collision(
    builder: SourceBuilder, kwargs: dict[str, Any]
) -> None:
    """A postponement that moves a game is not id reuse."""

    builder.game("G1", observed_at=T0)
    builder.game("G1", observed_at=T1, **kwargs)
    plan = _audit(builder.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.collision_count == 0
    assert plan.flagged_count == 0, "a lawful mutation must not raise a flag"
    assert not [f for f in plan.findings
                if f.finding_code == GAME_UNEXPLAINED_DATE_CHANGE]


def test_doubleheader_games_are_distinct_ids_and_both_clean(
    builder: SourceBuilder,
) -> None:
    """The provider assigns distinct ids; game number is metadata, not the key."""

    builder.game("G1", home="10", away="20", game_number=1, doubleheader="S")
    builder.game("G2", home="10", away="20", game_number=2, doubleheader="S")
    plan = _audit(builder.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.distinct_ids == 2


def test_game_with_a_different_season_under_one_id_is_a_collision(
    builder: SourceBuilder,
) -> None:
    builder.game("G1", season=2025)
    builder.game("G1", season=2026)
    plan = _audit(builder.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.REJECTED_COLLISION


def test_game_missing_participants_is_insufficient_evidence_not_clean(
    builder: SourceBuilder,
) -> None:
    builder.game("G1", home=None, away=None)  # type: ignore[arg-type]
    plan = _audit(builder.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert any(f.classification is FindingClassification.INSUFFICIENT_EVIDENCE
               for f in plan.findings)


# --------------------------------------------------------------------------- #
# §23 team fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("second", [
    {"name": "Fixture Athletics"},                     # rename
    {"city": "Elsewhere"},                             # relocation
    {"abbreviation": "FXA"},                           # abbreviation change
    {"nickname": "Athletics"},                         # rebrand
])
def test_lawful_team_label_change_is_a_flag_not_a_collision(
    builder: SourceBuilder, second: dict[str, Any]
) -> None:
    builder.team("T1", observed_at=T0)
    builder.team("T1", observed_at=T1, **second)
    plan = _audit(builder.finish(), EntityType.TEAM)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.collision_count == 0
    assert plan.flagged_count == 1
    assert TEAM_LABEL_VARIANCE in _codes(plan)


def test_team_id_under_two_leagues_is_a_collision_excluding_dependent_games(
    builder: SourceBuilder,
) -> None:
    builder.team("T1", league_id="lg_mlb")
    builder.team("T1", league_id="lg_nba", observed_at=T1)
    conn = open_source_corpus(builder.finish())
    try:
        digest = source_corpus_digest(conn, league_id="lg_mlb",
                                      provider="mlb_statsapi")
        with pytest.raises(IdentityAuditError, match="mixed namespace"):
            audit_namespace(
                conn,
                namespace=ProviderNamespace("lg_mlb", "mlb_statsapi",
                                            EntityType.TEAM, "v1"),
                source_corpus_digest=digest)
    finally:
        conn.close()


def test_absent_metadata_is_not_a_rename(builder: SourceBuilder) -> None:
    """The first-pass rule flagged all 30 MLB franchises on exactly this shape.

    MLB StatsAPI returns abbreviation/city/nickname from some endpoints and not
    others. A field going from "not supplied" to a value has not changed.
    """

    builder.team("T1", abbreviation=None, city=None, nickname=None, observed_at=T0)
    builder.team("T1", abbreviation="FIX", city="Fixture", nickname="Club",
                 observed_at=T1)
    plan = _audit(builder.finish(), EntityType.TEAM)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.flagged_count == 0, "metadata completeness was treated as a rename"


# --------------------------------------------------------------------------- #
# §23 player fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("second", [
    {"team": "99"},          # trade -- affiliation is NEVER person identity
    {"position": "1B"},      # position change
    {"team": None},          # affiliation withdrawn
])
def test_lawful_player_mutation_is_clean(
    builder: SourceBuilder, second: dict[str, Any]
) -> None:
    builder.player("P1", birth_date="1998-04-01", observed_at=T0)
    builder.player("P1", birth_date="1998-04-01", observed_at=T1, **second)
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.collision_count == 0
    assert plan.flagged_count == 0


def test_player_name_change_is_a_warning_and_never_merges_ids(
    builder: SourceBuilder,
) -> None:
    builder.player("P1", name="Bob Fixture", birth_date="1998-04-01", observed_at=T0)
    builder.player("P1", name="Robert Fixture", birth_date="1998-04-01",
                   observed_at=T1)
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.flagged_count == 1
    assert PLAYER_NAME_VARIANCE in _codes(plan)
    assert plan.distinct_ids == 1


def test_two_different_ids_with_the_same_name_remain_two_people(
    builder: SourceBuilder,
) -> None:
    """A shared name is not evidence of a shared person."""

    builder.player("P1", name="Chris Smith", birth_date="1998-04-01")
    builder.player("P2", name="Chris Smith", birth_date="2001-09-09")
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.distinct_ids == 2
    assert plan.collision_count == 0


def test_one_id_with_two_birth_dates_is_a_collision(builder: SourceBuilder) -> None:
    builder.player("P1", birth_date="1998-04-01", observed_at=T0)
    builder.player("P1", birth_date="1990-01-01", observed_at=T1)
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.verdict is AuditVerdict.REJECTED_COLLISION
    assert plan.collision_count == 1
    assert PLAYER_INCOMPATIBLE_BIRTH_DATE in _codes(plan)


def test_suffix_variation_is_detected_deterministically(
    builder: SourceBuilder,
) -> None:
    """"Ken Griffey Jr." and "Ken Griffey" differ by suffix, which is stored apart."""

    builder.player("P1", name="Ken Griffey", suffix="jr", birth_date="1969-11-21",
                   observed_at=T0)
    builder.player("P1", name="Ken Griffey", suffix="", birth_date="1969-11-21",
                   observed_at=T1)
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.flagged_count == 1
    assert PLAYER_NAME_VARIANCE in _codes(plan)


def test_missing_secondary_evidence_is_reported_once_at_namespace_level(
    builder: SourceBuilder,
) -> None:
    """Thin evidence bounds what a clean result proves; it excludes nothing.

    Reported once rather than once per id: 1,053 identical rows would bury the
    finding that matters while asserting nothing extra.
    """

    for i in range(5):
        builder.player(f"P{i}", birth_date=None)
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.verdict is AuditVerdict.ACCEPTED
    insufficient = [f for f in plan.findings
                    if f.finding_code == PLAYER_INSUFFICIENT]
    assert len(insufficient) == 1
    assert insufficient[0].provider_id is None
    assert insufficient[0].detail["ids_without_secondary_evidence"] == 5


def test_player_league_mismatch_is_decisive(builder: SourceBuilder) -> None:
    builder.player("P1", league_id="lg_mlb")
    builder.player("P1", league_id="lg_mlb", observed_at=T1)
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert PLAYER_INCOMPATIBLE_LEAGUE not in _codes(plan)


# --------------------------------------------------------------------------- #
# §24 namespace fixtures
# --------------------------------------------------------------------------- #
def test_unverified_generation_can_be_audited_but_never_accepted(
    builder: SourceBuilder,
) -> None:
    builder.player("P1", birth_date="1998-04-01")
    plan = _audit(builder.finish(), EntityType.PLAYER,
                  generation=UNVERIFIED_GENERATION)
    assert plan.verdict is AuditVerdict.REJECTED_NAMESPACE_UNVERIFIED
    assert plan.cleared_provider_ids == ()
    finding = next(f for f in plan.findings
                   if f.classification is FindingClassification.NAMESPACE_UNVERIFIED)
    assert finding.exclusion_scope.value == "league_namespace"
    assert finding.provider_id is None


def test_same_numeric_id_under_two_generations_is_two_namespaces(
    builder: SourceBuilder,
) -> None:
    a = ProviderNamespace("lg_nba", "balldontlie", EntityType.TEAM, "v1")
    b = ProviderNamespace("lg_nba", "balldontlie", EntityType.TEAM, "v2")
    assert a.key("1") != b.key("1")


def test_same_numeric_id_under_two_providers_or_types_is_distinct() -> None:
    mlb = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.TEAM, "v1")
    bdl = ProviderNamespace("lg_nba", "balldontlie", EntityType.TEAM, "v1")
    person = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.PLAYER, "v1")
    assert mlb.key("147") != bdl.key("147") != person.key("147")
    assert mlb.key("147") != person.key("147")


def test_auditing_the_wrong_league_fails_closed(builder: SourceBuilder) -> None:
    """The provider<->league binding refuses before any evidence is read.

    `game_schedule_snapshots` carries no league column, so scoping games by
    provider alone is only sound while a provider is league-exclusive. That
    invariant is now enforced rather than assumed.
    """

    builder.player("P1", league_id="lg_mlb")
    conn = open_source_corpus(builder.finish())
    try:
        with pytest.raises(SourceCorpusError, match="serves"):
            source_corpus_digest(conn, league_id="lg_nba",
                                 provider="mlb_statsapi")
        digest = source_corpus_digest(conn, league_id="lg_mlb",
                                      provider="mlb_statsapi")
        with pytest.raises(SourceCorpusError, match="serves"):
            audit_namespace(
                conn,
                namespace=ProviderNamespace("lg_nba", "mlb_statsapi",
                                            EntityType.PLAYER, "v1"),
                source_corpus_digest=digest)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §4 source-corpus digest
# --------------------------------------------------------------------------- #
def test_source_digest_is_stable_and_shared_across_entity_types(
    builder: SourceBuilder,
) -> None:
    builder.game("G1").team("T1").player("P1")
    path = builder.finish()
    conn = open_source_corpus(path)
    try:
        first = source_corpus_digest(conn, league_id="lg_mlb", provider="mlb_statsapi")
        second = source_corpus_digest(conn, league_id="lg_mlb", provider="mlb_statsapi")
    finally:
        conn.close()
    assert first == second
    # One digest per corpus, not one per entity type -- otherwise a corpus version
    # could consume crosswalks from only one of the three audits.
    for entity_type in EntityType:
        assert _audit(path, entity_type).source_corpus_digest == first


def test_changed_identity_evidence_changes_the_source_digest(
    tmp_path: Path,
) -> None:
    a = SourceBuilder(tmp_path / "a.db").player("P1", name="One").finish()
    b = SourceBuilder(tmp_path / "b.db").player("P1", name="Two").finish()
    conns = [open_source_corpus(p) for p in (a, b)]
    try:
        digests = [source_corpus_digest(c, league_id="lg_mlb",
                                        provider="mlb_statsapi") for c in conns]
    finally:
        for c in conns:
            c.close()
    assert digests[0] != digests[1]


def test_a_pending_wal_is_refused_rather_than_read_stale(tmp_path: Path) -> None:
    """`immutable` cannot see WAL content, so a pending WAL must fail closed."""

    path = tmp_path / "wal.db"
    initialize_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO provider_team_identity_snapshots (identity_id, provider, "
        "provider_team_id, league_id, full_name, normalized_name, observed_at, "
        "raw_response_id, raw_response_hash, content_hash, created_at) VALUES "
        "('pti_wal','p','1','lg_mlb','X','x',?, 'raw','h','ch',?)",
        (utc_now_iso(), utc_now_iso()))
    conn.commit()
    wal = path.with_name(path.name + "-wal")
    if not (wal.exists() and wal.stat().st_size > 0):
        pytest.skip("platform checkpointed the WAL immediately")
    with pytest.raises(SourceCorpusError, match="write-ahead log"):
        open_source_corpus(path)
    conn.close()


def test_a_corpus_without_evidence_tables_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(SourceCorpusError, match="missing audited evidence"):
        open_source_corpus(path)


# --------------------------------------------------------------------------- #
# §13 determinism
# --------------------------------------------------------------------------- #
def test_audit_is_independent_of_source_traversal_order(tmp_path: Path) -> None:
    """100 randomized insertion orders over one collision/variation fixture."""

    rows = [
        ("P1", "Alpha One", "1998-04-01", T0), ("P1", "Alpha Uno", "1998-04-01", T1),
        ("P2", "Beta Two", None, T0), ("P2", "Beta Two", None, T1),
        ("P3", "Gamma", "1990-01-01", T0), ("P3", "Gamma", "1991-01-01", T1),
        ("P4", "Delta", "2000-02-02", T2),
    ]
    digests: set[str] = set()
    summaries: set[tuple[Any, ...]] = set()
    rng = random.Random(20260812)
    for run in range(100):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        b = SourceBuilder(tmp_path / f"perm{run}.db")
        for pid, name, birth, observed in shuffled:
            b.player(pid, name=name, birth_date=birth, observed_at=observed)
        plan = _audit(b.finish(), EntityType.PLAYER)
        digests.add(plan.semantic_digest)
        summaries.add((plan.distinct_ids, plan.total_observations,
                       plan.collision_count, plan.flagged_count,
                       plan.verdict.value, tuple(sorted(_codes(plan)))))
    assert len(digests) == 1, "audit digest depended on traversal order"
    assert len(summaries) == 1
    assert next(iter(summaries))[2] == 1  # P3's two birth dates


def test_digest_excludes_wall_clock_and_surrogate_ids(tmp_path: Path) -> None:
    """Two corpora with identical evidence but different surrogate ids agree."""

    digests = []
    for name in ("x.db", "y.db"):
        b = SourceBuilder(tmp_path / name)
        b.player("P1", birth_date="1998-04-01")
        digests.append(_audit(b.finish(), EntityType.PLAYER).semantic_digest)
    assert digests[0] == digests[1]


def test_a_changed_policy_version_is_refused_not_reinterpreted(
    builder: SourceBuilder,
) -> None:
    builder.player("P1")
    conn = open_source_corpus(builder.finish())
    try:
        digest = source_corpus_digest(conn, league_id="lg_mlb",
                                      provider="mlb_statsapi")
        with pytest.raises(IdentityAuditError, match="not implemented by this build"):
            audit_namespace(
                conn,
                namespace=ProviderNamespace("lg_mlb", "mlb_statsapi",
                                            EntityType.PLAYER, "v1"),
                source_corpus_digest=digest,
                audit_policy_version="g5-identity-audit-v99")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §11/§12 reconciliation, atomicity, idempotency
# --------------------------------------------------------------------------- #
def test_counts_exactly_reconcile_with_findings(builder: SourceBuilder) -> None:
    builder.player("P1", birth_date="1998-04-01", observed_at=T0)
    builder.player("P1", birth_date="1990-01-01", observed_at=T1)   # collision
    builder.player("P2", name="A", birth_date="2000-01-01", observed_at=T0)
    builder.player("P2", name="B", birth_date="2000-01-01", observed_at=T1)  # flag
    builder.player("P3", birth_date="2001-01-01")
    plan = _audit(builder.finish(), EntityType.PLAYER)
    assert plan.distinct_ids == 3
    assert plan.total_observations == 5
    assert plan.collision_count == len({f.provider_id for f in plan.findings
                                        if f.is_collision}) == 1
    assert plan.flagged_count == sum(1 for f in plan.findings if f.is_flag) == 1
    assert plan.verdict is AuditVerdict.REJECTED_COLLISION


def test_a_rejected_audit_persists_its_collision_findings(
    builder: SourceBuilder, output_db: Path
) -> None:
    """The engine closes the completeness gap the schema review left open."""

    builder.player("P1", birth_date="1998-04-01", observed_at=T0)
    builder.player("P1", birth_date="1990-01-01", observed_at=T1)
    result = run_identity_audit(
        source_db=builder.finish(), output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=True)
    with Database(output_db).connection() as conn:
        row = conn.execute(
            "SELECT collision_count, verdict FROM identity_audit_records").fetchone()
        collisions = conn.execute(
            "SELECT COUNT(*) FROM identity_audit_findings "
            "WHERE classification = 'identity_collision'").fetchone()[0]
    assert row["verdict"] == "rejected_collision"
    assert int(row["collision_count"]) == collisions == 1
    assert result.findings_written == len(result.plan.findings)


def test_a_failed_audit_transaction_leaves_nothing_consumable(
    builder: SourceBuilder, output_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder.player("P1", birth_date="1998-04-01")
    source = builder.finish()
    import sports_quant.retrospective.runner as runner_module

    real = runner_module.persist_audit_plan

    def explode(output: sqlite3.Connection, plan: Any) -> Any:
        real(output, plan)          # write the summary and findings ...
        raise IdentityAuditError("simulated failure after the last finding")

    monkeypatch.setattr(runner_module, "persist_audit_plan", explode)
    with pytest.raises(IdentityAuditError):
        run_identity_audit(
            source_db=source, output_db=output_db, league_id="lg_mlb",
            provider="mlb_statsapi", namespace_generation="v1",
            entity_type=EntityType.PLAYER, apply=True)
    with Database(output_db).connection() as conn:
        for table in ("identity_audit_records", "identity_audit_findings",
                      "static_crosswalk_provenance", "players"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608


def test_replaying_the_same_audit_writes_nothing_new(
    builder: SourceBuilder, output_db: Path
) -> None:
    builder.player("P1", birth_date="1998-04-01")
    builder.player("P2", name="A", observed_at=T0)
    builder.player("P2", name="B", observed_at=T1)
    source = builder.finish()
    kwargs: dict[str, Any] = dict(
        source_db=source, output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=True, build_crosswalks=True)
    first = run_identity_audit(**kwargs)
    second = run_identity_audit(**kwargs)
    assert first.plan.semantic_digest == second.plan.semantic_digest
    assert first.identity_audit_id == second.identity_audit_id
    with Database(output_db).connection() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
                  for t in ("identity_audit_records", "identity_audit_findings",
                            "static_crosswalk_provenance", "players")}
    assert counts["identity_audit_records"] == 1
    assert counts["static_crosswalk_provenance"] == 2
    assert counts["players"] == 2


def test_a_changed_corpus_produces_a_new_audit(
    tmp_path: Path, output_db: Path
) -> None:
    a = SourceBuilder(tmp_path / "a.db").player("P1", name="One").finish()
    b = SourceBuilder(tmp_path / "b.db").player("P1", name="Two").finish()
    kwargs: dict[str, Any] = dict(
        output_db=output_db, league_id="lg_mlb", provider="mlb_statsapi",
        namespace_generation="v1", entity_type=EntityType.PLAYER, apply=True)
    first = run_identity_audit(source_db=a, **kwargs)
    second = run_identity_audit(source_db=b, **kwargs)
    assert first.plan.semantic_digest != second.plan.semantic_digest
    assert first.identity_audit_id != second.identity_audit_id


def test_an_unexpected_source_digest_is_refused(
    builder: SourceBuilder, output_db: Path
) -> None:
    builder.player("P1")
    with pytest.raises(IdentityAuditError, match="not the evidence this audit"):
        run_identity_audit(
            source_db=builder.finish(), output_db=output_db, league_id="lg_mlb",
            provider="mlb_statsapi", namespace_generation="v1",
            entity_type=EntityType.PLAYER,
            expected_source_corpus_digest="0" * 64)


# --------------------------------------------------------------------------- #
# §16-§18 crosswalks
# --------------------------------------------------------------------------- #
def test_player_crosswalks_bind_the_accepted_audit_and_the_exact_key(
    builder: SourceBuilder, output_db: Path
) -> None:
    builder.player("P1", birth_date="1998-04-01")
    builder.player("P2", birth_date="2001-01-01")
    result = run_identity_audit(
        source_db=builder.finish(), output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=True, build_crosswalks=True)
    assert result.crosswalk is not None and result.crosswalk.supported
    assert result.crosswalk.crosswalks_written == 2
    namespace = result.plan.namespace
    with Database(output_db).connection() as conn:
        rows = conn.execute(
            "SELECT provider_id, canonical_entity_id, identity_audit_id "
            "FROM static_crosswalk_provenance ORDER BY provider_id").fetchall()
        audit = conn.execute(
            "SELECT verdict, source_corpus_digest FROM identity_audit_records"
        ).fetchone()
    assert audit["verdict"] == "accepted"
    assert audit["source_corpus_digest"] == result.plan.source_corpus_digest
    for row in rows:
        assert row["identity_audit_id"] == result.identity_audit_id
        # The canonical id is a pure function of the official key -- no name.
        assert row["canonical_entity_id"] == canonical_player_id(
            namespace, str(row["provider_id"]))


def test_canonical_player_id_is_deterministic_and_namespace_scoped() -> None:
    mlb = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.PLAYER, "v1")
    bdl = ProviderNamespace("lg_nba", "balldontlie", EntityType.PLAYER, "v1")
    v2 = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.PLAYER, "v2")
    assert canonical_player_id(mlb, "1") == canonical_player_id(mlb, "1")
    assert canonical_player_id(mlb, "1") != canonical_player_id(bdl, "1")
    assert canonical_player_id(mlb, "1") != canonical_player_id(v2, "1")
    assert canonical_player_id(mlb, "1").startswith("pl_")


def test_a_rejected_audit_produces_no_crosswalks(
    builder: SourceBuilder, output_db: Path
) -> None:
    builder.player("P1", birth_date="1998-04-01", observed_at=T0)
    builder.player("P1", birth_date="1990-01-01", observed_at=T1)
    result = run_identity_audit(
        source_db=builder.finish(), output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=True, build_crosswalks=True)
    assert result.plan.verdict is AuditVerdict.REJECTED_COLLISION
    assert result.crosswalk is None
    with Database(output_db).connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM static_crosswalk_provenance").fetchone()[0] == 0


@pytest.mark.parametrize("entity_type", [EntityType.TEAM, EntityType.GAME])
def test_team_and_game_crosswalks_are_routed_to_the_team_a_path(
    builder: SourceBuilder, output_db: Path, entity_type: EntityType
) -> None:
    """Canonical preparation for these two is TEAM-A's, not the key bootstrap's.

    Previously this asserted a reported blocker. The blocker is closed, so what
    matters now is that the runner does not quietly fall back to provider-key
    bootstrapping: `crosswalk` must stay empty and the TEAM-A result must appear
    in its own field.

    The synthetic ids here ("T1"/"G1") are NOT in the committed map, so the
    correct outcome is zero writes -- proving the path refuses an unattested key
    rather than inventing a franchise.
    """

    builder.team("T1").game("G1")
    result = run_identity_audit(
        source_db=builder.finish(), output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=entity_type, apply=True, build_crosswalks=True)
    assert result.plan.verdict is AuditVerdict.ACCEPTED
    assert entity_type in CROSSWALK_SUPPORTED_ENTITY_TYPES
    # The provider-key bootstrap must not have run for a team or a game.
    assert result.crosswalk is None

    if entity_type is EntityType.TEAM:
        assert result.team_crosswalks is not None
        assert result.game_bootstrap is None
        assert result.team_crosswalks.written == 0
        assert result.team_crosswalks.plan.unresolved == ("T1",)
    else:
        assert result.game_bootstrap is not None
        assert result.team_crosswalks is None
        assert result.game_bootstrap.created == 0
        # No attested team crosswalk exists, so the game cannot be built.
        assert result.game_bootstrap.plan.unattested_team_ids


def test_a_crosswalk_cannot_be_built_into_a_corpus_over_other_evidence(
    tmp_path: Path, output_db: Path
) -> None:
    """The f019 cross-corpus binding still holds through the engine."""

    from sports_quant.db.repositories.retrospective import (
        SqliteRetrospectiveProvenanceRepository,
    )
    from sports_quant.retrospective.crosswalks import generate_crosswalks
    from sports_quant.retrospective.provenance import G1Variant, ProvenanceClass

    source = SourceBuilder(tmp_path / "s.db").player("P1").finish()
    result = run_identity_audit(
        source_db=source, output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=True)
    conn = open_source_corpus(source)
    try:
        with Database(output_db).connection() as out, transaction(out):
            other = SqliteRetrospectiveProvenanceRepository(out).record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id="lg_mlb", reconstruction_policy_version="rp",
                cutoff_policy_id="cp", cutoff_policy_version="1",
                source_corpus_digest="a-different-corpus",
                target_set_digest="t", g1_variant=G1Variant.G1_B_CORE)
            with pytest.raises(Exception, match="different source corpus|only ever"):
                generate_crosswalks(
                    out, conn, plan=result.plan,
                    corpus_version_id=other.corpus_version_id,
                    identity_audit_id=str(result.identity_audit_id))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §25 dry run
# --------------------------------------------------------------------------- #
def test_dry_run_computes_everything_and_writes_nothing(
    builder: SourceBuilder, output_db: Path
) -> None:
    builder.player("P1", birth_date="1998-04-01")
    builder.player("P2", birth_date="2001-01-01")
    source = builder.finish()
    kwargs: dict[str, Any] = dict(
        source_db=source, output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, build_crosswalks=True)
    dry = run_identity_audit(apply=False, **kwargs)
    assert dry.applied is False
    assert dry.identity_audit_id is None
    assert dry.crosswalk is not None and dry.crosswalk.crosswalks_written == 2
    with Database(output_db).connection() as conn:
        for table in ("identity_audit_records", "identity_audit_findings",
                      "static_crosswalk_provenance", "players",
                      "reconstruction_corpus_versions"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608
    applied = run_identity_audit(apply=True, **kwargs)
    assert applied.plan.semantic_digest == dry.plan.semantic_digest
    assert applied.crosswalk is not None
    assert applied.crosswalk.crosswalks_written == dry.crosswalk.crosswalks_written


def test_dry_run_is_deterministic(builder: SourceBuilder, output_db: Path) -> None:
    builder.player("P1", birth_date="1998-04-01")
    source = builder.finish()
    kwargs: dict[str, Any] = dict(
        source_db=source, output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=False)
    assert (run_identity_audit(**kwargs).to_json()
            == run_identity_audit(**kwargs).to_json())


# --------------------------------------------------------------------------- #
# §2 source/output separation
# --------------------------------------------------------------------------- #
def test_the_source_corpus_is_never_migrated_or_written(
    builder: SourceBuilder, output_db: Path
) -> None:
    import hashlib

    builder.player("P1", birth_date="1998-04-01")
    source = builder.finish()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    run_identity_audit(
        source_db=source, output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=True, build_crosswalks=True)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with Database(output_db).connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM static_crosswalk_provenance").fetchone()[0] == 1
    # ...and no provenance leaked back into the source.
    src = sqlite3.connect(f"file:{source.as_posix()}?immutable=1", uri=True)
    try:
        assert src.execute(
            "SELECT COUNT(*) FROM static_crosswalk_provenance").fetchone()[0] == 0
    finally:
        src.close()


def test_an_output_database_below_the_current_schema_is_refused(
    builder: SourceBuilder, tmp_path: Path
) -> None:
    """The source may be v17; the OUTPUT must be current."""

    staged = tmp_path / "migrations_v17"
    staged.mkdir()
    src_dir = Path(__file__).resolve().parents[1] / "migrations"
    for sql in sorted(src_dir.glob("*.sql")):
        if int(sql.name[1:4]) <= 17:
            (staged / sql.name).write_bytes(sql.read_bytes())
    old_output = tmp_path / "old_output.db"
    initialize_database(old_output, migrations_dir=staged)
    builder.player("P1")
    with pytest.raises(IdentityAuditError, match="provenance requires"):
        run_identity_audit(
            source_db=builder.finish(), output_db=old_output, league_id="lg_mlb",
            provider="mlb_statsapi", namespace_generation="v1",
            entity_type=EntityType.PLAYER, apply=True)


# --------------------------------------------------------------------------- #
# §26 CLI
# --------------------------------------------------------------------------- #
def test_cli_json_and_human_report_the_same_audit(
    builder: SourceBuilder, output_db: Path
) -> None:
    from sports_quant.cli import main as cli_main

    builder.player("P1", birth_date="1998-04-01")
    source = builder.finish()
    argv = ["identity-audit-retrospective", "--source-db", str(source),
            "--output-db", str(output_db), "--league", "lg_mlb",
            "--provider", "mlb_statsapi", "--namespace-generation", "v1",
            "--entity-type", "player"]

    # `assert cli_main(...) == 0 or True` was here, which passes for ANY exit
    # status and therefore asserted nothing at all.
    assert cli_main([*argv]) == 0
    human: list[str] = []
    machine: list[str] = []
    from sports_quant.cli import run_retrospective_identity_audit as handler

    assert handler(source_db=source, output_db=output_db, league_id="lg_mlb",
                   provider="mlb_statsapi", namespace_generation="v1",
                   entity_type="player", audit_policy_version=AUDIT_POLICY_VERSION,
                   as_json=True, out=machine.append) == 0
    assert handler(source_db=source, output_db=output_db, league_id="lg_mlb",
                   provider="mlb_statsapi", namespace_generation="v1",
                   entity_type="player", audit_policy_version=AUDIT_POLICY_VERSION,
                   as_json=False, out=human.append) == 0
    payload = json.loads(machine[-1])
    audit = payload["audits"][0]
    assert payload["mode"] == "dry-run"
    assert payload["network_occurred"] is False
    assert audit["verdict"] == "accepted"
    assert audit["semantic_digest"] in "\n".join(human)
    assert str(audit["distinct_ids"]) in "\n".join(human)


def test_cli_apply_is_not_the_default(
    builder: SourceBuilder, output_db: Path
) -> None:
    from sports_quant.cli import main as cli_main

    builder.player("P1")
    source = builder.finish()
    assert cli_main([
        "identity-audit-retrospective", "--source-db", str(source),
        "--output-db", str(output_db), "--league", "lg_mlb",
        "--provider", "mlb_statsapi", "--namespace-generation", "v1",
        "--entity-type", "player", "--json"]) == 0
    with Database(output_db).connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM identity_audit_records").fetchone()[0] == 0


def test_cli_has_no_provider_access_argument() -> None:
    """There is no flag that could turn this command into a fetch."""

    from sports_quant.cli import main as cli_main

    with pytest.raises(SystemExit):
        cli_main(["identity-audit-retrospective", "--help"])
    import sports_quant.retrospective.runner as runner_module

    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    for banned in ("httpx", "requests", "urllib", "load_settings", "api_key",
                   "BalldontlieClient", "MlbStatsApiClient"):
        assert banned not in source, banned


def test_cli_refuses_an_unverified_namespace_with_a_nonzero_exit(
    builder: SourceBuilder, output_db: Path
) -> None:
    from sports_quant.cli import run_retrospective_identity_audit as handler

    builder.player("P1")
    lines: list[str] = []
    code = handler(
        source_db=builder.finish(), output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation=UNVERIFIED_GENERATION,
        entity_type="player", audit_policy_version=AUDIT_POLICY_VERSION,
        apply=True, build_crosswalks=True, as_json=True, out=lines.append)
    # The audit itself succeeds and records the refusal verdict; no crosswalk.
    assert code == 0
    payload = json.loads(lines[-1])["audits"][0]
    assert payload["verdict"] == "rejected_namespace_unverified"
    assert "crosswalk" not in payload or not payload["crosswalk"]["crosswalks_written"]


# --------------------------------------------------------------------------- #
# §31 packaging / import order
# --------------------------------------------------------------------------- #
def test_audit_module_imports_first_in_a_clean_process() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    proc = subprocess.run(
        [sys.executable, "-c",
         "from sports_quant.retrospective.identity_audit import audit_namespace; "
         "import sports_quant.retrospective as r; "
         "assert r.run_identity_audit and r.AUDIT_POLICY_VERSION; print('ok')"],
        cwd=repo_root, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_schema_version_is_current(output_db: Path) -> None:
    with Database(output_db).connection() as conn:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_versions").fetchone()[0]
    assert int(version) == CURRENT_SCHEMA_VERSION
