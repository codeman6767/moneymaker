"""Venue-aware ``game_date_local`` resolution (ENTITY_MATCHING.md §4, task §8).

The venue-local calendar date -- not the UTC date -- is the doubleheader and
schedule key: a 7pm Pacific game is 03:00 UTC the next day and must stay on its
Pacific date. The resolution hierarchy is fixed and explicit:

1. the **actual event venue** timezone (a neutral / temporary / relocated /
   international venue takes priority over the home city);
2. a **reliable official-provider local game date** (the provider stated it);
3. the **canonical home venue** timezone;
4. the **UTC calendar date** as a last resort -- which writes ``DQ-TZ-001`` and
   caps the achievable confidence, so it is never treated as equivalent to a
   real venue-local match.

Conversions use ``zoneinfo`` (never the host's local timezone). An IANA name
that does not resolve is an error, not a silent UTC fallback: a wrong timezone
silently shifts a game to the wrong slate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import (
    LOCALDATE_ACTUAL_VENUE,
    LOCALDATE_HOME_VENUE,
    LOCALDATE_PROVIDER_LOCAL,
    LOCALDATE_UTC_FALLBACK,
    UTC_FALLBACK_CONFIDENCE_CAP,
)


class InvalidTimezoneError(ValueError):
    """A provided IANA timezone name did not resolve.

    Raised rather than silently using UTC, so the caller records a data-quality
    issue and refuses the date instead of shifting the game to the wrong slate.
    """


@dataclass(frozen=True)
class LocalDate:
    """A resolved venue-local date and the tier that produced it."""

    game_date_local: str
    tier: str
    tz_name: Optional[str]
    #: 1.0 except the UTC fallback, which caps downstream confidence.
    confidence_cap: float
    #: ``DQ-TZ-001`` when the UTC fallback was used, else ``None``.
    dq_code: Optional[str]


def _parse_utc(scheduled_start: str) -> datetime:
    text = scheduled_start.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # malformed timestamp -> caller treats as unusable
        raise InvalidTimezoneError(f"unparseable scheduled_start {scheduled_start!r}") from exc
    if parsed.tzinfo is None:
        # An offset-free instant is ambiguous; never assume it is UTC.
        raise InvalidTimezoneError(f"scheduled_start {scheduled_start!r} carries no offset")
    return parsed.astimezone(timezone.utc)


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise InvalidTimezoneError(f"unknown timezone {tz_name!r}") from exc


def _date_in_zone(moment_utc: datetime, tz_name: str) -> str:
    return moment_utc.astimezone(_zone(tz_name)).date().isoformat()


def resolve_local_date(
    *,
    scheduled_start: Optional[str],
    actual_venue_tz: Optional[str] = None,
    provider_local_date: Optional[str] = None,
    home_venue_tz: Optional[str] = None,
) -> LocalDate:
    """Resolve the venue-local calendar date by the fixed hierarchy.

    ``scheduled_start`` is an offset-bearing UTC ISO instant. A provided
    timezone that does not resolve raises :class:`InvalidTimezoneError`; the
    caller records ``DQ-TZ-001`` (or a stricter code) and refuses rather than
    guessing.
    """

    moment: Optional[datetime] = _parse_utc(scheduled_start) if scheduled_start else None

    # Tier 1 -- the actual event venue timezone. Priority over everything, so a
    # neutral/temporary/international/relocated venue lands on its own date.
    if actual_venue_tz:
        if moment is None:
            raise InvalidTimezoneError("actual venue timezone needs a scheduled_start")
        return LocalDate(
            game_date_local=_date_in_zone(moment, actual_venue_tz),
            tier=LOCALDATE_ACTUAL_VENUE,
            tz_name=actual_venue_tz,
            confidence_cap=1.0,
            dq_code=None,
        )

    # Tier 2 -- a reliable official-provider local game date (provider stated it).
    if provider_local_date:
        return LocalDate(
            game_date_local=provider_local_date,
            tier=LOCALDATE_PROVIDER_LOCAL,
            tz_name=None,
            confidence_cap=1.0,
            dq_code=None,
        )

    # Tier 3 -- the canonical home venue timezone.
    if home_venue_tz:
        if moment is None:
            raise InvalidTimezoneError("home venue timezone needs a scheduled_start")
        return LocalDate(
            game_date_local=_date_in_zone(moment, home_venue_tz),
            tier=LOCALDATE_HOME_VENUE,
            tz_name=home_venue_tz,
            confidence_cap=1.0,
            dq_code=None,
        )

    # Tier 4 -- UTC calendar date. Last resort: DQ-TZ-001 + capped confidence.
    if moment is None:
        raise InvalidTimezoneError("no scheduled_start and no local date available")
    return LocalDate(
        game_date_local=moment.date().isoformat(),
        tier=LOCALDATE_UTC_FALLBACK,
        tz_name="UTC",
        confidence_cap=UTC_FALLBACK_CONFIDENCE_CAP,
        dq_code="DQ-TZ-001",
    )
