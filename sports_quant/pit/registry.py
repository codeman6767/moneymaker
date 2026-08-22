"""Fail-closed safe-join table registry for Phase E dataset construction (task §6).

Every table reachable by a future Phase E row builder is classified exactly once.
An *unknown* table fails closed (raises): future dataset code must declare each
join and cannot silently pull an unregistered — possibly mutable — table into a
feature row. The registry is a static, deterministic mapping; classification
carries a written justification and, where relevant, the ``observed_at`` /
stable-id / ``content_hash`` columns (``asof_filtered``), the season key/window
(``season_scoped``), or an explicit column policy for a mixed identity/current-state
table (``allowed_columns`` allowlist and/or ``forbidden_columns`` denylist).

Classifications
---------------
* ``immutable`` -- set once at creation; safe to read directly (identity /
  structural dimensions). A MIXED table that also holds mutable current-state
  columns declares either an ``allowed_columns`` allowlist (only those columns are
  readable; ``SELECT *`` is prohibited) or a ``forbidden_columns`` denylist for
  the specific denormalized mutable columns (e.g. ``games.status`` /
  ``games.scheduled_start``).
* ``season_scoped`` -- a dimension safe only inside a stated season key/window.
* ``asof_filtered`` -- append-only observation; read via the canonical
  ``observed_at <= cutoff`` selection with a ``content_hash`` equality tie-break
  (identical content returns one row deterministically; conflicting equal-timestamp
  content fails closed).
* ``evaluation_only`` -- market microstructure read only by the evaluation-only
  module (never a feature-facing identity/feature read).
* ``forbidden_current_state`` -- mutable current-state / link table whose current
  ``*_id`` link is NOT independently point-in-time-safe; identity must instead be
  reconstructed from the accepted-decision + DQ/review timeline.
* ``unsupported`` -- exists but is not a valid Phase E dataset join (run/audit
  plumbing, or a resolver-only alias table); declaring it as a join fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
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
    "assert_selectable",
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


_FEATURE_SAFE = frozenset(
    {TableClass.IMMUTABLE, TableClass.SEASON_SCOPED, TableClass.ASOF_FILTERED}
)


class UnknownTableError(KeyError):
    """Raised when a table is not registered (fail-closed default)."""


class ForbiddenJoinError(RuntimeError):
    """Raised when a declared dataset join names a non-feature-safe table, or uses
    ``SELECT *`` against a column-restricted table."""


class ForbiddenColumnError(RuntimeError):
    """Raised when a forbidden mutable current-state column is read directly, or a
    column outside a structural allowlist is requested."""


@dataclass(frozen=True)
class TableEntry:
    table: str
    classification: TableClass
    justification: str
    observed_at_column: Optional[str] = None    # asof_filtered
    id_column: Optional[str] = None              # asof_filtered stable provenance id
    content_column: Optional[str] = None         # asof_filtered content-hash tie key
    season_key: Optional[str] = None             # season_scoped
    forbidden_columns: frozenset[str] = field(default_factory=frozenset)
    allowed_columns: Optional[frozenset[str]] = None  # explicit structural allowlist
    #: For an ``asof_filtered`` table, the ONLY columns an as-of ``Observation`` may
    #: surface as features. It is a strict subset of the columns that feed the
    #: table's ``content_hash`` (never provenance/audit/provider-time/id columns),
    #: so a returned feature object is fully determined by the content hash and
    #: therefore rebuild-stable. ``None`` => no policy yet => ``observation()`` fails
    #: closed on that table.
    feature_columns: Optional[frozenset[str]] = None
    via_parent: Optional[str] = None             # child rows filtered by a parent


def _asof(table: str, id_column: str, justification: str, *,
          observed_at: str = "observed_at", content_column: Optional[str] = "content_hash",
          via_parent: Optional[str] = None) -> TableEntry:
    return TableEntry(table, TableClass.ASOF_FILTERED, justification,
                      observed_at_column=observed_at, id_column=id_column,
                      content_column=content_column, via_parent=via_parent)


def _immutable(table: str, justification: str, *, forbidden: frozenset[str] = frozenset(),
               allowed: Optional[frozenset[str]] = None) -> TableEntry:
    return TableEntry(table, TableClass.IMMUTABLE, justification, forbidden_columns=forbidden,
                      allowed_columns=allowed)


def _forbidden(table: str, justification: str) -> TableEntry:
    return TableEntry(table, TableClass.FORBIDDEN_CURRENT_STATE, justification)


def _evaluation(table: str, justification: str) -> TableEntry:
    return TableEntry(table, TableClass.EVALUATION_ONLY, justification)


def _unsupported(table: str, justification: str) -> TableEntry:
    return TableEntry(table, TableClass.UNSUPPORTED, justification)


# Every table shipped at schema v18 is classified exactly once. Ordering is
# alphabetical and deterministic. The five f018 Lane-R provenance tables are
# `unsupported`: retrospective provenance is a different lane, and the registry
# failing closed on them is what keeps a Lane-L dataset builder from reaching it.
_ENTRIES: tuple[TableEntry, ...] = (
    _asof("data_quality_issues", "issue_id",
          "DQ timeline; active-at-cutoff via detected_at<=cutoff AND (resolved_at IS NULL OR "
          "resolved_at>cutoff). Read via SqliteDataQualityRepository.list_active_at, not "
          "latest_as_of; no content_hash column.", observed_at="detected_at", content_column=None),
    _unsupported("corpus_evidence_lane_acquisitions",
                 "Lane-R (f022) membership of an evidence lane. Provenance bookkeeping naming "
                 "which acquisitions compose a lane; carries no observation, no canonical game "
                 "id and no value that could be a predictor."),
    _unsupported("corpus_evidence_lane_bindings",
                 "Lane-R (f022) per-lane evidence digest and policy versions. Manifest-level "
                 "provenance describing a whole reconstruction lane, exactly like "
                 "reconstruction_corpus_versions; not a per-row fact."),
    _asof("entity_match_decisions", "match_id",
          "Append-only except review columns; as-of via decided_at<=cutoff. Read via "
          "decisions_for_source, not latest_as_of; no content_hash column. Review columns are "
          "gated on reviewed_at<=cutoff; needs_manual_review has no timeline.",
          observed_at="decided_at", content_column=None),
    _asof("game_result_snapshots", "result_id",
          "Official result observations; a result is visible only when observed_at<=cutoff (label, "
          "never a pregame feature)."),
    _asof("game_schedule_snapshots", "schedule_id", "Official schedule observations, as-of."),
    _asof("game_status_history", "status_id",
          "Append-only status log; the ONLY safe source of game status AND scheduled_start as-of "
          "(both are forbidden on games)."),
    _immutable("games",
               "Canonical game identity (game_id, league/season, home/away teams, original_start, "
               "game_date_local, game_number, is_neutral_site) set at creation. games.status and "
               "games.scheduled_start are denormalized MUTABLE current-state and must be read via "
               "game_status_history as-of; games.updated_at is mutable.",
               forbidden=frozenset({"status", "scheduled_start", "updated_at"})),
    _unsupported("identity_audit_findings",
                 "Lane-R (f018) identity-audit detail. Retrospective RESEARCH provenance, not a "
                 "strict-forward feature: it records what a corpus-scoped G5 audit observed, at "
                 "audit wall-clock, with no availability semantics of its own. Joining it into a "
                 "Lane-L dataset row would import a conclusion reached long after the cutoff."),
    _unsupported("historical_market_event_observations",
                 "Lane-R (f020) historical market EVENT observations. Retrospectively acquired "
                 "secondary-provider evidence that a market existed for an event at a provider "
                 "snapshot instant: E0 identity/availability evidence, never a predictor, and "
                 "never a price. Reaching it from a strict-forward dataset row would import a "
                 "snapshot fetched long after that row's cutoff, which is precisely the lane "
                 "crossing f018 exists to prevent. It also carries no canonical game id, so it "
                 "cannot be joined to a dataset row even by accident."),
    _unsupported("identity_audit_records",
                 "Lane-R (f018) identity-audit results, for the same reason as the findings table. "
                 "An accepted audit clears a namespace for RECONSTRUCTION use; it says nothing "
                 "about what was knowable at a forward decision time."),
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
          "cutoff.", observed_at="", content_column=None, via_parent="lineup_snapshots"),
    _asof("lineup_snapshots", "lineup_id",
          "Lineup observations, as-of; a confirmed lineup is visible only once observed_at<=cutoff."),
    _unsupported("match_candidates", "Append-only decision audit detail; not a feature join."),
    _asof("mlb_inning_lines", "line_id", "In-game inning observations, as-of."),
    _asof("nba_game_results", "result_id", "NBA result observations; label only, as-of."),
    _asof("nba_player_statistics", "stat_id", "NBA player stat observations, as-of."),
    _asof("nba_quarter_lines", "line_id", "In-game quarter observations, as-of."),
    _asof("nba_team_statistics", "stat_id", "NBA team stat observations, as-of."),
    _asof("play_snapshots", "play_id", "Play-by-play observations, as-of."),
    _unsupported("player_aliases",
                 "Provider->canonical player alias: a matching/RESOLVER input, not a predictor. "
                 "Late alias curation must not rewrite an earlier dataset row; canonical identity "
                 "is available only through cutoff-filtered match decisions. Matching code outside "
                 "sports_quant.pit still uses it directly."),
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
    _unsupported("provider_player_identity_snapshots",
                 "Provider-written player name observations (e017): a matching/RESOLVER input, not "
                 "a predictor. Append-only and as-of queryable, but a player's name carries no "
                 "predictive signal and joining on it would smuggle canonical identity into "
                 "features without going through cutoff-filtered match decisions. Matching code "
                 "outside sports_quant.pit reads it directly, bounded by its own as_of."),
    _unsupported("provider_team_identity_snapshots",
                 "Provider-written team name observations (e017): a matching/RESOLVER input, not a "
                 "predictor, for the same reason as the player table above."),
    _unsupported("raw_responses", "Immutable provider payloads; provenance, not a feature join."),
    _unsupported("reconstructed_input_provenance",
                 "Lane-R (f018) input certifications. Deliberately unreachable from the strict "
                 "AsOfReader path: this table is the OTHER lane, and the entire point of keeping "
                 "the two apart is that a reconstructed-research eligibility verdict must never be "
                 "mistaken for a transaction-time-exact one. It stores no feature values."),
    _unsupported("reconstruction_corpus_target_runs",
                 "Lane-R (f023) target-population derivation provenance: which acquisition runs "
                 "a corpus's membership was derived from. Corpus-level provenance, not a "
                 "per-row fact and not a predictor."),
    _unsupported("reconstruction_corpus_target_seals",
                 "Lane-R (f023) target-population finalization. Records the frozen policy "
                 "versions, the precommitted acquisition manifest binding and the member count; "
                 "corpus-level provenance, never a feature."),
    _unsupported("reconstruction_corpus_targets",
                 "Lane-R (f023) corpus target membership: WHICH games a reconstruction corpus is "
                 "about. Retrospective by construction -- it is derived from the complete "
                 "official listing acquisition, so it is knowable only after the fact and must "
                 "never reach the strict AsOfReader path as a predictor."),
    _unsupported("reconstruction_corpus_versions",
                 "Lane-R (f018) corpus identity. Manifest-level provenance describing a whole "
                 "reconstruction; not a per-row fact and not a predictor."),
    _asof("roster_snapshots", "roster_id",
          "Roster observations, as-of; membership must additionally be constrained to the game's "
          "league season by the caller."),
    _unsupported("schema_versions", "Migration bookkeeping."),
    _immutable("seasons", "Canonical season dimension; immutable window (label/dates set once)."),
    _forbidden("sportsbook_events",
               "Current game_id/orientation are mutable current-state; use is_orientation_approved "
               "as-of + the accepted decision timeline (neutral swapped events stay excluded)."),
    _immutable("sportsbook_markets",
               "MIXED structural identity + current metadata. Only the structural columns "
               "(sb_market_id, sb_event_id, bookmaker_key, market_key) are feature-readable; the "
               "mutable bookmaker_title, bookmaker_last_update, market_last_update, current "
               "raw_response_id provenance, first/last_observed_at, and updated_at are current "
               "metadata, NOT a full historical snapshot. Prices are read only via the append-only "
               "sportsbook_price_snapshots table. SELECT * is prohibited.",
               allowed=frozenset({"sb_market_id", "sb_event_id", "bookmaker_key", "market_key"})),
    _immutable("sportsbook_outcomes",
               "Immutable outcome definition (role/point); structural.",
               allowed=frozenset({"sb_outcome_id", "sb_market_id", "outcome_role", "point",
                                  "point_key"})),
    _asof("sportsbook_price_snapshots", "snapshot_id",
          "Price observations, as-of (feature: price as of cutoff). The closing-line query over "
          "this table is isolated in the evaluation_only module."),
    # f022 Stage-A acquisition provenance. Every one of these is ACQUISITION
    # bookkeeping -- what we planned to request, what we requested, and what came
    # back. None of them is an observation about a game, none carries a feature
    # value, and none may ever become a Lane-L feature source. Stage A is
    # identity-free by construction, so none carries a provider->canonical link
    # either. `stage_a_plan_targets` does name canonical games, but only as a
    # DECLARED POPULATION -- "these are the games this plan is about" -- which is
    # a statement about the plan, not a fact knowable at any decision time.
    _unsupported("stage_a_acquisitions",
                 "Lane-R (f022) one execution of a Stage-A plan. Run bookkeeping."),
    _unsupported("stage_a_plan_targets",
                 "Lane-R (f022) declared target population and target->bucket mapping. Names "
                 "canonical games as a plan-scope declaration only; it is not an observation "
                 "and holds nothing that was knowable at a forward decision time."),
    _unsupported("stage_a_planned_buckets",
                 "Lane-R (f022) the closed set of authorized request buckets. A request bucket "
                 "is what WE asked for, never a provider observation."),
    _unsupported("stage_a_plans",
                 "Lane-R (f022) Stage-A plan identity and its manifest binding. Manifest-level "
                 "provenance; not a per-row fact and not a predictor."),
    _unsupported("stage_a_probe_registrations",
                 "Lane-R (f022) capability-probe reuse eligibility. Grants no identity semantics "
                 "and holds no observation."),
    _unsupported("stage_a_request_attempts",
                 "Lane-R (f022) per-attempt request outcome ledger. Records whether a request "
                 "succeeded or failed -- acquisition bookkeeping, never evidence about a game."),
    _unsupported("static_crosswalk_provenance",
                 "Lane-R (f018) provider->canonical static crosswalk. Same reasoning as "
                 "team_aliases and the e017 identity tables: a RESOLVER input, never a predictor. "
                 "It is retrospectively usable inside its own corpus under the reviewed "
                 "timeless-identity rule, which is a statement about Lane R and confers nothing on "
                 "a forward dataset row."),
    _unsupported("team_aliases",
                 "Provider->canonical team alias: a matching/RESOLVER input, not a predictor. Its "
                 "season window does NOT make it feature-safe; canonical identity comes only via "
                 "cutoff-filtered match decisions. Matching code still uses it directly."),
    _asof("team_game_statistics", "stat_id", "Team stat observations, as-of."),
    _immutable("teams", "Canonical team identity; immutable."),
    _unsupported("venue_aliases",
                 "Provider->canonical venue alias: a matching/RESOLVER input, not a predictor; not "
                 "a feature join. Matching code still uses it directly."),
    _immutable("venues", "Canonical venue identity (incl. timezone); immutable."),
    _asof("weather_snapshots", "weather_id",
          "Weather observations, as-of; a pregame FEATURE requires additionally "
          "weather_kind='current_forecast' AND pit_eligible=1."),
)

# The feature-safe column allowlist for each as-of observation table. Each set is
# a STRICT SUBSET of the columns hashed into that table's content_hash (the append
# repository's ``content`` dict), with provider ids, provider timestamps,
# raw-response / run / ingestion / created provenance, and the surrogate id
# excluded. Anchor/identity columns the caller already supplies are omitted too.
# A table absent here has no policy and is not readable via ``observation()``
# (fail-closed). Kept in sync with the append content dicts so red-flag-6 holds:
# a returned feature object is fully determined by content_hash.
_FEATURE_COLUMNS: dict[str, frozenset[str]] = {
    "game_status_history": frozenset({"status", "scheduled_start", "detail"}),
    "game_schedule_snapshots": frozenset({
        "season", "game_type", "game_date_local", "scheduled_start", "home_team_id",
        "away_team_id", "venue_id"}),
    "game_result_snapshots": frozenset({
        "home_runs", "away_runs", "home_hits", "away_hits", "home_errors", "away_errors",
        "innings_played", "winning_side", "mapped_status", "result_detail"}),
    "mlb_inning_lines": frozenset({"inning", "side", "runs", "hits", "errors"}),
    "team_game_statistics": frozenset({"home_away", "runs", "hits", "errors", "at_bats", "extra"}),
    "player_game_statistics": frozenset({
        "role", "is_starter", "batting_order", "position", "batting_stats", "pitching_stats",
        "extra"}),
    "roster_snapshots": frozenset({"roster_date", "roster_status", "jersey_number", "position"}),
    "probable_pitcher_snapshots": frozenset({"side", "status"}),
    "lineup_snapshots": frozenset({"home_away", "is_confirmed"}),
    "injury_snapshots": frozenset({
        "status", "description", "reason", "return_date", "return_estimate"}),
    "nba_quarter_lines": frozenset({"period", "side", "points"}),
    "play_snapshots": frozenset({
        "period", "play_sequence", "clock", "event_type", "description", "is_substitution",
        "extra"}),
    "nba_game_results": frozenset({
        "home_points", "away_points", "period", "winning_side", "mapped_status", "result_detail"}),
    "nba_team_statistics": frozenset({"home_away", "points", "stats"}),
    "nba_player_statistics": frozenset({
        "stat_group", "position", "is_starter", "points", "stats"}),
    "weather_snapshots": frozenset({
        "weather_kind", "applicability", "forecast_mode", "valid_time", "forecast_target_time",
        "model_reference_time", "lead_time_seconds", "pit_eligible", "roof_type_at_decision",
        "temperature_c", "apparent_temperature_c", "dew_point_c", "relative_humidity_pct",
        "wind_speed_ms", "wind_gust_ms", "wind_direction_deg", "precip_probability_pct",
        "precip_amount_mm", "weather_code", "condition_text", "extra"}),
}

TABLE_REGISTRY: dict[str, TableEntry] = {
    e.table: (replace(e, feature_columns=_FEATURE_COLUMNS[e.table])
              if e.table in _FEATURE_COLUMNS else e)
    for e in _ENTRIES
}


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

    for table in sorted(tables):
        entry = classify(table)  # unknown -> UnknownTableError
        if entry.classification not in _FEATURE_SAFE:
            raise ForbiddenJoinError(
                f"table {table!r} is classified {entry.classification.value} and is not a "
                "feature-safe dataset join")


def assert_column_readable(table: str, column: str) -> None:
    """Fail closed unless ``table.column`` is a feature-readable column.

    A non-feature-safe table (forbidden/evaluation/unsupported/unknown) raises for
    ANY column. For a feature-safe table: when an ``allowed_columns`` allowlist is
    declared, only those columns are readable; otherwise a ``forbidden_columns``
    denylist blocks the specific mutable current-state columns.
    """

    entry = classify(table)
    if entry.classification not in _FEATURE_SAFE:
        raise ForbiddenColumnError(
            f"table {table!r} is {entry.classification.value}; no column is directly "
            "feature-readable (reconstruct identity via the decision timeline)")
    if entry.allowed_columns is not None:
        if column not in entry.allowed_columns:
            raise ForbiddenColumnError(
                f"column {table}.{column} is not in the structural allowlist "
                f"{sorted(entry.allowed_columns)}; it is current metadata, not a feature")
        return
    if column in entry.forbidden_columns:
        raise ForbiddenColumnError(
            f"column {table}.{column} is a mutable current-state field and must be read as-of "
            "(e.g. game status/scheduled_start via game_status_history), never directly")


def assert_selectable(table: str, columns: Sequence[str]) -> None:
    """Validate a feature-join column selection (fail-closed).

    The table must be feature-safe; ``SELECT *`` is prohibited for any table with a
    column policy (allowlist or denylist), so E2 cannot bypass column checks with a
    star; every named column must be feature-readable.
    """

    entry = classify(table)  # unknown -> UnknownTableError
    if entry.classification not in _FEATURE_SAFE:
        raise ForbiddenJoinError(
            f"table {table!r} is classified {entry.classification.value} and is not a "
            "feature-safe dataset join")
    cols = list(columns)
    if "*" in cols:
        if entry.allowed_columns is not None or entry.forbidden_columns:
            raise ForbiddenJoinError(
                f"SELECT * is prohibited for column-restricted table {table!r}; "
                "enumerate the exact structural columns")
        return
    for column in cols:
        assert_column_readable(table, column)
