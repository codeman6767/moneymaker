"""``providers-check`` behaviour: status classification, exit codes, redaction.

The check must never place or simulate an order, never print the Odds API key,
use GET only, treat an out-of-season league as a successful skip, and exit
non-zero only when something genuinely active fails.
"""

from __future__ import annotations

import httpx
import pytest

from sports_quant.cli import CheckStatus, run_providers_check
from sports_quant.config import PRODUCTION_KALSHI_REST_URL, Settings

API_KEY = "cli-test-key-do-not-log"

SPORTS_MLB_ACTIVE_NBA_OFF = [
    {"key": "baseball_mlb", "title": "MLB", "active": True, "has_outcomes": True},
    {"key": "basketball_nba", "title": "NBA", "active": False, "has_outcomes": True},
]


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        odds_api_key=API_KEY,
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=True,
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[httpx.Request],
    *,
    mlb_status: int = 200,
    kalshi_status: int = 200,
) -> None:
    """Make ``build_readonly_client`` wrap a MockTransport instead of the network.

    The policy wrapper is preserved, so GET-only + allow-list enforcement still
    applies exactly as it would against the real network.
    """

    import sports_quant.http_policy as http_policy
    import sports_quant.providers.kalshi as kalshi_mod
    import sports_quant.providers.odds_api as odds_mod

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v4/sports":
            return httpx.Response(200, json=SPORTS_MLB_ACTIVE_NBA_OFF)
        if path.endswith("/odds"):
            if mlb_status != 200:
                return httpx.Response(mlb_status, json={"message": "boom"})
            return httpx.Response(200, json=[])
        if path.endswith("/exchange/status"):
            if kalshi_status != 200:
                return httpx.Response(kalshi_status, json={"message": "boom"})
            return httpx.Response(200, json={"exchange_active": True, "trading_active": True})
        if path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        return httpx.Response(404, json={})

    real_build = http_policy.build_readonly_client

    def build(**kwargs: object) -> httpx.AsyncClient:
        kwargs["inner_transport"] = httpx.MockTransport(handler)
        return real_build(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(kalshi_mod, "build_readonly_client", build)
    monkeypatch.setattr(odds_mod, "build_readonly_client", build)


async def test_all_healthy_exits_zero_and_skips_out_of_season_league(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    _install_mock(monkeypatch, seen)
    lines: list[str] = []

    code = await run_providers_check(_settings(), out=lines.append)

    assert code == 0
    output = "\n".join(lines)
    # NBA is inactive -> a successful skip, not a failure.
    assert "odds/NBA" in output
    assert CheckStatus.SKIPPED.value.upper() in output
    assert CheckStatus.FAILED.value.upper() not in output
    # Every request was a GET; nothing was ordered or cancelled.
    assert {r.method for r in seen} == {"GET"}


async def test_active_provider_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    # MLB is active in the sports list but its odds endpoint fails.
    _install_mock(monkeypatch, seen, mlb_status=500)
    lines: list[str] = []

    code = await run_providers_check(_settings(), out=lines.append)

    assert code == 1
    assert "odds/MLB" in "\n".join(lines)
    assert CheckStatus.FAILED.value.upper() in "\n".join(lines)


async def test_kalshi_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    _install_mock(monkeypatch, seen, kalshi_status=503)
    lines: list[str] = []

    code = await run_providers_check(_settings(), out=lines.append)

    assert code == 1
    assert "kalshi/status" in "\n".join(lines)


async def test_api_key_is_never_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    _install_mock(monkeypatch, seen, mlb_status=500)
    lines: list[str] = []

    await run_providers_check(_settings(), out=lines.append)

    output = "\n".join(lines)
    assert API_KEY not in output
    assert "value not displayed" in output


async def test_missing_key_is_a_skip_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    _install_mock(monkeypatch, seen)
    lines: list[str] = []

    code = await run_providers_check(_settings(odds_api_key=""), out=lines.append)

    assert code == 0
    output = "\n".join(lines)
    assert "odds/api-key" in output
    assert "not configured" in output
    # No Odds API request was attempted without a key.
    assert not [r for r in seen if "the-odds-api" in (r.url.host or "")]
