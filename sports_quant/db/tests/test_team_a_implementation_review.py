"""Independent review of the TEAM-A implementation committed at 982b73b.

Written against the reviewed architecture, not against the implementation
report: every claim in that report is treated as unproven here. The harness is
independent of `test_team_a_crosswalks.py` on purpose -- a reviewer that reuses
the implementer's fixtures inherits the implementer's assumptions.

Everything is synthetic and offline: no provider client, no settings load, no
protected corpus opened for writing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.retrospective.attestations import (
    TEAM_ATTESTATION_POLICY_VERSION,
    attestation_map_digest,
)
from sports_quant.retrospective.identity_audit import audit_namespace
from sports_quant.retrospective.provenance import (
    AuditVerdict,
    EntityType,
    G1Variant,
    ProvenanceClass,
    ProviderNamespace,
    RetrospectiveProvenanceError,
)
from sports_quant.retrospective.sources import open_source_corpus, source_corpus_digest

TS = "2026-06-01T00:00:00.000000Z"
LATER = "2026-06-02T00:00:00.000000Z"

#: Real MLB StatsAPI ids, with the labels the provider actually writes. Chosen
#: from the committed map so the attested path is exercised with true keys.
MLB_TEAMS: dict[str, tuple[str, str, str, str]] = {
    "108": ("Los Angeles Angels", "LAA", "Los Angeles", "Angels"),
    "117": ("Houston Astros", "HOU", "Houston", "Astros"),
    "147": ("New York Yankees", "NYY", "New York", "Yankees"),
}


class Corpus:
    """A minimal, independently written source corpus of identity evidence."""

    def __init__(self, path: Path) -> None:
        initialize_database(path)
        self.path = path
        self._n = 0

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    def team(self, provider_team_id: str, *, provider: str = "mlb_statsapi",
             league: str = "lg_mlb", observed: str = TS,
             label: Optional[tuple[str, str, str, str]] = None) -> "Corpus":
        self._n += 1
        name, abbr, city, nick = label or MLB_TEAMS.get(
            provider_team_id, (f"Team {provider_team_id}", "ZZZ", "City", "Nick"))
        with self._conn() as c:
            c.execute(
                "INSERT INTO provider_team_identity_snapshots (identity_id, "
                "provider, provider_team_id, league_id, full_name, "
                "normalized_name, abbreviation, city, nickname, observed_at, "
                "raw_response_id, raw_response_hash, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'raw','h',?,?)",
                (f"pti_{self._n}", provider, provider_team_id, league, name,
                 name.lower(), abbr, city, nick, observed, f"ch_{self._n}",
                 observed))
        return self

    def game(self, provider_game_id: str, *, home: str, away: str,
             provider: str = "mlb_statsapi", season: int = 2026,
             date: str = "2026-06-01", start: str = "2026-06-01T22:45:00Z",
             status: str = "final", number: int = 1,
             observed: str = TS) -> "Corpus":
        self._n += 1
        with self._conn() as c:
            c.execute(
                "INSERT INTO game_schedule_snapshots (schedule_id, game_ref_id, "
                "provider, provider_game_id, season, game_date_local, "
                "scheduled_start, home_provider_team_id, away_provider_team_id, "
                "venue_provider_id, mapped_status, game_number, "
                "doubleheader_code, reschedule_info, observed_at, ingested_at, "
                "run_id, raw_response_id, raw_response_hash, content_hash, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,'1',?,?,'N',NULL,?,?,"
                "'run','raw','h',?,?)",
                (f"gss_{self._n}", f"pgr_{provider_game_id}", provider,
                 provider_game_id, season, date, start, home, away, status,
                 number, observed, observed, f"ch_{self._n}", observed))
        return self

    def finish(self) -> Path:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        return self.path


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    return Corpus(tmp_path / "source.db")


@pytest.fixture
def out_db(tmp_path: Path) -> Path:
    """A real, disposable v19 output database with seasons prepared."""

    path = tmp_path / "out.db"
    initialize_database(path)
    conn = sqlite3.connect(path)
    for league, year in (("lg_mlb", 2026), ("lg_nba", 2025), ("lg_nba", 2026)):
        conn.execute(
            "INSERT OR IGNORE INTO seasons (season_id, league_id, year, phase, "
            "label, start_date, end_date, created_at, updated_at) "
            "VALUES (?,?,?,'regular',?,?,?,?,?)",
            (f"sn_{league[3:]}_{year}_regular", league, year, str(year),
             f"{year}-01-01", f"{year + 1}-12-31", TS, TS))
    conn.commit()
    conn.close()
    return path


def plan_for(source_path: Path, entity_type: EntityType, *,
             league: str = "lg_mlb", provider: str = "mlb_statsapi") -> Any:
    conn = open_source_corpus(source_path)
    try:
        digest = source_corpus_digest(conn, league_id=league, provider=provider)
        return audit_namespace(
            conn,
            namespace=ProviderNamespace(league, provider, entity_type, "v1"),
            source_corpus_digest=digest)
    finally:
        conn.close()


def make_corpus_row(conn: sqlite3.Connection, plan: Any, *,
                    map_digest: Optional[str] = ...,   # type: ignore[assignment]
                    code_version: str = "review",
                    target_set_digest: str = "t") -> Any:
    digest = attestation_map_digest() if map_digest is ... else map_digest
    return SqliteRetrospectiveProvenanceRepository(conn).record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        league_id=plan.namespace.league_id,
        reconstruction_policy_version="review-v1",
        cutoff_policy_id="review", cutoff_policy_version="1",
        source_corpus_digest=plan.source_corpus_digest,
        target_set_digest=target_set_digest,
        g1_variant=G1Variant.G1_B_CORE,
        static_identity_map_digest=digest, code_version=code_version)


def persist_audit(conn: sqlite3.Connection, plan: Any) -> Any:
    return SqliteRetrospectiveProvenanceRepository(conn).record_identity_audit(
        namespace=plan.namespace,
        source_corpus_digest=plan.source_corpus_digest,
        audit_policy_version=plan.audit_policy_version,
        distinct_ids=plan.distinct_ids,
        total_observations=plan.total_observations,
        collision_count=plan.collision_count,
        verdict=plan.verdict)


# --------------------------------------------------------------------------- #
# Review §3 -- dry-run / apply parity
# --------------------------------------------------------------------------- #
def test_the_team_dry_run_predicts_what_apply_actually_does(
    corpus: Corpus, out_db: Path, tmp_path: Path
) -> None:
    """A dry run must run the SAME planning logic as apply, minus persistence.

    The implementation routes every entity type through the generic
    provider-key `generate_crosswalks` on the dry-run path, and that module
    returns `supported=False` for teams. So the dry run predicted "unsupported,
    0 writes" for evidence that apply then happily wrote. That is exactly the
    failure a dry run exists to prevent.
    """

    from sports_quant.retrospective.runner import run_identity_audit

    for pid in MLB_TEAMS:
        corpus.team(pid)
    source = corpus.finish()

    dry = run_identity_audit(
        source_db=source, output_db=out_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.TEAM, apply=False, build_crosswalks=True)
    applied = run_identity_audit(
        source_db=source, output_db=out_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.TEAM, apply=True, build_crosswalks=True)

    assert applied.team_crosswalks is not None
    assert applied.team_crosswalks.written == 3

    assert dry.team_crosswalks is not None, (
        "the TEAM dry run produced no TEAM-A prediction at all; it fell through "
        "to the generic provider-key path")
    assert dry.team_crosswalks.written == applied.team_crosswalks.written
    assert (dry.team_crosswalks.plan.attested
            == applied.team_crosswalks.plan.attested)
    assert (dry.team_crosswalks.plan.unresolved
            == applied.team_crosswalks.plan.unresolved)
    assert (dry.team_crosswalks.plan.conflicts
            == applied.team_crosswalks.plan.conflicts)
    # The generic crosswalk field must stay empty for a team.
    assert dry.crosswalk is None and applied.crosswalk is None


def test_the_game_dry_run_predicts_what_apply_actually_does(
    corpus: Corpus, out_db: Path
) -> None:
    """Same contract for games, including every reported gap category."""

    from sports_quant.retrospective.runner import run_identity_audit

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    corpus.game("700002", home="117", away="108")
    source = corpus.finish()

    # Teams must be attested first; the game plan reads persisted crosswalks.
    run_identity_audit(
        source_db=source, output_db=out_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.TEAM, apply=True, build_crosswalks=True)

    dry = run_identity_audit(
        source_db=source, output_db=out_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.GAME, apply=False, build_crosswalks=True)
    applied = run_identity_audit(
        source_db=source, output_db=out_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=EntityType.GAME, apply=True, build_crosswalks=True)

    assert applied.game_bootstrap is not None
    assert applied.game_bootstrap.created == 2

    assert dry.game_bootstrap is not None, (
        "the GAME dry run produced no bootstrap prediction at all")
    dp, ap = dry.game_bootstrap.plan, applied.game_bootstrap.plan
    assert len(dp.ready) == len(ap.ready) == 2
    assert dp.unattested_team_ids == ap.unattested_team_ids == ()
    assert dp.missing_metadata == ap.missing_metadata == ()
    assert dp.missing_output_season == ap.missing_output_season == ()
    assert dp.blocked_games == ap.blocked_games == ()
    assert dp.qualified_provider == ap.qualified_provider == "mlb_statsapi:mlb:v1"
    assert dry.game_bootstrap.created == applied.game_bootstrap.created


def test_a_dry_run_writes_absolutely_nothing(corpus: Corpus, out_db: Path) -> None:
    """Parity must be achieved by withholding persistence, not by faking it."""

    from sports_quant.retrospective.runner import run_identity_audit

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    source = corpus.finish()

    def snapshot() -> dict[str, int]:
        with Database(out_db).connection() as conn:
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("teams", "games", "static_crosswalk_provenance",
                          "reconstruction_corpus_versions",
                          "identity_audit_records", "identity_audit_findings")
            }

    before = snapshot()
    for entity_type in (EntityType.TEAM, EntityType.GAME):
        run_identity_audit(
            source_db=source, output_db=out_db, league_id="lg_mlb",
            provider="mlb_statsapi", namespace_generation="v1",
            entity_type=entity_type, apply=False, build_crosswalks=True)
    assert snapshot() == before, "a dry run mutated the output database"


# --------------------------------------------------------------------------- #
# Review §4 -- a canonical game must require PERSISTED accepted audit provenance
# --------------------------------------------------------------------------- #
def test_a_forged_audit_plan_cannot_mint_canonical_games(
    corpus: Corpus, out_db: Path
) -> None:
    """A caller-supplied object claiming ACCEPTED is not provenance.

    `write_team_crosswalks` takes an `identity_audit_id` and the schema forces
    it to name a real accepted audit. `write_game_bootstrap` takes no such
    argument: it trusts `plan.accepted` on an in-memory dataclass. Anything that
    can construct an AuditPlan can therefore mint canonical games that no G5
    audit ever cleared.
    """

    from dataclasses import replace

    from sports_quant.retrospective.game_bootstrap import write_game_bootstrap

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    source_path = corpus.finish()

    team_plan = plan_for(source_path, EntityType.TEAM)
    game_plan = plan_for(source_path, EntityType.GAME)
    assert game_plan.verdict is AuditVerdict.ACCEPTED

    source = open_source_corpus(source_path)
    try:
        with Database(out_db).connection() as conn:
            with transaction(conn):
                cv = make_corpus_row(conn, game_plan)
                # Teams ARE properly attested, with a real persisted audit.
                team_cv = make_corpus_row(conn, team_plan, target_set_digest="t2")
                team_audit = persist_audit(conn, team_plan)
                from sports_quant.retrospective.team_crosswalks import (
                    write_team_crosswalks,
                )
                write_team_crosswalks(
                    conn, plan=team_plan,
                    corpus_version_id=team_cv.corpus_version_id,
                    identity_audit_id=team_audit.identity_audit_id)

            # Copy the team crosswalks into the GAME corpus version so the only
            # thing missing is the game audit itself.
            with transaction(conn):
                for row in conn.execute(
                        "SELECT league_id, provider, namespace_generation, "
                        "provider_id, canonical_entity_id FROM "
                        "static_crosswalk_provenance WHERE corpus_version_id = ?",
                        (team_cv.corpus_version_id,)).fetchall():
                    SqliteRetrospectiveProvenanceRepository(
                        conn).record_static_crosswalk(
                        corpus_version_id=cv.corpus_version_id,
                        namespace=ProviderNamespace(
                            row[0], row[1], EntityType.TEAM, row[2]),
                        provider_id=row[3], canonical_entity_id=row[4],
                        identity_audit_id=team_audit.identity_audit_id,
                        provenance_policy_version=TEAM_ATTESTATION_POLICY_VERSION,
                        attestation_map_digest=attestation_map_digest())

            # NO game audit is ever persisted. The plan merely claims ACCEPTED.
            assert conn.execute(
                "SELECT COUNT(*) FROM identity_audit_records "
                "WHERE entity_type = 'game'").fetchone()[0] == 0

            forged = replace(game_plan)   # same shape, still unpersisted
            with pytest.raises(Exception) as caught:
                with transaction(conn):
                    write_game_bootstrap(
                        conn, source, plan=forged,
                        corpus_version_id=cv.corpus_version_id,
                        identity_audit_id="ida_does_not_exist")
            assert "audit" in str(caught.value).lower(), caught.value

            assert conn.execute(
                "SELECT COUNT(*) FROM games").fetchone()[0] == 0, (
                "a canonical game was created with no persisted accepted G5 "
                "audit backing it")
    finally:
        source.close()


def _attest_teams(conn: sqlite3.Connection, source_path: Path) -> tuple[Any, Any]:
    """Persist a real accepted TEAM audit plus its TEAM-A crosswalks."""

    from sports_quant.retrospective.team_crosswalks import write_team_crosswalks

    plan = plan_for(source_path, EntityType.TEAM)
    with transaction(conn):
        cv = make_corpus_row(conn, plan)
        audit = persist_audit(conn, plan)
        write_team_crosswalks(conn, plan=plan, corpus_version_id=cv.corpus_version_id,
                              identity_audit_id=audit.identity_audit_id)
    return cv, audit


def _copy_team_crosswalks(conn: sqlite3.Connection, target_corpus: str,
                          audit_id: str) -> None:
    """Re-record the attested team crosswalks under another corpus version."""

    rows = conn.execute(
        "SELECT DISTINCT provider_id, canonical_entity_id FROM "
        "static_crosswalk_provenance WHERE entity_type = 'team'").fetchall()
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    for provider_id, canonical in rows:
        repo.record_static_crosswalk(
            corpus_version_id=target_corpus,
            namespace=ProviderNamespace(
                "lg_mlb", "mlb_statsapi", EntityType.TEAM, "v1"),
            provider_id=provider_id, canonical_entity_id=canonical,
            identity_audit_id=audit_id,
            provenance_policy_version=TEAM_ATTESTATION_POLICY_VERSION,
            attestation_map_digest=attestation_map_digest())


# --------------------------------------------------------------------------- #
# Review section 7 -- convergence with a conventionally matched canonical game
# --------------------------------------------------------------------------- #
def test_a_conventionally_matched_game_is_not_duplicated_by_team_a(
    corpus: Corpus, out_db: Path
) -> None:
    """The bare and qualified representations denote the SAME real game.

    The conventional matcher writes official_provider='mlb_statsapi'. TEAM-A
    writes 'mlb_statsapi:mlb:v1'. UNIQUE(official_provider, official_game_key)
    sees two distinct pairs, so nothing stops TEAM-A minting a second canonical
    row for a game that already exists.
    """

    from sports_quant.db.schema import utc_now_iso
    from sports_quant.retrospective.game_bootstrap import write_game_bootstrap

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("123", home="147", away="117")
    source_path = corpus.finish()

    source = open_source_corpus(source_path)
    try:
        with Database(out_db).connection() as conn:
            _, team_audit = _attest_teams(conn, source_path)
            now = utc_now_iso()
            with transaction(conn):
                conn.execute(
                    "INSERT INTO games (game_id, league_id, season_id, "
                    "home_team_id, away_team_id, scheduled_start, original_start, "
                    "game_date_local, game_number, is_neutral_site, status, "
                    "official_provider, official_game_key, created_at, updated_at) "
                    "VALUES ('gm_conventional','lg_mlb','sn_mlb_2026_regular',"
                    "'tm_mlb_nyy','tm_mlb_hou','2026-06-01T22:45:00Z',"
                    "'2026-06-01T22:45:00Z','2026-06-01',1,0,'final',"
                    "'mlb_statsapi','123',?,?)", (now, now))

            game_plan = plan_for(source_path, EntityType.GAME)
            with transaction(conn):
                cv = make_corpus_row(conn, game_plan, target_set_digest="g")
                audit = persist_audit(conn, game_plan)
                _copy_team_crosswalks(conn, cv.corpus_version_id,
                                      team_audit.identity_audit_id)
                write_game_bootstrap(
                    conn, source, plan=game_plan,
                    corpus_version_id=cv.corpus_version_id,
                    identity_audit_id=audit.identity_audit_id)

            rows = conn.execute(
                "SELECT game_id, official_provider FROM games "
                "WHERE official_game_key = '123' ORDER BY official_provider"
                ).fetchall()
            assert len(rows) == 1, (
                f"the same real game now has {len(rows)} canonical rows: "
                f"{[tuple(r) for r in rows]}")
            assert rows[0][0] == "gm_conventional", (
                "TEAM-A replaced the conventionally matched canonical game")
    finally:
        source.close()


# --------------------------------------------------------------------------- #
# Review section 9 -- an existing game with contradictory season must fail closed
# --------------------------------------------------------------------------- #
def test_an_existing_game_with_the_wrong_season_is_not_silently_reused(
    corpus: Corpus, out_db: Path
) -> None:
    """Agreeing teams do not make a corrupt canonical row a valid replay."""

    from sports_quant.db.schema import utc_now_iso
    from sports_quant.retrospective.game_bootstrap import (
        canonical_game_id,
        write_game_bootstrap,
    )
    from sports_quant.retrospective.namespaces import qualified_provider_for

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117", season=2026)
    source_path = corpus.finish()

    source = open_source_corpus(source_path)
    try:
        with Database(out_db).connection() as conn:
            _, team_audit = _attest_teams(conn, source_path)
            with transaction(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO seasons (season_id, league_id, year, "
                    "phase, label, start_date, end_date, created_at, updated_at) "
                    "VALUES ('sn_mlb_2024_regular','lg_mlb',2024,'regular','2024',"
                    "'2024-01-01','2025-12-31',?,?)", (TS, TS))

            gns = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.GAME, "v1")
            qualified = qualified_provider_for(gns)
            gid = canonical_game_id(qualified, "700001")
            now = utc_now_iso()
            with transaction(conn):
                # Right id, right teams, right key -- WRONG season.
                conn.execute(
                    "INSERT INTO games (game_id, league_id, season_id, "
                    "home_team_id, away_team_id, scheduled_start, original_start, "
                    "game_date_local, game_number, is_neutral_site, status, "
                    "official_provider, official_game_key, created_at, updated_at) "
                    "VALUES (?,'lg_mlb','sn_mlb_2024_regular','tm_mlb_nyy',"
                    "'tm_mlb_hou','2026-06-01T22:45:00Z','2026-06-01T22:45:00Z',"
                    "'2026-06-01',1,0,'final',?,'700001',?,?)",
                    (gid, qualified.value, now, now))

            game_plan = plan_for(source_path, EntityType.GAME)
            with transaction(conn):
                cv = make_corpus_row(conn, game_plan, target_set_digest="g")
                game_audit = persist_audit(conn, game_plan)
                _copy_team_crosswalks(conn, cv.corpus_version_id,
                                      team_audit.identity_audit_id)

            with pytest.raises(Exception) as caught:
                with transaction(conn):
                    write_game_bootstrap(
                        conn, source, plan=game_plan,
                        corpus_version_id=cv.corpus_version_id,
                        identity_audit_id=game_audit.identity_audit_id)
            assert "season" in str(caught.value).lower(), caught.value
    finally:
        source.close()


# --------------------------------------------------------------------------- #
# Review section 14 -- the verifier must recompute the semantic digest
# --------------------------------------------------------------------------- #
def test_the_verifier_detects_a_tampered_crosswalk_semantic_digest(
    corpus: Corpus, out_db: Path
) -> None:
    """"Cryptographically bound to the map" must be a checked claim.

    Folding the map digest into semantic_digest only binds anything if something
    later recomputes it. The verifier checks the corpus digest, the mapping and
    the policy -- but if it never recomputes the row's own digest, a tampered
    digest passes and the binding is decorative.
    """

    from sports_quant.retrospective.verifier import verify_corpus

    for pid in MLB_TEAMS:
        corpus.team(pid)
    source_path = corpus.finish()

    with Database(out_db).connection() as conn:
        cv, _audit = _attest_teams(conn, source_path)
        clean = verify_corpus(conn, cv.corpus_version_id)
        assert clean.ok, clean.as_json()

        # static_crosswalk_provenance is append-only, so tamper the way a
        # determined operator with direct SQL access would: rebuild the table.
        conn.execute("DROP TRIGGER trg_xwk_no_update")
        with transaction(conn):
            conn.execute(
                "UPDATE static_crosswalk_provenance SET semantic_digest = ? "
                "WHERE provider_id = '147'", ("0" * 64,))

        report = verify_corpus(conn, cv.corpus_version_id)
        assert not report.ok, (
            "the verifier accepted a TEAM-A crosswalk whose semantic digest does "
            "not match its own contents; the map binding is not actually verified")
        assert any("digest" in p for p in report.problems), report.problems


def _seed_reference(conn: sqlite3.Connection, *, ref: str, provider_team_id: str,
                    team_id: str, decision: bool = True,
                    decision_id_present: bool = True,
                    outcome: str = "accepted", entity_type: str = "team",
                    matched: Optional[str] = None,
                    src_provider: str = "mlb_statsapi",
                    src_ref: Optional[str] = None) -> None:
    """A live provider-team reference with a controllable decision shape."""

    from sports_quant.db.schema import utc_now_iso

    now = utc_now_iso()
    decision_id = f"mtc_{ref}" if decision_id_present else None
    if decision and decision_id is not None:
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, "
            "source_provider, source_ref, matched_entity_id, outcome, method, "
            "score, threshold, needs_manual_review, matcher_version, decided_at, "
            "rejection_reason, created_at) "
            "VALUES (?,?,?,?,?,?,'exact_provider_id',1.0,1.0,0,'t',?,?,?)",
            (decision_id, entity_type, src_provider, src_ref or provider_team_id,
             matched or team_id, outcome, now,
             None if outcome == "accepted" else "synthetic rejection", now))
    conn.execute(
        "INSERT INTO provider_team_references (reference_id, provider, "
        "provider_team_id, team_id, match_decision_id, first_raw_response_id, "
        "current_raw_response_id, current_raw_response_hash, first_observed_at, "
        "last_observed_at, created_at, updated_at) "
        "VALUES (?,'mlb_statsapi',?,?,?,'raw','raw','h',?,?,?,?)",
        (ref, provider_team_id, team_id, decision_id, now, now, now, now))


# --------------------------------------------------------------------------- #
# Review section 11 -- live-reference validity must be decision-backed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "shape,expected",
    [
        ("valid_agreeing", "attested"),
        ("valid_disagreeing", "conflict"),
        ("no_decision_id", "broken"),
        ("missing_decision", "broken"),
        ("rejected_decision", "broken"),
        ("wrong_entity_type", "broken"),
        ("wrong_target", "broken"),
        ("other_provider_id", "broken"),
        ("other_provider", "broken"),
        ("absent", "attested"),
    ],
)
def test_only_a_decision_backed_live_reference_is_authoritative(
    corpus: Corpus, out_db: Path, shape: str, expected: str
) -> None:
    """A corrupt live link is neither agreement nor a genuine identity conflict.

    147 is the Yankees in the committed map. Every shape below stores that same
    canonical target unless stated, so the ONLY thing varying is how well the
    decision backs it.
    """

    from sports_quant.retrospective.team_crosswalks import plan_team_crosswalks

    corpus.team("147")
    source_path = corpus.finish()
    plan = plan_for(source_path, EntityType.TEAM)

    kwargs: dict[str, Any] = {"ref": "ptr_1", "provider_team_id": "147",
                              "team_id": "tm_mlb_nyy"}
    if shape == "valid_disagreeing":
        kwargs.update(team_id="tm_mlb_hou")
    elif shape == "no_decision_id":
        kwargs.update(decision=False, decision_id_present=False)
    elif shape == "missing_decision":
        kwargs.update(decision=False)          # id recorded, row never written
    elif shape == "rejected_decision":
        kwargs.update(outcome="rejected")
    elif shape == "wrong_entity_type":
        kwargs.update(entity_type="player")
    elif shape == "wrong_target":
        kwargs.update(matched="tm_mlb_hou")
    elif shape == "other_provider_id":
        kwargs.update(src_ref="108")
    elif shape == "other_provider":
        kwargs.update(src_provider="balldontlie")

    with Database(out_db).connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")   # synthetic raw_response ids
        with transaction(conn):
            cv = make_corpus_row(conn, plan)
            persist_audit(conn, plan)
            if shape != "absent":
                _seed_reference(conn, **kwargs)
        team_plan = plan_team_crosswalks(
            conn, plan=plan, corpus_version_id=cv.corpus_version_id)

    if expected == "attested":
        assert team_plan.attested == (("147", "tm_mlb_nyy"),), team_plan.as_json()
        assert not team_plan.blocked
    elif expected == "conflict":
        assert team_plan.conflicts and not team_plan.broken_live_links
        assert team_plan.blocked
    else:
        assert team_plan.broken_live_links, (
            f"{shape} was treated as an authoritative live identity")
        assert not team_plan.conflicts, (
            f"{shape} was reported as a genuine identity conflict, which "
            "misattributes matcher corruption to TEAM-A")
        assert team_plan.blocked


# --------------------------------------------------------------------------- #
# Review section 12 -- map membership is exact lookup, many-to-one allowed
# --------------------------------------------------------------------------- #
def test_the_map_permits_many_provider_ids_for_one_franchise() -> None:
    """T1 forbids one key meaning two franchises; it does NOT force injectivity."""

    from sports_quant.retrospective.attestations import (
        AttestationError,
        TeamAttestation,
        _validate_t1,
    )

    shared = [
        TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "111", "tm_mlb_bos"),
        TeamAttestation("lg_mlb", "mlb_statsapi", "v2", "9111", "tm_mlb_bos"),
    ]
    assert len(_validate_t1(tuple(shared))) == 2, "many->one was rejected"

    duplicated = [
        TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "111", "tm_mlb_bos"),
        TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "111", "tm_mlb_nyy"),
    ]
    with pytest.raises(AttestationError):
        _validate_t1(tuple(duplicated))


@pytest.mark.parametrize("league,provider,generation,provider_id", [
    ("lg_mlb", "mlb_statsapi", "v1", "999999"),   # unknown id
    ("lg_nba", "mlb_statsapi", "v1", "147"),      # wrong league
    ("lg_mlb", "mlb_statsapi", "v9", "147"),      # wrong generation
    ("lg_mlb", "totally_made_up", "v1", "147"),   # arbitrary provider
])
def test_an_unattested_key_resolves_to_nothing(
    league: str, provider: str, generation: str, provider_id: str
) -> None:
    from sports_quant.retrospective.attestations import attested_canonical_team

    ns = ProviderNamespace(league, provider, EntityType.TEAM, generation)
    assert attested_canonical_team(ns, provider_id) is None


# --------------------------------------------------------------------------- #
# Review section 13 -- digests reconstructed WITHOUT the production helpers
# --------------------------------------------------------------------------- #
def _independent_seed_digest() -> str:
    """Recompute the canonical franchise seed digest from first principles."""

    import hashlib
    import json

    from sports_quant.db.ids import team_id
    from sports_quant.db.seeds.loader import alias_specs
    from sports_quant.db.seeds.mlb_teams import MLB_TEAMS as MLB_SEED
    from sports_quant.db.seeds.nba_teams import NBA_TEAMS as NBA_SEED

    rows = []
    for code, league_id, seeds in (("MLB", "lg_mlb", MLB_SEED),
                                   ("NBA", "lg_nba", NBA_SEED)):
        for seed in seeds:
            rows.append({
                "league_id": league_id,
                "canonical_team_id": team_id(code, seed.abbreviation),
                "abbreviation": seed.abbreviation,
                "canonical_name": seed.canonical_name,
                "city": seed.city,
                "nickname": seed.nickname,
                "aliases": sorted([a, k] for a, k in alias_specs(seed)),
            })
    rows.sort(key=lambda r: (r["league_id"], r["canonical_team_id"]))
    payload = {"kind": "canonical_team_seed", "version": "seed-v1",
               "franchises": rows}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_the_seed_digest_matches_an_independent_reconstruction() -> None:
    from sports_quant.retrospective.attestations import canonical_team_seed_digest

    assert _independent_seed_digest() == canonical_team_seed_digest()


def test_the_map_digest_is_order_independent_but_semantics_sensitive() -> None:
    """Row/dict order must not matter; every semantic axis must."""

    import sports_quant.retrospective.attestations as att

    baseline = att.attestation_map_digest()

    shuffled = list(reversed(att.TEAM_ATTESTATIONS))
    original = att.TEAM_ATTESTATIONS
    try:
        att.TEAM_ATTESTATIONS = tuple(shuffled)     # type: ignore[misc]  # noqa
        assert att.attestation_map_digest() == baseline, (
            "the map digest depends on row order")
    finally:
        att.TEAM_ATTESTATIONS = original            # type: ignore[misc]

    # Every one of these SHOULD move the digest.
    moved = []
    for label, patch in (
        ("map entry", lambda: setattr(
            att, "TEAM_ATTESTATIONS",
            original[:-1] + (att.TeamAttestation(
                original[-1].league_id, original[-1].provider,
                original[-1].namespace_generation, "999999",
                original[-1].canonical_team_id),))),
        ("policy version", lambda: setattr(
            att, "TEAM_ATTESTATION_POLICY_VERSION", "g5-team-attestation-v2")),
        ("format version", lambda: setattr(
            att, "MAP_FORMAT_VERSION", "team-a-map-v2")),
    ):
        saved = (att.TEAM_ATTESTATIONS, att.TEAM_ATTESTATION_POLICY_VERSION,
                 att.MAP_FORMAT_VERSION)
        try:
            patch()
            moved.append((label, att.attestation_map_digest() != baseline))
        finally:
            # Rebinding module Finals is exactly the point: this test proves a
            # semantic edit MOVES the digest, then restores the real values.
            att.TEAM_ATTESTATIONS = saved[0]                # type: ignore[misc]
            att.TEAM_ATTESTATION_POLICY_VERSION = saved[1]  # type: ignore[misc]
            att.MAP_FORMAT_VERSION = saved[2]               # type: ignore[misc]
    assert all(changed for _, changed in moved), moved
    assert att.attestation_map_digest() == baseline, "state leaked between patches"


def test_the_seed_digest_feeds_the_map_digest() -> None:
    """A franchise-semantics edit must change the corpus, not pass unnoticed."""

    import sports_quant.retrospective.attestations as att

    baseline = att.attestation_map_digest()
    original = att.canonical_team_seed_digest
    try:
        att.canonical_team_seed_digest = lambda: "0" * 64   # type: ignore[assignment]
        assert att.attestation_map_digest() != baseline, (
            "the canonical seed digest does not participate in the map digest")
    finally:
        att.canonical_team_seed_digest = original           # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Review section 17 -- completeness means REFERENCED coverage
# --------------------------------------------------------------------------- #
def test_a_subset_corpus_is_complete_without_the_whole_league_map(
    corpus: Corpus, out_db: Path
) -> None:
    """A two-team corpus is complete when its two teams are covered."""

    from sports_quant.retrospective.verifier import (
        referenced_provider_team_ids,
        verify_corpus,
    )

    corpus.team("147").team("117")
    source_path = corpus.finish()

    with Database(out_db).connection() as conn:
        cv, _ = _attest_teams(conn, source_path)
        referenced = referenced_provider_team_ids(source_path)
        assert referenced == {"147", "117"}

        by_reference = verify_corpus(
            conn, cv.corpus_version_id,
            referenced_provider_team_ids=referenced)
        assert by_reference.ok, by_reference.problems
        assert by_reference.referenced_checked == 2

        # The other contract legitimately fails, and must not be conflated.
        whole_map = verify_corpus(conn, cv.corpus_version_id,
                                  require_full_league_map=True)
        assert not whole_map.ok
        assert len(whole_map.problems) == 28, whole_map.problems[:2]


def test_a_referenced_id_missing_from_the_map_is_surfaced(
    corpus: Corpus, out_db: Path
) -> None:
    """The selection-bias case the whole-map check structurally cannot see."""

    from sports_quant.retrospective.verifier import (
        referenced_provider_team_ids,
        verify_corpus,
    )

    corpus.team("147").team("117")
    # A historical franchise id the committed map has never heard of, referenced
    # only by a game -- so it never appears in team identity evidence either.
    corpus.game("G1", home="147", away="8675309")
    source_path = corpus.finish()

    with Database(out_db).connection() as conn:
        cv, _ = _attest_teams(conn, source_path)
        referenced = referenced_provider_team_ids(source_path)
        assert "8675309" in referenced

        report = verify_corpus(conn, cv.corpus_version_id,
                               referenced_provider_team_ids=referenced)
        assert not report.ok
        assert any("8675309" in p and "NOT in the committed map" in p
                   for p in report.problems), report.problems


# --------------------------------------------------------------------------- #
# Review sections 21/22 -- qualified namespaces and canonical game determinism
# --------------------------------------------------------------------------- #
def test_a_canonical_game_id_binds_the_namespace_and_nothing_mutable() -> None:
    from sports_quant.retrospective.game_bootstrap import canonical_game_id
    from sports_quant.retrospective.namespaces import (
        QUALIFIED_PROVIDERS,
        qualified_provider_for,
    )

    mlb = QUALIFIED_PROVIDERS["mlb_statsapi:mlb:v1"]
    nba = QUALIFIED_PROVIDERS["balldontlie:nba:v1"]

    assert canonical_game_id(mlb, "555") == canonical_game_id(mlb, "555")
    assert canonical_game_id(mlb, "555") != canonical_game_id(nba, "555")

    # Nothing mutable may appear in the derivation.
    import inspect
    body = inspect.getsource(canonical_game_id)
    body = "".join(body.split('"""')[::2])          # strip the docstring
    for mutable in ("score", "winner", "status", "scheduled_start", "venue",
                    "game_date_local"):
        assert mutable not in body, mutable

    for bad in (ProviderNamespace("lg_nba", "mlb_statsapi", EntityType.GAME, "v1"),
                ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.GAME, "v9"),
                ProviderNamespace("lg_mlb", "nonsense", EntityType.GAME, "v1")):
        # Both AttestationError and SourceCorpusError derive from this; what
        # matters is that it fails closed with a domain error, not which subclass.
        with pytest.raises(RetrospectiveProvenanceError):
            qualified_provider_for(bad)


