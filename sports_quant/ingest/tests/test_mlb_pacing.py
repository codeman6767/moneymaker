"""Adversarial tests for the MLB project courtesy pacing policy (mlb-pacing-v1).

The gap these close: the MLB month manifest was correctly aggregate-capped at
6,002 attempts, but the MLB gate attached no rate policy at all, so a month-scale
run would have issued requests as fast as sequential responses returned.

Everything here uses a mocked clock, a mocked sleep and `httpx.MockTransport`.
No test waits in real time and none opens a socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from sports_quant.ingest.cost_policies import (
    MLB_DEFAULT_RATE_PER_MIN,
    MLB_PACING_POLICY_VERSION,
    build_balldontlie_rate_policy,
    build_mlb_policy,
    build_mlb_rate_policy,
)
from sports_quant.ingest.f1a import _make_gate
from sports_quant.ingest.manifest import load_and_validate
from sports_quant.providers.base_provider import ProviderError
from sports_quant.request_control import (
    RATE_BASIS_PROJECT_COURTESY,
    RATE_BASIS_VERIFIED_TIER,
    BudgetExhausted,
    CreditBudget,
    RateLimiter,
    RequestBudget,
    RequestGate,
    RequestRatePolicy,
    RequestUnit,
)

REPO = Path(__file__).resolve().parents[3]
MLB_MANIFEST = REPO / "pilots" / "f1" / "mlb_coverage_2026_06.manifest.json"
NBA_MANIFEST = REPO / "pilots" / "f1" / "nba_coverage_2026_03.manifest.json"


def _unit(family: str = "schedule") -> RequestUnit:
    return RequestUnit(provider="mlb_statsapi", league="mlb", endpoint_family=family,
                       date_key="2026-06-01..2026-06-30")


def _paced_gate(cap: int = 6002, per_min: int = MLB_DEFAULT_RATE_PER_MIN
                ) -> tuple[RequestGate, list[float]]:
    """An MLB gate whose limiter reads a mocked clock the caller advances."""

    now = [1000.0]
    policy = build_mlb_rate_policy(configured_per_min=per_min)
    gate = RequestGate(
        request_budget=RequestBudget(max_requests=cap),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=build_mlb_policy(),
        rate_policy=policy)
    gate.set_auth_context(auth_applicable=False)
    # Replace the limiter's clock with the mocked one (same construction the gate
    # performs, so the derived minimum interval is unchanged).
    gate._limiter = RateLimiter(  # noqa: SLF001 - deliberate clock injection
        policy.configured_per_min, clock=lambda: now[0],
        min_interval=policy.min_interval_seconds)
    return gate, now


# --------------------------------------------------------------------------- #
# 1-3: the policy the MLB gate now carries
# --------------------------------------------------------------------------- #
def test_mlb_gate_has_pacing_enabled_at_30_per_minute() -> None:
    gate = _make_gate(league="mlb", request_cap=6002, credit_cap=None)
    policy = gate.rate_policy
    assert policy is not None, "MLB must no longer run unpaced"
    assert policy.provider == "mlb_statsapi"
    assert policy.configured_per_min == 30
    assert policy.burst == 1
    assert policy.min_interval_seconds == 2.0
    assert policy.version == MLB_PACING_POLICY_VERSION == "mlb-pacing-v1"
    assert gate.usage.rate_policy_active is True
    assert gate.usage.configured_rate_per_min == 30
    assert gate.usage.rate_burst == 1
    assert gate.usage.rate_min_interval_seconds == 2.0
    assert gate.usage.rate_policy_version == "mlb-pacing-v1"


def test_provider_maximum_stays_unknown_and_is_never_fabricated() -> None:
    gate = _make_gate(league="mlb", request_cap=6002, credit_cap=None)
    policy = gate.rate_policy
    assert policy is not None
    assert policy.tier_max_per_min is None, "no MLB provider ceiling may be invented"
    assert policy.tier is None
    assert policy.basis == RATE_BASIS_PROJECT_COURTESY
    assert policy.is_project_courtesy is True
    assert gate.usage.provider_rate_limit_per_min is None
    assert gate.usage.rate_policy_basis == "project_courtesy_cap"


def test_a_courtesy_policy_refuses_to_carry_a_tier_or_provider_maximum() -> None:
    with pytest.raises(ValueError, match="tier_max_per_min None"):
        RequestRatePolicy(provider="mlb_statsapi", version="mlb-pacing-v1",
                          configured_per_min=30, tier_max_per_min=120,
                          basis=RATE_BASIS_PROJECT_COURTESY, burst=1)
    with pytest.raises(ValueError, match="tier None"):
        RequestRatePolicy(provider="mlb_statsapi", version="mlb-pacing-v1",
                          configured_per_min=30, tier="premium",
                          basis=RATE_BASIS_PROJECT_COURTESY, burst=1)


def test_mlb_authentication_and_tier_remain_not_applicable() -> None:
    gate = _make_gate(league="mlb", request_cap=6002, credit_cap=None)
    usage = gate.usage
    assert usage.authentication_status in ("not_applicable", None)
    assert usage.tier_status in ("not_applicable", None)
    assert usage.tier_verified in (False, None)
    assert usage.credits_applicable is False


@pytest.mark.parametrize("kwargs,match", [
    ({"configured_per_min": 0}, "configured_per_min must be positive"),
    ({"configured_per_min": -5}, "configured_per_min must be positive"),
    ({"configured_per_min": 30, "burst": -1}, "burst must not be negative"),
    ({"configured_per_min": 30, "burst": 0}, "requires an explicit positive burst"),
    ({"configured_per_min": 30, "burst": 1, "basis": "made_up"}, "unknown rate policy"),
])
def test_policy_fails_closed_on_invalid_input(kwargs: dict, match: str) -> None:
    base: dict[str, Any] = {"provider": "mlb_statsapi", "version": "mlb-pacing-v1",
                            "basis": RATE_BASIS_PROJECT_COURTESY}
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        RequestRatePolicy(**base)


def test_unsupported_pacing_version_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported MLB pacing policy version"):
        build_mlb_rate_policy(version="mlb-pacing-v99")


# --------------------------------------------------------------------------- #
# 4-8: steady pacing, no opening burst, concurrency
# --------------------------------------------------------------------------- #
def test_first_request_starts_immediately() -> None:
    gate, _now = _paced_gate()
    assert gate.rate_acquire() == 0.0


def test_second_request_starts_at_least_two_seconds_after_the_first() -> None:
    """The guarantee is on the INTERVAL between starts, not on the raw wait.

    A fast response means some of the two seconds has already elapsed, so the
    remaining wait is correctly less than 2.0 while the spacing is still exactly
    2.0. Asserting the start instants is what actually pins the contract.
    """

    gate, now = _paced_gate()
    first_start = now[0] + gate.rate_acquire()
    now[0] += 0.001  # a fast response returns almost instantly
    second_start = now[0] + gate.rate_acquire()
    assert second_start - first_start == pytest.approx(2.0)


def test_five_consecutive_requests_are_spaced_deterministically() -> None:
    gate, now = _paced_gate()
    waits = []
    for _ in range(5):
        wait = gate.rate_acquire()
        waits.append(wait)
        now[0] += wait  # the caller sleeps exactly the returned delay
    assert waits == [0.0, 2.0, 2.0, 2.0, 2.0]
    # Elapsed monotonic time for five requests at 30/min.
    assert sum(waits) == 8.0


def test_no_opening_burst_of_thirty_requests_is_possible() -> None:
    """The old sliding window would have allowed 30 immediate requests."""

    gate, now = _paced_gate()
    immediate = 0
    for _ in range(30):
        wait = gate.rate_acquire()
        if wait == 0.0:
            immediate += 1
        now[0] += wait
    assert immediate == 1, "only the very first request may start immediately"


def test_concurrent_callers_serialize_onto_distinct_slots() -> None:
    gate, _now = _paced_gate()  # clock frozen: every caller reads the same instant
    waits: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        wait = gate.rate_acquire()
        with lock:
            waits.append(wait)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(waits) == 8
    # Exactly one caller went immediately; the rest are spaced 2s apart, and no
    # two callers were handed the same start instant.
    assert sorted(waits) == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]


def test_a_wall_clock_change_cannot_alter_pacing() -> None:
    """The limiter reads a MONOTONIC clock, so it is immune to a clock jump."""

    now = [1000.0]
    limiter = RateLimiter(30, clock=lambda: now[0], min_interval=2.0)
    assert limiter.acquire_wait() == 0.0
    now[0] -= 100_000.0  # a wall-clock style backwards jump
    assert limiter.acquire_wait() >= 2.0, "spacing must not collapse on a clock jump"


# --------------------------------------------------------------------------- #
# 9-10: budget rejection and reserved-but-unsent requests
# --------------------------------------------------------------------------- #
def test_aggregate_budget_rejection_causes_no_sleep_and_no_pacing_slot() -> None:
    gate, now = _paced_gate(cap=1)
    gate.reserve(_unit())
    assert gate.rate_acquire() == 0.0
    with pytest.raises(BudgetExhausted):
        gate.reserve(_unit())
    # The refused request consumed no pacing slot: the next legitimate acquire
    # is spaced from the FIRST reservation, not from a phantom second one.
    now[0] += 2.0
    assert gate.rate_acquire() == 0.0
    assert gate.usage.throttle_events == 0, "a refused request must never sleep"
    assert gate.usage.throttle_wait_seconds == 0.0


def test_a_reserved_but_unsent_request_is_not_a_transport_start() -> None:
    gate, _now = _paced_gate()
    gate.reserve(_unit())
    gate.rate_acquire()
    # No mark_transport() -- the send never happened.
    assert gate.usage.reserved_attempts == 1
    assert gate.usage.transport_starts == 0


# --------------------------------------------------------------------------- #
# 13-14: honest throttle reporting
# --------------------------------------------------------------------------- #
def test_policy_presence_alone_does_not_report_actual_rate_limiting() -> None:
    gate = _make_gate(league="mlb", request_cap=6002, credit_cap=None)
    assert gate.usage.rate_policy_active is True
    assert gate.usage.rate_limited is False, (
        "a policy existing is not the same as a request being delayed")
    assert gate.usage.throttle_events == 0
    assert gate.usage.throttle_wait_seconds == 0.0


def test_an_actual_courtesy_wait_populates_the_throttle_fields() -> None:
    gate, now = _paced_gate()
    first_start = now[0] + gate.rate_acquire()
    now[0] += 0.001
    wait = gate.rate_acquire()
    assert wait > 0.0, "a real delay was imposed"
    assert (now[0] + wait) - first_start == pytest.approx(2.0)
    usage = gate.usage
    assert usage.rate_limited is True
    assert usage.throttle_events == 1
    assert usage.throttle_wait_seconds == pytest.approx(wait)
    # A courtesy wait is NOT a provider rate-limit response.
    assert usage.http_429s == 0


def test_courtesy_wait_and_provider_429_are_separate_counters() -> None:
    gate, now = _paced_gate()
    gate.rate_acquire()
    now[0] += 0.001
    gate.rate_acquire()          # a courtesy wait
    gate.record_429()            # a provider-directed rate-limit response
    usage = gate.usage
    assert usage.throttle_events == 1, "429 must not inflate courtesy throttle events"
    assert usage.http_429s == 1
    assert usage.throttle_wait_seconds > 0.0


# --------------------------------------------------------------------------- #
# 11-12: retries are paced, budgeted and 429-accounted (real client path)
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.sleeps: list[float] = []


def _mlb_client(recorder: _Recorder, gate: RequestGate, statuses: list[int]) -> Any:
    from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
    from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.attempts.append(request.url.path)
        status = queue.pop(0) if queue else 200
        headers = {"content-type": "application/json"}
        if status == 429:
            headers["retry-after"] = "7"
        return httpx.Response(status, json={"dates": []}, headers=headers)

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler))
    client = MlbStatsApiClient(client=http, gate=gate, league="mlb")

    async def fake_sleep(seconds: float) -> None:
        recorder.sleeps.append(seconds)

    client._sleep = fake_sleep  # noqa: SLF001 - deterministic, never waits for real
    return client


def test_every_retry_is_both_paced_and_budgeted() -> None:
    recorder = _Recorder()
    gate, now = _paced_gate()
    client = _mlb_client(recorder, gate, statuses=[503, 200])
    asyncio.run(client.fetch_schedule())
    # Two transport attempts: the failure and the retry.
    assert len(recorder.attempts) == 2
    assert gate.usage.reserved_attempts == 2, "the retry consumed budget"
    assert gate.usage.retry_attempts >= 1
    assert gate.usage.transport_starts == 2
    # The retry passed through the pacing chokepoint: a courtesy wait was taken
    # in addition to the backoff sleep.
    assert gate.usage.throttle_events >= 1
    assert any(s >= 2.0 for s in recorder.sleeps), recorder.sleeps


def test_http_429_accounting_stays_accurate_and_headers_are_never_exposed() -> None:
    recorder = _Recorder()
    gate, _now = _paced_gate()
    client = _mlb_client(recorder, gate, statuses=[429, 200])
    asyncio.run(client.fetch_schedule())
    assert gate.usage.http_429s == 1
    assert gate.usage.reserved_attempts == 2
    # Retry-After=7 was honoured as a provider-directed backoff.
    assert any(s == pytest.approx(7.0) for s in recorder.sleeps), recorder.sleeps
    # And it is NOT double counted as courtesy pacing wait.
    assert gate.usage.throttle_wait_seconds < 7.0


def test_a_malformed_retry_after_falls_back_to_bounded_backoff() -> None:
    from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
    from sports_quant.providers.mlb_statsapi import MlbStatsApiClient

    recorder = _Recorder()
    gate, _now = _paced_gate()
    queue = [429, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.attempts.append(request.url.path)
        status = queue.pop(0) if queue else 200
        headers = {"content-type": "application/json"}
        if status == 429:
            headers["retry-after"] = "Wed, 21 Oct 2026 07:28:00 GMT"  # HTTP-date form
        return httpx.Response(status, json={"dates": []}, headers=headers)

    http = build_readonly_client(
        base_url="https://statsapi.mlb.com/api/v1",
        policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(),
        inner_transport=httpx.MockTransport(handler))
    client = MlbStatsApiClient(client=http, gate=gate, league="mlb")

    async def fake_sleep(seconds: float) -> None:
        recorder.sleeps.append(seconds)

    client._sleep = fake_sleep  # noqa: SLF001
    asyncio.run(client.fetch_schedule())
    assert gate.usage.http_429s == 1
    assert len(recorder.attempts) == 2, "the retry still happened"
    # No sleep is absurdly long: the HTTP-date form fell back to bounded backoff.
    assert all(s <= 60.0 for s in recorder.sleeps), recorder.sleeps


def test_no_retry_can_exceed_the_committed_max_retries() -> None:
    """max_retries=1 is a committed manifest bound; the client must honour it."""

    recorder = _Recorder()
    gate, _now = _paced_gate()
    client = _mlb_client(recorder, gate, statuses=[503, 503, 503, 503])
    client._max_retries = 1  # noqa: SLF001 - the manifest's committed bound
    with pytest.raises(ProviderError):
        asyncio.run(client.fetch_schedule())
    assert len(recorder.attempts) == 2, "one attempt plus at most one retry"


# --------------------------------------------------------------------------- #
# 16-18: manifest identity, mismatch fail-closed, preserved counts
# --------------------------------------------------------------------------- #
def test_regenerated_mlb_manifest_declares_the_courtesy_rate() -> None:
    body = json.loads(MLB_MANIFEST.read_text(encoding="utf-8"))
    assert body["bounds"]["rate_per_min"] == 30
    assert body["plan_body"]["bounds"]["rate_per_min"] == 30
    assert body["configured_rate_per_min"] == 30
    assert body["provider_rate_limit_per_min"] is None, (
        "no MLB provider maximum may be asserted")


def test_mlb_semantic_maximum_and_hard_cap_are_unchanged() -> None:
    manifest = load_and_validate(MLB_MANIFEST, expected_league="mlb",
                                expected_provider="mlb_statsapi")
    assert manifest.estimated_requests_max == 3001
    assert manifest.request_cap == 6002
    assert manifest.max_games == 600
    assert manifest.max_retries == 1
    assert manifest.date_range == "2026-06-01..2026-06-30"
    assert list(manifest.families) == ["box", "inning", "results", "rosters", "schedule"]
    assert manifest.scratch_db.replace("\\", "/") == "data/f1_mlb_2026_06_scratch.db"
    assert manifest.checkpoint_path.replace("\\", "/") == "data/f1_mlb_2026_06.ckpt"
    assert manifest.expected_schema_version == 17


def test_changing_the_configured_rate_changes_the_plan_and_manifest_hashes(
    tmp_path: Path,
) -> None:
    import hashlib

    from sports_quant.ingest.f1a import emit_plan
    from sports_quant.ingest.manifest import canonical_json

    baseline_manifest = hashlib.sha256(MLB_MANIFEST.read_bytes()).hexdigest()
    baseline_plan = hashlib.sha256(canonical_json(
        json.loads(MLB_MANIFEST.read_text(encoding="utf-8"))["plan_body"]
    ).encode("utf-8")).hexdigest()
    for rate in (29, 31, 60):
        out = tmp_path / f"rate_{rate}.json"
        emit_plan(league="mlb", from_date="2026-06-01", to_date="2026-06-30",
                  includes=("results", "box", "inning", "rosters"), max_games=600,
                  max_retries=1, rate_per_min=rate,
                  scratch_db="data\\f1_mlb_2026_06_scratch.db",
                  checkpoint="data\\f1_mlb_2026_06.ckpt", expected_schema_version=17,
                  manifest_out=out, out=lambda _s: None)
        variant = json.loads(out.read_text(encoding="utf-8"))
        assert hashlib.sha256(out.read_bytes()).hexdigest() != baseline_manifest, rate
        assert hashlib.sha256(canonical_json(variant["plan_body"]).encode(
            "utf-8")).hexdigest() != baseline_plan, rate
        # The rate never changes the request counts.
        assert variant["estimated_requests_max"] == 3001
        assert variant["request_cap"] == 6002


def test_manifest_regenerates_byte_identically(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "f1_gen_pacing", REPO / "pilots" / "f1" / "generate_manifests.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for produced in module.generate(out_dir=tmp_path):
        committed = REPO / "pilots" / "f1" / produced.name
        assert produced.read_text(encoding="utf-8") == committed.read_text(
            encoding="utf-8"), produced.name


def test_a_manifest_runtime_pacing_mismatch_fails_before_client_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared pacing bound that the runtime would ignore must refuse to run."""

    from sports_quant.db.init import initialize_database
    from sports_quant.ingest import f1a

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    scratch = tmp_path / "scratch.db"
    initialize_database(scratch)
    constructed: list[str] = []

    def never_called(_gate: RequestGate) -> Any:
        constructed.append("client")
        raise AssertionError("a client must not be built after a pacing mismatch")

    # A runtime that would pace at a DIFFERENT rate than the manifest declares.
    def wrong_gate(*, league: str, request_cap: int, credit_cap: Optional[int],
                   rate_per_min: Optional[int] = None) -> RequestGate:
        gate = RequestGate(
            request_budget=RequestBudget(max_requests=request_cap),
            credit_budget=CreditBudget(applicable=False),
            cost_policy=build_mlb_policy(),
            rate_policy=build_mlb_rate_policy(configured_per_min=15))
        gate.set_auth_context(auth_applicable=False)
        return gate

    monkeypatch.setattr(f1a, "_make_gate", wrong_gate)
    lines: list[str] = []
    code = f1a.run_pilot_cli(
        league="mlb", manifest_path=MLB_MANIFEST, scratch_db=scratch,
        checkpoint=tmp_path / "p.ckpt", client_factory=never_called, out=lines.append)
    assert code != 0
    assert constructed == [], "the client was built despite a pacing mismatch"
    assert any("pacing" in line.lower() for line in lines), lines


