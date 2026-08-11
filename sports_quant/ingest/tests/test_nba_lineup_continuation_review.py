"""Regressions for the defects the independent review of the NBA lineup
continuation preparation confirmed
(``NBA_LINEUP_CONTINUATION_PREPARATION_REVIEW.md``).

The preparation was reviewed before any provider request, so none of these ever
reached live data. Each was a way the *future* recovery would have produced wrong
or unverifiable evidence:

1. **The executor persisted nothing.** After a successful continuation the
   recovery database still held zero raw responses, lineup rows, references,
   identity observations, DQ findings and runs -- the whole run existed only as
   in-memory ``ContinuationOutcome`` objects.
2. **Conflicts were resolved by traversal order.** Two contradictory
   observations of one player kept whichever page happened to be folded first,
   so the stored starter/position depended on arrival rather than evidence.
3. **Cursor typing was inconsistent.** ``fetch_lineups`` accepted opaque text
   while ``next_cursor``/``_next_cursor_of`` read only integers, so a text cursor
   could be sent but never read back -- a live chain would look finished.
4. **Every exception was a provider failure.** A ``TypeError`` or a
   ``sqlite3.Error`` in our own code was reported as ``DQ-NBA-LINEUP-R006``
   provider terminal failure.
5. **``--execute`` was not wired.** After its authorization and path checks the
   CLI returned a hard-coded refusal; no client, gate, recovery database or
   checkpoint runner existed.
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
from sports_quant.ingest.lineup_continuation import (
    DQ_CHAIN_PROVENANCE,
    DQ_CONFLICTING_PLAYER,
    STOP_EXHAUSTED,
    LineupContinuationExecutor,
    LineupTarget,
    merge_lineup_rows,
)
from sports_quant.providers.balldontlie import BalldontlieClient, _validate_cursor

DATE_RANGE = "2026-03-01..2026-03-31"
SENTINEL_KEY = "sk-continuation-review-must-never-be-read"


def _discard(_line: str) -> None:
    """Swallow output for a setup call whose text is not under test."""


def row(gid: int, pid: int, *, rid: Optional[int] = None, team: int = 1,
        starter: bool = False, position: str = "G") -> dict[str, Any]:
    return {"game_id": gid, "id": rid if rid is not None else 5_000_000 + pid,
            "player": {"id": pid, "first_name": "P", "last_name": str(pid),
                       "team_id": team},
            "team": {"id": team, "full_name": f"Team {team}", "abbreviation": "TM",
                     "city": "City", "name": "Name"},
            "position": position, "starter": starter}


def body(gid: int, players: list[int], nxt: Optional[int], **kw: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {"per_page": 25}
    if nxt is not None:
        meta["next_cursor"] = nxt
    return {"data": [row(gid, p, **kw) for p in players], "meta": meta}


def target(gid: str = "1", cursor: int = 500) -> LineupTarget:
    return LineupTarget(provider_game_id=gid, first_raw_response_id="raw_first",
                        first_raw_response_hash="hash_first",
                        first_observed_at="2026-03-02T00:00:00.000000Z",
                        first_page_rows=25, first_page_teams=2, first_page_players=25,
                        first_page_starters=10, start_cursor=cursor)


class Recorder:
    def __init__(self, pages: dict[Any, Any]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        cur = request.url.params.get("cursor")
        if cur is None:
            raise AssertionError("a FIRST page was requested; the corpus holds it")
        return httpx.Response(200, json=self.pages.get(int(cur), {"data": [], "meta": {}}),
                              headers={"content-type": "application/json"})


def factory(rec: Recorder) -> Any:
    def make(_gate: Any) -> BalldontlieClient:
        return BalldontlieClient(
            SENTINEL_KEY, client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(rec.handler)))
    return make


def recovery_db(tmp_path: Path, name: str = "recovery.db") -> Database:
    path = tmp_path / name
    initialize_database(path)
    return Database(path)


def counts(db: Database) -> dict[str, int]:
    with db.connection() as conn:
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for (t,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")}


# --------------------------------------------------------------------------- #
# 1. Persistence
# --------------------------------------------------------------------------- #
def test_a_completed_continuation_persists_real_recovery_evidence(
    tmp_path: Path,
) -> None:
    """The recovery must be reconstructable from the database, not from a log."""

    db = recovery_db(tmp_path)
    rec = Recorder({500: body(1, [26], 501), 501: body(1, [27], None)})
    ex = LineupContinuationExecutor(database=db, client_factory=factory(rec),
                                    targets=(target("1", 500),),
                                    date_range=DATE_RANGE)
    units = list(ex.iter_units(gate=object(), completed=set()))

    assert len(units) == 1
    after = counts(db)
    assert after["raw_responses"] == 2, "both continuation pages must be stored"
    assert after["ingestion_runs"] == 1
    assert after["lineup_snapshots"] >= 1 and after["lineup_players"] >= 2
    assert after["provider_game_references"] == 1
    assert after["provider_team_references"] >= 1
    assert after["provider_player_references"] >= 2
    assert after["provider_player_identity_snapshots"] >= 2
    assert after["data_quality_issues"] >= 1


def test_stored_responses_carry_the_requested_and_returned_cursors(
    tmp_path: Path,
) -> None:
    """Cursor-chain provenance must be durable, not merely computed."""

    db = recovery_db(tmp_path)
    rec = Recorder({500: body(1, [26], 501), 501: body(1, [27], None)})
    ex = LineupContinuationExecutor(database=db, client_factory=factory(rec),
                                    targets=(target("1", 500),),
                                    date_range=DATE_RANGE)
    list(ex.iter_units(gate=object(), completed=set()))

    with db.connection() as conn:
        stored = conn.execute(
            "SELECT request_params_json, body FROM raw_responses "
            "ORDER BY raw_response_id").fetchall()
        note = conn.execute(
            "SELECT detail_json FROM data_quality_issues WHERE rule_code = ?",
            (DQ_CHAIN_PROVENANCE,)).fetchone()

    requested = [json.loads(p)["cursor"] for p, _b in stored]
    returned = [(json.loads(b).get("meta") or {}).get("next_cursor") for _p, b in stored]
    assert sorted(requested) == ["500", "501"]           # the cursor we SENT
    assert sorted(x for x in returned if x) == [501]      # the cursor we GOT BACK

    detail = json.loads(note[0])
    assert detail["start_cursor"] == 500
    assert detail["cursor_chain"] == [500, 501]
    assert detail["returned_cursors"] == [501, None]
    assert detail["page_ordinals"] == [1, 2]
    assert detail["first_page_raw_response_id"] == "raw_first"
    assert detail["complete"] is True


def test_a_failed_chain_still_persists_the_pages_it_did_fetch(
    tmp_path: Path,
) -> None:
    """Evidence of a failure is still evidence."""

    from sports_quant.ingest.lineup_continuation import ContinuationUnitFailed

    db = recovery_db(tmp_path)
    rec = Recorder({500: body(1, [26], 500)})            # repeats immediately
    ex = LineupContinuationExecutor(database=db, client_factory=factory(rec),
                                    targets=(target("1", 500),),
                                    date_range=DATE_RANGE)
    with pytest.raises(ContinuationUnitFailed):
        list(ex.iter_units(gate=object(), completed=set()))

    after = counts(db)
    assert after["raw_responses"] == 1
    assert after["ingestion_runs"] == 1
    with db.connection() as conn:
        status = conn.execute("SELECT status, error_type FROM ingestion_runs").fetchone()
        codes = {r[0] for r in conn.execute(
            "SELECT rule_code FROM data_quality_issues")}
    assert status[0] == "failed" and status[1] == "repeated_cursor"
    assert "DQ-NBA-LINEUP-R001" in codes


def test_no_recovery_evidence_is_written_when_nothing_was_fetched(
    tmp_path: Path,
) -> None:
    db = recovery_db(tmp_path)
    rec = Recorder({})
    ex = LineupContinuationExecutor(database=db, client_factory=factory(rec),
                                    targets=(target("1", 500),),
                                    date_range=DATE_RANGE)
    list(ex.iter_units(gate=object(), completed=set()))
    # an empty terminal page IS a page and is stored; nothing beyond it appears
    after = counts(db)
    assert after["lineup_players"] == 0
    assert after["raw_responses"] == 1


# --------------------------------------------------------------------------- #
# 2. Conflict resolution is provenance-ordered, not traversal-ordered
# --------------------------------------------------------------------------- #
def test_contradictory_rows_resolve_identically_under_opposite_traversal() -> None:
    """Same evidence, opposite iteration order, identical stored value."""

    page1 = (1, [row(1, 11, rid=10, starter=True, position="G")])
    page2 = (2, [row(1, 11, rid=20, starter=False, position="F")])

    forward, conflicts_f, _r = merge_lineup_rows([page1, page2])
    backward, conflicts_b, _r2 = merge_lineup_rows([page2, page1])

    assert forward == backward, "the retained observation must not depend on order"
    assert conflicts_f == conflicts_b == [("1", "11")]
    # the provenance-earliest observation (page 1, lowest row id) is the one kept
    assert forward[("1", "11")] == {"position": "G", "starter": True}


def test_row_order_within_a_page_does_not_change_the_result() -> None:
    import random

    rows = [row(1, 11, rid=10), row(1, 12, rid=11, starter=True),
            row(1, 13, rid=12, position="C")]
    results = set()
    for seed in range(8):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        merged, _c, _r = merge_lineup_rows([(1, shuffled)])
        results.add(json.dumps({f"{t}:{p}": v for (t, p), v in sorted(merged.items())},
                               sort_keys=True))
    assert len(results) == 1


def test_page_order_across_many_pages_does_not_change_the_result() -> None:
    import random

    pages = [(i + 1, [row(1, 20 + i, rid=100 + i, starter=(i % 2 == 0))])
             for i in range(6)]
    pages.append((3, [row(1, 20, rid=999, starter=False, position="F")]))
    results = set()
    for seed in range(8):
        shuffled = list(pages)
        random.Random(seed).shuffle(shuffled)
        merged, conflicts, _r = merge_lineup_rows(shuffled)
        results.add(json.dumps(
            {f"{t}:{p}": v for (t, p), v in sorted(merged.items())},
            sort_keys=True) + "|" + str(conflicts))
    assert len(results) == 1


def test_a_conflict_is_reported_and_does_not_silently_overwrite(
    tmp_path: Path,
) -> None:
    db = recovery_db(tmp_path)
    rec = Recorder({
        500: {"data": [row(1, 11, rid=10, starter=True, position="G")],
              "meta": {"next_cursor": 501}},
        501: {"data": [row(1, 11, rid=20, starter=False, position="F")], "meta": {}},
    })
    ex = LineupContinuationExecutor(database=db, client_factory=factory(rec),
                                    targets=(target("1", 500),),
                                    date_range=DATE_RANGE)
    list(ex.iter_units(gate=object(), completed=set()))
    outcome = ex.report.outcomes[0]
    assert outcome.stop_reason == STOP_EXHAUSTED
    assert any(DQ_CONFLICTING_PLAYER in f for f in outcome.findings)
    with db.connection() as conn:
        kept = [tuple(r) for r in conn.execute(
            "SELECT position, is_starter FROM lineup_players")]
        codes = {r[0] for r in conn.execute(
            "SELECT rule_code FROM data_quality_issues")}
    assert kept == [("G", 1)], "the provenance-earliest observation is stored"
    assert DQ_CONFLICTING_PLAYER in codes


# --------------------------------------------------------------------------- #
# 3. Cursor typing is consistent at both ends
# --------------------------------------------------------------------------- #
def test_the_cursor_contract_is_integer_only_at_both_ends() -> None:
    from sports_quant.ingest.lineup_continuation import _next_cursor_of
    from sports_quant.providers.balldontlie import next_cursor

    assert _validate_cursor(5615604) == 5615604
    assert next_cursor({"meta": {"next_cursor": 5615604}}) == 5615604
    assert _next_cursor_of(json.dumps({"meta": {"next_cursor": 5615604}})) == (5615604,
                                                                              True)
    # text is refused on the WRITE side because it cannot be read back
    with pytest.raises(ValueError, match="integer"):
        _validate_cursor("opaque-token")
    assert next_cursor({"meta": {"next_cursor": "opaque-token"}}) is None
    assert _next_cursor_of(json.dumps({"meta": {"next_cursor": "opaque"}})) == (None,
                                                                               True)


def test_zero_is_a_real_cursor_not_an_absent_one() -> None:
    from sports_quant.providers.balldontlie import next_cursor

    assert _validate_cursor(0) == 0
    assert next_cursor({"meta": {"next_cursor": 0}}) == 0


# --------------------------------------------------------------------------- #
# 4. Exception classification
# --------------------------------------------------------------------------- #
class _Boom:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.closed = 0

    async def fetch_lineups(self, **_kw: Any) -> Any:
        raise self._exc

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("exc", [TypeError("our bug"),
                                 sqlite3.OperationalError("database is locked"),
                                 KeyError("bug"), AttributeError("bug")])
def test_our_own_errors_are_not_reported_as_provider_failures(exc: Exception) -> None:
    """A programming or database fault must surface as itself.

    Labelling it ``DQ-NBA-LINEUP-R006`` would blame the provider for our defect
    and hide a real bug behind a resumable "terminal failure".
    """

    client = _Boom(exc)
    ex = LineupContinuationExecutor(database=object(), client_factory=lambda _g: client,
                                    targets=(target(),), date_range=DATE_RANGE,
                                    persist=False)
    with pytest.raises(type(exc)):
        asyncio.run(ex._run_target(object(), target()))
    assert client.closed == 1, "the client is still closed exactly once"


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
])
def test_transport_errors_are_provider_terminal_failures(exc: Exception) -> None:
    from sports_quant.ingest.lineup_continuation import (
        DQ_TERMINAL_FAILURE,
        STOP_FAILED,
    )

    client = _Boom(exc)
    ex = LineupContinuationExecutor(database=object(), client_factory=lambda _g: client,
                                    targets=(target(),), date_range=DATE_RANGE,
                                    persist=False)
    outcome, _responses = asyncio.run(ex._run_target(object(), target()))
    assert outcome.stop_reason == STOP_FAILED and not outcome.complete
    assert any(DQ_TERMINAL_FAILURE in f for f in outcome.findings)
    assert client.closed == 1


# --------------------------------------------------------------------------- #
# 5. The --execute path is genuinely wired
# --------------------------------------------------------------------------- #
class _FakeSecret:
    def get_secret_value(self) -> str:
        return "mock-key-never-a-real-credential"


class _FakeSettings:
    nba_data_api_key = _FakeSecret()


def _recovery_manifest(tmp_path: Path, source: Path) -> Path:
    import importlib.util

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "lc_gen", root / "pilots/f1/generate_lineup_continuation_manifest.py")
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The generator's expectations are the March constants; a fixture corpus has
    # its own shape, so point them at it rather than at the real month.
    survey = module.derive_targets(source)
    module.EXPECTED_TARGETS = len(survey.targets)
    module.EXPECTED_SELECTED_GAMES = survey.selected_games
    # Same reason, for the schema pin: the committed generator declares v17
    # because the preserved March corpus IS v17 and its manifest hash is recorded
    # in the preserved checkpoint. This fixture builds its corpus fresh, so it is
    # at CURRENT_SCHEMA_VERSION, and the CLI's exact-match guard would correctly
    # refuse a v17 manifest against it. Overriding here keeps the test about the
    # CLI's behaviour rather than about the pin.
    module.SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
    manifest, _info = module.build(
        source_manifest=root / "pilots/f1/nba_coverage_2026_03.manifest.json",
        source_database=source, recovery_db=r"data\rec.db",
        recovery_ckpt=r"data\rec.ckpt")
    path = tmp_path / "recovery.manifest.json"
    path.write_text(manifest.canonical(), encoding="utf-8")
    return path


def _source_corpus(tmp_path: Path, games: dict[int, Optional[int]]) -> Path:
    from sports_quant.ingest.nba_ingestor import ingest_nba

    db_path = tmp_path / "march.db"
    initialize_database(db_path)
    database = Database(db_path)

    def payload(gid: int) -> dict[str, Any]:
        return {"id": gid, "date": "2026-03-02", "datetime": "2026-03-02T00:30:00Z",
                "season": 2025, "status": "Final", "period": 4, "postseason": False,
                "home_team": {"id": 1, "full_name": "Home", "abbreviation": "HOM"},
                "visitor_team": {"id": 2, "full_name": "Away", "abbreviation": "AWY"},
                "home_team_score": 110, "visitor_team_score": 104}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/games/"):
            out: Any = {"data": payload(int(path.rsplit("/", 1)[1])), "meta": {}}
        elif path == "/v1/games":
            out = {"data": [payload(g) for g in games], "meta": {}}
        elif path == "/v1/lineups":
            gid = int(request.url.params.get_list("game_ids[]")[0])
            out = body(gid, list(range(1, 26)), games[gid])
        else:
            out = {"data": [], "meta": {}}
        return httpx.Response(200, json=out,
                              headers={"content-type": "application/json"})

    async def one(gid: int) -> Any:
        client = BalldontlieClient(
            SENTINEL_KEY, client=build_readonly_client(
                base_url="https://api.balldontlie.io",
                policy=ReadOnlyHTTPPolicy.for_balldontlie(),
                inner_transport=httpx.MockTransport(handler)))
        try:
            return await ingest_nba(database=database, client=client,
                                    from_date="2026-03-01", to_date="2026-03-31",
                                    game_id=gid, includes=("lineups",), dry_run=False)
        finally:
            await client.aclose()

    for gid in games:
        assert asyncio.run(one(gid)).status == "succeeded"
    return db_path


def test_execute_runs_the_whole_production_path_under_a_mock_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prepared path must actually execute, not return a hard-coded refusal."""

    from sports_quant.cli import run_nba_lineup_continuation

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501, 2: None, 3: 503})
    manifest = _recovery_manifest(tmp_path, source)
    rec = Recorder({501: body(1, [26], None), 503: body(3, [26], None)})

    lines: list[str] = []
    code = run_nba_lineup_continuation(
        manifest_path=manifest, source_db=source,
        recovery_db=tmp_path / "rec.db", checkpoint=tmp_path / "rec.ckpt",
        execute=True, as_json=True, out=lines.append,
        client_factory=factory(rec), settings=_FakeSettings())
    payload = json.loads(lines[-1])

    assert code == 0
    assert payload["executed"] is True and payload["success"] is True
    assert payload["targets_completed"] == 2 and payload["targets_incomplete"] == 0
    assert payload["continuation_requests"] == 2 == len(rec.requests)
    assert all("cursor" in r.url.params for r in rec.requests)
    assert {r.url.path for r in rec.requests} == {"/v1/lineups"}
    assert (tmp_path / "rec.db").exists() and (tmp_path / "rec.ckpt").exists()

    con = sqlite3.connect(f"file:{(tmp_path / 'rec.db').as_posix()}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0] == 2
    assert con.execute(
        "SELECT MAX(version) FROM schema_versions").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


def test_execute_completed_resume_does_no_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_quant.cli import run_nba_lineup_continuation

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501, 2: None})
    manifest = _recovery_manifest(tmp_path, source)
    rec = Recorder({501: body(1, [26], None)})
    kw: dict[str, Any] = dict(
        manifest_path=manifest, source_db=source, recovery_db=tmp_path / "rec.db",
        checkpoint=tmp_path / "rec.ckpt", execute=True, as_json=True,
        settings=_FakeSettings())
    run_nba_lineup_continuation(out=_discard, client_factory=factory(rec), **kw)
    assert len(rec.requests) == 1
    before_db = (tmp_path / "rec.db").read_bytes()
    before_ck = (tmp_path / "rec.ckpt").read_bytes()

    fresh = Recorder({501: body(1, [26], None)})
    lines: list[str] = []
    code = run_nba_lineup_continuation(out=lines.append, client_factory=factory(fresh),
                                       resume=True, **kw)
    payload = json.loads(lines[-1])
    assert code == 0
    assert fresh.requests == [], "a completed resume must issue no request"
    assert payload["performed_new_work"] is False
    assert payload["checkpoint_mutated"] is False
    assert (tmp_path / "rec.db").read_bytes() == before_db
    assert (tmp_path / "rec.ckpt").read_bytes() == before_ck


