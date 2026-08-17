"""Historical-market target anchoring: grid, Repair-4 resolution, budget, entitlement.

Every test here is offline. The resolver takes its evidence through an injected
snapshot source and the client tests run on ``httpx.MockTransport``, so the whole
suite exercises the real code paths without a socket or a credit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pytest

from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.providers.odds_api import (
    DEFAULT_BASE_URL,
    CreditHeaders,
    HistoricalAccessError,
    OddsApiClient,
    OddsApiHTTPError,
    _parse_historical_snapshot,
)
from sports_quant.providers.raw_exchange import RawExchange
from sports_quant.retrospective.market_anchor import (
    ANCHOR_POLICY_VERSION,
    ARCHIVE_START,
    CUTOFF_LEAD,
    GRID_CHANGE,
    LEGACY_GRID_SECONDS,
    MAX_ITERATIONS,
    MODERN_GRID_SECONDS,
    AnchorOutcome,
    BudgetExceeded,
    IdentityUnresolved,
    RefuseNameMatching,
    RequestBudget,
    SnapshotEvent,
    SnapshotView,
    floor_to_snapshot_grid,
    plan_snapshot_requests,
    resolve_target_anchor,
    snapshot_grid_seconds,
)

API_KEY = "test-key-do-not-log"
SPORT = "basketball_nba"
GAME = "gam_nba_test"
EVENT = "odds_event_abc"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class ExactIdentity:
    """An exact id link, which is what Lane-R requires and does not yet have."""

    def __init__(self, event_id: str = EVENT) -> None:
        self._event_id = event_id

    def provider_event_id(self, *, canonical_game_id: str, sport_key: str) -> str:
        return self._event_id


class ScriptedSource:
    """Serves snapshots from a dict and records every instant requested."""

    def __init__(self, snapshots: dict[datetime, SnapshotView]) -> None:
        self._snapshots = snapshots
        self.requested: list[datetime] = []

    def fetch(self, *, sport_key: str, at: datetime) -> Optional[SnapshotView]:
        self.requested.append(at)
        return self._snapshots.get(at)


def snapshot(
    at: str, commence: Optional[str], *, timestamp: Optional[str] = None,
    event_id: str = EVENT, home: str = "Boston Celtics", away: str = "Miami Heat",
) -> SnapshotView:
    return SnapshotView(
        timestamp=utc(timestamp or at),
        requested_at=utc(at),
        events=(
            SnapshotEvent(
                event_id=event_id,
                sport_key=SPORT,
                commence_time=utc(commence) if commence else None,
                home_team=home,
                away_team=away,
            ),
        ),
    )


def resolve(source: ScriptedSource, hint: str, **kwargs: object):
    return resolve_target_anchor(
        canonical_game_id=GAME,
        sport_key=SPORT,
        search_hint=utc(hint),
        source=source,
        identity=kwargs.pop("identity", ExactIdentity()),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 1-10. The snapshot grid
# --------------------------------------------------------------------------- #
def test_instant_already_on_the_grid_is_unchanged() -> None:
    at = utc("2026-03-05T23:10:00Z")
    assert floor_to_snapshot_grid(at) == at


def test_one_second_past_a_boundary_floors_down() -> None:
    assert floor_to_snapshot_grid(utc("2026-03-05T23:10:01Z")) == utc(
        "2026-03-05T23:10:00Z")


def test_one_second_before_a_boundary_floors_to_the_previous_bucket() -> None:
    assert floor_to_snapshot_grid(utc("2026-03-05T23:14:59Z")) == utc(
        "2026-03-05T23:10:00Z")


def test_microseconds_are_discarded_downward() -> None:
    at = utc("2026-03-05T23:10:00Z").replace(microsecond=999_999)
    assert floor_to_snapshot_grid(at) == utc("2026-03-05T23:10:00Z")


def test_legacy_archive_uses_the_ten_minute_grid() -> None:
    at = utc("2021-05-05T23:19:00Z")
    assert snapshot_grid_seconds(at) == LEGACY_GRID_SECONDS
    assert floor_to_snapshot_grid(at) == utc("2021-05-05T23:10:00Z")


def test_grid_change_instant_itself_is_already_the_five_minute_grid() -> None:
    assert snapshot_grid_seconds(GRID_CHANGE) == MODERN_GRID_SECONDS


def test_one_second_before_the_grid_change_is_still_the_legacy_grid() -> None:
    just_before = GRID_CHANGE - timedelta(seconds=1)
    assert snapshot_grid_seconds(just_before) == LEGACY_GRID_SECONDS
    assert floor_to_snapshot_grid(just_before) == GRID_CHANGE - timedelta(minutes=10)


def test_naive_datetimes_are_refused_rather_than_assumed_utc() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        floor_to_snapshot_grid(datetime(2026, 3, 5, 23, 10))


def test_non_utc_offsets_are_converted_not_stripped() -> None:
    eastern = timezone(timedelta(hours=-5))
    at = datetime(2026, 3, 5, 18, 12, tzinfo=eastern)  # 23:12Z
    assert floor_to_snapshot_grid(at) == utc("2026-03-05T23:10:00Z")


def test_every_floored_instant_lands_on_a_real_grid_multiple() -> None:
    base = utc("2026-03-05T23:00:00Z")
    for offset in range(0, 900, 7):
        floored = floor_to_snapshot_grid(base + timedelta(seconds=offset))
        elapsed = int((floored - ARCHIVE_START).total_seconds())
        assert elapsed % MODERN_GRID_SECONDS == 0
        assert floored <= base + timedelta(seconds=offset)


# --------------------------------------------------------------------------- #
# 11-17. Snapshot timestamp semantics: requested date is not the answer
# --------------------------------------------------------------------------- #
def test_requested_instant_and_snapshot_instant_are_kept_apart() -> None:
    view = snapshot("2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z",
                    timestamp="2026-03-05T23:05:00Z")
    assert view.requested_at != view.timestamp


def test_parser_uses_the_provider_timestamp_not_the_requested_date() -> None:
    parsed = _parse_historical_snapshot(
        {"timestamp": "2026-03-05T23:05:00Z", "data": []},
        requested_date="2026-03-05T23:10:00Z", credits=CreditHeaders(), exchange=None,
    )
    assert parsed.timestamp == "2026-03-05T23:05:00Z"
    assert parsed.requested_date == "2026-03-05T23:10:00Z"


def test_snapshot_without_a_timestamp_is_refused_not_defaulted() -> None:
    with pytest.raises(ValueError, match="no snapshot timestamp"):
        _parse_historical_snapshot(
            {"data": []}, requested_date="2026-03-05T23:10:00Z",
            credits=CreditHeaders(), exchange=None,
        )


def test_null_data_parses_as_no_events() -> None:
    parsed = _parse_historical_snapshot(
        {"timestamp": "2026-03-05T23:05:00Z", "data": None},
        requested_date="d", credits=CreditHeaders(), exchange=None,
    )
    assert parsed.events == []


def test_a_bare_list_response_is_refused() -> None:
    with pytest.raises(ValueError, match="expected the"):
        _parse_historical_snapshot(
            [], requested_date="d", credits=CreditHeaders(), exchange=None,
        )


def test_non_list_data_is_refused() -> None:
    with pytest.raises(ValueError, match="expected a list"):
        _parse_historical_snapshot(
            {"timestamp": "t", "data": {"id": "x"}},
            requested_date="d", credits=CreditHeaders(), exchange=None,
        )


def test_a_non_object_event_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="not an object"):
        _parse_historical_snapshot(
            {"timestamp": "t", "data": ["abc"]},
            requested_date="d", credits=CreditHeaders(), exchange=None,
        )


# --------------------------------------------------------------------------- #
# 18-29. Repair-4 resolution
# --------------------------------------------------------------------------- #
def test_stable_commence_time_resolves_on_the_first_iteration() -> None:
    source = ScriptedSource({
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z"),
    })
    result = resolve(source, "2026-03-06T00:10:00Z")

    assert result.outcome is AnchorOutcome.RESOLVED
    assert result.cutoff == utc("2026-03-05T23:10:00Z")
    assert result.commence_time_snapshot == utc("2026-03-06T00:10:00Z")
    assert result.iterations == 1
    assert result.policy_version == ANCHOR_POLICY_VERSION


def test_a_moved_start_is_resolved_by_a_second_iteration() -> None:
    # Retrospectively the game started at 00:40; contemporaneously it was 00:10.
    source = ScriptedSource({
        utc("2026-03-05T23:40:00Z"): snapshot(
            "2026-03-05T23:40:00Z", "2026-03-06T00:10:00Z"),
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z"),
    })
    result = resolve(source, "2026-03-06T00:40:00Z")

    assert result.outcome is AnchorOutcome.RESOLVED
    assert result.cutoff == utc("2026-03-05T23:10:00Z")
    assert result.iterations == 2
    assert source.requested == [
        utc("2026-03-05T23:40:00Z"), utc("2026-03-05T23:10:00Z")]


def test_a_cutoff_that_never_settles_is_rejected_not_retried_forever() -> None:
    # Each snapshot reports a start an hour earlier, so the cutoff keeps moving.
    source = ScriptedSource({
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-05T23:10:00Z"),
    })

    class Sliding:
        def __init__(self) -> None:
            self.requested: list[datetime] = []

        def fetch(self, *, sport_key: str, at: datetime) -> Optional[SnapshotView]:
            self.requested.append(at)
            return SnapshotView(
                timestamp=at, requested_at=at,
                events=(SnapshotEvent(
                    event_id=EVENT, sport_key=SPORT,
                    commence_time=at + timedelta(minutes=30)),),
            )

    sliding = Sliding()
    result = resolve_target_anchor(
        canonical_game_id=GAME, sport_key=SPORT,
        search_hint=utc("2026-03-06T00:10:00Z"),
        source=sliding, identity=ExactIdentity(),
    )
    assert result.outcome is AnchorOutcome.NO_CONVERGENCE
    assert result.iterations == MAX_ITERATIONS
    assert len(sliding.requested) == MAX_ITERATIONS
    assert result.cutoff is None
    assert source.requested == []


def test_a_missing_snapshot_is_no_market_at_the_cutoff() -> None:
    source = ScriptedSource({})
    result = resolve(source, "2026-03-06T00:10:00Z")
    assert result.outcome is AnchorOutcome.NO_MARKET_AT_CUTOFF
    assert result.cutoff is None


def test_an_absent_event_is_rejected() -> None:
    source = ScriptedSource({
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z", event_id="someone_else"),
    })
    result = resolve(source, "2026-03-06T00:10:00Z")
    assert result.outcome is AnchorOutcome.EVENT_ABSENT


def test_a_snapshot_without_a_commence_time_carries_no_anchor_evidence() -> None:
    source = ScriptedSource({
        utc("2026-03-05T23:10:00Z"): snapshot("2026-03-05T23:10:00Z", None),
    })
    result = resolve(source, "2026-03-06T00:10:00Z")
    assert result.outcome is AnchorOutcome.MISSING_COMMENCE_TIME


def test_an_event_that_had_already_started_is_rejected() -> None:
    source = ScriptedSource({
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-05T23:00:00Z"),
    })
    result = resolve(source, "2026-03-06T00:10:00Z")
    assert result.outcome is AnchorOutcome.ALREADY_COMMENCED


def test_commencement_is_judged_against_the_snapshot_clock_not_the_request() -> None:
    # ATTACK: requested 23:10, but the provider answered with a 00:20 snapshot in
    # which the game (00:10) had already tipped. Comparing against the REQUESTED
    # instant would wrongly accept this.
    source = ScriptedSource({
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z",
            timestamp="2026-03-06T00:20:00Z"),
    })
    result = resolve(source, "2026-03-06T00:10:00Z")
    assert result.outcome is AnchorOutcome.ALREADY_COMMENCED


def test_a_hint_before_the_archive_exists_is_rejected_without_requesting() -> None:
    source = ScriptedSource({})
    result = resolve(source, "2019-01-05T00:10:00Z")
    assert result.outcome is AnchorOutcome.BEFORE_ARCHIVE_START
    assert source.requested == []


def test_the_default_identity_refuses_and_no_snapshot_is_requested() -> None:
    source = ScriptedSource({})
    result = resolve_target_anchor(
        canonical_game_id=GAME, sport_key=SPORT,
        search_hint=utc("2026-03-06T00:10:00Z"), source=source,
    )
    assert result.outcome is AnchorOutcome.IDENTITY_UNRESOLVED
    assert source.requested == []
    assert "name matching" in result.detail

    with pytest.raises(IdentityUnresolved):
        RefuseNameMatching().provider_event_id(
            canonical_game_id=GAME, sport_key=SPORT)


def test_matching_team_names_do_not_substitute_for_a_matching_id() -> None:
    # ATTACK: the snapshot holds exactly the right teams under a different id.
    # Any name-based fallback would resolve here; there must not be one.
    source = ScriptedSource({
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z",
            event_id="a_different_provider_id",
            home="Boston Celtics", away="Miami Heat"),
    })
    result = resolve(source, "2026-03-06T00:10:00Z")
    assert result.outcome is AnchorOutcome.EVENT_ABSENT
    assert result.cutoff is None


def test_the_retrospective_hint_never_becomes_the_anchor() -> None:
    # ATTACK: the hint is a half-hour EARLIER than the contemporaneous start.
    # The circular rule (hint - 60 min) would anchor at 22:40; the correct
    # anchor follows the snapshot's own commence_time to 23:10.
    hint = "2026-03-05T23:40:00Z"
    source = ScriptedSource({
        utc("2026-03-05T22:40:00Z"): snapshot(
            "2026-03-05T22:40:00Z", "2026-03-06T00:10:00Z"),
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z"),
    })
    result = resolve(source, hint)
    assert result.outcome is AnchorOutcome.RESOLVED
    assert result.cutoff == utc("2026-03-05T23:10:00Z")
    assert result.cutoff != utc(hint) - CUTOFF_LEAD


def test_resolution_is_deterministic() -> None:
    def build() -> ScriptedSource:
        return ScriptedSource({
            utc("2026-03-05T23:10:00Z"): snapshot(
                "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z"),
        })

    first = resolve(build(), "2026-03-06T00:10:00Z")
    second = resolve(build(), "2026-03-06T00:10:00Z")
    assert first == second


# --------------------------------------------------------------------------- #
# 30-34. Budget
# --------------------------------------------------------------------------- #
def test_the_request_cap_stops_the_run() -> None:
    budget = RequestBudget(max_requests=2, max_credits=100)
    budget.charge(requests=1, credits=1)
    budget.charge(requests=1, credits=1)
    with pytest.raises(BudgetExceeded, match="request cap"):
        budget.charge(requests=1, credits=1)


def test_the_credit_cap_binds_independently_of_the_request_cap() -> None:
    budget = RequestBudget(max_requests=10, max_credits=5)
    with pytest.raises(BudgetExceeded, match="credit cap"):
        budget.charge(requests=1, credits=10)
    assert budget.requests_used == 0
    assert budget.credits_used == 0


def test_a_refused_charge_costs_nothing() -> None:
    budget = RequestBudget(max_requests=1, max_credits=1)
    budget.charge(requests=1, credits=1)
    with pytest.raises(BudgetExceeded):
        budget.charge(requests=1, credits=1)
    assert (budget.requests_used, budget.credits_used) == (1, 1)
    assert budget.requests_remaining == 0


def test_negative_caps_are_refused() -> None:
    with pytest.raises(ValueError):
        RequestBudget(max_requests=-1)
    with pytest.raises(ValueError):
        RequestBudget().charge(requests=-1)


def test_the_resolver_charges_the_budget_before_each_snapshot() -> None:
    source = ScriptedSource({
        utc("2026-03-05T23:40:00Z"): snapshot(
            "2026-03-05T23:40:00Z", "2026-03-06T00:10:00Z"),
        utc("2026-03-05T23:10:00Z"): snapshot(
            "2026-03-05T23:10:00Z", "2026-03-06T00:10:00Z"),
    })
    budget = RequestBudget(max_requests=1, max_credits=100)
    with pytest.raises(BudgetExceeded):
        resolve(source, "2026-03-06T00:40:00Z", budget=budget)
    assert budget.requests_used == 1
    assert len(source.requested) == 1


# --------------------------------------------------------------------------- #
# 35-37. Dry-run planning
# --------------------------------------------------------------------------- #
def test_games_sharing_a_bucket_cost_one_request() -> None:
    plan = plan_snapshot_requests(sport_key=SPORT, hints={
        "g1": utc("2026-03-06T00:10:00Z"),
        "g2": utc("2026-03-06T00:12:00Z"),
        "g3": utc("2026-03-06T00:40:00Z"),
    })
    assert plan.request_count == 2
    assert plan.credit_cost == 2
    assert plan.instants == (
        utc("2026-03-05T23:10:00Z"), utc("2026-03-05T23:40:00Z"))
    assert plan.games_by_instant[utc("2026-03-05T23:10:00Z")] == ("g1", "g2")


def test_a_plan_reports_whether_it_fits_the_budget() -> None:
    plan = plan_snapshot_requests(sport_key=SPORT, hints={
        f"g{i}": utc("2026-03-06T00:10:00Z") + timedelta(minutes=5 * i)
        for i in range(6)
    })
    assert plan.request_count == 6
    assert not plan.within(RequestBudget(max_requests=5, max_credits=100))
    assert plan.within(RequestBudget(max_requests=10, max_credits=100))


def test_a_priced_endpoint_plan_costs_far_more_per_request() -> None:
    hints = {"g1": utc("2026-03-06T00:10:00Z")}
    assert plan_snapshot_requests(sport_key=SPORT, hints=hints).credit_cost == 1
    priced = plan_snapshot_requests(
        sport_key=SPORT, hints=hints, credits_per_request=30)
    assert priced.credit_cost == 30


# --------------------------------------------------------------------------- #
# 38-42. Client: entitlement, secrets, endpoint
# --------------------------------------------------------------------------- #
def _client(handler) -> OddsApiClient:
    http = build_readonly_client(
        base_url=DEFAULT_BASE_URL,
        policy=ReadOnlyHTTPPolicy.for_odds_api(),
        inner_transport=httpx.MockTransport(handler),
    )
    return OddsApiClient(API_KEY, client=http)


async def test_historical_events_hits_the_events_endpoint_and_parses() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "timestamp": "2026-03-05T23:05:00Z",
            "previous_timestamp": "2026-03-05T23:00:00Z",
            "next_timestamp": "2026-03-05T23:10:00Z",
            "data": [{
                "id": EVENT, "sport_key": SPORT, "sport_title": "NBA",
                "commence_time": "2026-03-06T00:10:00Z",
                "home_team": "Boston Celtics", "away_team": "Miami Heat",
            }],
        }, headers={"x-requests-remaining": "499", "x-requests-used": "1",
                    "x-requests-last": "1"})

    result = await _client(handler).get_historical_events(
        sport_key=SPORT, date="2026-03-05T23:10:00Z")

    assert seen[0].url.path == f"/v4/historical/sports/{SPORT}/events"
    assert result.timestamp == "2026-03-05T23:05:00Z"
    assert result.requested_date == "2026-03-05T23:10:00Z"
    assert result.credits.requests_remaining == "499"
    (event,) = result.events
    assert event.id == EVENT
    assert event.commence_time == "2026-03-06T00:10:00Z"


async def test_the_free_plan_refusal_becomes_a_terminal_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={
            "message": "Historical data is not available on the free usage plan",
            "error_code": "HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN"})

    with pytest.raises(HistoricalAccessError, match="subscription question"):
        await _client(handler).get_historical_events(
            sport_key=SPORT, date="2026-03-05T23:10:00Z")
    assert calls["n"] == 1  # never retried


async def test_an_unrelated_failure_is_not_disguised_as_an_entitlement_problem() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Invalid date format"})

    with pytest.raises(OddsApiHTTPError):
        await _client(handler).get_historical_events(
            sport_key=SPORT, date="not-a-date")


async def test_the_api_key_never_appears_in_the_captured_exchange() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "timestamp": "2026-03-05T23:05:00Z", "data": []})

    result = await _client(handler).get_historical_events(
        sport_key=SPORT, date="2026-03-05T23:10:00Z")

    assert result.exchange is not None
    blob = result.exchange.model_dump_json()
    assert API_KEY not in blob
    # The parameter NAME survives, which is what makes the request auditable;
    # only the secret itself is replaced.
    assert (result.exchange.request_params or {})["apiKey"] == "***REDACTED***"


async def test_an_empty_sport_or_date_is_refused_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made")

    client = _client(handler)
    with pytest.raises(ValueError, match="sport_key"):
        await client.get_historical_events(sport_key="  ", date="2026-03-05T23:10:00Z")
    with pytest.raises(ValueError, match="explicit date"):
        await client.get_historical_events(sport_key=SPORT, date="")


async def test_the_captured_exchange_fits_the_existing_raw_response_store() -> None:
    # Readiness, not materialization: the historical path must reuse the one
    # provenance store, so its exchange has to be the same RawExchange the
    # ingestors already persist -- not a parallel record type.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "timestamp": "2026-03-05T23:05:00Z", "data": []})

    result = await _client(handler).get_historical_events(
        sport_key=SPORT, date="2026-03-05T23:10:00Z")

    assert isinstance(result.exchange, RawExchange)
    assert result.exchange.endpoint == f"/v4/historical/sports/{SPORT}/events"
    assert result.exchange.http_status == 200
    assert result.exchange.received_at is not None
    assert result.exchange.elapsed_ns >= 0


async def test_absent_quota_headers_do_not_break_the_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "timestamp": "2026-03-05T23:05:00Z", "data": []})

    result = await _client(handler).get_historical_events(
        sport_key=SPORT, date="2026-03-05T23:10:00Z")
    assert result.credits.requests_remaining is None


async def test_a_list_shaped_historical_response_is_refused() -> None:
    # The current-odds endpoints return a bare list; the historical ones return
    # a wrapper. Accepting the wrong shape here would silently lose the
    # snapshot timestamp, which is the whole point of the endpoint.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": EVENT}])

    with pytest.raises(ValueError, match="expected the"):
        await _client(handler).get_historical_events(
            sport_key=SPORT, date="2026-03-05T23:10:00Z")


async def test_the_entitlement_refusal_preserves_its_exchange() -> None:
    """A refusal is evidence; the one authorized probe must not lose it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "message": "Historical data is not available on the free usage plan",
            "error_code": "HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN"})

    with pytest.raises(HistoricalAccessError) as caught:
        await _client(handler).get_historical_events(
            sport_key=SPORT, date="2026-03-05T23:10:00Z")

    assert caught.value.status_code == 401
    assert caught.value.exchange is not None
    assert caught.value.exchange.http_status == 401
    # Still redacted: a preserved refusal must not leak the key either.
    assert API_KEY not in caught.value.exchange.model_dump_json()
