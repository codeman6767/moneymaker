"""Typed row models for the Phase A tables.

Frozen dataclasses rather than raw ``sqlite3.Row`` mappings, so a caller gets a
checked attribute instead of a string key lookup that fails at runtime. Kept in
one module so repositories can reference each other's models without an import
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .schema import NO_PROVIDER, SEASON_UNBOUNDED_END, SEASON_UNBOUNDED_START


@dataclass(frozen=True)
class League:
    league_id: str
    code: str
    name: str
    sport: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Season:
    season_id: str
    league_id: str
    year: int
    label: str
    phase: str
    start_date: str
    end_date: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Team:
    team_id: str
    league_id: str
    canonical_name: str
    city: str
    nickname: str
    abbreviation: str
    first_season: Optional[int]
    last_season: Optional[int]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TeamAlias:
    alias_id: str
    team_id: str
    league_id: str
    alias: str
    normalized: str
    alias_type: str
    provider: str = NO_PROVIDER
    valid_from_season: int = SEASON_UNBOUNDED_START
    valid_to_season: int = SEASON_UNBOUNDED_END
    is_ambiguous: bool = False
    source: str = "seed"
    created_at: str = ""


@dataclass(frozen=True)
class Player:
    player_id: str
    league_id: str
    full_name: str
    first_name: Optional[str]
    last_name: Optional[str]
    suffix: Optional[str]
    birth_date: Optional[str]
    primary_position: Optional[str]
    debut_date: Optional[str]
    final_game_date: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlayerAlias:
    alias_id: str
    player_id: str
    league_id: str
    alias: str
    normalized: str
    suffix: str = ""
    alias_type: str = "full"
    provider: str = NO_PROVIDER
    is_ambiguous: bool = False
    source: str = "seed"
    created_at: str = ""


@dataclass(frozen=True)
class Game:
    game_id: str
    league_id: str
    season_id: str
    home_team_id: str
    away_team_id: str
    scheduled_start: str
    original_start: str
    game_date_local: str
    game_number: int
    doubleheader_type: Optional[str]
    venue: Optional[str]
    is_neutral_site: bool
    status: str
    official_provider: Optional[str]
    official_game_key: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GameStatusRecord:
    """One append-only observation of a game's status.

    ``observed_at`` is the point-in-time cutoff column: when *we* learned this.
    ``provider_timestamp`` is when the provider says it became true, and is
    nullable because many providers omit it.
    """

    status_id: str
    game_id: str
    status: str
    scheduled_start: str
    provider: str
    observed_at: str
    ingested_at: str
    content_hash: str
    detail: Optional[str] = None
    provider_timestamp: Optional[str] = None
    raw_response_id: Optional[str] = None
    raw_response_hash: Optional[str] = None
    created_at: str = ""


# --------------------------------------------------------------------------- #
# Phase B: ingestion provenance and sportsbook prices
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IngestionRun:
    """One invocation of an ingest command, from request to terminal status.

    ``records_*`` are five separate counters on purpose: "1000 received, 0
    inserted" and "0 received" are different incidents and must stay
    distinguishable after the fact.
    """

    run_id: str
    command: str
    provider: str
    operation: str
    args_json: str
    status: str
    requested_at: str
    started_at: str
    started_monotonic_ns: int
    tool_version: str
    created_at: str
    sport: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ns: Optional[int] = None
    requests_made: int = 0
    records_received: int = 0
    records_normalized: int = 0
    records_inserted: int = 0
    records_deduplicated: int = 0
    records_rejected: int = 0
    error_type: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class RawResponse:
    """A provider response preserved exactly as received, minus any credential.

    ``received_at`` is the ``observed_at`` every derived fact inherits, so all
    facts parsed from one response share one point-in-time cutoff.
    """

    raw_response_id: str
    run_id: str
    provider: str
    endpoint: str
    request_params_json: str
    http_status: int
    response_headers_json: str
    requested_at: str
    received_at: str
    elapsed_ns: int
    body: str
    body_bytes: int
    body_hash: str
    content_hash: str
    created_at: str
    http_method: str = "GET"
    content_type: Optional[str] = None


@dataclass(frozen=True)
class SportsbookEvent:
    sb_event_id: str
    provider: str
    provider_event_id: str
    sport_key: str
    commence_time: str
    home_team_raw: str
    away_team_raw: str
    raw_response_id: str
    first_observed_at: str
    last_observed_at: str
    created_at: str
    updated_at: str
    league_id: Optional[str] = None
    game_id: Optional[str] = None


@dataclass(frozen=True)
class SportsbookMarket:
    sb_market_id: str
    sb_event_id: str
    bookmaker_key: str
    market_key: str
    raw_response_id: str
    first_observed_at: str
    last_observed_at: str
    created_at: str
    updated_at: str
    bookmaker_title: Optional[str] = None
    bookmaker_last_update: Optional[str] = None
    market_last_update: Optional[str] = None


@dataclass(frozen=True)
class SportsbookOutcome:
    """The stable identity of a betting line, separate from its price.

    A changed price is never a new identity; the line (``point``) is part of
    the identity, because "Over 8.5" and "Over 9.5" settle differently.
    """

    sb_outcome_id: str
    sb_market_id: str
    outcome_name: str
    provider_outcome_name: str
    outcome_role: str
    point_key: str
    created_at: str
    point: Optional[float] = None


@dataclass(frozen=True)
class SportsbookPriceSnapshot:
    """One append-only observation of a price. ``price_american`` is exact."""

    snapshot_id: str
    sb_outcome_id: str
    price_american: int
    observed_at: str
    ingested_at: str
    raw_response_id: str
    raw_response_hash: str
    run_id: str
    content_hash: str
    created_at: str
    price_decimal: Optional[float] = None
    implied_probability: Optional[float] = None
    point: Optional[float] = None
    bookmaker_last_update: Optional[str] = None
    market_last_update: Optional[str] = None
    provider_timestamp: Optional[str] = None


# --------------------------------------------------------------------------- #
# Phase C: Kalshi public events, markets, order books, and trades
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KalshiEvent:
    """Public Kalshi event. ``event_ticker`` is the stable provider identity;
    ``game_id`` (canonical, Phase D) stays NULL in Phase C."""

    kalshi_event_id: str
    event_ticker: str
    raw_response_id: str
    first_observed_at: str
    last_observed_at: str
    created_at: str
    updated_at: str
    series_ticker: Optional[str] = None
    title: Optional[str] = None
    sub_title: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    mutually_exclusive: Optional[bool] = None
    game_id: Optional[str] = None


@dataclass(frozen=True)
class KalshiMarket:
    """Public Kalshi market. ``market_ticker`` is the stable provider identity."""

    kalshi_market_id: str
    market_ticker: str
    raw_response_id: str
    first_observed_at: str
    last_observed_at: str
    created_at: str
    updated_at: str
    event_ticker: Optional[str] = None
    kalshi_event_id: Optional[str] = None
    series_ticker: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    yes_sub_title: Optional[str] = None
    no_sub_title: Optional[str] = None
    status: Optional[str] = None
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    expiration_time: Optional[str] = None
    settlement_time: Optional[str] = None
    result: Optional[str] = None
    rules_primary: Optional[str] = None
    rules_secondary: Optional[str] = None
    rules_hash: Optional[str] = None
    game_id: Optional[str] = None


@dataclass(frozen=True)
class KalshiOrderbookLevel:
    """One ladder level of an order-book snapshot. ``level_index`` 0 = best."""

    level_id: str
    snapshot_id: str
    side: str
    price: int
    quantity: int
    level_index: int
    created_at: str


@dataclass(frozen=True)
class KalshiOrderbookSnapshot:
    """Append-only order-book observation. Derived asks are stored, never a wire
    ask: ``derived_yes_ask = 100 - best_no_bid`` and vice versa."""

    snapshot_id: str
    market_ticker: str
    observed_at: str
    ingested_at: str
    run_id: str
    raw_response_id: str
    raw_response_hash: str
    content_hash: str
    created_at: str
    yes_levels: int = 0
    no_levels: int = 0
    depth_levels: int = 0
    kalshi_market_id: Optional[str] = None
    best_yes_bid: Optional[int] = None
    best_no_bid: Optional[int] = None
    derived_yes_ask: Optional[int] = None
    derived_no_ask: Optional[int] = None
    provider_timestamp: Optional[str] = None


@dataclass(frozen=True)
class KalshiPublicTrade:
    """One anonymous public trade print. NOT an account fill (we have none)."""

    trade_id: str
    market_ticker: str
    count: int
    observed_at: str
    ingested_at: str
    run_id: str
    raw_response_id: str
    content_hash: str
    created_at: str
    provider_trade_id: Optional[str] = None
    kalshi_market_id: Optional[str] = None
    taker_side: Optional[str] = None
    yes_price: Optional[int] = None
    no_price: Optional[int] = None
    trade_time: Optional[str] = None
    provider_timestamp: Optional[str] = None
