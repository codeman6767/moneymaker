"""Canonical identifier construction.

Two kinds of identifier, and the split is deliberate (see
``DATA_ARCHITECTURE.md`` §2.2):

* **Deterministic** IDs are derived from a natural key that genuinely never
  changes -- a league's identity, a franchise slot within a league, a season.
  Rebuilding the corpus yields identical IDs, which is what makes a corpus diff
  meaningful.
* **Surrogate** IDs (ULIDs) are used wherever the natural key can change.
  Players change names; games get postponed to a different date. Deriving an ID
  from a mutable key means the ID changes when reality changes, silently
  orphaning every foreign key that pointed at it.

ULIDs are lexicographically sortable by creation time, so index locality is
preserved without leaking a mutable fact into the identifier.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Final

# Crockford base32: no I, L, O, or U -- the alphabet is deliberately free of
# characters that are misread when an ID is copied out of a log by eye.
_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TIMESTAMP_CHARS: Final = 10  # 48 bits of milliseconds
_RANDOM_CHARS: Final = 16  # 80 bits of entropy
ULID_LENGTH: Final = _TIMESTAMP_CHARS + _RANDOM_CHARS

_RANDOM_BITS: Final = 80
_MAX_RANDOM: Final = (1 << _RANDOM_BITS) - 1

# Prefixes. Kept here so a prefix is never spelled as a literal at a call site.
LEAGUE_PREFIX: Final = "lg_"
SEASON_PREFIX: Final = "sn_"
TEAM_PREFIX: Final = "tm_"
PLAYER_PREFIX: Final = "pl_"
GAME_PREFIX: Final = "gm_"
TEAM_ALIAS_PREFIX: Final = "tal_"
PLAYER_ALIAS_PREFIX: Final = "pal_"
GAME_STATUS_PREFIX: Final = "gst_"

# Phase B: ingestion provenance and sportsbook prices.
INGESTION_RUN_PREFIX: Final = "run_"
RAW_RESPONSE_PREFIX: Final = "raw_"
SB_EVENT_PREFIX: Final = "sbe_"
SB_MARKET_PREFIX: Final = "sbm_"
SB_OUTCOME_PREFIX: Final = "sbo_"
SB_PRICE_SNAPSHOT_PREFIX: Final = "sbp_"

# Phase C: Kalshi public events, markets, order books, and trades.
KALSHI_EVENT_PREFIX: Final = "kev_"
KALSHI_MARKET_PREFIX: Final = "kmk_"
KALSHI_BOOK_PREFIX: Final = "kob_"
KALSHI_LEVEL_PREFIX: Final = "kol_"
KALSHI_TRADE_PREFIX: Final = "ktr_"

# Phase D (D1): provider infrastructure, venues, matching, data quality.
PROVIDER_TEAM_REF_PREFIX: Final = "ptr_"
PROVIDER_PLAYER_REF_PREFIX: Final = "ppr_"
PROVIDER_GAME_REF_PREFIX: Final = "pgr_"
VENUE_PREFIX: Final = "ven_"
VENUE_ALIAS_PREFIX: Final = "val_"
MATCH_DECISION_PREFIX: Final = "mtc_"
MATCH_CANDIDATE_PREFIX: Final = "mcn_"
DATA_QUALITY_PREFIX: Final = "dqi_"
PROVIDER_CAPABILITY_PREFIX: Final = "cap_"

# Phase D2: official MLB game/stat snapshots.
SCHEDULE_SNAPSHOT_PREFIX: Final = "gss_"
RESULT_SNAPSHOT_PREFIX: Final = "grs_"
INNING_LINE_PREFIX: Final = "mil_"
TEAM_GAME_STAT_PREFIX: Final = "tgs_"
PLAYER_GAME_STAT_PREFIX: Final = "pgs_"
ROSTER_SNAPSHOT_PREFIX: Final = "ros_"
PROBABLE_PITCHER_PREFIX: Final = "pps_"
LINEUP_SNAPSHOT_PREFIX: Final = "lns_"
LINEUP_PLAYER_PREFIX: Final = "lnp_"

# Phase D3: NBA-specific append-only observations (BALLDONTLIE / offline hoopR).
QUARTER_LINE_PREFIX: Final = "nql_"
INJURY_SNAPSHOT_PREFIX: Final = "inj_"
PLAY_SNAPSHOT_PREFIX: Final = "ply_"

# Phase D3 repair (d013): NBA-typed results + team/player statistics.
NBA_RESULT_PREFIX: Final = "nbr_"
NBA_TEAM_STAT_PREFIX: Final = "nts_"
NBA_PLAYER_STAT_PREFIX: Final = "nps_"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _encode(value: int, length: int) -> str:
    """Encode ``value`` as ``length`` Crockford base32 characters, big-endian."""

    if value < 0:
        raise ValueError("cannot encode a negative value")
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    if value:
        raise ValueError(f"value does not fit in {length} base32 characters")
    return "".join(reversed(chars))


class _MonotonicUlidFactory:
    """Generates ULIDs that strictly increase, even within one millisecond.

    Plain random ULIDs sharing a millisecond sort arbitrarily against each
    other. That is enough to make a rebuilt corpus order rows differently from
    the original, which breaks deterministic tie-breaking in as-of queries.
    Incrementing the random component within a millisecond removes the problem
    at negligible cost.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_random = 0

    def new(self, timestamp_ms: int) -> str:
        with self._lock:
            if timestamp_ms > self._last_ms:
                self._last_ms = timestamp_ms
                self._last_random = secrets.randbits(_RANDOM_BITS)
            else:
                # Same millisecond, or the wall clock stepped backwards. Either
                # way, keep issuing increasing ids rather than trusting the
                # clock -- ids must never go backwards.
                if self._last_random >= _MAX_RANDOM:
                    self._last_ms += 1
                    self._last_random = secrets.randbits(_RANDOM_BITS)
                else:
                    self._last_random += 1
            return _encode(self._last_ms, _TIMESTAMP_CHARS) + _encode(
                self._last_random, _RANDOM_CHARS
            )


