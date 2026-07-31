"""Tests for the shared identity recorder and the offline corpus replay path.

These cover the seam both ingestors use, so "dry-run agrees with persistence" and
"replaying a corpus is idempotent and order-independent" are properties of the
production code rather than of a bespoke harness.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import DbInitResult, initialize_database
from sports_quant.db.repositories.identity import SqliteProviderIdentityRepository
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.ingest.identity_record import (
    DQ_IDENTITY_EQUAL_TIME,
    DQ_IDENTITY_MISSING_NAME,
    REPLAY_ORDERS,
    IdentityRecorder,
    replay_identities_from_corpus,
)

from .test_identity_pilot_fixtures import (
    MLB_BOXSCORE,
    MLB_LINESCORE,
    MLB_ROSTER,
    MLB_SCHEDULE,
    NBA_BOX_SCORES,
    NBA_GAMES,
    NBA_LINEUPS,
    NBA_STATS,
)

MLB = "mlb_statsapi"
NBA = "balldontlie"
MLB_CORPUS = (
    ("/schedule", MLB_SCHEDULE, "2026-07-20T18:00:00.000000Z"),
    ("/game/822788/boxscore", MLB_BOXSCORE, "2026-07-20T18:00:01.000000Z"),
    ("/game/822788/linescore", MLB_LINESCORE, "2026-07-20T18:00:02.000000Z"),
    ("/teams/141/roster", MLB_ROSTER, "2026-07-20T18:00:03.000000Z"),
)
NBA_CORPUS = (
    ("/v1/games", NBA_GAMES, "2026-01-05T18:00:00.000000Z"),
    ("/v1/box_scores", NBA_BOX_SCORES, "2026-01-05T18:00:01.000000Z"),
    ("/v1/stats", NBA_STATS, "2026-01-05T18:00:02.000000Z"),
    ("/v1/lineups", NBA_LINEUPS, "2026-01-05T18:00:03.000000Z"),
)


@pytest.fixture()
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "corpus.db"
    result: DbInitResult = initialize_database(db_path)
    assert result.schema_version >= 17
    database = Database(db_path)
    with database.connection() as connection:
        yield connection


def _store(
    conn: sqlite3.Connection, provider: str, endpoint: str, body: str, observed_at: str
) -> tuple[str, str]:
    runs = SqliteIngestionRunRepository(conn)
    with transaction(conn):
        run = runs.start(command="seed", provider=provider, operation="seed",
                         args_json="{}", started_monotonic_ns=0, tool_version="t")
        content_hash = response_content_hash(
            provider=provider, endpoint=endpoint, request_params={}, body=body)
        raw = SqliteRawResponseRepository(conn).store(
            run_id=run.run_id, provider=provider, endpoint=endpoint,
            request_params_json="{}", http_status=200, response_headers_json="{}",
            requested_at=observed_at, received_at=observed_at, elapsed_ns=1, body=body,
            content_hash=content_hash)
    return raw.raw_response_id, content_hash


def _load(conn: sqlite3.Connection, corpus: tuple, provider: str) -> None:
    for endpoint, body, observed_at in corpus:
        _store(conn, provider, endpoint, body, observed_at)


def _identity_rows(conn: sqlite3.Connection) -> tuple[list, list]:
    teams = [tuple(r) for r in conn.execute(
        "SELECT provider, provider_team_id, full_name, abbreviation, city, nickname, "
        "observed_at, content_hash FROM provider_team_identity_snapshots "
        "ORDER BY provider, provider_team_id, observed_at, content_hash")]
    players = [tuple(r) for r in conn.execute(
        "SELECT provider, provider_player_id, full_name, first_name, last_name, suffix, "
        "birth_date, position, provider_team_id, observed_at, content_hash "
        "FROM provider_player_identity_snapshots "
        "ORDER BY provider, provider_player_id, observed_at, content_hash")]
    return teams, players


# --------------------------------------------------------------------------- #
def test_dry_run_counts_equal_persisted_counts(conn: sqlite3.Connection) -> None:
    _load(conn, MLB_CORPUS, MLB)
    dry = IdentityRecorder(dry_run=True)
    for endpoint, body, observed_at in MLB_CORPUS:
        dry.observe_response(provider=MLB, endpoint=endpoint, body=body,
                             raw_response_id="dry", raw_response_hash="dry",
                             observed_at=observed_at)
    persisted = replay_identities_from_corpus(conn, provider=MLB)
    assert dry.counts.team_identities_inserted == persisted.team_identities_inserted
    assert dry.counts.player_identities_inserted == persisted.player_identities_inserted
    assert dry.counts.identities_rejected == persisted.identities_rejected
    assert persisted.team_identities_inserted == SqliteProviderIdentityRepository(
        conn).count_teams()
    assert persisted.player_identities_inserted == SqliteProviderIdentityRepository(
        conn).count_players()


def test_replay_is_idempotent(conn: sqlite3.Connection) -> None:
    _load(conn, NBA_CORPUS, NBA)
    first = replay_identities_from_corpus(conn, provider=NBA)
    before = _identity_rows(conn)
    second = replay_identities_from_corpus(conn, provider=NBA)
    assert first.team_identities_inserted > 0 and first.player_identities_inserted > 0
    assert second.team_identities_inserted == 0
    assert second.player_identities_inserted == 0
    assert _identity_rows(conn) == before


@pytest.mark.parametrize("order", REPLAY_ORDERS)
def test_replay_order_does_not_change_the_stored_rows(
    tmp_path, order: str,
) -> None:
    """Every supported traversal order must converge on identical rows."""

    def build(name: str, replay_order: str) -> tuple[list, list]:
        db_path = tmp_path / f"{name}.db"
        initialize_database(db_path)
        with Database(db_path).connection() as connection:
            _load(connection, MLB_CORPUS, MLB)
            replay_identities_from_corpus(connection, provider=MLB, order=replay_order)
            return _identity_rows(connection)

    baseline = build(f"base_{order}", "received")
    variant = build(f"variant_{order}", order)
    assert variant == baseline


def test_unknown_replay_order_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown replay order"):
        replay_identities_from_corpus(conn, provider=MLB, order="whatever")


def test_only_successful_responses_are_replayed(conn: sqlite3.Connection) -> None:
    """An error body is not an identity observation."""

    runs = SqliteIngestionRunRepository(conn)
    with transaction(conn):
        run = runs.start(command="seed", provider=MLB, operation="seed", args_json="{}",
                         started_monotonic_ns=0, tool_version="t")
        SqliteRawResponseRepository(conn).store(
            run_id=run.run_id, provider=MLB, endpoint="/schedule",
            request_params_json="{}", http_status=503, response_headers_json="{}",
            requested_at="2026-07-20T18:00:00.000000Z",
            received_at="2026-07-20T18:00:00.000000Z", elapsed_ns=1, body=MLB_SCHEDULE,
            content_hash=response_content_hash(provider=MLB, endpoint="/schedule",
                                               request_params={}, body="err"))
    counts = replay_identities_from_corpus(conn, provider=MLB)
    assert counts.team_identities_inserted == 0
    assert SqliteProviderIdentityRepository(conn).count_teams() == 0


def test_nameless_entity_raises_a_dq_note_and_is_counted(
    conn: sqlite3.Connection,
) -> None:
    _load(conn, MLB_CORPUS, MLB)
    counts = replay_identities_from_corpus(conn, provider=MLB)
    # The roster fixture carries one deliberately nameless person.
    assert counts.identities_rejected == 1
    note = conn.execute(
        "SELECT severity, rule_code, entity_type, entity_id, description "
        "FROM data_quality_issues WHERE rule_code = ?",
        (DQ_IDENTITY_MISSING_NAME,)).fetchone()
    assert note["severity"] == "note"
    assert note["entity_type"] == "player"
    assert note["entity_id"] == "999999"
    assert "no usable fullName" in note["description"]
    # The note must not leak a body, a header or a credential.
    low = note["description"].lower()
    for bad in ("api_key", "authorization", "bearer", "http", "{"):
        assert bad not in low


def test_equal_time_conflict_raises_a_dq_issue(conn: sqlite3.Connection) -> None:
    """Two different names for one entity at one instant must be visible."""

    same_time = "2026-07-20T18:00:00.000000Z"
    rid, rhash = _store(conn, MLB, "/schedule", MLB_SCHEDULE, same_time)
    repo = SqliteProviderIdentityRepository(conn)
    recorder = IdentityRecorder(conn=conn)
    with transaction(conn):
        for name in ("Toronto Blue Jays", "Toronto Bluejays"):
            repo.record_team(provider=MLB, provider_team_id="141", league_id="lg_mlb",
                             full_name=name, observed_at=same_time,
                             raw_response_id=rid, raw_response_hash=rhash)
    with transaction(conn):
        found = recorder.report_equal_time_conflicts(MLB)
    assert found == 1
    issue = conn.execute(
        "SELECT severity, entity_type, entity_id, description FROM data_quality_issues "
        "WHERE rule_code = ?", (DQ_IDENTITY_EQUAL_TIME,)).fetchone()
    assert issue["severity"] == "issue"
    assert issue["entity_id"] == "141"
    assert "conflicting provider identity names" in issue["description"]
    # And the answer is still deterministic, not a coin flip.
    picked_first = repo.latest_team(MLB, "141")
    picked_again = repo.latest_team(MLB, "141")
    assert picked_first is not None and picked_again is not None
    assert picked_first.full_name == picked_again.full_name


def test_an_unsupported_provider_records_nothing(conn: sqlite3.Connection) -> None:
    recorder = IdentityRecorder(conn=conn)
    counts = recorder.observe_response(
        provider="the_odds_api", endpoint="/v4/sports", body="{}",
        raw_response_id="x", raw_response_hash="y",
        observed_at="2026-07-20T18:00:00.000000Z")
    assert counts.team_identities_inserted == 0
    assert counts.player_identities_inserted == 0
    assert counts.identity_endpoints_unsupported == 1


def test_identity_rows_reference_the_exact_response_that_supplied_them(
    conn: sqlite3.Connection,
) -> None:
    _load(conn, MLB_CORPUS, MLB)
    replay_identities_from_corpus(conn, provider=MLB)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM provider_player_identity_snapshots i "
        "LEFT JOIN raw_responses r ON r.raw_response_id = i.raw_response_id "
        "WHERE r.raw_response_id IS NULL").fetchone()[0]
    assert orphans == 0
    # The box-score position came from the box-score response, not the schedule.
    row = conn.execute(
        "SELECT r.endpoint FROM provider_player_identity_snapshots i "
        "JOIN raw_responses r ON r.raw_response_id = i.raw_response_id "
        "WHERE i.provider_player_id = '670764' AND i.position = 'SS' LIMIT 1").fetchone()
    assert row is not None and "boxscore" in row["endpoint"]
