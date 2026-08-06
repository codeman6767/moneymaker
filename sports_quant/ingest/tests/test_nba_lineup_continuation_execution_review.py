"""Independent execution review of the live NBA March 2026 lineup continuation.

Two defects were proved from the preserved execution evidence and are pinned
here, each with the reproducer written before the repair:

* the human ``rate:`` line presented a provider maximum as a *verified* tier
  ceiling while the run's own tier evidence said ``configured_not_verified``;
* the continuation report gave an operator no way to see that 19 of 40
  continuation pages came back empty -- the count had to be recomputed by hand
  from the raw bodies.

The remaining tests pin the pagination semantics the live run depended on, so a
later change cannot quietly turn a legitimate terminal empty page into either a
silent loss or a spurious finding.
"""

from __future__ import annotations

import json

from ..f1a import _render_rate_line
from ..lineup_continuation import (
    DQ_EMPTY_PAGE_WITH_CURSOR,
    STOP_EXHAUSTED,
    ContinuationOutcome,
    ContinuationPage,
    ContinuationReport,
    lineup_row_content,
    merge_lineup_rows,
    render_report,
    semantic_lineup_key,
)

# --------------------------------------------------------------------------- #
# Defect 1 -- a provider maximum must never read as verified when it is not.
# --------------------------------------------------------------------------- #

def _usage(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "rate_policy_active": True,
        "rate_policy_basis": "verified_tier_max",
        "rate_policy_version": "bdl-rate-v1",
        "configured_rate_per_min": 60,
        "provider_rate_limit_per_min": 600,
        "rate_burst": 0,
        "rate_min_interval_seconds": 0.0,
        "throttle_events": 0,
        "throttle_wait_seconds": 0.0,
        "http_429s": 0,
        "rate_limited": False,
        "tier_verified": False,
        "tier_status": "configured_not_verified:goat",
        "tier_evidence_source": "none",
    }
    base.update(over)
    return base


def test_unverified_tier_max_is_not_presented_as_verified() -> None:
    """The exact reporting shape the live recovery emitted.

    ``basis=verified_tier_max`` sits directly beside ``provider_max=600/min``
    while the run's own tier evidence is ``configured_not_verified:goat`` with
    ``tier_evidence_source=none``. A reader of this one line must not come away
    believing the 600/min ceiling was verified for this account.
    """

    line = _render_rate_line(_usage())
    assert "provider_max=600/min" in line
    assert "TIER NOT VERIFIED" in line, line


def test_verified_tier_max_prints_no_caveat_once_the_tier_is_verified() -> None:
    line = _render_rate_line(_usage(
        tier_verified=True, tier_status="verified:goat",
        tier_evidence_source="bounded_capability_audit"))
    assert "provider_max=600/min" in line
    assert "TIER NOT VERIFIED" not in line


def test_courtesy_cap_wording_is_unchanged() -> None:
    """The MLB courtesy-cap disclaimer must not be disturbed by the repair."""

    line = _render_rate_line({
        "rate_policy_active": True,
        "rate_policy_basis": "project_courtesy_cap",
        "rate_policy_version": "mlb-pacing-v1",
        "configured_rate_per_min": 30,
        "provider_rate_limit_per_min": None,
        "rate_burst": 1,
        "rate_min_interval_seconds": 2.0,
        "throttle_events": 0,
        "throttle_wait_seconds": 0.0,
        "http_429s": 0,
        "rate_limited": False,
    })
    assert "PROJECT COURTESY CAP, not a provider limit" in line
    assert "provider_max=unknown" in line
    assert "TIER NOT VERIFIED" not in line


def test_no_tier_caveat_when_no_provider_max_is_claimed() -> None:
    line = _render_rate_line(_usage(provider_rate_limit_per_min=None))
    assert "provider_max=unknown" in line
    assert "TIER NOT VERIFIED" not in line


# --------------------------------------------------------------------------- #
# Defect 2 -- empty continuation pages must be visible in the report.
# --------------------------------------------------------------------------- #

def _outcome(game: str, rows: int, *, cursor: int = 100) -> ContinuationOutcome:
    out = ContinuationOutcome(provider_game_id=game, start_cursor=cursor)
    out.pages.append(ContinuationPage(
        provider_game_id=game, page_ordinal=1, requested_cursor=cursor,
        returned_cursor=None, rows=rows, observed_at="2026-03-01T00:00:00Z"))
    out.stop_reason = STOP_EXHAUSTED
    out.lineup_rows = rows
    out.players_added = rows
    return out


