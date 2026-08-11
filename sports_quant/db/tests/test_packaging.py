"""Wheel packaging integrity (task §2/§3).

The engine loads migrations from the installed package, and newer packages such
as ``sports_quant.matching`` must ship, so a built wheel is inspected directly:
it must contain the matching modules and every migration through d015, declare
the ``sports-quant`` console entry point, and never contain tests, a database,
``.env``, or a raw payload. Building the wheel is done once here as a subprocess.
A missing build backend or a broken wheel is a real packaging **failure**, not a
reason to skip -- the suite must catch a packaging regression, so the build error
is surfaced in full and fails the test.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> tuple[list[str], str]:
    """Build the project wheel once and return ``(namelist, entry_points.txt)``."""

    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation",
         "--wheel-dir", str(out)],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    # A failed build is a hard failure with the complete, useful build output --
    # never a skip. A packaging regression must fail the suite.
    assert proc.returncode == 0, (
        f"wheel build failed (exit {proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    wheels = list(out.glob("sports_quant-*.whl"))
    assert len(wheels) == 1, f"expected exactly one project wheel, got {[w.name for w in wheels]}"
    with zipfile.ZipFile(wheels[0]) as z:
        names = z.namelist()
        ep_entries = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        entry_points = z.read(ep_entries[0]).decode("utf-8") if ep_entries else ""
    return names, entry_points


def test_wheel_includes_matching_package(built_wheel: tuple[list[str], str]) -> None:
    names, _ = built_wheel
    for module in (
        "sports_quant/matching/__init__.py",
        "sports_quant/matching/sportsbook.py",
        "sports_quant/matching/service.py",
        "sports_quant/matching/players_service.py",
        "sports_quant/matching/linkatomic.py",
    ):
        assert module in names, f"{module} missing from wheel"


def test_wheel_includes_every_migration_through_f019(built_wheel: tuple[list[str], str]) -> None:
    names, _ = built_wheel
    migrations = sorted(
        n.split("/")[-1] for n in names
        if "/db/migrations/" in n and n.endswith(".sql")
    )
    assert len(migrations) == 19, migrations
    assert migrations[0] == "a001_core_entities.sql"
    # A representative intermediate migration and the latest one.
    assert "sports_quant/db/migrations/d009_provider_infra.sql" in names
    assert "sports_quant/db/migrations/e017_provider_identity.sql" in names
    assert "sports_quant/db/migrations/f018_retrospective_provenance.sql" in names
    assert migrations[-1] == "f019_retrospective_provenance_repairs.sql"


def test_wheel_declares_console_entry_point(built_wheel: tuple[list[str], str]) -> None:
    _names, entry_points = built_wheel
    assert "[console_scripts]" in entry_points, entry_points
    flat = entry_points.replace(" ", "")
    assert "sports-quant=sports_quant.cli:main" in flat, entry_points


def test_wheel_excludes_tests_and_secrets(built_wheel: tuple[list[str], str]) -> None:
    names, _ = built_wheel
    assert not any("/tests/" in n for n in names), "tests must not be packaged"
    for n in names:
        low = n.lower()
        assert not low.endswith(".env"), n
        assert not low.endswith(".db"), n
        assert "corpus" not in low, n
        assert "graphify-out" not in low, n
        assert not low.endswith(".raw"), n  # no raw payload export
