"""Phase D1 ingestion: provider-audit and ingest-venues (mocked transports).

No live provider call is made. Every probe uses a mocked transport. A
whole-database secret sweep asserts the sentinel BALLDONTLIE key never lands in
any stored column. These tests pin the D1 *integrity repair*: a static
declaration is never persisted as an endpoint observation, one probe verifies
only its own capability group, and auth/tier failures are classified honestly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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


def _bdl_client(handler, **kwargs) -> BalldontlieClient:
    http = build_readonly_client(
        base_url="https://api.balldontlie.io",
        policy=ReadOnlyHTTPPolicy.for_balldontlie(),
        inner_transport=httpx.MockTransport(handler),
    )
    return BalldontlieClient(SENTINEL_KEY, client=http, **kwargs)


#: A deterministic BALLDONTLIE handler with a game (id + date) to seed dependent
#: probes. The default games row carries a valid ISO date so the box-score probe
#: can extract it.
def _bdl_routing_handler(*, plays_body: Optional[dict] = None, games_body: Optional[dict] = None):
    plays = plays_body if plays_body is not None else {"data": [{"id": 1, "type": "shot"}]}
    games = (
        games_body
        if games_body is not None
        else {"data": [{"id": 18444208, "date": "2024-04-09"}]}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict = {"data": [{"id": 1}]}
        if path == "/v1/games":
            body = games
        elif path == "/v1/plays":
            body = plays
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    return handler


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
    # MLB StatsAPI is keyless -> authentication is not applicable (None).
    assert result.authenticated is None
    assert result.auth_applicable is False

    with db.connection() as conn:
        observed = {
            r[0]
            for r in conn.execute(
                "SELECT capability FROM provider_capabilities "
                "WHERE provider='mlb_statsapi' AND is_observed = 1"
            )
        }
        # Only the probed groups are observed (players via roster/person is
        # dependency-aware and here has no team id, so it stays unobserved).
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


async def test_audit_unobserved_capabilities_are_never_observed(db: Database) -> None:
    """With an empty games response, the game-dependent capabilities (plays,
    lineups) are skipped and starters/substitutions stay declared-only -- none is
    ever marked observed or carries evidence."""

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
            assert row["is_observed"] == 0, f"{cap} must not be observed"
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
# Dependency-aware probes (plays / lineups / advanced / MLB roster)
# --------------------------------------------------------------------------- #
async def test_audit_balldontlie_dependent_probes_use_game_id(db: Database) -> None:
    """plays/lineups/advanced are observed via their own endpoints once a game id
    is extracted from the games response -- each with its own raw-response
    evidence, never the games response."""

    client = _bdl_client(_bdl_routing_handler())
    await audit_provider(
        database=db,
        provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client),
        declaration=_bdl_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        rows = {
            r["capability"]: (r["endpoint"], r["raw_response_id"], r["observed_state"])
            for r in conn.execute(
                "SELECT capability, endpoint, raw_response_id, observed_state "
                "FROM provider_capabilities WHERE provider='balldontlie' AND is_observed=1"
            )
        }
        assert rows["plays"][0] == "/v1/plays"
        assert rows["lineups"][0] == "/v1/lineups"
        assert rows["advanced_statistics"][0] == "/nba/v1/stats/advanced"
        # Each dependent capability links its OWN raw response, distinct from games.
        games_raw = rows["games"][1]
        assert rows["plays"][1] not in (None, games_raw)
        assert rows["lineups"][1] not in (None, games_raw)
        assert rows["advanced_statistics"][1] not in (None, games_raw)
        assert len({rows["plays"][1], rows["lineups"][1], rows["advanced_statistics"][1]}) == 3


async def test_audit_no_game_marks_dependents_unknown_not_supported(db: Database) -> None:
    """An empty games response leaves plays/lineups/advanced unknown_until_audited
    -- never supported, never an auth failure -- and no game id is fabricated."""

    client = _bdl_client(_bdl_routing_handler(games_body={"data": []}))
    result = await audit_provider(
        database=db,
        provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client),
        declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "succeeded"  # not an auth failure
    assert result.authenticated is True
    with db.connection() as conn:
        for cap in ("plays", "lineups", "advanced_statistics"):
            row = conn.execute(
                "SELECT is_observed, state, observed_state, raw_response_id, detail "
                "FROM provider_capabilities WHERE provider='balldontlie' AND capability=?",
                (cap,),
            ).fetchone()
            assert row["is_observed"] == 0
            assert row["state"] == "unknown_until_audited"
            assert row["observed_state"] is None
            assert row["raw_response_id"] is None
            assert "no suitable" in (row["detail"] or "")
        # No plays request was issued, so there is no /v1/plays raw response.
        plays_raw = conn.execute(
            "SELECT COUNT(*) FROM raw_responses WHERE endpoint = '/v1/plays'"
        ).fetchone()[0]
        assert plays_raw == 0


async def test_audit_substitutions_observed_only_when_plays_contain_them(db: Database) -> None:
    """Substitutions are observed only if the plays payload actually has them."""

    # Case 1: plays WITH a substitution event -> substitutions observed (via plays).
    with_subs = _bdl_client(
        _bdl_routing_handler(plays_body={"data": [{"id": 9, "type": "substitution"}]})
    )
    await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(with_subs), declaration=_bdl_decl(),
    )
    await with_subs.aclose()
    with db.connection() as conn:
        sub = conn.execute(
            "SELECT is_observed, endpoint FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='substitutions' AND is_observed=1"
        ).fetchone()
        assert sub is not None and sub["endpoint"] == "/v1/plays"

    # Case 2: plays WITHOUT substitutions -> substitutions stays declared-only.
    db2 = Database(db.path.parent / "corpus2.db")
    initialize_database(db2.path)
    without = _bdl_client(
        _bdl_routing_handler(plays_body={"data": [{"id": 9, "type": "shot"}]})
    )
    await audit_provider(
        database=db2, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(without), declaration=_bdl_decl(),
    )
    await without.aclose()
    with db2.connection() as conn:
        observed = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='substitutions' AND is_observed=1"
        ).fetchone()[0]
        assert observed == 0


async def test_audit_lineup_access_does_not_confirm_pregame_starters(db: Database) -> None:
    """Lineup endpoint access must never mark confirmed_pregame_starters observed."""

    client = _bdl_client(_bdl_routing_handler())
    await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        lineups = conn.execute(
            "SELECT is_observed FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='lineups' AND is_observed=1"
        ).fetchone()
        assert lineups is not None  # lineup access WAS observed
        starters_observed = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='confirmed_pregame_starters' "
            "AND is_observed=1"
        ).fetchone()[0]
        assert starters_observed == 0  # but starters are NOT inferred from it


def _mlb_routing_client(*, teams_body: dict, roster_body: dict) -> MlbStatsApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict = {"venues": [], "dates": []}
        if path == "/api/v1/teams":
            body = teams_body
        elif path.endswith("/roster"):
            body = roster_body
        elif "/people/" in path:
            body = {"people": [{"id": 592450}]}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    return MlbStatsApiClient(client=http)


async def test_audit_mlb_players_verified_via_roster_not_teams(db: Database) -> None:
    """MLB players is observed via the roster (and person) endpoints, not teams."""

    client = _mlb_routing_client(
        teams_body={"teams": [{"id": 133, "name": "Tigers"}]},
        roster_body={"roster": [{"person": {"id": 592450, "fullName": "X"}}]},
    )
    await audit_provider(
        database=db, provider=PROVIDER_MLB_STATSAPI,
        probes=build_mlb_statsapi_probes(client), declaration=_mlb_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        endpoints = {
            r[0]
            for r in conn.execute(
                "SELECT endpoint FROM provider_capabilities "
                "WHERE provider='mlb_statsapi' AND capability='players' AND is_observed=1"
            )
        }
        assert "/teams/{id}/roster" in endpoints
        assert "/teams" not in endpoints  # teams success alone never verifies players


async def test_audit_mlb_players_unknown_when_no_team_id(db: Database) -> None:
    """No extractable team id -> players recorded unverified, never supported."""

    client = _mlb_routing_client(teams_body={"teams": []}, roster_body={"roster": []})
    await audit_provider(
        database=db, provider=PROVIDER_MLB_STATSAPI,
        probes=build_mlb_statsapi_probes(client), declaration=_mlb_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        observed = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='mlb_statsapi' AND capability='players' AND is_observed=1"
        ).fetchone()[0]
        assert observed == 0
        unknown = conn.execute(
            "SELECT state FROM provider_capabilities "
            "WHERE provider='mlb_statsapi' AND capability='players' AND probe_name='roster'"
        ).fetchone()
        assert unknown is not None and unknown["state"] == "unknown_until_audited"


async def test_audit_oversized_response_never_stored(db: Database) -> None:
    """An oversized provider response is refused and never lands in raw_responses."""

    big = b'{"data":[' + b'{"id":1},' * 5000 + b'{"id":2}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "application/json"})

    client = _bdl_client(handler, max_body_bytes=256)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    # The run completed; every probe failed as UNEXPECTED (oversized), nothing
    # supported, and NO oversized body is anywhere in raw_responses.
    with db.connection() as conn:
        supported = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND observed_state='supported'"
        ).fetchone()[0]
        assert supported == 0
        for (body,) in conn.execute("SELECT body FROM raw_responses"):
            assert len(body) <= 256
    assert result.observed_count == 0


# --------------------------------------------------------------------------- #
# Box-score date dependency
# --------------------------------------------------------------------------- #
async def test_audit_box_scores_uses_extracted_game_date(db: Database) -> None:
    """The box-score probe sends the date extracted from the games fixture."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v1/games":
            body = {"data": [{"id": 55, "date": "2024-04-09T00:00:00.000Z"}]}
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    box = [r for r in captured if r.url.path == "/v1/box_scores"]
    assert box, "box-score probe never issued a request"
    # The date is the normalized calendar date extracted from the game row.
    assert box[0].url.params.get("date") == "2024-04-09"
    with db.connection() as conn:
        row = conn.execute(
            "SELECT is_observed, endpoint FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='team_statistics' AND is_observed=1"
        ).fetchone()
        assert row is not None and row["endpoint"] == "/v1/box_scores"


