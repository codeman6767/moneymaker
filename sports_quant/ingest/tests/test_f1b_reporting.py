"""F1B skeleton-pilot REPORTING correctness (offline; mocked transports, no key).

The independent review of the completed MLB and NBA skeleton pilots found that the
runs were correct but their reports were not. These tests pin the repaired contract:

* A: an attached rate policy is ``rate_policy_active``, never ``rate_limited``
  -- for BALLDONTLIE's verified tier policy and for MLB's project courtesy
  policy alike.
  ``rate_limited`` is true only when a request actually waited or got a 429.
* B: ``max_games`` SELECTION accounting (received / selected / excluded /
  ``selection_truncated``) is reported separately from BUDGET truncation
  (``truncated`` / ``budget_exhausted`` / ``blocked_requests``).
* C: ``pages_fetched`` counts unique SUCCESSFUL listing pages -- page 0 included, a
  failed transport excluded, a retried page counted once.
* D: authentication and tier are reported honestly and secret-free; a 200 from an
  endpoint available below the subscribed tier proves auth but never the tier.
* E: a completed resume no longer erases the first run's transport provenance.

Both preserved artifact shapes (MLB 30 games -> 2, NBA 8 games -> 2) are
reconstructed from deterministic fixtures and drive the real orchestration.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.checkpoint import load_checkpoint
from sports_quant.ingest.cost_policies import (
    build_balldontlie_policy,
    build_balldontlie_rate_policy,
    build_mlb_policy,
)
from sports_quant.ingest.f1a import (
    _F1B_AUTHORIZED_ENV,
    _make_gate,
    emit_plan,
    run_pilot_cli,
)
from sports_quant.ingest.tests.test_phase_d2_mlb import game as mlb_game
from sports_quant.ingest.tests.test_phase_d3_nba import game as nba_game
from sports_quant.ingest.tests.test_phase_d3_nba import page as nba_page
from sports_quant.providers.balldontlie import BalldontlieClient
from sports_quant.providers.mlb_statsapi import MlbStatsApiClient
from sports_quant.request_control import (
    CreditBudget,
    RateLimiter,
    RequestBudget,
    RequestGate,
    RequestUnit,
)

SENTINEL = "sk-f1b-reporting-sentinel-do-not-store"


@pytest.fixture
def f1b_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_F1B_AUTHORIZED_ENV, "1")


@pytest.fixture
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block DNS and every non-loopback connect (asyncio needs loopback on Windows)."""

    real_connect = socket.socket.connect

    def boom(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("external network access attempted in an offline test")

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            raise AssertionError(f"external connect to {host!r} in an offline test")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", guarded)


def _bdl_gate(*, cap: int = 8, rate: int = 60) -> RequestGate:
    gate = RequestGate(
        request_budget=RequestBudget(max_requests=cap),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=build_balldontlie_policy(),
        rate_policy=build_balldontlie_rate_policy("goat", rate))
    gate.set_auth_context(auth_applicable=True, configured_tier="goat")
    return gate


def _mlb_gate(*, cap: int = 4) -> RequestGate:
    gate = RequestGate(
        request_budget=RequestBudget(max_requests=cap),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=build_mlb_policy())
    gate.set_auth_context(auth_applicable=False)
    return gate


def _unit(family: str = "games", page: int = 0, entity: str = "") -> RequestUnit:
    return RequestUnit(provider="balldontlie", league="nba", endpoint_family=family,
                       date_key="2026-01-05", page=page, entity_key=entity)


# ===================================================================== #
# A. rate semantics
# ===================================================================== #
def test_rate_policy_active_does_not_imply_rate_limited() -> None:
    gate = _bdl_gate()
    assert gate.usage.rate_policy_active is True
    assert gate.usage.rate_limited is False
    assert gate.usage.throttle_events == 0
    assert gate.usage.throttle_wait_seconds == 0.0
    assert gate.usage.http_429s == 0
    # The contract that actually shipped wrong: a clean run must NOT look throttled.
    gate.reserve(_unit())
    gate.mark_transport()
    gate.record_success(_unit())
    assert gate.usage.rate_limited is False
    assert gate.usage.configured_rate_per_min == 60
    assert gate.usage.provider_rate_limit_per_min == 600


def test_actual_throttle_wait_sets_rate_limited_fields() -> None:
    now = [0.0]
    gate = _bdl_gate(rate=2)
    gate._limiter = RateLimiter(2, clock=lambda: now[0], window=60.0)  # type: ignore[attr-defined]
    assert gate.rate_acquire() == 0.0
    assert gate.rate_acquire() == 0.0
    assert gate.usage.rate_limited is False
    wait = gate.rate_acquire()
    assert wait == pytest.approx(60.0)
    assert gate.usage.throttle_events == 1
    assert gate.usage.throttle_wait_seconds == pytest.approx(60.0)
    assert gate.usage.rate_limited is True
    assert gate.usage.rate_policy_active is True


def test_http_429_sets_rate_limited() -> None:
    gate = _bdl_gate()
    gate.record_429()
    assert gate.usage.http_429s == 1
    assert gate.usage.rate_limited is True
    # A 429 is never a credit figure.
    assert gate.usage.reported_credits_consumed is None


def test_mlb_gate_carries_a_project_courtesy_policy_and_is_not_rate_limited() -> None:
    """MLB is now PACED, but a policy existing is still not rate limiting.

    This test previously asserted the MLB gate had NO rate policy at all. That was
    the accurate description of the code at the time and it was the safety gap:
    the aggregate cap bounded total attempts while nothing bounded the rate. MLB
    now carries a project-owned courtesy pacing policy (30/min, burst 1). The
    distinction the original test protected is preserved and re-asserted:
    ``rate_policy_active`` is about a policy being attached, ``rate_limited`` only
    about a request actually being delayed.
    """

    gate = _make_gate(league="mlb", request_cap=12, credit_cap=None)
    assert gate.rate_policy is not None
    assert gate.usage.rate_policy_active is True
    assert gate.usage.rate_limited is False, "no request has been delayed yet"
    assert gate.usage.throttle_events == 0
    # Our own rate, and no fabricated provider ceiling.
    assert gate.usage.configured_rate_per_min == 30
    assert gate.usage.provider_rate_limit_per_min is None
    assert gate.usage.rate_policy_basis == "project_courtesy_cap"
    assert gate.usage.rate_policy_version == "mlb-pacing-v1"
    # Authentication and tier remain not applicable: MLB StatsAPI is keyless and
    # the pacing policy makes no tier claim.
    assert gate.usage.authentication_status == "not_applicable"
    assert gate.usage.tier_status == "not_applicable"

def test_one_successful_listing_page_counts_one_page() -> None:
    gate = _bdl_gate()
    gate.reserve(_unit(page=0))
    gate.mark_transport()
    gate.record_success(_unit(page=0))
    assert gate.usage.pages_fetched == 1          # page 0 counts (the shipped bug)
    assert gate.usage.transport_starts == 1


def test_failed_transport_counts_no_page() -> None:
    gate = _bdl_gate()
    gate.reserve(_unit(page=0))
    gate.mark_transport()
    gate.record_failure(status_code=500)
    assert gate.usage.pages_fetched == 0
    assert gate.usage.transport_starts == 1
    assert gate.usage.failed_responses == 1


def test_retry_separates_attempts_from_unique_successful_pages() -> None:
    gate = _bdl_gate()
    # First attempt fails, second attempt of the SAME page succeeds.
    gate.reserve(_unit(page=0))
    gate.mark_transport()
    gate.record_failure(status_code=503)
    gate.reserve(_unit(page=0), is_retry=True)
    gate.mark_transport()
    gate.record_success(_unit(page=0))
    # A third successful read of the same page still cannot add a page.
    gate.record_success(_unit(page=0))
    assert gate.usage.attempted_requests == 2     # attempts counted
    assert gate.usage.retry_attempts == 1
    assert gate.usage.transport_starts == 2
    assert gate.usage.pages_fetched == 1          # unique successful page counted once


def test_distinct_pagination_pages_count_deterministically() -> None:
    gate = _bdl_gate()
    for p in (0, 1, 2):
        gate.reserve(_unit(page=p))
        gate.mark_transport()
        gate.record_success(_unit(page=p))
    assert gate.usage.pages_fetched == 3


def test_non_listing_family_does_not_count_as_a_page() -> None:
    gate = _bdl_gate()
    unit = RequestUnit(provider="balldontlie", league="nba",
                       endpoint_family="box_scores", date_key="2026-01-05")
    gate.reserve(unit)
    gate.mark_transport()
    gate.record_success(unit)
    assert gate.usage.pages_fetched == 0
    assert gate.usage.successful_responses == 1


# ===================================================================== #
# B. selection vs budget truncation
# ===================================================================== #
def test_eight_received_two_selected_gives_six_excluded_and_selection_truncated() -> None:
    gate = _bdl_gate()
    gate.record_selection(games_received=8, games_selected=2, excluded=6)
    u = gate.usage
    assert (u.games_received, u.games_selected, u.games_excluded_by_max_games) == (8, 2, 6)
    assert u.selection_truncated is True
    # BUDGET truncation is a different concept and must stay untouched.
    assert u.budget_exhausted is None
    assert u.blocked_requests == 0
    assert u.families_truncated == ()


def test_selection_not_truncated_when_nothing_excluded() -> None:
    gate = _bdl_gate()
    gate.record_selection(games_received=2, games_selected=2, excluded=0)
    assert gate.usage.selection_truncated is False


def test_budget_truncation_is_independent_of_selection_truncation() -> None:
    from sports_quant.request_control import BudgetExhausted
    gate = _bdl_gate(cap=1)
    gate.record_selection(games_received=8, games_selected=2, excluded=6)
    gate.reserve(_unit(page=0))
    with pytest.raises(BudgetExhausted):
        gate.reserve(_unit(page=1))          # cap 1 -> budget exhaustion
    u = gate.usage
    assert u.selection_truncated is True     # planned bound
    assert u.games_excluded_by_max_games == 6
    assert u.attempted_requests == 1         # the blocked attempt reserved nothing


# ===================================================================== #
# D. authentication / tier honesty
# ===================================================================== #
def test_mlb_authentication_is_not_applicable() -> None:
    u = _mlb_gate().usage
    assert u.authentication_status == "not_applicable"
    assert u.authentication_succeeded is None
    assert u.tier_status == "not_applicable"
    assert u.tier_verified is False
    assert u.tier_evidence_source == "none"


def test_tier_status_does_not_overclaim_from_a_games_only_200() -> None:
    gate = _bdl_gate()
    gate.reserve(_unit())
    gate.mark_transport()
    gate.record_success(_unit())              # /v1/games 200 -- available below GOAT
    u = gate.usage
    assert u.authentication_succeeded is True          # auth IS proven
    assert u.authentication_status == "succeeded"
    assert u.tier_verified is False                    # tier is NOT
    assert u.tier_status == "configured_not_verified:goat"
    assert u.tier_evidence_source == "none"
    assert "verified:" not in u.tier_status.replace("not_verified", "")


def test_declared_capabilities_alone_cannot_verify_tier() -> None:
    gate = _bdl_gate()
    gate.record_tier_evidence(source="declared_capabilities", verified=True, tier="goat")
    # Declaration is not observation: verified must be forced back to False.
    assert gate.usage.tier_verified is False
    assert gate.usage.tier_evidence_source == "declared_capabilities"
    assert gate.usage.tier_status == "configured_not_verified:goat"


def test_bounded_capability_audit_can_verify_tier_explicitly() -> None:
    gate = _bdl_gate()
    gate.record_tier_evidence(source="bounded_capability_audit", verified=True, tier="goat")
    assert gate.usage.tier_verified is True
    assert gate.usage.tier_status == "verified:goat"
    assert gate.usage.tier_evidence_source == "bounded_capability_audit"


def test_auth_failure_status_is_recorded_from_401_403() -> None:
    for code in (401, 403):
        gate = _bdl_gate()
        gate.record_failure(status_code=code)
        assert gate.usage.authentication_succeeded is False
        assert gate.usage.authentication_status == "failed"


def test_authentication_status_never_exposes_credentials() -> None:
    gate = _bdl_gate()
    gate.reserve(_unit())
    gate.mark_transport()
    gate.record_success(_unit())
    blob = json.dumps(gate.usage.as_dict(), sort_keys=True, default=str)
    assert SENTINEL not in blob
    for banned in ("api_key", "apikey", "authorization", "bearer", "secret", "token"):
        assert banned not in blob.lower()
    # Only booleans/enums describe auth -- never a credential-shaped value.
    assert isinstance(gate.usage.authentication_succeeded, bool)
    assert gate.usage.authentication_status in ("succeeded", "failed", "unknown",
                                                "not_applicable")


# ===================================================================== #
# E. reports are deterministic and secret-free
# ===================================================================== #
def test_usage_report_is_deterministic_and_secret_free() -> None:
    def build() -> dict[str, Any]:
        gate = _bdl_gate()
        gate.record_selection(games_received=8, games_selected=2, excluded=6)
        gate.reserve(_unit())
        gate.mark_transport()
        gate.record_success(_unit())
        return gate.usage.as_dict()

    a, b = build(), build()
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str)
    assert SENTINEL not in json.dumps(a, default=str)


