"""Fixtures + adversarial seeders for the Phase E1 point-in-time tests.

Everything is offline against an isolated temporary corpus. Seeders write through
the real append-only repositories (so provenance, content hashes, and triggers
are exercised exactly as in production) at explicitly chosen ``observed_at``
transaction times, which is what the leakage guards must respect.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.game_statistics import (
    SqlitePlayerGameStatRepository,
    SqliteTeamGameStatRepository,
)
from sports_quant.db.repositories.games import SqliteGameRepository
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.lineups import LineupPlayerInput, SqliteLineupRepository
from sports_quant.db.repositories.matching import CandidateInput, SqliteMatchingRepository
from sports_quant.db.repositories.nba import SqliteInjurySnapshotRepository
from sports_quant.db.repositories.official_games import SqliteResultRepository
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.db.repositories.sportsbook import SqliteSportsbookRepository, price_content_hash
from sports_quant.db.repositories.weather import SqliteWeatherRepository, WeatherValues
from sports_quant.db.schema import utc_now_iso

# Reuse the audited matching-test seeders for canonical/provider entities.
from sports_quant.matching.tests.conftest import (
    seed_player,
    seed_player_ref,
    seed_sb_event,
    seed_sb_market,
    seed_sb_outcome,
    seed_schedule,
    seed_team,
    seed_venue,
)
from sports_quant.matching.tests.test_phase_d5a_matching import _create_canonical

# A deterministic timeline. A pregame CUTOFF sees T1 but never T2 (post-cutoff).
T1 = "2026-07-10T00:00:00.000000Z"
CUTOFF = "2026-07-12T00:00:00.000000Z"
T2 = "2026-07-16T00:00:00.000000Z"
SCHED_START = "2026-07-15T02:10:00Z"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "corpus.db"
    initialize_database(path)
    return path


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with Database(db_path).connection() as connection:
        yield connection


@dataclass(frozen=True)
class Ctx:
    game_id: str
    game_ref_id: str
    home_team_id: str
    away_team_id: str
    venue_id: str
    player_id: str
    player_ref_id: str


def _prov(conn: sqlite3.Connection) -> tuple[str, str, str]:
    marker = uuid.uuid4().hex
    run = SqliteIngestionRunRepository(conn).start(
        command="seed", provider="test", operation="seed", args_json="{}",
        started_monotonic_ns=0, tool_version="t")
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run.run_id, provider="test", endpoint="/seed", request_params_json="{}",
        http_status=200, response_headers_json="{}", requested_at=utc_now_iso(),
        received_at=utc_now_iso(), elapsed_ns=1, body=marker,
        content_hash=response_content_hash(provider="test", endpoint="/seed",
                                           request_params={}, body=marker))
    return run.run_id, raw.raw_response_id, raw.content_hash


@pytest.fixture()
def ctx(conn: sqlite3.Connection) -> Ctx:
    """A fully wired canonical MLB game with a provider game ref, teams, a player,
    a player reference, and a venue -- enough to satisfy every snapshot FK."""

    home = seed_team(conn, league_code="MLB", abbreviation="LAD",
                     canonical_name="Los Angeles Dodgers", city="Los Angeles", nickname="Dodgers")
    away = seed_team(conn, league_code="MLB", abbreviation="SD",
                     canonical_name="San Diego Padres", city="San Diego", nickname="Padres")
    venue = seed_venue(conn, name="Dodger Stadium", timezone="America/Los_Angeles")
    player = seed_player(conn, league_code="MLB", full_name="Test Player")
    player_ref = seed_player_ref(conn, provider="mlb_statsapi", provider_player_id="P1")
    game_ref = seed_schedule(conn, provider="mlb_statsapi", provider_game_id="G1",
                             home_provider_team_id="101", away_provider_team_id="102",
                             scheduled_start=SCHED_START, season=2026, game_date_local="2026-07-14")
    game_id = _create_canonical(
        conn, league_code="MLB", home_team_id=home, away_team_id=away,
        scheduled_start=SCHED_START, game_date_local="2026-07-14",
        official_provider="mlb_statsapi", official_game_key="G1")
    return Ctx(game_id=game_id, game_ref_id=game_ref, home_team_id=home, away_team_id=away,
               venue_id=venue, player_id=player, player_ref_id=player_ref)


# --------------------------------------------------------------------------- #
# Append-only seeders at chosen transaction times
# --------------------------------------------------------------------------- #
def seed_status(conn: sqlite3.Connection, *, game_id: str, status: str, observed_at: str,
                provider: str = "mlb_statsapi", provider_timestamp: Optional[str] = None) -> None:
    with transaction(conn):
        SqliteGameRepository(conn).record_status(
            game_id=game_id, status=status, scheduled_start=SCHED_START, provider=provider,
            observed_at=observed_at, provider_timestamp=provider_timestamp)


def seed_result(conn: sqlite3.Connection, *, game_ref_id: str, observed_at: str,
                winning_side: str = "home", mapped_status: str = "final") -> None:
    run_id, rid, rhash = _prov(conn)
    with transaction(conn):
        SqliteResultRepository(conn).append(
            game_ref_id=game_ref_id, provider="mlb_statsapi", provider_game_id="G1",
            observed_at=observed_at, ingested_at=observed_at, run_id=run_id, raw_response_id=rid,
            raw_response_hash=rhash, mapped_status=mapped_status, home_runs=5, away_runs=3,
            winning_side=winning_side)


def seed_team_stat(conn: sqlite3.Connection, *, game_ref_id: str, team_id: str, observed_at: str,
                   runs: int, provider_team_id: str = "101", home_away: str = "home",
                   provider_timestamp: Optional[str] = None) -> None:
    run_id, rid, rhash = _prov(conn)
    with transaction(conn):
        SqliteTeamGameStatRepository(conn).append(
            game_ref_id=game_ref_id, provider="mlb_statsapi", provider_game_id="G1",
            provider_team_id=provider_team_id, home_away=home_away, observed_at=observed_at,
            ingested_at=observed_at, run_id=run_id, raw_response_id=rid, raw_response_hash=rhash,
            team_id=team_id, runs=runs, provider_timestamp=provider_timestamp)


def seed_player_stat(conn: sqlite3.Connection, *, game_ref_id: str, team_id: str, player_id: str,
                     observed_at: str, hits: int) -> None:
    run_id, rid, rhash = _prov(conn)
    with transaction(conn):
        SqlitePlayerGameStatRepository(conn).append(
            game_ref_id=game_ref_id, provider="mlb_statsapi", provider_game_id="G1",
            provider_player_id="P1", observed_at=observed_at, ingested_at=observed_at,
            run_id=run_id, raw_response_id=rid, raw_response_hash=rhash, player_id=player_id,
            team_id=team_id, role="batting", batting_stats=f'{{"hits": {hits}}}')


def seed_lineup(conn: sqlite3.Connection, *, game_ref_id: str, team_id: str, observed_at: str,
                is_confirmed: bool) -> None:
    run_id, rid, rhash = _prov(conn)
    with transaction(conn):
        SqliteLineupRepository(conn).append(
            game_ref_id=game_ref_id, provider="mlb_statsapi", provider_game_id="G1",
            provider_team_id="101", players=[LineupPlayerInput(
                batting_order=1, provider_player_id="P1", position="CF", is_starter=True,
                player_id=None)],
            observed_at=observed_at, ingested_at=observed_at, run_id=run_id, raw_response_id=rid,
            raw_response_hash=rhash, team_id=team_id, home_away="home", is_confirmed=is_confirmed)


def seed_injury(conn: sqlite3.Connection, *, player_ref_id: str, team_id: str, player_id: str,
                game_ref_id: str, observed_at: str, published_at: Optional[str],
                status: str = "out") -> None:
    run_id, rid, rhash = _prov(conn)
    with transaction(conn):
        SqliteInjurySnapshotRepository(conn).append(
            player_ref_id=player_ref_id, provider="balldontlie", provider_player_id="P1",
            status=status, observed_at=observed_at, ingested_at=observed_at, run_id=run_id,
            raw_response_id=rid, raw_response_hash=rhash, player_id=player_id, team_id=team_id,
            game_ref_id=game_ref_id, published_at=published_at)


def seed_weather(conn: sqlite3.Connection, *, game_ref_id: str, venue_id: str, weather_kind: str,
                 observed_at: str, pit_eligible: Optional[bool], forecast_mode: str = "point",
                 valid_time: Optional[str] = None, temperature_c: float = 20.0) -> None:
    run_id, rid, rhash = _prov(conn)
    with transaction(conn):
        SqliteWeatherRepository(conn).append(
            game_ref_id=game_ref_id, provider="open_meteo", provider_game_id="G1",
            venue_id=venue_id, weather_kind=weather_kind, applicability="applicable",
            forecast_mode=forecast_mode, valid_time=valid_time, observed_at=observed_at,
            retrieved_at=observed_at, ingested_at=observed_at, run_id=run_id, raw_response_id=rid,
            raw_response_hash=rhash, values=WeatherValues(temperature_c=temperature_c),
            pit_eligible=pit_eligible)


def seed_price(conn: sqlite3.Connection, *, sb_outcome_id: str, price_american: int,
               observed_at: str, provider_timestamp: Optional[str] = None) -> None:
    run_id, rid, rhash = _prov(conn)
    with transaction(conn):
        SqliteSportsbookRepository(conn).append_price_snapshot(
            sb_outcome_id=sb_outcome_id, price_american=price_american, observed_at=observed_at,
            raw_response_id=rid, raw_response_hash=rhash, run_id=run_id,
            provider_timestamp=provider_timestamp,
            content_hash=price_content_hash(
                price_american=price_american, point=None, bookmaker_last_update=None,
                market_last_update=None, provider_timestamp=provider_timestamp))


def seed_sb_outcome_ctx(conn: sqlite3.Connection) -> str:
    """A sportsbook event/market/outcome; returns the sb_outcome_id."""

    ev = seed_sb_event(conn, provider_event_id="E1", sport_key="baseball_mlb",
                       commence_time=SCHED_START, home_team_raw="Los Angeles Dodgers",
                       away_team_raw="San Diego Padres")
    mkt = seed_sb_market(conn, sb_event_id=ev, market_key="h2h")
    return seed_sb_outcome(conn, sb_market_id=mkt, provider_outcome_name="Los Angeles Dodgers",
                           outcome_role="home")


def link_sb_event(conn: sqlite3.Connection, *, sb_event_id: str, game_id: str,
                  orientation: str = "direct", needs_review: bool = False) -> str:
    """Accept a decision and link a sportsbook event; returns the decision id."""

    from sports_quant.db.repositories.references import LinkOutcome
    with transaction(conn):
        match = SqliteMatchingRepository(conn)
        decision = match.record_decision(
            entity_type="sportsbook_event", source_provider="the_odds_api",
            source_ref=sb_event_id, outcome="accepted", method="exact", score=1.0, threshold=0.85,
            matcher_version="t", candidates=[CandidateInput(score=1.0, tier="exact",
                                                            candidate_entity_id=game_id)],
            matched_entity_id=game_id, needs_manual_review=needs_review)
        outcome = SqliteSportsbookRepository(conn).link_game(
            sb_event_id=sb_event_id, game_id=game_id, match_decision_id=decision.match_id,
            orientation=orientation)
        assert outcome is LinkOutcome.LINKED  # noqa: S101
    return decision.match_id


def seed_kalshi_linked(conn: sqlite3.Connection, *, game_id: str, yes_team_id: str) -> str:
    """Seed a Kalshi game-winner market linked to ``game_id`` with an accepted
    decision; returns the kalshi_market_id."""

    from sports_quant.db.repositories.kalshi import SqliteKalshiRepository
    from sports_quant.db.repositories.references import LinkOutcome
    from sports_quant.matching.tests.conftest import seed_kalshi_event, seed_kalshi_market

    kev = seed_kalshi_event(conn, event_ticker="KXMLBGAME-26JUL141910SDLAD",
                            series_ticker="KXMLBGAME", title="San Diego vs Los Angeles")
    kmk = seed_kalshi_market(
        conn, market_ticker="KXMLBGAME-26JUL141910SDLAD-LAD",
        event_ticker="KXMLBGAME-26JUL141910SDLAD", series_ticker="KXMLBGAME",
        kalshi_event_id=kev, title="San Diego vs Los Angeles", yes_sub_title="Los Angeles Dodgers",
        rules_primary="If Los Angeles Dodgers wins ... then Yes.")
    repo = SqliteKalshiRepository(conn)
    market = repo.get_market(kmk)
    assert market is not None  # noqa: S101
    with transaction(conn):
        decision = SqliteMatchingRepository(conn).record_decision(
            entity_type="kalshi_market", source_provider="kalshi_public", source_ref=kmk,
            outcome="accepted", method="kalshi_date", score=0.92, threshold=0.85,
            matcher_version="t", candidates=[CandidateInput(score=0.92, tier="kalshi_date",
                                                           candidate_entity_id=game_id)],
            matched_entity_id=game_id)
        outcome = repo.link_market_game(
            kalshi_market_id=kmk, game_id=game_id, match_decision_id=decision.match_id,
            yes_team_id=yes_team_id, matched_rules_hash=market.rules_hash or "",
            market_semantic="game_winner")
        assert outcome is LinkOutcome.LINKED  # noqa: S101
    return kmk


def seed_dq(conn: sqlite3.Connection, *, rule_code: str, entity_type: str, entity_id: str,
            severity: str, detected_at: str, resolved_at: Optional[str] = None,
            provider: Optional[str] = None) -> str:
    """Insert a data-quality issue with an explicit detected/resolved timeline."""

    from sports_quant.db.ids import new_data_quality_id
    issue_id = new_data_quality_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO data_quality_issues (issue_id, severity, rule_code, entity_type, "
            "entity_id, provider, description, detail_json, run_id, raw_response_id, detected_at, "
            "resolved_at, resolution_note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, ?)",
            (issue_id, severity, rule_code, entity_type, entity_id, provider, "seeded", detected_at,
             resolved_at, detected_at))
    return issue_id
