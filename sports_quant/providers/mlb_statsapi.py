"""MLB StatsAPI client (read-only, GET-only, unauthenticated).

D1 provides the **venue** surface (to seed `venues`/`venue_aliases`) plus the
read-only GET methods a ``provider-audit`` exercises: teams, schedule, team
roster, and a single person lookup. The full game/box surface is added in D2. No
key is used; the base URL is pinned in `sports_quant.config` and enforced by the
transport policy.

Undocumented API, no SLA, no explicit correction timestamps -- see
`PHASE_D_PROVIDER_DECISIONS.md` §2.1. Nothing here runs at import time.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import DEFAULT_MLB_STATS_API_BASE_URL
from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_MLB_STATSAPI

_HOST = "statsapi.mlb.com"


def _validate_positive_id(value: object, *, label: str) -> int:
    """Coerce a StatsAPI id to a positive int, rejecting empty/invalid values."""

    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer, not a bool")
    if value is None:
        raise ValueError(f"{label} is required and must be a positive integer")
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isdigit():
            raise ValueError(f"{label} must be a positive integer (got {value!r})")
        parsed = int(text)
    elif isinstance(value, int):
        parsed = value
    else:
        raise ValueError(f"{label} must be a positive integer (got {type(value).__name__})")
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer (got {parsed})")
    return parsed


def _first_id(data: object, list_key: str) -> Optional[int]:
    """Extract the first valid positive id from ``data[list_key][*]['id']``."""

    if not isinstance(data, dict):
        return None
    rows = data.get(list_key)
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and "id" in row:
            try:
                return _validate_positive_id(row["id"], label="id")
            except ValueError:
                continue
    return None


def first_person_id_from_roster(data: object) -> Optional[int]:
    """Extract the first valid person id from a StatsAPI ``/roster`` response.

    Roster rows nest the player under ``person.id``; returns ``None`` for an
    empty or unexpected payload rather than fabricating an id.
    """

    if not isinstance(data, dict):
        return None
    roster = data.get("roster")
    if not isinstance(roster, list):
        return None
    for row in roster:
        if isinstance(row, dict):
            person = row.get("person")
            if isinstance(person, dict) and "id" in person:
                try:
                    return _validate_positive_id(person["id"], label="person_id")
                except ValueError:
                    continue
    return None


class MlbStatsApiClient(BaseProviderClient):
    """Async, read-only adapter for the MLB StatsAPI public surface."""

    provider_name = PROVIDER_MLB_STATSAPI

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_MLB_STATS_API_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            policy=ReadOnlyHTTPPolicy.for_mlb_statsapi(_HOST),
            client=client,
            **kwargs,
        )

    async def fetch_venues(self) -> ProviderResponse:
        """GET /venues -- venue directory with location/field info to seed venues.

        Returns the parsed payload plus the sanitized raw exchange; the caller
        preserves the raw response before normalizing.
        """

        return await self._get("/venues", params={"hydrate": "location,fieldInfo"})

    async def fetch_venue(self, venue_id: int) -> ProviderResponse:
        """GET /venues/{id} -- a single venue (used by the audit)."""

        return await self._get(
            f"/venues/{int(venue_id)}", params={"hydrate": "location,fieldInfo"}
        )

    # -- Audit probe endpoints ------------------------------------------------
    async def fetch_teams(self, *, sport_id: int = 1) -> ProviderResponse:
        """GET /teams -- teams group; verifies the teams surface for the audit."""

        return await self._get("/teams", params={"sportId": sport_id})

    async def fetch_schedule(self, *, sport_id: int = 1) -> ProviderResponse:
        """GET /schedule -- schedules/games group for the audit."""

        return await self._get("/schedule", params={"sportId": sport_id})

    async def fetch_roster(self, team_id: object) -> ProviderResponse:
        """GET /teams/{id}/roster -- a team's roster (players group).

        ``team_id`` is validated to a positive integer before the request; the
        audit obtains it from the teams response rather than hardcoding one.
        """

        tid = _validate_positive_id(team_id, label="team_id")
        return await self._get(f"/teams/{tid}/roster", params={})

    async def fetch_person(self, person_id: object) -> ProviderResponse:
        """GET /people/{id} -- a single person (optional player verification)."""

        pid = _validate_positive_id(person_id, label="person_id")
        return await self._get(f"/people/{pid}", params={})

    async def first_team_id(self, *, sport_id: int = 1) -> Optional[int]:
        """Return a valid team id from ``/teams``, or ``None`` if none is present.

        Seeds the dependency-aware roster probe. Never fabricates an id; an empty
        or oddly-shaped payload yields ``None``.
        """

        response = await self.fetch_teams(sport_id=sport_id)
        return _first_id(response.data, "teams")