def test_the_same_game_key_may_coexist_across_two_legitimate_namespaces(
    corpus: Corpus, out_db: Path
) -> None:
    """MLB game 555 and NBA game 555 are different events, not a collision."""

    from sports_quant.db.schema import utc_now_iso
    from sports_quant.retrospective.game_bootstrap import canonical_game_id
    from sports_quant.retrospective.namespaces import QUALIFIED_PROVIDERS

    now = utc_now_iso()
    with Database(out_db).connection() as conn:
        with transaction(conn):
            for qualified, league, home, away, season in (
                (QUALIFIED_PROVIDERS["mlb_statsapi:mlb:v1"], "lg_mlb",
                 "tm_mlb_nyy", "tm_mlb_hou", "sn_mlb_2026_regular"),
                (QUALIFIED_PROVIDERS["balldontlie:nba:v1"], "lg_nba",
                 "tm_nba_bos", "tm_nba_lal", "sn_nba_2026_regular"),
            ):
                conn.execute(
                    "INSERT INTO games (game_id, league_id, season_id, "
                    "home_team_id, away_team_id, scheduled_start, original_start, "
                    "game_date_local, game_number, is_neutral_site, status, "
                    "official_provider, official_game_key, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,'2026-06-01T22:45:00Z',"
                    "'2026-06-01T22:45:00Z','2026-06-01',1,0,'final',?,'555',?,?)",
                    (canonical_game_id(qualified, "555"), league, season, home,
                     away, qualified.value, now, now))
        assert conn.execute(
            "SELECT COUNT(*) FROM games WHERE official_game_key='555'"
            ).fetchone()[0] == 2


