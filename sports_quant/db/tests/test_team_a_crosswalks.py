"""TEAM-A team attestation and canonical game bootstrap.

The load-bearing test in this file is the RV1 pair: a crosswalk that contradicts
the committed map must be refused by the generator, **and** caught by the verifier
even when written behind its back — because schema v19 accepts such a row and
cannot be made to reject it (the map is an external artifact).

Everything is synthetic and offline. No provider client, no settings, no protected
corpus opened for writing.
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
    MAP_FORMAT_VERSION,
    TEAM_ATTESTATION_POLICY_VERSION,
    TEAM_ATTESTATIONS,
    AttestationError,
    TeamAttestation,
    attestation_map_digest,
    attested_canonical_team,
    canonical_team_seed_digest,
    describe_map_shape,
)
from sports_quant.retrospective.game_bootstrap import (
    GAME_BOOTSTRAP_POLICY_VERSION,
    canonical_game_id,
    plan_game_bootstrap,
    write_game_bootstrap,
)
from sports_quant.retrospective.identity_audit import audit_namespace
from sports_quant.retrospective.namespaces import (
    QUALIFIED_PROVIDERS,
    qualified_provider,
)
from sports_quant.retrospective.provenance import (
    AuditVerdict,
    EntityType,
    G1Variant,
    ProvenanceClass,
    ProviderNamespace,
)
from sports_quant.retrospective.sources import (
    SourceCorpusError,
    open_source_corpus,
    source_corpus_digest,
)
from sports_quant.retrospective.team_crosswalks import (
    write_team_crosswalks,
)
from sports_quant.retrospective.verifier import verify_corpus

ISO = "2026-08-12T00:00:00.000000Z"
T0, T1 = "2026-06-01T00:00:00.000000Z", "2026-06-02T00:00:00.000000Z"
MLB_NS = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.TEAM, "v1")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class SourceCorpus:
    """A minimal source corpus of audited identity evidence."""

    def __init__(self, path: Path) -> None:
        initialize_database(path)
        self.path = path
        self._n = 0

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    def team(self, provider_team_id: str, *, name: str, abbr: str, city: str,
             nick: str, league: str = "lg_mlb", provider: str = "mlb_statsapi",
             observed: str = T0) -> "SourceCorpus":
        self._n += 1
        with self._conn() as c:
            c.execute(
                "INSERT INTO provider_team_identity_snapshots (identity_id, provider, "
                "provider_team_id, league_id, full_name, normalized_name, "
                "abbreviation, city, nickname, observed_at, raw_response_id, "
                "raw_response_hash, content_hash, created_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?, 'raw','h',?,?)",
                (f"pti_{self._n}", provider, provider_team_id, league, name,
                 name.lower(), abbr, city, nick, observed, f"ch_{self._n}", observed))
        return self

    def game(self, provider_game_id: str, *, home: str, away: str,
             season: int = 2026, date: str = "2026-06-01",
             start: str = "2026-06-01T22:45:00Z", status: str = "final",
             number: Optional[int] = 1, provider: str = "mlb_statsapi",
             observed: str = T0) -> "SourceCorpus":
        self._n += 1
        with self._conn() as c:
            c.execute(
                "INSERT INTO game_schedule_snapshots (schedule_id, game_ref_id, "
                "provider, provider_game_id, season, game_date_local, "
                "scheduled_start, home_provider_team_id, away_provider_team_id, "
                "venue_provider_id, mapped_status, game_number, doubleheader_code, "
                "reschedule_info, observed_at, ingested_at, run_id, raw_response_id, "
                "raw_response_hash, content_hash, created_at) VALUES "
                "(?,?,?,?,?,?,?,?,?, '1',?,?, 'N', NULL, ?,?, 'run','raw','h',?,?)",
                (f"gss_{self._n}", f"pgr_{provider_game_id}", provider,
                 provider_game_id, season, date, start, home, away, status, number,
                 observed, observed, f"ch_{self._n}", observed))
        return self

    def finish(self) -> Path:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        return self.path


#: Provider-written labels for the franchises these tests use, in the order the
#: tests reference them. Seeding by explicit id rather than "the first N map
#: entries" matters: the games below name 117 and 147 specifically.
_LABELS: dict[str, tuple[str, str, str, str]] = {
    "117": ("Houston Astros", "HOU", "Houston", "Astros"),
    "147": ("New York Yankees", "NYY", "New York", "Yankees"),
    "111": ("Boston Red Sox", "BOS", "Boston", "Red Sox"),
}


def _real_mlb_teams(corpus: SourceCorpus, count: int = 3) -> list[TeamAttestation]:
    """Seed evidence for real attested MLB franchises, by explicit provider id."""

    wanted = list(_LABELS)[:count]
    by_id = {e.provider_team_id: e for e in TEAM_ATTESTATIONS
             if e.league_id == "lg_mlb"}
    entries = []
    for provider_team_id in wanted:
        name, abbr, city, nick = _LABELS[provider_team_id]
        corpus.team(provider_team_id, name=name, abbr=abbr, city=city, nick=nick)
        entries.append(by_id[provider_team_id])
    return entries


@pytest.fixture
def source(tmp_path: Path) -> SourceCorpus:
    return SourceCorpus(tmp_path / "source.db")


@pytest.fixture
def output_db(tmp_path: Path) -> Path:
    path = tmp_path / "out.db"
    initialize_database(path)
    conn = sqlite3.connect(path)
    for league, year in (("lg_mlb", 2026), ("lg_nba", 2025)):
        conn.execute(
            "INSERT OR IGNORE INTO seasons (season_id, league_id, year, phase, label, "
            "start_date, end_date, created_at, updated_at) VALUES "
            "(?,?,?,'regular',?,?,?,?,?)",
            (f"sn_{league[3:]}_{year}_regular", league, year, str(year),
             f"{year}-01-01", f"{year + 1}-12-31", ISO, ISO))
    conn.commit()
    conn.close()
    return path


def code_only(text: str) -> str:
    """Strip docstrings, so prose about a concept is not mistaken for code doing it."""

    parts = text.split('"""')
    return "".join(parts[::2])   # keep everything outside triple quotes


