"""Shared transition-aware append-only helper for Phase D2 official snapshots.

Every official MLB observation table (schedule / result / inning / team + player
stats / roster / probable / lineup) is append-only with transition-aware
deduplication: a new row is written only when its ``content_hash`` differs from
its *immediate temporal predecessor* for the same anchor. This mirrors the
Phase B ``sportsbook_price_snapshots`` rule (POINT_IN_TIME_DATA §4) and keeps the
A -> B -> A case (all three retained), out-of-order backfills (compared against
their own temporal neighbour), and exact replays (idempotent) all correct.

The logic is identical across nine tables, so it lives here once.
"""

from __future__ import annotations

import enum
import hashlib
import sqlite3
from typing import Any, Mapping

from streaming.event_envelope import canonical_json


class ObservationOutcome(str, enum.Enum):
    """Result of appending one observation.

    * ``INSERTED``   -- a new observation row was written (new information).
    * ``UNCHANGED``  -- identical to the immediate predecessor (or an exact
      replay); no row written.
    """

    INSERTED = "inserted"
    UNCHANGED = "unchanged"


def observation_content_hash(payload: Mapping[str, Any]) -> str:
    """A stable content hash over the normalized (provenance-free) fields.

    Excludes ids/observed_at/provenance so the same observed content collapses
    across re-polls and distinct content always differs.
    """

    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def append_transition(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    anchor_where: str,
    anchor_params: tuple[Any, ...],
    observed_at: str,
    content_hash: str,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
) -> ObservationOutcome:
    """Append one observation transition-aware; return the outcome.

    ``anchor_where`` is the SQL identifying the transition anchor (e.g.
    ``"game_ref_id = ?"``) and ``anchor_params`` its bind values. The table's
    ``UNIQUE (<anchor…>, observed_at, content_hash)`` constraint is the backstop
    for an exact replay.
    """

    if len(columns) != len(values):
        raise ValueError("columns/values length mismatch")
    predecessor = conn.execute(
        f"SELECT content_hash FROM {table} "
        f"WHERE {anchor_where} AND observed_at <= ? "
        f"ORDER BY observed_at DESC, {id_column} DESC LIMIT 1",
        (*anchor_params, observed_at),
    ).fetchone()
    if predecessor is not None and str(predecessor["content_hash"]) == content_hash:
        return ObservationOutcome.UNCHANGED

    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return ObservationOutcome.INSERTED if cursor.rowcount > 0 else ObservationOutcome.UNCHANGED
