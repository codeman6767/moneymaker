"""Offline merge of reviewed lineup-continuation evidence into a protected copy.

The merge exists because ``lineup_snapshots`` and ``lineup_players`` are hard
append-only, so a page-one snapshot cannot gain members in place. These tests pin
the consequence: one REVISION snapshot per affected ``(game, team)`` carrying page
one plus the reviewed additions, planned from the ORIGINAL page-one observation so
a replay collapses instead of stacking.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from sports_quant.cli import run_merge_nba_lineup_continuation
from sports_quant.db.init import initialize_database
from sports_quant.ingest.lineup_merge import (
    DQ_MERGE_PROVENANCE,
    MERGE_CONTRACT_VERSION,
    LineupMergeError,
    apply_merge,
    plan_merge,
    player_identity,
)

PROVIDER = "balldontlie"
P1_AT = "2026-03-02T00:00:00.000000Z"
CONT_AT = "2026-08-06T07:35:44.000000Z"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def _row(pid: int, tid: int, game: int, *, starter: bool = False, pos: str = "G",
         row_id: Optional[int] = None) -> dict[str, Any]:
    return {"id": row_id if row_id is not None else 5_000_000 + pid, "game_id": game,
            "position": pos, "starter": starter,
            "player": {"id": pid, "first_name": "A", "last_name": str(pid)},
            "team": {"id": tid, "full_name": f"T{tid}"}}


def _conn(path: Path) -> sqlite3.Connection:
    initialize_database(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _raw(conn: sqlite3.Connection, rid: str, params: dict, body: dict, *,
         run_id: str, at: str = P1_AT) -> str:
    blob = json.dumps(body, sort_keys=True)
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_method, http_status, response_headers_json, content_type, "
        "requested_at, received_at, elapsed_ns, body, body_bytes, body_hash, content_hash, "
        "created_at) VALUES (?, ?, ?, '/v1/lineups', ?, 'GET', 200, '{}', "
        "'application/json', ?, ?, 1, ?, ?, ?, ?, ?)",
        (rid, run_id, PROVIDER, json.dumps(params, sort_keys=True), at, at, blob,
         len(blob), f"h_{rid}", f"c_{rid}", at))
    return rid


def _run(conn: sqlite3.Connection, run_id: str) -> str:
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, sport, operation, args_json, "
        "status, requested_at, started_at, started_monotonic_ns, completed_at, requests_made, "
        "tool_version, created_at) VALUES (?, 'nba-lineup-continuation', ?, 'basketball', "
        "'lineup_continuation', '{}', 'succeeded', ?, ?, 1, ?, 1, 'test', ?)",
        (run_id, PROVIDER, CONT_AT, CONT_AT, CONT_AT, CONT_AT))
    return run_id


def _game_ref(conn: sqlite3.Connection, ref: str, game: str, raw_id: str) -> str:
    conn.execute(
        "INSERT INTO provider_game_references (reference_id, provider, provider_game_id, "
        "first_raw_response_id, current_raw_response_id, current_raw_response_hash, "
        "first_observed_at, last_observed_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'h', ?, ?, ?, ?)",
        (ref, PROVIDER, game, raw_id, raw_id, P1_AT, P1_AT, P1_AT, P1_AT))
    return ref


def _snapshot(conn: sqlite3.Connection, lid: str, ref: str, game: str, team: str,
              players: list[tuple[int, str, str, bool]], raw_id: str) -> None:
    conn.execute(
        "INSERT INTO lineup_snapshots (lineup_id, game_ref_id, provider, provider_game_id, "
        "provider_team_id, team_id, home_away, is_confirmed, player_count, observed_at, "
        "ingested_at, run_id, raw_response_id, raw_response_hash, content_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (lid, ref, PROVIDER, game, team, len(players), P1_AT, P1_AT, raw_id,
         f"h_{raw_id}", f"content_{lid}", P1_AT))
    for order, pid, pos, starter in players:
        conn.execute(
            "INSERT INTO lineup_players (lineup_player_id, lineup_id, batting_order, "
            "provider_player_id, player_id, position, is_starter, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            (f"lnp_{lid}_{order}", lid, order, pid, pos, 1 if starter else 0, P1_AT))


def build_source(path: Path, *, game: str = "900", next_cursor: int = 77) -> sqlite3.Connection:
    """A one-game corpus whose page one is full and advertises a cursor."""

    conn = _conn(path)
    month_run = _run(conn, "run_month1")
    rows = [_row(100 + i, 1 if i < 5 else 2, int(game), starter=i % 5 == 0) for i in range(10)]
    rid = _raw(conn, "raw_p1", {"game_ids[]": [game], "per_page": "25"},
               {"data": rows, "meta": {"next_cursor": next_cursor, "per_page": 25}},
               run_id=month_run)
    ref = _game_ref(conn, "pgr_dest1", game, rid)
    for team, members in ((("1"), [(i + 1, str(100 + i), "G", i % 5 == 0) for i in range(5)]),
                          (("2"), [(i + 1, str(105 + i), "F", i == 0) for i in range(5)])):
        _snapshot(conn, f"lns_{team}", ref, game, team, members, rid)
    conn.commit()
    return conn


def build_recovery(path: Path, *, game: str = "900", cursor: int = 77,
                   rows: Optional[list[dict]] = None) -> sqlite3.Connection:
    conn = _conn(path)
    run_id = _run(conn, "run_rec1")
    body = {"data": rows if rows is not None else [_row(200, 1, int(game)),
                                                   _row(201, 2, int(game))],
            "meta": {"prev_cursor": cursor, "per_page": 25}}
    _raw(conn, "raw_cont", {"game_ids[]": [game], "per_page": "25", "cursor": str(cursor)},
         body, run_id=run_id, at=CONT_AT)
    conn.commit()
    return conn


def build_destination(path: Path, source: Path) -> sqlite3.Connection:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(path))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture()
def corpora(tmp_path: Path):
    sp, rp, dp = tmp_path / "src.db", tmp_path / "rec.db", tmp_path / "dest.db"
    src = build_source(sp)
    rec = build_recovery(rp)
    src.close()
    rec.close()
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    yield {"src": src, "rec": rec, "dest": dest, "sp": sp, "rp": rp, "dp": dp}
    for c in (src, rec, dest):
        c.close()


def _plan(c, **kw):
    return plan_merge(source=c["src"], recovery=c["rec"], destination=c["dest"],
                      expected_targets=kw.pop("expected_targets", 1), **kw)


def _apply(c, plan, dry: bool = False):
    with c["dest"]:
        return apply_merge(destination=c["dest"], recovery=c["rec"], plan=plan,
                           provenance={"merge_contract_version": MERGE_CONTRACT_VERSION},
                           dry_run=dry)


# --------------------------------------------------------------------------- #
# binding, eligibility, dry run
# --------------------------------------------------------------------------- #

def test_plan_binds_page_one_to_the_requested_cursor(corpora) -> None:
    plan = _plan(corpora)
    assert plan.targets == 1
    assert plan.outcomes[0].requested_cursor == 77
    assert plan.outcomes[0].page_one_rows == 10


def test_plan_refuses_a_cursor_that_does_not_match_page_one(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp, next_cursor=77).close()
    build_recovery(rp, cursor=999).close()          # a cursor page one never issued
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    with pytest.raises(LineupMergeError, match="does not match the page-one"):
        plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)


def test_plan_refuses_an_unterminated_chain(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    conn = _conn(rp)
    run_id = _run(conn, "run_rec1")
    _raw(conn, "raw_cont", {"game_ids[]": ["900"], "per_page": "25", "cursor": "77"},
         {"data": [_row(200, 1, 900)], "meta": {"next_cursor": 78}}, run_id=run_id, at=CONT_AT)
    conn.commit()
    conn.close()
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    with pytest.raises(LineupMergeError, match="did not terminate"):
        plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)


def test_plan_refuses_a_target_count_the_review_did_not_accept(corpora) -> None:
    with pytest.raises(LineupMergeError, match="expected 40"):
        _plan(corpora, expected_targets=40)


def test_dry_run_writes_nothing(corpora) -> None:
    before = corpora["dest"].execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0]
    report = apply_merge(destination=corpora["dest"], recovery=corpora["rec"],
                         plan=_plan(corpora), provenance={}, dry_run=True)
    after = corpora["dest"].execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0]
    assert report.dry_run is True
    assert after == before
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0] == 2
    assert report.network_occurred is False


# --------------------------------------------------------------------------- #
# merge semantics
# --------------------------------------------------------------------------- #

def test_merge_appends_one_revision_per_affected_team(corpora) -> None:
    plan = _plan(corpora)
    report = _apply(corpora, plan)
    assert report.snapshots_appended == 2            # one per affected team
    # each revision restates page one (5) plus its single addition
    assert report.player_rows_appended == 12
    assert report.recovered_observations == 2


def test_page_one_rows_and_provenance_survive_the_merge(corpora) -> None:
    _apply(corpora, _plan(corpora))
    row = corpora["dest"].execute(
        "SELECT observed_at, raw_response_id, player_count FROM lineup_snapshots "
        "WHERE lineup_id = 'lns_1'").fetchone()
    assert row["observed_at"] == P1_AT
    assert row["raw_response_id"] == "raw_p1"
    assert row["player_count"] == 5                  # untouched, still page one


def test_the_two_acquisition_times_stay_distinct(corpora) -> None:
    _apply(corpora, _plan(corpora))
    times = {r[0] for r in corpora["dest"].execute(
        "SELECT DISTINCT observed_at FROM lineup_snapshots")}
    assert times == {P1_AT, CONT_AT}, "the merge must not flatten the two stages"


def test_latest_observation_carries_page_one_plus_continuation(corpora) -> None:
    _apply(corpora, _plan(corpora))
    latest = corpora["dest"].execute(
        "SELECT lineup_id FROM lineup_snapshots WHERE provider_team_id = '1' "
        "ORDER BY observed_at DESC LIMIT 1").fetchone()["lineup_id"]
    members = {r["provider_player_id"] for r in corpora["dest"].execute(
        "SELECT provider_player_id FROM lineup_players WHERE lineup_id = ?", (latest,))}
    assert members == {"100", "101", "102", "103", "104", "200"}


def test_empty_continuation_page_adds_nothing_but_is_still_recorded(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    build_recovery(rp, rows=[]).close()
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    plan = plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    assert plan.outcomes[0].empty_continuation_page is True
    assert plan.new_snapshots == 0
    with dest:
        report = apply_merge(destination=dest, recovery=rec, plan=plan, provenance={},
                             dry_run=False)
    assert report.snapshots_appended == 0
    assert report.provenance_rows == 1               # the game is still accounted for
    src.close()
    rec.close()
    dest.close()


def test_a_row_already_on_page_one_never_replaces_it(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    # player 100 is already on page one with identical content -> collapses
    build_recovery(rp, rows=[_row(100, 1, 900, starter=True, pos="G")]).close()
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    plan = plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    assert plan.new_snapshots == 0, "an identical overlap must be a no-op"
    src.close()
    rec.close()
    dest.close()


def test_contradictory_overlap_fails_closed(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    # player 100 is a starter at G on page one; the continuation disagrees
    build_recovery(rp, rows=[_row(100, 1, 900, starter=False, pos="C")]).close()
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    with pytest.raises(LineupMergeError, match="contradictory"):
        plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    src.close()
    rec.close()
    dest.close()


def test_player_on_opposing_teams_fails_closed(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    build_recovery(rp, rows=[_row(100, 2, 900)]).close()   # 100 is a team-1 player
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    with pytest.raises(LineupMergeError, match="opposing teams"):
        plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    src.close()
    rec.close()
    dest.close()


def test_wrong_game_row_fails_closed(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    build_recovery(rp, rows=[_row(200, 1, 111)]).close()
    dest = build_destination(dp, sp)
    src = sqlite3.connect(f"file:{sp}?mode=ro", uri=True)
    rec = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
    for c in (src, rec):
        c.row_factory = sqlite3.Row
    with pytest.raises(LineupMergeError, match="wrong-game"):
        plan_merge(source=src, recovery=rec, destination=dest, expected_targets=1)
    src.close()
    rec.close()
    dest.close()


def test_two_team_and_starter_invariants_hold_after_merge(corpora) -> None:
    _apply(corpora, _plan(corpora))
    teams = {r["provider_team_id"] for r in corpora["dest"].execute(
        "SELECT DISTINCT provider_team_id FROM lineup_snapshots")}
    assert teams == {"1", "2"}
    starters = 0
    for team in sorted(teams):
        latest = corpora["dest"].execute(
            "SELECT lineup_id FROM lineup_snapshots WHERE provider_team_id = ? "
            "ORDER BY observed_at DESC LIMIT 1", (team,)).fetchone()["lineup_id"]
        starters += corpora["dest"].execute(
            "SELECT COUNT(*) FROM lineup_players WHERE lineup_id = ? AND is_starter = 1",
            (latest,)).fetchone()[0]
    assert starters == 2                             # this fixture marks one per side


def test_merge_never_upgrades_is_confirmed(corpora) -> None:
    _apply(corpora, _plan(corpora))
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM lineup_snapshots WHERE is_confirmed <> 0").fetchone()[0] == 0


def test_merge_invents_no_canonical_ids(corpora) -> None:
    _apply(corpora, _plan(corpora))
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM lineup_snapshots WHERE team_id IS NOT NULL").fetchone()[0] == 0
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM lineup_players WHERE player_id IS NOT NULL").fetchone()[0] == 0
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM entity_match_decisions").fetchone()[0] == 0


def test_recovery_raw_response_is_traceable_from_the_merged_copy(corpora) -> None:
    _apply(corpora, _plan(corpora))
    row = corpora["dest"].execute(
        "SELECT raw_response_id, run_id FROM lineup_snapshots WHERE observed_at = ? LIMIT 1",
        (CONT_AT,)).fetchone()
    assert row["raw_response_id"] == "raw_cont"
    assert corpora["dest"].execute(
        "SELECT 1 FROM raw_responses WHERE raw_response_id = 'raw_cont'").fetchone()
    assert corpora["dest"].execute(
        "SELECT 1 FROM ingestion_runs WHERE run_id = ?", (row["run_id"],)).fetchone()


def test_merge_provenance_row_is_recorded_and_carries_no_secret(corpora) -> None:
    _apply(corpora, _plan(corpora))
    rows = corpora["dest"].execute(
        "SELECT severity, detail_json, description FROM data_quality_issues "
        "WHERE rule_code = ?", (DQ_MERGE_PROVENANCE,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["severity"] == "note"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["network_occurred"] is False
    assert detail["contract_version"] == MERGE_CONTRACT_VERSION
    blob = (rows[0]["description"] + rows[0]["detail_json"]).lower()
    for secret in ("api_key", "authorization", "bearer", "x-api-key", "token", "secret"):
        assert secret not in blob


# --------------------------------------------------------------------------- #
# idempotency, determinism, immutability
# --------------------------------------------------------------------------- #

def test_second_merge_is_a_no_op(corpora) -> None:
    """The revision must be planned from PAGE ONE, never from the latest row.

    Planning from the latest observation would fold the continuation rows into the
    base of the next revision and stack them on every replay.
    """

    _apply(corpora, _plan(corpora))
    snaps = corpora["dest"].execute("SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0]
    players = corpora["dest"].execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0]
    report = _apply(corpora, _plan(corpora))
    assert report.snapshots_appended == 0
    assert report.snapshots_unchanged == 2
    assert report.player_rows_appended == 0
    assert report.provenance_rows == 0
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0] == snaps
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM lineup_players").fetchone()[0] == players


def test_digest_is_stable_across_replays(corpora) -> None:
    first = _plan(corpora).digest()
    _apply(corpora, _plan(corpora))
    assert _plan(corpora).digest() == first


def test_digest_is_independent_of_plan_ordering(corpora) -> None:
    plan = _plan(corpora)
    digest = plan.digest()
    plan.revisions.reverse()
    plan.outcomes.reverse()
    for revision in plan.revisions:
        revision.players.reverse()
    assert plan.digest() == digest


def test_source_and_recovery_are_never_written(corpora) -> None:
    before_src = corpora["src"].execute(
        "SELECT COUNT(*) FROM lineup_players").fetchone()[0]
    before_rec = corpora["rec"].execute(
        "SELECT COUNT(*) FROM raw_responses").fetchone()[0]
    _apply(corpora, _plan(corpora))
    assert corpora["src"].execute(
        "SELECT COUNT(*) FROM lineup_players").fetchone()[0] == before_src
    assert corpora["rec"].execute(
        "SELECT COUNT(*) FROM raw_responses").fetchone()[0] == before_rec
    with pytest.raises(sqlite3.OperationalError):
        corpora["src"].execute("DELETE FROM lineup_players")   # opened read-only


def test_unrelated_tables_are_untouched(corpora) -> None:
    before = corpora["dest"].execute("SELECT COUNT(*) FROM nba_game_results").fetchone()[0]
    _apply(corpora, _plan(corpora))
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM nba_game_results").fetchone()[0] == before


def test_atomic_rollback_leaves_no_partial_merge(corpora, monkeypatch) -> None:
    import sports_quant.ingest.lineup_merge as module

    before = corpora["dest"].execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0]
    real = module.SqliteLineupRepository.append
    calls = {"n": 0}

    def exploding(self, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected mid-merge failure")
        return real(self, **kw)

    monkeypatch.setattr(module.SqliteLineupRepository, "append", exploding)
    with pytest.raises(RuntimeError, match="injected"):
        _apply(corpora, _plan(corpora))
    monkeypatch.setattr(module.SqliteLineupRepository, "append", real)
    assert corpora["dest"].execute(
        "SELECT COUNT(*) FROM lineup_players").fetchone()[0] == before
    # and the retry still closes exactly
    report = _apply(corpora, _plan(corpora))
    assert report.snapshots_appended == 2


# --------------------------------------------------------------------------- #
# CLI refusals
# --------------------------------------------------------------------------- #

def _cli(**kw) -> tuple[int, dict]:
    lines: list[Any] = []
    rc = run_merge_nba_lineup_continuation(out=lines.append, as_json=True, **kw)
    return rc, json.loads("\n".join(str(x) for x in lines))


def test_cli_requires_an_explicit_offline_acknowledgement(tmp_path: Path) -> None:
    sp, rp = tmp_path / "s.db", tmp_path / "r.db"
    build_source(sp).close()
    build_recovery(rp).close()
    rc, body = _cli(source_db=sp, recovery_db=rp, destination_db=tmp_path / "d.db",
                    manifest_path=tmp_path / "m.json", offline_ack=False)
    assert rc == 2
    assert body["refused"] is True
    assert "offline" in body["reason"]


def test_cli_refuses_a_destination_that_aliases_the_source(tmp_path: Path) -> None:
    sp, rp = tmp_path / "s.db", tmp_path / "r.db"
    build_source(sp).close()
    build_recovery(rp).close()
    rc, body = _cli(source_db=sp, recovery_db=rp, destination_db=sp,
                    manifest_path=tmp_path / "m.json", offline_ack=True)
    assert rc == 2
    assert "aliases the protected source" in body["reason"]


def test_cli_refuses_a_destination_that_aliases_the_recovery(tmp_path: Path) -> None:
    sp, rp = tmp_path / "s.db", tmp_path / "r.db"
    build_source(sp).close()
    build_recovery(rp).close()
    rc, body = _cli(source_db=sp, recovery_db=rp, destination_db=rp,
                    manifest_path=tmp_path / "m.json", offline_ack=True)
    assert rc == 2
    assert "aliases the protected recovery" in body["reason"]


def test_cli_refuses_a_missing_destination_without_create(tmp_path: Path) -> None:
    sp, rp = tmp_path / "s.db", tmp_path / "r.db"
    build_source(sp).close()
    build_recovery(rp).close()
    rc, body = _cli(source_db=sp, recovery_db=rp, destination_db=tmp_path / "nope.db",
                    manifest_path=tmp_path / "m.json", offline_ack=True)
    assert rc == 2
    assert "--create-destination" in body["reason"]


def test_cli_refuses_to_recreate_an_existing_destination(tmp_path: Path) -> None:
    sp, rp, dp = tmp_path / "s.db", tmp_path / "r.db", tmp_path / "d.db"
    build_source(sp).close()
    build_recovery(rp).close()
    build_destination(dp, sp).close()
    rc, body = _cli(source_db=sp, recovery_db=rp, destination_db=dp,
                    manifest_path=tmp_path / "m.json", offline_ack=True,
                    create_destination=True)
    assert rc == 2
    assert "already exists" in body["reason"]


def test_cli_refuses_a_missing_source(tmp_path: Path) -> None:
    rp = tmp_path / "r.db"
    build_recovery(rp).close()
    rc, body = _cli(source_db=tmp_path / "absent.db", recovery_db=rp,
                    destination_db=tmp_path / "d.db", manifest_path=tmp_path / "m.json",
                    offline_ack=True)
    assert rc == 2
    assert "source database not found" in body["reason"]


def test_player_identity_requires_both_ids() -> None:
    assert player_identity(_row(1, 2, 3)) == ("2", "1")
    assert player_identity({"player": {"id": 1}}) is None
    assert player_identity({"team": {"id": 2}, "player": {}}) is None