def _seed_live_reference(
    conn: sqlite3.Connection, reference_id: str, provider_team_id: str,
    team_id: str, *, decision: bool = True, decision_team_id: Optional[str] = None,
    decision_provider: str = "mlb_statsapi",
    decision_ref: Optional[str] = None, entity_type: str = "team",
    outcome: str = "accepted",
) -> None:
    """A live/current canonical binding, optionally decision-backed.

    Review repair: a reference is authoritative ONLY when its own
    `match_decision_id` names an accepted team decision that adjudicated this
    exact provider and provider team id and matched that same canonical team.
    `decision=False` seeds the corrupt shape the earlier tests used.
    """

    decision_id = None
    if decision:
        decision_id = f"mtc_{reference_id}"
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, "
            "source_provider, source_ref, matched_entity_id, outcome, method, "
            "score, threshold, needs_manual_review, matcher_version, decided_at, "
            "created_at) "
            "VALUES (?,?,?,?,?,?,'exact_provider_id',1.0,1.0,0,'test',?,?)",
            (decision_id, entity_type, decision_provider,
             decision_ref or provider_team_id, decision_team_id or team_id,
             outcome, ISO, ISO))
    conn.execute(
        "INSERT INTO provider_team_references (reference_id, provider, "
        "provider_team_id, team_id, match_decision_id, first_raw_response_id, "
        "current_raw_response_id, current_raw_response_hash, first_observed_at, "
        "last_observed_at, created_at, updated_at) "
        "VALUES (?, 'mlb_statsapi', ?, ?, ?, 'raw_live', 'raw_live', 'h', ?, ?, ?, ?)",
        (reference_id, provider_team_id, team_id, decision_id, ISO, ISO, ISO, ISO))


def _audit(source_path: Path, entity_type: EntityType, *, league: str = "lg_mlb",
           provider: str = "mlb_statsapi") -> Any:
    conn = open_source_corpus(source_path)
    try:
        digest = source_corpus_digest(conn, league_id=league, provider=provider)
        return audit_namespace(
            conn,
            namespace=ProviderNamespace(league, provider, entity_type, "v1"),
            source_corpus_digest=digest)
    finally:
        conn.close()


def _corpus(conn: sqlite3.Connection, plan: Any, *,
            map_digest: Optional[str] = None,
            code_version: Optional[str] = "test-revision") -> Any:
    repo = SqliteRetrospectiveProvenanceRepository(conn)
    return repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        league_id=plan.namespace.league_id, reconstruction_policy_version="rp",
        cutoff_policy_id="cp", cutoff_policy_version="1",
        source_corpus_digest=plan.source_corpus_digest, target_set_digest="tgt",
        g1_variant=G1Variant.G1_B_CORE,
        static_identity_map_digest=(
            attestation_map_digest() if map_digest is None else map_digest),
        code_version=code_version)


def _persist_audit(conn: sqlite3.Connection, plan: Any) -> str:
    from sports_quant.retrospective.identity_audit import persist_audit_plan

    audit_id, _ = persist_audit_plan(conn, plan)
    return audit_id


# --------------------------------------------------------------------------- #
# The committed map, its digests, and T1
# --------------------------------------------------------------------------- #
def test_the_committed_map_holds_sixty_reviewed_entries() -> None:
    shape = describe_map_shape()
    assert shape["entries"] == 60
    assert shape["entries_by_league"] == {"lg_mlb": 30, "lg_nba": 30}
    assert shape["attestation_policy_version"] == "g5-team-attestation-v1"
    assert shape["map_format_version"] == MAP_FORMAT_VERSION


def test_lookup_is_exact_and_an_unknown_id_is_unresolved() -> None:
    assert attested_canonical_team(MLB_NS, "147") == "tm_mlb_nyy"
    assert attested_canonical_team(MLB_NS, "999999") is None
    # ...and a real name is not a key: only the provider id is.
    assert attested_canonical_team(MLB_NS, "New York Yankees") is None


def test_the_expos_franchise_maps_to_one_canonical_team() -> None:
    """Franchise continuity, preserved rather than redefined."""

    assert attested_canonical_team(MLB_NS, "120") == "tm_mlb_wsh"


def test_hornets_and_pelicans_remain_distinct_franchises() -> None:
    nba = ProviderNamespace("lg_nba", "balldontlie", EntityType.TEAM, "v1")
    assert attested_canonical_team(nba, "4") == "tm_nba_cha"
    assert attested_canonical_team(nba, "19") == "tm_nba_nop"


@pytest.mark.parametrize("digest_fn", [attestation_map_digest,
                                       canonical_team_seed_digest])
def test_digests_are_deterministic(digest_fn: Any) -> None:
    assert digest_fn() == digest_fn()
    assert len(digest_fn()) == 64


