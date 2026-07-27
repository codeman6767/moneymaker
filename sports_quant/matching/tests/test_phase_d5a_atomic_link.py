"""Phase D5A atomic decision-and-link hardening (task §7/§8).

An accepted match decision and its provider-reference link are one atomic unit,
matching the D5B1 sportsbook invariant: a clean reference records the accepted
decision and applies + verifies the link together; an existing link to another
entity (or a corrupt supporting decision) is a blocking rejection that records no
accepted decision; a link failure rolls the whole run back; an idempotent replay
records no new accepted decision. Isolated temporary corpora only.
"""

from __future__ import annotations

import sqlite3

import pytest

from sports_quant.db.engine import transaction
from sports_quant.db.ids import new_match_decision_id
from sports_quant.db.repositories.matching import SqliteMatchingRepository
from sports_quant.db.repositories.references import (
    LinkOutcome,
    SqliteProviderReferenceRepository,
)
from sports_quant.matching.linkatomic import MatchLinkError
from sports_quant.matching.model import SCORE_OFFICIAL_KEY, TIER_OFFICIAL_KEY
from sports_quant.matching.players_service import MatchPlayersService
from sports_quant.matching.service import MatchGamesResult, MatchGamesService

from .conftest import seed_player, seed_player_ref, seed_schedule
from .test_phase_d5a_matching import (
    MLB,
    _create_canonical,
    _match,
    _mlb_setup,
    _two_mlb_teams,
)
from .test_phase_d5a_review import _players


def _game_decisions(conn: sqlite3.Connection, source_ref: str, outcome: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM entity_match_decisions WHERE entity_type='game' "
        "AND source_ref=? AND outcome=?", (source_ref, outcome)).fetchone()[0]


def _prelink_game(conn: sqlite3.Connection, *, provider: str, provider_game_id: str, game_id: str,
                  dec_source_ref: str = None, dec_matched: str = None,  # type: ignore[assignment]
                  outcome: str = "accepted") -> str:
    """Seed a provider game reference (if absent) and force a specific prior link."""

    with transaction(conn):
        rid = conn.execute(
            "SELECT current_raw_response_id FROM provider_game_references "
            "WHERE provider=? AND provider_game_id=?", (provider, provider_game_id)).fetchone()
        mid = new_match_decision_id()
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, "
            "source_ref, matched_entity_id, outcome, method, score, threshold, matcher_version, "
            "needs_manual_review, decided_at, created_at) VALUES "
            "(?, 'game', ?, ?, ?, ?, 'official_key_exact', 1.0, 0.85, 'd5a-1', 0, "
            "'2026-07-05T00:00:00.000000Z', '2026-07-05T00:00:00.000000Z')",
            (mid, provider, dec_source_ref if dec_source_ref is not None else provider_game_id,
             (dec_matched if dec_matched is not None else game_id) if outcome == "accepted"
             else None, outcome))
        conn.execute(
            "UPDATE provider_game_references SET game_id=?, match_decision_id=? "
            "WHERE provider=? AND provider_game_id=?", (game_id, mid, provider, provider_game_id))
        _ = rid
    return mid


# --------------------------------------------------------------------------- #
# Official-game reference atomicity
# --------------------------------------------------------------------------- #
def test_game_clean_decision_and_link_atomic(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1", game_type="R")
    r = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    assert r.counters.canonical_games_created == 1 and r.counters.provider_references_linked == 1
    ref = SqliteProviderReferenceRepository(conn).get("game", MLB, "G1")
    assert ref is not None and ref.canonical_id is not None and ref.match_decision_id is not None
    accepted = SqliteMatchingRepository(conn).decisions_for_source(
        source_provider=MLB, source_ref="G1", entity_type="game")
    assert len(accepted) == 1 and accepted[0].match_id == ref.match_decision_id
    assert accepted[0].matched_entity_id == ref.canonical_id


def test_game_link_conflict_leaves_no_accepted_decision(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    ga = _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                           scheduled_start="2026-07-20T23:05:00Z", game_date_local="2026-07-20",
                           official_provider=MLB, official_game_key="GA")
    gb = _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                           scheduled_start="2026-07-25T23:05:00Z", game_date_local="2026-07-25",
                           official_provider=MLB, official_game_key="GB")
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25")
    _prelink_game(conn, provider=MLB, provider_game_id="G1", game_id=ga)  # already linked to GA
    svc = MatchGamesService(conn)
    result = MatchGamesResult(dry_run=False)
    with transaction(conn):
        svc._accept_game(MLB, "G1", gb, TIER_OFFICIAL_KEY, SCORE_OFFICIAL_KEY, [gb], result,
                         created=False)
    assert result.counters.accepted == 0 and result.counters.blocking_issues >= 1
    assert _game_decisions(conn, "G1", "accepted") == 1  # only the pre-seeded one naming GA
    ref = SqliteProviderReferenceRepository(conn).get("game", MLB, "G1")
    assert ref is not None and ref.canonical_id == ga  # existing link unchanged
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-003" in codes


