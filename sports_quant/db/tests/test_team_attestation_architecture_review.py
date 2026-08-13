"""Independent review of the TEAM-A architecture: adversarial reproducers.

Design review only — nothing here implements an attestation map, a team
crosswalk, a game crosswalk or a reader. Each test pins a claim the design made
that this review found to be wrong, or a requirement the implementation phase
must satisfy.

Findings pinned here:

  RV1  the corpus `static_identity_map_digest` does NOT bind the crosswalk; a
       crosswalk contradicting the committed map is accepted by v19
  RV2  the curation "no other provider id claims it" rule contradicts the
       "many provider ids -> one franchise" rule; the schema sides with
       multiplicity, so injectivity must not be an architecture rule
  RV3  canonical game identity claims to include the namespace generation, but
       the enforced key is `(official_provider, official_game_key)` globally --
       no league, no generation
  RV4  the crosswalk semantic digest covers the CONCLUSION, not the curation
       evidence, so "curation evidence digest folded into semantic_digest" is
       false as written
  RV5  no canonical-team seed digest exists, so a later seed edit would silently
       change what an old corpus's attestation means
  RV6  the "second independent attribute" is not independent-source evidence
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.ids import team_id
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.retrospective import (
    ProvenanceConflictError,
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.seeds.mlb_teams import MLB_TEAMS
from sports_quant.db.seeds.nba_teams import NBA_TEAMS
from sports_quant.retrospective.provenance import (
    AuditVerdict,
    EntityType,
    G1Variant,
    ProvenanceClass,
    ProviderNamespace,
)

ISO = "2026-08-13T00:00:00.000000Z"


@pytest.fixture
def output_db(tmp_path: Path) -> Path:
    path = tmp_path / "out.db"
    initialize_database(path)
    return path


def _corpus_audit(conn: sqlite3.Connection, *, map_digest: str | None = None):
    """A corpus version plus an accepted team audit over the same source."""

    repo = SqliteRetrospectiveProvenanceRepository(conn)
    namespace = ProviderNamespace("lg_mlb", "mlb_statsapi", EntityType.TEAM, "v1")
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH, league_id="lg_mlb",
        reconstruction_policy_version="rp", cutoff_policy_id="cp",
        cutoff_policy_version="1", source_corpus_digest="src-a",
        target_set_digest="tgt", g1_variant=G1Variant.G1_B_CORE,
        static_identity_map_digest=map_digest)
    audit = repo.record_identity_audit(
        namespace=namespace, source_corpus_digest="src-a", audit_policy_version="ap",
        distinct_ids=30, total_observations=1630, collision_count=0,
        verdict=AuditVerdict.ACCEPTED)
    return repo, namespace, corpus, audit


# --------------------------------------------------------------------------- #
# RV1 -- the map digest does not bind the crosswalk
# --------------------------------------------------------------------------- #
def test_rv1_a_crosswalk_may_contradict_the_declared_map_digest(
    output_db: Path,
) -> None:
    """The design claimed the map digest binds the crosswalk. It does not.

    The corpus records a digest for committed map M; the caller then writes a
    crosswalk asserting something M does not say. v19 accepts it, because the
    database has no access to M's contents. This is the load-bearing gap the
    implementation phase must close in code -- see the review's §11/§32.
    """

    with Database(output_db).connection() as conn:
        repo, namespace, corpus, audit = _corpus_audit(
            conn, map_digest="MAP_M_says_147_is_tm_mlb_hou")
        with transaction(conn):
            wrong = repo.record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=namespace,
                provider_id="147",
                canonical_entity_id="tm_mlb_nyy",        # M says tm_mlb_hou
                identity_audit_id=audit.identity_audit_id,
                provenance_policy_version="g5-team-attestation-v1")
    assert wrong.canonical_entity_id == "tm_mlb_nyy"
    assert corpus.static_identity_map_digest == "MAP_M_says_147_is_tm_mlb_hou"


def test_rv1_and_the_wrong_crosswalk_is_indistinguishable_in_the_database(
    output_db: Path,
) -> None:
    """Neither the row nor its digest references the map's contents."""

    with Database(output_db).connection() as conn:
        repo, namespace, corpus, audit = _corpus_audit(conn, map_digest="MAP_M")
        with transaction(conn):
            row = repo.record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=namespace,
                provider_id="147", canonical_entity_id="tm_mlb_nyy",
                identity_audit_id=audit.identity_audit_id,
                provenance_policy_version="g5-team-attestation-v1")
        stored = conn.execute(
            "SELECT * FROM static_crosswalk_provenance WHERE crosswalk_id = ?",
            (row.crosswalk_id,)).fetchone()
    assert "MAP_M" not in " ".join(str(v) for v in tuple(stored))


