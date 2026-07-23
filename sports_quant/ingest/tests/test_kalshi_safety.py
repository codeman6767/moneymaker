"""Static safety guarantees for the Kalshi public ingestion path.

These assert, by scanning source, that the Kalshi provider and ingestor never
load a credential, sign a request, or reach the quarantined execution gateway.
The behavioural GET-only / no-auth-header checks live in test_kalshi_ingestor.py
and test_http_policy.py; this file guards against the code ever growing an
authenticated path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_KALSHI_SOURCES = (
    _REPO_ROOT / "sports_quant" / "providers" / "kalshi.py",
    _REPO_ROOT / "sports_quant" / "ingest" / "kalshi_ingestor.py",
    _REPO_ROOT / "sports_quant" / "db" / "repositories" / "kalshi.py",
)

# Tokens that would signal authentication, request signing, or key loading.
# Matched case-insensitively at word boundaries so an incidental substring
# (e.g. "rsa" inside "vice versa") does not trigger a false positive.
_FORBIDDEN_TOKENS = (
    "private_key",
    "rsa",
    "signature",
    "authorization",
    "api_key",
    "apikey",
    "load_pem",
    "cryptography",
    "bearer",
    "hmac",
)


@pytest.mark.parametrize("path", _KALSHI_SOURCES, ids=lambda p: p.name)
def test_no_credential_or_signing_in_kalshi_source(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    offenders = [
        tok for tok in _FORBIDDEN_TOKENS
        if re.search(rf"\b{re.escape(tok)}\b", text, re.IGNORECASE)
    ]
    assert not offenders, f"{path.name} references credential/signing tokens: {offenders}"


@pytest.mark.parametrize("path", _KALSHI_SOURCES, ids=lambda p: p.name)
def test_kalshi_source_does_not_import_gateway(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "import gateway" not in text and "from gateway" not in text, (
        f"{path.name} imports the quarantined execution gateway"
    )


def test_kalshi_client_sends_no_default_headers() -> None:
    """The Kalshi client is built with no auth/signing headers."""

    from sports_quant.providers.kalshi import DEFAULT_BASE_URL, KalshiClient

    client = KalshiClient(base_url=DEFAULT_BASE_URL)
    try:
        header_names = {k.lower() for k in client._client.headers}  # type: ignore[attr-defined]
    finally:
        # No network was opened; just release the client object.
        pass
    assert "authorization" not in header_names
    assert not any(name.startswith("kalshi-") for name in header_names)
    assert "x-api-key" not in header_names
