"""Phase D1 provider infrastructure: capabilities, policies, clients, safety.

Every network interaction uses a mocked transport wrapped in the real read-only
policy, so GET-only + allow-list enforcement is exercised exactly as it would be
live. No live call is made; importing a provider module performs no network I/O.
"""

from __future__ import annotations

import importlib

import httpx
import pytest

from sports_quant.http_policy import (
    ReadOnlyHTTPPolicy,
    ReadOnlyPolicyError,
    build_readonly_client,
)
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.providers.base_provider import ProviderError
from sports_quant.providers.capabilities import (
    BALLDONTLIE_FREE_DECLARATION,
    BALLDONTLIE_GOAT_DECLARATION,
    PROVIDER_BALLDONTLIE,
    PROVIDER_MLB_STATSAPI,
    BalldontlieTier,
    CapabilityState,
    ProviderCapability,
    ProviderErrorKind,
    balldontlie_declaration,
    classify_http_status,
    is_tier_restriction,
)
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

SENTINEL_KEY = "sk-phase-d-sentinel-do-not-store"


# --------------------------------------------------------------------------- #
# Capability declarations
# --------------------------------------------------------------------------- #
def test_goat_is_explicitly_declared_not_inferred() -> None:
    decl = BALLDONTLIE_GOAT_DECLARATION
    assert decl.tier == BalldontlieTier.GOAT.value
    assert decl.state(ProviderCapability.PLAYER_STATISTICS) is CapabilityState.SUPPORTED
    assert decl.state(ProviderCapability.INJURIES) is CapabilityState.SUPPORTED
    assert decl.state(ProviderCapability.PLAYS) is CapabilityState.SUPPORTED
    # GOAT lineups are best-effort, not a confirmed-starter guarantee.
    assert decl.state(ProviderCapability.LINEUPS) is CapabilityState.BEST_EFFORT
    assert decl.state(ProviderCapability.CONFIRMED_PREGAME_STARTERS) is CapabilityState.UNAVAILABLE


def test_free_and_all_star_mark_goat_only_paid() -> None:
    free = BALLDONTLIE_FREE_DECLARATION
    assert free.state(ProviderCapability.PLAYER_STATISTICS) is CapabilityState.PAID_TIER_REQUIRED
    assert free.state(ProviderCapability.INJURIES) is CapabilityState.PAID_TIER_REQUIRED
    assert free.state(ProviderCapability.PLAYS) is CapabilityState.PAID_TIER_REQUIRED
    # Free still supplies teams/players/games.
    assert free.state(ProviderCapability.TEAMS) is CapabilityState.SUPPORTED
    assert free.state(ProviderCapability.GAMES) is CapabilityState.SUPPORTED

    all_star = balldontlie_declaration(BalldontlieTier.ALL_STAR)
    # ALL-STAR unlocks player stats + injuries...
    assert all_star.state(ProviderCapability.PLAYER_STATISTICS) is CapabilityState.SUPPORTED
    assert all_star.state(ProviderCapability.INJURIES) is CapabilityState.SUPPORTED
    # ...but not plays/lineups.
    assert all_star.state(ProviderCapability.PLAYS) is CapabilityState.PAID_TIER_REQUIRED
    assert all_star.state(ProviderCapability.LINEUPS) is CapabilityState.PAID_TIER_REQUIRED


def test_mlb_statsapi_declares_starters_unavailable() -> None:
    from sports_quant.providers.capabilities import MLB_STATSAPI_DECLARATION

    assert (
        MLB_STATSAPI_DECLARATION.state(ProviderCapability.CONFIRMED_PREGAME_STARTERS)
        is CapabilityState.UNAVAILABLE
    )
    assert MLB_STATSAPI_DECLARATION.state(ProviderCapability.VENUES) is CapabilityState.SUPPORTED


def test_undeclared_capability_is_unknown_until_audited() -> None:
    # A capability not in the map is unknown, never fabricated as supported.
    from sports_quant.providers.capabilities import NWS_DECLARATION

    assert (
        NWS_DECLARATION.state(ProviderCapability.PLAYER_STATISTICS)
        is CapabilityState.UNKNOWN_UNTIL_AUDITED
    )


