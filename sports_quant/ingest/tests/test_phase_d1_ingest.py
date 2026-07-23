"""Phase D1 ingestion: provider-audit and ingest-venues (mocked transports).

No live provider call is made. Every probe uses a mocked transport. A
whole-database secret sweep asserts the sentinel BALLDONTLIE key never lands in
any stored column. These tests pin the D1 *integrity repair*: a static
declaration is never persisted as an endpoint observation, one probe verifies
only its own capability group, and auth/tier failures are classified honestly.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.provider_audit import (
    audit_provider,
    build_balldontlie_probes,
    build_mlb_statsapi_probes,
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


def _mlb_decl():
    return declaration_for(PROVIDER_MLB_STATSAPI, balldontlie_tier=BalldontlieTier.GOAT)


def _bdl_decl():
    return declaration_for(PROVIDER_BALLDONTLIE, balldontlie_tier=BalldontlieTier.GOAT)


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
    assert result.aliases_inserted == 2
    assert result.aliases_unchanged == 0
    assert result.aliases_conflict == 0
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
    # The aliases already exist unchanged -- an INSERT OR IGNORE no-op is NOT
    # counted as a fresh insert.
    assert second.aliases_inserted == 0
    assert second.aliases_unchanged == 2
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM venue_aliases").fetchone()[0] == 2


async def test_ingest_venues_alias_conflict_is_surfaced_not_arbitrary(db: Database) -> None:
    """Two venues sharing a provider id is an ambiguity: surfaced, never resolved.

    The StatsAPI would never do this, but the corpus must not silently pick one
    canonical venue for a re-used ``(provider, provider_venue_id)``.
    """

    body = {
        "venues": [
            {"id": 999, "name": "Stadium A", "location": {"city": "Alpha"}, "fieldInfo": {}},
            {"id": 999, "name": "Stadium B", "location": {"city": "Beta"}, "fieldInfo": {}},
        ]
    }
    result = await ingest_venues(database=db, client=_mlb_client(body))
    assert result.venues_inserted == 2  # two distinct canonical venues by name
    assert result.aliases_inserted == 1  # first bound the provider id
    assert result.aliases_conflict == 1  # second is a conflict, not an insert
    with db.connection() as conn:
        # Exactly one alias holds provider_venue_id 999; the conflict was not written.
        bound = conn.execute(
            "SELECT COUNT(*) FROM venue_aliases WHERE provider_venue_id = '999'"
        ).fetchone()[0]
        assert bound == 1
        note = conn.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE rule_code = 'DQ-VENUE-ALIAS-001'"
        ).fetchone()[0]
        assert note == 1


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
# provider-audit: declared vs observed separation
# --------------------------------------------------------------------------- #
async def test_audit_mlb_marks_only_probed_groups_observed(db: Database) -> None:
    """A successful audit observes ONLY the probed groups; the rest stay declared.

    The MLB probes cover teams / schedule / venues, so exactly those capabilities
    (teams, schedules, games, venues) may be ``is_observed = 1``. Player stats,
    injuries, plays, lineups, etc. must remain declared-only.
    """

    client = _mlb_client(VENUES_BODY)
    result = await audit_provider(
        database=db,
        provider=PROVIDER_MLB_STATSAPI,
        probes=build_mlb_statsapi_probes(client),
        declaration=_mlb_decl(),
    )
    await client.aclose()
    assert result.status == "succeeded"
    assert result.authenticated is True

    with db.connection() as conn:
        observed = {
            r[0]
            for r in conn.execute(
                "SELECT capability FROM provider_capabilities "
                "WHERE provider='mlb_statsapi' AND is_observed = 1"
            )
        }
        # Only the probed groups are observed.
        assert observed == {"teams", "schedules", "games", "venues"}
        # An observed row carries evidence: a raw_response_id and an http_status.
        row = conn.execute(
            "SELECT raw_response_id, http_status, probe_name, endpoint, observed_state "
            "FROM provider_capabilities WHERE provider='mlb_statsapi' AND capability='teams' "
            "AND is_observed = 1"
        ).fetchone()
        assert row["raw_response_id"] is not None
        assert row["http_status"] == 200
        assert row["probe_name"] == "teams"
        assert row["endpoint"] == "/teams"
        assert row["observed_state"] == "supported"
        # A never-probed capability is declared-only: is_observed = 0, no evidence.
        starters = conn.execute(
            "SELECT is_observed, observed_state, raw_response_id, declared_state "
            "FROM provider_capabilities "
            "WHERE provider='mlb_statsapi' AND capability='confirmed_pregame_starters'"
        ).fetchone()
        assert starters["is_observed"] == 0
        assert starters["observed_state"] is None
        assert starters["raw_response_id"] is None
        assert starters["declared_state"] == "unavailable"
        # Player statistics were NOT probed for MLB -> declared-only, never observed.
        pstats_observed = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='mlb_statsapi' AND capability='player_statistics' AND is_observed = 1"
        ).fetchone()[0]
        assert pstats_observed == 0


async def test_audit_balldontlie_tier_restriction_is_per_group(db: Database) -> None:
    """One tier-restricted endpoint restricts ONLY its group; others still observe.

    teams/players/games answer 200 (observed supported); stats/box_scores/injuries
    answer a plan-gated 403 (observed paid_tier_required) -- and the run still
    succeeds and keeps probing the unrelated groups.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in ("/v1/stats", "/v1/box_scores", "/v1/player_injuries"):
            return httpx.Response(
                403,
                json={"error": "this endpoint requires a higher plan tier"},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200, json={"data": [{"id": 1}]}, headers={"content-type": "application/json"}
        )

    client = _bdl_client(handler)
    result = await audit_provider(
        database=db,
        provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client),
        declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "succeeded"
    assert result.tier_restricted is True
    assert result.authenticated is True

    with db.connection() as conn:
        teams = conn.execute(
            "SELECT observed_state FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='teams' AND is_observed=1"
        ).fetchone()
        assert teams["observed_state"] == "supported"
        pstats = conn.execute(
            "SELECT observed_state, error_kind FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='player_statistics' AND is_observed=1"
        ).fetchone()
        assert pstats["observed_state"] == "paid_tier_required"
        assert pstats["error_kind"] == "tier_restricted"
        notes = conn.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE rule_code = 'DQ-CAP-001'"
        ).fetchone()[0]
        assert notes > 0


