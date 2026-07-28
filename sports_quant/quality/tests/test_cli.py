"""Phase E2 CLI tests for ``data-status`` and ``data-quality`` (offline)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sports_quant.db.engine import Database
from sports_quant.matching.tests.conftest import seed_schedule, seed_team
from sports_quant.matching.tests.test_phase_d5a_matching import _create_canonical
from sports_quant.pit.tests.conftest import SCHED_START, seed_result
from sports_quant.quality.runner import run_data_quality
from sports_quant.status import run_data_status


def _build_corpus(db_path: Path, *, result_at: str = "2026-07-16T00:00:00.000000Z",
                  winning: str = "home") -> None:
    """Seed one fully-linked MLB game + final result, then checkpoint the WAL into
    the main file (the read-only reader opens ``immutable=1`` and ignores the WAL)."""

    with Database(db_path).connection() as conn:
        home = seed_team(conn, league_code="MLB", abbreviation="LAD",
                         canonical_name="Los Angeles Dodgers", city="LA", nickname="Dodgers")
        away = seed_team(conn, league_code="MLB", abbreviation="SD",
                         canonical_name="San Diego Padres", city="SD", nickname="Padres")
        ref = seed_schedule(conn, provider="mlb_statsapi", provider_game_id="G1",
                            home_provider_team_id="101", away_provider_team_id="102",
                            scheduled_start=SCHED_START, season=2026, game_date_local="2026-07-14")
        _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                          scheduled_start=SCHED_START, game_date_local="2026-07-14",
                          official_provider="mlb_statsapi", official_game_key="G1")
        seed_result(conn, game_ref_id=ref, observed_at=result_at, winning_side=winning)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _no_sidecars(db_path: Path) -> bool:
    return not any(n.name.endswith(("-wal", "-shm", "-journal"))
                   for n in db_path.parent.iterdir())


# --------------------------------------------------------------------------- #
# data-status
# --------------------------------------------------------------------------- #
def test_data_status_json_deterministic(db_path: Path) -> None:
    _build_corpus(db_path)
    out1: list[str] = []
    assert run_data_status(database_path=db_path, as_json=True, out=out1.append) == 0
    payload = json.loads(out1[-1])
    assert payload["schema_version"] == 16 and payload["canonical_games"] == 1
    assert payload["command"] == "data-status"
    assert set(payload) >= {"table_row_counts", "observation_coverage", "provider_runs",
                            "unresolved_references", "unmatched_sportsbook_events",
                            "unmatched_kalshi", "pending_manual_review", "open_data_quality"}
    out2: list[str] = []
    run_data_status(database_path=db_path, as_json=True, out=out2.append)
    assert out1[-1] == out2[-1]  # deterministic


def test_data_status_creates_no_sidecars(db_path: Path) -> None:
    _build_corpus(db_path)
    baseline = sorted(n.name for n in db_path.parent.iterdir())
    run_data_status(database_path=db_path, as_json=True, out=lambda _s: None)
    assert sorted(n.name for n in db_path.parent.iterdir()) == baseline
    assert _no_sidecars(db_path)


def test_data_status_league_and_since_filters(db_path: Path) -> None:
    _build_corpus(db_path)
    out: list[str] = []
    assert run_data_status(database_path=db_path, league="nba", as_json=True, out=out.append) == 0
    assert json.loads(out[-1])["canonical_games"] == 0  # the only game is MLB
    out2: list[str] = []
    run_data_status(database_path=db_path, since="2027-01-01", as_json=True, out=out2.append)
    assert json.loads(out2[-1])["canonical_games"] == 0  # game_date_local is 2026


def test_data_status_missing_db_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert run_data_status(database_path=missing, out=lambda _s: None) == 3
    assert not missing.exists()


def test_data_status_unmigrated_db(tmp_path: Path) -> None:
    p = tmp_path / "bare.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE x (a)")
    conn.commit()
    conn.close()
    assert run_data_status(database_path=p, out=lambda _s: None) == 3


# --------------------------------------------------------------------------- #
# data-quality
# --------------------------------------------------------------------------- #
def test_data_quality_clean_exit0(db_path: Path) -> None:
    _build_corpus(db_path)
    out: list[str] = []
    rc = run_data_quality(database_path=db_path, as_json=True, out=out.append)
    payload = json.loads(out[-1])
    assert rc == 0 and payload["grade"] == "A" and payload["execution_valid"] is True
    assert payload["counts"]["blocking"] == 0


def test_data_quality_leak_exit1(db_path: Path) -> None:
    _build_corpus(db_path, result_at="2026-07-10T00:00:00.000000Z")  # result before scheduled start
    out: list[str] = []
    rc = run_data_quality(database_path=db_path, as_json=True, out=out.append)
    payload = json.loads(out[-1])
    assert rc == 1 and payload["grade"] == "F" and payload["execution_valid"] is False
    assert "DQ-PIT-001" in payload["by_rule"]


def test_data_quality_fail_on_note(db_path: Path) -> None:
    # A game with no provable final label -> a NOTE. Default (blocking) passes;
    # --fail-on note fails.
    with Database(db_path).connection() as conn:
        home = seed_team(conn, league_code="MLB", abbreviation="LAD",
                         canonical_name="LAD", city="LA", nickname="D")
        away = seed_team(conn, league_code="MLB", abbreviation="SD",
                         canonical_name="SD", city="SD", nickname="P")
        ref = seed_schedule(conn, provider="mlb_statsapi", provider_game_id="G1",
                            home_provider_team_id="101", away_provider_team_id="102",
                            scheduled_start=SCHED_START, season=2026, game_date_local="2026-07-14")
        _create_canonical(conn, league_code="MLB", home_team_id=home, away_team_id=away,
                          scheduled_start=SCHED_START, game_date_local="2026-07-14",
                          official_provider="mlb_statsapi", official_game_key="G1")
        seed_result(conn, game_ref_id=ref, observed_at="2026-07-16T00:00:00.000000Z",
                    winning_side=None, mapped_status="in_progress")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert run_data_quality(database_path=db_path, out=lambda _s: None) == 0        # default blocking
    assert run_data_quality(database_path=db_path, fail_on="note", out=lambda _s: None) == 1


def test_data_quality_repeated_run_no_mutation(db_path: Path) -> None:
    _build_corpus(db_path, result_at="2026-07-10T00:00:00.000000Z")
    with Database(db_path).connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0]
    out1: list[str] = []
    out2: list[str] = []
    run_data_quality(database_path=db_path, as_json=True, out=out1.append)
    run_data_quality(database_path=db_path, as_json=True, out=out2.append)
    with Database(db_path).connection() as conn:
        after = conn.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0]
    assert before == after == 0 and out1[-1] == out2[-1]  # no rows written; deterministic


def test_data_quality_missing_db(tmp_path: Path) -> None:
    assert run_data_quality(database_path=tmp_path / "nope.db", out=lambda _s: None) == 3


def test_cli_main_wiring(db_path: Path) -> None:
    from sports_quant.cli import main
    _build_corpus(db_path)
    assert main(["data-status", "--db", str(db_path), "--json"]) == 0
    assert main(["data-quality", "--db", str(db_path), "--json"]) == 0