def _full_run(source_path: Path, out_db: Path, entity_type: EntityType, *,
              apply: bool = True, **kw: Any) -> Any:
    from sports_quant.retrospective.runner import run_identity_audit

    return run_identity_audit(
        source_db=source_path, output_db=out_db, league_id="lg_mlb",
        provider="mlb_statsapi", namespace_generation="v1",
        entity_type=entity_type, apply=apply, build_crosswalks=True, **kw)


# --------------------------------------------------------------------------- #
# Review sections 5/31 -- every Lane-R game carries corpus+audit provenance
# --------------------------------------------------------------------------- #
def test_each_canonical_game_gets_corpus_scoped_audit_backed_provenance(
    corpus: Corpus, out_db: Path
) -> None:
    """The canonical `games` row alone cannot answer the reader's question.

    `games.official_provider`/`official_game_key` is global: it says nothing
    about which reconstruction corpus authorized the identity, which G5 audit
    cleared it, or under which policy. v19's `static_crosswalk_provenance`
    already models exactly that, so the bootstrap now writes it (GAME-PROV-C).
    """

    from sports_quant.retrospective.game_bootstrap import (
        GAME_BOOTSTRAP_POLICY_VERSION,
    )

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    corpus.game("700002", home="117", away="108")
    source_path = corpus.finish()

    _full_run(source_path, out_db, EntityType.TEAM)
    game = _full_run(source_path, out_db, EntityType.GAME)
    assert game.game_bootstrap.created == 2
    assert game.game_bootstrap.provenance_written == 2

    with Database(out_db).connection() as conn:
        rows = conn.execute(
            "SELECT provider_id, canonical_entity_id, corpus_version_id, "
            "       identity_audit_id, provenance_policy_version, league_id, "
            "       provider, namespace_generation "
            "FROM static_crosswalk_provenance WHERE entity_type = 'game' "
            "ORDER BY provider_id").fetchall()
        assert len(rows) == 2, [tuple(r) for r in rows]
        for row in rows:
            assert row["corpus_version_id"] == game.corpus_version_id
            assert row["identity_audit_id"] == game.identity_audit_id
            assert row["provenance_policy_version"] == GAME_BOOTSTRAP_POLICY_VERSION
            assert (row["league_id"], row["provider"], row["namespace_generation"]
                    ) == ("lg_mlb", "mlb_statsapi", "v1")
            # And the binding names a game that actually exists.
            assert conn.execute(
                "SELECT COUNT(*) FROM games WHERE game_id = ?",
                (row["canonical_entity_id"],)).fetchone()[0] == 1

        # The audit cited is a real ACCEPTED game audit.
        assert conn.execute(
            "SELECT verdict, entity_type FROM identity_audit_records "
            "WHERE identity_audit_id = ?", (game.identity_audit_id,)
            ).fetchone()[:2] == ("accepted", "game")


