"""Offline replay of the NBA ``results`` family from preserved raw responses.

Every test here is fully offline. Fixtures are built by running the REAL
``ingest_nba`` against an ``httpx.MockTransport`` with ``results`` deliberately
NOT included -- which reproduces exactly the state the executed March 2026 month
run left behind (see ``F1_NBA_2026_03_EXECUTION_REVIEW.md`` §5) -- and the repair
is then driven through the real CLI handler.

The properties under test are the ones that make the repair safe to run against
a real corpus: it invents nothing, it refuses every ambiguity instead of picking
by insertion order, it preserves the source response's own observation time and
provenance, it touches nothing but the results table, and running it twice is a
no-op.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.f1a import emit_plan
from sports_quant.ingest.nba_ingestor import ingest_nba
from sports_quant.ingest.results_repair import (
    REPAIR_RULE_CODE,
    ResultsRepairError,
    repair_nba_results_from_raw,
)
from sports_quant.providers.balldontlie import BalldontlieClient

FROM_DATE, TO_DATE = "2026-03-01", "2026-03-31"
DATE_RANGE = f"{FROM_DATE}..{TO_DATE}"

#: A sentinel credential. No test may cause it to be read, stored or printed.
SENTINEL_KEY = "sk-results-repair-must-never-read-this"


# --------------------------------------------------------------------------- #
# Fixture construction: the real ingestor, results deliberately absent
# --------------------------------------------------------------------------- #
def game_payload(
    gid: int,
    *,
    home: int = 1,
    away: int = 2,
    home_score: Optional[int] = 110,
    away_score: Optional[int] = 104,
    status: str = "Final",
    period: int = 4,
    day: int = 2,
) -> dict[str, Any]:
    return {
        "id": gid,
        "date": f"2026-03-{day:02d}",
        "datetime": f"2026-03-{day:02d}T00:30:00Z",
        "season": 2025,
        "status": status,
        "period": period,
        "postseason": False,
        "home_team": {"id": home, "full_name": f"Home {home}", "abbreviation": "HOM",
                      "city": "Home", "name": f"T{home}"},
        "visitor_team": {"id": away, "full_name": f"Away {away}", "abbreviation": "AWY",
                         "city": "Away", "name": f"T{away}"},
        "home_team_score": home_score,
        "visitor_team_score": away_score,
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> BalldontlieClient:
    return BalldontlieClient(
        SENTINEL_KEY,
        client=build_readonly_client(
            base_url="https://api.balldontlie.io",
            policy=ReadOnlyHTTPPolicy.for_balldontlie(),
            inner_transport=httpx.MockTransport(handler)))


def build_corpus(
    tmp_path: Path, payloads: list[dict[str, Any]], *, name: str = "corpus.db",
) -> Path:
    """A schema-v17 corpus holding one preserved ``/v1/games/{id}`` per game.

    ``results`` is NOT in the includes -- exactly the March 2026 shape: schedule
    observations and game references exist, ``nba_game_results`` is empty, and the
    final scores live only inside the preserved response bodies.
    """

    db_path = tmp_path / name
    initialize_database(db_path)
    database = Database(db_path)
    by_id = {str(p["id"]): p for p in payloads}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/games/"):
            body: Any = {"data": by_id[path.rsplit("/", 1)[1]], "meta": {}}
        elif path == "/v1/games":
            body = {"data": payloads, "meta": {}}
        else:
            body = {"data": [], "meta": {}}
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})

    async def _one(gid: int) -> Any:
        client = _client(handler)
        try:
            return await ingest_nba(
                database=database, client=client, from_date=FROM_DATE, to_date=TO_DATE,
                game_id=gid, includes=(), dry_run=False)   # <- no `results`
        finally:
            await client.aclose()

    for payload in payloads:
        outcome = asyncio.run(_one(int(payload["id"])))
        assert outcome.status == "succeeded", outcome.error_message
    return db_path


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    out = tmp_path / "nba.manifest.json"
    emit_plan(league="nba", from_date=FROM_DATE, to_date=TO_DATE,
              includes=("box", "quarters"), max_games=400, max_pages=8,
              max_records=1000, max_retries=1, rate_per_min=60,
              expected_schema_version=CURRENT_SCHEMA_VERSION, manifest_out=out, out=lambda _s: None)
    return out


def run_repair(db: Path, manifest_path: Path, **kw: Any) -> Any:
    params: dict[str, Any] = dict(
        database_path=db, manifest_path=manifest_path, provider="balldontlie",
        league="nba", date_range=DATE_RANGE, offline=True, dry_run=False)
    params.update(kw)
    return repair_nba_results_from_raw(**params)


def counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for (t,) in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")}
    finally:
        con.close()


def rows(db: Path, sql: str) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 1. The 239-shape replay
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def _shape_payloads() -> list[dict[str, Any]]:
    """239 games across all 31 March days, 9 of them into overtime."""

    out = []
    for i in range(239):
        gid = 18447000 + i
        ot = i % 26 == 0            # 10 of 239 reach a 5th period
        home = 100 + (i % 17)
        away = 95 + (i % 13)
        if home == away:            # a real NBA final is never tied
            away += 1
        out.append(game_payload(
            gid, home=(i % 30) + 1, away=((i + 7) % 30) + 1,
            home_score=home, away_score=away,
            period=5 if ot else 4, day=(i % 31) + 1))
    return out


def test_replays_every_preserved_response_at_month_shape(
    tmp_path: Path, manifest: Path, _shape_payloads: list[dict[str, Any]]
) -> None:
    """The whole month replays to exactly one typed result per selected game."""

    db = build_corpus(tmp_path, _shape_payloads)
    before = counts(db)
    assert before["nba_game_results"] == 0
    assert before["provider_game_references"] == 239

    result = run_repair(db, manifest)

    assert result.single_game_responses == 239
    assert result.selected_games == 239
    assert result.candidates == 239
    assert result.valid_results == 239
    assert result.rejected == 0 and result.rejections == []
    assert result.results_inserted == 239
    assert result.corrections_appended == 0
    assert result.raw_responses_inserted == 0
    assert result.network_occurred is False
    assert result.provider_client_constructed is False
    assert counts(db)["nba_game_results"] == 239


def test_only_results_and_the_provenance_note_change(
    tmp_path: Path, manifest: Path
) -> None:
    """No schedule, quarter, lineup, stat, identity or raw row may move."""

    db = build_corpus(tmp_path, [game_payload(1), game_payload(2, day=3)])
    before = counts(db)
    run_repair(db, manifest)
    after = counts(db)

    delta = {t: after[t] - before[t] for t in before if before[t] != after[t]}
    # Exactly two tables move: the results themselves, and ONE provenance note.
    assert delta == {"nba_game_results": 2, "data_quality_issues": 1}
    note = rows(db, "SELECT rule_code, severity, entity_type FROM "
                    f"data_quality_issues WHERE rule_code = '{REPAIR_RULE_CODE}'")
    assert len(note) == 1
    assert note[0]["severity"] == "note"
    assert note[0]["entity_type"] == "repair"


# --------------------------------------------------------------------------- #
# 2. Dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_mutates_nothing(tmp_path: Path, manifest: Path) -> None:
    db = build_corpus(tmp_path, [game_payload(1), game_payload(2, day=4)])
    before = counts(db)
    digest_before = db.read_bytes()

    result = run_repair(db, manifest, dry_run=True)

    assert result.dry_run is True
    assert result.valid_results == 2
    assert result.results_inserted == 0
    assert result.database_mutated is False
    assert counts(db) == before
    assert db.read_bytes() == digest_before   # byte-identical, not merely equal counts


def test_dry_run_and_apply_agree_on_the_semantic_hash(
    tmp_path: Path, manifest: Path
) -> None:
    """The dry-run digest is the contract the apply must satisfy."""

    db = build_corpus(tmp_path, [game_payload(1), game_payload(2, day=5)])
    dry = run_repair(db, manifest, dry_run=True)
    applied = run_repair(db, manifest)
    assert dry.semantic_result_hash == applied.semantic_result_hash
    assert dry.valid_results == applied.results_inserted


# --------------------------------------------------------------------------- #
# 3. Provenance, normalization, orientation, overtime
# --------------------------------------------------------------------------- #
def test_provenance_is_the_source_response_not_the_replay_clock(
    tmp_path: Path, manifest: Path
) -> None:
    """``observed_at`` must be the preserved receipt instant, never "now".

    A replayed observation that stamped the repair's wall clock would claim the
    corpus learned the result months after it actually did, silently breaking
    every point-in-time guarantee built on ``observed_at``.
    """

    db = build_corpus(tmp_path, [game_payload(1)])
    run_repair(db, manifest)

    row = rows(db, "SELECT * FROM nba_game_results")[0]
    src = rows(db, "SELECT * FROM raw_responses WHERE endpoint LIKE '/v1/games/%'")[0]
    assert row["observed_at"] == src["received_at"]
    assert row["ingested_at"] == src["received_at"]
    assert row["raw_response_id"] == src["raw_response_id"]
    assert row["raw_response_hash"] == src["body_hash"]
    assert row["run_id"] == src["run_id"]          # the run that really fetched it
    assert row["provider"] == "balldontlie"
    assert row["is_correction"] == 0


def test_final_score_and_winner_normalization(tmp_path: Path, manifest: Path) -> None:
    db = build_corpus(tmp_path, [
        game_payload(1, home_score=118, away_score=110),
        game_payload(2, home_score=99, away_score=121, day=6),
    ])
    run_repair(db, manifest)
    by_game = {r["provider_game_id"]: r
               for r in rows(db, "SELECT * FROM nba_game_results")}
    assert (by_game["1"]["home_points"], by_game["1"]["away_points"]) == (118, 110)
    assert by_game["1"]["winning_side"] == "home"
    assert (by_game["2"]["home_points"], by_game["2"]["away_points"]) == (99, 121)
    assert by_game["2"]["winning_side"] == "away"
    assert all(r["mapped_status"] == "final" for r in by_game.values())


def test_home_away_orientation_follows_the_provider_not_the_score(
    tmp_path: Path, manifest: Path
) -> None:
    """``home_points`` is the HOME team's score even when the home team loses."""

    db = build_corpus(tmp_path, [game_payload(1, home=7, away=19,
                                              home_score=95, away_score=130)])
    run_repair(db, manifest)
    row = rows(db, "SELECT * FROM nba_game_results")[0]
    sched = rows(db, "SELECT * FROM game_schedule_snapshots")[0]
    assert sched["home_provider_team_id"] == "7"
    assert sched["away_provider_team_id"] == "19"
    assert row["home_points"] == 95 and row["away_points"] == 130
    assert row["winning_side"] == "away"