async def test_audit_box_scores_skipped_when_no_game_date(db: Database) -> None:
    """A games row without a usable date -> box scores skipped (unknown), no call."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v1/games":
            body = {"data": [{"id": 55}]}  # no date field
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    assert not [r for r in captured if r.url.path == "/v1/box_scores"]
    with db.connection() as conn:
        row = conn.execute(
            "SELECT is_observed, state, detail FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='team_statistics'"
        ).fetchone()
        assert row["is_observed"] == 0
        assert row["state"] == "unknown_until_audited"
        assert "game date" in (row["detail"] or "")


async def test_audit_box_scores_skipped_when_game_date_malformed(db: Database) -> None:
    """A malformed provider date is not used; box scores skip rather than send it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/games":
            body = {"data": [{"id": 55, "date": "not-a-date"}]}
        else:
            body = {"data": []}
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        row = conn.execute(
            "SELECT is_observed, state FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='team_statistics'"
        ).fetchone()
        assert row["is_observed"] == 0 and row["state"] == "unknown_until_audited"


# --------------------------------------------------------------------------- #
# Overall audit status semantics + authentication evidence
# --------------------------------------------------------------------------- #
def _bdl_status_client(handler, **kwargs) -> BalldontlieClient:
    return _bdl_client(handler, max_retries=0, **kwargs)


