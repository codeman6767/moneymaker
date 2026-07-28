"""Shared helpers for F1A tests (not a test module).

``known_cost_policy`` is a TEST-ONLY credit policy with explicit per-family costs,
used to exercise the provider-agnostic gate/pilot MECHANISM. It must never be
confused with the real :func:`sports_quant.ingest.cost_policies.build_balldontlie_policy`,
which intentionally has UNKNOWN costs (fail closed) until an authoritative source
exists. Dedicated tests assert that real policy fails closed.
"""

from __future__ import annotations

from sports_quant.ingest.cost_policies import _classify_balldontlie
from sports_quant.request_control import EndpointCostPolicy

_KNOWN_COSTS = {
    "teams": 1, "players": 1, "games": 1, "game": 1, "box_scores": 1, "stats": 1,
    "advanced_stats": 1, "plays": 1, "lineups": 1, "player_injuries": 1, "schedule": 1,
}


def known_cost_policy() -> EndpointCostPolicy:
    """A metered TEST policy (1 credit/family) for gate/pilot mechanism tests."""

    return EndpointCostPolicy(
        provider="test_metered", version="test-cost-v1", credit_applicable=True,
        costs=_KNOWN_COSTS, classifier=_classify_balldontlie,
    )
