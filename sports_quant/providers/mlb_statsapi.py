"""MLB StatsAPI client (read-only, GET-only, unauthenticated).

D1 provides the **venue** surface (to seed `venues`/`venue_aliases`) plus the
read-only GET methods a ``provider-audit`` exercises: teams, schedule, team
roster, and a single person lookup. D2 adds the per-game reads the MLB ingestor
needs: a date-ranged schedule (with optional `probablePitcher`/`lineups`
hydration), a game box score (team/player stats + batting order), and a game line
score (inning-by-inning + final R/H/E). No key is used; the base URL is pinned in
`sports_quant.config` and enforced by the transport policy.

Undocumented API, no SLA, no explicit correction timestamps -- see
`PHASE_D_PROVIDER_DECISIONS.md` §2.1. Nothing here runs at import time.
"""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Any, Optional

import httpx

from ..config import DEFAULT_MLB_STATS_API_BASE_URL
from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_MLB_STATSAPI

_HOST = "statsapi.mlb.com"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Hydration groups the D2 ingestor may request. Bounded to documented values so
#: an arbitrary hydrate string can never be injected into the query.
_ALLOWED_HYDRATE = frozenset({"probablePitcher", "lineups", "team", "linescore", "venue"})


def validate_iso_date(value: object, *, label: str = "date") -> str:
    """Strictly validate a ``YYYY-MM-DD`` date, or raise ``ValueError``."""

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a YYYY-MM-DD string (got {type(value).__name__})")
    text = value.strip()
    if not _ISO_DATE_RE.match(text):
        raise ValueError(f"{label} must be a YYYY-MM-DD calendar date (got {value!r})")
    try:
        _date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid calendar date (got {value!r})") from exc
    return text


def _validate_hydrate(hydrate: Optional[str]) -> Optional[str]:
    """Validate a comma-separated hydrate string against the allowed groups."""

    if hydrate is None:
        return None
    groups = [g.strip() for g in hydrate.split(",") if g.strip()]
    bad = [g for g in groups if g not in _ALLOWED_HYDRATE]
    if bad:
        raise ValueError(f"unsupported hydrate group(s): {bad}; allowed {sorted(_ALLOWED_HYDRATE)}")
    return ",".join(groups) if groups else None


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

    async def fetch_schedule(
        self,
        *,
        sport_id: int = 1,
        date: Optional[object] = None,
        start_date: Optional[object] = None,
        end_date: Optional[object] = None,
        game_pk: Optional[object] = None,
        hydrate: Optional[str] = None,
    ) -> ProviderResponse:
        """GET /schedule -- schedules/games, optionally by date and hydrated.

        ``date`` (a single day) or ``start_date``/``end_date`` (an inclusive
        range) filter the schedule; all are strictly validated ``YYYY-MM-DD``
        before the request. ``game_pk`` limits to one game. ``hydrate`` is bounded
        to documented groups (``probablePitcher``/``lineups``/...). No date and no
        game_pk returns the default schedule (used by the audit).
        """

        params: dict[str, Any] = {"sportId": int(sport_id)}
        if date is not None:
            params["date"] = validate_iso_date(date)
        if start_date is not None:
            params["startDate"] = validate_iso_date(start_date, label="start_date")
        if end_date is not None:
            params["endDate"] = validate_iso_date(end_date, label="end_date")
        if (("startDate" in params) ^ ("endDate" in params)):
            raise ValueError("start_date and end_date must be provided together")
        if game_pk is not None:
            params["gamePk"] = _validate_positive_id(game_pk, label="game_pk")
        cleaned = _validate_hydrate(hydrate)
        if cleaned is not None:
            params["hydrate"] = cleaned
        return await self._get("/schedule", params=params)

    async def fetch_boxscore(self, game_pk: object) -> ProviderResponse:
        """GET /game/{gamePk}/boxscore -- team + player game statistics.

        ``game_pk`` is validated to a positive integer before the request.
        """

        pk = _validate_positive_id(game_pk, label="game_pk")
        return await self._get(f"/game/{pk}/boxscore", params={})

    async def fetch_linescore(self, game_pk: object) -> ProviderResponse:
        """GET /game/{gamePk}/linescore -- inning-by-inning + final R/H/E."""

        pk = _validate_positive_id(game_pk, label="game_pk")
        return await self._get(f"/game/{pk}/linescore", params={})

    async def fetch_roster(
        self, team_id: object, *, date: Optional[object] = None, roster_type: str = "active"
    ) -> ProviderResponse:
        """GET /teams/{id}/roster -- a team's roster (players group).

        ``team_id`` is validated to a positive integer before the request. An
        optional ``date`` (strictly validated ``YYYY-MM-DD``) requests the roster
        **as of that date** -- so an ingest anchored on a date records the roster
        that held then, not merely today's roster.
        """

        tid = _validate_positive_id(team_id, label="team_id")
        params: dict[str, Any] = {"rosterType": roster_type}
        if date is not None:
            params["date"] = validate_iso_date(date)
        return await self._get(f"/teams/{tid}/roster", params=params)

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