async def test_audit_network_failure_is_overall_failure(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _bdl_status_client(handler)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "failed"
    assert result.has_active_failure is True
    assert result.probes_succeeded == 0
    # No auth evidence either way -> unknown, never falsely True.
    assert result.authenticated is None


async def test_audit_5xx_after_retries_is_overall_failure(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"}, headers={"content-type": "application/json"})

    client = _bdl_status_client(handler)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "failed"
    assert result.active_failures > 0
    assert result.error_type == "server"


async def test_audit_exhausted_rate_limit_is_active_failure(db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"},
                              headers={"content-type": "application/json"})

    client = _bdl_status_client(handler)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.has_active_failure is True
    assert result.status == "failed"  # nothing verified + active failures


async def test_audit_mixed_success_and_active_failure_is_partial(db: Database) -> None:
    """teams succeeds but games hits a 5xx after retries -> partially_failed, not
    a fully-successful audit."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/games":
            return httpx.Response(500, json={"error": "boom"},
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, json={"data": [{"id": 1}]},
                              headers={"content-type": "application/json"})

    client = _bdl_status_client(handler)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "partially_failed"
    assert result.probes_succeeded > 0
    assert result.has_active_failure is True
    # teams (a 2xx) proves the key was accepted.
    assert result.authenticated is True


async def test_audit_authenticated_none_without_evidence(db: Database) -> None:
    """Only network/5xx/forbidden occurred -> authentication unknown, not True."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"},
                              headers={"content-type": "application/json"})

    client = _bdl_status_client(handler)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    # A generic forbidden is not authentication evidence in either direction.
    assert result.authenticated is None