def test_the_map_digest_binds_the_seed_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """RV5: a seed edit must change the map digest."""

    import sports_quant.retrospective.attestations as attest

    before = attest.attestation_map_digest()
    monkeypatch.setattr(attest, "canonical_team_seed_digest",
                        lambda: "a-different-seed-digest")
    assert attest.attestation_map_digest() != before


def test_a_changed_mapping_changes_the_map_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sports_quant.retrospective.attestations as attest

    before = attest.attestation_map_digest()
    altered = (*TEAM_ATTESTATIONS[:-1],
               TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "158", "tm_mlb_col"))
    monkeypatch.setattr(attest, "TEAM_ATTESTATIONS", altered)
    assert attest.attestation_map_digest() != before


def test_reordering_entries_does_not_change_the_map_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Format-only differences are not semantic differences."""

    import sports_quant.retrospective.attestations as attest

    before = attest.attestation_map_digest()
    monkeypatch.setattr(attest, "TEAM_ATTESTATIONS",
                        tuple(reversed(TEAM_ATTESTATIONS)))
    assert attest.attestation_map_digest() == before


def test_t1_many_provider_ids_may_denote_one_franchise() -> None:
    """A provider-id transition: old id and new id, one franchise (§26)."""

    from sports_quant.retrospective.attestations import _validate_t1

    entries = (
        TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "old-id", "tm_mlb_hou"),
        TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "new-id", "tm_mlb_hou"),
    )
    index = _validate_t1(entries)
    assert index[("lg_mlb", "mlb_statsapi", "v1", "old-id")] == "tm_mlb_hou"
    assert index[("lg_mlb", "mlb_statsapi", "v1", "new-id")] == "tm_mlb_hou"
    # Injectivity is NOT required: one target, two keys.
    assert len({v for v in index.values()}) == 1


def test_t1_one_provider_key_may_not_denote_two_franchises() -> None:
    from sports_quant.retrospective.attestations import _validate_t1

    with pytest.raises(AttestationError, match="denotes exactly one"):
        _validate_t1((
            TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "1", "tm_mlb_hou"),
            TeamAttestation("lg_mlb", "mlb_statsapi", "v1", "1", "tm_mlb_nyy"),
        ))


def test_the_current_one_to_one_shape_is_reported_not_enforced() -> None:
    """60 keys onto 60 targets today; that is an observation about this evidence."""

    shape = describe_map_shape()
    assert shape["distinct_provider_keys"] == shape["entries"] == 60
    assert shape["distinct_canonical_targets"] == 60


# --------------------------------------------------------------------------- #
# Namespace-qualified provider constants (RV3)
# --------------------------------------------------------------------------- #
def test_qualified_providers_carry_provider_sport_and_generation() -> None:
    assert set(QUALIFIED_PROVIDERS) == {"mlb_statsapi:mlb:v1", "balldontlie:nba:v1"}
    assert qualified_provider("lg_mlb", "mlb_statsapi", "v1").value \
        == "mlb_statsapi:mlb:v1"


@pytest.mark.parametrize("league,provider,generation", [
    ("lg_nba", "mlb_statsapi", "v1"),      # wrong league
    ("lg_mlb", "mlb_statsapi", "v2"),      # unattested generation
    ("lg_mlb", "mlb_statsapi", "banana"),  # arbitrary string
    ("lg_mlb", "some_other_api", "v1"),    # secondary provider
])
def test_unregistered_namespaces_fail_closed(
    league: str, provider: str, generation: str
) -> None:
    with pytest.raises(SourceCorpusError):
        qualified_provider(league, provider, generation)


def test_the_same_numeric_game_key_coexists_across_qualified_namespaces(
    output_db: Path,
) -> None:
    """The repair: one numeric id in two products no longer collides."""

    conn = sqlite3.connect(output_db)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        for league, qualified, year in (
            ("lg_mlb", "mlb_statsapi:mlb:v1", 2026),
            ("lg_nba", "balldontlie:nba:v1", 2025),
        ):
            teams = [str(r[0]) for r in conn.execute(
                "SELECT team_id FROM teams WHERE league_id=? ORDER BY team_id LIMIT 2",
                (league,))]
            conn.execute(
                "INSERT INTO games (game_id, league_id, season_id, home_team_id, "
                "away_team_id, scheduled_start, original_start, game_date_local, "
                "game_number, is_neutral_site, status, official_provider, "
                "official_game_key, created_at, updated_at) VALUES "
                "(?,?,?,?,?, '2026-06-01T22:45:00Z','2026-06-01T22:45:00Z',"
                "'2026-06-01',1,0,'final',?, '12345', ?, ?)",
                (f"gm_{qualified[:3]}", league, f"sn_{league[3:]}_{year}_regular",
                 teams[0], teams[1], qualified, ISO, ISO))
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2
        # ...but the SAME qualified namespace plus key still cannot repeat.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO games (game_id, league_id, season_id, home_team_id, "
                "away_team_id, scheduled_start, original_start, game_date_local, "
                "game_number, is_neutral_site, status, official_provider, "
                "official_game_key, created_at, updated_at) VALUES "
                "('gm_dup','lg_mlb','sn_mlb_2026_regular','tm_mlb_hou','tm_mlb_nyy',"
                "'2026-06-02T22:45:00Z','2026-06-02T22:45:00Z','2026-06-02',1,0,"
                "'final','mlb_statsapi:mlb:v1','12345',?,?)", (ISO, ISO))
    finally:
        conn.close()


def test_canonical_game_id_is_deterministic_and_namespace_scoped() -> None:
    mlb = qualified_provider("lg_mlb", "mlb_statsapi", "v1")
    nba = qualified_provider("lg_nba", "balldontlie", "v1")
    assert canonical_game_id(mlb, "1") == canonical_game_id(mlb, "1")
    assert canonical_game_id(mlb, "1") != canonical_game_id(nba, "1")
    assert canonical_game_id(mlb, "1").startswith("gm_")


# --------------------------------------------------------------------------- #
# RV1 -- map membership before write
# --------------------------------------------------------------------------- #
def test_team_crosswalks_are_written_for_attested_ids(
    source: SourceCorpus, output_db: Path
) -> None:
    entries = _real_mlb_teams(source, 3)
    plan = _audit(source.finish(), EntityType.TEAM)
    assert plan.verdict is AuditVerdict.ACCEPTED
    with Database(output_db).connection() as conn, transaction(conn):
        corpus = _corpus(conn, plan)
        audit_id = _persist_audit(conn, plan)
        result = write_team_crosswalks(
            conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
            identity_audit_id=audit_id)
    assert result.written == len(entries)
    assert result.plan.unresolved == ()
    with Database(output_db).connection() as conn:
        rows = {str(r[0]): str(r[1]) for r in conn.execute(
            "SELECT provider_id, canonical_entity_id FROM static_crosswalk_provenance "
            "WHERE entity_type='team'")}
    for entry in entries:
        assert rows[entry.provider_team_id] == entry.canonical_team_id


def test_an_unattested_provider_team_id_is_unresolved_not_guessed(
    source: SourceCorpus, output_db: Path
) -> None:
    """Wider-window behaviour (§17): no alias fallback, no fuzzy match."""

    source.team("999999", name="Expansion Club", abbr="EXP", city="Somewhere",
                nick="Club")
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn, transaction(conn):
        corpus = _corpus(conn, plan)
        audit_id = _persist_audit(conn, plan)
        result = write_team_crosswalks(
            conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
            identity_audit_id=audit_id)
    assert result.written == 0
    assert result.plan.unresolved == ("999999",)


def test_a_corpus_without_a_map_digest_is_refused(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 2)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with transaction(conn):
            corpus = _corpus(conn, plan, map_digest=None)
            # Force the NULL the repository would otherwise fill.
            repo = SqliteRetrospectiveProvenanceRepository(conn)
            bare = repo.record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id="lg_mlb", reconstruction_policy_version="rp",
                cutoff_policy_id="cp", cutoff_policy_version="1",
                source_corpus_digest=plan.source_corpus_digest,
                target_set_digest="other", g1_variant=G1Variant.G1_B_CORE,
                code_version="rev")
            audit_id = _persist_audit(conn, plan)
        assert corpus is not None
        with pytest.raises(AttestationError, match="no static_identity_map_digest"):
            with transaction(conn):
                write_team_crosswalks(
                    conn, plan=plan, corpus_version_id=bare.corpus_version_id,
                    identity_audit_id=audit_id)


def test_a_corpus_with_a_mismatched_map_digest_is_refused(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 2)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with transaction(conn):
            corpus = _corpus(conn, plan, map_digest="a" * 64)
            audit_id = _persist_audit(conn, plan)
        with pytest.raises(AttestationError, match="declares attestation map digest"):
            with transaction(conn):
                write_team_crosswalks(
                    conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
                    identity_audit_id=audit_id)


def test_a_corpus_without_a_code_version_is_refused(
    source: SourceCorpus, output_db: Path
) -> None:
    """§13: the reproducibility contract uses the revision as the map's version axis."""

    _real_mlb_teams(source, 2)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with transaction(conn):
            corpus = _corpus(conn, plan, code_version=None)
            audit_id = _persist_audit(conn, plan)
        with pytest.raises(AttestationError, match="no code_version"):
            with transaction(conn):
                write_team_crosswalks(
                    conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
                    identity_audit_id=audit_id)