# --------------------------------------------------------------------------- #
# Tier-error classification
# --------------------------------------------------------------------------- #
def test_balldontlie_403_is_tier_restriction_only_with_plan_evidence() -> None:
    # A bare 403 carries no plan/subscription evidence -> FORBIDDEN, never an
    # assumed tier restriction (which would fabricate a paid_tier_required belief).
    assert (
        classify_http_status(403, provider=PROVIDER_BALLDONTLIE)
        is ProviderErrorKind.FORBIDDEN
    )
    # With explicit plan wording it IS a tier restriction.
    kind = classify_http_status(
        403, provider=PROVIDER_BALLDONTLIE, body_snippet="upgrade to the GOAT plan"
    )
    assert kind is ProviderErrorKind.TIER_RESTRICTED
    assert is_tier_restriction(kind)


def test_401_is_authentication_separate_from_tier() -> None:
    assert classify_http_status(401, provider=PROVIDER_BALLDONTLIE) is ProviderErrorKind.AUTHENTICATION
    assert not is_tier_restriction(ProviderErrorKind.AUTHENTICATION)
    # A 401 whose body names a bad key is the INVALID_KEY subtype -- still never
    # a supported/tier observation.
    assert (
        classify_http_status(401, body_snippet="Invalid API key")
        is ProviderErrorKind.INVALID_KEY
    )


def test_generic_403_without_plan_wording_is_forbidden_not_tier() -> None:
    # No provider's bare 403 is a tier restriction; it is a generic FORBIDDEN.
    assert (
        classify_http_status(403, provider=PROVIDER_MLB_STATSAPI)
        is ProviderErrorKind.FORBIDDEN
    )
    assert (
        classify_http_status(403, provider=PROVIDER_MLB_STATSAPI, body_snippet="subscription plan")
        is ProviderErrorKind.TIER_RESTRICTED
    )


def test_429_and_5xx_classified() -> None:
    assert classify_http_status(429) is ProviderErrorKind.RATE_LIMITED
    assert classify_http_status(503) is ProviderErrorKind.SERVER
    assert classify_http_status(404) is ProviderErrorKind.NOT_FOUND


# --------------------------------------------------------------------------- #
# HTTP policies: approved host + unapproved path still blocked; write verbs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "policy_factory,base,allowed,unapproved",
    [
        (ReadOnlyHTTPPolicy.for_mlb_statsapi, "https://statsapi.mlb.com",
         "/api/v1/venues", "/api/v1/awards"),
        (ReadOnlyHTTPPolicy.for_balldontlie, "https://api.balldontlie.io",
         "/v1/teams", "/v1/account"),
        (ReadOnlyHTTPPolicy.for_nws, "https://api.weather.gov",
         "/points/40.7,-74.0", "/alerts"),
        (ReadOnlyHTTPPolicy.for_open_meteo, "https://api.open-meteo.com",
         "/v1/forecast", "/v1/subscription"),
    ],
)
def test_approved_host_unapproved_path_blocked(policy_factory, base, allowed, unapproved) -> None:
    policy = policy_factory()
    policy.enforce("GET", base + allowed)  # should not raise
    with pytest.raises(ReadOnlyPolicyError):
        policy.enforce("GET", base + unapproved)


