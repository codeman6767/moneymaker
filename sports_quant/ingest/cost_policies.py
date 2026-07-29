"""Typed endpoint policies for the F1A request gate (offline).

Two providers participate in the F1 pilot, and NEITHER is credit/billing metered:

* **MLB StatsAPI** -- keyless/public. Requests are hard-capped; credits are **not
  applicable** and never fabricated.
* **BALLDONTLIE** -- authenticated by a subscription tier, but the official
  documentation meters access by a **per-minute REQUEST-RATE limit** per tier
  (Free 5/min, ALL-STAR 60/min, GOAT 600/min), NOT by a monetary credit balance
  or endpoint-weighted credit cost, and it publishes no consumed/remaining-credit
  response-header contract. We therefore model BALLDONTLIE credits as **not
  applicable** (never fabricated) and instead attach a versioned request-RATE
  policy (:func:`build_balldontlie_rate_policy`) whose configured rate defaults
  well below the tier maximum. The hard aggregate request cap still bounds the
  total call count for the logical run; retries and pagination each count as a
  request; and an unrecognised endpoint family still fails closed at the gate.

Each policy carries a ``classifier`` (path -> endpoint family) and a declared
``known_families`` set, so the gate can label any call from the trusted path and
fail closed on an unclassifiable one.
"""

from __future__ import annotations

from ..request_control import EndpointCostPolicy, RequestRatePolicy

# --- MLB StatsAPI (keyless; credits N/A) ----------------------------------- #
MLB_FAMILIES: frozenset[str] = frozenset(
    {"schedule", "teams", "venue", "game_linescore", "game_boxscore", "roster", "person"}
)


def _classify_mlb(path: str) -> str:
    p = path.lower()
    if "/schedule" in p:
        return "schedule"
    if "/teams" in p:
        return "teams"
    if "/venues" in p:
        return "venue"
    if "linescore" in p:
        return "game_linescore"
    if "boxscore" in p:
        return "game_boxscore"
    if "/roster" in p:
        return "roster"
    if "/people" in p or "/person" in p:
        return "person"
    return "unknown"


def build_mlb_policy() -> EndpointCostPolicy:
    return EndpointCostPolicy(
        provider="mlb_statsapi",
        version="mlb-cost-v1",
        credit_applicable=False,  # keyless/public: no paid credit balance
        costs={},
        classifier=_classify_mlb,
        known_families=MLB_FAMILIES,
    )


# --- BALLDONTLIE (request-rate limited by tier; credits N/A) ---------------- #
BALLDONTLIE_FAMILIES: frozenset[str] = frozenset(
    {"teams", "players", "games", "game", "box_scores", "stats", "advanced_stats",
     "plays", "lineups", "player_injuries"}
)

#: Official per-minute request-rate ceilings by BALLDONTLIE tier (requests/min).
#: Source: BALLDONTLIE API documentation (rate limits section). These are REQUEST
#: rates, not credit balances.
BALLDONTLIE_TIER_RATES: dict[str, int] = {"free": 5, "all-star": 60, "goat": 600}

#: Conservative default operating rate, well below the GOAT tier maximum. A pilot
#: manifest may lower it further; it can never exceed the verified tier maximum.
BALLDONTLIE_DEFAULT_RATE_PER_MIN = 100
BALLDONTLIE_RATE_POLICY_VERSION = "bdl-rate-v1"


def _classify_balldontlie(path: str) -> str:
    p = path.lower()
    if "advanced" in p:
        return "advanced_stats"
    if "box_scores" in p:
        return "box_scores"
    if "player_injuries" in p or "injuries" in p:
        return "player_injuries"
    if "/plays" in p:
        return "plays"
    if "/lineups" in p:
        return "lineups"
    if "/stats" in p:
        return "stats"
    if "/games" in p:
        return "game" if any(seg.isdigit() for seg in p.split("/")) else "games"
    if "/teams" in p:
        return "teams"
    if "/players" in p:
        return "players"
    return "unknown"


def build_balldontlie_policy() -> EndpointCostPolicy:
    # credits NOT applicable: BALLDONTLIE is request-rate limited, not credit
    # metered. Requests are hard-capped and rate-limited; an unrecognised endpoint
    # family fails closed via ``known_families``.
    return EndpointCostPolicy(
        provider="balldontlie",
        version="bdl-cost-v1",
        credit_applicable=False,
        costs={},
        classifier=_classify_balldontlie,
        known_families=BALLDONTLIE_FAMILIES,
    )


def build_balldontlie_rate_policy(
    tier: str = "goat", configured_per_min: int = BALLDONTLIE_DEFAULT_RATE_PER_MIN
) -> RequestRatePolicy:
    """A versioned BALLDONTLIE request-rate policy for ``tier`` (default GOAT).

    ``configured_per_min`` is validated (by :class:`RequestRatePolicy`) to never
    exceed the verified tier maximum; the default is conservatively far below it.
    """

    tier_key = tier.lower()
    if tier_key not in BALLDONTLIE_TIER_RATES:
        raise ValueError(f"unknown BALLDONTLIE tier {tier!r}")
    return RequestRatePolicy(
        provider="balldontlie",
        version=BALLDONTLIE_RATE_POLICY_VERSION,
        tier=tier_key,
        tier_max_per_min=BALLDONTLIE_TIER_RATES[tier_key],
        configured_per_min=configured_per_min,
    )