def test_game_provenance_is_isolated_per_corpus_and_replays(
    corpus: Corpus, out_db: Path
) -> None:
    """Replay reuses; a second corpus gets its own binding, not a shared one."""

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    source_path = corpus.finish()

    _full_run(source_path, out_db, EntityType.TEAM)
    first = _full_run(source_path, out_db, EntityType.GAME)
    assert first.game_bootstrap.created == 1

    replay = _full_run(source_path, out_db, EntityType.GAME)
    assert replay.game_bootstrap.created == 0
    assert replay.game_bootstrap.reused == 1

    with Database(out_db).connection() as conn:
        # One canonical game, but a provenance row per corpus version.
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
        corpora = {r[0] for r in conn.execute(
            "SELECT DISTINCT corpus_version_id FROM static_crosswalk_provenance "
            "WHERE entity_type = 'game'")}
        assert len(corpora) >= 1
        targets = {r[0] for r in conn.execute(
            "SELECT DISTINCT canonical_entity_id FROM static_crosswalk_provenance "
            "WHERE entity_type = 'game'")}
        assert len(targets) == 1, "one provider game bound to two canonical games"


# --------------------------------------------------------------------------- #
# Review section 26 -- ordering, and no hidden order-dependent state
# --------------------------------------------------------------------------- #
def test_game_before_team_is_reported_not_guessed(
    corpus: Corpus, out_db: Path
) -> None:
    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    source_path = corpus.finish()

    game = _full_run(source_path, out_db, EntityType.GAME)
    assert game.game_bootstrap.created == 0
    assert set(game.game_bootstrap.plan.unattested_team_ids) == {"147", "117"}
    with Database(out_db).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0

    _full_run(source_path, out_db, EntityType.TEAM)
    after = _full_run(source_path, out_db, EntityType.GAME)
    assert after.game_bootstrap.created == 1