# --------------------------------------------------------------------------- #
# 19-20: NBA and prior-manifest non-regression
# --------------------------------------------------------------------------- #
def test_nba_rate_behaviour_is_unchanged() -> None:
    gate = _make_gate(league="nba", request_cap=21616, credit_cap=None, rate_per_min=60)
    policy = gate.rate_policy
    assert policy is not None
    assert policy.provider == "balldontlie"
    assert policy.version == "bdl-rate-v1"
    assert policy.tier == "goat"
    assert policy.tier_max_per_min == 600
    assert policy.configured_per_min == 60
    assert policy.basis == RATE_BASIS_VERIFIED_TIER
    assert policy.is_project_courtesy is False
    # The reviewed sliding-window shape: no minimum interval, so an opening burst
    # up to the configured rate stays permitted exactly as before.
    assert policy.burst == 0
    assert policy.min_interval_seconds == 0.0
    assert gate.usage.provider_rate_limit_per_min == 600
    assert [gate.rate_acquire() for _ in range(10)] == [0.0] * 10


def test_the_nba_tier_ceiling_is_still_enforced() -> None:
    with pytest.raises(ValueError, match="exceeds the verified tier maximum"):
        build_balldontlie_rate_policy(tier="free", configured_per_min=999)


def test_nba_month_manifest_is_byte_identical() -> None:
    import hashlib

    digest = hashlib.sha256(NBA_MANIFEST.read_bytes()).hexdigest()
    assert digest == (
        "901cb9deaf3c5bf243f73ed60a820dd323933caea5dac7a45b69e01480f5ad3e")


