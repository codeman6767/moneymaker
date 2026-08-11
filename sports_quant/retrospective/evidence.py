"""Which preserved tables may serve as Lane-R source evidence (f019).

The independent review of f018 found that `source_evidence_table` accepted any
string at all: a nonexistent table, a mutable current-state table, SQL-shaped
junk, or the provenance table citing itself. A future reader would have had no
way to tell a real pointer from a typo.

The allowlist below is the fix's code half. Its DB half is the f019 trigger
`trg_rip_evidence_table_allowed`, which carries the same names. They are checked
against each other by test, because two copies of a list that can drift is worse
than one copy that cannot be enforced.

What qualifies
--------------
Exactly the **append-only observation** tables that record facts about games,
participants, conditions or markets, plus ``raw_responses`` (immutable provider
payloads -- the anchor every other observation is derived from).

Deliberately excluded, each for a reason:

* **Mutable current-state and link tables.** A reconstruction may not derive a
  fact from something that can change under it after the corpus is sealed.
* **Canonical dimensions** (``teams``, ``players``, ``games``). Identity reaches
  Lane R through the static crosswalk, which carries an audit; a bare dimension
  row carries no availability argument at all.
* **Matcher and DQ plumbing** (``entity_match_decisions``,
  ``data_quality_issues``). These are conclusions the system reached, not
  observations of the world, and ``decided_at`` is matcher wall-clock.
* **The v18/v19 provenance tables themselves.** Provenance citing provenance is
  a loop, not evidence.
"""

from __future__ import annotations

from typing import Final

from .provenance import RetrospectiveProvenanceError

__all__ = [
    "SOURCE_EVIDENCE_TABLES",
    "evidence_id_column",
    "require_source_evidence_table",
]

#: Table -> its primary-key column, so the repository can check the cited row
#: actually exists. SQLite cannot resolve a table name held in a column, which is
#: why row existence is verified here rather than by a trigger; the f019 trigger
#: enforces the NAME, this enforces the ROW.
_EVIDENCE_TABLES: Final[dict[str, str]] = {
    "game_result_snapshots": "result_id",
    "game_schedule_snapshots": "schedule_id",
    "game_status_history": "status_id",
    "injury_snapshots": "injury_id",
    "lineup_players": "lineup_player_id",
    "lineup_snapshots": "lineup_id",
    "mlb_inning_lines": "line_id",
    "nba_game_results": "result_id",
    "nba_player_statistics": "stat_id",
    "nba_quarter_lines": "line_id",
    "nba_team_statistics": "stat_id",
    "play_snapshots": "play_id",
    "player_game_statistics": "stat_id",
    "probable_pitcher_snapshots": "probable_id",
    "raw_responses": "raw_response_id",
    "roster_snapshots": "roster_id",
    "sportsbook_price_snapshots": "snapshot_id",
    "team_game_statistics": "stat_id",
    "weather_snapshots": "weather_id",
}

#: The allowlist, frozen. Mirrored by the f019 trigger.
SOURCE_EVIDENCE_TABLES: Final[frozenset[str]] = frozenset(_EVIDENCE_TABLES)


def require_source_evidence_table(table: str) -> str:
    """Return the primary-key column of an allowed evidence table, or fail closed."""

    try:
        return _EVIDENCE_TABLES[table]
    except KeyError:
        raise RetrospectiveProvenanceError(
            f"{table!r} is not an allowed Lane-R source evidence table. Evidence must "
            "be an append-only observation (or a raw provider response); mutable "
            "current-state, canonical dimensions, matcher/DQ conclusions and the "
            f"provenance tables themselves are refused. Allowed: "
            f"{sorted(SOURCE_EVIDENCE_TABLES)}"
        ) from None


def evidence_id_column(table: str) -> str:
    """The primary-key column used to verify a cited evidence row exists."""

    return require_source_evidence_table(table)
