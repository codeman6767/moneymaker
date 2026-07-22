"""Pregame-prior integration.

The pregame prior comes from the research lane (expected runs for MLB, expected
margin/total for NBA). The live model does not relearn the prior; it *adjusts*
it with in-game state. Here we convert pregame expectations into a prior
win-probability logit that becomes feature 0 of every live feature vector, so
the fast model is literally a residual on top of the prior.
"""

from __future__ import annotations

import math

# Scale factors chosen so plausible pregame edges map to sensible priors.
MLB_RUN_DIFF_TO_LOGIT = 0.35   # ~1 run favorite -> ~0.35 logit (~0.59 prob)
NBA_MARGIN_TO_LOGIT = 1.0 / 6.0  # ~6 point favorite -> ~1.0 logit (~0.73 prob)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def mlb_prior_logit(exp_runs_home: float, exp_runs_away: float) -> float:
    return (exp_runs_home - exp_runs_away) * MLB_RUN_DIFF_TO_LOGIT


def mlb_prior_prob(exp_runs_home: float, exp_runs_away: float) -> float:
    return _sigmoid(mlb_prior_logit(exp_runs_home, exp_runs_away))


def nba_prior_logit(exp_margin: float) -> float:
    """exp_margin is the pregame expected home margin (home minus away)."""

    return exp_margin * NBA_MARGIN_TO_LOGIT


def nba_prior_prob(exp_margin: float) -> float:
    return _sigmoid(nba_prior_logit(exp_margin))
