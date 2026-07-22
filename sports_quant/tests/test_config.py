"""Read-only startup invariants and secret handling."""

from __future__ import annotations

import pytest

from sports_quant.config import ReadOnlyStartupError, Settings


def _good_settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        odds_api_key="super-secret-key",
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
