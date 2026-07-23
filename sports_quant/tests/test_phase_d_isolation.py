"""Phase D1 isolation + safety: gateway quarantine, no credential/signing.

Static source scans over the Phase D provider/ingest modules, plus a runtime
check that importing them loads no execution-lane code.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PHASE_D_SOURCES = (
    _REPO_ROOT / "sports_quant" / "providers" / "capabilities.py",
    _REPO_ROOT / "sports_quant" / "providers" / "base_provider.py",
    _REPO_ROOT / "sports_quant" / "providers" / "mlb_statsapi.py",
    _REPO_ROOT / "sports_quant" / "providers" / "balldontlie.py",
    _REPO_ROOT / "sports_quant" / "providers" / "nws.py",
    _REPO_ROOT / "sports_quant" / "providers" / "open_meteo.py",
    _REPO_ROOT / "sports_quant" / "ingest" / "provider_audit.py",
    _REPO_ROOT / "sports_quant" / "ingest" / "venues_ingestor.py",
    _REPO_ROOT / "sports_quant" / "db" / "repositories" / "references.py",
    _REPO_ROOT / "sports_quant" / "db" / "repositories" / "venues.py",
    _REPO_ROOT / "sports_quant" / "db" / "repositories" / "matching.py",
    _REPO_ROOT / "sports_quant" / "db" / "repositories" / "data_quality.py",
    _REPO_ROOT / "sports_quant" / "db" / "repositories" / "capabilities.py",
)

# Credential/signing tokens that must not appear in the read-only data providers.
_FORBIDDEN_TOKENS = (
    "private_key",
    "rsa",
    "signature",
    "hmac",
    "bearer",
    "load_pem",
    "cryptography",
)


@pytest.mark.parametrize("path", _PHASE_D_SOURCES, ids=lambda p: p.name)
def test_phase_d_source_does_not_import_gateway(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "import gateway" not in text and "from gateway" not in text, (
        f"{path.name} imports the quarantined execution gateway"
    )


@pytest.mark.parametrize("path", _PHASE_D_SOURCES, ids=lambda p: p.name)
def test_phase_d_provider_source_has_no_signing_tokens(path: Path) -> None:
    # Only the provider modules must be credential/signing-free; the repos may
    # legitimately mention nothing of the sort either, so the scan is uniform.
    text = path.read_text(encoding="utf-8")
    offenders = [
        tok for tok in _FORBIDDEN_TOKENS
        if re.search(rf"\b{re.escape(tok)}\b", text, re.IGNORECASE)
    ]
    assert not offenders, f"{path.name} references credential/signing tokens: {offenders}"


def test_importing_phase_d_providers_loads_no_gateway() -> None:
    script = (
        "import sports_quant.providers.mlb_statsapi, sports_quant.providers.balldontlie, "
        "sports_quant.providers.nws, sports_quant.providers.open_meteo, "
        "sports_quant.ingest.provider_audit, sports_quant.ingest.venues_ingestor, sys; "
        "leaked = [m for m in sys.modules if m.startswith('gateway')]; "
        "print(','.join(sorted(leaked)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", f"importing Phase D loaded gateway: {result.stdout.strip()}"


def test_db_package_still_isolated_from_execution() -> None:
    """The new repositories must not import the quarantined execution gateway."""

    for repo in ("references", "venues", "matching", "data_quality", "capabilities"):
        text = (_REPO_ROOT / "sports_quant" / "db" / "repositories" / f"{repo}.py").read_text(
            encoding="utf-8"
        )
        assert "import gateway" not in text and "from gateway" not in text
