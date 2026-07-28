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


# --- BALLDONTLIE (GOAT; credits == metered calls, 1:1 conservative) -------- #
#: One metered call == one credit (conservative GOAT request-quota unit).
BALLDONTLIE_COSTS: dict[str, int] = {
    "teams": 1,
    "players": 1,
    "games": 1,
    "game": 1,
    "box_scores": 1,
    "stats": 1,
    "advanced_stats": 1,
    "plays": 1,
    "lineups": 1,
    "player_injuries": 1,
}


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
