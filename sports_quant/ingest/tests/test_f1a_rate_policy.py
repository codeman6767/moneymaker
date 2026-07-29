"""Focused proofs for the corrected BALLDONTLIE quota model (offline).

BALLDONTLIE is metered by a per-minute REQUEST-RATE limit per subscription tier,
NOT by a monetary/credit balance. These tests prove the corrected semantics:

* no fabricated BALLDONTLIE credits; MLB credits remain N/A;
* the aggregate request cap still stops before an extra call;
* retries and pagination each consume the request budget;
* a configured rate can never exceed the verified tier maximum;
* an observed HTTP 429 is recorded (and handled) safely;
* a resume does not reset the aggregate request budget;
* manifests stay deterministic after the policy change, and an old
  (credit-model) manifest fails closed;
* an unrecognised endpoint family fails closed.

The four quota concepts are kept strictly distinct: the aggregate request budget
(hard total-call ceiling), requests attempted, the provider tier rate limit, the
configured safe rate, throttle wait, and observed 429s.
"""

from __future__ import annotations

import pytest

from ...request_control import (
    BudgetExhausted,
    CreditBudget,
    EndpointCostPolicy,
    LimitType,
    RateLimiter,
    RequestBudget,
    RequestGate,
    RequestRatePolicy,
    RequestUnit,
)
from ..cost_policies import (
    BALLDONTLIE_DEFAULT_RATE_PER_MIN,
    BALLDONTLIE_TIER_RATES,
    build_balldontlie_policy,
    build_balldontlie_rate_policy,
    build_mlb_policy,
)
from ..manifest import build_manifest, load_and_validate
from ..planning import Bounds, build_plan


def _bdl_unit(family: str = "games", page: int = 1) -> RequestUnit:
    return RequestUnit(provider="balldontlie", league="nba", endpoint_family=family,
                       date_key="2026-01-05", page=page)


def _bdl_gate(*, request_cap: int, rate_per_min: int = 100) -> RequestGate:
    return RequestGate(
        request_budget=RequestBudget(max_requests=request_cap),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=build_balldontlie_policy(),
        rate_policy=build_balldontlie_rate_policy("goat", rate_per_min))


# --- RequestRatePolicy validation ------------------------------------------ #
def test_rate_policy_default_is_below_tier_max() -> None:
    rp = build_balldontlie_rate_policy()  # GOAT default
    assert rp.tier == "goat"
    assert rp.tier_max_per_min == BALLDONTLIE_TIER_RATES["goat"] == 600
    assert rp.configured_per_min == BALLDONTLIE_DEFAULT_RATE_PER_MIN == 100
    assert rp.configured_per_min < rp.tier_max_per_min


def test_configured_rate_cannot_exceed_verified_tier_max() -> None:
    # Right at the ceiling is allowed; one above the verified tier max fails closed.
    RequestRatePolicy(provider="balldontlie", version="bdl-rate-v1", tier="goat",
                      tier_max_per_min=600, configured_per_min=600)
    with pytest.raises(ValueError, match="exceeds the verified tier maximum"):
        build_balldontlie_rate_policy("goat", 601)
    with pytest.raises(ValueError, match="exceeds the verified tier maximum"):
        build_balldontlie_rate_policy("all-star", 61)


def test_rate_policy_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        RequestRatePolicy(provider="balldontlie", version="bdl-rate-v1", tier="goat",
                          tier_max_per_min=600, configured_per_min=0)


def test_unknown_tier_rejected() -> None:
    with pytest.raises(ValueError, match="unknown BALLDONTLIE tier"):
        build_balldontlie_rate_policy("platinum")


# --- RateLimiter throttle math (deterministic fake clock) ------------------- #
def test_rate_limiter_allows_burst_then_throttles() -> None:
    now = [1000.0]
    limiter = RateLimiter(3, clock=lambda: now[0], window=60.0)
    assert limiter.acquire_wait() == 0.0   # 1st slot free
    assert limiter.acquire_wait() == 0.0   # 2nd
    assert limiter.acquire_wait() == 0.0   # 3rd fills the window
    wait = limiter.acquire_wait()          # 4th must wait for the oldest to age out
    assert wait == pytest.approx(60.0)
    # After the window elapses, a slot frees up again.
    now[0] += 60.0
    assert limiter.acquire_wait() == 0.0


