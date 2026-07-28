"""Fail-closed safe-join table registry for Phase E dataset construction (task §6).

Every table reachable by a future Phase E row builder is classified exactly once.
An *unknown* table fails closed (raises): future dataset code must declare each
join and cannot silently pull an unregistered — possibly mutable — table into a
feature row. The registry is a static, deterministic mapping; classification
carries a written justification and, where relevant, the ``observed_at`` /
stable-id columns (``asof_filtered``), the season key/window (``season_scoped``),
or the forbidden current-state columns (``immutable`` identity tables that also
hold a mutable denormalized column such as ``games.status``).

Classifications
---------------
* ``immutable`` -- set once at creation; safe to read directly (identity /
  structural dimensions). May still declare ``forbidden_columns`` for a
  denormalized mutable column that must instead be read as-of.
* ``season_scoped`` -- a dimension safe only inside a stated season key/window.
* ``asof_filtered`` -- append-only observation; read via the canonical
  ``observed_at <= cutoff`` + stable-id tie-break contract.
* ``evaluation_only`` -- market microstructure read only by the evaluation-only
  module (never a feature-facing identity/feature read).
* ``forbidden_current_state`` -- mutable current-state / link table whose current
  ``*_id`` link is NOT independently point-in-time-safe; identity must instead be
  reconstructed from the accepted-decision + DQ/review timeline.
* ``unsupported`` -- exists but is not a valid Phase E dataset join (run/audit
  plumbing); declaring it as a join fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

__all__ = [
    "TableClass",
    "TableEntry",
    "UnknownTableError",
    "ForbiddenJoinError",
    "ForbiddenColumnError",
    "classify",
    "require_asof",
    "assert_joinable",
    "assert_column_readable",
    "registered_tables",
    "TABLE_REGISTRY",
]


class TableClass(str, Enum):
    IMMUTABLE = "immutable"
    SEASON_SCOPED = "season_scoped"
    ASOF_FILTERED = "asof_filtered"
    EVALUATION_ONLY = "evaluation_only"
    FORBIDDEN_CURRENT_STATE = "forbidden_current_state"
    UNSUPPORTED = "unsupported"


class UnknownTableError(KeyError):
    """Raised when a table is not registered (fail-closed default)."""


class ForbiddenJoinError(RuntimeError):
    """Raised when a declared dataset join names a non-feature-safe table."""


class ForbiddenColumnError(RuntimeError):
    """Raised when a forbidden mutable current-state column is read directly."""


@dataclass(frozen=True)
class TableEntry:
    table: str
    classification: TableClass
    justification: str
    observed_at_column: Optional[str] = None   # asof_filtered
    id_column: Optional[str] = None             # asof_filtered stable tie-break
    season_key: Optional[str] = None            # season_scoped
    forbidden_columns: frozenset[str] = field(default_factory=frozenset)
    via_parent: Optional[str] = None            # child rows filtered by a parent


def _asof(table: str, id_column: str, justification: str, *,
          observed_at: str = "observed_at", via_parent: Optional[str] = None) -> TableEntry:
    return TableEntry(table, TableClass.ASOF_FILTERED, justification,
                      observed_at_column=observed_at, id_column=id_column, via_parent=via_parent)


def _immutable(table: str, justification: str, *,
               forbidden: frozenset[str] = frozenset()) -> TableEntry:
    return TableEntry(table, TableClass.IMMUTABLE, justification, forbidden_columns=forbidden)


def _forbidden(table: str, justification: str) -> TableEntry:
    return TableEntry(table, TableClass.FORBIDDEN_CURRENT_STATE, justification)


def _evaluation(table: str, justification: str) -> TableEntry:
    return TableEntry(table, TableClass.EVALUATION_ONLY, justification)


def _unsupported(table: str, justification: str) -> TableEntry:
    return TableEntry(table, TableClass.UNSUPPORTED, justification)


# Every table shipped at schema v16 is classified exactly once. Ordering is
# alphabetical and deterministic.
_ENTRIES: tuple[TableEntry, ...] = (
    _asof("data_quality_issues", "issue_id",
          "DQ timeline; active-at-cutoff via detected_at<=cutoff AND (resolved_at IS NULL OR "
          "resolved_at>cutoff). Never auto-resolved by a later run.", observed_at="detected_at"),
    _asof("entity_match_decisions", "match_id",
          "Append-only except review columns; as-of via decided_at<=cutoff. Review columns are "
          "revealed only when reviewed_at<=cutoff; needs_manual_review has no timeline and is not "
          "historical truth.", observed_at="decided_at"),
    _asof("game_result_snapshots", "result_id",
          "Official result observations; a result is visible only when observed_at<=cutoff (label, "
          "never a pregame feature)."),
    _asof("game_schedule_snapshots", "schedule_id", "Official schedule observations, as-of."),
    _asof("game_status_history", "status_id",
          "Append-only status log; the ONLY safe source of game status as-of (games.status is "
          "forbidden)."),
    _immutable("games",
               "Canonical game identity (teams, scheduled_start, game_date_local, season) set at "
               "creation. games.status is a denormalized mutable current-state column and must be "
               "read via game_status_history as-of.",
               forbidden=frozenset({"status", "updated_at"})),
    _unsupported("ingestion_runs", "Run bookkeeping; never a dataset join."),
    _asof("injury_snapshots", "injury_id",
          "Injury observations; filtered by observed_at (transaction time), NOT published_at "
          "(provider publication time)."),
    _forbidden("kalshi_events",
               "Current game_id link + mutable title are current-state; identity only via the "
               "accepted event decision timeline."),
    _forbidden("kalshi_markets",
               "Current game_id/yes_team_id/rules are current-state; orientation only via the "
               "fail-closed as-of readiness method."),
    _evaluation("kalshi_orderbook_levels",
                "Market microstructure (child of kalshi_orderbook_snapshots); evaluation-only, "
                "separate from identity readiness."),
    _evaluation("kalshi_orderbook_snapshots",
                "Market microstructure; evaluation-only, separate from identity readiness."),
    _evaluation("kalshi_public_trades",
                "Public trades; evaluation-only, separate from identity readiness."),
    _immutable("leagues", "Canonical league dimension; immutable identity."),
    _asof("lineup_players", "lineup_player_id",
          "Child of lineup_snapshots; reachable only through the parent snapshot's observed_at "
          "cutoff.", observed_at="", via_parent="lineup_snapshots"),
    _asof("lineup_snapshots", "lineup_id",
          "Lineup observations, as-of; a confirmed lineup is visible only once observed_at<=cutoff."),
    _unsupported("match_candidates", "Append-only decision audit detail; not a feature join."),
    _asof("mlb_inning_lines", "line_id", "In-game inning observations, as-of."),
    _asof("nba_game_results", "result_id", "NBA result observations; label only, as-of."),
    _asof("nba_player_statistics", "stat_id", "NBA player stat observations, as-of."),
    _asof("nba_quarter_lines", "line_id", "In-game quarter observations, as-of."),
    _asof("nba_team_statistics", "stat_id", "NBA team stat observations, as-of."),
    _asof("play_snapshots", "play_id", "Play-by-play observations, as-of."),
    _immutable("player_aliases",
               "Provider->canonical player alias mapping (resolver dimension). is_ambiguous is a "
               "resolver flag; aliases are not a direct feature join."),
    _asof("player_game_statistics", "stat_id", "Player stat observations, as-of."),
    _immutable("players", "Canonical player identity; immutable."),
    _asof("probable_pitcher_snapshots", "probable_id", "Probable-pitcher observations, as-of."),
    _unsupported("provider_capabilities", "Provider capability audit; not a dataset join."),
    _forbidden("provider_game_references",
               "Current canonical game_id link is mutable current-state; use the accepted decision "
               "timeline (decisions_for_source as_of), never the link alone."),
    _forbidden("provider_player_references",
               "Current canonical player_id link is mutable current-state; use the decision "
               "timeline."),
    _forbidden("provider_team_references",
               "Current canonical team_id link is mutable current-state; use the decision "
               "timeline."),
    _unsupported("raw_responses", "Immutable provider payloads; provenance, not a feature join."),
    _asof("roster_snapshots", "roster_id",
          "Roster observations, as-of; membership must additionally be constrained to the game's "
          "league season by the caller."),
    _unsupported("schema_versions", "Migration bookkeeping."),
    _immutable("seasons", "Canonical season dimension; immutable window."),
    _forbidden("sportsbook_events",
               "Current game_id/orientation are mutable current-state; use is_orientation_approved "
               "as-of + the accepted decision timeline (neutral swapped events stay excluded)."),
    _immutable("sportsbook_markets", "Immutable market structure (market_key)."),
    _immutable("sportsbook_outcomes", "Immutable outcome definition (role/point)."),
    _asof("sportsbook_price_snapshots", "snapshot_id",
          "Price observations, as-of (feature: price as of cutoff). The closing-line query over "
          "this table is isolated in the evaluation_only module."),
    TableEntry("team_aliases", TableClass.SEASON_SCOPED,
               "Provider->canonical team alias, safe only within its season window.",
               season_key="valid_from_season..valid_to_season"),
    _asof("team_game_statistics", "stat_id", "Team stat observations, as-of."),
    _immutable("teams", "Canonical team identity; immutable."),
    _immutable("venue_aliases", "Provider->canonical venue alias mapping; resolver dimension."),
    _immutable("venues", "Canonical venue identity (incl. timezone); immutable."),
    _asof("weather_snapshots", "weather_id",
          "Weather observations, as-of; a pregame FEATURE requires additionally "
          "weather_kind='current_forecast' AND pit_eligible=1."),
)

TABLE_REGISTRY: dict[str, TableEntry] = {e.table: e for e in _ENTRIES}


def registered_tables() -> tuple[str, ...]:
    """All registered table names, deterministically ordered."""

    return tuple(sorted(TABLE_REGISTRY))


def classify(table: str) -> TableEntry:
    """Return the registry entry for ``table`` or raise (fail-closed default)."""

    try:
        return TABLE_REGISTRY[table]
    except KeyError as exc:
        raise UnknownTableError(
            f"table {table!r} is not registered in the Phase E safe-join registry; "
            "unknown tables fail closed and must be classified before use") from exc


def require_asof(table: str) -> TableEntry:
    """Return the entry, asserting it is ``asof_filtered`` (else fail closed)."""

    entry = classify(table)
    if entry.classification is not TableClass.ASOF_FILTERED:
        raise ForbiddenJoinError(
            f"table {table!r} is classified {entry.classification.value}, not asof_filtered; "
            "an as-of observation read is not permitted")
    return entry


def assert_joinable(tables: set[str]) -> None:
    """Fail closed unless every declared join is a feature-safe classification.

    Feature-safe = immutable / season_scoped / asof_filtered. A
    forbidden_current_state, evaluation_only, unsupported, or *unknown* table
    raises, so future dataset code cannot silently join a mutable current-state or
    unregistered table.
    """

    safe = {TableClass.IMMUTABLE, TableClass.SEASON_SCOPED, TableClass.ASOF_FILTERED}
    for table in sorted(tables):
        entry = classify(table)  # unknown -> UnknownTableError
        if entry.classification not in safe:
            raise ForbiddenJoinError(
                f"table {table!r} is classified {entry.classification.value} and is not a "
                "feature-safe dataset join")


def assert_column_readable(table: str, column: str) -> None:
    """Fail closed when reading a forbidden mutable current-state column directly."""

    entry = classify(table)
    if column in entry.forbidden_columns:
        raise ForbiddenColumnError(
            f"column {table}.{column} is a mutable current-state field and must be read as-of "
            "(e.g. game status via game_status_history), never directly")