def test_the_map_digest_participates_in_the_team_crosswalk_digest(
    source: SourceCorpus, output_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RV1 repair #2: map M and map N cannot yield the same crosswalk digest."""

    from sports_quant.retrospective import team_crosswalks as tc

    _real_mlb_teams(source, 1)
    plan = _audit(source.finish(), EntityType.TEAM)
    digests = []
    for fake_map_digest in ("digest-for-map-M", "digest-for-map-N"):
        path = output_db.with_name(f"out_{fake_map_digest}.db")
        initialize_database(path)
        monkeypatch.setattr(tc, "attestation_map_digest", lambda d=fake_map_digest: d)
        with Database(path).connection() as conn, transaction(conn):
            corpus = _corpus(conn, plan, map_digest=fake_map_digest)
            audit_id = _persist_audit(conn, plan)
            write_team_crosswalks(
                conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
                identity_audit_id=audit_id)
        with Database(path).connection() as conn:
            digests.append(str(conn.execute(
                "SELECT semantic_digest FROM static_crosswalk_provenance "
                "WHERE entity_type='team'").fetchone()[0]))
    assert digests[0] != digests[1]


def test_player_crosswalk_digests_are_unaffected_by_the_new_binding() -> None:
    """Backward compatibility: the map digest is optional and omitted for players."""

    import inspect

    from sports_quant.db.repositories.retrospective import (
        SqliteRetrospectiveProvenanceRepository as Repo,
    )

    src = inspect.getsource(Repo.record_static_crosswalk)
    assert "if attestation_map_digest is not None:" in src
    assert "attestation_map_digest: Optional[str] = None" in src


# --------------------------------------------------------------------------- #
# RV1 repair #3 -- the verifier
# --------------------------------------------------------------------------- #
def test_the_verifier_catches_a_crosswalk_the_database_accepts(
    source: SourceCorpus, output_db: Path
) -> None:
    """The exact adversarial case the independent review constructed.

    v19 accepts ``147 -> tm_mlb_nyy`` even when the committed map says otherwise,
    because SQLite cannot read the map. The verifier is what catches it.
    """

    _real_mlb_teams(source, 2)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with transaction(conn):
            corpus = _corpus(conn, plan)
            audit_id = _persist_audit(conn, plan)
            repo = SqliteRetrospectiveProvenanceRepository(conn)
            # Written behind the generator's back, exactly as direct SQL would.
            repo.record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=MLB_NS,
                provider_id="117",
                canonical_entity_id="tm_mlb_nyy",   # map says tm_mlb_hou
                identity_audit_id=audit_id,
                provenance_policy_version=TEAM_ATTESTATION_POLICY_VERSION,
                attestation_map_digest=attestation_map_digest())
        report = verify_corpus(conn, corpus.corpus_version_id)
    assert not report.ok
    assert any("committed map says" in p for p in report.problems)