@pytest.mark.parametrize("over,match", [
    ({"recovery_db": None}, "explicit --recovery-db"),
    ({"checkpoint": None}, "explicit --recovery-db"),
])
def test_execute_requires_explicit_recovery_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, over: dict, match: str
) -> None:
    from sports_quant.cli import run_nba_lineup_continuation

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501})
    manifest = _recovery_manifest(tmp_path, source)
    kw: dict[str, Any] = dict(
        manifest_path=manifest, source_db=source, recovery_db=tmp_path / "rec.db",
        checkpoint=tmp_path / "rec.ckpt", execute=True, as_json=True,
        settings=_FakeSettings())
    kw.update(over)
    lines: list[str] = []

    def exploding(_gate: Any) -> Any:
        raise AssertionError("a client was constructed despite a bad bound")

    code = run_nba_lineup_continuation(out=lines.append, client_factory=exploding, **kw)
    assert code == 2 and match in json.loads(lines[-1])["reason"]


def test_execute_refuses_to_write_onto_the_protected_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_quant.cli import run_nba_lineup_continuation

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501})
    manifest = _recovery_manifest(tmp_path, source)
    before = source.read_bytes()
    lines: list[str] = []

    def exploding(_gate: Any) -> Any:
        raise AssertionError("a client was constructed despite a bad bound")

    code = run_nba_lineup_continuation(
        manifest_path=manifest, source_db=source, recovery_db=source,
        checkpoint=tmp_path / "rec.ckpt", execute=True, as_json=True,
        out=lines.append, client_factory=exploding, settings=_FakeSettings())
    assert code == 2
    assert "protected source corpus" in json.loads(lines[-1])["reason"]
    assert source.read_bytes() == before