async def test_audit_401_records_no_supported_observation(db: Database) -> None:
    """A 401 fails the run and creates NO supported/observed capability at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "unauthorized"}, headers={"content-type": "application/json"}
        )

    client = _bdl_client(handler)
    result = await audit_provider(
        database=db,
        provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client),
        declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "failed"
    assert result.authenticated is False
    with db.connection() as conn:
        observed = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND is_observed = 1"
        ).fetchone()[0]
        assert observed == 0
        supported = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND observed_state = 'supported'"
        ).fetchone()[0]
        assert supported == 0


async def test_audit_generic_403_is_unavailable_not_paid_tier(db: Database) -> None:
    """A 403 with no plan evidence is UNAVAILABLE/forbidden, never paid_tier_required."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/stats":
            return httpx.Response(
                403, json={"error": "forbidden"}, headers={"content-type": "application/json"}
            )
        return httpx.Response(
            200, json={"data": []}, headers={"content-type": "application/json"}
        )

    client = _bdl_client(handler)
    result = await audit_provider(
        database=db,
        provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client),
        declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "succeeded"
    assert result.tier_restricted is False  # a generic 403 is NOT a tier restriction
    with db.connection() as conn:
        pstats = conn.execute(
            "SELECT observed_state, error_kind FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='player_statistics' AND is_observed=1"
        ).fetchone()
        assert pstats["observed_state"] == "unavailable"
        assert pstats["error_kind"] == "forbidden"
        # Nothing got mislabelled paid_tier_required from a bare 403.
        paid = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='player_statistics' "
            "AND observed_state='paid_tier_required'"
        ).fetchone()[0]
        assert paid == 0


async def test_audit_unprobed_capabilities_are_declared_only(db: Database) -> None:
    """plays/lineups/starters have no documented probe -> declared-only, never observed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []}, headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    await audit_provider(
        database=db,
        provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client),
        declaration=_bdl_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        for cap in ("plays", "lineups", "confirmed_pregame_starters", "substitutions"):
            row = conn.execute(
                "SELECT is_observed, observed_state, raw_response_id FROM provider_capabilities "
                "WHERE provider='balldontlie' AND capability=?",
                (cap,),
            ).fetchone()
            assert row is not None, f"{cap} not recorded"
            assert row["is_observed"] == 0, f"{cap} must not be observed (no documented probe)"
            assert row["observed_state"] is None
            assert row["raw_response_id"] is None


async def test_audit_dry_run_persists_nothing(db: Database) -> None:
    client = _mlb_client(VENUES_BODY)
    result = await audit_provider(
        database=db,
        provider=PROVIDER_MLB_STATSAPI,
        probes=build_mlb_statsapi_probes(client),
        declaration=_mlb_decl(),
        dry_run=True,
    )
    await client.aclose()
    assert result.dry_run is True
    assert result.run_id is None
    assert result.observed_count > 0  # would-be observations reported in memory
    with db.connection() as conn:
        for table in ("ingestion_runs", "raw_responses", "provider_capabilities", "data_quality_issues"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_audit_appends_history_never_overwrites(db: Database) -> None:
    """A second audit appends new point-in-time rows; the first belief survives."""

    client1 = _mlb_client(VENUES_BODY)
    await audit_provider(
        database=db,
        provider=PROVIDER_MLB_STATSAPI,
        probes=build_mlb_statsapi_probes(client1),
        declaration=_mlb_decl(),
    )
    await client1.aclose()
    with db.connection() as conn:
        first_count = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities WHERE provider='mlb_statsapi'"
        ).fetchone()[0]

    # A later audit where teams now 403s -> a new observation, old rows untouched.
    def handler(request: httpx.Request) -> httpx.Response:
        # The base path /api/v1 is part of the URL the handler sees.
        if request.url.path == "/api/v1/teams":
            return httpx.Response(
                403, json={"error": "forbidden"}, headers={"content-type": "application/json"}
            )
        return httpx.Response(200, json=VENUES_BODY, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    client2 = MlbStatsApiClient(client=http)
    await audit_provider(
        database=db,
        provider=PROVIDER_MLB_STATSAPI,
        probes=build_mlb_statsapi_probes(client2),
        declaration=_mlb_decl(),
    )
    await client2.aclose()
    with db.connection() as conn:
        second_count = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities WHERE provider='mlb_statsapi'"
        ).fetchone()[0]
        # History grew; the original supported-teams observation is still present.
        assert second_count > first_count
        teams_states = {
            r[0]
            for r in conn.execute(
                "SELECT observed_state FROM provider_capabilities "
                "WHERE provider='mlb_statsapi' AND capability='teams' AND is_observed=1"
            )
        }
        assert "supported" in teams_states and "unavailable" in teams_states


# --------------------------------------------------------------------------- #
# Secret sweep + GET-only
# --------------------------------------------------------------------------- #
async def test_whole_database_never_stores_the_balldontlie_key(db: Database) -> None:
    # Successful audit (200) so rows are actually written, key in the header only.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": 1, "name": "Team"}]},
                              headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    await audit_provider(
        database=db,
        provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client),
        declaration=_bdl_decl(),
    )
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