@pytest.mark.parametrize(
    "policy_factory,base,path",
    [
        (ReadOnlyHTTPPolicy.for_mlb_statsapi, "https://statsapi.mlb.com", "/api/v1/venues"),
        (ReadOnlyHTTPPolicy.for_balldontlie, "https://api.balldontlie.io", "/v1/teams"),
        (ReadOnlyHTTPPolicy.for_nws, "https://api.weather.gov", "/points/40,-74"),
        (ReadOnlyHTTPPolicy.for_open_meteo, "https://api.open-meteo.com", "/v1/forecast"),
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_verbs_blocked(policy_factory, base, path, method) -> None:
    with pytest.raises(ReadOnlyPolicyError):
        policy_factory().enforce(method, base + path)


@pytest.mark.parametrize(
    "path",
    ["/v1/account", "/v1/subscription", "/v1/billing", "/v1/payment", "/v1/orders",
     "/v1/balance", "/v1/positions", "/v1/user", "/v1/login", "/v1/auth"],
)
def test_account_and_payment_paths_blocked(path: str) -> None:
    with pytest.raises(ReadOnlyPolicyError):
        ReadOnlyHTTPPolicy.for_balldontlie().enforce("GET", "https://api.balldontlie.io" + path)


def test_unapproved_host_blocked() -> None:
    with pytest.raises(ReadOnlyPolicyError):
        ReadOnlyHTTPPolicy.for_mlb_statsapi().enforce("GET", "https://evil.example.com/api/v1/venues")


# --------------------------------------------------------------------------- #
# Provider clients (mocked transports)
# --------------------------------------------------------------------------- #
def _mlb_client(handler) -> MlbStatsApiClient:
    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )
    return MlbStatsApiClient(client=http)


def _bdl_client(handler, key: str = SENTINEL_KEY) -> BalldontlieClient:
    http = build_readonly_client(
        base_url="https://api.balldontlie.io",
        policy=ReadOnlyHTTPPolicy.for_balldontlie(),
        inner_transport=httpx.MockTransport(handler),
    )
    return BalldontlieClient(key, client=http)


async def test_mlb_fetch_venues_get_only() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={"venues": [{"id": 1, "name": "Park"}]},
                              headers={"content-type": "application/json"})

    client = _mlb_client(handler)
    try:
        resp = await client.fetch_venues()
    finally:
        await client.aclose()
    assert seen == ["GET"]
    assert resp.data["venues"][0]["name"] == "Park"
    assert resp.exchange.endpoint == "/venues"
    assert resp.exchange.http_status == 200


async def test_balldontlie_403_raises_tier_restriction_without_leaking_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # The key rides the Authorization header (never stored); return a tier 403.
        return httpx.Response(403, json={"error": "this endpoint requires a higher plan tier"},
                              headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_teams()
    finally:
        await client.aclose()
    err = excinfo.value
    assert err.kind is ProviderErrorKind.TIER_RESTRICTED
    assert SENTINEL_KEY not in str(err)
    assert err.exchange is not None and SENTINEL_KEY not in err.exchange.body
    # The key never appears in stored request params/headers either.
    assert SENTINEL_KEY not in str(err.exchange.request_params)
    assert SENTINEL_KEY not in str(err.exchange.response_headers)


async def test_unexpected_content_type_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"})

    client = _mlb_client(handler)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_venues()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.UNEXPECTED


async def test_retry_after_is_honoured_then_succeeds() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "slow down"},
                                  headers={"content-type": "application/json", "retry-after": "2"})
        return httpx.Response(200, json={"venues": []}, headers={"content-type": "application/json"})

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler),
    )

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    client = MlbStatsApiClient(client=http, sleep=fake_sleep)
    try:
        resp = await client.fetch_venues()
    finally:
        await client.aclose()
    assert calls["n"] == 2
    assert slept == [2.0]  # honoured Retry-After
    assert resp.data == {"venues": []}


def test_clients_do_no_network_at_import() -> None:
    # Importing the provider modules must not open a socket or make a call.
    for mod in (
        "sports_quant.providers.mlb_statsapi",
        "sports_quant.providers.balldontlie",
        "sports_quant.providers.nws",
        "sports_quant.providers.open_meteo",
        "sports_quant.providers.capabilities",
        "sports_quant.providers.base_provider",
    ):
        importlib.import_module(mod)  # no exception, no network


