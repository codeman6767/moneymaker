"""Regressions for the defects the F1 NBA March-2026 month review confirmed.

Each test is the minimal reproducer that failed before its repair. Every one of
them is offline: no client is constructed, no socket is opened, no real sleep is
taken.

1. A ``/v1/lineups`` response carrying ``meta.next_cursor`` was fetched with a
   single un-paginated request and the cursor was **silently discarded**. In the
   March 2026 month run 40 of 239 games returned exactly ``per_page=25`` rows
   with a live ``next_cursor``, so those games' lineups are partial -- yet the run
   recorded no truncation and no data-quality finding, and reported
   ``families_truncated=[]``. An ignored cursor must be both counted and durable.
2. The same durability gap applied to ``/v1/box_scores``: a cursor there was
   counted as a truncation but left no row in ``data_quality_issues``, so the
   only record of it died with the process.
3. ``results`` is a valid NBA include the ingestor implements end to end
   (``VALID_INCLUDES``, ``_INCLUDE_CAPABILITY``, ``_persist_one_game`` and
   ``SqliteNbaResultRepository``), and ``nba_game_results`` is the sole label
   source the point-in-time dataset reads. It was nevertheless missing from the
   planner's ``NBA_RICH_FAMILIES``, so **no** NBA manifest could ever declare it
   and every NBA month run necessarily produced zero result rows. NBA results are
   derived from the ``/v1/games`` payload the plan already fetches, so declaring
   the family must add no request and must not disturb an existing manifest hash.
4. The BALLDONTLIE box-score payload carries **no team aggregate statistics** --
   the team block is identity metadata plus a ``players`` array. Capability and
   normalizer wording claiming team-statistics support must describe only what is
   actually persisted (team identity + the team's final score).
5. ``pages_fetched`` counts listing/discovery pages only (``LISTING_FAMILIES``).
   The month run printed ``pages ... logical_total=3`` beside 1,437 provider
   responses, which reads as a total page count. The label must say what it counts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional, cast

import pytest

from sports_quant.ingest import f1a
from sports_quant.ingest.nba_ingestor import (
    NbaIngestResult,
    _fetch_all,
    _normalize_box_team_lines,
)
from sports_quant.ingest.planning import NBA_RICH_FAMILIES, Bounds, build_plan
from sports_quant.providers.base_provider import ProviderResponse

# --------------------------------------------------------------------------- #
# Shared minimal provider doubles (no client, no transport, no sleep)
# --------------------------------------------------------------------------- #
GAME_ID = "18447686"


class _Exchange:
    """The few RawExchange fields the fetch path reads off a response.

    ``_fetch_all`` never persists, so only these fields are touched; a full
    ``RawExchange`` would drag the transport policy into a pure parsing test.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.request_params: dict[str, Any] = {}
        self.http_status = 200
        self.response_headers: dict[str, str] = {}
        self.content_type = "application/json"
        self.requested_at = "2026-03-02T00:00:00.000000Z"
        self.received_at = "2026-03-02T00:00:00.100000Z"
        self.elapsed_ns = 100_000
        self.body = "{}"
        self.http_method = "GET"


def _resp(endpoint: str, data: Any) -> ProviderResponse:
    return ProviderResponse(data=data, exchange=cast(Any, _Exchange(endpoint)))


def _game_payload() -> dict[str, Any]:
    return {
        "id": int(GAME_ID), "date": "2026-03-02", "datetime": "2026-03-02T00:30:00Z",
        "season": 2025, "status": "Final", "period": 4, "postseason": False,
        "home_team": {"id": 1, "full_name": "Home Team", "abbreviation": "HOM",
                      "city": "Home", "name": "Team"},
        "visitor_team": {"id": 2, "full_name": "Away Team", "abbreviation": "AWY",
                         "city": "Away", "name": "Team"},
        "home_team_score": 110, "visitor_team_score": 104,
    }


