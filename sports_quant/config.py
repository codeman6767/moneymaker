"""Configuration + read-only startup invariants.

Loads the provider/safety settings from the repository-root ``.env`` file --
the *only* environment file the application reads -- and refuses to start unless
the read-only invariants hold:

* ``READ_ONLY_MODE=true``
* ``ORDER_SUBMISSION_ENABLED=false``
* ``PAPER_TRADING=false``
* ``LIVE_TRADING=false``
* ``MANUAL_LIVE_ARMING=false``
* ``KALSHI_ENVIRONMENT=production``
* ``KALSHI_PUBLIC_REST_URL`` exactly equal to the canonical production
  public-data URL (see :data:`PRODUCTION_KALSHI_REST_URL`)

The Odds API key is stored as a :class:`~pydantic.SecretStr` so it never leaks
through ``repr``/``str``; nothing in this module prints or logs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default location of the local historical corpus, relative to the repository
# root. Kept out of any tracked source directory and git-ignored: it is derived
# data that grows without bound. A relative default (rather than an absolute
# path) is what keeps the checkout portable between machines.
DEFAULT_DATABASE_PATH = "data/corpus.db"

# Repository root = parent of the ``sports_quant`` package directory.
REPO_ROOT = Path(__file__).resolve().parent.parent

# ``.env`` is the single supported environment file. ``.env.txt`` is no longer
# read: it leaked a real API key into git history and support for it was removed.
_ENV_FILE = str(REPO_ROOT / ".env")

# The one Kalshi REST base URL production read-only mode will accept. Any other
# host (including Kalshi demo) is rejected at startup.
PRODUCTION_KALSHI_REST_URL = "https://external-api.kalshi.com/trade-api/v2"

# Environment name that means "real, production, read-only public data".
PRODUCTION_ENVIRONMENT = "production"

# --------------------------------------------------------------------------- #
# Phase D (official data) provider base URLs -- pinned and validated.
# --------------------------------------------------------------------------- #
# These are the ONLY hosts the Phase D providers may reach. They are pinned here
# (like PRODUCTION_KALSHI_REST_URL) so an arbitrary provider host can never be
# substituted through an environment variable, and are validated at startup for
# scheme + host + port + path prefix. The transport policy blocks anything else
# too, but pinning fails closed before any I/O.
DEFAULT_MLB_STATS_API_BASE_URL = "https://statsapi.mlb.com/api/v1"
DEFAULT_NWS_BASE_URL = "https://api.weather.gov"
DEFAULT_OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1"

# (host, required-path-prefix) each pinned URL must exactly satisfy. The scheme
# must be https and no port may be supplied (default 443).
_PINNED_URL_SPECS: dict[str, tuple[str, str]] = {
    "mlb_stats_api_base_url": ("statsapi.mlb.com", "/api/v1"),
    "nws_base_url": ("api.weather.gov", ""),
    "open_meteo_base_url": ("api.open-meteo.com", "/v1"),
}

# Accepted BALLDONTLIE tiers (mirrors providers.capabilities.BalldontlieTier).
_VALID_NBA_TIERS: frozenset[str] = frozenset({"free", "all_star", "goat"})


def _pinned_url_violation(field: str, value: str) -> Optional[str]:
    """Return a human-readable violation for a pinned base URL, or ``None``.

    Rejects a non-https scheme, an unexpected host, an explicit port, or a path
    that does not start with the required prefix -- so environment substitution
    cannot redirect a provider to an arbitrary host.
    """

    from urllib.parse import urlsplit

    host, prefix = _PINNED_URL_SPECS[field]
    parts = urlsplit(value.rstrip("/"))
    if parts.scheme != "https":
        return f"{field} must use https (got {parts.scheme or 'no'} scheme)"
    if parts.hostname != host:
        return f"{field} host must be {host!r} (got {parts.hostname!r})"
    if parts.port is not None:
        return f"{field} must not specify a port (got {parts.port})"
    if prefix and not parts.path.startswith(prefix):
        return f"{field} path must start with {prefix!r} (got {parts.path!r})"
    return None


class ReadOnlyStartupError(RuntimeError):
    """Raised when the read-only startup invariants are not satisfied.

    The message enumerates every violated invariant so an operator can see, at
    a glance, exactly which flag is unsafe.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        joined = "\n  - ".join(violations)
        super().__init__(
            "Refusing to start: read-only invariants are not satisfied.\n"
            f"  - {joined}"
        )