# ===================================================================== #
# End-to-end: reconstruct BOTH preserved artifact shapes deterministically
# ===================================================================== #
def _mlb_factory(seen: list[httpx.Request], n_games: int = 30):
    """MLB /schedule returning n_games across the pilot's two-day range."""

    games = []
    for i in range(n_games):
        date = "2026-07-20" if i < n_games // 2 else "2026-07-21"
        games.append(mlb_game(game_pk=822788 + i * 7, official_date=date,
                              home_probable=656302, away_probable=607259))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/schedule" in request.url.path:
            body = {"dates": [
                {"date": "2026-07-20",
                 "games": [g for g in games if g["officialDate"] == "2026-07-20"]},
                {"date": "2026-07-21",
                 "games": [g for g in games if g["officialDate"] == "2026-07-21"]}]}
            return httpx.Response(200, json=body,
                                  headers={"content-type": "application/json"})
        return httpx.Response(500, json={"error": f"unexpected {request.url.path}"})

    def factory(gate: RequestGate) -> MlbStatsApiClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://mlb.invalid")
        client = MlbStatsApiClient(client=http, gate=gate, league="mlb", max_retries=0)
        # MLB now paces at 30/min. The delay itself is verified with a mocked
        # clock in test_mlb_pacing.py; here the returned wait is swallowed so the
        # fixture still traverses the real pacing chokepoint without sleeping.
        async def _no_wait(_seconds: float) -> None:
            return None

        client._sleep = _no_wait  # noqa: SLF001 - deterministic test pacing
        return client

    return factory


