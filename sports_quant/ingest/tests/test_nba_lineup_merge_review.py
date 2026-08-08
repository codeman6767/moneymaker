"""Independent review of the offline lineup-continuation merge.

The defect pinned first here is the revision ANCHOR. The merge must rebase each
affected ``(game, team)`` on the snapshot that page one actually produced, and it
must identify that snapshot by PROVENANCE -- the page-one raw response -- not by
position in the observation order. Picking the "earliest" observation is only
accidentally right: any earlier observation at the same anchor silently becomes
the base, and the merged lineup then loses the real page-one members.

The remaining tests pin observation-time and as-of behaviour, so a later change
cannot make August-retrieved evidence visible at a March pregame cutoff.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_quant.ingest.lineup_merge import LineupMergeError, apply_merge, plan_merge
from sports_quant.pit.asof import latest_as_of
from sports_quant.pit.models import Cutoff

from .test_nba_lineup_merge import (
    CONT_AT,
    P1_AT,
    build_destination,
    build_recovery,
    build_source,
)

GAME = "900"
EARLY_AT = "2026-03-01T12:00:00.000000Z"
LATER_AT = "2026-09-01T00:00:00.000000Z"


def _open(sp: Path, rp: Path, dp: Path):
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    return src, rec, dest


def _corpora(tmp_path: Path):
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    build_recovery(rp).close()
    return _open(sp, rp, dp)


def _inject_snapshot(dest: sqlite3.Connection, *, lineup_id: str, observed_at: str,
                     players: list[tuple[int, str]], team: str = "1") -> None:
    """An extra observation at an affected anchor, unrelated to page one."""

    base = dest.execute(
        "SELECT * FROM lineup_snapshots WHERE provider_game_id = ? AND provider_team_id = ? "
        "ORDER BY observed_at LIMIT 1", (GAME, team)).fetchone()
    # A genuinely unrelated observation comes from its OWN response, not page one's.
    own = f"raw_{lineup_id}"
    run_id = dest.execute("SELECT run_id FROM ingestion_runs LIMIT 1").fetchone()[0]
    dest.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_method, http_status, response_headers_json, content_type, "
        "requested_at, received_at, elapsed_ns, body, body_bytes, body_hash, content_hash, "
        "created_at) VALUES (?, ?, 'balldontlie', '/v1/lineups', '{}', 'GET', 200, '{}', "
        "'application/json', ?, ?, 1, '{}', 2, ?, ?, ?)",
        (own, run_id, observed_at, observed_at, f"bh_{own}", f"ch_{own}", observed_at))
    dest.execute(
        "INSERT INTO lineup_snapshots (lineup_id, game_ref_id, provider, provider_game_id, "
        "provider_team_id, team_id, home_away, is_confirmed, player_count, observed_at, "
        "ingested_at, run_id, raw_response_id, raw_response_hash, content_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lineup_id, base["game_ref_id"], base["provider"], GAME, team, len(players),
         observed_at, observed_at, base["run_id"], own,
         f"bh_{own}", f"hash_{lineup_id}", observed_at))
    for order, pid in players:
        dest.execute(
            "INSERT INTO lineup_players (lineup_player_id, lineup_id, batting_order, "
            "provider_player_id, player_id, position, is_starter, created_at) "
            "VALUES (?, ?, ?, ?, NULL, 'G', 0, ?)",
            (f"lnp_{lineup_id}_{order}", lineup_id, order, pid, observed_at))
    dest.commit()


def _revision(plan, team: str = "1"):
    return next(r for r in plan.revisions
                if r.provider_game_id == GAME and r.provider_team_id == team)


# --------------------------------------------------------------------------- #
# Defect -- the revision base must be page one, identified by provenance
# --------------------------------------------------------------------------- #

def test_earlier_unrelated_observation_must_not_become_the_revision_base(
        tmp_path: Path) -> None:
    """The reproducer for the anchor defect.

    An observation recorded BEFORE page one at the same ``(game, team)`` is not
    page one. Rebasing on it drops the real page-one members from the merged
    lineup, which is silent data loss rather than a visible failure.
    """

    src, rec, dest = _corpora(tmp_path)
    page_one_members = {
        str(r["provider_player_id"]) for r in dest.execute(
            "SELECT p.provider_player_id FROM lineup_players p JOIN lineup_snapshots s "
            "ON p.lineup_id = s.lineup_id WHERE s.provider_game_id = ? "
            "AND s.provider_team_id = '1'", (GAME,))}
    assert len(page_one_members) == 5

    _inject_snapshot(dest, lineup_id="lns_earlier", observed_at=EARLY_AT,
                     players=[(1, "999001"), (2, "999002")])

    revision = _revision(plan_merge(source=src, recovery=rec, destination=dest,
                                    expected_targets=1))
    planned = {p.provider_player_id for p in revision.players}
    assert page_one_members <= planned, (
        "the real page-one members were dropped from the revision")
    assert not ({"999001", "999002"} & planned), (
        "an unrelated earlier observation leaked into the revision base")
    assert revision.page_one_count == 5


def test_later_unrelated_revision_does_not_change_the_plan(tmp_path: Path) -> None:
    src, rec, dest = _corpora(tmp_path)
    before = _revision(plan_merge(source=src, recovery=rec, destination=dest,
                                  expected_targets=1))
    _inject_snapshot(dest, lineup_id="lns_later", observed_at=LATER_AT,
                     players=[(1, "888001")])
    after = _revision(plan_merge(source=src, recovery=rec, destination=dest,
                                 expected_targets=1))
    assert [p.provider_player_id for p in after.players] == \
           [p.provider_player_id for p in before.players]
    assert after.page_one_count == before.page_one_count


def test_plan_is_unchanged_by_an_already_applied_merge(tmp_path: Path) -> None:
    src, rec, dest = _corpora(tmp_path)
    before = plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    digest = before.digest()
    with dest:
        apply_merge(destination=dest, recovery=rec, plan=before, provenance={},
                    dry_run=False)
    after = plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    assert after.digest() == digest
    assert _revision(after).page_one_count == _revision(before).page_one_count


def test_plan_does_not_depend_on_snapshot_insertion_order(tmp_path: Path) -> None:
    """Two destinations holding the same rows inserted in opposite order agree."""

    src, rec, dest_a = _corpora(tmp_path)
    digest_a = plan_merge(source=src, recovery=rec, destination=dest_a,
                          expected_targets=1).digest()
    _inject_snapshot(dest_a, lineup_id="lns_x", observed_at=LATER_AT, players=[(1, "777")])
    _inject_snapshot(dest_a, lineup_id="lns_y", observed_at=EARLY_AT, players=[(1, "778")])
    digest_b = plan_merge(source=src, recovery=rec, destination=dest_a,
                          expected_targets=1).digest()
    assert digest_a == digest_b


def test_snapshot_not_produced_by_page_one_fails_closed(tmp_path: Path) -> None:
    """A snapshot that did not come from the page-one response is not page one.

    Built that way from the start rather than by mutating an append-only table:
    the team-1 snapshot references a different (real) lineups response, so the
    merge has no page-one base and must refuse instead of guessing one.
    """

    from .test_nba_lineup_merge import _conn, _game_ref, _raw, _row, _run, _snapshot

    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    conn = _conn(sp)
    run = _run(conn, "run_month1")
    rows = [_row(100 + i, 1 if i < 5 else 2, int(GAME), starter=i % 5 == 0) for i in range(10)]
    p1 = _raw(conn, "raw_p1", {"game_ids[]": [GAME], "per_page": "25"},
              {"data": rows, "meta": {"next_cursor": 77, "per_page": 25}}, run_id=run)
    other = _raw(conn, "raw_other", {"game_ids[]": [GAME], "per_page": "25", "cursor": "5"},
                 {"data": [], "meta": {}}, run_id=run)
    ref = _game_ref(conn, "pgr_dest1", GAME, p1)
    _snapshot(conn, "lns_1", ref, GAME, "1",
              [(i + 1, str(100 + i), "G", i % 5 == 0) for i in range(5)], other)
    _snapshot(conn, "lns_2", ref, GAME, "2",
              [(i + 1, str(105 + i), "F", i == 0) for i in range(5)], p1)
    conn.commit()
    conn.close()
    build_recovery(rp).close()
    src, rec, dest = _open(sp, rp, dp)
    with pytest.raises(LineupMergeError, match="page-one"):
        plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)


# --------------------------------------------------------------------------- #
# Observation time and as-of behaviour
# --------------------------------------------------------------------------- #

def _anchor(dest: sqlite3.Connection, team: str = "1") -> tuple[str, str]:
    row = dest.execute(
        "SELECT game_ref_id FROM lineup_snapshots WHERE provider_game_id = ? LIMIT 1",
        (GAME,)).fetchone()
    return str(row["game_ref_id"]), team


def _as_of(dest: sqlite3.Connection, ref: str, team: str, cutoff: str):
    row = latest_as_of(dest, table="lineup_snapshots", cutoff=Cutoff.parse(cutoff),
                       anchor_where="game_ref_id = ? AND provider_team_id = ?",
                       anchor_params=(ref, team))
    if row is None:
        return None
    return str(row["observed_at"]), dest.execute(
        "SELECT COUNT(*) FROM lineup_players WHERE lineup_id = ?",
        (row["lineup_id"],)).fetchone()[0]


def test_revision_uses_the_continuation_observation_time(tmp_path: Path) -> None:
    src, rec, dest = _corpora(tmp_path)
    plan = plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    revision = _revision(plan)
    assert revision.observed_at == CONT_AT
    assert revision.observed_at != P1_AT
    assert not revision.observed_at.startswith("2026-03")


def test_continuation_is_invisible_before_it_was_observed(tmp_path: Path) -> None:
    """The merge must never backdate evidence into a pregame cutoff."""

    src, rec, dest = _corpora(tmp_path)
    ref, team = _anchor(dest)
    with dest:
        apply_merge(destination=dest, recovery=rec,
                    plan=plan_merge(source=src, recovery=rec, destination=dest,
                                    expected_targets=1),
                    provenance={}, dry_run=False)
    assert _as_of(dest, ref, team, "2026-03-01T18:00:00.000000Z") is None
    between = _as_of(dest, ref, team, "2026-08-05T00:00:00.000000Z")
    assert between is not None and between[0] == P1_AT and between[1] == 5
    after = _as_of(dest, ref, team, "2026-09-01T00:00:00.000000Z")
    assert after is not None and after[0] == CONT_AT and after[1] == 6


def test_page_one_snapshot_remains_queryable_after_the_merge(tmp_path: Path) -> None:
    src, rec, dest = _corpora(tmp_path)
    before = dest.execute(
        "SELECT lineup_id, observed_at, content_hash FROM lineup_snapshots "
        "WHERE provider_team_id = '1'").fetchone()
    with dest:
        apply_merge(destination=dest, recovery=rec,
                    plan=plan_merge(source=src, recovery=rec, destination=dest,
                                    expected_targets=1),
                    provenance={}, dry_run=False)
    after = dest.execute(
        "SELECT lineup_id, observed_at, content_hash FROM lineup_snapshots "
        "WHERE lineup_id = ?", (before["lineup_id"],)).fetchone()
    assert tuple(after) == tuple(before)


def test_merge_does_not_reduce_raw_snapshot_rows_to_two_per_game(tmp_path: Path) -> None:
    """Raw snapshot rows and logical lineups are different quantities."""

    src, rec, dest = _corpora(tmp_path)
    with dest:
        apply_merge(destination=dest, recovery=rec,
                    plan=plan_merge(source=src, recovery=rec, destination=dest,
                                    expected_targets=1),
                    provenance={}, dry_run=False)
    rows = dest.execute("SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0]
    anchors = dest.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT game_ref_id, provider_team_id "
        "FROM lineup_snapshots)").fetchone()[0]
    assert rows == 4 and anchors == 2, "two page-one rows plus two revisions"