def test_the_verifier_flags_an_entry_absent_from_the_map(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 1)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with transaction(conn):
            corpus = _corpus(conn, plan)
            audit_id = _persist_audit(conn, plan)
            SqliteRetrospectiveProvenanceRepository(conn).record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=MLB_NS,
                provider_id="not-in-the-map", canonical_entity_id="tm_mlb_hou",
                identity_audit_id=audit_id,
                provenance_policy_version=TEAM_ATTESTATION_POLICY_VERSION)
        report = verify_corpus(conn, corpus.corpus_version_id)
    assert not report.ok
    assert any("NOT a member" in p for p in report.problems)


def test_the_verifier_flags_a_stale_corpus_map_digest(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 1)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with transaction(conn):
            repo = SqliteRetrospectiveProvenanceRepository(conn)
            corpus = repo.record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id="lg_mlb", reconstruction_policy_version="rp",
                cutoff_policy_id="cp", cutoff_policy_version="1",
                source_corpus_digest=plan.source_corpus_digest,
                target_set_digest="tgt", g1_variant=G1Variant.G1_B_CORE,
                static_identity_map_digest="b" * 64, code_version="rev")
            audit_id = _persist_audit(conn, plan)
            repo.record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=MLB_NS,
                provider_id="117", canonical_entity_id="tm_mlb_hou",
                identity_audit_id=audit_id,
                provenance_policy_version=TEAM_ATTESTATION_POLICY_VERSION)
        report = verify_corpus(conn, corpus.corpus_version_id)
    assert not report.ok
    assert any("map digest" in p for p in report.problems)


def test_a_clean_corpus_verifies(source: SourceCorpus, output_db: Path) -> None:
    _real_mlb_teams(source, 3)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with transaction(conn):
            corpus = _corpus(conn, plan)
            audit_id = _persist_audit(conn, plan)
            write_team_crosswalks(
                conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
                identity_audit_id=audit_id)
        report = verify_corpus(conn, corpus.corpus_version_id)
    assert report.ok, report.problems
    assert report.checked == 3


# --------------------------------------------------------------------------- #
# §19 Lane-R versus live conflict
# --------------------------------------------------------------------------- #
def test_a_live_canonical_disagreement_blocks_the_write(
    source: SourceCorpus, output_db: Path
) -> None:
    """Never silently prefer TEAM-A or the live matcher."""

    _real_mlb_teams(source, 1)   # 117 -> tm_mlb_hou in the committed map
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")   # synthetic reference row
        with transaction(conn):
            _seed_live_reference(conn, "ptr_x", "117", "tm_mlb_nyy")
            corpus = _corpus(conn, plan)
            audit_id = _persist_audit(conn, plan)
        with pytest.raises(AttestationError, match="disagree with an existing"):
            with transaction(conn):
                write_team_crosswalks(
                    conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
                    identity_audit_id=audit_id)
        assert conn.execute(
            "SELECT COUNT(*) FROM static_crosswalk_provenance").fetchone()[0] == 0


def test_an_agreeing_live_mapping_is_not_a_conflict(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 1)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")   # synthetic reference row
        with transaction(conn):
            _seed_live_reference(conn, "ptr_ok", "117", "tm_mlb_hou")
            corpus = _corpus(conn, plan)
            audit_id = _persist_audit(conn, plan)
            result = write_team_crosswalks(
                conn, plan=plan, corpus_version_id=corpus.corpus_version_id,
                identity_audit_id=audit_id)
    assert result.written == 1


# --------------------------------------------------------------------------- #
# Game bootstrap
# --------------------------------------------------------------------------- #
def _prepared(source: SourceCorpus, output_db: Path) -> tuple[Any, str, Any]:
    """A corpus with team crosswalks written, ready for game bootstrap."""

    team_plan = _audit(source.path, EntityType.TEAM)
    with Database(output_db).connection() as conn, transaction(conn):
        corpus = _corpus(conn, team_plan)
        audit_id = _persist_audit(conn, team_plan)
        write_team_crosswalks(conn, plan=team_plan,
                              corpus_version_id=corpus.corpus_version_id,
                              identity_audit_id=audit_id)
    return team_plan, corpus.corpus_version_id, corpus


