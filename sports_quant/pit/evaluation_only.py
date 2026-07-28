"""Evaluation-only closing lines -- structurally isolated from features (task §8).

A closing line (the last market price at or before game start) is a legitimate
*evaluation* reference (e.g. closing-line value) but is a leakage vector if it
ever enters a pregame feature row. It is therefore quarantined here, behind an
explicitly named function, and NOTHING that builds features may import this
module (an import-boundary test in ``pit/tests`` enforces that). This module
computes no CLV and evaluates no model -- it only isolates the read.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from ..db.repositories.sportsbook import SportsbookPriceSnapshot, SqliteSportsbookRepository
from .models import Cutoff

__all__ = ["closing_line_for_evaluation"]


def closing_line_for_evaluation(
    conn: sqlite3.Connection, *, sb_outcome_id: str, game_start: Cutoff,
) -> Optional[SportsbookPriceSnapshot]:
    """The closing line for an outcome: the last price snapshot OBSERVED at or
    before ``game_start`` (the documented evaluation contract), with a
    deterministic ``snapshot_id`` tie-break for equal ``observed_at``.

    This is EVALUATION-ONLY. It is deliberately unavailable through
    ``pit.asof`` / ``AsOfReader`` and must never be placed in a feature
    dictionary or predictor row. Price reversions before the game are preserved
    because the underlying series is append-only; this returns the final
    pre-start observation, whatever the path there was.
    """

    # `price_as_of` applies exactly the canonical `observed_at <= cutoff` +
    # `snapshot_id DESC` selection, so the closing line reuses the one as-of
    # contract rather than a second bespoke query.
    return SqliteSportsbookRepository(conn).price_as_of(sb_outcome_id, game_start.iso)
