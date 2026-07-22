"""Command-line entry points for the read-only engine.

Currently exposes ``providers-check``: a safe, GET-only smoke test that
confirms both public-data providers are reachable and reports sanitized record
counts plus Odds API credit headers. It never places or simulates an order and
never prints the Odds API key.
"""

from __future__ import annotations

import asyncio
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


def _fmt_credits(credits: CreditHeaders) -> str:
    return (
        f"requests-remaining={credits.requests_remaining} "
        f"requests-used={credits.requests_used} "
        f"requests-last={credits.requests_last}"
    )


async def _check_sport(
    odds: OddsApiClient,
    out: Printer,
    *,
    label: str,
    sport_key: str,
    active: Optional[bool],
    fetch: Callable[[], Awaitable[OddsApiResult]],
) -> None:
    """Fetch odds for one sport, tolerating out-of-season / unavailable cases."""

    if active is False:
        out(f"[odds] {label}: not active (out of season) -- skipped")
        return
    try:
        result = await fetch()
        out(f"[odds] {label}: {len(result.events)} events | {_fmt_credits(result.credits)}")
    except Exception as exc:  # noqa: BLE001 -- report, never fail the whole check
        out(f"[odds] {label}: unavailable ({type(exc).__name__}: {exc})")


async def run_providers_check(
    settings: Optional[Settings] = None,
    *,
    out: Printer = print,
) -> int:
    """Run the read-only provider check. Returns a process exit code."""

    if settings is None:
        settings = load_settings()
    else:
        settings.enforce_read_only()

    out("Read-only provider check (GET-only; no orders are placed or simulated)")
    out(f"  Kalshi environment: {settings.kalshi_environment}")
    out(f"  Kalshi public REST: {settings.kalshi_public_rest_url}")

    # 1) Odds API key presence -- existence only, never the value.
    if settings.has_odds_api_key():
        out("[odds] API key: present (value not displayed)")
    else:
        out("[odds] API key: MISSING -- set ODDS_API_KEY in .env to query The Odds API")

    ok = True

    # 2) The Odds API: sports list, then MLB/NBA odds when those sports are active.
    if settings.has_odds_api_key():
        odds = OddsApiClient(settings.odds_api_key)
        try:
            sports = await odds.get_sports()
            active_keys = {s.key for s in sports.sports if s.active}
            out(f"[odds] sports: {len(sports.sports)} listed | {_fmt_credits(sports.credits)}")

            await _check_sport(
                odds, out,
                label="MLB",
                sport_key=MLB_SPORT_KEY,
                active=MLB_SPORT_KEY in active_keys,
                fetch=lambda: odds.get_mlb_odds(),
            )
            await _check_sport(
                odds, out,
                label="NBA",
                sport_key=NBA_SPORT_KEY,
                active=NBA_SPORT_KEY in active_keys,
                fetch=lambda: odds.get_nba_odds(),
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            out(f"[odds] sports endpoint unavailable ({type(exc).__name__}: {exc})")
        finally:
            await odds.aclose()
    else:
        out("[odds] skipping Odds API calls: no API key configured")

    # 3) Kalshi public REST: exchange status, then five open markets.
    kalshi = KalshiClient(base_url=settings.kalshi_public_rest_url)
    try:
        status = await kalshi.exchange_status()
        out(
            "[kalshi] exchange status: "
            f"exchange_active={status.get('exchange_active')} "
            f"trading_active={status.get('trading_active')}"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        out(f"[kalshi] exchange status unavailable ({type(exc).__name__}: {exc})")

    try:
        markets = await kalshi.list_markets(status="open", limit=5)
        out(f"[kalshi] open markets: retrieved {len(markets.items)} (requested 5)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        out(f"[kalshi] open markets unavailable ({type(exc).__name__}: {exc})")
    finally:
        await kalshi.aclose()

    out("Provider check complete." if ok else "Provider check finished with warnings.")
    return 0 if ok else 1


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