def test_execute_refuses_without_a_configured_key_before_any_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_quant.cli import run_nba_lineup_continuation

    class _Empty:
        nba_data_api_key = None

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501})
    manifest = _recovery_manifest(tmp_path, source)
    lines: list[str] = []

    def exploding(_gate: Any) -> Any:
        raise AssertionError("a client was constructed without a key")

    code = run_nba_lineup_continuation(
        manifest_path=manifest, source_db=source, recovery_db=tmp_path / "rec.db",
        checkpoint=tmp_path / "rec.ckpt", execute=True, as_json=True,
        out=lines.append, client_factory=exploding, settings=_Empty())
    reason = json.loads(lines[-1])["reason"]
    assert code == 2 and "no BALLDONTLIE API key" in reason
    assert not (tmp_path / "rec.db").exists(), "no database before authentication"
    assert not (tmp_path / "rec.ckpt").exists()


def test_the_key_value_is_never_printed_or_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_quant.cli import run_nba_lineup_continuation

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501, 2: None})
    manifest = _recovery_manifest(tmp_path, source)
    rec = Recorder({501: body(1, [26], None)})
    lines: list[str] = []
    run_nba_lineup_continuation(
        manifest_path=manifest, source_db=source, recovery_db=tmp_path / "rec.db",
        checkpoint=tmp_path / "rec.ckpt", execute=True, as_json=True,
        out=lines.append, client_factory=factory(rec), settings=_FakeSettings())
    blob = "\n".join(lines)
    assert "mock-key-never-a-real-credential" not in blob
    assert SENTINEL_KEY not in blob
    con = sqlite3.connect(f"file:{(tmp_path / 'rec.db').as_posix()}?mode=ro", uri=True)
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'"):
        for col in [c[1] for c in con.execute(f'PRAGMA table_info("{table}")')]:
            hit = con.execute(f'SELECT COUNT(*) FROM "{table}" WHERE '
                              f'CAST("{col}" AS TEXT) LIKE ?',
                              ("%mock-key-never-a-real-credential%",)).fetchone()[0]
            assert hit == 0, f"{table}.{col}"
    con.close()


