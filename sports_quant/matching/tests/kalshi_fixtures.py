"""Sanitized real-contract Kalshi fixtures from a bounded public audit (D5B2).

Provenance: captured by a bounded, GET-only, UNAUTHENTICATED audit of the public
Kalshi surface (`https://external-api.kalshi.com/trade-api/v2`, `/events` with
nested markets) on the D5B2 live-contract-repair task (session date 2026-07;
3 GETs total). Only the minimum public fields needed for deterministic parser
tests are recorded here -- no prices, volume, order books, trades, or full
payloads. These are the AUTHORITATIVE current provider shapes; synthetic strings
used only for unit edge cases are labelled ``SYNTHETIC`` at their call site.

Verified current shapes:
* MLB ``KXMLBGAME`` (parser ``kmlb-2``): event ``KXMLBGAME-{YYMONDD}{HHMM}{AWAY}{HOME}``;
  title ``A vs B``; rules ``If {Yes} wins the {A} vs {B} professional baseball
  game originally scheduled for {Mon D, YYYY} at {H:MM AM/PM TZ}, then ...``.
* NBA ``KXNBAGAME`` (parser ``knba-1``): event ``KXNBAGAME-{YYMONDD}{AWAY}{HOME}``
  (date-only); title ``[Game N: ]A at B``; rules ``... originally scheduled for
  {Mon D, YYYY}, then ...`` (date only, no clock).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KalshiFixture:
    """One audited public game market (sanitized)."""

    series_ticker: str
    event_ticker: str
    market_ticker: str
    title: str
    sub_title: str
    yes_sub_title: str
    no_sub_title: str
    rules_primary: str
    provenance: str


# MLB #1 -- KXMLBGAME-26JUL271840AZPIT (Arizona at Pittsburgh, 18:40 ET).
MLB_AZ_PIT = KalshiFixture(
    series_ticker="KXMLBGAME",
    event_ticker="KXMLBGAME-26JUL271840AZPIT",
    market_ticker="KXMLBGAME-26JUL271840AZPIT-AZ",
    title="Arizona vs Pittsburgh",
    sub_title="AZ vs PIT (Jul 27)",
    yes_sub_title="Arizona",
    no_sub_title="Arizona",  # current Kalshi: no_sub_title == the Yes-subject team
    rules_primary=("If Arizona wins the Arizona vs Pittsburgh professional baseball game "
                   "originally scheduled for Jul 27, 2026 at 6:40 PM EDT, then the market "
                   "resolves to Yes."),
    provenance="public GET /events KXMLBGAME, audited 2026-07",
)

# MLB #2 -- a different team-code shape (Baltimore at Detroit).
MLB_BAL_DET = KalshiFixture(
    series_ticker="KXMLBGAME",
    event_ticker="KXMLBGAME-26JUL271840BALDET",
    market_ticker="KXMLBGAME-26JUL271840BALDET-BAL",
    title="Baltimore vs Detroit",
    sub_title="BAL vs DET (Jul 27)",
    yes_sub_title="Baltimore",
    no_sub_title="Baltimore",
    rules_primary=("If Baltimore wins the Baltimore vs Detroit professional baseball game "
                   "originally scheduled for Jul 27, 2026 at 6:40 PM EDT, then the market "
                   "resolves to Yes."),
    provenance="public GET /events KXMLBGAME, audited 2026-07",
)

# NBA -- KXNBAGAME-26JUN13NYKSAS (Game 5: New York at San Antonio, date-only).
NBA_NYK_SAS = KalshiFixture(
    series_ticker="KXNBAGAME",
    event_ticker="KXNBAGAME-26JUN13NYKSAS",
    market_ticker="KXNBAGAME-26JUN13NYKSAS-SAS",
    title="Game 5: New York at San Antonio",
    sub_title="NYK at SAS (Jun 13)",
    yes_sub_title="San Antonio",
    no_sub_title="San Antonio",
    rules_primary=("If San Antonio wins the Game 5: New York at San Antonio professional "
                   "basketball game originally scheduled for Jun 13, 2026, then the market "
                   "resolves to Yes."),
    provenance="public GET /events KXNBAGAME, audited 2026-07",
)