def test_game_replay_decision_belongs_to_other_ref_is_blocking(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    ga = _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                           scheduled_start="2026-07-25T23:05:00Z", game_date_local="2026-07-25",
                           official_provider=MLB, official_game_key="GA")
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z", season=2026,
                  game_date_local="2026-07-25")
    # Ref links to GA, but the supporting decision belongs to a DIFFERENT source ref.
    _prelink_game(conn, provider=MLB, provider_game_id="G1", game_id=ga, dec_source_ref="OTHER")
    svc = MatchGamesService(conn)
    result = MatchGamesResult(dry_run=False)
    with transaction(conn):
        svc._accept_game(MLB, "G1", ga, TIER_OFFICIAL_KEY, SCORE_OFFICIAL_KEY, [ga], result,
                         created=False)
    assert result.counters.accepted == 0 and result.counters.blocking_issues >= 1  # corrupt pairing


def test_game_replay_decision_names_other_entity_is_blocking(conn: sqlite3.Connection) -> None:
    home, away = _two_mlb_teams(conn)
    ga = _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                           scheduled_start="2026-07-25T23:05:00Z", game_date_local="2026-07-25",
                           official_provider=MLB, official_game_key="GA")
    gb = _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                           scheduled_start="2026-07-20T23:05:00Z", game_date_local="2026-07-20",
                           official_provider=MLB, official_game_key="GB")
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z", season=2026,
                  game_date_local="2026-07-25")
    _prelink_game(conn, provider=MLB, provider_game_id="G1", game_id=ga, dec_matched=gb)
    svc = MatchGamesService(conn)
    result = MatchGamesResult(dry_run=False)
    with transaction(conn):
        svc._accept_game(MLB, "G1", ga, TIER_OFFICIAL_KEY, SCORE_OFFICIAL_KEY, [ga], result,
                         created=False)
    assert result.counters.accepted == 0 and result.counters.blocking_issues >= 1


