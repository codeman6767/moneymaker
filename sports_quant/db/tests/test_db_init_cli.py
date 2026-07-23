"""``python -m sports_quant db-init``: output, exit codes, repeat safety.

Every test writes to a temporary path, so the developer's real corpus at
``DATABASE_PATH`` is never touched. No network call is made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sports_quant.cli import EXIT_DATABASE_ERROR, main, run_db_init
from sports_quant.config import PRODUCTION_KALSHI_REST_URL, Settings

API_KEY = "db-init-test-key-do-not-log"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        odds_api_key=API_KEY,
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


def test_first_run_creates_migrates_and_seeds(db_path: Path) -> None:
    lines: list[str] = []
    code = run_db_init(_settings(), database_path=db_path, out=lines.append)
    output = "\n".join(lines)

    assert code == 0
    assert db_path.exists()
    assert "database file created" in output
    assert "applied migration 001 a001_core_entities" in output
    assert "applied migration 002 a002_games" in output
    assert "applied migration 003 a003_integrity_guards" in output
    assert "applied migration 004 b004_raw_responses" in output
    assert "applied migration 005 b005_sportsbook" in output
    assert "applied migration 006 b006_sportsbook_transition_dedup" in output
    assert "applied migration 007 c007_kalshi" in output
    assert "applied migration 008 c008_kalshi_metadata_integrity" in output
    assert "applied migration 009 d009_provider_infra" in output
    assert "applied migration 010 d010_provider_audit_integrity" in output
    assert "Schema version: 10" in output
    assert "MLB: 30 teams (30 new)" in output
    assert "NBA: 30 teams (30 new)" in output


def test_second_run_succeeds_and_changes_nothing(db_path: Path) -> None:
    first: list[str] = []
    second: list[str] = []
    assert run_db_init(_settings(), database_path=db_path, out=first.append) == 0
    assert run_db_init(_settings(), database_path=db_path, out=second.append) == 0

    output = "\n".join(second)
    assert "database file already present" in output
    assert "schema already current" in output
    assert "MLB: 30 teams (0 new)" in output
    assert "NBA: 30 teams (0 new)" in output
    assert "already up to date" in output


def test_run_is_safe_repeated_many_times(db_path: Path) -> None:
    for _ in range(4):
        assert run_db_init(_settings(), database_path=db_path, out=lambda _s: None) == 0


def test_output_never_contains_the_api_key(db_path: Path) -> None:
    lines: list[str] = []
    run_db_init(_settings(), database_path=db_path, out=lines.append)
    assert API_KEY not in "\n".join(lines)


def test_output_reports_the_database_path(db_path: Path) -> None:
    lines: list[str] = []
    run_db_init(_settings(), database_path=db_path, out=lines.append)
    assert str(db_path) in "\n".join(lines)


def test_creates_missing_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested" / "corpus.db"
    assert run_db_init(_settings(), database_path=nested, out=lambda _s: None) == 0
    assert nested.exists()


def test_unsafe_settings_are_refused_before_touching_the_database(tmp_path: Path) -> None:
    """The read-only invariants gate db-init exactly as they gate everything else."""

    from sports_quant.config import ReadOnlyStartupError

    unsafe = _settings(live_trading=True)
    path = tmp_path / "never-created.db"
    with pytest.raises(ReadOnlyStartupError):
        run_db_init(unsafe, database_path=path, out=lambda _s: None)
    assert not path.exists()


def test_database_error_returns_exit_code_three(db_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from sports_quant import cli
    from sports_quant.db.engine import MigrationError

    def boom(*_args: object, **_kwargs: object) -> None:
        raise MigrationError("simulated migration failure")

    monkeypatch.setattr(cli, "initialize_database", boom)
    lines: list[str] = []
    code = run_db_init(_settings(), database_path=db_path, out=lines.append)
    assert code == EXIT_DATABASE_ERROR == 3
    assert "simulated migration failure" in "\n".join(lines)


def test_main_dispatches_db_init(db_path: Path) -> None:
    assert main(["db-init", "--db", str(db_path)]) == 0
    assert db_path.exists()


def test_main_db_init_is_repeatable(db_path: Path) -> None:
    assert main(["db-init", "--db", str(db_path)]) == 0
    assert main(["db-init", "--db", str(db_path)]) == 0


def test_main_rejects_an_unknown_command() -> None:
    with pytest.raises(SystemExit):
        main(["not-a-command"])


def test_db_init_help_is_registered(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "db-init" in capsys.readouterr().out