# --------------------------------------------------------------------------- #
# BALLDONTLIE documented dependent endpoints: validation + GET-only
# --------------------------------------------------------------------------- #
def _ok_handler(seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": []}, headers={"content-type": "application/json"})

    return handler


async def test_balldontlie_plays_lineups_advanced_hit_documented_paths() -> None:
    seen: list[str] = []
    client = _bdl_client(_ok_handler(seen))
    try:
        await client.fetch_plays(game_id=18444208)
        await client.fetch_lineups(game_ids=[18444208, 7])
        await client.fetch_advanced_stats(game_id=18444208)
        await client.fetch_advanced_stats(season=2024)
    finally:
        await client.aclose()
    assert seen == ["/v1/plays", "/v1/lineups", "/nba/v1/stats/advanced", "/nba/v1/stats/advanced"]


@pytest.mark.parametrize("bad", [None, "", "  ", "abc", 0, -5, 1.5, True, "12x"])
async def test_balldontlie_plays_rejects_invalid_game_id_without_request(bad) -> None:  # noqa: ANN001
    seen: list[str] = []
    client = _bdl_client(_ok_handler(seen))
    try:
        with pytest.raises(ValueError):
            await client.fetch_plays(game_id=bad)
    finally:
        await client.aclose()
    assert seen == []  # never issued a request with a bad id


async def test_balldontlie_lineups_requires_at_least_one_id() -> None:
    seen: list[str] = []
    client = _bdl_client(_ok_handler(seen))
    try:
        with pytest.raises(ValueError):
            await client.fetch_lineups(game_ids=[])
    finally:
        await client.aclose()
    assert seen == []


async def test_balldontlie_advanced_stats_requires_a_bounding_filter() -> None:
    seen: list[str] = []
    client = _bdl_client(_ok_handler(seen))
    try:
        with pytest.raises(ValueError):
            await client.fetch_advanced_stats()  # no game_id and no season
    finally:
        await client.aclose()
    assert seen == []


async def test_balldontlie_per_page_is_bounded() -> None:
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return httpx.Response(200, json={"data": []}, headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    try:
        await client.fetch_stats(per_page=100000)  # absurd; must be clamped to 100
    finally:
        await client.aclose()
    assert captured and captured[0].params.get("per_page") == "100"


async def test_mlb_roster_and_person_reject_invalid_ids_without_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"roster": []}, headers={"content-type": "application/json"})

    client = _mlb_client(handler)
    try:
        for bad in (None, "", "abc", 0, -1):
            with pytest.raises(ValueError):
                await client.fetch_roster(bad)
            with pytest.raises(ValueError):
                await client.fetch_person(bad)
    finally:
        await client.aclose()
    assert seen == []


# --------------------------------------------------------------------------- #
# Error classification edge cases (sanitized bodies)
# --------------------------------------------------------------------------- #
async def test_empty_403_body_is_forbidden_not_tier() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={}, headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_teams()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.FORBIDDEN


async def test_malformed_error_body_still_classified_by_status() -> None:
    # A 403 whose body is not JSON is still a FORBIDDEN by status; a 401 an auth
    # failure. The unparseable body never crashes classification or leaks.
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html>nope</html>", headers={"content-type": "application/json"})

    client = _bdl_client(forbidden)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_teams()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.FORBIDDEN
    assert SENTINEL_KEY not in str(excinfo.value)


async def test_401_body_naming_bad_key_is_invalid_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid API key"},
                              headers={"content-type": "application/json"})

    client = _bdl_client(handler)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await client.fetch_teams()
    finally:
        await client.aclose()
    assert excinfo.value.kind is ProviderErrorKind.INVALID_KEY


def test_provider_error_kind_has_every_referenced_member() -> None:
    # Guards against a reference to a nonexistent enum member at runtime.
    for name in (
        "AUTHENTICATION", "INVALID_KEY", "TIER_RESTRICTED", "FORBIDDEN", "RATE_LIMITED",
        "NOT_FOUND", "NETWORK", "SERVER", "INVALID_PAYLOAD", "PARSER", "UNSUPPORTED",
        "UNEXPECTED",
    ):
        assert hasattr(ProviderErrorKind, name), f"ProviderErrorKind.{name} is missing"