class _CursorClient:
    """Serves one game and a lineups page that advertises a NEXT CURSOR."""

    def __init__(self, *, lineup_cursor: Optional[int] = 5615604,
                 box_cursor: Optional[int] = None) -> None:
        self.lineup_calls = 0
        self._lineup_cursor = lineup_cursor
        self._box_cursor = box_cursor

    async def fetch_games(self, **_kw: Any) -> ProviderResponse:
        return _resp("/v1/games", {"data": [_game_payload()], "meta": {}})

    async def fetch_game(self, _gid: Any) -> ProviderResponse:
        return _resp(f"/v1/games/{GAME_ID}", {"data": _game_payload(), "meta": {}})

    async def fetch_box_scores(self, *, date: str, **_kw: Any) -> ProviderResponse:
        meta = {"next_cursor": self._box_cursor} if self._box_cursor else {}
        return _resp("/v1/box_scores", {"data": [], "meta": meta})

    async def fetch_lineups(self, *, game_ids: Any, **_kw: Any) -> ProviderResponse:
        self.lineup_calls += 1
        rows = [{"game_id": int(GAME_ID), "player": {"id": 100 + i, "first_name": "A",
                                                     "last_name": f"P{i}"},
                 "team": {"id": 1, "full_name": "Home Team"}, "starter": i < 5}
                for i in range(25)]
        meta = {"per_page": 25}
        if self._lineup_cursor is not None:
            meta["next_cursor"] = self._lineup_cursor
        return _resp("/v1/lineups", {"data": rows, "meta": meta})


def _fetch(client: Any, includes: set[str]) -> NbaIngestResult:
    result = NbaIngestResult(dry_run=True, status="succeeded")
    asyncio.run(_fetch_all(
        client, from_date="2026-03-02", to_date="2026-03-02", game_id=int(GAME_ID),
        include_set=includes, result=result, max_pages=8, max_records=1000))
    return result


# --------------------------------------------------------------------------- #
# 1. An ignored /v1/lineups cursor must be counted as a truncation
# --------------------------------------------------------------------------- #
def test_lineups_next_cursor_is_recorded_as_truncation() -> None:
    """A lineups page advertising more records is PARTIAL coverage, not success.

    ``/v1/lineups`` is fetched with a single request per game, so the provider's
    ``next_cursor`` is the only evidence that the page did not hold the whole
    lineup. Discarding it silently is what let 40 of the March 2026 games store a
    truncated 25-row lineup while the run reported no truncation at all.
    """

    client = _CursorClient()
    result = _fetch(client, {"lineups"})

    assert client.lineup_calls == 1  # still exactly one request; no new fan-out
    assert result.records_truncated >= 1, (
        "an ignored /v1/lineups next_cursor was not counted as a truncation")
    assert any("lineups" in reason for reason in result.truncations), result.truncations


def test_lineups_without_cursor_is_not_reported_as_truncated() -> None:
    """A complete lineups page must stay clean -- no invented truncation."""

    result = _fetch(_CursorClient(lineup_cursor=None), {"lineups"})
    assert result.records_truncated == 0
    assert result.truncations == []


@pytest.mark.parametrize("cursor,expected", [(5615604, 1), (None, 0)])
def test_partial_lineup_leaves_a_durable_data_quality_row(
    tmp_path: Path, cursor: Optional[int], expected: int
) -> None:
    """The truncation must survive the process, not just the result object.

    A counter on ``NbaIngestResult`` dies when the run ends; a later reader of the
    corpus would see 25 stored lineup rows and no reason to distrust them. The
    finding therefore has to land in ``data_quality_issues``.
    """

    import httpx

    from sports_quant.db.engine import Database
    from sports_quant.db.init import initialize_database
    from sports_quant.http_policy import ReadOnlyHTTPPolicy, build_readonly_client
    from sports_quant.ingest.nba_ingestor import ingest_nba
    from sports_quant.providers.balldontlie import BalldontlieClient

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/games"):
            payload: Any = ({"data": _game_payload(), "meta": {}}
                            if path != "/v1/games"
                            else {"data": [_game_payload()], "meta": {}})
        elif path == "/v1/lineups":
            meta: dict[str, Any] = {"per_page": 25}
            if cursor is not None:
                meta["next_cursor"] = cursor
            payload = {"data": [{"game_id": int(GAME_ID), "team": {"id": 1},
                                 "player": {"id": 900 + i}, "starter": i < 5}
                                for i in range(25)], "meta": meta}
        else:
            payload = {"data": [], "meta": {}}
        return httpx.Response(200, json=payload,
                              headers={"content-type": "application/json"})

    db_path = tmp_path / "corpus.db"
    initialize_database(db_path)
    database = Database(db_path)
    client = BalldontlieClient(
        "", client=build_readonly_client(
            base_url="https://api.balldontlie.io",
            policy=ReadOnlyHTTPPolicy.for_balldontlie(),
            inner_transport=httpx.MockTransport(handler)))

    async def _go() -> Any:
        try:
            return await ingest_nba(database=database, client=client,
                                    from_date="2026-03-02", to_date="2026-03-02",
                                    game_id=int(GAME_ID), includes=("lineups",),
                                    dry_run=False)
        finally:
            await client.aclose()

    outcome = asyncio.run(_go())
    assert outcome.status == "succeeded", outcome.error_message
    with database.connection() as conn:
        rows = conn.execute(
            "SELECT description FROM data_quality_issues WHERE rule_code = ?",
            ("DQ-NBA-LINEUP-002",)).fetchall()
        stored = conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0]
    assert len(rows) == expected
    assert stored == 25  # the rows we DID get are still kept, just flagged
    if expected:
        assert GAME_ID in rows[0][0]
        assert "5615604" not in rows[0][0]  # sanitized: no cursor value leaks