def _report() -> ContinuationReport:
    """The live shape in miniature: 3 targets, 2 of them empty-page."""

    outcomes = [_outcome("1", 2), _outcome("2", 0), _outcome("3", 0)]
    return ContinuationReport(
        targets=3, targets_completed=3, targets_incomplete=0,
        continuation_requests=3, pages_persisted=3, lineup_rows=2, findings=0,
        first_page_requests=0, outcomes=outcomes)


def test_report_dict_counts_empty_continuation_pages() -> None:
    body = _report().as_dict()
    assert body["empty_continuation_pages"] == 2
    assert body["nonempty_continuation_pages"] == 1
    assert (body["empty_continuation_pages"] + body["nonempty_continuation_pages"]
            == body["pages_persisted"])


def test_human_report_shows_the_empty_page_count() -> None:
    lines: list[str] = []
    render_report(_report(), lines.append)
    joined = "\n".join(lines)
    assert "empty_pages=2" in joined, joined


def test_empty_page_counts_are_page_level_not_target_level() -> None:
    """A target whose chain has one empty and one non-empty page counts once each."""

    out = ContinuationOutcome(provider_game_id="9", start_cursor=1)
    out.pages.append(ContinuationPage(provider_game_id="9", page_ordinal=1,
                                      requested_cursor=1, returned_cursor=2, rows=0,
                                      observed_at=""))
    out.pages.append(ContinuationPage(provider_game_id="9", page_ordinal=2,
                                      requested_cursor=2, returned_cursor=None, rows=5,
                                      observed_at=""))
    out.stop_reason = STOP_EXHAUSTED
    out.lineup_rows = 5
    report = ContinuationReport(targets=1, targets_completed=1, continuation_requests=2,
                                pages_persisted=2, lineup_rows=5, outcomes=[out])
    body = report.as_dict()
    assert body["empty_continuation_pages"] == 1
    assert body["nonempty_continuation_pages"] == 1


# --------------------------------------------------------------------------- #
# Pagination semantics the live run relied on.
# --------------------------------------------------------------------------- #

def test_terminal_empty_page_raises_no_finding() -> None:
    """A full page one, then an empty terminal page, is ordinary pagination.

    Nineteen of the forty live targets had exactly 25 lineup rows, so the
    provider advertised a cursor and the page behind it was legitimately empty.
    That must not be reported as an anomaly.
    """

    merged, conflicts, rejected = merge_lineup_rows([(1, [])])
    assert merged == {} and conflicts == [] and rejected == 0


def test_empty_page_that_advertises_more_is_still_a_finding() -> None:
    """R005 must keep firing for the genuinely anomalous shape."""

    assert DQ_EMPTY_PAGE_WITH_CURSOR == "DQ-NBA-LINEUP-R005"


def _row(pid: int, tid: int, *, starter: bool = False, pos: str = "G") -> dict:
    return {"id": 5_000_000 + pid, "game_id": 7, "position": pos, "starter": starter,
            "player": {"id": pid, "first_name": "A", "last_name": str(pid)},
            "team": {"id": tid, "full_name": f"T{tid}"}}


def test_continuation_rows_do_not_overlap_page_one() -> None:
    """Cursor pagination is non-overlapping: the live run saw zero overlap."""

    page_one = [_row(i, 1 if i % 2 else 2) for i in range(1, 26)]
    continuation = [_row(26, 1), _row(27, 2)]
    merged, conflicts, rejected = merge_lineup_rows([(1, page_one), (2, continuation)])
    assert len(merged) == 27
    assert conflicts == [] and rejected == 0
    assert len({k[0] for k in merged}) == 2


def test_merge_of_page_one_and_continuation_is_order_independent() -> None:
    page_one = [_row(i, 1 if i % 2 else 2) for i in range(1, 26)]
    continuation = [_row(26, 1), _row(27, 2)]
    a = merge_lineup_rows([(1, page_one), (2, continuation)])
    b = merge_lineup_rows([(2, list(reversed(continuation))),
                           (1, list(reversed(page_one)))])
    assert a == b


def test_row_without_team_or_player_identity_is_rejected_not_silently_dropped() -> None:
    rows = [_row(1, 1), {"id": 2, "game_id": 7, "player": {"id": 2}}]
    merged, _conflicts, rejected = merge_lineup_rows([(1, rows)])
    assert len(merged) == 1
    assert rejected == 1, "a row losing identity must be counted, never silently lost"


def test_semantic_key_and_content_are_stable_for_a_continuation_row() -> None:
    row = _row(11, 3, starter=True, pos="F")
    assert semantic_lineup_key(row) == ("3", "11")
    assert lineup_row_content(row) == {"position": "F", "starter": True}


def test_report_dict_is_json_serialisable_and_leaks_no_names() -> None:
    body = _report().as_dict()
    blob = json.dumps(body)
    assert "last_name" not in blob and "first_name" not in blob
