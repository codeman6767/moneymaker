"""Phase E2 independent-audit regressions (RF5-RF9): WAL completeness, open-DQ
validity/exit, pending-review lifecycle, status determinism, CLI validation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.ids import new_match_decision_id
from sports_quant.quality.runner import run_data_quality
from sports_quant.report_access import pending_review_counts
from sports_quant.status import build_status_report, run_data_status

from .conftest import T2, Ctx, seed_dq


# --------------------------------------------------------------------------- #
# RF5 -- uncheckpointed WAL must fail closed (never silently read stale corpus)
# --------------------------------------------------------------------------- #
def test_uncheckpointed_wal_fails_closed(db_path: Path) -> None:
    conn = Database(db_path).connect()
    try:
        conn.execute("UPDATE leagues SET updated_at = '2026-07-24T00:00:00.000000Z'")  # -> WAL
        wal = Path(f"{db_path}-wal")
        assert wal.exists() and wal.stat().st_size > 0  # committed-but-uncheckpointed
        assert run_data_status(database_path=db_path, out=lambda _s: None) == 3
        assert run_data_quality(database_path=db_path, out=lambda _s: None) == 3
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# RF6 -- open DQ issues gate corpus validity + exit
# --------------------------------------------------------------------------- #
def _checkpoint(db_path: Path, seed) -> None:  # type: ignore[no-untyped-def]
    with Database(db_path).connection() as conn:
        seed(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_open_blocking_dq_blocks_validity_and_exit(db_path: Path) -> None:
    _checkpoint(db_path, lambda c: seed_dq(
        c, rule_code="DQ-MATCH-004", entity_type="kalshi_market", entity_id="km_x",
        severity="blocking", detected_at=T2))
    out: list[str] = []
    rc = run_data_quality(database_path=db_path, as_json=True, out=out.append)
    payload = json.loads(out[-1])
    assert rc == 1                                   # open blocking -> exit 1 (default fail-on)
    assert payload["execution_valid"] is True        # no E2 rule findings
    assert payload["corpus_valid"] is False          # ... but an open blocking issue exists
    assert payload["open_counts"]["blocking"] == 1


def test_open_note_dq_threshold_behavior(db_path: Path) -> None:
    _checkpoint(db_path, lambda c: seed_dq(
        c, rule_code="DQ-WX-PIT-001", entity_type="weather", entity_id="w1",
        severity="note", detected_at=T2))
    assert run_data_quality(database_path=db_path, out=lambda _s: None) == 0        # default blocking
    assert run_data_quality(database_path=db_path, fail_on="note", out=lambda _s: None) == 1
    # Rule filter applies to open issues too.
    out: list[str] = []
    run_data_quality(database_path=db_path, rule_code="NOPE", as_json=True, out=out.append)
    assert json.loads(out[-1])["open_data_quality_issues"] == []


# --------------------------------------------------------------------------- #
# RF7 -- pending manual review: latest-flagged-per-source only
# --------------------------------------------------------------------------- #
def _insert_decision(conn: sqlite3.Connection, *, source_ref: str, decided_at: str,
                     needs_review: bool, entity_type: str = "sportsbook_event",
                     provider: str = "the_odds_api") -> str:
    mid = new_match_decision_id()
    conn.execute(
        "INSERT INTO entity_match_decisions (match_id, entity_type, source_provider, source_ref, "
        "matched_entity_id, outcome, method, score, threshold, matcher_version, "
        "needs_manual_review, decided_at, created_at) VALUES "
        "(?, ?, ?, ?, 'gm_x', 'accepted', 'exact', 1.0, 0.85, 't', ?, ?, ?)",
        (mid, entity_type, provider, source_ref, 1 if needs_review else 0, decided_at, decided_at))
    conn.commit()
    return mid


def test_pending_review_counts_lifecycle(conn: sqlite3.Connection) -> None:
    # A single flagged decision is pending.
    _insert_decision(conn, source_ref="E1", decided_at="2026-01-01T00:00:00.000000Z",
                     needs_review=True)
    assert pending_review_counts(conn) == {"sportsbook_event": 1}
    # A NEWER decision for the SAME source that is NOT flagged supersedes it -> 0.
    _insert_decision(conn, source_ref="E1", decided_at="2026-02-01T00:00:00.000000Z",
                     needs_review=False)
    assert pending_review_counts(conn) == {}
    # A different source whose LATEST decision is flagged -> counted; the older
    # flagged decision for that source is superseded and not double-counted.
    _insert_decision(conn, source_ref="E2", decided_at="2026-01-01T00:00:00.000000Z",
                     needs_review=True)
    _insert_decision(conn, source_ref="E2", decided_at="2026-03-01T00:00:00.000000Z",
                     needs_review=True)
    assert pending_review_counts(conn) == {"sportsbook_event": 1}  # only the latest E2, once


def test_completed_review_not_pending(conn: sqlite3.Connection) -> None:
    from sports_quant.db.engine import transaction
    from sports_quant.db.repositories.matching import SqliteMatchingRepository
    mid = _insert_decision(conn, source_ref="E9", decided_at="2026-01-01T00:00:00.000000Z",
                           needs_review=True)
    assert pending_review_counts(conn) == {"sportsbook_event": 1}
    with transaction(conn):
        SqliteMatchingRepository(conn).mark_reviewed(mid, reviewed_by="alice")
    assert pending_review_counts(conn) == {}  # review completed -> not pending


# --------------------------------------------------------------------------- #
# RF8 -- provider_runs deterministic; equal-time conflict is explicit
# --------------------------------------------------------------------------- #
def _insert_run(conn: sqlite3.Connection, *, provider: str, started_at: str, status: str) -> None:
    from sports_quant.db.ids import new_ingestion_run_id
    completed = None if status == "started" else started_at
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, args_json, status, "
        "requested_at, started_at, completed_at, started_monotonic_ns, tool_version, created_at) "
        "VALUES (?, 'ingest', ?, 'op', '{}', ?, ?, ?, ?, 0, 't', ?)",
        (new_ingestion_run_id(), provider, status, started_at, started_at, completed, started_at))
    conn.commit()


def test_provider_runs_equal_time_conflict_is_ambiguous(conn: sqlite3.Connection) -> None:
    # Two runs share the max started_at with DIFFERENT statuses -> deterministic
    # "ambiguous(...)" rather than a generated run_id winner.
    _insert_run(conn, provider="mlb_statsapi", started_at="2026-05-01T00:00:00.000000Z",
                status="succeeded")
    _insert_run(conn, provider="mlb_statsapi", started_at="2026-05-01T00:00:00.000000Z",
                status="failed")
    r1 = build_status_report(conn)
    r2 = build_status_report(conn)
    assert r1["provider_runs"]["mlb_statsapi"]["status"] == "ambiguous(failed,succeeded)"
    assert r1 == r2  # deterministic across rebuilds


# --------------------------------------------------------------------------- #
# RF9 -- CLI scoping & validation
# --------------------------------------------------------------------------- #
def test_since_validation_rejects_bad_dates(db_path: Path) -> None:
    for bad in ("2026-13-01", "2026-02-30", "not-a-date", "2026/01/01", "2026-1-1"):
        with pytest.raises(ValueError):
            run_data_status(database_path=db_path, since=bad, out=lambda _s: None)
    assert run_data_status(database_path=db_path, since="2026-01-01", out=lambda _s: None) == 0


def test_cli_since_invalid_is_usage_error(db_path: Path) -> None:
    from sports_quant.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["data-status", "--db", str(db_path), "--since", "2026-13-40"])
    assert exc.value.code == 2  # argparse usage error


def test_status_league_filter_and_unattributable_notes(conn: sqlite3.Connection,
                                                        ctx: Ctx) -> None:
    from .conftest import seed_result
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    report = build_status_report(conn, league="nba")
    assert report["canonical_games"] == 0  # the only game is MLB
    # League-unattributable metrics carry an explicit note rather than a false zero.
    assert any("not league-attributable" in n or "no league" in n
               for n in report["scope_notes"])