def test_offline_validation_loads_no_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default path must be provably credential-free."""

    from sports_quant.cli import run_nba_lineup_continuation

    source = _source_corpus(tmp_path, {1: 501, 2: None})
    manifest = _recovery_manifest(tmp_path, source)

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("offline validation reached a credential path")

    # patched only AFTER the fixture exists, so anything the offline path itself
    # touches is what trips these
    monkeypatch.setattr("sports_quant.config.load_settings", boom)
    monkeypatch.setattr("sports_quant.cli.load_settings", boom)
    monkeypatch.setattr("sports_quant.providers.balldontlie.BalldontlieClient.__init__",
                        boom)
    lines: list[str] = []
    code = run_nba_lineup_continuation(manifest_path=manifest, source_db=source,
                                       as_json=True, out=lines.append)
    payload = json.loads(lines[-1])
    assert code == 0
    assert payload["executed"] is False and payload["network_occurred"] is False


# --------------------------------------------------------------------------- #
# 6. Recovery-database contract
# --------------------------------------------------------------------------- #
def test_a_nonempty_recovery_database_is_refused_without_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second fresh run must not append to someone else's evidence."""

    from sports_quant.cli import run_nba_lineup_continuation

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501, 2: None})
    manifest = _recovery_manifest(tmp_path, source)
    kw: dict[str, Any] = dict(
        manifest_path=manifest, source_db=source, recovery_db=tmp_path / "rec.db",
        checkpoint=tmp_path / "rec.ckpt", execute=True, as_json=True,
        settings=_FakeSettings())
    run_nba_lineup_continuation(out=_discard,
                                client_factory=factory(Recorder(
                                    {501: body(1, [26], None)})), **kw)
    lines: list[str] = []
    code = run_nba_lineup_continuation(out=lines.append,
                                       client_factory=factory(Recorder({})), **kw)
    assert code == 2
    assert "already holds continuation evidence" in json.loads(lines[-1])["reason"]


