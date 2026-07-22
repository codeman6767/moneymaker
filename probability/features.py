"""Fixed-size feature vectors for live win-probability inference.

Every live event is turned into a fixed-length ``float32`` NumPy vector -- no
pandas, no dict-of-arrays, no per-event allocation of variable-size structures
(a hot-path rule in ``CLAUDE.md``). The layout is stable and documented so it
maps cleanly onto a Rust struct later if the execution service is ported.

Feature 0 is always the pregame prior logit (see :mod:`pregame_prior`); the rest
are the live state. Each spec also carries per-feature out-of-distribution
bounds used by the OOD detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

from . import pregame_prior

F32 = np.float32


@dataclass(frozen=True)
class FeatureSpec:
    sport: str
    names: Tuple[str, ...]
    lo: np.ndarray  # per-feature OOD lower bound (float32)
    hi: np.ndarray  # per-feature OOD upper bound (float32)

    @property
    def size(self) -> int:
        return len(self.names)


# --------------------------------------------------------------------------- #
# MLB
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MLBLiveState:
    exp_runs_home: float
    exp_runs_away: float
    inning: int
    half: str  # "top" | "bottom"
    outs: int
    on_1b: bool
    on_2b: bool
    on_3b: bool
    score_home: int
    score_away: int
    pitcher_quality: float = 0.5
    bullpen_quality: float = 0.5
    bullpen_availability: float = 1.0
    lineup_position: int = 1
    modeled_is_home: bool = True

    def game_progress(self) -> float:
        frac = ((self.inning - 1) + (0.5 if self.half == "bottom" else 0.0)) / 9.0
        return max(0.0, min(1.0, frac))


MLB_FEATURE_NAMES = (
    "prior_logit",
    "inning_norm",
    "half_bottom",
    "outs_norm",
    "on_1b",
    "on_2b",
    "on_3b",
    "score_diff_scaled",
    "score_diff_leverage",
    "pitcher_quality",
    "bullpen_quality",
    "bullpen_availability",
    "lineup_pos_norm",
    "modeled_is_home",
)

MLB_SPEC = FeatureSpec(
    sport="mlb",
    names=MLB_FEATURE_NAMES,
    lo=np.array([-3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -4.0, -4.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=F32),
    hi=np.array([3.0, 1.5, 1.0, 1.0, 1.0, 1.0, 1.0, 4.0, 4.0, 1.0, 1.0, 1.0, 1.2, 1.0], dtype=F32),
)


def mlb_vector(state: MLBLiveState, out: np.ndarray | None = None) -> np.ndarray:
    """Vectorize an MLB live state into MLB_SPEC layout (float32)."""

    if out is None:
        out = np.empty(MLB_SPEC.size, dtype=F32)
    prior = pregame_prior.mlb_prior_logit(state.exp_runs_home, state.exp_runs_away)
    score_diff = state.score_home - state.score_away
    progress = state.game_progress()
    out[0] = prior
    out[1] = state.inning / 9.0
    out[2] = 1.0 if state.half == "bottom" else 0.0
    out[3] = state.outs / 2.0
    out[4] = 1.0 if state.on_1b else 0.0
    out[5] = 1.0 if state.on_2b else 0.0
    out[6] = 1.0 if state.on_3b else 0.0
    out[7] = score_diff / 4.0
    out[8] = (score_diff * progress) / 4.0
    out[9] = state.pitcher_quality
    out[10] = state.bullpen_quality
    out[11] = state.bullpen_availability
    out[12] = state.lineup_position / 9.0
    out[13] = 1.0 if state.modeled_is_home else 0.0
    return out


# --------------------------------------------------------------------------- #
# NBA
# --------------------------------------------------------------------------- #
NBA_TOTAL_GAME_SECONDS = 48 * 60.0


@dataclass(frozen=True)
class NBALiveState:
    exp_margin: float
    exp_total: float
    period: int
    seconds_remaining: float  # remaining in the whole game
    score_home: int
    score_away: int
    possession: str = "none"  # "home" | "away" | "none"
    pace: float = 100.0
    timeouts_home: int = 7
    timeouts_away: int = 7
    team_fouls_home: int = 0
    team_fouls_away: int = 0
    lineup_strength_home: float = 0.0
    lineup_strength_away: float = 0.0
    availability_home: float = 1.0
    availability_away: float = 1.0

    def time_remaining_frac(self) -> float:
        return max(0.0, min(1.0, self.seconds_remaining / NBA_TOTAL_GAME_SECONDS))


NBA_FEATURE_NAMES = (
    "prior_logit",
    "total_norm",
    "period_norm",
    "time_remaining_frac",
    "score_diff_scaled",
    "score_diff_leverage",
    "possession",
    "pace_norm",
    "timeouts_home_norm",
    "timeouts_away_norm",
    "team_fouls_diff_norm",
    "lineup_strength_diff_norm",
    "availability_diff",
)

NBA_SPEC = FeatureSpec(
    sport="nba",
    names=NBA_FEATURE_NAMES,
    lo=np.array([-3.0, 0.5, 0.0, 0.0, -5.0, -5.0, -1.0, 0.6, 0.0, 0.0, -2.0, -2.0, -1.0], dtype=F32),
    hi=np.array([3.0, 1.3, 1.75, 1.0, 5.0, 5.0, 1.0, 1.3, 1.0, 1.0, 2.0, 2.0, 1.0], dtype=F32),
)


def _possession_val(possession: str) -> float:
    return {"home": 1.0, "away": -1.0}.get(possession, 0.0)


def nba_vector(state: NBALiveState, out: np.ndarray | None = None) -> np.ndarray:
    if out is None:
        out = np.empty(NBA_SPEC.size, dtype=F32)
    prior = pregame_prior.nba_prior_logit(state.exp_margin)
    score_diff = state.score_home - state.score_away
    leverage = 1.0 - state.time_remaining_frac()
    out[0] = prior
    out[1] = state.exp_total / 220.0
    out[2] = state.period / 4.0
    out[3] = state.time_remaining_frac()
    out[4] = score_diff / 12.0
    out[5] = (score_diff * leverage) / 12.0
    out[6] = _possession_val(state.possession)
    out[7] = state.pace / 100.0
    out[8] = state.timeouts_home / 7.0
    out[9] = state.timeouts_away / 7.0
    out[10] = (state.team_fouls_home - state.team_fouls_away) / 5.0
    out[11] = (state.lineup_strength_home - state.lineup_strength_away) / 10.0
    out[12] = state.availability_home - state.availability_away
    return out
