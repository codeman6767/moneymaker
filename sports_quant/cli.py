"""Command-line entry points for the read-only engine.

Currently exposes ``providers-check``: a safe, GET-only smoke test that
confirms both public-data providers are reachable and reports sanitized record
counts plus Odds API credit headers.

Guarantees:

* It never places, cancels or simulates an order -- no execution module is
  imported here, and every request is forced through the GET-only transport
  policy in :mod:`sports_quant.http_policy`.
* It never prints the Odds API key; only its presence is reported.
* Each check is classified as **ok**, **skipped** (inactive / out of season /
  not configured) or **failed**. The process exits non-zero only when something
  that was genuinely active failed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

from .config import ReadOnlyStartupError, Settings, load_settings
from .providers.kalshi import KalshiClient
from .providers.odds_api import (
    MLB_SPORT_KEY,
    NBA_SPORT_KEY,
    CreditHeaders,
    OddsApiClient,
    OddsApiResult,
)

Printer = Callable[[str], None]


class CheckStatus(str, Enum):
    """Outcome of a single provider check."""

    OK = "ok"
    #: Nothing was wrong -- the league is out of season, or the provider is not
    #: configured. Never contributes to a non-zero exit code.
    SKIPPED = "skipped"
    #: Something active genuinely failed. Forces a non-zero exit code.
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    """One named check plus its status and a sanitized human-readable detail."""

    name: str
    status: CheckStatus
    detail: str

    def line(self) -> str:
        return f"[{self.status.value.upper():7}] {self.name}: {self.detail}"


def _fmt_credits(credits: CreditHeaders) -> str:
    return (
        f"requests-remaining={credits.requests_remaining} "
        f"requests-used={credits.requests_used} "
        f"requests-last={credits.requests_last}"
    )


def _describe(exc: BaseException) -> str:
    """Render an exception safely (provider adapters already sanitize URLs)."""

    return f"{type(exc).__name__}: {exc}"


async def _check_sport(
    *,
    label: str,
    active: Optional[bool],
    fetch: Callable[[], Awaitable[OddsApiResult]],
) -> CheckResult:
    """Fetch odds for one sport.

    An out-of-season league is a **successful skip**, not a failure. A league
    the provider reports as active that then fails to fetch *is* a failure.
    """

    if active is False:
        return CheckResult(
            f"odds/{label}", CheckStatus.SKIPPED, "not active (out of season)"
        )
    try:
        result = await fetch()
    except Exception as exc:  # noqa: BLE001 -- classify, do not crash the check
        return CheckResult(f"odds/{label}", CheckStatus.FAILED, _describe(exc))
    return CheckResult(
        f"odds/{label}",
        CheckStatus.OK,
        f"{len(result.events)} events | {_fmt_credits(result.credits)}",
    )


async def _check_odds_api(settings: Settings) -> list[CheckResult]:
    """Check The Odds API: key presence, sports list, then MLB/NBA odds."""

    if not settings.has_odds_api_key():
        return [
            CheckResult(
                "odds/api-key",
                CheckStatus.SKIPPED,
                "not configured -- set ODDS_API_KEY in .env (value never displayed)",
            )
        ]

    results = [
        CheckResult("odds/api-key", CheckStatus.OK, "present (value not displayed)")
    ]
    odds = OddsApiClient(settings.odds_api_key)
    try:
        try:
            sports = await odds.get_sports()
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult("odds/sports", CheckStatus.FAILED, _describe(exc)))
            return results

        active_keys = {s.key for s in sports.sports if s.active}
        results.append(
            CheckResult(
                "odds/sports",
                CheckStatus.OK,
                f"{len(sports.sports)} listed | {_fmt_credits(sports.credits)}",
            )
        )
        results.append(
            await _check_sport(
                label="MLB",
                active=MLB_SPORT_KEY in active_keys,
                fetch=odds.get_mlb_odds,
            )
        )
        results.append(
            await _check_sport(
                label="NBA",
                active=NBA_SPORT_KEY in active_keys,
                fetch=odds.get_nba_odds,
            )
        )
    finally:
        await odds.aclose()
    return results


async def _check_kalshi(settings: Settings) -> list[CheckResult]:
    """Check Kalshi public REST: exchange status, then five open markets."""

    results: list[CheckResult] = []
    kalshi = KalshiClient(base_url=settings.kalshi_public_rest_url)
    try:
        try:
            status = await kalshi.exchange_status()
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult("kalshi/status", CheckStatus.FAILED, _describe(exc)))
        else:
            results.append(
                CheckResult(
                    "kalshi/status",
                    CheckStatus.OK,
                    f"exchange_active={status.get('exchange_active')} "
                    f"trading_active={status.get('trading_active')}",
                )
            )

        try:
            markets = await kalshi.list_markets(status="open", limit=5)
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult("kalshi/markets", CheckStatus.FAILED, _describe(exc)))
        else:
            results.append(
                CheckResult(
                    "kalshi/markets",
                    CheckStatus.OK,
                    f"retrieved {len(markets.items)} open markets (requested 5)",
                )
            )
    finally:
        await kalshi.aclose()
    return results


async def run_providers_check(
    settings: Optional[Settings] = None,
    *,
    out: Printer = print,
) -> int:
    """Run the read-only provider check.

    Returns ``0`` when nothing active failed and ``1`` when at least one active
    provider genuinely failed. Out-of-season leagues and unconfigured providers
    are reported as successful skips.
    """

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    out("Read-only provider check (GET-only; no orders are placed or simulated)")
    out(f"  Kalshi environment: {settings.kalshi_environment}")
    out(f"  Kalshi public REST: {settings.kalshi_public_rest_url}")

    results = await _check_odds_api(settings)
    results += await _check_kalshi(settings)

    for result in results:
        out(result.line())

    counts = {
        status: sum(1 for r in results if r.status is status) for status in CheckStatus
    }
    out(
        f"Summary: {counts[CheckStatus.OK]} ok, "
        f"{counts[CheckStatus.SKIPPED]} skipped (inactive), "
        f"{counts[CheckStatus.FAILED]} failed."
    )
    return 1 if counts[CheckStatus.FAILED] else 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI dispatch. Usage: ``python -m sports_quant providers-check``."""

    import argparse

    parser = argparse.ArgumentParser(prog="sports_quant", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("providers-check", help="Read-only reachability check for both data providers")

    args = parser.parse_args(argv)

    if args.command == "providers-check":
        try:
            return asyncio.run(run_providers_check())
        except ReadOnlyStartupError as exc:
            print(str(exc))
            return 2

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error raises SystemExit