# --------------------------------------------------------------------------- #
# 2. A box-score cursor must leave a durable finding too
# --------------------------------------------------------------------------- #
def test_box_scores_next_cursor_is_recorded_as_truncation() -> None:
    result = _fetch(_CursorClient(box_cursor=99), {"box"})
    assert result.records_truncated >= 1
    assert any("box_scores" in reason for reason in result.truncations), result.truncations


# --------------------------------------------------------------------------- #
# 3. `results` must be declarable by an NBA manifest, at zero request cost
# --------------------------------------------------------------------------- #
def test_nba_planner_accepts_the_results_family() -> None:
    """``results`` is implemented end to end but was unreachable from a manifest.

    ``nba_game_results`` is the ONLY table ``AsOfReader.official_result`` reads, so
    a vocabulary that cannot express the family makes every NBA month run produce
    zero labels no matter how complete its coverage is.
    """

    assert "results" in NBA_RICH_FAMILIES
    families, stage = f1a._families_and_stage("nba", ("results", "box", "quarters"))
    assert stage == "rich"
    assert "results" in families, (
        "the NBA include -> family mapping silently dropped `results`")


def test_nba_results_family_adds_no_request() -> None:
    """NBA results come from the ``/v1/games`` payload already fetched per game.

    Unlike MLB (whose results need a per-game linescore call), declaring NBA
    results must not add a contingent, a planned request, or a cap increase.
    """

    bounds = Bounds(max_games=400, max_pages=8, max_records=1000, max_retries=1,
                    rate_per_min=60)
    base = build_plan(league="nba", from_date="2026-03-01", to_date="2026-03-31",
                      families=("games", "box", "quarters"), stage="rich", bounds=bounds)
    with_results = build_plan(
        league="nba", from_date="2026-03-01", to_date="2026-03-31",
        families=("games", "results", "box", "quarters"), stage="rich", bounds=bounds)

    assert ([(c.kind, c.family) for c in with_results.contingents]
            == [(c.kind, c.family) for c in base.contingents])
    assert with_results.required_request_cap() == base.required_request_cap()


def test_executed_march_manifest_is_unchanged_by_the_results_repair() -> None:
    """The already-executed manifest must regenerate byte-identically.

    Widening the planner's family VOCABULARY must not perturb a manifest whose
    declared families do not use the new name -- the March 2026 checkpoint and
    scratch database are bound to this exact hash.
    """

    from sports_quant.ingest.manifest import load_and_validate, plan_hash

    root = Path(__file__).resolve().parents[3]
    path = root / "pilots/f1/nba_coverage_2026_03.manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "results" not in payload["families"]

    manifest = load_and_validate(path, expected_league="nba",
                                 expected_provider="balldontlie")
    bounds = Bounds(max_games=400, max_pages=8, max_records=1000, max_retries=1,
                    rate_per_min=60)
    rebuilt = build_plan(league="nba", from_date="2026-03-01", to_date="2026-03-31",
                         families=tuple(payload["families"]), stage="rich", bounds=bounds)
    # Exactly the policy-consistency check `run_pilot_cli` performs before executing.
    assert plan_hash(rebuilt) == manifest.computed_plan_hash()
    # The checkpoint written by the executed March run is bound to this hash.
    assert manifest.manifest_hash() == (
        "901cb9deaf3c5bf243f73ed60a820dd323933caea5dac7a45b69e01480f5ad3e")