def test_overtime_period_is_preserved(tmp_path: Path, manifest: Path) -> None:
    db = build_corpus(tmp_path, [game_payload(1, period=4),
                                 game_payload(2, period=6, day=7)])
    run_repair(db, manifest)
    periods = {r["provider_game_id"]: r["period"]
               for r in rows(db, "SELECT * FROM nba_game_results")}
    assert periods == {"1": 4, "2": 6}   # 6 == regulation 4 + 2 overtimes


# --------------------------------------------------------------------------- #
# 4. Refusals -- every ambiguity fails closed, none is resolved by row order
# --------------------------------------------------------------------------- #
def _inject_second_response(db: Path, gid: str, *, body: dict[str, Any],
                            received_at: Optional[str] = None) -> None:
    """Add a second preserved response for one game, reusing real provenance."""

    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "SELECT * FROM raw_responses WHERE endpoint = ?", (f"/v1/games/{gid}",))
        src = cur.fetchone()
        cols = [d[0] for d in cur.description]
        row = dict(zip(cols, src, strict=True))
        row["raw_response_id"] = row["raw_response_id"] + "x"
        row["body"] = json.dumps(body)
        row["body_hash"] = row["body_hash"][:-1] + ("a" if row["body_hash"][-1] != "a"
                                                    else "b")
        row["content_hash"] = row["content_hash"][:-1] + "c"
        if received_at is not None:
            row["received_at"] = received_at
        con.execute(
            f"INSERT INTO raw_responses ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
        con.commit()
    finally:
        con.close()


def test_equal_time_conflicting_bodies_are_refused(
    tmp_path: Path, manifest: Path
) -> None:
    """Two different results at one observation time cannot both be true.

    There is no deterministic correction rule that orders them, so the repair
    must refuse the game rather than let whichever row the cursor returned first
    win.
    """

    db = build_corpus(tmp_path, [game_payload(1)])
    same_time = rows(db, "SELECT received_at FROM raw_responses "
                         "WHERE endpoint LIKE '/v1/games/%'")[0]["received_at"]
    _inject_second_response(db, "1", body={"data": game_payload(
        1, home_score=999, away_score=1)}, received_at=same_time)

    with pytest.raises(ResultsRepairError, match="conflicting response bodies"):
        run_repair(db, manifest)
    assert counts(db)["nba_game_results"] == 0


def test_disagreeing_responses_are_refused_even_at_different_times(
    tmp_path: Path, manifest: Path
) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    _inject_second_response(db, "1", body={"data": game_payload(
        1, home_score=77, away_score=70)}, received_at="2027-01-01T00:00:00.000000Z")
    with pytest.raises(ResultsRepairError, match="disagree"):
        run_repair(db, manifest)
    assert counts(db)["nba_game_results"] == 0


@pytest.mark.parametrize("home,away,expected", [
    (None, 104, "missing or asymmetric"),
    (110, None, "missing or asymmetric"),
    (None, None, "missing or asymmetric"),
    (108, 108, "tied"),
])
def test_incomplete_or_tied_scores_are_refused(
    tmp_path: Path, manifest: Path, home: Optional[int], away: Optional[int],
    expected: str,
) -> None:
    """Never coerce a missing side to zero, and never persist a tied "final"."""

    db = build_corpus(tmp_path, [game_payload(1, home_score=home, away_score=away)])
    with pytest.raises(ResultsRepairError, match=expected):
        run_repair(db, manifest)
    assert counts(db)["nba_game_results"] == 0


@pytest.mark.parametrize("missing", ["raw_response_id", "body_hash", "received_at"])
def test_missing_provenance_is_refused(missing: str) -> None:
    """A response without full provenance can never become a result row.

    ``raw_responses`` is append-only (a b004 trigger refuses UPDATE), so the
    corpus itself makes this state unreachable -- which is exactly why the guard
    is asserted directly against the normalizer rather than by corrupting a
    database. Without a receipt instant there is no honest ``observed_at``, and
    without a response id/hash the row could not be traced back to its source.
    """

    from sports_quant.ingest.results_repair import RepairResult, _build

    good = {
        "raw_response_id": "raw_1", "run_id": "run_1", "body_hash": "abc",
        "received_at": "2026-03-02T01:00:00.000000Z",
        "body": json.dumps({"data": game_payload(1)}),
    }
    good[missing] = ""
    result = RepairResult()
    assert _build(good, "1", "ref_1", result) is None      # type: ignore[arg-type]
    assert result.rejected == 1
    assert "missing provenance" in result.rejections[0]


def test_complete_provenance_is_accepted_by_the_same_guard() -> None:
    """The negative test above must not be passing for an unrelated reason."""

    from sports_quant.ingest.results_repair import RepairResult, _build

    row = {
        "raw_response_id": "raw_1", "run_id": "run_1", "body_hash": "abc",
        "received_at": "2026-03-02T01:00:00.000000Z",
        "body": json.dumps({"data": game_payload(1)}),
    }
    result = RepairResult()
    candidate = _build(row, "1", "ref_1", result)          # type: ignore[arg-type]
    assert result.rejected == 0
    assert candidate is not None
    assert candidate.observed_at == "2026-03-02T01:00:00.000000Z"
    assert candidate.raw_response_id == "raw_1"


def test_non_final_status_is_refused(tmp_path: Path, manifest: Path) -> None:
    db = build_corpus(tmp_path, [game_payload(1, status="3rd Qtr")])
    with pytest.raises(ResultsRepairError, match="not final"):
        run_repair(db, manifest)


def test_offline_flag_is_mandatory(tmp_path: Path, manifest: Path) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    with pytest.raises(ResultsRepairError, match="offline-only"):
        run_repair(db, manifest, offline=False)


@pytest.mark.parametrize("field,value,match", [
    ("provider", "mlb_statsapi", "unsupported provider"),
    ("league", "mlb", "unsupported league"),
    ("date_range", "2026-04-01..2026-04-30", "does not match"),
])
def test_wrong_slice_is_refused(tmp_path: Path, manifest: Path, field: str,
                                value: str, match: str) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    with pytest.raises(ResultsRepairError, match=match):
        run_repair(db, manifest, **{field: value})
    assert counts(db)["nba_game_results"] == 0


def test_manifest_from_a_different_league_is_refused(tmp_path: Path) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    other = tmp_path / "mlb.manifest.json"
    emit_plan(league="mlb", from_date="2026-06-01", to_date="2026-06-30",
              includes=("results",), max_games=600, max_retries=1,
              expected_schema_version=CURRENT_SCHEMA_VERSION, manifest_out=other, out=lambda _s: None)
    with pytest.raises(ResultsRepairError, match="manifest rejected"):
        run_repair(db, other)


def test_database_outside_the_manifest_range_is_refused(tmp_path: Path) -> None:
    """A corpus whose games are not in the plan's range is not that plan's corpus."""

    db = build_corpus(tmp_path, [game_payload(1)])
    narrow = tmp_path / "narrow.manifest.json"
    emit_plan(league="nba", from_date="2026-03-10", to_date="2026-03-11",
              includes=("box",), max_games=400, max_pages=8, max_records=1000,
              max_retries=1, rate_per_min=60, expected_schema_version=CURRENT_SCHEMA_VERSION,
              manifest_out=narrow, out=lambda _s: None)
    with pytest.raises(ResultsRepairError, match="outside the manifest range"):
        repair_nba_results_from_raw(
            database_path=db, manifest_path=narrow, provider="balldontlie",
            league="nba", date_range="2026-03-10..2026-03-11", offline=True,
            dry_run=False)
    assert counts(db)["nba_game_results"] == 0


def test_target_aliasing_a_protected_path_is_refused(
    tmp_path: Path, manifest: Path
) -> None:
    """The frozen pre-repair evidence must never be the repair target."""

    db = build_corpus(tmp_path, [game_payload(1)])
    with pytest.raises(ResultsRepairError, match="protected/frozen artifact"):
        run_repair(db, manifest, forbidden_paths=(db,))
    # ... including when it is reached by a different spelling of the same file
    with pytest.raises(ResultsRepairError, match="protected/frozen artifact"):
        run_repair(db, manifest, forbidden_paths=(db.parent / "." / db.name,))
    assert counts(db)["nba_game_results"] == 0


def test_conflicting_preexisting_results_are_refused(
    tmp_path: Path, manifest: Path
) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    run_repair(db, manifest)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO nba_game_results (result_id, game_ref_id, provider, "
        " provider_game_id, home_points, away_points, period, winning_side, "
        " mapped_status, is_correction, observed_at, ingested_at, raw_response_id, "
        " raw_response_hash, content_hash, created_at) "
        "SELECT result_id || 'z', game_ref_id, provider, provider_game_id, 1, 2, "
        " period, 'away', mapped_status, 0, observed_at, ingested_at, "
        " raw_response_id, raw_response_hash, content_hash || 'z', created_at "
        "FROM nba_game_results")
    con.commit()
    con.close()
    with pytest.raises(ResultsRepairError, match="conflicting result observations"):
        run_repair(db, manifest)


# --------------------------------------------------------------------------- #
# 5. Idempotency and correction semantics
# --------------------------------------------------------------------------- #
def test_second_replay_is_a_no_op(tmp_path: Path, manifest: Path) -> None:
    db = build_corpus(tmp_path, [game_payload(1), game_payload(2, day=8)])
    first = run_repair(db, manifest)
    snapshot = db.read_bytes()

    second = run_repair(db, manifest)

    assert first.results_inserted == 2
    assert second.results_inserted == 0
    assert second.results_unchanged == 2
    assert second.corrections_appended == 0
    assert second.raw_responses_inserted == 0
    assert second.provenance_notes_written == 0
    assert second.already_complete is True
    assert second.database_mutated is False
    assert second.semantic_result_hash == first.semantic_result_hash
    assert db.read_bytes() == snapshot        # byte-identical after the second run

    third = run_repair(db, manifest, dry_run=True)
    assert third.already_complete is True
    assert third.results_before == third.results_after == 2


def test_replay_after_a_genuine_correction_is_not_itself_a_correction(
    tmp_path: Path, manifest: Path
) -> None:
    """Correction semantics stay the repository's, and the replay adds none.

    A later, genuinely different observation IS a correction; replaying the same
    preserved response again is not.
    """

    db = build_corpus(tmp_path, [game_payload(1)])
    run_repair(db, manifest)
    from sports_quant.db.engine import transaction
    from sports_quant.db.repositories.nba import SqliteNbaResultRepository

    src = rows(db, "SELECT * FROM raw_responses WHERE endpoint LIKE '/v1/games/%'")[0]
    ref = rows(db, "SELECT reference_id FROM provider_game_references")[0][0]
    database = Database(db)
    with database.connection() as conn:
        with transaction(conn):
            _rid, _outcome, corrected = SqliteNbaResultRepository(conn).append(
                game_ref_id=ref, provider="balldontlie", provider_game_id="1",
                observed_at="2027-01-01T00:00:00.000000Z",
                ingested_at="2027-01-01T00:00:00.000000Z", run_id=None,
                raw_response_id=src["raw_response_id"],
                raw_response_hash=src["body_hash"], mapped_status="final",
                home_points=112, away_points=104, period=4, winning_side="home")
    assert corrected is True        # a revised final score IS a correction

    again = run_repair(db, manifest)
    assert again.results_inserted == 0
    assert again.corrections_appended == 0


# --------------------------------------------------------------------------- #
# 6. Isolation: checkpoint, provider client, settings, secrets
# --------------------------------------------------------------------------- #
def test_checkpoint_is_never_touched(tmp_path: Path, manifest: Path) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    ckpt = tmp_path / "run.ckpt"
    ckpt.write_bytes(b'{"state": "completed"}')
    before = ckpt.read_bytes()
    result = run_repair(db, manifest)
    assert result.checkpoint_mutated is False
    assert ckpt.read_bytes() == before


def test_no_provider_client_and_no_settings_load(
    tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair must be structurally incapable of reaching a provider.

    Both the client constructor and the settings loader are made explosive; a
    repair that so much as resolved a default database path would trip them.
    """

    db = build_corpus(tmp_path, [game_payload(1)])

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the offline repair reached a network-capable path")

    monkeypatch.setattr("sports_quant.providers.balldontlie.BalldontlieClient.__init__",
                        boom)
    monkeypatch.setattr("sports_quant.config.load_settings", boom)
    monkeypatch.setattr("sports_quant.http_policy.build_readonly_client", boom)
    monkeypatch.setattr("socket.getaddrinfo", boom)

    result = run_repair(db, manifest)
    assert result.results_inserted == 1
    assert result.network_occurred is False
    assert result.provider_client_constructed is False


def test_no_secret_reaches_the_database_or_the_refusal_messages(
    tmp_path: Path, manifest: Path
) -> None:
    """The sentinel key is used to build the fixture; it must appear nowhere."""

    db = build_corpus(tmp_path, [game_payload(1)])
    run_repair(db, manifest)

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        for (table,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"):
            cols = [c[1] for c in con.execute(f'PRAGMA table_info("{table}")')]
            for col in cols:
                hit = con.execute(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f'WHERE CAST("{col}" AS TEXT) LIKE ?', (f"%{SENTINEL_KEY}%",)
                ).fetchone()[0]
                assert hit == 0, f"{table}.{col} stored the sentinel key"
        headers = {k.lower() for r in con.execute(
            "SELECT response_headers_json FROM raw_responses")
            for k in json.loads(r[0] or "{}")}
        assert "authorization" not in headers
    finally:
        con.close()

    # A refusal message must stay sanitized too.
    with pytest.raises(ResultsRepairError) as exc:
        run_repair(db, manifest, provider="mlb_statsapi")
    assert SENTINEL_KEY not in str(exc.value)


# --------------------------------------------------------------------------- #
# 7. The label contract must NOT be weakened by this repair
# --------------------------------------------------------------------------- #
def test_pit_dataset_stays_label_empty_without_canonical_ids(
    tmp_path: Path, manifest: Path
) -> None:
    """239 typed provider results is not 239 usable labels.

    The dataset builder reads canonical ``games``; provider ids alone must never
    be accepted as a substitute. Matching has not run in this corpus, so the
    label count must still be zero AFTER the results repair.
    """

    from sports_quant.pit.dataset import build_historical_dataset

    db = build_corpus(tmp_path, [game_payload(1), game_payload(2, day=9)])
    run_repair(db, manifest)

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        assert con.execute("SELECT COUNT(*) FROM nba_game_results").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM provider_game_references WHERE game_id IS NOT NULL"
        ).fetchone()[0] == 0
        assert len(build_historical_dataset(con, league="nba")) == 0
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 8. MLB non-regression
# --------------------------------------------------------------------------- #
def test_mlb_result_tables_are_untouched(tmp_path: Path, manifest: Path) -> None:
    """The NBA repair must not reach the baseball-named result tables at all."""

    db = build_corpus(tmp_path, [game_payload(1)])
    before = counts(db)
    run_repair(db, manifest)
    after = counts(db)
    for table in ("game_result_snapshots", "team_game_statistics",
                  "player_game_statistics", "roster_snapshots"):
        if table in before:
            assert after[table] == before[table] == 0, table


def test_repair_refuses_an_mlb_corpus(tmp_path: Path, manifest: Path) -> None:
    """An MLB database must be rejected on provider binding, not silently skipped."""

    db_path = tmp_path / "mlb.db"
    initialize_database(db_path)
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, sport, operation, "
        " args_json, status, requested_at, started_at, started_monotonic_ns, "
        " completed_at, tool_version, created_at) "
        "VALUES ('run_x','ingest-mlb','mlb_statsapi','mlb','ingest_mlb','{}', "
        " 'succeeded','2026-06-01T00:00:00.000000Z','2026-06-01T00:00:00.000000Z',1, "
        " '2026-06-01T00:00:01.000000Z','sports_quant', "
        " '2026-06-01T00:00:00.000000Z')")
    con.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        " request_params_json, http_method, http_status, response_headers_json, "
        " requested_at, received_at, elapsed_ns, body, body_bytes, body_hash, "
        " content_hash, created_at) "
        "VALUES ('raw_x','run_x','mlb_statsapi','/api/v1/schedule','{}','GET',200, "
        " '{}','2026-06-01T00:00:00.000000Z','2026-06-01T00:00:00.000000Z',1,'{}',2, "
        " 'h','c','2026-06-01T00:00:00.000000Z')")
    con.commit()
    con.close()
    with pytest.raises(ResultsRepairError, match="provider this manifest does not"):
        run_repair(db_path, manifest)


# --------------------------------------------------------------------------- #
# 9. CLI surface: output shapes, exit codes, and the flags it must not accept
# --------------------------------------------------------------------------- #
def _cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the real CLI and capture stdout.

    ``cli.main`` binds ``print`` as a default argument at import time, so stdout
    has to be captured rather than the builtin patched.
    """

    from sports_quant.cli import main

    code = main(argv)
    return code, capsys.readouterr().out


def _cli_args(db: Path, manifest_path: Path, *extra: str) -> list[str]:
    return ["repair-nba-results-from-raw", "--db", str(db),
            "--manifest", str(manifest_path), "--provider", "balldontlie",
            "--league", "nba", "--date-range", DATE_RANGE, "--offline", *extra]


def test_cli_json_output_is_machine_readable(
    tmp_path: Path, manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    code, printed = _cli(_cli_args(db, manifest, "--dry-run", "--json"), capsys)
    assert code == 0
    payload = json.loads(printed.strip().splitlines()[-1])
    assert payload["command"] == "repair-nba-results-from-raw"
    assert payload["dry_run"] is True
    assert payload["valid_results"] == 1
    assert payload["network_occurred"] is False
    assert payload["results_inserted"] == 0
    assert counts(db)["nba_game_results"] == 0


def test_cli_human_output_states_the_offline_contract(
    tmp_path: Path, manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    code, text = _cli(_cli_args(db, manifest), capsys)
    assert code == 0
    assert "APPLIED" in text
    assert "no provider request" in text
    assert "inserted=1" in text
    assert "checkpoint_mutated=False" in text
    assert "network_occurred=False" in text


def test_cli_refusal_exits_non_zero_and_stays_sanitized(
    tmp_path: Path, manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    code, printed = _cli(
        _cli_args(db, manifest, "--forbid-path", str(db), "--json"), capsys)
    assert code == 2
    payload = json.loads(printed.strip().splitlines()[-1])
    assert payload["refused"] is True
    assert "protected/frozen artifact" in payload["reason"]
    assert SENTINEL_KEY not in payload["reason"]
    assert counts(db)["nba_game_results"] == 0


@pytest.mark.parametrize("extra", [
    ["--base-url", "https://api.balldontlie.io"],
    ["--api-key", "sk-should-never-be-accepted"],
    ["--url", "https://example.invalid"],
    ["--timeout", "30"],
])
def test_cli_rejects_every_network_shaped_option(
    tmp_path: Path, manifest: Path, extra: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command has no network surface, so argparse must refuse one."""

    db = build_corpus(tmp_path, [game_payload(1)])
    with pytest.raises(SystemExit) as exc:
        _cli(_cli_args(db, manifest, *extra), capsys)
    assert exc.value.code == 2
    assert counts(db)["nba_game_results"] == 0


@pytest.mark.parametrize("drop", ["--offline", "--db", "--manifest",
                                  "--provider", "--league", "--date-range"])
def test_cli_requires_every_explicit_argument(
    tmp_path: Path, manifest: Path, drop: str, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_corpus(tmp_path, [game_payload(1)])
    argv = _cli_args(db, manifest)
    idx = argv.index(drop)
    del argv[idx:idx + (1 if drop == "--offline" else 2)]
    with pytest.raises(SystemExit) as exc:
        _cli(argv, capsys)
    assert exc.value.code == 2
