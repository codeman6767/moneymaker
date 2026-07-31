"""Adversarial tests for e017 provider identity observations.

Offline only: nothing here opens a socket. The properties under test are the ones
the F1 matching repair depends on -- structured extraction from the real payload
shapes, honest handling of a missing name, append-only history, and a latest-as-of
answer that never depends on insertion order.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sports_quant.db.engine import transaction
from sports_quant.db.models import ProviderPlayerIdentity, ProviderTeamIdentity
from sports_quant.db.repositories.base import RepositoryError
from sports_quant.db.repositories.identity import (
    SqliteProviderIdentityRepository,
    prepare_player_identity,
    prepare_team_identity,
)
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.observations import ObservationOutcome
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.db.schema import to_iso, utc_now
from sports_quant.ingest.identity_extract import (
    extract_identities,
    mlb_player_identity,
    mlb_team_identity,
    nba_player_identity,
    nba_team_identity,
)

T1 = "2026-07-20T18:00:00.000000Z"
T2 = "2026-07-20T19:00:00.000000Z"


def _raw(conn: sqlite3.Connection, marker: str = "identity") -> tuple[str, str]:
    runs = SqliteIngestionRunRepository(conn)
    run = runs.start(command="seed", provider="test", operation="seed", args_json="{}",
                     started_monotonic_ns=0, tool_version="t")
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run.run_id, provider="test", endpoint="/seed", request_params_json="{}",
        http_status=200, response_headers_json="{}", requested_at=to_iso(utc_now()),
        received_at=to_iso(utc_now()), elapsed_ns=1, body="{}",
        content_hash=response_content_hash(
            provider="test", endpoint="/seed", request_params={}, body=marker),
    )
    return raw.raw_response_id, raw.content_hash


def _record_team(
    conn: sqlite3.Connection, **kw: object
) -> tuple[ProviderTeamIdentity, ObservationOutcome]:
    rid, rhash = _raw(conn, marker=f"team:{kw.get('full_name')}:{kw.get('observed_at')}")
    repo = SqliteProviderIdentityRepository(conn)
    with transaction(conn):
        return repo.record_team(
            provider="mlb_statsapi", league_id="lg_mlb", raw_response_id=rid,
            raw_response_hash=rhash, **kw)  # type: ignore[arg-type]


def _record_player(
    conn: sqlite3.Connection, **kw: object
) -> tuple[ProviderPlayerIdentity, ObservationOutcome]:
    rid, rhash = _raw(conn, marker=f"player:{kw.get('full_name')}:{kw.get('observed_at')}")
    repo = SqliteProviderIdentityRepository(conn)
    with transaction(conn):
        return repo.record_player(
            provider="mlb_statsapi", league_id="lg_mlb", raw_response_id=rid,
            raw_response_hash=rhash, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Structured extraction from the real payload shapes
# --------------------------------------------------------------------------- #
def test_mlb_schedule_team_identity_is_structured() -> None:
    body = json.dumps({"dates": [{"games": [{"teams": {
        "home": {"team": {"id": 141, "name": "Toronto Blue Jays"}},
        "away": {"team": {"id": 139, "name": "Tampa Bay Rays"}},
    }}]}]})
    out = extract_identities(provider="mlb_statsapi", endpoint="/schedule", body=body)
    assert [(t.provider_team_id, t.full_name) for t in out.teams] == [
        ("139", "Tampa Bay Rays"), ("141", "Toronto Blue Jays")]
    # The thin schedule shape supplies no parts, so none are invented.
    assert all(t.abbreviation is None and t.city is None and t.nickname is None
               for t in out.teams)


def test_mlb_boxscore_supplies_city_nickname_abbreviation_when_present() -> None:
    team = mlb_team_identity({
        "id": 141, "name": "Toronto Blue Jays", "abbreviation": "TOR",
        "locationName": "Toronto", "teamName": "Blue Jays"})
    assert team is not None
    assert (team.abbreviation, team.city, team.nickname) == ("TOR", "Toronto", "Blue Jays")


def test_mlb_player_identity_keeps_full_name_and_never_splits_it() -> None:
    identity = mlb_player_identity(
        {"id": 670764, "fullName": "Bo Bichette"},
        position={"abbreviation": "SS", "name": "Shortstop"}, provider_team_id="141")
    assert identity is not None
    assert identity.full_name == "Bo Bichette"
    # StatsAPI sends no parts: splitting the full name would be a guess.
    assert identity.first_name is None and identity.last_name is None
    assert identity.position == "SS" and identity.provider_team_id == "141"
    assert identity.birth_date is None


def test_nba_team_and_player_identity_use_supplied_parts() -> None:
    team = nba_team_identity({"id": 20, "full_name": "New York Knicks", "city": "New York",
                              "name": "Knicks", "abbreviation": "NYK"})
    assert team is not None
    assert (team.full_name, team.city, team.nickname, team.abbreviation) == (
        "New York Knicks", "New York", "Knicks", "NYK")
    player = nba_player_identity({"id": 100, "first_name": "Jordan", "last_name": "Clarkson",
                                  "position": "G", "team_id": 20})
    assert player is not None
    assert (player.full_name, player.first_name, player.last_name) == (
        "Jordan Clarkson", "Jordan", "Clarkson")
    # BALLDONTLIE never supplies a birth date; one is never fabricated.
    assert player.birth_date is None


def test_nba_plays_yield_teams_but_no_players() -> None:
    """Plays carry bare integer participants -- an id is not an identity."""

    body = json.dumps({"data": [{
        "team": {"id": 9, "full_name": "Detroit Pistons", "city": "Detroit",
                 "name": "Pistons", "abbreviation": "DET"},
        "participants": [17896033, 3547267]}]})
    out = extract_identities(provider="balldontlie", endpoint="/v1/plays", body=body)
    assert [t.provider_team_id for t in out.teams] == ["9"]
    assert out.players == []


def test_nba_flat_lineup_row_yields_the_same_identity_as_the_stats_row() -> None:
    """A lineup row and a stats row naming one player must not create two identities."""

    player = {"id": 100, "first_name": "Jordan", "last_name": "Clarkson",
              "position": "G", "team_id": 20}
    team = {"id": 20, "full_name": "New York Knicks", "city": "New York",
            "name": "Knicks", "abbreviation": "NYK"}
    lineup = extract_identities(
        provider="balldontlie", endpoint="/v1/lineups",
        body=json.dumps({"data": [{"game_id": 1, "id": 7, "player": player,
                                   "team": team, "position": "G", "starter": True}]}))
    stats = extract_identities(
        provider="balldontlie", endpoint="/v1/stats",
        body=json.dumps({"data": [{"player": player, "team": team, "pts": 10}]}))
    assert lineup.players == stats.players
    assert prepare_player_identity(
        provider="balldontlie", provider_player_id=lineup.players[0].provider_player_id,
        league_id="lg_nba", full_name=lineup.players[0].full_name,
        first_name=lineup.players[0].first_name, last_name=lineup.players[0].last_name,
        position=lineup.players[0].position,
        provider_team_id=lineup.players[0].provider_team_id,
    ).content_hash == prepare_player_identity(
        provider="balldontlie", provider_player_id=stats.players[0].provider_player_id,
        league_id="lg_nba", full_name=stats.players[0].full_name,
        first_name=stats.players[0].first_name, last_name=stats.players[0].last_name,
        position=stats.players[0].position,
        provider_team_id=stats.players[0].provider_team_id,
    ).content_hash


def test_richest_observation_wins_within_one_response() -> None:
    """A thin and a rich shape in one document must not depend on traversal order."""

    rich = {"id": 141, "name": "Toronto Blue Jays", "abbreviation": "TOR",
            "locationName": "Toronto", "teamName": "Blue Jays"}
    thin = {"id": 141, "name": "Toronto Blue Jays"}
    body_a = json.dumps({"teams": {"away": {"team": thin, "players": {}},
                                   "home": {"team": rich, "players": {}}}})
    body_b = json.dumps({"teams": {"away": {"team": rich, "players": {}},
                                   "home": {"team": thin, "players": {}}}})
    a = extract_identities(provider="mlb_statsapi", endpoint="/game/1/boxscore", body=body_a)
    b = extract_identities(provider="mlb_statsapi", endpoint="/game/1/boxscore", body=body_b)
    assert a.teams == b.teams
    assert a.teams[0].abbreviation == "TOR"


# --------------------------------------------------------------------------- #
# Missing / malformed names are surfaced, never invented
# --------------------------------------------------------------------------- #
def test_missing_name_yields_no_identity_and_is_reported() -> None:
    body = json.dumps({"roster": [{"person": {"id": 999}}], "teamId": 141})
    out = extract_identities(provider="mlb_statsapi", endpoint="/teams/141/roster", body=body)
    assert out.players == []
    assert [(r.kind, r.provider_entity_id) for r in out.rejected] == [("player", "999")]


def test_a_provider_id_is_never_used_as_a_name() -> None:
    assert mlb_player_identity({"id": 518886, "fullName": ""}) is None
    assert mlb_team_identity({"id": 141, "name": "   "}) is None
    assert nba_player_identity({"id": 100, "first_name": "", "last_name": ""}) is None


def test_numeric_name_field_is_not_accepted_as_a_name() -> None:
    """A provider that puts a number where a name belongs has supplied no name."""

    assert mlb_team_identity({"id": 141, "name": 141}) is None


def test_unknown_endpoint_family_is_fail_closed() -> None:
    out = extract_identities(provider="mlb_statsapi", endpoint="/some/new/thing", body="{}")
    assert out.teams == [] and out.players == []
    assert [r.kind for r in out.rejected] == ["endpoint"]


def test_malformed_body_is_reported_not_raised() -> None:
    out = extract_identities(provider="balldontlie", endpoint="/v1/games", body="not json")
    assert out.teams == [] and out.players == []
    assert [r.kind for r in out.rejected] == ["body"]


def test_repository_refuses_an_empty_name(conn: sqlite3.Connection) -> None:
    with pytest.raises(RepositoryError, match="empty full_name"):
        _record_team(conn, provider_team_id="141", full_name="  ", observed_at=T1)
    with pytest.raises(RepositoryError, match="empty full_name"):
        _record_player(conn, provider_player_id="1", full_name="", observed_at=T1)


def test_repository_refuses_a_name_that_normalizes_to_nothing(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(RepositoryError, match="normalizes to"):
        _record_team(conn, provider_team_id="141", full_name="...", observed_at=T1)


def test_empty_optional_strings_become_null_not_blank(conn: sqlite3.Connection) -> None:
    identity, _ = _record_team(conn, provider_team_id="141", full_name="Toronto Blue Jays",
                               abbreviation="", city="  ", nickname="Blue Jays",
                               observed_at=T1)
    assert identity.abbreviation is None and identity.city is None
    assert identity.nickname == "Blue Jays"


# --------------------------------------------------------------------------- #
# Provenance, append-only history, idempotency, order independence
# --------------------------------------------------------------------------- #
def test_identity_carries_raw_response_provenance(conn: sqlite3.Connection) -> None:
    identity, outcome = _record_team(conn, provider_team_id="141",
                                     full_name="Toronto Blue Jays", observed_at=T1)
    assert outcome is ObservationOutcome.INSERTED
    row = conn.execute(
        "SELECT r.raw_response_id FROM provider_team_identity_snapshots i "
        "JOIN raw_responses r ON r.raw_response_id = i.raw_response_id "
        "WHERE i.identity_id = ?", (identity.identity_id,)).fetchone()
    assert row is not None
    assert identity.content_hash and identity.raw_response_hash


def test_replaying_identical_content_and_time_is_idempotent(
    conn: sqlite3.Connection,
) -> None:
    _record_team(conn, provider_team_id="141", full_name="Toronto Blue Jays",
                 observed_at=T1)
    _identity, outcome = _record_team(conn, provider_team_id="141",
                                      full_name="Toronto Blue Jays", observed_at=T1)
    assert outcome is ObservationOutcome.UNCHANGED
    assert SqliteProviderIdentityRepository(conn).count_teams() == 1


def test_changed_name_appends_and_does_not_rewrite_history(
    conn: sqlite3.Connection,
) -> None:
    _record_team(conn, provider_team_id="114", full_name="Cleveland Indians",
                 observed_at=T1)
    _record_team(conn, provider_team_id="114", full_name="Cleveland Guardians",
                 observed_at=T2)
    repo = SqliteProviderIdentityRepository(conn)
    assert repo.count_teams() == 2
    # Both the old and the new name remain answerable, each as of its own time.
    early = repo.latest_team("mlb_statsapi", "114", as_of=T1)
    latest = repo.latest_team("mlb_statsapi", "114")
    assert early is not None and latest is not None
    assert early.full_name == "Cleveland Indians"
    assert latest.full_name == "Cleveland Guardians"


def test_identity_table_is_append_only(conn: sqlite3.Connection) -> None:
    identity, _ = _record_team(conn, provider_team_id="141",
                               full_name="Toronto Blue Jays", observed_at=T1)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with transaction(conn):
            conn.execute("UPDATE provider_team_identity_snapshots SET full_name = ? "
                         "WHERE identity_id = ?", ("Wrong", identity.identity_id))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with transaction(conn):
            conn.execute("DELETE FROM provider_team_identity_snapshots WHERE identity_id = ?",
                         (identity.identity_id,))


def test_latest_does_not_depend_on_insertion_order(conn: sqlite3.Connection) -> None:
    """The same observations inserted in reverse order give the same latest answer."""

    _record_team(conn, provider_team_id="114", full_name="Cleveland Guardians",
                 observed_at=T2)
    _record_team(conn, provider_team_id="114", full_name="Cleveland Indians",
                 observed_at=T1)
    repo = SqliteProviderIdentityRepository(conn)
    latest = repo.latest_team("mlb_statsapi", "114")
    early = repo.latest_team("mlb_statsapi", "114", as_of=T1)
    assert latest is not None and early is not None
    assert latest.full_name == "Cleveland Guardians"
    assert early.full_name == "Cleveland Indians"


def test_same_content_at_two_times_keeps_both_observations(
    conn: sqlite3.Connection,
) -> None:
    """Keying uniqueness on content alone would make the stored time order-dependent."""

    _record_team(conn, provider_team_id="141", full_name="Toronto Blue Jays",
                 observed_at=T1)
    _record_team(conn, provider_team_id="141", full_name="Toronto Blue Jays",
                 observed_at=T2)
    repo = SqliteProviderIdentityRepository(conn)
    assert repo.count_teams() == 2
    early = repo.latest_team("mlb_statsapi", "141", as_of=T1)
    latest = repo.latest_team("mlb_statsapi", "141")
    assert early is not None and latest is not None
    assert early.observed_at == T1
    assert latest.observed_at == T2


def test_equal_time_conflict_resolves_deterministically_and_is_reported(
    conn: sqlite3.Connection,
) -> None:
    """Two different names at one instant: a stable answer AND a visible conflict."""

    _record_team(conn, provider_team_id="141", full_name="Toronto Blue Jays",
                 observed_at=T1)
    _record_team(conn, provider_team_id="141", full_name="Toronto Bluejays",
                 observed_at=T1)
    repo = SqliteProviderIdentityRepository(conn)
    conflicts = repo.equal_time_conflicts("team")
    assert [(c.provider_entity_id, c.observed_at, c.contents) for c in conflicts] == [
        ("141", T1, 2)]
    # Deterministic: the content-hash tie-break, never rowid order.
    picked_first = repo.latest_team("mlb_statsapi", "141")
    picked_again = repo.latest_team("mlb_statsapi", "141")
    assert picked_first is not None and picked_again is not None
    assert picked_first.full_name == picked_again.full_name
    hashes = [r[0] for r in conn.execute(
        "SELECT content_hash FROM provider_team_identity_snapshots "
        "WHERE provider_team_id = '141' ORDER BY content_hash DESC")]
    picked = conn.execute(
        "SELECT content_hash FROM provider_team_identity_snapshots "
        "WHERE provider_team_id = '141' ORDER BY observed_at DESC, content_hash DESC "
        "LIMIT 1").fetchone()[0]
    assert picked == hashes[0]


def test_as_of_never_uses_a_future_observation(conn: sqlite3.Connection) -> None:
    _record_team(conn, provider_team_id="114", full_name="Cleveland Guardians",
                 observed_at=T2)
    repo = SqliteProviderIdentityRepository(conn)
    assert repo.latest_team("mlb_statsapi", "114", as_of=T1) is None


def test_suffix_is_split_and_stored_separately(conn: sqlite3.Connection) -> None:
    identity, _ = _record_player(conn, provider_player_id="665489",
                                 full_name="Vladimir Guerrero Jr.", observed_at=T1)
    assert identity.suffix == "jr"
    assert identity.normalized_name == "vladimir guerrero"
    assert identity.full_name == "Vladimir Guerrero Jr."


def test_suffix_makes_two_similar_names_distinct_content() -> None:
    senior = prepare_player_identity(
        provider="mlb_statsapi", provider_player_id="1", league_id="lg_mlb",
        full_name="Vladimir Guerrero")
    junior = prepare_player_identity(
        provider="mlb_statsapi", provider_player_id="2", league_id="lg_mlb",
        full_name="Vladimir Guerrero Jr.")
    assert senior.suffix == "" and junior.suffix == "jr"
    assert senior.content_hash != junior.content_hash


def test_prepared_hash_excludes_observed_at() -> None:
    """Two observations of one identity share a content hash; time is not hashed."""

    a = prepare_team_identity(provider="mlb_statsapi", provider_team_id="141",
                              league_id="lg_mlb", full_name="Toronto Blue Jays")
    b = prepare_team_identity(provider="mlb_statsapi", provider_team_id="141",
                              league_id="lg_mlb", full_name="Toronto Blue Jays")
    assert a.content_hash == b.content_hash


def test_identity_rows_carry_no_secret_or_credential(conn: sqlite3.Connection) -> None:
    _record_team(conn, provider_team_id="141", full_name="Toronto Blue Jays",
                 observed_at=T1)
    _record_player(conn, provider_player_id="670764", full_name="Bo Bichette",
                   observed_at=T1)
    for table in ("provider_team_identity_snapshots", "provider_player_identity_snapshots"):
        columns = {r[1].lower() for r in conn.execute(f"PRAGMA table_info({table})")}
        assert not {c for c in columns if any(
            bad in c for bad in ("key", "auth", "token", "secret", "header", "url",
                                 "password"))}
        blob = " ".join(
            str(v).lower() for row in conn.execute(f"SELECT * FROM {table}") for v in row)
        for bad in ("api_key", "authorization", "bearer", "x-api-key", "?key=", "&key="):
            assert bad not in blob


def test_league_must_exist(conn: sqlite3.Connection) -> None:
    rid, rhash = _raw(conn, marker="badleague")
    repo = SqliteProviderIdentityRepository(conn)
    with pytest.raises(sqlite3.DatabaseError):
        with transaction(conn):
            repo.record_team(
                provider="mlb_statsapi", provider_team_id="141", league_id="lg_nope",
                full_name="Nowhere Nobodies", observed_at=T1, raw_response_id=rid,
                raw_response_hash=rhash)
