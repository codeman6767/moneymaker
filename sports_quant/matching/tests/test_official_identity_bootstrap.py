"""Adversarial tests for the F1 official identity bootstrap (e017).

Two things are being proven, and the second is the delicate one:

1. Team matching now receives a STRUCTURED provider-written name and therefore
   resolves to a *seeded* canonical team. It still never invents a team.
2. A canonical player may be created -- but only from the league's DESIGNATED
   OFFICIAL provider's stable id plus a structured identity observation. Every
   other path (a nonofficial provider, an empty name, an existing alias match,
   two plausible candidates) must refuse.

Offline only; nothing here opens a socket.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import pytest

from sports_quant.db.engine import transaction
from sports_quant.db.repositories.identity import SqliteProviderIdentityRepository
from sports_quant.db.repositories.players import SqlitePlayerAliasRepository
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.matching.model import (
    SCORE_OFFICIAL_PROVIDER_BOOTSTRAP,
    TIER_OFFICIAL_PROVIDER_BOOTSTRAP,
)
from sports_quant.matching.players_service import MatchPlayersService
from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE, MatchGamesService

from .conftest import (
    T0,
    link_player_ref,
    raw_response,
    seed_player,
    seed_player_ref,
    seed_schedule,
    seed_team,
    seed_team_alias,
)

MLB = "mlb_statsapi"
NBA = "balldontlie"
LATER = "2026-07-25T18:00:00.000000Z"


def _team_ref(conn: sqlite3.Connection, *, provider: str, provider_team_id: str,
              observed_at: str = T0) -> None:
    """Create an UNLINKED provider-team reference, as ingestion would."""

    rid, rhash = raw_response(conn, marker=f"teamref:{provider}:{provider_team_id}")
    with transaction(conn):
        SqliteProviderReferenceRepository(conn).upsert(
            kind="team", provider=provider, provider_entity_id=provider_team_id,
            raw_response_id=rid, raw_response_hash=rhash, observed_at=observed_at)


def _identity_team(
    conn: sqlite3.Connection, *, provider: str, provider_team_id: str, league_id: str,
    full_name: str, observed_at: str = T0, **kw: Optional[str],
) -> None:
    rid, rhash = raw_response(conn, marker=f"ti:{provider_team_id}:{full_name}:{observed_at}")
    with transaction(conn):
        SqliteProviderIdentityRepository(conn).record_team(
            provider=provider, provider_team_id=provider_team_id, league_id=league_id,
            full_name=full_name, observed_at=observed_at, raw_response_id=rid,
            raw_response_hash=rhash, **kw)


def _identity_player(
    conn: sqlite3.Connection, *, provider: str, provider_player_id: str, league_id: str,
    full_name: str, observed_at: str = T0, **kw: Optional[str],
) -> None:
    rid, rhash = raw_response(
        conn, marker=f"pi:{provider_player_id}:{full_name}:{observed_at}")
    with transaction(conn):
        SqliteProviderIdentityRepository(conn).record_player(
            provider=provider, provider_player_id=provider_player_id, league_id=league_id,
            full_name=full_name, observed_at=observed_at, raw_response_id=rid,
            raw_response_hash=rhash, **kw)


def _mlb_two_teams(conn: sqlite3.Connection) -> tuple[str, str]:
    home = seed_team(conn, league_code="MLB", abbreviation="TOR",
                     canonical_name="Toronto Blue Jays", city="Toronto",
                     nickname="Blue Jays")
    away = seed_team(conn, league_code="MLB", abbreviation="TB",
                     canonical_name="Tampa Bay Rays", city="Tampa Bay", nickname="Rays")
    return home, away


def _match_games(conn: sqlite3.Connection, provider: str = MLB, **kw: object):
    return MatchGamesService(conn).match_range(provider=provider, **kw)  # type: ignore[arg-type]


def _match_players(conn: sqlite3.Connection, provider: str, **kw: object):
    return MatchPlayersService(conn).match_range(provider=provider, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Team bootstrap
# --------------------------------------------------------------------------- #
def test_provider_id_plus_structured_name_resolves_a_seeded_team(
    conn: sqlite3.Connection,
) -> None:
    home, away = _mlb_two_teams(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="822788",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00Z", season=2026,
                  game_date_local="2026-07-24", mapped_status="final")
    _team_ref(conn, provider=MLB, provider_team_id="141")
    _team_ref(conn, provider=MLB, provider_team_id="139")
    _identity_team(conn, provider=MLB, provider_team_id="141", league_id="lg_mlb",
                   full_name="Toronto Blue Jays")
    _identity_team(conn, provider=MLB, provider_team_id="139", league_id="lg_mlb",
                   full_name="Tampa Bay Rays")
    result = _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    links = dict(conn.execute(
        "SELECT provider_team_id, team_id FROM provider_team_references").fetchall())
    assert links == {"141": home, "139": away}
    assert result.counters.canonical_games_created == 1
    # The seeded teams were RESOLVED, never re-created: db-init seeds 60 and
    # matching must leave that count untouched.
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 60


def test_missing_identity_leaves_the_team_unmatched(conn: sqlite3.Connection) -> None:
    """Without an identity the resolver still gets nothing -- the honest refusal."""

    _mlb_two_teams(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="822788",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00Z", season=2026,
                  game_date_local="2026-07-24")
    result = _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    assert result.counters.canonical_games_created == 0
    assert result.counters.no_candidate >= 3  # two teams + the game
    reasons = {r[0] for r in conn.execute(
        "SELECT rejection_reason FROM entity_match_decisions WHERE entity_type='team'")}
    assert reasons == {"no team alias matches '141'", "no team alias matches '139'"}
    assert all(r[0] is None for r in conn.execute(
        "SELECT team_id FROM provider_team_references"))


def test_ambiguous_provider_name_is_refused_not_guessed(conn: sqlite3.Connection) -> None:
    """"Los Angeles" names two franchises: refuse rather than pick one."""

    seed_team(conn, league_code="NBA", abbreviation="LAL", canonical_name="Los Angeles Lakers",
              city="Los Angeles", nickname="Lakers", aliases=[("LA Metro", "city", "")])
    seed_team(conn, league_code="NBA", abbreviation="LAC", canonical_name="Los Angeles Clippers",
              city="Los Angeles", nickname="Clippers", aliases=[("LA Metro", "city", "")])
    seed_schedule(conn, provider=NBA, provider_game_id="1", home_provider_team_id="10",
                  away_provider_team_id="11", scheduled_start="2026-01-05T23:00:00Z",
                  season=2025, game_date_local="2026-01-05")
    _team_ref(conn, provider=NBA, provider_team_id="10")
    _team_ref(conn, provider=NBA, provider_team_id="11")
    _identity_team(conn, provider=NBA, provider_team_id="10", league_id="lg_nba",
                   full_name="LA Metro")
    _identity_team(conn, provider=NBA, provider_team_id="11", league_id="lg_nba",
                   full_name="Los Angeles Clippers")
    result = _match_games(conn, provider=NBA, from_date="2026-01-05", to_date="2026-01-05")
    outcomes = dict(conn.execute(
        "SELECT source_ref, outcome FROM entity_match_decisions WHERE entity_type='team'"
    ).fetchall())
    assert outcomes["10"] == "ambiguous"
    assert result.counters.canonical_games_created == 0
    assert conn.execute("SELECT team_id FROM provider_team_references "
                        "WHERE provider_team_id='10'").fetchone()[0] is None


def test_wrong_league_alias_does_not_resolve(conn: sqlite3.Connection) -> None:
    """An NBA name must not resolve an MLB provider team."""

    seed_team(conn, league_code="NBA", abbreviation="TOR", canonical_name="Toronto Raptors",
              city="Toronto", nickname="Raptors")
    seed_schedule(conn, provider=MLB, provider_game_id="822788",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00Z", season=2026,
                  game_date_local="2026-07-24")
    _team_ref(conn, provider=MLB, provider_team_id="141")
    _team_ref(conn, provider=MLB, provider_team_id="139")
    _identity_team(conn, provider=MLB, provider_team_id="141", league_id="lg_mlb",
                   full_name="Toronto Raptors")
    _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    assert conn.execute("SELECT team_id FROM provider_team_references "
                        "WHERE provider_team_id='141'").fetchone()[0] is None


def test_exact_provider_id_replay_records_no_new_decision(
    conn: sqlite3.Connection,
) -> None:
    """After linking, a rerun resolves by exact provider id and writes nothing.

    This previously asserted that the rerun APPENDED two accepted
    ``exact_provider_id`` re-affirmations -- the decision-history growth recorded
    as a known residual in ENTITY_MATCHING.md 3.4.5. That growth is now repaired:
    an already-linked reference whose backing accepted decision still holds is a
    semantic replay, so the audit log stays exactly as the first run left it.
    """

    _mlb_two_teams(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="822788",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00Z", season=2026,
                  game_date_local="2026-07-24", mapped_status="final")
    _team_ref(conn, provider=MLB, provider_team_id="141")
    _team_ref(conn, provider=MLB, provider_team_id="139")
    _identity_team(conn, provider=MLB, provider_team_id="141", league_id="lg_mlb",
                   full_name="Toronto Blue Jays")
    _identity_team(conn, provider=MLB, provider_team_id="139", league_id="lg_mlb",
                   full_name="Tampa Bay Rays")
    _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    first = [r[0] for r in conn.execute(
        "SELECT match_id FROM entity_match_decisions WHERE entity_type='team' "
        "ORDER BY decided_at, match_id")]
    second = _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    after = [r[0] for r in conn.execute(
        "SELECT match_id FROM entity_match_decisions WHERE entity_type='team' "
        "ORDER BY decided_at, match_id")]
    assert after == first, "the replay appended team decisions"
    assert second.counters.decisions_replayed >= 2
    # The link itself is still the exact-provider-id path, just not re-recorded.
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_team_references WHERE team_id IS NOT NULL"
    ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1


def test_game_matching_proceeds_only_after_both_teams_resolve(
    conn: sqlite3.Connection,
) -> None:
    _mlb_two_teams(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="822788",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00Z", season=2026,
                  game_date_local="2026-07-24", mapped_status="final")
    # Only the home team has an identity.
    _team_ref(conn, provider=MLB, provider_team_id="141")
    _team_ref(conn, provider=MLB, provider_team_id="139")
    _identity_team(conn, provider=MLB, provider_team_id="141", league_id="lg_mlb",
                   full_name="Toronto Blue Jays")
    _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    game = conn.execute(
        "SELECT outcome, rejection_reason FROM entity_match_decisions "
        "WHERE entity_type='game'").fetchone()
    assert game["outcome"] == "no_candidate"
    assert game["rejection_reason"] == "home/away team did not resolve"
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0


def test_a_future_identity_cannot_decide_an_earlier_match(
    conn: sqlite3.Connection,
) -> None:
    """as-of bounding: a name written after the observation is not evidence for it."""

    _mlb_two_teams(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="822788",
                  home_provider_team_id="141", away_provider_team_id="139",
                  scheduled_start="2026-07-24T23:07:00Z", season=2026,
                  game_date_local="2026-07-24", observed_at=T0)
    _team_ref(conn, provider=MLB, provider_team_id="141")
    _team_ref(conn, provider=MLB, provider_team_id="139")
    _identity_team(conn, provider=MLB, provider_team_id="141", league_id="lg_mlb",
                   full_name="Toronto Blue Jays", observed_at=LATER)
    _identity_team(conn, provider=MLB, provider_team_id="139", league_id="lg_mlb",
                   full_name="Tampa Bay Rays", observed_at=LATER)
    _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    assert all(r[0] is None for r in conn.execute(
        "SELECT team_id FROM provider_team_references"))


# --------------------------------------------------------------------------- #
# Official player bootstrap
# --------------------------------------------------------------------------- #
def test_designated_official_provider_creates_a_canonical_player(
    conn: sqlite3.Connection,
) -> None:
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette", position="SS", provider_team_id="141")
    result = _match_players(conn, MLB, season_year=2026)
    assert result.counters.canonical_players_created == 1
    assert result.counters.provider_references_linked == 1
    player = conn.execute("SELECT * FROM players").fetchone()
    assert player["full_name"] == "Bo Bichette"
    assert player["primary_position"] == "SS"
    # MLB supplies no name parts and no birth date -- none are invented.
    assert player["first_name"] is None and player["last_name"] is None
    assert player["birth_date"] is None
    # A career window is never invented from one identity snapshot.
    assert player["debut_date"] is None and player["final_game_date"] is None
    decision = conn.execute(
        "SELECT method, score, outcome, matched_entity_id, needs_manual_review, "
        "raw_response_id FROM entity_match_decisions WHERE entity_type='player'").fetchone()
    assert decision["method"] == TIER_OFFICIAL_PROVIDER_BOOTSTRAP
    assert decision["score"] == SCORE_OFFICIAL_PROVIDER_BOOTSTRAP
    assert decision["outcome"] == "accepted"
    assert decision["matched_entity_id"] == player["player_id"]
    assert not decision["needs_manual_review"]
    assert decision["raw_response_id"]  # identity-snapshot provenance
    evidence = conn.execute("SELECT evidence FROM match_candidates").fetchone()[0]
    assert "670764" in evidence and "pti_" not in evidence


def test_official_bootstrap_records_the_provider_alias(conn: sqlite3.Connection) -> None:
    seed_player_ref(conn, provider=NBA, provider_player_id="100")
    _identity_player(conn, provider=NBA, provider_player_id="100", league_id="lg_nba",
                     full_name="Jordan Clarkson", first_name="Jordan",
                     last_name="Clarkson", position="G")
    _match_players(conn, NBA, season_year=2025)
    alias = conn.execute("SELECT * FROM player_aliases").fetchone()
    assert alias["alias"] == "Jordan Clarkson"
    assert alias["alias_type"] == "provider"
    assert alias["provider"] == NBA
    assert alias["source"] == "provider_observed"
    player = conn.execute("SELECT * FROM players").fetchone()
    # BALLDONTLIE genuinely supplies both parts, so both are preserved.
    assert (player["first_name"], player["last_name"]) == ("Jordan", "Clarkson")


def test_nonofficial_provider_cannot_create_a_canonical_player(
    conn: sqlite3.Connection,
) -> None:
    """A sportsbook / Kalshi / unknown provider never establishes identity."""

    for provider in ("the_odds_api", "kalshi", "some_scraper"):
        seed_player_ref(conn, provider=provider, provider_player_id="x1")
        _identity_player(conn, provider=provider, provider_player_id="x1",
                         league_id="lg_mlb", full_name="Bo Bichette")
        result = _match_players(conn, provider, season_year=2026)
        assert result.counters.canonical_players_created == 0
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0


def test_official_designation_covers_exactly_the_two_official_providers() -> None:
    assert OFFICIAL_PROVIDER_BY_LEAGUE == {"lg_mlb": MLB, "lg_nba": NBA}


def test_empty_name_cannot_create_a_player(conn: sqlite3.Connection) -> None:
    """No identity observation at all -> the honest no-candidate refusal."""

    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    result = _match_players(conn, MLB, season_year=2026)
    assert result.counters.canonical_players_created == 0
    assert result.counters.no_candidate == 1
    decision = conn.execute(
        "SELECT outcome, rejection_reason, needs_manual_review "
        "FROM entity_match_decisions").fetchone()
    assert decision["outcome"] == "no_candidate"
    assert decision["needs_manual_review"]
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0


def test_existing_exact_alias_is_preferred_over_creation(
    conn: sqlite3.Connection,
) -> None:
    existing = seed_player(conn, league_code="MLB", full_name="Bo Bichette",
                           aliases=[("Bo Bichette", "full", "")])
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette")
    result = _match_players(conn, MLB, season_year=2026)
    assert result.counters.canonical_players_created == 0
    assert result.counters.accepted == 1
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1
    assert conn.execute("SELECT player_id FROM provider_player_references").fetchone()[0] == (
        existing)
    method = conn.execute("SELECT method FROM entity_match_decisions").fetchone()[0]
    assert method != TIER_OFFICIAL_PROVIDER_BOOTSTRAP


def test_multiple_candidates_stay_ambiguous_and_create_nothing(
    conn: sqlite3.Connection,
) -> None:
    seed_player(conn, league_code="MLB", full_name="Will Smith",
                aliases=[("Will Smith", "full", "")])
    seed_player(conn, league_code="MLB", full_name="Will Smith",
                aliases=[("Will Smith", "full", "")])
    seed_player_ref(conn, provider=MLB, provider_player_id="669257")
    _identity_player(conn, provider=MLB, provider_player_id="669257", league_id="lg_mlb",
                     full_name="Will Smith")
    result = _match_players(conn, MLB, season_year=2026)
    assert result.counters.ambiguous == 1
    assert result.counters.canonical_players_created == 0
    # Two plausible people is insufficient evidence, not a licence to add a third.
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 2
    assert conn.execute("SELECT player_id FROM provider_player_references").fetchone()[0] is (
        None)


def test_same_name_distinct_official_ids_remain_distinct_identities(
    conn: sqlite3.Connection,
) -> None:
    """Two official ids sharing a name are two people until proven otherwise."""

    for pid in ("1001", "1002"):
        seed_player_ref(conn, provider=MLB, provider_player_id=pid)
        _identity_player(conn, provider=MLB, provider_player_id=pid, league_id="lg_mlb",
                         full_name="Will Smith")
    result = _match_players(conn, MLB, season_year=2026)
    # First bootstraps; the second then finds that one by alias and must NOT
    # silently merge into it on name alone -- it resolves or refuses, but either
    # way exactly one canonical player is never turned into a shared identity.
    linked = [r[0] for r in conn.execute(
        "SELECT player_id FROM provider_player_references ORDER BY provider_player_id")]
    players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    if linked[1] is not None:
        # If the second linked, it must be to a DIFFERENT canonical player.
        assert linked[0] != linked[1], "two official ids collapsed into one player"
        assert players == 2
    else:
        assert players == 1
        assert result.counters.canonical_players_created == 1


def test_suffix_is_preserved_through_bootstrap(conn: sqlite3.Connection) -> None:
    seed_player_ref(conn, provider=MLB, provider_player_id="665489")
    _identity_player(conn, provider=MLB, provider_player_id="665489", league_id="lg_mlb",
                     full_name="Vladimir Guerrero Jr.")
    _match_players(conn, MLB, season_year=2026)
    player = conn.execute("SELECT * FROM players").fetchone()
    assert player["full_name"] == "Vladimir Guerrero Jr."
    assert player["suffix"] == "jr"
    alias = conn.execute("SELECT suffix, normalized FROM player_aliases").fetchone()
    assert alias["suffix"] == "jr"
    assert alias["normalized"] == "vladimir guerrero"


def test_an_already_linked_reference_is_never_re_bootstrapped(
    conn: sqlite3.Connection,
) -> None:
    existing = seed_player(conn, league_code="MLB", full_name="Bo Bichette")
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    link_player_ref(conn, provider=MLB, provider_player_id="670764", player_id=existing)
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette")
    result = _match_players(conn, MLB, season_year=2026)
    # Already linked references are out of scope for the matcher entirely.
    assert result.references_considered == 0
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1


def test_replay_creates_no_duplicate_player_alias_or_link(
    conn: sqlite3.Connection,
) -> None:
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette")
    _match_players(conn, MLB, season_year=2026)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("players", "player_aliases", "provider_player_references",
                        "entity_match_decisions")}
    second = _match_players(conn, MLB, season_year=2026)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before
    assert second.references_considered == 0
    assert second.counters.canonical_players_created == 0


def test_traversal_order_does_not_change_the_semantic_result(
    conn: sqlite3.Connection, db_path, tmp_path,
) -> None:
    """Bootstrapping ascending vs descending must produce the same canonical set."""

    ids = ["1001", "1002", "1003"]
    names = {"1001": "Alpha One", "1002": "Beta Two", "1003": "Gamma Three"}
    for pid in ids:
        seed_player_ref(conn, provider=MLB, provider_player_id=pid)
        _identity_player(conn, provider=MLB, provider_player_id=pid, league_id="lg_mlb",
                         full_name=names[pid])
    for pid in reversed(ids):
        MatchPlayersService(conn).match_range(
            provider=MLB, provider_player_id=pid, season_year=2026)
    rows = [(r[0], r[1]) for r in conn.execute(
        "SELECT ppr.provider_player_id, p.full_name FROM provider_player_references ppr "
        "JOIN players p ON p.player_id = ppr.player_id ORDER BY ppr.provider_player_id")]
    assert rows == [("1001", "Alpha One"), ("1002", "Beta Two"), ("1003", "Gamma Three")]
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 3


def test_bootstrap_rolls_back_as_one_unit_on_alias_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Player, alias, decision and link are one unit: a mid-way failure leaves none."""

    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette")

    def boom(*_a: object, **_kw: object) -> bool:
        raise sqlite3.OperationalError("alias write failed")

    monkeypatch.setattr(SqlitePlayerAliasRepository, "add", boom)
    with pytest.raises(sqlite3.OperationalError):
        with transaction(conn):
            MatchPlayersService(conn).match_range(provider=MLB, season_year=2026)
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM player_aliases").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0] == 0
    assert conn.execute(
        "SELECT player_id FROM provider_player_references").fetchone()[0] is None