def _digest_block(source: str) -> str:
    """The crosswalk semantic-digest inputs, as written in the repository.

    Spans from the payload dict to the `semantic_digest(...)` call, so the
    conditional map-digest branch introduced by the RV1 repair is included.
    """

    start = source.index("digest_payload: dict[str, Any] = {")
    end = source.index("digest = semantic_digest(digest_payload)")
    return source[start:end]


def test_rv1_requirement_the_map_digest_must_enter_the_crosswalk_digest() -> None:
    """RV1's digest-input requirement, now CLOSED by the implementation phase.

    The review recorded that the crosswalk semantic digest omitted the map
    digest, so a crosswalk built under map M and one built under map M' were
    cryptographically identical. The implementation folds the map digest in --
    a digest-input change, not a schema change -- and this assertion has flipped
    accordingly.

    It is folded in ONLY when supplied, so player crosswalk digests written
    before the implementation stay byte-identical.
    """

    import inspect

    from sports_quant.db.repositories.retrospective import (
        SqliteRetrospectiveProvenanceRepository as Repo,
    )

    source = inspect.getsource(Repo.record_static_crosswalk)
    block = _digest_block(source)
    assert "identity_audit_digest" in block
    assert "attestation_map_digest" in block, (
        "RV1 regressed: the attestation map digest no longer participates, so a "
        "crosswalk built under a different map would digest identically")


# --------------------------------------------------------------------------- #
# RV2 -- injectivity is an observation, not a rule
# --------------------------------------------------------------------------- #
def test_rv2_many_provider_ids_may_denote_one_canonical_franchise(
    output_db: Path,
) -> None:
    """A provider-id transition is real: old id and new id, one franchise.

    The design's curation rule said "no other provider id in that namespace
    claims it", which forbids exactly this. The schema already permits it, so the
    curation rule -- not the schema -- was wrong.
    """

    with Database(output_db).connection() as conn:
        repo, namespace, corpus, audit = _corpus_audit(conn)
        target = str(conn.execute(
            "SELECT team_id FROM teams WHERE league_id='lg_mlb' ORDER BY team_id "
            "LIMIT 1").fetchone()[0])
        with transaction(conn):
            old = repo.record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=namespace,
                provider_id="100", canonical_entity_id=target,
                identity_audit_id=audit.identity_audit_id,
                provenance_policy_version="pp")
            new = repo.record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=namespace,
                provider_id="200", canonical_entity_id=target,
                identity_audit_id=audit.identity_audit_id,
                provenance_policy_version="pp")
    assert old.canonical_entity_id == new.canonical_entity_id == target
    assert old.provider_id != new.provider_id


def test_rv2_one_provider_id_may_not_denote_two_franchises(
    output_db: Path,
) -> None:
    """The invariant that IS correct: provider-key functional uniqueness (T1)."""

    with Database(output_db).connection() as conn:
        repo, namespace, corpus, audit = _corpus_audit(conn)
        teams = [str(r[0]) for r in conn.execute(
            "SELECT team_id FROM teams WHERE league_id='lg_mlb' ORDER BY team_id "
            "LIMIT 2")]
        with transaction(conn):
            repo.record_static_crosswalk(
                corpus_version_id=corpus.corpus_version_id, namespace=namespace,
                provider_id="100", canonical_entity_id=teams[0],
                identity_audit_id=audit.identity_audit_id,
                provenance_policy_version="pp")
        with pytest.raises(ProvenanceConflictError):
            with transaction(conn):
                repo.record_static_crosswalk(
                    corpus_version_id=corpus.corpus_version_id, namespace=namespace,
                    provider_id="100", canonical_entity_id=teams[1],
                    identity_audit_id=audit.identity_audit_id,
                    provenance_policy_version="pp")


