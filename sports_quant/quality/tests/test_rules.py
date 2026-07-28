"""Phase E2 corpus quality-rule tests (offline, deterministic, read-only)."""

from __future__ import annotations

import sqlite3

from sports_quant.quality import Severity, grade_findings
from sports_quant.quality.rules import open_dq_findings, run_rules

from .conftest import T2, Ctx, seed_dq, seed_nba_ctx, seed_nba_result, seed_result, seed_weather


def _codes(findings) -> set:  # type: ignore[no-untyped-def]
    return {f.rule_code for f in findings}


def test_clean_corpus_grades_A(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    findings = run_rules(conn)
    assert findings == []
    report = grade_findings(findings)
    assert report.grade == "A" and report.execution_valid and report.score == 1.0


def test_result_leak_is_blocking_and_fails_grade(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at="2026-07-10T00:00:00.000000Z",
                winning_side="home", mapped_status="final")  # before scheduled start
    findings = run_rules(conn)
    assert "DQ-PIT-001" in _codes(findings)
    blk = [f for f in findings if f.rule_code == "DQ-PIT-001"]
    assert blk[0].severity is Severity.BLOCKING and blk[0].league == "MLB"
    report = grade_findings(findings)
    assert report.grade == "F" and not report.execution_valid and report.score == 0.0


def test_equal_time_conflict_is_blocking(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="away")
    findings = run_rules(conn)
    conflict = [f for f in findings if f.rule_code == "DQ-PIT-008"]
    assert conflict and conflict[0].severity is Severity.BLOCKING
    assert conflict[0].entity_type == "game_result_snapshots"
    assert not grade_findings(findings).execution_valid


def test_weather_unknown_eligibility_is_issue(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    seed_weather(conn, game_ref_id=ctx.game_ref_id, venue_id=ctx.venue_id,
                 weather_kind="current_forecast", observed_at=T2, pit_eligible=None)
    findings = run_rules(conn)
    wx = [f for f in findings if f.rule_code == "DQ-PIT-011"]
    assert wx and wx[0].severity is Severity.ISSUE
    report = grade_findings(findings)
    assert report.execution_valid and report.grade == "B" and report.score == 0.9  # one issue


def test_unfinished_game_is_note(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side=None,
                mapped_status="in_progress")
    findings = run_rules(conn)
    note = [f for f in findings if f.rule_code == "E2-LABEL-UNAVAILABLE"]
    assert note and note[0].severity is Severity.NOTE
    # A note keeps the corpus PIT-valid (execution_valid) but caps below A.
    report = grade_findings(findings)
    assert report.execution_valid and report.grade == "A" and report.score == 0.98


def test_rule_and_league_filters(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at="2026-07-10T00:00:00.000000Z",
                winning_side="home")  # MLB leak
    nba = seed_nba_ctx(conn)
    seed_nba_result(conn, game_ref_id=nba.game_ref_id, observed_at="2026-07-10T00:00:00.000000Z",
                    winning_side="home")  # NBA leak
    only_001 = run_rules(conn, rule_code="DQ-PIT-001")
    assert _codes(only_001) == {"DQ-PIT-001"} and len(only_001) == 2
    mlb_only = run_rules(conn, league="mlb", rule_code="DQ-PIT-001")
    assert len(mlb_only) == 1 and mlb_only[0].league == "MLB"


def test_repeated_run_no_mutation_no_duplicates(conn: sqlite3.Connection, ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at="2026-07-10T00:00:00.000000Z",
                winning_side="home")
    dq_before = conn.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0]
    first = run_rules(conn)
    second = run_rules(conn)
    dq_after = conn.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0]
    assert [f.as_dict() for f in first] == [f.as_dict() for f in second]  # deterministic
    assert dq_before == dq_after  # rules NEVER write to data_quality_issues


def test_open_dq_issues_reported_separately_and_not_graded(conn: sqlite3.Connection,
                                                          ctx: Ctx) -> None:
    seed_result(conn, game_ref_id=ctx.game_ref_id, observed_at=T2, winning_side="home")
    seed_dq(conn, rule_code="DQ-MATCH-004", entity_type="kalshi_market", entity_id="km_x",
            severity="blocking", detected_at=T2)  # a pre-existing OPEN blocking issue
    e2 = run_rules(conn)                       # clean -> no E2 findings
    opened = open_dq_findings(conn)
    assert e2 == [] and grade_findings(e2).grade == "A"   # open DQ issue does NOT fail the E2 grade
    assert len(opened) == 1 and opened[0].source == "open_dq_issue"
    # Even mixed into the list, only e2_rule findings are graded.
    assert grade_findings(e2 + opened).execution_valid