def _game_audit(source: SourceCorpus, output_db: Path) -> str:
    """Persist the ACCEPTED game audit `write_game_bootstrap` now requires.

    Review repair: the bootstrap used to trust `plan.accepted` on an in-memory
    object, so a canonical game could exist with no G5 audit behind it.
    """

    game_plan = _audit(source.path, EntityType.GAME)
    with Database(output_db).connection() as conn, transaction(conn):
        return _persist_audit(conn, game_plan)


def test_a_canonical_game_bootstraps_from_attested_teams(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 2)
    source.game("G1", home="117", away="147")
    source.finish()
    _, corpus_id, _ = _prepared(source, output_db)
    game_audit_id = _game_audit(source, output_db)
    game_plan = _audit(source.path, EntityType.GAME)
    src = open_source_corpus(source.path)
    try:
        with Database(output_db).connection() as conn, transaction(conn):
            result = write_game_bootstrap(
                conn, src, plan=game_plan, corpus_version_id=corpus_id,
                identity_audit_id=game_audit_id)
    finally:
        src.close()
    assert result.created == 1
    with Database(output_db).connection() as conn:
        row = conn.execute(
            "SELECT official_provider, official_game_key, home_team_id, away_team_id "
            "FROM games").fetchone()
    assert row["official_provider"] == "mlb_statsapi:mlb:v1"
    assert row["official_game_key"] == "G1"
    assert (row["home_team_id"], row["away_team_id"]) == ("tm_mlb_hou", "tm_mlb_nyy")


def test_a_game_with_an_unattested_team_is_excluded_not_guessed(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 2)
    source.team("999999", name="Unknown Club", abbr="UNK", city="Nowhere", nick="Club")
    source.game("G1", home="117", away="999999")
    source.finish()
    _, corpus_id, _ = _prepared(source, output_db)
    game_plan = _audit(source.path, EntityType.GAME)
    src = open_source_corpus(source.path)
    try:
        with Database(output_db).connection() as conn:
            plan = plan_game_bootstrap(conn, src, plan=game_plan,
                                       corpus_version_id=corpus_id)
    finally:
        src.close()
    assert plan.ready == ()
    assert plan.unattested_team_ids == ("999999",)


def test_a_reschedule_does_not_mint_a_second_canonical_game(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 2)
    source.game("G1", home="117", away="147", date="2026-06-01",
                start="2026-06-01T22:45:00Z", status="postponed", observed=T0)
    source.game("G1", home="117", away="147", date="2026-06-04",
                start="2026-06-04T22:45:00Z", status="final", observed=T1)
    source.finish()
    _, corpus_id, _ = _prepared(source, output_db)
    game_audit_id = _game_audit(source, output_db)
    game_plan = _audit(source.path, EntityType.GAME)
    src = open_source_corpus(source.path)
    try:
        with Database(output_db).connection() as conn, transaction(conn):
            first = write_game_bootstrap(
                conn, src, plan=game_plan, corpus_version_id=corpus_id,
                identity_audit_id=game_audit_id)
        with Database(output_db).connection() as conn, transaction(conn):
            second = write_game_bootstrap(
                conn, src, plan=game_plan, corpus_version_id=corpus_id,
                identity_audit_id=game_audit_id)
    finally:
        src.close()
    assert first.created == 1
    assert second.created == 0 and second.reused == 1
    with Database(output_db).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
        stored = conn.execute("SELECT game_date_local FROM games").fetchone()[0]
    # The LATEST observation supplies the descriptive date.
    assert stored == "2026-06-04"


def test_an_existing_game_with_different_teams_fails_closed(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 3)
    source.game("G1", home="117", away="147")
    source.finish()
    _, corpus_id, _ = _prepared(source, output_db)
    game_audit_id = _game_audit(source, output_db)
    game_plan = _audit(source.path, EntityType.GAME)
    src = open_source_corpus(source.path)
    try:
        with Database(output_db).connection() as conn:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO games (game_id, league_id, season_id, home_team_id, "
                    "away_team_id, scheduled_start, original_start, game_date_local, "
                    "game_number, is_neutral_site, status, official_provider, "
                    "official_game_key, created_at, updated_at) VALUES "
                    "('gm_other','lg_mlb','sn_mlb_2026_regular','tm_mlb_bos',"
                    "'tm_mlb_nyy','2026-06-01T22:45:00Z','2026-06-01T22:45:00Z',"
                    "'2026-06-01',1,0,'final','mlb_statsapi:mlb:v1','G1',?,?)",
                    (ISO, ISO))
            with pytest.raises(AttestationError, match="conflict with existing"):
                with transaction(conn):
                    write_game_bootstrap(
                conn, src, plan=game_plan, corpus_version_id=corpus_id,
                identity_audit_id=game_audit_id)
    finally:
        src.close()


def test_game_bootstrap_is_idempotent_and_deterministic(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 2)
    source.game("G1", home="117", away="147")
    source.game("G2", home="147", away="117")
    source.finish()
    _, corpus_id, _ = _prepared(source, output_db)
    game_audit_id = _game_audit(source, output_db)
    game_plan = _audit(source.path, EntityType.GAME)
    src = open_source_corpus(source.path)
    try:
        for _ in range(2):
            with Database(output_db).connection() as conn, transaction(conn):
                write_game_bootstrap(
                conn, src, plan=game_plan, corpus_version_id=corpus_id,
                identity_audit_id=game_audit_id)
    finally:
        src.close()
    with Database(output_db).connection() as conn:
        ids = [str(r[0]) for r in conn.execute(
            "SELECT game_id FROM games ORDER BY official_game_key")]
    assert len(ids) == 2
    qualified = qualified_provider("lg_mlb", "mlb_statsapi", "v1")
    assert ids == [canonical_game_id(qualified, "G1"),
                   canonical_game_id(qualified, "G2")]


