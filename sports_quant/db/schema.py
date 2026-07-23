"""Storage conventions: timestamp format, enumerations, table registry.

SQLite has no native date, boolean, or enum type, so the conventions have to
live somewhere explicit. This module is that place, and it is the single source
for them -- a second opinion about the timestamp format would silently break
lexicographic ordering, and with it every point-in-time query.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final, Optional

# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
# ISO-8601 UTC with an explicit trailing 'Z'. Chosen so that lexicographic
# TEXT ordering equals chronological ordering -- which is what lets an as-of
# query be a plain `observed_at <= :cutoff` comparison with an index behind it.
TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"
DATE_FORMAT: Final = "%Y-%m-%d"

# The shape every timestamp CHECK constraint in the migrations tests against.
TIMESTAMP_LIKE_PATTERN: Final = "____-__-__T__:__:__%Z"


def utc_now() -> datetime:
    """Current UTC time. The one wall-clock entry point for the db package."""

    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Render a datetime in the storage format, normalizing to UTC.

    A naive datetime is rejected rather than assumed to be UTC: guessing a
    timezone is how an hours-wide error enters a corpus silently.
    """

    if moment.tzinfo is None:
        raise ValueError(
            "refusing to store a naive datetime; attach a timezone "
            "(timestamps are stored as UTC)"
        )
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def from_iso(text: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime."""

    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def utc_now_iso() -> str:
    """Current UTC time in the storage format."""

    return to_iso(utc_now())


def to_date(moment: datetime) -> str:
    """Render the date portion in the storage format."""

    return moment.strftime(DATE_FORMAT)


# --------------------------------------------------------------------------- #
# Enumerations (mirrored by CHECK constraints in the migrations)
# --------------------------------------------------------------------------- #
LEAGUE_CODES: Final[tuple[str, ...]] = ("MLB", "NBA")
SPORTS: Final[tuple[str, ...]] = ("baseball", "basketball")
SEASON_PHASES: Final[tuple[str, ...]] = ("preseason", "regular", "postseason")

GAME_STATUSES: Final[tuple[str, ...]] = (
    "scheduled",
    "pregame",
    "in_progress",
    "final",
    "postponed",
    "suspended",
    "cancelled",
    "rescheduled",
    "delayed",
)

TEAM_ALIAS_TYPES: Final[tuple[str, ...]] = (
    "abbreviation",
    "city",
    "nickname",
    "full",
    "historical",
    "provider",
)

PLAYER_ALIAS_TYPES: Final[tuple[str, ...]] = (
    "full",
    "short",
    "nickname",
    "accent_stripped",
    "suffix_variant",
    "provider",
)

ALIAS_SOURCES: Final[tuple[str, ...]] = ("seed", "manual", "provider_observed")

DOUBLEHEADER_TYPES: Final[tuple[str, ...]] = ("traditional", "split")

# --------------------------------------------------------------------------- #
# Ingestion (Phase B)
# --------------------------------------------------------------------------- #
#: Lifecycle of one ingestion run. ``partially_succeeded`` is a real outcome,
#: not a euphemism: a sweep that stored eight events and refused two malformed
#: ones neither succeeded nor failed, and flattening it into either loses the
#: only signal that something needs looking at.
INGESTION_RUN_STATUSES: Final[tuple[str, ...]] = (
    "started",
    "succeeded",
    "partially_succeeded",
    "failed",
)

#: The only HTTP verb the corpus can record. Mirrors the CHECK constraint on
#: ``raw_responses.http_method`` and the transport policy.
ALLOWED_HTTP_METHOD: Final = "GET"

#: Provider name recorded on every Odds API row.
THE_ODDS_API_PROVIDER: Final = "the_odds_api"

#: Sportsbook market keys supported in Phase B. Mirrors the CHECK constraint on
#: ``sportsbook_markets.market_key``.
SUPPORTED_MARKET_KEYS: Final[tuple[str, ...]] = ("h2h", "spreads", "totals")

#: Roles an outcome can play. ``unknown`` records an outcome whose role could
#: not be determined rather than dropping it.
OUTCOME_ROLES: Final[tuple[str, ...]] = ("home", "away", "over", "under", "draw", "unknown")

#: Provider sport keys to league codes. A static enum map, not a name match:
#: no fuzzy matching happens anywhere in Phase B.
SPORT_KEY_TO_LEAGUE_CODE: Final[dict[str, str]] = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
}

#: CLI sport arguments to provider sport keys.
SPORT_ARG_TO_SPORT_KEY: Final[dict[str, str]] = {
    "mlb": "baseball_mlb",
    "nba": "basketball_nba",
}

# --------------------------------------------------------------------------- #
# Kalshi public data (Phase C)
# --------------------------------------------------------------------------- #
#: Provider name recorded on every Kalshi public-data row. ``_public`` makes it
#: explicit in the corpus that this is the unauthenticated public surface.
KALSHI_PUBLIC_PROVIDER: Final = "kalshi_public"

#: Order-book / trade sides.
KALSHI_SIDES: Final[tuple[str, ...]] = ("yes", "no")

#: Kalshi binary-market prices are integer cents in [1, 99]; a Yes and its No
#: complement sum to 100 (mirrors ``providers.kalshi.PRICE_COMPLEMENT``).
KALSHI_PRICE_MIN: Final = 1
KALSHI_PRICE_MAX: Final = 99
KALSHI_PRICE_COMPLEMENT: Final = 100

# Sentinel meaning "not scoped to a provider". A NOT NULL sentinel rather than
# NULL because SQLite treats two NULLs as distinct inside a UNIQUE constraint,
# which would let identical seed rows insert twice on every re-run.
NO_PROVIDER: Final = ""

# Sentinels bounding an alias's season validity window.
SEASON_UNBOUNDED_START: Final = 0
SEASON_UNBOUNDED_END: Final = 9999


# --------------------------------------------------------------------------- #
# Table registry
# --------------------------------------------------------------------------- #
SCHEMA_VERSION_TABLE: Final = "schema_versions"

#: Every table created by Phase A migrations, in dependency order.
PHASE_A_TABLES: Final[tuple[str, ...]] = (
    "leagues",
    "seasons",
    "teams",
    "team_aliases",
    "players",
    "player_aliases",
    "games",
    "game_status_history",
)

#: Every table created by Phase B migrations, in dependency order.
PHASE_B_TABLES: Final[tuple[str, ...]] = (
    "ingestion_runs",
    "raw_responses",
    "sportsbook_events",
    "sportsbook_markets",
    "sportsbook_outcomes",
    "sportsbook_price_snapshots",
)

#: Every table created by Phase C migrations, in dependency order.
PHASE_C_TABLES: Final[tuple[str, ...]] = (
    "kalshi_events",
    "kalshi_markets",
    "kalshi_orderbook_snapshots",
    "kalshi_orderbook_levels",
    "kalshi_public_trades",
)

# --------------------------------------------------------------------------- #
# Phase D (D1): provider infrastructure. Later D-stages add more tables.
# --------------------------------------------------------------------------- #
#: Tables created by the D1 migration d009_provider_infra, in dependency order.
PHASE_D1_TABLES: Final[tuple[str, ...]] = (
    "provider_team_references",
    "provider_player_references",
    "provider_game_references",
    "venues",
    "venue_aliases",
    "entity_match_decisions",
    "match_candidates",
    "data_quality_issues",
    "provider_capabilities",
)

#: Approved venue roof-type vocabulary (mirrors the CHECK in d009).
VENUE_ROOF_TYPES: Final[tuple[str, ...]] = ("open", "retractable", "dome", "fixed", "indoor")

#: Entity kinds a match decision can resolve.
MATCH_ENTITY_TYPES: Final[tuple[str, ...]] = (
    "team",
    "player",
    "game",
    "venue",
    "sportsbook_event",
    "kalshi_event",
    "kalshi_market",
)

#: Match-decision outcomes (mirrors ENTITY_MATCHING.md §7 + the CHECK in d009).
MATCH_OUTCOMES: Final[tuple[str, ...]] = (
    "accepted",
    "rejected",
    "ambiguous",
    "no_candidate",
    "manual_override",
)

#: Data-quality severities (reuses the backtest vocabulary).
DATA_QUALITY_SEVERITIES: Final[tuple[str, ...]] = ("blocking", "issue", "note")

#: Provider-capability states (mirrors providers.capabilities.CapabilityState).
CAPABILITY_STATES: Final[tuple[str, ...]] = (
    "supported",
    "unsupported",
    "paid_tier_required",
    "best_effort",
    "unavailable",
    "unknown_until_audited",
    "provider_history_limited",
)

#: Tables that are immutable once written. UPDATE and DELETE are blocked by
#: BEFORE triggers, not by convention.
#:
#: ``ingestion_runs`` is deliberately absent: a run is opened as ``started``
#: and closed with its counters, which is a mutation of the same row. What a
#: run produced -- its raw responses and snapshots -- is immutable. Kalshi
#: events and markets are mutable current-state (like sportsbook_events); their
#: order-book snapshots, ladder levels, and public trades are append-only.
APPEND_ONLY_TABLES: Final[tuple[str, ...]] = (
    "game_status_history",
    "raw_responses",
    "sportsbook_price_snapshots",
    "kalshi_orderbook_snapshots",
    "kalshi_orderbook_levels",
    "kalshi_public_trades",
    # Phase D1: candidate rows and capability snapshots are immutable audit
    # records. `entity_match_decisions` is append-only *except* its review
    # columns (a partial trigger in d009), so it is deliberately absent here.
    # `data_quality_issues` is mutable (a `resolved_at`/resolution can be set).
    "match_candidates",
    "provider_capabilities",
)


def is_valid_status(status: str) -> bool:
    return status in GAME_STATUSES


def season_label(league_code: str, year: int, phase: str) -> str:
    """Human label for a season.

    Baseball seasons sit inside one calendar year; basketball seasons straddle
    two, and are conventionally written with both.
    """

    base = f"{year - 1}-{str(year)[2:]}" if league_code == "NBA" else str(year)
    return base if phase == "regular" else f"{base} {phase}"


def normalize_optional(value: Optional[str]) -> Optional[str]:
    """Collapse an empty-or-whitespace string to None."""

    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