def _nba_factory(seen: list[httpx.Request], n_games: int = 8):
    games = [nba_game(gid=18447316 + i, date="2026-01-05") for i in range(n_games)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/games":
            return httpx.Response(200, json=nba_page(games),
                                  headers={"content-type": "application/json"})
        return httpx.Response(500, json={"error": f"unexpected {request.url.path}"})

    def factory(gate: RequestGate) -> BalldontlieClient:
        http = build_readonly_client(
            base_url="https://api.balldontlie.io",
            policy=ReadOnlyHTTPPolicy.for_balldontlie(),
            inner_transport=httpx.MockTransport(handler))
        return BalldontlieClient(SENTINEL, client=http, gate=gate, league="nba")

    return factory


def _plan(tmp_path: Path, league: str, **kw: Any) -> Path:
    out = tmp_path / f"{league}.json"
    emit_plan(league=league, includes=(), manifest_out=out, out=lambda _s: None, **kw)
    return out


def _run(tmp_path: Path, league: str, manifest: Path, factory, resume: bool = False):
    db, ckpt = tmp_path / f"{league}.db", tmp_path / f"{league}.ckpt"
    if not db.exists():
        initialize_database(db)
    lines: list[str] = []
    rc = run_pilot_cli(league=league, manifest_path=manifest, scratch_db=db,
                       checkpoint=ckpt, resume=resume, out=lines.append,
                       client_factory=factory)
    return rc, lines, db, ckpt


def test_mlb_skeleton_reconstruction_reports_one_page_and_selection(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    """MLB: 30 received -> 2 selected, 28 excluded, exactly ONE listing page."""

    m = _plan(tmp_path, "mlb", from_date="2026-07-20", to_date="2026-07-21",
              max_games=2, request_cap=4)
    seen: list[httpx.Request] = []
    rc, lines, db, ckpt = _run(tmp_path, "mlb", m, _mlb_factory(seen, 30))
    assert rc == 0, "\n".join(lines)
    assert len(seen) == 1
    ck = load_checkpoint(ckpt)
    u = ck.usage
    assert u["pages_fetched"] == 1                       # defect C repaired
    assert u["games_received"] == 30                     # defect B repaired
    assert u["games_selected"] == 2
    assert u["games_excluded_by_max_games"] == 28
    assert u["selection_truncated"] is True
    assert u["budget_exhausted"] is None                 # NOT budget truncation
    assert u["attempted_requests"] == 1
    assert u["authentication_status"] == "not_applicable"  # defect D: MLB is keyless
    assert u["tier_status"] == "not_applicable"
    # MLB is now paced by a project courtesy policy; a single request never waits.
    assert u["rate_policy_active"] is True
    assert u["rate_policy_basis"] == "project_courtesy_cap"
    assert u["configured_rate_per_min"] == 30
    assert u["provider_rate_limit_per_min"] is None
    assert u["rate_limited"] is False
    assert u["throttle_events"] == 0
    assert u["rate_limited"] is False
    text = "\n".join(lines)
    assert "selection_truncated=True" in text
    assert "excluded_by_max_games=28" in text


def test_nba_skeleton_reconstruction_reports_one_page_and_selection(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    """NBA: 8 received -> 2 selected, 6 excluded, one page, auth proven, tier not."""

    m = _plan(tmp_path, "nba", from_date="2026-01-05", to_date="2026-01-05",
              max_games=2, max_pages=2, rate_per_min=60, request_cap=8)
    seen: list[httpx.Request] = []
    rc, lines, db, ckpt = _run(tmp_path, "nba", m, _nba_factory(seen, 8))
    assert rc == 0, "\n".join(lines)
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/games"
    ck = load_checkpoint(ckpt)
    u = ck.usage
    assert u["pages_fetched"] == 1
    assert u["games_received"] == 8
    assert u["games_selected"] == 2
    assert u["games_excluded_by_max_games"] == 6
    assert u["selection_truncated"] is True
    assert u["budget_exhausted"] is None
    assert u["attempted_requests"] == 1
    # Rate policy active, but nothing was throttled.
    assert u["rate_policy_active"] is True
    assert u["rate_limited"] is False
    assert u["throttle_events"] == 0
    assert u["http_429s"] == 0
    assert u["configured_rate_per_min"] == 60
    assert u["provider_rate_limit_per_min"] == 600
    # Auth proven by the 200; tier NOT proven by a games-only call.
    assert u["authentication_succeeded"] is True
    assert u["tier_verified"] is False
    assert u["tier_status"] == "configured_not_verified:goat"
    # No credential anywhere in the report or the checkpoint.
    assert SENTINEL not in ckpt.read_text(encoding="utf-8")
    assert SENTINEL not in "\n".join(lines)


def test_completed_resume_adds_zero_requests_pages_selections_or_mutations(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    m = _plan(tmp_path, "nba", from_date="2026-01-05", to_date="2026-01-05",
              max_games=2, max_pages=2, rate_per_min=60, request_cap=8)
    seen: list[httpx.Request] = []
    factory = _nba_factory(seen, 8)
    rc, _lines, db, ckpt = _run(tmp_path, "nba", m, factory)
    assert rc == 0
    assert len(seen) == 1
    first = load_checkpoint(ckpt).usage

    before_bytes = ckpt.read_bytes()
    rc2, lines2, _db, _ck = _run(tmp_path, "nba", m, factory, resume=True)
    assert rc2 == 0
    assert len(seen) == 1                      # ZERO additional transport
    # A completed NBA resume with nothing left is a true no-op on disk.
    assert ckpt.read_bytes() == before_bytes
    second = load_checkpoint(ckpt).usage
    # `usage` is the LOGICAL-RUN total, so the first process's evidence survives
    # instead of being overwritten with the resume's zeros.
    assert second == first
    assert second["transport_starts"] == 1
    assert second["pages_fetched"] == 1
    assert second["attempted_requests"] == first["attempted_requests"]  # budget carried
    assert second["database_mutated"] is True
    assert second["http_429s"] == 0
    # Selection accounting survives the resume rather than resetting to zero.
    assert second["games_received"] == 8
    assert second["games_selected"] == 2
    assert second["games_excluded_by_max_games"] == 6
    assert second["selection_truncated"] is True
    assert second["authentication_status"] == "succeeded"
    # The resuming process itself reports zero new work, and the human report
    # shows the preserved logical totals beside its own zeros rather than a
    # misleading clean-zero summary.
    text = "\n".join(lines2)
    assert "no work remaining" in text
    assert "new_work=False" in text and "checkpoint_mutated=False" in text
    # Nothing is attributed to this process; every count is prior evidence.
    assert "requests      this_process=0 prior=1 logical_total=1" in text
    assert "successes     this_process=0 prior=1 logical_total=1" in text
    # `pages_fetched` counts LISTING/discovery pages only, so the label says so --
    # a bare `pages` beside `requests` read as a total provider page count.
    assert "listing_pages this_process=0 prior=1 logical_total=1" in text
    assert "pages      this_process" not in text


# ===================================================================== #
# MLB probable-pitcher hydration policy (documented classification)
# ===================================================================== #
def test_probable_pitcher_hydration_stays_within_the_schedule_family(
    tmp_path: Path, f1b_authorized: None, no_external_network: None
) -> None:
    """Documented policy: inline hydration is part of the schedule representation.

    ``hydrate=probablePitcher`` is a response-SHAPING parameter on the same
    ``/schedule`` endpoint. It therefore stays inside the reviewed ``schedule``
    family: it adds no request, no endpoint family, and no budget consumption, and it
    must never populate a rich-data table (``probable_pitcher_snapshots``). Any future
    strict-PIT skeleton that must suppress hydration changes the real planned request
    shape and so requires a NEW cost/plan policy version, not a retrofit.
    """

    import sqlite3

    m = _plan(tmp_path, "mlb", from_date="2026-07-20", to_date="2026-07-21",
              max_games=2, request_cap=4)
    seen: list[httpx.Request] = []
    rc, lines, db, _ckpt = _run(tmp_path, "mlb", m, _mlb_factory(seen, 30))
    assert rc == 0, "\n".join(lines)

    # One request only -- hydration costs nothing extra.
    assert len(seen) == 1
    assert "/schedule" in seen[0].url.path
    assert dict(seen[0].url.params).get("hydrate") == "probablePitcher"

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # The hydrated ids live INLINE on the schedule snapshot ...
        rows = con.execute("select home_probable_pitcher_id, away_probable_pitcher_id "
                           "from game_schedule_snapshots").fetchall()
        assert rows and all(r[0] is not None for r in rows)
        # ... and never as a rich-family observation.
        assert con.execute(
            "select count(*) from probable_pitcher_snapshots").fetchone()[0] == 0
        assert con.execute("select count(*) from players").fetchone()[0] == 0
        assert con.execute("select count(*) from roster_snapshots").fetchone()[0] == 0
        # The persisted endpoint stays a sanitized path (no query string).
        ep = con.execute("select endpoint from raw_responses").fetchone()[0]
        assert ep == "/schedule" and "?" not in ep
        # Exactly one raw response: the single schedule page.
        assert con.execute("select count(*) from raw_responses").fetchone()[0] == 1
    finally:
        con.close()


def test_schedule_family_is_a_listing_family_and_games_too() -> None:
    from sports_quant.request_control import LISTING_FAMILIES
    assert "schedule" in LISTING_FAMILIES      # MLB discovery
    assert "games" in LISTING_FAMILIES         # NBA discovery
    # Rich families are never listing pages.
    for rich in ("box_scores", "stats", "advanced_stats", "plays", "lineups"):
        assert rich not in LISTING_FAMILIES