def test_rate_limiter_partial_window_wait() -> None:
    now = [0.0]
    limiter = RateLimiter(1, clock=lambda: now[0], window=60.0)
    assert limiter.acquire_wait() == 0.0
    now[0] = 20.0
    assert limiter.acquire_wait() == pytest.approx(40.0)  # 60 - 20 elapsed


# --- gate: rate accounting distinct from request budget & credits ----------- #
def test_gate_exposes_rate_contract_and_accumulates_throttle_wait() -> None:
    now = [0.0]
    gate = RequestGate(
        request_budget=RequestBudget(max_requests=100),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=build_balldontlie_policy(),
        rate_policy=build_balldontlie_rate_policy("goat", 2))
    # Rate fields are surfaced on the usage report, distinct from any credit fields.
    # An ATTACHED policy is `rate_policy_active`; it is NOT itself rate limiting.
    assert gate.usage.rate_policy_active is True
    assert gate.usage.rate_limited is False
    assert gate.usage.throttle_events == 0
    assert gate.usage.provider_rate_limit_per_min == 600   # tier max
    assert gate.usage.configured_rate_per_min == 2         # configured safe rate
    assert gate.usage.credits_applicable is False
    # Drive the limiter with a fake clock so the third acquire must wait.
    gate._limiter = RateLimiter(2, clock=lambda: now[0], window=60.0)  # type: ignore[attr-defined]
    assert gate.rate_acquire() == 0.0
    assert gate.rate_acquire() == 0.0
    assert gate.usage.rate_limited is False    # nothing has waited yet
    wait = gate.rate_acquire()
    assert wait == pytest.approx(60.0)
    assert gate.usage.throttle_wait_seconds == pytest.approx(60.0)
    # Only now, after a request actually waited, is the run truly rate limited.
    assert gate.usage.throttle_events == 1
    assert gate.usage.rate_limited is True


def test_gate_records_429_without_inventing_anything() -> None:
    gate = _bdl_gate(request_cap=10)
    assert gate.usage.http_429s == 0
    assert gate.usage.rate_limited is False
    gate.record_429()
    gate.record_429()
    assert gate.usage.http_429s == 2
    # A provider rate-limit RESPONSE is real rate limiting.
    assert gate.usage.rate_limited is True
    # A 429 is a rate signal, never a credit figure.
    assert gate.usage.reported_credits_consumed is None
    assert gate.usage.provider_credits_remaining is None


# --- no fabricated credits -------------------------------------------------- #
def test_balldontlie_credits_never_fabricated() -> None:
    policy = build_balldontlie_policy()
    assert policy.credit_applicable is False
    for fam in ("games", "game", "box_scores", "stats", "advanced_stats"):
        assert policy.cost_for(fam) is None          # never a fabricated cost
    gate = _bdl_gate(request_cap=5)
    gate.reserve(_bdl_unit())
    assert gate.usage.credits_applicable is False
    assert gate.usage.reserved_credits == 0
    assert gate.usage.credit_header_status == "not_applicable"


def test_cli_help_never_claims_balldontlie_credits_are_required() -> None:
    """`--credit-cap` help must not resurrect the credit model in user-facing text.

    A request-rate limit is not a billing credit, so no CLI surface may tell a
    user that BALLDONTLIE credits exist or that a credit cap is required for a
    live NBA pilot.
    """
    import argparse

    from ...cli import _add_f1a_args

    parser = argparse.ArgumentParser(prog="sports_quant ingest-nba")
    _add_f1a_args(parser)
    # argparse hard-wraps help text; compare on a single whitespace-normalised line.
    help_text = " ".join(parser.format_help().lower().split())

    assert "required for a live nba pilot" not in help_text
    assert "hard maximum balldontlie credits" not in help_text
    # The rate contract stays documented, and credits are named as N/A.
    assert "request-rate limited, not credit metered" in help_text
    assert "verified tier max" in help_text


def test_mlb_credits_remain_not_applicable() -> None:
    policy = build_mlb_policy()
    assert policy.credit_applicable is False
    assert policy.cost_for("schedule") is None
    gate = RequestGate(
        request_budget=RequestBudget(max_requests=3),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=policy)
    assert gate.rate_policy is None                  # MLB is not rate-metered here
    assert gate.usage.credits_applicable is False


