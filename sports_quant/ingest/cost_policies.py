"""Typed endpoint-cost policies for the F1A request gate (offline).

Two providers participate in the F1 pilot:

* **MLB StatsAPI** -- keyless/public. Requests are hard-capped, but there is no
  paid credit balance: credits are **not applicable** and never fabricated.
* **BALLDONTLIE (GOAT)** -- metered. We model one metered API call as **one
  credit** (the conservative GOAT request-quota unit); this 1:1 model is the
  cost-policy's documented semantics, versioned so a future weighting bumps the
  version. An endpoint family **not** in the policy has an *unknown* cost, which
  makes a credit-capped plan/run non-executable (fail closed) -- never assumed 1.

Each policy carries a ``classifier`` mapping a raw request path to its endpoint
family, so the gate at the transport chokepoint can label *any* call even if a
helper forgot to -- an unclassifiable path becomes the ``unknown`` family, which
under a credit cap fails closed.
"""

from __future__ import annotations

from ..request_control import EndpointCostPolicy

# --- MLB StatsAPI (keyless; credits N/A) ----------------------------------- #
MLB_FAMILIES: frozenset[str] = frozenset(
    {"schedule", "teams", "venue", "game_linescore", "game_boxscore", "roster",
     "person", "unknown"}
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
    )


# --- BALLDONTLIE (GOAT; per-request credit cost UNPROVEN -> fail closed) ---- #
# Independent review found NO authoritative, versioned basis in the repository for
# a per-endpoint BALLDONTLIE credit cost: the provider documentation describes
# per-minute request-rate limits by tier, not a per-request credit weight, and no
# response-header contract for consumed/remaining credits is documented. Assuming
# "1 credit per request" would be a guess that makes NBA planning falsely
# executable. Per the F1A review we therefore assign NO costs: every BALLDONTLIE
# family has an UNKNOWN credit cost, so any credit-capped NBA plan/run is
# non-executable and fails closed with `unknown_credit_cost` until a later
# controlled capability verification (F1B pilot) establishes and versions the real
# cost contract. Requests are still hard-capped; credits are never fabricated. To
# populate this once an authoritative source exists, bump the version below and add
# the audited per-endpoint costs.
BALLDONTLIE_COSTS: dict[str, int] = {}


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
        # /games/{id} vs /games list share the games family (cost 1 either way).
        return "game" if any(seg.isdigit() for seg in p.split("/")) else "games"
    if "/teams" in p:
        return "teams"
    if "/players" in p:
        return "players"
    return "unknown"


def build_balldontlie_policy() -> EndpointCostPolicy:
    return EndpointCostPolicy(
        provider="balldontlie",
        version="bdl-cost-v1",
        credit_applicable=True,
        costs=BALLDONTLIE_COSTS,
        classifier=_classify_balldontlie,
    )
