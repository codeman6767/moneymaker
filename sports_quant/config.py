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

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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
        return violations

    def enforce_read_only(self) -> None:
        """Raise :class:`ReadOnlyStartupError` unless every invariant holds."""

        violations = self.read_only_violations()
        if violations:
            raise ReadOnlyStartupError(violations)

    def has_odds_api_key(self) -> bool:
        """True if an Odds API key is configured (its value is never revealed)."""

        return bool(self.odds_api_key.get_secret_value().strip())


def load_settings() -> Settings:
    """Load settings and enforce the read-only startup invariants.

    This is the single entry point the application uses at startup; it will
    raise :class:`ReadOnlyStartupError` rather than run in an unsafe mode.
    """

    settings = Settings()
    settings.enforce_read_only()
    return settings
