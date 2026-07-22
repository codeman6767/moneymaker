"""Read-only startup invariants and secret handling."""

from __future__ import annotations

import pytest

from sports_quant.config import (
    PRODUCTION_KALSHI_REST_URL,
    ReadOnlyStartupError,
    Settings,
)


def _good_settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        odds_api_key="super-secret-key",
        kalshi_public_rest_url=PRODUCTION_KALSHI_REST_URL,
        kalshi_environment="production",
        read_only_mode=True,
        order_submission_enabled=False,
        paper_trading=False,
        live_trading=False,
        manual_live_arming=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_valid_read_only_config_passes() -> None:
    settings = _good_settings()
    assert settings.read_only_violations() == []
    settings.enforce_read_only()  # must not raise


@pytest.mark.parametrize(
    "overrides",
    [
        {"read_only_mode": False},
        {"order_submission_enabled": True},
        {"paper_trading": True},
        {"live_trading": True},
        {"manual_live_arming": True},
        {"kalshi_environment": "demo"},
    ],
)
def test_each_unsafe_flag_refuses_startup(overrides: dict[str, object]) -> None:
    settings = _good_settings(**overrides)
    assert settings.read_only_violations(), "expected at least one violation"
    with pytest.raises(ReadOnlyStartupError):
        settings.enforce_read_only()


@pytest.mark.parametrize(
    "url",
    [
        # Kalshi demo host -- never permitted in production read-only mode.
        "https://demo-api.kalshi.co/trade-api/v2",
        # Another Kalshi host / a different API surface.
        "https://api.elections.kalshi.com/trade-api/v2",
        "https://external-api.kalshi.com/trade-api/v1",
        # An entirely unrelated host.
        "https://evil.example.com/trade-api/v2",
        # Plaintext transport to the right host.
        "http://external-api.kalshi.com/trade-api/v2",
    ],
)
def test_non_canonical_kalshi_url_refuses_startup(url: str) -> None:
    settings = _good_settings(kalshi_public_rest_url=url)
    assert any("KALSHI_PUBLIC_REST_URL" in v for v in settings.read_only_violations())
    with pytest.raises(ReadOnlyStartupError):
        settings.enforce_read_only()


def test_canonical_kalshi_url_is_accepted_with_or_without_trailing_slash() -> None:
    for url in (PRODUCTION_KALSHI_REST_URL, PRODUCTION_KALSHI_REST_URL + "/"):
        settings = _good_settings(kalshi_public_rest_url=url)
        assert settings.read_only_violations() == []
        settings.enforce_read_only()  # must not raise


def test_missing_odds_api_key_handled_safely() -> None:
    settings = _good_settings(odds_api_key="")
    # Missing key does not violate read-only startup ...
    settings.enforce_read_only()
    # ... it is simply reported as absent.
    assert settings.has_odds_api_key() is False


def test_api_key_is_not_revealed_in_repr() -> None:
    settings = _good_settings(odds_api_key="super-secret-key")
    assert settings.has_odds_api_key() is True
    assert "super-secret-key" not in repr(settings)
    assert "super-secret-key" not in str(settings.odds_api_key)
    # The real value is only accessible via the explicit accessor.
    assert settings.odds_api_key.get_secret_value() == "super-secret-key"
