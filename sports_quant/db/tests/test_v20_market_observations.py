"""v20 historical market event observations: schema, repository, adversarial.

Attacks go through DIRECT SQL wherever the claim is about the database, because
a guarantee that only holds when the caller cooperates is not a guarantee.

No provider is contacted, no credit is spent, and nothing under ``data/`` is
opened.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database, discover_migrations
from sports_quant.db.repositories.base import RepositoryError
from sports_quant.db.repositories.market_observations import (
    SqliteMarketObservationRepository,
)
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION, utc_now_iso
from sports_quant.retrospective import sources
from sports_quant.retrospective.market_observations import (
    OBSERVATION_CONTENT_POLICY_VERSION,
    MarketEventObservation,
    ObservationValidationError,
    observation_content_hash,
    observation_id,
)

ODDS = "the_odds_api:basketball_nba"
GEN = "v4"
EVENT_A = "be25eb82b82629d959c1e5ccb8dcc1e7"
EVENT_B = "111a955795876d50988b15c219ce0796"
BUCKET = "2026-03-05T23:10:00.000000Z"
SNAP = "2026-03-05T23:05:00.000000Z"
COMMENCE = "2026-03-06T00:10:00.000000Z"


def observation(**over: object) -> MarketEventObservation:
    base: dict[str, object] = dict(
        league_id="lg_nba", provider=ODDS, namespace_generation=GEN,
        sport_key="basketball_nba", provider_event_id=EVENT_A,
        requested_at_bucket=BUCKET, provider_snapshot_timestamp=SNAP,
        commence_time=COMMENCE, home_team_raw="Boston Celtics",
        away_team_raw="Miami Heat")
    base.update(over)
    return MarketEventObservation(**base)  # type: ignore[arg-type]


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "v20.db"
    Database(path).migrate()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    now = utc_now_iso()
    conn.execute("INSERT INTO leagues (league_id, code, name, sport, created_at, "
                 "updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)",
                 (now, now))
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, "
        "args_json, status, requested_at, started_at, started_monotonic_ns, "
        "requests_made, records_received, records_normalized, records_inserted, "
        "records_deduplicated, records_rejected, records_updated, tool_version, "
        "created_at) VALUES ('run_1','x',?, 'op','{}','started',?,?,1,"
        "0,0,0,0,0,0,0,'v',?)", (ODDS, now, now, now))
    for rid, status, provider in (("raw_ok", 200, ODDS), ("raw_500", 500, ODDS),
                                  ("raw_other", 200, "balldontlie")):
        conn.execute(
            "INSERT INTO raw_responses (raw_response_id, run_id, provider, "
            "endpoint, request_params_json, http_status, response_headers_json, "
            "body, body_bytes, body_hash, content_hash, requested_at, "
            "received_at, elapsed_ns, created_at) VALUES (?, 'run_1', ?, "
            "'/v4/historical/sports/basketball_nba/events', '{}', ?, '{}', '[]', "
            "2, ?, 'c', ?, ?, 1, ?)",
            (rid, provider, status, rid, now, now, now))
    conn.commit()
    return conn


def repo(conn: sqlite3.Connection) -> SqliteMarketObservationRepository:
    return SqliteMarketObservationRepository(conn)


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
def test_fresh_init_reaches_the_current_version(tmp_path: Path) -> None:
    result = Database(tmp_path / "fresh.db").migrate()
    assert len(result.applied) == CURRENT_SCHEMA_VERSION
    assert result.schema_version == CURRENT_SCHEMA_VERSION
    assert len(discover_migrations()) == CURRENT_SCHEMA_VERSION


def test_migration_is_idempotent_on_replay(tmp_path: Path) -> None:
    path = tmp_path / "replay.db"
    first = Database(path).migrate()
    second = Database(path).migrate()
    assert second.applied == ()
    assert second.schema_version == first.schema_version == CURRENT_SCHEMA_VERSION


@pytest.mark.parametrize("stop_after", [17, 18, 19, 20])
def test_upgrade_from_an_older_version_preserves_data(
    tmp_path: Path, stop_after: int,
) -> None:
    """v17/v18/v19 -> v20 succeeds and leaves the older rows untouched."""

    partial = tmp_path / f"mig{stop_after}"
    partial.mkdir()
    for migration in discover_migrations():
        if migration.version <= stop_after:
            (partial / f"{migration.name}.sql").write_text(
                migration.sql, encoding="utf-8")

    path = tmp_path / f"from{stop_after}.db"
    first = Database(path, migrations_dir=partial).migrate()
    assert first.schema_version == stop_after

    conn = sqlite3.connect(path)
    now = utc_now_iso()
    conn.execute("INSERT INTO leagues (league_id, code, name, sport, created_at, "
                 "updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)",
                 (now, now))
    conn.commit()
    conn.close()

    result = Database(path).migrate()
    assert result.schema_version == CURRENT_SCHEMA_VERSION
    assert [m.version for m in result.applied] == list(
        range(stop_after + 1, CURRENT_SCHEMA_VERSION + 1))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT code FROM leagues").fetchone()["code"] == "NBA"
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = "
        "'historical_market_event_observations'").fetchone() is not None


def test_no_canonical_game_id_or_identity_column_exists(
    db: sqlite3.Connection,
) -> None:
    cols = {r["name"] for r in db.execute(
        "PRAGMA table_info(historical_market_event_observations)")}
    for forbidden in ("canonical_game_id", "game_id", "match_decision_id",
                      "orientation", "price", "odds", "status", "event_status"):
        assert forbidden not in cols
    assert "observation_content_hash" in cols


# --------------------------------------------------------------------------- #
# Content hash and deterministic id
# --------------------------------------------------------------------------- #
def test_content_hash_matches_an_independently_constructed_digest() -> None:
    """Rebuilt here from the spec, not by calling the production helper twice."""

    obs = observation()
    payload = {
        "policy": OBSERVATION_CONTENT_POLICY_VERSION,
        "league_id": "lg_nba", "provider": ODDS, "namespace_generation": GEN,
        "sport_key": "basketball_nba", "provider_event_id": EVENT_A,
        "requested_at_bucket": BUCKET, "provider_snapshot_timestamp": SNAP,
        "commence_time": COMMENCE, "home_team_raw": "Boston Celtics",
        "away_team_raw": "Miami Heat",
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    assert observation_content_hash(obs) == expected


def test_null_commence_time_hashes_distinctly_from_any_string() -> None:
    assert observation_content_hash(observation(commence_time=None)) != \
        observation_content_hash(observation(commence_time=COMMENCE))
    # And distinctly from the empty string, which is a different claim.
    with pytest.raises(ObservationValidationError):
        observation(commence_time="")


def test_the_hash_excludes_our_clocks_and_db_local_ids(
    db: sqlite3.Connection,
) -> None:
    """Same provider statement, different observed_at -> same hash, one row."""

    obs = observation()
    first = repo(db).record(obs, raw_response_id="raw_ok",
                            observed_at="2026-08-01T00:00:00.000000Z")
    second = repo(db).record(obs, raw_response_id="raw_ok",
                             observed_at="2026-09-01T00:00:00.000000Z")
    assert first.created is True
    assert second.created is False
    assert first.row.observation_content_hash == second.row.observation_content_hash
    assert second.row.observed_at == "2026-08-01T00:00:00.000000Z"


@pytest.mark.parametrize("field,value", [
    ("league_id", "lg_mlb"), ("provider", "other"), ("namespace_generation", "v5"),
    ("sport_key", "baseball_mlb"), ("provider_event_id", EVENT_B),
    ("requested_at_bucket", "2026-03-05T23:15:00.000000Z"),
    ("provider_snapshot_timestamp", "2026-03-05T23:00:00.000000Z"),
    ("commence_time", "2026-03-06T00:15:00.000000Z"), ("commence_time", None),
    ("home_team_raw", "Boston  Celtics"), ("away_team_raw", "Miami  Heat"),
])
def test_every_semantic_field_changes_the_hash(field: str, value: object) -> None:
    assert observation_content_hash(observation(**{field: value})) != \
        observation_content_hash(observation())


def test_observation_id_is_deterministic_and_prefixed() -> None:
    assert observation_id(observation()) == observation_id(observation())
    assert observation_id(observation()).startswith("hme_")
    assert observation_id(observation(provider_event_id=EVENT_B)) != \
        observation_id(observation())


# --------------------------------------------------------------------------- #
# Uniqueness: contradictions must survive
# --------------------------------------------------------------------------- #
def test_identical_evidence_replays_idempotently(db: sqlite3.Connection) -> None:
    r = repo(db)
    a = r.record(observation(), raw_response_id="raw_ok")
    b = r.record(observation(), raw_response_id="raw_ok")
    assert (a.created, b.created) == (True, False)
    assert db.execute("SELECT COUNT(*) c FROM "
                      "historical_market_event_observations").fetchone()["c"] == 1


def test_same_event_same_snapshot_different_content_both_survive(
    db: sqlite3.Connection,
) -> None:
    """The contradiction the audit exists to find must NOT be deduplicated."""

    r = repo(db)
    r.record(observation(home_team_raw="Boston Celtics"), raw_response_id="raw_ok")
    r.record(observation(home_team_raw="Miami Heat"), raw_response_id="raw_ok")

    rows = r.for_event(provider=ODDS, namespace_generation=GEN,
                       provider_event_id=EVENT_A)
    assert len(rows) == 2
    assert {row.home_team_raw for row in rows} == {"Boston Celtics", "Miami Heat"}


def test_a_changed_commence_time_is_preserved_not_overwritten(
    db: sqlite3.Connection,
) -> None:
    r = repo(db)
    r.record(observation(commence_time=COMMENCE), raw_response_id="raw_ok")
    r.record(observation(commence_time="2026-03-06T00:40:00.000000Z"),
             raw_response_id="raw_ok")
    rows = r.for_event(provider=ODDS, namespace_generation=GEN,
                       provider_event_id=EVENT_A)
    assert len(rows) == 2
    assert {row.commence_time for row in rows} == {
        COMMENCE, "2026-03-06T00:40:00.000000Z"}


def test_a_null_commence_time_observation_persists(db: sqlite3.Connection) -> None:
    stored = repo(db).record(observation(commence_time=None),
                             raw_response_id="raw_ok")
    assert stored.row.commence_time is None


def test_the_repository_exposes_no_mutation_methods() -> None:
    names = set(dir(SqliteMarketObservationRepository))
    for forbidden in ("update", "delete", "upsert", "merge", "set_game",
                      "link", "resolve"):
        assert forbidden not in names


# --------------------------------------------------------------------------- #
# Event-id format: reject, never repair
# --------------------------------------------------------------------------- #
_BAD_IDS = [
    EVENT_A.upper(),                    # uppercase hex
    EVENT_A[:-1] + chr(0x0435),         # Cyrillic small letter IE
    EVENT_A[:-1] + chr(0xff45),         # fullwidth latin small e
    chr(0x20) + EVENT_A,                # leading space
    EVENT_A + chr(0x20),                # trailing space
    EVENT_A + chr(0x200b),              # zero-width space
    EVENT_A[:-1],                       # 31 chars
    EVENT_A + "a",                      # 33 chars
    EVENT_A[:-1] + "g",                 # non-hex ASCII
    EVENT_A + chr(0x0a),                # trailing newline
]


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_the_domain_type_refuses_a_bad_event_id(bad: str) -> None:
    with pytest.raises(ObservationValidationError, match="lowercase 32-hex"):
        observation(provider_event_id=bad)


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_direct_sql_also_refuses_a_bad_event_id(
    db: sqlite3.Connection, bad: str,
) -> None:
    """The DB enforces the format too, so a direct writer cannot bypass it."""

    now = utc_now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO historical_market_event_observations (observation_id, "
            "league_id, provider, namespace_generation, sport_key, "
            "provider_event_id, requested_at_bucket, provider_snapshot_timestamp, "
            "commence_time, home_team_raw, away_team_raw, "
            "observation_content_hash, raw_response_id, observed_at, created_at) "
            "VALUES ('hme_x','lg_nba',?,?, 'basketball_nba', ?, ?, ?, NULL, 'H', "
            "'A', 'hash', 'raw_ok', ?, ?)",
            (ODDS, GEN, bad, BUCKET, SNAP, now, now))


def test_a_lookalike_id_is_refused_by_the_read_path_too(
    db: sqlite3.Connection,
) -> None:
    with pytest.raises(ObservationValidationError):
        repo(db).for_event(provider=ODDS, namespace_generation=GEN,
                           provider_event_id=EVENT_A.upper())


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", [
    "requested_at_bucket", "provider_snapshot_timestamp", "commence_time"])
@pytest.mark.parametrize("bad", [
    "2026-03-05T23:10:00Z",             # no microseconds
    "2026-03-05T23:10:00.000000",       # naive: no Z
    "2026-03-05T23:10:00.000000+00:00",  # offset-bearing
    "2026-03-05T23:10:00.000000z",      # lowercase z
    "2026-02-30T23:10:00.000000Z",      # impossible calendar day
    "2026-99-05T23:10:00.000000Z",      # month 99
    "2026-03-05T24:00:00.000000Z",      # hour 24
    "not-a-time",
])
def test_malformed_timestamps_are_refused(field: str, bad: str) -> None:
    with pytest.raises(ObservationValidationError):
        observation(**{field: bad})


def test_direct_sql_refuses_a_malformed_snapshot_timestamp(
    db: sqlite3.Connection,
) -> None:
    now = utc_now_iso()
    with pytest.raises(sqlite3.IntegrityError, match="real instant"):
        db.execute(
            "INSERT INTO historical_market_event_observations (observation_id, "
            "league_id, provider, namespace_generation, sport_key, "
            "provider_event_id, requested_at_bucket, provider_snapshot_timestamp, "
            "commence_time, home_team_raw, away_team_raw, "
            "observation_content_hash, raw_response_id, observed_at, created_at) "
            "VALUES ('hme_x','lg_nba',?,?, 'basketball_nba', ?, ?, "
            "'2026-02-30T23:05:00.000000Z', NULL, 'H','A','hash','raw_ok', ?, ?)",
            (ODDS, GEN, EVENT_A, BUCKET, now, now))


def test_the_requested_bucket_and_the_snapshot_instant_stay_distinct(
    db: sqlite3.Connection,
) -> None:
    stored = repo(db).record(observation(), raw_response_id="raw_ok")
    assert stored.row.requested_at_bucket == BUCKET
    assert stored.row.provider_snapshot_timestamp == SNAP
    assert stored.row.requested_at_bucket != stored.row.provider_snapshot_timestamp
    # Neither of our clocks was backdated to a provider instant.
    assert stored.row.observed_at > SNAP
    assert stored.row.created_at > SNAP


# --------------------------------------------------------------------------- #
# Referential integrity and raw-response binding
# --------------------------------------------------------------------------- #
def test_a_nonexistent_league_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(RepositoryError):
        repo(db).record(observation(league_id="lg_nope"), raw_response_id="raw_ok")


def test_a_nonexistent_raw_response_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(RepositoryError):
        repo(db).record(observation(), raw_response_id="raw_nope")


def test_a_raw_response_from_another_provider_is_refused(
    db: sqlite3.Connection,
) -> None:
    with pytest.raises(RepositoryError, match="different provider"):
        repo(db).record(observation(), raw_response_id="raw_other")


def test_a_failed_raw_response_cannot_become_evidence(
    db: sqlite3.Connection,
) -> None:
    """A 500 is not evidence that a market did or did not exist."""

    with pytest.raises(RepositoryError, match="non-200"):
        repo(db).record(observation(), raw_response_id="raw_500")


# --------------------------------------------------------------------------- #
# Append-only, via direct SQL
# --------------------------------------------------------------------------- #
def test_update_is_refused_by_the_database(db: sqlite3.Connection) -> None:
    repo(db).record(observation(), raw_response_id="raw_ok")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE historical_market_event_observations "
                   "SET home_team_raw = 'tampered'")


def test_delete_is_refused_by_the_database(db: sqlite3.Connection) -> None:
    repo(db).record(observation(), raw_response_id="raw_ok")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM historical_market_event_observations")


# --------------------------------------------------------------------------- #
# Source-corpus digest: old corpora must be byte-identical
# --------------------------------------------------------------------------- #
def test_official_providers_keep_the_exact_three_table_audited_set() -> None:
    assert sources.audited_source_tables("balldontlie") == (
        sources.AUDITED_SOURCE_TABLES)
    assert set(sources.AUDITED_SOURCE_TABLES) == {
        "game_schedule_snapshots",
        "provider_player_identity_snapshots",
        "provider_team_identity_snapshots",
    }


def test_the_market_table_is_in_the_linking_set_only() -> None:
    assert sources.LINKING_SOURCE_TABLES == (
        "historical_market_event_observations",)
    assert "historical_market_event_observations" not in \
        sources.AUDITED_SOURCE_TABLES


def test_the_linking_digest_excludes_the_db_local_raw_response_id() -> None:
    columns = sources.digest_columns_for("historical_market_event_observations")
    assert "raw_response_id" not in columns
    assert "observation_content_hash" in columns
    assert "observation_id" not in columns
    assert "created_at" not in columns
    assert "observed_at" not in columns


def test_an_official_corpus_digest_is_unchanged_by_market_rows(
    db: sqlite3.Connection,
) -> None:
    """The load-bearing compatibility proof: v19 digests survive v20 byte-identical."""

    before = sources.source_corpus_digest(
        db, league_id="lg_nba", provider="balldontlie")
    repo(db).record(observation(), raw_response_id="raw_ok")
    repo(db).record(observation(provider_event_id=EVENT_B),
                    raw_response_id="raw_ok")
    after = sources.source_corpus_digest(
        db, league_id="lg_nba", provider="balldontlie")
    assert before == after


def test_source_corpus_digest_still_refuses_an_unregistered_provider(
    db: sqlite3.Connection,
) -> None:
    """No linking provider is registered at v20; the branch stays unreachable."""

    with pytest.raises(sources.SourceCorpusError):
        sources.source_corpus_digest(db, league_id="lg_nba", provider=ODDS)


def test_insertion_order_does_not_change_the_stored_evidence_set(
    tmp_path: Path,
) -> None:
    """Two databases, opposite insertion orders, identical observation id sets."""

    def build(order: list[str]) -> set[str]:
        path = tmp_path / f"order_{order[0]}.db"
        Database(path).migrate()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        now = utc_now_iso()
        conn.execute("INSERT INTO leagues (league_id, code, name, sport, "
                     "created_at, updated_at) VALUES "
                     "('lg_nba','NBA','NBA','basketball',?,?)", (now, now))
        conn.execute(
            "INSERT INTO ingestion_runs (run_id, command, provider, operation, "
            "args_json, status, requested_at, started_at, started_monotonic_ns, "
            "requests_made, records_received, records_normalized, "
            "records_inserted, records_deduplicated, records_rejected, "
            "records_updated, tool_version, created_at) VALUES "
            "('run_1','x',?, 'op','{}','started',?,?,1,0,0,0,0,0,0,0,'v',?)",
            (ODDS, now, now, now))
        conn.execute(
            "INSERT INTO raw_responses (raw_response_id, run_id, provider, "
            "endpoint, request_params_json, http_status, response_headers_json, "
            "body, body_bytes, body_hash, content_hash, requested_at, "
            "received_at, elapsed_ns, created_at) VALUES ('raw_ok','run_1',?, "
            "'/e','{}',200,'{}','[]',2,'h','c',?,?,1,?)", (ODDS, now, now, now))
        conn.commit()
        r = SqliteMarketObservationRepository(conn)
        for event in order:
            r.record(observation(provider_event_id=event), raw_response_id="raw_ok")
        return {row.observation_id for row in r.for_namespace(
            provider=ODDS, namespace_generation=GEN, league_id="lg_nba")}

    assert build([EVENT_A, EVENT_B]) == build([EVENT_B, EVENT_A])


# --------------------------------------------------------------------------- #
# Non-regression: authority, strict PIT, identity
# --------------------------------------------------------------------------- #
def test_v20_grants_the_odds_api_no_provider_authority() -> None:
    from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE
    from sports_quant.retrospective.namespaces import QUALIFIED_PROVIDERS
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS

    assert ODDS not in OFFICIAL_PROVIDER_BY_LEAGUE.values()
    assert ODDS not in sources.PROVIDER_LEAGUES
    assert ODDS not in ATTESTED_GENERATIONS
    assert not any(ODDS in key for key in QUALIFIED_PROVIDERS)
    assert set(OFFICIAL_PROVIDER_BY_LEAGUE.values()) == {
        "mlb_statsapi", "balldontlie"}


def test_no_linking_namespace_registry_was_added() -> None:
    assert not hasattr(sources, "LINKING_NAMESPACES")


def test_writing_an_observation_creates_no_canonical_game(
    db: sqlite3.Connection,
) -> None:
    before = db.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
    refs = db.execute(
        "SELECT COUNT(*) c FROM provider_game_references").fetchone()["c"]
    repo(db).record(observation(), raw_response_id="raw_ok")
    assert db.execute("SELECT COUNT(*) c FROM games").fetchone()["c"] == before
    assert db.execute(
        "SELECT COUNT(*) c FROM provider_game_references").fetchone()["c"] == refs
    assert db.execute(
        "SELECT COUNT(*) c FROM static_crosswalk_provenance").fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM identity_audit_records").fetchone()["c"] == 0


def test_the_observation_table_is_not_an_asof_reader_source() -> None:
    """Lane-R evidence must not become readable as a Lane-L feature."""

    from sports_quant.pit import asof

    source = Path(asof.__file__).read_text(encoding="utf-8")
    assert "historical_market_event_observations" not in source
