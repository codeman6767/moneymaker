"""BALLDONTLIE client (read-only, GET-only) for NBA data.

Authentication is a single request **header** carrying the API key. Endpoint
availability depends on the **account tier** (`sports_quant.config.nba_data_tier`,
selected: GOAT) -- a key alone does not grant GOAT. A plan-gated endpoint answers
``403``, which the base client classifies as a *subscription-tier restriction*
(``capability unavailable for current subscription tier``), never an invalid key.

D1 provides the read-only GET methods the ``provider-audit`` exercises across the
documented GOAT endpoint families (teams, players, games, per-player stats, box
scores, injuries, **play-by-play**, **lineups**, and **advanced stats**). Every
method is gated at ingestion time by the typed capability declaration; a
plan-gated one answers ``403`` and is classified as a tier restriction only with
explicit plan evidence. The key never reaches a stored URL, param, header, body,
log, or CLI/JSON output.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

import httpx
from pydantic import SecretStr

from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_BALLDONTLIE

_HOST = "api.balldontlie.io"
DEFAULT_BALLDONTLIE_BASE_URL = "https://api.balldontlie.io"

#: Hard cap on page size sent to BALLDONTLIE (its documented maximum is 100).
_MAX_PER_PAGE = 100


def _validate_game_id(game_id: object) -> int:
    """Coerce a provider game id to a positive int, or reject it.

    Rejects empty/blank/None and non-numeric or non-positive values *before* any
    request is made, so a dependent probe never issues a GET with a fabricated or
    malformed id.
    """

    if isinstance(game_id, bool):  # bool is an int subclass; never a valid id
        raise ValueError("game_id must be a positive integer, not a bool")
    if game_id is None:
        raise ValueError("game_id is required and must be a positive integer")
    if isinstance(game_id, str):
        text = game_id.strip()
        if not text or not text.isdigit():
            raise ValueError(f"game_id must be a positive integer (got {game_id!r})")
        value = int(text)
    elif isinstance(game_id, int):
        value = game_id
    else:
        raise ValueError(f"game_id must be a positive integer (got {type(game_id).__name__})")
    if value <= 0:
        raise ValueError(f"game_id must be a positive integer (got {value})")
    return value


def _clamp_per_page(per_page: int) -> int:
    if per_page < 1:
        raise ValueError(f"per_page must be >= 1 (got {per_page})")
    return min(per_page, _MAX_PER_PAGE)


def game_id_from_payload(data: object) -> Optional[int]:
    """Extract the first valid provider game id from a ``/v1/games`` payload.

    Returns ``None`` for an empty ``data`` array or an unexpected shape rather
    than fabricating an id. Never raises.
    """

    if not isinstance(data, dict):
        return None
    rows = data.get("data")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and "id" in row:
            try:
                return _validate_game_id(row["id"])
            except ValueError:
                continue
    return None


def substitutions_present(data: object) -> bool:
    """Whether a ``/v1/plays`` payload actually contains substitution events.

    Substitutions are marked observed **only** when the returned play data
    contains and validates at least one substitution-typed play -- never inferred
    from lineup or play *endpoint access* alone. A play qualifies when its type
    or description names a substitution.
    """

    if not isinstance(data, dict):
        return False
    rows = data.get("data")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        event_type = str(row.get("type") or row.get("event_type") or "").lower()
        description = str(row.get("description") or "").lower()
        if "substitution" in event_type or event_type == "sub":
            return True
        if "substitution" in description:
            return True
    return False


class BalldontlieClient(BaseProviderClient):
    """Async, read-only adapter for BALLDONTLIE.

    The key is sent as the ``Authorization`` request header (never captured in a
    :class:`RawExchange`, which stores only allow-listed *response* headers), and
    is additionally registered for body redaction as belt-and-braces.
    """

    provider_name = PROVIDER_BALLDONTLIE

    def __init__(
        self,
        api_key: SecretStr | str = "",
        *,
        base_url: str = DEFAULT_BALLDONTLIE_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        headers = {"Authorization": key} if key else {}
        super().__init__(
            base_url=base_url,
            policy=ReadOnlyHTTPPolicy.for_balldontlie(_HOST),
            client=client,
            default_headers=headers,
            redact_values=[key] if key else [],
            **kwargs,
        )

    async def fetch_teams(self, *, per_page: int = 100) -> ProviderResponse:
        """GET /v1/teams -- available on every tier; used by the audit."""

        return await self._get("/v1/teams", params={"per_page": per_page})

    async def fetch_players(
        self, *, cursor: Optional[int] = None, per_page: int = 25
    ) -> ProviderResponse:
        """GET /v1/players -- available on every tier; used by the audit."""

        params: dict[str, Any] = {"per_page": per_page}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get("/v1/players", params=params)

    # -- Audit probe endpoints ------------------------------------------------
    # One minimal GET per capability group the audit verifies. Each is a
    # *documented* BALLDONTLIE endpoint on the policy allow-list; a plan-gated
    # one answers 403-with-plan-evidence and is classified TIER_RESTRICTED for
    # *its own group only*. Play-by-play and lineups are documented GOAT
    # endpoints (they require a valid provider game id); the audit obtains that id
    # from the games probe rather than guessing one.
    async def fetch_games(self, *, per_page: int = 1) -> ProviderResponse:
        """GET /v1/games -- games / schedules / results group."""

        return await self._get("/v1/games", params={"per_page": _clamp_per_page(per_page)})

    async def fetch_stats(self, *, per_page: int = 1) -> ProviderResponse:
        """GET /v1/stats -- per-player game statistics group (GOAT-gated)."""

        return await self._get("/v1/stats", params={"per_page": _clamp_per_page(per_page)})

    async def fetch_box_scores(self, *, date: Optional[str] = None) -> ProviderResponse:
        """GET /v1/box_scores -- team/box statistics group (GOAT-gated)."""

        params: dict[str, Any] = {}
        if date is not None:
            params["date"] = date
        return await self._get("/v1/box_scores", params=params)

    async def fetch_player_injuries(self, *, per_page: int = 1) -> ProviderResponse:
        """GET /v1/player_injuries -- injuries group (tier-gated)."""

        return await self._get(
            "/v1/player_injuries", params={"per_page": _clamp_per_page(per_page)}
        )

    async def fetch_plays(self, *, game_id: object, per_page: int = 1) -> ProviderResponse:
        """GET /v1/plays?game_id=ID -- play-by-play for one game (GOAT-gated).

        ``game_id`` is required and validated to a positive integer before any
        request is issued; an empty/blank/non-numeric id raises ``ValueError``.
        """

        gid = _validate_game_id(game_id)
        return await self._get(
            "/v1/plays", params={"game_id": gid, "per_page": _clamp_per_page(per_page)}
        )

    async def fetch_lineups(
        self, *, game_ids: Iterable[object], per_page: int = 25
    ) -> ProviderResponse:
        """GET /v1/lineups?game_ids[]=ID -- lineups for one or more games (GOAT).

        Requires at least one game id; every id is validated to a positive integer
        before the request. Sends the documented ``game_ids[]`` array parameter.
        """

        validated = [_validate_game_id(g) for g in game_ids]
        if not validated:
            raise ValueError("fetch_lineups requires at least one game_id")
        return await self._get(
            "/v1/lineups",
            params={"game_ids[]": validated, "per_page": _clamp_per_page(per_page)},
        )

    async def fetch_advanced_stats(
        self,
        *,
        game_id: Optional[object] = None,
        season: Optional[int] = None,
        cursor: Optional[int] = None,
        per_page: int = 25,
    ) -> ProviderResponse:
        """GET /nba/v1/stats/advanced -- advanced statistics (GOAT-gated).

        Requires a bounding filter: at least one of ``game_id`` or ``season`` so
        the query is never an unbounded full scan. ``game_id`` (when given) is
        validated to a positive integer; ``season`` must be a plausible 4-digit
        year. Page size is bounded.
        """

        params: dict[str, Any] = {"per_page": _clamp_per_page(per_page)}
        if game_id is None and season is None:
            raise ValueError("fetch_advanced_stats requires a game_id or a season filter")
        if game_id is not None:
            params["game_id"] = _validate_game_id(game_id)
        if season is not None:
            if not isinstance(season, int) or isinstance(season, bool) or not (1900 <= season <= 2100):
                raise ValueError(f"season must be a 4-digit year (got {season!r})")
            params["season"] = season
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get("/nba/v1/stats/advanced", params=params)

    async def first_game_id(self) -> Optional[int]:
        """Return a valid provider game id from ``/v1/games``, or ``None``.

        Used by the dependency-aware audit to seed the plays/lineups probes. A
        2xx with an empty ``data`` array yields ``None`` (endpoint reachable, but
        no game to probe) rather than a fabricated id. Never raises for an empty
        or oddly-shaped payload -- it only extracts an id it can validate.
        """

        response = await self.fetch_games(per_page=1)
        return game_id_from_payload(response.data)