async def test_audit_tier_restriction_counts_as_auth_evidence(db: Database) -> None:
    """A plan-worded 403 proves the key was recognized -> authenticated True."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/teams":
            return httpx.Response(
                403, json={"error": "this endpoint requires a higher plan tier"},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json={"data": []}, headers={"content-type": "application/json"})

    client = _bdl_status_client(handler)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.authenticated is True
    assert result.tier_restricted is True


async def test_audit_keyless_provider_auth_is_not_applicable(db: Database) -> None:
    client = _mlb_client(VENUES_BODY)
    result = await audit_provider(
        database=db, provider=PROVIDER_MLB_STATSAPI,
        probes=build_mlb_statsapi_probes(client), declaration=_mlb_decl(),
    )
    await client.aclose()
    assert result.auth_applicable is False
    assert result.authenticated is None


# --------------------------------------------------------------------------- #
# Capability-outcome uniqueness (no duplicate rows)
# --------------------------------------------------------------------------- #
async def test_audit_401_produces_one_outcome_per_capability(db: Database) -> None:
    """A 401 on the first probe: exactly one outcome per capability, no duplicate,
    no falsely-supported rows, honest unprobed rows for the rest."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"},
                              headers={"content-type": "application/json"})

    client = _bdl_status_client(handler)
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    assert result.status == "failed" and result.authenticated is False
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT capability, COUNT(*) c FROM provider_capabilities "
            "WHERE provider='balldontlie' GROUP BY capability HAVING c > 1"
        ).fetchall()
        assert rows == [], f"duplicate capability outcomes: {[dict(r) for r in rows]}"
        # teams (the attempted capability) has exactly one row and it is the
        # authentication outcome, not a supported fallback.
        teams = conn.execute(
            "SELECT COUNT(*) c, MAX(error_kind) k, MAX(is_observed) obs FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='teams'"
        ).fetchone()
        assert teams["c"] == 1 and teams["k"] == "authentication" and teams["obs"] == 0
        supported = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND observed_state='supported'"
        ).fetchone()[0]
        assert supported == 0


async def test_audit_every_capability_has_one_outcome(db: Database) -> None:
    """A normal successful audit yields exactly one outcome per declared capability."""

    client = _bdl_client(_bdl_routing_handler())
    result = await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    seen: dict[str, int] = {}
    for obs in result.observations:
        seen[obs.capability] = seen.get(obs.capability, 0) + 1
    dupes = {c: n for c, n in seen.items() if n > 1}
    assert dupes == {}, f"duplicate outcomes within one audit run: {dupes}"
    # Every declared capability is represented exactly once.
    declared = {c.value for c in _bdl_decl().states}
    assert set(seen) == declared


# --------------------------------------------------------------------------- #
# Substitution evidence vs endpoint access
# --------------------------------------------------------------------------- #
async def test_audit_plays_access_alone_does_not_prove_substitutions(db: Database) -> None:
    """A 200 from /v1/plays with no substitution events keeps substitutions
    declared-only, even though plays endpoint access is observed."""

    client = _bdl_client(
        _bdl_routing_handler(plays_body={"data": [{"id": 1, "type": "shot", "text": "made 3"}]})
    )
    await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        plays = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='plays' AND is_observed=1"
        ).fetchone()[0]
        subs = conn.execute(
            "SELECT COUNT(*) FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='substitutions' AND is_observed=1"
        ).fetchone()[0]
        assert plays == 1  # plays endpoint access observed
        assert subs == 0   # but substitutions NOT inferred from it


async def test_audit_substitution_recognized_via_documented_text_field(db: Database) -> None:
    """Substitution evidence is read from the documented ``text`` play field."""

    client = _bdl_client(
        _bdl_routing_handler(
            plays_body={"data": [{"id": 1, "type": "sub", "text": "Player A substitution"}]}
        )
    )
    await audit_provider(
        database=db, provider=PROVIDER_BALLDONTLIE,
        probes=build_balldontlie_probes(client), declaration=_bdl_decl(),
    )
    await client.aclose()
    with db.connection() as conn:
        subs = conn.execute(
            "SELECT is_observed, endpoint FROM provider_capabilities "
            "WHERE provider='balldontlie' AND capability='substitutions' AND is_observed=1"
        ).fetchone()
        assert subs is not None and subs["endpoint"] == "/v1/plays"


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