def test_rv2_the_current_thirty_to_thirty_shape_is_an_observation() -> None:
    """Recorded, not enforced.

    The prior architecture test asserted `len(set(attested.values())) == 30`,
    which makes canonical-target injectivity a global rule. It is a property of
    two one-month 2026 corpora, and a provider-id transition in a wider window
    would legitimately break it.
    """

    for seeds in (MLB_TEAMS, NBA_TEAMS):
        assert len(seeds) == 30


# --------------------------------------------------------------------------- #
# RV3 -- the enforced game key carries no league and no generation
# --------------------------------------------------------------------------- #
def test_rv3_official_game_key_uniqueness_is_global_not_league_scoped(
    tmp_path: Path,
) -> None:
    """One provider STRING may therefore only ever denote one league/product."""

    path = tmp_path / "games.db"
    initialize_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        for league, year in (("lg_mlb", 2026), ("lg_nba", 2026)):
            conn.execute(
                "INSERT OR IGNORE INTO seasons (season_id, league_id, year, phase, "
                "label, start_date, end_date, created_at, updated_at) VALUES "
                "(?,?,?,'regular',?,?,?,?,?)",
                (f"sn_{league[3:]}_{year}_regular", league, year, str(year),
                 f"{year}-01-01", f"{year}-12-31", ISO, ISO))

        def add(gid: str, league: str, key: str, date: str) -> None:
            teams = [str(r[0]) for r in conn.execute(
                "SELECT team_id FROM teams WHERE league_id=? ORDER BY team_id LIMIT 2",
                (league,))]
            conn.execute(
                "INSERT INTO games (game_id, league_id, season_id, home_team_id, "
                "away_team_id, scheduled_start, original_start, game_date_local, "
                "game_number, is_neutral_site, status, official_provider, "
                "official_game_key, created_at, updated_at) VALUES "
                "(?,?,?,?,?,?,?,?,1,0,'final','balldontlie',?,?,?)",
                (gid, league, f"sn_{league[3:]}_2026_regular", teams[0], teams[1],
                 f"{date}T22:45:00Z", f"{date}T22:45:00Z", date, key, ISO, ISO))

        add("gm_a", "lg_mlb", "12345", "2026-06-01")
        with pytest.raises(sqlite3.IntegrityError):
            add("gm_b", "lg_nba", "12345", "2026-06-02")
    finally:
        conn.close()


def test_rv3_provider_strings_carry_no_generation_or_sport() -> None:
    """So the design's stated identity key and the enforced key disagree.

    `official_provider` is plain TEXT with no CHECK, so the repair
    (GAME-NAMESPACE-B: namespace-qualified provider values) needs no migration.
    """

    from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS
    from sports_quant.retrospective.sources import PROVIDER_LEAGUES

    for provider in set(OFFICIAL_PROVIDER_BY_LEAGUE.values()) | set(PROVIDER_LEAGUES):
        assert ":" not in provider and "@" not in provider
    # More than one generation could exist per provider in future.
    assert all(len(v) >= 1 for v in ATTESTED_GENERATIONS.values())


