"""Hot-path isolation: the database must not reach the live decision path.

``CLAUDE.md`` forbids querying a database on the hot decision path.
``probability/tests/test_probability.py`` already enforces the sibling rule that
``sqlite3`` never appears in ``probability/``; this is the same guarantee for
the new package, extended to every live-lane package.

Import-time isolation is checked as well as source text, because an indirect
import (``evaluation`` -> something -> ``sports_quant.db``) would breach the
rule just as effectively as a direct one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Packages that run on, or feed, the live decision path.
LIVE_LANE_PACKAGES = ("probability", "state", "evaluation", "gateway")


def _python_files(package: str) -> list[Path]:
    """Source files of a package, excluding caches and its own test modules.

    Test modules are excluded because these checks look for literal tokens, and
    a test that asserts "``import gateway`` must not appear" would otherwise
    match itself.
    """

    root = REPO_ROOT / package
    return [
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    ]


@pytest.mark.parametrize("package", LIVE_LANE_PACKAGES)
def test_live_lane_source_does_not_reference_the_db_package(package: str) -> None:
    offenders = []
    for path in _python_files(package):
        text = path.read_text(encoding="utf-8")
        if "sports_quant.db" in text or "from sports_quant import db" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"{package} references sports_quant.db in: {offenders}. "
        "The database is a research/ingestion component and must never reach "
        "the hot decision path."
    )


@pytest.mark.parametrize("package", LIVE_LANE_PACKAGES)
def test_live_lane_source_does_not_import_sqlite3(package: str) -> None:
    offenders = []
    for path in _python_files(package):
        text = path.read_text(encoding="utf-8")
        if "import sqlite3" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"{package} imports sqlite3 in: {offenders}"


@pytest.mark.parametrize("package", LIVE_LANE_PACKAGES)
def test_importing_a_live_lane_package_does_not_load_the_db_package(package: str) -> None:
    """Catches an indirect import that a source-text scan would miss."""

    script = (
        f"import {package}, sys; "
        "leaked = [m for m in sys.modules if m.startswith('sports_quant.db')]; "
        "print(','.join(sorted(leaked)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"importing {package} loaded {result.stdout.strip()}"
    )


def test_db_package_makes_no_network_import() -> None:
    """Phase A is entirely offline; no provider client belongs in it."""

    forbidden = ("httpx", "requests", "urllib.request", "aiohttp")
    offenders = []
    for path in _python_files("sports_quant/db"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if f"import {token}" in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
    assert not offenders, f"db package performs network imports: {offenders}"


def test_db_package_does_not_import_execution_code() -> None:
    """The quarantined execution lane must stay unreachable from the corpus."""

    offenders = []
    for path in _python_files("sports_quant/db"):
        text = path.read_text(encoding="utf-8")
        if "import gateway" in text or "from gateway" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"db package imports the quarantined gateway: {offenders}"
