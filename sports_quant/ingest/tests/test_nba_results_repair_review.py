"""Regressions for the defects the independent review of the offline NBA results
repair confirmed (``NBA_RESULTS_REPAIR_INDEPENDENT_REVIEW.md``).

None of these affected the applied March 2026 repair -- every one of its 239 rows
was verified byte-consistent with the preserved integer scores, and its coverage
was 239/239. They are latent hardening gaps in a command whose whole contract is
"invent nothing, fail closed", and each one is a way a *future* corpus could be
repaired wrongly and silently.

1. A non-integer provider score was silently coerced: ``110.7`` became ``110``
   and ``"110"`` became ``110``. A repair that exists to avoid fabricating data
   must not quietly round or reinterpret a value the provider did not send.
2. A negative score was accepted. An NBA final cannot be below zero.
3. A non-positive ``period`` was accepted.
4. A deeply nested response body raised a raw ``RecursionError`` instead of a
   sanitized refusal, so a corrupt preserved body crashed the command with a
   traceback rather than failing closed.
5. A selected game with no usable preserved response was silently skipped: run
   against a skeleton-only corpus the repair reported success, ``rejected=0`` and
   ``already_complete=True`` while inserting nothing at all.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.f1a import emit_plan
from sports_quant.ingest.nba_ingestor import ingest_nba
from sports_quant.ingest.results_repair import (
    RepairResult,
    ResultsRepairError,
    _build,
    _payload_of,
    repair_nba_results_from_raw,
)
from sports_quant.providers.balldontlie import BalldontlieClient

FROM_DATE, TO_DATE = "2026-03-01", "2026-03-31"
DATE_RANGE = f"{FROM_DATE}..{TO_DATE}"


def game_payload(gid: int = 1, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": gid, "date": "2026-03-02", "datetime": "2026-03-02T00:30:00Z",
        "season": 2025, "status": "Final", "period": 4, "postseason": False,
        "home_team": {"id": 1, "full_name": "Home", "abbreviation": "HOM"},
        "visitor_team": {"id": 2, "full_name": "Away", "abbreviation": "AWY"},
        "home_team_score": 110, "visitor_team_score": 104,
    }
    base.update(over)
    return base


def _row(payload: Any) -> dict[str, Any]:
    return {
        "raw_response_id": "raw_1", "run_id": "run_1", "body_hash": "bh",
        "received_at": "2026-03-02T01:00:00.000000Z",
        "body": payload if isinstance(payload, str) else json.dumps({"data": payload}),
    }


def build(payload: Any) -> tuple[Any, RepairResult]:
    result = RepairResult()
    candidate = _build(_row(payload), "1", "ref_1", result)  # type: ignore[arg-type]
    return candidate, result


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    out = tmp_path / "nba.manifest.json"
    emit_plan(league="nba", from_date=FROM_DATE, to_date=TO_DATE,
              includes=("box", "quarters"), max_games=400, max_pages=8,
              max_records=1000, max_retries=1, rate_per_min=60,
              expected_schema_version=CURRENT_SCHEMA_VERSION, manifest_out=out, out=lambda _s: None)
    return out


# --------------------------------------------------------------------------- #
# 1-3. Score and period values must be exactly what the provider sent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [110.7, 110.0, "110", "abc", True, False])
def test_non_integer_score_is_refused_not_coerced(value: Any) -> None:
    """``int(110.7)`` is 110 -- a number the provider never sent.

    The shared normalizer is deliberately permissive because live ingestion has
    to survive odd provider payloads; a *repair* has no such excuse. It must
    persist the integer the provider actually reported or refuse the game.
    """

    candidate, result = build(game_payload(home_team_score=value))
    assert candidate is None, f"{value!r} was accepted as {getattr(candidate, 'home_points', None)!r}"
    assert result.rejected == 1
    assert "integer" in result.rejections[0] or "missing or asymmetric" in result.rejections[0]


@pytest.mark.parametrize("home,away", [(-5, 104), (110, -1), (-2, -3)])
def test_negative_score_is_refused(home: int, away: int) -> None:
    candidate, result = build(game_payload(home_team_score=home,
                                           visitor_team_score=away))
    assert candidate is None
    assert result.rejected == 1
    assert "negative" in result.rejections[0]


@pytest.mark.parametrize("period", [0, -3])
def test_non_positive_period_is_refused(period: int) -> None:
    candidate, result = build(game_payload(period=period))
    assert candidate is None
    assert result.rejected == 1
    assert "period" in result.rejections[0]


def test_a_genuine_integer_payload_still_normalizes(tmp_path: Path) -> None:
    """The negative tests above must not be passing for an unrelated reason."""

    candidate, result = build(game_payload(home_team_score=118,
                                           visitor_team_score=110, period=5))
    assert result.rejected == 0
    assert candidate is not None
    assert (candidate.home_points, candidate.away_points) == (118, 110)
    assert candidate.winning_side == "home"
    assert candidate.period == 5
    # a missing period is genuine absence, not an invalid one
    candidate2, result2 = build(game_payload(period=None))
    assert result2.rejected == 0 and candidate2 is not None
    assert candidate2.period is None


# --------------------------------------------------------------------------- #
# 4. A hostile body fails closed, it does not crash
# --------------------------------------------------------------------------- #
def test_deeply_nested_body_fails_closed_instead_of_raising() -> None:
    """A corrupt preserved body must produce a sanitized refusal, not a traceback."""

    hostile = "[" * 60_000 + "]" * 60_000
    assert _payload_of(hostile) is None          # must not raise RecursionError

    candidate, result = build(hostile)
    assert candidate is None
    assert result.rejected == 1
    assert "usable game object" in result.rejections[0]


@pytest.mark.parametrize("body", ["", "{", "null", '{"data": "nope"}',
                                  '{"data": []}', "[1,2,3]"])
def test_malformed_bodies_fail_closed(body: str) -> None:
    candidate, result = build(body)
    assert candidate is None and result.rejected == 1


# --------------------------------------------------------------------------- #
# 5. A selected game with no usable response must not pass silently
# --------------------------------------------------------------------------- #
def _skeleton_corpus(tmp_path: Path, games: int = 4) -> Path:
    """A SKELETON-stage corpus: game references exist, no per-game responses.

    This is the state a games-listing-only run leaves behind, and it is the
    realistic shape of "a selected game has no preserved single-game response"
    (``raw_responses`` is append-only, so one cannot simply be deleted).
    """

    payloads = [game_payload(i + 1, home_team_score=100 + i, away_team_score=95 + i)
                for i in range(games)]
    for p in payloads:
        p["visitor_team_score"] = p.pop("away_team_score")
    db_path = tmp_path / "skeleton.db"
    initialize_database(db_path)
    database = Database(db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body: Any = ({"data": payloads, "meta": {}}
                     if request.url.path == "/v1/games" else {"data": [], "meta": {}})
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})

    async def _run() -> Any:
        client = BalldontlieClient(
            "", client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(handler)))
        try:
            return await ingest_nba(database=database, client=client,
                                    from_date=FROM_DATE, to_date=TO_DATE,
                                    includes=(), dry_run=False)
        finally:
            await client.aclose()

    outcome = asyncio.run(_run())
    assert outcome.status == "succeeded", outcome.error_message
    return db_path


def test_selected_games_without_a_usable_response_are_refused(
    tmp_path: Path, manifest: Path
) -> None:
    """Reporting success while repairing nothing is the worst possible outcome.

    Against a skeleton-only corpus the repair used to return
    ``results_inserted=0``, ``rejected=0`` and ``already_complete=True`` -- an
    operator would reasonably read that as "the results are already in place".
    """

    db = _skeleton_corpus(tmp_path)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM provider_game_references").fetchone()[0] == 4
    assert con.execute("SELECT COUNT(*) FROM raw_responses WHERE endpoint "
                       "LIKE '/v1/games/%'").fetchone()[0] == 0
    con.close()

    with pytest.raises(ResultsRepairError, match="without a usable preserved response"):
        repair_nba_results_from_raw(
            database_path=db, manifest_path=manifest, provider="balldontlie",
            league="nba", date_range=DATE_RANGE, offline=True, dry_run=False)

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM nba_game_results").fetchone()[0] == 0
    con.close()


def test_the_shortfall_guard_is_reported_in_dry_run_too(
    tmp_path: Path, manifest: Path
) -> None:
    db = _skeleton_corpus(tmp_path)
    with pytest.raises(ResultsRepairError, match="without a usable preserved response"):
        repair_nba_results_from_raw(
            database_path=db, manifest_path=manifest, provider="balldontlie",
            league="nba", date_range=DATE_RANGE, offline=True, dry_run=True)


def test_full_coverage_still_succeeds(tmp_path: Path, manifest: Path) -> None:
    """The shortfall guard must not fire on a genuinely complete corpus."""

    payloads = [game_payload(i + 1, home_team_score=100 + i,
                             visitor_team_score=90 + i) for i in range(3)]
    db_path = tmp_path / "full.db"
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
        client = BalldontlieClient(
            "", client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(handler)))
        try:
            return await ingest_nba(database=database, client=client,
                                    from_date=FROM_DATE, to_date=TO_DATE,
                                    game_id=gid, includes=(), dry_run=False)
        finally:
            await client.aclose()

    for p in payloads:
        assert asyncio.run(_one(int(p["id"]))).status == "succeeded"

    result = repair_nba_results_from_raw(
        database_path=db_path, manifest_path=manifest, provider="balldontlie",
        league="nba", date_range=DATE_RANGE, offline=True, dry_run=False)
    assert result.results_inserted == 3
    assert result.games_without_response == 0
    # ... and a second pass, where every game already HAS a result, must not
    # trip the guard either.
    again = repair_nba_results_from_raw(
        database_path=db_path, manifest_path=manifest, provider="balldontlie",
        league="nba", date_range=DATE_RANGE, offline=True, dry_run=False)
    assert again.results_inserted == 0 and again.already_complete is True


# --------------------------------------------------------------------------- #
# 6. Result-repository correction semantics under replay
# --------------------------------------------------------------------------- #
def _one_game_corpus(tmp_path: Path, payload: Optional[dict[str, Any]] = None) -> Path:
    p = payload or game_payload(1)
    db_path = tmp_path / "one.db"
    initialize_database(db_path)
    database = Database(db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/games/"):
            body: Any = {"data": p, "meta": {}}
        elif request.url.path == "/v1/games":
            body = {"data": [p], "meta": {}}
        else:
            body = {"data": [], "meta": {}}
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})

    async def _run() -> Any:
        client = BalldontlieClient(
            "", client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(handler)))
        try:
            return await ingest_nba(database=database, client=client,
                                    from_date=FROM_DATE, to_date=TO_DATE,
                                    game_id=int(p["id"]), includes=(), dry_run=False)
        finally:
            await client.aclose()

    assert asyncio.run(_run()).status == "succeeded"
    return db_path


def _append(db: Path, *, observed_at: str, home: int, away: int,
            status: str = "final", period: int = 4,
            detail: Optional[str] = "Final") -> tuple[Any, bool]:
    """Append one observation through the PRODUCTION repository.

    ``detail`` defaults to what the repair itself writes (``status_raw``), so a
    test that means "the same content" really produces the same content hash.
    """

    from sports_quant.db.engine import transaction
    from sports_quant.db.repositories.nba import SqliteNbaResultRepository

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    src = con.execute("SELECT * FROM raw_responses "
                      "WHERE endpoint LIKE '/v1/games/%'").fetchone()
    ref = con.execute("SELECT reference_id FROM provider_game_references").fetchone()[0]
    con.close()
    database = Database(db)
    with database.connection() as conn:
        with transaction(conn):
            return SqliteNbaResultRepository(conn).append(
                game_ref_id=ref, provider="balldontlie", provider_game_id="1",
                observed_at=observed_at, ingested_at=observed_at, run_id=None,
                raw_response_id=src["raw_response_id"],
                raw_response_hash=src["body_hash"], mapped_status=status,
                home_points=home, away_points=away, period=period,
                result_detail=detail,
                winning_side="home" if home > away else "away")[1:]


def _results(db: Path) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM nba_game_results "
                           "ORDER BY observed_at, result_id").fetchall()
    finally:
        con.close()


def _repair(db: Path, manifest: Path, **kw: Any) -> Any:
    params: dict[str, Any] = dict(
        database_path=db, manifest_path=manifest, provider="balldontlie",
        league="nba", date_range=DATE_RANGE, offline=True, dry_run=False)
    params.update(kw)
    return repair_nba_results_from_raw(**params)


def test_first_observation_inserts_exactly_one_row(
    tmp_path: Path, manifest: Path
) -> None:
    db = _one_game_corpus(tmp_path)
    result = _repair(db, manifest)
    assert result.results_inserted == 1
    rows = _results(db)
    assert len(rows) == 1 and rows[0]["is_correction"] == 0


def test_later_changed_score_is_a_correction_and_append_only(
    tmp_path: Path, manifest: Path
) -> None:
    """Final-to-final revision appends a correction; it never overwrites."""

    db = _one_game_corpus(tmp_path)
    _repair(db, manifest)
    original = _results(db)[0]

    _outcome, corrected = _append(db, observed_at="2026-09-01T00:00:00.000000Z",
                                  home=112, away=104)
    rows = _results(db)
    assert corrected is True
    assert len(rows) == 2, "a correction must APPEND, not overwrite"
    assert rows[0]["home_points"] == original["home_points"]   # history intact
    assert rows[1]["home_points"] == 112 and rows[1]["is_correction"] == 1


def test_an_earlier_observation_arriving_late_does_not_displace_a_correction(
    tmp_path: Path, manifest: Path
) -> None:
    """Back-dated evidence is appended in its own place, not promoted.

    The repository orders by ``observed_at``, so a late-arriving EARLIER
    observation must not become the latest state.
    """

    db = _one_game_corpus(tmp_path)
    _repair(db, manifest)
    _append(db, observed_at="2026-09-01T00:00:00.000000Z", home=112, away=104)
    _append(db, observed_at="2026-01-01T00:00:00.000000Z", home=50, away=48)

    rows = _results(db)
    assert len(rows) == 3
    assert rows[0]["observed_at"].startswith("2026-01-01")
    latest = max(rows, key=lambda r: r["observed_at"])
    assert latest["home_points"] == 112, "the newest observation must remain latest"


def test_replaying_the_same_preserved_response_is_never_a_correction(
    tmp_path: Path, manifest: Path
) -> None:
    db = _one_game_corpus(tmp_path)
    for _ in range(3):
        result = _repair(db, manifest)
    assert result.results_inserted == 0 and result.corrections_appended == 0
    rows = _results(db)
    assert len(rows) == 1 and rows[0]["is_correction"] == 0


def test_final_to_nonfinal_regression_is_recorded_not_silently_dropped(
    tmp_path: Path, manifest: Path
) -> None:
    """A previously-final observation changing substantively is a correction."""

    db = _one_game_corpus(tmp_path)
    _repair(db, manifest)
    _outcome, corrected = _append(db, observed_at="2026-09-01T00:00:00.000000Z",
                                  home=60, away=55, status="in_progress", period=3)
    rows = _results(db)
    assert corrected is True          # points went backwards -> a revision
    assert len(rows) == 2
    assert rows[-1]["mapped_status"] == "in_progress"


def test_duplicate_content_from_a_distinct_raw_row_stays_idempotent(
    tmp_path: Path, manifest: Path
) -> None:
    """Same content at the same instant -> still exactly one observation."""

    db = _one_game_corpus(tmp_path)
    _repair(db, manifest)
    row = _results(db)[0]
    _outcome, corrected = _append(db, observed_at=row["observed_at"],
                                  home=row["home_points"], away=row["away_points"],
                                  detail=row["result_detail"])
    assert corrected is False
    assert len(_results(db)) == 1, "identical content must not append a second row"


def test_a_detail_only_difference_appends_but_is_not_a_correction(
    tmp_path: Path, manifest: Path
) -> None:
    """Status WORDING changing is a new observation, never a correction.

    Documented repository behaviour, pinned here because the boundary matters:
    the substantive values (points, period, winner) are what define a revision.
    """

    db = _one_game_corpus(tmp_path)
    _repair(db, manifest)
    row = _results(db)[0]
    _outcome, corrected = _append(db, observed_at=row["observed_at"],
                                  home=row["home_points"], away=row["away_points"],
                                  detail="FINAL/OT")
    assert corrected is False, "a wording change is not a score revision"
    rows = _results(db)
    assert len(rows) == 2
    assert {r["home_points"] for r in rows} == {row["home_points"]}


def test_repair_refuses_a_corpus_with_equal_time_conflicting_results(
    tmp_path: Path, manifest: Path
) -> None:
    """Pre-existing contradictions must block the repair, not be added to."""

    db = _one_game_corpus(tmp_path)
    _repair(db, manifest)
    row = _results(db)[0]
    _append(db, observed_at=row["observed_at"], home=1, away=2)   # same instant
    assert len(_results(db)) == 2
    with pytest.raises(ResultsRepairError, match="conflicting result observations"):
        _repair(db, manifest, dry_run=True)


def test_the_repair_never_touches_the_mlb_result_tables() -> None:
    """The NBA hardening must not reach the baseball-named result path."""

    import inspect

    from sports_quant.ingest import results_repair

    src = inspect.getsource(results_repair)
    for table in ("game_result_snapshots", "team_game_statistics",
                  "player_game_statistics", "roster_snapshots", "inning_lines"):
        assert table not in src, table
    assert results_repair.SUPPORTED_LEAGUE == "nba"
    assert results_repair.SUPPORTED_PROVIDER == "balldontlie"