def test_rv3_official_provider_column_can_hold_a_qualified_namespace(
    tmp_path: Path,
) -> None:
    """The GAME-NAMESPACE-B repair is expressible at v19."""

    path = tmp_path / "q.db"
    initialize_database(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO seasons (season_id, league_id, year, phase, label, "
            "start_date, end_date, created_at, updated_at) VALUES "
            "('sn_nba_2026_regular','lg_nba',2026,'regular','2026','2026-01-01',"
            "'2026-12-31',?,?)", (ISO, ISO))
        teams = [str(r[0]) for r in conn.execute(
            "SELECT team_id FROM teams WHERE league_id='lg_nba' ORDER BY team_id "
            "LIMIT 2")]
        conn.execute(
            "INSERT INTO games (game_id, league_id, season_id, home_team_id, "
            "away_team_id, scheduled_start, original_start, game_date_local, "
            "game_number, is_neutral_site, status, official_provider, "
            "official_game_key, created_at, updated_at) VALUES "
            "('gm_q','lg_nba','sn_nba_2026_regular',?,?, "
            "'2026-06-01T22:45:00Z','2026-06-01T22:45:00Z','2026-06-01',1,0,'final',"
            "'balldontlie:nba:v1','12345',?,?)", (teams[0], teams[1], ISO, ISO))
        stored = conn.execute(
            "SELECT official_provider FROM games WHERE game_id='gm_q'").fetchone()[0]
        assert stored == "balldontlie:nba:v1"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# RV4 / RV5 -- what the provenance actually captures
# --------------------------------------------------------------------------- #
def test_rv4_the_crosswalk_digest_captures_the_conclusion_not_the_evidence() -> None:
    """"Curation evidence digest folded into semantic_digest" was false."""

    import inspect

    from sports_quant.db.repositories.retrospective import (
        SqliteRetrospectiveProvenanceRepository as Repo,
    )

    source = inspect.getsource(Repo.record_static_crosswalk)
    block = _digest_block(source)
    # `map_digest` is deliberately NOT in this list any more: RV1 closed, and the
    # map digest is a VERSIONING input (which map was in force), not curation
    # evidence. What must stay out is the name evidence itself.
    for evidence_field in ("normalized_name", "abbreviation", "nickname",
                           "full_name", "curation_evidence"):
        assert evidence_field not in block, evidence_field


def test_rv5_no_canonical_team_seed_digest_exists_yet() -> None:
    """A later seed edit would silently change an old corpus's attestation meaning.

    Pinned so that adding one is a deliberate, visible change.
    """

    from sports_quant.db import seeds

    package = Path(seeds.__file__).parent
    names = {p.name for p in package.glob("*.py")}
    assert "digests.py" not in names
    for module in ("mlb_teams", "nba_teams", "loader"):
        text = (package / f"{module}.py").read_text(encoding="utf-8")
        assert "SEED_DIGEST" not in text


def test_rv5_a_seed_edit_would_change_the_canonical_id_when_abbreviation_moves(
) -> None:
    """Why the seed needs a semantic digest: ids are abbreviation-derived."""

    assert team_id("MLB", "OAK") != team_id("MLB", "ATH")


# --------------------------------------------------------------------------- #
# RV6 -- corroboration is not independence
# --------------------------------------------------------------------------- #
def test_rv6_corroborating_attributes_come_from_the_same_observation_family(
) -> None:
    """Name, abbreviation and nickname are columns of ONE provider observation.

    They are secondary corroborating attributes, not independent-source evidence.
    A provider that reused a team id and copied its labels coherently would pass
    all of them, so corroboration lowers accidental-label-match risk and proves
    nothing about provider-id permanence.
    """

    from sports_quant.retrospective.sources import TeamObservation

    fields = set(TeamObservation.__dataclass_fields__)
    assert {"full_name", "normalized_name", "abbreviation", "nickname"} <= fields
    # All of them arrive on the same row, from the same provider, at one instant.
    assert "observed_at" in fields and "provider_team_id" in fields


# --------------------------------------------------------------------------- #
# Nothing was implemented
# --------------------------------------------------------------------------- #
def test_the_reviewed_scope_was_implemented_and_nothing_beyond_it() -> None:
    """Was a review-phase scope guard; now pins the implemented boundary.

    The review shipped no code. The implementation phase (authorized separately)
    shipped exactly the TEAM-A map, team crosswalks and game bootstrap -- and
    still no reader and no market machinery.
    """

    import sports_quant.retrospective as retro
    from sports_quant.retrospective.crosswalks import (
        CROSSWALK_SUPPORTED_ENTITY_TYPES,
        DIRECT_BOOTSTRAP_ENTITY_TYPES,
    )

    assert CROSSWALK_SUPPORTED_ENTITY_TYPES == frozenset(
        {EntityType.PLAYER, EntityType.TEAM, EntityType.GAME})
    # Only players are bootstrapped straight from a provider key; teams and games
    # go through the attested TEAM-A path.
    assert DIRECT_BOOTSTRAP_ENTITY_TYPES == frozenset({EntityType.PLAYER})

    package = Path(retro.__file__).parent
    for present in ("attestations.py", "team_crosswalks.py", "game_bootstrap.py",
                    "namespaces.py", "verifier.py"):
        assert (package / present).exists(), present
    # Still blocked, and still out of scope.
    for forbidden in ("reader.py", "market.py", "odds.py", "anchoring.py",
                      "features.py"):
        assert not (package / forbidden).exists(), forbidden


def test_schema_unchanged_at_v19() -> None:
    from sports_quant.db.engine import discover_migrations
    from sports_quant.db.schema import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == 19
    assert len(discover_migrations()) == 19