# --------------------------------------------------------------------------- #
# Review section 27 -- a dry run predicts reuse against a NONEMPTY target
# --------------------------------------------------------------------------- #
def test_a_dry_run_against_a_prepared_database_predicts_reuse(
    corpus: Corpus, out_db: Path
) -> None:
    """The old scratch-database design always assumed an empty target."""

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    source_path = corpus.finish()

    _full_run(source_path, out_db, EntityType.TEAM)
    _full_run(source_path, out_db, EntityType.GAME)

    dry = _full_run(source_path, out_db, EntityType.GAME, apply=False)
    assert dry.game_bootstrap is not None
    assert dry.game_bootstrap.created == 0, "dry run predicted a redundant create"
    assert dry.game_bootstrap.reused == 1

    team_dry = _full_run(source_path, out_db, EntityType.TEAM, apply=False)
    assert team_dry.team_crosswalks is not None
    assert team_dry.team_crosswalks.written == 0,         'dry run predicted redundant crosswalk writes against a prepared target'
    assert team_dry.team_crosswalks.reused == 3


# --------------------------------------------------------------------------- #
# Review section 28 -- atomicity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fail_at", ["before_game", "after_game", "in_provenance"])
def test_a_failed_game_bootstrap_leaves_no_orphan_canonical_game(
    corpus: Corpus, out_db: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    """No canonical game may survive a bootstrap that did not complete."""

    from sports_quant.db.repositories import retrospective as repo_mod
    from sports_quant.retrospective import game_bootstrap as gb

    for pid in MLB_TEAMS:
        corpus.team(pid)
    corpus.game("700001", home="147", away="117")
    corpus.game("700002", home="117", away="108")
    source_path = corpus.finish()
    _full_run(source_path, out_db, EntityType.TEAM)

    boom = RuntimeError("injected failure")
    if fail_at == "before_game":
        monkeypatch.setattr(gb, "plan_game_bootstrap",
                            lambda *a, **k: (_ for _ in ()).throw(boom))
    elif fail_at == "after_game":
        real = repo_mod.SqliteRetrospectiveProvenanceRepository.record_static_crosswalk

        def once(self: Any, **kw: Any) -> Any:
            if getattr(once, "seen", False):
                raise boom
            once.seen = True                     # type: ignore[attr-defined]
            return real(self, **kw)
        monkeypatch.setattr(
            repo_mod.SqliteRetrospectiveProvenanceRepository,
            "record_static_crosswalk", once)
    else:
        monkeypatch.setattr(
            repo_mod.SqliteRetrospectiveProvenanceRepository,
            "record_static_crosswalk",
            lambda self, **kw: (_ for _ in ()).throw(boom))

    with pytest.raises(RuntimeError):
        _full_run(source_path, out_db, EntityType.GAME)

    with Database(out_db).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0, (
            "a canonical game outlived the failed bootstrap that created it")
        assert conn.execute(
            "SELECT COUNT(*) FROM static_crosswalk_provenance "
            "WHERE entity_type = 'game'").fetchone()[0] == 0


def test_a_failed_team_write_leaves_no_partial_corpus(
    corpus: Corpus, out_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_quant.db.repositories import retrospective as repo_mod

    for pid in MLB_TEAMS:
        corpus.team(pid)
    source_path = corpus.finish()

    real = repo_mod.SqliteRetrospectiveProvenanceRepository.record_static_crosswalk
    calls = {"n": 0}

    def midway(self: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected mid-crosswalk failure")
        return real(self, **kw)

    monkeypatch.setattr(repo_mod.SqliteRetrospectiveProvenanceRepository,
                        "record_static_crosswalk", midway)
    with pytest.raises(RuntimeError):
        _full_run(source_path, out_db, EntityType.TEAM)

    with Database(out_db).connection() as conn:
        for table in ("static_crosswalk_provenance",
                      "reconstruction_corpus_versions", "identity_audit_records"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


# --------------------------------------------------------------------------- #
# Review section 29 -- concurrency converges or fails closed
# --------------------------------------------------------------------------- #
def test_two_writers_of_the_same_team_crosswalks_converge(
    corpus: Corpus, out_db: Path
) -> None:
    """Second writer must reuse, never duplicate or contradict."""

    for pid in MLB_TEAMS:
        corpus.team(pid)
    source_path = corpus.finish()

    first = _full_run(source_path, out_db, EntityType.TEAM)
    second = _full_run(source_path, out_db, EntityType.TEAM)

    # Scientifically identical inputs must land on the same corpus version.
    assert second.corpus_version_id == first.corpus_version_id
    assert second.team_crosswalks.reused == 3
    assert second.team_crosswalks.written == 0
    with Database(out_db).connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM static_crosswalk_provenance "
            "WHERE entity_type='team'").fetchone()[0] == 3


def test_a_changed_scientific_identity_does_not_reuse_another_corpus(
    corpus: Corpus, out_db: Path
) -> None:
    """A different target set is a different corpus; crosswalks do not carry over."""

    for pid in MLB_TEAMS:
        corpus.team(pid)
    source_path = corpus.finish()

    base = _full_run(source_path, out_db, EntityType.TEAM)
    other = _full_run(source_path, out_db, EntityType.TEAM,
                      target_set_digest="a-different-target-set")
    assert other.corpus_version_id != base.corpus_version_id
    assert other.team_crosswalks.written == 3, (
        "the new corpus silently inherited another corpus's crosswalks")


# --------------------------------------------------------------------------- #
# Review section 33 -- strict PIT is untouched by any of this
# --------------------------------------------------------------------------- #
def test_the_repairs_did_not_move_strict_forward_pit() -> None:
    import hashlib
    import inspect

    from sports_quant.pit.dataset import _feature_cutoff
    from sports_quant.pit.registry import TABLE_REGISTRY, TableClass

    digest = hashlib.sha256(
        inspect.getsource(_feature_cutoff).encode("utf-8")).hexdigest()[:32]
    assert digest == "5d55345b6e2d8836df83428de82462df", (
        "the strict-forward cutoff policy moved during an identity review")

    for table in ("identity_audit_records", "identity_audit_findings",
                  "reconstruction_corpus_versions", "static_crosswalk_provenance",
                  "reconstructed_input_provenance"):
        assert TABLE_REGISTRY[table].classification is TableClass.UNSUPPORTED, table


def test_no_feature_or_model_code_imports_team_a() -> None:
    """TEAM-A must not become reachable from the strict-forward lane."""

    roots = [Path("sports_quant/pit"), Path("sports_quant/quality")]
    for root in roots:
        for path in root.rglob("*.py"):
            if "test" in path.name:
                continue
            body = path.read_text(encoding="utf-8")
            for banned in ("retrospective.attestations", "team_crosswalks",
                           "game_bootstrap", "attested_canonical_team"):
                assert banned not in body, f"{path} references {banned}"