def test_game_link_failure_rolls_back(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1", game_type="R")
    svc = MatchGamesService(conn)
    svc._refs.link_canonical = lambda **_kw: (None, LinkOutcome.CONFLICT)  # type: ignore[method-assign]
    before_dec = SqliteMatchingRepository(conn).count()
    from sports_quant.db.repositories.games import SqliteGameRepository
    before_games = SqliteGameRepository(conn).count()
    with pytest.raises(MatchLinkError):
        with transaction(conn):
            svc.match_range(provider=MLB, from_date="2026-07-25", to_date="2026-07-25")
    # Rolled back: neither the accepted decision nor the created game survive.
    assert SqliteMatchingRepository(conn).count() == before_dec
    assert SqliteGameRepository(conn).count() == before_games


def test_game_identical_replay_records_no_new_accepted(conn: sqlite3.Connection) -> None:
    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1", game_type="R")
    _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    ref1 = SqliteProviderReferenceRepository(conn).get("game", MLB, "G1")
    assert ref1 is not None
    r2 = _match(conn, from_date="2026-07-25", to_date="2026-07-25")
    # The game entity is recognized as unchanged and records NO new game accepted
    # decision (team/venue input resolutions are separate audit records and may
    # re-record; the game link itself is idempotent).
    assert r2.counters.canonical_entities_unchanged == 1 and r2.counters.canonical_games_created == 0
    assert _game_decisions(conn, "G1", "accepted") == 1  # no new game accepted decision on replay
    ref2 = SqliteProviderReferenceRepository(conn).get("game", MLB, "G1")
    assert ref2 is not None and ref2.match_decision_id == ref1.match_decision_id  # unchanged


# --------------------------------------------------------------------------- #
# Provider-player reference atomicity
# --------------------------------------------------------------------------- #
def test_player_clean_decision_and_link(conn: sqlite3.Connection) -> None:
    pid = seed_player(conn, league_code="MLB", full_name="Solo Guy",
                      aliases=[("solo", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="solo")
    r = _players(conn, provider=MLB)
    assert r.counters.accepted == 1 and r.counters.provider_references_linked == 1
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "solo")
    assert ref is not None and ref.canonical_id == pid and ref.match_decision_id is not None


def test_player_link_conflict_leaves_no_accepted_decision(conn: sqlite3.Connection) -> None:
    b = seed_player(conn, league_code="MLB", full_name="Solo Guy",
                    aliases=[("solo", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="solo")
    # Corrupt prior link: 'solo' is linked to b, but its supporting decision
    # belongs to a DIFFERENT source reference -- a mismatched decision/link pair.
    with transaction(conn):
        mid = new_match_decision_id()
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, "
            "source_ref, matched_entity_id, outcome, method, score, threshold, matcher_version, "
            "needs_manual_review, decided_at, created_at) VALUES "
            "(?, 'player', ?, 'OTHER', ?, 'accepted', 'exact_alias', 0.99, 0.85, 'd5a-1', 0, "
            "'2026-07-05T00:00:00.000000Z', '2026-07-05T00:00:00.000000Z')", (mid, MLB, b))
        conn.execute("UPDATE provider_player_references SET player_id=?, match_decision_id=? "
                     "WHERE provider=? AND provider_player_id='solo'", (b, mid, MLB))
    # Invoke the hardened resolve directly on the already-linked reference (the
    # entry point skips linked refs, so this is the defensive corruption path).
    from sports_quant.matching.players_service import MatchPlayersResult
    r = MatchPlayersResult(dry_run=False)
    with transaction(conn):
        MatchPlayersService(conn)._resolve_one(MLB, "solo", "lg_mlb", None, r)
    assert r.counters.accepted == 0 and r.counters.blocking_issues >= 1
    ref = SqliteProviderReferenceRepository(conn).get("player", MLB, "solo")
    assert ref is not None and ref.canonical_id == b  # unchanged
    codes = {row[0] for row in conn.execute("SELECT rule_code FROM data_quality_issues")}
    assert "DQ-MATCH-016" in codes


def test_player_link_failure_rolls_back(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="Solo Guy", aliases=[("solo", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="solo")
    svc = MatchPlayersService(conn)
    svc._refs.link_canonical = lambda **_kw: (None, LinkOutcome.CONFLICT)  # type: ignore[method-assign]
    before = SqliteMatchingRepository(conn).count()
    with pytest.raises(MatchLinkError):
        with transaction(conn):
            svc.match_range(provider=MLB)
    assert SqliteMatchingRepository(conn).count() == before  # accepted decision rolled back


def test_player_identical_replay_is_stable(conn: sqlite3.Connection) -> None:
    seed_player(conn, league_code="MLB", full_name="Solo Guy", aliases=[("solo", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="solo")
    _players(conn, provider=MLB)
    accepted_after_first = conn.execute(
        "SELECT COUNT(*) FROM entity_match_decisions WHERE entity_type='player' "
        "AND source_ref='solo' AND outcome='accepted'").fetchone()[0]
    r2 = _players(conn, provider=MLB)
    # The reference is now linked, so it is not re-processed; no new accepted decision.
    assert r2.references_considered == 0 and r2.counters.accepted == 0
    accepted_after_second = conn.execute(
        "SELECT COUNT(*) FROM entity_match_decisions WHERE entity_type='player' "
        "AND source_ref='solo' AND outcome='accepted'").fetchone()[0]
    assert accepted_after_first == accepted_after_second == 1


# --------------------------------------------------------------------------- #
# Team crosswalk conflict (where applicable) + dry-run
# --------------------------------------------------------------------------- #
def test_team_crosswalk_conflict_is_blocking(conn: sqlite3.Connection) -> None:
    # The repository refuses to re-point a linked crosswalk (the guarantee the
    # service relies on to keep team links atomic).
    from .conftest import raw_response, seed_team
    tx = seed_team(conn, league_code="MLB", abbreviation="TX", canonical_name="Team X",
                   city="X", nickname="Xs")
    ty = seed_team(conn, league_code="MLB", abbreviation="TY", canonical_name="Team Y",
                   city="Y", nickname="Ys")
    rid, rhash = raw_response(conn, marker="teamref:55")
    refs = SqliteProviderReferenceRepository(conn)
    with transaction(conn):
        refs.upsert(kind="team", provider=MLB, provider_entity_id="55",
                    raw_response_id=rid, raw_response_hash=rhash,
                    observed_at="2026-07-01T00:00:00.000000Z")
        conn.execute("UPDATE provider_team_references SET team_id=? WHERE provider=? "
                     "AND provider_team_id='55'", (ty, MLB))
        mid = new_match_decision_id()
        conn.execute(
            "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, "
            "source_ref, matched_entity_id, outcome, method, score, threshold, matcher_version, "
            "needs_manual_review, decided_at, created_at) VALUES "
            "(?, 'team', ?, '55', ?, 'accepted', 'exact_alias', 0.99, 0.85, 'v', 0, "
            "'2026-07-05T00:00:00.000000Z', '2026-07-05T00:00:00.000000Z')", (mid, MLB, tx))
        _ref, outcome = refs.link_canonical(
            kind="team", provider=MLB, provider_entity_id="55", canonical_id=tx,
            match_decision_id=mid)
    assert outcome == LinkOutcome.CONFLICT  # never re-points an established crosswalk


def test_dry_run_persists_nothing(conn: sqlite3.Connection) -> None:
    import hashlib

    _mlb_setup(conn)
    seed_schedule(conn, provider=MLB, provider_game_id="G1", home_provider_team_id="101",
                  away_provider_team_id="102", scheduled_start="2026-07-25T23:05:00Z",
                  season=2026, game_date_local="2026-07-25", venue_provider_id="V1", game_type="R")
    seed_player(conn, league_code="MLB", full_name="Solo Guy", aliases=[("solo", "provider", MLB)])
    seed_player_ref(conn, provider=MLB, provider_player_id="solo")

    def _dump() -> str:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()

    before = _dump()
    rg = MatchGamesService(conn, dry_run=True).match_range(
        provider=MLB, from_date="2026-07-25", to_date="2026-07-25")
    rp = MatchPlayersService(conn, dry_run=True).match_range(provider=MLB)
    assert rg.counters.canonical_games_created == 1 and rp.counters.accepted == 1  # computed
    assert _dump() == before  # but nothing persisted
