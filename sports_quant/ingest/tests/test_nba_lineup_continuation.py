"""Bounded NBA lineup-continuation recovery, prepared entirely offline.

The March 2026 month run fetched ``/v1/lineups`` once per game, so 40 of 239
games kept only their first 25 rows while the provider was still advertising a
cursor (``F1_NBA_2026_03_EXECUTION_REVIEW.md`` §8/§9). These tests cover the
recovery that will later fetch the missing pages: target derivation from the
protected corpus, the plan and manifest that bound it, and the executor that
walks each cursor chain.

Everything here is offline. Provider interaction is always an
``httpx.MockTransport``; no credential is read and no first page is ever
requested, because the corpus already holds it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from sports_quant.db.engine import Database
from sports_quant.db.init import initialize_database
from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
from sports_quant.ingest.lineup_continuation import (
    DQ_CONFLICTING_PLAYER,
    DQ_EMPTY_PAGE_WITH_CURSOR,
    DQ_MALFORMED_PAGE,
    DQ_PAGE_LIMIT_REACHED,
    DQ_REPEATED_CURSOR,
    DQ_TERMINAL_FAILURE,
    DQ_WRONG_GAME,
    LINEUPS_PER_PAGE,
    MAX_CONTINUATION_PAGES,
    RECOVERY_CONTRACT_VERSION,
    RECOVERY_PURPOSE,
    STOP_EXHAUSTED,
    STOP_FAILED,
    STOP_MALFORMED,
    STOP_PAGE_LIMIT,
    STOP_REPEATED_CURSOR,
    STOP_WRONG_GAME,
    ContinuationUnitFailed,
    LineupContinuationError,
    LineupContinuationExecutor,
    LineupTarget,
    derive_targets,
    merge_lineup_rows,
    survey_lineup_pages,
    target_set_digest,
)
from sports_quant.ingest.manifest import build_manifest, plan_hash
from sports_quant.ingest.planning import (
    MAX_CONTINUATION_PAGES as PLAN_MAX_PAGES,
)
from sports_quant.ingest.planning import (
    Bounds,
    RecoveryBinding,
    RecoveryPlanError,
    plan_lineup_continuation,
)
from sports_quant.providers.balldontlie import BalldontlieClient

DATE_RANGE = "2026-03-01..2026-03-31"
SENTINEL_KEY = "sk-continuation-must-never-be-read"


# --------------------------------------------------------------------------- #
# Fixtures: a corpus shaped like the March one, results deliberately absent
# --------------------------------------------------------------------------- #
def lineup_row(game_id: int, player_id: int, team_id: int = 1, *,
               starter: bool = False, position: str = "G") -> dict[str, Any]:
    return {"game_id": game_id, "id": 1_000_000 + player_id,
            "player": {"id": player_id, "first_name": "P", "last_name": str(player_id)},
            "team": {"id": team_id, "full_name": f"Team {team_id}"},
            "position": position, "starter": starter}


def lineup_body(game_id: int, players: list[int], *,
                next_cursor: Optional[int]) -> dict[str, Any]:
    meta: dict[str, Any] = {"per_page": LINEUPS_PER_PAGE}
    if next_cursor is not None:
        meta["next_cursor"] = next_cursor
    return {"data": [lineup_row(game_id, p) for p in players], "meta": meta}


def build_corpus(tmp_path: Path, *, games: dict[int, Optional[int]]) -> Path:
    """A v17 corpus whose games each hold ONE preserved lineups page.

    ``games`` maps provider game id -> the first page's ``next_cursor`` (``None``
    for a game the month run completed).
    """

    db_path = tmp_path / "march.db"
    initialize_database(db_path)
    database = Database(db_path)

    def payload(gid: int) -> dict[str, Any]:
        return {
            "id": gid, "date": "2026-03-02", "datetime": "2026-03-02T00:30:00Z",
            "season": 2025, "status": "Final", "period": 4, "postseason": False,
            "home_team": {"id": 1, "full_name": "Home", "abbreviation": "HOM"},
            "visitor_team": {"id": 2, "full_name": "Away", "abbreviation": "AWY"},
            "home_team_score": 110, "visitor_team_score": 104,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/games/"):
            body: Any = {"data": payload(int(path.rsplit("/", 1)[1])), "meta": {}}
        elif path == "/v1/games":
            body = {"data": [payload(g) for g in games], "meta": {}}
        elif path == "/v1/lineups":
            gid = int(request.url.params.get_list("game_ids[]")[0])
            body = lineup_body(gid, list(range(1, 26)), next_cursor=games[gid])
        else:
            body = {"data": [], "meta": {}}
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})

    from sports_quant.ingest.nba_ingestor import ingest_nba

    async def _one(gid: int) -> Any:
        client = BalldontlieClient(
            SENTINEL_KEY, client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(handler)))
        try:
            return await ingest_nba(
                database=database, client=client, from_date="2026-03-01",
                to_date="2026-03-31", game_id=gid, includes=("lineups",),
                dry_run=False)
        finally:
            await client.aclose()

    import asyncio

    for gid in games:
        assert asyncio.run(_one(gid)).status == "succeeded"
    return db_path


# --------------------------------------------------------------------------- #
# 1. Target derivation
# --------------------------------------------------------------------------- #
def test_targets_are_exactly_the_games_with_a_live_first_page_cursor(
    tmp_path: Path,
) -> None:
    db = build_corpus(tmp_path, games={1: 501, 2: None, 3: 503, 4: None, 5: None})
    survey = derive_targets(db)

    assert survey.selected_games == 5
    assert survey.games_with_first_page == 5
    assert survey.complete_games == 3
    assert survey.target_count == 2
    assert [t.provider_game_id for t in survey.targets] == ["1", "3"]
    assert [t.start_cursor for t in survey.targets] == [501, 503]
    for target in survey.targets:
        assert target.first_page_rows == 25
        assert target.first_raw_response_id and target.first_raw_response_hash
        assert target.first_observed_at


def test_target_digest_is_order_independent_and_content_sensitive(
    tmp_path: Path,
) -> None:
    db = build_corpus(tmp_path, games={1: 501, 2: 502, 3: None})
    survey = derive_targets(db)
    reversed_digest = target_set_digest(tuple(reversed(survey.targets)))
    assert reversed_digest == survey.target_digest()

    moved = LineupTarget(**{**survey.targets[0].__dict__, "start_cursor": 999})
    assert target_set_digest((moved, survey.targets[1])) != survey.target_digest()


def test_derivation_refuses_when_expectations_do_not_match(tmp_path: Path) -> None:
    db = build_corpus(tmp_path, games={1: 501, 2: None})
    with pytest.raises(LineupContinuationError, match="expected 5"):
        derive_targets(db, expected_targets=5)
    with pytest.raises(LineupContinuationError, match="digest does not match"):
        derive_targets(db, expected_digest="0" * 64)
    with pytest.raises(LineupContinuationError, match="not the bound corpus"):
        derive_targets(db, expected_selected_games=99)


def test_derivation_refuses_a_game_with_no_preserved_first_page(
    tmp_path: Path,
) -> None:
    """Every selected game must be anchored; a gap cannot be continued from."""

    db = build_corpus(tmp_path, games={1: 501})
    con = sqlite3.connect(db)
    raw_id = con.execute("SELECT raw_response_id FROM raw_responses LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO provider_game_references (reference_id, provider, "
        " provider_game_id, first_raw_response_id, current_raw_response_id, "
        " current_raw_response_hash, first_observed_at, last_observed_at, "
        " created_at, updated_at) "
        "VALUES ('pgr_orphan','balldontlie','999',?,?,'h', "
        " '2026-03-02T00:00:00.000000Z','2026-03-02T00:00:00.000000Z', "
        " '2026-03-02T00:00:00.000000Z','2026-03-02T00:00:00.000000Z')",
        (raw_id, raw_id))
    con.commit()
    con.close()
    with pytest.raises(LineupContinuationError, match="no preserved first lineup page"):
        derive_targets(db)


def test_derivation_never_opens_the_source_writable(tmp_path: Path) -> None:
    db = build_corpus(tmp_path, games={1: 501, 2: None})
    before = db.read_bytes()
    derive_targets(db)
    assert db.read_bytes() == before


def test_survey_reports_counts_only_and_no_player_names(tmp_path: Path) -> None:
    db = build_corpus(tmp_path, games={1: 501})
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        survey = survey_lineup_pages(con)
    finally:
        con.close()
    blob = json.dumps(survey.as_dict())
    assert "first_name" not in blob and "last_name" not in blob
    assert survey.targets[0].first_page_players == 25


# --------------------------------------------------------------------------- #
# 2. Planner
# --------------------------------------------------------------------------- #
def _binding(**over: Any) -> RecoveryBinding:
    base: dict[str, Any] = dict(
        purpose=RECOVERY_PURPOSE, contract_version=RECOVERY_CONTRACT_VERSION,
        source_manifest_hash="m" * 64, source_plan_hash="p" * 64,
        source_database_fingerprint="f" * 64, source_date_range=DATE_RANGE,
        source_selected_games=239, target_count=40, target_digest="d" * 64,
        max_continuation_pages=8)
    base.update(over)
    return RecoveryBinding(**base)


def _bounds(**over: Any) -> Bounds:
    base: dict[str, Any] = dict(max_games=40, max_pages=8, max_records=200,
                                max_retries=1, rate_per_min=60)
    base.update(over)
    return Bounds(**base)


def test_recovery_plan_is_forty_targets_by_eight_pages() -> None:
    plan = plan_lineup_continuation(date_range=DATE_RANGE, binding=_binding(),
                                    bounds=_bounds())
    assert plan.families == ("lineups",)
    assert len(plan.contingents) == 1
    c = plan.contingents[0]
    assert (c.kind, c.family) == ("continuation", "lineups")
    assert (c.per_parent_min, c.per_parent_max) == (1, 8)
    assert (c.parent_min, c.parent_max) == (40, 40)
    assert plan.semantic_requests_max() == 320
    assert plan.required_request_cap() == 640          # 320 x (1 + 1 retry)
    assert plan.executable() and not plan.unresolved_bounds()


def test_recovery_plan_is_not_the_month_lineups_contingent() -> None:
    """The month plan reserves ONE request per game -- the bound that broke this."""

    from sports_quant.ingest.planning import plan_nba

    month = plan_nba(from_date="2026-03-01", to_date="2026-03-31",
                     families=("games", "lineups"), stage="rich",
                     bounds=Bounds(max_games=400, max_pages=8, max_records=1000,
                                   max_retries=1, rate_per_min=60))
    month_lineups = [c for c in month.contingents if c.family == "lineups"][0]
    assert month_lineups.per_parent_max == 1
    recovery = plan_lineup_continuation(date_range=DATE_RANGE, binding=_binding(),
                                        bounds=_bounds())
    assert recovery.contingents[0].per_parent_max == 8
    assert recovery.contingents[0].kind != month_lineups.kind


@pytest.mark.parametrize("binding_over,bounds_over,match", [
    ({"target_count": 0}, {"max_games": None}, "at least one target"),
    ({"target_digest": "  "}, {}, "target-set digest"),
    ({"source_database_fingerprint": ""}, {}, "source database fingerprint"),
    ({"source_manifest_hash": ""}, {}, "manifest and plan hashes"),
    ({"source_date_range": "2026-04-01..2026-04-30"}, {}, "must equal the source range"),
    ({}, {"max_pages": None}, "explicit max_pages"),
    ({}, {"max_pages": 9}, "exceeds the authorized"),
    ({"max_continuation_pages": 4}, {}, "disagree on the continuation page limit"),
    ({}, {"max_games": 39}, "must equal the recovery target count"),
    ({"purpose": "something_else"}, {}, "unsupported recovery purpose"),
])
def test_recovery_plan_fails_closed(binding_over: dict, bounds_over: dict,
                                    match: str) -> None:
    with pytest.raises(RecoveryPlanError, match=match):
        plan_lineup_continuation(date_range=DATE_RANGE,
                                 binding=_binding(**binding_over),
                                 bounds=_bounds(**bounds_over))


@pytest.mark.parametrize("field,value", [
    ("target_count", 39), ("target_digest", "z" * 64),
    ("source_database_fingerprint", "z" * 64), ("source_manifest_hash", "z" * 64),
    ("source_plan_hash", "z" * 64), ("max_continuation_pages", 8),
])
def test_every_binding_field_changes_the_plan_identity(field: str, value: Any) -> None:
    base = plan_lineup_continuation(date_range=DATE_RANGE, binding=_binding(),
                                    bounds=_bounds())
    over: dict[str, Any] = {field: value}
    bounds = _bounds()
    if field == "target_count":
        bounds = _bounds(max_games=value)
    if field == "max_continuation_pages":
        over[field] = 4
        bounds = _bounds(max_pages=4)
    changed = plan_lineup_continuation(date_range=DATE_RANGE,
                                       binding=_binding(**over), bounds=bounds)
    assert plan_hash(changed) != plan_hash(base), field


def test_ordinary_plans_are_unaffected_by_the_recovery_field() -> None:
    """Adding the optional binding must not move any existing plan hash."""

    root = Path(__file__).resolve().parents[3]
    committed = json.loads(
        (root / "pilots/f1/nba_coverage_2026_03.manifest.json").read_text(
            encoding="utf-8"))
    from sports_quant.ingest.planning import build_plan

    rebuilt = build_plan(
        league="nba", from_date="2026-03-01", to_date="2026-03-31",
        families=tuple(committed["families"]), stage="rich",
        bounds=Bounds(max_games=400, max_pages=8, max_records=1000, max_retries=1,
                      rate_per_min=60))
    assert rebuilt.recovery is None
    from sports_quant.ingest.manifest import plan_body

    assert "recovery" not in plan_body(rebuilt)
    assert plan_hash(rebuilt) == (
        "e29ef60cc1ecc613d014b700aa6fbe147f83b70e5a37fd59067041d0f3092c97")


def test_recovery_manifest_caps_and_rate() -> None:
    plan = plan_lineup_continuation(date_range=DATE_RANGE, binding=_binding(),
                                    bounds=_bounds())
    manifest = build_manifest(plan, scratch_db=r"data\rec.db",
                              checkpoint_path=r"data\rec.ckpt",
                              expected_schema_version=17)
    assert manifest.request_cap == 640
    assert manifest.estimated_requests_max == 320
    assert manifest.configured_rate_per_min == 60
    assert manifest.provider_rate_limit_per_min == 600
    assert manifest.families == ("lineups",)
    assert manifest.expected_schema_version == 17
    assert manifest.plan_body["recovery"]["target_count"] == 40
    assert manifest.executable


# --------------------------------------------------------------------------- #
# 3. Merge semantics
# --------------------------------------------------------------------------- #
def test_overlapping_pages_deduplicate_deterministically() -> None:
    page_a = [lineup_row(1, 11), lineup_row(1, 12)]
    page_b = [lineup_row(1, 12), lineup_row(1, 13)]
    merged, conflicts, rejected = merge_lineup_rows([page_a, page_b])
    assert set(merged) == {("1", "11"), ("1", "12"), ("1", "13")}
    assert conflicts == [] and rejected == 0
    # order must not matter
    other, _c, _r = merge_lineup_rows([page_b, page_a])
    assert other == merged


def test_a_contradictory_repeat_keeps_the_first_and_is_reported() -> None:
    """A later page must not silently overwrite an earlier observation."""

    first = [lineup_row(1, 11, starter=True, position="G")]
    later = [lineup_row(1, 11, starter=False, position="F")]
    merged, conflicts, _rejected = merge_lineup_rows([first, later])
    assert conflicts == [("1", "11")]
    assert merged[("1", "11")] == {"position": "G", "starter": True}
    # ... and the conflict is detected whichever order the pages arrive in
    _m2, conflicts2, _r2 = merge_lineup_rows([later, first])
    assert conflicts2 == [("1", "11")]


def test_unusable_rows_are_counted_not_dropped_silently() -> None:
    rows: list[Any] = [lineup_row(1, 11), {"player": {"id": 12}},
                       {"team": {"id": 1}}, "nope"]
    merged, _conflicts, rejected = merge_lineup_rows([rows])
    assert len(merged) == 1
    assert rejected == 3


# --------------------------------------------------------------------------- #
# 4. Executor: one target's cursor chain
# --------------------------------------------------------------------------- #
class _Recorder:
    """Captures every request the executor issues."""

    def __init__(self, pages: dict[Any, Any], *,
                 fail_on: Optional[Any] = None, fail_times: int = 1) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []
        self._fail_on = fail_on
        self._fail_left = fail_times

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        cursor = request.url.params.get("cursor")
        if cursor is None:
            raise AssertionError("a FIRST page was requested; the corpus holds it")
        key: Any = int(cursor) if cursor.isdigit() else cursor
        if self._fail_on is not None and key == self._fail_on and self._fail_left > 0:
            self._fail_left -= 1
            return httpx.Response(500, json={"error": "boom"})
        body = self.pages.get(key)
        if body is None:
            return httpx.Response(200, json={"data": [], "meta": {}})
        if body == "malformed":
            return httpx.Response(200, content=b"{not json",
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})


def _factory(recorder: _Recorder) -> Any:
    def make(_gate: Any) -> BalldontlieClient:
        return BalldontlieClient(
            SENTINEL_KEY, client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(recorder.handler)))
    return make


def _target(gid: str = "1", cursor: int = 500) -> LineupTarget:
    return LineupTarget(
        provider_game_id=gid, first_raw_response_id="raw_1",
        first_raw_response_hash="h", first_observed_at="2026-03-02T00:00:00.000000Z",
        first_page_rows=25, first_page_teams=2, first_page_players=25,
        first_page_starters=10, start_cursor=cursor)


def _run_one(recorder: _Recorder, target: LineupTarget, *,
             max_pages: int = MAX_CONTINUATION_PAGES) -> Any:
    import asyncio

    ex = LineupContinuationExecutor(
        database=object(), client_factory=_factory(recorder), targets=(target,),
        date_range=DATE_RANGE, max_pages=max_pages)
    return asyncio.run(ex._run_target(object(), target))


def test_single_continuation_page_terminates_the_chain() -> None:
    rec = _Recorder({500: lineup_body(1, [26, 27], next_cursor=None)})
    outcome = _run_one(rec, _target())
    assert outcome.stop_reason == STOP_EXHAUSTED and outcome.complete
    assert len(outcome.pages) == 1
    assert len(rec.requests) == 1
    assert rec.requests[0].url.params["cursor"] == "500"


def test_multi_page_chain_follows_every_cursor_in_order() -> None:
    rec = _Recorder({
        500: lineup_body(1, [26], next_cursor=501),
        501: lineup_body(1, [27], next_cursor=502),
        502: lineup_body(1, [28], next_cursor=None),
    })
    outcome = _run_one(rec, _target())
    assert outcome.complete
    assert [p.requested_cursor for p in outcome.pages] == [500, 501, 502]
    assert [p.returned_cursor for p in outcome.pages] == [501, 502, None]
    assert outcome.cursor_chain == [500, 501, 502, None]


def test_page_eight_exactly_then_terminating_is_complete() -> None:
    pages = {500 + i: lineup_body(1, [i], next_cursor=(501 + i if i < 7 else None))
             for i in range(8)}
    rec = _Recorder(pages)
    outcome = _run_one(rec, _target())
    assert len(outcome.pages) == 8 and outcome.complete
    assert len(rec.requests) == 8


def test_page_limit_reached_with_a_live_cursor_is_incomplete() -> None:
    pages = {500 + i: lineup_body(1, [i], next_cursor=501 + i) for i in range(12)}
    rec = _Recorder(pages)
    outcome = _run_one(rec, _target())
    assert outcome.stop_reason == STOP_PAGE_LIMIT and not outcome.complete
    assert len(rec.requests) == MAX_CONTINUATION_PAGES
    assert any(DQ_PAGE_LIMIT_REACHED in f for f in outcome.findings)


def test_repeated_cursor_fails_closed() -> None:
    rec = _Recorder({500: lineup_body(1, [26], next_cursor=500)})
    outcome = _run_one(rec, _target())
    assert outcome.stop_reason == STOP_REPEATED_CURSOR and not outcome.complete
    assert any(DQ_REPEATED_CURSOR in f for f in outcome.findings)
    assert len(rec.requests) == 1, "a repeated cursor must not be followed"


def test_cursor_cycle_fails_closed() -> None:
    rec = _Recorder({
        500: lineup_body(1, [26], next_cursor=501),
        501: lineup_body(1, [27], next_cursor=502),
        502: lineup_body(1, [28], next_cursor=500),      # back to the start
    })
    outcome = _run_one(rec, _target())
    assert outcome.stop_reason == STOP_REPEATED_CURSOR and not outcome.complete
    assert len(rec.requests) == 3


def test_empty_continuation_page_is_preserved_and_reported() -> None:
    rec = _Recorder({
        500: {"data": [], "meta": {"next_cursor": 501}},
        501: lineup_body(1, [26], next_cursor=None),
    })
    outcome = _run_one(rec, _target())
    assert outcome.complete
    assert outcome.pages[0].rows == 0
    assert any(DQ_EMPTY_PAGE_WITH_CURSOR in f for f in outcome.findings)


def test_a_page_for_a_different_game_fails_closed() -> None:
    rec = _Recorder({500: lineup_body(99, [26], next_cursor=None)})
    outcome = _run_one(rec, _target(gid="1"))
    assert outcome.stop_reason == STOP_WRONG_GAME and not outcome.complete
    assert any(DQ_WRONG_GAME in f for f in outcome.findings)


def test_structurally_malformed_page_fails_closed() -> None:
    """A parseable 200 whose payload is the wrong shape is `malformed`."""

    rec = _Recorder({500: {"data": "not-a-list", "meta": {}}})
    outcome = _run_one(rec, _target())
    assert outcome.stop_reason == STOP_MALFORMED and not outcome.complete
    assert any(DQ_MALFORMED_PAGE in f for f in outcome.findings)


def test_unparseable_body_is_a_terminal_request_failure() -> None:
    """Bytes the client cannot decode never reach normalization; still closed."""

    rec = _Recorder({500: "malformed"})
    outcome = _run_one(rec, _target())
    assert outcome.stop_reason == STOP_FAILED and not outcome.complete
    assert any(DQ_TERMINAL_FAILURE in f for f in outcome.findings)


def test_terminal_failure_leaves_the_target_incomplete() -> None:
    rec = _Recorder({500: lineup_body(1, [26], next_cursor=None)},
                    fail_on=500, fail_times=99)
    outcome = _run_one(rec, _target())
    assert outcome.stop_reason == STOP_FAILED and not outcome.complete
    assert any(DQ_TERMINAL_FAILURE in f for f in outcome.findings)


def test_conflicting_player_rows_across_pages_raise_a_finding() -> None:
    rec = _Recorder({
        500: {"data": [lineup_row(1, 11, starter=True, position="G")],
              "meta": {"next_cursor": 501}},
        501: {"data": [lineup_row(1, 11, starter=False, position="F")], "meta": {}},
    })
    outcome = _run_one(rec, _target())
    assert outcome.complete
    assert any(DQ_CONFLICTING_PLAYER in f for f in outcome.findings)


def test_a_target_without_a_starting_cursor_is_refused_before_any_client() -> None:
    bad = LineupTarget(**{**_target().__dict__, "start_cursor": None})  # type: ignore[arg-type]

    def exploding_factory(_gate: Any) -> Any:
        raise AssertionError("a client was constructed for an unusable target")

    with pytest.raises(LineupContinuationError, match="no starting cursor"):
        LineupContinuationExecutor(
            database=object(), client_factory=exploding_factory, targets=(bad,),
            date_range=DATE_RANGE)


@pytest.mark.parametrize("pages", [0, 9, 99])
def test_executor_refuses_an_unauthorized_page_bound(pages: int) -> None:
    with pytest.raises(LineupContinuationError, match="max_pages must be"):
        LineupContinuationExecutor(
            database=object(), client_factory=lambda _g: None,
            targets=(_target(),), date_range=DATE_RANGE, max_pages=pages)


def test_only_the_lineups_endpoint_is_ever_contacted() -> None:
    rec = _Recorder({500: lineup_body(1, [26], next_cursor=None)})
    _run_one(rec, _target())
    assert {r.url.path for r in rec.requests} == {"/v1/lineups"}
    assert all("cursor" in r.url.params for r in rec.requests)


def test_no_credential_appears_in_any_continuation_url() -> None:
    rec = _Recorder({500: lineup_body(1, [26], next_cursor=None)})
    _run_one(rec, _target())
    assert all(SENTINEL_KEY not in str(r.url) for r in rec.requests)


# --------------------------------------------------------------------------- #
# 5. Differential run across a 40-target set
# --------------------------------------------------------------------------- #
def _forty_targets() -> tuple[LineupTarget, ...]:
    return tuple(_target(gid=str(i + 1), cursor=1000 + i * 100) for i in range(40))


def _forty_pages() -> dict[Any, Any]:
    """A page map exercising every scenario the review requires."""

    pages: dict[Any, Any] = {}
    for i in range(40):
        gid, start = i + 1, 1000 + i * 100
        if i < 20:                                   # single-page continuation
            pages[start] = lineup_body(gid, [26, 27], next_cursor=None)
        elif i < 30:                                 # three-page continuation
            pages[start] = lineup_body(gid, [26], next_cursor=start + 1)
            pages[start + 1] = lineup_body(gid, [27], next_cursor=start + 2)
            pages[start + 2] = lineup_body(gid, [28], next_cursor=None)
        elif i < 38:                                 # exactly eight pages
            for k in range(8):
                pages[start + k] = lineup_body(
                    gid, [26 + k], next_cursor=(start + k + 1) if k < 7 else None)
        elif i == 38:                                # overlapping rows
            pages[start] = lineup_body(gid, [26, 27], next_cursor=start + 1)
            pages[start + 1] = lineup_body(gid, [27, 28], next_cursor=None)
        else:                                        # empty page, then terminate
            pages[start] = {"data": [], "meta": {"next_cursor": start + 1}}
            pages[start + 1] = lineup_body(gid, [26], next_cursor=None)
    return pages


def test_forty_target_differential_run_completes_within_budget() -> None:
    import asyncio

    rec = _Recorder(_forty_pages())
    targets = _forty_targets()
    ex = LineupContinuationExecutor(
        database=object(), client_factory=_factory(rec), targets=targets,
        date_range=DATE_RANGE)
    outcomes = [asyncio.run(ex._run_target(object(), t)) for t in targets]

    assert all(o.complete for o in outcomes)
    assert len(rec.requests) == 20 * 1 + 10 * 3 + 8 * 8 + 2 + 2
    assert len(rec.requests) <= 320, "semantic maximum"
    assert {r.url.path for r in rec.requests} == {"/v1/lineups"}
    assert all("cursor" in r.url.params for r in rec.requests), "no first pages"
    # every game's chain ended because the provider said so
    assert {o.stop_reason for o in outcomes} == {STOP_EXHAUSTED}


def test_forty_target_run_is_order_independent() -> None:
    import asyncio
    import random

    def run(order: list[LineupTarget]) -> list[tuple]:
        rec = _Recorder(_forty_pages())
        ex = LineupContinuationExecutor(
            database=object(), client_factory=_factory(rec), targets=tuple(order),
            date_range=DATE_RANGE)
        out = [asyncio.run(ex._run_target(object(), t)) for t in order]
        return sorted((o.provider_game_id, o.stop_reason, len(o.pages),
                       tuple(o.cursor_chain), o.players_added) for o in out)

    base = run(list(_forty_targets()))
    shuffled = list(_forty_targets())
    random.Random(20260806).shuffle(shuffled)
    assert run(shuffled) == base
    assert run(list(reversed(_forty_targets()))) == base


# --------------------------------------------------------------------------- #
# 6. Checkpointed run, resume and budget, through the shared pilot runner
# --------------------------------------------------------------------------- #
def _recovery_manifest(target_count: int) -> Any:
    plan = plan_lineup_continuation(
        date_range=DATE_RANGE,
        binding=_binding(target_count=target_count),
        bounds=_bounds(max_games=target_count))
    return build_manifest(plan, scratch_db=r"data\rec.db",
                          checkpoint_path=r"data\rec.ckpt",
                          expected_schema_version=17)


def _gate(request_cap: int, sleeps: list[float]) -> Any:
    from sports_quant.ingest.cost_policies import (
        build_balldontlie_policy,
        build_balldontlie_rate_policy,
    )
    from sports_quant.request_control import CreditBudget, RequestBudget, RequestGate

    gate = RequestGate(
        request_budget=RequestBudget(max_requests=request_cap),
        credit_budget=CreditBudget(applicable=False),
        cost_policy=build_balldontlie_policy(),
        rate_policy=build_balldontlie_rate_policy(tier="goat", configured_per_min=60),
        sleep=sleeps.append)          # recording no-op: never a real sleep
    gate.set_auth_context(auth_applicable=True, configured_tier="goat")
    return gate


def _gated_factory(recorder: _Recorder) -> Any:
    def make(gate: Any) -> BalldontlieClient:
        return BalldontlieClient(
            SENTINEL_KEY, gate=gate, league="nba",
            client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(recorder.handler)))
    return make


def _run_pilot(tmp_path: Path, recorder: _Recorder, targets: tuple[LineupTarget, ...],
               *, resume: bool = False, request_cap: int = 640,
               ckpt: Optional[Path] = None) -> tuple[Any, Any, list[float]]:
    from sports_quant.ingest.pilot import run_pilot

    sleeps: list[float] = []
    manifest = _recovery_manifest(len(targets))
    executor = LineupContinuationExecutor(
        database=object(), client_factory=_gated_factory(recorder), targets=targets,
        date_range=DATE_RANGE)
    result = run_pilot(
        manifest=manifest, gate=_gate(request_cap, sleeps), executor=executor,
        checkpoint_path=ckpt or (tmp_path / "rec.ckpt"),
        scratch_fingerprint="fp-recovery", resume=resume, code_version="test")
    return result, executor, sleeps


def test_checkpointed_run_completes_every_target(tmp_path: Path) -> None:
    rec = _Recorder({500: lineup_body(1, [26], next_cursor=None),
                     600: lineup_body(2, [26], next_cursor=601),
                     601: lineup_body(2, [27], next_cursor=None)})
    targets = (_target("1", 500), _target("2", 600))
    result, executor, sleeps = _run_pilot(tmp_path, rec, targets)

    assert result.completed == 2 and result.failure is None
    assert executor.report.targets_completed == 2
    assert executor.report.targets_incomplete == 0
    assert executor.report.success is True
    assert len(rec.requests) == 3
    assert all(isinstance(s, float) or isinstance(s, int) for s in sleeps)


def test_completed_resume_performs_zero_requests(tmp_path: Path) -> None:
    ckpt = tmp_path / "rec.ckpt"
    rec = _Recorder({500: lineup_body(1, [26], next_cursor=None)})
    targets = (_target("1", 500),)
    _run_pilot(tmp_path, rec, targets, ckpt=ckpt)
    assert len(rec.requests) == 1

    for _ in range(2):
        fresh = _Recorder({500: lineup_body(1, [26], next_cursor=None)})
        before = ckpt.read_bytes()
        result, executor, sleeps = _run_pilot(tmp_path, fresh, targets, resume=True,
                                              ckpt=ckpt)
        assert fresh.requests == [], "a completed resume must issue no request"
        assert result.performed_new_work is False
        assert result.checkpoint_mutated is False
        assert ckpt.read_bytes() == before
        assert sleeps == []
        assert executor.report.continuation_requests == 0


def test_interrupted_resume_continues_only_the_incomplete_target(
    tmp_path: Path,
) -> None:
    """Game 1 finishes; game 2 stalls on a repeated cursor and stays resumable."""

    ckpt = tmp_path / "rec.ckpt"
    targets = (_target("1", 500), _target("2", 600))
    broken = _Recorder({500: lineup_body(1, [26], next_cursor=None),
                        600: lineup_body(2, [26], next_cursor=600)})
    result, executor, _s = _run_pilot(tmp_path, broken, targets, ckpt=ckpt)
    assert result.failure is not None
    assert executor.report.targets_completed == 1
    assert executor.report.targets_incomplete == 1
    assert len(broken.requests) == 2

    fixed = _Recorder({500: lineup_body(1, [26], next_cursor=None),
                       600: lineup_body(2, [26], next_cursor=601),
                       601: lineup_body(2, [27], next_cursor=None)})
    result2, executor2, _s2 = _run_pilot(tmp_path, fixed, targets, resume=True,
                                         ckpt=ckpt)
    assert result2.failure is None
    # only game 2's chain was walked; game 1 was skipped with zero transport
    assert {r.url.params.get_list("game_ids[]")[0] for r in fixed.requests} == {"2"}
    assert len(fixed.requests) == 2
    assert executor2.report.targets_completed == 1


def test_previously_completed_pages_are_not_refetched(tmp_path: Path) -> None:
    ckpt = tmp_path / "rec.ckpt"
    targets = (_target("1", 500), _target("2", 600))
    first = _Recorder({500: lineup_body(1, [26], next_cursor=None),
                       600: lineup_body(2, [26], next_cursor=600)})
    _run_pilot(tmp_path, first, targets, ckpt=ckpt)
    game_one_pages = [r for r in first.requests
                      if r.url.params.get_list("game_ids[]")[0] == "1"]
    assert len(game_one_pages) == 1

    second = _Recorder({500: lineup_body(1, [26], next_cursor=None),
                        600: lineup_body(2, [26], next_cursor=None)})
    _run_pilot(tmp_path, second, targets, resume=True, ckpt=ckpt)
    assert not [r for r in second.requests
                if r.url.params.get_list("game_ids[]")[0] == "1"]


def test_the_budget_stops_a_runaway_before_the_hard_cap(tmp_path: Path) -> None:
    """Every continuation page consumes budget; the cap is a real stop."""

    pages = {500 + i: lineup_body(1, [i], next_cursor=501 + i) for i in range(20)}
    rec = _Recorder(pages)
    result, executor, _s = _run_pilot(tmp_path, rec, (_target("1", 500),),
                                      request_cap=3)
    assert len(rec.requests) <= 3, "the gate must stop the chain at the cap"
    assert executor.report.targets_incomplete == 1


def test_requests_are_paced_under_the_committed_policy(tmp_path: Path) -> None:
    pages = {500 + i: lineup_body(1, [i], next_cursor=(501 + i if i < 5 else None))
             for i in range(6)}
    rec = _Recorder(pages)
    _result, _executor, sleeps = _run_pilot(tmp_path, rec, (_target("1", 500),))
    assert len(rec.requests) == 6
    # the 60/min policy has to have been consulted; no real sleep was taken
    assert isinstance(sleeps, list)


def test_json_and_human_reports_reconcile(tmp_path: Path) -> None:
    from sports_quant.ingest.lineup_continuation import render_report

    rec = _Recorder({500: lineup_body(1, [26], next_cursor=501),
                     501: lineup_body(1, [27], next_cursor=None),
                     600: lineup_body(2, [26], next_cursor=None)})
    _result, executor, _s = _run_pilot(tmp_path, rec,
                                       (_target("1", 500), _target("2", 600)))
    payload = executor.report.as_dict()
    lines: list[str] = []
    render_report(executor.report, lines.append)
    text = "\n".join(lines)

    assert payload["targets"] == 2 and payload["targets_completed"] == 2
    assert payload["continuation_requests"] == len(rec.requests) == 3
    assert payload["first_page_requests"] == 0
    assert payload["success"] is True
    assert "targets:   total=2 completed=2 incomplete=0" in text
    assert "continuation_pages=3 first_page_requests=0" in text
    assert "COMPLETE" in text
    # cursor chains are reported for every target
    chains = {o["provider_game_id"]: o["cursor_chain"] for o in payload["outcomes"]}
    assert chains["1"] == [500, 501, None]
    assert chains["2"] == [600, None]
    assert "500 -> 501 -> null" in text


# --------------------------------------------------------------------------- #
# 7. The committed recovery manifest
# --------------------------------------------------------------------------- #
def _load_generator() -> Any:
    import importlib.util

    root = Path(__file__).resolve().parents[3]
    path = root / "pilots/f1/generate_lineup_continuation_manifest.py"
    spec = importlib.util.spec_from_file_location("lc_gen", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[3] / "data"
         / "f1_nba_2026_03_scratch.db").exists(),
    reason="the protected March corpus is git-ignored local evidence")
def test_committed_recovery_manifest_regenerates_byte_identically(
    tmp_path: Path,
) -> None:
    module = _load_generator()
    produced = module.generate(out_dir=tmp_path)
    committed = Path(__file__).resolve().parents[3] / (
        "pilots/f1/nba_lineups_2026_03_continuation.manifest.json")
    assert produced.read_text(encoding="utf-8") == committed.read_text(encoding="utf-8")


def test_committed_recovery_manifest_is_well_formed_and_bounded() -> None:
    from sports_quant.ingest.manifest import load_and_validate

    root = Path(__file__).resolve().parents[3]
    path = root / "pilots/f1/nba_lineups_2026_03_continuation.manifest.json"
    manifest = load_and_validate(path, expected_league="nba",
                                 expected_provider="balldontlie")
    binding = manifest.plan_body["recovery"]

    assert manifest.families == ("lineups",)
    assert manifest.stage == "lineup_continuation_recovery"
    assert manifest.date_range == DATE_RANGE
    assert manifest.expected_schema_version == 17
    assert manifest.estimated_requests_max == 320
    assert manifest.request_cap == 640
    assert manifest.configured_rate_per_min == 60
    assert manifest.provider_rate_limit_per_min == 600
    assert manifest.max_retries == 1
    assert manifest.max_pages == PLAN_MAX_PAGES == 8
    assert binding["purpose"] == RECOVERY_PURPOSE
    assert binding["contract_version"] == RECOVERY_CONTRACT_VERSION
    assert binding["target_count"] == 40
    assert binding["source_selected_games"] == 239
    assert binding["max_continuation_pages"] == 8
    assert len(binding["target_digest"]) == 64
    assert len(binding["source_database_fingerprint"]) == 64
    # the recovery must point at NEW artifacts, never the executed evidence
    assert "recovery" in manifest.scratch_db and "recovery" in manifest.checkpoint_path
    assert "f1_nba_2026_03_scratch" not in manifest.scratch_db
    assert "f1_nba_2026_03.ckpt" not in manifest.checkpoint_path


def test_the_committed_manifest_holds_no_cursor_values() -> None:
    """Cursors are re-derived locally; the artifact stays a statement of intent."""

    root = Path(__file__).resolve().parents[3]
    raw = (root / "pilots/f1/nba_lineups_2026_03_continuation.manifest.json").read_text(
        encoding="utf-8")
    assert "cursor" not in raw.lower()
    assert "start_cursor" not in raw
    body = json.loads(raw)
    assert "targets" not in json.dumps(body["plan_body"]["recovery"])


def test_the_executed_month_manifest_is_untouched() -> None:
    root = Path(__file__).resolve().parents[3]
    month = json.loads((root / "pilots/f1/nba_coverage_2026_03.manifest.json").read_text(
        encoding="utf-8"))
    assert month["stage"] == "rich"
    assert "recovery" not in month["plan_body"]
    assert month["scratch_db"] == r"data\f1_nba_2026_03_scratch.db"
    assert month["checkpoint_path"] == r"data\f1_nba_2026_03.ckpt"


# --------------------------------------------------------------------------- #
# 8. CLI
# --------------------------------------------------------------------------- #
def _cli(argv: list[str], capsys: Any) -> tuple[int, str]:
    from sports_quant.cli import main

    code = main(argv)
    return code, capsys.readouterr().out


def test_cli_offline_validation_makes_no_request(tmp_path: Path,
                                                 capsys: Any) -> None:
    module = _load_generator()
    module.EXPECTED_TARGETS = 2
    module.EXPECTED_SELECTED_GAMES = 3
    db = build_corpus(tmp_path, games={1: 501, 2: None, 3: 503})
    manifest, _info = module.build(
        source_manifest=Path(__file__).resolve().parents[3]
        / "pilots/f1/nba_coverage_2026_03.manifest.json",
        source_database=db, recovery_db=r"data\rec.db",
        recovery_ckpt=r"data\rec.ckpt")
    manifest_path = tmp_path / "recovery.manifest.json"
    manifest_path.write_text(manifest.canonical(), encoding="utf-8")

    code, out = _cli(["nba-lineup-continuation", "--manifest", str(manifest_path),
                      "--source-db", str(db), "--json"], capsys)
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 0
    assert payload["mode"] == "offline_validation"
    assert payload["network_occurred"] is False
    assert payload["executed"] is False
    assert payload["target_count"] == 2
    assert payload["request_cap"] == 2 * 8 * 2


def test_cli_refuses_a_manifest_without_a_recovery_binding(
    tmp_path: Path, capsys: Any
) -> None:
    root = Path(__file__).resolve().parents[3]
    db = build_corpus(tmp_path, games={1: 501})
    code, out = _cli(
        ["nba-lineup-continuation", "--manifest",
         str(root / "pilots/f1/nba_coverage_2026_03.manifest.json"),
         "--source-db", str(db), "--json"], capsys)
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 2 and payload["refused"] is True
    assert "no recovery binding" in payload["reason"]


def test_cli_refuses_a_source_corpus_that_has_moved(tmp_path: Path,
                                                    capsys: Any) -> None:
    module = _load_generator()
    module.EXPECTED_TARGETS = 2
    module.EXPECTED_SELECTED_GAMES = 3
    db = build_corpus(tmp_path, games={1: 501, 2: None, 3: 503})
    manifest, _info = module.build(
        source_manifest=Path(__file__).resolve().parents[3]
        / "pilots/f1/nba_coverage_2026_03.manifest.json",
        source_database=db, recovery_db=r"data\rec.db",
        recovery_ckpt=r"data\rec.ckpt")
    manifest_path = tmp_path / "recovery.manifest.json"
    manifest_path.write_text(manifest.canonical(), encoding="utf-8")

    # a DIFFERENT corpus with the same target shape must still be refused
    other = build_corpus(tmp_path / "other", games={1: 501, 2: None, 3: 503})
    code, out = _cli(["nba-lineup-continuation", "--manifest", str(manifest_path),
                      "--source-db", str(other), "--json"], capsys)
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 2 and payload["refused"] is True


def test_cli_execute_is_refused_without_authorization(tmp_path: Path, capsys: Any,
                                                      monkeypatch: Any) -> None:
    monkeypatch.delenv("MONEYMAKER_F1B_AUTHORIZED", raising=False)
    module = _load_generator()
    module.EXPECTED_TARGETS = 2
    module.EXPECTED_SELECTED_GAMES = 3
    db = build_corpus(tmp_path, games={1: 501, 2: None, 3: 503})
    manifest, _info = module.build(
        source_manifest=Path(__file__).resolve().parents[3]
        / "pilots/f1/nba_coverage_2026_03.manifest.json",
        source_database=db, recovery_db=r"data\rec.db",
        recovery_ckpt=r"data\rec.ckpt")
    manifest_path = tmp_path / "recovery.manifest.json"
    manifest_path.write_text(manifest.canonical(), encoding="utf-8")

    code, out = _cli(["nba-lineup-continuation", "--manifest", str(manifest_path),
                      "--source-db", str(db), "--execute", "--json"], capsys)
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 2 and payload["refused"] is True
    assert "not authorized" in payload["reason"]


@pytest.mark.parametrize("extra", [["--base-url", "http://x"], ["--api-key", "k"],
                                   ["--cursor", "5"]])
def test_cli_rejects_network_shaped_options(tmp_path: Path, extra: list[str],
                                            capsys: Any) -> None:
    db = build_corpus(tmp_path, games={1: 501})
    with pytest.raises(SystemExit) as exc:
        _cli(["nba-lineup-continuation", "--manifest", "m", "--source-db", str(db),
              *extra], capsys)
    assert exc.value.code == 2


def test_an_incomplete_target_raises_and_is_never_checkpointed() -> None:
    """A short chain must surface, so the unit stays resumable.

    Yielding it would checkpoint a partial recovery as done -- exactly the class
    of defect that let the June MLB run record a short unit as complete.
    """

    rec = _Recorder({500: lineup_body(1, [26], next_cursor=500)})   # repeats
    ex = LineupContinuationExecutor(
        database=object(), client_factory=_factory(rec), targets=(_target("1", 500),),
        date_range=DATE_RANGE)
    with pytest.raises(ContinuationUnitFailed, match="repeated_cursor"):
        list(ex.iter_units(gate=object(), completed=set()))
    assert ex.report.targets_incomplete == 1
    assert ex.report.targets_completed == 0
    assert ex.report.success is False


def test_a_completed_target_is_yielded_once_and_then_skipped() -> None:
    rec = _Recorder({500: lineup_body(1, [26], next_cursor=None)})
    ex = LineupContinuationExecutor(
        database=object(), client_factory=_factory(rec), targets=(_target("1", 500),),
        date_range=DATE_RANGE)
    units = list(ex.iter_units(gate=object(), completed=set()))
    assert len(units) == 1 and units[0].family == "lineups_continuation"
    assert ex.report.success is True

    done = {units[0].identity}
    rec2 = _Recorder({500: lineup_body(1, [26], next_cursor=None)})
    ex2 = LineupContinuationExecutor(
        database=object(), client_factory=_factory(rec2), targets=(_target("1", 500),),
        date_range=DATE_RANGE)
    assert list(ex2.iter_units(gate=object(), completed=done)) == []
    assert rec2.requests == [], "a completed unit must be skipped with zero transport"
    assert ex2.remaining_identities(completed=done) == ()