def test_resume_without_an_existing_recovery_database_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_quant.cli import run_nba_lineup_continuation

    monkeypatch.setenv("MONEYMAKER_F1B_AUTHORIZED", "1")
    source = _source_corpus(tmp_path, {1: 501})
    manifest = _recovery_manifest(tmp_path, source)
    lines: list[str] = []
    code = run_nba_lineup_continuation(
        manifest_path=manifest, source_db=source, recovery_db=tmp_path / "missing.db",
        checkpoint=tmp_path / "rec.ckpt", execute=True, resume=True, as_json=True,
        out=lines.append, client_factory=lambda _g: None, settings=_FakeSettings())
    assert code == 2
    assert "does not exist" in json.loads(lines[-1])["reason"]


# --------------------------------------------------------------------------- #
# 7. Merge contract with page one (defined now, performed later)
# --------------------------------------------------------------------------- #
def test_page_one_participates_in_the_merge_and_is_never_refetched() -> None:
    """Page one lives in the March corpus; the merge must fold it in, not skip it.

    Continuation rows alone are NOT a lineup: here page one supplies the starters
    and the continuation supplies the rest, and only the union is the whole team.
    """

    page_one = (1, [row(1, p, rid=1000 + p, team=1, starter=p <= 5)
                    for p in range(1, 26)])
    continuation = (2, [row(1, p, rid=2000 + p, team=1) for p in range(26, 30)])

    merged, conflicts, _r = merge_lineup_rows([page_one, continuation])
    assert len(merged) == 29
    assert conflicts == []
    starters = [k for k, v in merged.items() if v["starter"]]
    assert len(starters) == 5

    continuation_only, _c, _r2 = merge_lineup_rows([continuation])
    assert len(continuation_only) == 4
    assert continuation_only != merged, "a continuation alone is not the lineup"