_factory = _MonotonicUlidFactory()


def new_ulid() -> str:
    """A fresh, monotonically increasing ULID."""

    return _factory.new(time.time_ns() // 1_000_000)


def prefixed_id(prefix: str) -> str:
    """A fresh surrogate identifier with the given prefix."""

    return f"{prefix}{new_ulid()}"


def _slug(value: str) -> str:
    """Lowercase alphanumeric slug used inside deterministic identifiers."""

    slug = _SLUG_STRIP.sub("_", value.strip().lower()).strip("_")
    if not slug:
        raise ValueError(f"cannot build an identifier slug from {value!r}")
    return slug


# --------------------------------------------------------------------------- #
# Deterministic identifiers
# --------------------------------------------------------------------------- #
def league_id(code: str) -> str:
    """``'MLB'`` -> ``'lg_mlb'``."""

    return f"{LEAGUE_PREFIX}{_slug(code)}"


def season_id(league_code: str, year: int, phase: str) -> str:
    """``('MLB', 2026, 'regular')`` -> ``'sn_mlb_2026_regular'``.

    The phase is part of the identifier because a league runs a preseason, a
    regular season and a postseason within one year, and the ``seasons``
    uniqueness key covers all three.
    """

    return f"{SEASON_PREFIX}{_slug(league_code)}_{year}_{_slug(phase)}"


def team_id(league_code: str, abbreviation: str) -> str:
    """``('MLB', 'NYY')`` -> ``'tm_mlb_nyy'``."""

    return f"{TEAM_PREFIX}{_slug(league_code)}_{_slug(abbreviation)}"


# --------------------------------------------------------------------------- #
# Surrogate identifiers
# --------------------------------------------------------------------------- #
def new_player_id() -> str:
    return prefixed_id(PLAYER_PREFIX)


def new_game_id() -> str:
    return prefixed_id(GAME_PREFIX)


def new_team_alias_id() -> str:
    return prefixed_id(TEAM_ALIAS_PREFIX)


def new_player_alias_id() -> str:
    return prefixed_id(PLAYER_ALIAS_PREFIX)


def new_game_status_id() -> str:
    return prefixed_id(GAME_STATUS_PREFIX)


def new_ingestion_run_id() -> str:
    return prefixed_id(INGESTION_RUN_PREFIX)


def new_raw_response_id() -> str:
    return prefixed_id(RAW_RESPONSE_PREFIX)


def new_sb_event_id() -> str:
    return prefixed_id(SB_EVENT_PREFIX)


def new_sb_market_id() -> str:
    return prefixed_id(SB_MARKET_PREFIX)


def new_sb_outcome_id() -> str:
    return prefixed_id(SB_OUTCOME_PREFIX)


def new_sb_price_snapshot_id() -> str:
    return prefixed_id(SB_PRICE_SNAPSHOT_PREFIX)


def new_kalshi_event_id() -> str:
    return prefixed_id(KALSHI_EVENT_PREFIX)


def new_kalshi_market_id() -> str:
    return prefixed_id(KALSHI_MARKET_PREFIX)


def new_kalshi_book_id() -> str:
    return prefixed_id(KALSHI_BOOK_PREFIX)


def new_kalshi_level_id() -> str:
    return prefixed_id(KALSHI_LEVEL_PREFIX)


def new_kalshi_trade_id() -> str:
    return prefixed_id(KALSHI_TRADE_PREFIX)


def new_provider_team_ref_id() -> str:
    return prefixed_id(PROVIDER_TEAM_REF_PREFIX)


def new_provider_player_ref_id() -> str:
    return prefixed_id(PROVIDER_PLAYER_REF_PREFIX)


def new_provider_game_ref_id() -> str:
    return prefixed_id(PROVIDER_GAME_REF_PREFIX)


def new_venue_id() -> str:
    return prefixed_id(VENUE_PREFIX)


def new_venue_alias_id() -> str:
    return prefixed_id(VENUE_ALIAS_PREFIX)


def new_match_decision_id() -> str:
    return prefixed_id(MATCH_DECISION_PREFIX)


def new_match_candidate_id() -> str:
    return prefixed_id(MATCH_CANDIDATE_PREFIX)


def new_data_quality_id() -> str:
    return prefixed_id(DATA_QUALITY_PREFIX)


def new_provider_capability_id() -> str:
    return prefixed_id(PROVIDER_CAPABILITY_PREFIX)


# Phase D2 factories.
def new_schedule_snapshot_id() -> str:
    return prefixed_id(SCHEDULE_SNAPSHOT_PREFIX)


def new_result_snapshot_id() -> str:
    return prefixed_id(RESULT_SNAPSHOT_PREFIX)


def new_inning_line_id() -> str:
    return prefixed_id(INNING_LINE_PREFIX)


def new_team_game_stat_id() -> str:
    return prefixed_id(TEAM_GAME_STAT_PREFIX)


def new_player_game_stat_id() -> str:
    return prefixed_id(PLAYER_GAME_STAT_PREFIX)


def new_roster_snapshot_id() -> str:
    return prefixed_id(ROSTER_SNAPSHOT_PREFIX)


def new_probable_pitcher_id() -> str:
    return prefixed_id(PROBABLE_PITCHER_PREFIX)


def new_lineup_snapshot_id() -> str:
    return prefixed_id(LINEUP_SNAPSHOT_PREFIX)


def new_lineup_player_id() -> str:
    return prefixed_id(LINEUP_PLAYER_PREFIX)


# Phase D3 factories.
def new_quarter_line_id() -> str:
    return prefixed_id(QUARTER_LINE_PREFIX)


def new_injury_snapshot_id() -> str:
    return prefixed_id(INJURY_SNAPSHOT_PREFIX)


def new_play_snapshot_id() -> str:
    return prefixed_id(PLAY_SNAPSHOT_PREFIX)


# Phase D3 repair (d013) factories.
def new_nba_result_id() -> str:
    return prefixed_id(NBA_RESULT_PREFIX)


def new_nba_team_stat_id() -> str:
    return prefixed_id(NBA_TEAM_STAT_PREFIX)


def new_nba_player_stat_id() -> str:
    return prefixed_id(NBA_PLAYER_STAT_PREFIX)