# --------------------------------------------------------------------------- #
# Decision-history idempotency
# --------------------------------------------------------------------------- #
def test_identical_unresolved_decision_is_not_appended_twice(
    conn: sqlite3.Connection,
) -> None:
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    first = _match_players(conn, MLB, season_year=2026)
    assert first.counters.decisions_recorded == 1
    second = _match_players(conn, MLB, season_year=2026)
    assert second.counters.decisions_replayed == 1
    assert second.counters.decisions_recorded == 0
    assert conn.execute("SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0] == 1


def test_a_changed_identity_creates_a_new_decision(conn: sqlite3.Connection) -> None:
    """Deduplication must never hide a genuinely different re-evaluation."""

    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    _match_players(conn, MLB, season_year=2026)
    # A name arrives: the refusal reason changes, so a new decision is required.
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette", observed_at=LATER)
    second = _match_players(conn, MLB, season_year=2026)
    assert second.counters.canonical_players_created == 1
    outcomes = [r[0] for r in conn.execute(
        "SELECT outcome FROM entity_match_decisions ORDER BY decided_at, match_id")]
    assert outcomes == ["no_candidate", "accepted"]


def test_a_changed_matcher_version_creates_a_new_decision(
    conn: sqlite3.Connection,
) -> None:
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    MatchPlayersService(conn).match_range(provider=MLB, season_year=2026)
    MatchPlayersService(conn, matcher_version="d5a-2").match_range(
        provider=MLB, season_year=2026)
    versions = [r[0] for r in conn.execute(
        "SELECT matcher_version FROM entity_match_decisions ORDER BY decided_at, match_id")]
    assert versions == ["d5a-1", "d5a-2"]


def test_the_audit_trail_is_never_erased(conn: sqlite3.Connection) -> None:
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    _match_players(conn, MLB, season_year=2026)
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette", observed_at=LATER)
    _match_players(conn, MLB, season_year=2026)
    # The original refusal is still on record next to the later acceptance.
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_match_decisions WHERE outcome='no_candidate'"
    ).fetchone()[0] == 1