def test_an_identical_overlap_between_page_one_and_a_continuation_collapses() -> None:
    shared = row(1, 25, rid=1025, team=1, starter=False, position="G")
    page_one = (1, [row(1, p, rid=1000 + p, team=1) for p in range(1, 25)] + [shared])
    continuation = (2, [shared, row(1, 26, rid=2026, team=1)])
    merged, conflicts, _r = merge_lineup_rows([page_one, continuation])
    assert len(merged) == 26 and conflicts == []


def test_a_player_may_not_appear_for_both_teams_without_a_conflict() -> None:
    """``(team, player)`` is the identity, so a cross-team appearance is visible."""

    merged, _c, _r = merge_lineup_rows([
        (1, [row(1, 11, rid=10, team=1)]),
        (2, [row(1, 11, rid=20, team=2)]),
    ])
    assert set(merged) == {("1", "11"), ("2", "11")}


def test_a_merged_lineup_yields_exactly_two_team_snapshots(tmp_path: Path) -> None:
    db = recovery_db(tmp_path)
    rec = Recorder({500: {"data": [row(1, 26, rid=10, team=1),
                                   row(1, 27, rid=11, team=2)], "meta": {}}})
    ex = LineupContinuationExecutor(database=db, client_factory=factory(rec),
                                    targets=(target("1", 500),),
                                    date_range=DATE_RANGE)
    list(ex.iter_units(gate=object(), completed=set()))
    with db.connection() as conn:
        teams = conn.execute(
            "SELECT COUNT(DISTINCT provider_team_id) FROM lineup_snapshots").fetchone()[0]
        confirmed = [tuple(r) for r in conn.execute(
            "SELECT DISTINCT is_confirmed FROM lineup_snapshots")]
    assert teams == 2
    assert confirmed == [(0,)], "a continuation never claims confirmed pregame starters"