def test_game_bootstrap_policy_is_distinct_from_the_team_policy() -> None:
    assert GAME_BOOTSTRAP_POLICY_VERSION == "g5-game-bootstrap-v1"
    assert TEAM_ATTESTATION_POLICY_VERSION == "g5-team-attestation-v1"
    assert GAME_BOOTSTRAP_POLICY_VERSION != TEAM_ATTESTATION_POLICY_VERSION


# --------------------------------------------------------------------------- #
# Atomicity
# --------------------------------------------------------------------------- #
def test_a_failed_transaction_leaves_no_partial_team_or_game_state(
    source: SourceCorpus, output_db: Path
) -> None:
    _real_mlb_teams(source, 2)
    source.game("G1", home="117", away="147")
    source.finish()
    team_plan = _audit(source.path, EntityType.TEAM)
    with Database(output_db).connection() as conn:
        with pytest.raises(RuntimeError):
            with transaction(conn):
                corpus = _corpus(conn, team_plan)
                audit_id = _persist_audit(conn, team_plan)
                write_team_crosswalks(
                    conn, plan=team_plan,
                    corpus_version_id=corpus.corpus_version_id,
                    identity_audit_id=audit_id)
                raise RuntimeError("simulated failure after the crosswalks")
    with Database(output_db).connection() as conn:
        for table in ("static_crosswalk_provenance", "games",
                      "reconstruction_corpus_versions", "identity_audit_records"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_verifier_reports_digests_without_a_database() -> None:
    import json

    from sports_quant.cli import run_team_attestation_verify

    lines: list[str] = []
    assert run_team_attestation_verify(as_json=True, out=lines.append) == 0
    payload = json.loads(lines[-1])
    assert payload["attestation_map_digest"] == attestation_map_digest()
    assert payload["canonical_team_seed_digest"] == canonical_team_seed_digest()
    assert payload["map_shape"]["entries"] == 60
    assert payload["network_occurred"] is False


def test_cli_verifier_exits_nonzero_on_a_contradicting_crosswalk(
    source: SourceCorpus, output_db: Path
) -> None:
    from sports_quant.cli import main as cli_main

    _real_mlb_teams(source, 1)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn, transaction(conn):
        corpus = _corpus(conn, plan)
        audit_id = _persist_audit(conn, plan)
        SqliteRetrospectiveProvenanceRepository(conn).record_static_crosswalk(
            corpus_version_id=corpus.corpus_version_id, namespace=MLB_NS,
            provider_id="117", canonical_entity_id="tm_mlb_nyy",
            identity_audit_id=audit_id,
            provenance_policy_version=TEAM_ATTESTATION_POLICY_VERSION)
    assert cli_main(["team-attestation-verify", "--db", str(output_db),
                     "--json"]) == 1


def test_cli_verifier_exits_zero_on_a_clean_database(
    source: SourceCorpus, output_db: Path
) -> None:
    from sports_quant.cli import main as cli_main

    _real_mlb_teams(source, 2)
    plan = _audit(source.finish(), EntityType.TEAM)
    with Database(output_db).connection() as conn, transaction(conn):
        corpus = _corpus(conn, plan)
        audit_id = _persist_audit(conn, plan)
        write_team_crosswalks(conn, plan=plan,
                              corpus_version_id=corpus.corpus_version_id,
                              identity_audit_id=audit_id)
    assert cli_main(["team-attestation-verify", "--db", str(output_db),
                     "--json"]) == 0


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def test_no_runtime_name_or_alias_lookup_exists_in_the_team_path() -> None:
    """§14: aliases may inform curation; they may not resolve at runtime."""

    import inspect

    from sports_quant.retrospective import team_crosswalks
    from sports_quant.retrospective.attestations import attested_canonical_team

    # The RESOLVER consults nothing but the exact key. (`attestations` as a
    # module does read seed aliases -- but only inside the seed DIGEST, which is
    # a versioning input, not a resolution path.)
    resolver = code_only(inspect.getsource(attested_canonical_team))
    for banned in ("normalize_name", "alias_specs", "full_name", "normalized_name",
                   "abbreviation", "nickname"):
        assert banned not in resolver, banned

    body = code_only(inspect.getsource(team_crosswalks))
    for banned in ("normalize_name", "alias_specs", "normalized_name",
                   "abbreviation", "nickname"):
        assert banned not in body, banned


def test_schema_is_unchanged_at_v19() -> None:
    from sports_quant.db.engine import discover_migrations
    from sports_quant.db.schema import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == 19
    assert len(discover_migrations()) == 19


def test_no_market_or_feature_module_was_added() -> None:
    """The reader was separately authorized and shipped; the rest was not.

    `reader.py` was on this forbidden list until the Lane-R reader was authorized
    as its own phase. Market anchoring, odds fetching and feature engineering
    remain out of scope, so they stay on it.
    """

    import sports_quant.retrospective as retro

    package = Path(retro.__file__).parent
    assert (package / "reader.py").exists(), (
        "the authorized Lane-R reader is missing")
    for forbidden in ("market.py", "odds.py", "anchoring.py", "features.py",
                      "f1r.py", "builder.py"):
        assert not (package / forbidden).exists(), forbidden


# --------------------------------------------------------------------------- #
# Strict-PIT isolation re-proof (task §33)
#
# TEAM-A adds canonical rows and Lane-R provenance. None of it may become
# reachable from the strict-forward Lane-L builder, so the isolation claims are
# re-proved here rather than asserted in prose.
# --------------------------------------------------------------------------- #

#: Source hash of the strict-forward cutoff policy. TEAM-A must not have moved
#: it. A deliberate change to `_feature_cutoff` should update this pin *and* be
#: reviewed as a PIT change -- an accidental one fails here first.
_FEATURE_CUTOFF_SOURCE_SHA256 = "5d55345b6e2d8836df83428de82462df"


def test_the_strict_forward_cutoff_policy_is_unchanged() -> None:
    import hashlib
    import inspect

    from sports_quant.pit.dataset import _feature_cutoff

    digest = hashlib.sha256(
        inspect.getsource(_feature_cutoff).encode("utf-8")).hexdigest()[:32]
    assert digest == _FEATURE_CUTOFF_SOURCE_SHA256, (
        "the strict-forward feature cutoff moved; this is a PIT policy change and "
        "must be reviewed as one, not absorbed into a crosswalk task")


def test_every_lane_r_table_is_unsupported_for_strict_forward_joins() -> None:
    """A Lane-R table must never be joinable into a Lane-L dataset row."""

    import re

    from sports_quant.pit.registry import TABLE_REGISTRY, TableClass

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    sql = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(migrations.glob("f01[89]*.sql")))
    lane_r = sorted(set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", sql)))
    assert len(lane_r) == 5, lane_r

    for table in lane_r:
        entry = TABLE_REGISTRY.get(table)
        assert entry is not None, f"Lane-R table {table} is not registered at all"
        assert entry.classification is TableClass.UNSUPPORTED, (
            f"{table} is {entry.classification.value}, not unsupported: a "
            "retrospective conclusion would become reachable from a forward row")


def test_the_asof_reader_has_no_retrospective_or_corpus_mode() -> None:
    import inspect

    from sports_quant.pit.asof import AsOfReader

    # Docstrings are stripped: prose may legitimately discuss reconstruction,
    # what must not exist is executable code reaching into Lane-R.
    source = code_only(inspect.getsource(AsOfReader))
    for needle in ("corpus_version", "retrospective", "static_crosswalk",
                   "attestation", "identity_audit"):
        assert needle not in source, (
            f"AsOfReader has executable code mentioning {needle!r}; the "
            "strict-forward reader must stay blind to Lane-R")


def test_the_team_a_modules_do_not_reach_into_the_strict_forward_builder() -> None:
    """Dependency direction: Lane-R may not import the Lane-L dataset builder."""

    import sports_quant.retrospective as retro

    package = Path(retro.__file__).parent
    for module in ("attestations.py", "team_crosswalks.py", "game_bootstrap.py",
                   "namespaces.py", "verifier.py"):
        source = (package / module).read_text(encoding="utf-8")
        for forbidden in ("pit.dataset", "pit.asof", "AsOfReader",
                          "_feature_cutoff"):
            assert forbidden not in source, f"{module} references {forbidden}"


def test_an_august_fetched_march_lineup_is_invisible_at_a_march_cutoff(
    output_db: Path,
) -> None:
    """The canonical rule TEAM-A must not weaken.

    A lineup row *about* a March game but **observed** in August is retrospective
    evidence. At a March cutoff the strict-forward reader must not see it,
    however convenient it would be for a reconstruction.
    """

    from datetime import datetime, timezone

    from sports_quant.pit.asof import AsOfReader
    from sports_quant.pit.models import Cutoff

    def _row(lineup_id: str, observed_at: str, confirmed: int, count: int
             ) -> tuple[Any, ...]:
        return (lineup_id, "gmr_1", "mlb_statsapi", "700001", "147",
                "tm_mlb_nyy", "home", confirmed, None, count, observed_at,
                observed_at, observed_at, observed_at, "run_1", "raw_1",
                "rh", f"content_{lineup_id}", observed_at)

    with Database(output_db).connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")   # only the snapshot is needed
        with transaction(conn):
            conn.executemany(
                "INSERT INTO lineup_snapshots (lineup_id, game_ref_id, provider, "
                "provider_game_id, provider_team_id, team_id, home_away, "
                "is_confirmed, confirmed_at, player_count, provider_timestamp, "
                "published_at, observed_at, ingested_at, run_id, raw_response_id, "
                "raw_response_hash, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [_row("lns_mar", "2026-03-01T18:00:00.000000Z", 0, 9),
                 _row("lns_aug", "2026-08-01T18:00:00.000000Z", 1, 10)])

        march = AsOfReader(
            conn, Cutoff(datetime(2026, 3, 2, tzinfo=timezone.utc))
        ).lineup("gmr_1", "tm_mlb_nyy")
        assert march is not None, "the March-observed lineup should be visible"
        assert march.row_id == "lns_mar", (
            f"the August backfill leaked into a March cutoff: {march.row_id}")
        assert march.observed_at == "2026-03-01T18:00:00.000000Z", march.observed_at

        # And the August row IS visible once the cutoff genuinely reaches it --
        # otherwise this test would pass on a reader that returns nothing at all.
        august = AsOfReader(
            conn, Cutoff(datetime(2026, 8, 2, tzinfo=timezone.utc))
        ).lineup("gmr_1", "tm_mlb_nyy")
        assert august is not None and august.row_id == "lns_aug", august