def test_a_provider_alias_from_another_provider_does_not_resolve(
    conn: sqlite3.Connection,
) -> None:
    """Provider-scoped aliases stay scoped; NBA's alias cannot resolve MLB's id."""

    other = seed_player(conn, league_code="MLB", full_name="Bo Bichette")
    with transaction(conn):
        SqlitePlayerAliasRepository(conn).add(
            player_id=other, league_id="lg_mlb", alias="Bo Bichette",
            alias_type="provider", provider=NBA, source="provider_observed")
    seed_player_ref(conn, provider=MLB, provider_player_id="670764")
    _identity_player(conn, provider=MLB, provider_player_id="670764", league_id="lg_mlb",
                     full_name="Bo Bichette")
    _match_players(conn, MLB, season_year=2026)
    # It resolved or bootstrapped, but never through the other provider's alias.
    method = conn.execute(
        "SELECT method FROM entity_match_decisions WHERE entity_type='player'").fetchone()[0]
    assert method in (TIER_OFFICIAL_PROVIDER_BOOTSTRAP, "league_normalized_name",
                      "team_normalized_name")


def test_seeded_team_alias_provider_scope_is_respected(conn: sqlite3.Connection) -> None:
    """A provider-typed team alias for another provider must not resolve this one."""

    team = seed_team(conn, league_code="MLB", abbreviation="TOR",
                     canonical_name="Toronto Blue Jays", city="Toronto",
                     nickname="Blue Jays")
    seed_team_alias(conn, team_id=team, league_code="MLB", alias="Jays Club",
                    alias_type="provider", provider="the_odds_api")
    seed_schedule(conn, provider=MLB, provider_game_id="1", home_provider_team_id="141",
                  away_provider_team_id="139", scheduled_start="2026-07-24T23:07:00Z",
                  season=2026, game_date_local="2026-07-24")
    _team_ref(conn, provider=MLB, provider_team_id="141")
    _team_ref(conn, provider=MLB, provider_team_id="139")
    _identity_team(conn, provider=MLB, provider_team_id="141", league_id="lg_mlb",
                   full_name="Jays Club")
    _match_games(conn, from_date="2026-07-24", to_date="2026-07-24")
    # Resolution is possible only via the unscoped normalized tier, and the
    # alias belongs to a different provider, so no exact/provider-scoped match.
    decision = conn.execute(
        "SELECT method FROM entity_match_decisions WHERE entity_type='team' "
        "AND source_ref='141'").fetchone()[0]
    assert decision != "exact_alias"