# --------------------------------------------------------------------------- #
# 8. Determinism of the persisted result
# --------------------------------------------------------------------------- #
def test_two_runs_over_shuffled_targets_persist_identical_semantics(
    tmp_path: Path,
) -> None:
    import random

    pages = {500: body(1, [26], 502), 502: body(1, [27], None),
             600: body(2, [28], None), 700: body(3, [29], 701),
             701: body(3, [30], None)}
    targets = (target("1", 500), target("2", 600), target("3", 700))

    def run(order: tuple[LineupTarget, ...], name: str) -> list[tuple]:
        db = recovery_db(tmp_path, name)
        ex = LineupContinuationExecutor(database=db, client_factory=factory(
            Recorder(pages)), targets=order, date_range=DATE_RANGE)
        list(ex.iter_units(gate=object(), completed=set()))
        with db.connection() as conn:
            return sorted(tuple(r) for r in conn.execute(
                "SELECT provider_game_id, provider_team_id, provider_player_id, "
                "position, is_starter FROM lineup_snapshots "
                "JOIN lineup_players USING(lineup_id)"))

    base = run(targets, "a.db")
    shuffled = list(targets)
    random.Random(20260807).shuffle(shuffled)
    assert run(tuple(shuffled), "b.db") == base
    assert run(tuple(reversed(targets)), "c.db") == base