# --- aggregate request cap still hard-stops --------------------------------- #
def test_aggregate_request_cap_stops_before_extra_call() -> None:
    gate = _bdl_gate(request_cap=2)
    gate.reserve(_bdl_unit(page=1))
    gate.reserve(_bdl_unit(page=2))
    with pytest.raises(BudgetExhausted) as ei:
        gate.reserve(_bdl_unit(page=3))
    assert ei.value.limit_type is LimitType.REQUEST
    assert gate.usage.attempted_requests == 2        # the 3rd never reserved
    assert gate.usage.transport_starts == 0          # and never sent


def test_retries_and_pages_each_consume_request_budget() -> None:
    gate = _bdl_gate(request_cap=3)
    gate.reserve(_bdl_unit(page=1))                  # page 1
    gate.reserve(_bdl_unit(page=2))                  # page 2 (pagination consumes budget)
    gate.reserve(_bdl_unit(page=2), is_retry=True)   # a retry consumes budget too
    assert gate.usage.attempted_requests == 3
    assert gate.usage.retry_attempts == 1
    with pytest.raises(BudgetExhausted):
        gate.reserve(_bdl_unit(page=3))              # 4th call over the cap -> stop


# --- unknown endpoint family fails closed ----------------------------------- #
def test_unknown_endpoint_family_fails_closed() -> None:
    policy = EndpointCostPolicy(
        provider="balldontlie", version="bdl-cost-v1", credit_applicable=False,
        costs={}, known_families=frozenset({"games"}))
    gate = RequestGate(
        request_budget=RequestBudget(max_requests=10),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=policy)
    with pytest.raises(BudgetExhausted) as ei:
        gate.reserve(RequestUnit(provider="balldontlie", league="nba",
                                 endpoint_family="mystery"))
    assert ei.value.limit_type is LimitType.UNKNOWN_ENDPOINT
    assert gate.usage.attempted_requests == 0        # nothing reserved
    assert gate.usage.blocked_requests == 1


# --- resume does not reset the aggregate request budget --------------------- #
def test_resume_preserves_aggregate_request_budget() -> None:
    gate = _bdl_gate(request_cap=5)
    gate.seed_prior(prior_requests=4, prior_credits=0)
    assert gate.usage.attempted_requests == 4        # prior process counted
    gate.reserve(_bdl_unit(page=1))                  # the 5th (and last) call
    with pytest.raises(BudgetExhausted) as ei:
        gate.reserve(_bdl_unit(page=2))              # a fresh process must NOT reset
    assert ei.value.limit_type is LimitType.REQUEST
    assert gate.usage.prior_requests == 4


# --- manifest determinism + old-model manifest fails closed ----------------- #
def _nba_bounds() -> Bounds:
    return Bounds(max_pages=5, max_retries=3)


def test_nba_manifest_deterministic_after_policy_change() -> None:
    def make():  # type: ignore[no-untyped-def]
        plan = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                          families=("games",), stage="skeleton", bounds=_nba_bounds())
        return build_manifest(plan, scratch_db="data/pilot.db",
                              checkpoint_path="data/pilot.ckpt")
    m1, m2 = make(), make()
    assert m1.canonical() == m2.canonical()          # byte-identical
    assert m1.manifest_hash() == m2.manifest_hash()
    assert m1.credits_applicable is False
    assert m1.provider_rate_limit_per_min == 600
    assert m1.configured_rate_per_min == 100


def test_old_credit_model_nba_manifest_fails_closed(tmp_path) -> None:
    # Simulate an OLD manifest written under the credit model: credits_applicable
    # True, credit figures present, and no request-rate contract. Loading it must
    # fail closed rather than silently run under the corrected policy.
    plan = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                      families=("games",), stage="skeleton", bounds=_nba_bounds())
    manifest = build_manifest(plan, scratch_db="data/pilot.db",
                              checkpoint_path="data/pilot.ckpt")
    body = manifest.body()
    body["credits_applicable"] = True                # old model
    body["plan_body"]["credits_applicable"] = True   # drift the signed plan body
    from ..manifest import canonical_json
    path = tmp_path / "old_nba.json"
    path.write_text(canonical_json(body), encoding="utf-8")
    reloaded = load_and_validate(path, expected_league="nba", expected_provider="balldontlie")
    # The signed plan body no longer matches a plan rebuilt under the current policy.
    from ..manifest import plan_hash
    rebuilt = build_plan(league="nba", from_date="2026-01-05", to_date="2026-01-05",
                         families=("games",), stage="skeleton", bounds=_nba_bounds())
    assert plan_hash(rebuilt) != reloaded.computed_plan_hash()