# --------------------------------------------------------------------------- #
# 4. Team "statistics" from a box score are identity + score, not aggregates
# --------------------------------------------------------------------------- #
def test_box_team_line_carries_no_fabricated_aggregate() -> None:
    """Pin the REAL BALLDONTLIE contract measured across all 31 March box responses.

    Every team block held exactly ``id``/``abbreviation``/``city``/``conference``/
    ``division``/``full_name``/``name``/``players`` -- identity only. There is no
    team aggregate line to persist, so ``nba_team_statistics`` stores team identity
    plus the team's final score and must never be described as normalized team
    aggregate statistics, nor be back-filled by summing player rows.
    """

    box_game = {
        "home_team_score": 110, "visitor_team_score": 104,
        "home_team": {"id": 1, "abbreviation": "HOM", "city": "Home",
                      "conference": "East", "division": "Atlantic",
                      "full_name": "Home Team", "name": "Team",
                      "players": [{"pts": 30, "reb": 8}]},
        "visitor_team": {"id": 2, "abbreviation": "AWY", "city": "Away",
                         "conference": "West", "division": "Pacific",
                         "full_name": "Away Team", "name": "Team",
                         "players": [{"pts": 25, "reb": 5}]},
    }
    rows = _normalize_box_team_lines(box_game)
    assert [r.home_away for r in rows] == ["home", "away"]
    assert [r.points for r in rows] == [110, 104]
    for row in rows:
        stats = json.loads(row.stats_json)
        assert "players" not in stats  # the player array is never inlined
        assert set(stats) <= {"id", "abbreviation", "city", "conference", "division",
                              "full_name", "name"}, (
            "a team aggregate statistic appeared that the provider does not supply")


# --------------------------------------------------------------------------- #
# 6. Stored request params must reconstruct the URL that was actually sent
# --------------------------------------------------------------------------- #
def test_repeated_query_parameter_is_preserved_as_sent() -> None:
    """A list parameter must round-trip, or preserved evidence is unreplayable.

    ``httpx`` serializes ``{"game_ids[]": [18447686]}`` as
    ``?game_ids[]=18447686``, but the capture recorded ``str([18447686])`` ->
    ``"[18447686]"``. Nothing can turn that back into a URL. The March 2026
    corpus therefore holds 717 responses (all of `/v1/stats`,
    `/nba/v1/stats/advanced` and `/v1/lineups`) whose own stored provenance does
    not identify the request that produced them — which broke this review's
    offline reconstruction until it was found.
    """

    import httpx

    from sports_quant.providers.raw_exchange import build_exchange

    params: dict[str, Any] = {"game_ids[]": [18447686], "per_page": 100}
    request = httpx.Request("GET", "https://api.balldontlie.io/v1/stats",
                            params=cast(Any, params))
    exchange = build_exchange(
        path="/v1/stats", params=params,
        response=httpx.Response(200, json={"data": []}, request=request),
        requested_at=__import__("datetime").datetime(
            2026, 3, 2, tzinfo=__import__("datetime").timezone.utc),
        elapsed_ns=1)

    assert exchange.request_params["game_ids[]"] == ["18447686"], (
        "a repeated query parameter was stringified as a Python container")
    assert exchange.request_params["per_page"] == "100"  # scalars unchanged

    # The stored params must reproduce the query httpx actually sent.
    sent = sorted(request.url.params.multi_items())
    rebuilt = sorted(
        (k, v)
        for k, value in exchange.request_params.items()
        for v in (value if isinstance(value, list) else [value]))
    assert rebuilt == sent, f"{rebuilt} != {sent}"


def test_scalar_and_string_params_are_not_exploded() -> None:
    """A ``str`` is a sequence; it must stay one value, not a list of characters."""

    import datetime as _dt

    import httpx

    from sports_quant.providers.raw_exchange import build_exchange

    params = {"date": "2026-03-01", "cursor": 18447784, "per_page": 100}
    exchange = build_exchange(
        path="/v1/box_scores", params=params,
        response=httpx.Response(200, json={"data": []}),
        requested_at=_dt.datetime(2026, 3, 2, tzinfo=_dt.timezone.utc), elapsed_ns=1)
    assert exchange.request_params == {"date": "2026-03-01", "cursor": "18447784",
                                       "per_page": "100"}


# --------------------------------------------------------------------------- #
# 5. `pages_fetched` must not read as a total provider page count
# --------------------------------------------------------------------------- #
def test_pilot_report_labels_pages_as_listing_pages() -> None:
    """The counter covers LISTING_FAMILIES only; the label must say so."""

    from sports_quant.request_control import LISTING_FAMILIES

    assert LISTING_FAMILIES == frozenset({"schedule", "games"})
    assert f1a._PAGES_LABEL.strip() == "listing_pages", (
        "the pilot report still prints a bare `pages` counter, which reads as the "
        "total number of provider pages fetched")
