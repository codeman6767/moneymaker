"""Phase D3 offline hoopR Parquet import — OPTIONAL (pyarrow) tests.

``pyarrow`` is an OPTIONAL dependency (pyproject ``tracking`` extra); the standard
CI job installs ``.[dev]`` only, so this module skips cleanly when pyarrow is
absent. Every other D3 test lives in ``test_phase_d3_nba.py`` and runs without
pyarrow. When pyarrow IS installed these tests exercise the offline importer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip the WHOLE module (collection included) when the optional Arrow stack is
# absent, so standard CI without pyarrow collects and runs the rest of D3.
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from sports_quant.db.engine import Database  # noqa: E402
from sports_quant.db.init import initialize_database  # noqa: E402
from sports_quant.ingest.hoopr_import import import_hoopr_parquet  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> Database:
    p = tmp_path / "corpus.db"
    initialize_database(p)
    return Database(p)


def _count(db: Database, table: str, where: str = "") -> int:
    with db.connection() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0])


def _write_pbp(path: Path, *, sub_text: str = "substitution") -> None:
    table = pa.table({
        "game_id": ["401585", "401585", "401585"],
        "sequence_number": [1, 2, 3],
        "period_number": [1, 1, 1],
        "clock_display_value": ["11:34", "10:00", "9:12"],
        "type_text": ["Made Shot", sub_text, "Rebound"],
        "text": ["X makes shot", "Y enters", "Z rebound"],
        "team_id": ["2", "2", "14"],
        "athlete_id_1": ["111", "222", "333"],
    })
    pq.write_table(table, path)


def test_hoopr_import_succeeds(db: Database, tmp_path: Path) -> None:
    f = tmp_path / "pbp.parquet"
    _write_pbp(f)
    r = import_hoopr_parquet(database=db, path=f)
    assert r.status == "succeeded"
    assert r.rows_read == 3 and r.records_inserted == 3
    assert r.file_sha256 is not None
    with db.connection() as conn:
        providers = {row[0] for row in conn.execute("SELECT DISTINCT provider FROM play_snapshots")}
        subs = conn.execute(
            "SELECT COUNT(*) FROM play_snapshots WHERE is_substitution=1").fetchone()[0]
    assert providers == {"hoopr"}  # no mixing with live balldontlie
    assert subs == 1


def test_hoopr_duplicate_import_is_idempotent(db: Database, tmp_path: Path) -> None:
    f = tmp_path / "pbp.parquet"
    _write_pbp(f)
    import_hoopr_parquet(database=db, path=f)
    r2 = import_hoopr_parquet(database=db, path=f)
    assert r2.records_inserted == 0 and r2.records_changed == 0
    assert r2.records_unchanged == 3
    assert _count(db, "play_snapshots") == 3


def test_hoopr_changed_source_row_appends(db: Database, tmp_path: Path) -> None:
    f1 = tmp_path / "pbp.parquet"
    _write_pbp(f1, sub_text="substitution")
    import_hoopr_parquet(database=db, path=f1)
    f2 = tmp_path / "pbp2.parquet"
    _write_pbp(f2, sub_text="Made Shot")  # play 2 content changes
    r = import_hoopr_parquet(database=db, path=f2)
    assert r.records_changed == 1 and r.records_unchanged == 2


def test_hoopr_unsupported_schema_is_rejected(db: Database, tmp_path: Path) -> None:
    bad = tmp_path / "bad.parquet"
    pq.write_table(pa.table({"foo": [1], "bar": [2]}), bad)
    r = import_hoopr_parquet(database=db, path=bad)
    assert r.status == "failed"
    assert r.error_type == "HooprImportError"
    good = tmp_path / "pbp.parquet"
    _write_pbp(good)
    r2 = import_hoopr_parquet(database=db, path=good, schema="nope")
    assert r2.status == "failed"


def test_hoopr_dry_run_persists_nothing(db: Database, tmp_path: Path) -> None:
    f = tmp_path / "pbp.parquet"
    _write_pbp(f)
    r = import_hoopr_parquet(database=db, path=f, dry_run=True)
    assert r.observations_normalized == 3 and r.rows_persisted == 0
    assert _count(db, "play_snapshots") == 0
