"""Independent review of the identity-audit engine: defect reproducers.

Every test here was written as a FAILING reproducer against `b1a207d` before its
repair existed. The engine's audit policy moved from ``g5-identity-audit-v1`` to
``v2`` as a result, because the compatibility criteria changed materially.

Defects proven and closed:

  R1  a game id reused for the SAME matchup on another date read as clean
  R2  a game id reused across both halves of a doubleheader read as clean
  R3  `reschedule_info` was bound into the source digest but never reached the
      compatibility rules, so a postponement and a reuse were indistinguishable
  R4  any generation string except the literal "unverified" counted as VERIFIED
  R5  an empty namespace was reported as an ACCEPTED clean audit
  R6  the game audit and game half of the digest were scoped by provider alone,
      although `game_schedule_snapshots` carries no league column
  R7  detection power was never recorded, so "zero collisions" read as
      "identity verified" even where nothing was comparable
  R8  the dry-run crosswalk prediction ran against a two-column stub schema
  R9  `assert cli_main(...) == 0 or True` asserted nothing

Deliberately NOT repaired, and documented instead (see the review report §3/§8):
same-league team reuse and no-DOB person reuse remain **undetectable** from this
evidence. That is a property of the corpus, not a bug — and the reviewed G5
contract already accepts exact official ids as the static-identity basis. What
was wrong was the silence about it, which R7 fixes.
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.retrospective.identity_audit import (
    AUDIT_POLICY_VERSION,
    DETECTION_POWER_CODE,
    GAME_DOUBLEHEADER_REUSE,
    GAME_LEGITIMATE_MUTATION,
    GAME_UNEXPLAINED_DATE_CHANGE,
    IdentityAuditError,
    audit_namespace,
)
from sports_quant.retrospective.provenance import (
    ATTESTED_GENERATIONS,
    UNVERIFIED_GENERATION,
    AuditVerdict,
    EntityType,
    FindingSeverity,
    ProviderNamespace,
)
from sports_quant.retrospective.runner import run_identity_audit
from sports_quant.retrospective.sources import (
    PROVIDER_LEAGUES,
    SourceCorpusError,
    open_source_corpus,
    source_corpus_digest,
)

T0, T1 = "2026-06-01T00:00:00.000000Z", "2026-06-02T00:00:00.000000Z"


class Corpus:
    """A minimal source corpus holding only audited identity evidence."""

    def __init__(self, path: Path) -> None:
        initialize_database(path)
        self.path = path
        self._n = 0

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    def game(self, gid: str, *, season: int = 2026, home: str = "10",
             away: str = "20", date: str = "2026-06-01",
             start: str = "2026-06-01T22:45:00Z", status: str = "final",
             number: int = 1, dh: str = "N", venue: str = "1",
             resched: Optional[str] = None, observed: str = T0) -> "Corpus":
        self._n += 1
        with self._conn() as c:
            c.execute(
                "INSERT INTO game_schedule_snapshots (schedule_id, game_ref_id, "
                "provider, provider_game_id, season, game_date_local, "
                "scheduled_start, home_provider_team_id, away_provider_team_id, "
                "venue_provider_id, mapped_status, game_number, doubleheader_code, "
                "reschedule_info, observed_at, ingested_at, run_id, raw_response_id, "
                "raw_response_hash, content_hash, created_at) VALUES "
                "(?,?,'mlb_statsapi',?,?,?,?,?,?,?,?,?,?,?,?,?, 'run','raw','h',?,?)",
                (f"gss_{self._n}", f"pgr_{gid}", gid, season, date, start, home, away,
                 venue, status, number, dh, resched, observed, observed,
                 f"ch_{self._n}", observed))
        return self

    def team(self, tid: str, *, league: str = "lg_mlb", name: str = "Alpha Club",
             abbr: str = "ALP", city: str = "Alpha", nick: str = "Club",
             observed: str = T0) -> "Corpus":
        self._n += 1
        with self._conn() as c:
            c.execute(
                "INSERT INTO provider_team_identity_snapshots (identity_id, provider, "
                "provider_team_id, league_id, full_name, normalized_name, "
                "abbreviation, city, nickname, observed_at, raw_response_id, "
                "raw_response_hash, content_hash, created_at) VALUES "
                "(?, 'mlb_statsapi', ?,?,?,?,?,?,?,?, 'raw','h',?,?)",
                (f"pti_{self._n}", tid, league, name, name.lower(), abbr, city, nick,
                 observed, f"ch_{self._n}", observed))
        return self

    def player(self, pid: str, *, league: str = "lg_mlb", name: str = "Alpha Player",
               suffix: str = "", birth: Optional[str] = None,
               observed: str = T0) -> "Corpus":
        self._n += 1
        with self._conn() as c:
            c.execute(
                "INSERT INTO provider_player_identity_snapshots (identity_id, "
                "provider, provider_player_id, league_id, full_name, normalized_name, "
                "suffix, birth_date, position, provider_team_id, observed_at, "
                "raw_response_id, raw_response_hash, content_hash, created_at) VALUES "
                "(?, 'mlb_statsapi', ?,?,?,?,?,?,'P','10',?, 'raw','h',?,?)",
                (f"ppi_{self._n}", pid, league, name, name.lower(), suffix, birth,
                 observed, f"ch_{self._n}", observed))
        return self

    def finish(self) -> Path:
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        return self.path


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    return Corpus(tmp_path / "source.db")


@pytest.fixture
def output_db(tmp_path: Path) -> Path:
    path = tmp_path / "out.db"
    initialize_database(path)
    return path


def _audit(path: Path, entity_type: EntityType, *, league: str = "lg_mlb",
           provider: str = "mlb_statsapi", generation: str = "v1") -> Any:
    conn = open_source_corpus(path)
    try:
        digest = source_corpus_digest(conn, league_id=league, provider=provider)
        return audit_namespace(
            conn,
            namespace=ProviderNamespace(league, provider, entity_type, generation),
            source_corpus_digest=digest)
    finally:
        conn.close()


def _codes(plan: Any) -> set[str]:
    return {f.finding_code for f in plan.findings}


# --------------------------------------------------------------------------- #
# R1 / R3 -- same-matchup reuse on another date
# --------------------------------------------------------------------------- #
def test_r1_same_matchup_on_two_dates_is_not_called_clean(corpus: Corpus) -> None:
    """Yankees-vs-RedSox on Jun 1 AND Jun 15 under one id.

    Both observations share the (season, home, away) triple, so the v1 identity
    signature saw nothing at all and reported LAWFUL MUTATION. A postponement
    legitimately moves a date, so this is still not called a collision -- but it
    must not be called lawful either, because the corpus cannot tell the two
    apart. It is recorded as a detection gap.
    """

    corpus.game("G1", date="2026-06-01", start="2026-06-01T22:45:00Z", observed=T0)
    corpus.game("G1", date="2026-06-15", start="2026-06-15T22:45:00Z", observed=T1)
    plan = _audit(corpus.finish(), EntityType.GAME)
    assert GAME_UNEXPLAINED_DATE_CHANGE in _codes(plan)
    assert GAME_LEGITIMATE_MUTATION not in _codes(plan)
    finding = next(f for f in plan.findings
                   if f.finding_code == GAME_UNEXPLAINED_DATE_CHANGE)
    assert finding.severity is FindingSeverity.WARNING
    assert plan.flagged_count == 1


@pytest.mark.parametrize("continuity", [
    {"resched": '{"rescheduledFrom": "2026-06-01"}'},
    {"status": "postponed"},
    {"status": "suspended"},
])
def test_r3_provider_continuity_evidence_explains_a_date_move(
    corpus: Corpus, continuity: dict[str, Any]
) -> None:
    """`reschedule_info` was in the digest but never reached the rules."""

    corpus.game("G1", date="2026-06-01", observed=T0)
    corpus.game("G1", date="2026-06-04", observed=T1, **continuity)
    plan = _audit(corpus.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert GAME_UNEXPLAINED_DATE_CHANGE not in _codes(plan)
    assert plan.flagged_count == 0


# --------------------------------------------------------------------------- #
# R2 -- doubleheader reuse
# --------------------------------------------------------------------------- #
def test_r2_one_id_across_both_halves_of_a_doubleheader_is_a_collision(
    corpus: Corpus,
) -> None:
    """Two events on one day under one id, provable from the game numbers."""

    corpus.game("G1", date="2026-06-01", number=1, dh="S",
                start="2026-06-01T17:05:00Z", observed=T0)
    corpus.game("G1", date="2026-06-01", number=2, dh="S",
                start="2026-06-01T22:45:00Z", observed=T1)
    plan = _audit(corpus.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.REJECTED_COLLISION
    assert plan.collision_count == 1
    assert GAME_DOUBLEHEADER_REUSE in _codes(plan)


def test_r2_a_real_doubleheader_with_distinct_ids_stays_clean(
    corpus: Corpus,
) -> None:
    """The repair must not turn every legitimate doubleheader into a collision."""

    corpus.game("G1", date="2026-06-01", number=1, dh="S")
    corpus.game("G2", date="2026-06-01", number=2, dh="S")
    plan = _audit(corpus.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert plan.distinct_ids == 2


def test_r2_a_makeup_game_on_a_later_date_is_not_a_same_day_collision(
    corpus: Corpus,
) -> None:
    corpus.game("G1", date="2026-06-01", number=1, status="postponed", observed=T0)
    corpus.game("G1", date="2026-06-20", number=2, status="final", observed=T1,
                resched='{"rescheduledFrom": "2026-06-01"}')
    plan = _audit(corpus.finish(), EntityType.GAME)
    assert plan.verdict is AuditVerdict.ACCEPTED
    assert GAME_DOUBLEHEADER_REUSE not in _codes(plan)


# --------------------------------------------------------------------------- #
# R4 -- namespace generation was verified by nothing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("generation", ["banana", "V1", "v99", "", " v1", "v2"])
def test_r4_an_unattested_generation_is_not_verified(generation: str) -> None:
    """`verified` used to mean "not literally the string 'unverified'"."""

    if not generation.strip():
        pytest.skip("empty generation is refused by ProviderNamespace itself")
    namespace = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.PLAYER,
                                  generation)
    assert namespace.verified is False


def test_r4_only_attested_generations_are_verified() -> None:
    for provider, generations in ATTESTED_GENERATIONS.items():
        league = PROVIDER_LEAGUES[provider]
        for generation in generations:
            assert ProviderNamespace(league, provider, EntityType.TEAM,
                                     generation).verified
    assert ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.TEAM,
                             UNVERIFIED_GENERATION).verified is False


def test_r4_an_unattested_generation_cannot_produce_an_accepted_audit(
    corpus: Corpus,
) -> None:
    corpus.player("P1", birth="1998-01-01")
    plan = _audit(corpus.finish(), EntityType.PLAYER, generation="banana")
    assert plan.verdict is AuditVerdict.REJECTED_NAMESPACE_UNVERIFIED
    assert plan.cleared_provider_ids == ()


def test_r4_an_unattested_generation_authorizes_no_crosswalk(
    corpus: Corpus, output_db: Path
) -> None:
    corpus.player("P1", birth="1998-01-01")
    result = run_identity_audit(
        source_db=corpus.finish(), output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="banana",
        entity_type=EntityType.PLAYER, apply=True, build_crosswalks=True)
    assert result.plan.verdict is AuditVerdict.REJECTED_NAMESPACE_UNVERIFIED
    with Database(output_db).connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM static_crosswalk_provenance").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# R5 -- an empty namespace is not a clean namespace
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entity_type", list(EntityType))
def test_r5_an_empty_namespace_is_refused_not_accepted(
    corpus: Corpus, entity_type: EntityType
) -> None:
    """An audit of nothing found no contradiction, which is true and useless."""

    with pytest.raises(IdentityAuditError, match="no .* identity evidence"):
        _audit(corpus.finish(), entity_type)


def test_r5_a_single_observation_still_audits_but_reports_no_comparability(
    corpus: Corpus,
) -> None:
    corpus.player("P1", birth="1998-01-01")
    plan = _audit(corpus.finish(), EntityType.PLAYER)
    assert plan.verdict is AuditVerdict.ACCEPTED
    power = next(f for f in plan.findings if f.finding_code == DETECTION_POWER_CODE)
    assert power.detail["ids_audited"] == 1
    assert power.detail["ids_observed_more_than_once"] == 0


# --------------------------------------------------------------------------- #
# R6 -- provider/league binding
# --------------------------------------------------------------------------- #
def test_r6_a_provider_may_only_be_audited_under_its_own_league(
    corpus: Corpus,
) -> None:
    """`game_schedule_snapshots` has no league column; the scope must come from
    a declared invariant rather than from hope."""

    corpus.game("G1")
    conn = open_source_corpus(corpus.finish())
    try:
        with pytest.raises(SourceCorpusError, match="serves"):
            source_corpus_digest(conn, league_id="lg_nba", provider="mlb_statsapi")
    finally:
        conn.close()


def test_r6_an_undeclared_provider_is_refused(corpus: Corpus) -> None:
    corpus.game("G1")
    conn = open_source_corpus(corpus.finish())
    try:
        with pytest.raises(SourceCorpusError, match="no declared league"):
            source_corpus_digest(conn, league_id="lg_mlb", provider="some_new_api")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# R7 -- detection power is recorded on every audit
# --------------------------------------------------------------------------- #
def test_r7_every_audit_records_what_it_was_able_to_detect(
    corpus: Corpus,
) -> None:
    """"Zero collisions" over uncomparable evidence is not evidence of stability."""

    corpus.game("G1", home="10", away="20")
    corpus.game("G2", home="30", away="40")
    plan = _audit(corpus.finish(), EntityType.GAME)
    power = next(f for f in plan.findings if f.finding_code == DETECTION_POWER_CODE)
    assert power.detail["ids_audited"] == 2
    # Neither id was seen twice, so no contradiction COULD have surfaced.
    assert power.detail["ids_observed_more_than_once"] == 0
    assert plan.collision_count == 0


def test_r7_detection_power_distinguishes_discriminating_evidence(
    corpus: Corpus,
) -> None:
    corpus.player("P1", birth="1998-01-01", observed=T0)
    corpus.player("P1", birth="1998-01-01", observed=T1)   # comparable + DOB
    corpus.player("P2", birth=None, observed=T0)
    corpus.player("P2", birth=None, observed=T1)           # comparable, no DOB
    plan = _audit(corpus.finish(), EntityType.PLAYER)
    power = next(f for f in plan.findings if f.finding_code == DETECTION_POWER_CODE)
    assert power.detail["ids_audited"] == 2
    assert power.detail["ids_observed_more_than_once"] == 2
    assert power.detail["ids_with_discriminating_evidence"] == 1


def test_r7_detection_power_survives_persistence(
    corpus: Corpus, output_db: Path
) -> None:
    corpus.player("P1", birth="1998-01-01", observed=T0)
    corpus.player("P1", birth="1998-01-01", observed=T1)
    run_identity_audit(
        source_db=corpus.finish(), output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, apply=True)
    with Database(output_db).connection() as conn:
        row = conn.execute(
            "SELECT detail_json FROM identity_audit_findings WHERE finding_code = ?",
            (DETECTION_POWER_CODE,)).fetchone()
    assert row is not None
    assert json.loads(row["detail_json"])["ids_observed_more_than_once"] == 1


# --------------------------------------------------------------------------- #
# R8 -- dry-run fidelity
# --------------------------------------------------------------------------- #
def test_r8_dry_run_predicts_exactly_what_apply_writes(
    corpus: Corpus, output_db: Path
) -> None:
    """The dry run used a two-column stub with no NOT NULLs and no CHECKs."""

    corpus.player("P1", birth="1998-01-01")
    corpus.player("P2", birth="2001-01-01")
    source = corpus.finish()
    kwargs: dict[str, Any] = dict(
        source_db=source, output_db=output_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.PLAYER, build_crosswalks=True)
    dry = run_identity_audit(apply=False, **kwargs)
    applied = run_identity_audit(apply=True, **kwargs)
    assert dry.crosswalk is not None and applied.crosswalk is not None
    assert dry.crosswalk.crosswalks_written == applied.crosswalk.crosswalks_written
    assert (dry.crosswalk.canonical_bootstrapped
            == applied.crosswalk.canonical_bootstrapped)


def test_r8_dry_run_runs_the_real_apply_path_not_a_separate_one() -> None:
    """R8, strengthened by the TEAM-A implementation review.

    R8 originally required the dry run's scratch database to be a genuinely
    migrated schema rather than a hand-written `players` stub, because a stub
    with no NOT NULLs or CHECKs could promise a crosswalk the real schema would
    refuse. The scratch database is gone entirely: the dry run now executes the
    SAME body as apply against the REAL output database and rolls it back.

    That subsumes R8 (there is no stub schema to drift) and fixes what R8 could
    not see -- the scratch path reached the generic provider-key module, so it
    predicted "unsupported, 0 writes" for teams and games that apply then wrote.
    """

    import inspect

    from sports_quant.retrospective import runner

    assert not hasattr(runner, "_dry_run_crosswalks"), (
        "a separate dry-run code path is back; dry run and apply must share one "
        "body or they will drift again")
    source = inspect.getsource(runner)
    assert "CREATE TABLE players" not in source, "a hand-written stub is back"

    body = inspect.getsource(runner._execute)
    assert "commit" in body and "rollback" in body, (
        "the shared body must persist or discard, and nothing else may differ")


def test_r8_dry_run_scratch_enforces_the_canonical_player_checks(
    tmp_path: Path,
) -> None:
    """Prove the scratch database really carries the production constraints."""

    from sports_quant.db.init import initialize_database as init

    scratch = tmp_path / "scratch.db"
    init(scratch)
    conn = sqlite3.connect(scratch)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO players (player_id, league_id, full_name, created_at, "
                "updated_at) VALUES ('pl_x', 'lg_mlb', '', ?, ?)", (T0, T0))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO players (player_id, league_id, full_name, created_at, "
                "updated_at) VALUES ('bad_prefix', 'lg_mlb', 'X', ?, ?)", (T0, T0))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# R9 -- the CLI test that asserted nothing
# --------------------------------------------------------------------------- #
def test_r9_cli_exit_status_is_actually_asserted(
    corpus: Corpus, output_db: Path
) -> None:
    from sports_quant.cli import main as cli_main

    corpus.player("P1", birth="1998-01-01")
    argv = ["identity-audit-retrospective", "--source-db", str(corpus.finish()),
            "--output-db", str(output_db), "--league", "lg_mlb",
            "--provider", "mlb_statsapi", "--namespace-generation", "v1",
            "--entity-type", "player", "--json"]
    assert cli_main(argv) == 0
    # ...and a genuinely bad invocation is non-zero, so the assertion has teeth.
    bad = [*argv]
    bad[bad.index("lg_mlb")] = "lg_nba"
    assert cli_main(bad) == 1


def test_r9_cli_refuses_the_same_file_for_source_and_output(
    corpus: Corpus,
) -> None:
    """A source corpus is evidence; it must never also be the write target."""

    from sports_quant.cli import main as cli_main

    corpus.player("P1", birth="1998-01-01")
    path = corpus.finish()
    assert cli_main([
        "identity-audit-retrospective", "--source-db", str(path),
        "--output-db", str(path), "--league", "lg_mlb",
        "--provider", "mlb_statsapi", "--namespace-generation", "v1",
        "--entity-type", "player", "--apply", "--json"]) == 1


# --------------------------------------------------------------------------- #
# Determinism, re-proved under the repaired policy
# --------------------------------------------------------------------------- #
def test_determinism_over_randomized_orders_including_the_new_rules(
    tmp_path: Path,
) -> None:
    """100 permutations across the cases the repairs added."""

    games = [
        ("G1", "2026-06-01", 1, None, "final", T0),
        ("G1", "2026-06-01", 2, None, "final", T1),      # doubleheader reuse
        ("G2", "2026-06-02", 1, None, "postponed", T0),
        ("G2", "2026-06-09", 1, '{"r":1}', "final", T1),  # explained move
        ("G3", "2026-06-03", 1, None, "final", T0),
        ("G3", "2026-06-19", 1, None, "final", T1),      # unexplained move
        ("G4", "2026-06-04", 1, None, "final", T0),
    ]
    digests: set[str] = set()
    summaries: set[tuple[Any, ...]] = set()
    rng = random.Random(20260813)
    for run in range(100):
        shuffled = games[:]
        rng.shuffle(shuffled)
        c = Corpus(tmp_path / f"perm{run}.db")
        for gid, date, number, resched, status, observed in shuffled:
            c.game(gid, date=date, number=number, resched=resched, status=status,
                   observed=observed)
        plan = _audit(c.finish(), EntityType.GAME)
        digests.add(plan.semantic_digest)
        summaries.add((plan.distinct_ids, plan.total_observations,
                       plan.collision_count, plan.flagged_count,
                       plan.verdict.value, tuple(sorted(_codes(plan)))))
    assert len(digests) == 1, "digest depended on traversal order"
    assert len(summaries) == 1
    summary = next(iter(summaries))
    assert summary[2] == 1, "the doubleheader reuse must be the one collision"
    assert summary[4] == "rejected_collision"


def test_the_policy_version_was_bumped_for_the_changed_rules() -> None:
    """v1 records must never be reinterpreted under v2 criteria."""

    assert AUDIT_POLICY_VERSION == "g5-identity-audit-v2"