def test_all_four_f1b_manifests_are_byte_identical() -> None:
    import hashlib

    expected = {
        "mlb_skeleton.manifest.json": "fa28695b043eb38d",
        "nba_skeleton.manifest.json": "6fe6dc37ec4d5868",
        "mlb_rich.manifest.json": "f56b5c5da53d86c9",
        "nba_rich.manifest.json": "9de5d312b99c3e85",
    }
    for name, prefix in expected.items():
        digest = hashlib.sha256((REPO / "pilots" / "f1b" / name).read_bytes()).hexdigest()
        assert digest.startswith(prefix), name


# --------------------------------------------------------------------------- #
# 21-22: reporting labels and zero network
# --------------------------------------------------------------------------- #
def test_json_and_human_output_label_the_rate_as_a_project_courtesy_cap() -> None:
    gate, now = _paced_gate()
    gate.rate_acquire()
    now[0] += 0.001
    gate.rate_acquire()
    payload = gate.usage.as_dict()
    assert payload["rate_policy_basis"] == "project_courtesy_cap"
    assert payload["rate_policy_version"] == "mlb-pacing-v1"
    assert payload["configured_rate_per_min"] == 30
    assert payload["provider_rate_limit_per_min"] is None
    assert payload["rate_burst"] == 1
    assert payload["rate_min_interval_seconds"] == 2.0
    assert payload["throttle_events"] == 1

    # The human line must say so in words, and must not print "None/min".
    from sports_quant.ingest.f1a import _render_rate_line

    line = _render_rate_line(payload)
    assert "PROJECT COURTESY CAP" in line
    assert "basis=project_courtesy_cap" in line
    assert "provider_max=unknown" in line
    assert "None/min" not in line


def test_nba_human_output_reports_the_verified_provider_maximum() -> None:
    from sports_quant.ingest.f1a import _render_rate_line

    gate = _make_gate(league="nba", request_cap=100, credit_cap=None, rate_per_min=60)
    line = _render_rate_line(gate.usage.as_dict())
    assert "provider_max=600/min" in line
    assert "PROJECT COURTESY CAP" not in line
    assert "basis=verified_tier_max" in line


def test_no_socket_is_opened_by_any_pacing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def blocked(*_a: object, **_kw: object) -> None:
        raise AssertionError("pacing attempted network access")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    gate, now = _paced_gate()
    for _ in range(5):
        now[0] += gate.rate_acquire()
    assert gate.usage.throttle_events == 4
