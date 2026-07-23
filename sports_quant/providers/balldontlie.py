"""BALLDONTLIE client (read-only, GET-only) for NBA data.

Authentication is a single request **header** carrying the API key. Endpoint
availability depends on the **account tier** (`sports_quant.config.nba_data_tier`,
selected: GOAT) -- a key alone does not grant GOAT. A plan-gated endpoint answers
``403``, which the base client classifies as a *subscription-tier restriction*
(``capability unavailable for current subscription tier``), never an invalid key.

D1 needs only the infrastructure a ``provider-audit`` exercises (teams/players);
the stats/box/injuries/plays methods are added in D3 and are gated by the typed
capability declaration. The key never reaches a stored URL, param, header, body,
log, or CLI/JSON output.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from pydantic import SecretStr

from ..http_policy import ReadOnlyHTTPPolicy
from .base_provider import BaseProviderClient, ProviderResponse
from .capabilities import PROVIDER_BALLDONTLIE

_HOST = "api.balldontlie.io"
DEFAULT_BALLDONTLIE_BASE_URL = "https://api.balldontlie.io"


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
    # *its own group only*. Capabilities without a documented endpoint (plays,
    # lineups, confirmed pregame starters, substitutions) are deliberately NOT
    # given a method here: the audit leaves them declared-only rather than
    # guessing an endpoint name.
    async def fetch_games(self, *, per_page: int = 1) -> ProviderResponse:
        """GET /v1/games -- games / schedules / results group."""

        return await self._get("/v1/games", params={"per_page": per_page})

    async def fetch_stats(self, *, per_page: int = 1) -> ProviderResponse:
        """GET /v1/stats -- per-player game statistics group (GOAT-gated)."""

        return await self._get("/v1/stats", params={"per_page": per_page})

    async def fetch_box_scores(self, *, date: Optional[str] = None) -> ProviderResponse:
        """GET /v1/box_scores -- team/box statistics group (GOAT-gated)."""

        params: dict[str, Any] = {}
        if date is not None:
            params["date"] = date
        return await self._get("/v1/box_scores", params=params)

    async def fetch_player_injuries(self, *, per_page: int = 1) -> ProviderResponse:
        """GET /v1/player_injuries -- injuries group (tier-gated)."""

        return await self._get("/v1/player_injuries", params={"per_page": per_page})
