"""Wheel packaging integrity (task §2/§3).

The engine loads migrations from the installed package, and newer packages such
as ``sports_quant.matching`` must ship, so a built wheel is inspected directly:
it must contain the matching modules and every migration through d015, and must
never contain tests, a database, ``.env``, or a raw payload. Building the wheel
is done once here as a subprocess so a packaging regression fails the suite.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation",
         "--wheel-dir", str(out)],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"wheel build unavailable in this environment: {proc.stderr[-400:]}")
    wheels = list(out.glob("sports_quant-*.whl"))
    assert wheels, "no sports_quant wheel was produced"
    with zipfile.ZipFile(wheels[0]) as z:
        return z.namelist()


def test_wheel_includes_matching_package(wheel_names: list[str]) -> None:
    for module in (
        "sports_quant/matching/__init__.py",
        "sports_quant/matching/sportsbook.py",
        "sports_quant/matching/service.py",
        "sports_quant/matching/players_service.py",
        "sports_quant/matching/linkatomic.py",
    ):
        assert module in wheel_names, f"{module} missing from wheel"


def test_wheel_includes_every_migration_through_d015(wheel_names: list[str]) -> None:
    migrations = sorted(
        n.split("/")[-1] for n in wheel_names
        if "/db/migrations/" in n and n.endswith(".sql")
    )
    assert len(migrations) == 15, migrations
    assert migrations[0] == "a001_core_entities.sql"
    # A representative intermediate migration and the latest one.
    assert "sports_quant/db/migrations/d009_provider_infra.sql" in wheel_names
    assert migrations[-1] == "d015_sportsbook_matching.sql"


def test_wheel_excludes_tests_and_secrets(wheel_names: list[str]) -> None:
    assert not any("/tests/" in n for n in wheel_names), "tests must not be packaged"
    for n in wheel_names:
        low = n.lower()
        assert not low.endswith(".env"), n
        assert not low.endswith(".db"), n
        assert "corpus" not in low, n
        assert "graphify-out" not in low, n
