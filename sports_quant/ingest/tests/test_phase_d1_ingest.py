"""Phase D1 ingestion: provider-audit and ingest-venues (mocked transports).

No live provider call is made. A whole-database secret sweep asserts the sentinel
BALLDONTLIE key never lands in any stored column.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.provider_audit import (
    SingleGetProbe,
    audit_provider,
    declaration_for,
)
from sports_quant.ingest.venues_ingestor import ingest_venues, normalize_venue
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.providers.capabilities import (
    PROVIDER_BALLDONTLIE,
    PROVIDER_MLB_STATSAPI,
    BalldontlieTier,
)
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

SENTINEL_KEY = "sk-d1-ingest-sentinel-do-not-store"

VENUES_BODY = {
    "venues": [
        {
            "id": 3313,
            "name": "Fenway Park",
            "location": {
                "city": "Boston",
                "country": "USA",
                "defaultCoordinates": {"latitude": 42.3467, "longitude": -71.0972},
                "timeZone": {"id": "America/New_York"},
            },
            "fieldInfo": {"roofType": "Open"},
        },
        {
            "id": 14,
            "name": "Rogers Centre",
            "location": {
                "city": "Toronto",
                "country": "Canada",
                "defaultCoordinates": {"latitude": 43.6414, "longitude": -79.3894},
                "timeZone": {"id": "America/Toronto"},
            },
            "fieldInfo": {"roofType": "Retractable"},
        },
    ]
}


@pytest.fixture
def db(tmp_path: Path) -> Database:
    p = tmp_path / "corpus.db"
    initialize_database(p)
    return Database(p)


def _mlb_client(body: dict, *, status: int = 200) -> MlbStatsApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    return MlbStatsApiClient(client=http)


def _bdl_client(handler) -> BalldontlieClient:
    http = build_readonly_client(
        base_url="https://api.balldontlie.io",
        policy=ReadOnlyHTTPPolicy.for_balldontlie(),
        inner_transport=httpx.MockTransport(handler),
    )
    return BalldontlieClient(SENTINEL_KEY, client=http)


# --------------------------------------------------------------------------- #
# Venue normalization
# --------------------------------------------------------------------------- #
def test_normalize_venue_maps_roof_and_coords() -> None:
    norm, reason = normalize_venue(VENUES_BODY["venues"][0])
    assert reason is None and norm is not None
    assert norm.roof_type == "open"
    assert norm.timezone == "America/New_York"
    assert norm.latitude == pytest.approx(42.3467)


def test_normalize_venue_blank_name_rejected() -> None:
    norm, reason = normalize_venue({"name": "  "})
    assert norm is None and reason is not None


# --------------------------------------------------------------------------- #
# ingest-venues
# --------------------------------------------------------------------------- #
async def test_ingest_venues_seeds_venues_and_aliases(db: Database) -> None:
    result = await ingest_venues(database=db, client=_mlb_client(VENUES_BODY))
    assert result.status == "succeeded"
    assert result.venues_seen == 2
    assert result.venues_inserted == 2
    assert result.aliases_written == 2
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 2
        fenway = conn.execute(
            "SELECT roof_type, is_outdoor, timezone FROM venues WHERE normalized_name = 'fenway park'"
        ).fetchone()
        assert fenway["roof_type"] == "open" and fenway["is_outdoor"] == 1
        rogers = conn.execute(
            "SELECT roof_type FROM venues WHERE normalized_name = 'rogers centre'"
        ).fetchone()
        assert rogers["roof_type"] == "retractable"


async def test_ingest_venues_idempotent(db: Database) -> None:
    await ingest_venues(database=db, client=_mlb_client(VENUES_BODY))
    second = await ingest_venues(database=db, client=_mlb_client(VENUES_BODY))
    # A second live ingest has a newer observed_at -> venues update, no new rows.
    assert second.venues_inserted == 0
    assert second.venues_updated == 2
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 2


async def test_ingest_venues_dry_run_persists_nothing(db: Database) -> None:
    result = await ingest_venues(database=db, client=_mlb_client(VENUES_BODY), dry_run=True)
    assert result.dry_run is True
    assert result.run_id is None
    assert result.venues_seen == 2
    with db.connection() as conn:
        for table in ("ingestion_runs", "raw_responses", "venues", "venue_aliases"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_ingest_venues_preserves_raw_provenance(db: Database) -> None:
    result = await ingest_venues(database=db, client=_mlb_client(VENUES_BODY))
    with db.connection() as conn:
        raw = conn.execute("SELECT raw_response_id, body FROM raw_responses").fetchone()
        assert raw is not None and "Fenway Park" in raw["body"]
        dangling = conn.execute(
            "SELECT COUNT(*) FROM venues v LEFT JOIN raw_responses r "
            "ON v.first_raw_response_id = r.raw_response_id WHERE r.raw_response_id IS NULL"
        ).fetchone()[0]
        assert dangling == 0
    assert result.run_id is not None


async def test_ingest_venues_http_failure_is_failed(db: Database) -> None:
    result = await ingest_venues(database=db, client=_mlb_client({"error": "boom"}, status=500))
    assert result.failed is True
    assert result.status == "failed"


# --------------------------------------------------------------------------- #
# provider-audit
# --------------------------------------------------------------------------- #
async def test_audit_mlb_records_capabilities(db: Database) -> None:
    client = _mlb_client(VENUES_BODY)
    decl = declaration_for(PROVIDER_MLB_STATSAPI, balldontlie_tier=BalldontlieTier.GOAT)
    probe = SingleGetProbe(declaration=decl, fetch=client.fetch_venues)
    result = await audit_provider(database=db, provider=PROVIDER_MLB_STATSAPI, probe=probe)
    await client.aclose()
    assert result.status == "succeeded"
    assert result.authenticated is True
    assert result.capabilities_recorded > 0
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities WHERE provider = 'mlb_statsapi'"
        ).fetchone()[0]
        assert rows == result.capabilities_recorded


async def test_audit_balldontlie_tier_restriction_not_failure(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "requires a higher plan tier"},
                              headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    decl = declaration_for(PROVIDER_BALLDONTLIE, balldontlie_tier=BalldontlieTier.GOAT)
    probe = SingleGetProbe(declaration=decl, fetch=client.fetch_teams)
    result = await audit_provider(database=db, provider=PROVIDER_BALLDONTLIE, probe=probe)
    await client.aclose()
    # A tier restriction is honest capability info, not a failed run.
    assert result.status == "succeeded"
    assert result.tier_restricted is True
    assert result.authenticated is True
    with db.connection() as conn:
        paid = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND state='paid_tier_required'"
        ).fetchone()[0]
        assert paid > 0
        # A DQ-CAP note was recorded for the gaps.
        notes = conn.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE rule_code = 'DQ-CAP-001'"
        ).fetchone()[0]
        assert notes > 0


async def test_audit_dry_run_persists_nothing(db: Database) -> None:
    client = _mlb_client(VENUES_BODY)
    decl = declaration_for(PROVIDER_MLB_STATSAPI, balldontlie_tier=BalldontlieTier.GOAT)
    probe = SingleGetProbe(declaration=decl, fetch=client.fetch_venues)
    result = await audit_provider(
        database=db, provider=PROVIDER_MLB_STATSAPI, probe=probe, dry_run=True
    )
    await client.aclose()
    assert result.dry_run is True
    assert result.run_id is None
    assert len(result.observations) > 0  # would-be capabilities reported
    with db.connection() as conn:
        for table in ("ingestion_runs", "raw_responses", "provider_capabilities", "data_quality_issues"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Secret sweep + GET-only
# --------------------------------------------------------------------------- #
async def test_whole_database_never_stores_the_balldontlie_key(db: Database) -> None:
    # Successful audit (200) so rows are actually written, key in the header only.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": 1, "name": "Team"}]},
                              headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    decl = declaration_for(PROVIDER_BALLDONTLIE, balldontlie_tier=BalldontlieTier.GOAT)
    probe = SingleGetProbe(declaration=decl, fetch=client.fetch_teams)
    await audit_provider(database=db, provider=PROVIDER_BALLDONTLIE, probe=probe)
    await client.aclose()

    offenders = []
    with db.connection() as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                for col, value in zip(cols, row, strict=True):
                    if isinstance(value, str) and SENTINEL_KEY in value:
                        offenders.append(f"{table}.{col}")
    assert not offenders, f"BALLDONTLIE key leaked into: {offenders}"


async def test_every_request_is_get(db: Database) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json=VENUES_BODY, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    client = MlbStatsApiClient(client=http)
    await ingest_venues(database=db, client=client)
    await client.aclose()
    assert seen and set(seen) == {"GET"}
