"""F1A checkpoint + scratch-DB isolation tests (offline; no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION
from sports_quant.ingest.checkpoint import (
    Checkpoint,
    CheckpointError,
    load_checkpoint,
    verify_resume,
    write_checkpoint,
)
from sports_quant.ingest.scratch_db import (
    ScratchClass,
    ScratchDbError,
    classify_scratch_db,
    require_usable,
)


def _empty_v16_db(tmp_path: Path, name: str = "scratch.db") -> Path:
    path = tmp_path / name
    initialize_database(path)
    return path


def _make_nonempty(path: Path) -> None:
    with Database(path).connection() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, "
            "status, requested_at, started_at, completed_at, started_monotonic_ns, tool_version, "
            "created_at) VALUES ('run_x','ingest','mlb_statsapi','op','{}','succeeded',"
            "'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z',"
            "'2026-01-01T00:00:00.000000Z',0,'t','2026-01-01T00:00:00.000000Z')")


# --- scratch-DB classification -------------------------------------------- #
def test_missing_path_is_new(tmp_path: Path) -> None:
    c = classify_scratch_db(tmp_path / "nope.db")
    assert c.kind is ScratchClass.NEW


def test_empty_v16_is_scratch(tmp_path: Path) -> None:
    c = classify_scratch_db(_empty_v16_db(tmp_path))
    assert c.kind is ScratchClass.EMPTY_V16
    assert c.schema_version == CURRENT_SCHEMA_VERSION
    require_usable(c, resume=False)  # usable for a fresh run


def test_nonempty_v16_without_resume_is_unsafe(tmp_path: Path) -> None:
    db = _empty_v16_db(tmp_path)
    _make_nonempty(db)
    c = classify_scratch_db(db)
    assert c.kind is ScratchClass.UNSAFE
    with pytest.raises(ScratchDbError):
        require_usable(c, resume=False)


def test_nonempty_v16_authorized_by_matching_fingerprint(tmp_path: Path) -> None:
    db = _empty_v16_db(tmp_path)
    _make_nonempty(db)
    fp = classify_scratch_db(db).fingerprint
    c = classify_scratch_db(db, resume=True, expected_fingerprint=fp)
    assert c.kind is ScratchClass.AUTHORIZED_RESUMABLE
    require_usable(c, resume=True)
    # A different (stale) fingerprint must NOT authorize.
    bad = classify_scratch_db(db, resume=True, expected_fingerprint="deadbeef")
    assert bad.kind is ScratchClass.UNSAFE


def test_explicit_path_required(tmp_path: Path) -> None:
    with pytest.raises(ScratchDbError):
        classify_scratch_db(None)


def test_directory_and_forbidden_paths_rejected(tmp_path: Path) -> None:
    with pytest.raises(ScratchDbError):
        classify_scratch_db(tmp_path)  # a directory
    db = _empty_v16_db(tmp_path)
    with pytest.raises(ScratchDbError):
        classify_scratch_db(db, forbidden_paths=(db,))  # resolves to a protected DB


def test_rejected_classification_leaves_db_byte_identical(tmp_path: Path) -> None:
    db = _empty_v16_db(tmp_path)
    _make_nonempty(db)
    before = db.read_bytes()
    with pytest.raises(ScratchDbError):
        require_usable(classify_scratch_db(db), resume=False)
    assert db.read_bytes() == before  # never mutated


# --- checkpoint atomic write / load / resume verification ------------------ #
def _ckpt(**over) -> Checkpoint:  # type: ignore[no-untyped-def]
    base = dict(
        manifest_hash="H", plan_version="f1a-plan-v1", provider="balldontlie", league="nba",
        date_range="2026-01-05", families=("games",), scratch_db="s.db",
        scratch_fingerprint="FP", schema_version=16, request_cap=20, credit_cap=20,
    )
    base.update(over)
    return Checkpoint(**base)  # type: ignore[arg-type]


def test_checkpoint_atomic_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "p.ckpt"
    ck = _ckpt(completed_identities=["a", "b"], state="in_progress")
    write_checkpoint(path, ck)
    assert not (path.with_name(f"{path.name}.tmp-{__import__('os').getpid()}")).exists()  # temp gone
    loaded = load_checkpoint(path)
    assert loaded.is_complete_for("a") and loaded.is_complete_for("b")
    assert loaded.manifest_hash == "H" and loaded.schema_version == 16


def test_resume_matches_and_rejects(tmp_path: Path) -> None:
    ck = _ckpt()
    # Matching manifest/db -> OK.
    verify_resume(ck, manifest_hash="H", provider="balldontlie", league="nba",
                  date_range="2026-01-05", families=("games",), plan_version="f1a-plan-v1",
                  scratch_fingerprint="FP")
    # Changed plan/manifest -> rejected.
    with pytest.raises(CheckpointError):
        verify_resume(ck, manifest_hash="DIFFERENT", provider="balldontlie", league="nba",
                      date_range="2026-01-05", families=("games",), plan_version="f1a-plan-v1",
                      scratch_fingerprint="FP")
    # Changed database fingerprint -> rejected.
    with pytest.raises(CheckpointError):
        verify_resume(ck, manifest_hash="H", provider="balldontlie", league="nba",
                      date_range="2026-01-05", families=("games",), plan_version="f1a-plan-v1",
                      scratch_fingerprint="CHANGED")


def test_checkpoint_carries_no_secret_fields(tmp_path: Path) -> None:
    path = tmp_path / "p.ckpt"
    write_checkpoint(path, _ckpt(usage={"attempted_requests": 3}))
    blob = path.read_text(encoding="utf-8").lower()
    for token in ("api_key", "authorization", "bearer", "secret", "header", "body"):
        assert token not in blob
