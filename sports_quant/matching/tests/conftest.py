"""Fixtures and offline seeding helpers for the D5A matching tests.

Everything here is mocked/offline: a fresh migrated corpus, plus small typed
helpers that write canonical teams / players / venues and provider schedule
observations directly through the real repositories. No network, no provider
client.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.ingestion_runs import SqliteIngestionRunRepository
from sports_quant.db.repositories.official_games import SqliteScheduleRepository
from sports_quant.db.repositories.players import SqlitePlayerAliasRepository, SqlitePlayerRepository
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    response_content_hash,
)
from sports_quant.db.repositories.references import SqliteProviderReferenceRepository
from sports_quant.db.repositories.teams import SqliteTeamAliasRepository, SqliteTeamRepository
from sports_quant.db.repositories.venues import SqliteVenueRepository
from sports_quant.db.schema import to_iso, utc_now

T0 = "2026-07-24T18:00:00.000000Z"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "corpus.db"
    initialize_database(path)
    return path


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with Database(db_path).connection() as connection:
        yield connection


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def raw_response(conn: sqlite3.Connection, marker: str = "seed") -> tuple[str, str]:
    """A committed run + raw response, returning ``(raw_response_id, hash)``."""

    runs = SqliteIngestionRunRepository(conn)
    run = runs.start(
        command="seed", provider="test", operation="seed", args_json="{}",
        started_monotonic_ns=0, tool_version="t",
    )
    raw = SqliteRawResponseRepository(conn).store(
        run_id=run.run_id, provider="test", endpoint="/seed", request_params_json="{}",
        http_status=200, response_headers_json="{}", requested_at=to_iso(utc_now()),
        received_at=to_iso(utc_now()), elapsed_ns=1, body="{}",
        content_hash=response_content_hash(provider="test", endpoint="/seed", request_params={}, body=marker),
    )
    return raw.raw_response_id, raw.content_hash


def seed_team(
    conn: sqlite3.Connection,
    *,
    league_code: str,
    abbreviation: str,
    canonical_name: str,
    city: str,
    nickname: str,
    aliases: Optional[list[tuple[str, str, str]]] = None,
    first_season: Optional[int] = None,
    last_season: Optional[int] = None,
) -> str:
    """Create a canonical team + aliases. ``aliases`` = list of (alias, type, provider)."""

    league_id = f"lg_{league_code.lower()}"
    with transaction(conn):
        teams = SqliteTeamRepository(conn)
        team = teams.upsert(
            league_code=league_code, league_id=league_id, canonical_name=canonical_name,
            city=city, nickname=nickname, abbreviation=abbreviation,
            first_season=first_season, last_season=last_season,
        )
        alias_repo = SqliteTeamAliasRepository(conn)
        for alias, alias_type, provider in aliases or []:
            alias_repo.add(
                team_id=team.team_id, league_id=league_id, alias=alias,
                alias_type=alias_type, provider=provider, source="seed",
            )
    return team.team_id


def seed_team_alias(
    conn: sqlite3.Connection, *, team_id: str, league_code: str, alias: str,
    alias_type: str = "provider", provider: str = "", valid_from: int = 0, valid_to: int = 9999,
) -> None:
    with transaction(conn):
        SqliteTeamAliasRepository(conn).add(
            team_id=team_id, league_id=f"lg_{league_code.lower()}", alias=alias,
            alias_type=alias_type, provider=provider, valid_from_season=valid_from,
            valid_to_season=valid_to, source="seed",
        )


def mark_team_ambiguous(conn: sqlite3.Connection, league_code: str) -> int:
    with transaction(conn):
        return SqliteTeamAliasRepository(conn).mark_ambiguous_duplicates(f"lg_{league_code.lower()}")


def seed_player(
    conn: sqlite3.Connection,
    *,
    league_code: str,
    full_name: str,
    suffix: Optional[str] = None,
    birth_date: Optional[str] = None,
    debut_date: Optional[str] = None,
    final_game_date: Optional[str] = None,
    aliases: Optional[list[tuple[str, str, str]]] = None,
) -> str:
    """Create a canonical player + aliases. ``aliases`` = (alias, type, provider)."""

    league_id = f"lg_{league_code.lower()}"
    with transaction(conn):
        players = SqlitePlayerRepository(conn)
        player = players.create(
            league_id=league_id, full_name=full_name, suffix=suffix, birth_date=birth_date,
            debut_date=debut_date, final_game_date=final_game_date,
        )
        alias_repo = SqlitePlayerAliasRepository(conn)
        for alias, alias_type, provider in aliases or [(full_name, "full", "")]:
            alias_repo.add(
                player_id=player.player_id, league_id=league_id, alias=alias,
                alias_type=alias_type, provider=provider, source="seed",
            )
    return player.player_id


def seed_venue(
    conn: sqlite3.Connection,
    *,
    name: str,
    provider: str = "mlb_statsapi",
    provider_venue_id: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    timezone: Optional[str] = None,
    country: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    roof_type: Optional[str] = None,
) -> str:
    rid, rhash = raw_response(conn, marker=f"venue:{name}")
    with transaction(conn):
        venues = SqliteVenueRepository(conn)
        venue, _ = venues.upsert(
            name=name, raw_response_id=rid, raw_response_hash=rhash, observed_at=T0,
            timezone=timezone, country=country, latitude=latitude, longitude=longitude,
            roof_type=roof_type,
        )
        if provider_venue_id is not None:
            venues.add_alias(
                venue_id=venue.venue_id, alias=name, provider=provider,
                provider_venue_id=provider_venue_id,
            )
        for alias in aliases or []:
            venues.add_alias(venue_id=venue.venue_id, alias=alias, provider=provider)
    return venue.venue_id


def seed_player_ref(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_player_id: str,
    observed_at: str = T0,
) -> str:
    """Create an UNLINKED provider-player reference (player_id NULL); returns id."""

    rid, rhash = raw_response(conn, marker=f"playerref:{provider}:{provider_player_id}")
    with transaction(conn):
        ref, _ = SqliteProviderReferenceRepository(conn).upsert(
            kind="player", provider=provider, provider_entity_id=provider_player_id,
            raw_response_id=rid, raw_response_hash=rhash, observed_at=observed_at,
        )
    return ref.reference_id


def link_player_ref(
    conn: sqlite3.Connection, *, provider: str, provider_player_id: str, player_id: str,
) -> None:
    """Directly link a provider-player reference (test setup for scope checks)."""

    with transaction(conn):
        conn.execute(
            "UPDATE provider_player_references SET player_id = ? "
            "WHERE provider = ? AND provider_player_id = ? AND player_id IS NULL",
            (player_id, provider, provider_player_id),
        )


def seed_sb_event(
    conn: sqlite3.Connection,
    *,
    provider_event_id: str,
    sport_key: str,
    commence_time: str,
    home_team_raw: str,
    away_team_raw: str,
    league_code: Optional[str] = "MLB",
    observed_at: str = T0,
) -> str:
    """Create a The Odds API sportsbook event; returns sb_event_id."""

    from sports_quant.db.repositories.sportsbook import SqliteSportsbookRepository

    rid, _rhash = raw_response(conn, marker=f"sbevent:{provider_event_id}:{observed_at}")
    league_id = f"lg_{league_code.lower()}" if league_code else None
    with transaction(conn):
        event = SqliteSportsbookRepository(conn).upsert_event(
            provider="the_odds_api", provider_event_id=provider_event_id, sport_key=sport_key,
            commence_time=commence_time, home_team_raw=home_team_raw, away_team_raw=away_team_raw,
            raw_response_id=rid, observed_at=observed_at, league_id=league_id,
        )
    return event.sb_event_id


def seed_sb_market(
    conn: sqlite3.Connection, *, sb_event_id: str, market_key: str, bookmaker_key: str = "draftkings",
) -> str:
    from sports_quant.db.repositories.sportsbook import SqliteSportsbookRepository

    rid, _rhash = raw_response(conn, marker=f"sbmarket:{sb_event_id}:{bookmaker_key}:{market_key}")
    with transaction(conn):
        market = SqliteSportsbookRepository(conn).upsert_market(
            sb_event_id=sb_event_id, bookmaker_key=bookmaker_key, market_key=market_key,
            raw_response_id=rid, observed_at=T0,
        )
    return market.sb_market_id


def seed_sb_outcome(
    conn: sqlite3.Connection, *, sb_market_id: str, provider_outcome_name: str, outcome_role: str,
    point: Optional[float] = None,
) -> str:
    from sports_quant.db.normalize import normalized_key
    from sports_quant.db.repositories.sportsbook import SqliteSportsbookRepository

    with transaction(conn):
        outcome = SqliteSportsbookRepository(conn).upsert_outcome(
            sb_market_id=sb_market_id, outcome_name=normalized_key(provider_outcome_name),
            provider_outcome_name=provider_outcome_name, outcome_role=outcome_role, point=point,
        )
    return outcome.sb_outcome_id


def seed_sb_price(
    conn: sqlite3.Connection, *, sb_outcome_id: str, price_american: int, observed_at: str = T0,
) -> None:
    from sports_quant.db.repositories.sportsbook import (
        SqliteSportsbookRepository,
        price_content_hash,
    )

    rid, rhash = raw_response(conn, marker=f"sbprice:{sb_outcome_id}:{price_american}:{observed_at}")
    run = SqliteIngestionRunRepository(conn).start(
        command="seed", provider="the_odds_api", operation="seed", args_json="{}",
        started_monotonic_ns=0, tool_version="t")
    with transaction(conn):
        SqliteSportsbookRepository(conn).append_price_snapshot(
            sb_outcome_id=sb_outcome_id, price_american=price_american, observed_at=observed_at,
            raw_response_id=rid, raw_response_hash=rhash, run_id=run.run_id,
            content_hash=price_content_hash(
                price_american=price_american, point=None, bookmaker_last_update=None,
                market_last_update=None, provider_timestamp=None),
        )


def seed_roster(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_team_id: str,
    team_id: str,
    provider_player_id: str,
    player_id: str,
    observed_at: str = T0,
    roster_date: Optional[str] = None,
) -> None:
    """Link a canonical player to a canonical team via a roster observation.

    Creates the provider team reference (linking its canonical ``team_id``
    NULL->value directly, which the identity trigger permits) and one
    ``roster_snapshots`` row carrying the canonical ``player_id`` -- the evidence
    the player resolver uses to disambiguate same-name players by team.
    """

    from sports_quant.db.ids import new_roster_snapshot_id

    rid, rhash = raw_response(conn, marker=f"roster:{provider_team_id}:{provider_player_id}")
    with transaction(conn):
        refs = SqliteProviderReferenceRepository(conn)
        ref, _ = refs.upsert(
            kind="team", provider=provider, provider_entity_id=provider_team_id,
            raw_response_id=rid, raw_response_hash=rhash, observed_at=observed_at,
        )
        conn.execute(
            "UPDATE provider_team_references SET team_id = ? WHERE reference_id = ? "
            "AND team_id IS NULL",
            (team_id, ref.reference_id),
        )
        conn.execute(
            "INSERT INTO roster_snapshots "
            "(roster_id, team_ref_id, provider, provider_team_id, provider_player_id, "
            " player_id, roster_date, observed_at, ingested_at, raw_response_id, "
            " raw_response_hash, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_roster_snapshot_id(), ref.reference_id, provider, provider_team_id,
                provider_player_id, player_id, roster_date, observed_at, observed_at, rid, rhash,
                f"roster-{provider_team_id}-{provider_player_id}", observed_at,
            ),
        )


def seed_schedule(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_game_id: str,
    home_provider_team_id: str,
    away_provider_team_id: str,
    scheduled_start: str,
    season: int,
    game_date_local: Optional[str] = None,
    venue_provider_id: Optional[str] = None,
    game_type: Optional[str] = None,
    game_number: Optional[int] = None,
    mapped_status: str = "scheduled",
    observed_at: str = T0,
) -> str:
    """Create a provider game reference + one schedule snapshot; returns ref id."""

    rid, rhash = raw_response(conn, marker=f"sched:{provider_game_id}:{observed_at}")
    with transaction(conn):
        refs = SqliteProviderReferenceRepository(conn)
        ref, _ = refs.upsert(
            kind="game", provider=provider, provider_entity_id=provider_game_id,
            raw_response_id=rid, raw_response_hash=rhash, observed_at=observed_at,
        )
        SqliteScheduleRepository(conn).append(
            game_ref_id=ref.reference_id, provider=provider, provider_game_id=provider_game_id,
            observed_at=observed_at, ingested_at=observed_at, run_id=None, raw_response_id=rid,
            raw_response_hash=rhash, mapped_status=mapped_status, season=season,
            game_type=game_type, game_date_local=game_date_local, scheduled_start=scheduled_start,
            home_provider_team_id=home_provider_team_id,
            away_provider_team_id=away_provider_team_id, venue_provider_id=venue_provider_id,
            game_number=game_number,
        )
    return ref.reference_id