class Settings(BaseSettings):
    """Typed application settings loaded from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Odds API. Optional at load time -- a missing key is reported safely by the
    # provider check rather than crashing the process.
    odds_api_key: SecretStr = Field(default=SecretStr(""))

    # Kalshi public REST (no authentication is used).
    kalshi_public_rest_url: str = PRODUCTION_KALSHI_REST_URL
    kalshi_environment: str = PRODUCTION_ENVIRONMENT

    # -- Phase D (official data). Optional; unused until Phase D ingestion. ----
    # BALLDONTLIE key for NBA data. Endpoint access depends on the ACCOUNT TIER,
    # not on possessing a key; the selected Phase D path expects GOAT (see
    # `nba_data_tier`). Held as SecretStr; never printed or stored.
    nba_data_api_key: SecretStr = Field(default=SecretStr(""))
    # Expected BALLDONTLIE tier: 'free' | 'all_star' | 'goat'. The selected
    # project tier is GOAT. Never inferred from key possession.
    nba_data_tier: str = "goat"
    # Optional keys for a keyed weather provider / professional feeds. Blank on
    # the no-paid MVP path.
    weather_api_key: SecretStr = Field(default=SecretStr(""))
    sportradar_mlb_api_key: SecretStr = Field(default=SecretStr(""))
    sportradar_nba_api_key: SecretStr = Field(default=SecretStr(""))
    # Pinned, validated public base URLs for the key-less providers.
    mlb_stats_api_base_url: str = DEFAULT_MLB_STATS_API_BASE_URL
    nws_base_url: str = DEFAULT_NWS_BASE_URL
    open_meteo_base_url: str = DEFAULT_OPEN_METEO_BASE_URL

    # Local historical corpus. Relative paths resolve against the repository
    # root so the value is portable across checkouts.
    database_path: str = DEFAULT_DATABASE_PATH

    # Safety flags -- all must hold their read-only values (see below).
    read_only_mode: bool = True
    order_submission_enabled: bool = False
    paper_trading: bool = False
    live_trading: bool = False
    manual_live_arming: bool = False

    # -- Invariants -----------------------------------------------------------
    def read_only_violations(self) -> list[str]:
        """Return a human-readable list of violated read-only invariants."""

        violations: list[str] = []
        if not self.read_only_mode:
            violations.append("READ_ONLY_MODE must be true")
        if self.order_submission_enabled:
            violations.append("ORDER_SUBMISSION_ENABLED must be false")
        if self.paper_trading:
            violations.append("PAPER_TRADING must be false")
        if self.live_trading:
            violations.append("LIVE_TRADING must be false")
        if self.manual_live_arming:
            violations.append("MANUAL_LIVE_ARMING must be false")
        if self.kalshi_environment != PRODUCTION_ENVIRONMENT:
            violations.append(
                f"KALSHI_ENVIRONMENT must be {PRODUCTION_ENVIRONMENT!r} "
                f"(got {self.kalshi_environment!r}; demo environments are rejected)"
            )
        # Pin the Kalshi base URL: an arbitrary or demo host must never be
        # reachable, even though the transport policy would also reject it.
        if self.kalshi_public_rest_url.rstrip("/") != PRODUCTION_KALSHI_REST_URL:
            violations.append(
                "KALSHI_PUBLIC_REST_URL must be exactly "
                f"{PRODUCTION_KALSHI_REST_URL!r} in production read-only mode "
                f"(got {self.kalshi_public_rest_url!r})"
            )
        # Pin the Phase D provider base URLs (fail closed before any I/O).
        for field, value in (
            ("mlb_stats_api_base_url", self.mlb_stats_api_base_url),
            ("nws_base_url", self.nws_base_url),
            ("open_meteo_base_url", self.open_meteo_base_url),
        ):
            violation = _pinned_url_violation(field, value)
            if violation is not None:
                violations.append(violation)
        # Validate the declared NBA tier against the known set.
        if self.nba_data_tier not in _VALID_NBA_TIERS:
            violations.append(
                f"NBA_DATA_TIER must be one of {sorted(_VALID_NBA_TIERS)} "
                f"(got {self.nba_data_tier!r})"
            )
        return violations

    def enforce_read_only(self) -> None:
        """Raise :class:`ReadOnlyStartupError` unless every invariant holds."""

        violations = self.read_only_violations()
        if violations:
            raise ReadOnlyStartupError(violations)

    def has_odds_api_key(self) -> bool:
        """True if an Odds API key is configured (its value is never revealed)."""

        return bool(self.odds_api_key.get_secret_value().strip())

    def has_nba_data_api_key(self) -> bool:
        """True if a BALLDONTLIE key is configured (value never revealed).

        A configured key does **not** imply GOAT access -- endpoint availability
        depends on the account tier (see :attr:`nba_data_tier`).
        """

        return bool(self.nba_data_api_key.get_secret_value().strip())

    def resolved_database_path(self) -> Path:
        """Absolute path to the corpus database.

        A relative ``DATABASE_PATH`` resolves against the repository root, not
        the current working directory, so ``db-init`` writes to the same file
        no matter where it is invoked from.
        """

        configured = Path(self.database_path).expanduser()
        if configured.is_absolute():
            return configured
        return (REPO_ROOT / configured).resolve()


def load_settings() -> Settings:
    """Load settings and enforce the read-only startup invariants.

    This is the single entry point the application uses at startup; it will
    raise :class:`ReadOnlyStartupError` rather than run in an unsafe mode.
    """

    settings = Settings()
    settings.enforce_read_only()
    return settings
