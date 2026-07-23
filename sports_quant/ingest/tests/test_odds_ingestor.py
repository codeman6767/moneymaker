"""Odds ingestion: normalization, idempotency, partial data, dry-run, counts."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.ingest.odds_ingestor import (
    american_to_decimal,
    american_to_implied,
    ingest_odds,
    is_valid_american,
)

from .conftest import mlb_payload, nba_payload


def _snap_count(database: Database) -> int:
    with database.connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sportsbook_price_snapshots").fetchone()[0])


async def test_mlb_odds_are_normalized(database: Database, make_client, client_for) -> None:
    client = make_client(client_for(mlb_payload()))
    result = await ingest_odds(database=database, client=client, sport="mlb")

    assert result.status == "succeeded"
    assert result.events_seen == 1
    # h2h(2) + spreads(2) + totals(2) = 6 outcomes/observations.
    assert result.markets_seen == 3
    assert result.outcomes_seen == 6
    assert result.snapshots_inserted == 6
    assert result.records_rejected == 0

    with database.connection() as conn:
        event = conn.execute(
            "SELECT provider, provider_event_id, sport_key, league_id, home_team_raw, "
            "away_team_raw, game_id FROM sportsbook_events"
        ).fetchone()
    assert event["provider"] == "the_odds_api"
    assert event["provider_event_id"] == "mlb-event-1"
    assert event["sport_key"] == "baseball_mlb"
    # league resolved from the static sport_key map, not a name match.
    assert event["league_id"] == "lg_mlb"
    assert event["home_team_raw"] == "New York Yankees"
    # No fuzzy matching in Phase B: game link stays NULL.
    assert event["game_id"] is None


async def test_nba_odds_are_normalized(database: Database, make_client, client_for) -> None:
    client = make_client(client_for(nba_payload()))
    result = await ingest_odds(database=database, client=client, sport="nba")

    assert result.status == "succeeded"
    assert result.events_seen == 1
    assert result.outcomes_seen == 2
    with database.connection() as conn:
        league = conn.execute("SELECT league_id FROM sportsbook_events").fetchone()[0]
    assert league == "lg_nba"


async def test_h2h_spreads_and_totals_all_persist(
    database: Database, make_client, client_for
) -> None:
    client = make_client(client_for(mlb_payload()))
    await ingest_odds(database=database, client=client, sport="mlb")

    with database.connection() as conn:
        market_keys = {
            r[0] for r in conn.execute("SELECT DISTINCT market_key FROM sportsbook_markets")
        }
        roles = {
            r[0] for r in conn.execute("SELECT DISTINCT outcome_role FROM sportsbook_outcomes")
        }
        # totals carry a point; the identity keys them by it.
        totals_points = sorted(
            r[0]
            for r in conn.execute(
                "SELECT o.point FROM sportsbook_outcomes o JOIN sportsbook_markets m "
                "ON o.sb_market_id = m.sb_market_id WHERE m.market_key = 'totals'"
            )
        )
    assert market_keys == {"h2h", "spreads", "totals"}
    assert {"home", "away", "over", "under"} <= roles
    assert totals_points == [8.5, 8.5]


async def test_repeated_ingestion_does_not_duplicate_snapshots(
    database: Database, make_client, client_for
) -> None:
    first = await ingest_odds(
        database=database, client=make_client(client_for(mlb_payload())), sport="mlb"
    )
    second = await ingest_odds(
        database=database, client=make_client(client_for(mlb_payload())), sport="mlb"
    )

    assert first.snapshots_inserted == 6
    assert second.snapshots_inserted == 0
    assert second.snapshots_duplicate == 6
    assert _snap_count(database) == 6
    # Identity rows are not re-created either.
    with database.connection() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM sportsbook_outcomes").fetchone()[0]) == 6
        assert int(conn.execute("SELECT COUNT(*) FROM sportsbook_events").fetchone()[0]) == 1


async def test_changed_price_creates_a_new_snapshot(
    database: Database, make_client, client_for
) -> None:
    await ingest_odds(
        database=database, client=make_client(client_for(mlb_payload())), sport="mlb"
    )

    moved = copy.deepcopy(mlb_payload())
    # Only the Yankees moneyline drifts -140 -> -150. The other five prices are
    # unchanged, so exactly one new snapshot should be written.
    moved[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -150
    result = await ingest_odds(
        database=database, client=make_client(client_for(moved)), sport="mlb"
    )

    # Exactly one new snapshot: the moved line. The five unchanged prices dedup.
    assert result.snapshots_inserted == 1
    assert result.snapshots_duplicate == 5
    assert _snap_count(database) == 7

    # Both prices are preserved for the same outcome identity.
    with database.connection() as conn:
        prices = sorted(
            r[0]
            for r in conn.execute(
                "SELECT s.price_american FROM sportsbook_price_snapshots s "
                "JOIN sportsbook_outcomes o ON s.sb_outcome_id = o.sb_outcome_id "
                "JOIN sportsbook_markets m ON o.sb_market_id = m.sb_market_id "
                "WHERE m.market_key = 'h2h' AND o.outcome_role = 'home'"
            )
        )
    assert prices == [-150, -140]


async def test_partial_response_is_handled_safely(
    database: Database, make_client, client_for
) -> None:
    """One malformed event does not abort ingestion of the good one."""

    payload: list[dict[str, Any]] = mlb_payload() + [
        {  # malformed: no provider id, no commence time
            "sport_key": "baseball_mlb",
            "home_team": "A",
            "away_team": "B",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [
                        {"key": "h2h", "outcomes": [{"name": "A", "price": -110}]}
                    ],
                }
            ],
        }
    ]
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )

    # The good event still landed; the bad one was rejected and counted.
    assert result.events_seen == 1
    assert result.events_rejected == 1
    assert result.records_rejected >= 1
    assert result.status == "partially_succeeded"
    assert _snap_count(database) == 6


async def test_malformed_odds_are_rejected(database: Database, make_client, client_for) -> None:
    payload = copy.deepcopy(mlb_payload())
    # An American price of 50 is impossible (magnitude < 100): malformed.
    payload[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 50
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )

    assert result.records_rejected == 1
    assert any("malformed American odds" in r for r in result.rejections)
    # The other five outcomes still stored.
    assert _snap_count(database) == 5


async def test_spread_outcome_missing_point_is_rejected(
    database: Database, make_client, client_for
) -> None:
    payload = copy.deepcopy(mlb_payload())
    del payload[0]["bookmakers"][0]["markets"][1]["outcomes"][0]["point"]
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert result.records_rejected == 1
    assert any("missing required point" in r for r in result.rejections)


async def test_unsupported_market_is_rejected(
    database: Database, make_client, client_for
) -> None:
    payload = copy.deepcopy(mlb_payload())
    payload[0]["bookmakers"][0]["markets"].append(
        {
            "key": "player_props",
            "last_update": "2026-07-22T22:50:00Z",
            "outcomes": [{"name": "Aaron Judge Over 1.5", "price": -120, "point": 1.5}],
        }
    )
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert any("unsupported market" in r for r in result.rejections)
    with database.connection() as conn:
        keys = {r[0] for r in conn.execute("SELECT DISTINCT market_key FROM sportsbook_markets")}
    assert "player_props" not in keys


async def test_blank_home_team_is_rejected(database: Database, make_client, client_for) -> None:
    payload = copy.deepcopy(mlb_payload())
    payload[0]["home_team"] = "   "
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert result.events_seen == 0
    assert result.events_rejected == 1
    assert any("home team" in r for r in result.rejections)
    assert _snap_count(database) == 0
    with database.connection() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM sportsbook_events").fetchone()[0]) == 0


async def test_missing_away_team_is_rejected(database: Database, make_client, client_for) -> None:
    payload = copy.deepcopy(mlb_payload())
    del payload[0]["away_team"]
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert result.events_rejected == 1
    assert any("away team" in r for r in result.rejections)
    assert _snap_count(database) == 0


async def test_identical_teams_are_rejected(database: Database, make_client, client_for) -> None:
    payload = copy.deepcopy(mlb_payload())
    # Same name, different punctuation/case -> identical after normalization.
    payload[0]["home_team"] = "New York Yankees"
    payload[0]["away_team"] = "new york yankees"
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert result.events_rejected == 1
    assert any("identical" in r for r in result.rejections)
    assert _snap_count(database) == 0


async def test_sport_key_mismatch_is_rejected(database: Database, make_client, client_for) -> None:
    """A basketball payload returned to the baseball endpoint is not persisted."""

    payload = copy.deepcopy(mlb_payload())
    payload[0]["sport_key"] = "basketball_nba"  # wrong league for this endpoint
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert result.events_seen == 0
    assert result.events_rejected == 1
    assert any("sport_key mismatch" in r for r in result.rejections)
    with database.connection() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM sportsbook_events").fetchone()[0]) == 0
        assert int(
            conn.execute("SELECT COUNT(*) FROM sportsbook_price_snapshots").fetchone()[0]
        ) == 0


async def test_valid_event_survives_alongside_a_mismatched_one(
    database: Database, make_client, client_for
) -> None:
    payload = mlb_payload() + copy.deepcopy(mlb_payload())
    payload[1]["id"] = "mlb-event-2"
    payload[1]["sport_key"] = "basketball_nba"  # mismatch
    result = await ingest_odds(
        database=database, client=make_client(client_for(payload)), sport="mlb"
    )
    assert result.events_seen == 1
    assert result.events_rejected == 1
    assert result.status == "partially_succeeded"
    with database.connection() as conn:
        ids = {r[0] for r in conn.execute("SELECT provider_event_id FROM sportsbook_events")}
    assert ids == {"mlb-event-1"}


async def test_no_games_available_is_success_not_failure(
    database: Database, make_client, client_for
) -> None:
    result = await ingest_odds(
        database=database, client=make_client(client_for([])), sport="mlb"
    )
    assert result.status == "succeeded"
    assert result.events_seen == 0
    assert result.records_received == 0
    assert _snap_count(database) == 0


async def test_ingestion_run_counts_are_correct(
    database: Database, make_client, client_for
) -> None:
    result = await ingest_odds(
        database=database, client=make_client(client_for(mlb_payload())), sport="mlb"
    )
    with database.connection() as conn:
        run = conn.execute(
            "SELECT status, requests_made, records_received, records_normalized, "
            "records_inserted, records_deduplicated, records_rejected, completed_at, "
            "duration_ns FROM ingestion_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    assert run["status"] == "succeeded"
    assert run["requests_made"] == 1
    assert run["records_received"] == 6
    assert run["records_normalized"] == 6
    assert run["records_inserted"] == 6
    assert run["records_deduplicated"] == 0
    assert run["records_rejected"] == 0
    assert run["completed_at"] is not None
    assert run["duration_ns"] is not None and run["duration_ns"] >= 0


async def test_dry_run_persists_nothing(database: Database, make_client, client_for) -> None:
    result = await ingest_odds(
        database=database, client=make_client(client_for(mlb_payload())), sport="mlb", dry_run=True
    )
    assert result.dry_run is True
    assert result.run_id is None
    # Reports the counts a real run would have produced...
    assert result.events_seen == 1
    assert result.outcomes_seen == 6
    # ...but wrote nothing at all.
    with database.connection() as conn:
        for table in (
            "ingestion_runs",
            "raw_responses",
            "sportsbook_events",
            "sportsbook_markets",
            "sportsbook_outcomes",
            "sportsbook_price_snapshots",
        ):
            assert int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) == 0


async def test_http_failure_marks_run_failed_but_preserves_bytes(
    database: Database, make_client, client_for
) -> None:
    client = make_client(client_for({"message": "quota exceeded"}, status=429))
    result = await ingest_odds(database=database, client=client, sport="mlb")

    assert result.failed is True
    assert result.status == "failed"
    assert result.error_type is not None
    # A completed 4xx/5xx round-trip counts as one request.
    assert result.requests_made == 1
    # The failed run is recorded, and its response body is still preserved for a
    # later re-parse -- a parse/HTTP failure never loses the bytes.
    with database.connection() as conn:
        run = conn.execute(
            "SELECT status, error_type, requests_made FROM ingestion_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        raw = conn.execute(
            "SELECT http_status, body FROM raw_responses WHERE run_id = ?", (result.run_id,)
        ).fetchone()
    assert run["status"] == "failed"
    assert run["requests_made"] == 1
    assert raw["http_status"] == 429
    assert "quota exceeded" in raw["body"]


async def test_pre_request_failure_counts_zero_requests(database: Database, make_client) -> None:
    """A connect/DNS failure before any response arrives records requests_made = 0."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    result = await ingest_odds(database=database, client=make_client(handler), sport="mlb")
    assert result.status == "failed"
    assert result.requests_made == 0
    with database.connection() as conn:
        run = conn.execute(
            "SELECT requests_made FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()
        # No HTTP response arrived, so nothing was preserved for this run.
        raw_count = conn.execute(
            "SELECT COUNT(*) FROM raw_responses WHERE run_id = ?", (result.run_id,)
        ).fetchone()[0]
    assert run["requests_made"] == 0
    assert raw_count == 0


async def test_every_request_is_a_get(database: Database, make_client) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=mlb_payload())

    await ingest_odds(database=database, client=make_client(handler), sport="mlb")
    assert seen and {r.method for r in seen} == {"GET"}


def test_american_odds_arithmetic() -> None:
    assert is_valid_american(-110)
    assert is_valid_american(150)
    assert not is_valid_american(50)
    assert not is_valid_american(-99)
    assert not is_valid_american(0)
    assert american_to_decimal(100) == pytest.approx(2.0)
    assert american_to_decimal(-200) == pytest.approx(1.5)
    assert american_to_implied(100) == pytest.approx(0.5)
    assert american_to_implied(-200) == pytest.approx(2 / 3)
